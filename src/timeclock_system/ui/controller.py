"""The kiosk's screen state machine and reader schedule (spec §6.3, §6.4; R23, R40.2, R45).

The page only renders `screen`; every input and scanner event comes through one entry point here, under one lock.
"""

# Standard library imports
import asyncio
import contextlib
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol

# Third party imports
from timeclock_schema.enums import OverrideReason

# First party imports
from timeclock_system.clock import MenuInfo, OverrideGrant, PunchRefused
from timeclock_system.hours import is_open
from timeclock_system.scanner import Enrolled, EnrollFailed, EnrollStage, FingerDown, Matched, NoMatch, ReaderMode, Retry
from timeclock_system.ui import text

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import AsyncGenerator, Callable
  from zoneinfo import ZoneInfo

  # Third party imports
  from timeclock_schema.enums import PunchKind

  # First party imports
  from timeclock_system.clock import Clock
  from timeclock_system.scanner import ScannerEvent

RESCAN_AFTER_SECS = 30  # R45.2
IDLE_CLOSE_SECS = 120  # R45.4, for every non-idle screen
CONFIRM_SECS = 5
MESSAGE_SECS = 3
WAKE_SECS = 30  # R40.2
SCAN_IN_PROGRESS_SECS = 16  # the spike's slowest resting thumb
_MAX_DIGITS = 12


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
  """A punch tapped more than 30 s after the scan, waiting for the same employee's finger (R45.2)."""

  employee_id: int
  kind: PunchKind
  auth: OverrideGrant | None
  touched_at: float
  message: str | None = None


@dataclass(frozen=True)
class Confirmation:
  """A recorded punch, shown until `until`."""

  text: str
  hours: str
  warnings: tuple[str, ...]
  until: float


@dataclass(frozen=True)
class OverrideRequest:
  """What a manager override is for: an employee, the reason, and a punch to record at once, if any."""

  employee_id: int
  reason: OverrideReason
  pending_kind: PunchKind | None


@dataclass(frozen=True)
class PinEntry:
  """The number pad: a PIN login, or a manager's PIN for `override`."""

  field: Literal["username", "password"]
  username: str
  digits: str
  override: OverrideRequest | None
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
  """Enrollment in progress."""

  employee_id: int
  done: int
  total: int
  touched_at: float


@dataclass(frozen=True)
class OverridePrompt:
  """Waiting for a manager's finger or PIN (R23.5)."""

  request: OverrideRequest
  touched_at: float
  message: str | None = None


