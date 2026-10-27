"""Business-hours lookup (spec §4.3, R40.3): decides whether the reader waits for a finger."""

# Standard library imports
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Iterable
  from datetime import datetime
  from zoneinfo import ZoneInfo

  # Third party imports
  from timeclock_schema.models import BusinessHours


def applicable_hours(rows: Iterable[BusinessHours]) -> tuple[BusinessHours, ...]:
  """Return the rows that apply to this clock.

  The single lookup that per-clock hours would change later (R40.3); today it returns every row.

  Args:
    rows: All `config.business_hours` rows.

  Returns:
    The applicable rows.
  """
  return tuple(rows)


def is_open(rows: Iterable[BusinessHours], at: datetime, tz: ZoneInfo) -> bool:
  """Whether `at` falls inside an applicable window, evaluated in local time `tz`.

  Args:
    rows: All `config.business_hours` rows.
    at: An aware instant.
    tz: The business's zone (aeth_ext's `settings.tz`).

  Returns:
    True when a row matches the local ISO weekday with `opens <= time < closes`.

  Raises:
    ValueError: If `at` is naive.
  """
  if at.tzinfo is None:
    raise ValueError("`at` must be timezone-aware")
  local = at.astimezone(tz)
  return any(r.weekday == local.isoweekday() and r.opens <= local.time() < r.closes for r in applicable_hours(rows))
