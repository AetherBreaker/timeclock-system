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
  """Why a press is replaced.

  A press failing its own checks (`press_failure`) is replaced as soon as it's judged; `DISAGREES` waits for `PRESSES`
  kept presses, since a later press may yet agree with it. A registration never gives up (owner, 2026-11-17): it ends
  on a passing set, the press timeout or Cancel.
  """

  OFF_CENTRE = "off centre"
  TOO_FEW = "too few detail points"
  TOO_MANY = "too many detail points"
  DISAGREES = "disagrees"


@dataclass(frozen=True)
class Press:
  """One captured press, scored on the matcher threads."""

  press_id: int  # the session's capture count, from 0
  minutiae: Minutiae = field(repr=False)  # R15: biometric, like the probe
  probe: bytes = field(repr=False)  # R15: biometric; the single-press print a template is merged from
  vs_session: Mapping[int, int]  # agreement with each earlier press by press_id: the higher of both directions' scores
  vs_gallery: Mapping[UUID, tuple[int, ...]]  # score against each press of each other person's print, by print id
  # The other prints it scores the resemblance bar against, with their presses and agreement: how a session learns them.
  resembled: Mapping[UUID, OtherPrint] = field(default_factory=dict, repr=False)  # R15: biometric


@dataclass(frozen=True)
class OtherRef:
  """Another person's print by identity alone: what a session knows of it until a press resembles it."""

  print_id: UUID
  employee_id: int
  finger: Finger


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
  print its kept presses resemble.
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


type Verdict = NextPress | TooSimilar | Done


