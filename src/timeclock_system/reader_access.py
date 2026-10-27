"""Startup script (runs as root, before aeth_ext initializes): give uid 999 the fingerprint reader's USB node.

Without it libfprint as uid 999 gets "Access denied (insufficient permissions)" (R14.7). Must not import
aeth_ext, hence plain ``print``; devkit-container logs the script's stderr.
"""

# Standard library imports
import os
import sys
from pathlib import Path

READER_VENDOR = "05ba"
READER_PRODUCT = "000a"
NONROOT_UID = 999


def find_reader_nodes(sysfs: Path = Path("/sys/bus/usb/devices"), devfs: Path = Path("/dev/bus/usb")) -> list[Path]:
  """Return the ``/dev/bus/usb/BBB/DDD`` node of every attached reader (05ba:000a).

  Args:
    sysfs: Directory of USB device entries.
    devfs: Root of the USB device nodes.

  Returns:
    The nodes, in sysfs directory order; entries that lack the id files (interfaces, hubs) are skipped.
  """
  nodes: list[Path] = []
  for entry in sorted(sysfs.iterdir()):
    try:
      if (entry / "idVendor").read_text().strip() != READER_VENDOR or (entry / "idProduct").read_text().strip() != READER_PRODUCT:
        continue
      busnum = int((entry / "busnum").read_text())
      devnum = int((entry / "devnum").read_text())
    except OSError, ValueError:
      continue
    nodes.append(devfs / f"{busnum:03d}" / f"{devnum:03d}")
  return nodes


def main() -> int:
  """Chown the reader's node to ``NONROOT_UID``; return 1 (failing startup) when it is not attached."""
  found = False
  for node in find_reader_nodes():
    try:
      os.chown(node, NONROOT_UID, NONROOT_UID)
    except OSError as e:
      print(f"reader-access: chown {node} failed: {e}", file=sys.stderr)
      continue
    found = True
  if not found:
    print("reader-access: fingerprint reader not found", file=sys.stderr)
    return 1
  return 0
