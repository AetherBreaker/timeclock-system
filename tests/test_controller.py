# Standard library imports
import json
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

# Third party imports
import bcrypt
import pytest
from pydantic import SecretBytes, SecretStr
from timeclock_schema.alerts import CONFIRMATION_NOTICE, WEEKLY_HOURS, Firing, Hook
from timeclock_schema.enums import BypassReason, EmployeeRole, Finger, FingerprintChangeAction, PunchKind, ScanOutcome, ScanResult
from timeclock_schema.models import BusinessHours, Employee, Fingerprint, Group, Punch, Settings

# First party imports
from timeclock_system.clock import Approval, Clock, HoursSummary, NewEmployee, OverrideGrant
from timeclock_system.registration import PRESSES, WINDOW, Done, Failure, OtherPrint, Press, TooSimilar
from timeclock_system.scanner import (
  EnrollFailed,
  FingerStatus,
  Matched,
  Merged,
  NoMatch,
  Pressed,
  PressRetry,
  ReaderMode,
  ReaderPaused,
  RegisterGallery,
  Retry,
  ScanReport,
  ScoredPrint,
)
from timeclock_system.store import LocalStore
from timeclock_system.sync import DbStatus
from timeclock_system.ui import controller as controller_module, text
from timeclock_system.ui.controller import (
  CONFIRM_SECS,
  IDLE_CLOSE_SECS,
  RESCAN_AFTER_SECS,
  AddEmployeeDetails,
  AdminMenu,
  AdminReconfirm,
  ApprovalPrompt,
  Confirmation,
  EmployeeAdded,
  FingerChange,
  FingerPick,
  FingerprintActions,
  FingerprintLogin,
  ForceSave,
  GearMenu,
  Idle,
  KioskController,
  Menu,
  NewFingerprint,
  NewPin,
  OverridePrompt,
  OverrideRequest,
  PinEntry,
  PinRecheck,
  PinScan,
  ReaderIndicator,
  ReaderMessage,
  Registering,
  RegisterPrompt,
  Rescan,
  SimilarReview,
  TooSimilarBlocked,
  TooSimilarReview,
)

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import AsyncIterator, Sequence
  from pathlib import Path

  # First party imports
  from timeclock_system.registration import Minutiae
  from timeclock_system.ui.controller import DetailsField

NY = ZoneInfo("America/New_York")
START = datetime(2026, 10, 27, 12, 2, tzinfo=UTC)  # Tuesday 8:02 AM EDT
ANN, BOB, MIA, ADA, EVE = 1, 2, 3, 4, 5
ANN_PRINT, MIA_PRINT = uuid4(), uuid4()
PINS = {ANN: "1001", BOB: "1002", MIA: "1003", ADA: "1004", EVE: "1005"}


class FakeTime:
  def __init__(self) -> None:
    self.mono = 1000.0
    self.wall = START

  def advance(self, secs: float) -> None:
    self.mono += secs
    self.wall += timedelta(seconds=secs)


class FakeReaderPort:
  def __init__(self) -> None:
    self.modes: list[ReaderMode] = []
    self.registrants: list[int] = []
    self.merges: list[tuple[bytes, ...]] = []

  def set_mode(self, mode: ReaderMode) -> None:
    assert mode is not ReaderMode.REGISTER, "only register() enters REGISTER"
    self.modes.append(mode)

  def register(self, employee_id: int) -> None:
    self.modes.append(ReaderMode.REGISTER)
    self.registrants.append(employee_id)

  def merge(self, presses: Sequence[bytes]) -> None:
    self.merges.append(tuple(presses))


@dataclass
class Kiosk:
  ctl: KioskController
  clock: Clock
  store: LocalStore
  t: FakeTime
  reader: FakeReaderPort

  async def no_hours(self) -> None:
    self.store.replace_table("business_hours", [])
    self.clock.notify_changed({"business_hours"})
    await self.ctl.tick()

  async def pin_login(self, employee_id: int) -> None:
    await self.ctl.use_pin()
    await self.type_and_enter(PINS[employee_id])
    await self.type_and_enter("1234")

  async def type_and_enter(self, digits: str) -> None:
    for d in digits:
      await self.ctl.digit(d)
    await self.ctl.enter()


@pytest.fixture
async def kiosk(tmp_path: Path) -> AsyncIterator[Kiosk]:
  store = LocalStore(tmp_path / "clock.sqlite3")
  pin_hash = SecretStr(bcrypt.hashpw(b"1234", bcrypt.gensalt(rounds=4)).decode())
  store.replace_table(
    "employees",
    [
      Employee(id=ANN, first_name="Ann", last_name="Lee", pin_username=PINS[ANN], pin_hash=pin_hash, updated_at=START),
      Employee(id=BOB, first_name="Bob", last_name="Lee", pin_username=PINS[BOB], pin_hash=pin_hash, updated_at=START),
      Employee(
        id=MIA,
        first_name="Mia",
        last_name="Lee",
        pin_username=PINS[MIA],
        pin_hash=pin_hash,
        role=EmployeeRole.MANAGER,
        updated_at=START,
      ),
      Employee(
        id=ADA, first_name="Ada", last_name="Lee", pin_username=PINS[ADA], pin_hash=pin_hash, role=EmployeeRole.ADMIN, updated_at=START
      ),
      Employee(
        id=EVE, first_name="Eve", last_name="Lee", pin_username=PINS[EVE], pin_hash=pin_hash, fingerprint_exempt=True, updated_at=START
      ),
    ],
  )
  store.replace_table("groups", [Group(id=1, name="default")])
  store.replace_table("business_hours", [BusinessHours(id=d, weekday=d, opens=time(0, 0), closes=time(23, 59)) for d in range(1, 8)])
  for print_id, employee_id in ((ANN_PRINT, ANN), (MIA_PRINT, MIA)):
    store.insert_fingerprint(
      Fingerprint(
        id=print_id,
        employee_id=employee_id,
        finger=Finger.LEFT_INDEX,
        template=SecretBytes(b"t"),
        registered_at=START,
        instance_id="1",
      ),
      origin="db",
    )
  t = FakeTime()
  clock = Clock(store, instance_id="1", clock_number=1, tz=NY, alert=lambda _s, _b: None, now=lambda: t.wall)
  await clock.load()
  reader = FakeReaderPort()
  ctl = KioskController(clock, reader, tz=NY, monotonic=lambda: t.mono, wall_now=lambda: t.wall, status_dir=tmp_path / "status")
  yield Kiosk(ctl, clock, store, t, reader)
  store.close()


def matched(employee_id: int) -> Matched:
  return Matched(print_id=MIA_PRINT if employee_id == MIA else ANN_PRINT, employee_id=employee_id)


# The reader's finger signal as the scanner posts it.
WAITING = FingerStatus(present=False, waiting=True)
DOWN = FingerStatus(present=True, waiting=False)
LIFTED = FingerStatus(present=False, waiting=False)

# Registration presses, judged by the default bars.
BARS = Settings()
GOOD = 100  # an agreement score far above the bar
LOW = BARS.registration_agreement_bar - 1
SIMILAR = BARS.registration_resemblance_bar


