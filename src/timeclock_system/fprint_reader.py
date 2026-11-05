"""The libfprint reader behind `scanner.Reader` (spec §6.2), and the hardware probe Task 12 runs on timeclock-dev.

Runs only in the image: PyGObject and the patched libfprint don't exist on Windows, so nothing imports this module there.
Every method blocks and runs on the scanner thread, the only thread that touches libfprint (R11.1).
"""

# Standard library imports
import argparse
import logging
import threading
import time
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

# Third party imports
import gi
from timeclock_schema.enums import Finger

gi.require_version("FPrint", "2.0")

# Third party imports
from gi.repository import FPrint, Gio, GLib  # after require_version, or gi picks whatever FPrint it finds

# First party imports
from timeclock_system.scanner import (
  Capture,
  FingerStatus,
  GalleryPrint,
  ReaderFailure,
  ReaderGone,
  ReaderTooHot,
  RetryScan,
  ScanCancelled,
  ScanImage,
)

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Sequence

  # First party imports
  from timeclock_system.registration import Minutiae

# The domains are compared through libfprint's quark functions, not their literal strings. Older PyGObject builds expose
# the retry quark only on the enum; the probe prints which source this build used.
if hasattr(FPrint, "device_retry_quark"):
  _RETRY_SOURCE, _RETRY_DOMAIN = "FPrint.device_retry_quark", GLib.quark_to_string(FPrint.device_retry_quark())
else:
  _RETRY_SOURCE, _RETRY_DOMAIN = "FPrint.DeviceRetry.quark", GLib.quark_to_string(FPrint.DeviceRetry.quark())
