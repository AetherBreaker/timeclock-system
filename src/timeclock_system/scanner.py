"""The fingerprint scanner thread (spec §6.2): one daemon thread owns the reader; the event loop drives its mode.

Concurrency: `_lock` guards the fields `set_mode`/`register`/`merge`/`set_gallery`/`stop`/`busy_seconds` share with the
thread. It is never held across a blocking reader call or a `post`/`on_fatal` call. Every pause waits on `_wake`, which a
mode change, a merge or `stop` sets, so pauses end early; the one exception, the pause after an early result, waits on
`_stopped` (see `_ready_once`). Every reader call runs on the thread: bozorth3, behind `score`, keeps global state.
"""

# Standard library imports
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID  # noqa: TC003  # the dataclass annotation is evaluated at runtime

# Third party imports
from aeth_ext.errors import alert
from timeclock_schema.enums import Finger, ScanResult  # the dataclass annotations are evaluated at runtime

# First party imports
from timeclock_system.registration import OtherPrint, Press

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Sequence

  # First party imports
  from timeclock_system.registration import Minutiae

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class GalleryPrint:
  """One enrolled print as the matcher consumes it."""

  print_id: UUID
  employee_id: int
  template: bytes = field(repr=False)  # R15: keep the template out of tracebacks and alerts
  finger: Finger


@dataclass(frozen=True)
class ScanImage:
  """The raw image of one capture."""

  data: bytes = field(repr=False)  # R15: biometric, like a template
  width: int
  height: int


@dataclass(frozen=True)
class Capture:
  """One identify as the reader saw it: `match` is libfprint's pick (the best print at or above its threshold, or None).

  `scores` gives every gallery print's score in gallery order; -1 means libfprint never scored it.
  """

  match: UUID | None
  scores: tuple[tuple[UUID, int], ...]
  probe: bytes | None = field(default=None, repr=False)  # R15: the serialized probe print
  image: ScanImage | None = None


@dataclass(frozen=True)
class ScoredPrint:
  """A gallery print with the score it got in one capture."""

  print_id: UUID
  employee_id: int
  finger: Finger
  score: int


@dataclass(frozen=True)
class ScanReport:
  """What one identify found and how it was decided.

  `best` is libfprint's match if any, else the top score; `other` is the top-scoring print of anyone but `best`'s employee.
  """

  result: ScanResult
  best: ScoredPrint | None
  other: ScoredPrint | None
  scores: tuple[tuple[UUID, int], ...]
  margin: int
  probe: bytes | None = field(default=None, repr=False)  # R15
  image: ScanImage | None = None
  retry_message: str | None = None


@dataclass(frozen=True)
class Matched:
  """A capture matched an enrolled print."""

  print_id: UUID
  employee_id: int
  scan: ScanReport | None = None


@dataclass(frozen=True)
class NoMatch:
  """A capture matched no enrolled print, or matched one too closely to another person's."""

  scan: ScanReport | None = None


@dataclass(frozen=True)
class Retry:
  """A capture was unusable; `message` tells the user what to do."""

  message: str
  scan: ScanReport | None = None


@dataclass(frozen=True)
class FingerStatus:
  """libfprint's finger-status flags: `present` (FP_FINGER_STATUS_PRESENT), `waiting` (FP_FINGER_STATUS_NEEDED)."""

  present: bool
  waiting: bool


_NO_FINGER = FingerStatus(present=False, waiting=False)


@dataclass(frozen=True)
class ReaderPaused:
  """The reader reported itself too hot and is paused for `seconds`; the patched reader shouldn't (§7)."""

  seconds: float


@dataclass(frozen=True)
class EnrollStage:
  """An enrollment press: `done` of `total` stages counted so far."""

  done: int
  total: int
  ok: bool = True  # False: libfprint reported a retry; the press wasn't counted


@dataclass(frozen=True)
class Enrolled:
  """Enrollment finished with this serialized template."""

  template: bytes = field(repr=False)  # R15


@dataclass(frozen=True)
class EnrollFailed:
  """Enrollment or registration ended without a template (REGISTER: a press timed out or the reader failed), or a merge failed."""

  reason: Literal["timeout", "error"]


@dataclass(frozen=True)
class RegisterGallery:
  """A REGISTER session began: every gallery print but the registrant's, which its presses are scored against."""

  others: tuple[OtherPrint, ...]