def points(n: int = 40, cx: int = WINDOW[0] // 2) -> Minutiae:
  """`n` detail points (even) centred on x = `cx`."""
  return tuple((cx + d, WINDOW[1] // 2, 0) for k in range(n // 2) for d in (-k - 1, k + 1))


def press(press_id: int, *, n: int = 40, cx: int = WINDOW[0] // 2, agrees: bool = True, like_mia: bool = False) -> Press:
  """A press agreeing with every earlier one unless not `agrees`; `like_mia` resembles two presses of Mia's print."""
  return Press(
    press_id,
    points(n, cx),
    f"probe-{press_id}".encode(),
    dict.fromkeys(range(press_id), GOOD if agrees else LOW),
    {MIA_PRINT: (0, 0, SIMILAR, 0, SIMILAR)} if like_mia else {},
  )


def mias_print(*, careless: bool = False) -> OtherPrint:
  """Mia's left index as a registration sees it; a careless one has 3 of 5 presses agreeing with no other (case B)."""
  failing = {0, 1, 2} if careless else set()
  agreement = tuple(tuple(-1 if i == j else LOW if {i, j} & failing else GOOD for j in range(PRESSES)) for i in range(PRESSES))
  return OtherPrint(MIA_PRINT, MIA, Finger.LEFT_INDEX, (points(),) * PRESSES, agreement)


async def give_presses(kiosk: Kiosk, *, others: tuple[OtherPrint, ...] = (), like_mia: bool = False) -> None:
  """A register session's gallery, then 5 clean presses; with `like_mia` the first two resemble Mia's print."""
  await kiosk.ctl.on_scanner_event(RegisterGallery(others))
  for i in range(PRESSES):
    await kiosk.ctl.on_scanner_event(Pressed(press(i, like_mia=like_mia and i < 2)))  # noqa: PLR2004


async def register_cleanly(kiosk: Kiosk, template: bytes = b"new-template") -> None:
  await give_presses(kiosk)
  await kiosk.ctl.on_scanner_event(Merged(template))


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
  sent: list[tuple[str, str]] = []
  monkeypatch.setattr(controller_module, "alert", lambda reason, details, **_kw: sent.append((reason, details)))
  return sent


def test_format_clock_time() -> None:
  assert text.format_clock_time(START, NY) == "8:02 AM"
  assert text.format_clock_time(datetime(2026, 10, 27, 4, 5, tzinfo=UTC), NY) == "12:05 AM"
  assert text.format_clock_time(datetime(2026, 10, 27, 17, 30, tzinfo=UTC), NY) == "1:30 PM"


def test_format_duration_and_db_status() -> None:
  assert text.format_duration(timedelta(hours=1, minutes=5)) == "1 hr 5 min"
  assert text.format_duration(timedelta(hours=8)) == "8 hr"
  assert text.format_duration(timedelta(minutes=45, seconds=59)) == "45 min"  # truncated, never rounded
  assert text.format_duration(timedelta(hours=32, minutes=30)) == "32 hr 30 min"
  assert text.format_duration(timedelta(seconds=35)) == "0 min"
  assert text.format_db_status(DbStatus(connected=True, last_contact=START), NY) == "Database connected · last contact 8:02 AM"
  assert text.format_db_status(DbStatus(connected=False, last_contact=None), NY) == "Database offline · last contact never"


def test_hours_lines() -> None:
  hours = HoursSummary(
    week=timedelta(hours=32, minutes=30),
    today=timedelta(hours=7, minutes=5),
    breaks=timedelta(minutes=25),
    last_break=timedelta(minutes=10),
    on_break=False,
    lunch=timedelta(),
  )
  assert text.hours_lines(hours) == (
    ("This week", "32 hr 30 min"),
    ("Today", "7 hr 5 min"),
    ("Breaks", "25 min"),
    ("Last break", "10 min"),
    ("Lunch", "0 min"),
  )
  assert text.hours_lines(replace(hours, on_break=True))[3] == ("Current break", "10 min")
  assert [label for label, _ in text.hours_lines(replace(hours, breaks=None, lunch=None))] == ["This week", "Today"]


async def test_match_opens_the_menu(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(matched(ANN))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu)
  assert screen.info.name == "Ann Lee"
  assert screen.auth is None
  assert screen.info.valid_next == (PunchKind.CLOCK_IN,)


async def test_punch_within_30s_records_and_confirms(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(matched(ANN))
  kiosk.t.advance(10)
  await kiosk.ctl.punch(PunchKind.CLOCK_IN)
  screen = kiosk.ctl.screen
  assert isinstance(screen, Confirmation)
  assert screen.text == "Clocked in at 8:02 AM"
  assert screen.hours == tuple((label, "0 min") for label in ("This week", "Today", "Breaks", "Last break", "Lunch"))
  assert [p.kind for p in kiosk.store.punches(ANN)] == [PunchKind.CLOCK_IN]
  kiosk.t.advance(5.1)
  await kiosk.ctl.tick()
  assert kiosk.ctl.screen == Idle()


async def test_a_double_tap_ends_the_confirmation_early(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(matched(ANN))
  await kiosk.ctl.punch(PunchKind.CLOCK_IN)
  await kiosk.ctl.tap()
  assert isinstance(kiosk.ctl.screen, Confirmation)  # one tap isn't enough
  kiosk.t.advance(0.3)
  await kiosk.ctl.tap()
  assert kiosk.ctl.screen == Idle()


async def test_two_slow_taps_dont_end_the_confirmation(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(matched(ANN))
  await kiosk.ctl.punch(PunchKind.CLOCK_IN)
  await kiosk.ctl.tap()
  kiosk.t.advance(0.6)
  await kiosk.ctl.tap()
  assert isinstance(kiosk.ctl.screen, Confirmation)
  kiosk.t.advance(0.3)
  await kiosk.ctl.tap()  # pairs with the previous tap
  assert kiosk.ctl.screen == Idle()


async def test_punch_after_30s_needs_a_rescan_of_the_same_employee(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(matched(ANN))
  kiosk.t.advance(31)
  await kiosk.ctl.punch(PunchKind.CLOCK_IN)
  assert kiosk.ctl.screen == Rescan(ANN, PunchKind.CLOCK_IN, None, touched_at=kiosk.t.mono)
  assert kiosk.store.punches() == []
  await kiosk.ctl.on_scanner_event(matched(ANN))
  assert isinstance(kiosk.ctl.screen, Confirmation)
  assert [p.kind for p in kiosk.store.punches(ANN)] == [PunchKind.CLOCK_IN]


async def test_rescan_by_someone_else_opens_their_menu_without_punching(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(matched(ANN))
  kiosk.t.advance(31)
  await kiosk.ctl.punch(PunchKind.CLOCK_IN)
  await kiosk.ctl.on_scanner_event(matched(MIA))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu)
  assert screen.info.employee_id == MIA
  assert kiosk.store.punches() == []


async def test_new_scan_switches_the_menu(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(matched(ANN))
  kiosk.t.advance(20)
  await kiosk.ctl.on_scanner_event(matched(MIA))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu)
  assert screen.info.employee_id == MIA
  assert screen.opened_at == kiosk.t.mono


async def test_menu_closes_after_two_minutes_untouched(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(matched(ANN))
  kiosk.t.advance(119)
  await kiosk.ctl.tick()
  assert isinstance(kiosk.ctl.screen, Menu)
  kiosk.t.advance(2)
  await kiosk.ctl.tick()
  assert kiosk.ctl.screen == Idle()


async def test_refused_punch_shows_notice_and_stores_nothing(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(matched(ANN))
  await kiosk.ctl.punch(PunchKind.CLOCK_OUT)  # Ann is clocked out, so clock-out isn't valid
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu)
  assert screen.notice == "That punch isn't available right now"
  assert kiosk.store.punches() == []


async def test_a_refused_punch_keeps_the_scans_30s_window(kiosk: Kiosk) -> None:
  # R45.2: only a new scan restarts the window; reopening the menu with a refusal doesn't.
  await kiosk.ctl.on_scanner_event(matched(ANN))
  scanned_at = kiosk.t.mono
  kiosk.t.advance(20)
  await kiosk.ctl.punch(PunchKind.CLOCK_OUT)  # refused: Ann is clocked out
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu)
  assert screen.opened_at == scanned_at
  kiosk.t.advance(11)
  await kiosk.ctl.punch(PunchKind.CLOCK_IN)
  assert kiosk.ctl.screen == Rescan(ANN, PunchKind.CLOCK_IN, None, touched_at=kiosk.t.mono)
  assert kiosk.store.punches() == []


async def test_pin_login_without_print_prompts_registration(kiosk: Kiosk) -> None:
  await kiosk.pin_login(BOB)
  assert kiosk.ctl.screen == RegisterPrompt(BOB, touched_at=kiosk.t.mono)


async def test_an_inactive_employee_without_a_print_is_sent_to_a_manager_not_registration(kiosk: Kiosk) -> None:
  kiosk.store.insert_punch(
    Punch(id=uuid4(), employee_id=BOB, kind=PunchKind.CLOCK_IN, at=START - timedelta(hours=1), instance_id="1"), origin="db"
  )
  bob = kiosk.clock.employee(BOB)
  assert bob is not None
  others = [e for e in cast("list[Employee]", kiosk.store.load("employees")) if e.id != BOB]
  kiosk.store.replace_table("employees", [*others, bob.model_copy(update={"active": False})])
  kiosk.clock.notify_changed({"employees"})
  await kiosk.pin_login(BOB)
  request = OverrideRequest(BOB, BypassReason.OVERRIDE_NO_FINGERPRINT, None)
  assert kiosk.ctl.screen == OverridePrompt(request, touched_at=kiosk.t.mono)


async def test_pin_login_with_print_requires_that_employees_finger(kiosk: Kiosk) -> None:
  await kiosk.pin_login(ANN)
  assert kiosk.ctl.screen == PinScan(ANN, touched_at=kiosk.t.mono)
  await kiosk.ctl.on_scanner_event(matched(MIA))
  assert kiosk.ctl.screen == PinScan(ANN, touched_at=kiosk.t.mono, message=text.NOT_YOUR_FINGER)
  await kiosk.ctl.on_scanner_event(matched(ANN))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu)
  assert screen.info.employee_id == ANN
  assert screen.auth is None


async def test_the_pin_is_never_held_as_plain_text(kiosk: Kiosk) -> None:
  # Typed PIN digits stay in a SecretStr until bcrypt checks them, so no repr, log or alert can show them.
  await kiosk.ctl.use_pin()
  await kiosk.type_and_enter(PINS[1])
  for d in "1234":
    await kiosk.ctl.digit(d)
  s = kiosk.ctl.screen
  assert isinstance(s, PinEntry)
  assert isinstance(s.pin, SecretStr)
  assert s.pin.get_secret_value() == "1234"
  assert "1234" not in repr(s)
  assert s.username == PINS[1]  # the username isn't secret


async def test_wrong_pin_returns_to_username_with_error(kiosk: Kiosk) -> None:
  await kiosk.ctl.use_pin()
  await kiosk.type_and_enter(PINS[ANN])
  await kiosk.type_and_enter("9999")
  assert kiosk.ctl.screen == PinEntry("username", "", SecretStr(""), None, touched_at=kiosk.t.mono, error=text.WRONG_PIN)


async def bob_registers(kiosk: Kiosk) -> None:
  """Bob, with no print, logs in by PIN and registers alone (the RegisterPrompt path)."""
  await kiosk.pin_login(BOB)
  await kiosk.ctl.register()
  await kiosk.ctl.pick_finger(Finger.LEFT_INDEX)


async def test_presses_advance_kept(kiosk: Kiosk) -> None:
  await bob_registers(kiosk)
  assert kiosk.ctl.screen == Registering(BOB, touched_at=kiosk.t.mono, finger=Finger.LEFT_INDEX)
  assert (kiosk.reader.modes[-1], kiosk.reader.registrants) == (ReaderMode.REGISTER, [BOB])
  await kiosk.ctl.on_scanner_event(RegisterGallery(()))
  for i in range(PRESSES - 1):
    await kiosk.ctl.on_scanner_event(Pressed(press(i)))
    screen = kiosk.ctl.screen
    assert isinstance(screen, Registering)
    assert (screen.kept, screen.advice) == (i + 1, None)
  assert kiosk.reader.merges == []


@pytest.mark.parametrize(
  ("last", "advice"),
  [
    (press(4, n=BARS.registration_min_detail_points - 2), "Press your finger flat on the reader"),
    (press(4, n=BARS.registration_max_detail_points + 2), "Clean and dry your finger, then press again"),
    (press(4, cx=5), "Keep your finger centred on the reader"),
    (press(4, agrees=False), "Press the same finger the same way each time"),
  ],
  ids=[f.name for f in Failure],
)
async def test_failure_shows_advice(kiosk: Kiosk, last: Press, advice: str) -> None:
  await bob_registers(kiosk)
  await kiosk.ctl.on_scanner_event(RegisterGallery(()))
  for i in range(PRESSES - 1):
    await kiosk.ctl.on_scanner_event(Pressed(press(i)))
  await kiosk.ctl.on_scanner_event(Pressed(last))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Registering)
  assert (screen.kept, screen.advice) == (PRESSES - 1, advice)
  await kiosk.ctl.on_scanner_event(Pressed(press(5)))  # the replacement passes: the advice goes
  screen = kiosk.ctl.screen
  assert isinstance(screen, Registering)
  assert (screen.kept, screen.advice) == (PRESSES, None)
  assert kiosk.reader.merges == [(b"probe-0", b"probe-1", b"probe-2", b"probe-3", b"probe-5")]


async def test_done_merges_then_saves(kiosk: Kiosk, alerts: list[tuple[str, str]]) -> None:
  await bob_registers(kiosk)
  await give_presses(kiosk, others=(mias_print(),))
  assert kiosk.reader.merges == [tuple(f"probe-{i}".encode() for i in range(PRESSES))]
  screen = kiosk.ctl.screen
  assert isinstance(screen, Registering) and screen.verdict == Done(chosen=(0, 1, 2, 3, 4), flagged=())
  assert not await kiosk.clock.has_fingerprint(BOB)
  await kiosk.ctl.on_scanner_event(Merged(b"new-template"))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu) and screen.info.employee_id == BOB and screen.notice is None
  assert kiosk.reader.modes[-1] is ReaderMode.READY
  [(f, _)] = kiosk.store.unsynced_fingerprints()
  assert (f.employee_id, f.finger, f.template.get_secret_value()) == (BOB, Finger.LEFT_INDEX, b"new-template")
  [c] = kiosk.store.unsynced_fingerprint_changes()
  assert (c.action, c.approved_by, c.pin_only) == (FingerprintChangeAction.ADD, None, None)
  assert alerts == []


async def test_a_session_takes_the_bars_current_at_its_start(kiosk: Kiosk) -> None:
  await bob_registers(kiosk)
  kiosk.store.set_settings(Settings(registration_min_detail_points=41))
  kiosk.clock.notify_changed({"settings"})
  await kiosk.ctl.on_scanner_event(RegisterGallery(()))
  kiosk.store.set_settings(Settings())
  kiosk.clock.notify_changed({"settings"})
  for i in range(PRESSES):
    await kiosk.ctl.on_scanner_event(Pressed(press(i)))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Registering) and screen.advice == text.PRESS_ADVICE[Failure.TOO_FEW]


async def test_gave_up_message(kiosk: Kiosk) -> None:
  await bob_registers(kiosk)
  await kiosk.ctl.on_scanner_event(RegisterGallery(()))
  for i in range(PRESSES + BARS.registration_disagreement_retries):
    await kiosk.ctl.on_scanner_event(Pressed(press(i, agrees=i < PRESSES - 1)))
  assert kiosk.ctl.screen == Idle(
    "Couldn't get a clean registration. Try again or use another finger.", until=kiosk.t.mono + controller_module.IDLE_ERROR_SECS
  )
  assert kiosk.reader.modes[-1] is ReaderMode.READY
  assert kiosk.reader.merges == []
  assert not await kiosk.clock.has_fingerprint(BOB)


async def test_registration_timeout_shows_message(kiosk: Kiosk) -> None:
  await bob_registers(kiosk)
  await kiosk.ctl.on_scanner_event(EnrollFailed("timeout"))
  assert kiosk.ctl.screen == Idle(text.REGISTRATION_TIMED_OUT, until=kiosk.t.mono + controller_module.IDLE_ERROR_SECS)
  assert kiosk.reader.modes[-1] is ReaderMode.READY


async def test_a_failed_merge_saves_nothing(kiosk: Kiosk) -> None:
  await bob_registers(kiosk)
  await give_presses(kiosk)
  await kiosk.ctl.on_scanner_event(EnrollFailed("error"))
  assert kiosk.ctl.screen == Idle(text.REGISTRATION_FAILED, until=kiosk.t.mono + controller_module.IDLE_ERROR_SECS)
  assert kiosk.reader.modes[-1] is ReaderMode.READY  # a failed merge leaves the scanner in REGISTER
  assert not await kiosk.clock.has_fingerprint(BOB)


async def test_press_on_review_screen_ignored(kiosk: Kiosk) -> None:
  # Review Focus 3: the scanner captures until the mode changes, and a press already posted can land after the verdict.
  await bob_registers(kiosk)
  await give_presses(kiosk)
  waiting = kiosk.ctl.screen
  assert isinstance(waiting, Registering) and waiting.verdict is not None and waiting.session is not None
  await kiosk.ctl.on_scanner_event(Pressed(press(5)))
  assert kiosk.ctl.screen == waiting
  assert len(waiting.session.kept) == PRESSES
  await kiosk.ctl.cancel()
  await bob_registers(kiosk)
  await give_presses(kiosk, others=(mias_print(),), like_mia=True)
  blocked = kiosk.ctl.screen
  assert isinstance(blocked, TooSimilarBlocked)
  await kiosk.ctl.on_scanner_event(Pressed(press(5)))
  assert kiosk.ctl.screen == blocked
  assert blocked.registering.session is not None and len(blocked.registering.session.kept) == PRESSES
  await kiosk.ctl.ask_admin()
  await kiosk.ctl.on_scanner_event(matched(ADA))
  review = kiosk.ctl.screen
  assert isinstance(review, TooSimilarReview)
  await kiosk.ctl.on_scanner_event(Pressed(press(6)))
  assert kiosk.ctl.screen == review
  assert len(blocked.registering.session.kept) == PRESSES


# -- too similar to another person's print (case C) and careless prints (case B) ---------------------------------------


async def test_too_similar_alone_blocked_no_names(kiosk: Kiosk, alerts: list[tuple[str, str]]) -> None:
  await bob_registers(kiosk)
  await give_presses(kiosk, others=(mias_print(),), like_mia=True)
  screen = kiosk.ctl.screen
  assert isinstance(screen, TooSimilarBlocked)  # the page names no one on it
  assert screen.verdict.other.print_id == MIA_PRINT
  assert kiosk.reader.merges == []  # nothing is built that nobody may save
  assert kiosk.reader.modes[-1] is ReaderMode.READY
  assert alerts == []
  await kiosk.ctl.cancel()
  assert kiosk.ctl.screen == Idle()
  assert not await kiosk.clock.has_fingerprint(BOB)


async def test_try_another_finger(kiosk: Kiosk) -> None:
  await bob_registers(kiosk)
  await give_presses(kiosk, others=(mias_print(),), like_mia=True)
  await kiosk.ctl.try_another_finger()
  assert kiosk.ctl.screen == FingerPick(BOB, None, tuple(f for f in Finger if f is not Finger.LEFT_INDEX), touched_at=kiosk.t.mono)
  await kiosk.ctl.pick_finger(Finger.RIGHT_INDEX)
  assert kiosk.ctl.screen == Registering(BOB, touched_at=kiosk.t.mono, finger=Finger.RIGHT_INDEX)
  # The scanner left REGISTER in between, so registering again starts a new session.
  assert kiosk.reader.modes[-3:] == [ReaderMode.REGISTER, ReaderMode.READY, ReaderMode.REGISTER]
  await register_cleanly(kiosk)
  assert set(await kiosk.clock.prints_by_finger(BOB)) == {Finger.RIGHT_INDEX}


async def test_too_similar_alone_admin_steps_in(kiosk: Kiosk, alerts: list[tuple[str, str]]) -> None:
  await bob_registers(kiosk)
  await give_presses(kiosk, others=(mias_print(),), like_mia=True)
  blocked = kiosk.ctl.screen
  assert isinstance(blocked, TooSimilarBlocked)
  await kiosk.ctl.ask_admin()
  review = SimilarReview(blocked.registering, blocked.verdict)
  assert kiosk.ctl.screen == ApprovalPrompt(review, touched_at=kiosk.t.mono)
  await kiosk.ctl.on_scanner_event(matched(ANN))
  assert kiosk.ctl.screen == ApprovalPrompt(review, touched_at=kiosk.t.mono, message=text.NOT_AN_ADMIN)
  assert kiosk.reader.merges == []
  await kiosk.ctl.on_scanner_event(matched(ADA))
  screen = kiosk.ctl.screen
  assert isinstance(screen, TooSimilarReview)
  assert (screen.name, screen.verdict.other.finger, screen.approval) == ("Mia Lee", Finger.LEFT_INDEX, Approval(ADA, pin_only=False))
  assert kiosk.reader.merges == [tuple(f"probe-{i}".encode() for i in range(PRESSES))]
  await kiosk.ctl.on_scanner_event(Merged(b"bob"))
  await kiosk.ctl.allow()
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu) and screen.info.employee_id == BOB
  [c] = kiosk.store.unsynced_fingerprint_changes()
  assert (c.employee_id, c.action, c.approved_by, c.pin_only) == (BOB, FingerprintChangeAction.ADD, ADA, False)
  assert alerts == []


async def test_an_admin_who_steps_in_leaves_no_reregister_advice(kiosk: Kiosk, alerts: list[tuple[str, str]]) -> None:
  # The employee keeps the punch menu after the admin leaves: naming a coworker's print there is the alert's job only.
  careless = OtherPrint(uuid4(), EVE, Finger.RIGHT_THUMB, (points(),) * PRESSES, mias_print(careless=True).agreement)
  on_both = {MIA_PRINT: (0, 0, SIMILAR, 0, SIMILAR), careless.print_id: (SIMILAR,) * PRESSES}
  await bob_registers(kiosk)
  await kiosk.ctl.on_scanner_event(RegisterGallery((mias_print(), careless)))
  for i in range(PRESSES):
    p = press(i)
    await kiosk.ctl.on_scanner_event(Pressed(Press(i, p.minutiae, p.probe, p.vs_session, on_both if i < 2 else {})))  # noqa: PLR2004
  await kiosk.ctl.ask_admin()
  await kiosk.ctl.on_scanner_event(matched(ADA))
  await kiosk.ctl.allow()
  await kiosk.ctl.on_scanner_event(Merged(b"bob"))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu) and screen.info.employee_id == BOB and screen.notice is None
  [(subject, body)] = alerts
  assert subject == "Fingerprint print needs re-registering" and "Eve Lee's right thumb" in body


async def test_the_resembled_print_approves_no_review_of_it(kiosk: Kiosk) -> None:
  # Case C against an admin's print: the registrant's own finger can be identified as that print, so it mustn't pass
  # the admin check, on the prompt or on its PIN pad. The admin's other fingers and PIN still do.
  ada_print = uuid4()
  adas = OtherPrint(ada_print, ADA, Finger.LEFT_INDEX, (points(),) * PRESSES, mias_print().agreement)
  await bob_registers(kiosk)
  await kiosk.ctl.on_scanner_event(RegisterGallery((adas,)))
  for i in range(PRESSES):
    p = press(i)
    like_ada = {ada_print: (0, 0, SIMILAR, 0, SIMILAR)} if i < 2 else {}  # noqa: PLR2004
    await kiosk.ctl.on_scanner_event(Pressed(Press(i, p.minutiae, p.probe, p.vs_session, like_ada)))
  blocked = kiosk.ctl.screen
  assert isinstance(blocked, TooSimilarBlocked)
  await kiosk.ctl.ask_admin()
  prompt = ApprovalPrompt(SimilarReview(blocked.registering, blocked.verdict), touched_at=kiosk.t.mono, message=text.NOT_AN_ADMIN)
  await kiosk.ctl.on_scanner_event(Matched(ada_print, ADA))
  assert kiosk.ctl.screen == prompt
  await kiosk.ctl.use_pin()
  await kiosk.ctl.on_scanner_event(Matched(ada_print, ADA))
  assert kiosk.ctl.screen == prompt
  assert kiosk.reader.merges == []
  await kiosk.ctl.on_scanner_event(Matched(uuid4(), ADA))  # another of Ada's fingers
  screen = kiosk.ctl.screen
  assert isinstance(screen, TooSimilarReview) and screen.approval == Approval(ADA, pin_only=False)


async def test_no_print_the_finger_resembles_approves_its_review(kiosk: Kiosk) -> None:
  # Not only the most resembling print: the finger may be identified as a further case-C candidate or a flagged print.
  second, careless = uuid4(), uuid4()
  adas = OtherPrint(second, ADA, Finger.LEFT_INDEX, (points(),) * PRESSES, mias_print().agreement)
  adas_careless = OtherPrint(careless, ADA, Finger.RIGHT_INDEX, (points(),) * PRESSES, mias_print(careless=True).agreement)
  like = {MIA_PRINT: (0, 0, SIMILAR + 1, 0, SIMILAR), second: (0, 0, SIMILAR, 0, SIMILAR), careless: (SIMILAR,) * PRESSES}
  await bob_registers(kiosk)
  await kiosk.ctl.on_scanner_event(RegisterGallery((mias_print(), adas, adas_careless)))
  for i in range(PRESSES):
    p = press(i)
    await kiosk.ctl.on_scanner_event(Pressed(Press(i, p.minutiae, p.probe, p.vs_session, like if i < 2 else {})))  # noqa: PLR2004
  blocked = kiosk.ctl.screen
  assert isinstance(blocked, TooSimilarBlocked) and blocked.verdict.other.print_id == MIA_PRINT
  await kiosk.ctl.ask_admin()
  prompt = ApprovalPrompt(SimilarReview(blocked.registering, blocked.verdict), touched_at=kiosk.t.mono, message=text.NOT_AN_ADMIN)
  for print_id in (second, careless):
    await kiosk.ctl.on_scanner_event(Matched(print_id, ADA))
    assert kiosk.ctl.screen == prompt
  assert kiosk.reader.merges == []
  await kiosk.ctl.on_scanner_event(Matched(uuid4(), ADA))  # an admin's print the finger doesn't resemble
  screen = kiosk.ctl.screen
  assert isinstance(screen, TooSimilarReview) and screen.approval == Approval(ADA, pin_only=False)


async def test_a_scan_on_the_too_similar_screens_changes_nothing(kiosk: Kiosk) -> None:
  # Review Focus 3: the reader is READY on both, so a finger there arrives as a scan, not a press.
  scans = (Matched(uuid4(), ADA), matched(ANN), scanned(ANN), NoMatch(), unmatched())
  await bob_registers(kiosk)
  await give_presses(kiosk, others=(mias_print(),), like_mia=True)
  blocked = kiosk.ctl.screen
  assert isinstance(blocked, TooSimilarBlocked) and kiosk.reader.modes[-1] is ReaderMode.READY
  for ev in scans:
    await kiosk.ctl.on_scanner_event(ev)
    assert kiosk.ctl.screen == blocked
  await kiosk.ctl.ask_admin()
  await kiosk.ctl.on_scanner_event(Matched(uuid4(), ADA))
  review = kiosk.ctl.screen
  assert isinstance(review, TooSimilarReview) and kiosk.reader.modes[-1] is ReaderMode.READY
  for ev in scans:
    await kiosk.ctl.on_scanner_event(ev)
    assert kiosk.ctl.screen == review
  assert blocked.registering.session is not None and len(blocked.registering.session.kept) == PRESSES
  assert len(kiosk.reader.merges) == 1
  assert kiosk.store.punches() == []
  assert not await kiosk.clock.has_fingerprint(BOB)


async def test_an_admins_pin_steps_in_too(kiosk: Kiosk) -> None:
  await bob_registers(kiosk)
  await give_presses(kiosk, others=(mias_print(),), like_mia=True)
  await kiosk.ctl.ask_admin()
  await kiosk.ctl.use_pin()
  await kiosk.type_and_enter(PINS[ADA])
  await kiosk.type_and_enter("1234")
  screen = kiosk.ctl.screen
  assert isinstance(screen, TooSimilarReview) and screen.approval == Approval(ADA, pin_only=True)


async def test_force_save_after_five_presses_asks_an_admin(kiosk: Kiosk) -> None:
  await bob_registers(kiosk)
  await kiosk.ctl.on_scanner_event(RegisterGallery(()))
  for i in range(PRESSES - 1):
    await kiosk.ctl.on_scanner_event(Pressed(press(i)))
  before = kiosk.ctl.screen
  await kiosk.ctl.force_save()
  assert kiosk.ctl.screen == before  # not before 5 presses
  await kiosk.ctl.on_scanner_event(Pressed(press(PRESSES - 1, agrees=False)))
  assert isinstance(kiosk.ctl.screen, Registering) and kiosk.ctl.screen.advice == text.PRESS_ADVICE[Failure.DISAGREES]
  await kiosk.ctl.force_save()
  prompt = kiosk.ctl.screen
  assert isinstance(prompt, ApprovalPrompt) and isinstance(prompt.change, ForceSave)
  assert [p.press_id for p in prompt.change.chosen] == list(range(PRESSES))  # the replaced press too: only 5 were made
  assert kiosk.reader.merges == [tuple(f"probe-{i}".encode() for i in range(PRESSES))]  # merging while an admin comes
  await kiosk.ctl.on_scanner_event(Merged(b"bob"))
  assert kiosk.ctl.screen == prompt  # the template's arrival redraws nothing
  await kiosk.ctl.on_scanner_event(matched(ANN))
  assert kiosk.ctl.screen == ApprovalPrompt(prompt.change, touched_at=prompt.touched_at, message=text.NOT_AN_ADMIN)
  await kiosk.ctl.on_scanner_event(matched(ADA))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu) and screen.info.employee_id == BOB  # saved at once, no registration screen between
  assert len(kiosk.reader.merges) == 1
  [f] = [f for f in kiosk.store.fingerprints() if f.employee_id == BOB]
  assert f.template.get_secret_value() == b"bob"
  [c] = kiosk.store.unsynced_fingerprint_changes()
  assert (c.employee_id, c.action, c.approved_by, c.pin_only) == (BOB, FingerprintChangeAction.ADD, ADA, False)


async def test_force_save_asks_again_though_an_admin_approved_the_change(kiosk: Kiosk) -> None:
  await approved_reregistration(kiosk)
  await kiosk.ctl.on_scanner_event(RegisterGallery(()))
  for i in range(PRESSES):
    await kiosk.ctl.on_scanner_event(Pressed(press(i, agrees=i < PRESSES - 1)))
  await kiosk.ctl.force_save()
  prompt = kiosk.ctl.screen
  assert isinstance(prompt, ApprovalPrompt) and isinstance(prompt.change, ForceSave)
  await kiosk.ctl.use_pin()
  await kiosk.ctl.on_scanner_event(Merged(b"ann"))  # lands on the PIN pad
  await kiosk.type_and_enter(PINS[ADA])
  await kiosk.type_and_enter("1234")
  screen = kiosk.ctl.screen
  assert isinstance(screen, Idle) and screen.message == text.FINGERPRINT_SAVED
  assert {(c.approved_by, c.pin_only) for c in kiosk.store.unsynced_fingerprint_changes()} == {(ADA, True)}


async def test_an_approval_faster_than_the_merge_saves_when_it_lands(kiosk: Kiosk) -> None:
  await bob_registers(kiosk)
  await kiosk.ctl.on_scanner_event(RegisterGallery(()))
  for i in range(PRESSES):
    await kiosk.ctl.on_scanner_event(Pressed(press(i, agrees=i < PRESSES - 1)))
  await kiosk.ctl.force_save()
  await kiosk.ctl.on_scanner_event(matched(ADA))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Registering) and screen.approval == Approval(ADA, pin_only=False)
  await kiosk.ctl.on_scanner_event(Merged(b"bob"))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu) and screen.info.employee_id == BOB
  assert len(kiosk.reader.merges) == 1


