"""The clock's SQLite mirror of the PostgreSQL tables it reads, plus its unsynced queue and weekly prune (spec §6.5).

Synchronous and guarded by one lock; callers on the event loop wrap calls in `asyncio.to_thread`.

Employees added at this clock wait in `pending_employees` until the database accepts them (§6.6). While one is pending,
its records stay out of the push queues, and rows with its id that didn't come from this clock (the database's holder of
a refused number) are hidden from `punches`, `corrections` and `fingerprints`, so they're never filed under it here.
"""

# Standard library imports
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID

# Third party imports
from timeclock_schema.base import REVEAL, TimeclockModel
from timeclock_schema.models import (
  BusinessHours,
  Employee,
  Fingerprint,
  FingerprintBypass,
  FingerprintChange,
  Group,
  Punch,
  PunchCorrection,
  RuleRow,
  Settings,
)

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Mapping, Sequence
  from pathlib import Path

logger = logging.getLogger(__name__)

Origin = Literal["local", "peer", "db"]
"""Where a row came from: recorded here, received over the LAN, or read from the database."""
MirrorTable = Literal["employees", "groups", "rules", "business_hours"]

_MODELS: dict[str, type[TimeclockModel]] = {
  "employees": Employee,
  "groups": Group,
  "rules": RuleRow,
  "business_hours": BusinessHours,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (id INTEGER PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS groups (id INTEGER PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rules (id INTEGER PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS business_hours (id INTEGER PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK (id = 1), json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS punches (
  id TEXT PRIMARY KEY, employee_id INTEGER NOT NULL, at TEXT NOT NULL,
  origin TEXT NOT NULL, synced INTEGER NOT NULL, json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS punches_at ON punches (at);
-- punch_id is null for a login that punched nothing; `at` lets the prune drop those once synced.
CREATE TABLE IF NOT EXISTS fingerprint_bypasses (
  id TEXT PRIMARY KEY, punch_id TEXT, at TEXT NOT NULL, synced INTEGER NOT NULL, json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fingerprint_bypasses_punch ON fingerprint_bypasses (punch_id);
CREATE TABLE IF NOT EXISTS pending_employees (id INTEGER PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS punch_corrections (id TEXT PRIMARY KEY, punch_id TEXT NOT NULL, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fingerprints (
  id TEXT PRIMARY KEY, employee_id INTEGER NOT NULL, origin TEXT NOT NULL, synced INTEGER NOT NULL, json TEXT NOT NULL
);
-- Prints removed here that the database may still hold. `replaced_by` null: a removal pushed on its own; else the new
-- print of a re-register, pushed in that print's transaction. Catch-up and notifications never bring one back.
CREATE TABLE IF NOT EXISTS fingerprint_removals (id TEXT PRIMARY KEY, replaced_by TEXT);
-- The change log's unpushed rows; a pushed row is deleted.
CREATE TABLE IF NOT EXISTS fingerprint_changes (id TEXT PRIMARY KEY, employee_id INTEGER NOT NULL, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


# A pending employee's id: rows not made here are hidden, and records made here wait for it (module docstring).
_VISIBLE = "(origin = 'local' OR employee_id NOT IN (SELECT id FROM pending_employees))"
_HELD = "employee_id NOT IN (SELECT id FROM pending_employees)"


def _dump(model: TimeclockModel) -> str:
  return model.model_dump_json(context=REVEAL)


def _iso(at: datetime) -> str:
  # Fixed-width UTC text (models are already aware UTC), so string order is time order.
  return at.astimezone(UTC).isoformat(timespec="microseconds")


class LocalStore:
  """SQLite store: mirrored server tables, local/peer/db-origin punches and fingerprints, and the unsynced queue."""

  def __init__(self, path: Path) -> None:
    """Open (creating if needed) the database at `path`.

    Args:
      path: The SQLite file.
    """
    # foreign_keys stays off: a punch's correction can arrive before the punch itself.
    self._conn = sqlite3.connect(path, check_same_thread=False)
    self._lock = threading.Lock()
    with self._lock:
      self._conn.execute("PRAGMA journal_mode=WAL")
      self._conn.execute("PRAGMA foreign_keys=OFF")
      self._conn.executescript(_SCHEMA)
      with self._conn:
        # Stored before prints named their finger; they no longer load. The database refused to migrate with any.
        dropped = self._conn.execute("DELETE FROM fingerprints WHERE json_extract(json, '$.finger') IS NULL").rowcount
    if dropped:
      logger.warning("dropped %d local fingerprints stored without a finger", dropped)

  def close(self) -> None:
    """Close the connection."""
    with self._lock:
      self._conn.close()

  # -- mirrored tables ----------------------------------------------------------------------------------------------

  def replace_table(self, table: MirrorTable, rows: Sequence[TimeclockModel]) -> None:
    """Replace every row of a mirrored table in one transaction."""
    with self._lock, self._conn:
      self._conn.execute(f"DELETE FROM {table}")
      self._conn.executemany(
        f"INSERT INTO {table} (id, json) VALUES (?, ?)",
        [(row.id, _dump(row)) for row in rows],  # pyright: ignore[reportAttributeAccessIssue]
      )

  def upsert(self, table: MirrorTable, row: TimeclockModel) -> None:
    """Insert or replace one row of a mirrored table."""
    with self._lock, self._conn:
      self._conn.execute(f"INSERT OR REPLACE INTO {table} (id, json) VALUES (?, ?)", (row.id, _dump(row)))  # pyright: ignore[reportAttributeAccessIssue]

  def delete(self, table: MirrorTable, row_id: int) -> None:
    """Delete one row of a mirrored table by id."""
    with self._lock, self._conn:
      self._conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))

  def replace_employees(self, rows: Sequence[Employee]) -> None:
    """Replace the employee mirror, settling each pending employee the mirror now holds with the same `pin_hash`.

    That's an insert whose acknowledgement was lost (§6.6): the database has the row, the clock never heard back.
    """
    fetched = {e.id: e.pin_hash.get_secret_value() for e in rows}
    with self._lock, self._conn:
      self._conn.execute("DELETE FROM employees")
      self._conn.executemany("INSERT INTO employees (id, json) VALUES (?, ?)", [(e.id, _dump(e)) for e in rows])
      for pid, json in self._conn.execute("SELECT id, json FROM pending_employees").fetchall():
        if fetched.get(pid) == Employee.model_validate_json(json).pin_hash.get_secret_value():
          self._conn.execute("DELETE FROM pending_employees WHERE id = ?", (pid,))

  def insert_pending_employee(self, e: Employee) -> None:
    """Store an employee added at this clock, unsynced until the database accepts it."""
    with self._lock, self._conn:
      self._conn.execute("INSERT INTO pending_employees (id, json) VALUES (?, ?)", (e.id, _dump(e)))

  def pending_employees(self) -> list[Employee]:
    """Return the employees added here that the database hasn't accepted yet, by id."""
    with self._lock:
      rows = self._conn.execute("SELECT json FROM pending_employees ORDER BY id").fetchall()
    return [Employee.model_validate_json(r[0]) for r in rows]

  def accept_employee(self, e: Employee) -> None:
    """The database accepted `e`: move it from pending into the mirror in one transaction."""
    with self._lock, self._conn:
      self._conn.execute("DELETE FROM pending_employees WHERE id = ?", (e.id,))
      self._conn.execute("INSERT OR REPLACE INTO employees (id, json) VALUES (?, ?)", (e.id, _dump(e)))

  def load(self, table: MirrorTable) -> list[TimeclockModel]:
    """Return a mirrored table's rows, validated back into their model, ordered by id."""
    model = _MODELS[table]
    with self._lock:
      rows = self._conn.execute(f"SELECT json FROM {table} ORDER BY id").fetchall()
    return [model.model_validate_json(r[0]) for r in rows]

  def set_settings(self, s: Settings) -> None:
    """Store the single settings row."""
    with self._lock, self._conn:
      self._conn.execute("INSERT OR REPLACE INTO settings (id, json) VALUES (1, ?)", (_dump(s),))

  def get_settings(self) -> Settings:
    """Return the stored settings, or the defaults if none are stored."""
    with self._lock:
      row = self._conn.execute("SELECT json FROM settings WHERE id = 1").fetchone()
    return Settings() if row is None else Settings.model_validate_json(row[0])

  # -- punches ------------------------------------------------------------------------------------------------------

  def insert_punch(self, p: Punch, *, origin: Origin, bypass: FingerprintBypass | None = None) -> bool:
    """Insert a punch (and the fingerprint bypass that authorized it, in the same transaction).

    Args:
      p: The punch.
      origin: Where it came from; only `local` punches start unsynced.
      bypass: The override or PIN login recorded with it; synced with it.

    Returns:
      False if a punch with that id already exists (nothing is written).
    """
    with self._lock, self._conn:
      cur = self._conn.execute(
        "INSERT OR IGNORE INTO punches (id, employee_id, at, origin, synced, json) VALUES (?, ?, ?, ?, ?, ?)",
        (str(p.id), p.employee_id, _iso(p.at), origin, int(origin != "local"), _dump(p)),
      )
      if cur.rowcount == 0:
        return False
      if bypass is not None:
        self._conn.execute(
          "INSERT OR REPLACE INTO fingerprint_bypasses (id, punch_id, at, synced, json) VALUES (?, ?, ?, ?, ?)",
          (str(bypass.id), str(p.id), _iso(bypass.at), int(origin != "local"), _dump(bypass)),
        )
    return True

  def insert_bypass(self, b: FingerprintBypass) -> None:
    """Insert an unsynced bypass for a PIN login that punched nothing."""
    with self._lock, self._conn:
      self._conn.execute(
        "INSERT INTO fingerprint_bypasses (id, punch_id, at, synced, json) VALUES (?, NULL, ?, 0, ?)",
        (str(b.id), _iso(b.at), _dump(b)),
      )

  def upsert_db_punch(self, p: Punch) -> None:
    """Apply a database copy: update an existing row's JSON (origin and synced stay), else insert it as `db`."""
    with self._lock, self._conn:
      self._conn.execute(
        "INSERT INTO punches (id, employee_id, at, origin, synced, json) VALUES (?, ?, ?, 'db', 1, ?) "
        "ON CONFLICT (id) DO UPDATE SET employee_id = excluded.employee_id, at = excluded.at, json = excluded.json",
        (str(p.id), p.employee_id, _iso(p.at), _dump(p)),
      )

  def delete_punch(self, punch_id: UUID) -> None:
    """Delete a punch and its bypass."""
    with self._lock, self._conn:
      self._conn.execute("DELETE FROM punches WHERE id = ?", (str(punch_id),))
      self._conn.execute("DELETE FROM fingerprint_bypasses WHERE punch_id = ?", (str(punch_id),))

  def replace_db_punches(self, punches: Sequence[Punch], *, since: datetime) -> None:
    """Replace the local copies with a catch-up fetch of the window `at >= since` (spec §6.6 step 3), in one transaction.

    An existing row takes the fetched JSON (a `peer` row becomes `db`: the server now confirms it); a new id is inserted
    as `db`. Rows in the window absent from the fetch are deleted if `db` or synced `local`, since the owner deleted them
    on the server. Unsynced local rows and `peer` rows are kept: the server doesn't have them yet.
    """
    since_text = _iso(since)
    fetched = {str(p.id) for p in punches}
    with self._lock, self._conn:
      for p in punches:
        self._conn.execute(
          "INSERT INTO punches (id, employee_id, at, origin, synced, json) VALUES (?, ?, ?, 'db', 1, ?) "
          "ON CONFLICT (id) DO UPDATE SET employee_id = excluded.employee_id, at = excluded.at, json = excluded.json, "
          "origin = CASE origin WHEN 'peer' THEN 'db' ELSE origin END, synced = CASE origin WHEN 'peer' THEN 1 ELSE synced END",
          (str(p.id), p.employee_id, _iso(p.at), _dump(p)),
        )
      stale = self._conn.execute(
        "SELECT id FROM punches WHERE at >= ? AND (origin = 'db' OR (origin = 'local' AND synced = 1))", (since_text,)
      ).fetchall()
      self._conn.executemany("DELETE FROM punches WHERE id = ?", [(r[0],) for r in stale if r[0] not in fetched])

  def punches(self, employee_id: int | None = None) -> list[Punch]:
    """Return stored punches, oldest first, optionally for one employee; hides what the module docstring says."""
    sql = f"SELECT json FROM punches WHERE {_VISIBLE}" + ("" if employee_id is None else " AND employee_id = ?") + " ORDER BY at, id"
    with self._lock:
      rows = self._conn.execute(sql, () if employee_id is None else (employee_id,)).fetchall()
    return [Punch.model_validate_json(r[0]) for r in rows]

  # -- corrections --------------------------------------------------------------------------------------------------

  def upsert_correction(self, c: PunchCorrection) -> None:
    """Insert or replace a correction."""
    with self._lock, self._conn:
      self._conn.execute(
        "INSERT OR REPLACE INTO punch_corrections (id, punch_id, json) VALUES (?, ?, ?)", (str(c.id), str(c.punch_id), _dump(c))
      )

  def delete_correction(self, cid: UUID) -> None:
    """Delete a correction by id."""
    with self._lock, self._conn:
      self._conn.execute("DELETE FROM punch_corrections WHERE id = ?", (str(cid),))

  def replace_corrections(self, cs: Sequence[PunchCorrection]) -> None:
    """Replace every correction in one transaction."""
    with self._lock, self._conn:
      self._conn.execute("DELETE FROM punch_corrections")
      self._conn.executemany(
        "INSERT INTO punch_corrections (id, punch_id, json) VALUES (?, ?, ?)", [(str(c.id), str(c.punch_id), _dump(c)) for c in cs]
      )

  def corrections(self, employee_id: int | None = None) -> list[PunchCorrection]:
    """Return corrections, optionally only those of one employee's punches."""
    sql = "SELECT c.json FROM punch_corrections c"
    if employee_id is not None:
      sql += f" JOIN punches p ON p.id = c.punch_id WHERE p.employee_id = ? AND {_VISIBLE}"
    with self._lock:
      rows = self._conn.execute(sql + " ORDER BY c.id", () if employee_id is None else (employee_id,)).fetchall()
    return [PunchCorrection.model_validate_json(r[0]) for r in rows]

  # -- fingerprints -------------------------------------------------------------------------------------------------

  def insert_fingerprint(self, f: Fingerprint, *, origin: Origin, replaces: UUID | None = None) -> bool:
    """Insert a fingerprint (unsynced only if `origin` is `local`); False if the id exists.

    `replaces` (a re-register) goes in the same transaction, its removal pushed with `f`.
    """
    with self._lock, self._conn:
      cur = self._conn.execute(
        "INSERT OR IGNORE INTO fingerprints (id, employee_id, origin, synced, json) VALUES (?, ?, ?, ?, ?)",
        (str(f.id), f.employee_id, origin, int(origin != "local"), _dump(f)),
      )
      if replaces is not None and cur.rowcount > 0:
        self._drop_print(replaces, str(f.id))
    return cur.rowcount > 0

  def remove_fingerprint(self, fid: UUID) -> None:
    """Remove a print here at once, queueing its removal from the database."""
    with self._lock, self._conn:
      self._drop_print(fid, None)

  def _drop_print(self, fid: UUID, replaced_by: str | None) -> None:
    # Caller holds the lock and transaction. A print the database never had queues nothing; a removal waiting on it (it
    # was itself an unpushed replacement) passes to `replaced_by` instead, so the database's print still goes.
    row = self._conn.execute("DELETE FROM fingerprints WHERE id = ? RETURNING origin, synced", (str(fid),)).fetchone()
    if row == ("local", 0):
      self._conn.execute("UPDATE fingerprint_removals SET replaced_by = ? WHERE replaced_by = ?", (replaced_by, str(fid)))
    elif row is not None:
      self._conn.execute("INSERT OR REPLACE INTO fingerprint_removals (id, replaced_by) VALUES (?, ?)", (str(fid), replaced_by))

  def pending_removals(self) -> list[UUID]:
    """Return the plain removals not yet pushed (a re-register's travels with its new print)."""
    with self._lock:
      rows = self._conn.execute("SELECT id FROM fingerprint_removals WHERE replaced_by IS NULL ORDER BY id").fetchall()
    return [UUID(r[0]) for r in rows]

  def mark_removal_synced(self, fid: UUID) -> None:
    """Forget a pushed removal."""
    with self._lock, self._conn:
      self._conn.execute("DELETE FROM fingerprint_removals WHERE id = ?", (str(fid),))

  def insert_fingerprint_change(self, c: FingerprintChange) -> None:
    """Queue a change-log row for the push."""
    with self._lock, self._conn:
      self._conn.execute(
        "INSERT OR IGNORE INTO fingerprint_changes (id, employee_id, json) VALUES (?, ?, ?)", (str(c.id), c.employee_id, _dump(c))
      )

  def unsynced_fingerprint_changes(self) -> list[FingerprintChange]:
    """Return the change-log rows not yet pushed, oldest first; a pending employee's are held."""
    with self._lock:
      rows = self._conn.execute(f"SELECT json FROM fingerprint_changes WHERE {_HELD} ORDER BY rowid").fetchall()
    return [FingerprintChange.model_validate_json(r[0]) for r in rows]

  def mark_fingerprint_change_synced(self, cid: UUID) -> None:
    """Delete a pushed change-log row."""
    with self._lock, self._conn:
      self._conn.execute("DELETE FROM fingerprint_changes WHERE id = ?", (str(cid),))

  def upsert_db_fingerprint(self, f: Fingerprint) -> None:
    """Apply a database copy: update an existing row's JSON (origin and synced stay), else insert it as `db`."""
    with self._lock, self._conn:
      if self._conn.execute("SELECT 1 FROM fingerprint_removals WHERE id = ?", (str(f.id),)).fetchone():
        return
      self._conn.execute(
        "INSERT INTO fingerprints (id, employee_id, origin, synced, json) VALUES (?, ?, 'db', 1, ?) "
        "ON CONFLICT (id) DO UPDATE SET employee_id = excluded.employee_id, json = excluded.json",
        (str(f.id), f.employee_id, _dump(f)),
      )

  def delete_fingerprint(self, fid: UUID) -> None:
    """Delete a fingerprint by id."""
    with self._lock, self._conn:
      self._conn.execute("DELETE FROM fingerprints WHERE id = ?", (str(fid),))

  def replace_db_fingerprints(self, fs: Sequence[Fingerprint]) -> None:
    """Replace the local copies with the full fingerprint list, by `replace_db_punches`'s rule with no window."""
    fetched = {str(f.id) for f in fs}
    with self._lock, self._conn:
      removed = {r[0] for r in self._conn.execute("SELECT id FROM fingerprint_removals")}
      for f in fs:
        if str(f.id) in removed:
          continue
        self._conn.execute(
          "INSERT INTO fingerprints (id, employee_id, origin, synced, json) VALUES (?, ?, 'db', 1, ?) "
          "ON CONFLICT (id) DO UPDATE SET employee_id = excluded.employee_id, json = excluded.json, "
          "origin = CASE origin WHEN 'peer' THEN 'db' ELSE origin END, synced = CASE origin WHEN 'peer' THEN 1 ELSE synced END",
          (str(f.id), f.employee_id, _dump(f)),
        )
      stale = self._conn.execute("SELECT id FROM fingerprints WHERE origin = 'db' OR (origin = 'local' AND synced = 1)").fetchall()
      self._conn.executemany("DELETE FROM fingerprints WHERE id = ?", [(r[0],) for r in stale if r[0] not in fetched])

  def fingerprints(self) -> list[Fingerprint]:
    """Return every stored fingerprint but those the module docstring hides."""
    with self._lock:
      rows = self._conn.execute(f"SELECT json FROM fingerprints WHERE {_VISIBLE} ORDER BY id").fetchall()
    return [Fingerprint.model_validate_json(r[0]) for r in rows]

  # -- unsynced queue and prune -------------------------------------------------------------------------------------

  def unsynced_punches(self) -> list[tuple[Punch, FingerprintBypass | None]]:
    """Return local punches not yet pushed with their bypasses, oldest `at` first; a pending employee's are held."""
    with self._lock:
      rows = self._conn.execute(
        "SELECT p.json, b.json FROM punches p LEFT JOIN fingerprint_bypasses b ON b.punch_id = p.id "
        f"WHERE p.synced = 0 AND {_HELD} ORDER BY p.at, p.id"
      ).fetchall()
    return [(Punch.model_validate_json(p), None if b is None else FingerprintBypass.model_validate_json(b)) for p, b in rows]

  def unsynced_bypasses(self) -> list[FingerprintBypass]:
    """Return PIN-login bypasses that punched nothing and aren't pushed yet, oldest first; a pending employee's are held."""
    with self._lock:
      rows = self._conn.execute(
        "SELECT json FROM fingerprint_bypasses WHERE punch_id IS NULL AND synced = 0 ORDER BY at, id"
      ).fetchall()
    bypasses = [FingerprintBypass.model_validate_json(r[0]) for r in rows]
    pending = {e.id for e in self.pending_employees()}
    return [b for b in bypasses if b.employee_id not in pending]

  def unsynced_fingerprints(self) -> list[tuple[Fingerprint, UUID | None]]:
    """Return local fingerprints not yet pushed, each with the database print it replaces; a pending employee's are held."""
    with self._lock:
      rows = self._conn.execute(
        "SELECT f.json, r.id FROM fingerprints f LEFT JOIN fingerprint_removals r ON r.replaced_by = f.id "
        f"WHERE f.synced = 0 AND {_HELD} ORDER BY f.id"
      ).fetchall()
    return [(Fingerprint.model_validate_json(f), None if r is None else UUID(r)) for f, r in rows]

  def mark_punch_synced(self, pid: UUID) -> None:
    """Mark a pushed punch synced, with the bypass pushed in its transaction."""
    with self._lock, self._conn:
      self._conn.execute("UPDATE punches SET synced = 1 WHERE id = ?", (str(pid),))
      self._conn.execute("UPDATE fingerprint_bypasses SET synced = 1 WHERE punch_id = ?", (str(pid),))

  def mark_bypass_synced(self, bid: UUID) -> None:
    """Mark a pushed PIN-login bypass synced."""
    with self._lock, self._conn:
      self._conn.execute("UPDATE fingerprint_bypasses SET synced = 1 WHERE id = ?", (str(bid),))

  def mark_fingerprint_synced(self, fid: UUID) -> None:
    """Mark a pushed fingerprint synced, with the removal pushed in its transaction."""
    with self._lock, self._conn:
      self._conn.execute("UPDATE fingerprints SET synced = 1 WHERE id = ?", (str(fid),))
      self._conn.execute("DELETE FROM fingerprint_removals WHERE replaced_by = ?", (str(fid),))

  def prune(self, before: datetime, keep_from: Mapping[int, datetime]) -> int:
    """Delete synced punches older than `before`, then the corrections and bypasses of punches that are gone.

    A synced bypass that punched nothing goes once it's older than `before`.

    Args:
      before: Punches with `at` earlier than this are candidates.
      keep_from: Per employee, the clock-in of a still-open shift; punches at or after it stay.

    Returns:
      The number of punches and corrections deleted.
    """
    with self._lock, self._conn:
      candidates = self._conn.execute(
        "SELECT id, employee_id, at FROM punches WHERE synced = 1 AND at < ?", (_iso(before),)
      ).fetchall()
      doomed = [(pid,) for pid, employee_id, at in candidates if employee_id not in keep_from or at < _iso(keep_from[employee_id])]
      self._conn.executemany("DELETE FROM punches WHERE id = ?", doomed)
      corrections = self._conn.execute("DELETE FROM punch_corrections WHERE punch_id NOT IN (SELECT id FROM punches)").rowcount
      self._conn.execute(
        "DELETE FROM fingerprint_bypasses WHERE (punch_id IS NOT NULL AND punch_id NOT IN (SELECT id FROM punches)) "
        "OR (punch_id IS NULL AND synced = 1 AND at < ?)",
        (_iso(before),),
      )
    return len(doomed) + corrections

  # -- meta ---------------------------------------------------------------------------------------------------------

  def get_meta(self, key: str) -> str | None:
    """Return a bookkeeping value, or None."""
    with self._lock:
      row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]

  def set_meta(self, key: str, value: str) -> None:
    """Store a bookkeeping value."""
    with self._lock, self._conn:
      self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
