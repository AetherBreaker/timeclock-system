"""Registration's matching on threads (spec `2026-11-16-registration-matching-pool-design.md`).

The patched libfprint's bozorth3 is thread-local and cancellable, so a registration's presses score on matcher threads
while the scanner thread keeps capturing. `ScoringSession` runs one registration's scoring; its threads live only as
long as the registration, so their copies of bozorth3's tables are freed between registrations.
"""

# Standard library imports
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal, Protocol, runtime_checkable

# First party imports
from timeclock_system.registration import OtherPrint, Press

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Sequence
  from uuid import UUID

  # First party imports
  from timeclock_system.registration import Minutiae
  from timeclock_system.scanner import CancelToken, GalleryPrint

LOGGER = logging.getLogger(__name__)

# Matcher threads per registration: fixed from bozorth3's measured memory per thread (46.7 MB, thread_tests.py.in in
# timeclock-extras; scripts/matcher_check.py on a clock), so all of them together stay under 256 MB. Not the CPU count:
# a thread past the cores waits its turn rather than costing memory.
MATCHER_THREADS: Final = 5
JOB_DEADLINE_SECS: Final = 15.0  # a job not back by then is cancelled and counts as a failure
CANCEL_GRACE_SECS: Final = 5.0  # a call still running this long after its cancel has its thread abandoned
FAILURE_LIMIT: Final = 3  # a registration's failures that end it


@runtime_checkable
class Matching(Protocol):
  """The matcher registration scores with; every method may be called from any number of threads at once."""

  def new_cancel(self) -> CancelToken:
    """Return a fresh token for one call."""
    ...

  def presses(self, template: bytes) -> tuple[Minutiae, ...]:
    """Decode a template's presses into their detail points. Raises `ReaderFailure` if it doesn't decode."""
    ...

  def score(self, press: bytes, index: int, other: bytes, cancel: CancelToken) -> tuple[int, ...]:
    """Score `press`'s press `index` against each of `other`'s presses.

    Raises `ScanCancelled` once `cancel` fires, `ReaderFailure` for any other error.
    """
    ...


@dataclass(eq=False)
class _Job:
  """One unit of a press's scoring, run on a matcher thread; `token`, `thread` and `dropped` belong to one attempt."""

  kind: Literal["gallery", "session", "agreement"]
  press_id: int  # an agreement's is the press that first resembled its print; it serves every press resembling it
  probe: bytes = field(repr=False)  # R15
  prints: tuple[GalleryPrint, ...] = ()  # the gallery chunk, or the one print an agreement is for
  earlier: tuple[tuple[int, bytes], ...] = field(default=(), repr=False)  # a session job's earlier probes, by press_id
  token: CancelToken | None = None
  thread: threading.Thread | None = None
  dropped: bool = False  # timed out (and retried elsewhere) or closed: its result no longer counts

  def retry(self) -> _Job:
    return _Job(self.kind, self.press_id, self.probe, self.prints, self.earlier)


@dataclass(eq=False)
class _Pending:
  """A press whose jobs aren't all back yet."""

  probe: bytes = field(repr=False)  # R15
  gallery_left: int = 0
  minutiae: Minutiae | None = field(default=None, repr=False)  # R15
  vs_session: dict[int, int] | None = None
  vs_gallery: dict[UUID, tuple[int, ...]] = field(default_factory=dict)
  resembled: dict[UUID, OtherPrint] = field(default_factory=dict, repr=False)  # R15
  awaiting: set[UUID] = field(default_factory=set)  # resembled prints whose agreement is still being worked out

  @property
  def complete(self) -> bool:
    return self.gallery_left == 0 and self.vs_session is not None and not self.awaiting


