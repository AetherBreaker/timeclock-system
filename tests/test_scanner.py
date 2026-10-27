"""Tests for the scanner thread, driven by a scripted FakeReader."""

# Standard library imports
import queue
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, override
from uuid import UUID, uuid4

# Third party imports
import pytest

# First party imports
from timeclock_system.scanner import (
  CancelToken,
  Enrolled,
  EnrollFailed,
  EnrollStage,
  FingerDown,
  GalleryPrint,
  Matched,
  NoMatch,
  ReaderFailure,
  ReaderGone,
  ReaderMode,
  ReaderTooHot,
  Retry,
  RetryScan,
  ScanCancelled,
  Scanner,
  ScannerEvent,
  ScannerTimings,
)

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Iterator, Sequence

WAIT = 2.0
BLOCK = object()  # identify/enroll outcome: block until the token is cancelled, then raise ScanCancelled
FINGER = object()  # identify script entry: call on_finger, then take the next entry

type Outcome = UUID | Exception | object | None


class FakeToken:
  def __init__(self) -> None:
    self.event = threading.Event()

  def cancel(self) -> None:
    self.event.set()


class FakeReader:
  """Scripted reader; every call is recorded in `calls` as (name, monotonic time, detail)."""

  def __init__(
    self,
    script: Sequence[Outcome] = (),
    *,
    default: Outcome = BLOCK,
    enroll_stages: int = 5,
    stage_gap: float = 0.0,
    enroll_result: bytes | object = b"template",
  ) -> None:
    self.script: deque[Outcome] = deque(script)
    self.default = default
    self.enroll_stages = enroll_stages
    self.stage_gap = stage_gap
    self.enroll_result = enroll_result
    self.calls: queue.Queue[tuple[str, float, object]] = queue.Queue()

  def _record(self, name: str, detail: object = None) -> None:
    self.calls.put((name, time.monotonic(), detail))

  def new_cancel(self) -> FakeToken:
    return FakeToken()

  def open(self) -> None:
    self._record("open")

  def close(self) -> None:
    self._record("close")

  def identify(self, gallery: Sequence[GalleryPrint], cancel: CancelToken, on_finger: Callable[[], None]) -> UUID | None:
    assert isinstance(cancel, FakeToken)
    self._record("identify", tuple(gallery))
    try:
      while True:
        outcome = self.script.popleft() if self.script else self.default
        if outcome is not FINGER:
          break
        on_finger()
      if outcome is BLOCK:
        cancel.event.wait()
        raise ScanCancelled
      if isinstance(outcome, Exception):
        raise outcome
      assert outcome is None or isinstance(outcome, UUID)
      return outcome
    finally:
      self._record("identify_end")

  def enroll(self, cancel: CancelToken, on_stage: Callable[[int, int], None]) -> bytes:
    assert isinstance(cancel, FakeToken)
    self._record("enroll")
    for done in range(1, self.enroll_stages + 1):
      if cancel.event.wait(self.stage_gap):
        raise ScanCancelled
      on_stage(done, self.enroll_stages)
    if self.enroll_result is BLOCK:
      cancel.event.wait()
      raise ScanCancelled
    assert isinstance(self.enroll_result, bytes)
    return self.enroll_result

  def wait_finger_off(self, timeout: float) -> None:
    self._record("finger_off", timeout)

  def next_call(self, name: str, timeout: float = WAIT) -> tuple[float, object]:
    """Wait for the next call named `name`, skipping others."""
    deadline = time.monotonic() + timeout
    while True:
      got, at, detail = self.calls.get(timeout=max(0.0, deadline - time.monotonic()))
      if got == name:
        return at, detail

  def drain(self) -> list[str]:
    names: list[str] = []
    while True:
      try:
        names.append(self.calls.get_nowait()[0])
      except queue.Empty:
        return names


class Harness:
  def __init__(self, reader: FakeReader, timings: ScannerTimings) -> None:
    self.reader = reader
    self.events: queue.Queue[ScannerEvent] = queue.Queue()
    self.fatal: queue.Queue[str] = queue.Queue()
    self.scanner = Scanner(reader, self.events.put, self.fatal.put, timings=timings)

  def event(self) -> ScannerEvent:
    return self.events.get(timeout=WAIT)

  def no_event(self, within: float) -> None:
    with pytest.raises(queue.Empty):
      self.events.get(timeout=within)


A, B = uuid4(), uuid4()
DEFAULT_TIMINGS = ScannerTimings()
GALLERY = [GalleryPrint(A, 1, b"ta"), GalleryPrint(B, 2, b"tb")]


