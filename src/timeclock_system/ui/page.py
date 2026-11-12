"""The kiosk's one NiceGUI page (spec §6.4, R39, R13.2, R25): renders `KioskController.screen` and forwards every input to it.

`controller.tick` runs from an app task (R5), not from here: business hours drive the reader with no browser connected.
"""

# Standard library imports
import contextlib
import inspect
import logging
import time
from datetime import UTC, date, datetime
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING

# Third party imports
from nicegui import app, ui

# At runtime: NiceGUI inspects a handler's signature before calling it, which evaluates its annotations (_on_key).
from nicegui.events import KeyEventArguments  # noqa: TC002
from timeclock_schema.enums import Finger

# First party imports
from timeclock_system.registration import PRESSES
from timeclock_system.ui import text
from timeclock_system.ui.controller import (
  AddEmployeeDetails,
  AdminMenu,
  AdminReconfirm,
  ApprovalPrompt,
  Confirmation,
  EmployeeAdded,
  FingerPick,
  FingerprintActions,
  ForceSave,
  GearMenu,
  Idle,
  Menu,
  NewFingerprint,
  NewPin,
  OverridePrompt,
  PinEntry,
  PinScan,
  Registering,
  RegisterPrompt,
  Rescan,
  SimilarReview,
  TooSimilarBlocked,
  TooSimilarReview,
)

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable
  from zoneinfo import ZoneInfo

  # Third party imports
  from nicegui.events import ValueChangeEventArguments

  # First party imports
  from timeclock_system.sync import DbStatus
  from timeclock_system.ui.controller import DetailsField, KioskController, Screen

logger = logging.getLogger(__name__)

