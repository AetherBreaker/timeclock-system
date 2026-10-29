# Clock QoL wave: design

2026-10-29. Every item is decided. All of it ships in one branch and one PR (owner).

## 1. Phone field (decided)

- The add-employee form's phone field uses the input's mask `(###) ###-####` with `unmasked-value`: digits only, at
  most ten. The brackets, space and dash are drawn by the field and never part of the value.
- Backspace removes digits only: `(810) 2` → `(810)` plus its trailing space → `(81`. The repo has no real-browser
  tests and this doesn't add any: a test checks the field carries the mask, and the sequence is checked by hand on the
  clock (my call).
- Stored format unchanged (`+1` and ten digits). Fewer than ten digits fails Next with the existing "Enter a 10-digit
  US phone number"; empty is still allowed. A leading `1` isn't special-cased (owner).

## 2. Num Lock (decided)

- Cause: main-office's keyboard drops off USB and re-enumerates (five times on 2026-10-29; the mouse on the same hub
  never did), and cage 0.2.0 starts each new keyboard with Num Lock off. RustDesk's virtual keyboard can flip it too.
- Fix: the kiosk sets the XKB option `numpad:mac` (`XKB_DEFAULT_OPTIONS`), so the number pad always types digits. The
  Num Lock key and light still toggle but no longer matter; the pad loses its arrow/Home/End roles, which the clock
  never uses.
- No further keyboard hardware testing (owner).

## 3. A fingerprint on every PIN pad (decided)

On any PIN pad (username or PIN field), a recognized finger does what it would have done on the screen before Use PIN;
digits already typed are discarded.

| Pad | Recognized finger |
| --- | --- |
| Idle's Use PIN (login) | Opens that person's punch menu |
| Admin reconfirm | Own finger: admin menu. Anyone else's: their punch menu |
| Rescan (admins, PIN-only employees) | Own finger: records the punch. Anyone else's: their punch menu |
| Manager override | A manager's: approves. Anyone else's: back to the override prompt with "A manager must authorize this", as a non-manager's PIN does |
| Admin approval | An admin's: approves. Anyone else's: back to the approval prompt with "An admin must approve this", as a non-admin's PIN does |
| Fingerprints login | Opens that person's fingerprint options; changes still need an admin's approval |

The new employee's PIN pad is unchanged: it isn't a login, and a finger there is ignored.

## 4. Reader indicator (decided)

**Split of jobs.** The indicator reports the reader alone: its live state and whether a print was read. Each screen
keeps its own error line, in place, for that screen's issues (who scanned, roles, PINs, refused punches, form errors).
"That fingerprint doesn't match. Try again." stays on the screen. The idle title no longer changes for reader messages;
it still changes for its other news, as today.

**Layout.** Bottom-left: `[icon] [status] [message]`. Icon 48 px (was 36), text 20 px (was 16).

**Icon and status: live only, never on a timer** (the one floor is the failed read's 1 s).

| Reader | Icon | Status |
| --- | --- | --- |
| Waiting for a finger | Yellow | "Ready" |
| Finger down, being read | Blue | "Keep your finger down" (at login too, however briefly) |
| Read succeeded, finger still down | Green, ending when the finger lifts | "Lift your finger" |
| Login scan read but matched nobody, finger still down | Orange, ending when the finger lifts | "Lift your finger" |
| Read failed | Red, until the reader is waiting for a finger again, at least 1 s | "Couldn't read that, try again" |
| Paused itself (too hot) | Red | "Reader paused" |
| Not taking scans (the gap after a scan) | Dim | None |
| Asleep outside business hours | Dim | "Tap to wake the reader" |

**Message: the one thing that lingers.**

- Error (red): when the status moves on from an error, its text moves here. It clears on a screen change, a
  successful read, a newer message, or, on the idle screen only, after 15 s.
- Success (green): "Scan successful", for any read that matched someone or, while registering, any read that
  succeeded; the indicator doesn't tell a new print from a recognized one, which is the screen's job. It lasts 3 s or
  until replaced. A screen change clears it only when the new screen is asking for a finger: idle, scan your finger,
  rescan, the manager and admin prompts, the approval prompt, registering. It carries into the punch menu, the
  confirmation, the PIN pads and every other screen.
- Not recognized (orange): "Not recognized, try again", for a login scan read fine that matched nobody: not a success.
  Same rules as the success message. Registration never shows orange.
- Typing doesn't count as a screen change (moving from username to PIN, typing in the form).
- Messages show on screens where a finger does something: idle, punch menu, punch confirmation, scan your finger,
  rescan, override prompt, admin reconfirm, approval prompt, every PIN pad, registering. Elsewhere only the live status
  shows.

**Where the live data comes from.** libfprint's finger-status flags: `NEEDED` (waiting for a finger) and `PRESENT`
(finger on), plus the scanner's own states (closed, paused) and each scan's outcome. Identify already watches
`notify::finger-status`; enroll must too. In libfprint 1.94.100 an enroll waits for the lift between presses and
reports it; after the last press it completes and reports no finger, so that lift isn't seen and the status follows
what's reported. Each registration press's result comes from the enroll progress callback (an error is a failed read).
The once-a-minute re-arm's millisecond gap isn't shown (my call).

**Login results arrive early (owner).** `identify_sync` returns only after the finger lifts (the device deactivates on
finger-off), so today a login's result waits for the lift. The reader passes libfprint's match callback, which
`fpi_device_identify_report` calls the moment the scan is matched, and the scanner posts the result from there: the
screen acts while the finger is still down, the indicator shows green or orange "Lift your finger" until the lift, and
the next identify starts only after `identify_sync` returns and the 1 s pause. The result is posted once; a cancel
after it (a mode change, closing time, the re-arm) isn't a failed read. A retry already ends without waiting for the
lift. The indicator follows the reader's finger signal, so the 16 s scan-in-progress guess goes.

**Check on the main-office clock** (a dev clock during the trial): the timings above on the real reader.

## 5. Version number (decided)

- It moves from the top-left (beside the gear) to the very bottom-left corner, keeping its size (14 px) and dimness
  (50%). The reader line sits above it with about 12 px between; both are 16 px from the left edge. Nothing else moves.
- Checked against main-office's 1920×1080 screen: the longest reader line ends near 650 px; the punch menu's Admin
  button starts near 840 px, and the database status line is in the bottom-right.

## Housekeeping (decided)

- Merge `docs/todo.md` into the root `TODO.md` and point every reference at it.
- New TODO item: testing and debug menus, e.g. tapping the reader icon a few times opens a verbose log of the reader's
  scans, for troubleshooting someone's fingerprint. It can include a capture mode showing each press's picture with the
  matcher's detail points circled (failed presses included), in memory only, never saved or logged. Samples on
  2026-10-29 showed ridge cut-offs at the window's left edge counted as detail points, and a faint line across the
  middle of every press.
- New TODO item: a placeholder screen that takes over while the app is down (Coolify stopping it to deploy an update),
  saying the clock is being updated, so people don't panic when the menu drops. Which part shows it is to be designed:
  if the kiosk container restarts too, something outside it has to.
- Fingerprint pictures on the everyday screens: considered and dropped; the indicator's feedback covers it (owner).
