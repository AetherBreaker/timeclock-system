"""A local copy of every crash alert (R20, spec §6.8): five rotating JSON-lines files, written at fatal shutdown, never resent."""

# Standard library imports
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

# Third party imports
from aeth_ext.errors.shutdown import SHUTDOWN, ShutdownKind, ShutdownPhase, register_for_shutdown

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable
  from pathlib import Path

  # Third party imports
  from aeth_ext.errors.exception_trail import ExceptionTrail

CRASH_FILES = 5
LINES_PER_FILE = 200


class CrashAlertWriter:
  """Appends one line per crash to `crash-alerts.<n>.jsonl`, moving to the next of five files every 200 lines."""

  def __init__(self, directory: Path, *, program: str, host: str, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
    """Pick up where the newest file left off.

    Args:
      directory: Where the files live (created on the first write).
      program: The program name the alert names (`PROJECT_NAME-INSTANCE_ID`).
      host: This machine's host name.
      now: The wall clock, injectable for tests.
    """
    self._dir = directory
    self._program = program
    self._host = host
    self._now = now
    existing = [p for n in range(CRASH_FILES) if (p := self._path(n)).exists()]
    if existing:
      newest = max(existing, key=lambda p: p.stat().st_mtime_ns)
      self._index = int(newest.name.split(".")[1])
      self._lines = len(newest.read_text(encoding="utf-8").splitlines())
    else:
      self._index, self._lines = 0, 0

  def _path(self, n: int) -> Path:
    return self._dir / f"crash-alerts.{n}.jsonl"

  def write(self, trails: tuple[ExceptionTrail, ...]) -> None:
    """Append one crash's line: when, which program and host, and each fatal exception's trail."""
    mode = "a"
    if self._lines >= LINES_PER_FILE:
      self._index, self._lines, mode = (self._index + 1) % CRASH_FILES, 0, "w"  # "w": the file being reused starts empty
    record = {
      "at": self._now().astimezone(UTC).isoformat(),
      "program": self._program,
      "host": self._host,
      "trails": [
        {
          "origin": {"module": t.origin.module, "file": t.origin.file},
          "entries": [{"module": e.module, "category": e.category.value, "file": e.file} for e in t.entries],
        }
        for t in trails
      ],
    }
    self._dir.mkdir(parents=True, exist_ok=True)
    with self._path(self._index).open(mode, encoding="utf-8") as f:
      f.write(json.dumps(record) + "\n")
    self._lines += 1


def register_crash_alert_writer(writer: CrashAlertWriter) -> None:
  """Save every fatal shutdown's trails through `writer` (R20: every crash alert saved, none resent)."""

  def save(trails: tuple[ExceptionTrail, ...]) -> None:
    if SHUTDOWN.kind >= ShutdownKind.FATAL:
      writer.write(trails)

  # A closure, not a bound method: aeth_ext holds bound methods weakly.
  register_for_shutdown(save, phase=ShutdownPhase.THREADED, priority=-10)
