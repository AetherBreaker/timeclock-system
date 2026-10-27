"""The clock's domain rules (spec §4.5, §4.6, §6.4-§6.6, §6.11).

Punches, PINs, fingerprint bypasses, registration, adding employees, the weekly alert, the gallery and the prune.

Sync store calls run in `asyncio.to_thread`. The small mirrored tables are cached in memory and reloaded synchronously
by `notify_changed`.
"""

# Standard library imports
import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, cast
from uuid import uuid4

# Third party imports
import bcrypt
from pydantic import SecretBytes, SecretStr
from timeclock_schema.alerts import AlertContext, Firing, fired_hooks
from timeclock_schema.enums import BypassReason, EmployeeRole, PunchKind, RuleAction
from timeclock_schema.models import BusinessHours, Employee, Fingerprint, FingerprintBypass, Group, Punch, RuleRow, Settings
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

  # First party imports
  from timeclock_system.store import LocalStore

ChangeTable = Literal["employees", "groups", "fingerprints", "punches", "punch_corrections", "rules", "business_hours", "settings"]

type Record = Punch | Fingerprint | Employee | FingerprintBypass
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
class OverrideGrant:
  """A manager's authorization to punch without a fingerprint match."""

  manager_id: int
  reason: BypassReason
  pin_only: bool


@dataclass(frozen=True)
class NewEmployee:
  """The add-employee form's details, already checked; `phone` is normalized."""

  name: str
  phone: str | None
  email: str | None
  role: EmployeeRole
  group_id: int


@dataclass(frozen=True)
class MenuInfo:
  """What the punch menu shows for one employee."""

  employee_id: int
  name: str
  state: EmployeeState
  since: datetime | None
  """The open shift's clock-in while working; the last applied event on a break or lunch; None when off."""
  valid_next: tuple[PunchKind, ...]
  hours_this_week: timedelta
  """Worked plus paid break time (§6.4)."""
  is_admin: bool


@dataclass(frozen=True)
class PunchOutcome:
  """A stored punch with the warnings its rules raised and the alert hooks it reached (§6.11)."""

  punch: Punch
  warnings: tuple[str, ...]
  hours_this_week: timedelta
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
    """Return the active employee with this PIN username if the password matches its bcrypt hash."""
    for e in self._employees.values():
      if e.active and e.pin_username == username:
        ok = await asyncio.to_thread(bcrypt.checkpw, pin.get_secret_value().encode(), e.pin_hash.get_secret_value().encode())
        return e if ok else None
    return None

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
      PunchRefused: The employee is unknown.
    """
    e = self._employees.get(employee_id)
    if e is None:
      raise PunchRefused(_UNKNOWN_EMPLOYEE)
    punches, corrections = await asyncio.to_thread(lambda: (self._store.punches(employee_id), self._store.corrections(employee_id)))
    now = self._now()
    # A punch counts from its stamped time: the automatic clock-out may be stamped after its run, and until then the employee
    # can still clock out by hand, which makes the automatic one the ignored repeat.
    timeline = build_timeline(punches, corrections, as_of=now)
    # "Clocked in since" names the clock-in, not a lunch that followed it; a break or lunch shows its own start.
    open_shift = timeline.open_shift
    if open_shift is None:
      since = None
    elif timeline.state is EmployeeState.WORKING:
      since = open_shift.clock_in.at
    else:
      since = timeline.last_event_at
    s = self._settings
    start, end = week_bounds(now, self._tz)
    totals = total_hours(punches, corrections, settings=s, tz=self._tz, start=start, end=end, now=now)
    # A switched-off lunch or break hides only its start: someone already on one can still end it.
    off = {k for k, on in ((PunchKind.LUNCH_START, s.lunches_enabled), (PunchKind.BREAK_START, s.breaks_enabled)) if not on}
    valid = tuple(k for k in timeline.valid_next if k not in off)
    hours = totals.worked + totals.paid_break
    return MenuInfo(employee_id, e.name, timeline.state, since, valid, hours, is_admin=e.role is EmployeeRole.ADMIN)

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
      PunchRefused: The employee is unknown or inactive, the punch isn't valid now, or a REFUSE rule fired.
    """
    e = self._employees.get(employee_id)
    if e is None or not e.active:
      raise PunchRefused(_UNKNOWN_EMPLOYEE)
    async with self._punch_lock:
      punches, corrections = await asyncio.to_thread(lambda: (self._store.punches(employee_id), self._store.corrections(employee_id)))
      now = self._now()
      timeline = build_timeline(punches, corrections, as_of=now)  # as in menu_info
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
      await asyncio.to_thread(self._store.insert_punch, punch, origin="local", bypass=bypass)
    for cb in self._record_listeners:
      cb(punch)
    self.notify_changed({"punches"})
    s, punches = self._settings, [*punches, punch]
    start, end = week_bounds(now, self._tz)
    totals = total_hours(punches, corrections, settings=s, tz=self._tz, start=start, end=end, now=now)
    # Alerts only inform; nothing here can refuse the punch (§2).
    firings = fired_hooks(AlertContext(employee_id, punches, corrections, s, self._tz, now))
    return PunchOutcome(punch, warnings, totals.worked + totals.paid_break, firings)

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
      name=new.name,
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

  async def register_fingerprint(self, employee_id: int, template: bytes) -> Fingerprint:
    """Store a newly enrolled print as a local, unsynced registration."""
    f = Fingerprint(
      id=uuid4(), employee_id=employee_id, template=SecretBytes(template), registered_at=self._now(), instance_id=self.instance_id
    )
    await asyncio.to_thread(self._store.insert_fingerprint, f, origin="local")
    for cb in self._record_listeners:
      cb(f)
    self.notify_changed({"fingerprints"})
    return f

  async def gallery(self) -> list[GalleryPrint]:
    """Return the active employees' prints, those who punched here most recently first (R36.4)."""
    fingerprints, punches = await asyncio.to_thread(lambda: (self._store.fingerprints(), self._store.punches()))
    latest: dict[int, datetime] = {}
    for p in punches:  # oldest first, so the last write per employee is the newest
      if p.instance_id == self.instance_id:
        latest[p.employee_id] = p.at
    active = [f for f in fingerprints if (e := self._employees.get(f.employee_id)) is not None and e.active]
    active.sort(
      key=lambda f: (f.employee_id not in latest, -latest[f.employee_id].timestamp() if f.employee_id in latest else 0.0, str(f.id))
    )
    return [GalleryPrint(f.id, f.employee_id, f.template.get_secret_value()) for f in active]

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
