"""The app's side of the standby screen (standby-screen spec §6): its startup steps out, the update banner and the host update in.

The host's status app (timeclock-standby) reads the steps from `status/app.json` and writes the banner to
`status/banner.json`; the host updater writes `status/host-update.json`. All in the persisted folder the host shares with the container.
"""

# Standard library imports
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, time
from importlib.metadata import version
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

STATUS_FILE = "app.json"
BANNER_FILE = "banner.json"
BANNER_MAX_AGE_SECS = 600
HOST_UPDATE_FILE = "host-update.json"
HOST_UPDATE_STALE_SECS = 300
SCAN_FILE = "scan"
RECORDING_FILE = "recording"


def write_status(status_dir: Path, step: str) -> None:
  """Report the app's current startup step, display-ready, with its version; atomically, so a read never sees half.

  A failure is ignored: the standby page is a nicety, and nothing may stop the clock for it.
  """
  try:
    status_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=status_dir, prefix=".app-")
    with os.fdopen(fd, "w") as f:
      json.dump({"step": step, "version": version("timeclock-system"), "at": datetime.now(UTC).isoformat()}, f)
    os.chmod(tmp, 0o644)  # the host's status app runs as root, but keep it readable like the rest
    os.replace(tmp, status_dir / STATUS_FILE)
  except OSError:
    pass


def write_scan(status_dir: Path, at: datetime) -> None:
  """Mark the moment a finger landed, for the host's screen recorder; atomically, ignoring any failure like `write_status`."""
  try:
    fd, tmp = tempfile.mkstemp(dir=status_dir, prefix=".scan-")
    with os.fdopen(fd, "w") as f:
      f.write(at.isoformat())
    os.chmod(tmp, 0o644)
    os.replace(tmp, status_dir / SCAN_FILE)
  except OSError:
    pass


def read_open_clip(status_dir: Path) -> str | None:
  """The name of the clip the host's screen recorder has open, or None when none is (missing, empty or unreadable file)."""
  try:
    return (status_dir / RECORDING_FILE).read_text().strip() or None
  except OSError:
    return None


def read_banner(status_dir: Path, now: float) -> bool:
  """Whether the host's update banner is up: its file exists, is under 10 minutes old, and carries text.

  Only the fact is used: the page words its own toast, while the file's text keeps serving the previous app release.
  The age limit means a status app that died mid-deploy can't leave the toast up. An unreadable or malformed file
  reads as no banner.
  """
  path = status_dir / BANNER_FILE
  try:
    if now - path.stat().st_mtime >= BANNER_MAX_AGE_SECS:
      return False
    text = json.loads(path.read_text())["text"]
  except OSError, ValueError, KeyError, TypeError:
    return False
  return isinstance(text, str) and bool(text)


@dataclass(frozen=True)
class HostUpdate:
  """A host update's download, as the host updater reports it in `host-update.json`."""

  downloaded: bool
  done: int  # bytes
  total: int  # bytes
  window_start: time | None  # None: installs as soon as it downloads
  at: float  # unix seconds of the file's write


def read_host_update(status_dir: Path, now: float) -> HostUpdate | None:
  """The host updater's download state, or None when there's none to show.

  A `downloading` file older than 5 minutes is stale (the updater dies without removing it). A `downloaded` one stays
  through the restart; the next boot's updater removes it before Docker starts the app. An unreadable, malformed or unrecognised file reads as none: a toast must never stop the clock.
  """
  path = status_dir / HOST_UPDATE_FILE
  try:
    stale = now - path.stat().st_mtime >= HOST_UPDATE_STALE_SECS
    data = json.loads(path.read_text())
    if data["state"] not in {"downloading", "downloaded"} or data["mode"] not in {"window", "immediate"}:
      return None
    downloaded = data["state"] == "downloaded"
    if stale and not downloaded:
      return None
    window_start = time.fromisoformat(data["window_start"]) if data["mode"] == "window" else None
    return HostUpdate(downloaded, int(data["done"]), int(data["total"]), window_start, float(data["at"]))
  except OSError, ValueError, KeyError, TypeError, ArithmeticError:
    return None
