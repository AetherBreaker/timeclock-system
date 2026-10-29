"""Every string the kiosk shows (spec §6.4), and the formats for times, hours and the database status."""

# Standard library imports
from datetime import timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

# Third party imports
from timeclock_schema.enums import Finger, PunchKind
from timeclock_schema.shifts import EmployeeState

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Mapping
  from datetime import datetime
  from zoneinfo import ZoneInfo

  # Third party imports
  from timeclock_schema.alerts import Firing

  # First party imports
  from timeclock_system.sync import DbStatus

IDLE_READY: Final = "Place your finger on the reader"
IDLE_ASLEEP: Final = "Tap to scan"
READER_ASLEEP: Final = "Tap to wake the reader"
NOT_RECOGNIZED: Final = "Not recognized, try again"
RETRY: Final = "Couldn't read that, try again"
READER_READY: Final = "Ready"
READER_SCANNING: Final = "Keep your finger down"
READER_LIFT: Final = "Lift your finger"
READER_PAUSED: Final = "Reader paused"
SCAN_SUCCESSFUL: Final = "Scan successful"
# The reader line's status, by the controller's ReaderIndicator value.
READER_STATUS: Final[Mapping[str, str]] = MappingProxyType(
  {
    "ready": READER_READY,
    "scanning": READER_SCANNING,
    "read": READER_LIFT,
    "unrecognized": READER_LIFT,
    "failed": RETRY,
    "paused": READER_PAUSED,
    "busy": "",
    "asleep": READER_ASLEEP,
  }
)
USE_PIN: Final = "Use PIN"
ENTER_USERNAME: Final = "Enter your PIN username"
ENTER_PIN: Final = "Enter your PIN"
WRONG_PIN: Final = "Incorrect PIN"
SCAN_YOUR_FINGER: Final = "Scan your finger"
NOT_YOUR_FINGER: Final = "That fingerprint doesn't match. Try again."
REGISTER_PROMPT: Final = "Register your fingerprint"
REGISTER: Final = "Register"
REGISTERING: Final = "Press your {finger} on the reader ({done} of {total})"  # the finger label, lower-cased
REGISTRATION_TIMED_OUT: Final = "Registration timed out"
REGISTRATION_FAILED: Final = "Registration failed, try again"
RESCAN: Final = "Scan your finger to confirm"
MANAGER_PROMPT: Final = "Manager: scan your finger or enter your PIN"
NOT_A_MANAGER: Final = "A manager must authorize this"
NOT_AN_ADMIN: Final = "An admin must approve this"
FINGERPRINT_SAVED: Final = "Fingerprint saved"
FINGERPRINT_REMOVED: Final = "Fingerprint removed"
FINGERPRINTS: Final = "Fingerprints"
FINGERPRINT_ACTIONS: Final = "{name}'s fingerprints"
REREGISTER_FINGER: Final = "Re-register a finger"
ADD_FINGER: Final = "Add a finger"
REMOVE_FINGER: Final = "Remove a finger"
APPROVE_HOW: Final = "An admin scans their finger, or uses their PIN"
FINGER_LABELS: Final[Mapping[Finger, str]] = MappingProxyType(
  {
    Finger.LEFT_THUMB: "Left thumb",
    Finger.LEFT_INDEX: "Left index",
    Finger.LEFT_MIDDLE: "Left middle",
    Finger.LEFT_RING: "Left ring",
    Finger.LEFT_LITTLE: "Left little",
    Finger.RIGHT_THUMB: "Right thumb",
    Finger.RIGHT_INDEX: "Right index",
    Finger.RIGHT_MIDDLE: "Right middle",
    Finger.RIGHT_RING: "Right ring",
    Finger.RIGHT_LITTLE: "Right little",
  }
)
# By the controller's FingerAction; None is a first registration.
PICK_FINGER: Final[Mapping[str | None, str]] = MappingProxyType(
  {
    None: "Which finger will you register?",
    "add": "Which finger to add?",
    "replace": "Which finger to re-register?",
    "remove": "Which finger to remove?",
  }
)
APPROVE_ADD: Final = "Admin: approve adding a fingerprint for {name}"  # before the finger is picked
APPROVE_CHANGE: Final[Mapping[str, str]] = MappingProxyType(  # {finger}: the label, lower-cased
  {
    "replace": "Admin: approve re-registering {name}'s {finger} print",
    "remove": "Admin: approve removing {name}'s {finger} print",
  }
)
MANAGER_OVERRIDE: Final = "Manager override"
CANCEL: Final = "Cancel"
NOT_YOUR_PIN: Final = "That isn't your PIN. Try again."

