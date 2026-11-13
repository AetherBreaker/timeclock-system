"""Tests for the Clock service."""

# Standard library imports
import dataclasses
import sqlite3
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
  ScanOutcome,
  ScanResult,
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
from timeclock_system.scanner import GalleryPrint, ScanImage, ScanReport, ScoredPrint
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


async def test_verify_pin_accepts_inactive_with_an_open_shift(env: Env) -> None:
  env.store.insert_punch(punch_at(3, PunchKind.CLOCK_IN, T - timedelta(hours=2)), origin="db")
  assert await env.clock.verify_pin("13", SecretStr("1234")) == env.clock.employee(3)


async def test_inactive_employee_finishes_their_shift_but_cannot_clock_in(env: Env) -> None:
  # Switched off mid-shift: breaks and the clock-out still work; once clocked out, the clock shuts them out.
  env.store.insert_punch(punch_at(3, PunchKind.CLOCK_IN, T - timedelta(hours=2)), origin="db")
  assert (await env.clock.menu_info(3)).valid_next == (PunchKind.CLOCK_OUT, PunchKind.BREAK_START, PunchKind.LUNCH_START)
  await env.clock.record_punch(3, PunchKind.BREAK_START)
  assert (await env.clock.menu_info(3)).valid_next == (PunchKind.BREAK_END, PunchKind.CLOCK_OUT)
  env.now += timedelta(minutes=10)
  await env.clock.record_punch(3, PunchKind.BREAK_END)
  env.now += timedelta(minutes=10)
  await env.clock.record_punch(3, PunchKind.CLOCK_OUT)
  with pytest.raises(PunchRefused):
    await env.clock.menu_info(3)
  with pytest.raises(PunchRefused):
    await env.clock.record_punch(3, PunchKind.CLOCK_IN)
  assert await env.clock.verify_pin("13", SecretStr("1234")) is None
  assert [p.kind for p in env.store.punches(3)] == [
    PunchKind.CLOCK_IN,
    PunchKind.BREAK_START,
    PunchKind.BREAK_END,
    PunchKind.CLOCK_OUT,
  ]


async def test_inactive_admin_has_no_admin_button(env: Env) -> None:
  admin = make_employee(5, role=EmployeeRole.ADMIN, active=False)
  env.store.replace_table("employees", [make_employee(1), admin])
  await env.clock.load()
  env.store.insert_punch(punch_at(5, PunchKind.CLOCK_IN, T - timedelta(hours=2)), origin="db")
  assert not (await env.clock.menu_info(5)).is_admin
  env.store.replace_table("employees", [make_employee(1), admin.model_copy(update={"active": True})])
  await env.clock.load()
  assert (await env.clock.menu_info(5)).is_admin


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
  assert outcome.hours.week == timedelta(hours=8)


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


@pytest.mark.parametrize(
  ("started", "ended"), [(None, None), (PunchKind.BREAK_START, PunchKind.BREAK_END), (PunchKind.LUNCH_START, PunchKind.LUNCH_END)]
)
async def test_clock_out_from_a_break_or_lunch_writes_its_end_first(
  env: Env, started: PunchKind | None, ended: PunchKind | None
) -> None:
  heard: list[object] = []
  env.clock.add_record_listener(heard.append)
  env.store.insert_punch(punch_at(1, PunchKind.CLOCK_IN, T - timedelta(hours=2)), origin="db")
  if started is not None:
    env.store.insert_punch(punch_at(1, started, T - timedelta(hours=1)), origin="db")
  grant = OverrideGrant(manager_id=2, reason=BypassReason.OVERRIDE_FINGERPRINT_SKIPPED, pin_only=True)
  outcome = await env.clock.record_punch(1, PunchKind.CLOCK_OUT, override=grant)
  *endings, (clock_out, bypass) = env.store.unsynced_punches()
  assert clock_out == outcome.punch
  assert bypass is not None  # the override stays with the clock-out it authorized
  assert bypass.punch_id == clock_out.id
  assert [(p.kind, p.at, b) for p, b in endings] == ([] if ended is None else [(ended, T - timedelta(microseconds=1), None)])
  assert heard == [*(p for p, _ in endings), clock_out]
  assert (await env.clock.menu_info(1)).state is EmployeeState.OFF


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
  assert outcome.hours.week == timedelta(hours=7, minutes=30)
  assert (await env.clock.menu_info(1)).hours.week == timedelta(hours=7, minutes=30)


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
  env.store.insert_punch(punch_at(3, PunchKind.CLOCK_IN, T - timedelta(hours=1)), origin="db")  # inactive, shift closed
  env.store.insert_punch(punch_at(3, PunchKind.CLOCK_OUT, T), origin="db")
  gallery = await env.clock.gallery()
  assert [g.employee_id for g in gallery] == [2, 1, 4]
  assert gallery[0].template == b"t2"
  assert gallery[0].finger is Finger.LEFT_INDEX
  assert gallery[0].print_id == prints[2].id


