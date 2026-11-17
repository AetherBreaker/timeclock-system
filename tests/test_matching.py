# Standard library imports
import gc
import threading
import time
import weakref
from typing import TYPE_CHECKING
from uuid import uuid4

# Third party imports
import pytest
from timeclock_schema.enums import Finger

# First party imports
from timeclock_system.matching import GalleryPrint, ScoringSession
from timeclock_system.registration import OtherPrint, Press
from timeclock_system.scanner import ReaderFailure, ScanCancelled

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Iterator

BAR = 40
WAIT = 3.0  # seconds a test waits for the threads before failing


class Token:
  def __init__(self) -> None:
    self.cancelled = threading.Event()

  def cancel(self) -> None:
    self.cancelled.set()


class FakeMatching:
  """Scores from `table` by (press, other) (one score per press of `other`); `behave` decides a call's fate first.

  `behave(press, other)` returns None (score at once), a float (sleep that long), "block" (until cancelled, then raise
  ScanCancelled), "ignore" (block on `release`, ignoring the cancel), or "fail" (raise ReaderFailure).
  """

  def __init__(self, table: dict[tuple[bytes, bytes], tuple[int, ...]], presses: dict[bytes, int] | None = None) -> None:
    self.table = table
    self.n_presses = presses or {}
    self.behave: Callable[[bytes, bytes], object] = lambda _p, _o: None
    self.release = threading.Event()
    self.calls: list[tuple[bytes, int, bytes]] = []
    self.threads: set[int] = set()
    self.alive_peak = 0
    self._lock = threading.Lock()

  def new_cancel(self) -> Token:
    return Token()

  def presses(self, template: bytes) -> tuple[tuple[tuple[int, int, int], ...], ...]:
    return tuple(((i, i, i),) for i in range(self.n_presses.get(template, 1)))

  def score(self, press: bytes, index: int, other: bytes, cancel: Token) -> tuple[int, ...]:
    with self._lock:
      self.calls.append((press, index, other))
      self.threads.add(threading.get_ident())
      self.alive_peak = max(self.alive_peak, sum(t.name.startswith("matcher") for t in threading.enumerate()))
    fate = self.behave(press, other)
    if fate == "fail":
      raise ReaderFailure("matcher hiccup")
    if fate == "block":
      cancel.cancelled.wait()
      raise ScanCancelled
    if fate == "ignore":
      self.release.wait()
      raise ScanCancelled
    if isinstance(fate, float):
      time.sleep(fate)
    if cancel.cancelled.is_set():
      raise ScanCancelled
    return self.table.get((press, other), (0,) * self.n_presses.get(other, 5))


def gallery(n: int) -> list[GalleryPrint]:
  return [GalleryPrint(uuid4(), 100 + k, f"t{k}".encode(), Finger.RIGHT_THUMB) for k in range(n)]


class Recorder:
  def __init__(self) -> None:
    self.delivered: list[Press] = []
    self.failed = 0
    self.cond = threading.Condition()

  def deliver(self, press: Press) -> None:
    with self.cond:
      self.delivered.append(press)
      self.cond.notify_all()

  def fail(self) -> None:
    with self.cond:
      self.failed += 1
      self.cond.notify_all()

  def wait(self, predicate: Callable[[], bool]) -> None:
    with self.cond:
      assert self.cond.wait_for(predicate, timeout=WAIT), "the matcher threads didn't get there in time"


def eventually(predicate: Callable[[], bool]) -> None:
  deadline = time.monotonic() + WAIT
  while not predicate():
    assert time.monotonic() < deadline, "never happened"
    time.sleep(0.01)


def no_matcher_threads() -> bool:
  return not any(t.name.startswith("matcher") for t in threading.enumerate())


def session(matching: FakeMatching, others: list[GalleryPrint], rec: Recorder, **kwargs: object) -> ScoringSession:
  return ScoringSession(matching, others, BAR, rec.deliver, rec.fail, **kwargs)  # pyright: ignore[reportArgumentType]  # kwargs are the keyword options


@pytest.fixture
def closing() -> Iterator[list[ScoringSession]]:
  made: list[ScoringSession] = []
  yield made
  for s in made:
    s.close()


def test_presses_are_delivered_in_capture_order(closing: list[ScoringSession]) -> None:
  others = gallery(2)
  m, rec = FakeMatching({}), Recorder()
  m.behave = lambda press, _o: 0.3 if press == b"p0" else None  # press 1 finishes first
  s = session(m, others, rec, threads=4)
  closing.append(s)
  s.add(0, b"p0")
  s.add(1, b"p1")
  rec.wait(lambda: len(rec.delivered) == 2)  # noqa: PLR2004
  assert [p.press_id for p in rec.delivered] == [0, 1]
  assert s.outstanding == 0


