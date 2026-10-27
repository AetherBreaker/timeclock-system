"""The clock's SQLite mirror of the PostgreSQL tables it reads, plus its unsynced queue and weekly prune (spec §6.5).

Synchronous and guarded by one lock; callers on the event loop wrap calls in `asyncio.to_thread`.
"""

# Standard library imports
import sqlite3
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

# Third party imports
from timeclock_schema.base import REVEAL, TimeclockModel
from timeclock_schema.models import (
  BusinessHours,
  Employee,
  Fingerprint,
  Group,
  Override,
  Punch,
  PunchCorrection,
  RuleRow,
  Settings,
)

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Mapping, Sequence
  from pathlib import Path
  from uuid import UUID

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
CREATE TABLE IF NOT EXISTS overrides (id TEXT PRIMARY KEY, punch_id TEXT NOT NULL, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS punch_corrections (id TEXT PRIMARY KEY, punch_id TEXT NOT NULL, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fingerprints (
  id TEXT PRIMARY KEY, employee_id INTEGER NOT NULL, origin TEXT NOT NULL, synced INTEGER NOT NULL, json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


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
    # foreign_keys stays off: a punch's correction or override can arrive before the punch itself.
    self._conn = sqlite3.connect(path, check_same_thread=False)
    self._lock = threading.Lock()
    with self._lock:
      self._conn.execute("PRAGMA journal_mode=WAL")
      self._conn.execute("PRAGMA foreign_keys=OFF")
      self._conn.executescript(_SCHEMA)

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

  def insert_punch(self, p: Punch, *, origin: Origin, override: Override | None = None) -> bool:
    """Insert a punch (and its override, in the same transaction).

    Args:
      p: The punch.
      origin: Where it came from; only `local` punches start unsynced.
      override: The manager override recorded with it.

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
      if override is not None:
        self._conn.execute(
          "INSERT OR REPLACE INTO overrides (id, punch_id, json) VALUES (?, ?, ?)", (str(override.id), str(p.id), _dump(override))
        )
    return True

  def upsert_db_punch(self, p: Punch) -> None:
    """Apply a database copy: update an existing row's JSON (origin and synced stay), else insert it as `db`."""
    with self._lock, self._conn:
      self._conn.execute(
        "INSERT INTO punches (id, employee_id, at, origin, synced, json) VALUES (?, ?, ?, 'db', 1, ?) "
        "ON CONFLICT (id) DO UPDATE SET employee_id = excluded.employee_id, at = excluded.at, json = excluded.json",
        (str(p.id), p.employee_id, _iso(p.at), _dump(p)),
      )

  def delete_punch(self, punch_id: UUID) -> None:
    """Delete a punch and its override."""
    with self._lock, self._conn:
      self._conn.execute("DELETE FROM punches WHERE id = ?", (str(punch_id),))
      self._conn.execute("DELETE FROM overrides WHERE punch_id = ?", (str(punch_id),))

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
    """Return stored punches, oldest first, optionally for one employee."""
    sql = "SELECT json FROM punches" + ("" if employee_id is None else " WHERE employee_id = ?") + " ORDER BY at, id"
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
      sql += " JOIN punches p ON p.id = c.punch_id WHERE p.employee_id = ?"
    with self._lock:
      rows = self._conn.execute(sql + " ORDER BY c.id", () if employee_id is None else (employee_id,)).fetchall()
    return [PunchCorrection.model_validate_json(r[0]) for r in rows]

  # -- fingerprints -------------------------------------------------------------------------------------------------

  def insert_fingerprint(self, f: Fingerprint, *, origin: Origin) -> bool:
    """Insert a fingerprint (unsynced only if `origin` is `local`); False if the id exists."""
    with self._lock, self._conn:
      cur = self._conn.execute(
        "INSERT OR IGNORE INTO fingerprints (id, employee_id, origin, synced, json) VALUES (?, ?, ?, ?, ?)",
        (str(f.id), f.employee_id, origin, int(origin != "local"), _dump(f)),
      )
    return cur.rowcount > 0

  def upsert_db_fingerprint(self, f: Fingerprint) -> None:
    """Apply a database copy: update an existing row's JSON (origin and synced stay), else insert it as `db`."""
    with self._lock, self._conn:
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
      for f in fs:
        self._conn.execute(
          "INSERT INTO fingerprints (id, employee_id, origin, synced, json) VALUES (?, ?, 'db', 1, ?) "
          "ON CONFLICT (id) DO UPDATE SET employee_id = excluded.employee_id, json = excluded.json, "
          "origin = CASE origin WHEN 'peer' THEN 'db' ELSE origin END, synced = CASE origin WHEN 'peer' THEN 1 ELSE synced END",
          (str(f.id), f.employee_id, _dump(f)),
        )
      stale = self._conn.execute("SELECT id FROM fingerprints WHERE origin = 'db' OR (origin = 'local' AND synced = 1)").fetchall()
      self._conn.executemany("DELETE FROM fingerprints WHERE id = ?", [(r[0],) for r in stale if r[0] not in fetched])

  def fingerprints(self) -> list[Fingerprint]:
    """Return every stored fingerprint."""
    with self._lock:
      rows = self._conn.execute("SELECT json FROM fingerprints ORDER BY id").fetchall()
    return [Fingerprint.model_validate_json(r[0]) for r in rows]

  # -- unsynced queue and prune -------------------------------------------------------------------------------------

  def unsynced_punches(self) -> list[tuple[Punch, Override | None]]:
    """Return local punches not yet pushed with their overrides, oldest `at` first."""
    with self._lock:
      rows = self._conn.execute(
        "SELECT p.json, o.json FROM punches p LEFT JOIN overrides o ON o.punch_id = p.id WHERE p.synced = 0 ORDER BY p.at, p.id"
      ).fetchall()
    return [(Punch.model_validate_json(p), None if o is None else Override.model_validate_json(o)) for p, o in rows]

  def unsynced_fingerprints(self) -> list[Fingerprint]:
    """Return local fingerprints not yet pushed."""
    with self._lock:
      rows = self._conn.execute("SELECT json FROM fingerprints WHERE synced = 0 ORDER BY id").fetchall()
    return [Fingerprint.model_validate_json(r[0]) for r in rows]

  def mark_punch_synced(self, pid: UUID) -> None:
    """Mark a pushed punch synced."""
    with self._lock, self._conn:
      self._conn.execute("UPDATE punches SET synced = 1 WHERE id = ?", (str(pid),))

  def mark_fingerprint_synced(self, fid: UUID) -> None:
    """Mark a pushed fingerprint synced."""
    with self._lock, self._conn:
      self._conn.execute("UPDATE fingerprints SET synced = 1 WHERE id = ?", (str(fid),))

  def prune(self, before: datetime, keep_from: Mapping[int, datetime]) -> int:
    """Delete synced punches older than `before`, then the corrections (and overrides) of punches that are gone.

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
      self._conn.execute("DELETE FROM overrides WHERE punch_id NOT IN (SELECT id FROM punches)")
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
