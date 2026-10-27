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
from timeclock_schema.enums import BypassReason, EmployeeRole
from timeclock_schema.models import EMAIL_PATTERN

# First party imports
from timeclock_system.clock import (
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
  FingerDown,
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
MESSAGE_SECS = 3
WAKE_SECS = 30  # R40.2
DOUBLE_TAP_SECS = 0.5  # two taps this close end a confirmation early
SCAN_IN_PROGRESS_SECS = 16  # the spike's slowest resting thumb
_MAX_DIGITS = 12
_ADDABLE_ROLES = (EmployeeRole.EMPLOYEE, EmployeeRole.MANAGER)  # never admin (§2)


class ReaderIndicator(StrEnum):
  """The reader icon in the screen's corner, on every screen."""

  READY = "ready"  # waiting for a finger
  SCANNING = "scanning"  # a finger is down and its scan hasn't finished
  PAUSED = "paused"  # the reader paused itself (too hot), which the patched reader shouldn't do
  ASLEEP = "asleep"  # closed outside business hours until a tap


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


@dataclass(frozen=True)
class PinEntry:
  """The number pad: a PIN login, a manager's PIN for an override, or a PIN recheck."""

  field: Literal["username", "password"]
  username: str  # typed first; not secret
  pin: SecretStr  # typed second; a SecretStr until bcrypt checks it, so no repr, log or alert shows it
  purpose: OverrideRequest | PinRecheck | None
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
  """Enrollment in progress; `added` is set when it's the add-employee flow's optional step."""

  employee_id: int
  done: int
  total: int
  touched_at: float
  added: Employee | None = None


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


type DetailsField = Literal["name", "phone", "email", "role", "group_id"]


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
  name: str = field(default="", compare=False)
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
)


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
    self._last_finger = float("-inf")
    self._scanning = False  # a FingerDown not yet followed by its scan's outcome
    self._paused_until = float("-inf")
    self._mode: ReaderMode | None = None
    self._sync_reader()

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
    """What the reader icon shows; a scan with no outcome within the slowest scan's time stops counting."""
    now = self._mono()
    if not self.reader_awake:
      return ReaderIndicator.ASLEEP
    if now < self._paused_until:
      return ReaderIndicator.PAUSED
    if self._scanning and now - self._last_finger < SCAN_IN_PROGRESS_SECS:
      return ReaderIndicator.SCANNING
    return ReaderIndicator.READY

  def add_listener(self, cb: Callable[[], object]) -> None:
    """Call `cb` after any entry point that changed `screen`, `reader_awake` or `reader_indicator`."""
    self._listeners.append(cb)

  def _sync_reader(self) -> None:
    now = self._mono()
    if isinstance(self._screen, Registering):
      mode = ReaderMode.ENROLL
    elif (
      is_open(self._clock.business_hours(), self._wall(), self._tz)
      or now < self._wake_until
      or self._screen != Idle()
      or now - self._last_finger < SCAN_IN_PROGRESS_SECS  # a scan in progress at closing finishes first (R40.2)
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
      before = (self._screen, self.reader_awake, self.reader_indicator)
      yield
      self._sync_reader()
      if (self._screen, self.reader_awake, self.reader_indicator) != before:
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

  async def _override_request(self, employee_id: int, pending_kind: PunchKind | None) -> OverrideRequest:
    has_print = await self._clock.has_fingerprint(employee_id)
    reason = BypassReason.OVERRIDE_FINGERPRINT_SKIPPED if has_print else BypassReason.OVERRIDE_NO_FINGERPRINT
    return OverrideRequest(employee_id, reason, pending_kind)

  # -- entry points ---------------------------------------------------------------------------------------------

  async def on_scanner_event(self, ev: ScannerEvent) -> None:
    """Apply one event the scanner posted."""
    async with self._transition():
      now = self._mono()
      s = self._screen
      # Every outcome below ends the scan the indicator shows; only FingerDown starts one.
      self._scanning = isinstance(ev, FingerDown)
      match ev:
        case FingerDown():
          self._last_finger = now
          if not isinstance(s, Idle | Confirmation):
            self._screen = replace(s, touched_at=now)
        case ReaderPaused(seconds=seconds):
          self._paused_until = now + seconds
        case NoMatch() | Retry():
          message = text.NOT_RECOGNIZED if isinstance(ev, NoMatch) else text.RETRY
          if isinstance(s, Idle):
            self._screen = Idle(message, until=now + MESSAGE_SECS)
          elif isinstance(s, Rescan | PinScan | OverridePrompt | AdminReconfirm):
            self._screen = replace(s, message=message)
        case Matched(employee_id=employee_id):
          await self._on_match(s, employee_id)
        case EnrollStage() | Enrolled() | EnrollFailed() if isinstance(s, Registering):
          await self._on_enroll(s, ev)
        case _:
          pass  # every (screen, event) pair the table doesn't list is ignored (R7f)

  async def _on_enroll(self, s: Registering, ev: EnrollStage | Enrolled | EnrollFailed) -> None:
    # Split out of on_scanner_event for C901.
    now = self._mono()
    match ev:
      case EnrollStage(done=done, total=total):
        self._last_finger = now  # a press, like a FingerDown, holds the reader open at closing
        self._screen = replace(s, done=done, total=total, touched_at=now)
      case Enrolled(template=template):
        await self._clock.register_fingerprint(s.employee_id, template)
        added = s.added
        self._screen = await self._open_menu(s.employee_id) if added is None else EmployeeAdded(added.name, added.id, now)
      case EnrollFailed(reason=reason):
        message = text.REGISTRATION_TIMED_OUT if reason == "timeout" else text.REGISTRATION_FAILED
        # In the add-employee flow the employee is already saved: back to the optional step, to retry or skip.
        self._screen = Idle(message, until=now + MESSAGE_SECS) if s.added is None else NewFingerprint(s.added, now, message)

  async def _on_match(self, s: Screen, employee_id: int) -> None:
    # Split out of on_scanner_event for C901.
    if isinstance(s, Idle | Menu | Confirmation):
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

  async def digit(self, d: str) -> None:
    """Type one digit on the number pad."""
    async with self._transition():
      s = self._screen
      if not (isinstance(s, PinEntry | NewPin) and len(d) == 1 and d.isdecimal() and d.isascii()):
        return
      now = self._mono()
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
      elif isinstance(s.purpose, OverrideRequest):
        if self._clock.override_manager(employee, s.purpose.employee_id):
          self._screen = await self._granted(s.purpose, OverrideGrant(employee.id, s.purpose.reason, pin_only=True))
        else:
          self._screen = OverridePrompt(s.purpose, touched_at=now, message=text.NOT_A_MANAGER)
      elif isinstance(s.purpose, PinRecheck):
        self._screen = await self._recheck(s.purpose, employee)
      elif self._clock.pin_only_login(employee):
        # No fingerprint step, no register prompt, no override (§6.4); each such login is recorded.
        await self._clock.record_pin_login(employee)
        self._screen = await self._open_menu(employee.id)
      elif await self._clock.has_fingerprint(employee.id):
        self._screen = PinScan(employee.id, touched_at=now)
      else:
        self._screen = RegisterPrompt(employee.id, touched_at=now)

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
    """Start enrolling the employee's finger (R23.4), or the new employee's (§6.4 step 3)."""
    async with self._transition():
      s = self._screen
      if isinstance(s, RegisterPrompt):
        self._screen = Registering(s.employee_id, 0, 5, touched_at=self._mono())
      elif isinstance(s, NewFingerprint):
        self._screen = Registering(s.employee.id, 0, 5, touched_at=self._mono(), added=s.employee)

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
      name, phone, email = s.name.strip(), s.phone.strip(), s.email.strip()
      if not name:
        errors.append(("name", text.NAME_REQUIRED))
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
        details = NewEmployee(name, stored_phone, email or None, role, s.group_id)
        self._screen = NewPin(s.admin_id, details, "first", SecretStr(""), SecretStr(""), touched_at=now)

  async def skip_fingerprint(self) -> None:
    """Skip the new employee's optional fingerprint; they enrol later through the normal flow."""
    async with self._transition():
      s = self._screen
      if isinstance(s, NewFingerprint):
        self._screen = EmployeeAdded(s.employee.name, s.employee.id, touched_at=self._mono())

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
    """Back to idle from any screen; in the add-employee flow's optional fingerprint step it counts as a skip (§6.4)."""
    async with self._transition():
      if self._mode is ReaderMode.ENROLL:
        self._mode = ReaderMode.READY
        self._reader.set_mode(ReaderMode.READY)
      s = self._screen
      if isinstance(s, NewFingerprint):
        self._screen = EmployeeAdded(s.employee.name, s.employee.id, touched_at=self._mono())
      elif isinstance(s, Registering) and s.added is not None:
        self._screen = EmployeeAdded(s.added.name, s.added.id, touched_at=self._mono())
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
          self._screen = EmployeeAdded(s.employee.name, s.employee.id, touched_at=now)
        elif isinstance(s, Registering) and s.added is not None:
          self._screen = EmployeeAdded(s.added.name, s.added.id, touched_at=now)
        else:
          self._screen = Idle()
