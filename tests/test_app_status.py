"""The app's side of the standby screen: its startup steps out, the update banner in."""

# Standard library imports
import json
import os
import time
from datetime import UTC, datetime, time as dtime, timedelta
from importlib.metadata import version
from typing import TYPE_CHECKING

# First party imports
from timeclock_system.app_status import HostUpdate, read_banner, read_host_update, write_status

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


def test_banner_fresh_is_up(tmp_path: Path) -> None:
  assert read_banner(banner(tmp_path, age=599), time.time()) is True


def test_banner_older_than_10_minutes_is_ignored(tmp_path: Path) -> None:
  assert read_banner(banner(tmp_path, age=600), time.time()) is False


def test_banner_missing_or_malformed_is_down(tmp_path: Path) -> None:
  now = time.time()
  assert read_banner(tmp_path, now) is False
  (tmp_path / "banner.json").write_text("{not json")
  assert read_banner(tmp_path, now) is False
  (tmp_path / "banner.json").write_text(json.dumps({"words": "x"}))
  assert read_banner(tmp_path, now) is False
  (tmp_path / "banner.json").write_text(json.dumps({"text": ""}))
  assert read_banner(tmp_path, now) is False


def host_update(tmp_path: Path, data: dict[str, object] | None = None, age: float = 0) -> Path:
  path = tmp_path / "host-update.json"
  path.write_text(
    json.dumps(
      data
      or {"state": "downloading", "done": 312000000, "total": 740000000, "mode": "window", "window_start": "03:00", "at": 1762905600}
    )
  )
  stamp = time.time() - age
  os.utime(path, (stamp, stamp))
  return tmp_path


def test_host_update_window_mode_parses(tmp_path: Path) -> None:
  assert read_host_update(host_update(tmp_path), time.time()) == HostUpdate(
    downloaded=False, done=312000000, total=740000000, window_start=dtime(3, 0), at=1762905600.0
  )


def test_host_update_immediate_mode_has_no_window(tmp_path: Path) -> None:
  data = {"state": "downloaded", "done": 5, "total": 5, "mode": "immediate", "window_start": None, "at": 1.0}
  update = read_host_update(host_update(tmp_path, data), time.time())
  assert update is not None
  assert update.downloaded
  assert update.window_start is None


def test_a_stale_downloading_file_is_ignored_but_a_stale_downloaded_one_is_read(tmp_path: Path) -> None:
  assert read_host_update(host_update(tmp_path, age=300), time.time()) is None
  data = {"state": "downloaded", "done": 5, "total": 5, "mode": "window", "window_start": "03:00", "at": 1.0}
  assert read_host_update(host_update(tmp_path, data, age=3000), time.time()) is not None


def test_host_update_missing_or_malformed_is_none(tmp_path: Path) -> None:
  now = time.time()
  assert read_host_update(tmp_path, now) is None
  (tmp_path / "host-update.json").write_text("{not json")
  assert read_host_update(tmp_path, now) is None
  (tmp_path / "host-update.json").write_text(json.dumps({"state": "downloading"}))
  assert read_host_update(tmp_path, now) is None
