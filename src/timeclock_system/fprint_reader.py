"""The libfprint reader behind `scanner.Reader` (spec §6.2), and the hardware probe Task 12 runs on timeclock-dev.

Runs only in the image: PyGObject and the patched libfprint don't exist on Windows, so nothing imports this module there.
Every method blocks and runs on the scanner thread, the only thread that touches libfprint (R11.1).
"""

# Standard library imports
import argparse
import threading
import time
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

# Third party imports
import gi

gi.require_version("FPrint", "2.0")

# Third party imports
from gi.repository import FPrint, Gio, GLib  # after require_version, or gi picks whatever FPrint it finds

# First party imports
from timeclock_system.scanner import (
  GalleryPrint,
  ReaderFailure,
  ReaderGone,
  ReaderTooHot,
  RetryScan,
  ScanCancelled,
)

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Sequence

# The domains are compared through libfprint's quark functions, not their literal strings. Older PyGObject builds expose
# the retry quark only on the enum; the probe prints which source this build used.
if hasattr(FPrint, "device_retry_quark"):
  _RETRY_SOURCE, _RETRY_DOMAIN = "FPrint.device_retry_quark", GLib.quark_to_string(FPrint.device_retry_quark())
else:
  _RETRY_SOURCE, _RETRY_DOMAIN = "FPrint.DeviceRetry.quark", GLib.quark_to_string(FPrint.DeviceRetry.quark())
_ERROR_DOMAIN = GLib.quark_to_string(FPrint.device_error_quark())


def _reader_error(e: GLib.Error) -> Exception:
  """Map a libfprint error onto the scanner's exceptions (§6.2)."""
  if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
    return ScanCancelled()
  if e.domain == _RETRY_DOMAIN:
    return RetryScan(e.message)
  if e.domain == _ERROR_DOMAIN and e.code == FPrint.DeviceError.REMOVED:
    return ReaderGone(e.message)
  if e.domain == _ERROR_DOMAIN and e.code == FPrint.DeviceError.TOO_HOT:
    return ReaderTooHot(e.message)
  return ReaderFailure(e.message)


class FprintReader:
  """The DigitalPersona U.are.U 4500 (`uru4000`) through libfprint's blocking API."""

  def __init__(self) -> None:
    """Find the reader.

    Raises:
      ReaderGone: No `uru4000` device is attached.
    """
    # Held for the object's life: dropping the Context while its device is in use segfaults (spike).
    self._ctx = FPrint.Context.new()
    self._ctx.enumerate()
    devices = [d for d in self._ctx.get_devices() if d.get_driver() == "uru4000"]
    if not devices:
      raise ReaderGone("no uru4000 fingerprint reader is attached")
    self._device = devices[0]
    # Deserialized once per print (R36.4); rebuilt from each call's gallery, so removed prints drop out.
    self._prints: dict[UUID, FPrint.Print] = {}

  def new_cancel(self) -> Gio.Cancellable:
    """Return a fresh cancellable for one call; `cancel()` is safe from any thread."""
    return Gio.Cancellable()

  def open(self) -> None:
    """Open the device.

    Raises:
      ReaderGone: Any open failure (R9: §6.2 makes "open fails" a lost reader).
    """
    try:
      self._device.open_sync(None)
    except GLib.Error as e:
      raise ReaderGone(e.message) from e

  def close(self) -> None:
    """Close the device so it autosuspends (R40.1); a no-op when it isn't open."""
    if not self._device.is_open():
      return
    try:
      self._device.close_sync(None)
    except GLib.Error as e:
      raise _reader_error(e) from e

  def identify(self, gallery: Sequence[GalleryPrint], cancel: Gio.Cancellable, on_finger: Callable[[], None]) -> UUID | None:
    """Capture one print and match it against `gallery` in order; return the matched print id or None."""
    try:
      self._prints = {p.print_id: self._prints.get(p.print_id) or FPrint.Print.deserialize(p.template) for p in gallery}
    except GLib.Error as e:
      raise _reader_error(e) from e

    def on_status(device: FPrint.Device, _pspec: object) -> None:
      if device.get_finger_status() & FPrint.FingerStatusFlags.PRESENT:
        on_finger()

    handler = self._device.connect("notify::finger-status", on_status)
    try:
      match, _probe = self._device.identify_sync(list(self._prints.values()), cancel, None, None)
    except GLib.Error as e:
      raise _reader_error(e) from e
    finally:
      self._device.disconnect(handler)
    if match is None:
      return None
    # libfprint hands back the gallery object it matched, so identity finds its id.
    return next(print_id for print_id, print_ in self._prints.items() if print_ is match)

  def enroll(self, cancel: Gio.Cancellable, on_stage: Callable[[int, int], None]) -> bytes:
    """Run every enrollment stage; return the serialized template."""

    def progress(device: FPrint.Device, completed_stages: int, _print: object, _user_data: object, _error: object) -> None:
      on_stage(completed_stages, device.get_nr_enroll_stages())

    try:
      enrolled = self._device.enroll_sync(FPrint.Print.new(self._device), cancel, progress, None)
    except GLib.Error as e:
      raise _reader_error(e) from e
    return bytes(enrolled.serialize())


