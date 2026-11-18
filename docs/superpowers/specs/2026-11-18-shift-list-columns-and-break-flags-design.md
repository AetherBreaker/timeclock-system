# Shift list columns and break flags

Draft for the owner's approval, 2026-11-18. The owner's decisions are numbered as in the decisions log
(`2026-11-18-shift-list-columns-and-break-flags-decisions.md`); everything marked "Claude's call" is open to correction.

## Intent

Two changes to the backend's shift corrections page (timeclock-backend), shipped together.

1. **Columns line up across employee cards.** Each card is its own `<table class="sh">` with automatic layout, so each
   sizes its columns from its own contents and no two cards line up. Aligned columns make the week far easier to scan.
2. **TODO #19: flag breaks and lunches that need a second look.** A break or lunch that ran long suggests a forgotten
   end punch or an over-long break; one that ran into the clock-out suggests the person never ended it, or, for a short
   break, ended their shift on paid break time, which can be abuse. A manager corrects the punches or marks them as
   correct.

## Goals, non-goals, anti-goals

- Goals: every card's columns at the same positions; five new orange flags, each with a grey reviewed form; three new
  settings.
- Non-goals: hours and payroll (reports, emails) are unchanged; the clock is unchanged; nothing alerts anyone. Lunches
  being off hides only the list's Lunch column: the editor's lunch sections and the reports are unchanged (Claude's
  call; decision 9 named the column).
- Anti-goals: a flag that blocks a punch or a save; a flag with no way to clear it; any change to paid hours.

## Approaches

Columns (decision 1):

1. **Chosen: fixed column widths** (`table-layout: fixed`). Small and CSS-led; widths are set by hand, so they must fit
   each column's longest value.
2. Fallback: one CSS grid shared by every card (subgrid), columns sized by the widest value on the page.
3. Fallback: one table for the whole page, each employee a block of rows, the card look rebuilt from table cells.

Flags (decision 13):

1. **Chosen: settings in timeclock-schema, rules in the backend, the existing Mark as correct.** Three
   `config.settings` columns; the flags worked out from the punches in `web/rules.py`, mirrored in `editor/rules.js`;
   marking reuses the editor's same-values correction, as "too many breaks" does. Nothing new stored.
2. A table of review marks, apart from punch corrections: keeps marks out of punch history, but adds a table, grants
   and a save path, and works differently from "too many breaks".
3. The rules in timeclock-schema, so reports could flag too: not asked for, and ties every rule change to a schema
   release.

## Part 1: columns (timeclock-backend, `web/shift_list.py`, `static/theme.css`)

### Widths

Every card's table gets `table-layout: fixed`, and every column except the status column a set width, enough for its
longest value at the page's font (Roboto 14px; header 12px):

| Column | Longest value |
|---|---|
| Day | "Wed, Nov 18" |
| Clock in, Clock out | "12:45 PM", "working" |
| Breaks | "2 · 0:45" then "(0:15 unpaid)" at 12px |
| Lunch | "2 · 1:00" |
| Worked, Paid | "10:30 so far" (Paid bold) |
| Clock, Punches (technical details only) | unbounded: these wrap inside their width instead of spilling |

The status column takes the rest of the card's width. Its chips start at its left edge, so they line up across cards
too (today they hug the right edge). Exact widths are the builder's.

### Which columns a card shows (decisions 2, 8, 9)

- **Breaks and Worked:** shown when breaks are on, or the card's shown rows hold a break punch (the 2026-11-13 rule,
  unchanged).
- **Lunch:** shown when lunches are on (`lunches_enabled`), or the card's shown rows hold a lunch punch. A lunch punch
  is a lunch start or end, ignored ones included, as the breaks rule counts them. New: today the column always shows.
- **Clock and Punches:** the technical details preference, page-wide (unchanged).

Cards therefore share one column set, except when breaks or lunches are off and some card still holds such a punch:
that card's columns fall out of line for that week, which the owner accepts (decision 8). A stray row's spanning cell
covers Clock in through the last of Breaks and Lunch shown (2, 3 or 4 columns).

### Done when

In Firefox, with 12-hour and 24-hour times and technical details on and off, over cards with and without breaks,
unpaid notes, "working", "so far", stray rows and several chips: every column edge sits at the same position on every
card, and no value spills into the next column. If fixed widths can't meet that, stop and report: the fallbacks are
the owner's to choose.

## Part 2: settings (timeclock-schema)

Migration `0026` adds to `config.settings`, each `integer NOT NULL` with `CHECK (... >= 0)`:

| Column | Default | Meaning |
|---|---|---|
| `break_grace_minutes` | 15 | How far past `break_limit_minutes` a break may run before the page flags it |
| `lunch_expected_minutes` | 30 | A lunch's expected longest length; never used by payroll, only by the flag |
| `lunch_grace_minutes` | 15 (Claude's call) | How far past `lunch_expected_minutes` a lunch may run before the page flags it |

`Settings` gains the three fields with docstrings saying the same. Only the page reads them: a clock's settings
SELECT names its own columns. The migration's header comment follows 0023's style. Release as 0.18.0.

## Part 3: the flags (timeclock-backend)

### Which breaks and lunches

A break or lunch is an interval of a shift as the hours walk counts it (the schema's `Shift.breaks` and
`Shift.lunches`, over corrected punches): it starts at a counted start punch and ends at a counted end punch, or at the
clock-out that ends it. One still in progress is never flagged (decision 6).

- **Threshold:** a break's is `break_limit_minutes + break_grace_minutes` (decision 3; with the limit at 0 it's the
  grace alone); a lunch's is `lunch_expected_minutes + lunch_grace_minutes` (decision 4).
- **Long:** its length reaches the threshold (at today's settings a break of exactly 30:00 is long).
- **Ended at the clock-out** (decision 12): its end is the shift's clock-out time, or 1 µs before it (the clock writes
  the end punch 1 µs before a clock-out made on a break). Nothing looser: End break then Clock out back to back isn't.

### The flags (decisions 7, 10, 11)

| | Ended at the clock-out, long | Ended at the clock-out, short | Otherwise, long |
|---|---|---|---|
| Break | **Break not ended** | **Left on break** | **Long break** |
| Lunch | **Lunch not ended** | none | **Long lunch** |

"Short" is under the threshold. A break or lunch ended at an **automatic** clock-out is "not ended" whatever its
length: nobody chose to clock out (Claude's call).

### Reviewed (decision 5)

A flagged break or lunch is reviewed once its start punch or its end punch (its own end punch, or the clock-out that
ended it) was changed by hand: `PagePunch.by_hand`, which Mark as correct's same-values correction sets. A correction
that brings it under the threshold, or moves its end away from the clock-out, re-sorts it by the same rules.

### Chips and the attention bar (Claude's calls)

- One chip per flag per shift, orange; it turns grey with ", reviewed" only when every break or lunch behind it in the
  shift is reviewed, as "too many breaks" does. A count shows when there are several: "Long break", "2 long breaks",
  "Long lunch, reviewed". "Not ended" and "Left on break" happen at most once per shift.
- Order, after the "too many breaks" chip: Long break, Left on break, Break not ended, Long lunch, Lunch not ended.
- A break both past the day's expected count and long shows both chips.
- The start and end punches of a flagged break or lunch don't also raise Changed, as "too many breaks" punches don't.
- Unreviewed flags count in the attention bar as orange keys after "too many breaks", each counting shifts:
  "1 shift with a long break", "2 shifts with a long lunch", "1 shift with a break not ended", "1 shift with a lunch
  not ended", "1 shift ended on a break". Each filters the list like the existing counts, and "Only shifts that need
  attention" includes them.

### The editor (Claude's calls)

One orange callout per unreviewed flagged break or lunch, under the "too many breaks" callout, hidden on read-only
shifts like the others. Its Mark as correct confirms that break's start punch. Texts (times in the page's format):

- Long break: "Sam's break from 2:10 PM to 2:52 PM lasted 0:42; breaks of 0:30 or more are flagged. Change it below,
  or mark it as correct if it's right."
- Left on break: "Sam clocked out at 5:00 PM during a break that started at 4:48 PM. Change it below, or mark it as
  correct if it's right."
- Break not ended: "Sam was still on a break from 2:10 PM when the shift ended at 9:00 PM. Add when the break really
  ended below, or mark it as correct if it's right."
- Long lunch and Lunch not ended: the same with "lunch" and the lunch threshold.

The editor works the flags out from the draft, so chips and callouts follow edits live, as today. Its page context
gains `breakGraceMinutes`, `lunchExpectedMinutes` and `lunchGraceMinutes` (`breakLimitMinutes` is already there). The
editor's title chips (`shift_editor.py`) use the same rules. `editor/rules.js` mirrors `rules.py`, and the drift test
covers the new flags.

## Delivery

1. timeclock-schema: migration and fields; release 0.18.0 from the main session.
2. timeclock-backend: on top of the other session's move to schema 0.17 once it merges, bump to 0.18.0 and build parts
   1 and 3; PR, merge, `poe release-and-pin` (the backend applies 0026 at startup).
3. TODO #19 is checked off once it ships.

## Left to the builder

Exact widths and markup (`<colgroup>` or header classes), how `flags` and its callers take the thresholds, how the flag
kinds are represented, attention key names, and test names.
