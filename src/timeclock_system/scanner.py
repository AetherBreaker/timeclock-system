"""The fingerprint scanner thread (spec §6.2): one daemon thread owns the reader; the event loop drives its mode.

Concurrency: `_lock` guards the fields `set_mode`/`register`/`merge`/`set_gallery`/`stop`/`busy_seconds` share with the
thread. It is never held across a blocking reader call. A registration's events are posted under it, so none lands after
the change that ended its session (`post` never blocks); every other `post` is made outside it. A registration's
presses are scored on its matcher threads (`ScoringSession`), whose callbacks take `_lock` inside the session's own lock:
so a `ScoringSession` method is never called under `_lock`. Every pause waits on `_wake`, which a mode change, a merge or
`stop` sets, so pauses end early; the one exception, the pause after an early result, waits on `_stopped` (see
`_ready_once`). Every reader call runs on the thread.
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
from timeclock_system.matching import FAILURE_LIMIT, CancelToken, GalleryPrint, ScoringSession
from timeclock_system.registration import OtherRef, Press

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Sequence

  # First party imports
  from timeclock_system.matching import Matching

LOGGER = logging.getLogger(__name__)


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
class EnrollFailed:
  """A registration ended without a template: a press timed out or the reader failed, or a merge failed."""

  reason: Literal["timeout", "error"]


@dataclass(frozen=True)
class RegisterGallery:
  """A REGISTER session began: every gallery print but the registrant's, which its presses are scored against."""

  others: tuple[OtherRef, ...]
  resemblance_bar: int  # the session's, from `set_gallery` as it began


@dataclass(frozen=True)
class Captured:
  """A REGISTER capture, posted as it's made; its `Pressed` follows once it's scored."""

  press_id: int


@dataclass(frozen=True)
class Pressed:
  """A REGISTER capture, scored against the session's earlier presses and the session's `others`; in capture order."""

  press: Press


@dataclass(frozen=True)
class PressRetry:
  """A REGISTER capture was unusable (libfprint's retry); no press was counted."""


@dataclass(frozen=True)
class Merged:
  """A queued merge finished with this serialized template."""

  template: bytes = field(repr=False)  # R15


type ScannerEvent = (
  Matched | NoMatch | Retry | FingerStatus | ReaderPaused | EnrollFailed | RegisterGallery | Captured | Pressed | PressRetry | Merged
)


class ReaderMode(StrEnum):
  """What the scanner thread does with the reader."""

  CLOSED = "closed"
  READY = "ready"
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

  def capture(self, cancel: CancelToken, on_status: Callable[[FingerStatus], None]) -> bytes:
    """Capture one press and return it as a serialized one-press template. Raises like `identify`."""
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
  finger_off_pause: float = 1.0  # after a login or a failed press, about twice the spike's 0.54 s tail; see _ready_once
  failure_limit: float = 180.0  # a ReaderFailure streak this long is fatal
  enroll_idle_timeout: float = 30.0  # a registration's press timeout; it doesn't run while a press's results are out


def _decide(capture: Capture, gallery: Sequence[GalleryPrint], margin: int, min_score: int) -> Matched | NoMatch:
  """Turn a capture into the event to post: a reader match, refused when weak or close.

  A match must score at least `min_score` (40 or less: the reader's own threshold decides), then beat every other
  person's best print by `margin` (0: no rule).
  """
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
  elif best.score < min_score:
    result = ScanResult.TOO_WEAK
  elif margin > 0 and best.score - (0 if other is None else other.score) < margin:
    result = ScanResult.TOO_CLOSE
  else:
    result = ScanResult.MATCHED
  scan = ScanReport(result, best, other, capture.scores, margin, capture.probe, capture.image)
  if result is ScanResult.MATCHED and best is not None:
    return Matched(best.print_id, best.employee_id, scan)
  return NoMatch(scan)