def _probe_identify(reader: FprintReader) -> None:
  # Split out of main for PLR0915.
  reader.open()
  try:
    print("enroll: press the same finger 5 times")
    template = reader.enroll(reader.new_cancel(), lambda done, total: print(f"stage {done}/{total}"))
    print(f"template_bytes={len(template)}")
    gallery = [GalleryPrint(uuid4(), 0, template)]
    for attempt in range(1, 4):
      time.sleep(2.0)  # the scanner's finger_off_pause
      print(f"identify {attempt}: press the finger")
      started = time.monotonic()
      try:
        result = "match" if reader.identify(gallery, reader.new_cancel(), lambda: None) else "no match"
      except RetryScan as e:
        result = f"retry ({e})"
      print(f"identify {attempt}: {result} in {time.monotonic() - started:.2f} s")
  finally:
    reader.close()


def _soak(reader: FprintReader, minutes: float) -> None:
  # Split out of main for PLR0915.
  reader.open()
  too_hot = 0
  end = time.monotonic() + minutes * 60
  try:
    while (left := end - time.monotonic()) > 0:
      token = reader.new_cancel()
      rearm = threading.Timer(min(60.0, left), token.cancel)
      rearm.start()
      try:
        # An empty gallery still captures (§6.2's version-2 path), so no enrollment is needed to soak.
        reader.identify([], token, lambda: None)
        print(f"{time.strftime('%H:%M:%S')} capture")
        time.sleep(2.0)  # the scanner's finger_off_pause
      except ScanCancelled:
        print(f"{time.strftime('%H:%M:%S')} re-armed")
      except ReaderTooHot:
        too_hot += 1
        print(f"{time.strftime('%H:%M:%S')} TOO_HOT")
        time.sleep(60)
      except RetryScan, ReaderFailure:
        print(f"{time.strftime('%H:%M:%S')} retry or failure")
        time.sleep(1)
      finally:
        rearm.cancel()
  finally:
    reader.close()
  print(f"too_hot_count={too_hot}")


def main(argv: Sequence[str] | None = None) -> None:
  """Run one hardware check (Task 12); each prints plain lines for the PR."""
  parser = argparse.ArgumentParser(prog="python -m timeclock_system.fprint_reader")
  commands = parser.add_subparsers(dest="command", required=True)
  commands.add_parser("probe-identify", help="enroll one finger, then identify it 3 times")
  soak = commands.add_parser("soak", help="keep identify waiting with the 60 s re-arm; count TOO_HOT")
  soak.add_argument("minutes", type=float)
  hold = commands.add_parser("closed-hold", help="open, close, then hold the Context (the autosuspend check)")
  hold.add_argument("seconds", type=float)
  args = parser.parse_args(argv)

  reader = FprintReader()
  print(f"retry domain via {_RETRY_SOURCE}: {_RETRY_DOMAIN}")
  match args.command:
    case "probe-identify":
      _probe_identify(reader)
    case "soak":
      _soak(reader, args.minutes)
    case "closed-hold":
      reader.open()
      reader.close()
      print(f"closed; holding the Context for {args.seconds:g} s")
      time.sleep(args.seconds)
      print("done")


if __name__ == "__main__":
  main()
