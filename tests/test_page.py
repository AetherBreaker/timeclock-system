# Standard library imports
import asyncio
from datetime import UTC, datetime, time
from importlib.metadata import version
from typing import TYPE_CHECKING, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

# Third party imports
import bcrypt
import pytest
from nicegui import ui
from nicegui.events import GenericEventArguments
from nicegui.testing import user_simulation
from pydantic import SecretBytes, SecretStr
from timeclock_schema.enums import EmployeeRole, Finger, PunchKind
from timeclock_schema.models import BusinessHours, Employee, Fingerprint, Group

# First party imports
from timeclock_system.clock import Clock
from timeclock_system.scanner import FingerDown, Matched, ReaderMode, ReaderPaused
from timeclock_system.store import LocalStore
from timeclock_system.sync import DbStatus
from timeclock_system.ui import page, text
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
      Employee(id=1, first_name="Ann", last_name="Lee", pin_username="12", pin_hash=pin_hash, updated_at=NOW),
      Employee(id=2, first_name="Ada", last_name="Lee", pin_username="34", pin_hash=pin_hash, role=EmployeeRole.ADMIN, updated_at=NOW),
      Employee(id=3, first_name="Bob", last_name="Lee", pin_username="56", pin_hash=pin_hash, updated_at=NOW),
      Employee(id=4, first_name="Cal", last_name="Lee", pin_username="78", pin_hash=pin_hash, updated_at=NOW),
    ],
  )
  store.replace_table("groups", [Group(id=1, name="default")])
  store.replace_table("business_hours", [BusinessHours(id=d, weekday=d, opens=time(0, 0), closes=time(23, 59)) for d in range(1, 8)])
  prints = [(ANN_PRINT, 1, Finger.LEFT_INDEX), (ADA_PRINT, 2, Finger.LEFT_INDEX), *((uuid4(), 4, f) for f in Finger)]  # Cal: all ten
  for print_id, employee_id, finger in prints:
    store.insert_fingerprint(
      Fingerprint(id=print_id, employee_id=employee_id, finger=finger, template=SecretBytes(b"t"), registered_at=NOW, instance_id="1"),
      origin="db",
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
    await user.should_see(marker="field-first_name")


async def test_typed_form_text_survives_a_finger_touch_and_errors(controller: KioskController, tmp_path: Path) -> None:
  ctl = controller
  async with user_simulation(build_root(ctl, offline, tz=NY, branding_dir=tmp_path, on_error=unexpected_error)) as user:
    await user.open("/")
    await ctl.on_scanner_event(Matched(print_id=ADA_PRINT, employee_id=2))
    await ctl.admin()
    await ctl.on_scanner_event(Matched(print_id=ADA_PRINT, employee_id=2))
    await ctl.add_employee()
    await user.should_see(marker="field-first_name")
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
    await user.should_see(text.FIRST_NAME_REQUIRED)
    await user.should_see(text.PHONE_INVALID)
    redrawn = next(iter(user.find(marker="field-phone").elements))
    assert cast("ui.input", redrawn).value == "555-12"  # the redraw keeps what was typed


async def test_the_phone_field_masks_its_digits(controller: KioskController, tmp_path: Path) -> None:
  # The browser applies the mask; the backspace sequence is checked by hand on the clock (no real-browser tests).
  ctl = controller
  async with user_simulation(build_root(ctl, offline, tz=NY, branding_dir=tmp_path, on_error=unexpected_error)) as user:
    await user.open("/")
    await ctl.on_scanner_event(Matched(print_id=ADA_PRINT, employee_id=2))
    await ctl.admin()
    await ctl.on_scanner_event(Matched(print_id=ADA_PRINT, employee_id=2))
    await ctl.add_employee()
    await user.should_see(marker="field-phone")
    [field] = user.find(marker="field-phone").elements
    assert field.props["mask"] == "(###) ###-####"
    assert field.props["unmasked-value"] is True
    [other] = user.find(marker="field-email").elements
    assert "mask" not in other.props


async def test_the_page_carries_the_cursor_script_and_no_blanket_rule(controller: KioskController, tmp_path: Path) -> None:
  async with user_simulation(build_root(controller, offline, tz=NY, branding_dir=tmp_path, on_error=unexpected_error)) as user:
    client = await user.open("/")
    head = client._head_html  # pyright: ignore[reportPrivateUsage]  # NiceGUI has no public reader
  assert page.KIOSK_CURSOR in head
  assert 'addStyle("* { cursor: none' not in head  # the old blanket ui.add_css rule


async def press(keyboard: ui.keyboard, code: str, name: str) -> None:
  # Through the element's own handler, as a browser's key event arrives: NiceGUI inspects the page's handler before
  # calling it, so the handler's annotations must resolve at runtime.
  args = {"action": "keydown", "repeat": False, "altKey": False, "ctrlKey": False, "metaKey": False, "shiftKey": False}
  event = GenericEventArguments(sender=keyboard, client=keyboard.client, args=args | {"key": name, "code": code, "location": 0})
  keyboard._handle_key(event)  # pyright: ignore[reportPrivateUsage]
  await asyncio.sleep(0.05)  # the handler runs as a task


async def test_number_row_and_numpad_keys_type_into_the_pin_pad(controller: KioskController, tmp_path: Path) -> None:
  async with user_simulation(build_root(controller, offline, tz=NY, branding_dir=tmp_path, on_error=unexpected_error)) as user:
    await user.open("/")
    user.find(kind=ui.button, content=text.USE_PIN).click()
    await user.should_see(text.ENTER_USERNAME)
    keyboard = next(iter(user.find(kind=ui.keyboard).elements))
    await press(keyboard, "Digit1", "1")
    await press(keyboard, "Numpad2", "End")  # NumLock off: the key's name isn't a digit, its code still is
    await user.should_see("12")
    await press(keyboard, "NumpadEnter", "Enter")
    await user.should_see(text.ENTER_PIN)


async def test_the_page_keeps_focus_off_buttons(controller: KioskController, tmp_path: Path) -> None:
  # NiceGUI's ui.keyboard drops every key while a button has focus, and a tap focuses the button it lands on.
  async with user_simulation(build_root(controller, offline, tz=NY, branding_dir=tmp_path, on_error=unexpected_error)) as user:
    client = await user.open("/")
    head = client._head_html  # pyright: ignore[reportPrivateUsage]
  assert page.NO_BUTTON_FOCUS in head


async def press_held(keyboard: ui.keyboard, code: str, name: str, repeats: int) -> None:
  # A held key: one keydown, then the browser's auto-repeat keydowns.
  for repeat in [False] + [True] * repeats:
    args = {"action": "keydown", "repeat": repeat, "altKey": False, "ctrlKey": False, "metaKey": False, "shiftKey": False}
    event = GenericEventArguments(sender=keyboard, client=keyboard.client, args=args | {"key": name, "code": code, "location": 0})
    keyboard._handle_key(event)  # pyright: ignore[reportPrivateUsage]
    await asyncio.sleep(0.05)


async def test_a_held_backspace_keeps_deleting(controller: KioskController, tmp_path: Path) -> None:
  async with user_simulation(build_root(controller, offline, tz=NY, branding_dir=tmp_path, on_error=unexpected_error)) as user:
    await user.open("/")
    user.find(kind=ui.button, content=text.USE_PIN).click()
    keyboard = next(iter(user.find(kind=ui.keyboard).elements))
    for d in "123":
      await press(keyboard, f"Digit{d}", d)
    shown = next(iter(user.find(marker="pin-shown").elements))
    assert shown.text == "123"  # pyright: ignore[reportAttributeAccessIssue]
    await press_held(keyboard, "Backspace", "Backspace", repeats=2)
    shown = next(iter(user.find(marker="pin-shown").elements))
    assert shown.text == ""  # pyright: ignore[reportAttributeAccessIssue]  # one delete would leave "12"


async def test_the_version_shows_in_the_corner(controller: KioskController, tmp_path: Path) -> None:
  async with user_simulation(build_root(controller, offline, tz=NY, branding_dir=tmp_path, on_error=unexpected_error)) as user:
    await user.open("/")
    await user.should_see(f"v{version('timeclock-system')}")


async def fingerprints_as(controller: KioskController, username: str) -> None:
  await controller.gear()
  await controller.open_fingerprints()
  for typed in (username, "1234"):
    for d in typed:
      await controller.digit(d)
    await controller.enter()


async def test_gear_shows_on_idle_and_menu_only(controller: KioskController, tmp_path: Path) -> None:
  async with user_simulation(build_root(controller, offline, tz=NY, branding_dir=tmp_path, on_error=unexpected_error)) as user:
    await user.open("/")
    await user.should_see(marker="gear")
    user.find(marker="gear").click()
    await user.should_see(text.FINGERPRINTS)
    await user.should_see(text.CANCEL)
    await user.should_not_see(marker="gear")
    await controller.cancel()
    await controller.on_scanner_event(Matched(print_id=ANN_PRINT, employee_id=1))
    await user.should_see("Clock in")
    await user.should_see(marker="gear")
    await controller.cancel()
    await controller.use_pin()
    await user.should_see(text.ENTER_USERNAME)
    await user.should_not_see(marker="gear")


async def test_version_sits_right_of_the_gear(controller: KioskController, tmp_path: Path) -> None:
  async with user_simulation(build_root(controller, offline, tz=NY, branding_dir=tmp_path, on_error=unexpected_error)) as user:
    await user.open("/")
    [gear] = user.find(marker="gear").elements
    [label] = user.find(marker="version").elements
    assert {"fixed", "top-2", "left-4", "z-20"} <= set(gear.classes)
    assert {"fixed", "left-16"} <= set(label.classes)


@pytest.mark.parametrize(
  ("username", "offered", "absent"),
  [
    ("56", [text.ADD_FINGER], [text.REREGISTER_FINGER, text.REMOVE_FINGER]),
    ("12", [text.REREGISTER_FINGER, text.ADD_FINGER, text.REMOVE_FINGER], []),
    ("78", [text.REREGISTER_FINGER, text.REMOVE_FINGER], [text.ADD_FINGER]),
  ],
)
async def test_fingerprint_actions_offer_only_what_applies(
  controller: KioskController, tmp_path: Path, username: str, offered: list[str], absent: list[str]
) -> None:
  async with user_simulation(build_root(controller, offline, tz=NY, branding_dir=tmp_path, on_error=unexpected_error)) as user:
    await user.open("/")
    await fingerprints_as(controller, username)
    await user.should_see(text.CANCEL)
    for label in offered:
      await user.should_see(kind=ui.button, content=label)
    for label in absent:
      await user.should_not_see(kind=ui.button, content=label)


async def test_finger_pick_shows_one_button_per_choice(controller: KioskController, tmp_path: Path) -> None:
  async with user_simulation(build_root(controller, offline, tz=NY, branding_dir=tmp_path, on_error=unexpected_error)) as user:
    await user.open("/")
    await fingerprints_as(controller, "12")
    await user.should_see(kind=ui.button, content=text.ADD_FINGER)
    user.find(kind=ui.button, content=text.ADD_FINGER).click()
    await user.should_see(text.PICK_FINGER["add"])
    for finger in Finger:
      if finger is Finger.LEFT_INDEX:
        await user.should_not_see(kind=ui.button, content=text.FINGER_LABELS[finger])
      else:
        await user.should_see(kind=ui.button, content=text.FINGER_LABELS[finger])


async def test_approval_prompt_names_the_change(controller: KioskController, tmp_path: Path) -> None:
  async with user_simulation(build_root(controller, offline, tz=NY, branding_dir=tmp_path, on_error=unexpected_error)) as user:
    await user.open("/")
    await fingerprints_as(controller, "12")
    await user.should_see(kind=ui.button, content=text.REMOVE_FINGER)
    user.find(kind=ui.button, content=text.REMOVE_FINGER).click()
    await user.should_see(kind=ui.button, content="Left index")
    user.find(kind=ui.button, content="Left index").click()
    await user.should_see("Admin: approve removing Ann Lee's left index print")
    await user.should_see(text.APPROVE_HOW)
    await user.should_see(kind=ui.button, content=text.USE_PIN)