def test_a_press_scores_against_the_gallery_and_earlier_presses(closing: list[ScoringSession]) -> None:
  a, b = gallery(2)
  m = FakeMatching({(b"p1", a.template): (10, 11, 12, 13, 14), (b"p1", b"p0"): (30,), (b"p0", b"p1"): (35,)})
  rec = Recorder()
  s = session(m, [a, b], rec, threads=3)
  closing.append(s)
  s.add(0, b"p0")
  s.add(1, b"p1")
  rec.wait(lambda: len(rec.delivered) == 2)  # noqa: PLR2004
  first, second = rec.delivered
  assert first.vs_session == {}
  assert second.vs_session == {0: 35}  # the higher of both directions
  assert second.vs_gallery == {a.print_id: (10, 11, 12, 13, 14), b.print_id: (0, 0, 0, 0, 0)}
  assert (second.probe, second.minutiae) == (b"p1", ((0, 0, 0),))


def test_resembled_prints_get_their_agreement_once(closing: list[ScoringSession]) -> None:
  x, y = gallery(2)
  like_x = (BAR, 0, 0, 0, 0)
  table = {(b"p0", x.template): like_x, (b"p1", x.template): like_x, (b"p0", y.template): (BAR - 1,) * 5}
  table |= {(x.template, x.template): (50, 50, 50, 50, 50)}
  m, rec = FakeMatching(table, presses={x.template: 5, y.template: 5}), Recorder()
  s = session(m, [x, y], rec, threads=2)
  closing.append(s)
  s.add(0, b"p0")
  s.add(1, b"p1")
  rec.wait(lambda: len(rec.delivered) == 2)  # noqa: PLR2004
  own = [c for c in m.calls if c[0] == x.template and c[2] == x.template]
  assert sorted(index for _, index, _ in own) == [0, 1, 2, 3, 4]  # once, though two presses resemble x
  assert set(rec.delivered[0].resembled) == {x.print_id}
  other = rec.delivered[0].resembled[x.print_id]
  assert isinstance(other, OtherPrint)
  assert (other.employee_id, other.finger, len(other.presses)) == (x.employee_id, x.finger, 5)
  assert other.agreement[0] == (-1, 50, 50, 50, 50)
  assert not any(c[0] == y.template for c in m.calls)  # y never reached the bar


def test_a_failing_job_is_retried(closing: list[ScoringSession]) -> None:
  others = gallery(1)
  m, rec, failed_once = FakeMatching({}), Recorder(), threading.Event()

  def behave(_press: bytes, _other: bytes) -> object:
    if not failed_once.is_set():
      failed_once.set()
      return "fail"
    return None

  m.behave = behave
  s = session(m, others, rec, threads=2)
  closing.append(s)
  s.add(0, b"p0")
  rec.wait(lambda: len(rec.delivered) == 1)
  assert rec.failed == 0


def test_the_third_failure_fails_once(closing: list[ScoringSession]) -> None:
  others = gallery(1)
  m, rec = FakeMatching({}), Recorder()
  m.behave = lambda _p, _o: "fail"
  s = session(m, others, rec, threads=2)
  closing.append(s)
  s.add(0, b"p0")
  rec.wait(lambda: rec.failed == 1)
  time.sleep(0.2)
  assert (rec.failed, rec.delivered) == (1, [])
  assert len(m.calls) == 3  # noqa: PLR2004


def test_a_hung_job_is_cancelled_at_its_deadline(closing: list[ScoringSession]) -> None:
  others = gallery(1)
  m, rec, hung = FakeMatching({}), Recorder(), threading.Event()

  def behave(_press: bytes, _other: bytes) -> object:
    if not hung.is_set():
      hung.set()
      return "block"
    return None

  m.behave = behave
  s = session(m, others, rec, threads=1, deadline=0.2, grace=0.2)  # a 5 s grace timer would outlive the test
  closing.append(s)
  s.add(0, b"p0")
  rec.wait(lambda: len(rec.delivered) == 1)
  assert rec.failed == 0
  assert len(m.threads) == 1  # the one thread came back from its cancel and ran the retry