LOGO_FILE = "SFT-Logo-June-2022-color.svg"
# The host's os-release, mounted read-only by the compose file: timeclock-host stamps its version there.
HOST_OS_RELEASE = Path("/run/host/os-release")
_THEME_CSS = """
:root { --sft-yellow: #F9E60D; --sft-red: #ED1A2C; --sft-black: #161514; --sft-white: #FFFFFF; --sft-green: #2E7D32;
  --sft-blue: #298DF0; --sft-orange: #FB8C00; --sft-read-green: #43A047; }
body { background: var(--sft-black); color: var(--sft-white); font-size: 1.5rem; }
.sft-button { min-height: 64px; min-width: 160px; font-size: 1.5rem; }
.sft-punch { background: var(--sft-yellow) !important; color: var(--sft-black) !important; }
.sft-plain { background: var(--sft-white) !important; color: var(--sft-black) !important; }
.sft-key { min-height: 72px; min-width: 96px; font-size: 2rem; background: var(--sft-white) !important;
  color: var(--sft-black) !important; }
.sft-error { color: var(--sft-red); }
.sft-title { font-size: 2.5rem; font-weight: 600; }
/* The hours table (punch menu, confirmation): docked on the left edge, centred vertically (owner, 2026-11-06). */
.sft-hours { position: fixed; left: 2rem; top: 50%; transform: translateY(-50%); }
.reader-ready { color: var(--sft-yellow); }
.reader-scanning { color: var(--sft-blue); }
.reader-read { color: var(--sft-read-green); }
.reader-unrecognized { color: var(--sft-orange); }
.reader-failed, .reader-paused { color: var(--sft-red); }
.reader-busy, .reader-asleep { color: var(--sft-white); opacity: 0.35; }
.reader-message-error { color: var(--sft-red); }
.reader-message-success { color: var(--sft-read-green); }
.reader-message-unrecognized { color: var(--sft-orange); }
/* Taps land up to 12 px past a button's drawn edge (owner, 2026-11-11), on ::after since Quasar draws a button's shadow
   with ::before. Where two buttons sit under 24 px apart the gap splits down the middle, so no two areas overlap. */
.sft-button::after, .sft-key::after { content: ""; position: absolute; inset: -12px; }
.nicegui-row > .sft-button + .sft-button::after { left: -8px; }
.nicegui-row > .sft-button:has(+ .sft-button)::after { right: -8px; }
.nicegui-column > .sft-button + .sft-button::after { top: -8px; }
.nicegui-column > .sft-button:has(+ .sft-button)::after { bottom: -8px; }
.sft-pad > .sft-key::after { inset: -4px; }
.sft-pad > .sft-key:nth-child(3n+1)::after { left: -12px; }
.sft-pad > .sft-key:nth-child(3n)::after { right: -12px; }
.sft-pad > .sft-key:nth-child(-n+3)::after { top: -12px; }
.sft-pad > .sft-key:nth-last-child(-n+3)::after { bottom: -12px; }
/* The gear: half again as big, and any tap in its corner of the screen (0-136 px across, 0-120 px down) opens it. */
.sft-gear::after { content: ""; position: absolute; top: -8px; left: -16px; width: 136px; height: 120px; }
/* The update toasts, bottom right above the database line (owner's mockup, 2026-11-12). The stack passes taps through to
   the screen. A toast slides in from the right on leaving display:none; `sft-out` slides it away before the page hides it. */
:root { --sft-surface: #24221F; --sft-edge: #3A3733; --sft-muted: #A9A49B; }
.sft-toasts { position: fixed; right: 16px; bottom: 44px; z-index: 30; display: flex; flex-direction: column;
  align-items: flex-end; gap: 12px; pointer-events: none; }
.sft-toast { width: 400px; background: var(--sft-surface); border: 1px solid var(--sft-edge); border-radius: 10px;
  padding: 14px 16px 16px; box-shadow: 0 10px 28px rgba(0, 0, 0, 0.55); animation: sft-toast-in 0.4s cubic-bezier(0.2, 0.8, 0.2, 1); }
.sft-toast.sft-out { animation: sft-toast-out 0.4s cubic-bezier(0.2, 0.8, 0.2, 1) forwards; }
.sft-toast-head { display: flex; align-items: center; gap: 10px; font-size: 20px; font-weight: 500; }
.sft-toast-head .q-icon { font-size: 26px; color: var(--sft-yellow); }
.sft-toast-sub { margin-top: 8px; font-size: 16px; color: var(--sft-muted); font-variant-numeric: tabular-nums; }
.sft-toast-plan { margin-top: 10px; font-size: 17px; line-height: 1.35; }
.sft-bar { margin-top: 12px; height: 8px; border-radius: 4px; background: var(--sft-edge); overflow: hidden; }
.sft-bar > i { display: block; height: 100%; width: 0; background: var(--sft-yellow); border-radius: 4px; transition: width 0.25s linear; }
.sft-bar.sft-moving > i { width: 30%; animation: sft-bar-moving 1.6s ease-in-out infinite; }
@keyframes sft-toast-in { from { transform: translateX(calc(100% + 40px)); } to { transform: none; } }
@keyframes sft-toast-out { from { transform: none; } to { transform: translateX(calc(100% + 40px)); } }
@keyframes sft-bar-moving { from { transform: translateX(-110%); } to { transform: translateX(350%); } }
@media (prefers-reduced-motion: reduce) {
  .sft-toast, .sft-toast.sft-out { animation: none; }
  .sft-bar.sft-moving > i { animation: none; width: 100%; opacity: 0.5; }
}
"""
TOAST_SLIDE_SECS = 0.4  # `sft-toast-out`'s length: a toast is hidden once its slide-away has had this long
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
  host_os_release: Path = HOST_OS_RELEASE,
) -> Callable[[], None]:
  """Return the page's root function for `ui.run(root=…)`.

  Args:
    controller: The screen state machine the page renders and feeds.
    status: The database status for the status line.
    tz: The business's zone, for displayed times.
    branding_dir: Where the logo SVGs live on the device (R39.1).
    on_error: Gets any exception from a button, tap or key; NiceGUI alone would only log it.
    host_os_release: The host's os-release, for the host image's version beside the app's.
  """
  return _KioskPage(controller, status, tz, branding_dir, on_error, host_os_release=host_os_release).root


