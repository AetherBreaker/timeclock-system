"""Tests for the scanner thread, driven by a scripted FakeReader."""

# Standard library imports
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, override
from uuid import UUID, uuid4

# Third party imports
import pytest
from timeclock_schema.enums import Finger, ScanResult

# First party imports
from timeclock_system import scanner as scanner_module
from timeclock_system.registration import OtherPrint, Press
from timeclock_system.scanner import (
  CancelToken,
  Capture,
  Enrolled,
  EnrollFailed,
  EnrollStage,
  FingerStatus,
  GalleryPrint,
  Matched,
  Merged,
  NoMatch,
  Pressed,
  PressRetry,
  ReaderFailure,
  ReaderGone,
  ReaderMode,
  ReaderPaused,
  ReaderTooHot,
  RegisterGallery,
  Retry,
  RetryScan,
  ScanCancelled,
  ScanImage,
  Scanner,
  ScannerEvent,
  ScannerTimings,
  ScoredPrint,
)

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Iterator, Mapping, Sequence

  # First party imports
  from timeclock_system.registration import Minutiae

WAIT = 2.0
TICK = 0.02  # a wait on Windows can return up to a timer tick (~16 ms) early by time.monotonic()
BLOCK = object()  # identify/capture/enroll outcome: block until the token is cancelled, then raise ScanCancelled
ONE_PRESS: tuple[Minutiae, ...] = (((1, 2, 3),),)  # what the fake decodes a template it wasn't given into


@dataclass(frozen=True)
class Status:
  """Identify script entry: call on_status with these flags, then take the next entry."""

  present: bool
  waiting: bool


@dataclass(frozen=True)
class Early:
  """Identify script entry: report this capture through on_result (libfprint's match callback), then take the next entry."""

  capture: Capture


@dataclass(frozen=True)
class Do:
  """Identify script entry: call this, then take the next entry."""

  action: Callable[[], None]


type Outcome = Capture | Exception | object


class FakeToken:
  def __init__(self) -> None:
    self.event = threading.Event()

  def cancel(self) -> None:
    self.event.set()


