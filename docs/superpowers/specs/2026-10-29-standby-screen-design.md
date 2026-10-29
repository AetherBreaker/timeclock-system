# Standby screen, screen watcher and boot splash: design

2026-10-29. Replaces the TODO "A 'being updated' screen while the app is down". Decisions are marked **(owner)** when
the owner made them in the design discussion and **(my call)** when I made them; the owner hasn't reviewed the latter.

## 1. Why

When Coolify deploys, the clock's menu drops: first the page goes dead, then the kiosk container restarts and the
screen falls to a Linux text console until the new kiosk is up. After a reboot people see firmware, GRUB's menu, kernel
text and a login prompt. The goal is to **hide every Linux internal from the people using the clock**, and to replace
each of those moments with a professional screen that says what's happening (owner).

### Goals

- A standby screen that owns the display whenever the clock can't, with two pages: **updating** (a deploy) and
  **starting** (boot, or the app launching), each showing live steps (owner).
- A heads-up banner on the clock while a deploy builds, before the clock goes down (owner).
- From power-on: no boot menu, no kernel text, no login prompt; a boot splash until the standby screen is up (owner).
- Servicing still works: Ctrl+Alt+F*n* reaches a console, and nothing switches it away while it's in use (owner).

### Non-goals

- Seeing the standby screen over RustDesk. RustDesk captures the kiosk's compositor only and stops with the kiosk
  container; that's accepted (owner). kmsvnc, which could, is a TODO for later (owner).
- Covering the firmware's HP logo, which stays.
- Moving the display to the host (one compositor, RustDesk on the host). Considered and rejected: it would upend the
  kiosk just after the first production deploys (owner).

### Anti-goals

- Linux text in front of a user at any point a normal deploy, boot or kiosk restart can reach.
- A screen switcher that depends on a protocol or handshake with anything else (owner).
- The clock unusable for a whole build by default; punches keep working until the old app stops (owner, §5.4).

## 2. The pieces

| Piece | Where it runs | Language | Changes how often |
| --- | --- | --- | --- |
| **Watcher**: decides which terminal is on screen | host, systemd, root | Rust (owner) | written once, then frozen (owner) |
| **Status app**: collects live status, serves the standby pages, writes the banner | host, systemd, root | Python (owner), Debian's 3.13, standard library only (my call) | often; edited on the clock when debugging (owner) |
| **Standby display**: cage + Firefox showing the status app's page | host, systemd, terminal 7 | the kiosk's tooling (owner) | rarely |
| **Kiosk** (existing): cage + Firefox showing the clock | container (Coolify), terminal 1 | unchanged, plus its check-in (§3.3) | with the app |
| **Clock app** (existing) | container (Coolify) | Python | writes its status and shows the banner (§6) |
| **Boot**: GRUB hidden, Plymouth splash, quiet console | host | image config | rarely |

The watcher and the status app live in a **new repository** with its own releases, added to this repository as a git
submodule for development (owner). I'd name it `timeclock-standby` and put the submodule at `standby/` (my call). Its
releases are independent of the clock's: the only contracts are the small files and facts listed in §6 and §4.1, kept
stable, with each side ignoring fields it doesn't know (owner agreed none need tying).

## 3. Sharing the screen between two displays

### 3.1 Facts it rests on

From seatd 0.9.1's source (Debian's, the kiosk's version):

- seatd binds a display to **whichever terminal is showing when the display connects** (`seat_add_client` reads the
  active terminal). There's no option to choose one; `SEATD_VTBOUND=0` turns terminal handling off entirely, which
  would break Ctrl+Alt+F*n*.
- Two seatds on **different** terminals coexist: on a switch, each releases or reclaims only its own terminal
  (`seat_vt_release` / `seat_vt_activate`). The only conflict is two displays starting on the same terminal.
- A display whose terminal isn't showing keeps running; seatd only pauses its devices. But seatd **refuses to open a
  device** for a display that isn't showing ("client is not active"), so a display must keep its terminal on screen
  until it has opened its devices.

seatd stays inside the kiosk container, as today; the standby display gets its own seatd on the host (my call: the
owner asked about one shared seatd on the host, which would turn a mis-ordered start into a clean refusal but makes both
displays depend on one host daemon; not yet confirmed either way).

### 3.2 Terminals

