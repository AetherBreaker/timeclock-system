# Shift list columns and break flags: decisions (2026-11-18)

Two changes to the backend's shift list, settled together: its employee tables' columns line up, and TODO #19 (flag
breaks and lunches that run long or end at a clock-out). Each decision is recorded as it lands.

## The owner's decisions

1. **Columns line up by fixed widths first.** Each column gets a set width (`table-layout: fixed`), the status column
   takes the rest. If that isn't reliable enough, a grid shared by every card or one table for the page comes next.
2. **Every card shows the same columns.** A card with no values in a column shows it blank. A column a database
   setting turns off stays off (breaks off: Breaks and Worked).
3. **Long break.** A break whose length reaches `break_limit_minutes` + a new `break_grace_minutes` (default 15) is
   flagged: 30 minutes or more at today's values. With `break_limit_minutes` at 0 the threshold is the grace alone.
4. **Long lunch.** The same rule over a new `lunch_expected_minutes` (default 30; never used by payroll, only for this
   flag) + its own `lunch_grace_minutes`.
5. **Marked or corrected clears it.** Correcting or marking as correct either the start or the end punch of the break
   turns its chip grey ("reviewed"); a correction that brings it under the threshold removes it.
6. **Only an ended break is flagged.** A break in progress isn't, until its end punch.
7. **A break ended at a clock-out is flagged.** A short one gets a different flag from a long one: ending a shift on a
   short break can be abuse.

## Open

- A. Breaks off, but a shown shift has break punches (the 2026-11-13 rule shows them so nothing recorded is hidden):
  columns off everywhere, or on for every card that week?
- B. Lunches off (`lunches_enabled` false): is the Lunch column off too?
- C. A long break ended at a clock-out: one chip in place of Long break, or both? Does "short" mean under the long
  threshold?
- D. Lunch ended at a clock-out: lunch is unpaid, so the abuse flag doesn't apply. A long one flagged as not ended, a
  short one not at all?
- E. "Ended at a clock-out": exactly (the clock's end punch 1 µs before, or an end implied by the clock-out), or within
  a minute (also catching End break then Clock out back to back)?

## Claude's calls

- `lunch_grace_minutes` defaults to 15.
- "Mark as correct" is the existing editor button (a same-values correction of the punch), as the too-many-breaks
  callout uses; nothing new is stored.
- A break ended by an automatic clock-out (it writes no break end) counts as not ended, never as the short-break flag:
  nobody chose to clock out.
