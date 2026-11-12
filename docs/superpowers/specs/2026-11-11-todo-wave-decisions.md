# TODO wave: decisions log

The running record of what the owner decided for this pass of TODO items, kept as each one is settled so it survives
context compaction. The pass's spec is written from it once every item is settled. "My call" marks a choice the owner
left to Claude; everything else is the owner's decision.

## Scope and order

1. The pass covers TODO items 2, 3, 5, 14, 16, 17, 20 and the display step of 19 (18 was dropped).
2. The display step goes first: 17 and 20 build on the display and the host updater it moves.
3. 19's recorder moves to 15's pass (it isn't quick). 15 gets its own pass after this one.
4. The spec is written only when the whole bundle is ready to start, covering every item at once.

## 19, display step (goes first)

1. Moves from timeclock-standby to timeclock-host: sway's config and `display.sh`, `kiosk.py`, the clock browser's
   launcher and `clock-start.html`, the display units, the PAM file, the unit that blanks terminals 1 and 7, the plymouth
   theme and its two units, RustDesk (its units, `remote.sh`, `xdpw.conf`, `pipewire-root.conf`), and the console login
   timeout (`timeclock-tmout.sh`).
2. The host updater leaves the watcher and becomes its own Rust program and service in timeclock-host. `update.rs` moves
   nearly unchanged; the small helpers and the touch tracking are copied in.
3. Stays in standby: the watcher (screen logic only), the status app and standby page with their settings and units, and
   the standby window's launcher (`standby.sh` and its unit).
4. The updater's state folder gets a new name, e.g. `/var/lib/timeclock-updater` (name: my call). The update-splash unit
   follows the new path.
5. No one-time handover code. The host update that installs the new updater gets no automatic after-update check; the
   owner confirms it by seeing the clock up and running. The old `/var/lib/timeclock-watcher` is deleted over SSH.
6. The watcher still uses sway's socket, the clock window's app id and the clock browser's unit, which now live in
   timeclock-host.
7. Shipping: a smaller standby release, then a host release that pins it and adds the moved files. One dev-clock round
   covers both moves: the updater applies an update when the screen is idle, holds off during a touch or a deploy, and
   rolls back when the display is masked. Then promotion to `:stable`.
8. smartmontools goes into the host image in this step. Once it ships, remove the hand-installed
   `/usr/local/sbin/smartctl` from main-warehouse and main-office.

## 2, crash records name the error

1. Every place the clock shuts down on an error passes the exception itself to aeth_ext's exception path instead of
   `trigger_shutdown`: `keep()`'s done callback in `app.py`, the page's `on_error`, and the scanner's lost reader
   (`scanner.py`'s `_run` passes `str(e)` today; it passes the `ReaderGone`). The log then has the traceback, the alert
   gets the traceback picture, and the crash record gets its trail.
2. The crash record also holds each fatal error's message and traceback. aeth_ext hands shutdown callbacks only the
   trails, so the crash-alert writer keeps a copy of every CRITICAL log record (aeth_ext writes one, with the traceback,
   per fatal error) and writes them beside the trails. No aeth_ext change.

## 3, the local store survives a schema change

1. No schema version in `meta`. At every startup, read every stored row.
2. Mirror tables (employees, groups, rules, business hours, settings, punch corrections, synced punches, synced prints):
   a table with any unreadable row is emptied, and the next catch-up refills it. Readable rows stay, so a clock updated
   while the database is unreachable keeps working from its last copy.
3. Unsynced rows (pending employees, unsynced punches, bypasses, unsynced prints, fingerprint changes, scans): one that
   can't be read stops the clock with an error naming the row (its traceback reaches the log and crash record per 2).
   Fingerprint changes and scans count as unsynced (my call; the TODO's list left them out).
4. Remove `LocalStore.__init__`'s cleanup of prints stored without a finger; it has run on every clock (my call).
5. Known limit: models use `extra="ignore"`, so a renamed field with a default reads with the default until the next
   connect refreshes it.

## 5, the Fingerprints login accepts a fingerprint

1. Outdated: the owner tried it on the dev clock on 2026-11-11 and a finger worked. Likely fixed on 2026-11-05 by
   `03dd257` (a reordered gallery no longer restarts the identify) and timeclock-libfprint 1.94.100.post2 (a cancel
   during activation is applied). Remove the item from `TODO.md`.

## 14, PIN length rule

1. New PINs are 4 to 12 digits. Checked where PINs are set: the clock's `check_new_pin` and the backend page's Set PIN
   (`employee_editor.py`'s `save_pin`), kept in step by their cross-reference comment.
2. The page refuses more than 12 digits: the clock's pad stops at 12, so a longer PIN could never be typed there.
3. Existing PINs keep working; only hashes are stored, so their lengths are unknown.
4. Needs a backend release and submodule bump. Error wording: my call.

## 16, larger touch areas

1. The gear is drawn about half again as big and takes taps anywhere in its corner.
2. Every button takes taps up to 12 px past its drawn edge. Where two buttons sit closer than that, the space between
   them splits down the middle, so touch areas never overlap.

## 17, update toasts

Mockup (approved, version 4): https://claude.ai/artifact/92neD6jb6Ae9X7Ts42e9Qa

1. System update toast: bottom right, above the database status, slides in from the right. Title "Downloading a system
   update", a line like "42% · 312 of 740 MB", and a yellow bar following the download.
2. A line under the bar says when it installs, from the moment the toast appears:
   - Scheduled (window mode, production): "Installs at the scheduled time, 3:00 AM tonight." (the time comes from
     `HOST_UPDATE_WINDOW`). Once downloaded the title becomes "System update downloaded" and the toast slides away
     after a few seconds.
   - Immediate (test clocks): "The clock will restart as soon as download completes." while downloading; once
     downloaded, "The clock will restart to install it immediately.", and the toast stays up until the restart.
   - The updater's restart rule doesn't change (a minute with no touch, no deploy running); the wording doesn't mention it.
3. The app update's amber banner becomes a toast in the same spot (above the system one when both show): "Installing a
   clock app update", "The clock will restart in about a minute.", and a moving bar, since a build reports no progress.
4. Progress source: bootc's hidden `--progress-fd` (JSON lines; it needs a pipe; bootc 1.17.1 on the dev clock accepts
   it). The updater passes a pipe and writes progress where the app reads it (path and format: my call).

## 18, simultaneous host and app updates

1. Skipped: a much smaller issue than first thought, not worth the effort for the edge case. Remove the item from
   `TODO.md`. (Found: the host already waits for app deploys; a deploy during a host restart fails without a retry; a
   broken app release in the 10 minutes after a host update can roll back a good host release.)

## 20, screen off after hours

1. The screen turns off when the clock is asleep (after hours, 30 s after the last tap: the moment the reader sleeps
   today) and no input of any kind (touch, keyboard, mouse) has arrived for 30 s.
2. Any touch turns it back on. While the clock is asleep, an invisible layer over the whole page makes the first tap
   only wake the clock, never press a button underneath.
3. The app marks that it's asleep with a file in the shared status folder (as the standby banner works today). A small
   host service in timeclock-host powers the screen through sway. If the app goes down, the screen comes back on so the
   standby screen can show the problem.
4. A RustDesk session's mouse counts as input, so remoting in wakes the screen.
5. No touch-while-asleep test up front (owner); the dev-clock round and the rollout show it on each monitor.
6. If a monitor can't be woken by touch while its screen is off: no code; the screen service is masked on that clock
   over SSH (owner, 2026-11-12).

## 19, recorder (for 15's pass)

Settled so far:

1. A host service in timeclock-host records only the clock app's window, while the clock is awake (business hours, or
   from a wake until it sleeps).
2. Kept footage runs from 15 s before an interaction until 15 s pass with none.
3. While awake, frames stream to disk the whole time, so a crash keeps everything up to the moment it stops (owner,
   2026-11-11; the drives' wear was checked and is minor). On restart, pieces left behind are kept as a clip (my call).
4. A detected freeze or the clock window vanishing counts as an interaction.
5. Freeze detection: an invisible heartbeat of a few pixels, driven by the app's server about 4 times a second (rate: my
   call).
6. The recorder reads the touchscreen itself. Touches (time, position) and freezes (start, length) go to a text file
   beside the clip.
7. One post-processing pass right after each clip saves, at the lowest priority (SCHED_IDLE, one core, idle I/O),
   composites each frame's capture time to the microsecond (our own clock, not the page's), touch markers, and an
   `ss.ff` freeze timer from a freeze's first frame until it ends (the last frame repeats at a steady rate meanwhile).
8. Clip names go on database rows: punches, fingerprint scans, fingerprint changes, fingerprint bypasses, employee
   changes at the clock, employees added at the clock. Logs record a clip's path at its start and end; crash records and
   alerts name the clip in progress.
9. Encoding: software OpenH264. Storage (my call): `/data/timeclock_system_files/recordings` on the clock, fetched over
   SSH; raw, processed and text files kept; 40 GB cap per clock, oldest deleted first; rows keep names after deletion;
   interrupted post-processing reruns on start.
10. Claude fetches clips for the owner and watches one only when told.
11. About two weeks after it ships, re-read the production drives' SMART data against the 2026-11-11 baseline.

Open for 15's pass: who names a clip now that the recorder can start one on its own (proposal: the recorder names it and
keeps the current name in a shared file the app reads), how the app tells the recorder it's awake and about
interactions, and the rest of the details.
