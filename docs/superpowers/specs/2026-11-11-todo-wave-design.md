# TODO wave: design

One pass over a batch of smaller TODO items, shipped in as few releases and dev-clock rounds as possible. The owner's
decisions are in `2026-11-11-todo-wave-decisions.md` beside this file; this spec carries them out. "My call" marks a
choice the owner left to Claude.

## 1. Scope

| Item | What | Where |
|---|---|---|
| 19, display step | The display and the host updater move from timeclock-standby to timeclock-host | standby, host |
| 2 | Crash records and logs name the error | clock app |
| 3 | The local store survives a schema change | clock app |
| 14 | New PINs are 4 to 12 digits | clock app, backend |
| 16 | Buttons take taps a little past their edges; a bigger gear | clock app |
| 17 | Update toasts: a system-update toast with live progress; the app-update banner becomes a toast | host, clock app |
| 20 | The screen turns off after hours and wakes on touch | host, clock app |

Off the TODO list with no code: 5 (the Fingerprints login already takes a finger) and 18 (simultaneous updates; not
worth it). Not in this pass: 19's recorder and 15, which get the next pass together.

### Non-goals

- No change to when the updater restarts the clock (a minute with no input, no deploy running).
- No coordination between host and app updates (18).
- No aeth_ext change, no schema change.

## 2. Releases and order

1. **Display step.** A timeclock-standby release (the display and updater removed), then host release H1 that pins it and
   carries the moved files, the new updater and smartmontools. Dev-clock round, then promotion to `:stable`.
2. **Host release H2:** the updater's download progress (17) and the screen-power service (20). Kept out of H1 (my call):
   the update that installs the new updater gets no after-update check (§3.4), so H1 carries only the move, and H2 is
   the first host release the new updater judges and can roll back.
3. **Clock app release** (2, 3, 14, 16, 17, 20 app sides), through `poe release-and-pin` after a dev-clock round. It
   doesn't wait for H2: with no progress file there's no system toast, and an older host ignores the asleep mark. Until
   H2, the wake layer (§9.2) still makes the first tap on an asleep clock only wake it, with the screen on.
4. **Backend release** for 14's page side, `poe release-and-pin`, and the submodule bump in timeclock-system.

Every device-facing change goes on the dev clock (`timeclock-system-dev`, test database) before release.

## 3. Display step (19)

The display belongs to the host; standby keeps only what serves the standby screen. The watcher lived in standby only so
the screen-switching protocol isn't split between repos, and it stays there.

### 3.1 What moves to timeclock-host

From timeclock-standby, installed by timeclock-host's own `files/` tree instead of the Containerfile's standby stage:

- **The display:** `display/sway.conf`, `display/display.sh`, `display/kiosk.py`, `display/clock.sh`,
  `display/clock-start.html`, `systemd/timeclock-display.service`, `systemd/timeclock-clock.service`,
  `systemd/timeclock-display.pam`, `systemd/timeclock-tty1.service`.
