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

# First party imports
from timeclock_system import scanner as scanner_module
from timeclock_system.scanner import (
  CancelToken,
  Enrolled,
  EnrollFailed,
  EnrollStage,
  FingerStatus,
  GalleryPrint,
  Matched,
  NoMatch,
  ReaderFailure,
  ReaderGone,
  ReaderMode,
  ReaderPaused,
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
TICK = 0.02  # a wait on Windows can return up to a timer tick (~16 ms) early by time.monotonic()
BLOCK = object()  # identify/enroll outcome: block until the token is cancelled, then raise ScanCancelled


@dataclass(frozen=True)
class Status:
  """Identify script entry: call on_status with these flags, then take the next entry."""

  present: bool
  waiting: bool


@dataclass(frozen=True)
class Early:
  """Identify script entry: report this result through on_result (libfprint's match callback), then take the next entry."""

  print_id: UUID | None


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
    presses: Sequence[bool] | None = None,
    enroll_statuses: bool = False,
    stage_gap: float = 0.0,
    enroll_result: bytes | object = b"template",
  ) -> None:
    """`presses`: each enroll press's ok, None for all ok; `enroll_statuses`: report a finger down before each press."""
    self.script: deque[Outcome] = deque(script)
    self.default = default
    self.enroll_stages = enroll_stages
    self.presses = presses
    self.enroll_statuses = enroll_statuses
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

  def identify(
    self,
    gallery: Sequence[GalleryPrint],
    cancel: CancelToken,
    on_status: Callable[[FingerStatus], None],
    on_result: Callable[[UUID | None], None],
  ) -> UUID | None:
    assert isinstance(cancel, FakeToken)
    self._record("identify", tuple(gallery))
    try:
      while True:
        outcome = self.script.popleft() if self.script else self.default
        if isinstance(outcome, Status):
          on_status(FingerStatus(outcome.present, outcome.waiting))
        elif isinstance(outcome, Early):
          on_result(outcome.print_id)
        else:
          break
      if outcome is BLOCK:
        cancel.event.wait()
        raise ScanCancelled
      if isinstance(outcome, Exception):
        raise outcome
      assert outcome is None or isinstance(outcome, UUID)
      return outcome
    finally:
      self._record("identify_end")

  def enroll(
    self, cancel: CancelToken, on_stage: Callable[[int, int, bool], None], on_status: Callable[[FingerStatus], None]
  ) -> bytes:
    assert isinstance(cancel, FakeToken)
    self._record("enroll")
    done = 0
    for ok in [True] * self.enroll_stages if self.presses is None else self.presses:
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


PAUSE = ScannerTimings(finger_off_pause=0.3)


def test_ready_pauses_after_a_capture_before_rearming(make: Callable[..., Harness]) -> None:
  # Review Focus 1: libfprint reports no finger after a capture completes, so the 2 s wait (§6.2) is a real pause.
  h = make(FakeReader([A, B]), PAUSE)
  assert h.event() == Matched(print_id=A, employee_id=1)
  first_end, _ = h.reader.next_call("identify_end")
  second_start, _ = h.reader.next_call("identify")
  assert second_start - first_end >= 0.25  # noqa: PLR2004


def test_a_mode_change_ends_the_pause(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([A]), ScannerTimings(finger_off_pause=10.0))
  assert h.event() == Matched(print_id=A, employee_id=1)
  h.scanner.set_mode(ReaderMode.CLOSED)
  h.reader.next_call("close", timeout=1.0)


def test_no_match_posts_no_match(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([None]))
  assert h.event() == NoMatch()


def test_retry_posts_retry_and_pauses(make: Callable[..., Harness]) -> None:
  timings = ScannerTimings(retry_pause=0.2)
  h = make(FakeReader([RetryScan("Center your finger")]), timings)
  first, _ = h.reader.next_call("identify")
  assert h.event() == Retry("Center your finger")
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
  h = make(FakeReader([failure] * 4 + [None] + [failure] * 4, default=BLOCK), timings)
  assert h.event() == NoMatch()
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


def test_finger_status_is_posted_then_cleared_when_the_call_ends(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([Status(present=True, waiting=False), A]))
  assert h.event() == FingerStatus(present=True, waiting=False)
  assert h.event() == FingerStatus(present=False, waiting=False)
  assert h.event() == Matched(print_id=A, employee_id=1)


def test_a_call_that_reports_no_status_clears_nothing(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([A]))
  assert h.event() == Matched(print_id=A, employee_id=1)
  h.no_event(0.3)


def test_an_early_result_is_posted_once(make: Callable[..., Harness]) -> None:
  # The login result arrives while the finger is down; identify returns only after the lift.
  h = make(FakeReader([Status(present=True, waiting=False), Early(A), A]))
  assert h.event() == FingerStatus(present=True, waiting=False)
  assert h.event() == Matched(print_id=A, employee_id=1)
  assert h.event() == FingerStatus(present=False, waiting=False)
  h.no_event(0.3)


def test_an_early_no_match_is_posted_once(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([Early(None), None]))
  assert h.event() == NoMatch()
  h.no_event(0.3)


def test_a_cancel_after_an_early_result_is_no_failure_and_still_pauses(make: Callable[..., Harness]) -> None:
  # Review Focus 1: a finger resting after a match until the call is cancelled isn't read again at once.
  h = make(FakeReader([Early(A), BLOCK]), ScannerTimings(finger_off_pause=0.3))
  h.reader.next_call("identify")
  assert h.event() == Matched(print_id=A, employee_id=1)
  cancelled = time.monotonic()
  h.scanner.set_gallery(GALLERY[1:])  # cancels the call still waiting for the lift
  started, _ = h.reader.next_call("identify")
  assert started - cancelled >= 0.3 - TICK
  h.no_event(0.0)


def test_a_mode_change_after_an_early_result_posts_nothing_more(make: Callable[..., Harness]) -> None:
  h = make(FakeReader([Early(A), BLOCK]))
  assert h.event() == Matched(print_id=A, employee_id=1)
  h.scanner.set_mode(ReaderMode.ENROLL)
  h.reader.next_call("enroll")
  assert [h.event() for _ in range(6)] == [*(EnrollStage(i, 5, ok=True) for i in range(1, 6)), Enrolled(b"template")]


def test_enroll_posts_uncounted_presses(make: Callable[..., Harness]) -> None:
  h = make(FakeReader(enroll_stages=3, presses=[True, False, True, True]), mode=ReaderMode.ENROLL)
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
    FakeReader(enroll_stages=2, presses=[True, False, False, True], stage_gap=0.1),
    ScannerTimings(enroll_idle_timeout=0.25),
    mode=ReaderMode.ENROLL,
  )
  events = [h.event() for _ in range(5)]
  assert events[-1] == Enrolled(b"template")
