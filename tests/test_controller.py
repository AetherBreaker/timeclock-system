# Standard library imports
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4
from zoneinfo import ZoneInfo

# Third party imports
import bcrypt
import pytest
from pydantic import SecretBytes, SecretStr
from timeclock_schema.enums import EmployeeRole, OverrideReason, PunchKind
from timeclock_schema.models import BusinessHours, Employee, Fingerprint

# First party imports
from timeclock_system.clock import Clock, OverrideGrant
from timeclock_system.scanner import Enrolled, EnrollFailed, EnrollStage, FingerDown, Matched, NoMatch, ReaderMode, ReaderPaused
from timeclock_system.store import LocalStore
from timeclock_system.sync import DbStatus
from timeclock_system.ui import text
from timeclock_system.ui.controller import (
  Confirmation,
  Idle,
  KioskController,
  Menu,
  OverridePrompt,
  OverrideRequest,
  PinEntry,
  PinScan,
  ReaderIndicator,
  Registering,
  RegisterPrompt,
  Rescan,
)

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import AsyncIterator
  from pathlib import Path

NY = ZoneInfo("America/New_York")
START = datetime(2026, 10, 27, 12, 2, tzinfo=UTC)  # Tuesday 8:02 AM EDT
ANN, BOB, MIA = 1, 2, 3
ANN_PRINT, MIA_PRINT = uuid4(), uuid4()
PINS = {ANN: "1001", BOB: "1002", MIA: "1003"}


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

  def set_mode(self, mode: ReaderMode) -> None:
    self.modes.append(mode)


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
      Employee(id=ANN, name="Ann", pin_username=PINS[ANN], pin_hash=pin_hash, updated_at=START),
      Employee(id=BOB, name="Bob", pin_username=PINS[BOB], pin_hash=pin_hash, updated_at=START),
      Employee(id=MIA, name="Mia", pin_username=PINS[MIA], pin_hash=pin_hash, role=EmployeeRole.MANAGER, updated_at=START),
    ],
  )
  store.replace_table("business_hours", [BusinessHours(id=d, weekday=d, opens=time(0, 0), closes=time(23, 59)) for d in range(1, 8)])
  for print_id, employee_id in ((ANN_PRINT, ANN), (MIA_PRINT, MIA)):
    store.insert_fingerprint(
      Fingerprint(id=print_id, employee_id=employee_id, template=SecretBytes(b"t"), registered_at=START, instance_id="1"),
      origin="db",
    )
  t = FakeTime()
  clock = Clock(store, instance_id="1", tz=NY, alert=lambda _s, _b: None, now=lambda: t.wall)
  await clock.load()
  reader = FakeReaderPort()
  ctl = KioskController(clock, reader, tz=NY, monotonic=lambda: t.mono, wall_now=lambda: t.wall)
  yield Kiosk(ctl, clock, store, t, reader)
  store.close()


def matched(employee_id: int) -> Matched:
  return Matched(print_id=MIA_PRINT if employee_id == MIA else ANN_PRINT, employee_id=employee_id)


def test_format_clock_time() -> None:
  assert text.format_clock_time(START, NY) == "8:02 AM"
  assert text.format_clock_time(datetime(2026, 10, 27, 4, 5, tzinfo=UTC), NY) == "12:05 AM"
  assert text.format_clock_time(datetime(2026, 10, 27, 17, 30, tzinfo=UTC), NY) == "1:30 PM"


def test_format_hours_and_db_status() -> None:
  assert text.format_hours(timedelta(hours=7, minutes=30)) == "7.50 h"
  # Truncated, never rounded, and exact: 7 h 36 min is 7.6 h, which a float division puts just below 7.60.
  assert text.format_hours(timedelta(hours=7, minutes=36)) == "7.60 h"
  assert text.format_hours(timedelta(hours=7, minutes=7, seconds=30)) == "7.12 h"
  assert text.format_hours(timedelta(hours=7, minutes=59, seconds=59)) == "7.99 h"
  assert text.format_hours(timedelta(seconds=35)) == "0.00 h"
  assert text.format_db_status(DbStatus(connected=True, last_contact=START), NY) == "Database connected · last contact 8:02 AM"
  assert text.format_db_status(DbStatus(connected=False, last_contact=None), NY) == "Database offline · last contact never"


