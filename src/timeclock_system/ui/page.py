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

# At runtime: NiceGUI inspects a handler's signature before calling it, which evaluates its annotations (_on_key).
from nicegui.events import KeyEventArguments  # noqa: TC002

# First party imports
from timeclock_system.ui import text
from timeclock_system.ui.controller import (
  AddEmployeeDetails,
  AdminMenu,
  AdminReconfirm,
  Confirmation,
  EmployeeAdded,
  Idle,
  Menu,
  NewFingerprint,
  NewPin,
  OverridePrompt,
  PinEntry,
  PinScan,
  ReaderIndicator,
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
  from nicegui.events import ValueChangeEventArguments

  # First party imports
  from timeclock_system.sync import DbStatus
  from timeclock_system.ui.controller import DetailsField, KioskController, Screen

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
.reader-ready { color: var(--sft-white); opacity: 0.35; }
.reader-scanning { color: var(--sft-yellow); }
.reader-paused { color: var(--sft-red); }
.reader-asleep { color: var(--sft-white); opacity: 0.35; }
"""
# R13.2: no text selection, no long-press menu.
_NO_SELECT_CSS = "body { user-select: none; -webkit-user-select: none; touch-action: manipulation; }"
# cage draws a pointer mid-screen unless the page hides it. Only the kiosk hides it (its KIOSK_URL carries `?kiosk`);
# any other browser, e.g. one through an SSH tunnel, keeps it. Pointer events, not mouse events: a touch also fires
# compatibility mouse events, but its pointer events say `touch`, so touching never shows the cursor (owner,
# 2026-10-27). A mouse shows it until a touch or a minute without mouse movement.
KIOSK_CURSOR = """<style>html.sft-hide-cursor, html.sft-hide-cursor * { cursor: none !important; }</style>
<script>
if (new URLSearchParams(location.search).has("kiosk")) {
  const root = document.documentElement;
  let timer;
  const hide = () => root.classList.add("sft-hide-cursor");
  hide();
  document.addEventListener("pointermove", e => {
    if (e.pointerType !== "mouse") return;
    root.classList.remove("sft-hide-cursor");
    clearTimeout(timer);
    timer = setTimeout(hide, 60000);
  });
  document.addEventListener("pointerdown", e => {
    if (e.pointerType !== "mouse") { clearTimeout(timer); hide(); }
  });
}
</script>"""
_NO_CONTEXT_MENU = '<script>document.addEventListener("contextmenu", e => e.preventDefault());</script>'
# A tap focuses the button it lands on, and ui.keyboard drops every key while a button has focus (its `ignore`). Leaving
# buttons in `ignore` and keeping focus off them also stops Enter from clicking whichever button was tapped last.
NO_BUTTON_FOCUS = '<script>document.addEventListener("focusin", e => { if (e.target.closest("button")) e.target.blur(); });</script>'
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
    self._shown: Screen | None = None  # what `screen` last drew; one page instance serves every client
    controller.add_listener(self._on_change)

  def _on_change(self) -> None:
    # The indicator always; the screen only when it compares unequal to what's drawn, so the add-employee form's typing
    # (left out of its equality) never redraws it and drops the input's focus.
    self.indicator.refresh()
    if self._controller.screen != self._shown:
      self.screen.refresh()

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
    ui.add_head_html(KIOSK_CURSOR)
    ui.add_head_html(_NO_CONTEXT_MENU)
    ui.add_head_html(NO_BUTTON_FOCUS)
    ui.keyboard(on_key=self._on_key)

    clock_label = ui.label().classes("fixed top-2 right-4 text-xl")
    self.indicator()
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
    # By code, so the numpad counts too, NumLock on or off (NiceGUI's key.number knows only the number row).
    if (digit := e.key.code.removeprefix("Digit").removeprefix("Numpad")).isdigit() and len(digit) == 1:
      await self._guarded(lambda: self._controller.digit(digit))
    elif e.key.name == "Backspace":
      await self._guarded(self._controller.backspace)
    elif e.key.name == "Enter":
      await self._guarded(self._controller.enter)

  @ui.refreshable_method
  def screen(self) -> None:
    s = self._shown = self._controller.screen
    with ui.column().classes("w-full items-center gap-6 pt-8"):
      match s:
        case Idle():
          self._idle(s)
        case Menu():
          self._menu(s)
        case Rescan() | PinScan():
          rescan = isinstance(s, Rescan)
          self._finger_prompt(text.RESCAN if rescan else text.SCAN_YOUR_FINGER, s.message, pin_ok=rescan and s.pin_ok)
        case OverridePrompt():
          self._override_prompt(s)
        case RegisterPrompt():
          ui.label(text.REGISTER_PROMPT).classes("sft-title")
          with ui.row():
            self._button(text.REGISTER, self._controller.register, "sft-punch")
            self._button(text.MANAGER_OVERRIDE, self._controller.request_override)
            self._button(text.CANCEL, self._controller.cancel)
        case PinEntry():
          title = text.ENTER_USERNAME if s.field == "username" else text.ENTER_PIN
          self._pin_pad(title, s.username if s.field == "username" else "•" * len(s.pin.get_secret_value()), s.error)
        case Registering():
          ui.label(text.REGISTERING.format(done=s.done, total=s.total)).classes("sft-title")
          self._button(text.CANCEL, self._controller.cancel)
        case Confirmation():
          self._confirmation(s)
        case AdminReconfirm() | AdminMenu() | AddEmployeeDetails() | NewPin() | NewFingerprint() | EmployeeAdded():
          self._admin_screen(s)

  # The per-screen renderers below are split out of `screen` for C901/PLR0912.

  @ui.refreshable_method
  def indicator(self) -> None:
    # The reader icon, on every screen, redrawn on its own so a change doesn't redraw the screen under it.
    indicator = self._controller.reader_indicator
    with (
      ui.row()
      .classes(f"fixed bottom-2 left-4 z-20 items-center gap-2 text-base reader-{indicator}")
      .mark("reader-indicator", f"reader-{indicator}")
    ):
      ui.icon("fingerprint").classes("text-4xl")
      if indicator is ReaderIndicator.ASLEEP:
        ui.label(text.READER_ASLEEP)

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
    # A tap anywhere on the idle screen wakes the reader after hours (R40.2): a transparent layer over the whole
    # viewport holds the content, above the clock and status line, which still show through it.
    idle = ui.column().classes("fixed inset-0 z-10 items-center gap-6 pt-8").mark("idle-tap")
    with idle.on("click", lambda: self._guarded(self._controller.tap)):
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
    if info.is_admin:
      # Near the bottom, well away from the punch buttons (owner, 2026-10-27).
      with ui.row().classes("fixed bottom-14 left-0 right-0 justify-center"):
        self._button(text.ADMIN, self._controller.admin)

  def _finger_prompt(self, title: str, message: str | None, *, pin_ok: bool = False) -> None:
    ui.label(title).classes("sft-title")
    if message:
      ui.label(message)
    with ui.row():
      # An admin or exempt employee answers a rescan with their PIN and needs no override (§6.4).
      if pin_ok:
        self._button(text.USE_PIN, self._controller.use_pin)
      else:
        self._button(text.MANAGER_OVERRIDE, self._controller.request_override)
      self._button(text.CANCEL, self._controller.cancel)

  def _admin_screen(self, s: AdminReconfirm | AdminMenu | AddEmployeeDetails | NewPin | NewFingerprint | EmployeeAdded) -> None:
    match s:
      case AdminReconfirm():
        ui.label(text.ADMIN_RECONFIRM).classes("sft-title")
        if s.message:
          ui.label(s.message).classes("sft-error")
        with ui.row():
          self._button(text.USE_PIN, self._controller.use_pin)
          self._button(text.CANCEL, self._controller.cancel)
      case AdminMenu():
        ui.label(text.ADMIN_MENU).classes("sft-title")
        if s.message:
          ui.label(s.message).classes("sft-error")
        with ui.column().classes("items-center gap-4"):
          self._button(text.ADD_EMPLOYEE, self._controller.add_employee, "sft-punch")
          self._button(text.BACK, self._controller.cancel)
      case AddEmployeeDetails():
        self._details_form(s)
      case NewPin():
        title = text.NEW_PIN.format(name=s.details.name) if s.field == "first" else text.CONFIRM_NEW_PIN
        self._pin_pad(title, "•" * len(s.pin.get_secret_value()), s.error)
      case NewFingerprint():
        ui.label(text.NEW_FINGERPRINT.format(name=s.employee.name)).classes("sft-title")
        if s.message:
          ui.label(s.message).classes("sft-error")
        with ui.row():
          self._button(text.REGISTER, self._controller.register, "sft-punch")
          self._button(text.SKIP, self._controller.skip_fingerprint)
      case EmployeeAdded():
        ui.label(text.EMPLOYEE_ADDED.format(name=s.name)).classes("sft-title")
        ui.label(text.EMPLOYEE_NUMBER.format(number=s.number)).classes("text-3xl").mark("employee-number")
        self._button(text.OK, self._controller.cancel, "sft-punch")

  def _details_form(self, s: AddEmployeeDetails) -> None:
    # Typed on a plugged-in keyboard (§6.4). Every change goes to the controller, which keeps it without a redraw.
    errors = dict(s.errors)

    def edited(name: DetailsField) -> Callable[[ValueChangeEventArguments], object]:
      return lambda e: self._guarded(lambda: self._controller.edit_details(name, str(e.value)))

    ui.label(text.ADD_EMPLOYEE).classes("sft-title")
    with ui.column().classes("w-[32rem] max-w-full gap-1"):
      texts: tuple[tuple[DetailsField, str, str], ...] = (
        ("name", text.FIELD_NAME, s.name),
        ("phone", text.FIELD_PHONE, s.phone),
        ("email", text.FIELD_EMAIL, s.email),
      )
      for name, label, value in texts:
        ui.input(label, value=value, on_change=edited(name)).classes("w-full text-2xl").props("dark outlined").mark(f"field-{name}")
        if name in errors:
          ui.label(errors[name]).classes("sft-error text-base").mark(f"error-{name}")
      ui.select(dict(text.ROLE_LABELS), label=text.FIELD_ROLE, value=s.role, on_change=edited("role")).classes("w-full").props(
        "dark outlined"
      ).mark("field-role")
      ui.select(dict(s.groups), label=text.FIELD_GROUP, value=s.group_id, on_change=edited("group_id")).classes("w-full").props(
        "dark outlined"
      ).mark("field-group_id")
      choices: tuple[DetailsField, ...] = ("role", "group_id")
      for name in choices:
        if name in errors:
          ui.label(errors[name]).classes("sft-error text-base").mark(f"error-{name}")
    with ui.row():
      self._button(text.NEXT, self._controller.submit_details, "sft-punch")
      self._button(text.CANCEL, self._controller.cancel)

  def _override_prompt(self, s: OverridePrompt) -> None:
    ui.label(text.MANAGER_PROMPT).classes("sft-title")
    if s.message:
      ui.label(s.message).classes("sft-error")
    with ui.row():
      self._button(text.USE_PIN, self._controller.use_pin)
      self._button(text.CANCEL, self._controller.cancel)

  def _confirmation(self, s: Confirmation) -> None:
    # A double tap anywhere ends it early; the same full-screen layer as the idle screen's.
    layer = ui.column().classes("fixed inset-0 z-10 items-center gap-6 pt-8").mark("confirm-tap")
    with layer.on("click", lambda: self._guarded(self._controller.tap)):
      ui.label(s.text).classes("sft-title")
      ui.label(s.hours)
      for warning in s.warnings:
        ui.label(warning).classes("sft-error")
      for notice in s.notices:
        ui.label(notice).classes("text-3xl").style("color: var(--sft-yellow)").mark("alert-notice")

  def _pin_pad(self, title: str, shown: str, error: str | None) -> None:
    ui.label(title).classes("sft-title")
    ui.label(shown).classes("text-4xl h-12")
    if error:
      ui.label(error).classes("sft-error")
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
