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


def press(
  press_id: int,
  *,
  points: Minutiae | None = None,
  vs_session: dict[int, int] | None = None,
  vs_gallery: dict[UUID, tuple[int, ...]] | None = None,
) -> Press:
  """A press agreeing with every earlier one unless `vs_session` says otherwise."""
  return Press(
    press_id=press_id,
    minutiae=minutiae() if points is None else points,
    probe=b"probe-secret",
    vs_session=dict.fromkeys(range(press_id), GOOD) | (vs_session or {}),
    vs_gallery=vs_gallery or {},
  )


def other(failing: frozenset[int] = frozenset()) -> OtherPrint:
  """Another person's 5-press print; presses in `failing` agree with none of the others."""
  agreement = tuple(tuple(-1 if i == j else LOW if failing & {i, j} else GOOD for j in range(PRESSES)) for i in range(PRESSES))
  return OtherPrint(uuid4(), 99, Finger.RIGHT_INDEX, tuple(minutiae() for _ in range(PRESSES)), agreement)


def run(session: Session, presses: list[Press]) -> list[NextPress | TooSimilar | Done | GaveUp]:
  return [session.add(p) for p in presses]


def test_first_five_unjudged():
  assert run(Session((), S), [press(i) for i in range(PRESSES - 1)]) == [NextPress(None)] * (PRESSES - 1)


def test_clean_set_done():
  x = other()
  low = {x.print_id: (SIMILAR - 1,) * PRESSES}
  assert run(Session((x,), S), [press(i, vs_gallery=low) for i in range(PRESSES)])[-1] == Done(chosen=(0, 1, 2, 3, 4), flagged=())


@pytest.mark.parametrize(
  ("points", "failure"),
  [
    (minutiae(S.registration_min_detail_points - 1), Failure.TOO_FEW),
    (minutiae(S.registration_max_detail_points + 1), Failure.TOO_MANY),
    (minutiae(cx=5), Failure.OFF_CENTRE),
  ],
)
def test_failing_press_replaced(points: Minutiae, failure: Failure):
  session = Session((), S)
  presses = [press(i, points=points if i == ODD else None) for i in range(PRESSES)]
  assert run(session, presses)[-1] == NextPress(failure)
  assert session.add(press(5)) == Done(chosen=(0, 1, 3, 4, 5), flagged=())


def test_disagreeing_press_replaced():
  session = Session((), S)
  presses = [press(i) for i in range(4)] + [press(4, vs_session=dict.fromkeys(range(4), LOW))]
  assert run(session, presses)[-1] == NextPress(Failure.DISAGREES)
  assert session.add(press(5, vs_session={4: LOW})) == Done(chosen=(0, 1, 2, 3, 5), flagged=())


def test_worst_replaced_first():
  session = Session((), S)
  presses = [
    press(0),
    press(1, points=minutiae(S.registration_min_detail_points - 1)),
    press(2),
    press(3, vs_session=dict.fromkeys(range(3), LOW)),
    press(4, vs_session={3: LOW}),
  ]
  assert run(session, presses)[-1] == NextPress(Failure.TOO_FEW)
  assert session.add(press(5, vs_session={3: LOW})) == NextPress(Failure.DISAGREES)
  assert session.add(press(6, vs_session={3: LOW})) == Done(chosen=(0, 2, 4, 5, 6), flagged=())


def test_gives_up_after_the_extra_presses():
  light = minutiae(S.registration_min_detail_points - 1)
  total = PRESSES + S.registration_extra_presses
  verdicts = run(Session((), S), [press(i, points=light if i >= PRESSES - 1 else None) for i in range(total)])
  assert verdicts[PRESSES - 1 : -1] == [NextPress(Failure.TOO_FEW)] * S.registration_extra_presses
  assert verdicts[-1] == GaveUp()


def test_single_press_resemblance_is_a():
  x = other()
  session = Session((x,), S)
  presses = [press(i, vs_gallery={x.print_id: (SIMILAR, 0, 0, 0, 0)} if i == ODD else None) for i in range(PRESSES)]
  assert run(session, presses)[-1] == NextPress(Failure.DISAGREES)
  assert session.add(press(5)) == Done(chosen=(0, 1, 3, 4, 5), flagged=())


def test_stray_press_elsewhere_is_b_not_a():
  x = other(failing=frozenset({3}))
  on_stray = {x.print_id: (0, 0, 0, SIMILAR, 0)}
  presses = [press(i, vs_gallery=on_stray if i in {2, 4} else None) for i in range(PRESSES)]
  assert run(Session((x,), S), presses)[-1] == Done(chosen=(0, 1, 2, 3, 4), flagged=(x,))


