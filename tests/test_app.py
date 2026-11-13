# Standard library imports
import asyncio
import importlib
import inspect
import sys
import types
from typing import TYPE_CHECKING, Any, ClassVar, override
from zoneinfo import ZoneInfo

# Third party imports
import pytest
import uvicorn
from timeclock_schema.enums import ScanResult
from timeclock_schema.models import Settings

# First party imports
from timeclock_system.clock import Clock
from timeclock_system.scanner import ScanReport
from timeclock_system.store import LocalStore

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Iterator
  from pathlib import Path

NY = ZoneInfo("America/New_York")
NEW_MARGIN = 7
NEW_MIN_SCORE = 50


class FakeReader:
  """Stands in for libfprint, which doesn't exist on Windows."""


class FailingClock(Clock):
  @override
  async def load(self) -> None:
    raise RuntimeError("stored data failed validation")


class FakeScanner:
  """Records the gallery, margin and minimum score the app hands over; every other call does nothing."""

  def __init__(self, *_args: object) -> None:
    self.galleries: list[tuple[object, int, int]] = []

  def set_gallery(self, prints: object, margin: int, min_score: int) -> None:
    self.galleries.append((prints, margin, min_score))

  def __getattr__(self, _name: str) -> Callable[..., None]:
    return lambda *_a, **_k: None


class FakeSync:
  """Counts wake-ups; never connects."""

  def __init__(self, *_args: object, **_kwargs: object) -> None:
    self.wakes = 0

  def wake(self) -> None:
    self.wakes += 1

  def add_status_listener(self, _cb: object) -> None:
    pass

  async def run(self) -> None:
    await asyncio.Event().wait()


class RecordingClock(Clock):
  instances: ClassVar[list[RecordingClock]] = []

  def __init__(self, *args: Any, **kwargs: Any) -> None:
    super().__init__(*args, **kwargs)
    self.instances.append(self)


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
  """Run serve() with fakes for the hardware and NiceGUI, and capture what it hands to them."""
  yield from serve_with(tmp_path, monkeypatch, {"Clock": FailingClock})


def serve_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patches: dict[str, Any]) -> Iterator[dict[str, Any]]:
  fake_reader_module = types.ModuleType("timeclock_system.fprint_reader")
  fake_reader_module.FprintReader = FakeReader  # pyright: ignore[reportAttributeAccessIssue]
  monkeypatch.setitem(sys.modules, "timeclock_system.fprint_reader", fake_reader_module)
  monkeypatch.delitem(sys.modules, "timeclock_system.app", raising=False)
  app_module = importlib.import_module("timeclock_system.app")
  monkeypatch.setattr(uvicorn.Server, "capture_signals", uvicorn.Server.capture_signals)  # restored after serve() patches it

  settings = types.SimpleNamespace(
    store_path=tmp_path / "clock.sqlite3",
    instance_id="1",
    clock_number=1,
    tz=NY,
    conninfo=lambda: "host=127.0.0.1",
    crash_alerts_dir=tmp_path / "crash",
    heartbeat_path=tmp_path / "heartbeat.txt",
    status_dir=tmp_path / "status",
    branding_dir=tmp_path,
  )
  captured: dict[str, Any] = {"contexts": [], "calls": [], "startup": [], "run": {}}
  monkeypatch.setattr(app_module.ClockSettings, "get_settings", lambda: settings)
  for name, fake in patches.items():
    monkeypatch.setattr(app_module, name, fake)
  monkeypatch.setattr(app_module, "register_crash_alert_writer", lambda _w: captured["calls"].append("writer"))
  monkeypatch.setattr(app_module, "register_for_shutdown", lambda *_a, **_k: None)
  monkeypatch.setattr(app_module, "install_loop_exception_handler", lambda: None)
  monkeypatch.setattr(app_module.app, "on_startup", captured["startup"].append)
  monkeypatch.setattr(app_module.ui, "run", lambda **kwargs: captured["run"].update(kwargs))
  stores: list[LocalStore] = []

  def tracked_store(path: Path) -> LocalStore:
    captured["calls"].append("store")
    stores.append(LocalStore(path))
    return stores[-1]

  monkeypatch.setattr(app_module, "LocalStore", tracked_store)
  app_module.serve("timeclock-system")
  yield captured
  for store in stores:
    store.close()


