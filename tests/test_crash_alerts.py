# Standard library imports
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

# Third party imports
from aeth_ext.errors.exception_trail import ExceptionTrail, build_exception_trail
from aeth_ext.errors.shutdown import ShutdownKind, ShutdownPhase

# First party imports
from timeclock_system import crash_alerts
from timeclock_system.crash_alerts import CrashAlertWriter, register_crash_alert_writer

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

  # Third party imports
  import pytest

NOW = datetime(2026, 10, 27, 12, 2, tzinfo=UTC)


def a_trail() -> ExceptionTrail:
  try:
    raise ValueError("boom")  # raised to get a real traceback
  except ValueError as e:
    return build_exception_trail(e)


def writer(directory: Path) -> CrashAlertWriter:
  return CrashAlertWriter(directory, program="timeclock-system-1", host="timeclock-system-1", now=lambda: NOW)


def test_crash_alert_line_shape(tmp_path: Path) -> None:
  writer(tmp_path).write((a_trail(),))
  [line] = (tmp_path / "crash-alerts.0.jsonl").read_text(encoding="utf-8").splitlines()
  record = json.loads(line)
  assert set(record) == {"at", "program", "host", "trails"}
  assert (record["at"], record["program"], record["host"]) == (NOW.isoformat(), "timeclock-system-1", "timeclock-system-1")
  [trail] = record["trails"]
  assert set(trail) == {"origin", "entries"}
  assert set(trail["origin"]) == {"module", "file"}
  assert trail["entries"]
  assert all(set(entry) == {"module", "category", "file"} for entry in trail["entries"])


def test_crash_alerts_rotate_across_five_files(tmp_path: Path) -> None:
  w = writer(tmp_path)
  for _ in range(1001):
    w.write(())
  counts = {p.name: len(p.read_text(encoding="utf-8").splitlines()) for p in tmp_path.glob("crash-alerts.*.jsonl")}
  assert counts == {
    "crash-alerts.0.jsonl": 1,  # the 1,001st line wrapped round to file 0, truncated first
    "crash-alerts.1.jsonl": 200,
    "crash-alerts.2.jsonl": 200,
    "crash-alerts.3.jsonl": 200,
    "crash-alerts.4.jsonl": 200,
  }


def test_a_new_writer_continues_the_newest_file(tmp_path: Path) -> None:
  writer(tmp_path).write(())
  writer(tmp_path).write(())
  assert len((tmp_path / "crash-alerts.0.jsonl").read_text(encoding="utf-8").splitlines()) == 2  # noqa: PLR2004


def test_writer_callback_only_on_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
  registered: list[tuple[object, dict[str, object]]] = []
  monkeypatch.setattr(crash_alerts, "register_for_shutdown", lambda cb, **kwargs: registered.append((cb, kwargs)))
  register_crash_alert_writer(writer(tmp_path))
  [(callback, kwargs)] = registered
  assert kwargs == {"phase": ShutdownPhase.THREADED, "priority": -10}
  assert callable(callback)

  monkeypatch.setattr(crash_alerts, "SHUTDOWN", SimpleNamespace(kind=ShutdownKind.GRACEFUL))
  callback((a_trail(),))
  assert list(tmp_path.glob("crash-alerts.*.jsonl")) == []

  monkeypatch.setattr(crash_alerts, "SHUTDOWN", SimpleNamespace(kind=ShutdownKind.FATAL))
  callback((a_trail(),))
  assert len((tmp_path / "crash-alerts.0.jsonl").read_text(encoding="utf-8").splitlines()) == 1
