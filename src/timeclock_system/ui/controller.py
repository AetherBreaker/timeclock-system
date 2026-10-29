"""The kiosk's screen state machine and reader schedule (spec §6.3, §6.4, §6.11; R23, R40.2, R45).

The page only renders `screen`; every input and scanner event comes through one entry point here, under one lock.
"""

# Standard library imports
import asyncio
import contextlib
import re
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol

# Third party imports
from pydantic import SecretStr
from timeclock_schema.alerts import CONFIRMATION_NOTICE
from timeclock_schema.enums import BypassReason, EmployeeRole, Finger
from timeclock_schema.models import EMAIL_PATTERN

# First party imports
from timeclock_system.clock import (
  Approval,
  EmployeeBlockFull,
  MenuInfo,
  NewEmployee,
  OverrideGrant,
  PunchRefused,
  check_new_pin,
  normalize_phone,
)
from timeclock_system.hours import is_open
from timeclock_system.scanner import (
  Enrolled,
  EnrollFailed,
  EnrollStage,
  FingerStatus,
  Matched,
  NoMatch,
  ReaderMode,
  ReaderPaused,
  Retry,
)
from timeclock_system.ui import text

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import AsyncGenerator, Callable
  from uuid import UUID
  from zoneinfo import ZoneInfo

  # Third party imports
  from timeclock_schema.enums import PunchKind
  from timeclock_schema.models import Employee

  # First party imports
  from timeclock_system.clock import Clock
  from timeclock_system.scanner import ScannerEvent

RESCAN_AFTER_SECS = 30  # R45.2
IDLE_CLOSE_SECS = 120  # R45.4, for every non-idle screen
CONFIRM_SECS = 5
MESSAGE_SECS = 3  # also how long a success or unrecognized reader message lingers
FAILED_MIN_SECS = 1.0  # the failed read's red never shows shorter
IDLE_ERROR_SECS = 15  # a reader error message on the idle screen
WAKE_SECS = 30  # R40.2
DOUBLE_TAP_SECS = 0.5  # two taps this close end a confirmation early
_MAX_DIGITS = 12
_ADDABLE_ROLES = (EmployeeRole.EMPLOYEE, EmployeeRole.MANAGER)  # never admin (§2)


class ReaderIndicator(StrEnum):
  """The reader line's icon and status, on every screen; live from the reader, never on a timer but the red's floor."""

  READY = "ready"  # waiting for a finger: yellow, "Ready"
  SCANNING = "scanning"  # finger down, being read: blue, "Keep your finger down"
  READ = "read"  # read succeeded, finger still down: green, "Lift your finger"
  UNRECOGNIZED = "unrecognized"  # login read, matched nobody, finger still down: orange, "Lift your finger"
  FAILED = "failed"  # read failed: red, "Couldn't read that, try again"
  PAUSED = "paused"  # paused itself (too hot), which the patched reader shouldn't do: red, "Reader paused"
  BUSY = "busy"  # not taking scans (the gap after a scan): dim, no text
  ASLEEP = "asleep"  # closed after hours until a tap: dim, "Tap to wake the reader"


@dataclass(frozen=True)
class ReaderMessage:
  """What lingers on the reader line after the live status moves on (QoL design §4)."""

  text: str
  tone: Literal["error", "success", "unrecognized"]
  at: float  # monotonic time it was raised


@dataclass(frozen=True)
class Idle:
  """The idle screen, optionally with a message that clears at `until`."""

  message: str | None = None
  until: float | None = None


@dataclass(frozen=True)
class Menu:
  """An employee's punch menu; `auth` is the override grant it was opened with, None for a fingerprint."""

  info: MenuInfo
  auth: OverrideGrant | None
  opened_at: float
  touched_at: float
  notice: str | None = None


@dataclass(frozen=True)
class Rescan:
  """A punch tapped more than 30 s after the scan, waiting for the same employee's finger (R45.2).

  `pin_ok`: an admin or exempt employee may answer with their PIN instead (§6.4), and needs no override.
  """

  employee_id: int
  kind: PunchKind
  auth: OverrideGrant | None
  touched_at: float
  message: str | None = None
  pin_ok: bool = False


@dataclass(frozen=True)
class Confirmation:
  """A recorded punch, shown until `until`, with any rule warnings and alert notices (§6.11)."""

  text: str
  hours: str
  warnings: tuple[str, ...]
  until: float
  notices: tuple[str, ...] = ()


@dataclass(frozen=True)
class OverrideRequest:
  """What a manager override is for: an employee, the reason, and a punch to record at once, if any."""

  employee_id: int
  reason: BypassReason
  pending_kind: PunchKind | None


@dataclass(frozen=True)
class PinRecheck:
  """An admin's or exempt employee's own PIN again: the Admin button's reconfirm (`kind` None) or a 30 s rescan's punch."""

  employee_id: int
  kind: PunchKind | None


type FingerAction = Literal["add", "replace", "remove"]


@dataclass(frozen=True)
class FingerprintLogin:
  """The Fingerprints flow's PIN login, which names whose prints these are; not recorded as a PIN login."""


