"""The clock's domain rules (spec §4.5, §4.6, §6.4-§6.6, §6.11).

Punches, PINs, fingerprint bypasses, registration, adding employees, the weekly alert, the gallery and the prune.

Sync store calls run in `asyncio.to_thread`. The small mirrored tables are cached in memory and reloaded synchronously
by `notify_changed`.
"""

# Standard library imports
import asyncio
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Literal, cast
from uuid import uuid4

# Third party imports
import bcrypt
from pydantic import SecretBytes, SecretStr
from timeclock_schema.alerts import AlertContext, Firing, fired_hooks
from timeclock_schema.enums import (
  BypassReason,
  EmployeeRole,
  Finger,
  FingerprintChangeAction,
  PunchKind,
  RuleAction,
  ScanResult,
)
from timeclock_schema.models import (
  BusinessHours,
  Employee,
  Fingerprint,
  FingerprintBypass,
  FingerprintChange,
  FingerprintScan,
  Group,
  Punch,
  RuleRow,
  Settings,
)
from timeclock_schema.rules import RuleContext, RuleValidationError, ValidatedRule, applicable_rules, evaluate, validate_rule
from timeclock_schema.shifts import EmployeeState, apply_corrections, build_timeline, total_hours, week_bounds

# First party imports
from timeclock_system.hours import applicable_hours
from timeclock_system.scanner import GalleryPrint

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Iterable
  from uuid import UUID
  from zoneinfo import ZoneInfo

  # Third party imports
  from timeclock_schema.enums import ScanOutcome
  from timeclock_schema.models import PunchCorrection

  # First party imports
  from timeclock_system.scanner import ScanReport
  from timeclock_system.store import LocalStore

logger = logging.getLogger(__name__)

ChangeTable = Literal["employees", "groups", "fingerprints", "punches", "punch_corrections", "rules", "business_hours", "settings"]

type Record = Punch | Fingerprint | Employee | FingerprintBypass | FingerprintChange
"""What the record listeners hear about: everything this clock creates."""

_UNKNOWN_EMPLOYEE = "Unknown employee"
_BLOCK = 10000  # clock n hands out n*10000+1 .. n*10000+9999 (§6.4)
_PHONE_PUNCTUATION = re.compile(r"[\s().-]")


def normalize_phone(raw: str) -> str:
  """Return a US phone number typed in any common format as `+1` and ten digits, the one stored format (§4.2).

  Accepts e.g. `(555) 123-4567`, `555-123-4567`, `555.123.4567`, `5551234567`, `1 555 123 4567` and `+1 555 123 4567`.

  Raises:
    ValueError: It isn't a ten-digit US number.
  """
  digits = _PHONE_PUNCTUATION.sub("", raw)
  digits = digits.removeprefix("+1") if digits.startswith("+") else digits
  if len(digits) == 11 and digits.startswith("1"):  # noqa: PLR2004  # the country code without its plus
    digits = digits[1:]
  if not (len(digits) == 10 and digits.isascii() and digits.isdecimal()):  # noqa: PLR2004
    raise ValueError("not a ten-digit US phone number")
  return f"+1{digits}"


def check_new_pin(pin: SecretStr) -> bool:
  """Whether `pin` is acceptable for a new employee: digits only, any length. The one place a length rule would go."""
  value = pin.get_secret_value()
  return value.isascii() and value.isdecimal()


@dataclass(frozen=True)
class Approval:
  """An admin's approval of one fingerprint change: by their finger, or `pin_only`."""

  admin_id: int
  pin_only: bool


@dataclass(frozen=True)
class OverrideGrant:
  """A manager's authorization to punch without a fingerprint match."""

  manager_id: int
  reason: BypassReason
  pin_only: bool


