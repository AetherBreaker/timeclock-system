# Screen recorder: design

`TODO.md` item 12. Decisions: `2026-11-13-screen-recorder-decisions.md` (the owner's, with Claude's calls marked). This
spec is written from that log. Where they differ, the log wins and this spec is fixed.

## 1. Why

When someone reports the clock's screen saying or showing something odd, the owner wants to see what it actually showed.
Each clock keeps short video clips of its screen around every interaction, plus freezes and crashes. Claude fetches
them over SSH, and watches one only when the owner says to.

Goals:

1. Every interaction is on video, from 15 s before it until 15 s pass with no further interaction. An interaction is a
   touch, a key, a mouse click, a fingerprint scan, a freeze, or the clock window vanishing.
2. A crash of the app, the browser, the recorder or the whole host keeps the footage up to the moment it stopped.
3. Each clip comes with a text file of what happened in it, and a processed copy that shows each frame's capture time,
   touch marks and a freeze timer.
4. The clock never slows down for the recorder.

Non-goals:

1. The testing and debug menus (`TODO.md` 11) come later, in their own pass.
2. Recording before and after business hours (grace periods) is a possible future item (§9).
3. Database rows don't name clips. A row's clip is found by its time (§6).
4. No viewer, upload or sharing. Clips stay on the clock.

Anti-goals (a failure even if it works):

1. The clock app stutters, or a tap responds later, because of recording.
2. The recorder fills the disk.
3. A recorder failure rolls back a host release or takes down the clock.

## 2. Facts (checked 2026-11-13)

1. **Clocks.**
   - dev: i5-8500T, 6 cores, 15 GB, 1366x768.
   - main-office: i5-4590S, 4 cores, 7 GB, 1920x1080, 104 GB free on `/var` (the least).
   - main-warehouse: i5-4570S, 4 cores, 7 GB, 1366x768.
2. **Sway 1.11 (wlroots 0.19).**
   - It offers `wlr-screencopy` and `ext-image-copy-capture` with output sources, but **no per-window capture source**.
     So the recorder captures the screen and crops to the clock window (owner-approved fallback).
   - sway runs as root on terminal 7 (`timeclock-display.service`).
   - Control socket: `/run/timeclock-display/sway.sock`. Wayland socket: `/run/user/0/wayland-1`.
   - The clock window is app id `local.timeclock.Clock`. It waits in the scratchpad and the watcher shows it fullscreen
     over the standby.
   - sway's config doesn't map or calibrate the touchscreen.
3. **OpenH264.**
   - 2.6.0 (`/usr/lib64/libopenh264.so.8`) is already on every clock.
   - It comes from Fedora's `fedora-cisco-openh264` repo, which the Fedora CoreOS base enables, but nothing requires the
     package.
4. **Input devices.**
   - Touchscreen: `G2Touch Multi-Touch by G2TSP`, multitouch, absolute positions.
   - The dev clock also has a Logitech USB mouse plugged in, which moves the pointer by relative amounts.
   - The updater's `keys.rs` already reads every `/dev/input/event*` without grabbing.
5. **The status folder.**
   - On the host it's `/data/timeclock_system_files/status` (`/data` links to `/var/data`). In the app container it's
     `/app/persisted_data/status`, mounted read-write.
   - The app already writes `app.json` there. The updater writes `host-update.json`.
6. **The updater's build.** It's a static musl build tested inside the image build, with only `libc` as a dependency.
   Its `sway.rs` has a minimal JSON reader that drops numbers.
7. **Crash records and alerts in the app.**
   - The app's crash records are `crash_alerts.py`'s `CrashAlertWriter` (JSON lines).
   - The app's own alerts go through `app.py`'s `_send_alert`.
   - Fatal-crash alerts are built inside aeth_ext (`_handle_fatal` → `alert`). Their body is fixed: program, host, time
     and details.
8. **Logging.** The app logs almost nothing at info level. In particular, it logs no line when the clock adds an
   employee or changes one.

