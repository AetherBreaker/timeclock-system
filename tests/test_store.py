"""Tests for LocalStore."""

# Standard library imports
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

# Third party imports
import pytest
from pydantic import SecretBytes
from timeclock_schema.enums import CorrectionAction, OverrideReason, PunchKind
from timeclock_schema.models import Employee, Fingerprint, Group, Override, Punch, PunchCorrection, Settings

# First party imports
from timeclock_system.store import LocalStore

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Iterator
  from pathlib import Path

T = datetime(2026, 10, 27, 12, tzinfo=UTC)
TWO, THREE = 2, 3


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
  return tmp_path / "t.sqlite3"


@pytest.fixture
def store(db_path: Path) -> Iterator[LocalStore]:
  s = LocalStore(db_path)
  yield s
  s.close()


def query(db_path: Path, sql: str) -> list[tuple]:
  # A second connection reads what the store wrote (WAL), without reaching into its internals.
  conn = sqlite3.connect(db_path)
  try:
    return conn.execute(sql).fetchall()
  finally:
    conn.close()


def punch(at: datetime = T, employee_id: int = 1, pid: UUID | None = None) -> Punch:
  return Punch(id=pid or uuid4(), employee_id=employee_id, kind=PunchKind.CLOCK_IN, at=at, instance_id="clock-a")


def override(p: Punch) -> Override:
  return Override(
    id=uuid4(),
    punch_id=p.id,
    employee_id=p.employee_id,
    manager_id=2,
    reason=OverrideReason.NO_FINGERPRINT,
    pin_only=True,
    at=p.at,
    instance_id="clock-a",
  )


def fp(template: bytes = b"tmpl", fid: UUID | None = None) -> Fingerprint:
  return Fingerprint(id=fid or uuid4(), employee_id=1, template=SecretBytes(template), registered_at=T, instance_id="clock-a")


def origins(db_path: Path) -> dict[UUID, tuple[str, int]]:
  return {UUID(r[0]): (r[1], r[2]) for r in query(db_path, "SELECT id, origin, synced FROM punches")}


def test_round_trip_keeps_secrets(store: LocalStore) -> None:
  store.insert_fingerprint(fp(b"\x00tmpl"), origin="local")
  assert store.fingerprints()[0].template.get_secret_value() == b"\x00tmpl"


def test_employee_round_trip_keeps_pin_hash(store: LocalStore) -> None:
  e = Employee(id=1, name="A", pin_username="1", pin_hash="hash", updated_at=T)  # pyright: ignore[reportArgumentType]
  store.replace_table("employees", [e])
  employee = store.load("employees")[0]
  assert isinstance(employee, Employee)
  assert employee.pin_hash.get_secret_value() == "hash"


def test_replace_table_upsert_and_delete(store: LocalStore) -> None:
  store.replace_table("groups", [Group(id=1, name="a"), Group(id=2, name="b")])
  store.upsert("groups", Group(id=2, name="c"))
  store.delete("groups", 1)
  assert store.load("groups") == [Group(id=2, name="c")]
  store.replace_table("groups", [Group(id=3, name="d")])
  assert store.load("groups") == [Group(id=3, name="d")]


def test_insert_punch_ignores_duplicate_ids(store: LocalStore) -> None:
  p = punch()
  assert store.insert_punch(p, origin="local") is True
  assert store.insert_punch(p, origin="local") is False
  assert len(store.punches()) == 1


def test_local_punch_and_override_are_unsynced_together(store: LocalStore) -> None:
  p = punch()
  o = override(p)
  store.insert_punch(p, origin="local", override=o)
  assert store.unsynced_punches() == [(p, o)]
  store.mark_punch_synced(p.id)
  assert store.unsynced_punches() == []


def test_unsynced_punches_oldest_first(store: LocalStore) -> None:
  new, old = punch(T), punch(T - timedelta(hours=1))
  store.insert_punch(new, origin="local")
  store.insert_punch(old, origin="local")
  assert [x[0] for x in store.unsynced_punches()] == [old, new]


def test_peer_and_db_punches_are_never_pushed(store: LocalStore) -> None:
  store.insert_punch(punch(), origin="peer")
  store.insert_punch(punch(), origin="db")
  assert store.unsynced_punches() == []


def test_replace_db_punches_keeps_local_and_peer_copies(store: LocalStore, db_path: Path) -> None:
  p1, p2, p3, p4 = (punch() for _ in range(4))
  store.insert_punch(p1, origin="local")
  store.insert_punch(p2, origin="peer")
  store.insert_punch(p3, origin="db")
  store.replace_db_punches([p1, p4], since=T - timedelta(days=1))
  assert {p.id for p in store.punches()} == {p1.id, p2.id, p4.id}
  assert origins(db_path)[p1.id] == ("local", 0)


def test_replace_db_punches_deletes_synced_local_rows_absent_from_fetch(store: LocalStore) -> None:
  gone, unsynced = punch(), punch()
  store.insert_punch(gone, origin="local")
  store.mark_punch_synced(gone.id)
  store.insert_punch(unsynced, origin="local")
  store.replace_db_punches([], since=T - timedelta(days=1))
  assert {p.id for p in store.punches()} == {unsynced.id}


def test_replace_db_punches_leaves_rows_before_since(store: LocalStore) -> None:
  old = punch(T - timedelta(days=10))
  store.insert_punch(old, origin="db")
  store.replace_db_punches([], since=T - timedelta(days=1))
  assert [p.id for p in store.punches()] == [old.id]


