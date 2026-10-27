"""DatabaseSync against a real PostgreSQL 18 (spec §6.6).

Needs the throwaway server from the plan's Global Constraints (PowerShell):

    docker run --rm -d --name timeclock-clock-pg -e POSTGRES_PASSWORD=test -p 127.0.0.1:55433:5432 postgres:18
    $env:TIMECLOCK_TEST_PG_DSN = "postgresql://postgres:test@127.0.0.1:55433/postgres"

Without the variable every test here skips. Each test gets its own freshly migrated database and clock login.
"""

# Standard library imports
import asyncio
import contextlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, LiteralString, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

# Third party imports
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from timeclock_schema.enums import EmployeeRole, OverrideReason, PunchKind
from timeclock_schema.migrations import load_migrations
from timeclock_schema.models import Override, Punch
from timeclock_schema.shifts import EmployeeState

# First party imports
from timeclock_system.clock import Clock
from timeclock_system.store import LocalStore
from timeclock_system.sync import DatabaseSync, DbStatus

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
  from pathlib import Path

pytestmark = pytest.mark.skipif(
  not os.environ.get("TIMECLOCK_TEST_PG_DSN"), reason="needs TIMECLOCK_TEST_PG_DSN (see module docstring)"
)

WAIT = 5.0


@dataclass(frozen=True)
class Pg:
  admin_dsn: str
  """The test database as `postgres`, the owner's stand-in."""
  clock_dsn: str
  role: str


@pytest.fixture
def pg() -> Iterator[Pg]:
  server = os.environ["TIMECLOCK_TEST_PG_DSN"]
  hex_ = uuid4().hex
  name, role = f"tcc_{hex_}", f"tcc_clock_{hex_}"
  with psycopg.connect(server, autocommit=True) as admin:
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
  try:
    admin_dsn = make_conninfo(server, dbname=name)
    with psycopg.connect(admin_dsn) as conn:
      for m in load_migrations():
        with conn.transaction():
          conn.execute(cast("LiteralString", m.sql))  # package data, not user input
      with conn.transaction():
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'pw' IN ROLE timeclock_clock_access").format(sql.Identifier(role)))
    yield Pg(admin_dsn, make_conninfo(server, dbname=name, user=role, password="pw"), role)
  finally:
    with psycopg.connect(server, autocommit=True) as admin:
      admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
      admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


async def admin_execute(pg: Pg, query: LiteralString, params: dict[str, Any] | None = None) -> list[tuple[Any, ...]]:
  async with await psycopg.AsyncConnection.connect(pg.admin_dsn, autocommit=True) as conn:
    cur = await conn.execute(query, params)
    return await cur.fetchall() if cur.description else []


async def seed_employee(pg: Pg, name: str = "Ann", *, pin_username: str = "1001", role: EmployeeRole = EmployeeRole.EMPLOYEE) -> int:
  rows = await admin_execute(
    pg,
    "INSERT INTO employees (name, pin_username, pin_hash, role) VALUES (%(n)s, %(u)s, crypt('1234', gen_salt('bf')), %(r)s::employee_role) "
    "RETURNING id",
    {"n": name, "u": pin_username, "r": role.value},
  )
  return rows[0][0]


async def eventually(predicate: Callable[[], bool | Awaitable[bool]], what: str) -> None:
  """Poll `predicate` for up to WAIT seconds."""
  deadline = asyncio.get_running_loop().time() + WAIT
  while True:
    result = predicate()
    if result if isinstance(result, bool) else await result:
      return
    if asyncio.get_running_loop().time() > deadline:
      pytest.fail(f"timed out waiting for {what}")
    await asyncio.sleep(0.05)


@dataclass
class Rig:
  pg: Pg
  path: Path
  store: LocalStore
  clock: Clock
  sync: DatabaseSync
  statuses: list[bool]
  alerts: list[tuple[str, str]]


@pytest.fixture
async def rig(pg: Pg, tmp_path: Path) -> AsyncIterator[Callable[[], Awaitable[Rig]]]:
  """Yields a starter: each call builds a DatabaseSync over a fresh store and returns once it has connected."""
  tasks: list[asyncio.Task[None]] = []
  stores: list[LocalStore] = []

  async def start() -> Rig:
    path = tmp_path / "clock.sqlite3"
    store = LocalStore(path)
    stores.append(store)
    clock = Clock(store, instance_id="1", tz=ZoneInfo("America/New_York"), alert=lambda _s, _b: None)
    await clock.load()
    alerts: list[tuple[str, str]] = []
    sync = DatabaseSync(clock, store, pg.clock_dsn, alert=lambda reason, details: alerts.append((reason, details)))
    statuses: list[bool] = []
    sync.add_status_listener(lambda s: statuses.append(s.connected))
    tasks.append(asyncio.create_task(sync.run()))
    await eventually(lambda: sync.status.connected, "the first connect")
    return Rig(pg, path, store, clock, sync, statuses, alerts)

  yield start
  for task in tasks:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
      await task
  for store in stores:
    store.close()