## 3. The status folder: who writes what

| File | Writer | Meaning | Reader |
|---|---|---|---|
| `asleep` | the app, with screen off (`TODO.md` 13), not this pass | exists while the clock is asleep; empty | recorder; screen off's service |
| `scan` | the app | rewritten each time a finger lands on the reader | recorder |
| `recording` | the recorder | the open clip's name; gone when no clip is open | the app |
| `recorder.json` | the recorder | `{"at": <now>, "state": "recording" or "idle"}`, rewritten every minute while it works | the app (§5.5) |

All are replaced atomically: write a temp file in the same folder, then rename it. The recorder watches the folder with
inotify, so it sees each change at once. File names are Claude's call.

## 4. The recorder (timeclock-host)

### 4.1 Program and service

1. **Crate.** A Rust program, `timeclock-recorder`, in its own crate `host/recorder`.
   - It reuses the updater's `sway` and `keys` modules through a path dependency on the updater's library.
   - The sway JSON reader starts keeping numbers, since the recorder needs window rectangles. The updater's tests must
     still pass.
2. **The build.**
   - It's a glibc build, unlike the updater, because it loads the system's OpenH264 at run time and a static musl
     binary can't load a library.
   - Its tests run in the image build, as the updater's do.
   - The Containerfile installs `openh264` explicitly, so if the base ever drops it, the build fails instead of the
     recorder.
3. **The unit.** `timeclock-recorder.service`:
   - Runs as root.
   - Restarts on failure 5 s later.
   - Runs at `Nice=10`, so the clock app always gets the CPU first and the recorder drops frames instead.
   - Reaches sway's sockets (§2.2).
   - Reconnects, or restarts with the display, when sway restarts.
   - Not part of the updater's health check: a broken recorder never rolls back a host release.
4. **Settings.** `/etc/timeclock/recorder.conf`, in the same format as `updater.conf`, every key optional:

| Key | Default |
|---|---|
| `recordings_dir` | `/data/timeclock_system_files/recordings` |
| `status_dir` | `/data/timeclock_system_files/status` |
| `sway_socket` | `/run/timeclock-display/sway.sock` |
| `clock_app_id` | `local.timeclock.Clock` |
| `before_secs` | 15 |
| `after_secs` | 15 |
| `max_fps` | 30 |
| `freeze_frames` | 15 |
| `freeze_secs` | 1 |
| `cap_gb` | 40 |
| `keyframe_secs` | 2 |
| `max_clip_secs` | 600 |

### 4.2 When it records

1. It records while the clock window is showing and there's no `asleep` file.
2. Until screen off lands, nothing writes `asleep`, so it records whenever the clock window shows, around the clock.
3. A touch, key or mouse click while `asleep` exists starts recording at once. That catches the waking tap, and a clock
   that froze while asleep and so can't remove its file.
4. When `asleep` appears while a clip is open, the clip runs to its normal end, then recording stops.
5. While the standby screen shows (the clock window hidden or gone), nothing is captured.

### 4.3 Capture

1. **Frames.**
   - It captures the output through `ext-image-copy-capture` or `wlr-screencopy` (the builder's choice).
   - A frame is taken only when the screen has changed, at most `max_fps` a second.
   - Each frame is cropped to the clock window's rectangle (from sway's tree, read 4 times a second) at
     full resolution.
   - Each frame's capture time is kept to the microsecond, from the recorder's own clock.
2. **Encoding.**
   - Frames go to OpenH264 (loaded from `libopenh264.so.8`) in its screen-content real-time mode, with a keyframe
     every `keyframe_secs`.
   - Quality is tuned in the dev-clock round so text stays readable.
   - If encoding falls behind, frames are dropped. Capture never queues without limit.
3. **Streaming to disk.**
   - The encoded stream is written the whole time it records, as short segment files, each starting at a keyframe.
   - With no clip open, a segment is deleted once its end is more than `before_secs` plus `keyframe_secs` old.
   - So at any moment the disk holds the last ~17 s plus any open clip, and a crash loses at most what the kernel hadn't
     flushed.