@dataclass(frozen=True)
class FingerChange:
  """One fingerprint change for an admin to approve; the purpose of an approval by PIN, too."""

  employee_id: int
  name: str
  action: FingerAction
  finger: Finger
  fingerprint_id: UUID | None  # the print replaced or removed


@dataclass(frozen=True)
class PinEntry:
  """The number pad: a PIN login, a manager's PIN for an override, a PIN recheck, or the Fingerprints flow's."""

  field: Literal["username", "password"]
  username: str  # typed first; not secret
  pin: SecretStr  # typed second; a SecretStr until bcrypt checks it, so no repr, log or alert shows it
  purpose: OverrideRequest | PinRecheck | FingerprintLogin | FingerChange | None
  touched_at: float
  error: str | None = None


@dataclass(frozen=True)
class PinScan:
  """After a PIN login, waiting for that employee's finger (R23.3)."""

  employee_id: int
  touched_at: float
  message: str | None = None


@dataclass(frozen=True)
class RegisterPrompt:
  """After a PIN login with no print: register one, or ask a manager (R23.2)."""

  employee_id: int
  touched_at: float


@dataclass(frozen=True)
class Registering:
  """Enrollment in progress; `added` in the add-employee flow's optional step, `change` for an approved gear-menu change."""

  employee_id: int
  done: int
  total: int
  touched_at: float
  finger: Finger
  added: Employee | None = None
  change: FingerChange | None = None
  approval: Approval | None = None


@dataclass(frozen=True)
class OverridePrompt:
  """Waiting for a manager's finger or PIN (R23.5)."""

  request: OverrideRequest
  touched_at: float
  message: str | None = None


@dataclass(frozen=True)
class AdminReconfirm:
  """The Admin button was tapped: waiting for that admin's own finger or PIN before the admin menu (§6.4)."""

  employee_id: int
  touched_at: float
  message: str | None = None


@dataclass(frozen=True)
class AdminMenu:
  """The admin menu; more items (per-clock settings) slot in beside "Add employee" later."""

  admin_id: int
  touched_at: float
  message: str | None = None


type DetailsField = Literal["first_name", "middle_name", "last_name", "phone", "email", "role", "group_id"]


@dataclass(frozen=True)
class AddEmployeeDetails:
  """The add-employee form (§6.4 step 1).

  The typed values and `touched_at` are left out of equality: the page edits them as the admin types, and the page redraws
  only when the screen compares unequal, so typing never redraws the form (which would drop the input's focus). Errors
  do redraw it, with the values kept here.
  """

  admin_id: int
  groups: tuple[tuple[int, str], ...]
  touched_at: float = field(compare=False)
  first_name: str = field(default="", compare=False)
  middle_name: str = field(default="", compare=False)
  last_name: str = field(default="", compare=False)
  phone: str = field(default="", compare=False)
  email: str = field(default="", compare=False)
  role: str = field(default=EmployeeRole.EMPLOYEE.value, compare=False)
  group_id: int = field(default=1, compare=False)
  errors: tuple[tuple[DetailsField, str], ...] = ()


@dataclass(frozen=True)
class NewPin:
  """The new employee's PIN, typed twice on the number pad (§6.4 step 2); nothing is saved until both match."""

  admin_id: int
  details: NewEmployee
  field: Literal["first", "second"]
  first: SecretStr
  pin: SecretStr
  touched_at: float
  error: str | None = None


@dataclass(frozen=True)
class NewFingerprint:
  """The new employee is saved; registering their print is optional and needs no override (§6.4 step 3)."""

  employee: Employee
  touched_at: float
  message: str | None = None


@dataclass(frozen=True)
class EmployeeAdded:
  """The add-employee flow's last screen: the name and number, until OK (§6.4 step 4)."""

  name: str
  number: int
  touched_at: float


@dataclass(frozen=True)
class GearMenu:
  """The gear's menu, from the idle screen or a punch menu; only Fingerprints for now."""

  touched_at: float


@dataclass(frozen=True)
class FingerprintActions:
  """The Fingerprints flow after its login: re-register, add or remove; `on_file` in `Finger` order."""

  employee_id: int
  name: str
  on_file: tuple[Finger, ...]
  touched_at: float
  message: str | None = None


@dataclass(frozen=True)
class FingerPick:
  """Choosing the finger: for a Fingerprints action, or (`action` None) a first registration, `added` as in Registering."""

  employee_id: int
  action: FingerAction | None
  choices: tuple[Finger, ...]
  touched_at: float
  added: Employee | None = None


@dataclass(frozen=True)
class ApprovalPrompt:
  """Waiting for an admin's finger or PIN to approve `change`, immediately before it runs."""

  change: FingerChange
  touched_at: float
  message: str | None = None


type Screen = (
  Idle
  | Menu
  | Rescan
  | Confirmation
  | PinEntry
  | PinScan
  | RegisterPrompt
  | Registering
  | OverridePrompt
  | AdminReconfirm
  | AdminMenu
  | AddEmployeeDetails
  | NewPin
  | NewFingerprint
  | EmployeeAdded
  | GearMenu
  | FingerprintActions
  | FingerPick
  | ApprovalPrompt
)