class FakeReader:
  """Scripted reader; every call is recorded in `calls` as (name, monotonic time, detail), and in `history` as (name, detail)."""

  def __init__(
    self,
    script: Sequence[Outcome] = (),
    *,
    default: Outcome = BLOCK,
    captures: Sequence[Outcome] = (),
    decoded: Mapping[bytes, tuple[Minutiae, ...]] | None = None,
    scores: Mapping[tuple[bytes, int, bytes], tuple[int, ...]] | None = None,
    merge_error: Exception | None = None,
    enroll_stages: int = 5,
    stage_oks: Sequence[bool] | None = None,
    enroll_statuses: bool = False,
    stage_gap: float = 0.0,
    enroll_result: bytes | object = b"template",
  ) -> None:
    """`script` drives identify and `captures` drives capture, each falling back to `default`.

    `decoded`: what `presses` returns per template (else `ONE_PRESS`); `scores`: what `score` returns per (press, index,
    other) (else a 0 per press of `other`); `merge` joins its presses with b"+" unless `merge_error` is set.
    `stage_oks`: each enroll press's ok, None for all ok; `enroll_statuses`: report a finger down before each press.
    """
    self.script: deque[Outcome] = deque(script)
    self.default = default
    self.captures: deque[Outcome] = deque(captures)
    self.decoded = dict(decoded or {})
    self.scores = dict(scores or {})
    self.merge_error = merge_error
    self.enroll_stages = enroll_stages
    self.stage_oks = stage_oks
    self.enroll_statuses = enroll_statuses
    self.stage_gap = stage_gap
    self.enroll_result = enroll_result
    self.calls: queue.Queue[tuple[str, float, object]] = queue.Queue()
    self.history: list[tuple[str, object]] = []

  def _record(self, name: str, detail: object = None) -> None:
    self.history.append((name, detail))
    self.calls.put((name, time.monotonic(), detail))

  def _play(
    self,
    script: deque[Outcome],
    cancel: CancelToken,
    on_status: Callable[[FingerStatus], None],
    on_result: Callable[[Capture], None] | None = None,
  ) -> object:
    """Run `script`'s entries up to its next outcome; return it, or raise it (BLOCK: once cancelled)."""
    assert isinstance(cancel, FakeToken)
    while True:
      outcome = script.popleft() if script else self.default
      if isinstance(outcome, Status):
        on_status(FingerStatus(outcome.present, outcome.waiting))
      elif isinstance(outcome, Do):
        outcome.action()
      elif isinstance(outcome, Early):
        assert on_result is not None, "Early is an identify entry"
        on_result(outcome.capture)
      else:
        break
    if outcome is BLOCK:
      cancel.event.wait()
      raise ScanCancelled
    if isinstance(outcome, Exception):
      raise outcome
    return outcome

  def new_cancel(self) -> FakeToken:
    return FakeToken()

  def open(self) -> None:
    self._record("open")

  def close(self) -> None:
    self._record("close")

  def identify(
    self,
    gallery: Sequence[GalleryPrint],
    cancel: CancelToken,
    on_status: Callable[[FingerStatus], None],
    on_result: Callable[[Capture], None],
  ) -> Capture:
    self._record("identify", tuple(gallery))
    try:
      outcome = self._play(self.script, cancel, on_status, on_result)
      assert isinstance(outcome, Capture)
      return outcome
    finally:
      self._record("identify_end")

  def capture(self, cancel: CancelToken, on_status: Callable[[FingerStatus], None]) -> bytes:
    self._record("capture")
    try:
      outcome = self._play(self.captures, cancel, on_status)
      assert isinstance(outcome, bytes)
      return outcome
    finally:
      self._record("capture_end")

  def presses(self, template: bytes) -> tuple[Minutiae, ...]:
    self._record("presses", template)
    return self.decoded.get(template, ONE_PRESS)

  def score(self, press: bytes, index: int, other: bytes) -> tuple[int, ...]:
    self._record("score", (press, index, other))
    return self.scores.get((press, index, other), (0,) * len(self.decoded.get(other, ONE_PRESS)))

  def merge(self, presses: Sequence[bytes]) -> bytes:
    self._record("merge", tuple(presses))
    if self.merge_error is not None:
      raise self.merge_error
    return b"+".join(presses)

  def enroll(
    self, cancel: CancelToken, on_stage: Callable[[int, int, bool], None], on_status: Callable[[FingerStatus], None]
  ) -> bytes:
    assert isinstance(cancel, FakeToken)
    self._record("enroll")
    done = 0
    for ok in [True] * self.enroll_stages if self.stage_oks is None else self.stage_oks:
      if done == self.enroll_stages:
        break
      if cancel.event.wait(self.stage_gap):
        raise ScanCancelled
      if self.enroll_statuses:
        on_status(FingerStatus(present=True, waiting=False))
      done += ok
      on_stage(done, self.enroll_stages, ok)
    if self.enroll_result is BLOCK:
      cancel.event.wait()
      raise ScanCancelled
    assert isinstance(self.enroll_result, bytes)
    return self.enroll_result

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


A, A2, B = uuid4(), uuid4(), uuid4()
DEFAULT_TIMINGS = ScannerTimings()
GALLERY = [
  GalleryPrint(A, 1, b"ta", Finger.RIGHT_THUMB),
  GalleryPrint(A2, 1, b"ta2", Finger.LEFT_INDEX),
  GalleryPrint(B, 2, b"tb", Finger.RIGHT_THUMB),
]
MARGIN = 15
PROBE = b"probe-bytes"
IMAGE = ScanImage(b"pixels", 2, 3)


def cap(match: UUID | None, *scores: tuple[UUID, int]) -> Capture:
  """A capture with a probe and image; scores in the order given."""
  return Capture(match, scores, PROBE, IMAGE)


HIT_A = cap(A, (A, 80), (B, 20))
HIT_B = cap(B, (B, 80), (A, 20))
MISS = cap(None, (A, 30), (B, 25))


def matched(event: ScannerEvent, print_id: UUID) -> Matched:
  assert isinstance(event, Matched)
  assert event.print_id == print_id
  return event


def no_match(event: ScannerEvent) -> NoMatch:
  assert isinstance(event, NoMatch)
  return event