@dataclass(frozen=True)
class Pressed:
  """A REGISTER capture, scored against the session's earlier presses and the session's `others`."""

  press: Press


@dataclass(frozen=True)
class PressRetry:
  """A REGISTER capture was unusable (libfprint's retry); no press was counted."""


@dataclass(frozen=True)
class Merged:
  """A queued merge finished with this serialized template."""

  template: bytes = field(repr=False)  # R15


type ScannerEvent = (
  Matched
  | NoMatch
  | Retry
  | FingerStatus
  | ReaderPaused
  | EnrollStage
  | Enrolled
  | EnrollFailed
  | RegisterGallery
  | Pressed
  | PressRetry
  | Merged
)


class ReaderMode(StrEnum):
  """What the scanner thread does with the reader."""

  CLOSED = "closed"
  READY = "ready"
  ENROLL = "enroll"
  REGISTER = "register"  # entered through `Scanner.register`, for one registrant


# The names below are pinned by the plan's interface (N818 wants an `Error` suffix).
class ScanCancelled(Exception):  # noqa: N818
  """The call's cancel token fired."""


class RetryScan(Exception):  # noqa: N818
  """The capture was unusable; the message tells the user what to do."""


class ReaderGone(Exception):  # noqa: N818
  """The reader was removed or can't be opened; fatal."""


class ReaderTooHot(Exception):  # noqa: N818
  """The reader refuses captures until it cools down."""


class ReaderFailure(Exception):  # noqa: N818
  """Any other reader error; retried with backoff."""


class CancelToken(Protocol):
  """Cancels the blocking reader call it was passed to; callable from any thread."""

  def cancel(self) -> None:
    """Cancel the call."""


class Reader(Protocol):
  """The fingerprint reader; every method blocks and runs only on the scanner thread."""

  def new_cancel(self) -> CancelToken:
    """Return a fresh token for one call."""
    ...

  def open(self) -> None:
    """Open the device."""

  def close(self) -> None:
    """Close the device, letting it autosuspend (R40.1)."""

  def identify(
    self,
    gallery: Sequence[GalleryPrint],
    cancel: CancelToken,
    on_status: Callable[[FingerStatus], None],
    on_result: Callable[[Capture], None],
  ) -> Capture:
    """Capture one print and score it against every print in `gallery`.

    `on_status` gets every finger-status change. `on_result` gets the capture the moment it's made, while the finger may
    still be down; the call itself returns only after the lift.
    """
    ...

  def enroll(
    self, cancel: CancelToken, on_stage: Callable[[int, int, bool], None], on_status: Callable[[FingerStatus], None]
  ) -> bytes:
    """Run every enrollment stage; return the serialized template.

    `on_stage(done, total, ok)` gets every press, `ok` False for one the reader couldn't use (not counted).
    """
    ...

  def capture(self, cancel: CancelToken, on_status: Callable[[FingerStatus], None]) -> bytes:
    """Capture one press and return it as a serialized one-press template. Raises like `identify`."""
    ...

  def presses(self, template: bytes) -> tuple[Minutiae, ...]:
    """Decode a template's presses into their detail points."""
    ...

  def score(self, press: bytes, index: int, other: bytes) -> tuple[int, ...]:
    """Score `press`'s press `index` against each of `other`'s presses."""
    ...

  def merge(self, presses: Sequence[bytes]) -> bytes:
    """Merge one-press templates into one template, in order."""
    ...


@dataclass(frozen=True)
class ScannerTimings:
  """Pauses and timeouts in seconds (§6.2, R40.2)."""

  retry_pause: float = 0.5
  backoff_start: float = 1.0
  backoff_max: float = 30.0
  too_hot_pause: float = 60.0
  rearm_every: float = 60.0
  finger_off_pause: float = 1.0  # after a capture, about twice the 0.54 s tail the spike saw; see _ready_once
  failure_limit: float = 180.0  # a ReaderFailure streak this long is fatal
  enroll_idle_timeout: float = 30.0


