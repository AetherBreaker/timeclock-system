# Standard library imports
import os
import sys

# Third party imports
import aeth_ext
import pytest

# First party imports
from timeclock_system import __main__ as main

OFF = "glibc.pthread.stack_cache_size=0"


class Execed(Exception):  # noqa: N818  # stands in for the process being replaced
  pass


class Initialized(Exception):  # noqa: N818  # stands in for the app starting
  pass


@pytest.fixture
def execs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str], dict[str, str]]]:
  """Every `os.execve` call `run_app` makes (the first ends it); the app itself never starts."""
  calls: list[tuple[str, list[str], dict[str, str]]] = []

  def execve(path: str, argv: list[str], env: dict[str, str]) -> None:
    calls.append((path, argv, env))
    raise Execed

  def initialize(**_kwargs: object) -> None:
    raise Initialized

  monkeypatch.setattr(os, "execve", execve)
  monkeypatch.setattr(aeth_ext, "initialize", initialize)
  monkeypatch.delenv("GLIBC_TUNABLES", raising=False)
  monkeypatch.delenv(main.REEXECED, raising=False)
  return calls


def test_main_constants() -> None:
  # aeth_ext reads these by AST from the entry point (spec §6.9).
  assert main.PROJECT_NAME == "timeclock-system"
  assert main.REQUIRE_LOG_SERVER is False
  assert main.SHUTDOWN_ON_LOG_REJECTION is False


def test_run_app_restarts_itself_with_the_stack_cache_off(execs: list[tuple[str, list[str], dict[str, str]]]) -> None:
  with pytest.raises(Execed):
    main.run_app()
  [(path, argv, env)] = execs
  assert (path, argv) == (sys.executable, sys.orig_argv)
  assert env["GLIBC_TUNABLES"] == OFF
  assert env[main.REEXECED] == "1"
  assert {k: v for k, v in env.items() if k not in {"GLIBC_TUNABLES", main.REEXECED}} == dict(os.environ)


@pytest.mark.parametrize(
  ("before", "after"),
  [
    ("glibc.malloc.arena_max=2", f"glibc.malloc.arena_max=2:{OFF}"),
    ("glibc.pthread.stack_cache_size=1000:glibc.malloc.arena_max=2", f"glibc.malloc.arena_max=2:{OFF}"),
  ],
  ids=["another tunable", "a different cache size"],
)
def test_the_restart_keeps_other_tunables(
  execs: list[tuple[str, list[str], dict[str, str]]], monkeypatch: pytest.MonkeyPatch, before: str, after: str
) -> None:
  monkeypatch.setenv("GLIBC_TUNABLES", before)
  with pytest.raises(Execed):
    main.run_app()
  assert execs[0][2]["GLIBC_TUNABLES"] == after


@pytest.mark.parametrize("tunables", [OFF, f"glibc.malloc.arena_max=2:{OFF}"])
def test_run_app_starts_when_the_stack_cache_is_off(
  execs: list[tuple[str, list[str], dict[str, str]]], monkeypatch: pytest.MonkeyPatch, tunables: str
) -> None:
  monkeypatch.setenv("GLIBC_TUNABLES", tunables)
  with pytest.raises(Initialized):
    main.run_app()
  assert execs == []


def test_run_app_restarts_at_most_once(execs: list[tuple[str, list[str], dict[str, str]]], monkeypatch: pytest.MonkeyPatch) -> None:
  # Were the setting lost across the restart, a second one would loop forever: the app starts without it instead.
  monkeypatch.setenv(main.REEXECED, "1")
  with pytest.raises(Initialized):
    main.run_app()
  assert execs == []
