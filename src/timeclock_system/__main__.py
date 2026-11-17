"""Entry point; stays import-light so initialize() runs before anything else."""

PROJECT_NAME = "timeclock-system"
REQUIRE_LOG_SERVER = False  # R16.3
SHUTDOWN_ON_LOG_REJECTION = False  # R16.5

STACK_CACHE_OFF = "glibc.pthread.stack_cache_size=0"
REEXECED = "TIMECLOCK_SYSTEM_REEXECED"  # set on the restart below, so it happens at most once


def run_app() -> None:
  """Run the clock app; the exit code says whether it ended fatally."""
  # Standard library imports
  import contextlib
  import os
  import sys

  # glibc caches exited threads' stacks, thread-local storage included, so it would keep each registration matcher
  # thread's bozorth3 tables (about 47 MB; timeclock-libfprint's thread-safe-bozorth3.patch) after the thread ends. It
  # reads its tunables only at process start: the image sets this one, and any other launch restarts once with it.
  tunables = [t for t in os.environ.get("GLIBC_TUNABLES", "").split(":") if t]
  if STACK_CACHE_OFF not in tunables and REEXECED not in os.environ:
    kept = [t for t in tunables if not t.startswith("glibc.pthread.stack_cache_size=")]
    env = {**os.environ, "GLIBC_TUNABLES": ":".join([*kept, STACK_CACHE_OFF]), REEXECED: "1"}
    os.execve(sys.executable, sys.orig_argv, env)

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
