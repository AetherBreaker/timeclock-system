"""Registration's decisions (spec `2026-11-05-registration-quality-design.md`): judge the presses, then the finger.

Pure: the scanner thread captures and scores presses, the controller acts on the verdicts. Every bar comes from the
`Settings` a session starts with, so a change on the database applies from the next registration.
"""

# Standard library imports
import logging
import math
import statistics
from collections.abc import Mapping, Sequence  # noqa: TC003  # the dataclass annotations are evaluated at runtime
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID  # noqa: TC003  # the dataclass annotations are evaluated at runtime

# Third party imports
from timeclock_schema.enums import Finger  # noqa: TC002  # the dataclass annotations are evaluated at runtime

if TYPE_CHECKING:
  # Third party imports
  from timeclock_schema.models import Settings

LOGGER = logging.getLogger(__name__)

WINDOW = (384, 290)  # the uru4000's image in pixels; minutiae are NIST coordinates (y up from the bottom edge)
PRESSES = 5  # presses in a template

type Minutiae = tuple[tuple[int, int, int], ...]
"""A press's detail points: (x, y, theta)."""


class Failure(StrEnum):
  """Why a press is replaced, worst first: a bad press drags down the others' agreement, so it goes before them."""

  TOO_FEW = "too few detail points"
  TOO_MANY = "too many detail points"
  OFF_CENTRE = "off centre"
  DISAGREES = "disagrees"


@dataclass(frozen=True)
class Press:
  """One captured press, scored on the scanner thread."""

  press_id: int  # the session's capture count, from 0
  minutiae: Minutiae = field(repr=False)  # R15: biometric, like the probe
  probe: bytes = field(repr=False)  # R15: biometric; the single-press print a template is merged from
  vs_session: Mapping[int, int]  # agreement with each earlier press by press_id: the higher of both directions' scores
  vs_gallery: Mapping[UUID, tuple[int, ...]]  # score against each press of each other person's print, by print id


@dataclass(frozen=True)
class OtherPrint:
  """Another person's print, as registration compares against it."""

  print_id: UUID
  employee_id: int
  finger: Finger
  presses: tuple[Minutiae, ...] = field(repr=False)  # R15: biometric
  agreement: tuple[tuple[int, ...], ...]  # its presses against each other, as `Press.vs_session`; diagonal -1


@dataclass(frozen=True)
class NextPress:
  """Ask for another press; `failure` says why one was dropped (None while the first presses come in)."""

  failure: Failure | None


@dataclass(frozen=True)
class TooSimilar:
  """Case C: the finger as a whole resembles `other`; an admin allows or rejects it. `chosen`, `flagged` as `Done`.

  `also_similar`: the other prints it resembles as a whole, most resembling first. With `other` and `flagged`, every
  print the registrant's finger may be identified as.
  """

  other: OtherPrint
  chosen: tuple[int, ...]
  flagged: tuple[OtherPrint, ...]
  also_similar: tuple[OtherPrint, ...]


@dataclass(frozen=True)
class Done:
  """The kept presses (`chosen`, by press_id) pass; the `flagged` prints need re-registering (case B)."""

  chosen: tuple[int, ...]
  flagged: tuple[OtherPrint, ...]


@dataclass(frozen=True)
class GaveUp:
  """Every allowed extra press went by without a passing set."""


type Verdict = NextPress | TooSimilar | Done | GaveUp


def press_failure(minutiae: Minutiae, settings: Settings) -> Failure | None:
  """The checks a press needs no other press for: its detail-point count, then their centroid's distance from centre."""
  if not minutiae or len(minutiae) < settings.registration_min_detail_points:
    return Failure.TOO_FEW
  if len(minutiae) > settings.registration_max_detail_points:
    return Failure.TOO_MANY
  x, y = statistics.fmean(m[0] for m in minutiae), statistics.fmean(m[1] for m in minutiae)
  if math.hypot(x - WINDOW[0] / 2, y - WINDOW[1] / 2) > settings.registration_centre_tolerance:
    return Failure.OFF_CENTRE
  return None


def failing_presses(p: OtherPrint, settings: Settings) -> frozenset[int]:
  """Indexes of `p`'s presses failing `press_failure` or agreement, judged as a registration judges its own."""
  return frozenset(
    i
    for i, minutiae in enumerate(p.presses)
    if press_failure(minutiae, settings) is not None
    or sum(s >= settings.registration_agreement_bar for j, s in enumerate(p.agreement[i]) if j != i)
    < settings.registration_agreement_needed
  )


