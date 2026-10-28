"""Tests for the Clock service."""

# Standard library imports
import dataclasses
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4
from zoneinfo import ZoneInfo

# Third party imports
import bcrypt
import pytest
from pydantic import BaseModel, SecretBytes, SecretStr
from timeclock_schema import rules as schema_rules
from timeclock_schema.alerts import WEEKLY_HOURS
from timeclock_schema.enums import (
  BypassReason,
  CorrectionAction,
  EmployeeRole,
  Finger,
  FingerprintChangeAction,
  PunchKind,
  RuleAction,
)
from timeclock_schema.models import Employee, Fingerprint, Punch, PunchCorrection, RuleRow, Settings
from timeclock_schema.shifts import EmployeeState

# First party imports
from timeclock_system.clock import (
  Approval,
  Clock,
  EmployeeBlockFull,
  NewEmployee,
  OverrideGrant,
  PunchRefused,
  check_new_pin,
  normalize_phone,
)
from timeclock_system.scanner import GalleryPrint
from timeclock_system.store import LocalStore

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Iterator
  from pathlib import Path

  # Third party imports
  from timeclock_schema.rules import RuleContext

NY = ZoneInfo("America/New_York")
T = datetime(2026, 10, 27, 12, tzinfo=UTC)  # a Tuesday
MANAGER_ID = 2
HASH = bcrypt.hashpw(b"1234", bcrypt.gensalt(rounds=6, prefix=b"2a")).decode()


def make_employee(
  employee_id: int, *, role: EmployeeRole = EmployeeRole.EMPLOYEE, active: bool = True, username: str | None = None
) -> Employee:
  return Employee(
    id=employee_id,
    first_name="Emp",
    last_name=str(employee_id),
    pin_username=username or str(10 + employee_id),
    pin_hash=SecretStr(HASH),
    role=role,
    active=active,
    updated_at=T,
  )


def punch_at(employee_id: int, kind: PunchKind, at: datetime, instance_id: str = "dev") -> Punch:
  return Punch(id=uuid4(), employee_id=employee_id, kind=kind, at=at, instance_id=instance_id)


