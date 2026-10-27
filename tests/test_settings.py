"""Tests for ClockSettings."""

# Third party imports
import pytest
from pydantic import ValidationError

# First party imports
from timeclock_system.settings import ClockSettings


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
  # Independent of the repo's .env: every variable a test depends on is set or removed here.
  for name in ("INSTANCE_ID", "CLOCK_NUMBER", "TIMECLOCK_DB_HOST", "TIMECLOCK_DB_PORT", "PERSISTED_DIR_LOC", "LOG_LOC_FOLDER"):
    monkeypatch.delenv(name, raising=False)
  monkeypatch.setenv("TIMECLOCK_DB_NAME", "tc")
  monkeypatch.setenv("TIMECLOCK_DB_USER", "clock")
  monkeypatch.setenv("TIMECLOCK_DB_PASSWORD", "pw")
  monkeypatch.setenv("CLOCK_NUMBER", "1")


def test_requires_instance_id() -> None:
  with pytest.raises(ValidationError, match="INSTANCE_ID must be set"):
    ClockSettings(_env_file=None)  # pyright: ignore[reportCallIssue]


def test_defaults_and_paths(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setenv("INSTANCE_ID", "dev")
  s = ClockSettings(_env_file=None)  # pyright: ignore[reportCallIssue]
  assert s.db_host == "10.8.0.24"
  assert s.db_port == 5432  # noqa: PLR2004
  assert s.store_path.name == "timeclock.sqlite3"
  assert s.heartbeat_path.name == "heartbeat.txt"
  assert s.branding_dir.name == "branding"
  assert s.crash_alerts_dir.name == "crash_alerts"


def test_conninfo_carries_keepalives(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setenv("INSTANCE_ID", "dev")
  info = ClockSettings(_env_file=None).conninfo()  # pyright: ignore[reportCallIssue]
  assert "keepalives_idle=30" in info
  assert "application_name=timeclock-system-dev" in info