type Screen = Idle | Menu | Rescan | Confirmation | PinEntry | PinScan | RegisterPrompt | Registering | OverridePrompt


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
    self._last_finger = float("-inf")
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

  def add_listener(self, cb: Callable[[], object]) -> None:
    """Call `cb` after any entry point that changed `screen` or `reader_awake`."""
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
      before = (self._screen, self.reader_awake)
      yield
      self._sync_reader()
      if (self._screen, self.reader_awake) != before:
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

  async def _record(self, employee_id: int, kind: PunchKind, auth: OverrideGrant | None) -> Screen:
    """Record a punch and confirm it; a refusal reopens the menu with its message and the grant unspent."""
    try:
      outcome = await self._clock.record_punch(employee_id, kind, override=auth)
    except PunchRefused as refused:
      return await self._open_menu(employee_id, auth, notice=refused.message)
    return Confirmation(
      text.CONFIRMATIONS[kind].format(time=text.format_clock_time(outcome.punch.at, self._tz)),
      text.format_hours(outcome.hours_this_week),
      outcome.warnings,
      until=self._mono() + CONFIRM_SECS,
    )

  async def _granted(self, request: OverrideRequest, grant: OverrideGrant) -> Screen:
    """A manager authorized `request`: record its pending punch, or open the menu for exactly one punch (R45.3)."""
    if request.pending_kind is not None:
      return await self._record(request.employee_id, request.pending_kind, grant)
    return await self._open_menu(request.employee_id, grant)

  async def _override_request(self, employee_id: int, pending_kind: PunchKind | None) -> OverrideRequest:
    has_print = await self._clock.has_fingerprint(employee_id)
    reason = OverrideReason.FINGERPRINT_SKIPPED if has_print else OverrideReason.NO_FINGERPRINT
    return OverrideRequest(employee_id, reason, pending_kind)

  # -- entry points ---------------------------------------------------------------------------------------------

  async def on_scanner_event(self, ev: ScannerEvent) -> None:
    """Apply one event the scanner posted."""
    async with self._transition():
      now = self._mono()
      s = self._screen
      match ev:
        case FingerDown():
          self._last_finger = now
          if isinstance(s, Idle):
            self._screen = Idle(text.SCANNING, until=now + SCAN_IN_PROGRESS_SECS)
          elif isinstance(s, Rescan | PinScan | OverridePrompt):
            self._screen = replace(s, message=text.SCANNING, touched_at=now)
          elif isinstance(s, Menu | PinEntry | RegisterPrompt | Registering):
            self._screen = replace(s, touched_at=now)
        case NoMatch() | Retry():
          message = text.NOT_RECOGNIZED if isinstance(ev, NoMatch) else text.RETRY
          if isinstance(s, Idle):
            self._screen = Idle(message, until=now + MESSAGE_SECS)
          elif isinstance(s, Rescan | PinScan | OverridePrompt):
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
        self._screen = await self._open_menu(s.employee_id)
      case EnrollFailed(reason=reason):
        message = text.REGISTRATION_TIMED_OUT if reason == "timeout" else text.REGISTRATION_FAILED
        self._screen = Idle(message, until=now + MESSAGE_SECS)

  async def _on_match(self, s: Screen, employee_id: int) -> None:
    # Split out of on_scanner_event for C901.
    if isinstance(s, Idle | Menu | Confirmation):
      self._screen = await self._open_menu(employee_id)  # a new scan switches the menu (R45.4)
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
    """A tap on the idle screen wakes the reader for 30 s (R40.2)."""
    async with self._transition():
      if isinstance(self._screen, Idle):
        self._wake_until = self._mono() + WAKE_SECS

  async def use_pin(self) -> None:
    """Open the number pad: a PIN login from idle or a confirmation, or a manager's PIN on an override prompt."""
    async with self._transition():
      now = self._mono()
      s = self._screen
      if isinstance(s, Idle | Confirmation):
        self._screen = PinEntry("username", "", "", None, touched_at=now)
      elif isinstance(s, OverridePrompt):
        self._screen = PinEntry("username", "", "", s.request, touched_at=now)

  async def digit(self, d: str) -> None:
    """Type one digit on the number pad."""
    async with self._transition():
      s = self._screen
      if isinstance(s, PinEntry) and len(d) == 1 and d.isdecimal() and d.isascii() and len(s.digits) < _MAX_DIGITS:
        self._screen = replace(s, digits=s.digits + d, touched_at=self._mono(), error=None)

  async def backspace(self) -> None:
    """Delete the last digit."""
    async with self._transition():
      s = self._screen
      if isinstance(s, PinEntry):
        self._screen = replace(s, digits=s.digits[:-1], touched_at=self._mono(), error=None)

  async def enter(self) -> None:
    """Submit the username, then the password."""
    async with self._transition():
      s = self._screen
      if not isinstance(s, PinEntry) or not s.digits:
        return
      now = self._mono()
      if s.field == "username":
        self._screen = PinEntry("password", s.digits, "", s.override, touched_at=now)
        return
      employee = await self._clock.verify_pin(s.username, s.digits)
      if employee is None:
        self._screen = PinEntry("username", "", "", s.override, touched_at=now, error=text.WRONG_PIN)
      elif s.override is not None:
        if self._clock.override_manager(employee, s.override.employee_id):
          self._screen = await self._granted(s.override, OverrideGrant(employee.id, s.override.reason, pin_only=True))
        else:
          self._screen = OverridePrompt(s.override, touched_at=now, message=text.NOT_A_MANAGER)
      elif await self._clock.has_fingerprint(employee.id):
        self._screen = PinScan(employee.id, touched_at=now)
      else:
        self._screen = RegisterPrompt(employee.id, touched_at=now)

  async def punch(self, kind: PunchKind) -> None:
    """A punch button on the menu: recorded within 30 s of the scan, else it asks for a new scan (R45.2)."""
    async with self._transition():
      s = self._screen
      if not isinstance(s, Menu):
        return
      now = self._mono()
      if now - s.opened_at > RESCAN_AFTER_SECS:
        self._screen = Rescan(s.info.employee_id, kind, s.auth, touched_at=now)
      else:
        self._screen = await self._record(s.info.employee_id, kind, s.auth)

  async def register(self) -> None:
    """Start enrolling the employee's finger (R23.4)."""
    async with self._transition():
      s = self._screen
      if isinstance(s, RegisterPrompt):
        self._screen = Registering(s.employee_id, 0, 5, touched_at=self._mono())

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
        self._screen = OverridePrompt(OverrideRequest(s.employee_id, OverrideReason.NO_FINGERPRINT, None), touched_at=now)

  async def cancel(self) -> None:
    """Back to idle from any screen."""
    async with self._transition():
      if self._mode is ReaderMode.ENROLL:
        self._mode = ReaderMode.READY
        self._reader.set_mode(ReaderMode.READY)
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
        self._screen = Idle()