class Env:
  def __init__(self, store: LocalStore) -> None:
    self.store = store
    self.now = T
    self.alerts: list[tuple[str, str]] = []
    self.clock = Clock(
      store, instance_id="dev", clock_number=1, tz=NY, alert=lambda s, b: self.alerts.append((s, b)), now=lambda: self.now
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[LocalStore]:
  s = LocalStore(tmp_path / "t.sqlite3")
  s.replace_table("employees", [make_employee(1), make_employee(2, role=EmployeeRole.MANAGER), make_employee(3, active=False)])
  yield s
  s.close()


@pytest.fixture
async def env(store: LocalStore) -> Env:
  e = Env(store)
  await e.clock.load()
  return e


async def test_verify_pin_accepts_pgcrypto_style_hash(env: Env) -> None:
  found = await env.clock.verify_pin("11", SecretStr("1234"))
  assert found is not None
  assert found.id == 1
  assert await env.clock.verify_pin("11", SecretStr("9999")) is None


async def test_verify_pin_refuses_inactive(env: Env) -> None:
  assert await env.clock.verify_pin("13", SecretStr("1234")) is None


async def test_menu_info_for_a_clocked_in_employee(env: Env) -> None:
  env.store.insert_punch(punch_at(1, PunchKind.CLOCK_IN, T - timedelta(hours=2)), origin="db")
  info = await env.clock.menu_info(1)
  assert info.state is EmployeeState.WORKING
  assert info.since == T - timedelta(hours=2)
  assert info.valid_next == (PunchKind.CLOCK_OUT, PunchKind.BREAK_START, PunchKind.LUNCH_START)


async def test_menu_info_since_after_lunch_is_the_clock_in(env: Env) -> None:
  start = datetime(2026, 10, 27, 12, tzinfo=UTC)
  env.now = start + timedelta(hours=6)
  for kind, offset in (
    (PunchKind.CLOCK_IN, 0),
    (PunchKind.LUNCH_START, 4),
    (PunchKind.LUNCH_END, 4.5),
  ):
    env.store.insert_punch(punch_at(1, kind, start + timedelta(hours=offset)), origin="db")
  info = await env.clock.menu_info(1)
  assert info.state is EmployeeState.WORKING
  assert info.since == start


async def test_menu_info_since_is_the_last_event_at_lunch_and_none_when_off(env: Env) -> None:
  assert (await env.clock.menu_info(1)).since is None
  env.store.insert_punch(punch_at(1, PunchKind.CLOCK_IN, T - timedelta(hours=3)), origin="db")
  env.store.insert_punch(punch_at(1, PunchKind.LUNCH_START, T - timedelta(hours=1)), origin="db")
  info = await env.clock.menu_info(1)
  assert info.state is EmployeeState.AT_LUNCH
  assert info.since == T - timedelta(hours=1)


async def test_a_punch_stamped_later_than_now_waits_for_its_time(env: Env) -> None:
  # The backend's automatic clock-out can be stamped after its run; until then the employee can still clock out by hand.
  env.store.insert_punch(punch_at(1, PunchKind.CLOCK_IN, T - timedelta(hours=8)), origin="db")
  env.store.insert_punch(punch_at(1, PunchKind.CLOCK_OUT, T + timedelta(hours=1), instance_id="backend"), origin="db")
  info = await env.clock.menu_info(1)
  assert info.state is EmployeeState.WORKING
  assert PunchKind.CLOCK_OUT in info.valid_next
  outcome = await env.clock.record_punch(1, PunchKind.CLOCK_OUT)
  assert outcome.punch.at == T
  assert outcome.hours_this_week == timedelta(hours=8)


async def test_a_punch_stamped_later_counts_once_its_time_passes(env: Env) -> None:
  env.store.insert_punch(punch_at(1, PunchKind.CLOCK_IN, T - timedelta(hours=8)), origin="db")
  env.store.insert_punch(punch_at(1, PunchKind.CLOCK_OUT, T + timedelta(hours=1), instance_id="backend"), origin="db")
  env.now = T + timedelta(hours=1)
  assert (await env.clock.menu_info(1)).state is EmployeeState.OFF


async def test_record_punch_stores_unsynced_and_notifies(env: Env) -> None:
  heard: list[object] = []
  changed: list[frozenset[str]] = []
  env.clock.add_record_listener(heard.append)
  env.clock.add_change_listener(changed.append)
  outcome = await env.clock.record_punch(1, PunchKind.CLOCK_IN)
  assert outcome.punch.instance_id == "dev"
  assert env.store.unsynced_punches()[0][0] == outcome.punch
  assert heard == [outcome.punch]
  assert changed == [frozenset({"punches"})]


async def test_record_punch_refuses_invalid_kind(env: Env) -> None:
  with pytest.raises(PunchRefused):
    await env.clock.record_punch(1, PunchKind.CLOCK_OUT)
  assert env.store.punches() == []


async def test_record_punch_refuses_unknown_and_inactive(env: Env) -> None:
  for employee_id in (99, 3):
    with pytest.raises(PunchRefused, match="Unknown employee"):
      await env.clock.record_punch(employee_id, PunchKind.CLOCK_IN)


async def test_override_is_stored_with_its_punch(env: Env) -> None:
  grant = OverrideGrant(manager_id=2, reason=BypassReason.OVERRIDE_FINGERPRINT_SKIPPED, pin_only=True)
  await env.clock.record_punch(1, PunchKind.CLOCK_IN, override=grant)
  stored = env.store.unsynced_punches()[0][1]
  assert stored is not None
  assert stored.pin_only is True
  assert stored.reason is BypassReason.OVERRIDE_FINGERPRINT_SKIPPED
  assert stored.manager_id == MANAGER_ID


class _NoParams(BaseModel):
  pass


async def test_refuse_rule_blocks_and_warn_rule_warns(env: Env) -> None:
  def check_refuse(_p: _NoParams, ctx: RuleContext) -> str | None:
    return "No lunch today" if ctx.proposed is PunchKind.LUNCH_START else None

  def check_warn(_p: _NoParams, ctx: RuleContext) -> str | None:
    return "Clocking in early" if ctx.proposed is PunchKind.CLOCK_IN else None

  schema_rules.register_kind("t_refuse", _NoParams, check_refuse)
  schema_rules.register_kind("t_warn", _NoParams, check_warn)
  try:
    env.store.replace_table(
      "rules",
      [
        RuleRow(id=1, kind="t_refuse", group_id=1, on_violation=RuleAction.REFUSE, updated_at=T),
        RuleRow(id=2, kind="t_warn", group_id=1, on_violation=RuleAction.WARN, updated_at=T),
      ],
    )
    env.clock.notify_changed({"rules"})
    outcome = await env.clock.record_punch(1, PunchKind.CLOCK_IN)
    assert outcome.warnings == ("Clocking in early",)
    with pytest.raises(PunchRefused, match="No lunch today") as refused:
      await env.clock.record_punch(1, PunchKind.LUNCH_START)
    assert refused.value.message == "No lunch today"
    assert [p.kind for p in env.store.punches(1)] == [PunchKind.CLOCK_IN]
  finally:
    schema_rules.unregister_kind("t_refuse")
    schema_rules.unregister_kind("t_warn")


async def test_bad_rule_row_alerts_once(env: Env) -> None:
  def bad(updated_at: datetime) -> None:
    env.store.replace_table("rules", [RuleRow(id=7, kind="nope", group_id=1, on_violation=RuleAction.WARN, updated_at=updated_at)])

  bad(T)
  await env.clock.load()
  await env.clock.load()
  assert [a[0] for a in env.alerts] == ["Bad rule row 7"]
  bad(T + timedelta(minutes=1))
  await env.clock.load()
  assert len(env.alerts) == 2  # noqa: PLR2004


async def test_hours_this_week_leaves_out_lunch(env: Env) -> None:
  start = datetime(2026, 10, 27, 12, tzinfo=UTC)
  for kind, minutes in (
    (PunchKind.CLOCK_IN, 0),
    (PunchKind.LUNCH_START, 240),
    (PunchKind.LUNCH_END, 270),
  ):
    env.store.insert_punch(punch_at(1, kind, start + timedelta(minutes=minutes)), origin="db")
  env.now = start + timedelta(hours=8)
  outcome = await env.clock.record_punch(1, PunchKind.CLOCK_OUT)
  assert outcome.hours_this_week == timedelta(hours=7, minutes=30)
  assert (await env.clock.menu_info(1)).hours_this_week == timedelta(hours=7, minutes=30)


async def test_gallery_orders_by_latest_punch_here(env: Env) -> None:
  env.store.replace_table("employees", [make_employee(1), make_employee(2), make_employee(3, active=False), make_employee(4)])
  await env.clock.load()
  prints = {
    n: Fingerprint(
      id=uuid4(), employee_id=n, finger=Finger.LEFT_INDEX, template=SecretBytes(b"t%d" % n), registered_at=T, instance_id="dev"
    )
    for n in (1, 2, 3, 4)
  }
  for f in prints.values():
    env.store.insert_fingerprint(f, origin="db")
  env.store.insert_punch(punch_at(1, PunchKind.CLOCK_IN, T - timedelta(hours=3)), origin="db")
  env.store.insert_punch(punch_at(2, PunchKind.CLOCK_IN, T - timedelta(hours=1)), origin="db")
  env.store.insert_punch(punch_at(4, PunchKind.CLOCK_IN, T, instance_id="other"), origin="db")  # not recorded here
  env.store.insert_punch(punch_at(3, PunchKind.CLOCK_IN, T), origin="db")
  gallery = await env.clock.gallery()
  assert [g.employee_id for g in gallery] == [2, 1, 4]
  assert gallery[0].template == b"t2"
  assert gallery[0].print_id == prints[2].id


async def test_override_manager_rules(env: Env) -> None:
  clock = env.clock
  manager = make_employee(2, role=EmployeeRole.MANAGER)
  assert clock.override_manager(manager, 1)
  assert not clock.override_manager(make_employee(1), 2)
  assert not clock.override_manager(manager, 2)
  assert not clock.override_manager(make_employee(2, role=EmployeeRole.MANAGER, active=False), 1)


async def test_an_admin_can_approve_overrides(env: Env) -> None:
  # Admin ranks above manager.
  assert env.clock.override_manager(make_employee(2, role=EmployeeRole.ADMIN), 1)


async def test_switched_off_lunches_and_breaks_hide_their_starts(env: Env) -> None:
  env.store.set_settings(Settings(lunches_enabled=False, breaks_enabled=False))
  env.clock.notify_changed(["settings"])
  env.store.insert_punch(punch_at(1, PunchKind.CLOCK_IN, T - timedelta(hours=1)), origin="local")
  info = await env.clock.menu_info(1)
  assert PunchKind.LUNCH_START not in info.valid_next
  assert PunchKind.BREAK_START not in info.valid_next
  assert PunchKind.CLOCK_OUT in info.valid_next
  with pytest.raises(PunchRefused):
    await env.clock.record_punch(1, PunchKind.LUNCH_START)


async def test_a_lunch_under_way_can_still_end_after_the_switch_goes_off(env: Env) -> None:
  env.store.insert_punch(punch_at(1, PunchKind.CLOCK_IN, T - timedelta(hours=2)), origin="local")
  env.store.insert_punch(punch_at(1, PunchKind.LUNCH_START, T - timedelta(hours=1)), origin="local")
  env.store.set_settings(Settings(lunches_enabled=False))
  env.clock.notify_changed(["settings"])
  assert PunchKind.LUNCH_END in (await env.clock.menu_info(1)).valid_next


async def test_register_fingerprint_stores_local_unsynced(env: Env) -> None:
  heard: list[object] = []
  env.clock.add_record_listener(heard.append)
  assert not await env.clock.has_fingerprint(1)
  f = await env.clock.register_fingerprint(1, b"tmpl", Finger.LEFT_INDEX)
  assert env.store.unsynced_fingerprints() == [(f, None)]
  assert heard == [f]
  assert f.instance_id == "dev"
  assert await env.clock.has_fingerprint(1)


async def test_register_fingerprint_logs_an_add(env: Env) -> None:
  f = await env.clock.register_fingerprint(1, b"tmpl", Finger.RIGHT_THUMB)
  [c] = env.store.unsynced_fingerprint_changes()
  assert (c.employee_id, c.finger, c.action, c.approved_by, c.pin_only) == (
    1,
    Finger.RIGHT_THUMB,
    FingerprintChangeAction.ADD,
    None,
    None,
  )
  assert (c.at, c.instance_id) == (T, "dev")
  assert await env.clock.prints_by_finger(1) == {Finger.RIGHT_THUMB: f.id}


async def test_replace_logs_the_approval_and_drops_the_old_print(env: Env) -> None:
  old = await env.clock.register_fingerprint(1, b"old", Finger.LEFT_INDEX)
  new = await env.clock.register_fingerprint(1, b"new", Finger.LEFT_INDEX, approval=Approval(2, pin_only=True), replaces=old.id)
  assert await env.clock.prints_by_finger(1) == {Finger.LEFT_INDEX: new.id}
  c = env.store.unsynced_fingerprint_changes()[-1]
  assert (c.action, c.approved_by, c.pin_only) == (FingerprintChangeAction.REPLACE, 2, True)


async def test_remove_fingerprint_logs_and_stops_matching(env: Env) -> None:
  heard: list[frozenset[str]] = []
  env.clock.add_change_listener(heard.append)
  f = await env.clock.register_fingerprint(1, b"tmpl", Finger.LEFT_INDEX)
  await env.clock.remove_fingerprint(1, f.id, Finger.LEFT_INDEX, Approval(2, pin_only=False))
  assert await env.clock.gallery() == []
  assert await env.clock.prints_by_finger(1) == {}
  c = env.store.unsynced_fingerprint_changes()[-1]
  assert (c.action, c.finger, c.approved_by, c.pin_only) == (FingerprintChangeAction.REMOVE, Finger.LEFT_INDEX, 2, False)
  assert heard[-1] == {"fingerprints"}


async def test_fingerprint_approver_is_an_active_admin(env: Env) -> None:
  assert env.clock.fingerprint_approver(make_employee(5, role=EmployeeRole.ADMIN))
  assert not env.clock.fingerprint_approver(make_employee(2, role=EmployeeRole.MANAGER))
  assert not env.clock.fingerprint_approver(make_employee(6, role=EmployeeRole.ADMIN, active=False))
  pending = make_employee(7, role=EmployeeRole.ADMIN)
  env.store.insert_pending_employee(pending)
  await env.clock.load()
  assert not env.clock.fingerprint_approver(pending)  # its number may yet be refused


async def test_insert_peer_record_is_never_pushed_and_dedupes(env: Env) -> None:
  p = punch_at(1, PunchKind.CLOCK_IN, T, instance_id="other")
  assert await env.clock.insert_peer_record(p) is True
  assert await env.clock.insert_peer_record(p) is False
  assert env.store.unsynced_punches() == []
  f = Fingerprint(
    id=uuid4(), employee_id=1, finger=Finger.LEFT_INDEX, template=SecretBytes(b"x"), registered_at=T, instance_id="other"
  )
  assert await env.clock.insert_peer_record(f) is True
  assert env.store.unsynced_fingerprints() == []


async def test_prune_keeps_an_open_shift(env: Env) -> None:
  sunday_in = datetime(2026, 11, 8, 22, tzinfo=NY).astimezone(UTC)
  env.store.insert_punch(punch_at(1, PunchKind.CLOCK_IN, sunday_in), origin="db")
  env.store.insert_punch(punch_at(2, PunchKind.CLOCK_IN, sunday_in - timedelta(hours=9)), origin="db")
  env.store.insert_punch(punch_at(2, PunchKind.CLOCK_OUT, sunday_in - timedelta(hours=1)), origin="db")
  env.now = datetime(2026, 11, 9, 9, tzinfo=NY).astimezone(UTC)  # Monday
  assert await env.clock.prune_week() == 2  # noqa: PLR2004
  assert [p.employee_id for p in env.store.punches()] == [1]
  outcome = await env.clock.record_punch(1, PunchKind.CLOCK_OUT)
  assert outcome.punch.kind is PunchKind.CLOCK_OUT


async def test_catch_up_since_reaches_back_to_an_open_shift(env: Env) -> None:
  env.now = datetime(2026, 11, 9, 9, tzinfo=NY).astimezone(UTC)
  week_start = datetime(2026, 11, 9, tzinfo=NY).astimezone(UTC)
  assert await env.clock.catch_up_since() == week_start
  sunday_in = datetime(2026, 11, 8, 22, tzinfo=NY).astimezone(UTC)
  env.store.insert_punch(punch_at(1, PunchKind.CLOCK_IN, sunday_in), origin="db")
  assert await env.clock.catch_up_since() == sunday_in


async def test_notify_changed_reloads_caches(env: Env) -> None:
  env.store.replace_table("employees", [make_employee(1), make_employee(5)])
  assert env.clock.employee(5) is None
  seen: list[frozenset[str]] = []
  env.clock.add_change_listener(seen.append)
  env.clock.notify_changed(["employees"])
  assert env.clock.employee(5) is not None
  assert seen == [frozenset({"employees"})]


async def test_business_hours_and_settings_come_from_the_cache(env: Env) -> None:
  assert env.clock.business_hours() == ()
  assert env.clock.settings().lunches_enabled is True


async def _amended_open_shift(env: Env) -> tuple[datetime, Punch]:
  """An open shift stored Sunday 21:50 NY whose clock-in the owner amended to 22:00; clock is at Monday 09:00."""
  stored = datetime(2026, 11, 8, 21, 50, tzinfo=NY).astimezone(UTC)
  p = punch_at(1, PunchKind.CLOCK_IN, stored)
  env.store.insert_punch(p, origin="db")
  env.store.upsert_correction(
    PunchCorrection(
      id=uuid4(), punch_id=p.id, action=CorrectionAction.AMEND, new_at=stored + timedelta(minutes=10), recorded_at=stored
    )
  )
  env.now = datetime(2026, 11, 9, 9, tzinfo=NY).astimezone(UTC)
  return stored, p


async def test_prune_keeps_a_clock_in_amended_later(env: Env) -> None:
  await _amended_open_shift(env)
  assert await env.clock.prune_week() == 0
  assert (await env.clock.record_punch(1, PunchKind.CLOCK_OUT)).punch.kind is PunchKind.CLOCK_OUT


async def test_catch_up_since_uses_the_stored_time_of_an_amended_clock_in(env: Env) -> None:
  stored, _ = await _amended_open_shift(env)
  assert await env.clock.catch_up_since() == stored


def test_gallery_print_repr_hides_the_template() -> None:
  assert "SECRETBYTES" not in repr(GalleryPrint(uuid4(), 1, b"SECRETBYTES"))


NEW = NewEmployee("New", None, "Hire", "+15551234567", "new@example.com", EmployeeRole.EMPLOYEE, 1)


@pytest.mark.parametrize(
  ("raw", "stored"),
  [
    ("(555) 123-4567", "+15551234567"),
    ("555-123-4567", "+15551234567"),
    ("555.123.4567", "+15551234567"),
    ("5551234567", "+15551234567"),
    ("1 555 123 4567", "+15551234567"),
    ("+1 555 123 4567", "+15551234567"),
    (" +1 (555) 123-4567 ", "+15551234567"),
  ],
)
def test_normalize_phone_accepts_common_us_formats(raw: str, stored: str) -> None:
  assert normalize_phone(raw) == stored


@pytest.mark.parametrize(
  "raw",
  [
    "555-1234",
    "+44 20 7946 0958",
    "555-123-45678",
    "555-CALL-NOW",
    "",
    "\uff15\uff15\uff15\uff11\uff12\uff13\uff14\uff15\uff16\uff17",
  ],
)
def test_normalize_phone_refuses_anything_else(raw: str) -> None:
  with pytest.raises(ValueError, match="phone"):
    normalize_phone(raw)


def test_check_new_pin() -> None:
  assert check_new_pin(SecretStr("0042"))
  assert check_new_pin(SecretStr("1"))
  assert not check_new_pin(SecretStr(""))
  assert not check_new_pin(SecretStr("12a4"))
  assert not check_new_pin(SecretStr("\uff11\uff12"))  # full-width digits are decimal, but not ASCII


async def test_add_employee_numbers_from_the_clocks_block(env: Env) -> None:
  first = await env.clock.add_employee(NEW, SecretStr("2468"))
  assert (first.id, first.pin_username, first.role, first.phone) == (10001, "10001", EmployeeRole.EMPLOYEE, "+15551234567")
  second = await env.clock.add_employee(NEW, SecretStr("2468"))
  assert second.id == 10002  # noqa: PLR2004  # after its own unsynced one
  assert [e.id for e in env.store.pending_employees()] == [10001, 10002]
  found = await env.clock.verify_pin("10001", SecretStr("2468"))
  assert found is not None and found.id == 10001  # noqa: PLR2004  # can log in at once
  assert found.pin_hash.get_secret_value().startswith("$2a$")
  info = await env.clock.menu_info(first.id)
  assert info.valid_next == (PunchKind.CLOCK_IN,)


async def test_add_employee_skips_numbers_the_clock_knows(env: Env) -> None:
  # 10005 is taken, 10006 is someone's username, and clock 2's numbers don't count toward clock 1's.
  env.store.replace_table(
    "employees", [make_employee(10005, username="10005"), make_employee(4, username="10006"), make_employee(20009, username="20009")]
  )
  await env.clock.load()
  assert (await env.clock.add_employee(NEW, SecretStr("1"))).id == 10007  # noqa: PLR2004


async def test_add_employee_refuses_a_full_block_admins_and_bad_pins(env: Env) -> None:
  env.store.replace_table("employees", [make_employee(19999)])
  await env.clock.load()
  with pytest.raises(EmployeeBlockFull):
    await env.clock.add_employee(NEW, SecretStr("1"))
  with pytest.raises(ValueError, match="employees and managers"):
    await env.clock.add_employee(dataclasses.replace(NEW, role=EmployeeRole.ADMIN), SecretStr("1"))
  with pytest.raises(ValueError, match="PIN"):
    await env.clock.add_employee(NEW, SecretStr("12x"))
  assert env.store.pending_employees() == []


async def test_add_employee_tells_the_record_listeners(env: Env) -> None:
  heard: list[object] = []
  env.clock.add_record_listener(heard.append)
  e = await env.clock.add_employee(NEW, SecretStr("1"))
  assert heard == [e]


async def test_a_pending_employee_hides_the_mirror_row_sharing_its_number(env: Env) -> None:
  env.store.insert_pending_employee(make_employee(10001, username="10001"))
  env.store.replace_table("employees", [make_employee(1), make_employee(10001, username="555"), make_employee(7, username="10001")])
  await env.clock.load()
  held = env.clock.employee(10001)
  assert held is not None and held.pin_username == "10001"
  assert env.clock.employee(7) is None  # its username is the pending employee's
  assert env.clock.employee(1) is not None


async def test_a_pending_manager_cant_approve_overrides(env: Env) -> None:
  manager = await env.clock.add_employee(dataclasses.replace(NEW, role=EmployeeRole.MANAGER), SecretStr("1"))
  assert not env.clock.override_manager(manager, 1)
  env.store.accept_employee(manager)
  env.clock.notify_changed({"employees"})
  assert env.clock.override_manager(manager, 1)


async def test_pin_only_login_is_admins_and_exempt_employees(env: Env) -> None:
  assert env.clock.pin_only_login(make_employee(9, role=EmployeeRole.ADMIN))
  assert env.clock.pin_only_login(make_employee(9).model_copy(update={"fingerprint_exempt": True}))
  assert not env.clock.pin_only_login(make_employee(9, role=EmployeeRole.MANAGER))


async def test_pin_logins_are_recorded_as_bypasses(env: Env) -> None:
  admin = make_employee(9, role=EmployeeRole.ADMIN)
  exempt = make_employee(1).model_copy(update={"fingerprint_exempt": True})
  env.store.replace_table("employees", [admin, exempt])
  await env.clock.load()
  await env.clock.record_pin_login(admin)
  [login] = env.store.unsynced_bypasses()
  assert (login.employee_id, login.reason, login.punch_id, login.manager_id) == (9, BypassReason.ADMIN_PIN_LOGIN, None, None)
  outcome = await env.clock.record_punch(1, PunchKind.CLOCK_IN, pin_login=True)  # a 30 s rescan by PIN
  [(stored, bypass)] = env.store.unsynced_punches()
  assert stored == outcome.punch
  assert bypass is not None and (bypass.reason, bypass.punch_id) == (BypassReason.EXEMPT_PIN_LOGIN, outcome.punch.id)


async def test_menu_shows_is_admin(env: Env) -> None:
  env.store.replace_table("employees", [make_employee(1), make_employee(9, role=EmployeeRole.ADMIN)])
  await env.clock.load()
  assert (await env.clock.menu_info(9)).is_admin
  assert not (await env.clock.menu_info(1)).is_admin


async def test_hours_this_week_count_paid_breaks(env: Env) -> None:
  start = datetime(2026, 10, 27, 12, tzinfo=UTC)
  for kind, minutes in ((PunchKind.CLOCK_IN, 0), (PunchKind.BREAK_START, 60), (PunchKind.BREAK_END, 100)):
    env.store.insert_punch(punch_at(1, kind, start + timedelta(minutes=minutes)), origin="db")
  env.now = start + timedelta(hours=4)
  # 4 h on the clock with a 40-minute break: 3 h 20 worked, 30 minutes paid, 10 unpaid.
  assert (await env.clock.menu_info(1)).hours_this_week == timedelta(hours=3, minutes=50)
  outcome = await env.clock.record_punch(1, PunchKind.CLOCK_OUT)
  assert outcome.hours_this_week == timedelta(hours=3, minutes=50)


async def test_a_punch_reaching_the_weekly_threshold_fires_the_hook(env: Env) -> None:
  monday = datetime(2026, 10, 26, 8, tzinfo=NY)
  for day in range(4):
    env.store.insert_punch(punch_at(1, PunchKind.CLOCK_IN, monday + timedelta(days=day)), origin="db")
    env.store.insert_punch(punch_at(1, PunchKind.CLOCK_OUT, monday + timedelta(days=day, hours=9.5)), origin="db")
  env.now = monday + timedelta(days=4)
  under = await env.clock.record_punch(1, PunchKind.CLOCK_IN)  # 38 h
  assert under.firings == ()
  env.now += timedelta(hours=3)
  over = await env.clock.record_punch(1, PunchKind.CLOCK_OUT)
  assert over.hours_this_week == timedelta(hours=41)
  assert [(f.hook.alert, f.limit) for f in over.firings] == [(WEEKLY_HOURS, timedelta(hours=40))]