@pytest.fixture
def make() -> Iterator[Callable[..., Harness]]:
  """Build and start a scanner in `mode` (REGISTER for `registrant` if given) with GALLERY; on teardown stop it and check no threads leak."""
  harnesses: list[Harness] = []

  def build(
    reader: FakeReader,
    timings: ScannerTimings = DEFAULT_TIMINGS,
    mode: ReaderMode = ReaderMode.READY,
    margin: int = MARGIN,
    registrant: int | None = None,
  ) -> Harness:
    harness = Harness(reader, timings)
    harness.scanner.set_gallery(GALLERY, margin)
    if registrant is None:
      harness.scanner.set_mode(mode)
    else:
      harness.scanner.register(registrant)
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


PAUSE = ScannerTimings(finger_off_pause=0.3)


def test_ready_pauses_after_a_capture_before_rearming(make: Callable[..., Harness]) -> None:
  # Review Focus 1: libfprint reports no finger after a capture completes, so the 2 s wait (§6.2) is a real pause.
  h = make(FakeReader([HIT_A, HIT_B]), PAUSE)
  matched(h.event(), A)
  first_end, _ = h.reader.next_call("identify_end")
  second_start, _ = h.reader.next_call("identify")
  assert second_start - first_end >= 0.25  # noqa: PLR2004


def test_a_mode_change_ends_the_pause(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([HIT_A]), ScannerTimings(finger_off_pause=10.0))
  matched(h.event(), A)
  h.scanner.set_mode(ReaderMode.CLOSED)
  h.reader.next_call("close", timeout=1.0)


def test_no_match_posts_no_match(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([MISS]))
  no_match(h.event())


def test_retry_posts_retry_and_pauses(make: Callable[..., Harness]) -> None:
  timings = ScannerTimings(retry_pause=0.2)
  h = make(FakeReader([RetryScan("Center your finger")]), timings)
  first, _ = h.reader.next_call("identify")
  retry = h.event()
  assert isinstance(retry, Retry)
  assert retry.message == "Center your finger"
  second, _ = h.reader.next_call("identify")
  assert second - first >= timings.retry_pause - TICK


def test_failures_back_off_instead_of_spinning(make: Callable[..., Harness]) -> None:
  h = make(FakeReader(default=ReaderFailure("usb error")), ScannerTimings(backoff_start=0.01, backoff_max=0.04))
  time.sleep(0.5)
  # 0.01 + 0.02 + 0.04 + 0.04 … fits about 14 attempts in 0.5 s; a spin would make thousands.
  assert 1 <= h.reader.drain().count("identify") < 20  # noqa: PLR2004
  h.no_event(0.0)


def test_a_failure_streak_is_logged_once(make: Callable[..., Harness], caplog: pytest.LogCaptureFixture) -> None:
  caplog.set_level(logging.WARNING, logger="timeclock_system.scanner")
  h = make(FakeReader(default=ReaderFailure("usb error")), ScannerTimings(backoff_start=0.01, backoff_max=0.04))
  time.sleep(0.3)
  h.scanner.stop()
  warnings = [r.getMessage() for r in caplog.records if r.name == "timeclock_system.scanner"]
  assert len(warnings) == 1
  assert "usb error" in warnings[0]


def test_a_reader_failing_past_the_limit_is_fatal(make: Callable[..., Harness]) -> None:
  # §2's anti-goal: a reader that silently stops working while the screen still looks ready.
  h = make(FakeReader(default=ReaderFailure("usb error")), ScannerTimings(backoff_start=0.01, backoff_max=0.04, failure_limit=0.2))
  assert "usb error" in h.fatal.get(timeout=WAIT)


def test_a_capture_ends_the_failure_streak(make: Callable[..., Harness]) -> None:
  failure = ReaderFailure("usb error")
  timings = ScannerTimings(backoff_start=0.05, backoff_max=0.05, failure_limit=0.25, finger_off_pause=0.01)
  h = make(FakeReader([failure] * 4 + [MISS] + [failure] * 4, default=BLOCK), timings)
  no_match(h.event())
  # Each streak spans about 0.15 s, under the limit; counted as one streak, the failures would span about 0.4 s.
  with pytest.raises(queue.Empty):
    h.fatal.get(timeout=0.5)