def _decide(capture: Capture, gallery: Sequence[GalleryPrint], margin: int) -> Matched | NoMatch:
  """Turn a capture into the event to post: a match must beat every other person's best print by `margin` (0: no rule)."""
  known = {p.print_id: p for p in gallery}
  scored = [ScoredPrint(i, known[i].employee_id, known[i].finger, score) for i, score in capture.scores if i in known and score >= 0]
  best = next((s for s in scored if s.print_id == capture.match), None)
  matched = best is not None
  if best is None:
    best = max(scored, key=lambda s: s.score, default=None)  # max keeps the first of equal scores, as libfprint does
  others = [] if best is None else [s for s in scored if s.employee_id != best.employee_id]
  other = max(others, key=lambda s: s.score, default=None)
  if best is None or not matched:
    result = ScanResult.NO_MATCH
  elif margin > 0 and best.score - (0 if other is None else other.score) < margin:
    result = ScanResult.TOO_CLOSE
  else:
    result = ScanResult.MATCHED
  scan = ScanReport(result, best, other, capture.scores, margin, capture.probe, capture.image)
  if result is ScanResult.MATCHED and best is not None:
    return Matched(best.print_id, best.employee_id, scan)
  return NoMatch(scan)


class Scanner:
  """Owns the reader on a daemon thread; `post` and `on_fatal` are called on that thread."""

  def __init__(
    self,
    reader: Reader,
    post: Callable[[ScannerEvent], None],
    on_fatal: Callable[[str], None],
    *,
    timings: ScannerTimings = ScannerTimings(),  # noqa: B008  # frozen, so sharing the default is safe
    monotonic: Callable[[], float] = time.monotonic,
  ) -> None:
    """Create a scanner in CLOSED mode; `start` launches the thread."""
    self._reader = reader
    self._post = post
    self._on_fatal = on_fatal
    self._timings = timings
    self._monotonic = monotonic
    self._thread = threading.Thread(target=self._run, name="scanner", daemon=True)
    self._opened = False  # scanner thread only, like _backoff and _last_status
    self._last_status: FingerStatus | None = None  # the current call's last posted status; None once it ends
    self._backoff = timings.backoff_start  # doubles per ReaderFailure, reset by a completed capture
    self._failing_since: float | None = None  # start of the current ReaderFailure streak, reset with _backoff
    # The REGISTER session, scanner thread only: the _mode_changes it began at, the prints it scores against, and its
    # presses' probes by press_id. Decoded presses and agreement are cached across sessions, keyed by the whole print so
    # one whose template changed is decoded again (and no raw template sits in a key a traceback's locals would show).
    self._session: int | None = None
    self._others: tuple[GalleryPrint, ...] = ()
    self._probes: list[bytes] = []
    self._decoded: dict[GalleryPrint, tuple[tuple[Minutiae, ...], tuple[tuple[int, ...], ...]]] = {}
    self._lock = threading.Lock()
    self._wake = threading.Event()
    self._stopped = threading.Event()  # set only by stop
    # Guarded by _lock:
    self._mode = ReaderMode.CLOSED
    self._mode_changes = 0  # tells a mode change from an idle timeout; a REGISTER session lasts while it's unchanged
    self._registrant: int | None = None  # REGISTER's employee; None in every other mode
    self._merge: tuple[bytes, ...] | None = None  # a queued merge's presses
    self._gallery: tuple[GalleryPrint, ...] = ()
    self._margin = 0
    self._token: CancelToken | None = None
    self._busy_since: float | None = None
    self._stopping = False

  @property
  def mode(self) -> ReaderMode:
    """The current mode."""
    with self._lock:
      return self._mode

  def start(self) -> None:
    """Launch the scanner thread."""
    self._thread.start()

  def stop(self) -> None:
    """Cancel the current call and end the thread; doesn't wait for it, so a hung call can't block the caller."""
    with self._lock:
      self._stopping = True
      token = self._token
    self._stopped.set()
    self._wake.set()
    if token is not None:
      token.cancel()

  def set_mode(self, mode: ReaderMode) -> None:
    """Switch mode, cancelling the current call; the same mode again is a no-op so it can't cut a capture.

    REGISTER entered here has no registrant, so every gallery print counts as another person's; use `register`.
    """
    self._switch(mode, None)

  def register(self, employee_id: int) -> None:
    """Enter REGISTER for `employee_id`: a new session, which posts `RegisterGallery`, then a `Pressed` per capture.

    The same registrant again is a no-op, as in `set_mode`. The session lasts until `set_mode`, or until a press times
    out or the reader fails (`EnrollFailed`, then READY).
    """
    self._switch(ReaderMode.REGISTER, employee_id)

  def _switch(self, mode: ReaderMode, registrant: int | None) -> None:
    with self._lock:
      if (mode, registrant) == (self._mode, self._registrant):
        return
      self._mode, self._registrant = mode, registrant
      self._mode_changes += 1
      token = self._token
    self._wake.set()
    if token is not None:
      token.cancel()

  def merge(self, presses: Sequence[bytes]) -> None:
    """Queue a merge of one-press templates, in order, for the thread to run next in any mode; posts `Merged`.

    Cancels the current call so the merge needn't wait for a press; a REGISTER session goes on after it. A failed merge
    posts `EnrollFailed("error")`.
    """
    with self._lock:
      self._merge = tuple(presses)
      token = self._token
    self._wake.set()
    if token is not None:
      token.cancel()

  def set_gallery(self, prints: Sequence[GalleryPrint], margin: int) -> None:
    """Replace the gallery, restarting a running identify when its prints change; an equal gallery changes nothing (R6).

    Only an identify is restarted: an enrollment doesn't use the gallery, a registration reads it once as its session
    begins, and cancelling either would lose the user's presses.
    A new `margin`, or a new order alone (every punch moves its employee first), applies from the next call without a
    restart: libfprint drops a cancel that lands while it's still starting the reader, and the call then runs until
    the next press.
    """
    gallery = tuple(prints)
    with self._lock:
      self._margin = margin
      if gallery == self._gallery:
        return
      reordered = set(gallery) == set(self._gallery)
      self._gallery = gallery
      token = self._token if self._mode is ReaderMode.READY and not reordered else None
    if token is not None:
      token.cancel()

  def busy_seconds(self) -> float:
    """How long the current blocking reader call has run; 0.0 when none (the heartbeat's hung-call watchdog)."""
    with self._lock:
      return 0.0 if self._busy_since is None else self._monotonic() - self._busy_since

  def _blocking[**P, T](self, call: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    with self._lock:
      self._busy_since = self._monotonic()
    try:
      return call(*args, **kwargs)
    finally:
      with self._lock:
        self._busy_since = None

  def _run(self) -> None:
    try:
      self._loop()
    except ReaderGone as e:
      self._opened = False
      self._on_fatal(str(e))
    else:
      if self._opened:
        try:
          self._blocking(self._reader.close)
        except ReaderGone, ReaderFailure:
          pass

  def _loop(self) -> None:
    # Every branch ends in a blocking reader call or a pause on _wake; none may loop without one (the spike spun 3.3M times).
    while True:
      with self._lock:
        if self._stopping:
          return
        self._wake.clear()
        mode, mode_changes = self._mode, self._mode_changes
        presses, self._merge = self._merge, None
      if presses is not None:
        # Before any mode's call: the controller waits on it, and merging needs no device, so CLOSED doesn't open it.
        try:
          merged: ScannerEvent = Merged(self._blocking(self._reader.merge, presses))
        except RetryScan, ReaderTooHot, ReaderFailure:
          merged = EnrollFailed("error")
        self._post(merged)
        continue
      try:
        if mode is ReaderMode.CLOSED:
          if self._opened:
            self._opened = False
            self._blocking(self._reader.close)
          self._wake.wait()
          continue
        self._mode_once(mode, mode_changes)
      except ScanCancelled:
        pass  # the re-arm timer, a mode or gallery change, or stop; the loop re-reads the state
      except ReaderTooHot:
        # The patched reader never reports too hot (§7), so one that does means the patch isn't working on this clock.
        alert("Fingerprint reader paused itself (too hot)", f"Paused for {self._timings.too_hot_pause:g} s", wait=False)
        self._post(ReaderPaused(self._timings.too_hot_pause))
        self._wake.wait(self._timings.too_hot_pause)
      except ReaderFailure as e:
        self._on_failure(e)

  def _mode_once(self, mode: ReaderMode, mode_changes: int) -> None:
    # Split out of _loop only for C901.
    if not self._opened:
      self._blocking(self._reader.open)
      self._opened = True
    if mode is ReaderMode.ENROLL:
      self._enroll_once(mode_changes)
    elif mode is ReaderMode.REGISTER:
      self._register_once(mode_changes)
    else:
      self._ready_once()

  def _on_failure(self, e: ReaderFailure) -> None:
    # Split out of _loop only for C901. §2: a reader must never stop working silently while the screen looks ready. The
    # first failure of a streak is logged; a streak past failure_limit ends as a lost reader does (fatal: an alert, and
    # a restart reopens it).
    now = self._monotonic()
    if self._failing_since is None:
      self._failing_since = now
      LOGGER.warning("fingerprint reader failed, retrying with backoff: %s", e)
    elif now - self._failing_since >= self._timings.failure_limit:
      raise ReaderGone(f"the reader kept failing for {now - self._failing_since:.0f} s: {e}") from e
    self._wake.wait(self._backoff)
    self._backoff = min(self._backoff * 2, self._timings.backoff_max)

  def _on_status(self, status: FingerStatus) -> None:
    # Scanner thread only: libfprint calls back from inside the blocking call.
    self._last_status = status
    self._post(status)

  def _ready_once(self) -> None:
    # Split out of _loop only for C901.
    result, posted = self._identify_once()
    if not posted:
      self._post(result)
    if isinstance(result, Retry):
      self._wake.wait(self._timings.retry_pause)
      return
    self._backoff = self._timings.backoff_start
    self._failing_since = None
    # §6.2: no re-capture straight away. identify returns after the lift, but a call cancelled while the finger rests
    # (the re-arm, a gallery or mode change) returns before it; the pause keeps a bounce or that resting finger from
    # being read as the next press. After an early result only stop ends it: a mode change then (registration after an
    # admin's approving finger) must not start reading while that finger may still rest.
    (self._stopped if posted else self._wake).wait(self._timings.finger_off_pause)

  def _identify_once(self) -> tuple[Matched | NoMatch | Retry, bool]:
    """The one matcher boundary (R36.3): the only `reader.identify` call, which version 2 replaces.

    Returns the result and whether it was already posted: libfprint reports a match while the finger is still down,
    and it's posted then, so the screen acts before the lift.

    Raises:
      ScanCancelled: the mode changed or a merge was queued before the call, or its token was cancelled before a result.
    """
    token = self._reader.new_cancel()
    with self._lock:
      if self._stopping or self._mode is not ReaderMode.READY or self._merge is not None:
        raise ScanCancelled
      self._token = token
      gallery, margin = self._gallery, self._margin

    early: list[Matched | NoMatch] = []  # a list, not a nonlocal: pyright doesn't see a closure assign a local

    def on_result(capture: Capture) -> None:
      early.append(_decide(capture, gallery, margin))
      self._post(early[-1])

    # The periodic re-arm bounds every identify, so a call that outlives it is hung (busy_seconds, §6.2).
    rearm = threading.Timer(self._timings.rearm_every, token.cancel)
    rearm.daemon = True
    rearm.start()
    try:
      capture = self._blocking(self._reader.identify, gallery, token, self._on_status, on_result)
    except RetryScan as e:
      return Retry(str(e), ScanReport(ScanResult.RETRY, None, None, (), margin, retry_message=str(e))), False
    except ScanCancelled:
      # A cancel after the result (a mode change, closing time, the re-arm) only stopped the wait for the lift.
      if not early:
        raise
      return early[-1], True
    finally:
      rearm.cancel()
      with self._lock:
        self._token = None
      if self._last_status not in {None, _NO_FINGER}:
        self._post(_NO_FINGER)  # a cancelled call may never report the lift
      self._last_status = None
    return (early[-1], True) if early else (_decide(capture, gallery, margin), False)

  def _enroll_once(self, mode_changes: int) -> None:
    token = self._reader.new_cancel()
    with self._lock:
      if self._stopping or self._mode_changes != mode_changes:
        return
      self._token = token
    timed_out = threading.Event()

    def expire() -> None:
      timed_out.set()
      token.cancel()

    idle = threading.Timer(self._timings.enroll_idle_timeout, expire)

    def on_stage(done: int, total: int, ok: bool) -> None:
      nonlocal idle
      idle.cancel()  # any press, counted or not, shows someone is there
      idle = threading.Timer(self._timings.enroll_idle_timeout, expire)
      idle.daemon = True
      idle.start()
      self._post(EnrollStage(done, total, ok))

    idle.daemon = True
    idle.start()
    outcome: ScannerEvent | None
    try:
      outcome = Enrolled(self._blocking(self._reader.enroll, token, on_stage, self._on_status))
    except ScanCancelled:
      outcome = EnrollFailed("timeout") if timed_out.is_set() else None
    except RetryScan, ReaderTooHot, ReaderFailure:
      outcome = EnrollFailed("error")
    finally:
      idle.cancel()
      if self._last_status not in {None, _NO_FINGER}:
        self._post(_NO_FINGER)  # libfprint completes before the last press lifts and doesn't report that lift
      self._last_status = None
    with self._lock:
      self._token = None
      # A set_mode or stop during the call supersedes it: its cancel is the user's Cancel, not a failure (R16).
      superseded = self._stopping or self._mode_changes != mode_changes
      if not superseded:
        self._mode = ReaderMode.READY
    if superseded:
      return
    if outcome is not None:
      self._post(outcome)
    # Posted first so the UI shows the result at once. libfprint can finish before the last stage's finger lifts, and
    # READY's identify would match it as the employee who just enrolled: the same pause as _ready_once.
    self._wake.wait(self._timings.finger_off_pause)

  def _register_once(self, mode_changes: int) -> None:
    # One capture of the REGISTER session that began at `mode_changes`; the session's first call posts RegisterGallery. As
    # in _enroll_once, a set_mode or stop supersedes the call and posts nothing (R16), while a timed-out press or a reader
    # error ends the session in READY.
    token = self._reader.new_cancel()
    with self._lock:
      if self._stopping or self._mode_changes != mode_changes or self._merge is not None:
        return  # a queued merge runs first
      # Set before the gallery decoding, so a cancel landing during it still stops the capture that follows.
      self._token = token
      gallery, registrant = self._gallery, self._registrant
    timed_out = threading.Event()

    def expire() -> None:
      timed_out.set()
      token.cancel()

    idle = threading.Timer(self._timings.enroll_idle_timeout, expire)  # per capture, so a press or a retry restarts it
    idle.daemon = True
    outcome: ScannerEvent | None
    try:
      if self._session != mode_changes:
        self._start_session(mode_changes, gallery, registrant)
      idle.start()
      probe = self._blocking(self._reader.capture, token, self._on_status)
      idle.cancel()
      score = self._reader.score
      vs_session = {
        i: max(self._blocking(score, probe, 0, e)[0], self._blocking(score, e, 0, probe)[0]) for i, e in enumerate(self._probes)
      }
      vs_gallery = {p.print_id: self._blocking(score, probe, 0, p.template) for p in self._others}
      press = Press(len(self._probes), self._blocking(self._reader.presses, probe)[0], probe, vs_session, vs_gallery)
      self._probes.append(probe)
      outcome = Pressed(press)
    except ScanCancelled:
      outcome = EnrollFailed("timeout") if timed_out.is_set() else None  # else a merge, set_mode or stop
    except RetryScan:
      outcome = PressRetry()
    except ReaderTooHot, ReaderFailure:
      outcome = EnrollFailed("error")
    finally:
      idle.cancel()
      if self._last_status not in {None, _NO_FINGER}:
        self._post(_NO_FINGER)  # a cancelled capture may never report the lift
      self._last_status = None
    with self._lock:
      self._token = None
      superseded = self._stopping or self._mode_changes != mode_changes
      if not superseded and isinstance(outcome, EnrollFailed):
        self._mode, self._registrant = ReaderMode.READY, None
    if superseded or outcome is None:
      return
    self._post(outcome)
    # As _ready_once: a retry's finger may still rest, and a bounce mustn't read as the next press (or, after a failure,
    # as READY's next match).
    self._wake.wait(self._timings.retry_pause if isinstance(outcome, PressRetry) else self._timings.finger_off_pause)

  def _start_session(self, mode_changes: int, gallery: tuple[GalleryPrint, ...], registrant: int | None) -> None:
    # Split out of _register_once for C901: score every other person's print against itself, then post RegisterGallery.
    others = tuple(p for p in gallery if p.employee_id != registrant)
    decoded: dict[GalleryPrint, tuple[tuple[Minutiae, ...], tuple[tuple[int, ...], ...]]] = {}
    for p in others:
      if (cached := self._decoded.get(p)) is None:
        presses = self._blocking(self._reader.presses, p.template)
        n = range(len(presses))
        own = [self._blocking(self._reader.score, p.template, i, p.template) for i in n]
        cached = presses, tuple(tuple(-1 if i == j else max(own[i][j], own[j][i]) for j in n) for i in n)
      decoded[p] = cached
    self._decoded, self._session, self._others, self._probes = decoded, mode_changes, others, []
    self._post(RegisterGallery(tuple(OtherPrint(p.print_id, p.employee_id, p.finger, *decoded[p]) for p in others)))
