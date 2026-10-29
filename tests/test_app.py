# Standard library imports
import asyncio
import importlib
import inspect
import sys
import types
from typing import TYPE_CHECKING, Any, override
from zoneinfo import ZoneInfo

# Third party imports
import pytest
import uvicorn

# First party imports
from timeclock_system.clock import Clock
from timeclock_system.store import LocalStore

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Iterator
  from pathlib import Path

NY = ZoneInfo("America/New_York")


class FakeReader:
  """Stands in for libfprint, which doesn't exist on Windows."""


class FailingClock(Clock):
  @override
  async def load(self) -> None:
    raise RuntimeError("stored data failed validation")


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
  """Run serve() with fakes for the hardware and NiceGUI, and capture what it hands to them."""
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
  captured: dict[str, Any] = {"shutdowns": [], "startup": [], "run": {}}
  monkeypatch.setattr(app_module.ClockSettings, "get_settings", lambda: settings)
  monkeypatch.setattr(app_module, "Clock", FailingClock)
  monkeypatch.setattr(app_module, "register_crash_alert_writer", lambda _w: None)
  monkeypatch.setattr(app_module, "register_for_shutdown", lambda *_a, **_k: None)
  monkeypatch.setattr(app_module, "install_loop_exception_handler", lambda: None)
  monkeypatch.setattr(app_module, "trigger_shutdown", lambda reason, details: captured["shutdowns"].append((reason, details)))
  monkeypatch.setattr(app_module.app, "on_startup", captured["startup"].append)
  monkeypatch.setattr(app_module.ui, "run", lambda **kwargs: captured["run"].update(kwargs))
  stores: list[LocalStore] = []

  def tracked_store(path: Path) -> LocalStore:
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
  await invoke_like_nicegui(startup)
  [(reason, details)] = wired["shutdowns"]
  assert reason == "Startup stopped"
  assert "stored data failed validation" in details


async def test_a_screen_error_shuts_the_clock_down(wired: dict[str, Any]) -> None:
  # build_root's on_error; the page test checks that buttons reach it.
  on_error = wired["run"]["root"].__self__._on_error  # the page object build_root returned the root of
  on_error(RuntimeError("store failed"))
  [(reason, details)] = wired["shutdowns"]
  assert reason == "Screen action failed"
  assert "store failed" in details