def test_too_hot_posts_paused_alerts_and_pauses_long(make: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch) -> None:
  alerts: list[str] = []
  monkeypatch.setattr(scanner_module, "alert", lambda reason, *_a, **_k: alerts.append(reason))
  timings = ScannerTimings(too_hot_pause=0.3, retry_pause=0.01)
  h = make(FakeReader([ReaderTooHot()]), timings)
  first, _ = h.reader.next_call("identify")
  assert h.event() == ReaderPaused(timings.too_hot_pause)
  assert alerts == ["Fingerprint reader paused itself (too hot)"]  # the patch should make this impossible (§7)
  second, _ = h.reader.next_call("identify")
  assert second - first >= timings.too_hot_pause - TICK


def test_reader_gone_calls_on_fatal_and_stops(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([ReaderGone("reader unplugged")]))
  assert h.fatal.get(timeout=WAIT) == "reader unplugged"
  scanner_threads = [t for t in threading.enumerate() if t.name == "scanner"]
  for thread in scanner_threads:
    thread.join(WAIT)
  assert not any(t.is_alive() for t in scanner_threads)
  assert h.reader.drain().count("identify") == 1


def test_a_code_bug_reaches_threading_excepthook_not_on_fatal(make: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch) -> None:
  # aeth_ext's threading.excepthook alerts with the traceback; on_fatal would report a lost reader without one.
  hooked: queue.Queue[BaseException | None] = queue.Queue()
  monkeypatch.setattr(threading, "excepthook", lambda args: hooked.put(args.exc_value))
  h = make(FakeReader([RuntimeError("a bug")]))
  assert isinstance(hooked.get(timeout=WAIT), RuntimeError)
  assert h.fatal.empty()


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
  h = make(FakeReader(enroll_result=b"new-template"), PAUSE, mode=ReaderMode.ENROLL)
  assert [h.event() for _ in range(6)] == [*(EnrollStage(done, 5, ok=True) for done in range(1, 6)), Enrolled(b"new-template")]
  enrolled_at = time.monotonic()
  # The last stage's finger may still be down when enroll returns; identifying at once would match the new employee.
  identify_at, _ = h.reader.next_call("identify")
  assert identify_at - enrolled_at >= 0.25  # noqa: PLR2004
  assert h.scanner.mode is ReaderMode.READY


def test_enroll_stages_restart_the_idle_timer(make: Callable[..., Harness]) -> None:
  # Stages 0.1 s apart outlast the 0.25 s idle timeout in total, but never between two stages.
  h = make(FakeReader(stage_gap=0.1), ScannerTimings(enroll_idle_timeout=0.25), mode=ReaderMode.ENROLL)
  events = [h.event() for _ in range(6)]
  assert events[-1] == Enrolled(b"template")


def test_enroll_idle_timeout_posts_enroll_failed(make: Callable[..., Harness]) -> None:
  h = make(
    FakeReader(enroll_stages=0, enroll_result=BLOCK),
    ScannerTimings(enroll_idle_timeout=0.1, finger_off_pause=0.01),
    mode=ReaderMode.ENROLL,
  )
  assert h.event() == EnrollFailed("timeout")
  h.reader.next_call("identify")
  assert h.scanner.mode is ReaderMode.READY


def test_enroll_reader_error_posts_enroll_failed() -> None:
  class FailingEnroll(FakeReader):
    @override
    def enroll(
      self, cancel: CancelToken, on_stage: Callable[[int, int, bool], None], on_status: Callable[[FingerStatus], None]
    ) -> bytes:
      raise ReaderFailure("usb error")

  reader = FailingEnroll()
  harness = Harness(reader, ScannerTimings(finger_off_pause=0.01))
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
  h.scanner.set_gallery(GALLERY[1:], MARGIN)
  _, gallery = h.reader.next_call("identify")
  assert gallery == tuple(GALLERY[1:])
  h.no_event(0.0)


def test_set_gallery_with_an_equal_gallery_does_not_restart(make: Callable[..., Harness]) -> None:
  # Every echoed punch rebuilds the gallery; an unchanged one must not cut a capture in progress (R6).
  h = make(FakeReader())
  h.reader.next_call("identify")
  h.scanner.set_gallery([GalleryPrint(p.print_id, p.employee_id, p.template, p.finger) for p in GALLERY], MARGIN)
  with pytest.raises(queue.Empty):
    h.reader.next_call("identify_end", timeout=0.3)


