# Standard library imports
import asyncio
import contextlib
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

# First party imports
from timeclock_system.heartbeat import heartbeat_loop

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

NY = ZoneInfo("America/New_York")


async def run_for(seconds: float, path: Path, busy: float) -> list[tuple[Path, dict[str, object]]]:
  calls: list[tuple[Path, dict[str, object]]] = []

  async def write(p: Path, **kwargs: object) -> None:
    calls.append((p, kwargs))

  task = asyncio.create_task(heartbeat_loop(path, lambda: busy, tz=NY, write=write, interval=0.05))
  await asyncio.sleep(seconds)
  task.cancel()
  with contextlib.suppress(asyncio.CancelledError):
    await task
  return calls


async def test_heartbeat_writes_each_interval(tmp_path: Path) -> None:
  calls = await run_for(0.2, tmp_path / "heartbeat.txt", busy=0.0)
  assert len(calls) >= 3  # noqa: PLR2004
  # No ping arguments: the supervisor owns the pings (DEVKIT_SUPERVISED_PING).
  assert all(call == (tmp_path / "heartbeat.txt", {"tz": NY}) for call in calls)


async def test_heartbeat_skips_while_the_scanner_is_hung(tmp_path: Path) -> None:
  assert await run_for(0.2, tmp_path / "heartbeat.txt", busy=91.0) == []
