"""Every string the kiosk shows (spec §6.4), and the formats for times, hours and the database status."""

# Standard library imports
from datetime import timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

# Third party imports
from timeclock_schema.enums import PunchKind
from timeclock_schema.shifts import EmployeeState

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Mapping
  from datetime import datetime
  from zoneinfo import ZoneInfo

  # First party imports
  from timeclock_system.sync import DbStatus

IDLE_READY: Final = "Place your finger on the reader"
IDLE_ASLEEP: Final = "Tap to scan"
SCANNING: Final = "Scanning…"
NOT_RECOGNIZED: Final = "Not recognized: try again or use your PIN"
RETRY: Final = "Couldn't read that, try again"
USE_PIN: Final = "Use PIN"
ENTER_USERNAME: Final = "Enter your PIN username"
ENTER_PIN: Final = "Enter your PIN"
WRONG_PIN: Final = "Incorrect PIN"
SCAN_YOUR_FINGER: Final = "Scan your finger"
NOT_YOUR_FINGER: Final = "That fingerprint doesn't match. Try again."
REGISTER_PROMPT: Final = "Register your fingerprint"
REGISTER: Final = "Register"
REGISTERING: Final = "Press your finger on the reader ({done} of {total})"
REGISTRATION_TIMED_OUT: Final = "Registration timed out"
REGISTRATION_FAILED: Final = "Registration failed, try again"
RESCAN: Final = "Scan your finger to confirm"
MANAGER_PROMPT: Final = "Manager: scan your finger or enter your PIN"
NOT_A_MANAGER: Final = "A manager must authorize this"
MANAGER_OVERRIDE: Final = "Manager override"
CANCEL: Final = "Cancel"

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