async def test_gallery_keeps_an_inactive_employees_prints_until_their_shift_ends(env: Env) -> None:
  for n in (1, 3):
    env.store.insert_fingerprint(
      Fingerprint(id=uuid4(), employee_id=n, finger=Finger.LEFT_INDEX, template=SecretBytes(b"t"), registered_at=T, instance_id="dev"),
      origin="db",
    )
  assert {g.employee_id for g in await env.clock.gallery()} == {1}
  clock_in = punch_at(3, PunchKind.CLOCK_IN, T - timedelta(hours=2))
  env.store.insert_punch(clock_in, origin="db")
  assert {g.employee_id for g in await env.clock.gallery()} == {1, 3}
  void = PunchCorrection(id=uuid4(), punch_id=clock_in.id, action=CorrectionAction.VOID, recorded_at=T)
  env.store.upsert_correction(void)  # a correction synced from the database can end the shift too
  assert {g.employee_id for g in await env.clock.gallery()} == {1}
  env.store.delete_correction(void.id)
  await env.clock.record_punch(3, PunchKind.CLOCK_OUT)
  assert {g.employee_id for g in await env.clock.gallery()} == {1}


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
  assert info.hours.breaks is None  # switched off, none taken: the confirmation and menu drop the lines
  assert info.hours.lunch is None
  with pytest.raises(PunchRefused):
    await env.clock.record_punch(1, PunchKind.LUNCH_START)


async def test_a_lunch_under_way_can_still_end_after_the_switch_goes_off(env: Env) -> None:
  env.store.insert_punch(punch_at(1, PunchKind.CLOCK_IN, T - timedelta(hours=2)), origin="local")
  env.store.insert_punch(punch_at(1, PunchKind.LUNCH_START, T - timedelta(hours=1)), origin="local")
  env.store.set_settings(Settings(lunches_enabled=False))
  env.clock.notify_changed(["settings"])
  info = await env.clock.menu_info(1)
  assert PunchKind.LUNCH_END in info.valid_next
  assert info.hours.lunch == timedelta(hours=1)  # taken today, so still shown


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
  assert "SECRETBYTES" not in repr(GalleryPrint(uuid4(), 1, b"SECRETBYTES", Finger.RIGHT_THUMB))


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
  assert check_new_pin(SecretStr("123456789012"))
  for bad in ("", "1", "123", "1234567890123"):
    assert not check_new_pin(SecretStr(bad))
  assert not check_new_pin(SecretStr("12a4"))
  assert not check_new_pin(SecretStr("\uff11\uff12\uff13\uff14"))  # full-width digits are decimal, but not ASCII


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


async def test_add_employee_logs_its_number(env: Env, caplog: pytest.LogCaptureFixture) -> None:
  caplog.set_level("INFO")
  e = await env.clock.add_employee(NEW, SecretStr("2468"))
  assert any(str(e.id) in r.getMessage() for r in caplog.records)
  assert not any("2468" in r.getMessage() or "$2a$" in r.getMessage() for r in caplog.records)


