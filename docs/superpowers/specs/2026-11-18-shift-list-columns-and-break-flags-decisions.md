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
8. **Breaks off, but a card holds break punches:** the 2026-11-13 rule stands for that card (it shows Breaks and
   Worked), and its columns fall out of line with the others that week. A rare case; may be revisited.
9. **Lunches off hides the Lunch column** the same way: off while `lunches_enabled` is false, except on a card holding a
   lunch punch.
10. **One chip for a long break ended at a clock-out:** "Break not ended", in place of Long break. Under the long-break
    threshold it's "Left on break". Each turns grey with ", reviewed".
11. **Lunch ended at a clock-out:** a long one is "Lunch not ended"; a short one isn't flagged (lunch is unpaid).
12. **"Ended at a clock-out" means exactly:** the clock's own end punch 1 µs before the clock-out, or an end implied by
    the clock-out itself. An End break then Clock out back to back doesn't count.
13. **How the flags are built:** three `config.settings` columns in timeclock-schema; the flags worked out from the
    punches in `rules.py` (mirrored in `editor/rules.js`); Mark as correct reuses the editor's existing button (a
    same-values correction). Not a separate review table, not rules in timeclock-schema.

## Open

None.

## Claude's calls

- `lunch_grace_minutes` defaults to 15.
- "Mark as correct" is the existing editor button (a same-values correction of the punch), as the too-many-breaks
  callout uses; nothing new is stored.
- A break ended by an automatic clock-out (it writes no break end) counts as not ended, never as the short-break flag:
  nobody chose to clock out.
