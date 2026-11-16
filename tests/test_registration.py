# Standard library imports
import logging
from uuid import UUID, uuid4

# Third party imports
import pytest
from timeclock_schema.enums import Finger
from timeclock_schema.models import Settings

# First party imports
from timeclock_system.registration import (
  PRESSES,
  WINDOW,
  Done,
  Failure,
  GaveUp,
  Minutiae,
  NextPress,
  OtherPrint,
  OtherRef,
  Press,
  Session,
  TooSimilar,
)

S = Settings()
GOOD = 100  # an agreement score far above the bar
LOW = S.registration_agreement_bar - 1
SIMILAR = S.registration_resemblance_bar
ODD = 2  # the press a test makes fail


def minutiae(n: int = 40, cx: int = WINDOW[0] // 2, cy: int = WINDOW[1] // 2) -> Minutiae:
  """`n` detail points whose centroid is exactly (cx, cy)."""
  pairs = [((cx - k % 10 - 1, cy - k // 10, 0), (cx + k % 10 + 1, cy + k // 10, 0)) for k in range(n // 2)]
  return tuple(m for pair in pairs for m in pair) + (((cx, cy, 0),) if n % 2 else ())


KNOWN: dict[UUID, OtherPrint] = {}  # every print `other` made, so a press can carry the ones it resembles


def press(
  press_id: int,
  *,
  points: Minutiae | None = None,
  vs_session: dict[int, int] | None = None,
  vs_gallery: dict[UUID, tuple[int, ...]] | None = None,
  resembled: dict[UUID, OtherPrint] | None = None,
) -> Press:
  """A press agreeing with every earlier one unless `vs_session` says otherwise.

  Unless `resembled` is given, it carries every print `other` made that it scores the resemblance bar against, as the
  matcher's threads attach them.
  """
  vs_gallery = vs_gallery or {}
  if resembled is None:
    resembled = {pid: KNOWN[pid] for pid, scores in vs_gallery.items() if pid in KNOWN and max(scores) >= SIMILAR}
  return Press(
    press_id=press_id,
    minutiae=minutiae() if points is None else points,
    probe=b"probe-secret",
    vs_session=dict.fromkeys(range(press_id), GOOD) | (vs_session or {}),
    vs_gallery=vs_gallery,
    resembled=resembled,
  )


def other(failing: frozenset[int] = frozenset()) -> OtherPrint:
  """Another person's 5-press print; presses in `failing` agree with none of the others."""
  agreement = tuple(tuple(-1 if i == j else LOW if failing & {i, j} else GOOD for j in range(PRESSES)) for i in range(PRESSES))
  made = OtherPrint(uuid4(), 99, Finger.RIGHT_INDEX, tuple(minutiae() for _ in range(PRESSES)), agreement)
  KNOWN[made.print_id] = made
  return made


def start(*others: OtherPrint) -> Session:
  """A session over `others`' identities alone, in this order, as `RegisterGallery` gives them."""
  return Session(tuple(OtherRef(o.print_id, o.employee_id, o.finger) for o in others), S)


def run(session: Session, presses: list[Press]) -> list[NextPress | TooSimilar | Done | GaveUp]:
  return [session.add(p) for p in presses]


def test_first_five_unjudged():
  assert run(start(), [press(i) for i in range(PRESSES - 1)]) == [NextPress(None)] * (PRESSES - 1)


def test_clean_set_done():
  x = other()
  low = {x.print_id: (SIMILAR - 1,) * PRESSES}
  assert run(start(x), [press(i, vs_gallery=low) for i in range(PRESSES)])[-1] == Done(chosen=(0, 1, 2, 3, 4), flagged=())


@pytest.mark.parametrize(
  ("points", "failure"),
  [
    (minutiae(S.registration_min_detail_points - 1), Failure.TOO_FEW),
    (minutiae(S.registration_max_detail_points + 1), Failure.TOO_MANY),
    (minutiae(cx=5), Failure.OFF_CENTRE),
  ],
)
def test_failing_press_replaced(points: Minutiae, failure: Failure):
  session = start()
  presses = [press(i, points=points if i == ODD else None) for i in range(PRESSES)]
  assert run(session, presses)[-1] == NextPress(failure)
  assert session.add(press(5)) == Done(chosen=(0, 1, 3, 4, 5), flagged=())


def test_disagreeing_press_replaced():
  session = start()
  presses = [press(i) for i in range(4)] + [press(4, vs_session=dict.fromkeys(range(4), LOW))]
  assert run(session, presses)[-1] == NextPress(Failure.DISAGREES)
  assert session.add(press(5, vs_session={4: LOW})) == Done(chosen=(0, 1, 2, 3, 5), flagged=())


def test_edge_touch_is_off_centre_before_short_of_detail():
  edge = minutiae(S.registration_min_detail_points - 1, cx=5)
  presses = [press(i, points=edge if i == ODD else None) for i in range(PRESSES)]
  assert run(start(), presses)[-1] == NextPress(Failure.OFF_CENTRE)


def test_worst_replaced_first():
  session = start()
  presses = [
    press(0),
    press(1, points=minutiae(S.registration_min_detail_points - 1)),
    press(2, points=minutiae(cx=5)),
    press(3, vs_session=dict.fromkeys(range(3), LOW)),
    press(4, vs_session={3: LOW}),
  ]
  assert run(session, presses)[-1] == NextPress(Failure.OFF_CENTRE)
  assert session.add(press(5, vs_session={3: LOW})) == NextPress(Failure.TOO_FEW)
  assert session.add(press(6, vs_session={3: LOW})) == NextPress(Failure.DISAGREES)
  assert session.add(press(7, vs_session={3: LOW})) == Done(chosen=(0, 4, 5, 6, 7), flagged=())


def test_press_check_failures_never_give_up():
  free = [
    (minutiae(S.registration_min_detail_points - 1), Failure.TOO_FEW),
    (minutiae(cx=5), Failure.OFF_CENTRE),
    (minutiae(S.registration_max_detail_points + 1), Failure.TOO_MANY),
  ]
  n = len(free) * (S.registration_disagreement_retries + 1)
  session = start()
  run(session, [press(i) for i in range(PRESSES - 1)])
  verdicts = run(session, [press(PRESSES - 1 + k, points=free[k % len(free)][0]) for k in range(n)])
  assert verdicts == [NextPress(free[k % len(free)][1]) for k in range(n)]
  assert session.add(press(PRESSES - 1 + n)) == Done(chosen=(0, 1, 2, 3, PRESSES - 1 + n), flagged=())
  assert session.replaced == n


def test_gives_up_after_the_disagreeing_presses():
  limit, light = S.registration_disagreement_retries, minutiae(S.registration_min_detail_points - 1)
  session = start()
  run(session, [press(i) for i in range(PRESSES - 1)])
  ids = iter(range(PRESSES - 1, PRESSES + 2 * limit))
  verdicts: list[NextPress | TooSimilar | Done | GaveUp] = []
  for k in range(limit + 1):
    i = next(ids)
    verdicts.append(session.add(press(i, vs_session=dict.fromkeys(range(i), LOW))))
    if k < limit:
      verdicts.append(session.add(press(next(ids), points=light)))  # free: it doesn't count toward giving up
  assert verdicts == [NextPress(Failure.DISAGREES), NextPress(Failure.TOO_FEW)] * limit + [GaveUp()]


def test_best_presses_ranked_by_agreement_alone():
  # 0-2 agree with each other; 3 is off centre but agrees with them; 4 and 6 agree with nothing, 6 the lower.
  session = start()
  run(
    session,
    [
      press(0),
      press(1),
      press(2),
      press(3, points=minutiae(cx=5)),
      press(4, vs_session=dict.fromkeys(range(4), LOW)),
      press(5, vs_session={4: LOW}),
      press(6, vs_session=dict.fromkeys(range(6), LOW) | {0: LOW - 10}),
    ],
  )
  assert tuple(p.press_id for p in session.best()) == (0, 1, 2, 3, 5)


def test_resembled_lists_every_print_some_press_resembles():
  x, y, z = uuid4(), uuid4(), uuid4()
  session = start()
  run(session, [press(0, vs_gallery={x: (0, SIMILAR), y: (SIMILAR - 1,) * PRESSES}), press(1, vs_gallery={z: (SIMILAR,)})])
  assert session.resembled == {x, z}


def test_single_press_resemblance_is_a():
  x = other()
  session = start(x)
  presses = [press(i, vs_gallery={x.print_id: (SIMILAR, 0, 0, 0, 0)} if i == ODD else None) for i in range(PRESSES)]
  assert run(session, presses)[-1] == NextPress(Failure.DISAGREES)
  assert session.add(press(5)) == Done(chosen=(0, 1, 3, 4, 5), flagged=())


def test_stray_press_elsewhere_is_b_not_a():
  x = other(failing=frozenset({3}))
  on_stray = {x.print_id: (0, 0, 0, SIMILAR, 0)}
  presses = [press(i, vs_gallery=on_stray if i in {2, 4} else None) for i in range(PRESSES)]
  assert run(start(x), presses)[-1] == Done(chosen=(0, 1, 2, 3, 4), flagged=(x,))


def test_whole_finger_resembles_is_c():
  # 0 and 1 resemble them and agree with 2-4, which don't: the finger as a whole resembles them.
  x = other()
  on_x = {x.print_id: (0, 0, SIMILAR, 0, SIMILAR)}
  presses = [press(i, vs_gallery=on_x if i in {0, 1} else None) for i in range(PRESSES)]
  assert run(start(x), presses)[-1] == TooSimilar(other=x, chosen=(0, 1, 2, 3, 4), flagged=(), also_similar=())


@pytest.mark.parametrize(
  "like_x",
  [{0: (SIMILAR, 0, 0, 0, 0), 1: (SIMILAR, 0, 0, 0, 0)}, {0: (SIMILAR, SIMILAR, 0, 0, 0), 1: (0, 0, SIMILAR, 0, SIMILAR)}],
  ids=["one of their presses", "several of their presses"],
)
def test_own_stray_presses_behind_a_resemblance_are_replaced(like_x: dict[int, tuple[int, ...]]):
  # 0 and 1 resemble them and agree with each other, but not with 2-4: the registrant's own stray presses, not C. The
  # one agreeing less goes first.
  x = other()
  stray = {0: LOW, 1: LOW - 10}
  presses = [
    press(i, vs_session=None if i in like_x else stray, vs_gallery={x.print_id: like_x[i]} if i in like_x else None)
    for i in range(PRESSES)
  ]
  session = start(x)
  assert run(session, presses)[-1] == NextPress(Failure.DISAGREES)
  assert [p.press_id for p in session.kept] == [0, 2, 3, 4]


def test_every_press_resembling_is_c():
  # No kept press stands apart from the resemblance to judge the others against.
  x = other()
  presses = [press(i, vs_gallery={x.print_id: (SIMILAR, 0, 0, 0, 0)}) for i in range(PRESSES)]
  assert run(start(x), presses)[-1] == TooSimilar(other=x, chosen=(0, 1, 2, 3, 4), flagged=(), also_similar=())


def test_too_similar_keeps_flags():
  x, careless = other(), other(failing=frozenset({0, 1, 2}))
  on_both = {x.print_id: (SIMILAR, SIMILAR, 0, 0, 0), careless.print_id: (SIMILAR,) * PRESSES}
  presses = [press(i, vs_gallery=on_both if i in {0, 1} else None) for i in range(PRESSES)]
  assert run(start(x, careless), presses)[-1] == TooSimilar(other=x, chosen=(0, 1, 2, 3, 4), flagged=(careless,), also_similar=())


def test_too_similar_carries_every_resembled_print():
  # The registrant's finger may later be identified as any print it resembles, so the verdict carries each.
  x, y, z, careless = other(), other(), other(), other(failing=frozenset({0, 1, 2}))
  on_all = {
    x.print_id: (SIMILAR, SIMILAR, 0, 0, 0),
    y.print_id: (SIMILAR + 2, 0, 0, 0, 0),
    z.print_id: (0, SIMILAR + 1, 0, 0, 0),
    careless.print_id: (SIMILAR,) * PRESSES,
  }
  presses = [press(i, vs_gallery=on_all if i in {0, 1} else None) for i in range(PRESSES)]
  assert run(start(x, y, z, careless), presses)[-1] == TooSimilar(
    other=y, chosen=(0, 1, 2, 3, 4), flagged=(careless,), also_similar=(z, x)
  )


def test_stray_pairs_left_out_of_c():
  # One new press on their stray press, two on healthy ones: C from the healthy pairs, and the print isn't flagged too.
  x = other(failing=frozenset({3}))
  on_x = {0: (0, 0, 0, SIMILAR, 0), 1: (SIMILAR, 0, 0, 0, 0), 2: (0, SIMILAR, 0, 0, 0)}
  presses = [press(i, vs_gallery={x.print_id: on_x[i]} if i in on_x else None) for i in range(PRESSES)]
  assert run(start(x), presses)[-1] == TooSimilar(other=x, chosen=(0, 1, 2, 3, 4), flagged=(), also_similar=())


def test_stray_pairs_left_out_of_a():
  # Two new presses on their stray press, one on a healthy one: that one alone resembles them (A); the stray pairs
  # never count against the registrant, so once it's replaced the print is flagged (B).
  x = other(failing=frozenset({3}))
  on_stray, on_healthy = {x.print_id: (0, 0, 0, SIMILAR, 0)}, {x.print_id: (SIMILAR, 0, 0, 0, 0)}
  session = start(x)
  presses = [press(i, vs_gallery=on_stray if i in {0, 1} else on_healthy if i == ODD else None) for i in range(PRESSES)]
  assert run(session, presses)[-1] == NextPress(Failure.DISAGREES)
  assert [p.press_id for p in session.kept] == [0, 1, 3, 4]
  assert session.add(press(5)) == Done(chosen=(0, 1, 3, 4, 5), flagged=(x,))


def test_stray_resemblance_through_a_replacement_is_b():
  # Review Focus 1: a press like their stray press, then, after another press is replaced, its replacement like it too.
  x = other(failing=frozenset({3}))
  on_stray = {x.print_id: (0, 0, 0, SIMILAR, 0)}
  light = minutiae(S.registration_min_detail_points - 1)
  session = start(x)
  presses = [press(i, points=light if i == PRESSES - 1 else None, vs_gallery=on_stray if i == ODD else None) for i in range(PRESSES)]
  assert run(session, presses)[-1] == NextPress(Failure.TOO_FEW)
  assert session.add(press(5, vs_gallery=on_stray)) == Done(chosen=(0, 1, 2, 3, 5), flagged=(x,))


def test_careless_other_print_is_b():
  x = other(failing=frozenset({0, 1, 2}))
  on_x = {x.print_id: (0, 0, SIMILAR, 0, SIMILAR)}
  presses = [press(i, vs_gallery=on_x if i in {0, 1} else None) for i in range(PRESSES)]
  assert run(start(x), presses)[-1] == Done(chosen=(0, 1, 2, 3, 4), flagged=(x,))


def test_own_prints_excluded():
  # The caller leaves the registrant's prints out of `others`; scores against them are ignored.
  own = uuid4()
  presses = [press(i, vs_gallery={own: (GOOD,) * PRESSES}) for i in range(PRESSES)]
  assert run(start(other()), presses)[-1] == Done(chosen=(0, 1, 2, 3, 4), flagged=())


def test_no_gallery():
  session = start()
  presses = [press(i, points=minutiae(cx=5) if i == 0 else None) for i in range(PRESSES)]
  assert run(session, presses)[-1] == NextPress(Failure.OFF_CENTRE)
  assert session.add(press(5)) == Done(chosen=(1, 2, 3, 4, 5), flagged=())


def test_bars_from_settings():
  presses = [press(i) for i in range(PRESSES)]
  assert run(Session((), Settings(registration_agreement_bar=GOOD + 1)), presses)[-1] == NextPress(Failure.DISAGREES)
  assert run(Session((), Settings(registration_min_detail_points=41)), presses)[-1] == NextPress(Failure.TOO_FEW)
  assert run(Session((), Settings(registration_agreement_needed=0, registration_agreement_bar=GOOD + 1)), presses)[-1] == Done(
    chosen=(0, 1, 2, 3, 4), flagged=()
  )


def test_kept_follows_replacements():
  session = start()
  run(session, [press(i, points=minutiae(cx=5) if i == 1 else None) for i in range(PRESSES)])
  assert [p.press_id for p in session.kept] == [0, 2, 3, 4]


def test_reprs_hide_biometrics():
  # R15: detail points and probes are biometric, like templates.
  p, x = press(0), other()
  assert "probe-secret" not in repr(p)
  assert str(p.minutiae[0]) not in repr(p)
  assert str(x.presses[0][0]) not in repr(x)


def test_logs_each_press(caplog: pytest.LogCaptureFixture):
  with caplog.at_level(logging.INFO, logger="timeclock_system.registration"):
    run(start(), [press(i) for i in range(PRESSES)])
  assert len(caplog.records) == PRESSES
  assert "40 detail points" in caplog.records[0].getMessage()
  assert "done" in caplog.records[-1].getMessage()
  assert "probe-secret" not in caplog.text


def test_session_needs_no_other_prints_up_front():
  # Only identities up front: five agreeing presses that resemble nobody pass without any other print's presses.
  refs = tuple(OtherRef(uuid4(), 99, Finger.RIGHT_INDEX) for _ in range(3))
  presses = [press(i, vs_gallery={r.print_id: (SIMILAR - 1,) * PRESSES for r in refs}) for i in range(PRESSES)]
  assert run(Session(refs, S), presses)[-1] == Done(chosen=(0, 1, 2, 3, 4), flagged=())


def test_a_resembled_prints_failing_presses_come_from_the_press():
  # Case B from the print the presses brought: its stray press 3 is all they resemble.
  x = other(failing=frozenset({3}))
  on_stray = {x.print_id: (0, 0, 0, SIMILAR, 0)}
  presses = [press(i, vs_gallery=on_stray if i in {2, 4} else None) for i in range(PRESSES)]
  assert presses[2].resembled == {x.print_id: x}
  assert run(start(x), presses)[-1] == Done(chosen=(0, 1, 2, 3, 4), flagged=(x,))


def test_a_print_first_resembled_by_a_replaced_press_is_still_known():
  # Press 2 brings x and is replaced (off centre); press 5 resembles x too but brings nothing: x is already known.
  x = other()
  on_x = {x.print_id: (SIMILAR, 0, 0, 0, 0)}
  off = minutiae(cx=0)
  presses = [press(i, points=off if i == ODD else None, vs_gallery=on_x if i == ODD else None) for i in range(PRESSES)]
  s = start(x)
  assert run(s, presses)[-1] == NextPress(Failure.OFF_CENTRE)
  assert s.add(press(5, vs_gallery=on_x, resembled={})) == NextPress(Failure.DISAGREES)