### 4.4 Interactions

1. **Touch, key, mouse.** The recorder reads every `/dev/input/event*` without grabbing it, and opens new devices as
   they appear. Each device is classified by its capabilities.
   - **Touchscreen** (direct multitouch): each finger landing is an interaction. Its position goes from the device's
     raw range to the clock window's pixels in a straight line.
   - **Keyboard:** each key press (not autorepeat) is an interaction. The text file gets its time, never which key.
   - **Mouse:** each button press is an interaction. Movement keeps a clip open but isn't written down.
     - A device that reports exact positions gets them mapped like a touch.
     - A mouse that reports only movement can't give a click's position from outside sway (owner, "for now"): its
       clicks get a time and no ring. §9 adds the fix.
   - RustDesk's virtual devices count like any other.
2. **Scan.** Each change of `scan` is an interaction at that moment.
3. **Freeze.** See §4.5. A freeze is an interaction for as long as it lasts.
4. **The clock window vanishing** (closed, or hidden by the watcher) is an interaction.

### 4.5 Freezes

1. The app draws a heartbeat: a small block of pixels in a fixed corner of the page, changed about 4 times a second by
   the app's server (§5.1). The recorder reads that block from each captured frame before encoding it.
2. A freeze starts when the block hasn't changed for `freeze_frames` frames or `freeze_secs` seconds, whichever comes
   first. Frames catch a stalled server while the page still draws. Seconds catch a screen that stopped changing.
3. The freeze begins at the last frame where the block changed, and ends at the next frame where it changes.
4. Freeze checking starts at the first change seen after the window appears. A page without a heartbeat is never
   counted as frozen: the kiosk's start page, or an app release from before the heartbeat.

### 4.6 Clips

1. **Opening and closing.**
   - An interaction with no clip open opens one.
   - The clip's footage starts at the last keyframe at or before `before_secs` before that interaction.
   - It closes once `after_secs` pass with no interaction (an ongoing freeze holds it open).
   - A clip that reaches `max_clip_secs` closes, and the next one opens at once and carries on. It has its own name, and
     each `.txt` names the other.
   - So a freeze that lasts all night, or a mouse that never stops jittering, can't make one clip that's never
     processed or can't be deleted. The watcher keeps the clock window up while the browser still reaches the app, so a
     deadlocked app server can hold a freeze for hours.
2. **Name.** The local time its footage starts, `YYYY-MM-DD_HH-MM-SS` (e.g. `2026-11-13_08-15-02`), with `-2`, `-3`
   and so on added if two would collide.
   - While a clip is open, the recorder keeps its name in `status/recording`, and removes the file when the clip closes.
3. **Files** in `recordings_dir`, one set per clip, all named after the clip:
   - `.h264`: the raw stream, the kept segments joined.
   - `.frames`: one line per frame with its capture time.
   - `.txt`: the event log. One line per event, each with its time:
     - `touch x y`
     - `click x y`, or `click` from a mouse that reports only movement
     - `key`
     - `scan`
     - `freeze <start> <length>`
     - `window gone`
     - `asleep` / `awake` changes
     - the clip's start and end
   - `.mp4`: the processed copy (§4.7).
4. **Logging.** The recorder logs each clip's start and end, with its path, to the journal.
5. **Recovery after a crash.** When the recorder starts:
   - It removes a leftover `status/recording`.
   - Any segments left behind become a clip of their own: the footage up to the crash.
   - A clip with no finished `.mp4` is processed again.

### 4.7 Post-processing

1. **How it runs.** One pass per saved clip, right after it closes. It's a separate process at the lowest priority:
   `SCHED_IDLE`, one thread, idle I/O class. It runs one clip at a time, oldest first.
