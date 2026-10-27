"""Entry point; stays import-light so initialize() runs before anything else."""

PROJECT_NAME = "timeclock-system"
REQUIRE_LOG_SERVER = False  # R16.3
SHUTDOWN_ON_LOG_REJECTION = False  # R16.5


def run_app() -> None:
  """Run the clock app."""
  raise NotImplementedError("wired in Task 11")