def test_set_gallery_with_a_reordered_gallery_applies_from_the_next_identify(make: Callable[..., Harness]) -> None:
  # Every punch moves its employee first. A restart for that alone can land while libfprint is still starting the reader,
  # where the cancel is dropped and the call waits for the next press (main-warehouse, 2026-11-05).
  h = make(FakeReader(), ScannerTimings(rearm_every=0.5))
  h.reader.next_call("identify")
  h.scanner.set_gallery(GALLERY[::-1], MARGIN)
  with pytest.raises(queue.Empty):
    h.reader.next_call("identify_end", timeout=0.3)
  _, gallery = h.reader.next_call("identify")  # after the re-arm
  assert gallery == tuple(GALLERY[::-1])


def test_finger_status_is_posted_then_cleared_when_the_call_ends(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([Status(present=True, waiting=False), HIT_A]))
  assert h.event() == FingerStatus(present=True, waiting=False)
  assert h.event() == FingerStatus(present=False, waiting=False)
  matched(h.event(), A)


def test_a_call_that_reports_no_status_clears_nothing(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([HIT_A]))
  matched(h.event(), A)
  h.no_event(0.3)


def test_an_early_result_is_posted_once(make: Callable[..., Harness]) -> None:
  # The login result arrives while the finger is down; identify returns only after the lift.
  h = make(FakeReader([Status(present=True, waiting=False), Early(HIT_A), HIT_A]))
  assert h.event() == FingerStatus(present=True, waiting=False)
  matched(h.event(), A)
  assert h.event() == FingerStatus(present=False, waiting=False)
  h.no_event(0.3)


def test_an_early_no_match_is_posted_once(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([Early(MISS), MISS]))
  no_match(h.event())
  h.no_event(0.3)


def test_a_cancel_after_an_early_result_is_no_failure_and_still_pauses(make: Callable[..., Harness]) -> None:
  # Review Focus 1: a finger resting after a match until the call is cancelled isn't read again at once.
  h = make(FakeReader([Early(HIT_A), BLOCK]), ScannerTimings(finger_off_pause=0.3))
  h.reader.next_call("identify")
  matched(h.event(), A)
  cancelled = time.monotonic()
  h.scanner.set_gallery(GALLERY[1:], MARGIN)  # cancels the call still waiting for the lift
  started, _ = h.reader.next_call("identify")
  assert started - cancelled >= 0.3 - TICK
  h.no_event(0.0)


def test_a_mode_change_after_an_early_result_posts_nothing_more(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([Early(HIT_A), BLOCK]), PAUSE)
  matched(h.event(), A)
  h.scanner.set_mode(ReaderMode.ENROLL)
  h.reader.next_call("enroll")
  assert [h.event() for _ in range(6)] == [*(EnrollStage(i, 5, ok=True) for i in range(1, 6)), Enrolled(b"template")]


def test_a_mode_change_after_an_early_result_waits_out_the_pause(make: Callable[..., Harness]) -> None:
  # An admin's approving finger may still be down: enrolling at once would read it as the new print's first press.
  h = make(FakeReader([Early(HIT_A), BLOCK]), PAUSE)
  matched(h.event(), A)
  changed = time.monotonic()
  h.scanner.set_mode(ReaderMode.ENROLL)
  started, _ = h.reader.next_call("enroll")
  assert started - changed >= PAUSE.finger_off_pause - TICK


def test_stop_ends_the_pause_after_an_early_result(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([Early(HIT_A), BLOCK]), ScannerTimings(finger_off_pause=10.0))
  matched(h.event(), A)
  h.scanner.set_mode(ReaderMode.CLOSED)  # cancels the call; the pause that follows ignores mode changes
  [thread] = [t for t in threading.enumerate() if t.name == "scanner"]
  h.scanner.stop()
  thread.join(1.0)
  assert not thread.is_alive()


def test_enroll_posts_uncounted_presses(make: Callable[..., Harness]) -> None:
  h = make(FakeReader(enroll_stages=3, stage_oks=[True, False, True, True]), mode=ReaderMode.ENROLL)
  assert [h.event() for _ in range(4)] == [
    EnrollStage(1, 3, ok=True),
    EnrollStage(1, 3, ok=False),
    EnrollStage(2, 3, ok=True),
    EnrollStage(3, 3, ok=True),
  ]
  assert h.event() == Enrolled(b"template")


