"""Shared test setup."""

# Standard library imports
import asyncio
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable

  # Third party imports
  import pytest

# aeth_ext's import needs this; set before anything imports it.
os.environ.setdefault("ALERTS_EMAIL_PWD", "test")


def pytest_asyncio_loop_factories(config: pytest.Config, item: pytest.Item) -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
  """Psycopg's async mode can't use Windows' default Proactor loop.

  pytest-asyncio 1.4 deprecates overriding the `event_loop_policy` fixture (and the policy classes are deprecated on 3.14),
  so the loop is chosen through this hook.
  """
  return {"default": asyncio.SelectorEventLoop if sys.platform == "win32" else asyncio.new_event_loop}