def press_failure(minutiae: Minutiae, settings: Settings) -> Failure | None:
  """The checks a press needs no other press for: its centroid's distance from centre, then its detail-point count.

  Centre first, as `Failure` orders them: a touch at the edge is both off centre and short of detail, and centring fixes both.
  """
  if not minutiae:
    return Failure.TOO_FEW
  x, y = statistics.fmean(m[0] for m in minutiae), statistics.fmean(m[1] for m in minutiae)
  if math.hypot(x - WINDOW[0] / 2, y - WINDOW[1] / 2) > settings.registration_centre_tolerance:
    return Failure.OFF_CENTRE
  if len(minutiae) < settings.registration_min_detail_points:
    return Failure.TOO_FEW
  if len(minutiae) > settings.registration_max_detail_points:
    return Failure.TOO_MANY
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
  """One registration's presses: each checked alone as it comes, then together once `PRESSES` are kept."""

  def __init__(self, others: Sequence[OtherRef], settings: Settings) -> None:
    """`others`: every print but the registrant's (a re-registration's old print, their other fingers), in gallery order.

    A print's presses and agreement only matter once a press resembles it, so they arrive on the first press that does
    (`Press.resembled`), kept or replaced, and its failing presses are worked out then.
    """
    self._others = others
    self._known: dict[UUID, OtherPrint] = {}
    self._failing: dict[UUID, frozenset[int]] = {}
    self._settings = settings
    self._kept: list[Press] = []
    self._presses: list[Press] = []  # every press added, kept or replaced, for `best`
    self._replaced = 0

  @property
  def kept(self) -> tuple[Press, ...]:
    """The presses kept so far, in order; after `Done` or `TooSimilar`, the ones to merge."""
    return tuple(self._kept)

  @property
  def replaced(self) -> int:
    """How many presses were dropped and asked for again, for any `Failure`."""
    return self._replaced

  @property
  def resembled(self) -> frozenset[UUID]:
    """Every other print some press scored the resemblance bar against: the registrant's finger may be identified as it."""
    bar = self._settings.registration_resemblance_bar
    return frozenset(pid for p in self._presses for pid, scores in p.vs_gallery.items() if any(x >= bar for x in scores))

  def best(self) -> tuple[Press, ...]:
    """The `PRESSES` best of every press so far, in order, for an admin's forced save; none of the checks apply to it.

    Ranked by agreement alone: how many of the other presses each agrees with, then its scores, best first.
    """
    bar = self._settings.registration_agreement_bar

    def rank(p: Press) -> tuple[int, list[int]]:
      scores = sorted(
        (q.vs_session.get(p.press_id, p.vs_session.get(q.press_id, 0)) for q in self._presses if q is not p), reverse=True
      )
      return sum(x >= bar for x in scores), scores

    return tuple(sorted(sorted(self._presses, key=rank, reverse=True)[:PRESSES], key=lambda p: p.press_id))

  def add(self, press: Press) -> Verdict:  # noqa: C901, PLR0912, PLR0915  # one decision, read top to bottom
    """Replace `press` at once if it fails its own checks, else keep it; once `PRESSES` are kept, judge them together.

    Of the kept presses agreeing with too few others, the one agreeing least is replaced; with none, the finger is judged
    as a whole against other people's prints. Resemblance pairs a kept press with another person's press scoring at least the resemblance bar. Per other print:
    when it mostly fails its own checks, or only its failing (stray) presses are resembled, it's flagged (case B), so no
    careless existing print can fail the registrant's presses. Else the pairs on its stray presses are ignored and the
    rest decide: one kept press alone is replaced (case A). Of several, each agreeing with fewer than
    `registration_agreement_needed` of the kept presses that don't resemble it is the registrant's own stray press,
    replaced as in A; with none, or with every kept press resembling it, the finger resembles it (case C).
    """
    s = self._settings
    for pid, other in press.resembled.items():
      if pid not in self._known:
        self._known[pid], self._failing[pid] = other, failing_presses(other, s)
    self._presses.append(press)
    verdict: Verdict = NextPress(None)
    outcome = "waiting for more presses"
    if (own := press_failure(press.minutiae, s)) is not None:
      # Its own checks need no other press: replaced as it comes, so the count and advice don't wait for five.
      self._replaced += 1
      verdict, outcome = NextPress(own), f"replacing press {press.press_id}: {own}"
    else:
      self._kept.append(press)
    kept = self._kept
    # agreement[i][j]: press i's with press j, by press_id.
    agreement = {
      p.press_id: {q.press_id: q.vs_session.get(p.press_id, p.vs_session.get(q.press_id, 0)) for q in kept if q is not p} for p in kept
    }
    if len(kept) >= PRESSES:  # only when this press was kept: a replaced one leaves fewer
      disagreeing = {
        p.press_id
        for p in kept
        if sum(a >= s.registration_agreement_bar for a in agreement[p.press_id].values()) < s.registration_agreement_needed
      }
      flagged: list[OtherPrint] = []
      similar: list[tuple[int, OtherPrint]] = []
      if not disagreeing:
        for ref in self._others:
          pairs = [
            (p.press_id, j, score)
            for p in kept
            for j, score in enumerate(p.vs_gallery.get(ref.print_id, ()))
            if score >= s.registration_resemblance_bar
          ]
          if not pairs:
            continue
          if (other := self._known.get(ref.print_id)) is None:  # the matcher attaches every print at the bar
            LOGGER.warning("Registration: print %s is resembled but none of its presses arrived; not judged", ref.print_id)
            continue
          failing = self._failing[other.print_id]
          healthy = [(i, score) for i, j, score in pairs if j not in failing]
          if len(failing) * 2 > len(other.presses) or not healthy:
            flagged.append(other)
          elif len(new := {i for i, _ in healthy}) == 1:
            disagreeing.add(new.pop())  # case A: the message for a press whose own checks pass
          elif len(new) < len(kept) and (
            strays := [
              i
              for i in new
              if sum(a >= s.registration_agreement_bar for j, a in agreement[i].items() if j not in new)
              < s.registration_agreement_needed
            ]
          ):
            disagreeing.update(strays)  # the registrant's own stray presses
          else:
            similar.append((max(score for _, score in healthy), other))
      chosen = tuple(p.press_id for p in kept)
      if disagreeing:
        # Lowest agreement: the best score, then the next best (two strays agreeing only with each other tie on the best);
        # a tie goes to the earlier press.
        worst = min((i for i in chosen if i in disagreeing), key=lambda i: sorted(agreement[i].values(), reverse=True))
        self._replaced += 1
        self._kept = [p for p in kept if p.press_id != worst]
        verdict, outcome = NextPress(Failure.DISAGREES), f"replacing press {worst}: {Failure.DISAGREES}"
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
      max((q.vs_session.get(press.press_id, press.vs_session.get(q.press_id, 0)) for q in kept if q is not press), default=None),
      best_other,
      outcome,
    )
    return verdict
