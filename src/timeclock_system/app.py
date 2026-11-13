"""Wires the clock, scanner, sync and page together and runs NiceGUI (spec §6.1, §6.9).

`__main__.run_app` imports this only after aeth_ext's `initialize`, so nothing here runs before logging is set up.
"""

# Standard library imports
import asyncio
import contextlib
import socket
from typing import TYPE_CHECKING, Any

# Third party imports
import uvicorn
from aeth_ext.errors import alert, install_loop_exception_handler
from aeth_ext.errors.shutdown import ShutdownPhase, register_for_shutdown
from nicegui import app, ui

# First party imports
from timeclock_system.app_status import read_open_clip, write_status
from timeclock_system.clock import Clock
from timeclock_system.crash_alerts import CrashAlertWriter, register_crash_alert_writer
from timeclock_system.fprint_reader import FprintReader
from timeclock_system.heartbeat import heartbeat_loop
from timeclock_system.recorder_watch import recorder_watch_loop
from timeclock_system.scanner import Scanner
from timeclock_system.settings import ClockSettings
from timeclock_system.store import LocalStore
from timeclock_system.sync import DatabaseSync
from timeclock_system.ui.controller import KioskController
from timeclock_system.ui.page import build_root

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Coroutine, Generator

  # First party imports
  from timeclock_system.clock import ChangeTable
  from timeclock_system.scanner import ScannerEvent

_TICK_SECS = 0.5
# punch_corrections: a correction can open or close an inactive employee's shift, which moves their prints in or out.
_GALLERY_TABLES: frozenset[ChangeTable] = frozenset({"employees", "fingerprints", "punches", "punch_corrections", "settings"})


@contextlib.contextmanager
def _no_signal_capture(_server: uvicorn.Server) -> Generator[None]:
  # R21: aeth_ext's signal handlers drive the shutdown; uvicorn's own capture would take the signals from them.
  yield


def serve(project_name: str) -> None:  # noqa: C901, PLR0915 - one linear wiring of every part; splitting it would only scatter it
  """Build every part, then run NiceGUI until the shutdown stops it.

  Args:
    project_name: `__main__.PROJECT_NAME`, passed in so this module doesn't import `__main__`.
  """
  settings = ClockSettings.get_settings()

  def send_alert(reason: str, details: str) -> None:
    if clip := read_open_clip(settings.status_dir):
      details += f"\n\nRecording: {clip}"
    alert(reason, details, in_except_block=False, wait=False)  # wait=False: the loop never waits on a send

  # Before the store opens, so a failed store check or a missing reader leaves a crash record.
  register_crash_alert_writer(
    CrashAlertWriter(
      settings.crash_alerts_dir,
      program=f"{project_name}-{settings.instance_id}",
      host=socket.gethostname(),
      status_dir=settings.status_dir,
    )
  )
  store = LocalStore(settings.store_path)
  clock = Clock(  # the validator sets the id
    store, instance_id=settings.instance_id or "", clock_number=settings.clock_number, tz=settings.tz, alert=send_alert
  )
  write_status(settings.status_dir, "Connecting to the fingerprint reader")  # shown on the host's standby page
  reader = FprintReader()  # a missing reader raises ReaderGone here, uncaught: fatal at startup (R14.7)

  # asyncio holds tasks weakly, so every task and scanner-event future is kept here until it finishes, and one
  # that fails shuts the clock down: the global hooks don't see exceptions held in tasks (§6.9). The loop's handler
  # (aeth_ext's, set at startup) logs the traceback, alerts with its picture and records a trail.
  running: set[asyncio.Future[Any]] = set()
  loop: asyncio.AbstractEventLoop | None = None

  def keep(name: str, coro: Coroutine[Any, Any, Any]) -> None:
    fut = asyncio.ensure_future(coro)
    running.add(fut)

    def done(f: asyncio.Future[Any]) -> None:
      running.discard(f)
      if not f.cancelled() and (exc := f.exception()) is not None:
        f.get_loop().call_exception_handler({"message": f"{name} stopped", "exception": exc, "future": f})

    fut.add_done_callback(done)

  def post(ev: ScannerEvent) -> None:
    # On the scanner thread, which starts only after the startup hook has set `loop`; the coroutine is made on the loop.
    if loop is not None:
      loop.call_soon_threadsafe(lambda: keep("Scanner event", controller.on_scanner_event(ev)))

  scanner = Scanner(reader, post)
  sync = DatabaseSync(clock, store, settings.conninfo(), alert=send_alert)
  controller = KioskController(clock, scanner, tz=settings.tz, status_dir=settings.status_dir)

  # Plain lambdas: aeth_ext holds bound methods weakly. app.shutdown() sets uvicorn's should_exit.
  register_for_shutdown(lambda _trails: app.shutdown(), phase=ShutdownPhase.THREADED)
  register_for_shutdown(lambda _trails: scanner.stop(), phase=ShutdownPhase.THREADED)

  async def refresh_gallery() -> None:
    # Settings read after the await: a refresh that stalls in it must not put back what a newer one already applied.
    prints = await clock.gallery()
    s = clock.settings()
    scanner.set_gallery(prints, s.fingerprint_match_margin, s.fingerprint_match_min_score)

  async def tick_forever() -> None:
    # An app task, not a page timer, so business hours drive the reader with no browser connected.
    while True:
      await controller.tick()
      await asyncio.sleep(_TICK_SECS)

  async def startup() -> None:
    nonlocal loop
    install_loop_exception_handler()
    loop = asyncio.get_running_loop()
    write_status(settings.status_dir, "Loading employees and punches")  # shown on the host's standby page
    await clock.load()
    clock.add_record_listener(lambda _record: sync.wake())
    clock.add_scan_listener(sync.wake)
    clock.add_change_listener(lambda tables: keep("Gallery refresh", refresh_gallery()) if tables & _GALLERY_TABLES else None)
    await refresh_gallery()
    write_status(settings.status_dir, "Starting the fingerprint reader")
    scanner.start()  # here, not before ui.run: `post` needs the running loop
    await controller.tick()  # sets the first reader mode
    keep("Database sync", sync.run())
    keep("Heartbeat", heartbeat_loop(settings.heartbeat_path, scanner.busy_seconds, tz=settings.tz))
    keep("Recorder watch", recorder_watch_loop(settings.status_dir, send_alert, tz=settings.tz))
    keep("Week rollover", clock.run_week_rollover())
    keep("Screen timer", tick_forever())
    write_status(settings.status_dir, "Ready")

  # Through keep, like every task: NiceGUI would run the coroutine itself and only log its exception (§6.9).
  app.on_startup(lambda: keep("Startup", startup()))
  uvicorn.Server.capture_signals = _no_signal_capture  # pyright: ignore[reportAttributeAccessIssue]  # uvicorn is pinned for this (R21)
  ui.run(
    root=build_root(
      controller,
      lambda: sync.status,
      tz=settings.tz,
      branding_dir=settings.branding_dir,
      on_error=lambda e: asyncio.get_running_loop().call_exception_handler({"message": "Screen action failed", "exception": e}),
    ),
    host="0.0.0.0",  # the kiosk container reaches it over the compose network
    port=8080,
    title="Time Clock",
    dark=True,
    reload=False,
    show=False,
    viewport="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no",
    uvicorn_logging_level="warning",
    log_config=None,  # passed to uvicorn.Config; its dictConfig would close aeth_ext's handlers
  )