- **The splash:** `plymouth/timeclock.plymouth` (and the Containerfile's theme assembly),
  `systemd/plymouth-reboot-update.conf`, `systemd/timeclock-update-splash.service`.
- **Remote access:** `display/remote.sh`, `display/xdpw.conf`, `systemd/pipewire-root.conf`,
  `systemd/sweetfiresupportdesk.service`, `systemd/timeclock-remote.service`.
- **The console login timeout:** `config/timeclock-tmout.sh`.

The moved scripts go to `/usr/libexec/timeclock/` beside the host's own (my call), labelled as programs as today; the
units' paths follow.

### 3.2 What stays in timeclock-standby

The watcher (screen logic only), the status app and standby page (`status/`), `display/standby.sh` with
`systemd/timeclock-standby.service`, `systemd/timeclock-status.service`, `systemd/timeclock-watcher.service`,
`config/standby.conf`, and `config/watcher.conf` without its updater keys. The Containerfile still installs these from
the pinned standby commit into `/usr/libexec/timeclock-standby`, and still puts the logo there for `fallback.html`.

### 3.3 Links that cross the repos

Each side's docs name the other. Standby depends on the host for:

- sway's socket `/run/timeclock-display/sway.sock`, the clock window's app id `local.timeclock.Clock`, and
  `timeclock-clock.service`, used by the watcher and the status app;
- `timeclock-display.service`, which `timeclock-standby.service` binds to;
- `kiosk.py`, which `standby.sh` now runs by its host path instead of from its own folder.

### 3.4 The host updater

1. Its own Rust program and service in timeclock-host (`timeclock-updater`, `timeclock-updater.service`; names my call),
   built static (musl, `--locked`) in a Containerfile stage like the watcher's.
2. `update.rs` moves nearly unchanged. Copied in beside it: the sway client (`clock_window`, the JSON reader),
   `docker_get`, `helper_running`, and the watcher's input tracking (`keys.rs`), which the updater needs for "a minute
   with no input". Nothing may panic, as now.
3. Settings: the five updater keys leave `watcher.conf` (`host_image`, `app_service`, `update_check_secs`,
   `update_idle_secs`, `update_health_secs`) for `/etc/timeclock/updater.conf`, with the keys the copied helpers need
   (`sway_socket`, `clock_app_id`, `docker_socket`, `helper_image`), all with today's defaults (my call).
4. State moves to `/var/lib/timeclock-updater/` (`host-update`, `pending`, `bad-releases`).
   `timeclock-update-splash.service`'s condition follows it. `/run/timeclock-host-updating` stays as it is.
5. No handover code. H1's first boot finds no `pending` in the new folder, so that one update gets no after-update
   check; the owner confirms H1 by seeing each clock up. Afterwards, delete `/var/lib/timeclock-watcher` over SSH on
   each clock.
6. The watcher loses `update.rs`, the updater thread, the shared last-input value and the five keys. It keeps
   `docker_get` and `helper_running` for `flip = deploy-start`.

### 3.5 smartmontools

Added to the image's package list in H1. Once H1 runs on main-warehouse and main-office, delete the hand-installed
`/usr/local/sbin/smartctl` there (`/usr/local` is `/var/usrlocal`, which updates never touch).

### 3.6 The dev-clock round for H1

Before H1 reaches a clock, a read-only `ostree admin config-diff` there shows whether `watcher.conf` or `standby.conf` was
edited by hand: `/etc` keeps local edits across updates, and the slimmed watcher rejects a file that still sets an
updater key (falling back to its defaults). An edited file is fixed by hand first.

The clock comes up as before: splash, standby, clock window, RustDesk, consoles and their login timeout. The updater
applies an update only with nobody touching the screen, holds off during a touch and during a deploy, and rolls back when
the new host's display doesn't come up (e.g. `timeclock-display` masked on the new deployment). Then promotion.

## 4. Item 2: crash records name the error

Today a failed task calls aeth_ext's `trigger_shutdown(reason, traceback_text)`, which logs only the reason, emails the
text and records no trail, so the log says "Startup stopped" and `crash_alerts/*.jsonl` has `"trails": []`.

1. Every fatal path that holds an exception reports the exception itself through aeth_ext's exception handling
   (`report_exc` or the running loop's exception handler; builder's call): `keep()`'s done callback in `app.py`, the
   page's `on_error`, and the scanner's lost reader (`scanner.py`'s `_run` passes `str(e)` to `on_fatal` today; it
   passes the `ReaderGone`). The log then carries the traceback, the alert its traceback picture, and the crash record
   a trail.
2. Each crash record also carries every fatal error's message and traceback. aeth_ext gives shutdown callbacks only the
   trails, so the crash-alert writer keeps a copy of each CRITICAL log record (aeth_ext logs one with the traceback per
   fatal error) and writes them into the line, e.g. `"errors": [{"message": ..., "traceback": ...}]`, beside today's
   fields. It must still finish inside the 1 s fatal budget.
3. The crash-alert writer is registered before the reader opens and before the store check (§5), so a failure there
   also leaves a crash record (my call).
4. aeth_ext's fatal handling does nothing under `__debug__`; the clocks run with `-O`.

## 5. Item 3: the local store survives a schema change

The store keeps timeclock-schema models as JSON. A row in an old shape crashed every clock at startup on 2026-10-28.
Punches and prints are read later, so an old-shape row there would crash mid-day instead.

1. No schema version. At every startup, before the clock first loads its caches, read every stored row with its model.
2. **Copied rows** (employees, groups, rules, business hours, settings, punch corrections, and punches, prints and
   bypasses the clock doesn't still have to push): a table, or the copied part of one, with any unreadable row is
   emptied and logged. A pushed bypass is never read again and its punch went with it, so dropping it loses nothing.
   The next catch-up refills it. (Synced punches are safe to drop: the catch-up's open-shift query fetches open shifts
   from before its window.)
3. **Unsent rows** (pending employees, unsent punches, bypasses, unsent prints, fingerprint changes, scans): an
   unreadable one stops the clock with an error naming its table and id, whose traceback reaches the log and crash
   record through item 2.
4. Remove `LocalStore.__init__`'s cleanup of prints stored without a finger.
5. Known limit: models ignore unknown fields (`extra="ignore"`), so a renamed field with a default reads as the default
   until the next catch-up.

## 6. Item 14: PIN length

1. A new PIN is 4 to 12 digits, checked where PINs are set: the clock's `check_new_pin` (`clock.py`) and the backend
   page's Set PIN (`employee_editor.py`'s `save_pin`), which keep their cross-reference comment.
2. 12 is the clock's pad limit, so the page refuses more.
3. Existing PINs keep working (only hashes are stored).
4. Both error texts replace `PIN_DIGITS_ONLY` (wording my call, e.g. "A PIN is 4 to 12 digits"). The page's made-up PINs
   stay 4 digits.

## 7. Item 16: larger touch areas

1. Every button on every screen (keypad keys, punch buttons, Use PIN, Cancel, menus) takes taps up to 12 px past its
   drawn edge. Where two buttons sit closer than 24 px, the space between them splits down the middle, so no two touch
   areas overlap. On the keypad (8 px gaps) the gaps stop being dead spots. Nothing drawn moves or changes size.
2. The gear is drawn about half again as big and takes taps anywhere in a box over its corner of the screen (size my
   call), overlapping no other control.
3. How the areas are made is the builder's call (e.g. an invisible `::before` with per-side negative insets). The idle
   and confirmation screens' full-screen tap layers stay beneath every button.

## 8. Item 17: update toasts

The look is the approved mockup, version 4: https://claude.ai/artifact/92neD6jb6Ae9X7Ts42e9Qa

### 8.1 The system update toast

1. Bottom right, above the database status line, sliding in from the right. Title "Downloading a system update", a line
   like "42% · 312 of 740 MB", and a yellow bar on a dark track following the download.
2. A line under the bar says when it installs, from the moment the toast appears:
   - Window mode (production): "Installs at the scheduled time, 3:00 AM tonight.", the time taken from the window's
     start. Once downloaded, the title becomes "System update downloaded" and the toast slides away after a few
     seconds.
   - Immediate mode (test clocks): "The clock will restart as soon as download completes." while downloading; once
     downloaded, "The clock will restart to install it immediately.", and the toast stays until the restart.
3. Channel switches (`bootc switch`) download too, and show the same toast.

### 8.2 Where the progress comes from (H2)

1. The updater runs `bootc upgrade` and `bootc switch` with bootc's hidden `--progress-fd` on a pipe (JSON lines; it
   refuses anything but a pipe; bootc 1.17.1 on the dev clock accepts it). The line format is to be confirmed on the
   dev clock. If bootc ever refuses the option, the updater runs the command without it, so an update never waits on
   the toast.