def local_punch(employee_id: int, kind: PunchKind = PunchKind.CLOCK_IN) -> Punch:
  return Punch(id=uuid4(), employee_id=employee_id, kind=kind, at=datetime.now(UTC), instance_id="1")


def origin_of(rig: Rig, punch: Punch) -> str | None:
  with contextlib.closing(sqlite3.connect(rig.path)) as conn:
    row = conn.execute("SELECT origin FROM punches WHERE id = ?", (str(punch.id),)).fetchone()
  return None if row is None else row[0]


async def test_catch_up_mirrors_employees_and_settings(pg: Pg, rig: Callable[[], Awaitable[Rig]]) -> None:
  ann = await seed_employee(pg, role=EmployeeRole.ADMIN)
  await admin_execute(pg, "UPDATE config.settings SET subtract_breaks = true, lunches_enabled = false")
  r = await rig()
  employee = r.clock.employee(ann)
  assert employee is not None
  assert employee.name == "Ann"
  assert employee.role is EmployeeRole.ADMIN
  assert r.clock.settings().subtract_breaks is True
  assert r.clock.settings().lunches_enabled is False
  assert r.clock.settings().breaks_enabled is True
  assert r.sync.status.last_contact is not None


async def test_pushes_a_local_punch_with_its_override(pg: Pg, rig: Callable[[], Awaitable[Rig]]) -> None:
  ann, mo = await seed_employee(pg), await seed_employee(pg, "Mo", pin_username="1002", role=EmployeeRole.MANAGER)
  r = await rig()
  punch = local_punch(ann)
  override = Override(
    id=uuid4(),
    punch_id=punch.id,
    employee_id=ann,
    manager_id=mo,
    reason=OverrideReason.NO_FINGERPRINT,
    pin_only=True,
    at=punch.at,
    instance_id="1",
  )
  r.store.insert_punch(punch, origin="local", override=override)
  r.sync.wake()

  async def pushed() -> bool:
    return bool(await admin_execute(pg, "SELECT 1 FROM punches WHERE id = %(id)s", {"id": punch.id}))

  await eventually(pushed, "the punch to reach PostgreSQL")
  assert await admin_execute(pg, "SELECT db_login FROM punches WHERE id = %(id)s", {"id": punch.id}) == [(pg.role,)]
  overrides = await admin_execute(
    pg, "SELECT manager_id, reason::text, pin_only FROM overrides WHERE punch_id = %(id)s", {"id": punch.id}
  )
  assert overrides == [(mo, "no_fingerprint", True)]
  await eventually(lambda: r.store.unsynced_punches() == [], "the local row to be marked synced")


async def test_push_is_idempotent(pg: Pg, rig: Callable[[], Awaitable[Rig]]) -> None:
  # Review Focus 2: a re-push after a lost acknowledgement inserts nothing new.
  ann = await seed_employee(pg)
  r = await rig()
  punch = local_punch(ann)
  r.store.insert_punch(punch, origin="local")
  r.sync.wake()
  await eventually(lambda: r.store.unsynced_punches() == [], "the first push")
  with contextlib.closing(sqlite3.connect(r.path)) as conn, conn:
    conn.execute("UPDATE punches SET synced = 0")
  r.sync.wake()
  await eventually(lambda: r.store.unsynced_punches() == [], "the re-push")
  assert await admin_execute(pg, "SELECT count(*) FROM punches") == [(1,)]


async def test_a_refused_punch_doesnt_hold_back_later_ones(pg: Pg, rig: Callable[[], Awaitable[Rig]]) -> None:
  # An employee deleted on the server (by mistake: they're only ever disabled) while this clock held their punch.
  ann = await seed_employee(pg)
  r = await rig()
  refused = local_punch(ann + 1000)
  later = local_punch(ann)
  r.store.insert_punch(refused, origin="local")
  r.store.insert_punch(later, origin="local")
  r.sync.wake()
  await eventually(lambda: [p.id for p, _ in r.store.unsynced_punches()] == [refused.id], "the later punch's push")
  last = local_punch(ann, PunchKind.CLOCK_OUT)
  r.store.insert_punch(last, origin="local")
  r.sync.wake()
  await eventually(lambda: [p.id for p, _ in r.store.unsynced_punches()] == [refused.id], "the next push, retrying the refused")
  assert await admin_execute(pg, "SELECT count(*) FROM punches") == [(2,)]
  [(reason, details)] = r.alerts  # once, though the refused punch was retried
  assert "refused" in reason
  assert str(refused.id) in details
  assert r.sync.status.connected


async def test_reconnects_after_the_connection_is_killed(pg: Pg, rig: Callable[[], Awaitable[Rig]]) -> None:
  # Review Focus 2: a punch recorded while the connection is down arrives once it's back.
  ann = await seed_employee(pg)
  r = await rig()
  await admin_execute(pg, "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %(role)s", {"role": pg.role})
  punch = local_punch(ann)
  r.store.insert_punch(punch, origin="local")
  r.sync.wake()

  async def pushed() -> bool:
    return bool(await admin_execute(pg, "SELECT 1 FROM punches WHERE id = %(id)s", {"id": punch.id}))

  await eventually(pushed, "the punch to arrive after the reconnect")
  assert r.statuses[:3] == [True, False, True]