@dataclass(frozen=True)
class NewEmployee:
  """The add-employee form's details, already checked; `phone` is normalized."""

  first_name: str
  middle_name: str | None
  last_name: str
  phone: str | None
  email: str | None
  role: EmployeeRole
  group_id: int

  @property
  def display_name(self) -> str:
    """First and last name, as `Employee.display_name` gives it."""
    return f"{self.first_name} {self.last_name}"


@dataclass(frozen=True)
class HoursSummary:
  """The hours the punch menu and the confirmation show (§6.4).

  Today is the local calendar day. Its breaks and lunches are those that started today, at full length; one under way
  runs to now.
  """

  week: timedelta
  """Worked plus paid break time, this week."""
  today: timedelta
  """Worked plus paid break time, today."""
  breaks: timedelta | None
  """All of today's break time, paid or not; None while breaks are switched off and none was taken today."""
  last_break: timedelta
  """Today's latest break (the one under way when `on_break`); zero before the first."""
  on_break: bool
  lunch: timedelta | None
  """All of today's lunch time; None as for `breaks`."""


@dataclass(frozen=True)
class MenuInfo:
  """What the punch menu shows for one employee."""

  employee_id: int
  name: str
  state: EmployeeState
  since: datetime | None
  """The open shift's clock-in while working; the last applied event on a break or lunch; None when off."""
  valid_next: tuple[PunchKind, ...]
  hours: HoursSummary
  is_admin: bool


@dataclass(frozen=True)
class PunchOutcome:
  """A stored punch with the warnings its rules raised and the alert hooks it reached (§6.11)."""

  punch: Punch
  warnings: tuple[str, ...]
  hours: HoursSummary
  firings: tuple[Firing, ...]


class EmployeeBlockFull(Exception):  # noqa: N818  # a domain signal shown to the admin, like PunchRefused
  """This clock has handed out every number in its block."""


class PunchRefused(Exception):  # noqa: N818  # a domain signal shown to the employee, not an error class
  """A punch the clock won't record; `message` is shown to the employee."""

  def __init__(self, message: str) -> None:
    """Record the message shown on screen."""
    super().__init__(message)
    self.message = message


