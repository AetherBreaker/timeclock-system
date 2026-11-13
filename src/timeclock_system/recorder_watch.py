"""Alerts when the host's screen recorder stops reporting (screen-recorder spec §5.5)."""

# Standard library imports
import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Awaitable, Callable
  from pathlib import Path
  from zoneinfo import ZoneInfo

RECORDER_FILE = "recorder.json"
STALE_SECS = 600
CHECK_SECS = 60


async def recorder_watch_loop(
  status_dir: Path,
  alert: Callable[[str, str], None],
  *,
  tz: ZoneInfo,
  now: Callable[[], datetime] = lambda: datetime.now(UTC),
  sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
  """Every minute, alert once when `recorder.json`'s `at` is over 10 minutes old, and once when it's fresh again.

  The recorder writes the file only while it works, so a missing one means it never ran here: no alert. Nothing alerts in
  the app's first 10 minutes, as the recorder may still be starting; a file already stale then is judged after them.
  """
  started = now()
  stopped = False
  while True:
    current = now()
    shown = ""
    try:
      at = datetime.fromisoformat(json.loads((status_dir / RECORDER_FILE).read_text())["at"])
      if at.tzinfo is None:  # the recorder writes UTC; a zoneless time can't be subtracted from an aware one
        at = at.replace(tzinfo=UTC)
      age = (current - at).total_seconds()
      shown = f"{at.astimezone(tz):%Y-%m-%d %H:%M:%S %Z}"  # here too: an extreme year overflows the conversion
    except OSError, ValueError, KeyError, TypeError, OverflowError:
      age = None  # no usable file: no alert, and the loop must live on (a crash here shuts the clock down)
    if age is not None:
      if not stopped and age > STALE_SECS and (current - started).total_seconds() >= STALE_SECS:
        stopped = True
        alert("Screen recorder stopped", f"The screen recorder's last report was at {shown}.")
      elif stopped and age <= STALE_SECS:
        stopped = False
        alert("Screen recorder running again", f"The screen recorder reported again at {shown}.")
    await sleep(CHECK_SECS)
