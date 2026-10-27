"""Clock settings: aeth_ext's base plus the PostgreSQL connection and the clock's persisted paths."""

# Standard library imports
from typing import TYPE_CHECKING, Annotated, Self

# Third party imports
from aeth_ext.settings import BaseSettings
from psycopg.conninfo import make_conninfo
from pydantic import Field, SecretStr, model_validator

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path


class ClockSettings(BaseSettings):
  """Settings of one clock; ``INSTANCE_ID`` and ``CLOCK_NUMBER`` are mandatory, so a clock without them doesn't start."""

  db_host: Annotated[str, Field(alias="TIMECLOCK_DB_HOST")] = "10.8.0.24"
  db_port: Annotated[int, Field(alias="TIMECLOCK_DB_PORT")] = 5432
  db_name: Annotated[str, Field(alias="TIMECLOCK_DB_NAME")]
  db_user: Annotated[str, Field(alias="TIMECLOCK_DB_USER")]
  db_password: Annotated[SecretStr, Field(alias="TIMECLOCK_DB_PASSWORD")]
  clock_number: Annotated[int, Field(alias="CLOCK_NUMBER", ge=1, le=99)]
  """Picks this clock's block of employee numbers, n*10000+1 to n*10000+9999 (§6.4); unique per clock, separate from
  `INSTANCE_ID`, which names the clock and its location."""

  @model_validator(mode="after")
  def _require_instance_id(self) -> Self:
    if self.instance_id is None:
      raise ValueError("INSTANCE_ID must be set on a clock")
    return self

  @property
  def store_path(self) -> Path:
    """The local SQLite store."""
    return self.persisted_dir_loc / "timeclock.sqlite3"

  @property
  def branding_dir(self) -> Path:
    """Where the synced branding assets live."""
    return self.persisted_dir_loc / "branding"

  @property
  def crash_alerts_dir(self) -> Path:
    """Where crash alerts wait for a working network."""
    return self.persisted_dir_loc / "crash_alerts"

  @property
  def heartbeat_path(self) -> Path:
    """The file the supervisor reads to judge liveness."""
    return self.log_loc_folder / "heartbeat.txt"

  def conninfo(self) -> str:
    """Return the libpq conninfo string; it carries the password, so never log it.

    TCP keepalives (§6.6) make a half-dead VPN tunnel fail within ~60 s instead of hanging.
    """
    return make_conninfo(
      host=self.db_host,
      port=self.db_port,
      dbname=self.db_name,
      user=self.db_user,
      password=self.db_password.get_secret_value(),
      connect_timeout=10,
      keepalives=1,
      keepalives_idle=30,
      keepalives_interval=10,
      keepalives_count=3,
      application_name=f"timeclock-system-{self.instance_id}",
    )
