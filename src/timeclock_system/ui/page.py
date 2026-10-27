"""The kiosk's one NiceGUI page (spec §6.4, R39, R13.2, R25): renders `KioskController.screen` and forwards every input to it.

`controller.tick` runs from an app task (R5), not from here: business hours drive the reader with no browser connected.
"""

# Standard library imports
import inspect
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

# Third party imports
from nicegui import app, ui

# First party imports
from timeclock_system.ui import text
from timeclock_system.ui.controller import (
  Confirmation,
  Idle,
  Menu,
  OverridePrompt,
  PinEntry,
  PinScan,
  Registering,
  RegisterPrompt,
  Rescan,
)

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable
  from pathlib import Path
  from zoneinfo import ZoneInfo

  # Third party imports
  from nicegui.events import KeyEventArguments

  # First party imports
  from timeclock_system.sync import DbStatus
  from timeclock_system.ui.controller import KioskController

logger = logging.getLogger(__name__)

LOGO_FILE = "SFT-Logo-June-2022-color.svg"
_THEME_CSS = """
:root { --sft-yellow: #F9E60D; --sft-red: #ED1A2C; --sft-black: #161514; --sft-white: #FFFFFF; --sft-green: #2E7D32; }
body { background: var(--sft-black); color: var(--sft-white); font-size: 1.5rem; }
.sft-button { min-height: 64px; min-width: 160px; font-size: 1.5rem; }
.sft-punch { background: var(--sft-yellow) !important; color: var(--sft-black) !important; }
.sft-plain { background: var(--sft-white) !important; color: var(--sft-black) !important; }
.sft-key { min-height: 72px; min-width: 96px; font-size: 2rem; background: var(--sft-white) !important;
  color: var(--sft-black) !important; }
.sft-error { color: var(--sft-red); }
.sft-title { font-size: 2.5rem; font-weight: 600; }
"""
# R13.2: no text selection, no long-press menu.
_NO_SELECT_CSS = "body { user-select: none; -webkit-user-select: none; touch-action: manipulation; }"
_NO_CONTEXT_MENU = '<script>document.addEventListener("contextmenu", e => e.preventDefault());</script>'
_PAD = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "⌫", "0", "✓")


def build_root(
  controller: KioskController,
  status: Callable[[], DbStatus],
  *,
  tz: ZoneInfo,
  branding_dir: Path,
  on_error: Callable[[Exception], None],
) -> Callable[[], None]:
  """Return the page's root function for `ui.run(root=…)`.

  Args:
    controller: The screen state machine the page renders and feeds.
    status: The database status for the status line.
    tz: The business's zone, for displayed times.
    branding_dir: Where the logo SVGs live on the device (R39.1).
    on_error: Gets any exception from a button, tap or key; NiceGUI alone would only log it.
  """
  return _KioskPage(controller, status, tz, branding_dir, on_error).root