- **Terminal 1: the kiosk.** Its login prompt is removed (`getty@tty1` masked) and the console is blank: no cursor, no
  kernel messages (§8). When the kiosk isn't drawing, terminal 1 is black, never text.
- **Terminal 7: the standby display.** Above logind's automatic consoles (2–6), so no login prompt ever spawns there.
- **Terminals 2–6:** text consoles for servicing, Ctrl+Alt+F*n*, unchanged; they log out after 5 minutes idle (owner,
  §8).

### 3.3 Starting a display: check in, then hand back (owner)

The rule that prevents any fight: **a display only starts while its own terminal is showing.** So each display checks
in on its terminal and hands the screen back as soon as its devices are open:

1. Before starting cage, the display reads which terminal is showing (`/sys/class/tty/tty0/active`).
   - If it's its own terminal or the other display's (1 or 7): it switches to its own terminal itself (`chvt`).
   - If it's a text console (2–6), someone is servicing: it waits until that changes. It never switches away from a
     console.
2. It starts `seatd-launch -- cage -s -- <wrapper> firefox-esr ...`. Cage starts its client only once its display is
   up (to verify, §10), so the **wrapper** runs exactly when the devices are open.
3. The wrapper switches back to the terminal that was showing before step 1, if that terminal is still claimed
   (in graphics mode), then `exec`s Firefox. If it wasn't claimed (nothing to go back to), the display stays on screen.
4. Firefox loads behind the other screen. The watcher shows it when it's ready (§4).

The visible cost is a blink: from the switch in step 1 to the switch back in step 3. Keeping it short (owner agreed):

- **No monitor resync.** Everything uses the panel's native mode (cage and the console both default to it), and the
  kernel command line gets `i915.fastboot=1`, so a switch between same-mode terminals skips the full modeset. Haswell
  (the G1) may not default to it. Likely the largest part of the blink.
- **Switch as late as possible.** Everything that doesn't need the screen happens before step 1: udev, the Firefox
  profile, the remote-access install, the wait for the app's favicon (kiosk only, existing).
- **Hand back at once.** The wrapper (step 3) needs no polling; cage itself signals the moment.
- **Measure it** on main-office, by phone video and timestamps, before tuning further. My estimate, 0.3–0.7 s without a
  resync, is a guess.

The kiosk keeps its existing wait for the app's favicon (up to 60 s) before step 1, so it checks in once the app serves
and the clock loads straight away behind the standby screen (my call). Its image gains `kbd` for `chvt`.

### 3.4 Boot order

Plymouth holds the screen from early boot. The host units start in this order (my call):

1. **Status app** (so the page has something to serve).
2. **Standby display**: `plymouth quit --retain-splash`, then its check-in on terminal 7 (§3.3). At boot nothing is
   claimed, so it stays on screen.
3. **Docker**, ordered `After=` the standby display unit (a drop-in), so no kiosk can start while Plymouth still holds
   the display. `After=` only orders: a failed standby doesn't stop Docker.

The watcher starts early too, but it never switches to an unclaimed terminal (§4.2), so it can't pull Plymouth's screen
away before the standby is up.

## 4. The watcher (owner: Rust, written once, frozen after thorough testing)

It talks to nothing and nothing talks to it: **no protocol, no handshake** (owner). It decides only from what it can
observe on the machine.

### 4.1 What it observes

| Observation | How |
| --- | --- |
| Which terminal is showing | `/sys/class/tty/tty0/active`, waited on with `poll` for changes |
| Whether a terminal is claimed by a display | `KDGETMODE` on `/dev/ttyN`: graphics mode |
| Keyboard and touch input | every `/dev/input/event*`, read without grabbing (it never takes events from the kiosk); rescanned for hotplug |
| Ctrl+Alt+F*n* presses | the same input events: F*n* down while Ctrl and Alt are held |
| **Clock live** | terminal 1 claimed, **and** a process named `firefox-esr` in a network namespace other than the host's holds an ESTABLISHED TCP connection to port 8080 (`/proc/<pid>/net/tcp` and `tcp6`). The clock page keeps that connection open while it works and loses it within seconds when the app stops. |
| **Deploy running** (deploy-start mode only) | a running container whose image repository is `coollabsio/coolify-helper` (seen on main-office, tag `1.0.19`), via the Docker API on `/var/run/docker.sock`, every 2 s. Coolify's `sentinel` container also runs permanently and must not match. |

