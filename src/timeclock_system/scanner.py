"""The fingerprint scanner thread (spec §6.2): one daemon thread owns the reader; the event loop drives its mode.

Concurrency: `_lock` guards the fields `set_mode`/`set_gallery`/`stop`/`busy_seconds` share with the thread. It is never
held across a blocking reader call or a `post`/`on_fatal` call. Every pause waits on `_wake`, which a mode change or
`stop` sets, so pauses end early.
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

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Sequence

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class GalleryPrint:
  """One enrolled print as the matcher consumes it."""

  print_id: UUID
  employee_id: int
  template: bytes = field(repr=False)  # R15: keep the template out of tracebacks and alerts


@dataclass(frozen=True)
class Matched:
  """A capture matched an enrolled print."""

  print_id: UUID
  employee_id: int


@dataclass(frozen=True)
class NoMatch:
  """A capture matched no enrolled print."""


@dataclass(frozen=True)
class Retry:
  """A capture was unusable; `message` tells the user what to do."""

  message: str


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
  """Enrollment ended without a template."""

  reason: Literal["timeout", "error"]


type ScannerEvent = Matched | NoMatch | Retry | FingerStatus | ReaderPaused | EnrollStage | Enrolled | EnrollFailed


class ReaderMode(StrEnum):
  """What the scanner thread does with the reader."""

  CLOSED = "closed"
  READY = "ready"
  ENROLL = "enroll"


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
    on_result: Callable[[UUID | None], None],
  ) -> UUID | None:
    """Capture one print and match it against `gallery`; return the matched print id or None.

    `on_status` gets every finger-status change. `on_result` gets the match the moment it's made, while the finger may
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
    self._lock = threading.Lock()
    self._wake = threading.Event()
    # Guarded by _lock:
    self._mode = ReaderMode.CLOSED
    self._mode_changes = 0  # lets an enrollment tell a set_mode cancel from its own idle timeout
    self._gallery: tuple[GalleryPrint, ...] = ()
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
    self._wake.set()
    if token is not None:
      token.cancel()

  def set_mode(self, mode: ReaderMode) -> None:
    """Switch mode, cancelling the current call; the same mode again is a no-op so it can't cut a capture."""
    with self._lock:
      if mode is self._mode:
        return
      self._mode = mode
      self._mode_changes += 1
      token = self._token
    self._wake.set()
    if token is not None:
      token.cancel()

  def set_gallery(self, prints: Sequence[GalleryPrint]) -> None:
    """Replace the gallery, restarting a running identify; an equal gallery changes nothing (R6).

    Only an identify is restarted: an enrollment doesn't use the gallery, and cancelling it would lose the user's stages.
    """
    gallery = tuple(prints)
    with self._lock:
      if gallery == self._gallery:
        return
      self._gallery = gallery
      token = self._token if self._mode is ReaderMode.READY else None
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
      try:
        if mode is ReaderMode.CLOSED:
          if self._opened:
            self._opened = False
            self._blocking(self._reader.close)
          self._wake.wait()
          continue
        if not self._opened:
          self._blocking(self._reader.open)
          self._opened = True
        if mode is ReaderMode.ENROLL:
          self._enroll_once(mode_changes)
        else:
          self._ready_once()
      except ScanCancelled:
        pass  # the re-arm timer, a mode or gallery change, or stop; the loop re-reads the state
      except ReaderTooHot:
        # The patched reader never reports too hot (§7), so one that does means the patch isn't working on this clock.
        alert("Fingerprint reader paused itself (too hot)", f"Paused for {self._timings.too_hot_pause:g} s", wait=False)
        self._post(ReaderPaused(self._timings.too_hot_pause))
        self._wake.wait(self._timings.too_hot_pause)
      except ReaderFailure as e:
        self._on_failure(e)

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
    # (the re-arm, a gallery change) returns before it; the pause keeps a bounce or that resting finger from being read
    # as the next press.
    self._wake.wait(self._timings.finger_off_pause)

  def _identify_once(self) -> tuple[Matched | NoMatch | Retry, bool]:
    """The one matcher boundary (R36.3): the only `reader.identify` call, which version 2 replaces.

    Returns the result and whether it was already posted: libfprint reports a match while the finger is still down,
    and it's posted then, so the screen acts before the lift.

    Raises:
      ScanCancelled: the mode changed before the call, or its token was cancelled before a result.
    """
    token = self._reader.new_cancel()
    with self._lock:
      if self._stopping or self._mode is not ReaderMode.READY:
        raise ScanCancelled
      self._token = token
      gallery = self._gallery

    def result_of(print_id: UUID | None) -> Matched | NoMatch:
      match = next((p for p in gallery if p.print_id == print_id), None)
      return NoMatch() if match is None else Matched(match.print_id, match.employee_id)

    early: list[Matched | NoMatch] = []  # a list, not a nonlocal: pyright doesn't see a closure assign a local

    def on_result(print_id: UUID | None) -> None:
      early.append(result_of(print_id))
      self._post(early[-1])

    # The periodic re-arm bounds every identify, so a call that outlives it is hung (busy_seconds, §6.2).
    rearm = threading.Timer(self._timings.rearm_every, token.cancel)
    rearm.daemon = True
    rearm.start()
    try:
      print_id = self._blocking(self._reader.identify, gallery, token, self._on_status, on_result)
    except RetryScan as e:
      return Retry(str(e)), False
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
    return (early[-1], True) if early else (result_of(print_id), False)

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