async def test_add_employee_skips_numbers_the_clock_knows(env: Env) -> None:
  # 10005 is taken, 10006 is someone's username, and clock 2's numbers don't count toward clock 1's.
  env.store.replace_table(
    "employees", [make_employee(10005, username="10005"), make_employee(4, username="10006"), make_employee(20009, username="20009")]
  )
  await env.clock.load()
  assert (await env.clock.add_employee(NEW, SecretStr("1234"))).id == 10007  # noqa: PLR2004


async def test_add_employee_refuses_a_full_block_admins_and_bad_pins(env: Env) -> None:
  env.store.replace_table("employees", [make_employee(19999)])
  await env.clock.load()
  with pytest.raises(EmployeeBlockFull):
    await env.clock.add_employee(NEW, SecretStr("1234"))
  with pytest.raises(ValueError, match="employees and managers"):
    await env.clock.add_employee(dataclasses.replace(NEW, role=EmployeeRole.ADMIN), SecretStr("1234"))
  with pytest.raises(ValueError, match="PIN"):
    await env.clock.add_employee(NEW, SecretStr("123"))
  assert env.store.pending_employees() == []


async def test_add_employee_tells_the_record_listeners(env: Env) -> None:
  heard: list[object] = []
  env.clock.add_record_listener(heard.append)
  e = await env.clock.add_employee(NEW, SecretStr("1234"))
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
  manager = await env.clock.add_employee(dataclasses.replace(NEW, role=EmployeeRole.MANAGER), SecretStr("1234"))
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
  # 4 h on the clock with a 40-minute break: 3 h 20 worked, 15 minutes paid (the per-break limit), 25 unpaid.
  assert (await env.clock.menu_info(1)).hours.week == timedelta(hours=3, minutes=35)
  outcome = await env.clock.record_punch(1, PunchKind.CLOCK_OUT)
  assert outcome.hours.week == timedelta(hours=3, minutes=35)


async def test_hours_summary_counts_the_calendar_day(env: Env) -> None:
  monday_night = datetime(2026, 10, 26, 22, tzinfo=NY)
  tuesday = datetime(2026, 10, 27, 8, tzinfo=NY)
  for kind, at in (
    (PunchKind.CLOCK_IN, monday_night),
    (PunchKind.BREAK_START, monday_night + timedelta(hours=1)),  # yesterday's break, not today's
    (PunchKind.BREAK_END, monday_night + timedelta(hours=1, minutes=20)),
    (PunchKind.CLOCK_OUT, monday_night + timedelta(hours=4)),  # 2 h of it after midnight
    (PunchKind.CLOCK_IN, tuesday),
    (PunchKind.BREAK_START, tuesday + timedelta(hours=2)),
    (PunchKind.BREAK_END, tuesday + timedelta(hours=2, minutes=15)),
    (PunchKind.LUNCH_START, tuesday + timedelta(hours=4)),
    (PunchKind.LUNCH_END, tuesday + timedelta(hours=4, minutes=30)),
    (PunchKind.BREAK_START, tuesday + timedelta(hours=6)),
  ):
    env.store.insert_punch(punch_at(1, kind, at), origin="db")
  env.now = tuesday + timedelta(hours=6, minutes=10)
  hours = (await env.clock.menu_info(1)).hours
  # Today: 2 h after midnight, then 6 h 10 on the clock less a 30-minute lunch; both breaks fit the limit and the paid
  # allowance. The week adds Monday's 2 h before midnight, less the 5 minutes its 20-minute break ran past the limit.
  assert hours.today == timedelta(hours=7, minutes=40)
  assert hours.week == timedelta(hours=9, minutes=35)
  assert (hours.breaks, hours.last_break, hours.on_break) == (timedelta(minutes=25), timedelta(minutes=10), True)
  assert hours.lunch == timedelta(minutes=30)
  env.now += timedelta(minutes=2)
  ended = (await env.clock.record_punch(1, PunchKind.BREAK_END)).hours
  assert (ended.breaks, ended.last_break, ended.on_break) == (timedelta(minutes=27), timedelta(minutes=12), False)


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
  assert over.hours.week == timedelta(hours=41)
  assert [(f.hook.alert, f.limit) for f in over.firings] == [(WEEKLY_HOURS, timedelta(hours=40))]