These facts are its only dependencies: terminals 1 and 7, the browser's process name, the app's port, the helper's
image name. All of them are settings in its config file, so a change never needs a code edit (owner agreed).

### 4.2 Rules

- **Wanted screen:** terminal 1 when the clock is live and (in deploy-start mode) no deploy is running; terminal 7
  otherwise.
- **Debounce:** the clock must be not-live for 3 s before the watcher flips to the standby, so a connection blip
  doesn't flicker. A deploy starting (deploy-start mode) flips at once. Going to the clock is immediate.
- **Never switch to an unclaimed terminal.** If the wanted terminal isn't claimed, leave the screen alone.
- **The watcher's own switches** use `VT_ACTIVATE` + `VT_WAITACTIVE`; it remembers them so it never mistakes them for
  anyone else's.
- **A user's switch:** the terminal changed, Ctrl+Alt+F*n* was pressed within the last 0.5 s, and *n* is the new
  terminal (owner agreed). Touch input never counts. The watcher then stays hands-off until 2 minutes pass with no
  keyboard or touch input, every input resetting the timer (owner). It resumes at once if the user lands on the screen
  it wants anyway.
- **Anyone else's switch** (a display checking in or out, `chvt` over SSH): wait 5 s for things to settle, then apply
  the rules. This is what lets a check-in finish without the watcher interfering.
- **Flip mode**, a per-clock setting in its config (owner): `app-stopped` (default, owner) or `deploy-start`. With
  `deploy-start`, the standby takes over as soon as Coolify's helper appears; with `app-stopped`, when the clock stops
  being live.

Switching a terminal never clears it: a console keeps its text and its running commands.

### 4.3 Config

`/etc/timeclock/watcher.conf`, `key = value` lines, every key optional (my call on names and defaults):

```ini
flip = app-stopped              # or deploy-start
clock_vt = 1
standby_vt = 7
idle_return_secs = 120
settle_secs = 5
standby_delay_secs = 3
user_key_window_ms = 500
browser_process = firefox-esr
app_port = 8080
helper_image = coollabsio/coolify-helper
docker_socket = /var/run/docker.sock
```

It reads the file at start; a changed setting takes effect with `systemctl restart timeclock-watcher` (my call).

### 4.4 Failure behaviour

- **Status app or its page down:** no effect on the watcher; the standby display shows its fallback page (§5.5).
- **Standby display down:** terminal 7 isn't claimed, so the watcher can't show it and leaves the screen as it is.
  systemd restarts the standby display.
- **Docker down:** "deploy running" reads false.
- **Watcher down:** systemd restarts it (`Restart=always`, 1 s). While it's down a kiosk that checks in hands back to
  the standby and nothing shows the clock; that's the risk the freeze-after-testing rule exists for.

### 4.5 Build and tests

- Standard library plus `libc` only; no async runtime: one loop waiting on the terminal file, the input devices and a
  timer (100 ms). A static `x86_64-unknown-linux-musl` binary, about 1 MB, at `/usr/local/sbin/timeclock-watcher`.
- The decision is a pure function (observations and remembered state in, action out), unit-tested over every case:
  normal switching, debounce, unclaimed targets, a user's switch, the idle return, input resetting it, resuming on the
  wanted screen, another program's switch and the settle, both flip modes, Docker unreachable.
- On main-office (§10): deploys in each flip mode, Ctrl+Alt+F2 and F7 with the idle return, the status app killed
  mid-deploy, a reboot, the kiosk crashing.

## 5. The status app (owner: Python, editable on the clock)

### 5.1 Pages

- `GET /updating` and `GET /starting` (owner): **one HTML file and one CSS file** (owner), served for both; the page
  reads its mode from the path. `GET /` redirects to the current mode; the standby display opens `/` (my call).
- `GET /events`: Server-Sent Events carrying the mode and the step list as they change. A few lines of JavaScript in the
  page; the browser reconnects by itself. A mode change navigates the page to the other endpoint.
- Listens on `127.0.0.1:8091` only (my call on the port). To see it from the workstation: an SSH tunnel.
- The look matches the boot splash (logo, colours), so boot reads as one continuous screen.

### 5.2 Live steps and their sources

The status app collects everything; the watcher collects nothing (owner).