2. **What it does.** It decodes the raw stream with OpenH264 and writes the `.mp4` at a steady 30 frames a second. Each
   frame repeats until the next one's capture time, so the clip plays at real speed. On top it draws:
   - Each frame's capture time, local, to the microsecond.
   - A ring at each touch and click with a known position, held for half a second.
   - During a freeze, an `ss.ff` timer counting from the freeze's start, over the repeated last frame. Past a minute it
     reads `m:ss.ff`, and past an hour `h:mm:ss.ff`.
3. **Layout.** The time and the freeze timer go on a strip added below the picture, so they never cover the screen.
   The font is DejaVu Sans, which the image already has.
4. **If it's interrupted,** a clip without a finished `.mp4` gets processed again (§4.6.5).

### 4.8 Storage

1. Everything lives under `recordings_dir`: segments and every clip's four files.
2. The folder's total is held under `cap_gb`. After each clip is saved and at start, the oldest clips' files are deleted
   until it fits.
3. The open clip (at most `max_clip_secs`) and the live segments are never deleted to make room.

## 5. The clock app (timeclock-system)

### 5.1 Heartbeat

1. **What it is.** A small block of pixels, e.g. 2x2, fixed in a corner that always shows the page's background, above
   every layer and taking no input. It flips between two colours one shade apart near that background, so nobody
   notices it. Its position and colours are fixed constants that the recorder knows (§4.5).
2. **What drives it.** A server-side timer, about 4 times a second, so a stalled server stops it as surely as a stalled
   browser.

### 5.2 Scan file

When a finger lands on the reader, the app rewrites `status/scan` with the time. It counts the moment the scanner's
finger-present flag turns on (`_on_finger_status`), not when matching finishes, so a scan that hangs or fails still
starts a clip.

### 5.3 Naming the open clip

1. **Crash records.** `CrashAlertWriter`'s record gains `clip`: the contents of `status/recording` when it's written, or
   none.
2. **The app's own alerts** (`_send_alert`) add a line naming the open clip, when there is one.
3. **Fatal-crash alerts** are built inside aeth_ext and stay as they are, without the clip's name (owner). The alert's
   time finds the clip, and the crash record names it.

### 5.4 Log lines for employee changes at the clock

Employee changes and employees added at the clock are stamped by the database when they arrive, so their clip is found
through the clock's log. The app logs one info line each time the clock adds an employee or changes one: the employee's
id and what changed, never a PIN.

### 5.5 A broken recorder is reported

The owner's decision: the app alerts when the recorder stops. Each step below follows from it.

1. **What the recorder writes.** It rewrites `status/recorder.json` every minute while it works: connected to sway, with
   OpenH264 loaded, whether recording or idle. A capture or encoding failure ends the process with the error in the
   journal, so a recorder in a restart loop stops writing.
2. **When the app alerts.** Through `_send_alert`, once, when both hold:
   - the file's `at` is more than 10 minutes old;
   - the app itself has been up 10 minutes, so a host restart doesn't trip it.
3. **Recovery.** One more alert when the file updates again.
4. **No file, no alert.** A clock that has never run the recorder isn't alerted on, so the app release can ship before
   the host release.
5. **Wording:** Claude's call.

## 6. Finding a row's clip

1. A punch, fingerprint scan, fingerprint change or fingerprint bypass stores the clock's own time (`at`) and the clock
   (`instance_id`). Its clip is the one on that clock whose footage covers that time.
2. For an employee change or an employee added at the clock, the moment comes from the clock's log line (§5.4).
3. The app's logs are kept 14 days (`LOG_HISTORY_RETENTION_DAYS`).

## 7. Shipping

1. **Two releases.** A host release with the recorder, and an app release with §5. Either works without the other:
   - Without the app's part, the recorder never sees a heartbeat (so it checks for no freezes) and never sees a scan.
   - Without the recorder, nothing writes `recording` or `recorder.json`, and the app names no clip and alerts on
     nothing.