async def test_the_resembled_print_approves_no_forced_save(kiosk: Kiosk) -> None:
  # As for a review: the registrant's own finger may be identified as a print its presses resemble.
  ada_print = uuid4()
  await bob_registers(kiosk)
  await kiosk.ctl.on_scanner_event(RegisterGallery(()))
  for i in range(PRESSES):
    p = press(i, agrees=i < PRESSES - 1)
    await kiosk.ctl.on_scanner_event(Pressed(Press(i, p.minutiae, p.probe, p.vs_session, {ada_print: (SIMILAR,)} if i == 0 else {})))
  await kiosk.ctl.force_save()
  prompt = kiosk.ctl.screen
  assert isinstance(prompt, ApprovalPrompt)
  await kiosk.ctl.on_scanner_event(Matched(ada_print, ADA))
  assert kiosk.ctl.screen == ApprovalPrompt(prompt.change, touched_at=prompt.touched_at, message=text.NOT_AN_ADMIN)
  await kiosk.ctl.on_scanner_event(Matched(uuid4(), ADA))  # another of Ada's fingers
  screen = kiosk.ctl.screen
  assert isinstance(screen, Registering) and screen.approval == Approval(ADA, pin_only=False)


async def approved_reregistration(kiosk: Kiosk) -> None:
  """Ann re-registers her left index; Ada approves by finger."""
  await fingerprints_login(kiosk, ANN)
  await kiosk.ctl.choose_action("replace")
  await kiosk.ctl.pick_finger(Finger.LEFT_INDEX)
  await kiosk.ctl.on_scanner_event(matched(ADA))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Registering) and screen.admin_id == ADA


async def test_too_similar_admin_allow_saves_with_approver(kiosk: Kiosk) -> None:
  await approved_reregistration(kiosk)
  await give_presses(kiosk, others=(mias_print(),), like_mia=True)
  screen = kiosk.ctl.screen
  assert isinstance(screen, TooSimilarReview)
  assert (screen.name, screen.verdict.other.finger, screen.template) == ("Mia Lee", Finger.LEFT_INDEX, None)
  assert len(kiosk.reader.merges) == 1  # merging while the admin decides
  await kiosk.ctl.allow()  # before the merge lands: saved when it does
  assert isinstance(kiosk.ctl.screen, TooSimilarReview)
  await kiosk.ctl.on_scanner_event(Merged(b"ann-new"))
  assert kiosk.ctl.screen == Idle(text.FINGERPRINT_SAVED, until=kiosk.t.mono + 3)
  prints = await kiosk.clock.prints_by_finger(ANN)
  assert set(prints) == {Finger.LEFT_INDEX} and prints[Finger.LEFT_INDEX] != ANN_PRINT
  [c] = kiosk.store.unsynced_fingerprint_changes()
  assert (c.action, c.approved_by, c.pin_only) == (FingerprintChangeAction.REPLACE, ADA, False)


async def test_too_similar_admin_reject_saves_nothing(kiosk: Kiosk) -> None:
  await approved_reregistration(kiosk)
  await give_presses(kiosk, others=(mias_print(),), like_mia=True)
  await kiosk.ctl.on_scanner_event(Merged(b"ann-new"))
  review = kiosk.ctl.screen
  assert isinstance(review, TooSimilarReview) and review.template == b"ann-new"
  assert "ann-new" not in repr(review)  # R15
  await kiosk.ctl.reject()
  assert kiosk.ctl.screen == FingerprintActions(
    ANN, "Ann Lee", (Finger.LEFT_INDEX,), touched_at=kiosk.t.mono, message=text.TOO_SIMILAR_REJECTED
  )
  assert await kiosk.clock.prints_by_finger(ANN) == {Finger.LEFT_INDEX: ANN_PRINT}
  assert kiosk.store.unsynced_fingerprint_changes() == []