| Mode | Steps (wording my call) | Source |
| --- | --- | --- |
| starting | Starting up | the status app running |
| | Connecting to the network | NetworkManager connectivity |
| | Starting Docker | `docker.service` state |
| | Starting the time clock | the app container started (Docker events) |
| | *the app's own steps* | the app's status file (§6.1) |
| | Opening the clock | app serving (`/favicon.ico`), then terminal 1 claimed (the kiosk checked in) |
| updating | Preparing an update | Coolify's helper container appeared |
| | Building version X | a new app image appeared; its version label (§6.3) |
| | Stopping the old version | the old app container stopping |
| | Starting version X | the new app container started |
| | *the app's own steps* | the app's status file |
| | Opening the clock | as above |

The status app collects a deploy's steps from the moment the helper appears, whatever the flip mode, so the page has the
whole story when it shows, including when someone presses Ctrl+Alt+F7 to watch a deploy (owner).

**Updating vs starting:** updating from the helper's appearance until the clock is live again; starting otherwise
(boot, an app or kiosk restart). Decided from Docker's events on the clock itself, not GitHub tags: GitHub's latest tag
can differ from what's pinned, kiosk-only deploys have none, and it needs the internet (my call; the owner suggested
GitHub tags and an app version endpoint early on).

Containers are found by their compose service labels (`com.docker.compose.service` = `timeclock-system`, `kiosk`).
"Clock live" is observed the same way the watcher does (§4.1), in the status app's own code.
A clock that has never been deployed (fresh install, before `new_clock.py`) shows starting with "Waiting for setup"
(my call).

### 5.3 The banner

When the helper appears, the status app writes the banner file into the app's persisted folder (§6.2), with the text
to show, for example "An update is being installed. The clock will restart in about a minute." (wording my call). It
deletes the file once the clock is live again, or when the helper disappears without a restart.

### 5.4 Punches during a deploy

Safe in either flip mode: a punch is committed to the clock's SQLite store, on the host's disk through the persisted
folder, before the confirmation shows; the new app pushes unsynced punches on start; and a re-sent punch is ignored by
the database (`ON CONFLICT (id) DO NOTHING`). So `app-stopped` is the default: the v0.6.2 deploy took ~2 min from push
to new containers, against ~20 s of downtime.

### 5.5 Fallback when the status app is down

The standby Firefox's own `kiosk.cfg` shows a static copy of the page from disk (same look, no steps) when the status
app doesn't answer, and returns to `/` once it does (my call; the owner had accepted a black screen in this case when
the watcher still followed the status app).

### 5.6 Config and updates

`/etc/timeclock/standby.conf` for its own settings (port, paths, banner text) (my call). Files under
`/opt/timeclock-standby/`: `status.py`, `standby.html`, `standby.css`, the fallback page. Updating a clock is
replacing a file over SSH and restarting the unit (owner).

## 6. The clock app's part (this repository)

### 6.1 Status file

During startup the app writes its current step to `/app/persisted_data/status/app.json` (host:
`/data/timeclock_system_files/status/app.json`), atomically (temp file and rename): `{"step": "<display text>",
"version": "<its version>", "at": "<ISO time>"}`. The text is display-ready, so the app can add or rename steps without
the status app changing. Steps: loading the local store, starting the fingerprint reader, ready (my call on the cut).
The VPN comes up in devkit-container before the app starts and is covered by "Starting the time clock".

### 6.2 Banner

