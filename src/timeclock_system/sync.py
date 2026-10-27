"""Mirror PostgreSQL into the local store and push this clock's records, through the backend's relay (spec §6.6).

One task owns one connection. Any database or network error drops it and the loop starts over from connecting, except
the database refusing one pushed record: that record is alerted, kept and retried, and the push goes on (see _push).
"""

# Standard library imports
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, LiteralString
from uuid import UUID

# Third party imports
import psycopg
from psycopg.rows import DictRow, dict_row
from timeclock_schema.models import BusinessHours, Employee, Fingerprint, Group, Punch, PunchCorrection, RuleRow, Settings
from timeclock_schema.wire import NOTIFY_CHANNEL, Notification

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable

  # Third party imports
  from psycopg import AsyncConnection
  from timeclock_schema.base import TimeclockModel

  # First party imports
  from timeclock_system.clock import ChangeTable, Clock
  from timeclock_system.store import LocalStore, MirrorTable

logger = logging.getLogger(__name__)

# Explicit column lists: the clocks can't `SELECT *` on overrides, and elsewhere a list keeps `extra="ignore"` meaningful.
_SELECT: Final[dict[str, LiteralString]] = {
  "employees": "SELECT id, name, pin_username, pin_hash, is_manager, active, group_id, updated_at FROM employees",
  "groups": "SELECT id, name FROM groups",
  "rules": "SELECT id, kind, params, group_id, employee_id, on_violation, updated_at FROM rules",
  "config.business_hours": "SELECT id, weekday, opens, closes FROM config.business_hours",
  "config.settings": (
    "SELECT auto_clock_out_enabled, auto_clock_out_run_time, auto_clock_out_stamp_time, subtract_lunch, subtract_breaks "
    "FROM config.settings"
  ),
  "punches": "SELECT id, employee_id, kind, at, instance_id, automatic, received_at, db_login FROM punches",
  "punch_corrections": "SELECT id, punch_id, action, new_at, new_kind, note, recorded_at FROM punch_corrections",
  "fingerprints": "SELECT id, employee_id, template, registered_at, instance_id, db_login FROM fingerprints",
}
# Every employee's punches before the window with no clock-out at or after them: an open shift's tail, however old, so a
# shift begun at another clock while this one was offline across Monday 00:00 still offers a clock-out (§6.6 step 3).
_OPEN_TAILS: Final = (
  _SELECT["punches"] + " p WHERE at < %(since)s AND NOT EXISTS (SELECT 1 FROM punches o WHERE o.employee_id = p.employee_id "
  "AND o.kind = 'clock_out' AND o.at >= p.at AND o.at < %(since)s)"
)
# The small tables a notification updates row by row; business hours and settings are re-fetched whole.
_ROW_MIRRORED: Final[dict[MirrorTable, type[TimeclockModel]]] = {"employees": Employee, "groups": Group, "rules": RuleRow}
_CHANGE_NAME: Final[dict[str, ChangeTable]] = {
  "employees": "employees",
  "groups": "groups",
  "rules": "rules",
  "fingerprints": "fingerprints",
  "punches": "punches",
  "punch_corrections": "punch_corrections",
  "config.business_hours": "business_hours",
  "config.settings": "settings",
}

# Never name db_login, received_at or automatic: the clocks' column grants refuse them.
_INSERT_PUNCH: Final = (
  "INSERT INTO punches (id, employee_id, kind, at, instance_id) "
  "VALUES (%(id)s, %(employee_id)s, %(kind)s::punch_kind, %(at)s, %(instance_id)s) ON CONFLICT (id) DO NOTHING"
)
_INSERT_OVERRIDE: Final = (
  "INSERT INTO overrides (id, punch_id, employee_id, manager_id, reason, pin_only, at, instance_id) "
  "VALUES (%(id)s, %(punch_id)s, %(employee_id)s, %(manager_id)s, %(reason)s::override_reason, %(pin_only)s, %(at)s, "
  "%(instance_id)s) ON CONFLICT (id) DO NOTHING"
)
_INSERT_FINGERPRINT: Final = (
  "INSERT INTO fingerprints (id, employee_id, template, registered_at, instance_id) "
  "VALUES (%(id)s, %(employee_id)s, %(template)s, %(registered_at)s, %(instance_id)s) ON CONFLICT (id) DO NOTHING"
)

