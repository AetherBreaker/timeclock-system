# Standard library imports
import asyncio
from datetime import UTC, datetime, time
from typing import TYPE_CHECKING, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

# Third party imports
import bcrypt
import pytest
from nicegui import ui
from nicegui.testing import user_simulation
from pydantic import SecretBytes, SecretStr
from timeclock_schema.enums import EmployeeRole, PunchKind
from timeclock_schema.models import BusinessHours, Employee, Fingerprint, Group

# First party imports
from timeclock_system.clock import Clock
from timeclock_system.scanner import FingerDown, Matched, ReaderMode, ReaderPaused
from timeclock_system.store import LocalStore
from timeclock_system.sync import DbStatus
from timeclock_system.ui import text
from timeclock_system.ui.controller import KioskController
from timeclock_system.ui.page import build_root

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import AsyncIterator
  from pathlib import Path

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 27, 12, 2, tzinfo=UTC)
ANN_PRINT, ADA_PRINT = uuid4(), uuid4()


class RecordingReader:
  def __init__(self) -> None:
    self.modes: list[ReaderMode] = []

  def set_mode(self, mode: ReaderMode) -> None:
    self.modes.append(mode)


@pytest.fixture
async def controller(tmp_path: Path) -> AsyncIterator[KioskController]:
  store = LocalStore(tmp_path / "clock.sqlite3")
  pin_hash = SecretStr(bcrypt.hashpw(b"1234", bcrypt.gensalt(rounds=4)).decode())
  store.replace_table(
    "employees",
    [
      Employee(id=1, name="Ann", pin_username="12", pin_hash=pin_hash, updated_at=NOW),
      Employee(id=2, name="Ada", pin_username="34", pin_hash=pin_hash, role=EmployeeRole.ADMIN, updated_at=NOW),
    ],
  )
  store.replace_table("groups", [Group(id=1, name="default")])
  store.replace_table("business_hours", [BusinessHours(id=d, weekday=d, opens=time(0, 0), closes=time(23, 59)) for d in range(1, 8)])
  for print_id, employee_id in ((ANN_PRINT, 1), (ADA_PRINT, 2)):
    store.insert_fingerprint(
      Fingerprint(id=print_id, employee_id=employee_id, template=SecretBytes(b"t"), registered_at=NOW, instance_id="1"), origin="db"
    )
  clock = Clock(store, instance_id="1", clock_number=1, tz=NY, alert=lambda _s, _b: None)
  await clock.load()
  yield KioskController(clock, RecordingReader(), tz=NY)
  store.close()


def offline() -> DbStatus:
  return DbStatus(connected=False, last_contact=None)


def unexpected_error(e: Exception) -> None:
  raise AssertionError(e)


async def test_idle_shows_the_prompt_and_offline_status(controller: KioskController, tmp_path: Path) -> None:
  async with user_simulation(build_root(controller, offline, tz=NY, branding_dir=tmp_path, on_error=unexpected_error)) as user:
    await user.open("/")
    await user.should_see(text.IDLE_READY)
    await user.should_see("Database offline · last contact never")


async def test_pin_pad_types_digits(controller: KioskController, tmp_path: Path) -> None:
  async with user_simulation(build_root(controller, offline, tz=NY, branding_dir=tmp_path, on_error=unexpected_error)) as user:
    await user.open("/")
    user.find(kind=ui.button, content=text.USE_PIN).click()
    await user.should_see(text.ENTER_USERNAME)
    user.find(kind=ui.button, content="1").click()
    user.find(kind=ui.button, content="2").click()
    await user.should_see("12")
    user.find(kind=ui.button, content="✓").click()
    await user.should_see(text.ENTER_PIN)


async def test_menu_shows_valid_punch_buttons(controller: KioskController, tmp_path: Path) -> None:
  async with user_simulation(build_root(controller, offline, tz=NY, branding_dir=tmp_path, on_error=unexpected_error)) as user:
    await user.open("/")
    await controller.on_scanner_event(Matched(print_id=ANN_PRINT, employee_id=1))
    await user.should_see("Ann")
    await user.should_see("Clock in")
    await user.should_not_see("Clock out")