async def test_match_opens_the_menu(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(matched(ANN))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu)
  assert screen.info.name == "Ann"
  assert screen.auth is None
  assert screen.info.valid_next == (PunchKind.CLOCK_IN,)


async def test_no_match_shows_message_then_clears(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(NoMatch())
  assert kiosk.ctl.screen == Idle(text.NOT_RECOGNIZED, until=kiosk.t.mono + 3)
  kiosk.t.advance(3.1)
  await kiosk.ctl.tick()
  assert kiosk.ctl.screen == Idle()


async def test_punch_within_30s_records_and_confirms(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(matched(ANN))
  kiosk.t.advance(10)
  await kiosk.ctl.punch(PunchKind.CLOCK_IN)
  screen = kiosk.ctl.screen
  assert isinstance(screen, Confirmation)
  assert screen.text == "Clocked in at 8:02 AM"
  assert screen.hours == "0.00 h"
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


async def test_registration_happy_path_ends_in_the_menu(kiosk: Kiosk) -> None:
  await kiosk.pin_login(BOB)
  await kiosk.ctl.register()
  assert kiosk.ctl.screen == Registering(BOB, 0, 5, touched_at=kiosk.t.mono)
  assert kiosk.reader.modes[-1] is ReaderMode.ENROLL
  for done in range(1, 6):
    await kiosk.ctl.on_scanner_event(EnrollStage(done, 5))
  assert kiosk.ctl.screen == Registering(BOB, 5, 5, touched_at=kiosk.t.mono)
  await kiosk.ctl.on_scanner_event(Enrolled(b"new-template"))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu)
  assert screen.info.employee_id == BOB
  assert kiosk.reader.modes[-1] is ReaderMode.READY
  assert await kiosk.clock.has_fingerprint(BOB)


async def test_registration_timeout_shows_message(kiosk: Kiosk) -> None:
  await kiosk.pin_login(BOB)
  await kiosk.ctl.register()
  await kiosk.ctl.on_scanner_event(EnrollFailed("timeout"))
  assert kiosk.ctl.screen == Idle(text.REGISTRATION_TIMED_OUT, until=kiosk.t.mono + 3)
  assert kiosk.reader.modes[-1] is ReaderMode.READY


async def test_override_by_manager_finger_allows_one_punch(kiosk: Kiosk) -> None:
  await kiosk.pin_login(BOB)
  await kiosk.ctl.request_override()
  request = OverrideRequest(BOB, OverrideReason.NO_FINGERPRINT, None)
  assert kiosk.ctl.screen == OverridePrompt(request, touched_at=kiosk.t.mono)
  await kiosk.ctl.on_scanner_event(matched(MIA))
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu)
  assert screen.auth == OverrideGrant(MIA, OverrideReason.NO_FINGERPRINT, pin_only=False)
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
  request = OverrideRequest(BOB, OverrideReason.NO_FINGERPRINT, None)
  assert kiosk.ctl.screen == PinEntry("username", "", SecretStr(""), request, touched_at=kiosk.t.mono)
  await kiosk.type_and_enter(PINS[MIA])
  await kiosk.type_and_enter("1234")
  screen = kiosk.ctl.screen
  assert isinstance(screen, Menu)
  assert screen.auth == OverrideGrant(MIA, OverrideReason.NO_FINGERPRINT, pin_only=True)
  await kiosk.ctl.punch(PunchKind.CLOCK_IN)
  [(_, override)] = kiosk.store.unsynced_punches()
  assert override is not None
  assert override.pin_only is True


async def test_employee_cannot_override_for_themselves(kiosk: Kiosk) -> None:
  await kiosk.pin_login(MIA)
  await kiosk.ctl.request_override()
  request = OverrideRequest(MIA, OverrideReason.FINGERPRINT_SKIPPED, None)
  assert kiosk.ctl.screen == OverridePrompt(request, touched_at=kiosk.t.mono)
  await kiosk.ctl.on_scanner_event(matched(MIA))
  assert kiosk.ctl.screen == OverridePrompt(request, touched_at=kiosk.t.mono, message=text.NOT_A_MANAGER)


