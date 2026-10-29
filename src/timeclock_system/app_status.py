"""The app's side of the standby screen (standby-screen spec §6): its startup steps out, the update banner in.

The host's status app (timeclock-standby) reads the steps from `status/app.json` and writes the banner to
`status/banner.json`, both in the persisted folder the host shares with the container.
"""

# Standard library imports
import json
import os
import tempfile
from datetime import UTC, datetime
from importlib.metadata import version
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

STATUS_FILE = "app.json"
BANNER_FILE = "banner.json"
BANNER_MAX_AGE_SECS = 600


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


def read_banner(status_dir: Path, now: float) -> str | None:
  """The update banner's text while its file exists and is under 10 minutes old, else None.

  The age limit means a status app that died mid-deploy can't leave the banner up. An unreadable or malformed file
  reads as no banner.
  """
  path = status_dir / BANNER_FILE
  try:
    if now - path.stat().st_mtime >= BANNER_MAX_AGE_SECS:
      return None
    text = json.loads(path.read_text())["text"]
  except OSError, ValueError, KeyError, TypeError:
    return None
  return text if isinstance(text, str) and text else None