The app checks for `/app/persisted_data/status/banner.json` (`{"text": "..."}`) on its existing 0.5 s tick, and shows
the text in a banner across the top of every screen while it exists and was written in the last 10 minutes (so a
status app that died mid-deploy can't leave it up) (my call on the placement and the age limit).

### 6.3 Version label

`docker/Dockerfile` labels the final image `org.opencontainers.image.version=$GIT_TAG`, so the status app knows the
incoming version from the new image before the app starts.

### 6.4 Unchanged

The app's health check (heartbeats and favicon), `kiosk.cfg`'s 30 s error-page reload, `cage -s`, and RustDesk.

## 7. Timelines

**A deploy, `app-stopped` mode:** the helper appears → the banner shows on the clock, steps start collecting → the old
app stops → the clock page loses its connection; 3 s later the watcher shows the standby (updating) → the old kiosk
stops (terminal 1 goes black, hidden) → the new app starts and its steps show → it serves, compose starts the new kiosk →
the kiosk checks in (blink) and hands back → the clock loads behind the standby → the connection opens → the watcher
shows the clock. The banner file is deleted.

**`deploy-start` mode:** the same, except the watcher shows the standby as soon as the helper appears.

**Boot:** HP logo → (GRUB hidden) → Plymouth splash → standby (starting) with steps → kiosk check-in → clock.

**Kiosk crash:** connection gone → 3 s → standby (starting) → Docker restarts the kiosk → check-in → clock.

## 8. Hiding Linux

- **GRUB:** `GRUB_TIMEOUT_STYLE=hidden`, `GRUB_TIMEOUT=1`; holding Esc during that second shows the menu for servicing.
  On main-office today: `GRUB_TIMEOUT=5`, menu shown. Check the hidden style really shows nothing on the G1, and that the
  RAID's second EFI partition gets the same config (`sync-esp`).
- **Kernel command line** (`GRUB_CMDLINE_LINUX_DEFAULT`): `quiet splash loglevel=3 i915.fastboot=1`; and
  `kernel.printk = 3 4 1 3` by sysctl, so runtime kernel messages stay off the console.
- **No cursor on terminal 1:** a boot-time oneshot writes the hide-cursor sequence (`\033[?25l`) and a clear to
  `/dev/tty1`. Not `vt.global_cursor_default=0`, which would hide it on the servicing consoles too.
- **Plymouth** with a theme in the standby page's look (logo, colours, a spinner) (owner wants a splash; theme my call).
  It also covers shutdown and reboot.
- **No login prompt on terminal 1:** `systemctl mask getty@tty1.service`.
- **Console auto-logout** after 5 minutes idle on terminals 2–6 (owner): `TMOUT=300`, read-only, set in
  `/etc/profile.d/` for login shells on `/dev/tty*` only, so SSH sessions are unaffected.

## 9. Repository, build, install, update

- **New repo** `timeclock-standby` (public, like every timeclock repo): `watcher/` (Cargo crate), `status/`
  (`status.py`, `standby.html`, `standby.css`, fallback page), `display/` (the standby display's script, wrapper and
  Firefox policies / `kiosk.cfg`), `systemd/` (the units and the Docker drop-in), `plymouth/` (the theme). Releases
  are tags; a GitHub Actions workflow builds the watcher on a tag and attaches it with the other files (my call;
  names follow AGENTS.md's workflow naming).
- **This repo:** the submodule at `standby/`; §6's app changes; the kiosk check-in (`kiosk.sh`, the wrapper, `kbd`), in
  the backup kiosk (`docker/kiosk-cage-only/`) too; both images.
- **ISO build** (both images: the dev device runs the same compose, my call): downloads the new repo's latest release
  and bakes in the watcher and a fallback copy of the status app (owner: both baked in).
- **Install** (`late.sh`): installs the host packages (`python3` explicitly, `cage`, `seatd`, `firefox-esr`, `kbd`,
  `plymouth`), the units and config; **fetches the status app from the new repo's latest release** if the network
  allows, else uses the baked copy (owner: from its repo at first install if possible; "latest release" my call).
- **Updating a clock:** replace a file over SSH and restart its unit (owner). The watcher, frozen, normally never.
- **Existing clocks** (main-office, timeclock-dev): a one-off install script over SSH from this repo, or a reinstall
  (my call; to settle when it's time).

## 10. Verify first, on main-office

Before building on them, in this order:

1. Two seatds, container on terminal 1 and host on terminal 7, hand the screen back and forth with `chvt`, and
   Ctrl+Alt+F*n* works from both displays.
2. Cage starts its client only once its display is up, so the wrapper's switch back never makes cage fail.
3. Firefox loads the clock and opens its connection while its terminal isn't showing.
4. The blink: its length, with and without `i915.fastboot=1`.
5. Plymouth hands over to the standby display with no text or flash in between; GRUB hidden with Esc working.
6. During a Coolify deploy: the helper container's lifetime, the order compose stops and starts the containers, and how
   fast the clock page's connection drops when the app stops.

Then the on-device tests for the whole: each flip mode through a real deploy, a reboot, a kiosk crash, the status app
killed mid-deploy, the standby display killed, Ctrl+Alt+F2 / F7 with the 2-minute return, and a console left logged in.