async def test_catch_up_fetches_open_shifts_from_before_the_week(pg: Pg, rig: Callable[[], Awaitable[Rig]]) -> None:
  # A shift begun at another clock while this one was offline across Monday 00:00 must still be offered a clock-out.
  ann = await seed_employee(pg)
  bob = await seed_employee(pg, "Bob", pin_username="1002")
  old = datetime.now(UTC) - timedelta(days=10)  # before this week's start, whatever today is
  insert = "INSERT INTO punches (id, employee_id, kind, at, instance_id) VALUES (%(id)s, %(e)s, %(kind)s::punch_kind, %(at)s, '2')"
  ann_in = uuid4()
  await admin_execute(pg, insert, {"id": ann_in, "e": ann, "kind": "clock_in", "at": old})
  await admin_execute(pg, insert, {"id": uuid4(), "e": bob, "kind": "clock_in", "at": old})
  await admin_execute(pg, insert, {"id": uuid4(), "e": bob, "kind": "clock_out", "at": old + timedelta(hours=8)})
  r = await rig()
  assert [p.id for p in r.store.punches(ann)] == [ann_in]
  assert r.store.punches(bob) == []  # a closed shift from before the week stays on the server
  assert (await r.clock.menu_info(ann)).state is not EmployeeState.OFF


async def test_notification_inserts_another_clocks_punch(pg: Pg, rig: Callable[[], Awaitable[Rig]]) -> None:
  ann = await seed_employee(pg)
  r = await rig()
  punch = Punch(id=uuid4(), employee_id=ann, kind=PunchKind.CLOCK_IN, at=datetime.now(UTC), instance_id="2")
  await admin_execute(
    pg,
    "INSERT INTO punches (id, employee_id, kind, at, instance_id) VALUES (%(id)s, %(e)s, 'clock_in', %(at)s, '2')",
    {"id": punch.id, "e": ann, "at": punch.at},
  )
  await eventually(lambda: origin_of(r, punch) is not None, "the other clock's punch to arrive")
  assert origin_of(r, punch) == "db"


async def test_echo_of_own_punch_keeps_one_row(pg: Pg, rig: Callable[[], Awaitable[Rig]]) -> None:
  # Review Focus 4: the notification for the clock's own pushed punch updates its row; it doesn't add one.
  ann = await seed_employee(pg)
  r = await rig()
  punch = local_punch(ann)
  r.store.insert_punch(punch, origin="local")
  r.sync.wake()
  # Only the echo fills in db_login, which the clock never sends.
  await eventually(lambda: [p.db_login for p in r.store.punches()] == [pg.role], "the echo to be applied")
  assert len(r.store.punches()) == 1
  assert origin_of(r, punch) == "local"


async def test_owner_delete_removes_the_local_row(pg: Pg, rig: Callable[[], Awaitable[Rig]]) -> None:
  # Review Focus 4.
  ann = await seed_employee(pg)
  r = await rig()
  punch = local_punch(ann)
  r.store.insert_punch(punch, origin="local")
  r.sync.wake()
  await eventually(lambda: r.store.unsynced_punches() == [], "the push")
  await admin_execute(pg, "DELETE FROM punches WHERE id = %(id)s", {"id": punch.id})
  await eventually(lambda: r.store.punches() == [], "the local row to be deleted")


async def test_owner_update_of_settings_refetches(pg: Pg, rig: Callable[[], Awaitable[Rig]]) -> None:
  r = await rig()
  assert r.clock.settings().subtract_breaks is False
  await admin_execute(pg, "UPDATE config.settings SET subtract_breaks = true")
  await eventually(lambda: r.clock.settings().subtract_breaks, "the settings to be re-fetched")


async def test_notification_for_unknown_table_is_ignored(pg: Pg, rig: Callable[[], Awaitable[Rig]]) -> None:
  # A newer migration's table reaches clocks that don't know it yet.
  r = await rig()
  await admin_execute(pg, """NOTIFY timeclock, '{"table": "future_table", "op": "INSERT", "id": "1"}'""")
  await admin_execute(pg, "UPDATE config.settings SET subtract_breaks = true")
  await eventually(lambda: r.clock.settings().subtract_breaks, "the notification after the unknown one")
  assert r.statuses == [True], "the unknown table neither raised nor dropped the connection"


async def test_last_contact_survives_a_restart(pg: Pg, rig: Callable[[], Awaitable[Rig]]) -> None:
  r = await rig()
  saved = r.sync.status.last_contact
  assert saved is not None
  assert DatabaseSync(r.clock, r.store, pg.clock_dsn, alert=lambda _r, _d: None).status == DbStatus(
    connected=False, last_contact=saved
  )
