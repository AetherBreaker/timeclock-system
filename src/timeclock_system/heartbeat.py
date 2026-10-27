"""The heartbeat file devkit-container's health check reads (spec §6.2, §6.9)."""

# Standard library imports
import asyncio
from typing import TYPE_CHECKING

# Third party imports
from aeth_ext.monitoring import send_heartbeat_async

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Awaitable, Callable
  from pathlib import Path
  from zoneinfo import ZoneInfo

HEARTBEAT_INTERVAL_SECS = 60
HUNG_CALL_SECS = 90


async def heartbeat_loop(
  path: Path,
  scanner_busy: Callable[[], float],
  *,
  tz: ZoneInfo,
  write: Callable[..., Awaitable[None]] = send_heartbeat_async,
  interval: float = HEARTBEAT_INTERVAL_SECS,
) -> None:
  """Write the heartbeat file every `interval` seconds, forever, except while a reader call hangs.

  No ping arguments: the supervisor owns the healthchecks.io pings (`DEVKIT_SUPERVISED_PING`). While a blocking libfprint
  call has run past `HUNG_CALL_SECS` the file goes stale, and devkit-container's 180 s check fails (§6.2).
  """
  while True:
    if scanner_busy() <= HUNG_CALL_SECS:
      await write(path, tz=tz)
    await asyncio.sleep(interval)