class ScoringSession:
  """One registration's scoring on at most `threads` matcher threads, which start with its first press.

  `add` hands over each capture; `deliver` gets each scored `Press` in capture order, and `fail` is called once when
  `failure_limit` failures have been counted. Both are called from the matcher threads, under the session's lock, so
  they must only hand the result on (the scanner posts it). After `close`, or after `fail`, neither is called again.

  A job that raises is retried. A job not back within `deadline` is cancelled through its token (the matcher stops
  itself within one loop step), counted as a failure and retried; a call still running `grace` after its cancel has its
  thread abandoned, which still counts toward `threads` until it returns, so the cap holds.
  """

  def __init__(
    self,
    matching: Matching,
    others: Sequence[GalleryPrint],
    resemblance_bar: int,
    deliver: Callable[[Press], None],
    fail: Callable[[], None],
    *,
    threads: int = MATCHER_THREADS,
    deadline: float = JOB_DEADLINE_SECS,
    grace: float = CANCEL_GRACE_SECS,
    failure_limit: int = FAILURE_LIMIT,
  ) -> None:
    """Prepare the session; no thread starts until the first `add`."""
    self._matching = matching
    self._others = tuple(others)
    self._bar = resemblance_bar
    self._deliver = deliver
    self._fail = fail
    self._cap = max(1, threads)
    self._deadline = deadline
    self._grace = grace
    self._limit = failure_limit
    self._lock = threading.Lock()
    self._work = threading.Condition(self._lock)
    # Guarded by _lock:
    self._jobs: deque[_Job] = deque()
    self._running: set[_Job] = set()
    self._workers: list[threading.Thread] = []
    self._abandoned: set[threading.Thread] = set()
    self._probes: dict[int, bytes] = {}  # every press added, for the later presses' session scores
    self._pending: dict[int, _Pending] = {}
    self._order: deque[int] = deque()  # press_ids not yet delivered, in capture order
    self._agreements: dict[UUID, OtherPrint] = {}
    self._agreeing: set[UUID] = set()  # prints whose agreement job is queued or running
    self._failures = 0
    self._closed = False

  @property
  def outstanding(self) -> int:
    """Presses added and not yet delivered."""
    with self._lock:
      return len(self._order)

  def add(self, press_id: int, probe: bytes) -> None:
    """Score a capture against the gallery and the session's earlier captures; a no-op once closed."""
    with self._lock:
      if self._closed:
        return
      earlier = tuple(self._probes.items())
      self._probes[press_id] = probe
      chunks = [c for k in range(self._cap) if (c := self._others[k :: self._cap])]
      self._pending[press_id] = _Pending(probe, gallery_left=len(chunks))
      self._order.append(press_id)
      self._jobs.extend(_Job("gallery", press_id, probe, chunk) for chunk in chunks)
      self._jobs.append(_Job("session", press_id, probe, earlier=earlier))
      while len(self._workers) < self._cap:
        worker = threading.Thread(target=self._work_loop, name=f"matcher-{len(self._workers)}", daemon=True)
        self._workers.append(worker)
        worker.start()
      self._work.notify_all()

  def close(self) -> None:
    """Drop every queued and running job and let the threads end; returns at once, never waiting on a call."""
    with self._lock:
      self._close_locked()

  def _close_locked(self) -> None:
    if self._closed:
      return
    self._closed = True
    self._jobs.clear()
    for job in self._running:
      job.dropped = True
      if job.token is not None:
        job.token.cancel()
    self._work.notify_all()

  def _work_loop(self) -> None:
    me = threading.current_thread()
    while True:
      with self._lock:
        while not self._jobs and not self._closed:
          self._work.wait()
        if self._closed or me in self._abandoned:
          return
        job = self._jobs.popleft()
        job.token, job.thread = self._matching.new_cancel(), me
        self._running.add(job)
        timer = threading.Timer(self._deadline, self._on_deadline, args=(job,))
        timer.daemon = True
      timer.start()
      result: object = None
      error: Exception | None = None
      try:
        result = self._run(job)
      except Exception as e:  # noqa: BLE001  # every failure counts toward the limit; none may end the thread
        error = e
      finally:
        timer.cancel()
      with self._lock:
        self._running.discard(job)
        if me in self._abandoned:
          return
        if self._closed or job.dropped:
          continue
        if error is not None:
          self._failed(job, error)
        else:
          self._record(job, result)
          self._deliver_ready()

  def _run(self, job: _Job) -> object:
    m, token = self._matching, job.token
    assert token is not None  # set as the job was taken
    match job.kind:
      case "gallery":
        return {p.print_id: m.score(job.probe, 0, p.template, token) for p in job.prints}
      case "session":
        if not (presses := m.presses(job.probe)):
          raise ValueError("the press decoded to no press")
        vs = {i: max(m.score(job.probe, 0, e, token)[0], m.score(e, 0, job.probe, token)[0]) for i, e in job.earlier}
        return presses[0], vs
      case "agreement":
        [p] = job.prints
        presses = m.presses(p.template)
        n = range(len(presses))
        own = [m.score(p.template, i, p.template, token) for i in n]
        if any(len(row) < len(presses) for row in own):
          raise ValueError("a print's press scores are fewer than its presses")
        agreement = tuple(tuple(-1 if i == j else max(own[i][j], own[j][i]) for j in n) for i in n)
        return OtherPrint(p.print_id, p.employee_id, p.finger, presses, agreement)

  def _record(self, job: _Job, result: object) -> None:
    match job.kind:
      case "gallery":
        pending = self._pending[job.press_id]
        scores: dict[UUID, tuple[int, ...]] = result  # pyright: ignore[reportAssignmentType]  # a gallery job's result
        pending.vs_gallery.update(scores)
        pending.gallery_left -= 1
        for p in job.prints:
          if max(scores[p.print_id], default=-1) < self._bar:
            continue
          if (known := self._agreements.get(p.print_id)) is not None:
            pending.resembled[p.print_id] = known
            continue
          pending.awaiting.add(p.print_id)
          if p.print_id not in self._agreeing:
            self._agreeing.add(p.print_id)
            self._jobs.append(_Job("agreement", job.press_id, p.template, (p,)))
            self._work.notify_all()
      case "session":
        pending = self._pending[job.press_id]
        pending.minutiae, pending.vs_session = result  # pyright: ignore[reportGeneralTypeIssues]  # a session job's result
      case "agreement":
        other: OtherPrint = result  # pyright: ignore[reportAssignmentType]  # an agreement job's result
        self._agreements[other.print_id] = other
        self._agreeing.discard(other.print_id)
        for pending in self._pending.values():
          if other.print_id in pending.awaiting:
            pending.awaiting.discard(other.print_id)
            pending.resembled[other.print_id] = other

  def _deliver_ready(self) -> None:
    while self._order and (pending := self._pending[self._order[0]]).complete:
      press_id = self._order.popleft()
      del self._pending[press_id]
      assert pending.minutiae is not None and pending.vs_session is not None  # `complete` says so
      self._deliver(Press(press_id, pending.minutiae, pending.probe, pending.vs_session, pending.vs_gallery, pending.resembled))

  def _failed(self, job: _Job, error: Exception) -> None:
    # R15: the error's type only, never its message's arguments.
    LOGGER.warning("Registration matching failed (%s, %s job), retrying", type(error).__name__, job.kind)
    if self._count_failure():
      self._jobs.append(job.retry())
      self._work.notify_all()

  def _count_failure(self) -> bool:
    """Count one failure; at the limit, close and call `fail`. Returns whether the session goes on."""
    self._failures += 1
    if self._failures < self._limit:
      return True
    LOGGER.warning("Registration matching failed %d times; the registration ends", self._failures)
    self._close_locked()
    self._fail()
    return False

  def _on_deadline(self, job: _Job) -> None:
    with self._lock:
      if job not in self._running or self._closed:
        return
      job.dropped = True
      if job.token is not None:
        job.token.cancel()
      LOGGER.warning("Registration matching: a %s job ran past %g s and was cancelled", job.kind, self._deadline)
      if not self._count_failure():
        return
      self._jobs.append(job.retry())
      self._work.notify_all()
    grace = threading.Timer(self._grace, self._on_grace, args=(job,))
    grace.daemon = True
    grace.start()

  def _on_grace(self, job: _Job) -> None:
    with self._lock:
      if job not in self._running or self._closed or job.thread is None:
        return
      # Its thread still counts toward the cap until it returns, and exits then (`_work_loop`).
      self._abandoned.add(job.thread)
      LOGGER.warning("Registration matching: a %s job ignored its cancel; its thread is abandoned", job.kind)
      if all(w in self._abandoned or not w.is_alive() for w in self._workers):
        self._failures = self._limit - 1  # no thread is left to retry on
        self._count_failure()
