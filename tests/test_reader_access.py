"""Tests for the reader-access startup script."""

# Standard library imports
import os
from typing import TYPE_CHECKING

# Third party imports
import pytest

# First party imports
from timeclock_system.reader_access import find_reader_nodes, main

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path


@pytest.fixture
def sysfs(tmp_path: Path) -> Path:
  root = tmp_path / "sys"

  def make(name: str, **files: str) -> None:
    (root / name).mkdir(parents=True)
    for fname, content in files.items():
      (root / name / fname).write_text(content + "\n")

  make("1-1", idVendor="05ba", idProduct="000a", busnum="1", devnum="7")
  make("1-2", idVendor="046d", idProduct="c52b", busnum="1", devnum="3")
  make("usb1")
  return root


def test_finds_only_the_4500(sysfs: Path, tmp_path: Path) -> None:
  devfs = tmp_path / "dev"
  assert find_reader_nodes(sysfs, devfs) == [devfs / "001" / "007"]


def test_main_chowns_to_999_and_returns_0(sysfs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
  devfs = tmp_path / "dev"
  calls: list[tuple[Path, int, int]] = []
  monkeypatch.setattr(os, "chown", lambda path, uid, gid: calls.append((path, uid, gid)), raising=False)
  monkeypatch.setattr("timeclock_system.reader_access.find_reader_nodes", lambda: find_reader_nodes(sysfs, devfs))
  assert main() == 0
  assert calls == [(devfs / "001" / "007", 999, 999)]


def test_main_without_reader_returns_1_and_says_so(
  tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
  (tmp_path / "empty").mkdir()
  monkeypatch.setattr("timeclock_system.reader_access.find_reader_nodes", lambda: find_reader_nodes(tmp_path / "empty", tmp_path))
  assert main() == 1
  assert "fingerprint reader not found" in capsys.readouterr().err
