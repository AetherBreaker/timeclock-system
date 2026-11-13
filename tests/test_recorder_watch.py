"""The recorder watch: alerts once when `recorder.json` goes stale, once when it recovers, never without the file."""

# Standard library imports
import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

# First party imports
from timeclock_system.recorder_watch import RECORDER_FILE, recorder_watch_loop

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable
  from pathlib import Path

START = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
TZ = ZoneInfo("America/New_York")


class Done(Exception):  # noqa: N818 - ends the loop
  pass


def run_watch(status_dir: Path, minutes: int, on_tick: Callable[[datetime], None] | None = None) -> list[tuple[str, str]]:
  """Run the loop for `minutes` of simulated time; `on_tick(now)` runs as each sleep ends, and once before the start."""
  clock = [START]
  alerts: list[tuple[str, str]] = []

  async def sleep(secs: float) -> None:
    clock[0] += timedelta(seconds=secs)
    if clock[0] >= START + timedelta(minutes=minutes):
      raise Done
    if on_tick:
      on_tick(clock[0])

  async def go() -> None:
    if on_tick:
      on_tick(START)
    try:
      await recorder_watch_loop(status_dir, lambda r, d: alerts.append((r, d)), tz=TZ, now=lambda: clock[0], sleep=sleep)
    except Done:
      pass

  asyncio.run(go())
  return alerts


def report(status_dir: Path, at: datetime) -> None:
  (status_dir / RECORDER_FILE).write_text(json.dumps({"at": at.isoformat(), "state": "idle"}))


def test_no_file_never_alerts(tmp_path: Path) -> None:
  assert run_watch(tmp_path, 30) == []


def test_a_fresh_file_never_alerts(tmp_path: Path) -> None:
  assert run_watch(tmp_path, 30, lambda now: report(tmp_path, now)) == []


def test_a_stale_file_alerts_once_after_ten_minutes_of_uptime(tmp_path: Path) -> None:
  last = START - timedelta(minutes=11)
  report(tmp_path, last)
  assert run_watch(tmp_path, 10) == []
  [(reason, details)] = run_watch(tmp_path, 30)
  assert reason == "Screen recorder stopped"
  assert last.astimezone(TZ).strftime("%H:%M") in details


def test_recovery_alerts_once(tmp_path: Path) -> None:
  report(tmp_path, START - timedelta(minutes=11))

  def on_tick(now: datetime) -> None:
    if now >= START + timedelta(minutes=15):
      report(tmp_path, now)

  alerts = run_watch(tmp_path, 30, on_tick)
  assert [r for r, _ in alerts] == ["Screen recorder stopped", "Screen recorder running again"]