# Where a finger does something: reader messages are raised and shown only here.
_FINGER_SCREENS = (Idle, Menu, Confirmation, PinScan, Rescan, OverridePrompt, AdminReconfirm, ApprovalPrompt, PinEntry, Registering)
# Arriving at a screen that asks for a finger clears a success or unrecognized message; others carry it.
_ASKING_SCREENS = (Idle, PinScan, Rescan, OverridePrompt, AdminReconfirm, ApprovalPrompt, Registering)


class ReaderPort(Protocol):
  """The part of the scanner the controller drives."""

  def set_mode(self, mode: ReaderMode) -> None:
    """Switch the reader's mode."""


class KioskController:
  """Owns the current screen and the reader's mode."""

  def __init__(
    self,
    clock: Clock,
    reader: ReaderPort,
    *,
    tz: ZoneInfo,
    monotonic: Callable[[], float] = time.monotonic,
    wall_now: Callable[[], datetime] = lambda: datetime.now(UTC),
  ) -> None:
    """Start on the idle screen and set the reader's first mode.

    Args:
      clock: The loaded domain service.
      reader: The scanner, driven through `set_mode`.
      tz: The business's zone, for business hours and displayed times.
      monotonic: Seconds for timeouts, injectable for tests.
      wall_now: The wall clock for business hours, injectable for tests.
    """
    self._clock = clock
    self._reader = reader
    self._tz = tz
    self._mono = monotonic
    self._wall = wall_now
    self._screen: Screen = Idle()
    self._lock = asyncio.Lock()
    self._listeners: list[Callable[[], object]] = []
    self._wake_until = float("-inf")
    # The confirmation and time of its last tap, kept off the screen so the first tap of a double tap doesn't redraw it.
    self._confirmation_tap: tuple[Confirmation, float] | None = None
    # The reader's latest finger-status flags, as the scanner posted them.
    self._present = False
    self._waiting = False
    self._outcome: ReaderIndicator | None = None  # the press's READ or UNRECOGNIZED; cleared by a new press or the lift
    self._failed_at: float | None = None  # the last failed read, until a new press or its red ends
    self._message: ReaderMessage | None = None
    self._paused_until = float("-inf")
    self._mode: ReaderMode | None = None
    self._sync_reader()
    # What the listeners last heard: a change only time makes (an expired message) is found against this.
    self._notified = (self._screen, self.reader_awake, self.reader_indicator, self.reader_message)

  @property
  def screen(self) -> Screen:
    """What the page shows."""
    return self._screen

  @property
  def reader_awake(self) -> bool:
    """Whether the reader is open; the idle screen says "Tap to scan" when it isn't."""
    return self._mode is not ReaderMode.CLOSED

  @property
  def reader_indicator(self) -> ReaderIndicator:
    """What the reader line's icon and status show, in precedence order."""
    now = self._mono()
    if not self.reader_awake:
      return ReaderIndicator.ASLEEP
    if now < self._paused_until:
      return ReaderIndicator.PAUSED
    if self._failed_at is not None and (not self._waiting or now - self._failed_at < FAILED_MIN_SECS):
      return ReaderIndicator.FAILED
    if self._present:
      return self._outcome or ReaderIndicator.SCANNING
    return ReaderIndicator.READY if self._waiting else ReaderIndicator.BUSY

  @property
  def reader_message(self) -> ReaderMessage | None:
    """The reader line's lingering message, if it shows on this screen now."""
    m, s, now = self._message, self._screen, self._mono()
    if m is None or not isinstance(s, _FINGER_SCREENS):
      return None
    if m.tone != "error":
      return m if now - m.at < MESSAGE_SECS else None
    if self.reader_indicator is ReaderIndicator.FAILED:
      return None  # the status says it until the red ends; then the text moves here
    return None if isinstance(s, Idle) and now - m.at >= IDLE_ERROR_SECS else m

  def add_listener(self, cb: Callable[[], object]) -> None:
    """Call `cb` after any entry point that changed `screen`, `reader_awake`, `reader_indicator` or `reader_message`."""
    self._listeners.append(cb)

  def _sync_reader(self) -> None:
    now = self._mono()
    if isinstance(self._screen, Registering):
      mode = ReaderMode.ENROLL
    elif (
      is_open(self._clock.business_hours(), self._wall(), self._tz)
      or now < self._wake_until
      or self._screen != Idle()
      or self._present  # a scan in progress at closing finishes first (R40.2)
    ):
      mode = ReaderMode.READY
    else:
      mode = ReaderMode.CLOSED
    if mode is not self._mode:
      self._mode = mode
      self._reader.set_mode(mode)

  @contextlib.asynccontextmanager
  async def _transition(self) -> AsyncGenerator[None]:
    """Run one entry point under the lock, then set the reader's mode and tell the listeners of any change."""
    async with self._lock:
      before = self._screen
      yield
      self._sync_reader()
      # A screen change is a change of screen type: typing on a pad or in the form, or a new scan's menu, isn't one.
      m, changed = self._message, type(self._screen) is not type(before)
      if m is not None and changed and (m.tone == "error" or isinstance(self._screen, _ASKING_SCREENS)):
        self._message = None
      if self._failed_at is not None and self._waiting and self._mono() - self._failed_at >= FAILED_MIN_SECS:
        self._failed_at = None  # the red has ended; a later gap before the next scan mustn't bring it back
      after = (self._screen, self.reader_awake, self.reader_indicator, self.reader_message)
      if after != self._notified:
        self._notified = after
        for cb in self._listeners:
          cb()

  # -- helpers for the transitions ------------------------------------------------------------------------------

  async def _open_menu(self, employee_id: int, auth: OverrideGrant | None = None, notice: str | None = None) -> Screen:
    now = self._mono()
    try:
      info = await self._clock.menu_info(employee_id)
    except PunchRefused as refused:
      return Idle(refused.message, until=now + MESSAGE_SECS)
    return Menu(info, auth, opened_at=now, touched_at=now, notice=notice)

  async def _record(self, employee_id: int, kind: PunchKind, auth: OverrideGrant | None, *, pin_login: bool = False) -> Screen:
    """Record a punch and confirm it; a refusal reopens the menu with its message and the grant unspent."""
    try:
      outcome = await self._clock.record_punch(employee_id, kind, override=auth, pin_login=pin_login)
    except PunchRefused as refused:
      return await self._open_menu(employee_id, auth, notice=refused.message)
    # The confirmation notice is the clock's one alert action (§6.11); other actions belong to other deliverers.
    notices = tuple(text.alert_notice(f) for f in outcome.firings if CONFIRMATION_NOTICE in f.hook.actions)
    return Confirmation(
      text.CONFIRMATIONS[kind].format(time=text.format_clock_time(outcome.punch.at, self._tz)),
      text.format_hours(outcome.hours_this_week),
      outcome.warnings,
      until=self._mono() + CONFIRM_SECS,
      notices=notices,
    )

  async def _granted(self, request: OverrideRequest, grant: OverrideGrant) -> Screen:
    """A manager authorized `request`: record its pending punch, or open the menu for exactly one punch (R45.3)."""
    if request.pending_kind is not None:
      return await self._record(request.employee_id, request.pending_kind, grant)
    return await self._open_menu(request.employee_id, grant)

  async def _fingerprint_actions(self, employee_id: int, name: str, message: str | None = None) -> FingerprintActions:
    on_file = await self._clock.prints_by_finger(employee_id)
    return FingerprintActions(employee_id, name, tuple(f for f in Finger if f in on_file), self._mono(), message)

  async def _approved(self, change: FingerChange, approval: Approval) -> Screen:
    """Run an approved change: a removal at once, a registration through its touches."""
    now = self._mono()
    if change.action != "remove":
      return Registering(change.employee_id, 0, 5, now, change.finger, change=change, approval=approval)
    if change.fingerprint_id is not None:
      await self._clock.remove_fingerprint(change.employee_id, change.fingerprint_id, change.finger, approval)
    return Idle(text.FINGERPRINT_REMOVED, until=now + MESSAGE_SECS)

  async def _override_request(self, employee_id: int, pending_kind: PunchKind | None) -> OverrideRequest:
    has_print = await self._clock.has_fingerprint(employee_id)
    reason = BypassReason.OVERRIDE_FINGERPRINT_SKIPPED if has_print else BypassReason.OVERRIDE_NO_FINGERPRINT
    return OverrideRequest(employee_id, reason, pending_kind)

  # -- entry points ---------------------------------------------------------------------------------------------

  async def on_scanner_event(self, ev: ScannerEvent) -> None:
    """Apply one event the scanner posted.

    The reader line reports the reader alone; each screen keeps its own error line for who scanned, roles and PINs.
    """
    async with self._transition():
      now = self._mono()
      s = self._screen
      message: ReaderMessage | None = None
      match ev:
        case FingerStatus():
          self._on_finger_status(s, ev)
        case ReaderPaused(seconds=seconds):
          self._paused_until = now + seconds
        case NoMatch():
          self._outcome = ReaderIndicator.UNRECOGNIZED
          message = ReaderMessage(text.NOT_RECOGNIZED, "unrecognized", now)
        case Retry():
          self._failed_at = now
          message = ReaderMessage(text.RETRY, "error", now)
        case Matched(employee_id=employee_id):
          self._outcome = ReaderIndicator.READ
          message = ReaderMessage(text.SCAN_SUCCESSFUL, "success", now)
          await self._on_match(s, employee_id)
        case EnrollStage() | Enrolled() | EnrollFailed() if isinstance(s, Registering):
          message = await self._on_enroll(s, ev)
        case _:
          pass  # every (screen, event) pair the table doesn't list is ignored (R7f)
      if message is not None and isinstance(s, _FINGER_SCREENS):
        self._message = message  # a newer message replaces the old, so a success also clears an error

  def _on_finger_status(self, s: Screen, ev: FingerStatus) -> None:
    # Split out of on_scanner_event for C901.
    if ev.present != self._present:
      self._outcome = None  # a new press has none yet; the lift ends the last one's
    if ev.present and not self._present:
      self._failed_at = None  # a new finger ends the red at once
      if not isinstance(s, Idle | Confirmation):
        self._screen = replace(s, touched_at=self._mono())
    self._present, self._waiting = ev.present, ev.waiting

  async def _on_enroll(self, s: Registering, ev: EnrollStage | Enrolled | EnrollFailed) -> ReaderMessage | None:
    # Split out of on_scanner_event for C901. Returns a press's reader message; registration never shows unrecognized:
    # the indicator doesn't tell a new print from a known one, which is the screen's job.
    now = self._mono()
    match ev:
      case EnrollStage(done=done, total=total, ok=ok):
        self._screen = replace(s, done=done, total=total, touched_at=now)
        if ok:
          self._outcome = ReaderIndicator.READ
          return ReaderMessage(text.SCAN_SUCCESSFUL, "success", now)
        self._failed_at = now
        return ReaderMessage(text.RETRY, "error", now)
      case Enrolled(template=template) if s.change is not None:
        replaces = s.change.fingerprint_id
        await self._clock.register_fingerprint(s.employee_id, template, s.finger, approval=s.approval, replaces=replaces)
        self._screen = Idle(text.FINGERPRINT_SAVED, until=now + MESSAGE_SECS)
      case Enrolled(template=template):
        await self._clock.register_fingerprint(s.employee_id, template, s.finger)
        added = s.added
        self._screen = await self._open_menu(s.employee_id) if added is None else EmployeeAdded(added.display_name, added.id, now)
      case EnrollFailed(reason=reason):
        message = text.REGISTRATION_TIMED_OUT if reason == "timeout" else text.REGISTRATION_FAILED
        if s.change is not None:
          # Back to the actions, still logged in; the old print is untouched, and the next choice needs a new approval.
          self._screen = await self._fingerprint_actions(s.employee_id, s.change.name, message)
        else:
          # In the add-employee flow the employee is already saved: back to the optional step, to retry or skip.
          self._screen = Idle(message, until=now + MESSAGE_SECS) if s.added is None else NewFingerprint(s.added, now, message)

  async def _on_pad_match(self, pad: PinEntry, employee_id: int) -> None:
    """A finger on a PIN pad does what it would have done on the screen before Use PIN; typed digits are discarded.

    The new employee's pad (NewPin) isn't a login, so a finger there stays ignored. Split out of `_on_match` for PLR0912.
    """
    now, p = self._mono(), pad.purpose
    match p:
      case None:
        before: Screen = Idle()
      case PinRecheck(employee_id=owner, kind=kind):
        before = AdminReconfirm(owner, now) if kind is None else Rescan(owner, kind, None, now, pin_ok=True)
      case OverrideRequest():
        before = OverridePrompt(p, now)
      case FingerChange():
        before = ApprovalPrompt(p, now)
      case FingerprintLogin():
        if (employee := self._clock.employee(employee_id)) is not None:
          self._screen = await self._fingerprint_actions(employee.id, employee.display_name)
        return
    await self._on_match(before, employee_id)

  async def _on_match(self, s: Screen, employee_id: int) -> None:
    # Split out of on_scanner_event for C901.
    if isinstance(s, PinEntry):
      await self._on_pad_match(s, employee_id)
    elif isinstance(s, Idle | Menu | Confirmation):
      self._screen = await self._open_menu(employee_id)  # a new scan switches the menu (R45.4)
    elif isinstance(s, AdminReconfirm):
      # The admin's own finger opens the admin menu; anyone else's opens their punch menu, as a rescan does.
      self._screen = AdminMenu(employee_id, self._mono()) if employee_id == s.employee_id else await self._open_menu(employee_id)
    elif isinstance(s, Rescan):
      same = employee_id == s.employee_id
      self._screen = await (self._record(employee_id, s.kind, None) if same else self._open_menu(employee_id))
    elif isinstance(s, PinScan):
      self._screen = await self._open_menu(employee_id) if employee_id == s.employee_id else replace(s, message=text.NOT_YOUR_FINGER)
    elif isinstance(s, OverridePrompt):
      manager = self._clock.employee(employee_id)
      if manager is not None and self._clock.override_manager(manager, s.request.employee_id):
        grant = OverrideGrant(manager.id, s.request.reason, pin_only=False)
        self._screen = await self._granted(s.request, grant)
      else:
        self._screen = replace(s, message=text.NOT_A_MANAGER)
    elif isinstance(s, ApprovalPrompt):
      admin = self._clock.employee(employee_id)
      if admin is not None and self._clock.fingerprint_approver(admin):
        self._screen = await self._approved(s.change, Approval(admin.id, pin_only=False))
      else:
        self._screen = replace(s, message=text.NOT_AN_ADMIN)

  async def tap(self) -> None:
    """A tap on the idle screen wakes the reader for 30 s (R40.2); a double tap ends a confirmation early."""
    async with self._transition():
      s, now = self._screen, self._mono()
      if isinstance(s, Idle):
        self._wake_until = now + WAKE_SECS
      elif isinstance(s, Confirmation):
        last = self._confirmation_tap
        if last is not None and last[0] is s and now - last[1] <= DOUBLE_TAP_SECS:
          self._screen, self._confirmation_tap = Idle(), None
        else:
          self._confirmation_tap = (s, now)

  async def use_pin(self) -> None:
    """Open the number pad: a PIN login, a manager's override PIN, or an admin's or exempt employee's own PIN again."""
    async with self._transition():
      now = self._mono()
      s = self._screen
      if isinstance(s, Idle | Confirmation):
        self._screen = PinEntry("username", "", SecretStr(""), None, touched_at=now)
      elif isinstance(s, OverridePrompt):
        self._screen = PinEntry("username", "", SecretStr(""), s.request, touched_at=now)
      elif isinstance(s, AdminReconfirm):
        self._screen = PinEntry("username", "", SecretStr(""), PinRecheck(s.employee_id, None), touched_at=now)
      elif isinstance(s, Rescan) and s.pin_ok:
        self._screen = PinEntry("username", "", SecretStr(""), PinRecheck(s.employee_id, s.kind), touched_at=now)
      elif isinstance(s, ApprovalPrompt):
        self._screen = PinEntry("username", "", SecretStr(""), s.change, touched_at=now)

  async def digit(self, d: str) -> None:
    """Type one digit on the number pad; on the idle screen, open the pad with it typed (walk up and type)."""
    async with self._transition():
      s = self._screen
      if not (isinstance(s, Idle | PinEntry | NewPin) and len(d) == 1 and d.isdecimal() and d.isascii()):
        return
      now = self._mono()
      if isinstance(s, Idle):
        self._screen = PinEntry("username", d, SecretStr(""), None, touched_at=now)
        return
      if isinstance(s, PinEntry) and s.field == "username":
        if len(s.username) < _MAX_DIGITS:
          self._screen = replace(s, username=s.username + d, touched_at=now, error=None)
      elif len(pin := s.pin.get_secret_value()) < _MAX_DIGITS:
        self._screen = replace(s, pin=SecretStr(pin + d), touched_at=now, error=None)

  async def backspace(self) -> None:
    """Delete the last digit."""
    async with self._transition():
      s = self._screen
      if isinstance(s, PinEntry) and s.field == "username":
        self._screen = replace(s, username=s.username[:-1], touched_at=self._mono(), error=None)
      elif isinstance(s, PinEntry | NewPin):
        self._screen = replace(s, pin=SecretStr(s.pin.get_secret_value()[:-1]), touched_at=self._mono(), error=None)

  async def enter(self) -> None:
    """Submit the username, then the password; or the new employee's PIN, then its confirmation."""
    async with self._transition():
      s = self._screen
      if isinstance(s, NewPin) and s.pin.get_secret_value():
        self._screen = await self._enter_new_pin(s)
        return
      if not isinstance(s, PinEntry) or not (s.username if s.field == "username" else s.pin.get_secret_value()):
        return
      now = self._mono()
      if s.field == "username":
        self._screen = PinEntry("password", s.username, SecretStr(""), s.purpose, touched_at=now)
        return
      employee = await self._clock.verify_pin(s.username, s.pin)
      if employee is None:
        self._screen = PinEntry("username", "", SecretStr(""), s.purpose, touched_at=now, error=text.WRONG_PIN)
      elif isinstance(s.purpose, PinRecheck):
        self._screen = await self._recheck(s.purpose, employee)
      elif s.purpose is not None:
        self._screen = await self._purpose_pin(s.purpose, employee)
      elif self._clock.pin_only_login(employee):
        # No fingerprint step, no register prompt, no override (§6.4); each such login is recorded.
        await self._clock.record_pin_login(employee)
        self._screen = await self._open_menu(employee.id)
      elif await self._clock.has_fingerprint(employee.id):
        self._screen = PinScan(employee.id, touched_at=now)
      else:
        self._screen = RegisterPrompt(employee.id, touched_at=now)

  async def _purpose_pin(self, purpose: OverrideRequest | FingerprintLogin | FingerChange, employee: Employee) -> Screen:
    """A manager's override PIN, the Fingerprints flow's login, or an admin's approval by PIN; split out of `enter` for C901."""
    if isinstance(purpose, OverrideRequest):
      if self._clock.override_manager(employee, purpose.employee_id):
        return await self._granted(purpose, OverrideGrant(employee.id, purpose.reason, pin_only=True))
      return OverridePrompt(purpose, touched_at=self._mono(), message=text.NOT_A_MANAGER)
    if isinstance(purpose, FingerprintLogin):
      return await self._fingerprint_actions(employee.id, employee.display_name)
    if self._clock.fingerprint_approver(employee):
      return await self._approved(purpose, Approval(employee.id, pin_only=True))
    return ApprovalPrompt(purpose, touched_at=self._mono(), message=text.NOT_AN_ADMIN)

  async def _recheck(self, purpose: PinRecheck, employee: Employee) -> Screen:
    """A PIN recheck by `employee`; split out of `enter` for C901. It must be the same person's PIN."""
    if employee.id != purpose.employee_id:
      return PinEntry("username", "", SecretStr(""), purpose, touched_at=self._mono(), error=text.NOT_YOUR_PIN)
    if purpose.kind is None:
      await self._clock.record_pin_login(employee)
      return AdminMenu(employee.id, self._mono())
    return await self._record(employee.id, purpose.kind, None, pin_login=True)

  async def _enter_new_pin(self, s: NewPin) -> Screen:
    """The new employee's PIN, then its confirmation; saved once both match. Split out of `enter` for C901."""
    now = self._mono()
    if s.field == "first":
      return replace(s, field="second", first=s.pin, pin=SecretStr(""), touched_at=now, error=None)
    if s.pin.get_secret_value() != s.first.get_secret_value():
      return replace(s, field="first", first=SecretStr(""), pin=SecretStr(""), touched_at=now, error=text.PIN_MISMATCH)
    if not check_new_pin(s.pin):
      return replace(s, field="first", first=SecretStr(""), pin=SecretStr(""), touched_at=now, error=text.PIN_DIGITS_ONLY)
    try:
      employee = await self._clock.add_employee(s.details, s.pin)
    except EmployeeBlockFull:
      return AdminMenu(s.admin_id, now, message=text.BLOCK_FULL)
    return NewFingerprint(employee, now)

  async def punch(self, kind: PunchKind) -> None:
    """A punch button on the menu: recorded within 30 s of the scan, else it asks for a new scan (R45.2)."""
    async with self._transition():
      s = self._screen
      if not isinstance(s, Menu):
        return
      now = self._mono()
      if now - s.opened_at > RESCAN_AFTER_SECS:
        employee = self._clock.employee(s.info.employee_id)
        pin_ok = employee is not None and self._clock.pin_only_login(employee)
        self._screen = Rescan(s.info.employee_id, kind, s.auth, touched_at=now, pin_ok=pin_ok)
      else:
        screen = await self._record(s.info.employee_id, kind, s.auth)
        # A refusal reopens the menu; only a new scan restarts R45.2's 30 s window, so it keeps this menu's.
        self._screen = replace(screen, opened_at=s.opened_at) if isinstance(screen, Menu) else screen

  async def register(self) -> None:
    """Register the employee's first print (R23.4), or the new employee's (§6.4 step 3): which finger first."""
    async with self._transition():
      s = self._screen
      if isinstance(s, RegisterPrompt):
        self._screen = FingerPick(s.employee_id, None, tuple(Finger), touched_at=self._mono())
      elif isinstance(s, NewFingerprint):
        self._screen = FingerPick(s.employee.id, None, tuple(Finger), touched_at=self._mono(), added=s.employee)

  async def pick_finger(self, finger: Finger) -> None:
    """Pick the finger: a first registration starts at once; a Fingerprints action waits for an admin's approval."""
    async with self._transition():
      s, now = self._screen, self._mono()
      if not isinstance(s, FingerPick) or finger not in s.choices:
        return
      if s.action is None:
        self._screen = Registering(s.employee_id, 0, 5, now, finger, added=s.added)
        return
      employee = self._clock.employee(s.employee_id)
      name = str(s.employee_id) if employee is None else employee.display_name
      fingerprint_id = (await self._clock.prints_by_finger(s.employee_id)).get(finger)
      if (fingerprint_id is None) != (s.action == "add"):
        # Another clock changed this finger since the list was drawn.
        self._screen = await self._fingerprint_actions(s.employee_id, name)
      else:
        self._screen = ApprovalPrompt(FingerChange(s.employee_id, name, s.action, finger, fingerprint_id), touched_at=now)

  async def gear(self) -> None:
    """The gear, on the idle screen or a punch menu (which it leaves, as Cancel would): its menu."""
    async with self._transition():
      if isinstance(self._screen, Idle | Menu):
        self._screen = GearMenu(touched_at=self._mono())

  async def open_fingerprints(self) -> None:
    """The gear menu's Fingerprints: a PIN login first, so the clock knows whose prints these are."""
    async with self._transition():
      if isinstance(self._screen, GearMenu):
        self._screen = PinEntry("username", "", SecretStr(""), FingerprintLogin(), touched_at=self._mono())

  async def choose_action(self, action: FingerAction) -> None:
    """Choose a Fingerprints action, then a finger: one on file, or for add one that isn't."""
    async with self._transition():
      s = self._screen
      if not isinstance(s, FingerprintActions):
        return
      choices = tuple(f for f in Finger if (f in s.on_file) != (action == "add"))
      if choices:
        self._screen = FingerPick(s.employee_id, action, choices, touched_at=self._mono())

  async def admin(self) -> None:
    """The Admin button on an admin's menu: reconfirm by their own finger or PIN first (§6.4)."""
    async with self._transition():
      s = self._screen
      if isinstance(s, Menu) and s.info.is_admin:
        self._screen = AdminReconfirm(s.info.employee_id, touched_at=self._mono())

  async def add_employee(self) -> None:
    """Open the add-employee form from the admin menu."""
    async with self._transition():
      s = self._screen
      if isinstance(s, AdminMenu):
        groups = tuple((g.id, g.name) for g in self._clock.groups())
        self._screen = AddEmployeeDetails(s.admin_id, groups, touched_at=self._mono(), group_id=groups[0][0] if groups else 1)

  async def edit_details(self, name: DetailsField, value: str) -> None:
    """One field of the add-employee form changed; kept on the screen, which doesn't redraw (see `AddEmployeeDetails`)."""
    async with self._transition():
      s = self._screen
      if isinstance(s, AddEmployeeDetails):
        typed: str | int = int(value) if name == "group_id" else value
        self._screen = replace(s, touched_at=self._mono(), **{name: typed})

  async def submit_details(self) -> None:
    """Check the form: errors beside their fields, or on to the PIN."""
    async with self._transition():
      s = self._screen
      if not isinstance(s, AddEmployeeDetails):
        return
      errors: list[tuple[DetailsField, str]] = []
      first, middle, last = s.first_name.strip(), s.middle_name.strip(), s.last_name.strip()
      phone, email = s.phone.strip(), s.email.strip()
      if not first:
        errors.append(("first_name", text.FIRST_NAME_REQUIRED))
      if not last:
        errors.append(("last_name", text.LAST_NAME_REQUIRED))
      try:
        stored_phone = normalize_phone(phone) if phone else None
      except ValueError:
        stored_phone = None
        errors.append(("phone", text.PHONE_INVALID))
      if email and re.fullmatch(EMAIL_PATTERN, email) is None:
        errors.append(("email", text.EMAIL_INVALID))
      role = next((r for r in _ADDABLE_ROLES if r.value == s.role), None)
      if role is None:
        errors.append(("role", text.ROLE_INVALID))
      if s.group_id not in {gid for gid, _ in s.groups}:
        errors.append(("group_id", text.GROUP_INVALID))
      now = self._mono()
      if errors or role is None:
        self._screen = replace(s, touched_at=now, errors=tuple(errors))
      else:
        details = NewEmployee(first, middle or None, last, stored_phone, email or None, role, s.group_id)
        self._screen = NewPin(s.admin_id, details, "first", SecretStr(""), SecretStr(""), touched_at=now)

  async def skip_fingerprint(self) -> None:
    """Skip the new employee's optional fingerprint; they enrol later through the normal flow."""
    async with self._transition():
      s = self._screen
      if isinstance(s, NewFingerprint):
        self._screen = EmployeeAdded(s.employee.display_name, s.employee.id, touched_at=self._mono())

  async def request_override(self) -> None:
    """Ask a manager to authorize instead of a fingerprint (R23.5)."""
    async with self._transition():
      s = self._screen
      now = self._mono()
      if isinstance(s, Rescan):
        self._screen = OverridePrompt(await self._override_request(s.employee_id, s.kind), touched_at=now)
      elif isinstance(s, PinScan):
        self._screen = OverridePrompt(await self._override_request(s.employee_id, None), touched_at=now)
      elif isinstance(s, RegisterPrompt):
        self._screen = OverridePrompt(OverrideRequest(s.employee_id, BypassReason.OVERRIDE_NO_FINGERPRINT, None), touched_at=now)

  async def cancel(self) -> None:
    """Back to idle from any screen; in the add-employee flow's optional fingerprint step it counts as a skip (§6.4).

    Abandoning a fingerprint change, at its approval or its touches, changes nothing.
    """
    async with self._transition():
      if self._mode is ReaderMode.ENROLL:
        self._mode = ReaderMode.READY
        self._reader.set_mode(ReaderMode.READY)
      s = self._screen
      if isinstance(s, NewFingerprint):
        self._screen = EmployeeAdded(s.employee.display_name, s.employee.id, touched_at=self._mono())
      elif isinstance(s, Registering | FingerPick) and s.added is not None:
        self._screen = EmployeeAdded(s.added.display_name, s.added.id, touched_at=self._mono())
      else:
        self._screen = Idle()

  async def tick(self) -> None:
    """Expire timed screens and messages, and follow business hours; the app calls it every 0.5 s (R5)."""
    async with self._transition():
      now = self._mono()
      s = self._screen
      if isinstance(s, Idle | Confirmation):
        if s.until is not None and now >= s.until:
          self._screen = Idle()
      elif now - s.touched_at >= IDLE_CLOSE_SECS:
        # The new employee is already saved, so a timeout on their optional fingerprint step still shows their number.
        if isinstance(s, NewFingerprint):
          self._screen = EmployeeAdded(s.employee.display_name, s.employee.id, touched_at=now)
        elif isinstance(s, Registering | FingerPick) and s.added is not None:
          self._screen = EmployeeAdded(s.added.display_name, s.added.id, touched_at=now)
        else:
          self._screen = Idle()
