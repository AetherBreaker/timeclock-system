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

## Open

1. The asleep signal itself, and what the recorder does until screen off lands.
2. Who names a clip (proposal: the recorder names it and keeps the current name in a shared file the app reads).
3. How the app tells the recorder about interactions it can't see itself (a fingerprint scan).
4. The rest of the details.