def scan_report(result: ScanResult = ScanResult.MATCHED) -> ScanReport:
  best = ScoredPrint(uuid4(), 1, Finger.LEFT_INDEX, 90)
  other = ScoredPrint(uuid4(), 2, Finger.RIGHT_THUMB, 40)
  scores = ((best.print_id, 90), (other.print_id, 40))
  return ScanReport(result, best, other, scores, 15, probe=b"probe", image=ScanImage(b"img", 2, 3), retry_message="Remove finger")


async def test_record_scan_fills_the_row(env: Env) -> None:
  report = scan_report()
  assert report.best is not None
  assert report.other is not None
  scan_id = await env.clock.record_scan(report)
  assert scan_id is not None
  assert env.store.unsynced_scans() == []  # matched: open until settled
  await env.clock.settle_scan(scan_id, ScanOutcome.PUNCHED)
  [row] = env.store.unsynced_scans()
  assert (row.id, row.at, row.instance_id, row.result, row.margin) == (scan_id, T, "dev", ScanResult.MATCHED, 15)
  assert row.outcome is ScanOutcome.PUNCHED
  assert (row.best_fingerprint_id, row.best_employee_id, row.best_finger, row.best_score) == (
    report.best.print_id,
    1,
    Finger.LEFT_INDEX,
    90,
  )
  assert (row.other_fingerprint_id, row.other_employee_id, row.other_finger, row.other_score) == (
    report.other.print_id,
    2,
    Finger.RIGHT_THUMB,
    40,
  )
  assert row.scores == dict(report.scores)
  assert row.retry_message == report.retry_message


@pytest.mark.parametrize("result", [ScanResult.NO_MATCH, ScanResult.TOO_CLOSE, ScanResult.RETRY])
async def test_scans_without_a_match_are_settled_at_once(env: Env, result: ScanResult) -> None:
  scan_id = await env.clock.record_scan(scan_report(result))
  assert [r.id for r in env.store.unsynced_scans()] == [scan_id]


async def test_record_scan_drops_the_image_when_switched_off(env: Env) -> None:
  await env.clock.record_scan(scan_report(ScanResult.NO_MATCH))
  [kept] = env.store.unsynced_scans()
  assert kept.image is not None
  assert (kept.image_width, kept.image_height) == (2, 3)
  env.store.delete_scan(kept.id)
  env.store.set_settings(Settings(save_scan_images=False))
  env.clock.notify_changed({"settings"})
  await env.clock.record_scan(scan_report(ScanResult.NO_MATCH))
  [dropped] = env.store.unsynced_scans()
  assert (dropped.image, dropped.image_width, dropped.image_height) == (None, None, None)
  assert dropped.probe is not None
  assert dropped.probe.get_secret_value() == b"probe"


async def test_load_settles_leftover_scans(env: Env) -> None:
  scan_id = await env.clock.record_scan(scan_report())
  assert env.store.unsynced_scans() == []
  await env.clock.load()
  assert [(r.id, r.outcome) for r in env.store.unsynced_scans()] == [(scan_id, None)]


async def test_scan_log_failure_never_raises(env: Env, caplog: pytest.LogCaptureFixture) -> None:
  def boom(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("disk full")

  env.store.insert_scan = boom
  env.store.settle_scan = boom
  assert await env.clock.record_scan(scan_report()) is None
  await env.clock.settle_scan(uuid4(), ScanOutcome.CANCELLED)
  assert len(caplog.records) == 2  # noqa: PLR2004


async def test_load_survives_a_failing_scan_log(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
  def broken() -> None:
    raise sqlite3.OperationalError("database or disk is full")

  monkeypatch.setattr(env.store, "settle_open_scans", broken)
  await env.clock.load()  # startup must not depend on the scan log