def test_enroll_posts_finger_status(make: Callable[..., Harness]) -> None:
  h = make(FakeReader(enroll_stages=1, enroll_statuses=True), mode=ReaderMode.ENROLL)
  assert h.event() == FingerStatus(present=True, waiting=False)
  assert h.event() == EnrollStage(1, 1, ok=True)
  assert h.event() == FingerStatus(present=False, waiting=False)
  assert h.event() == Enrolled(b"template")


def test_an_uncounted_press_restarts_the_idle_timer(make: Callable[..., Harness]) -> None:
  # Presses 0.1 s apart outlast the 0.25 s idle timeout in total; uncounted ones keep it alive like counted ones.
  h = make(
    FakeReader(enroll_stages=2, stage_oks=[True, False, False, True], stage_gap=0.1),
    ScannerTimings(enroll_idle_timeout=0.25),
    mode=ReaderMode.ENROLL,
  )
  events = [h.event() for _ in range(5)]
  assert events[-1] == Enrolled(b"template")


FAST = ScannerTimings(finger_off_pause=0.01, retry_pause=0.01)
TB: tuple[Minutiae, ...] = (((1, 1, 1),), ((2, 2, 2),))  # B's print, two presses
B_OWN = {(b"tb", 0, b"tb"): (99, 30), (b"tb", 1, b"tb"): (45, 99)}  # B's presses against each other
B_GALLERY = RegisterGallery((OtherPrint(B, 2, Finger.RIGHT_THUMB, TB, ((-1, 45), (45, -1))),))
M0, M1, M2 = ((10, 10, 0),), ((20, 20, 0),), ((30, 30, 0),)


def gallery_of(event: ScannerEvent) -> RegisterGallery:
  assert isinstance(event, RegisterGallery)
  return event


def test_register_posts_gallery_without_registrant(make: Callable[..., Harness]) -> None:
  # Employee 1's own prints (A, A2) never count as resemblance; B's agreement keeps the higher direction, -1 on itself.
  h = make(FakeReader(decoded={b"tb": TB}, scores=B_OWN), FAST, registrant=1)
  assert h.event() == B_GALLERY
  assert [d for name, d in h.reader.history if name == "presses"] == [b"tb"]


def test_register_decodes_each_gallery_print_once(make: Callable[..., Harness]) -> None:
  h = make(FakeReader(decoded={b"tb": TB, b"tb-new": TB}, scores=B_OWN), FAST, registrant=1)
  assert h.event() == B_GALLERY
  h.scanner.set_mode(ReaderMode.READY)
  h.scanner.register(1)
  assert h.event() == B_GALLERY
  # A print whose template changed is decoded again.
  h.scanner.set_gallery([*GALLERY[:2], GalleryPrint(B, 2, b"tb-new", Finger.RIGHT_THUMB)], MARGIN)
  h.scanner.set_mode(ReaderMode.READY)
  h.scanner.register(1)
  gallery_of(h.event())
  assert [d for name, d in h.reader.history if name == "presses"] == [b"tb", b"tb-new"]


def test_register_posts_pressed_with_scores(make: Callable[..., Harness]) -> None:
  scores = {
    **B_OWN,
    (b"p0", 0, b"tb"): (12, 34),
    (b"p1", 0, b"tb"): (5, 6),
    (b"p1", 0, b"p0"): (30,),
    (b"p0", 0, b"p1"): (45,),  # the earlier press's direction scores higher
    (b"p2", 0, b"p0"): (50,),  # the new press's direction scores higher
    (b"p0", 0, b"p2"): (20,),
  }
  reader = FakeReader(captures=[b"p0", b"p1", b"p2"], decoded={b"tb": TB, b"p0": (M0,), b"p1": (M1,), b"p2": (M2,)}, scores=scores)
  h = make(reader, FAST, registrant=1)
  assert h.event() == B_GALLERY
  assert h.event() == Pressed(Press(0, M0, b"p0", {}, {B: (12, 34)}))
  assert h.event() == Pressed(Press(1, M1, b"p1", {0: 45}, {B: (5, 6)}))
  assert h.event() == Pressed(Press(2, M2, b"p2", {0: 50, 1: 0}, {B: (0, 0)}))