2. It writes the state into the app's status folder, `/data/timeclock_system_files/status`, as one small JSON file
   (name and fields my call: state downloading or downloaded, bytes done and total, mode, window start), replaced
   atomically and at most about once a second. `/data` sits on the clocks' old SSDs. The file goes away when the
   restart starts or the download fails.
3. The app reads it on its 0.5 s tick, as it reads the banner, and treats a file not updated for several minutes while
   "downloading" as gone, as the banner's 10-minute limit does.

### 8.3 The app update toast

The amber banner at the top becomes a toast in the same bottom-right stack, above the system toast when both show:
"Installing a clock app update", "The clock will restart in about a minute.", and a moving bar, since a build reports no
progress. The banner file still turns it on and off; the toast uses the app's own wording, not the file's text. The
status app keeps writing the text unchanged, so the app release before this one (v0.9.0) still shows its banner between
H1 and this app release (my call).

## 9. Item 20: screen off after hours

### 9.1 When

The screen turns off when the clock is asleep and no input of any kind (touch, keyboard, mouse) has arrived for 30 s.
Asleep is today's rule for the reader closing: after business hours, with the idle screen up, 30 s after the last tap.
Any input turns it back on. A RustDesk session's mouse is input, so remoting in wakes it.

### 9.2 The clock app

1. It marks that it's asleep with a file in its status folder, written when the reader closes and removed when it opens;
   a leftover is removed at startup (file name my call).
2. While asleep, an invisible layer over the whole page sits above every control; a tap on it only wakes the clock, as
   a tap on the idle screen does today, and never presses a button underneath. Keys behave as today.

### 9.3 The host (H2)

1. A small screen-power service in timeclock-host (`timeclock-screen`, name my call), in the updater's crate and sharing
   its copied sway client and input tracking, but its own process (my call): the updater is meant to change rarely.
2. Off exactly when the asleep file exists, the clock window is showing, and 30 s have passed with no input; on
   otherwise. It sends `output * power off` or `on` through sway's socket only when that changes, and turns the screen on
   when it starts.
3. Requiring the clock window covers a crashed app: the watcher shows the standby, the screen comes on, and the standby
   shows the problem even if the asleep file was left behind.
4. Whether each monitor's touch panel keeps sending touches while the screen is off isn't tested up front (owner); the
   dev-clock round and the rollout show it on each monitor.
5. If a monitor can't be woken by touch while off, the fallback is no code: `systemctl mask timeclock-screen` on that
   clock over SSH (owner). The mask lives in `/etc`, so host updates keep it.

## 10. TODO.md

Remove 5 and 18 in this branch, and each of 2, 3, 14, 16, 17 and 20 once it lands. 19 stays for its recorder: its
display step is done, and its settled recorder decisions live in the decisions log.

## 11. Constraints

- Production deploys only through `poe release-and-pin` (app, backend); host releases through timeclock-host's release
  and promotion. Dev clock first. Releases are run by hand, never from scripts.
- The watcher and the updater are written once with high scrutiny and meant to change rarely: move, don't rewrite.
- Branch work in worktrees; follow each repo's own `AGENTS.md`.
- Screens: 1366x768 (main-warehouse, dev), 1920x1080 (main-office).

## 12. Left to the builder

- How fatal exceptions reach aeth_ext in §4.1, and how the writer captures CRITICAL records in §4.2.
- Where the startup row check lives in the store, and the warning's wording (§5).
- How the touch areas are built (§7.3).
- The progress and asleep files' exact names and fields (§8.2, §9.2).
- Toast and error wording beyond what's quoted here.
- Test coverage, under each repo's rules.
