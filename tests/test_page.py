# Standard library imports
import asyncio
from datetime import UTC, datetime, time
from typing import TYPE_CHECKING
from uuid import uuid4
from zoneinfo import ZoneInfo

# Third party imports
import bcrypt
import pytest
from nicegui import ui
from nicegui.testing import user_simulation
from pydantic import SecretBytes, SecretStr
from timeclock_schema.enums import PunchKind
from timeclock_schema.models import BusinessHours, Employee, Fingerprint

# First party imports
from timeclock_system.clock import Clock
from timeclock_system.scanner import Matched, ReaderMode
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
ANN_PRINT = uuid4()


class RecordingReader:
  def __init__(self) -> None:
    self.modes: list[ReaderMode] = []

  def set_mode(self, mode: ReaderMode) -> None:
    self.modes.append(mode)


@pytest.fixture
async def controller(tmp_path: Path) -> AsyncIterator[KioskController]:
  store = LocalStore(tmp_path / "clock.sqlite3")
  pin_hash = SecretStr(bcrypt.hashpw(b"1234", bcrypt.gensalt(rounds=4)).decode())
  store.replace_table("employees", [Employee(id=1, name="Ann", pin_username="12", pin_hash=pin_hash, updated_at=NOW)])
  store.replace_table("business_hours", [BusinessHours(id=d, weekday=d, opens=time(0, 0), closes=time(23, 59)) for d in range(1, 8)])
  store.insert_fingerprint(
    Fingerprint(id=ANN_PRINT, employee_id=1, template=SecretBytes(b"t"), registered_at=NOW, instance_id="1"), origin="db"
  )
  clock = Clock(store, instance_id="1", tz=NY, alert=lambda _s, _b: None)
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