ADMIN: Final = "Admin"
ADMIN_RECONFIRM: Final = "Admin: scan your finger or enter your PIN"
ADMIN_MENU: Final = "Admin menu"
ADD_EMPLOYEE: Final = "Add employee"
BACK: Final = "Back"
NEXT: Final = "Next"
SKIP: Final = "Skip"
OK: Final = "OK"
FIELD_FIRST_NAME: Final = "First name"
FIELD_MIDDLE_NAME: Final = "Middle name (optional)"
FIELD_LAST_NAME: Final = "Last name"
FIELD_PHONE: Final = "Phone (optional)"
FIELD_EMAIL: Final = "Email (optional)"
FIELD_ROLE: Final = "Role"
FIELD_GROUP: Final = "Group"
ROLE_LABELS: Final[Mapping[str, str]] = MappingProxyType({"employee": "Employee", "manager": "Manager"})
FIRST_NAME_REQUIRED: Final = "Enter a first name"
LAST_NAME_REQUIRED: Final = "Enter a last name"
PHONE_INVALID: Final = "Enter a 10-digit US phone number"
EMAIL_INVALID: Final = "Enter an email like name@example.com"
ROLE_INVALID: Final = "Choose Employee or Manager"
GROUP_INVALID: Final = "Choose a group"
NEW_PIN: Final = "Choose a PIN for {name}"
CONFIRM_NEW_PIN: Final = "Enter the PIN again"
PIN_MISMATCH: Final = "The PINs didn't match. Start again."
PIN_DIGITS_ONLY: Final = "A PIN is digits only"
BLOCK_FULL: Final = "This clock has no employee numbers left"
NEW_FINGERPRINT: Final = "Register {name}'s fingerprint?"
EMPLOYEE_ADDED: Final = "Added {name}"
EMPLOYEE_NUMBER: Final = "Employee number (PIN username): {number}"
WEEKLY_HOURS_NOTICE: Final = "You've worked over {limit} hours this week"
WEEKLY_HOURS_EARLY_NOTICE: Final = "You're within {lead} h of {limit} hours this week"

PUNCH_LABELS: Final[Mapping[PunchKind, str]] = MappingProxyType(
  {
    PunchKind.CLOCK_IN: "Clock in",
    PunchKind.CLOCK_OUT: "Clock out",
    PunchKind.BREAK_START: "Start break",
    PunchKind.BREAK_END: "End break",
    PunchKind.LUNCH_START: "Start lunch",
    PunchKind.LUNCH_END: "End lunch",
  }
)
CONFIRMATIONS: Final[Mapping[PunchKind, str]] = MappingProxyType(
  {
    PunchKind.CLOCK_IN: "Clocked in at {time}",
    PunchKind.CLOCK_OUT: "Clocked out at {time}",
    PunchKind.BREAK_START: "Break started at {time}",
    PunchKind.BREAK_END: "Break ended at {time}",
    PunchKind.LUNCH_START: "Lunch started at {time}",
    PunchKind.LUNCH_END: "Lunch ended at {time}",
  }
)
STATE_LABELS: Final[Mapping[EmployeeState, str]] = MappingProxyType(
  {
    EmployeeState.OFF: "Clocked out",
    EmployeeState.WORKING: "Clocked in since {time}",
    EmployeeState.ON_BREAK: "On break since {time}",
    EmployeeState.AT_LUNCH: "At lunch since {time}",
  }
)


def format_clock_time(at: datetime, tz: ZoneInfo) -> str:
  """`at` as local `"8:02 AM"` (built by hand: `%-I` fails on Windows)."""
  local = at.astimezone(tz)
  return f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"  # noqa: PLR2004


def format_hours(td: timedelta) -> str:
  """Hours worked as `"7.50 h"`, truncated to the hundredth (owner's rule) and exact: integer hundredths, no float."""
  return f"{Decimal(td // timedelta(seconds=36)).scaleb(-2):.2f} h"


def format_db_status(status: DbStatus, tz: ZoneInfo) -> str:
  """The idle screen's status line, e.g. `"Database connected · last contact 8:02 AM"`."""
  contact = "never" if status.last_contact is None else format_clock_time(status.last_contact, tz)
  return f"Database {'connected' if status.connected else 'offline'} · last contact {contact}"


def _hours_number(td: timedelta) -> str:
  # Whole hours plainly, otherwise truncated hundredths without trailing zeros: 40, 37.5, 0.25.
  return f"{Decimal(td // timedelta(seconds=36)).scaleb(-2).normalize():f}"


def alert_notice(firing: Firing) -> str:
  """The confirmation-screen line for an alert hook the punch reached (§6.11); the weekly-hours type is the only one."""
  limit = _hours_number(firing.limit)
  if firing.hook.lead:
    return WEEKLY_HOURS_EARLY_NOTICE.format(lead=_hours_number(firing.hook.lead), limit=limit)
  return WEEKLY_HOURS_NOTICE.format(limit=limit)