def test_replace_db_punches_updates_json_and_promotes_peer(store: LocalStore, db_path: Path) -> None:
  local, peer = punch(), punch()
  store.insert_punch(local, origin="local")
  store.mark_punch_synced(local.id)
  store.insert_punch(peer, origin="peer")
  amended = local.model_copy(update={"at": T + timedelta(minutes=5)})
  store.replace_db_punches([amended, peer], since=T - timedelta(days=1))
  assert next(p for p in store.punches() if p.id == local.id).at == T + timedelta(minutes=5)
  assert origins(db_path) == {local.id: ("local", 1), peer.id: ("db", 1)}


def test_upsert_db_punch_updates_an_existing_local_row(store: LocalStore, db_path: Path) -> None:
  p = punch()
  store.insert_punch(p, origin="local")
  store.mark_punch_synced(p.id)
  store.upsert_db_punch(p.model_copy(update={"at": T + timedelta(minutes=1)}))
  assert store.punches()[0].at == T + timedelta(minutes=1)
  assert origins(db_path)[p.id] == ("local", 1)
  new = punch()
  store.upsert_db_punch(new)
  assert origins(db_path)[new.id] == ("db", 1)


def test_delete_punch_removes_its_override(store: LocalStore, db_path: Path) -> None:
  p = punch()
  store.insert_punch(p, origin="local", override=override(p))
  store.delete_punch(p.id)
  assert store.punches() == []
  assert query(db_path, "SELECT count(*) FROM overrides")[0][0] == 0


def test_punches_filter_by_employee(store: LocalStore) -> None:
  a, b = punch(employee_id=1), punch(employee_id=2)
  store.insert_punch(a, origin="db")
  store.insert_punch(b, origin="db")
  assert store.punches(2) == [b]


def test_corrections_join_on_punch_employee(store: LocalStore) -> None:
  a, b = punch(employee_id=1), punch(employee_id=2)
  store.insert_punch(a, origin="db")
  store.insert_punch(b, origin="db")
  ca = PunchCorrection(id=uuid4(), punch_id=a.id, action=CorrectionAction.VOID, recorded_at=T)
  cb = PunchCorrection(id=uuid4(), punch_id=b.id, action=CorrectionAction.VOID, recorded_at=T)
  store.upsert_correction(ca)
  store.upsert_correction(cb)
  assert store.corrections(1) == [ca]
  assert len(store.corrections()) == TWO
  store.delete_correction(ca.id)
  store.replace_corrections([ca])
  assert store.corrections() == [ca]


def test_prune_deletes_old_synced_punches_but_not_unsynced_or_kept(store: LocalStore) -> None:
  old = T - timedelta(days=10)
  synced, unsynced, kept, other = punch(old), punch(old), punch(old, employee_id=2), punch(old, employee_id=3)
  store.insert_punch(synced, origin="db")
  store.insert_punch(unsynced, origin="local")
  store.insert_punch(kept, origin="db")
  store.insert_punch(other, origin="db")
  assert store.prune(T - timedelta(days=5), {2: old}) == TWO
  assert {p.id for p in store.punches()} == {unsynced.id, kept.id}


def test_prune_drops_orphaned_corrections(store: LocalStore) -> None:
  old = punch(T - timedelta(days=10))
  fresh = punch()
  store.insert_punch(old, origin="db")
  store.insert_punch(fresh, origin="db")
  for p in (old, fresh):
    store.upsert_correction(PunchCorrection(id=uuid4(), punch_id=p.id, action=CorrectionAction.VOID, recorded_at=T))
  assert store.prune(T - timedelta(days=5), {}) == TWO
  assert [c.punch_id for c in store.corrections()] == [fresh.id]


def test_fingerprint_replace_rule(store: LocalStore) -> None:
  deleted, unsynced, kept = fp(), fp(), fp()
  store.insert_fingerprint(deleted, origin="local")
  store.mark_fingerprint_synced(deleted.id)
  store.insert_fingerprint(unsynced, origin="local")
  store.insert_fingerprint(kept, origin="db")
  assert store.unsynced_fingerprints() == [unsynced]
  store.replace_db_fingerprints([kept])
  assert {f.id for f in store.fingerprints()} == {unsynced.id, kept.id}
  store.upsert_db_fingerprint(fp(b"new"))
  assert len(store.fingerprints()) == THREE
  store.delete_fingerprint(kept.id)
  assert len(store.fingerprints()) == TWO
  assert store.insert_fingerprint(unsynced, origin="local") is False


def test_settings_default_when_missing(store: LocalStore) -> None:
  assert store.get_settings() == Settings()
  store.set_settings(Settings(subtract_breaks=True))
  assert store.get_settings() == Settings(subtract_breaks=True)


def test_meta_round_trip(store: LocalStore) -> None:
  assert store.get_meta("k") is None
  store.set_meta("k", "v")
  store.set_meta("k", "w")
  assert store.get_meta("k") == "w"


def test_naive_timestamps_never_stored(store: LocalStore, db_path: Path) -> None:
  store.insert_punch(punch(T), origin="local")
  store.insert_punch(punch(datetime(2026, 10, 27, 5, tzinfo=timezone(timedelta(hours=-7)))), origin="local")
  stored = [r[0] for r in query(db_path, "SELECT at FROM punches")]
  assert len(stored) == TWO
  assert all(a.endswith(("Z", "+00:00")) for a in stored)