class _KioskPage:
  """One instance serves every client: its refreshable re-renders each client's copy and drops closed ones itself."""

  def __init__(
    self,
    controller: KioskController,
    status: Callable[[], DbStatus],
    tz: ZoneInfo,
    branding_dir: Path,
    on_error: Callable[[Exception], None],
    *,
    host_os_release: Path,
  ) -> None:
    self._controller = controller
    self._on_error = on_error
    self._status = status
    self._tz = tz
    self._branding_dir = branding_dir
    # The host image's version beside the app's, where timeclock-host stamped the host's os-release (IMAGE_ID,
    # IMAGE_VERSION, and BUILD_ID, the build date); any other host shows the app's alone. Read once: a host update
    # restarts the container.
    self._version_text = f"v{version('timeclock-system')}"
    try:
      lines = host_os_release.read_text(encoding="utf-8").splitlines()
    except OSError:
      lines = []
    fields = {key: value.strip("'\"") for key, _, value in (line.partition("=") for line in lines)}
    if fields.get("IMAGE_ID") == "timeclock-host" and (host := fields.get("IMAGE_VERSION")):
      self._version_text += f" · host {host}"
      with contextlib.suppress(ValueError):
        built = date.fromisoformat(fields.get("BUILD_ID", ""))
        self._version_text += f" ({built:%b} {built.day}, {built.year})"
    self._logo_checked = False
    self._shown: Screen | None = None  # what `screen` last drew; one page instance serves every client
    controller.add_listener(self._on_change)

  def _on_change(self) -> None:
    # The indicator always; the screen only when it compares unequal to what's drawn, so the add-employee form's typing
    # (left out of its equality) never redraws it and drops the input's focus. The update toasts follow their own timer.
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
    # Small and dim: for whoever services the clock, telling which releases it runs (the app's, and the host image's).
    # The very bottom-left corner, below the reader line.
    ui.label(self._version_text).classes("fixed bottom-2 left-4 text-sm opacity-50").mark("version")
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
    self._toasts()

  def _toasts(self) -> None:
    # The update toasts, built once per client and driven by their own timer, so a toast never redraws the screen.
    with ui.element("div").classes("sft-toasts"):
      app_toast = ui.element("div").classes("sft-toast").mark("app-update-toast")
      with app_toast:
        with ui.element("div").classes("sft-toast-head"):
          ui.icon("system_update")
          ui.label(text.APP_UPDATE_TITLE)
        ui.label(text.APP_UPDATE_DETAIL).classes("sft-toast-sub")
        with ui.element("div").classes("sft-bar sft-moving"):
          ui.element("i")
      host_toast = ui.element("div").classes("sft-toast").mark("host-update-toast")
      with host_toast:
        with ui.element("div").classes("sft-toast-head"):
          ui.icon("download")
          host_title = ui.label()
        host_sub = ui.label().classes("sft-toast-sub")
        with ui.element("div").classes("sft-bar"):
          host_bar = ui.element("i")
        host_plan = ui.label().classes("sft-toast-plan")
    leaving: dict[ui.element, float] = {}  # toasts mid slide-away, by when it began (per client)
    app_toast.set_visibility(False)
    host_toast.set_visibility(False)

    def show(toast: ui.element, *, want: bool) -> None:
      # Slides in on leaving display:none; slides out first, then hides once the slide has had time.
      if want:
        if leaving.pop(toast, None) is not None or not toast.visible:
          toast.classes(remove="sft-out")
          toast.set_visibility(True)
      elif toast.visible:
        began = leaving.setdefault(toast, time.monotonic())
        toast.classes("sft-out")
        if time.monotonic() - began >= TOAST_SLIDE_SECS:
          toast.set_visibility(False)
          del leaving[toast]

    def update_toasts() -> None:
      show(app_toast, want=self._controller.app_updating)
      if (u := self._controller.host_update) is not None:
        # MB are 10^6 bytes, rounded; the percent is floored. The text stays while the toast slides away.
        done_mb, total_mb = (min(u.done, u.total) + 500_000) // 1_000_000, (u.total + 500_000) // 1_000_000
        percent = 100 if u.downloaded else min(100, u.done * 100 // u.total) if u.total else 0
        host_title.text = text.HOST_UPDATE_DOWNLOADED if u.downloaded else text.HOST_UPDATE_DOWNLOADING
        host_sub.text = (
          f"{total_mb} MB" if u.downloaded else text.HOST_UPDATE_PROGRESS.format(percent=percent, done=done_mb, total=total_mb)
        )
        host_bar.style(f"width: {percent}%")
        if u.window_start is not None:
          start = text.format_clock_time(datetime.combine(datetime.now(self._tz).date(), u.window_start, tzinfo=self._tz), self._tz)
          host_plan.text = text.HOST_UPDATE_AT.format(time=start)
        else:
          host_plan.text = text.HOST_UPDATE_NOW if u.downloaded else text.HOST_UPDATE_SOON
      show(host_toast, want=u is not None)

    update_toasts()
    ui.timer(0.5, update_toasts)

  async def _on_key(self, e: KeyEventArguments) -> None:
    # R25: the keyboard reaches the same calls as the on-page pad.
    # A held key repeats only for Backspace, as in a text field; a held digit types once.
    if not e.action.keydown or (e.action.repeat and e.key.name != "Backspace"):
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
        case Confirmation():
          self._confirmation(s)
        case AdminReconfirm() | AdminMenu() | AddEmployeeDetails() | NewPin() | NewFingerprint() | EmployeeAdded():
          self._admin_screen(s)
        case (
          Registering()
          | TooSimilarReview()
          | TooSimilarBlocked()
          | GearMenu()
          | FingerprintActions()
          | FingerPick()
          | ApprovalPrompt()
        ):
          self._fingerprint_screen(s)

  # The per-screen renderers below are split out of `screen` for C901/PLR0912.

  @ui.refreshable_method
  def indicator(self) -> None:
    # The reader line, on every screen: icon, live status, lingering message. Redrawn on its own so a change doesn't
    # redraw the screen under it. Color and dimness go on the icon and status, so a BUSY icon doesn't dim the message.
    indicator = self._controller.reader_indicator
    with ui.row().classes("fixed bottom-10 left-4 z-20 items-center gap-2").mark("reader-indicator", f"reader-{indicator}"):
      ui.icon("fingerprint").classes(f"text-6xl reader-{indicator}").mark("reader-icon")
      if status := text.READER_STATUS[indicator]:
        ui.label(status).classes(f"text-2xl reader-{indicator}").mark("reader-status")
      if (message := self._controller.reader_message) is not None:
        tone = f"reader-message-{message.tone}"
        ui.label(message.text).classes(f"text-2xl {tone}").mark("reader-message", tone)

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

  def _gear(self) -> None:
    # On the idle screen and the punch menu only; above the idle screen's tap layer (z-10), so tapping it isn't an idle tap.
    ui.button(icon="settings", on_click=lambda: self._guarded(self._controller.gear)).props("flat round size=30px").classes(
      "fixed top-2 left-4 z-20 sft-gear"
    ).mark("gear")

  def _idle(self, s: Idle) -> None:
    # A tap anywhere on the idle screen wakes the reader after hours (R40.2): a transparent layer over the whole
    # viewport holds the content, above the clock and status line, which still show through it.
    idle = ui.column().classes("fixed inset-0 z-10 items-center gap-6 pt-8").mark("idle-tap")
    with idle.on("click", lambda: self._guarded(self._controller.tap)):
      if (self._branding_dir / LOGO_FILE).exists():
        ui.image(f"/branding/{LOGO_FILE}").classes("w-96")
      ui.label(s.message or (text.IDLE_READY if self._controller.reader_awake else text.IDLE_ASLEEP)).classes("sft-title")
      if s.advice:  # a registration's re-register advice (case B), styled as the confirmation's alert notices
        ui.label(s.advice).classes("text-3xl").style("color: var(--sft-yellow)").mark("reregister-advice")
      self._button(text.USE_PIN, self._controller.use_pin)
    self._gear()

  def _menu(self, s: Menu) -> None:
    self._gear()
    info = s.info
    ui.label(info.name).classes("sft-title")
    state = text.STATE_LABELS[info.state]
    ui.label(state.format(time=text.format_clock_time(info.since, self._tz)) if info.since else state)
    with ui.grid(columns=2).classes("sft-hours gap-x-8 gap-y-0").mark("hours"):
      for label, value in text.hours_lines(info.hours):
        ui.label(label)
        ui.label(value).classes("text-right")
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
        title = text.NEW_PIN.format(name=s.details.display_name) if s.field == "first" else text.CONFIRM_NEW_PIN
        self._pin_pad(title, "•" * len(s.pin.get_secret_value()), s.error)
      case NewFingerprint():
        ui.label(text.NEW_FINGERPRINT.format(name=s.employee.display_name)).classes("sft-title")
        if s.message:
          ui.label(s.message).classes("sft-error")
        with ui.row():
          self._button(text.REGISTER, self._controller.register, "sft-punch")
          self._button(text.SKIP, self._controller.skip_fingerprint)
      case EmployeeAdded():
        self._employee_added(s)

  def _employee_added(self, s: EmployeeAdded) -> None:
    # Split out of _admin_screen for C901.
    ui.label(text.EMPLOYEE_ADDED.format(name=s.name)).classes("sft-title")
    ui.label(text.EMPLOYEE_NUMBER.format(number=s.number)).classes("text-3xl").mark("employee-number")
    if s.advice:  # as on the idle screen
      ui.label(s.advice).classes("text-3xl").style("color: var(--sft-yellow)").mark("reregister-advice")
    self._button(text.OK, self._controller.cancel, "sft-punch")

  def _registration(self, s: Registering | TooSimilarReview | TooSimilarBlocked) -> None:
    match s:
      case Registering():
        finger = text.FINGER_LABELS[s.finger].lower()
        # Each replaced press counts as made and adds one to the total, so the count never goes back.
        done, total = s.kept + s.replaced, PRESSES + s.replaced
        ui.label(text.REGISTERING.format(finger=finger, done=done, total=total)).classes("sft-title")
        if s.advice:
          ui.label(s.advice).classes("sft-error text-3xl").mark("register-advice")
        with ui.row():
          if done >= PRESSES and s.verdict is None:  # as `KioskController.force_save`
            self._button(text.FORCE_SAVE, self._controller.force_save)
          self._button(text.CANCEL, self._controller.cancel)
      case TooSimilarReview():
        finger = text.FINGER_LABELS[s.verdict.other.finger].lower()
        ui.label(text.TOO_SIMILAR_REVIEW.format(name=s.name, finger=finger)).classes("sft-title")
        with ui.row():  # neither is the default: the admin decides
          self._button(text.ALLOW, self._controller.allow)
          self._button(text.REJECT, self._controller.reject)
      case TooSimilarBlocked():
        ui.label(text.TOO_SIMILAR_BLOCKED).classes("sft-title")
        with ui.row():
          self._button(text.TRY_ANOTHER_FINGER, self._controller.try_another_finger, "sft-punch")
          self._button(text.ADMIN_CHECK, self._controller.ask_admin)
          self._button(text.CANCEL, self._controller.cancel)

  def _fingerprint_screen(
    self, s: Registering | TooSimilarReview | TooSimilarBlocked | GearMenu | FingerprintActions | FingerPick | ApprovalPrompt
  ) -> None:
    match s:
      case Registering() | TooSimilarReview() | TooSimilarBlocked():
        self._registration(s)
      case GearMenu():
        with ui.column().classes("items-center gap-4"):
          self._button(text.FINGERPRINTS, self._controller.open_fingerprints, "sft-punch")
          self._button(text.CANCEL, self._controller.cancel)
      case FingerprintActions():
        self._fingerprint_actions(s)
      case FingerPick():
        self._finger_pick(s)
      case ApprovalPrompt():
        c = s.change
        if isinstance(c, SimilarReview):
          title = text.APPROVE_SIMILAR  # the employee is still there: no names until the admin is
        elif isinstance(c, ForceSave):
          title = text.APPROVE_FORCE
        elif c.finger is None:
          title = text.APPROVE_ADD.format(name=c.name)
        else:
          title = text.APPROVE_CHANGE[c.action].format(name=c.name, finger=text.FINGER_LABELS[c.finger].lower())
        ui.label(title).classes("sft-title")
        ui.label(text.APPROVE_HOW)
        if s.message:
          ui.label(s.message).classes("sft-error")
        with ui.row():
          self._button(text.USE_PIN, self._controller.use_pin)
          self._button(text.CANCEL, self._controller.cancel)

  def _fingerprint_actions(self, s: FingerprintActions) -> None:
    ui.label(text.FINGERPRINT_ACTIONS.format(name=s.name)).classes("sft-title")
    if s.message:
      ui.label(s.message).classes("sft-error")
    with ui.column().classes("items-center gap-4"):
      if s.on_file:
        self._button(text.REREGISTER_FINGER, lambda: self._controller.choose_action("replace"), "sft-punch")
      if len(s.on_file) < len(Finger):
        self._button(text.ADD_FINGER, lambda: self._controller.choose_action("add"), "sft-punch")
      if s.on_file:
        self._button(text.REMOVE_FINGER, lambda: self._controller.choose_action("remove"), "sft-punch")
      self._button(text.CANCEL, self._controller.cancel)

  def _finger_pick(self, s: FingerPick) -> None:
    ui.label(text.PICK_FINGER[s.action]).classes("sft-title")
    for hand in ("left_", "right_"):  # a row per hand
      with ui.row().classes("gap-4"):
        for finger in (f for f in s.choices if f.value.startswith(hand)):
          self._button(text.FINGER_LABELS[finger], lambda finger=finger: self._controller.pick_finger(finger))
    self._button(text.CANCEL, self._controller.cancel)

  def _details_form(self, s: AddEmployeeDetails) -> None:
    # Typed on a plugged-in keyboard (§6.4). Every change goes to the controller, which keeps it without a redraw.
    errors = dict(s.errors)

    def edited(name: DetailsField) -> Callable[[ValueChangeEventArguments], object]:
      return lambda e: self._guarded(lambda: self._controller.edit_details(name, str(e.value)))

    ui.label(text.ADD_EMPLOYEE).classes("sft-title")
    with ui.column().classes("w-[32rem] max-w-full gap-1"):
      texts: tuple[tuple[DetailsField, str, str], ...] = (
        ("first_name", text.FIELD_FIRST_NAME, s.first_name),
        ("middle_name", text.FIELD_MIDDLE_NAME, s.middle_name),
        ("last_name", text.FIELD_LAST_NAME, s.last_name),
        ("phone", text.FIELD_PHONE, s.phone),
        ("email", text.FIELD_EMAIL, s.email),
      )
      for name, label, value in texts:
        field = (
          ui.input(label, value=value, on_change=edited(name)).classes("w-full text-2xl").props("dark outlined").mark(f"field-{name}")
        )
        if name == "phone":
          # The brackets, space and dash are drawn by the field and never part of the value.
          field.props('mask="(###) ###-####" unmasked-value')
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
      with ui.grid(columns=2).classes("sft-hours gap-x-8 gap-y-0").mark("hours"):
        for label, value in s.hours:
          ui.label(label)
          ui.label(value).classes("text-right")
      for warning in s.warnings:
        ui.label(warning).classes("sft-error")
      for notice in s.notices:
        ui.label(notice).classes("text-3xl").style("color: var(--sft-yellow)").mark("alert-notice")

  def _pin_pad(self, title: str, shown: str, error: str | None) -> None:
    ui.label(title).classes("sft-title")
    ui.label(shown).classes("text-4xl h-12").mark("pin-shown")
    # Always drawn, a line high: clearing the error at the next key mustn't shift the keys under a finger.
    ui.label(error or "").classes("sft-error").style("min-height: 1.5em").mark("pin-error")
    with ui.grid(columns=3).classes("gap-2 sft-pad"):
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