def test_a_job_ignoring_its_cancel_is_abandoned_within_the_cap(
  closing: list[ScoringSession], caplog: pytest.LogCaptureFixture
) -> None:
  others = gallery(1)
  m, rec, stuck = FakeMatching({}), Recorder(), threading.Event()

  def behave(_press: bytes, _other: bytes) -> object:
    if not stuck.is_set():
      stuck.set()
      return "ignore"
    return None

  m.behave = behave
  s = session(m, others, rec, threads=2, deadline=0.1, grace=0.1)
  closing.append(s)
  s.add(0, b"p0")
  rec.wait(lambda: len(rec.delivered) == 1)  # the retry, on the other thread
  eventually(lambda: "abandoned" in caplog.text)  # the stuck call outlived its grace
  assert m.alive_peak <= 2  # noqa: PLR2004
  assert rec.failed == 0  # a thread is left
  m.release.set()
  s.close()
  eventually(no_matcher_threads)  # the abandoned thread exits once its call returns


def test_results_that_never_come_back_fail_the_registration(closing: list[ScoringSession]) -> None:
  # Review Focus 1: each deadline cancels the job and retries it; the third ends the registration, not "checking".
  others = gallery(1)
  m, rec = FakeMatching({}), Recorder()
  m.behave = lambda _p, _o: "block"
  s = session(m, others, rec, threads=2, deadline=0.1, grace=0.1)
  closing.append(s)
  s.add(0, b"p0")
  rec.wait(lambda: rec.failed == 1)
  time.sleep(0.2)
  assert (rec.failed, rec.delivered) == (1, [])
  assert len(m.calls) == 3  # noqa: PLR2004


def test_the_last_thread_abandoned_fails_the_registration(closing: list[ScoringSession]) -> None:
  # No thread is left to retry on: the registration ends at once rather than waiting on calls that ignore their cancel.
  others = gallery(1)
  m, rec = FakeMatching({}), Recorder()
  m.behave = lambda _p, _o: "ignore"
  s = session(m, others, rec, threads=1, deadline=0.1, grace=0.1)
  closing.append(s)
  s.add(0, b"p0")
  rec.wait(lambda: rec.failed == 1)
  m.release.set()
  eventually(no_matcher_threads)
  assert rec.failed == 1


def test_ended_threads_are_freed_without_the_collector() -> None:
  # glibc releases an unjoined thread's stack, with its thread-local bozorth3 tables (about 47 MB), only once CPython
  # detaches it, when its Thread object is freed: a reference cycle would hold the tables until a full collection.
  others = gallery(2)
  m, rec = FakeMatching({}), Recorder()
  gc.disable()
  try:
    s = session(m, others, rec, threads=3)
    s.add(0, b"p0")
    rec.wait(lambda: len(rec.delivered) == 1)
    workers = [weakref.ref(t) for t in threading.enumerate() if t.name.startswith("matcher")]
    assert len(workers) == 3  # noqa: PLR2004
    s.close()
    eventually(lambda: all(w() is None for w in workers))
  finally:
    gc.enable()


def test_close_drops_outstanding_work() -> None:
  others = gallery(2)
  m, rec = FakeMatching({}), Recorder()
  m.behave = lambda _p, _o: "ignore"
  s = session(m, others, rec, threads=2)
  s.add(0, b"p0")
  deadline = time.monotonic() + WAIT
  while len(m.calls) < 2 and time.monotonic() < deadline:  # noqa: PLR2004
    time.sleep(0.01)
  t0 = time.monotonic()
  s.close()
  assert time.monotonic() - t0 < 0.5  # noqa: PLR2004
  m.release.set()
  time.sleep(0.2)
  assert (rec.delivered, rec.failed) == ([], 0)


def test_never_more_threads_than_the_cap(closing: list[ScoringSession]) -> None:
  others = gallery(6)
  m, rec = FakeMatching({}), Recorder()
  m.behave = lambda _p, _o: 0.002
  s = session(m, others, rec, threads=3)
  closing.append(s)
  for i in range(50):
    s.add(i, f"p{i}".encode())
  rec.wait(lambda: len(rec.delivered) == 50)  # noqa: PLR2004
  assert m.alive_peak <= 3  # noqa: PLR2004
  assert [p.press_id for p in rec.delivered] == list(range(50))


def test_the_threads_end_with_the_session() -> None:
  others = gallery(2)
  m, rec = FakeMatching({}), Recorder()
  s = session(m, others, rec, threads=3)
  s.add(0, b"p0")
  rec.wait(lambda: len(rec.delivered) == 1)
  s.close()
  deadline = time.monotonic() + WAIT
  while any(t.name.startswith("matcher") for t in threading.enumerate()) and time.monotonic() < deadline:
    time.sleep(0.01)
  assert not any(t.name.startswith("matcher") for t in threading.enumerate())
