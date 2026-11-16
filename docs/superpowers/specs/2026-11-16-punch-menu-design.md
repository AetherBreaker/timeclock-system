# Punch menu redesign

Owner-approved 2026-11-16. Every decision is logged, with its date, in `2026-11-13-punch-menu-decisions.md`; this
document is what gets built from them. "My call" marks a choice Claude proposed, approved with the document. Mockups:
the "Punch Menu Redesign" canvas, https://claude.ai/artifact/WFoPmKj62UVenBQX5fdP1y (1920×1080, the clock's screen),
"Proposed: …" artboards; "Alternative: clocked in, Clock out below" was not chosen.

## Intent

Employees misread the punch menu's state and press the wrong button. The state is a small plain line ("Clocked in since
8:30 AM") in the same words as the buttons, and every button is the same blue. The punch records show the results:
Clock out pressed in place of End break, breaks and lunches left running long, and menus opened on a break or lunch
closed without a punch. The misreading happens mostly on the menu, before a punch; after a punch the problem is a wrong
button press.

## Goals

1. The employee's state reads at a glance, from across the screen, before they touch anything.
2. Each kind of punch looks different, so a wrong press is harder to make.
3. A break or lunch menu offers only the way back to work.
4. After a punch, the screen confirms the state the employee is now in, so a wrong press shows at once.
5. How far the employee is from 40 hours this week is always in view.

## Non-goals

- Making the employee's name harder to miss (deferred, decision 11: the fingerprint tuning should make it unnecessary).
- The layout of any other screen. Every other screen changes only its button colors (below).
- Fingerprint matching, the rescan screen, overrides, PIN entry.

## Anti-goals

- A state told apart from another by hue alone, or the red-green pair: clocked out is gray, not red.
- New colors that the theme's default paints over again, as it paints over the yellow and white today.

## Constraints

- Touchscreens of different sizes in a kiosk browser (main-office 1920×1080, main-warehouse and dev 1366×768); NiceGUI
  3.18 on Quasar. Sizes below are the 1920×1080 mockups' px, scaled to the screen (owner, 2026-11-16): widths and
  horizontal positions by the screen's width ÷ 1920, heights and vertical positions by its height ÷ 1080 (a differently
  shaped screen squashes or stretches them), text and icons by the smaller of the two. Boxes set minimums only, so each
  grows around its text rather than clip it. The hours table, Cancel and the gear keep their fixed sizes.
- `ui.button`'s `color` defaults to `"primary"`, which Quasar draws as `bg-primary text-white` (#5898D4) over the
  `.sft-punch`/`.sft-plain`/`.sft-key` rules. So every button on the device is that blue, and the yellow and white never
  showed.
- Taps land up to 12 px past a button's edge (the `::after` rules on `.sft-button`, owner, 2026-11-11); the new punch
  buttons, the docked Clock out included, keep that.
- Device-facing: it goes on the dev clock first, and reaches production only by `poe release-and-pin`.

## Approaches

1. **Chosen: switch off NiceGUI's default button color in the shared helpers and color each button kind from the page's
   stylesheet; compute the hours line from the week's hours the clock already totals, against the weekly-hours
   setting.** All colors stay in one place, beside the existing tokens, and the line reuses the hours table's own figure.
2. Give each button its own Quasar color in code: splits colors between Python and CSS, and any button that misses it
   stays blue, which is today's bug.
3. Drive the hours line through the alert hooks, with a lead equal to the limit so the hook always fires: bends a
   threshold alert into a permanent label.

The owner approved approach 1 on 2026-11-16.

## Colors

New tokens beside the existing ones in `_THEME_CSS`:

| Use | Color | Text on it |
|---|---|---|
| Clocked in bar, Clock in button | green #38753B | white |
| On break bar, Start/End break buttons | blue #386BA8 | white |
| At lunch bar, Start/End lunch buttons | orange #D98826 | #161514 (`--sft-black`) |
| Clocked out bar | gray #4A4641 | white |
| Clock out button | red #BC2F3B | white |

Every pair passes 4.5:1. Cancel stays `.sft-plain`, yellow with black text (below).

## Every button

`_button` and the PIN pad's keys stop asking for NiceGUI's default color, so the `.sft-*` classes decide. Every screen
then shows its stylesheet color. Every button that was to be white with black text, `.sft-plain` (Cancel, Use PIN,
Manager override, Back, Skip, …) and the pad keys, is yellow with black text instead, like `.sft-punch` (Register, Add
employee, Fingerprints, Next, OK, …): white looked harsh (owner, 2026-11-16, yellow tried first). The gear stays a flat icon in the theme's blue, as today and in the mockups (my call).

## The punch menu

Top to bottom, centred unless said otherwise (artboards "Proposed: clocked in", "on break", "at lunch", "clocked out"):

