"""Tests for the business-hours lookup."""

# Standard library imports
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

# Third party imports
import pytest
from timeclock_schema.models import BusinessHours

# First party imports
# Local imports
from timeclock_system.hours import is_open

NY = ZoneInfo("America/New_York")


def _row(weekday: int, opens: time, closes: time) -> BusinessHours:
  return BusinessHours(id=weekday, weekday=weekday, opens=opens, closes=closes)


def test_open_inside_a_window() -> None:
  rows = [_row(1, time(8), time(17))]
  assert is_open(rows, datetime(2026, 10, 26, 12, 0, tzinfo=NY), NY)


def test_closes_is_exclusive_and_opens_inclusive() -> None:
  rows = [_row(1, time(8), time(17))]
  assert is_open(rows, datetime(2026, 10, 26, 8, 0, tzinfo=NY), NY)
  assert not is_open(rows, datetime(2026, 10, 26, 17, 0, tzinfo=NY), NY)
  assert not is_open(rows, datetime(2026, 10, 26, 7, 59, tzinfo=NY), NY)


def test_weekday_is_iso_monday_one() -> None:
  # 2026-10-26 is a Monday.
  assert is_open([_row(1, time(8), time(17))], datetime(2026, 10, 26, 9, 0, tzinfo=NY), NY)
  assert not is_open([_row(2, time(8), time(17))], datetime(2026, 10, 26, 9, 0, tzinfo=NY), NY)


def test_two_windows_in_a_day() -> None:
  rows = [_row(1, time(8), time(12)), _row(1, time(13), time(17))]
  assert not is_open(rows, datetime(2026, 10, 26, 12, 30, tzinfo=NY), NY)
  assert is_open(rows, datetime(2026, 10, 26, 13, 30, tzinfo=NY), NY)


def test_no_rows_means_closed() -> None:
  assert not is_open([], datetime(2026, 10, 26, 12, 0, tzinfo=NY), NY)


def test_uses_local_time_across_dst() -> None:
  # DST ended 2026-11-01 (a Sunday): 08:00 local is 13:00 UTC; the old offset would read 09:00 -- still open, so
  # also check 12:00 UTC (07:00 local), which must be closed.
  rows = [_row(7, time(8), time(17))]
  assert is_open(rows, datetime(2026, 11, 1, 13, 0, tzinfo=UTC), NY)
  assert not is_open(rows, datetime(2026, 11, 1, 12, 59, tzinfo=UTC), NY)


def test_naive_at_rejected() -> None:
  with pytest.raises(ValueError, match="aware"):
    is_open([], datetime(2026, 10, 26, 12, 0), NY)  # noqa: DTZ001
