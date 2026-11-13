# Punch menu redesign: decisions log

The running record of what the owner decided for the clock's punch menu and after-punch screen, kept as each point is
settled so it survives context compaction. The spec is written from it. "My call" marks a choice Claude made that the
owner hasn't confirmed; everything else is the owner's decision. Mockups: the "Punch Menu Redesign" canvas,
https://claude.ai/artifact/WFoPmKj62UVenBQX5fdP1y (1920×1080, the clock's real size).

## Why

Employees misread the menu's state and press the wrong button. "Clocked in since…" is small plain text in the same words
as the buttons, and the punch records show the results: Clock out pressed in place of End break, breaks and lunches
left running long, and menus opened on a break or lunch closed without a punch. The misreading happens mostly on the
menu, before a punch; after a punch the problem is a wrong button press (owner).

## The menu

1. A full-width status bar under the clock line: the state as a big caps word, with "since … · N min" under it. CLOCKED
   IN green #38753B, ON BREAK blue #386BA8, AT LUNCH orange #D98826 with dark text #161514, CLOCKED OUT gray #4A4641.
   Gray, not red, for clocked out: red is the error color, and a red bar over the green Clock in button is the red-green
   colorblind pair.
2. Buttons colored by kind, with icons, about 112 px tall: Clock in green, Clock out red #BC2F3B, break blue, lunch
   orange. Colors dulled a notch at the owner's request. Cancel stays neutral white.
3. No Clock out on the break and lunch menus: only End break or End lunch, and Cancel. Clocking out straight from a
   break is rare in the punch records.
4. Clocked in: Clock out docked on the right, lower than the centred break/lunch row (the "Proposed: clocked in"
   artboard), not the "below a divider" alternative. Clocked out: Clock in alone in the centre.

## The hours line

5. The weekly-hours alert line is replaced by a line that's always present: hours left to 40, switching to hours over 40
   once past it. The limit is `config.settings.weekly_hours_alert` (40). Today the only hook fires at ≥ the limit with
   lead 0 (`timeclock_schema/alerts.py`); `WEEKLY_HOURS_EARLY_NOTICE` exists in `text.py` but nothing triggers it.
6. It shows on both the punch menu and the after-punch screen (owner, 2026-11-13).
7. My call, drawn on the canvas: plain white, 30 px, under the name. Wording: "1 hr 55 min left to 40 hours this week",
   and past 40, "2 hr 10 min over 40 hours this week".

## After a punch

8. Approved as drawn on the "Proposed: after End break" and "Proposed: after Clock out" artboards (owner, 2026-11-13):
   the bar shows the new state, with the punch's sentence ("Break ended at 12:30 PM") under the big word; the name under
   the bar (today's screen shows no name); the hours line; rule warnings below in red.

## Build constraints

9. On the device every button is NiceGUI's default primary #5898D4 with white text: Quasar's `bg-primary`/`text-white`
   beat the `.sft-punch`/`.sft-plain` CSS, so the yellow never showed. The build must switch that default off, or the new
   colors get overridden the same way.

## Open

- Making the employee's name harder to miss. Floated after the 2026-11-13 false match, where a scan opened another
  employee's menu and they pressed Start lunch without noticing the name. Not decided.