class Scanner:
  """Owns the reader on a daemon thread; `post` is called on that thread."""

  def __init__(
    self,
    reader: Reader,
    post: Callable[[ScannerEvent], None],
    *,
    matching: Matching,
    timings: ScannerTimings = ScannerTimings(),  # noqa: B008  # frozen, so sharing the default is safe
    monotonic: Callable[[], float] = time.monotonic,
  ) -> None:
    """Create a scanner in CLOSED mode; `start` launches the thread. `matching` scores a registration's presses."""
    self._reader = reader
    self._post = post
    self._matching = matching
    self._timings = timings
    self._monotonic = monotonic
    self._thread = threading.Thread(target=self._run, name="scanner", daemon=True)
    self._opened = False  # scanner thread only, like _backoff and _last_status
    self._last_status: FingerStatus | None = None  # the current call's last posted status; None once it ends
    self._backoff = timings.backoff_start  # doubles per ReaderFailure, reset by a completed capture
    self._failing_since: float | None = None  # start of the current ReaderFailure streak, reset with _backoff
    # The REGISTER session, scanner thread only: the _mode_changes it began at, and its next capture's press_id.
    self._session: int | None = None
    self._next_press = 0
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
    self._min_score = 0
    self._resemblance_bar = 0
    self._scoring: ScoringSession | None = None  # the REGISTER session's, until a mode change, merge or failure ends it
    self._outstanding = 0  # its captures not yet delivered
    self._idle: threading.Timer | None = None  # the capture's press timeout, while no results are out
    self._timed_out: CancelToken | None = None  # the capture token the press timeout cancelled
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
      scoring = self._end_scoring()
    self._stopped.set()
    self._wake.set()
    if token is not None:
      token.cancel()
    if scoring is not None:
      scoring.close()

  def set_mode(self, mode: ReaderMode) -> None:
    """Switch mode, cancelling the current call; the same mode again is a no-op so it can't cut a capture.

    Raises:
      ValueError: `mode` is REGISTER, which only `register` enters: a session needs its registrant, whose own prints must
        never count as another person's.
    """
    if mode is ReaderMode.REGISTER:
      raise ValueError("REGISTER is entered through Scanner.register")
    self._switch(mode, None)

  def register(self, employee_id: int) -> None:
    """Enter REGISTER for `employee_id`: a new session, which posts `RegisterGallery`, then `Captured`s and `Pressed`s.

    Each capture posts a `Captured` at once and a `Pressed` once it's scored. The same registrant again is a no-op, as in `set_mode`. The session lasts until `set_mode`, or until a press times
    out, the reader fails or its matching keeps failing (`EnrollFailed`, then READY); a merge ends its scoring.
    """
    self._switch(ReaderMode.REGISTER, employee_id)

  def _switch(self, mode: ReaderMode, registrant: int | None) -> None:
    with self._lock:
      if (mode, registrant) == (self._mode, self._registrant):
        return
      self._mode, self._registrant = mode, registrant
      self._mode_changes += 1
      token = self._token
      scoring = self._end_scoring()
    self._wake.set()
    if token is not None:
      token.cancel()
    if scoring is not None:
      scoring.close()

  def merge(self, presses: Sequence[bytes]) -> None:
    """Queue a merge of one-press templates, in order, for the thread to run next in any mode; posts `Merged`.

    Cancels the current call so the merge needn't wait for a press. A REGISTER session's capturing goes on after it, but
    its scoring ends: the verdict is in, so no later press counts. A failed merge posts `EnrollFailed("error")`.
    """
    with self._lock:
      self._merge = tuple(presses)
      token = self._token
      scoring = self._end_scoring()
    self._wake.set()
    if token is not None:
      token.cancel()
    if scoring is not None:
      scoring.close()

  def set_gallery(self, prints: Sequence[GalleryPrint], margin: int, min_score: int, resemblance_bar: int) -> None:
    """Replace the gallery, restarting a running identify when its prints change; an equal gallery changes nothing (R6).

    Only an identify is restarted: a registration reads the gallery and `resemblance_bar` once as its session begins,
    and cancelling it would lose the user's presses.
    A new `margin` or `min_score`, or a new order alone (every punch moves its employee first), applies from the next
    call: none is worth starting the reader over for.
    """
    gallery = tuple(prints)
    with self._lock:
      self._margin, self._min_score, self._resemblance_bar = margin, min_score, resemblance_bar
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
    except ReaderGone:
      self._opened = False  # re-raised to threading.excepthook (aeth_ext's), which alerts with the traceback
      raise
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
    if mode is ReaderMode.REGISTER:
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
      gallery, margin, min_score = self._gallery, self._margin, self._min_score

    early: list[Matched | NoMatch] = []  # a list, not a nonlocal: pyright doesn't see a closure assign a local

    def on_result(capture: Capture) -> None:
      early.append(_decide(capture, gallery, margin, min_score))
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
    return (early[-1], True) if early else (_decide(capture, gallery, margin, min_score), False)

  def _register_once(self, mode_changes: int) -> None:  # noqa: C901  # one press, read top to bottom
    # One capture of the REGISTER session that began at `mode_changes`; the session's first call posts RegisterGallery.
    # A set_mode or stop supersedes the call and posts nothing (its cancel is the user's Cancel, not a failure: R16),
    # while a timed-out press or a reader error ends the session in READY. A capture is posted and handed to the
    # session's scoring at once, so the reader re-arms without waiting for its scores.
    if self._session != mode_changes:
      self._start_session(mode_changes)
    token = self._reader.new_cancel()
    with self._lock:
      if self._stopping or self._mode_changes != mode_changes or self._merge is not None:
        return  # a queued merge runs first
      self._token = token
      self._arm_idle()
    probe, outcome = self._capture(token)
    with self._lock:
      self._token, self._timed_out = None, None
      if self._idle is not None:
        self._idle.cancel()
        self._idle = None
      if self._stopping or self._mode_changes != mode_changes:
        return
      ended = add = None
      if isinstance(outcome, EnrollFailed):
        self._mode, self._registrant = ReaderMode.READY, None
        ended = self._end_scoring()
      elif probe is not None and (scoring := self._scoring) is not None:  # none once a merge ended it
        outcome = Captured(self._next_press)
        add = scoring, self._next_press, probe
        self._next_press += 1
        self._outstanding += 1
      if outcome is not None:
        self._post(outcome)
    if ended is not None:
      ended.close()
    if add is not None:
      scoring, press_id, probe = add
      scoring.add(press_id, probe)  # outside _lock (module docstring); a no-op if the session ended meanwhile
    if isinstance(outcome, Captured):
      return  # the capture returned after the reader saw the lift: no pause before the next press (owner, 2026-11-17)
    # A retry's finger may still rest, as may a failed or cancelled press's before READY's next match.
    self._wake.wait(self._timings.retry_pause if isinstance(outcome, PressRetry) else self._timings.finger_off_pause)

  def _capture(self, token: CancelToken) -> tuple[bytes | None, PressRetry | EnrollFailed | None]:
    # Split out of _register_once for C901: one capture, as its probe or what to post instead. None for both: a merge,
    # set_mode or stop cancelled it.
    try:
      return self._blocking(self._reader.capture, token, self._on_status), None
    except ScanCancelled:
      with self._lock:
        timed_out = self._timed_out is token  # set before the press timeout cancels
      return None, EnrollFailed("timeout") if timed_out else None
    except RetryScan:
      return None, PressRetry()
    except ReaderTooHot, ReaderFailure:
      return None, EnrollFailed("error")
    finally:
      if self._last_status not in {None, _NO_FINGER}:
        self._post(_NO_FINGER)  # a cancelled capture may never report the lift
      self._last_status = None

  def _start_session(self, mode_changes: int) -> None:
    # Split out of _register_once for C901: the session's scoring, which starts its threads with its first press, and its
    # RegisterGallery. Nothing is decoded or scored up front: a print's presses matter only once a press resembles it.
    # Built under _lock: ScoringSession's constructor takes no lock and starts no thread.
    with self._lock:
      if self._stopping or self._mode_changes != mode_changes or self._merge is not None:
        return  # a queued merge runs first
      others = tuple(p for p in self._gallery if p.employee_id != self._registrant)
      bar = self._resemblance_bar
      scoring = ScoringSession(
        self._matching, others, bar, lambda press: self._delivered(scoring, press), lambda: self._scoring_failed(scoring)
      )
      self._scoring, self._outstanding = scoring, 0
      self._session, self._next_press = mode_changes, 0
      self._post(RegisterGallery(tuple(OtherRef(p.print_id, p.employee_id, p.finger) for p in others), bar))

  def _delivered(self, scoring: ScoringSession, press: Press) -> None:
    # A matcher thread, under the session's lock: a press of the current session is posted; the last one out restarts
    # the press timeout.
    with self._lock:
      if self._scoring is not scoring:
        return
      self._outstanding -= 1
      self._post(Pressed(press))
      if self._outstanding == 0:
        self._arm_idle()

  def _scoring_failed(self, scoring: ScoringSession) -> None:
    # A matcher or timer thread, under the session's lock; the session closed itself. Ends the registration as a reader
    # error does, and supersedes the capture in progress so it posts nothing.
    with self._lock:
      if self._scoring is not scoring:
        return
      self._end_scoring()
      self._mode, self._registrant = ReaderMode.READY, None
      self._mode_changes += 1
      token = self._token
      self._post(EnrollFailed("error"))
    self._wake.set()
    if token is not None:
      token.cancel()
    alert(
      "Fingerprint matching kept failing",
      f"A registration ended after {FAILURE_LIMIT} matching failures",
      in_except_block=False,  # a condition: there's no exception to render
      wait=False,
    )

  def _end_scoring(self) -> ScoringSession | None:
    # Under _lock: the session's scoring ends, for the caller to close outside it.
    scoring, self._scoring, self._outstanding = self._scoring, None, 0
    if self._idle is not None:
      self._idle.cancel()
      self._idle = None
    return scoring

  def _arm_idle(self) -> None:
    # Under _lock: (re)start the press timeout for the capture in progress, unless results are out; the last of them to
    # arrive starts it again (_delivered).
    if self._idle is not None:
      self._idle.cancel()
      self._idle = None
    if (token := self._token) is None or self._outstanding > 0:
      return
    self._idle = threading.Timer(self._timings.enroll_idle_timeout, self._idle_expired, args=(token,))
    self._idle.daemon = True
    self._idle.start()

  def _idle_expired(self, token: CancelToken) -> None:
    with self._lock:
      if self._token is not token or self._outstanding > 0:
        return
      self._timed_out = token
    token.cancel()
