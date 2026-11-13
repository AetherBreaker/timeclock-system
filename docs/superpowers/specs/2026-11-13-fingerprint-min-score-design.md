# A minimum fingerprint match score

Approved by the owner on 2026-11-13.

## Why

On 2026-11-13 a press at main-warehouse matched another employee's right index at 46 against a runner-up of 30. The
reader's threshold is 40 and the lead rule asked for 15, so the clock opened that employee's punch menu and recorded a
lunch on their record. The person pressing scored 27 on their own registered thumb: a poor press, or a finger they never
registered. Genuine matches and wrong-person scores overlap between 40 and about 58 (83 of 478 genuine punches scored
under 50, and other people's prints reached 40 or more on 20 of 348 clear genuine scans), so the lead rule is the only
thing standing between a weak press and the wrong record.

## Decisions (owner)

1. The lead (`config.settings.fingerprint_match_margin`) goes from 15 to 20. Done in production on 2026-11-13: a
   settings change, read live by the clocks.
2. A minimum score of 45: a match whose best score is under it is refused.
3. The minimum is a database setting next to the lead, so it can be tuned without a release.
4. A refused press shows "Not recognized, try again", as an unrecognized one does.
5. The scan log records these refusals as their own result, so their cost can be measured.
6. Ships as a schema release, a backend release (which applies the migration to both databases), then a clock release,
   straight to both production clocks.

Measured cost on the scan log: 45 of 518 genuine matches (about 1 in 12) would have needed a second press under the
minimum and the lead together, most of them for a few people whose prints score low (up to about 1 press in 3 for the
worst); the minimum alone halves the wrong-person scores that clear it (11 scans at 45 or more
against 20 at 40).

## Approaches considered

- **A minimum of 50** (the owner's first idea): refuses 1 in 6 genuine presses, and every other press for two people.
- **The lead alone at 20**: stops the 2026-11-13 match at 1 in 37, but leaves 40–44 open to a wrong print when the right
  person's own press is poor.
- **Re-registering the weak prints first**: the 2026-11-13 registration review found prints failing today's checks match
  no worse than passing ones (17% under 50 in both), so it isn't a general fix.

## The design

- **Schema** (migration 0024): `config.settings.fingerprint_match_min_score integer NOT NULL DEFAULT 45 CHECK (>= 0)`; a
  value of 40 or less leaves the reader's own threshold in charge. `scan_result` gains `too_weak`. The model gains the
  field and `ScanResult.TOO_WEAK`.
- **Clock**: the sync's settings SELECT reads the new column; the scanner's decision takes the minimum beside the lead and
  checks it first: no reader match is `no_match`, a best score under the minimum `too_weak`, a lead under the margin
  `too_close`, otherwise `matched`. Every fingerprint the clock reads goes through that one decision (punches, rescans,
  manager and admin approvals), so all of them get the minimum. The controller shows `too_weak` as "Not recognized, try
  again".
- **Backend**: no code; its release carries the migration.
- Registration is untouched: its own bars decide which presses it keeps.

## My calls

- The result's name, `too_weak`.
- The lead's default (the model's and the column's) moves from 15 to 20 to match what production runs; it is the value a
  clock falls back on without a settings row, and a fresh database's.
- The test database's lead is set to 20 by hand, so the test clock behaves as production does.
