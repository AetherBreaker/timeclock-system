"""The app's side of the standby screen: its startup steps out, the update banner in."""

# Standard library imports
import json
import os
import time
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from typing import TYPE_CHECKING

# First party imports
from timeclock_system.app_status import read_banner, write_status

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path


def test_writes_step_version_and_time(tmp_path: Path) -> None:
  path = tmp_path / "status" / "app.json"
  write_status(tmp_path / "status", "Loading employees and punches")
  data = json.loads(path.read_text())
  assert data["step"] == "Loading employees and punches"
  assert data["version"] == version("timeclock-system")
  assert abs(datetime.fromisoformat(data["at"]) - datetime.now(UTC)) < timedelta(seconds=5)


def test_write_is_atomic_rename(tmp_path: Path) -> None:
  path = tmp_path / "app.json"
  write_status(tmp_path, "Starting the fingerprint reader")
  write_status(tmp_path, "Ready")
  assert [p.name for p in tmp_path.iterdir()] == ["app.json"]
  assert json.loads(path.read_text())["step"] == "Ready"


def test_creates_the_folder(tmp_path: Path) -> None:
  write_status(tmp_path / "a" / "b", "Ready")
  assert (tmp_path / "a" / "b" / "app.json").exists()


def test_write_failure_is_ignored(tmp_path: Path) -> None:
  # The standby page is a nicety: a status file it can't write must never stop the clock.
  (tmp_path / "not-a-folder").write_text("")
  write_status(tmp_path / "not-a-folder", "Ready")


def banner(tmp_path: Path, text: str = "An update is being installed.", age: float = 0) -> Path:
  path = tmp_path / "banner.json"
  path.write_text(json.dumps({"text": text}))
  stamp = time.time() - age
  os.utime(path, (stamp, stamp))
  return tmp_path


def test_banner_fresh_is_shown(tmp_path: Path) -> None:
  assert read_banner(banner(tmp_path, age=599), time.time()) == "An update is being installed."


def test_banner_older_than_10_minutes_is_ignored(tmp_path: Path) -> None:
  assert read_banner(banner(tmp_path, age=600), time.time()) is None


def test_banner_missing_or_malformed_is_none(tmp_path: Path) -> None:
  now = time.time()
  assert read_banner(tmp_path, now) is None
  (tmp_path / "banner.json").write_text("{not json")
  assert read_banner(tmp_path, now) is None
  (tmp_path / "banner.json").write_text(json.dumps({"words": "x"}))
  assert read_banner(tmp_path, now) is None
  (tmp_path / "banner.json").write_text(json.dumps({"text": ""}))
  assert read_banner(tmp_path, now) is None