async def test_missing_logo_still_renders(controller: KioskController, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
  async with user_simulation(
    build_root(controller, offline, tz=NY, branding_dir=tmp_path / "missing", on_error=unexpected_error)
  ) as user:
    await user.open("/")
    await user.should_see(text.IDLE_READY)
  assert any("logo" in r.getMessage() for r in caplog.records)


async def test_a_failing_button_reaches_on_error(controller: KioskController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
  # The app wires on_error to a fatal shutdown; NiceGUI alone would only log the exception.
  async def broken() -> None:
    raise RuntimeError("store failed")

  monkeypatch.setattr(controller, "use_pin", broken)
  errors: list[Exception] = []
  async with user_simulation(build_root(controller, offline, tz=NY, branding_dir=tmp_path, on_error=errors.append)) as user:
    await user.open("/")
    user.find(kind=ui.button, content=text.USE_PIN).click()
    for _ in range(50):  # the click's handler runs as a NiceGUI background task
      if errors:
        break
      await asyncio.sleep(0.02)
  assert [str(e) for e in errors] == ["store failed"]


async def test_the_whole_idle_screen_wakes_the_reader(
  controller: KioskController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
  # R40.2: a tap anywhere, not only on the logo, prompt or button. The layer is fixed over the whole viewport.
  taps: list[None] = []

  async def tap() -> None:
    taps.append(None)

  monkeypatch.setattr(controller, "tap", tap)
  async with user_simulation(build_root(controller, offline, tz=NY, branding_dir=tmp_path, on_error=unexpected_error)) as user:
    await user.open("/")
    [layer] = user.find(marker="idle-tap").elements
    assert {"fixed", "inset-0"} <= set(layer.classes)
    user.find(marker="idle-tap").click()
    for _ in range(50):  # the click's handler runs as a NiceGUI background task
      if taps:
        break
      await asyncio.sleep(0.02)
  assert taps == [None]


async def test_a_double_tap_anywhere_ends_the_confirmation(controller: KioskController, tmp_path: Path) -> None:
  async with user_simulation(build_root(controller, offline, tz=NY, branding_dir=tmp_path, on_error=unexpected_error)) as user:
    await user.open("/")
    await controller.on_scanner_event(Matched(print_id=ANN_PRINT, employee_id=1))
    await controller.punch(PunchKind.CLOCK_IN)
    await user.should_see("Clocked in at")
    [layer] = user.find(marker="confirm-tap").elements
    assert {"fixed", "inset-0"} <= set(layer.classes)
    user.find(marker="confirm-tap").click()
    user.find(marker="confirm-tap").click()
    await user.should_see(text.IDLE_READY)


async def test_the_reader_indicator_shows_on_every_screen_and_follows_the_scan(controller: KioskController, tmp_path: Path) -> None:
  async with user_simulation(build_root(controller, offline, tz=NY, branding_dir=tmp_path, on_error=unexpected_error)) as user:
    await user.open("/")
    [icon] = user.find(marker="reader-indicator").elements
    assert {"fixed", "bottom-2", "left-4"} <= set(icon.classes)
    assert "reader-ready" in icon.classes
    await controller.on_scanner_event(FingerDown())
    await user.should_see(marker="reader-scanning")
    await controller.on_scanner_event(Matched(print_id=ANN_PRINT, employee_id=1))
    await user.should_see("Ann")
    await user.should_see(marker="reader-ready")  # on the menu too
    await controller.on_scanner_event(ReaderPaused(60.0))
    await user.should_see(marker="reader-paused")
    await user.should_not_see("Scanning")


async def test_the_reader_indicator_says_how_to_wake_a_sleeping_reader(tmp_path: Path) -> None:
  store = LocalStore(tmp_path / "clock.sqlite3")
  clock = Clock(store, instance_id="1", clock_number=1, tz=NY, alert=lambda _s, _b: None)
  await clock.load()  # no business hours: the reader sleeps
  controller = KioskController(clock, RecordingReader(), tz=NY)
  async with user_simulation(build_root(controller, offline, tz=NY, branding_dir=tmp_path, on_error=unexpected_error)) as user:
    await user.open("/")
    await user.should_see(marker="reader-asleep")
    await user.should_see(text.READER_ASLEEP)
  store.close()


async def test_the_admin_button_is_only_on_an_admins_menu(controller: KioskController, tmp_path: Path) -> None:
  ctl = controller
  async with user_simulation(build_root(ctl, offline, tz=NY, branding_dir=tmp_path, on_error=unexpected_error)) as user:
    await user.open("/")
    await ctl.on_scanner_event(Matched(print_id=ANN_PRINT, employee_id=1))
    await user.should_see("Ann")
    await user.should_not_see(text.ADMIN)
    await ctl.on_scanner_event(Matched(print_id=ADA_PRINT, employee_id=2))
    await user.should_see("Ada")
    user.find(kind=ui.button, content=text.ADMIN).click()
    await user.should_see(text.ADMIN_RECONFIRM)
    await ctl.on_scanner_event(Matched(print_id=ADA_PRINT, employee_id=2))
    await user.should_see(text.ADMIN_MENU)
    user.find(kind=ui.button, content=text.ADD_EMPLOYEE).click()
    await user.should_see(marker="field-name")


async def test_typed_form_text_survives_a_finger_touch_and_errors(controller: KioskController, tmp_path: Path) -> None:
  ctl = controller
  async with user_simulation(build_root(ctl, offline, tz=NY, branding_dir=tmp_path, on_error=unexpected_error)) as user:
    await user.open("/")
    await ctl.on_scanner_event(Matched(print_id=ADA_PRINT, employee_id=2))
    await ctl.admin()
    await ctl.on_scanner_event(Matched(print_id=ADA_PRINT, employee_id=2))
    await ctl.add_employee()
    await user.should_see(marker="field-name")
    user.find(marker="field-phone").type("555-12")
    for _ in range(50):
      if getattr(ctl.screen, "phone", "") == "555-12":
        break
      await asyncio.sleep(0.02)
    phone = next(iter(user.find(marker="field-phone").elements))
    await ctl.on_scanner_event(FingerDown())  # the indicator changes; the form must not be redrawn
    await user.should_see(marker="reader-scanning")
    assert next(iter(user.find(marker="field-phone").elements)) is phone
    user.find(kind=ui.button, content=text.NEXT).click()
    await user.should_see(text.NAME_REQUIRED)
    await user.should_see(text.PHONE_INVALID)
    redrawn = next(iter(user.find(marker="field-phone").elements))
    assert cast("ui.input", redrawn).value == "555-12"  # the redraw keeps what was typed