_ERROR_DOMAIN = GLib.quark_to_string(FPrint.device_error_quark())
LOGGER = logging.getLogger(__name__)
_SCORED_CACHE_MAX = 1024  # deserialized templates kept for `score`; far above any gallery


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
    self._scored: dict[bytes, FPrint.Print] = {}  # `score`'s `other` templates, by bytes

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

  def _watch_finger(self, on_status: Callable[[FingerStatus], None]) -> int:
    """Report every finger-status change to `on_status`; returns the handler id to disconnect."""

    def changed(device: FPrint.Device, _pspec: object) -> None:
      flags = device.get_finger_status()
      present, needed = FPrint.FingerStatusFlags.PRESENT, FPrint.FingerStatusFlags.NEEDED
      on_status(FingerStatus(present=bool(flags & present), waiting=bool(flags & needed)))

    return self._device.connect("notify::finger-status", changed)

  def identify(
    self,
    gallery: Sequence[GalleryPrint],
    cancel: Gio.Cancellable,
    on_status: Callable[[FingerStatus], None],
    on_result: Callable[[Capture], None],
  ) -> Capture:
    """Capture one print and score it against every print in `gallery`, in order.

    `on_result` gets the capture from libfprint's match callback, which `fpi_device_identify_report` calls the moment the
    scan is matched; `identify_sync` returns only after the finger lifts (the device deactivates on finger-off). Every
    gallery print's score is read from the patched libfprint's `get_identify_score` (-1: not scored).
    """
    try:
      self._prints = {p.print_id: self._prints.get(p.print_id) or FPrint.Print.deserialize(p.template) for p in gallery}
    except GLib.Error as e:
      raise _reader_error(e) from e

    def capture_of(match: FPrint.Print | None, probe: FPrint.Print | None) -> Capture:
      # libfprint hands back the gallery object it matched, so identity finds its id.
      match_id = next((print_id for print_id, print_ in self._prints.items() if print_ is match), None)
      scores = tuple((print_id, print_.get_identify_score()) for print_id, print_ in self._prints.items())
      # The probe's bytes and image are best-effort: no failure there may cost the capture its match or scores. Once
      # the bytes are bound to `scan_data`, only `Capture(...)`, which can't raise, runs, so no traceback renders them.
      scan_data: tuple[bytes | None, ScanImage | None] = (None, None)
      if probe is not None:
        try:
          raw = probe.get_image()
          scan_data = (
            bytes(probe.serialize()),
            None if raw is None else ScanImage(bytes(raw.get_data()), raw.get_width(), raw.get_height()),
          )
        except Exception as e:  # noqa: BLE001 - best effort; anything escaping here would kill the reader thread
          LOGGER.warning("the probe's scan data is unavailable: %s: %s", type(e).__name__, e)  # never bytes
      return Capture(match_id, scores, *scan_data)

    def matched(_device: FPrint.Device, match: FPrint.Print | None, probe: FPrint.Print, _user_data: object, error: object) -> None:
      if error is None:  # a retry reports its error here too, and the call then raises it
        on_result(capture_of(match, probe))

    handler = self._watch_finger(on_status)
    try:
      match, probe = self._device.identify_sync(list(self._prints.values()), cancel, matched, None)
    except GLib.Error as e:
      raise _reader_error(e) from e
    finally:
      self._device.disconnect(handler)
    return capture_of(match, probe)

  def enroll(
    self, cancel: Gio.Cancellable, on_stage: Callable[[int, int, bool], None], on_status: Callable[[FingerStatus], None]
  ) -> bytes:
    """Run every enrollment stage; return the serialized template. A press with an error (a retry) isn't counted."""

    def progress(device: FPrint.Device, completed_stages: int, _print: object, _user_data: object, error: object) -> None:
      on_stage(completed_stages, device.get_nr_enroll_stages(), error is None)

    handler = self._watch_finger(on_status)
    try:
      enrolled = self._device.enroll_sync(FPrint.Print.new(self._device), cancel, progress, None)
    except GLib.Error as e:
      raise _reader_error(e) from e
    finally:
      self._device.disconnect(handler)
    return bytes(enrolled.serialize())

  def capture(self, cancel: Gio.Cancellable, on_status: Callable[[FingerStatus], None]) -> bytes:
    """Capture one press: an empty-gallery identify's probe, serialized (FP3, one press). Raises like `identify`."""
    handler = self._watch_finger(on_status)
    try:
      _match, probe = self._device.identify_sync([], cancel, None, None)
      return bytes(probe.serialize())
    except GLib.Error as e:
      raise _reader_error(e) from e
    finally:
      self._device.disconnect(handler)

  def presses(self, template: bytes) -> tuple[Minutiae, ...]:
    """Decode a template's presses: (x, y, theta) per detail point, from the FP3 payload after the 3-byte header."""
    variant = GLib.Variant.new_from_bytes(GLib.VariantType("(issbymsmsia{sv}v)"), GLib.Bytes(template[3:]), False)  # noqa: FBT003
    nbis = variant.get_child_value(9).get_variant().unpack()  # (a(aiaiai)): per press, its x, y and theta columns
    return tuple(tuple(zip(x, y, theta, strict=True)) for x, y, theta in nbis[0])

  def score(self, press: bytes, index: int, other: bytes) -> tuple[int, ...]:
    """Score `press`'s press `index` against each of `other`'s presses (bozorth3, as the scans are scored)."""
    try:
      if other not in self._scored:
        if len(self._scored) >= _SCORED_CACHE_MAX:
          self._scored.clear()
        self._scored[other] = FPrint.Print.deserialize(other)
      return tuple(FPrint.Print.deserialize(press).score_press(index, self._scored[other]))
    except GLib.Error as e:
      raise _reader_error(e) from e

  def merge(self, presses: Sequence[bytes]) -> bytes:
    """Merge one-press templates into one template, in order."""
    try:
      return bytes(FPrint.Print.new_from_presses([FPrint.Print.deserialize(p) for p in presses]).serialize())
    except GLib.Error as e:
      raise _reader_error(e) from e


def _probe_identify(reader: FprintReader) -> None:
  # Split out of main for PLR0915.
  reader.open()
  try:
    print("enroll: press the same finger 5 times")
    captured = []
    while len(captured) < 5:  # noqa: PLR2004
      try:
        captured.append(reader.capture(reader.new_cancel(), lambda _status: None))
        print(f"press {len(captured)}/5")
      except RetryScan as e:
        print(f"press {len(captured) + 1}/5 (retry: {e})")
      time.sleep(1.0)  # the scanner's finger_off_pause
    template = reader.merge(captured)
    print(f"template_bytes={len(template)}")
    gallery = [GalleryPrint(uuid4(), 0, template, Finger.RIGHT_INDEX)]
    for attempt in range(1, 4):
      time.sleep(1.0)  # the scanner's finger_off_pause
      print(f"identify {attempt}: press the finger")
      started = time.monotonic()
      try:
        capture = reader.identify(gallery, reader.new_cancel(), lambda _status: None, lambda _result: None)
        best = max((score for _print_id, score in capture.scores), default=None)
        image = capture.image
        size = "no image" if image is None else f"image {image.width}x{image.height}"
        result = f"{'match' if capture.match else 'no match'} (best score {best}, {len(capture.scores)} scores, {size})"
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
        reader.identify([], token, lambda _status: None, lambda _result: None)
        print(f"{time.strftime('%H:%M:%S')} capture")
        time.sleep(1.0)  # the scanner's finger_off_pause
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