class _KioskPage:
  """One instance serves every client: its refreshable re-renders each client's copy and drops closed ones itself."""

  def __init__(
    self,
    controller: KioskController,
    status: Callable[[], DbStatus],
    tz: ZoneInfo,
    branding_dir: Path,
    on_error: Callable[[Exception], None],
  ) -> None:
    self._controller = controller
    self._on_error = on_error
    self._status = status
    self._tz = tz
    self._branding_dir = branding_dir
    self._logo_checked = False
    controller.add_listener(self.screen.refresh)

  def root(self) -> None:
    if not self._logo_checked:
      # Once, on the first page load: media routes are app-wide (and ui.run's setup resets them in tests).
      self._logo_checked = True
      if (self._branding_dir / LOGO_FILE).exists():
        app.add_media_files("/branding", self._branding_dir)
      else:
        logger.warning("no logo at %s; the page renders without it (R39.1)", self._branding_dir / LOGO_FILE)
    ui.add_css(_THEME_CSS)
    ui.add_css(_NO_SELECT_CSS)
    ui.add_head_html(_NO_CONTEXT_MENU)
    ui.keyboard(on_key=self._on_key)

    clock_label = ui.label().classes("fixed top-2 right-4 text-xl")
    self.screen()
    with ui.row().classes("fixed bottom-2 right-4 items-center gap-2 text-base"):
      dot = ui.icon("circle").classes("text-sm")
      status_label = ui.label()

    def update_status() -> None:
      now = datetime.now(UTC).astimezone(self._tz)
      clock_label.text = f"{now:%A, %B} {now.day} · {text.format_clock_time(now, self._tz)}"
      s = self._status()
      dot.style(f"color: var({'--sft-green' if s.connected else '--sft-red'})")
      status_label.text = text.format_db_status(s, self._tz)

    update_status()
    ui.timer(1.0, update_status)

  async def _on_key(self, e: KeyEventArguments) -> None:
    # R25: the keyboard reaches the same calls as the on-page pad.
    if not e.action.keydown or e.action.repeat:
      return
    if e.key.number is not None:
      await self._guarded(lambda: self._controller.digit(str(e.key.number)))
    elif e.key.name == "Backspace":
      await self._guarded(self._controller.backspace)
    elif e.key.name == "Enter":
      await self._guarded(self._controller.enter)

  @ui.refreshable_method
  def screen(self) -> None:
    s = self._controller.screen
    with ui.column().classes("w-full items-center gap-6 pt-8"):
      match s:
        case Idle():
          self._idle(s)
        case Menu():
          self._menu(s)
        case Rescan():
          self._finger_prompt(text.RESCAN, s.message)
        case PinScan():
          self._finger_prompt(text.SCAN_YOUR_FINGER, s.message)
        case OverridePrompt():
          self._override_prompt(s)
        case RegisterPrompt():
          ui.label(text.REGISTER_PROMPT).classes("sft-title")
          with ui.row():
            self._button(text.REGISTER, self._controller.register, "sft-punch")
            self._button(text.MANAGER_OVERRIDE, self._controller.request_override)
            self._button(text.CANCEL, self._controller.cancel)
        case PinEntry():
          self._pin_pad(s)
        case Registering():
          ui.label(text.REGISTERING.format(done=s.done, total=s.total)).classes("sft-title")
          self._button(text.CANCEL, self._controller.cancel)
        case Confirmation():
          self._confirmation(s)

  # The per-screen renderers below are split out of `screen` for C901/PLR0912.

  async def _guarded(self, call: Callable[[], object]) -> None:
    # Every button, tap and key runs through here, so their exceptions reach on_error; NiceGUI's own errors (a send to a
    # closed browser, say) stay with its logger.
    try:
      result = call()
      if inspect.isawaitable(result):
        await result
    except Exception as e:  # noqa: BLE001  # handed on, not swallowed
      self._on_error(e)

  def _button(self, label: str, on_click: Callable[[], object], kind: str = "sft-plain") -> None:
    ui.button(label, on_click=lambda: self._guarded(on_click)).classes(f"sft-button {kind}").props("unelevated no-caps")

  def _idle(self, s: Idle) -> None:
    # A tap anywhere on the idle screen wakes the reader after hours (R40.2).
    with ui.column().classes("w-full items-center gap-6").on("click", lambda: self._guarded(self._controller.tap)):
      if (self._branding_dir / LOGO_FILE).exists():
        ui.image(f"/branding/{LOGO_FILE}").classes("w-96")
      ui.label(s.message or (text.IDLE_READY if self._controller.reader_awake else text.IDLE_ASLEEP)).classes("sft-title")
      self._button(text.USE_PIN, self._controller.use_pin)

  def _menu(self, s: Menu) -> None:
    info = s.info
    ui.label(info.name).classes("sft-title")
    state = text.STATE_LABELS[info.state]
    ui.label(state.format(time=text.format_clock_time(info.since, self._tz)) if info.since else state)
    ui.label(text.format_hours(info.hours_this_week))
    with ui.row().classes("gap-4"):
      for kind in info.valid_next:
        self._button(text.PUNCH_LABELS[kind], lambda kind=kind: self._controller.punch(kind), "sft-punch")
    if s.notice:
      ui.label(s.notice).classes("sft-error")
    self._button(text.CANCEL, self._controller.cancel)

  def _finger_prompt(self, title: str, message: str | None) -> None:
    ui.label(title).classes("sft-title")
    if message:
      ui.label(message)
    with ui.row():
      self._button(text.MANAGER_OVERRIDE, self._controller.request_override)
      self._button(text.CANCEL, self._controller.cancel)

  def _override_prompt(self, s: OverridePrompt) -> None:
    ui.label(text.MANAGER_PROMPT).classes("sft-title")
    if s.message:
      ui.label(s.message).classes("sft-error")
    with ui.row():
      self._button(text.USE_PIN, self._controller.use_pin)
      self._button(text.CANCEL, self._controller.cancel)

  def _confirmation(self, s: Confirmation) -> None:
    ui.label(s.text).classes("sft-title")
    ui.label(s.hours)
    for warning in s.warnings:
      ui.label(warning).classes("sft-error")

  def _pin_pad(self, s: PinEntry) -> None:
    ui.label(text.ENTER_USERNAME if s.field == "username" else text.ENTER_PIN).classes("sft-title")
    ui.label(s.username if s.field == "username" else "•" * len(s.pin.get_secret_value())).classes("text-4xl h-12")
    if s.error:
      ui.label(s.error).classes("sft-error")
    with ui.grid(columns=3).classes("gap-2"):
      for key in _PAD:
        match key:
          case "⌫":
            on_click = self._controller.backspace
          case "✓":
            on_click = self._controller.enter
          case _:
            on_click = lambda key=key: self._controller.digit(key)  # one binding per key
        # Typed as taking nothing: NiceGUI passes the click event only to a parameter without a default.
        guarded: Callable[[], object] = lambda c=on_click: self._guarded(c)
        ui.button(key, on_click=guarded).classes("sft-key").props("unelevated")
    self._button(text.CANCEL, self._controller.cancel)