2. **The dev-clock round** (the dev clock is on the test database).
   - Each item is checked from the clips and their text files.
   - Claude may look at dev-clock clips for these checks (owner).
   - The owner does the real touches and the finger scan at the dev clock when asked (owner).

   The items:
   1. A touch makes a clip from about 15 s before to 15 s after the last touch. `status/recording` appears and goes.
      Touch positions match the screen.
   2. A scan with no touch starts a clip.
   3. A key press starts a clip, and the text file doesn't name the key.
   4. `docker pause` on the app container for a few seconds records a freeze of that length. The `.mp4` shows the
      timer.
   5. Restarting the clock browser records `window gone`.
   6. A hand-made `status/asleep` stops recording once no clip is open. A touch while it exists starts recording.
   7. `kill -9` on the recorder mid-clip leaves a clip on restart, and its `.mp4` is made.
   8. A small `cap_gb` deletes the oldest clips first.
   9. A crash record made on the dev clock names the open clip.
   10. The recorder's CPU use, with the screen busy and with it still, is measured and reported to the owner.
   11. With the recorder stopped and `recorder.json` backdated past 10 minutes, the app alerts. Starting the recorder
       sends the recovery alert.
   12. A click from the dev clock's Logitech mouse is written as `click` with no position.
3. **Production.**
   - The host release goes to `:stable` by promotion after the round. Whether production takes it at once or in its
     03:00 window is asked at the time.
   - The app release ships by `poe release-and-pin`.
   - main-office (1920x1080 on a 4-core i5-4590S) is the heaviest. Its load is watched after rollout, and `max_fps` is
     the lever.

## 8. Docs

1. `host/README.md` describes the recorder, its settings and its files.
2. `docs/production-clock-setup.md` names `timeclock-recorder` and `/etc/timeclock/recorder.conf` beside the updater.

## 9. TODO.md

1. Remove item 12 once the recorder lands.
2. Add a deferred item: also record a short while before business hours start and after they end, where a problem with
   the screen and sleep is most likely. The `asleep` file would carry the next opening time.
3. Add a deferred item: the clock itself reports the exact position of the clicks it receives, so a mouse's clicks get
   rings too (owner).

## 10. Owner answers after the builder check

All four are written in above:

1. Dev-round viewing and touches (§7.2).
2. The broken-recorder alert (§5.5).
3. Fatal-crash alerts left as they are (§5.3.3).
4. Mouse clicks with no position (§4.4.1).

## 11. After it ships

About two weeks after it ships, re-read the production drives' SMART data against the 2026-11-11 baseline.

## 12. Claude's calls

1. Status file names and formats (§3).
2. The separate crate with a path dependency on the updater (§4.1.1), and the glibc build (§4.1.2).
3. `Nice=10`, and leaving the recorder out of the updater's health check (§4.1.3).
4. The settings file and its defaults (§4.1.4), including a keyframe every 2 s.
5. Segments deleted once older than about 17 s, unless a clip holds them (§4.3.3).
6. Clips ending only when `asleep` appears and no clip is open (§4.2.4).
7. Freeze checking waits for the first heartbeat change (§4.5.4).
8. The files per clip and the `.txt` lines (§4.6.3). Leftover segments become their own clip (§4.6.5).
9. Processed copies are MP4 at a steady 30 fps, with the time and timer on a strip below the picture and touch rings
   held half a second (§4.7).
10. The heartbeat's shape and colours (§5.1).
11. Log lines for employee changes at the clock (§5.4).
12. Clips split at 10 minutes (§4.6.1), and the freeze timer grows past `ss.ff` for long freezes (§4.7.2).
13. The recorder-alert details: the file's format, the 10 minutes of app uptime first, no alert without a file, and the
    alerts' wording (§5.5).

## 13. Left to the builder

1. Capture protocol and Wayland client crate.
2. Muxing the MP4, and the OpenH264 bindings.
3. The font rasterizer.
4. Segment file format.
5. The exact heartbeat corner and colours, within §5.1.
