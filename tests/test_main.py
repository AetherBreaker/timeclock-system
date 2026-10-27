# First party imports
from timeclock_system import __main__ as main


def test_main_constants() -> None:
  # aeth_ext reads these by AST from the entry point (spec §6.9).
  assert main.PROJECT_NAME == "timeclock-system"
  assert main.REQUIRE_LOG_SERVER is False
  assert main.SHUTDOWN_ON_LOG_REJECTION is False