async def test_rescan_with_override_records_the_pending_punch(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(matched(ANN))
  kiosk.t.advance(31)
  await kiosk.ctl.punch(PunchKind.CLOCK_IN)
  await kiosk.ctl.request_override()
  assert kiosk.ctl.screen == OverridePrompt(
    OverrideRequest(ANN, OverrideReason.FINGERPRINT_SKIPPED, PunchKind.CLOCK_IN), touched_at=kiosk.t.mono
  )
  await kiosk.ctl.on_scanner_event(matched(MIA))
  assert isinstance(kiosk.ctl.screen, Confirmation)
  [(punch, override)] = kiosk.store.unsynced_punches()
  assert (punch.employee_id, punch.kind) == (ANN, PunchKind.CLOCK_IN)
  assert override is not None
  assert (override.manager_id, override.reason) == (MIA, OverrideReason.FINGERPRINT_SKIPPED)


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
  # Review Focus 5: closing time doesn't cut a screen that's open or a scan in progress.
  await kiosk.ctl.on_scanner_event(FingerDown())
  await kiosk.ctl.on_scanner_event(matched(ANN))
  await kiosk.no_hours()
  assert kiosk.reader.modes[-1] is ReaderMode.READY
  kiosk.t.advance(5)
  await kiosk.ctl.cancel()
  assert kiosk.ctl.screen == Idle()
  assert kiosk.reader.modes[-1] is ReaderMode.READY, "a finger touched 5 s ago; the 16 s hold isn't over"
  kiosk.t.advance(11.5)
  await kiosk.ctl.tick()
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


async def test_the_indicator_lights_while_a_finger_is_down(kiosk: Kiosk) -> None:
  assert kiosk.ctl.reader_indicator is ReaderIndicator.READY
  await kiosk.ctl.on_scanner_event(FingerDown())
  assert kiosk.ctl.reader_indicator is ReaderIndicator.SCANNING
  assert kiosk.ctl.screen == Idle(), "no Scanning… message any more"
  await kiosk.ctl.on_scanner_event(NoMatch())
  assert kiosk.ctl.reader_indicator is ReaderIndicator.READY


async def test_the_indicator_drops_back_if_a_scan_never_finishes(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(FingerDown())
  kiosk.t.advance(16.5)
  await kiosk.ctl.tick()
  assert kiosk.ctl.reader_indicator is ReaderIndicator.READY


async def test_a_match_ends_the_scanning_indicator(kiosk: Kiosk) -> None:
  await kiosk.ctl.on_scanner_event(FingerDown())
  await kiosk.ctl.on_scanner_event(matched(ANN))
  assert isinstance(kiosk.ctl.screen, Menu)
  assert kiosk.ctl.reader_indicator is ReaderIndicator.READY


async def test_a_paused_reader_shows_red_for_its_pause_then_recovers(kiosk: Kiosk) -> None:
  heard: list[None] = []
  kiosk.ctl.add_listener(lambda: heard.append(None))
  await kiosk.ctl.on_scanner_event(ReaderPaused(60.0))
  assert kiosk.ctl.reader_indicator is ReaderIndicator.PAUSED
  assert kiosk.ctl.screen == Idle(), "nothing on screen to wait for"
  assert heard, "the page redraws for an indicator change"
  kiosk.t.advance(60.5)
  await kiosk.ctl.tick()
  assert kiosk.ctl.reader_indicator is ReaderIndicator.READY


async def test_the_indicator_shows_the_reader_asleep_after_hours(kiosk: Kiosk) -> None:
  await kiosk.no_hours()
  assert kiosk.ctl.reader_indicator is ReaderIndicator.ASLEEP
  await kiosk.ctl.tap()
  assert kiosk.ctl.reader_indicator is ReaderIndicator.READY