@pytest.fixture
def make() -> Iterator[Callable[..., Harness]]:
  """Build and start a scanner in READY with GALLERY; on teardown stop it and check no threads leak."""
  harnesses: list[Harness] = []

  def build(reader: FakeReader, timings: ScannerTimings = DEFAULT_TIMINGS, mode: ReaderMode = ReaderMode.READY) -> Harness:
    harness = Harness(reader, timings)
    harness.scanner.set_gallery(GALLERY)
    harness.scanner.set_mode(mode)
    harness.scanner.start()
    harnesses.append(harness)
    return harness

  yield build
  for harness in harnesses:
    harness.scanner.stop()
  for thread in threading.enumerate():
    if thread.name == "scanner" or isinstance(thread, threading.Timer):
      thread.join(WAIT)
      assert not thread.is_alive(), thread


def test_ready_posts_match_and_waits_for_finger_off(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([A]))
  assert h.event() == Matched(print_id=A, employee_id=1)
  _, timeout = h.reader.next_call("finger_off")
  assert timeout == pytest.approx(2.0)


def test_rearm_waits_for_finger_off(make: Callable[..., Harness]) -> None:
  # Review Focus 1: a finger left on the reader isn't re-captured before it lifts (or 2 s pass).
  h = make(FakeReader([B, A]))
  order: list[str] = []
  while order.count("identify") < 3:  # noqa: PLR2004
    order.append(h.reader.calls.get(timeout=WAIT)[0])
  assert [name for name in order if name in {"identify", "finger_off"}] == [
    "identify",
    "finger_off",
    "identify",
    "finger_off",
    "identify",
  ]


def test_no_match_posts_no_match(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([None]))
  assert h.event() == NoMatch()


def test_retry_posts_retry_and_pauses(make: Callable[..., Harness]) -> None:
  timings = ScannerTimings(retry_pause=0.2)
  h = make(FakeReader([RetryScan("Center your finger")]), timings)
  first, _ = h.reader.next_call("identify")
  assert h.event() == Retry("Center your finger")
  second, _ = h.reader.next_call("identify")
  assert second - first >= timings.retry_pause


def test_failures_back_off_instead_of_spinning(make: Callable[..., Harness]) -> None:
  h = make(FakeReader(default=ReaderFailure("usb error")), ScannerTimings(backoff_start=0.01, backoff_max=0.04))
  time.sleep(0.5)
  # 0.01 + 0.02 + 0.04 + 0.04 … fits about 14 attempts in 0.5 s; a spin would make thousands.
  assert 1 <= h.reader.drain().count("identify") < 20  # noqa: PLR2004
  h.no_event(0.0)


def test_too_hot_posts_retry_and_pauses_long(make: Callable[..., Harness]) -> None:
  timings = ScannerTimings(too_hot_pause=0.3, retry_pause=0.01)
  h = make(FakeReader([ReaderTooHot()]), timings)
  first, _ = h.reader.next_call("identify")
  assert h.event() == Retry("The reader is warming down, try again in a minute")
  second, _ = h.reader.next_call("identify")
  assert second - first >= timings.too_hot_pause


def test_reader_gone_calls_on_fatal_and_stops(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([ReaderGone("reader unplugged")]))
  assert h.fatal.get(timeout=WAIT) == "reader unplugged"
  scanner_threads = [t for t in threading.enumerate() if t.name == "scanner"]
  for thread in scanner_threads:
    thread.join(WAIT)
  assert not any(t.is_alive() for t in scanner_threads)
  assert h.reader.drain().count("identify") == 1


def test_closed_mode_closes_the_reader(make: Callable[..., Harness]) -> None:
  h = make(FakeReader())
  h.reader.next_call("identify")
  h.scanner.set_mode(ReaderMode.CLOSED)
  h.reader.next_call("close")
  time.sleep(0.2)
  assert "identify" not in h.reader.drain()
  assert h.scanner.mode is ReaderMode.CLOSED


def test_mode_change_cancels_a_waiting_identify(make: Callable[..., Harness]) -> None:
  h = make(FakeReader())
  h.reader.next_call("identify")
  changed = time.monotonic()
  h.scanner.set_mode(ReaderMode.CLOSED)
  ended, _ = h.reader.next_call("identify_end")
  assert ended - changed < 0.5  # noqa: PLR2004


def test_rearms_every_rearm_period(make: Callable[..., Harness]) -> None:
  h = make(FakeReader(), ScannerTimings(rearm_every=0.1))
  time.sleep(0.5)
  assert h.reader.drain().count("identify") >= 3  # noqa: PLR2004
  h.no_event(0.0)