def test_register_retry_posts_press_retry_and_restarts_the_idle_timer(make: Callable[..., Harness]) -> None:
  # Each capture takes 0.15 s, under the 0.25 s idle timeout, though both together outlast it; the retry isn't counted.
  slow = Do(lambda: time.sleep(0.15))
  timings = ScannerTimings(enroll_idle_timeout=0.25, finger_off_pause=0.01, retry_pause=0.01)
  h = make(FakeReader(captures=[slow, RetryScan("Remove finger"), slow, b"p0"]), timings, registrant=1)
  gallery_of(h.event())
  assert h.event() == PressRetry()
  pressed = h.event()
  assert isinstance(pressed, Pressed)
  assert pressed.press.press_id == 0


def test_register_idle_timeout(make: Callable[..., Harness]) -> None:
  h = make(FakeReader(), ScannerTimings(enroll_idle_timeout=0.1, finger_off_pause=0.01), registrant=1)
  gallery_of(h.event())
  assert h.event() == EnrollFailed("timeout")
  h.reader.next_call("identify")
  assert h.scanner.mode is ReaderMode.READY


@pytest.mark.parametrize("error", [ReaderFailure("usb error"), ReaderTooHot()])
def test_register_reader_error_posts_enroll_failed(make: Callable[..., Harness], error: Exception) -> None:
  h = make(FakeReader(captures=[error]), FAST, registrant=1)
  gallery_of(h.event())
  assert h.event() == EnrollFailed("error")
  h.reader.next_call("identify")
  assert h.scanner.mode is ReaderMode.READY


def test_set_mode_ends_register(make: Callable[..., Harness]) -> None:
  # The user's Cancel is not a failed registration (R16); coming back starts a new session.
  h = make(FakeReader(captures=[b"p0", Status(present=True, waiting=False), BLOCK, b"p1"]), FAST, registrant=1)
  gallery_of(h.event())
  first = h.event()
  assert isinstance(first, Pressed)
  assert h.event() == FingerStatus(present=True, waiting=False)
  h.scanner.set_mode(ReaderMode.READY)
  assert h.event() == FingerStatus(present=False, waiting=False)  # a cancelled capture may never report the lift
  h.reader.next_call("identify")
  h.no_event(0.2)
  h.scanner.register(1)
  gallery_of(h.event())
  assert h.event() == Pressed(Press(0, ONE_PRESS[0], b"p1", {}, {B: (0,)}))


def test_set_mode_rejects_register(make: Callable[..., Harness]) -> None:
  # Without a registrant, their own prints would count as other people's; and the running session must go on.
  h = make(FakeReader(), registrant=1)
  gallery_of(h.event())
  h.reader.next_call("capture")
  with pytest.raises(ValueError, match="register"):
    h.scanner.set_mode(ReaderMode.REGISTER)
  with pytest.raises(queue.Empty):
    h.reader.next_call("capture_end", timeout=0.3)
  assert h.scanner.mode is ReaderMode.REGISTER
  h.no_event(0.0)


def test_register_again_for_the_same_registrant_changes_nothing(make: Callable[..., Harness]) -> None:
  h = make(FakeReader(), registrant=1)
  gallery_of(h.event())
  h.reader.next_call("capture")
  h.scanner.register(1)
  with pytest.raises(queue.Empty):
    h.reader.next_call("capture_end", timeout=0.3)
  h.no_event(0.0)  # no new session's gallery


def test_merge_posts_merged(make: Callable[..., Harness]) -> None:
  h = make(FakeReader(), FAST, registrant=1)
  gallery_of(h.event())
  h.reader.next_call("capture")
  h.scanner.merge([b"p0", b"p1"])
  h.reader.next_call("capture_end")  # the waiting capture is cancelled, not waited out
  assert h.event() == Merged(b"p0+p1")
  # The session goes on until the mode changes: no new gallery.
  h.reader.next_call("capture")
  assert h.scanner.mode is ReaderMode.REGISTER
  h.no_event(0.2)


@pytest.mark.parametrize("mode", [ReaderMode.CLOSED, ReaderMode.READY])
def test_merge_runs_in_any_mode(make: Callable[..., Harness], mode: ReaderMode) -> None:
  h = make(FakeReader(), mode=mode)
  h.scanner.merge([b"p0"])
  assert h.event() == Merged(b"p0")
  assert h.scanner.mode is mode
  if mode is ReaderMode.CLOSED:
    assert "open" not in [name for name, _ in h.reader.history]  # merging needs no device


