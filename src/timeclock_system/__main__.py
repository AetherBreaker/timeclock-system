"""Entry point; stays import-light so initialize() runs before anything else."""

PROJECT_NAME = "timeclock-system"
REQUIRE_LOG_SERVER = False  # R16.3
SHUTDOWN_ON_LOG_REJECTION = False  # R16.5


def run_app() -> None:
  """Run the clock app; the exit code says whether it ended fatally."""
  # Standard library imports
  import contextlib

  # Third party imports
  from aeth_ext import initialize
  from aeth_ext.errors.shutdown import SHUTDOWN, ShutdownKind

  # asyncio stays False: uvicorn brings uvloop itself (R5).
  initialize(logging="socket", install_signal_handlers=True)

  # First party imports
  from timeclock_system.app import serve

  # Not wrapped in handle_fatal_exc_sync: aeth_ext's exit nudge (KeyboardInterrupt) would count as fatal (TODO #15).
  with contextlib.suppress(KeyboardInterrupt):
    serve(PROJECT_NAME)
  raise SystemExit(1 if SHUTDOWN.kind >= ShutdownKind.FATAL else 0)