def test_busy_seconds_reports_a_running_call(make: Callable[..., Harness]) -> None:
  h = make(FakeReader())
  h.reader.next_call("identify")
  time.sleep(0.2)
  assert h.scanner.busy_seconds() > 0.15  # noqa: PLR2004
  h.scanner.set_mode(ReaderMode.CLOSED)
  h.reader.next_call("close")
  deadline = time.monotonic() + WAIT
  while h.scanner.busy_seconds() != 0.0 and time.monotonic() < deadline:
    time.sleep(0.01)
  assert h.scanner.busy_seconds() == 0.0


def test_enroll_posts_stages_then_enrolled_and_returns_to_ready(make: Callable[..., Harness]) -> None:
  h = make(FakeReader(enroll_result=b"new-template"), mode=ReaderMode.ENROLL)
  assert [h.event() for _ in range(6)] == [*(EnrollStage(done, 5) for done in range(1, 6)), Enrolled(b"new-template")]
  order: list[str] = []
  while "identify" not in order:
    order.append(h.reader.calls.get(timeout=WAIT)[0])
  # The last stage's finger must lift before identify, or it's matched as the employee just enrolled.
  assert [name for name in order if name in {"enroll", "finger_off", "identify"}] == ["enroll", "finger_off", "identify"]
  assert h.scanner.mode is ReaderMode.READY


def test_enroll_stages_restart_the_idle_timer(make: Callable[..., Harness]) -> None:
  # Stages 0.1 s apart outlast the 0.25 s idle timeout in total, but never between two stages.
  h = make(FakeReader(stage_gap=0.1), ScannerTimings(enroll_idle_timeout=0.25), mode=ReaderMode.ENROLL)
  events = [h.event() for _ in range(6)]
  assert events[-1] == Enrolled(b"template")


def test_enroll_idle_timeout_posts_enroll_failed(make: Callable[..., Harness]) -> None:
  h = make(FakeReader(enroll_stages=0, enroll_result=BLOCK), ScannerTimings(enroll_idle_timeout=0.1), mode=ReaderMode.ENROLL)
  assert h.event() == EnrollFailed("timeout")
  h.reader.next_call("identify")
  assert h.scanner.mode is ReaderMode.READY


def test_enroll_reader_error_posts_enroll_failed() -> None:
  class FailingEnroll(FakeReader):
    @override
    def enroll(self, cancel: CancelToken, on_stage: Callable[[int, int], None]) -> bytes:
      raise ReaderFailure("usb error")

  reader = FailingEnroll()
  harness = Harness(reader, ScannerTimings())
  harness.scanner.set_mode(ReaderMode.ENROLL)
  harness.scanner.start()
  try:
    assert harness.event() == EnrollFailed("error")
    reader.next_call("identify")
  finally:
    harness.scanner.stop()


def test_cancelling_enroll_posts_nothing(make: Callable[..., Harness]) -> None:
  # The user's Cancel during registration is not a failed registration (R16).
  h = make(FakeReader(enroll_stages=0, enroll_result=BLOCK), mode=ReaderMode.ENROLL)
  h.reader.next_call("enroll")
  h.scanner.set_mode(ReaderMode.READY)
  h.reader.next_call("identify")
  h.no_event(0.2)
  assert h.scanner.mode is ReaderMode.READY


def test_set_gallery_restarts_identify_with_the_new_gallery(make: Callable[..., Harness]) -> None:
  h = make(FakeReader())
  _, gallery = h.reader.next_call("identify")
  assert gallery == tuple(GALLERY)
  h.scanner.set_gallery(GALLERY[1:])
  _, gallery = h.reader.next_call("identify")
  assert gallery == (GALLERY[1],)
  h.no_event(0.0)


def test_set_gallery_with_an_equal_gallery_does_not_restart(make: Callable[..., Harness]) -> None:
  # Every echoed punch rebuilds the gallery; an unchanged one must not cut a capture in progress (R6).
  h = make(FakeReader())
  h.reader.next_call("identify")
  h.scanner.set_gallery([GalleryPrint(p.print_id, p.employee_id, p.template) for p in GALLERY])
  with pytest.raises(queue.Empty):
    h.reader.next_call("identify_end", timeout=0.3)


def test_finger_down_is_posted(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([FINGER, A]))
  assert h.event() == FingerDown()
  assert h.event() == Matched(print_id=A, employee_id=1)