class Clock:
  """The clock's domain service over a `LocalStore`."""

  def __init__(
    self,
    store: LocalStore,
    *,
    instance_id: str,
    clock_number: int,
    tz: ZoneInfo,
    alert: Callable[[str, str], None],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
  ) -> None:
    """Create the service; call `load` before use.

    Args:
      store: The local SQLite store.
      instance_id: This clock's id, stamped on its punches and registrations.
      clock_number: `CLOCK_NUMBER`, which picks this clock's block of employee numbers.
      tz: The business's zone, for week boundaries.
      alert: Reports a problem to the owner as `(subject, body)`.
      now: The wall clock, injectable for tests.
    """
    self._store = store
    self.instance_id = instance_id
    self._clock_number = clock_number
    self._tz = tz
    self._alert = alert
    self._now = now
    self._employees: dict[int, Employee] = {}
    self._pending: frozenset[int] = frozenset()  # employees added here that the database hasn't accepted
    self._known_numbers: frozenset[int] = frozenset()  # every id and numeric username known, mirror and pending
    self._rules: tuple[ValidatedRule, ...] = ()
    self._groups: dict[int, Group] = {}
    self._hours: tuple[BusinessHours, ...] = ()
    self._settings = Settings()
    self._alerted_rules: set[tuple[int, datetime]] = set()
    self._change_listeners: list[Callable[[frozenset[ChangeTable]], None]] = []
    self._record_listeners: list[Callable[[Record], None]] = []
    self._scan_listeners: list[Callable[[], object]] = []
    # Serializes record_punch's guard-then-insert so two taps can't both pass the same state check.
    self._punch_lock = asyncio.Lock()

  # -- caches -------------------------------------------------------------------------------------------------------

  def _reload(self, tables: Iterable[ChangeTable]) -> None:
    tables = set(tables)
    # `load` is typed to the base model; each table's rows are its own model.
    if "employees" in tables:
      mirror = cast("list[Employee]", self._store.load("employees"))
      pending = self._store.pending_employees()
      # A pending employee keeps its number and username on this clock (§6.6), so a mirror row sharing either is hidden.
      taken = {e.id for e in pending} | {int(e.pin_username) for e in pending}
      self._employees = {e.id: e for e in mirror if e.id not in taken and int(e.pin_username) not in taken}
      self._employees.update({e.id: e for e in pending})
      self._pending = frozenset(e.id for e in pending)
      self._known_numbers = frozenset({e.id for e in [*mirror, *pending]} | {int(e.pin_username) for e in [*mirror, *pending]})
    if "groups" in tables:
      self._groups = {g.id: g for g in cast("list[Group]", self._store.load("groups"))}
    if "business_hours" in tables:
      self._hours = applicable_hours(cast("list[BusinessHours]", self._store.load("business_hours")))
    if "settings" in tables:
      self._settings = self._store.get_settings()
    if "rules" in tables:
      valid: list[ValidatedRule] = []
      for row in cast("list[RuleRow]", self._store.load("rules")):
        try:
          valid.append(validate_rule(row))
        except RuleValidationError as err:
          # Every notification reloads the rules; alert once per version of a bad row.
          if (row.id, row.updated_at) not in self._alerted_rules:
            self._alerted_rules.add((row.id, row.updated_at))
            self._alert(f"Bad rule row {row.id}", str(err))
      self._rules = tuple(valid)

  async def load(self) -> None:
    """Cache employees, groups, rules, business hours and settings from the store."""
    await asyncio.to_thread(self._reload, ("employees", "groups", "rules", "business_hours", "settings"))
    try:
      await asyncio.to_thread(self._store.settle_open_scans)  # a matched scan's outcome died with the last run
    except Exception:
      logger.exception("could not settle the previous run's open fingerprint scans")  # the log must not block startup

  def notify_changed(self, tables: Iterable[ChangeTable]) -> None:
    """Reload the caches of the changed tables, then tell every change listener."""
    changed = frozenset(tables)
    self._reload(changed)
    for cb in self._change_listeners:
      cb(changed)

  def add_change_listener(self, cb: Callable[[frozenset[ChangeTable]], None]) -> None:
    """Call `cb` with the changed tables after each `notify_changed`."""
    self._change_listeners.append(cb)

  def add_record_listener(self, cb: Callable[[Record], None]) -> None:
    """Call `cb` on the loop after every record this clock creates is stored (sync's wake-up; LAN sharing's seam)."""
    self._record_listeners.append(cb)

  def add_scan_listener(self, cb: Callable[[], object]) -> None:
    """Call `cb` on the loop whenever a scan becomes settled (sync's wake-up: scans aren't records)."""
    self._scan_listeners.append(cb)

  async def insert_peer_record(self, record: Punch | Fingerprint) -> bool:
    """LAN seam: store a record received from a peer (origin `peer`, so never pushed).

    Returns:
      False if the id was already known.
    """
    if isinstance(record, Punch):
      is_new = await asyncio.to_thread(self._store.insert_punch, record, origin="peer")
    else:
      is_new = await asyncio.to_thread(self._store.insert_fingerprint, record, origin="peer")
    if is_new:
      self.notify_changed({"punches" if isinstance(record, Punch) else "fingerprints"})
    return is_new

  # -- lookups ------------------------------------------------------------------------------------------------------

  def employee(self, employee_id: int) -> Employee | None:
    """Return the employee, active or not."""
    return self._employees.get(employee_id)

  def business_hours(self) -> tuple[BusinessHours, ...]:
    """Return the business-hours rows that apply to this clock."""
    return applicable_hours(self._hours)

  def settings(self) -> Settings:
    """Return the cached settings."""
    return self._settings

  def groups(self) -> tuple[Group, ...]:
    """Return the mirrored groups, by id."""
    return tuple(sorted(self._groups.values(), key=lambda g: g.id))

  def pin_only_login(self, e: Employee) -> bool:
    """Whether `e` logs in by PIN alone, with no fingerprint step and no override: admins and exempt employees (§6.4)."""
    return e.role is EmployeeRole.ADMIN or e.fingerprint_exempt

  async def verify_pin(self, username: str, pin: SecretStr) -> Employee | None:
    """Return the employee with this PIN username if the password matches its bcrypt hash.

    An inactive employee passes only while their shift is open (see `menu_info`); checked before the hash, so a shut-out
    employee costs no bcrypt.
    """
    e = next((e for e in self._employees.values() if e.pin_username == username), None)
    if e is None:
      return None
    if not e.active:
      punches, corrections = await asyncio.to_thread(lambda: (self._store.punches(e.id), self._store.corrections(e.id)))
      if build_timeline(punches, corrections, as_of=self._now()).open_shift is None:
        return None
    ok = await asyncio.to_thread(bcrypt.checkpw, pin.get_secret_value().encode(), e.pin_hash.get_secret_value().encode())
    return e if ok else None

  async def has_fingerprint(self, employee_id: int) -> bool:
    """Whether any print is stored for the employee."""
    return any(f.employee_id == employee_id for f in await asyncio.to_thread(self._store.fingerprints))

  def override_manager(self, manager: Employee, employee_id: int) -> bool:
    """Whether `manager` may authorize a punch for `employee_id`.

    Nobody overrides for themselves, and a manager added here can't until the database has accepted them (§6.4).
    """
    return (
      manager.active
      and manager.role in {EmployeeRole.MANAGER, EmployeeRole.ADMIN}
      and manager.id != employee_id
      and manager.id not in self._pending
    )

  def fingerprint_approver(self, e: Employee) -> bool:
    """Whether `e` may approve a fingerprint change: an active admin, themselves included, once the database accepted them."""
    return e.active and e.role is EmployeeRole.ADMIN and e.id not in self._pending

  async def prints_by_finger(self, employee_id: int) -> dict[Finger, UUID]:
    """Return the employee's prints on file here, by finger."""
    return {f.finger: f.id for f in await asyncio.to_thread(self._store.fingerprints) if f.employee_id == employee_id}

  def _login_bypass(self, e: Employee, punch_id: UUID | None) -> FingerprintBypass:
    reason = BypassReason.ADMIN_PIN_LOGIN if e.role is EmployeeRole.ADMIN else BypassReason.EXEMPT_PIN_LOGIN
    return FingerprintBypass(
      id=uuid4(), punch_id=punch_id, employee_id=e.id, reason=reason, at=self._now(), instance_id=self.instance_id
    )

  async def record_pin_login(self, e: Employee) -> None:
    """Record a PIN-only login (an admin's or an exempt employee's) that punched nothing, for later reports (§4.2)."""
    bypass = self._login_bypass(e, None)
    await asyncio.to_thread(self._store.insert_bypass, bypass)
    for cb in self._record_listeners:
      cb(bypass)

  # -- punches ------------------------------------------------------------------------------------------------------

  async def menu_info(self, employee_id: int) -> MenuInfo:
    """Return the punch menu's data for an employee.

    Raises:
      PunchRefused: The employee is unknown, or inactive with no open shift.
    """
    e = self._employees.get(employee_id)
    if e is None:
      raise PunchRefused(_UNKNOWN_EMPLOYEE)
    punches, corrections = await asyncio.to_thread(lambda: (self._store.punches(employee_id), self._store.corrections(employee_id)))
    now = self._now()
    # A punch counts from its stamped time: the automatic clock-out may be stamped after its run, and until then the employee
    # can still clock out by hand, which makes the automatic one the ignored repeat.
    timeline = build_timeline(punches, corrections, as_of=now)
    if not e.active and timeline.open_shift is None:
      raise PunchRefused(_UNKNOWN_EMPLOYEE)  # inactive: may finish an open shift, never clock in (owner, 2026-11-05)
    # "Clocked in since" names the clock-in, not a lunch that followed it; a break or lunch shows its own start.
    open_shift = timeline.open_shift
    if open_shift is None:
      since = None
    elif timeline.state is EmployeeState.WORKING:
      since = open_shift.clock_in.at
    else:
      since = timeline.last_event_at
    s = self._settings
    # A switched-off lunch or break hides only its start: someone already on one can still end it.
    off = {k for k, on in ((PunchKind.LUNCH_START, s.lunches_enabled), (PunchKind.BREAK_START, s.breaks_enabled)) if not on}
    valid = tuple(k for k in timeline.valid_next if k not in off)
    hours = self._hours_summary(punches, corrections, now)
    is_admin = e.active and e.role is EmployeeRole.ADMIN
    return MenuInfo(employee_id, e.display_name, timeline.state, since, valid, hours, is_admin=is_admin)

  def _hours_summary(self, punches: list[Punch], corrections: list[PunchCorrection], now: datetime) -> HoursSummary:
    """Total an employee's punches for the punch menu and the confirmation; `total_hours` keeps every pay rule."""
    s, tz = self._settings, self._tz
    week_start, week_end = week_bounds(now, tz)
    week = total_hours(punches, corrections, settings=s, tz=tz, start=week_start, end=week_end, now=now)
    local = now.astimezone(tz).date()
    day_start = datetime.combine(local, time(0), tzinfo=tz)
    day_end = datetime.combine(local + timedelta(days=1), time(0), tzinfo=tz)
    day = total_hours(punches, corrections, settings=s, tz=tz, start=day_start, end=day_end, now=now)
    timeline = build_timeline(punches, corrections, as_of=now)
    breaks: list[timedelta] = []
    lunches: list[timedelta] = []
    for shift in timeline.shifts:
      for spans, lengths in ((shift.breaks, breaks), (shift.lunches, lunches)):
        lengths.extend((now if i.end is None else i.end) - i.start for i in spans if i.start >= day_start)
    return HoursSummary(
      week=week.worked + week.paid_break,
      today=day.worked + day.paid_break,
      breaks=sum(breaks, timedelta()) if s.breaks_enabled or breaks else None,
      last_break=breaks[-1] if breaks else timedelta(),
      on_break=timeline.state is EmployeeState.ON_BREAK,
      lunch=sum(lunches, timedelta()) if s.lunches_enabled or lunches else None,
    )

  async def record_punch(
    self, employee_id: int, kind: PunchKind, *, override: OverrideGrant | None = None, pin_login: bool = False
  ) -> PunchOutcome:
    """Validate and store a punch, with the bypass that authorized it if it wasn't a fingerprint.

    Args:
      employee_id: Who punches.
      kind: The punch.
      override: A manager's grant, stored as an override bypass.
      pin_login: The employee's own PIN authorized it (an admin's or exempt employee's 30 s rescan), stored as a login bypass.

    Raises:
      PunchRefused: The employee is unknown, or inactive and clocking in; the punch isn't valid now; or a REFUSE rule fired.
    """
    e = self._employees.get(employee_id)
    if e is None:
      raise PunchRefused(_UNKNOWN_EMPLOYEE)
    async with self._punch_lock:
      punches, corrections = await asyncio.to_thread(lambda: (self._store.punches(employee_id), self._store.corrections(employee_id)))
      now = self._now()
      timeline = build_timeline(punches, corrections, as_of=now)  # as in menu_info
      # Inactive (see menu_info): only the off state offers a clock-in, so this shuts them out once clocked out.
      if not e.active and kind is PunchKind.CLOCK_IN:
        raise PunchRefused(_UNKNOWN_EMPLOYEE)
      # The menu only offers valid punches (R26.3); re-checked here since state may have changed since it was drawn.
      s = self._settings
      off = {k for k, on in ((PunchKind.LUNCH_START, s.lunches_enabled), (PunchKind.BREAK_START, s.breaks_enabled)) if not on}
      if kind not in timeline.valid_next or kind in off:
        raise PunchRefused("That punch isn't available right now")
      rules = applicable_rules(self._rules, employee_id=employee_id, group_id=e.group_id)
      violations = evaluate(rules, RuleContext(employee_id=employee_id, group_id=e.group_id, timeline=timeline, proposed=kind, at=now))
      refusals = [v.message for v in violations if v.action is RuleAction.REFUSE]
      if refusals:
        raise PunchRefused("; ".join(refusals))
      warnings = tuple(v.message for v in violations if v.action is RuleAction.WARN)
      punch = Punch(id=uuid4(), employee_id=employee_id, kind=kind, at=now, instance_id=self.instance_id)
      # A clock-out from a break or lunch also writes its end, a microsecond earlier, so the punch log reads plainly
      # (owner, 2026-11-04); the hours rules still end it at a clock-out, for punches recorded before this.
      end_kind = {EmployeeState.ON_BREAK: PunchKind.BREAK_END, EmployeeState.AT_LUNCH: PunchKind.LUNCH_END}.get(timeline.state)
      ending = None
      if kind is PunchKind.CLOCK_OUT and end_kind is not None:
        ending = Punch(
          id=uuid4(), employee_id=employee_id, kind=end_kind, at=now - timedelta(microseconds=1), instance_id=self.instance_id
        )
      if override is not None:
        bypass = FingerprintBypass(
          id=uuid4(),
          punch_id=punch.id,
          employee_id=employee_id,
          manager_id=override.manager_id,
          reason=override.reason,
          pin_only=override.pin_only,
          at=now,
          instance_id=self.instance_id,
        )
      else:
        bypass = self._login_bypass(e, punch.id) if pin_login else None
      await asyncio.to_thread(self._store.insert_punch, punch, origin="local", bypass=bypass, before=ending)
    written = [punch] if ending is None else [ending, punch]
    for cb in self._record_listeners:
      for p in written:
        cb(p)
    self.notify_changed({"punches"})
    punches = [*punches, *written]
    # Alerts only inform; nothing here can refuse the punch (§2).
    firings = fired_hooks(AlertContext(employee_id, punches, corrections, self._settings, self._tz, now))
    return PunchOutcome(punch, warnings, self._hours_summary(punches, corrections, now), firings)

  async def record_scan(self, scan: ScanReport) -> UUID | None:
    """Store a scan in the log; a matched one stays open until `settle_scan`. Never raises: the log must not stop a punch.

    Returns:
      The new row's id, or None if it couldn't be stored.
    """
    try:
      best, other = scan.best, scan.other
      image = scan.image if self._settings.save_scan_images else None
      row = FingerprintScan(
        id=uuid4(),
        at=self._now(),
        instance_id=self.instance_id,
        result=scan.result,
        best_fingerprint_id=best and best.print_id,
        best_employee_id=best and best.employee_id,
        best_finger=best and best.finger,
        best_score=best and best.score,
        other_fingerprint_id=other and other.print_id,
        other_employee_id=other and other.employee_id,
        other_finger=other and other.finger,
        other_score=other and other.score,
        scores=dict(scan.scores),
        margin=scan.margin,
        retry_message=scan.retry_message,
        probe=None if scan.probe is None else SecretBytes(scan.probe),
        image=None if image is None else SecretBytes(image.data),
        image_width=image and image.width,
        image_height=image and image.height,
      )
      await asyncio.to_thread(self._store.insert_scan, row, settled=scan.result is not ScanResult.MATCHED)
    except Exception:
      logger.exception("could not log a fingerprint scan")  # no bytes in the message: the scan is biometric
      return None
    if scan.result is not ScanResult.MATCHED:
      for cb in self._scan_listeners:
        cb()
    return row.id

  async def settle_scan(self, scan_id: UUID, outcome: ScanOutcome, punch_id: UUID | None = None) -> None:
    """Record what became of a matched scan, releasing it to the push. Never raises, like `record_scan`."""
    try:
      await asyncio.to_thread(self._store.settle_scan, scan_id, outcome, punch_id)
    except Exception:
      logger.exception("could not settle fingerprint scan %s", scan_id)
      return
    for cb in self._scan_listeners:
      cb()

  async def add_employee(self, new: NewEmployee, pin: SecretStr) -> Employee:
    """Create an employee at this clock, saved locally and pushed when the database is reachable (§6.4, §6.6).

    The number is one more than the highest this clock knows in its block, skipping any already used as a username; the
    username is the number written as digits.

    Raises:
      EmployeeBlockFull: Every number in this clock's block is taken.
      ValueError: The role is admin (admins are never created from a clock), or the PIN isn't acceptable.
    """
    if new.role not in {EmployeeRole.EMPLOYEE, EmployeeRole.MANAGER}:
      raise ValueError("a clock adds employees and managers only")
    if not check_new_pin(pin):
      raise ValueError("the PIN must be digits only")
    base = self._clock_number * _BLOCK
    number = max((n for n in self._known_numbers if base < n < base + _BLOCK), default=base) + 1
    while number in self._known_numbers:
      number += 1
    if number >= base + _BLOCK:
      raise EmployeeBlockFull
    # $2a$, like pgcrypto's gen_salt('bf'): pgcrypto's crypt() can't check bcrypt's default $2b$, and the owner may check a
    # PIN in SQL.
    pin_hash = await asyncio.to_thread(lambda: bcrypt.hashpw(pin.get_secret_value().encode(), bcrypt.gensalt(prefix=b"2a")).decode())
    e = Employee(
      id=number,
      first_name=new.first_name,
      middle_name=new.middle_name,
      last_name=new.last_name,
      pin_username=str(number),
      pin_hash=SecretStr(pin_hash),
      role=new.role,
      group_id=new.group_id,
      phone=new.phone,
      email=new.email,
      updated_at=self._now(),
    )
    await asyncio.to_thread(self._store.insert_pending_employee, e)
    self.notify_changed({"employees"})
    for cb in self._record_listeners:
      cb(e)
    return e

  async def register_fingerprint(
    self, employee_id: int, template: bytes, finger: Finger, *, approval: Approval | None = None, replaces: UUID | None = None
  ) -> Fingerprint:
    """Store a newly enrolled print as a local, unsynced registration, and log it.

    `replaces` (a re-register) is removed with it; only a first registration goes without `approval`.
    """
    now = self._now()
    f = Fingerprint(
      id=uuid4(),
      employee_id=employee_id,
      finger=finger,
      template=SecretBytes(template),
      registered_at=now,
      instance_id=self.instance_id,
    )
    action = FingerprintChangeAction.ADD if replaces is None else FingerprintChangeAction.REPLACE
    change = self._fingerprint_change(employee_id, finger, action, approval, now)
    await asyncio.to_thread(
      lambda: (self._store.insert_fingerprint(f, origin="local", replaces=replaces), self._store.insert_fingerprint_change(change))
    )
    for cb in self._record_listeners:
      cb(f)
    self.notify_changed({"fingerprints"})
    return f

  async def remove_fingerprint(self, employee_id: int, fingerprint_id: UUID, finger: Finger, approval: Approval) -> None:
    """Remove a print here at once (the database follows on the next push), and log it."""
    change = self._fingerprint_change(employee_id, finger, FingerprintChangeAction.REMOVE, approval, self._now())
    await asyncio.to_thread(lambda: (self._store.remove_fingerprint(fingerprint_id), self._store.insert_fingerprint_change(change)))
    for cb in self._record_listeners:
      cb(change)
    self.notify_changed({"fingerprints"})

  def _fingerprint_change(
    self, employee_id: int, finger: Finger, action: FingerprintChangeAction, approval: Approval | None, at: datetime
  ) -> FingerprintChange:
    return FingerprintChange(
      id=uuid4(),
      employee_id=employee_id,
      finger=finger,
      action=action,
      approved_by=None if approval is None else approval.admin_id,
      pin_only=None if approval is None else approval.pin_only,
      at=at,
      instance_id=self.instance_id,
    )

  async def gallery(self) -> list[GalleryPrint]:
    """Return the prints of active employees and of inactive ones on an open shift, latest punch here first (R36.4).

    An inactive employee's prints stay until their shift ends (see `menu_info`).
    """
    fingerprints, punches, corrections = await asyncio.to_thread(
      lambda: (self._store.fingerprints(), self._store.punches(), self._store.corrections())
    )
    latest: dict[int, datetime] = {}
    by_employee: dict[int, list[Punch]] = defaultdict(list)
    for p in punches:  # oldest first, so the last write per employee is the newest
      by_employee[p.employee_id].append(p)
      if p.instance_id == self.instance_id:
        latest[p.employee_id] = p.at
    now = self._now()
    finishing = {
      employee_id
      for employee_id, employee_punches in by_employee.items()
      if (e := self._employees.get(employee_id)) is not None
      and not e.active
      and build_timeline(employee_punches, corrections, as_of=now).open_shift is not None
    }
    shown = [f for f in fingerprints if (e := self._employees.get(f.employee_id)) is not None and (e.active or e.id in finishing)]
    shown.sort(
      key=lambda f: (f.employee_id not in latest, -latest[f.employee_id].timestamp() if f.employee_id in latest else 0.0, str(f.id))
    )
    return [GalleryPrint(f.id, f.employee_id, f.template.get_secret_value(), f.finger) for f in shown]

  # -- weekly prune -------------------------------------------------------------------------------------------------

  async def _open_clock_ins(self) -> dict[int, datetime]:
    """Per employee with an open shift, the earliest *stored* `at` among that shift's punches.

    Prune and the catch-up window compare stored times, so a correction moving the clock-in later must not make its
    stored row look older than the bound (§6.5 keeps the whole open shift regardless of corrections). The shift's punches
    are the clock-in plus every punch whose corrected time isn't before the corrected clock-in.
    """
    punches, corrections = await asyncio.to_thread(lambda: (self._store.punches(), self._store.corrections()))
    by_employee: dict[int, list[Punch]] = defaultdict(list)
    for p in punches:
      by_employee[p.employee_id].append(p)
    open_clock_ins: dict[int, datetime] = {}
    for employee_id, employee_punches in by_employee.items():
      open_shift = build_timeline(employee_punches, corrections).open_shift
      if open_shift is not None:
        stored_at = {p.id: p.at for p in employee_punches}
        open_clock_ins[employee_id] = min(
          stored_at[e.id] for e in apply_corrections(employee_punches, corrections) if e.at >= open_shift.clock_in.at
        )
    return open_clock_ins

  async def catch_up_since(self) -> datetime:
    """Return how far back sync must fetch: this week's start, or an older open shift's clock-in."""
    week_start = week_bounds(self._now(), self._tz)[0]
    return min([week_start, *(await self._open_clock_ins()).values()])

  async def prune_week(self) -> int:
    """Delete synced punches older than this week, keeping an open shift's clock-in onward (R41).

    Returns:
      The number of rows deleted.
    """
    keep_from = await self._open_clock_ins()
    return await asyncio.to_thread(self._store.prune, week_bounds(self._now(), self._tz)[0], keep_from)

  async def run_week_rollover(self) -> None:
    """Prune now and again just after every Monday 00:00 local, forever."""
    await self.prune_week()
    while True:
      now = self._now()
      await asyncio.sleep((week_bounds(now, self._tz)[1] + timedelta(seconds=1) - now).total_seconds())
      await self.prune_week()
