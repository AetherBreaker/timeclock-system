# Registration on timeclock-dev: decisions (2026-11-17)

The owner's first registration test of the matcher-thread branch on timeclock-dev. Each decision is recorded as it lands.

## The owner's decisions

1. **No pause between scans, login and registration alike** (corrected by the owner: "you didn't remove the delay for
   ALL scans"). Every call returns after the reader saw the lift, so the next scan is armed at once. The 1 s finger-off
   pause stays only for a call cut short while the finger rests (the re-arm, a gallery or mode change), which returns
   before its lift and would read that finger again; a failed read keeps the 0.5 s never-spin retry pause.
2. **The reader indicator says when to lift.** "Lift your finger" as soon as the image is taken (libfprint clears its
   waiting flag with the finger still down), even while the scan is still processed. "Processing…" when a login's
   finger lifts before its result is in.
3. **Registration's processing shows up top.** Never in the indicator: registration keeps taking presses while results
   are out. The screen's title says "Checking your presses…" once every needed press is made and results are still out.
4. **A press failing its own checks raises the total at once** (off centre, too few or too many detail points). It's
   replaced as soon as its result is in, before five presses, with its advice.
5. **No cap on rescans.** A registration no longer gives up after `registration_disagreement_retries` disagreeing
   presses: it ends on a passing set, the press timeout or Cancel. The clock stops reading the setting.

## Open

6. **Disagreeing presses before five.** The owner turned down counting every press failing at five ("isn't quite such a
   good idea"), and asked whether a press can be ruled out with confidence around the third or fourth press. Claude
   replayed timeclock-dev's registered prints press by press: too many presses with no agreeing partner at press 3 or 4
   found one by press 5, even presses far below the agreement bar, so no early rule-out reached high confidence (the
   figures went to the owner in chat). Awaiting the owner.

## Claude's calls

- "Rescan attempts" read as the registration's give-up cap, the only cap on presses.
- `registration_disagreement_retries` stays in timeclock-schema and the backend until this release is on every clock;
  a schema change drops it then (the clock stops selecting it first, so no clock's sync breaks).
- LIFT and PROCESSING are blue, as "Keep your finger down" is: no result is known yet.
- The hardware probe's register loop (`fprint_reader.py`) drops its 1 s sleep after a counted press and keeps 0.5 s after
  a retry, as the scanner does.
- A registration capture no longer sets the green READ indicator: it's posted after the lift, when the indicator no
  longer shows it.