async def new_employee_registers(kiosk: Kiosk) -> None:
  """Ada adds Zed (10001), then registers his left index in the add-employee flow."""
  await fill_details(kiosk)
  await kiosk.type_and_enter("1111")
  await kiosk.type_and_enter("1111")
  await kiosk.ctl.register()
  await kiosk.ctl.pick_finger(Finger.LEFT_INDEX)
  screen = kiosk.ctl.screen
  assert isinstance(screen, Registering) and screen.admin_id == ADA


async def test_too_similar_in_the_add_employee_flow(kiosk: Kiosk) -> None:
  # The admin adding the employee is present: they review it, and are recorded as its approver.
  await new_employee_registers(kiosk)
  await give_presses(kiosk, others=(mias_print(),), like_mia=True)
  await kiosk.ctl.on_scanner_event(Merged(b"zed"))
  review = kiosk.ctl.screen
  assert isinstance(review, TooSimilarReview) and review.approval == Approval(ADA, pin_only=True)
  await kiosk.ctl.allow()
  assert kiosk.ctl.screen == EmployeeAdded("Zed New", 10001, touched_at=kiosk.t.mono)
  mirror = cast("list[Employee]", kiosk.store.load("employees"))
  kiosk.store.replace_employees([*mirror, *kiosk.store.pending_employees()])  # the database accepts Zed: his rows go out
  [c] = kiosk.store.unsynced_fingerprint_changes()
  assert (c.employee_id, c.approved_by, c.pin_only) == (10001, ADA, True)


async def test_too_similar_review_times_out_like_a_reject(kiosk: Kiosk) -> None:
  await new_employee_registers(kiosk)
  await give_presses(kiosk, others=(mias_print(),), like_mia=True)
  await kiosk.ctl.on_scanner_event(Merged(b"zed"))
  kiosk.t.advance(IDLE_CLOSE_SECS)
  await kiosk.ctl.tick()
  assert kiosk.ctl.screen == EmployeeAdded("Zed New", 10001, touched_at=kiosk.t.mono)  # the employee is already saved
  assert kiosk.store.unsynced_fingerprint_changes() == []


async def test_flagged_print_alerts_and_advises_with_admin(kiosk: Kiosk, alerts: list[tuple[str, str]]) -> None:
  advice = "Mia Lee's left index print looks carelessly registered: have them re-register it."
  await approved_reregistration(kiosk)
  await give_presses(kiosk, others=(mias_print(careless=True),), like_mia=True)
  screen = kiosk.ctl.screen
  assert isinstance(screen, Registering) and screen.verdict == Done(chosen=(0, 1, 2, 3, 4), flagged=(mias_print(careless=True),))
  await kiosk.ctl.on_scanner_event(Merged(b"ann-new"))
  assert kiosk.ctl.screen == Idle(text.FINGERPRINT_SAVED, until=kiosk.t.mono + controller_module.ADVICE_SECS, advice=advice)
  [(subject, body)] = alerts
  assert subject == "Fingerprint print needs re-registering"
  assert advice in body and str(MIA_PRINT) in body
  # The add-employee flow's saved screen carries it too.
  alerts.clear()
  await kiosk.ctl.cancel()
  await new_employee_registers(kiosk)
  await give_presses(kiosk, others=(mias_print(careless=True),), like_mia=True)
  await kiosk.ctl.on_scanner_event(Merged(b"zed"))
  assert kiosk.ctl.screen == EmployeeAdded("Zed New", 10001, touched_at=kiosk.t.mono, advice=advice)
  assert len(alerts) == 1


async def test_flagged_print_alerts_without_advice_alone(kiosk: Kiosk, alerts: list[tuple[str, str]]) -> None:
  await bob_registers(kiosk)
  await give_presses(kiosk, others=(mias_print(careless=True),), like_mia=True)
  await kiosk.ctl.on_scanner_event(Merged(b"bob"))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu) and screen.notice is None  # it would name a coworker to the employee
  [(subject, body)] = alerts
  assert subject == "Fingerprint print needs re-registering" and "Mia Lee's left index" in body


async def test_an_allowed_c_alerts_its_flags_and_a_rejected_one_doesnt(kiosk: Kiosk, alerts: list[tuple[str, str]]) -> None:
  careless = OtherPrint(uuid4(), EVE, Finger.RIGHT_THUMB, (points(),) * PRESSES, mias_print(careless=True).agreement)
  on_both = {MIA_PRINT: (0, 0, SIMILAR, 0, SIMILAR), careless.print_id: (SIMILAR,) * PRESSES}
  for decide, alerted in ((kiosk.ctl.reject, 0), (kiosk.ctl.allow, 1)):
    await kiosk.ctl.cancel()
    await approved_reregistration(kiosk)
    await kiosk.ctl.on_scanner_event(RegisterGallery((mias_print(), careless)))
    for i in range(PRESSES):
      p = press(i)
      await kiosk.ctl.on_scanner_event(Pressed(Press(i, p.minutiae, p.probe, p.vs_session, on_both if i < 2 else {})))  # noqa: PLR2004
    review = kiosk.ctl.screen
    assert isinstance(review, TooSimilarReview) and review.verdict == TooSimilar(mias_print(), (0, 1, 2, 3, 4), (careless,), ())
    await kiosk.ctl.on_scanner_event(Merged(b"ann-new"))
    await decide()
    assert len(alerts) == alerted
  [(_, body)] = alerts
  assert "Eve Lee's right thumb" in body
  advice = text.REREGISTER_ADVICE.format(name="Eve Lee", finger="right thumb")
  assert kiosk.ctl.screen == Idle(text.FINGERPRINT_SAVED, until=kiosk.t.mono + controller_module.ADVICE_SECS, advice=advice)


async def test_cancel_keeps_old_print(kiosk: Kiosk) -> None:
  # Review Focus 4: neither Cancel nor the idle timeout mid-registration touches a re-registration's old print.
  await approved_reregistration(kiosk)
  await give_presses(kiosk)  # the presses pass and the merge is queued
  await kiosk.ctl.cancel()
  assert kiosk.ctl.screen == Idle()
  assert kiosk.reader.modes[-1] is ReaderMode.READY
  await kiosk.ctl.on_scanner_event(Merged(b"ann-new"))  # the queued merge lands after the cancel
  assert kiosk.ctl.screen == Idle()
  assert await kiosk.clock.prints_by_finger(ANN) == {Finger.LEFT_INDEX: ANN_PRINT}
  await approved_reregistration(kiosk)
  await kiosk.ctl.on_scanner_event(RegisterGallery(()))
  await kiosk.ctl.on_scanner_event(Pressed(press(0)))
  await kiosk.ctl.on_scanner_event(EnrollFailed("timeout"))  # the scanner's 30 s without a press
  assert kiosk.ctl.screen == FingerprintActions(
    ANN, "Ann Lee", (Finger.LEFT_INDEX,), touched_at=kiosk.t.mono, message=text.REGISTRATION_TIMED_OUT
  )
  assert await kiosk.clock.prints_by_finger(ANN) == {Finger.LEFT_INDEX: ANN_PRINT}
  assert kiosk.store.unsynced_fingerprint_changes() == []


async def test_override_by_manager_finger_allows_one_punch(kiosk: Kiosk) -> None:
  await kiosk.pin_login(BOB)
  await kiosk.ctl.request_override()
  request = OverrideRequest(BOB, BypassReason.OVERRIDE_NO_FINGERPRINT, None)
  assert kiosk.ctl.screen == OverridePrompt(request, touched_at=kiosk.t.mono)
  await kiosk.ctl.on_scanner_event(matched(MIA))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu)
  assert screen.auth == OverrideGrant(MIA, BypassReason.OVERRIDE_NO_FINGERPRINT, pin_only=False)
  await kiosk.ctl.punch(PunchKind.CLOCK_IN)
  assert isinstance(kiosk.ctl.screen, Confirmation)
  [(punch, override)] = kiosk.store.unsynced_punches()
  assert punch.employee_id == BOB
  assert override is not None
  assert (override.manager_id, override.pin_only) == (MIA, False)
  # The grant was spent: Bob's next punch starts over at the PIN login and needs a new override.
  await kiosk.pin_login(BOB)
  assert isinstance(kiosk.ctl.screen, RegisterPrompt)


async def test_override_by_manager_pin_sets_pin_only(kiosk: Kiosk) -> None:
  await kiosk.pin_login(BOB)
  await kiosk.ctl.request_override()
  await kiosk.ctl.use_pin()
  request = OverrideRequest(BOB, BypassReason.OVERRIDE_NO_FINGERPRINT, None)
  assert kiosk.ctl.screen == PinEntry("username", "", SecretStr(""), request, touched_at=kiosk.t.mono)
  await kiosk.type_and_enter(PINS[MIA])
  await kiosk.type_and_enter("1234")
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu)
  assert screen.auth == OverrideGrant(MIA, BypassReason.OVERRIDE_NO_FINGERPRINT, pin_only=True)
  await kiosk.ctl.punch(PunchKind.CLOCK_IN)
  [(_, override)] = kiosk.store.unsynced_punches()
  assert override is not None
  assert override.pin_only is True


async def test_employee_cannot_override_for_themselves(kiosk: Kiosk) -> None:
  await kiosk.pin_login(MIA)
  await kiosk.ctl.request_override()
  request = OverrideRequest(MIA, BypassReason.OVERRIDE_FINGERPRINT_SKIPPED, None)
  assert kiosk.ctl.screen == OverridePrompt(request, touched_at=kiosk.t.mono)
  await kiosk.ctl.on_scanner_event(matched(MIA))
  assert kiosk.ctl.screen == OverridePrompt(request, touched_at=kiosk.t.mono, message=text.NOT_A_MANAGER)


