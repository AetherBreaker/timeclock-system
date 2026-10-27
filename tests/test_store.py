"""Tests for LocalStore."""

# Standard library imports
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

# Third party imports
import pytest
from pydantic import SecretBytes
from timeclock_schema.enums import BypassReason, CorrectionAction, PunchKind
from timeclock_schema.models import Employee, Fingerprint, FingerprintBypass, Group, Punch, PunchCorrection, Settings

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


def override(p: Punch) -> FingerprintBypass:
  return FingerprintBypass(
    id=uuid4(),
    punch_id=p.id,
    employee_id=p.employee_id,
    manager_id=2,
    reason=BypassReason.OVERRIDE_NO_FINGERPRINT,
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
  store.insert_punch(p, origin="local", bypass=o)
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
  store.insert_punch(p, origin="local", bypass=override(p))
  store.delete_punch(p.id)
  assert store.punches() == []
  assert query(db_path, "SELECT count(*) FROM fingerprint_bypasses")[0][0] == 0


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
  store.set_settings(Settings(breaks_enabled=False))
  assert store.get_settings() == Settings(breaks_enabled=False)


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


def employee(employee_id: int, pin_hash: str = "$2a$06$" + "a" * 53) -> Employee:
  return Employee(id=employee_id, name=f"E{employee_id}", pin_username=str(employee_id), pin_hash=pin_hash, updated_at=T)  # pyright: ignore[reportArgumentType]


def login_bypass(employee_id: int, at: datetime = T) -> FingerprintBypass:
  return FingerprintBypass(id=uuid4(), employee_id=employee_id, reason=BypassReason.ADMIN_PIN_LOGIN, at=at, instance_id="clock-a")


def test_pending_employee_is_accepted_into_the_mirror(store: LocalStore) -> None:
  new = employee(10001)
  store.insert_pending_employee(new)
  assert store.pending_employees() == [new]
  assert store.load("employees") == []
  store.accept_employee(new)
  assert store.pending_employees() == []
  assert store.load("employees") == [new]


def test_replace_employees_settles_only_a_matching_pending_row(store: LocalStore) -> None:
  ours, theirs = employee(10001), employee(10002)
  store.insert_pending_employee(ours)
  store.insert_pending_employee(theirs)
  # The mirror now holds 10001 with our hash (a lost acknowledgement) and someone else's 10002 (a refusal).
  store.replace_employees([employee(10001), employee(10002, pin_hash="$2a$06$" + "b" * 53)])
  assert store.pending_employees() == [theirs]
  assert [e.model_dump()["id"] for e in store.load("employees")] == [10001, 10002]


def test_a_pending_employees_records_are_held(store: LocalStore) -> None:
  store.insert_pending_employee(employee(10001))
  held, free = punch(employee_id=10001), punch(employee_id=1)
  store.insert_punch(held, origin="local", bypass=override(held))
  store.insert_punch(free, origin="local")
  store.insert_fingerprint(fp().model_copy(update={"employee_id": 10001}), origin="local")
  store.insert_bypass(login_bypass(10001))
  assert [p.id for p, _ in store.unsynced_punches()] == [free.id]
  assert store.unsynced_fingerprints() == []
  assert store.unsynced_bypasses() == []
  store.accept_employee(employee(10001))
  assert {p.id for p, _ in store.unsynced_punches()} == {held.id, free.id}
  assert len(store.unsynced_fingerprints()) == 1
  assert len(store.unsynced_bypasses()) == 1


def test_rows_of_a_pending_id_from_elsewhere_are_hidden(store: LocalStore) -> None:
  # The database's employee 10001 is someone else: their punches and prints must not be filed under ours here.
  store.insert_pending_employee(employee(10001))
  ours, theirs = punch(employee_id=10001), punch(at=T + timedelta(minutes=1), employee_id=10001)
  store.insert_punch(ours, origin="local")
  store.upsert_db_punch(theirs)
  their_print = fp(b"theirs").model_copy(update={"employee_id": 10001})
  store.upsert_db_fingerprint(their_print)
  store.upsert_correction(PunchCorrection(id=uuid4(), punch_id=theirs.id, action=CorrectionAction.VOID, recorded_at=T))
  assert [p.id for p in store.punches(10001)] == [ours.id]
  assert [p.id for p in store.punches()] == [ours.id]
  assert store.corrections(10001) == []
  assert store.fingerprints() == []
  store.accept_employee(employee(10001))  # accepted: then it is the database's 10001, and everything shows
  assert {p.id for p in store.punches(10001)} == {ours.id, theirs.id}
  assert store.fingerprints() == [their_print]


def test_login_bypass_queue_and_prune(store: LocalStore) -> None:
  old, recent = login_bypass(1, T - timedelta(days=10)), login_bypass(1)
  store.insert_bypass(old)
  store.insert_bypass(recent)
  assert store.unsynced_bypasses() == [old, recent]
  store.mark_bypass_synced(old.id)
  assert store.unsynced_bypasses() == [recent]
  store.prune(T - timedelta(days=7), {})
  store.mark_bypass_synced(recent.id)
  assert store.unsynced_bypasses() == []
  assert store.prune(T - timedelta(days=7), {}) == 0  # nothing old left; the recent one stays


def test_a_punch_and_its_bypass_sync_together(store: LocalStore, db_path: Path) -> None:
  p = punch()
  store.insert_punch(p, origin="local", bypass=override(p))
  store.mark_punch_synced(p.id)
  assert query(db_path, "SELECT synced FROM fingerprint_bypasses") == [(1,)]
