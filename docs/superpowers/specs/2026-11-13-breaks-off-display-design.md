# Hiding break details while breaks are off

Owner-approved 2026-11-13. A short design: what the conversation settled, written down.

## Intent

Paid breaks will likely go away, leaving only unpaid lunches. Before then, the backend's pages and the emailed payroll
reports stop showing break details, and the regular hours they make redundant, wherever they can't matter. A shift
that still has break punches keeps showing them, so nothing recorded is ever hidden.

## The rule

Break details are hidden for a shift, an employee's table or a report workbook only when **both** hold:

1. breaks are off: `config.settings.breaks_enabled` is false (the existing business-wide switch);
2. it has no break punch: no break start or end, corrections applied and voided punches left out.

When hidden, the regular hours go too: with no breaks, worked hours equal paid hours, and only the paid figure stays.

## The clock

Nothing to build. `breaks_enabled = false` already hides the clock's "Start break" (someone on a break can still end
it), refuses a break start, and drops the break total from the punch menu's hours when there were none today.

## Shift list (`web/shift_list.py`)

Per employee table, over the rows it shows: when the rule holds for every shift in it (an entry's punches, ignored ones
included), the table drops its **Breaks** and **Worked** columns and the "(unpaid)" notes, and the week line reads
"Week: <paid> paid" without "· <worked> worked". A stray row's spanning cell and its "none" cells shrink to match.

## Shift editor (`web/shift_editor.py`, `web/editor/*.js`)

The page context gains `breaksEnabled`. Per shift (entry), decided from its **saved** punches, not the draft, so a
section never vanishes mid-edit; a shift added in the draft has none saved. When the rule holds, the shift hides:

- its Breaks card;
- "Break start" and "Break end" in its punch-type choices (so no break is added to it by accident);
- the timeline's Break and Unpaid-break keys;
- its worked hours, keeping the paid figure.

## Reports (backend payroll job; `build/reports/export.py`)

Per group workbook (a group's employees for the week): the job reads `breaks_enabled` and checks `punch_log` for any
break start or end by those employees in the week. When breaks are off and there's none, both tabs drop their break
columns and Hours:

- Shift Summary drops Hours, Breaks, Unpaid Breaks, Break Count, Breaks Over Limit, Break Overage, Break Times;
- Weekly Hours drops Hours, Breaks, Unpaid Breaks.

Paid Hours and the lunch columns stay; the Punch Log tab is unchanged. In the framework, a `Column` gains a flag for
"shown only while breaks show", and `fill_tab` takes whether they show; the job decides once per workbook. The local
review script `export.py` applies the same rule, so its workbooks match the emails.

## Approaches

1. **Chosen: apply the rule where each view already decides what to draw** (server-side for the list and the reports,
   in the browser for the editor). Small, and follows how each part is built today.
2. A shared "show breaks?" predicate in timeclock-schema, mirrored in `rules.js` under the drift test: ceremony for a
   one-line rule.
3. A "has breaks" column in the database views: a schema migration and release for what is only presentation.

## Calls made (mine, owner may correct)

- A break punch is a break start or end as the shift now stands (corrections applied, voids excluded). The pages count
  an ignored one too, so a stray break punch stays visible for fixing; the reports go by `punch_log`, the punches that
  count.
- The editor decides per shift from the saved punches, not the draft.
- The shift list decides per employee table over the rows it shows, not per cell: the shifts share one table.

## Left to the builder

Markup, test names and how each page threads the flag through.