async def test_rescan_with_override_records_the_pending_punch(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(matched(ANN))
  kiosk.t.advance(31)
  await kiosk.ctl.punch(PunchKind.CLOCK_IN)
  await kiosk.ctl.request_override()
  assert kiosk.ctl.screen == OverridePrompt(
    OverrideRequest(ANN, BypassReason.OVERRIDE_FINGERPRINT_SKIPPED, PunchKind.CLOCK_IN), touched_at=kiosk.t.mono
  )
  await kiosk.ctl.on_scanner_event(matched(MIA))
  assert isinstance(kiosk.ctl.screen, Confirmation)
  [(punch, override)] = kiosk.store.unsynced_punches()
  assert (punch.employee_id, punch.kind) == (ANN, PunchKind.CLOCK_IN)
  assert override is not None
  assert (override.manager_id, override.reason) == (MIA, BypassReason.OVERRIDE_FINGERPRINT_SKIPPED)


async def test_after_hours_idle_is_asleep_until_tapped(kiosk: Kiosk) -> None:
  assert kiosk.ctl.reader_awake
  await kiosk.no_hours()
  assert kiosk.reader.modes[-1] is ReaderMode.CLOSED
  assert not kiosk.ctl.reader_awake
  await kiosk.ctl.tap()
  assert kiosk.reader.modes[-1] is ReaderMode.READY
  kiosk.t.advance(29)
  await kiosk.ctl.tick()
  assert kiosk.reader.modes[-1] is ReaderMode.READY
  kiosk.t.advance(2)
  await kiosk.ctl.tick()
  assert kiosk.reader.modes[-1] is ReaderMode.CLOSED


async def test_reader_closes_only_when_idle_after_hours(kiosk: Kiosk) -> None:
  # Closing time doesn't cut a screen that's open.
  await kiosk.ctl.on_scanner_event(matched(ANN))
  await kiosk.no_hours()
  assert kiosk.reader.modes[-1] is ReaderMode.READY
  await kiosk.ctl.cancel()
  assert kiosk.ctl.screen == Idle()
  assert kiosk.reader.modes[-1] is ReaderMode.CLOSED


async def test_the_reader_stays_open_at_closing_while_a_finger_is_down(kiosk: Kiosk) -> None:
  # Review Focus 2: a scan in progress at closing finishes first (R40.2).
  await kiosk.ctl.on_scanner_event(DOWN)
  await kiosk.no_hours()
  assert kiosk.reader.modes[-1] is ReaderMode.READY
  await kiosk.ctl.on_scanner_event(LIFTED)
  assert kiosk.reader.modes[-1] is ReaderMode.CLOSED


async def test_pin_scan_after_hours_opens_the_reader(kiosk: Kiosk) -> None:
  await kiosk.no_hours()
  assert kiosk.reader.modes[-1] is ReaderMode.CLOSED
  await kiosk.pin_login(ANN)
  assert isinstance(kiosk.ctl.screen, PinScan)
  assert kiosk.reader.modes[-1] is ReaderMode.READY


async def test_set_mode_is_called_only_on_a_change(kiosk: Kiosk) -> None:
  await kiosk.ctl.tick()
  await kiosk.ctl.tick()
  assert kiosk.reader.modes == [ReaderMode.READY]


# -- the reader line: live indicator and lingering message (QoL design §4) -------------------------------------------


async def test_the_indicator_is_dim_until_the_reader_waits(kiosk: Kiosk) -> None:
  assert kiosk.ctl.reader_indicator is ReaderIndicator.BUSY
  await kiosk.ctl.on_scanner_event(WAITING)
  assert kiosk.ctl.reader_indicator is ReaderIndicator.READY


async def test_a_finger_down_shows_scanning(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(WAITING)
  await kiosk.ctl.on_scanner_event(DOWN)
  assert kiosk.ctl.reader_indicator is ReaderIndicator.SCANNING
  assert kiosk.ctl.screen == Idle()


async def test_a_match_shows_read_until_the_lift(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(WAITING)
  await kiosk.ctl.on_scanner_event(DOWN)
  await kiosk.ctl.on_scanner_event(matched(ANN))
  assert kiosk.ctl.reader_indicator is ReaderIndicator.READ
  assert isinstance(kiosk.ctl.screen, Menu)
  await kiosk.ctl.on_scanner_event(LIFTED)
  assert kiosk.ctl.reader_indicator is ReaderIndicator.BUSY
  await kiosk.ctl.on_scanner_event(WAITING)
  assert kiosk.ctl.reader_indicator is ReaderIndicator.READY


async def test_too_close_says_it_matches_more_than_one_person(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(WAITING)
  await kiosk.ctl.on_scanner_event(DOWN)
  read_at = kiosk.t.mono
  await kiosk.ctl.on_scanner_event(NoMatch(ScanReport(ScanResult.TOO_CLOSE, None, None, (), 15)))
  assert kiosk.ctl.reader_indicator is ReaderIndicator.UNRECOGNIZED
  assert kiosk.ctl.screen == Idle()
  assert kiosk.ctl.reader_message == ReaderMessage("Matches more than one person, try again", "unrecognized", read_at)


async def test_plain_no_match_scan_still_says_not_recognized(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(WAITING)
  await kiosk.ctl.on_scanner_event(DOWN)
  read_at = kiosk.t.mono
  await kiosk.ctl.on_scanner_event(unmatched())
  assert kiosk.ctl.reader_message == ReaderMessage("Not recognized, try again", "unrecognized", read_at)


async def test_no_match_is_orange_until_the_lift_and_lingers(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(WAITING)
  await kiosk.ctl.on_scanner_event(DOWN)
  read_at = kiosk.t.mono
  await kiosk.ctl.on_scanner_event(NoMatch())
  assert kiosk.ctl.reader_indicator is ReaderIndicator.UNRECOGNIZED
  assert kiosk.ctl.screen == Idle(), "the idle title no longer changes for reader messages"
  assert kiosk.ctl.reader_message == ReaderMessage("Not recognized, try again", "unrecognized", read_at)
  await kiosk.ctl.on_scanner_event(LIFTED)
  assert kiosk.ctl.reader_indicator is ReaderIndicator.BUSY
  assert kiosk.ctl.reader_message == ReaderMessage("Not recognized, try again", "unrecognized", read_at)
  kiosk.t.advance(3.1)
  await kiosk.ctl.tick()
  assert kiosk.ctl.reader_message is None


async def test_a_failed_read_is_red_for_at_least_a_second(kiosk: Kiosk) -> None:
  # The scanner's order: the finger goes down, the call ends (clearing it), then the retry.
  await kiosk.ctl.on_scanner_event(WAITING)
  await kiosk.ctl.on_scanner_event(DOWN)
  await kiosk.ctl.on_scanner_event(LIFTED)
  failed_at = kiosk.t.mono
  await kiosk.ctl.on_scanner_event(Retry("x"))
  assert kiosk.ctl.reader_indicator is ReaderIndicator.FAILED
  assert kiosk.ctl.reader_message is None, "the status already says it"
  kiosk.t.advance(0.5)
  await kiosk.ctl.on_scanner_event(WAITING)
  assert kiosk.ctl.reader_indicator is ReaderIndicator.FAILED
  kiosk.t.advance(0.6)
  await kiosk.ctl.tick()
  assert kiosk.ctl.reader_indicator is ReaderIndicator.READY
  assert kiosk.ctl.reader_message == ReaderMessage("Couldn't read that, try again", "error", failed_at)


async def test_a_new_finger_ends_the_red_at_once(kiosk: Kiosk) -> None:
  # Review Focus 3.
  await kiosk.ctl.on_scanner_event(DOWN)
  await kiosk.ctl.on_scanner_event(LIFTED)
  await kiosk.ctl.on_scanner_event(Retry("x"))
  kiosk.t.advance(0.3)
  await kiosk.ctl.on_scanner_event(DOWN)
  assert kiosk.ctl.reader_indicator is ReaderIndicator.SCANNING


async def test_red_holds_until_the_reader_waits_again(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(DOWN)
  await kiosk.ctl.on_scanner_event(LIFTED)
  await kiosk.ctl.on_scanner_event(Retry("x"))
  kiosk.t.advance(5)
  await kiosk.ctl.tick()
  assert kiosk.ctl.reader_indicator is ReaderIndicator.FAILED
  await kiosk.ctl.on_scanner_event(WAITING)
  assert kiosk.ctl.reader_indicator is ReaderIndicator.READY
  await kiosk.ctl.on_scanner_event(LIFTED)  # the gap after the next scan
  assert kiosk.ctl.reader_indicator is ReaderIndicator.BUSY


async def fail_a_read_and_settle(kiosk: Kiosk) -> float:
  """A failed read, then the reader waiting again past the red's floor; returns when it failed."""
  await kiosk.ctl.on_scanner_event(DOWN)
  await kiosk.ctl.on_scanner_event(LIFTED)
  failed_at = kiosk.t.mono
  await kiosk.ctl.on_scanner_event(Retry("x"))
  kiosk.t.advance(1.1)
  await kiosk.ctl.on_scanner_event(WAITING)
  assert kiosk.ctl.reader_indicator is ReaderIndicator.READY
  return failed_at


async def test_an_error_message_clears_on_a_screen_change(kiosk: Kiosk) -> None:
  await fail_a_read_and_settle(kiosk)
  assert kiosk.ctl.reader_message is not None
  await kiosk.ctl.use_pin()
  assert kiosk.ctl.reader_message is None


async def test_typing_on_a_pad_keeps_the_message(kiosk: Kiosk) -> None:
  await kiosk.ctl.use_pin()
  await fail_a_read_and_settle(kiosk)
  await kiosk.type_and_enter(PINS[ANN])  # username to PIN: the same screen
  assert isinstance(kiosk.ctl.screen, PinEntry)
  assert kiosk.ctl.reader_message is not None


async def test_an_error_message_on_idle_times_out_after_15_s(kiosk: Kiosk) -> None:
  failed_at = await fail_a_read_and_settle(kiosk)
  kiosk.t.advance(failed_at + 14.9 - kiosk.t.mono)
  await kiosk.ctl.tick()
  assert kiosk.ctl.reader_message is not None
  kiosk.t.advance(0.2)
  await kiosk.ctl.tick()
  assert kiosk.ctl.reader_message is None


async def test_an_error_message_elsewhere_has_no_timeout(kiosk: Kiosk) -> None:
  await kiosk.ctl.use_pin()
  await fail_a_read_and_settle(kiosk)
  kiosk.t.advance(60)
  await kiosk.ctl.tick()
  assert kiosk.ctl.reader_message is not None


async def test_a_success_message_carries_into_the_menu_and_lasts_3_s(kiosk: Kiosk) -> None:
  read_at = kiosk.t.mono
  await kiosk.ctl.on_scanner_event(matched(ANN))
  assert isinstance(kiosk.ctl.screen, Menu)
  assert kiosk.ctl.reader_message == ReaderMessage("Scan successful", "success", read_at)
  kiosk.t.advance(3.1)
  await kiosk.ctl.tick()
  assert kiosk.ctl.reader_message is None


async def test_the_page_hears_a_message_expire(kiosk: Kiosk) -> None:
  # Nothing but time changes: the tick must still tell the page to redraw the reader line.
  await kiosk.ctl.on_scanner_event(matched(ANN))
  heard: list[None] = []
  kiosk.ctl.add_listener(lambda: heard.append(None))
  kiosk.t.advance(3.1)
  await kiosk.ctl.tick()
  assert heard


async def test_a_success_message_clears_on_a_screen_asking_for_a_finger(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(matched(ADA))
  assert kiosk.ctl.reader_message is not None
  await kiosk.ctl.admin()
  assert isinstance(kiosk.ctl.screen, AdminReconfirm)
  assert kiosk.ctl.reader_message is None


async def test_no_messages_where_a_finger_does_nothing(kiosk: Kiosk) -> None:
  await kiosk.ctl.gear()
  assert isinstance(kiosk.ctl.screen, GearMenu)
  await kiosk.ctl.on_scanner_event(DOWN)
  await kiosk.ctl.on_scanner_event(NoMatch())
  assert kiosk.ctl.reader_indicator is ReaderIndicator.UNRECOGNIZED
  assert kiosk.ctl.reader_message is None
  await kiosk.ctl.cancel()
  assert kiosk.ctl.screen == Idle()
  assert kiosk.ctl.reader_message is None


async def test_registration_presses_are_green_or_red_never_orange(kiosk: Kiosk) -> None:
  await kiosk.pin_login(BOB)
  await kiosk.ctl.register()
  await kiosk.ctl.pick_finger(Finger.LEFT_INDEX)
  assert isinstance(kiosk.ctl.screen, Registering)
  await kiosk.ctl.on_scanner_event(RegisterGallery(()))
  await kiosk.ctl.on_scanner_event(DOWN)
  read_at = kiosk.t.mono
  await kiosk.ctl.on_scanner_event(Pressed(press(0)))
  assert kiosk.ctl.reader_indicator is ReaderIndicator.READ
  assert kiosk.ctl.reader_message == ReaderMessage("Scan successful", "success", read_at)
  screen = kiosk.ctl.screen
  assert isinstance(screen, Registering) and screen.kept == 1
  await kiosk.ctl.on_scanner_event(LIFTED)
  await kiosk.ctl.on_scanner_event(WAITING)
  await kiosk.ctl.on_scanner_event(DOWN)
  failed_at = kiosk.t.mono
  await kiosk.ctl.on_scanner_event(PressRetry())
  assert kiosk.ctl.reader_indicator is ReaderIndicator.FAILED
  screen = kiosk.ctl.screen
  assert isinstance(screen, Registering) and screen.kept == 1
  await kiosk.ctl.on_scanner_event(LIFTED)
  kiosk.t.advance(1.1)
  await kiosk.ctl.on_scanner_event(WAITING)
  assert kiosk.ctl.reader_message == ReaderMessage("Couldn't read that, try again", "error", failed_at)


async def test_a_paused_reader_shows_red_for_its_pause_then_recovers(kiosk: Kiosk) -> None:
  heard: list[None] = []
  kiosk.ctl.add_listener(lambda: heard.append(None))
  await kiosk.ctl.on_scanner_event(WAITING)
  await kiosk.ctl.on_scanner_event(ReaderPaused(60.0))
  assert kiosk.ctl.reader_indicator is ReaderIndicator.PAUSED
  assert kiosk.ctl.screen == Idle(), "nothing on screen to wait for"
  assert heard, "the page redraws for an indicator change"
  heard.clear()
  kiosk.t.advance(60.5)
  await kiosk.ctl.tick()
  assert kiosk.ctl.reader_indicator is ReaderIndicator.READY
  assert heard, "the pause ending is a change too, though only time moved"


async def test_the_indicator_shows_the_reader_asleep_after_hours(kiosk: Kiosk) -> None:
  await kiosk.no_hours()
  assert kiosk.ctl.reader_indicator is ReaderIndicator.ASLEEP
  await kiosk.ctl.tap()
  assert kiosk.ctl.reader_indicator is ReaderIndicator.BUSY, "open, but not yet waiting for a finger"
  await kiosk.ctl.on_scanner_event(WAITING)
  assert kiosk.ctl.reader_indicator is ReaderIndicator.READY


# -- admins, exempt employees and adding employees (§6.4) -------------------------------------------------------------


async def test_an_admin_pin_login_skips_the_fingerprint_and_is_recorded(kiosk: Kiosk) -> None:
  await kiosk.pin_login(ADA)
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu) and screen.info.is_admin and screen.auth is None
  [bypass] = kiosk.store.unsynced_bypasses()
  assert (bypass.employee_id, bypass.reason) == (ADA, BypassReason.ADMIN_PIN_LOGIN)


async def test_an_exempt_pin_login_skips_the_fingerprint_and_is_recorded(kiosk: Kiosk) -> None:
  await kiosk.pin_login(EVE)
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu) and not screen.info.is_admin
  assert [b.reason for b in kiosk.store.unsynced_bypasses()] == [BypassReason.EXEMPT_PIN_LOGIN]


async def test_only_an_admins_menu_opens_the_admin_reconfirm(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(matched(MIA))  # a manager
  await kiosk.ctl.admin()
  assert isinstance(kiosk.ctl.screen, Menu)
  await kiosk.ctl.on_scanner_event(matched(ADA))
  await kiosk.ctl.admin()
  assert kiosk.ctl.screen == AdminReconfirm(ADA, touched_at=kiosk.t.mono)


async def test_admin_reconfirm_by_finger(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(matched(ADA))
  await kiosk.ctl.admin()
  await kiosk.ctl.on_scanner_event(matched(ADA))
  assert kiosk.ctl.screen == AdminMenu(ADA, touched_at=kiosk.t.mono)
  assert kiosk.store.unsynced_bypasses() == []  # a fingerprint bypasses nothing


async def test_someone_elses_finger_on_the_reconfirm_opens_their_menu(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(matched(ADA))
  await kiosk.ctl.admin()
  await kiosk.ctl.on_scanner_event(matched(ANN))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu) and screen.info.employee_id == ANN


async def test_admin_reconfirm_by_pin_is_recorded_and_must_be_their_own(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(matched(ADA))
  await kiosk.ctl.admin()
  await kiosk.ctl.use_pin()
  await kiosk.type_and_enter(PINS[MIA])
  await kiosk.type_and_enter("1234")
  screen = kiosk.ctl.screen
  assert isinstance(screen, PinEntry) and screen.error == text.NOT_YOUR_PIN and screen.purpose == PinRecheck(ADA, None)
  await kiosk.type_and_enter(PINS[ADA])
  await kiosk.type_and_enter("1234")
  assert kiosk.ctl.screen == AdminMenu(ADA, touched_at=kiosk.t.mono)
  assert [b.reason for b in kiosk.store.unsynced_bypasses()] == [BypassReason.ADMIN_PIN_LOGIN]


@pytest.mark.parametrize("by", ["finger", "pin"])
async def test_an_admin_switched_off_mid_shift_gets_their_punch_menu_not_the_admin_menu(kiosk: Kiosk, by: str) -> None:
  # Switched off after their menu was drawn: the reconfirm leads back to the punch menu, without the Admin button.
  kiosk.store.insert_punch(
    Punch(id=uuid4(), employee_id=ADA, kind=PunchKind.CLOCK_IN, at=START - timedelta(hours=1), instance_id="1"), origin="db"
  )
  ada = kiosk.clock.employee(ADA)
  assert ada is not None
  await kiosk.ctl.on_scanner_event(matched(ADA))
  await kiosk.ctl.admin()
  others = [e for e in cast("list[Employee]", kiosk.store.load("employees")) if e.id != ADA]
  kiosk.store.replace_table("employees", [*others, ada.model_copy(update={"active": False})])
  kiosk.clock.notify_changed({"employees"})
  if by == "finger":
    await kiosk.ctl.on_scanner_event(matched(ADA))
  else:
    await kiosk.ctl.use_pin()
    await kiosk.type_and_enter(PINS[ADA])
    await kiosk.type_and_enter("1234")
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu)
  assert (screen.info.employee_id, screen.info.is_admin) == (ADA, False)
  assert kiosk.store.unsynced_bypasses() == []  # no admin PIN login was recorded


async def test_an_admins_rescan_takes_their_pin(kiosk: Kiosk) -> None:
  await kiosk.pin_login(ADA)
  kiosk.t.advance(31)
  await kiosk.ctl.punch(PunchKind.CLOCK_IN)
  rescan = kiosk.ctl.screen
  assert isinstance(rescan, Rescan) and rescan.pin_ok
  await kiosk.ctl.use_pin()
  await kiosk.type_and_enter(PINS[ADA])
  await kiosk.type_and_enter("1234")
  assert isinstance(kiosk.ctl.screen, Confirmation)
  [(punch, bypass)] = kiosk.store.unsynced_punches()
  assert bypass is not None and (bypass.reason, bypass.punch_id) == (BypassReason.ADMIN_PIN_LOGIN, punch.id)


async def test_a_fingerprint_employees_rescan_offers_no_pin(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(matched(ANN))
  kiosk.t.advance(31)
  await kiosk.ctl.punch(PunchKind.CLOCK_IN)
  rescan = kiosk.ctl.screen
  assert isinstance(rescan, Rescan) and not rescan.pin_ok
  await kiosk.ctl.use_pin()
  assert kiosk.ctl.screen is rescan


async def admin_menu(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(matched(ADA))
  await kiosk.ctl.admin()
  await kiosk.ctl.on_scanner_event(matched(ADA))
  await kiosk.ctl.add_employee()


async def test_the_details_form_shows_each_error_beside_its_field(kiosk: Kiosk) -> None:
  await admin_menu(kiosk)
  form = kiosk.ctl.screen
  assert isinstance(form, AddEmployeeDetails) and form.groups == ((1, "default"),)
  await kiosk.ctl.submit_details()
  assert dict(cast("AddEmployeeDetails", kiosk.ctl.screen).errors) == {
    "first_name": text.FIRST_NAME_REQUIRED,
    "last_name": text.LAST_NAME_REQUIRED,
  }
  for name, value in (
    ("first_name", "  "),
    ("last_name", " "),
    ("phone", "555-1234"),
    ("email", "zed@example"),
    ("role", "admin"),
    ("group_id", "9"),
  ):
    await kiosk.ctl.edit_details(cast("DetailsField", name), value)
  await kiosk.ctl.submit_details()
  assert dict(cast("AddEmployeeDetails", kiosk.ctl.screen).errors) == {
    "first_name": text.FIRST_NAME_REQUIRED,
    "last_name": text.LAST_NAME_REQUIRED,
    "phone": text.PHONE_INVALID,
    "email": text.EMAIL_INVALID,
    "role": text.ROLE_INVALID,
    "group_id": text.GROUP_INVALID,
  }


async def test_typing_in_the_form_doesnt_count_as_a_screen_change(kiosk: Kiosk) -> None:
  await admin_menu(kiosk)
  changes: list[None] = []
  kiosk.ctl.add_listener(lambda: changes.append(None))
  before = kiosk.ctl.screen
  kiosk.t.advance(100)
  await kiosk.ctl.edit_details("first_name", "Zed")
  assert kiosk.ctl.screen == before and changes == []
  form = kiosk.ctl.screen
  assert isinstance(form, AddEmployeeDetails) and form.first_name == "Zed"
  kiosk.t.advance(100)  # typing keeps the form open past two minutes from its start
  await kiosk.ctl.tick()
  assert isinstance(kiosk.ctl.screen, AddEmployeeDetails)


async def fill_details(kiosk: Kiosk, *, role: str = "manager") -> None:
  await admin_menu(kiosk)
  for name, value in (
    ("first_name", " Zed "),
    ("middle_name", " Q "),
    ("last_name", " New "),
    ("phone", "(555) 123-4567"),
    ("email", "zed@example.com"),
    ("role", role),
  ):
    await kiosk.ctl.edit_details(cast("DetailsField", name), value)
  await kiosk.ctl.submit_details()


async def test_add_an_employee_end_to_end(kiosk: Kiosk) -> None:
  await fill_details(kiosk)
  pin = kiosk.ctl.screen
  assert isinstance(pin, NewPin) and pin.details == NewEmployee(
    "Zed", "Q", "New", "+15551234567", "zed@example.com", EmployeeRole.MANAGER, 1
  )
  assert kiosk.store.pending_employees() == []  # nothing is saved before the PIN
  await kiosk.type_and_enter("2468")
  await kiosk.type_and_enter("1357")
  pin = kiosk.ctl.screen
  assert isinstance(pin, NewPin) and pin.field == "first" and pin.error == text.PIN_MISMATCH
  assert not pin.first.get_secret_value() and not pin.pin.get_secret_value()
  await kiosk.type_and_enter("2468")
  await kiosk.type_and_enter("2468")
  step = kiosk.ctl.screen
  assert isinstance(step, NewFingerprint) and step.employee.id == 10001  # noqa: PLR2004
  assert [e.id for e in kiosk.store.pending_employees()] == [10001]
  await kiosk.ctl.skip_fingerprint()
  assert kiosk.ctl.screen == EmployeeAdded("Zed New", 10001, touched_at=kiosk.t.mono)
  await kiosk.ctl.cancel()  # OK
  assert kiosk.ctl.screen == Idle()
  # They can log in at once; they have no print, so the usual register-or-override prompt follows.
  await kiosk.ctl.use_pin()
  await kiosk.type_and_enter("10001")
  await kiosk.type_and_enter("2468")
  assert isinstance(kiosk.ctl.screen, RegisterPrompt)


async def test_a_short_new_pin_is_refused_at_the_first_entry(kiosk: Kiosk) -> None:
  await fill_details(kiosk)
  await kiosk.type_and_enter("123")
  pin = kiosk.ctl.screen
  assert isinstance(pin, NewPin) and pin.field == "first" and pin.error == text.PIN_LENGTH
  assert pin.pin.get_secret_value() == ""


async def test_a_new_pin_under_four_digits_is_refused(kiosk: Kiosk) -> None:
  await fill_details(kiosk)
  await kiosk.type_and_enter("123")
  await kiosk.type_and_enter("123")
  pin = kiosk.ctl.screen
  assert isinstance(pin, NewPin) and pin.field == "first" and pin.error == text.PIN_LENGTH
  assert kiosk.store.pending_employees() == []


async def test_the_new_employees_fingerprint_is_optional(kiosk: Kiosk) -> None:
  await fill_details(kiosk, role="employee")
  await kiosk.type_and_enter("1111")
  await kiosk.type_and_enter("1111")
  await kiosk.ctl.register()
  await kiosk.ctl.pick_finger(Finger.LEFT_INDEX)
  registering = kiosk.ctl.screen
  assert isinstance(registering, Registering) and registering.added is not None
  await kiosk.ctl.on_scanner_event(EnrollFailed(reason="timeout"))
  retry = kiosk.ctl.screen
  assert isinstance(retry, NewFingerprint) and retry.message == text.REGISTRATION_TIMED_OUT and retry.admin_id == ADA
  await kiosk.ctl.register()
  await kiosk.ctl.pick_finger(Finger.LEFT_INDEX)
  await register_cleanly(kiosk)
  assert kiosk.ctl.screen == EmployeeAdded("Zed New", 10001, touched_at=kiosk.t.mono)
  assert [f.employee_id for f, _ in kiosk.store.unsynced_fingerprints()] == []  # held until the employee is accepted
  assert any(f.employee_id == 10001 for f in kiosk.store.fingerprints())  # noqa: PLR2004


async def test_cancel_on_the_fingerprint_step_counts_as_a_skip(kiosk: Kiosk) -> None:
  await fill_details(kiosk)
  await kiosk.type_and_enter("1111")
  await kiosk.type_and_enter("1111")
  await kiosk.ctl.cancel()
  assert isinstance(kiosk.ctl.screen, EmployeeAdded)
  assert [e.id for e in kiosk.store.pending_employees()] == [10001]


async def test_a_timeout_on_the_fingerprint_step_counts_as_a_skip(kiosk: Kiosk) -> None:
  await fill_details(kiosk)
  await kiosk.type_and_enter("1111")
  await kiosk.type_and_enter("1111")
  kiosk.t.advance(IDLE_CLOSE_SECS)
  await kiosk.ctl.tick()
  assert kiosk.ctl.screen == EmployeeAdded("Zed New", 10001, touched_at=kiosk.t.mono)
  kiosk.t.advance(IDLE_CLOSE_SECS)
  await kiosk.ctl.tick()  # Done itself times out to idle
  assert kiosk.ctl.screen == Idle()


async def test_a_timeout_while_registering_the_new_employee_counts_as_a_skip(kiosk: Kiosk) -> None:
  await fill_details(kiosk)
  await kiosk.type_and_enter("1111")
  await kiosk.type_and_enter("1111")
  await kiosk.ctl.register()
  await kiosk.ctl.pick_finger(Finger.LEFT_INDEX)
  kiosk.t.advance(IDLE_CLOSE_SECS)
  await kiosk.ctl.tick()
  assert kiosk.ctl.screen == EmployeeAdded("Zed New", 10001, touched_at=kiosk.t.mono)


async def test_the_weekly_hours_notice_shows_on_the_confirmation(kiosk: Kiosk) -> None:
  kiosk.store.set_settings(Settings(weekly_hours_alert=Decimal(30)))
  kiosk.clock.notify_changed({"settings"})
  monday = datetime(2026, 10, 26, 0, 1, tzinfo=NY)
  for kind, at in ((PunchKind.CLOCK_IN, monday), (PunchKind.CLOCK_OUT, monday + timedelta(hours=31))):
    kiosk.store.insert_punch(Punch(id=uuid4(), employee_id=ANN, kind=kind, at=at, instance_id="1"), origin="db")
  await kiosk.ctl.on_scanner_event(matched(ANN))
  await kiosk.ctl.punch(PunchKind.CLOCK_IN)
  screen = kiosk.ctl.screen
  assert isinstance(screen, Confirmation) and screen.notices == ("You've worked over 30 hours this week",)


def test_alert_notice_wording() -> None:
  limit = timedelta(hours=37, minutes=30)
  at_limit = Firing(Hook(WEEKLY_HOURS, timedelta(), (CONFIRMATION_NOTICE,)), measure=limit, limit=limit)
  assert text.alert_notice(at_limit) == "You've worked over 37.5 hours this week"
  early = Firing(Hook(WEEKLY_HOURS, timedelta(hours=1), (CONFIRMATION_NOTICE,)), measure=limit, limit=timedelta(hours=40))
  assert text.alert_notice(early) == "You're within 1 h of 40 hours this week"


async def test_a_digit_on_the_idle_screen_opens_the_pad_with_it_typed(kiosk: Kiosk) -> None:
  # Walk up and type: no need to tap "Use PIN" first.
  assert isinstance(kiosk.ctl.screen, Idle)
  await kiosk.ctl.digit("4")
  assert kiosk.ctl.screen == PinEntry("username", "4", SecretStr(""), None, touched_at=kiosk.t.mono)


# -- the gear menu and the Fingerprints flow ---------------------------------------------------------------------------


async def fingerprints_login(kiosk: Kiosk, employee_id: int) -> None:
  await kiosk.ctl.gear()
  await kiosk.ctl.open_fingerprints()
  await kiosk.type_and_enter(PINS[employee_id])
  await kiosk.type_and_enter("1234")


async def test_gear_opens_from_idle_and_the_menu_only(kiosk: Kiosk) -> None:
  await kiosk.ctl.gear()
  assert kiosk.ctl.screen == GearMenu(touched_at=kiosk.t.mono)
  await kiosk.ctl.cancel()
  await kiosk.ctl.on_scanner_event(matched(ANN))
  await kiosk.ctl.gear()
  assert isinstance(kiosk.ctl.screen, GearMenu)
  await kiosk.ctl.cancel()
  await kiosk.ctl.use_pin()
  await kiosk.ctl.gear()
  assert isinstance(kiosk.ctl.screen, PinEntry)


async def test_fingerprints_login_lists_prints_on_file(kiosk: Kiosk) -> None:
  await kiosk.ctl.gear()
  await kiosk.ctl.open_fingerprints()
  assert kiosk.ctl.screen == PinEntry("username", "", SecretStr(""), FingerprintLogin(), touched_at=kiosk.t.mono)
  await kiosk.type_and_enter(PINS[ADA])
  await kiosk.type_and_enter("1234")
  assert kiosk.ctl.screen == FingerprintActions(ADA, "Ada Lee", (), touched_at=kiosk.t.mono)
  assert kiosk.store.unsynced_bypasses() == []  # not recorded as a PIN login
  await kiosk.ctl.cancel()
  await fingerprints_login(kiosk, ANN)
  assert kiosk.ctl.screen == FingerprintActions(ANN, "Ann Lee", (Finger.LEFT_INDEX,), touched_at=kiosk.t.mono)


async def test_remove_needs_an_admin_and_returns_to_idle(kiosk: Kiosk) -> None:
  await fingerprints_login(kiosk, ANN)
  await kiosk.ctl.choose_action("remove")
  assert kiosk.ctl.screen == FingerPick(ANN, "remove", (Finger.LEFT_INDEX,), touched_at=kiosk.t.mono)
  await kiosk.ctl.pick_finger(Finger.LEFT_INDEX)
  change = FingerChange(ANN, "Ann Lee", "remove", Finger.LEFT_INDEX, ANN_PRINT)
  assert kiosk.ctl.screen == ApprovalPrompt(change, touched_at=kiosk.t.mono)
  await kiosk.ctl.on_scanner_event(matched(ADA))
  assert kiosk.ctl.screen == Idle(text.FINGERPRINT_REMOVED, until=kiosk.t.mono + 3)
  assert await kiosk.clock.prints_by_finger(ANN) == {}
  [c] = kiosk.store.unsynced_fingerprint_changes()
  assert (c.action, c.approved_by, c.pin_only) == (FingerprintChangeAction.REMOVE, ADA, False)


async def test_a_non_admin_cannot_approve(kiosk: Kiosk) -> None:
  await fingerprints_login(kiosk, ANN)
  await kiosk.ctl.choose_action("remove")
  await kiosk.ctl.pick_finger(Finger.LEFT_INDEX)
  await kiosk.ctl.on_scanner_event(matched(ANN))
  prompt = kiosk.ctl.screen
  assert isinstance(prompt, ApprovalPrompt) and prompt.message == text.NOT_AN_ADMIN
  await kiosk.ctl.on_scanner_event(matched(MIA))  # a manager
  assert isinstance(kiosk.ctl.screen, ApprovalPrompt)
  await kiosk.ctl.use_pin()
  await kiosk.type_and_enter(PINS[BOB])
  await kiosk.type_and_enter("1234")
  prompt = kiosk.ctl.screen
  assert isinstance(prompt, ApprovalPrompt) and prompt.message == text.NOT_AN_ADMIN
  assert await kiosk.clock.prints_by_finger(ANN) == {Finger.LEFT_INDEX: ANN_PRINT}
  assert kiosk.store.unsynced_fingerprint_changes() == []


async def test_a_deactivated_admin_cannot_approve(kiosk: Kiosk) -> None:
  ada = kiosk.clock.employee(ADA)
  assert ada is not None
  await fingerprints_login(kiosk, ANN)
  others = [e for e in cast("list[Employee]", kiosk.store.load("employees")) if e.id != ADA]
  kiosk.store.replace_table("employees", [*others, ada.model_copy(update={"active": False})])
  kiosk.clock.notify_changed({"employees"})
  await kiosk.ctl.choose_action("remove")
  await kiosk.ctl.pick_finger(Finger.LEFT_INDEX)
  await kiosk.ctl.on_scanner_event(matched(ADA))
  prompt = kiosk.ctl.screen
  assert isinstance(prompt, ApprovalPrompt) and prompt.message == text.NOT_AN_ADMIN
  assert await kiosk.clock.prints_by_finger(ANN) == {Finger.LEFT_INDEX: ANN_PRINT}


async def test_an_admin_approves_their_own_change_at_the_approval_step(kiosk: Kiosk) -> None:
  await fingerprints_login(kiosk, ADA)
  await kiosk.ctl.choose_action("add")
  request = FingerChange(ADA, "Ada Lee", "add", None, None)
  assert kiosk.ctl.screen == ApprovalPrompt(request, touched_at=kiosk.t.mono)
  await kiosk.ctl.use_pin()
  assert kiosk.ctl.screen == PinEntry("username", "", SecretStr(""), request, touched_at=kiosk.t.mono)
  await kiosk.type_and_enter(PINS[ADA])
  await kiosk.type_and_enter("1234")
  approval = Approval(ADA, pin_only=True)
  assert kiosk.ctl.screen == FingerPick(ADA, "add", tuple(Finger), touched_at=kiosk.t.mono, approval=approval)
  await kiosk.ctl.pick_finger(Finger.RIGHT_INDEX)
  change = FingerChange(ADA, "Ada Lee", "add", Finger.RIGHT_INDEX, None)
  assert kiosk.ctl.screen == Registering(
    ADA, touched_at=kiosk.t.mono, finger=Finger.RIGHT_INDEX, change=change, approval=approval, admin_id=ADA
  )
  assert kiosk.reader.modes[-1] is ReaderMode.REGISTER
  await register_cleanly(kiosk, b"ada")
  assert kiosk.ctl.screen == Idle(text.FINGERPRINT_SAVED, until=kiosk.t.mono + 3)
  assert set(await kiosk.clock.prints_by_finger(ADA)) == {Finger.RIGHT_INDEX}
  [c] = kiosk.store.unsynced_fingerprint_changes()
  assert (c.action, c.approved_by, c.pin_only) == (FingerprintChangeAction.ADD, ADA, True)


async def test_replace_keeps_the_old_print_until_saved(kiosk: Kiosk) -> None:
  await fingerprints_login(kiosk, ANN)
  await kiosk.ctl.choose_action("replace")
  await kiosk.ctl.pick_finger(Finger.LEFT_INDEX)
  await kiosk.ctl.on_scanner_event(matched(ADA))
  assert isinstance(kiosk.ctl.screen, Registering)
  await kiosk.ctl.on_scanner_event(EnrollFailed("timeout"))
  assert kiosk.ctl.screen == FingerprintActions(
    ANN, "Ann Lee", (Finger.LEFT_INDEX,), touched_at=kiosk.t.mono, message=text.REGISTRATION_TIMED_OUT
  )
  assert await kiosk.clock.prints_by_finger(ANN) == {Finger.LEFT_INDEX: ANN_PRINT}
  # Back at the actions, still logged in; the next choice needs a new approval.
  await kiosk.ctl.choose_action("replace")
  await kiosk.ctl.pick_finger(Finger.LEFT_INDEX)
  assert isinstance(kiosk.ctl.screen, ApprovalPrompt)
  await kiosk.ctl.on_scanner_event(matched(ADA))
  await register_cleanly(kiosk)
  assert kiosk.ctl.screen == Idle(text.FINGERPRINT_SAVED, until=kiosk.t.mono + 3)
  prints = await kiosk.clock.prints_by_finger(ANN)
  assert set(prints) == {Finger.LEFT_INDEX} and prints[Finger.LEFT_INDEX] != ANN_PRINT
  [(_, replaced)] = kiosk.store.unsynced_fingerprints()
  assert replaced == ANN_PRINT
  [c] = kiosk.store.unsynced_fingerprint_changes()
  assert (c.action, c.approved_by) == (FingerprintChangeAction.REPLACE, ADA)


async def test_abandoning_approval_or_touches_changes_nothing(kiosk: Kiosk) -> None:
  await fingerprints_login(kiosk, ANN)
  await kiosk.ctl.choose_action("replace")
  await kiosk.ctl.pick_finger(Finger.LEFT_INDEX)
  await kiosk.ctl.cancel()
  assert kiosk.ctl.screen == Idle()
  await fingerprints_login(kiosk, ANN)
  await kiosk.ctl.choose_action("replace")
  await kiosk.ctl.pick_finger(Finger.LEFT_INDEX)
  await kiosk.ctl.on_scanner_event(matched(ADA))
  kiosk.t.advance(IDLE_CLOSE_SECS)
  await kiosk.ctl.tick()
  assert kiosk.ctl.screen == Idle()
  assert await kiosk.clock.prints_by_finger(ANN) == {Finger.LEFT_INDEX: ANN_PRINT}
  assert kiosk.store.unsynced_fingerprint_changes() == []


async def test_add_is_approved_before_the_finger_is_picked(kiosk: Kiosk) -> None:
  # Owner, 2026-10-29: the admin approves adding a print first; the employee then picks the finger and registers.
  await fingerprints_login(kiosk, ANN)
  await kiosk.ctl.choose_action("add")
  assert kiosk.ctl.screen == ApprovalPrompt(FingerChange(ANN, "Ann Lee", "add", None, None), touched_at=kiosk.t.mono)
  await kiosk.ctl.on_scanner_event(matched(ADA))
  approval = Approval(ADA, pin_only=False)
  choices = tuple(f for f in Finger if f is not Finger.LEFT_INDEX)
  assert kiosk.ctl.screen == FingerPick(ANN, "add", choices, touched_at=kiosk.t.mono, approval=approval)
  await kiosk.ctl.pick_finger(Finger.RIGHT_INDEX)
  change = FingerChange(ANN, "Ann Lee", "add", Finger.RIGHT_INDEX, None)
  assert kiosk.ctl.screen == Registering(
    ANN, touched_at=kiosk.t.mono, finger=Finger.RIGHT_INDEX, change=change, approval=approval, admin_id=ADA
  )
  await register_cleanly(kiosk, b"ann2")
  assert kiosk.ctl.screen == Idle(text.FINGERPRINT_SAVED, until=kiosk.t.mono + 3)
  assert set(await kiosk.clock.prints_by_finger(ANN)) == {Finger.LEFT_INDEX, Finger.RIGHT_INDEX}
  [c] = kiosk.store.unsynced_fingerprint_changes()
  assert (c.action, c.approved_by, c.pin_only) == (FingerprintChangeAction.ADD, ADA, False)


async def test_cancelling_an_approved_add_at_the_finger_pick_changes_nothing(kiosk: Kiosk) -> None:
  await fingerprints_login(kiosk, ANN)
  await kiosk.ctl.choose_action("add")
  await kiosk.ctl.on_scanner_event(matched(ADA))
  assert isinstance(kiosk.ctl.screen, FingerPick)
  await kiosk.ctl.cancel()
  assert kiosk.ctl.screen == Idle()
  assert await kiosk.clock.prints_by_finger(ANN) == {Finger.LEFT_INDEX: ANN_PRINT}
  assert kiosk.store.unsynced_fingerprint_changes() == []


async def test_add_offers_only_fingers_not_on_file(kiosk: Kiosk) -> None:
  await fingerprints_login(kiosk, ANN)
  await kiosk.ctl.choose_action("add")
  await kiosk.ctl.on_scanner_event(matched(ADA))
  pick = kiosk.ctl.screen
  assert isinstance(pick, FingerPick)
  assert pick.choices == tuple(f for f in Finger if f is not Finger.LEFT_INDEX)
  await kiosk.ctl.pick_finger(Finger.LEFT_INDEX)  # not offered
  assert kiosk.ctl.screen == pick


async def test_first_registration_asks_the_finger(kiosk: Kiosk) -> None:
  await kiosk.pin_login(BOB)
  await kiosk.ctl.register()
  assert kiosk.ctl.screen == FingerPick(BOB, None, tuple(Finger), touched_at=kiosk.t.mono)
  await kiosk.ctl.pick_finger(Finger.RIGHT_THUMB)
  assert kiosk.ctl.screen == Registering(BOB, touched_at=kiosk.t.mono, finger=Finger.RIGHT_THUMB)
  await register_cleanly(kiosk, b"bob")
  assert isinstance(kiosk.ctl.screen, Menu)
  assert set(await kiosk.clock.prints_by_finger(BOB)) == {Finger.RIGHT_THUMB}
  [c] = kiosk.store.unsynced_fingerprint_changes()
  assert (c.action, c.approved_by, c.pin_only) == (FingerprintChangeAction.ADD, None, None)


async def test_add_employee_step_asks_the_finger(kiosk: Kiosk) -> None:
  await fill_details(kiosk)
  await kiosk.type_and_enter("1111")
  await kiosk.type_and_enter("1111")
  await kiosk.ctl.register()
  pick = kiosk.ctl.screen
  assert isinstance(pick, FingerPick) and pick.action is None and pick.added is not None
  await kiosk.ctl.cancel()
  assert kiosk.ctl.screen == EmployeeAdded("Zed New", 10001, touched_at=kiosk.t.mono)


# -- a finger on every PIN pad: it does what it would have done on the screen before Use PIN ----------------------------


async def test_a_finger_on_the_login_pad_opens_the_menu(kiosk: Kiosk) -> None:
  await kiosk.ctl.use_pin()
  await kiosk.ctl.digit("1")  # typed digits are discarded
  await kiosk.ctl.on_scanner_event(matched(ANN))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu) and screen.info.employee_id == ANN and screen.auth is None


async def reconfirm_pad(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(matched(ADA))
  await kiosk.ctl.admin()
  await kiosk.ctl.use_pin()
  screen = kiosk.ctl.screen
  assert isinstance(screen, PinEntry) and screen.purpose == PinRecheck(ADA, None)


async def test_an_admins_finger_on_the_reconfirm_pad_opens_the_admin_menu(kiosk: Kiosk) -> None:
  await reconfirm_pad(kiosk)
  await kiosk.ctl.on_scanner_event(matched(ADA))
  assert kiosk.ctl.screen == AdminMenu(ADA, touched_at=kiosk.t.mono)
  assert kiosk.store.unsynced_bypasses() == []  # a fingerprint bypasses nothing


async def test_someone_elses_finger_on_the_reconfirm_pad_opens_their_menu(kiosk: Kiosk) -> None:
  await reconfirm_pad(kiosk)
  await kiosk.ctl.on_scanner_event(matched(ANN))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu) and screen.info.employee_id == ANN


async def rescan_pad(kiosk: Kiosk) -> None:
  await kiosk.pin_login(ADA)
  kiosk.t.advance(31)
  await kiosk.ctl.punch(PunchKind.CLOCK_IN)
  await kiosk.ctl.use_pin()
  screen = kiosk.ctl.screen
  assert isinstance(screen, PinEntry) and screen.purpose == PinRecheck(ADA, PunchKind.CLOCK_IN)


async def test_a_finger_on_the_rescan_pad_records_the_punch(kiosk: Kiosk) -> None:
  await rescan_pad(kiosk)
  await kiosk.ctl.on_scanner_event(matched(ADA))
  assert isinstance(kiosk.ctl.screen, Confirmation)
  [(punch, bypass)] = kiosk.store.unsynced_punches()
  assert (punch.employee_id, punch.kind, bypass) == (ADA, PunchKind.CLOCK_IN, None)  # a finger, not a PIN


async def test_someone_elses_finger_on_the_rescan_pad_opens_their_menu(kiosk: Kiosk) -> None:
  await rescan_pad(kiosk)
  await kiosk.ctl.on_scanner_event(matched(ANN))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu) and screen.info.employee_id == ANN
  assert kiosk.store.punches() == []


async def override_pad(kiosk: Kiosk) -> OverrideRequest:
  await kiosk.pin_login(BOB)
  await kiosk.ctl.request_override()
  await kiosk.ctl.use_pin()
  request = OverrideRequest(BOB, BypassReason.OVERRIDE_NO_FINGERPRINT, None)
  assert kiosk.ctl.screen == PinEntry("username", "", SecretStr(""), request, touched_at=kiosk.t.mono)
  return request


async def test_a_managers_finger_on_the_override_pad_grants_it(kiosk: Kiosk) -> None:
  await override_pad(kiosk)
  await kiosk.ctl.on_scanner_event(matched(MIA))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu) and screen.info.employee_id == BOB
  assert screen.auth == OverrideGrant(MIA, BypassReason.OVERRIDE_NO_FINGERPRINT, pin_only=False)


async def test_a_non_managers_finger_on_the_override_pad_returns_to_the_prompt(kiosk: Kiosk) -> None:
  request = await override_pad(kiosk)
  await kiosk.ctl.on_scanner_event(matched(ANN))
  assert kiosk.ctl.screen == OverridePrompt(request, touched_at=kiosk.t.mono, message=text.NOT_A_MANAGER)


async def approval_pad(kiosk: Kiosk) -> FingerChange:
  await fingerprints_login(kiosk, ANN)
  await kiosk.ctl.choose_action("remove")
  await kiosk.ctl.pick_finger(Finger.LEFT_INDEX)
  await kiosk.ctl.use_pin()
  change = FingerChange(ANN, "Ann Lee", "remove", Finger.LEFT_INDEX, ANN_PRINT)
  assert kiosk.ctl.screen == PinEntry("username", "", SecretStr(""), change, touched_at=kiosk.t.mono)
  return change


async def test_an_admins_finger_on_the_approval_pad_approves(kiosk: Kiosk) -> None:
  await approval_pad(kiosk)
  await kiosk.ctl.on_scanner_event(matched(ADA))
  assert kiosk.ctl.screen == Idle(text.FINGERPRINT_REMOVED, until=kiosk.t.mono + 3)
  assert await kiosk.clock.prints_by_finger(ANN) == {}


async def test_an_admins_finger_on_the_add_approval_pad_opens_the_finger_pick(kiosk: Kiosk) -> None:
  await fingerprints_login(kiosk, ANN)
  await kiosk.ctl.choose_action("add")
  await kiosk.ctl.use_pin()
  await kiosk.ctl.on_scanner_event(matched(ADA))
  pick = kiosk.ctl.screen
  assert isinstance(pick, FingerPick) and pick.approval == Approval(ADA, pin_only=False)


async def test_a_non_admins_finger_on_the_approval_pad_returns_to_the_prompt(kiosk: Kiosk) -> None:
  change = await approval_pad(kiosk)
  await kiosk.ctl.on_scanner_event(matched(ANN))
  assert kiosk.ctl.screen == ApprovalPrompt(change, touched_at=kiosk.t.mono, message=text.NOT_AN_ADMIN)


async def test_a_finger_on_the_fingerprints_login_pad_opens_their_options(kiosk: Kiosk) -> None:
  await kiosk.ctl.gear()
  await kiosk.ctl.open_fingerprints()
  await kiosk.ctl.on_scanner_event(matched(ANN))
  assert kiosk.ctl.screen == FingerprintActions(ANN, "Ann Lee", (Finger.LEFT_INDEX,), touched_at=kiosk.t.mono)


async def test_an_unknown_employees_finger_on_the_fingerprints_login_pad_changes_nothing(kiosk: Kiosk) -> None:
  await kiosk.ctl.gear()
  await kiosk.ctl.open_fingerprints()
  pad = kiosk.ctl.screen
  await kiosk.ctl.on_scanner_event(Matched(print_id=uuid4(), employee_id=99))
  assert kiosk.ctl.screen == pad


async def test_a_finger_on_the_new_employees_pin_pad_is_ignored(kiosk: Kiosk) -> None:
  # Not a login: the new employee's PIN is being chosen.
  await fill_details(kiosk)
  await kiosk.ctl.digit("2")
  pad = kiosk.ctl.screen
  assert isinstance(pad, NewPin)
  await kiosk.ctl.on_scanner_event(matched(ANN))
  assert kiosk.ctl.screen == pad


async def test_an_unrecognized_finger_leaves_the_pad_alone(kiosk: Kiosk) -> None:
  # Review Focus 4.
  await kiosk.ctl.use_pin()
  await kiosk.ctl.digit("1")
  await kiosk.ctl.digit("0")
  pad = kiosk.ctl.screen
  await kiosk.ctl.on_scanner_event(NoMatch())
  assert kiosk.ctl.screen == pad
  assert isinstance(pad, PinEntry) and pad.username == "10"


async def test_registration_waits_for_the_approvers_finger_to_lift(kiosk: Kiosk) -> None:
  # The admin's finger approves while still down; registering then would read it as the employee's first press.
  await fingerprints_login(kiosk, ANN)
  await kiosk.ctl.choose_action("replace")
  await kiosk.ctl.pick_finger(Finger.LEFT_INDEX)
  await kiosk.ctl.on_scanner_event(DOWN)
  await kiosk.ctl.on_scanner_event(matched(ADA))
  assert isinstance(kiosk.ctl.screen, Registering)
  assert kiosk.reader.modes[-1] is ReaderMode.READY
  await kiosk.ctl.on_scanner_event(LIFTED)
  assert (kiosk.reader.modes[-1], kiosk.reader.registrants) == (ReaderMode.REGISTER, [ANN])


async def test_a_registration_press_keeps_the_reader_registering(kiosk: Kiosk) -> None:
  await kiosk.pin_login(BOB)
  await kiosk.ctl.register()
  await kiosk.ctl.pick_finger(Finger.LEFT_INDEX)
  assert kiosk.reader.modes[-1] is ReaderMode.REGISTER
  await kiosk.ctl.on_scanner_event(DOWN)
  assert kiosk.reader.modes[-1] is ReaderMode.REGISTER
  assert kiosk.reader.registrants == [BOB]


async def test_a_new_registration_after_the_scanner_timed_out_registers_again(kiosk: Kiosk) -> None:
  # On an idle timeout the scanner goes back to READY by itself; the controller must still send the next REGISTER.
  await bob_registers(kiosk)
  await kiosk.ctl.on_scanner_event(EnrollFailed("timeout"))
  await bob_registers(kiosk)
  assert kiosk.reader.modes[-2:] == [ReaderMode.READY, ReaderMode.REGISTER]
  assert kiosk.reader.registrants == [BOB, BOB]


# -- the update banner, from the host's standby screen (standby-screen spec §6.2) ------------------------------------

BANNER = "An update is being installed. The clock will restart in about a minute."  # the file's text; the app words its own toast


def put_banner(status_dir: Path, wall: datetime, age: float = 0) -> None:
  status_dir.mkdir(exist_ok=True)
  path = status_dir / "banner.json"
  path.write_text(json.dumps({"text": BANNER}))
  stamp = wall.timestamp() - age
  os.utime(path, (stamp, stamp))


async def test_app_updating_appears_on_tick_without_notifying(kiosk: Kiosk, tmp_path: Path) -> None:
  heard: list[bool] = []
  kiosk.ctl.add_listener(lambda: heard.append(kiosk.ctl.app_updating))
  put_banner(tmp_path / "status", kiosk.t.wall)
  assert not kiosk.ctl.app_updating
  await kiosk.ctl.tick()
  assert kiosk.ctl.app_updating
  assert heard == []  # the toast polls on its own timer


async def test_app_updating_expires_after_10_minutes(kiosk: Kiosk, tmp_path: Path) -> None:
  put_banner(tmp_path / "status", kiosk.t.wall, age=599)
  await kiosk.ctl.tick()
  assert kiosk.ctl.app_updating
  kiosk.t.advance(1)
  await kiosk.ctl.tick()
  assert not kiosk.ctl.app_updating


async def test_app_updating_ends_with_the_banner_file(kiosk: Kiosk, tmp_path: Path) -> None:
  put_banner(tmp_path / "status", kiosk.t.wall)
  await kiosk.ctl.tick()
  (tmp_path / "status" / "banner.json").unlink()
  await kiosk.ctl.tick()
  assert not kiosk.ctl.app_updating


def put_host_update(status_dir: Path, wall: datetime, **fields: object) -> float:
  """Write `host-update.json` stamped `at` the wall clock now, which is returned."""
  status_dir.mkdir(exist_ok=True)
  at = wall.timestamp()
  data = {"state": "downloaded", "done": 5, "total": 5, "mode": "window", "window_start": "03:00", "at": at} | fields
  (status_dir / "host-update.json").write_text(json.dumps(data))
  os.utime(status_dir / "host-update.json", (at, at))
  return at


async def test_host_update_appears_on_tick_without_notifying(kiosk: Kiosk, tmp_path: Path) -> None:
  # The toasts poll on their own timer, so progress doesn't rebuild the page: no notification.
  heard: list[object] = []
  kiosk.ctl.add_listener(lambda: heard.append(kiosk.ctl.host_update))
  put_host_update(tmp_path / "status", kiosk.t.wall, state="downloading", done=20)
  assert kiosk.ctl.host_update is None
  await kiosk.ctl.tick()
  assert kiosk.ctl.host_update is not None
  assert kiosk.ctl.host_update.done == 20  # noqa: PLR2004
  put_host_update(tmp_path / "status", kiosk.t.wall, state="downloading", done=30)
  await kiosk.ctl.tick()
  assert kiosk.ctl.host_update is not None
  assert kiosk.ctl.host_update.done == 30  # noqa: PLR2004
  assert heard == []


async def test_a_downloaded_window_update_clears_after_five_seconds(kiosk: Kiosk, tmp_path: Path) -> None:
  put_host_update(tmp_path / "status", kiosk.t.wall)
  kiosk.t.advance(1)
  await kiosk.ctl.tick()
  assert kiosk.ctl.host_update is not None
  kiosk.t.advance(5)
  await kiosk.ctl.tick()
  assert kiosk.ctl.host_update is None


async def test_a_downloaded_immediate_update_stays(kiosk: Kiosk, tmp_path: Path) -> None:
  put_host_update(tmp_path / "status", kiosk.t.wall, mode="immediate", window_start=None)
  kiosk.t.advance(60)
  await kiosk.ctl.tick()
  assert kiosk.ctl.host_update is not None


def scanned(employee_id: int) -> Matched:
  """A match carrying its scan report, as the scanner posts it."""
  print_id = MIA_PRINT if employee_id == MIA else ANN_PRINT
  best = ScoredPrint(print_id, employee_id, Finger.LEFT_INDEX, 90)
  return Matched(print_id, employee_id, ScanReport(ScanResult.MATCHED, best, None, ((print_id, 90),), 15))


def unmatched() -> NoMatch:
  return NoMatch(ScanReport(ScanResult.NO_MATCH, None, None, (), 15))


def outcomes(kiosk: Kiosk) -> list[ScanOutcome | None]:
  return [s.outcome for s in kiosk.store.unsynced_scans()]


async def test_scan_then_punch_settles_punched(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(scanned(ANN))
  assert outcomes(kiosk) == []  # a matched scan waits for its outcome
  await kiosk.ctl.punch(PunchKind.CLOCK_IN)
  [scan] = kiosk.store.unsynced_scans()
  [(punch, _)] = kiosk.store.unsynced_punches()
  assert (scan.outcome, scan.punch_id) == (ScanOutcome.PUNCHED, punch.id)


async def test_scan_then_cancel_settles_cancelled(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(scanned(ANN))
  await kiosk.ctl.cancel()
  assert outcomes(kiosk) == [ScanOutcome.CANCELLED]


async def test_menu_left_open_settles_timed_out(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(scanned(ANN))
  kiosk.t.advance(IDLE_CLOSE_SECS + 1)
  await kiosk.ctl.tick()
  assert outcomes(kiosk) == [ScanOutcome.TIMED_OUT]


async def test_next_match_settles_rescanned(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(scanned(ANN))
  kiosk.t.advance(1)
  await kiosk.ctl.on_scanner_event(scanned(MIA))
  assert outcomes(kiosk) == [ScanOutcome.RESCANNED]


async def test_unrecognized_repress_keeps_the_menus_scan(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(scanned(ANN))
  kiosk.t.advance(1)
  await kiosk.ctl.on_scanner_event(unmatched())
  await kiosk.ctl.on_scanner_event(Retry("again", ScanReport(ScanResult.RETRY, None, None, (), 15)))
  assert isinstance(kiosk.ctl.screen, Menu)
  await kiosk.ctl.punch(PunchKind.CLOCK_IN)
  [(punch, _)] = kiosk.store.unsynced_punches()
  matched_scan, *others = kiosk.store.unsynced_scans()
  assert (matched_scan.outcome, matched_scan.punch_id) == (ScanOutcome.PUNCHED, punch.id)
  assert [(o.result, o.outcome) for o in others] == [(ScanResult.NO_MATCH, None), (ScanResult.RETRY, None)]


async def test_a_scanless_match_leaves_the_open_scan_alone(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(scanned(ANN))
  await kiosk.ctl.on_scanner_event(matched(MIA))
  await kiosk.ctl.cancel()
  assert outcomes(kiosk) == [ScanOutcome.CANCELLED]


async def test_rescan_punch_belongs_to_the_new_scan(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(scanned(ANN))
  kiosk.t.advance(RESCAN_AFTER_SECS + 1)
  await kiosk.ctl.punch(PunchKind.CLOCK_IN)
  assert isinstance(kiosk.ctl.screen, Rescan)
  assert outcomes(kiosk) == []  # Menu -> Rescan is still the same visit
  await kiosk.ctl.on_scanner_event(scanned(ANN))
  assert isinstance(kiosk.ctl.screen, Confirmation)
  first, second = kiosk.store.unsynced_scans()
  [(punch, _)] = kiosk.store.unsynced_punches()
  assert (first.outcome, first.punch_id) == (ScanOutcome.RESCANNED, None)
  assert (second.outcome, second.punch_id) == (ScanOutcome.PUNCHED, punch.id)


async def test_gear_from_menu_settles_other(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(scanned(ANN))
  await kiosk.ctl.gear()
  assert outcomes(kiosk) == [ScanOutcome.OTHER]


async def test_a_refused_menu_settles_other_at_once(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(scanned(99))  # not an employee this clock knows, so the menu is refused
  assert isinstance(kiosk.ctl.screen, Idle)
  assert outcomes(kiosk) == [ScanOutcome.OTHER]


async def test_a_match_on_a_screen_that_ignores_it_settles_other_at_once(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(scanned(ANN))
  await kiosk.ctl.gear()
  await kiosk.ctl.on_scanner_event(scanned(MIA))  # GearMenu has no use for a match
  assert isinstance(kiosk.ctl.screen, GearMenu)
  assert outcomes(kiosk) == [ScanOutcome.OTHER, ScanOutcome.OTHER]


async def test_managers_override_scan_settles_other(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(scanned(ANN))
  kiosk.t.advance(RESCAN_AFTER_SECS + 1)
  await kiosk.ctl.punch(PunchKind.CLOCK_IN)
  await kiosk.ctl.request_override()
  await kiosk.ctl.on_scanner_event(scanned(MIA))
  assert isinstance(kiosk.ctl.screen, Confirmation)  # the manager's finger recorded Ann's punch
  assert outcomes(kiosk) == [ScanOutcome.OTHER, ScanOutcome.OTHER]  # both at once, before any tick
  kiosk.t.advance(CONFIRM_SECS + 0.1)
  await kiosk.ctl.tick()
  first, second = kiosk.store.unsynced_scans()
  assert (first.best_employee_id, first.outcome) == (ANN, ScanOutcome.OTHER)  # left for the override prompt
  assert (second.best_employee_id, second.outcome, second.punch_id) == (MIA, ScanOutcome.OTHER, None)


async def test_a_failing_scan_log_still_opens_the_menu(kiosk: Kiosk, monkeypatch: pytest.MonkeyPatch) -> None:
  async def failed(_scan: ScanReport) -> None:
    return None

  monkeypatch.setattr(kiosk.clock, "record_scan", failed)
  await kiosk.ctl.on_scanner_event(scanned(ANN))
  assert isinstance(kiosk.ctl.screen, Menu)
  await kiosk.ctl.punch(PunchKind.CLOCK_IN)
  assert [p.kind for p in kiosk.store.punches(ANN)] == [PunchKind.CLOCK_IN]
  assert outcomes(kiosk) == []