_BACKOFF_START, _BACKOFF_MAX = 1.0, 60.0
_PING_AFTER_SECS = 30.0
_LAST_CONTACT_KEY: Final = "last_contact"


@dataclass(frozen=True)
class DbStatus:
  """The idle screen's database indicator (§6.4)."""

  connected: bool
  last_contact: datetime | None
  """The last successful exchange with the database."""


class DatabaseSync:
  """Keeps the local store in step with PostgreSQL and pushes local records, forever."""

  def __init__(
    self,
    clock: Clock,
    store: LocalStore,
    conninfo: str,
    *,
    alert: Callable[[str, str], None],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
  ) -> None:
    """Create the sync; `run` starts it.

    Args:
      clock: Told which tables changed after every catch-up and notification.
      store: The local store it mirrors into and pushes from.
      conninfo: libpq connection string for the backend's relay, with its keepalives (`ClockSettings.conninfo`).
      alert: Sends an alert (reason, details) without waiting; told once about each record the database refuses.
      now: The wall clock, injectable for tests.
    """
    self._clock = clock
    self._alert = alert
    self._refused: set[UUID] = set()  # records already alerted
    self._store = store
    self._conninfo = conninfo
    self._now = now
    saved = store.get_meta(_LAST_CONTACT_KEY)  # one small read at startup, before the loop has work
    self._status = DbStatus(connected=False, last_contact=None if saved is None else datetime.fromisoformat(saved))
    self._listeners: list[Callable[[DbStatus], None]] = []
    self._wake = asyncio.Event()
    self._since: datetime | None = None
    self._last_exchange = 0.0

  @property
  def status(self) -> DbStatus:
    """Whether the database is reachable now, and when it last answered."""
    return self._status

  def add_status_listener(self, cb: Callable[[DbStatus], None]) -> None:
    """Call `cb` with the new status whenever `connected` flips."""
    self._listeners.append(cb)

  def wake(self) -> None:
    """A local record is waiting: push it soon."""
    self._wake.set()

  def _set_connected(self, *, connected: bool) -> None:
    if connected == self._status.connected:
      return
    self._status = DbStatus(connected=connected, last_contact=self._status.last_contact)
    for cb in self._listeners:
      cb(self._status)

  async def _contacted(self) -> None:
    now = self._now()
    self._last_exchange = time.monotonic()
    self._status = DbStatus(connected=self._status.connected, last_contact=now)
    await asyncio.to_thread(self._store.set_meta, _LAST_CONTACT_KEY, now.isoformat())

  async def run(self) -> None:
    """Connect, catch up, push and listen; on any database or network error, back off and start over."""
    backoff = _BACKOFF_START
    while True:
      try:
        async with await psycopg.AsyncConnection[DictRow].connect(self._conninfo, autocommit=True, row_factory=dict_row) as conn:
          await self._session(conn)
      except (psycopg.Error, OSError, TimeoutError) as e:
        if self._status.connected:
          logger.warning("database connection lost: %s", e)
          backoff = _BACKOFF_START  # a drop after a working session retries from the start
        else:
          logger.debug("database connect failed: %s", e)
      self._set_connected(connected=False)
      await asyncio.sleep(backoff)
      backoff = min(backoff * 2, _BACKOFF_MAX)

  async def _session(self, conn: AsyncConnection[DictRow]) -> None:
    await conn.execute(f"LISTEN {NOTIFY_CHANNEL}")
    await self._catch_up(conn)
    self._set_connected(connected=True)
    await self._contacted()
    self._wake.clear()
    await self._push(conn)
    while True:
      # No query may run while the generator is open, so collect first and apply afterwards.
      pending = [n.payload async for n in conn.notifies(timeout=1.0, stop_after=50)]
      for payload in pending:
        await self._apply(conn, payload)
      if pending:
        await self._contacted()
      if self._wake.is_set():
        self._wake.clear()
        await self._push(conn)
        await self._contacted()
      if time.monotonic() - self._last_exchange >= _PING_AFTER_SECS:
        await conn.execute("SELECT 1")
        await self._contacted()

  async def _fetch[M: TimeclockModel](
    self, conn: AsyncConnection[DictRow], model: type[M], query: LiteralString, **params: object
  ) -> list[M]:
    cur = await conn.execute(query, params or None)
    return [model.model_validate(row) for row in await cur.fetchall()]

  async def _catch_up(self, conn: AsyncConnection[DictRow]) -> None:
    """Fetch the local window and every small table, then replace the local copies (§6.6 step 3)."""
    since = await self._clock.catch_up_since()
    punches = await self._fetch(conn, Punch, _SELECT["punches"] + " WHERE at >= %(since)s", since=since)
    open_tails = await self._fetch(conn, Punch, _OPEN_TAILS, since=since)
    corrections = await self._fetch(
      conn,
      PunchCorrection,
      _SELECT["punch_corrections"] + " WHERE punch_id IN (SELECT id FROM punches WHERE at >= %(since)s) OR punch_id = ANY(%(tails)s)",
      since=since,
      tails=[t.id for t in open_tails],
    )
    small: dict[MirrorTable, list[TimeclockModel]] = {
      table: await self._fetch(conn, model, _SELECT[table]) for table, model in _ROW_MIRRORED.items()
    }
    hours = await self._fetch(conn, BusinessHours, _SELECT["config.business_hours"])
    fingerprints = await self._fetch(conn, Fingerprint, _SELECT["fingerprints"])
    settings = await self._fetch(conn, Settings, _SELECT["config.settings"])

    def store_all() -> None:
      # The tails are only upserted: outside the window, so never deleted here. Once stored they're open shifts the
      # clock holds, so the next catch_up_since reaches back to them and owner deletes land then.
      self._store.replace_db_punches([*open_tails, *punches], since=since)
      self._store.replace_corrections(corrections)
      for table, rows in small.items():
        self._store.replace_table(table, rows)
      self._store.replace_table("business_hours", hours)
      self._store.replace_db_fingerprints(fingerprints)
      self._store.set_settings(settings[0] if settings else Settings())

    await asyncio.to_thread(store_all)
    self._since = since
    self._clock.notify_changed(_CHANGE_NAME.values())

  def _refused_record(self, kind: str, record_id: UUID, summary: str, e: psycopg.Error) -> None:
    # One alert per record: it stays unsynced and is retried on every push, so the owner can correct it at the clock
    # (an employee is only ever disabled, so a refusal means a mistaken delete or a bug).
    logger.warning("the database refused %s %s: %s", kind, record_id, e)
    if record_id not in self._refused:
      self._refused.add(record_id)
      self._alert(f"Database refused a {kind}", f"{kind} {record_id} ({summary}) stays on this clock and is retried: {e}")

  async def _push(self, conn: AsyncConnection[DictRow]) -> None:
    """Push unsynced records oldest first, one transaction per punch with its override (§6.6 step 4, R38).

    A record the database refuses doesn't hold back the ones after it: it's alerted, kept unsynced and retried next push.
    """
    for punch, override in await asyncio.to_thread(self._store.unsynced_punches):
      try:
        async with conn.transaction():
          await conn.execute(
            _INSERT_PUNCH,
            {
              "id": punch.id,
              "employee_id": punch.employee_id,
              "kind": punch.kind.value,
              "at": punch.at,
              "instance_id": punch.instance_id,
            },
          )
          if override is not None:
            params = override.model_dump(include={"id", "punch_id", "employee_id", "manager_id", "pin_only", "at", "instance_id"})
            await conn.execute(_INSERT_OVERRIDE, {**params, "reason": override.reason.value})
      except (psycopg.errors.IntegrityError, psycopg.errors.DataError) as e:
        self._refused_record("punch", punch.id, f"employee {punch.employee_id}, {punch.kind.value} at {punch.at}", e)
        continue
      await asyncio.to_thread(self._store.mark_punch_synced, punch.id)
    for f in await asyncio.to_thread(self._store.unsynced_fingerprints):
      try:
        await conn.execute(
          _INSERT_FINGERPRINT,
          {
            "id": f.id,
            "employee_id": f.employee_id,
            "template": f.template.get_secret_value(),
            "registered_at": f.registered_at,
            "instance_id": f.instance_id,
          },
        )
      except (psycopg.errors.IntegrityError, psycopg.errors.DataError) as e:
        self._refused_record("fingerprint", f.id, f"employee {f.employee_id}", e)
        continue
      await asyncio.to_thread(self._store.mark_fingerprint_synced, f.id)

  async def _apply(self, conn: AsyncConnection[DictRow], payload: str) -> None:
    """Apply one notification (§4.4): re-read what it names and mirror it; a row that's gone is deleted locally."""
    n = Notification.model_validate_json(payload)
    change = _CHANGE_NAME.get(n.table)
    if change is None:
      return  # a newer migration's table; this clock doesn't mirror it
    if n.table == "config.business_hours":
      hours = await self._fetch(conn, BusinessHours, _SELECT[n.table])
      await asyncio.to_thread(self._store.replace_table, "business_hours", hours)
    elif n.table == "config.settings":
      settings = await self._fetch(conn, Settings, _SELECT[n.table])
      await asyncio.to_thread(self._store.set_settings, settings[0] if settings else Settings())
    elif not await self._apply_row(conn, n.table, n.id):
      return
    self._clock.notify_changed({change})

  async def _apply_row(self, conn: AsyncConnection[DictRow], table: str, row_id: str) -> bool:
    """Mirror or delete the one row a notification names; False if it's ignored. Split out of `_apply` for C901."""
    by_id = _SELECT[table] + " WHERE id = %(id)s"
    if table in _ROW_MIRRORED:
      rows = await self._fetch(conn, _ROW_MIRRORED[table], by_id, id=int(row_id))
      await asyncio.to_thread(lambda: self._store.upsert(table, rows[0]) if rows else self._store.delete(table, int(row_id)))
    elif table == "punches":
      punches = await self._fetch(conn, Punch, by_id, id=UUID(row_id))
      if not punches:
        await asyncio.to_thread(self._store.delete_punch, UUID(row_id))
        return True
      punch = punches[0]
      stored = any(p.id == punch.id for p in await asyncio.to_thread(self._store.punches, punch.employee_id))
      if self._since is not None and punch.at < self._since and not stored:
        return False  # older than the window and not held here: it touches no open shift the clock knows (§6.6)
      await asyncio.to_thread(self._store.upsert_db_punch, punch)
    elif table == "punch_corrections":
      corrections = await self._fetch(conn, PunchCorrection, by_id, id=UUID(row_id))
      if corrections:
        await asyncio.to_thread(self._store.upsert_correction, corrections[0])
      else:
        await asyncio.to_thread(self._store.delete_correction, UUID(row_id))
    else:  # fingerprints
      fingerprints = await self._fetch(conn, Fingerprint, by_id, id=UUID(row_id))
      if fingerprints:
        await asyncio.to_thread(self._store.upsert_db_fingerprint, fingerprints[0])
      else:
        await asyncio.to_thread(self._store.delete_fingerprint, UUID(row_id))
    return True