class Session:
  """One registration's presses: judged once `PRESSES` are kept, then compared with other people's prints."""

  def __init__(self, others: Sequence[OtherPrint], settings: Settings) -> None:
    """`others` leaves out the registrant's own prints (a re-registration's old print, their other fingers)."""
    self._others = others
    self._failing = {p.print_id: failing_presses(p, settings) for p in others}
    self._settings = settings
    self._kept: list[Press] = []
    self._replaced = 0

  @property
  def kept(self) -> tuple[Press, ...]:
    """The presses kept so far, in order; after `Done` or `TooSimilar`, the ones to merge."""
    return tuple(self._kept)

  def add(self, press: Press) -> Verdict:  # noqa: C901, PLR0912  # one decision, read top to bottom
    """Keep `press`; once `PRESSES` are kept, replace the worst failing one, else judge the finger as a whole.

    Resemblance pairs a kept press with another person's press scoring at least the resemblance bar. Per other print:
    when it mostly fails its own checks, or only its failing (stray) presses are resembled, it's flagged (case B), so no
    careless existing print can fail the registrant's presses. Else the pairs on its stray presses are ignored and the
    rest decide: one kept press alone is replaced (case A), several mean the finger resembles it (case C).
    """
    s = self._settings
    self._kept.append(press)
    kept = self._kept
    agreement = {p.press_id: [q.vs_session.get(p.press_id, p.vs_session.get(q.press_id, 0)) for q in kept if q is not p] for p in kept}
    verdict: Verdict = NextPress(None)
    outcome = "waiting for more presses"
    if len(kept) >= PRESSES:
      failures: dict[int, Failure] = {}
      for p in kept:
        failure = press_failure(p.minutiae, s)
        if failure is None and sum(a >= s.registration_agreement_bar for a in agreement[p.press_id]) < s.registration_agreement_needed:
          failure = Failure.DISAGREES
        if failure is not None:
          failures[p.press_id] = failure
      flagged: list[OtherPrint] = []
      similar: list[tuple[int, OtherPrint]] = []
      if not failures:
        for other in self._others:
          pairs = [
            (p.press_id, j, score)
            for p in kept
            for j, score in enumerate(p.vs_gallery.get(other.print_id, ()))
            if score >= s.registration_resemblance_bar
          ]
          if not pairs:
            continue
          failing = self._failing[other.print_id]
          healthy = [(i, score) for i, j, score in pairs if j not in failing]
          if len(failing) * 2 > len(other.presses) or not healthy:
            flagged.append(other)
          elif len(new := {i for i, _ in healthy}) == 1:
            failures[new.pop()] = Failure.DISAGREES  # case A: the message for a press whose own checks pass
          else:
            similar.append((max(score for _, score in healthy), other))
      chosen = tuple(p.press_id for p in kept)
      if failures:
        worst = min(failures, key=lambda i: (list(Failure).index(failures[i]), max(agreement[i], default=0)))
        if self._replaced >= s.registration_extra_presses:
          verdict, outcome = GaveUp(), f"gave up, press {worst} {failures[worst]}"
        else:
          self._replaced += 1
          self._kept = [p for p in kept if p.press_id != worst]
          verdict, outcome = NextPress(failures[worst]), f"replacing press {worst}: {failures[worst]}"
      elif similar:
        similar.sort(key=lambda pair: pair[0], reverse=True)  # stable: equal scores keep the gallery's order
        other, *rest = (o for _, o in similar)
        verdict, outcome = TooSimilar(other, chosen, tuple(flagged), tuple(rest)), f"too similar to print {other.print_id}"
      else:
        verdict, outcome = Done(chosen, tuple(flagged)), "done"
      if flagged:
        outcome += f"; re-register {', '.join(str(p.print_id) for p in flagged)}"
    best_other = max((x for o in self._others for x in press.vs_gallery.get(o.print_id, ())), default=None)
    x, y = (statistics.fmean(m[i] for m in press.minutiae) if press.minutiae else math.nan for i in (0, 1))
    LOGGER.info(
      "Registration press %d: %d detail points, %.0f px off centre, best own %s, best other %s; %s",
      press.press_id,
      len(press.minutiae),
      math.hypot(x - WINDOW[0] / 2, y - WINDOW[1] / 2),
      max(agreement[press.press_id], default=None),
      best_other,
      outcome,
    )
    return verdict