def test_a_failed_merge_posts_enroll_failed(make: Callable[..., Harness]) -> None:
  h = make(FakeReader(merge_error=ReaderFailure("bad press")), FAST, registrant=1)
  gallery_of(h.event())
  h.scanner.merge([b"p0"])
  assert h.event() == EnrollFailed("error")


def scan_of(event: ScannerEvent):
  assert isinstance(event, Matched | NoMatch | Retry)
  assert event.scan is not None
  return event.scan


def test_match_posts_its_scan_report(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([cap(A, (A, 80), (B, 20))]))
  event = matched(h.event(), A)
  assert event.employee_id == 1
  scan = scan_of(event)
  assert scan.result is ScanResult.MATCHED
  assert scan.best == ScoredPrint(A, 1, Finger.RIGHT_THUMB, 80)
  assert scan.other is not None
  assert (scan.other.print_id, scan.other.score) == (B, 20)
  assert scan.margin == MARGIN
  assert scan.scores == ((A, 80), (B, 20))
  assert (scan.probe, scan.image) == (PROBE, IMAGE)


def test_too_close_posts_no_match(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([cap(A, (A, 50), (B, 40))]))
  scan = scan_of(no_match(h.event()))
  assert scan.result is ScanResult.TOO_CLOSE
  assert scan.best is not None
  assert scan.other is not None
  assert (scan.best.print_id, scan.other.print_id) == (A, B)


def test_own_other_finger_never_counts(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([cap(A, (A, 60), (A2, 58), (B, 10))]))
  scan = scan_of(matched(h.event(), A))
  assert scan.other is not None
  assert scan.other.print_id == B


def test_tie_with_another_person_is_too_close(make: Callable[..., Harness]) -> None:
  tie = cap(A, (A, 45), (B, 45))
  assert scan_of(no_match(make(FakeReader([tie])).event())).result is ScanResult.TOO_CLOSE
  assert scan_of(matched(make(FakeReader([tie]), margin=0).event(), A)).result is ScanResult.MATCHED


def test_no_match_reports_the_best_print(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([cap(None, (A, 30), (B, 25))]))
  scan = scan_of(no_match(h.event()))
  assert scan.result is ScanResult.NO_MATCH
  assert scan.best is not None
  assert scan.other is not None
  assert (scan.best.print_id, scan.best.score) == (A, 30)
  assert (scan.other.print_id, scan.other.score) == (B, 25)


def test_empty_gallery_reports_no_prints(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([cap(None)]))
  scan = scan_of(no_match(h.event()))
  assert (scan.result, scan.best, scan.other, scan.scores) == (ScanResult.NO_MATCH, None, None, ())


def test_an_unscored_print_stays_out_of_best_and_other(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([cap(None, (A, -1), (B, 25))]))
  scan = scan_of(no_match(h.event()))
  assert scan.best is not None
  assert scan.best.print_id == B
  assert scan.other is None
  assert scan.scores == ((A, -1), (B, 25))


def test_retry_posts_a_retry_report(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([RetryScan("Remove finger")]))
  event = h.event()
  assert isinstance(event, Retry)
  assert event.message == "Remove finger"
  scan = scan_of(event)
  assert scan.result is ScanResult.RETRY
  assert scan.retry_message == "Remove finger"
  assert scan.best is None
  assert scan.margin == MARGIN


def test_new_margin_applies_without_restarting(make: Callable[..., Harness]) -> None:
  close = cap(A, (A, 41), (B, 40))
  holder: list[Harness] = []
  h = make(FakeReader([Do(lambda: holder[0].scanner.set_gallery(GALLERY, 0)), close, close]), ScannerTimings(finger_off_pause=0.01))
  holder.append(h)
  # The call running when the margin changed finishes with the margin it started with, so it wasn't restarted.
  assert scan_of(no_match(h.event())).margin == MARGIN
  assert scan_of(matched(h.event(), A)).margin == 0


def test_an_early_report_is_the_one_the_return_would_have_made(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([Early(cap(A, (A, 50), (B, 40))), cap(A, (A, 50), (B, 40))]))
  assert scan_of(no_match(h.event())).result is ScanResult.TOO_CLOSE
  h.no_event(0.3)