def test_whole_finger_resembles_is_c():
  x = other()
  on_x = {x.print_id: (0, 0, SIMILAR, 0, SIMILAR)}
  presses = [press(i, vs_gallery=on_x if i in {0, 1} else None) for i in range(PRESSES)]
  assert run(Session((x,), S), presses)[-1] == TooSimilar(other=x, chosen=(0, 1, 2, 3, 4), flagged=(), also_similar=())


def test_too_similar_keeps_flags():
  x, careless = other(), other(failing=frozenset({0, 1, 2}))
  on_both = {x.print_id: (SIMILAR, SIMILAR, 0, 0, 0), careless.print_id: (SIMILAR,) * PRESSES}
  presses = [press(i, vs_gallery=on_both if i in {0, 1} else None) for i in range(PRESSES)]
  assert run(Session((x, careless), S), presses)[-1] == TooSimilar(
    other=x, chosen=(0, 1, 2, 3, 4), flagged=(careless,), also_similar=()
  )


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
  assert run(Session((x, y, z, careless), S), presses)[-1] == TooSimilar(
    other=y, chosen=(0, 1, 2, 3, 4), flagged=(careless,), also_similar=(z, x)
  )


def test_stray_pairs_left_out_of_c():
  # One new press on their stray press, two on healthy ones: C from the healthy pairs, and the print isn't flagged too.
  x = other(failing=frozenset({3}))
  on_x = {0: (0, 0, 0, SIMILAR, 0), 1: (SIMILAR, 0, 0, 0, 0), 2: (0, SIMILAR, 0, 0, 0)}
  presses = [press(i, vs_gallery={x.print_id: on_x[i]} if i in on_x else None) for i in range(PRESSES)]
  assert run(Session((x,), S), presses)[-1] == TooSimilar(other=x, chosen=(0, 1, 2, 3, 4), flagged=(), also_similar=())


def test_stray_pairs_left_out_of_a():
  # Two new presses on their stray press, one on a healthy one: that one alone resembles them (A); the stray pairs
  # never count against the registrant, so once it's replaced the print is flagged (B).
  x = other(failing=frozenset({3}))
  on_stray, on_healthy = {x.print_id: (0, 0, 0, SIMILAR, 0)}, {x.print_id: (SIMILAR, 0, 0, 0, 0)}
  session = Session((x,), S)
  presses = [press(i, vs_gallery=on_stray if i in {0, 1} else on_healthy if i == ODD else None) for i in range(PRESSES)]
  assert run(session, presses)[-1] == NextPress(Failure.DISAGREES)
  assert [p.press_id for p in session.kept] == [0, 1, 3, 4]
  assert session.add(press(5)) == Done(chosen=(0, 1, 3, 4, 5), flagged=(x,))


def test_stray_resemblance_through_a_replacement_is_b():
  # Review Focus 1: a press like their stray press, then, after another press is replaced, its replacement like it too.
  x = other(failing=frozenset({3}))
  on_stray = {x.print_id: (0, 0, 0, SIMILAR, 0)}
  light = minutiae(S.registration_min_detail_points - 1)
  session = Session((x,), S)
  presses = [press(i, points=light if i == PRESSES - 1 else None, vs_gallery=on_stray if i == ODD else None) for i in range(PRESSES)]
  assert run(session, presses)[-1] == NextPress(Failure.TOO_FEW)
  assert session.add(press(5, vs_gallery=on_stray)) == Done(chosen=(0, 1, 2, 3, 5), flagged=(x,))


def test_careless_other_print_is_b():
  x = other(failing=frozenset({0, 1, 2}))
  on_x = {x.print_id: (0, 0, SIMILAR, 0, SIMILAR)}
  presses = [press(i, vs_gallery=on_x if i in {0, 1} else None) for i in range(PRESSES)]
  assert run(Session((x,), S), presses)[-1] == Done(chosen=(0, 1, 2, 3, 4), flagged=(x,))


def test_own_prints_excluded():
  # The caller leaves the registrant's prints out of `others`; scores against them are ignored.
  own = uuid4()
  presses = [press(i, vs_gallery={own: (GOOD,) * PRESSES}) for i in range(PRESSES)]
  assert run(Session((other(),), S), presses)[-1] == Done(chosen=(0, 1, 2, 3, 4), flagged=())


def test_no_gallery():
  session = Session((), S)
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
  session = Session((), S)
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
    run(Session((), S), [press(i) for i in range(PRESSES)])
  assert len(caplog.records) == PRESSES
  assert "40 detail points" in caplog.records[0].getMessage()
  assert "done" in caplog.records[-1].getMessage()
  assert "probe-secret" not in caplog.text