1. **The clock line and gear**, unchanged.
2. **The status bar**: full width, directly under the clock line (top 84 px, 168 px tall), in the state's color. The
   state as a big word, uppercase, 96 px, weight 800, letter-spacing 4 px: CLOCKED IN, ON BREAK, AT LUNCH, CLOCKED OUT.
   Under it, at 32 px weight 500:
   - clocked in: "since 8:30 AM", the open shift's clock-in (`MenuInfo.since`, as today);
   - on break or at lunch: "since 12:17 PM · 13 min", its start and how long it has run, as `format_duration` gives it
     (so a lunch left running reads "since 11:02 AM · 2 hr 10 min");
   - clocked out: nothing; the word alone.

   The minutes are worked out when the menu draws and don't tick while it stays up (my call: the menu closes after 120 s
   idle).
3. **The name**, 40 px weight 600, as today's title.
4. **The hours line** (below), 30 px.
5. **The punch buttons**, about 112 px tall, at least 380 px wide, 12 px corner radius, 36 px weight 600 text, a 44 px
   icon before the label, colored by kind:
   - clocked out: Clock in alone, centred;
   - clocked in: Start break and Start lunch in a centred row, 32 px apart, in that order; Clock out docked on the right
     (64 px from the edge, its top about 720 px down), lower than the row. Whichever of break and lunch is switched off
     drops out, as today. If neither is left, Clock out sits centred alone, like Clock in (my call);
   - on break: End break alone, centred. At lunch: End lunch alone, centred. No Clock out on either.
6. **The refusal notice** (`Menu.notice`), red, as today.
7. **Cancel**, yellow, 160×64, under the punch buttons.

Unchanged: the hours table docked on the left (`.sft-hours`), the Admin button near the bottom for admins, the reader
line and status line.

Icons (Material, NiceGUI's default set; my call, the nearest to the mockups): Clock in `login`, Clock out `logout`, Start
and End break `coffee`, Start and End lunch `restaurant`.

**No Clock out on a break or lunch** is a menu rule only: `Clock.menu_info` drops `CLOCK_OUT` from `valid_next` while the
state is on break or at lunch, beside where it drops a switched-off break or lunch start. `record_punch` keeps accepting
it, as the timeline allows it: a menu drawn before a break started on another clock can still send it. (The backend's
automatic clock-out writes to the database directly and never passes through `record_punch`.)

## The after-punch screen

The same frame as the menu, with no buttons (artboards "Proposed: after End break", "Proposed: after Clock out"):

1. **The status bar** in the state the punch put them in: Clock in, End break and End lunch → CLOCKED IN; Start break →
   ON BREAK; Start lunch → AT LUNCH; Clock out → CLOCKED OUT. Under the word, in place of "since …", the punch's sentence
   from `CONFIRMATIONS` ("Break ended at 12:30 PM").
2. **The name**: new on this screen (today's shows none).
3. **The hours line**, from the hours after the punch.
4. **Rule warnings**, red, as today.

The hours table stays docked on the left. It still shows for `CONFIRM_SECS` (5 s) and a double tap anywhere ends it
early, as today. The yellow alert notices go (below).

## The hours line

The limit is `config.settings.weekly_hours_alert` (a decimal over 0, default 40), the same setting the alert used, read
with the week's hours from the same settings. The week's hours are `HoursSummary.week` (worked plus paid break, Monday to
Sunday local), the same figure as the hours table's "This week" and the alert's measure.

- Under the limit: "{left} left to {limit} hours this week", white. `{left}` is the limit less the week's hours.
- At or over it: "{over} over {limit} hours this week", yellow (`--sft-yellow`), as today's over-40 alert. At exactly
  the limit it reads "0 min over", matching the alert's ≥.

Durations go through `format_duration` (whole minutes, truncated: "1 hr 55 min", "0 min"). `{limit}` is written as the
existing `_hours_number` writes it: "40", "37.5".

## What goes away

The weekly-hours hook's only action is the after-punch notice, and the hours line replaces it. So the clock stops
checking alert hooks: `record_punch` no longer calls `fired_hooks`, and `PunchOutcome.firings`, `Confirmation.notices`,
`text.alert_notice`, `WEEKLY_HOURS_NOTICE`, `WEEKLY_HOURS_EARLY_NOTICE` and `STATE_LABELS` go, with the tests that cover
them. The hook's registration in timeclock-schema (`alerts.py`) goes in that repo's next change, not a release of its
own. This supersedes the earlier spec's punch-menu state line and confirmation notice (§6.4, §6.11 of
`2026-10-26-timeclock-design.md`).

## Known and accepted

- NiceGUI bundles Roboto at 400, 500, 700 and 900 only, so the mockups' 600 and 800 render as 700 and 900. Fine as is
  (my call).
- The gear's tap area (0–136 px across, 0–120 px down) covers the bar's top-left corner, so a tap there opens the gear,
  as a tap in that corner does today. The bar itself takes no taps.
- Both update toasts at once partly cover the docked Clock out; they pass taps through, so it still works.

## Left to the builder

- How the layout is made in NiceGUI (fixed positioning or flow), and the CSS class and token names.
- How the limit reaches the page: a field on `HoursSummary` (built where the settings are already read) or on
  `MenuInfo` and the after-punch screen.
- How the name reaches the after-punch screen: `_record` is reached from the menu, a rescan, and a manager override.
- Whether the `!important`s in the button rules are still needed once the default color is off.
- The exact Material icon if one above doesn't exist in the bundled set.