async def invoke_like_nicegui(handler: Callable[[], object]) -> None:
  # NiceGUI's App.safe_invoke: call it, and run an awaitable result as a background task whose exception it only logs.
  result = handler()
  if inspect.isawaitable(result):
    task = asyncio.ensure_future(result)
    await asyncio.wait([task])
  for _ in range(50):
    await asyncio.sleep(0.01)


async def test_a_failing_startup_shuts_the_clock_down(wired: dict[str, Any]) -> None:
  [startup] = wired["startup"]
  # The running loop's handler stands in for aeth_ext's, which logs, alerts and records a trail.
  asyncio.get_running_loop().set_exception_handler(lambda _loop, context: wired["contexts"].append(context))
  await invoke_like_nicegui(startup)
  [context] = wired["contexts"]
  assert context["message"] == "Startup stopped"
  assert str(context["exception"]) == "stored data failed validation"


@pytest.fixture
def wired_scanner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
  RecordingClock.instances.clear()
  scanner, sync = FakeScanner(), FakeSync()
  patches = {
    "Clock": RecordingClock,
    "Scanner": lambda *_a: scanner,
    "DatabaseSync": lambda *_a, **_k: sync,
    "heartbeat_loop": lambda *_a, **_k: asyncio.Event().wait(),
  }
  serving = serve_with(tmp_path, monkeypatch, patches)
  captured = next(serving)
  yield {**captured, "scanner": scanner, "sync": sync, "clock": RecordingClock.instances[0]}
  next(serving, None)  # runs serve_with's teardown, which closes the store


async def test_a_settings_change_reaches_the_scanner_margin_and_minimum(wired_scanner: dict[str, Any]) -> None:
  [startup] = wired_scanner["startup"]
  await invoke_like_nicegui(startup)
  scanner, clock = wired_scanner["scanner"], wired_scanner["clock"]
  assert scanner.galleries[-1][1:] == (Settings().fingerprint_match_margin, Settings().fingerprint_match_min_score)
  clock._store.set_settings(Settings(fingerprint_match_margin=NEW_MARGIN, fingerprint_match_min_score=NEW_MIN_SCORE))
  clock.notify_changed({"settings"})
  await invoke_like_nicegui(lambda: None)  # the gallery refresh is a task
  assert scanner.galleries[-1][1:] == (NEW_MARGIN, NEW_MIN_SCORE)


async def test_a_correction_refreshes_the_gallery(wired_scanner: dict[str, Any]) -> None:
  [startup] = wired_scanner["startup"]
  await invoke_like_nicegui(startup)
  scanner, clock = wired_scanner["scanner"], wired_scanner["clock"]
  before = len(scanner.galleries)
  clock.notify_changed({"punch_corrections"})
  await invoke_like_nicegui(lambda: None)  # the gallery refresh is a task
  assert len(scanner.galleries) == before + 1


async def test_a_settled_scan_wakes_the_sync(wired_scanner: dict[str, Any]) -> None:
  [startup] = wired_scanner["startup"]
  await invoke_like_nicegui(startup)
  sync = wired_scanner["sync"]
  before = sync.wakes
  await wired_scanner["clock"].record_scan(ScanReport(ScanResult.NO_MATCH, None, None, (), 15))
  assert sync.wakes == before + 1


async def test_a_screen_error_shuts_the_clock_down(wired: dict[str, Any]) -> None:
  # build_root's on_error; the page test checks that buttons reach it.
  asyncio.get_running_loop().set_exception_handler(lambda _loop, context: wired["contexts"].append(context))
  on_error = wired["run"]["root"].__self__._on_error  # the page object build_root returned the root of
  error = RuntimeError("store failed")
  on_error(error)
  [context] = wired["contexts"]
  assert context["message"] == "Screen action failed"
  assert context["exception"] is error


def test_the_crash_writer_is_registered_before_the_store_opens(wired: dict[str, Any]) -> None:
  assert wired["calls"] == ["writer", "store"]
