# Screen recorder: decisions log

The running record of what the owner decided for `TODO.md` item 12 (screen recordings of interactions), kept as each
point is settled so it survives context compaction. The spec is written from it. "My call" marks a choice the owner left
to Claude; everything else is the owner's decision.

## Scope

1. The recorder only. The testing and debug menus (`TODO.md` 11) come later, in their own pass (owner, 2026-11-13).

## Carried over from the TODO wave (2026-11-11)

These were settled under "19, recorder" in `2026-11-11-todo-wave-decisions.md`:

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

## Awake and asleep

Screen off (`TODO.md` 13) is on hold. The asleep signal it will use is designed in this pass, and the recorder follows
it, so the recorder skips asleep time as soon as screen off lands, with no change to the recorder (owner, 2026-11-13).

1. The signal is a file in the clock's status folder, `/data/timeclock_system_files/status/asleep`, present while the
   clock is asleep (owner, 2026-11-13; screen off's decision 20.3 already used it).
2. No grace periods for now (owner, 2026-11-13). The idea: also record a short while before business hours start and
   after they end, where a problem with the screen and sleep is most likely; the asleep file would carry the next
   opening time for it. When the recorder lands, add it to `TODO.md` as a possible future item.
3. The recorder records while the clock window is showing and there's no asleep file, noticing the file's changes at once
   (owner, 2026-11-13).
4. Until screen off lands nothing writes the file, so the recorder records whenever the clock window shows, around the
   clock (owner).
5. A touch or key while the file exists starts recording at once: it catches the waking tap, and a clock that froze while
   asleep and so can't remove its file (owner).
6. Sleep never cuts a clip short: the clock sleeps 30 s after the last tap, and the clip ended 15 s after it.
7. Screen off brings the app's side (write the file when the reader closes, remove it when it opens, clear a leftover at
   startup). The file has no contents. Until then the recorder's side is tested on the dev clock with a hand-made file
   (owner).

## Clip names, rows, logs and alerts

1. The recorder names each clip, since it starts clips on its own: the local time its footage starts, like
   `2026-11-13_08-15-02` (owner, 2026-11-13).
2. Rows don't name clips; a row's clip is the one covering its time on its clock (owner, 2026-11-13). This replaces the
   rows part of carried-over decision 8. Punches, fingerprint scans, fingerprint changes and fingerprint bypasses store
   the clock's own time (`at`) and clock (`instance_id`). Employee changes and employees added at the clock are stamped
   by the database on arrival, so the clock's log line gives their moment.
3. The recorder logs each clip's start and end. While a clip is open, it keeps the clip's name in a small file in the
   status folder; the app reads it to name the clip in progress in crash records and alerts (owner, 2026-11-13).

## Interactions

1. Scans: the app updates a file in the status folder each time a finger lands on the reader; the recorder notices each
   update at once and counts it as an interaction (owner, 2026-11-13).
2. A scan counts when the finger lands, not when matching ends, so a scan that hangs or fails still starts a clip
   (owner).
3. Scan times go in the text file beside the clip, with touches and freezes (owner).
4. The recorder reads keyboards and mice itself (owner):
   1. Each key press is an interaction; the text file gets its time, never which key, so a PIN typed on a keyboard can't
      end up in it.
   2. A mouse click is an interaction, written down and marked with its position like a touch; a RustDesk session's
      mouse included.
   3. Mouse movement keeps a clip going but isn't written down.

## PINs in clips

1. A clip of a PIN login shows which digits were tapped (the keys ripple, and the touch markers and text file show the
   taps). Accepted (owner, 2026-11-13): clips stay on the clock, reachable only over SSH, where PINs could be captured
   other ways anyway, and they help with "my PIN doesn't work" reports.

## Open

1. The rest of the details.
