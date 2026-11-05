# Pickier fingerprint registration — design

Owner decisions are marked *(owner)*; everything else marked *(my call)* is open to the owner's veto.

## Why

Careless presses at registration make templates that interfere with other people's scans. Seen in production
(2026-11-04/05): a thumb print with 0 of its 5 presses agreeing with each other; and a thumb print with one stray press
that scores 36–43 against two other people's thumbs, which opened the wrong person's menu (45 for that print, 41 for the
scanning person's own). Registration is the one moment where strictness is cheap: a re-press costs seconds, a bad
template costs every scan for as long as it exists.

Regular scans keep the threshold of 40 and the margin of 15 *(owner)*. Only registration gets stricter.

## Goals

- Stop careless presses from entering a template *(owner)*: the main target.
- Tell the person what to fix, press by press, e.g. "keep your finger centred" *(owner)*.
- Catch a finger that is too similar to another person's print to be reliable, and have an admin allow it through or
  reject it *(owner)*.
- Over time, find existing carelessly registered prints through the resemblance they cause, and get them re-registered
  *(owner)*.

## Non-goals

- Any change to scan matching (threshold 40, margin 15).
- Proof presses after registration: shelved *(owner, 2026-11-05)*.
- Auditing all existing prints proactively; only prints a registration's resemblance check surfaces.
- Coaching: already done operationally *(owner)*.

## Anti-goals

- Blaming whoever registers second for a resemblance caused by someone else's careless print *(owner)*.
- Bars so strict that a careful, normal press fails; the bars come from data (Calibration).

## The flow

1. The employee gives the usual 5 presses. Nothing is judged until all 5 are in, because a press can only be compared
   with the others once they exist *(owner)*.
2. The clock checks the set (Press checks). A press that fails is dropped, and the screen asks for a replacement press
   with advice for that failure *(owner)*. The replacement is checked against the presses kept so far.
3. Step 2 repeats until 5 presses pass. With several failing presses, the worst is replaced first, one at a time, and the
   set is re-checked after each *(my call)*.
4. After 5 extra presses without a passing set, the registration fails: "Couldn't get a clean registration. Try again or
   use another finger." *(my call)*. A failed re-registration leaves the old print untouched, as today.
5. The passing finger is compared with other people's prints (Resemblance). Only then is the template built and saved.

### Press checks *(my calls; bars set by Calibration)*

| Check | Fails when | Message |
| --- | --- | --- |
| Detail points | too few (light or partial press) | "Press your finger flat on the reader" |
| Detail points | too many (smudged or noisy press) | "Clean and dry your finger, then press again" |
| Position | the detail points sit off-centre or run into an edge of the reader window | "Keep your finger centred on the reader" |
| Agreement | the press doesn't match enough of the other presses at a registration bar above 40 | "Press the same finger the same way each time" |
| Resemblance | the press alone resembles another person's print (case A below) | the message of its weakest other check, else "Press the same finger the same way each time" |

Position comes from the detail points' coordinates in the press, so no image processing is needed *(my call)*. No
direction words ("move left") *(my call)*: the reader's image orientation relative to the person isn't established.

### Resemblance to other people's prints

Compared with every print the clocks match scans against, minus the registrant's own prints *(my call)*. Three cases:

- **A. One new press resembles someone** (the rest don't): a careless new press. It is dropped and replaced like any
  failing press *(my call)*. Unless what it resembles is a press that fails its own print's press checks (a stray press
  in an existing print): then it's case B, so a careless existing print can't fail the registrant's presses one after
  another and end the registration blamed on them *(my call, from the owner's no-blame anti-goal)*.
- **B. The resemblance comes from one press of another person's existing print** (their stray press): the registration
  proceeds, and the screen warns and recommends that that person re-register that finger *(owner)*. The wording puts
  the problem on the existing print, not on the person registering *(owner)*. The clock also sends an alert naming the
  print to re-register, so the recommendation isn't lost when nobody acts on the screen *(my call)*.
- **C. The finger as a whole resembles another person's print** (several presses on both sides): too similar to be
  reliable. The screen shows the admin which person and finger it resembles and asks them to allow it through or reject
  it *(owner)*. Allowed, it's saved as usual, with that admin recorded as the change's approver *(my call)*. Rejected, no
  print is saved for that finger; in a re-registration the old print stays, as with any failed re-registration *(my
  call)*. Making the employee fingerprint-exempt instead is the admin's decision and change, made in the database, never
  on the clock *(owner)*.

Telling B from C needs press-by-press scores against the other person's individual presses, not only the best score
against their whole print.

Before calling it C, the other person's existing print goes through the same press checks. If it fails them (a
carelessly registered print as a whole, not one stray press), it's treated like B: the new registration proceeds and the
other person is told to re-register *(my call, from the owner's no-blame anti-goal and the aim of hunting down careless
prints)*.

The resemblance bar protects against both harms *(my call, from the owner's "too similar to be reliable")*: wrong
matches (another person's score near 40 or above) and frequent margin refusals (another person's score within 15 of the
finger's usual genuine scores). Calibration sets it from the same replay.

### Without an admin present

An employee with no print on file who logs in with their own PIN registers a first print with no admin there
(`controller.py`'s `RegisterPrompt`). On that path *(owner)*:

- **C:** the screen names no one. It says the finger can't be used without an admin, and offers to try another finger
  (the employee may pick one) or to have an admin check it. An admin who steps in approves by finger or PIN, as for
  other fingerprint changes, then sees the details and allows or rejects as above. No alert *(owner)*.
- **B:** the registration proceeds with no warning on screen, since it would name a coworker to the employee; the alert
  names the print to re-register *(owner)*.

With an admin present (an admin adding an employee, or an approved re-registration), the screens show the details
directly.

## How it gets built *(my calls)*

- libfprint's built-in registration takes exactly 5 presses and can't swap one out, and its scoring and its building of
  a template from presses are internal. The builder needs single presses the app can keep, drop and replace, and a
  template built from the chosen 5. Our libfprint patch gains: scoring one press against another, and building a
  template from chosen presses. Getting the single presses is the builder's choice: enroll's per-press progress prints
  (today ignored) or the identify path (which already returns the press and every gallery print's score). The patch
  goes in timeclock-extras (`libfprint/patches/`) as a new `timeclock-libfprint` version, released there before
  timeclock-system locks it.
- The saved template stays the same FP3 format with 5 presses, so scans, sync and the database don't change.
- Calibration: replay candidate rules over the current production prints (exported to the scratchpad, deleted after;
  no names or print ids in the repo). Ground truth, from the 2026-11-05 health check:
  - **Known-bad:** a print with 3 or fewer of its 5 presses matching at least one of its own other presses at 40 or
    more (seven such prints on 2026-11-05), the thumb whose stray press scores 36–43 on other people, and the two old
    prints kept in the gitignored `fingerprint-samples/` backup that drew near-collisions before they were replaced.
  - **Healthy:** every other current print.
  - If no bars separate the groups cleanly, catching every known-bad press wins over never asking a healthy print's
    weakest press again: a failed press costs one extra press, not the registration *(my call)*.

  The chosen bars go into code as constants, documented with the replay's numbers.

## Left to the builder

- Exact bars, from calibration.
- Screen layout for the replacement prompt and the resemblance screens.
- Code structure.
