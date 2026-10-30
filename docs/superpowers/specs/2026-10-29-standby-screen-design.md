# Standby screen, screen watcher and boot splash: design

2026-10-29, second version (same day). Replaces the TODO "A 'being updated' screen while the app is down". Decisions are
marked **(owner)** when the owner made them in the design discussion and **(my call)** when I made them; the owner
hasn't reviewed the latter.

## What changed from the first version

The first version gave each screen its own display server on its own terminal (the clock's cage + Firefox in a
container on terminal 1, the standby's cage + Firefox on the host on terminal 7) and a watcher that switched terminals.
Building it showed that every terminal switch costs a blink (7–9 black frames at 30 fps even after tuning) that only a
patched cage could remove, and that the standby's Firefox took 2 s to draw at boot. This version (owner):

- **One compositor on the host, sway, showing both screens as windows.** The watcher shows or hides the clock's window
  through sway's control socket instead of switching terminals. Measured on main-warehouse: no black frames at all.
- **cog (WPE WebKit) instead of Firefox** for both screens (owner), each a host service. **The kiosk container is
  gone**: Coolify deploys only the app.
- **RustDesk on the host**, capturing whatever sway shows, the standby included, and staying up through deploys
  (owner). Same package and settings folder, so every clock keeps its RustDesk ID.
- Unchanged: the status app and its page, the live steps, the banner, the app's status file, the Plymouth splash, the
  quiet boot, the console rules, the flip modes.

Already built and kept from the first version: the splash fix (all of the spinner theme's images), its hand-off (switch
terminals while Plymouth runs, then quit it), cog for the standby page, the status app, the banner, the app's steps.
Built and dropped: the kiosk's check-in and both `handback.sh` scripts, the graphics-mode trick, the kiosk images
(`docker/kiosk/`, `docker/kiosk-cage-only/`), the watcher's switching between terminals 1 and 7.

## 1. Why

When Coolify deploys, the clock's menu drops: first the page goes dead, then the kiosk container restarts and the
screen falls to a Linux text console until the new kiosk is up. After a reboot people see firmware, GRUB's menu, kernel
text and a login prompt. The goal is to **hide every Linux internal from the people using the clock**, and to replace
each of those moments with a professional screen that says what's happening (owner).

### Goals

- A standby screen that shows whenever the clock can't, in two modes: **updating** (a deploy) and **starting** (boot,
  or the app launching), each showing live steps (owner).
- A heads-up banner on the clock while a deploy builds, before the clock goes down (owner).
- From power-on: no boot menu, no kernel text, no login prompt; a boot splash until the standby screen is up (owner).
- No black between screens: the hand-over between the standby and the clock is instant (owner).
- Remote access (RustDesk) that sees and controls the screen at all times, deploys included (owner).
- Servicing still works: Ctrl+Alt+F*n* reaches a console, and nothing switches it away while it's in use (owner).

### Non-goals

- Turning the screen off after hours with wake on touch: later (owner). sway can do it (`output * power off`); it needs
  a finger test on the monitor and an overlay that catches the waking touch.
- Overlays drawn by the host (an on-screen keyboard, notices): none now; the banner stays in the app (owner).
- Covering the firmware's HP logo, which stays.

### Anti-goals

- Linux text in front of a user at any point a normal deploy, boot or browser restart can reach.
- A screen switcher that other programs have to talk to (owner). The watcher still decides from observation only;
  nothing reports to it. It now acts through sway's control socket where it used to act through the kernel's terminal
  calls (my reading of the owner's rule; see §4).
- The clock unusable for a whole build by default; punches keep working until the old app stops (owner, §5.4).

Accepted cost (owner): one compositor for both screens means a sway crash takes both down until systemd restarts it.
Functionally only the app's faults matter; anything else is visual and gone by the time a deploy completes.

## 2. The pieces

| Piece | Where it runs | Language | Changes how often |
| --- | --- | --- | --- |
| **Display**: sway on terminal 7, in a login session | host, systemd, root | config (my call) | rarely |
| **Standby browser**: cog on the status page | host, systemd | config | rarely |
| **Clock browser**: cog on the clock app | host, systemd | config + a small wrapper | rarely |
| **Remote access**: RustDesk's service and the screen-sharing stack | host, systemd | the vendor package | with the package |
| **Watcher**: decides which window shows | host, systemd, root | Rust (owner) | written once, then frozen (owner) |
| **Status app**: live status, the standby page, the banner | host, systemd, root | Python (owner), Debian's 3.13, standard library only | often; edited on the clock when debugging (owner) |
| **Clock app** (existing) | container (Coolify) | Python | writes its status, shows the banner, publishes its port to the host (§6) |
| **Boot**: GRUB hidden, Plymouth splash, quiet console | host | image config | rarely |

Everything on the host lives in the **`timeclock-standby` repository** (owner), a submodule at `standby/`, with its own
releases. The name stays though it now holds the whole display (my call: a rename is churn for nothing). The only
contracts with this repository are the files and facts in §6 and §4.1, kept stable, each side ignoring fields it doesn't
know (owner agreed).

## 3. One compositor for both screens

### 3.1 How it works

sway runs on the host, on terminal 7, from boot. It has two windows:

- the **standby window** (cog on the status app's page), tiled alone, so it fills the screen whenever nothing covers it;
- the **clock window** (cog on the clock app), hidden in sway's scratchpad except when the watcher shows it,
  fullscreen, on top of the standby.

Showing the standby is hiding the clock. When the clock window closes (its browser restarting, sway restarting) the
standby underneath is what shows, with no action from anyone. Measured on main-warehouse (spike, 2026-10-29, screen
captures at 30 fps): showing, hiding, the clock's browser stopping while shown and starting again, all with no black
frame; the clock's window appeared hidden every time.

### 3.2 Windows (my call: the mechanics; tested in the spike)

- Each cog sets its own app id with `--gapplication-app-id`: `local.timeclock.Standby` and `local.timeclock.Clock`.
  cog's default (`com.igalia.Cog`) is the same for both, which sway's rules can't tell apart.
- sway's config hides the clock's window the moment it appears: `for_window [app_id="^local\.timeclock\.Clock$"] move
  scratchpad`. A rule in the config applies before the window's first frame; a command sent afterwards doesn't. In the
  spike, without it, sway tiled the clock beside the standby, both half-width.
- **Any other window** (none is expected; a stray dialog) also goes to the scratchpad, so only the standby is ever tiled.
- Show the clock: `[app_id="^local\.timeclock\.Clock$"] scratchpad show, fullscreen enable`. Hide it: `... fullscreen
  disable, move scratchpad`. `scratchpad show` **toggles**, so the watcher reads the window's state from sway's tree
  before sending either, and sends nothing when it's already right.
- sway's config has **no key bindings** (nothing to rearrange or close a window with), no bar, no borders, no
  Xwayland, the cursor hidden after 3 s still (`seat * hide_cursor`). The number pad types digits whatever Num Lock says
  (`xkb_options numpad:mac`, as the kiosk has today). Ctrl+Alt+F*n* (sway handles it) still reaches the consoles.
- A hidden window isn't repainted. In the spike, the clock's logo faded in over about 0.5 s when it was first shown, the
  page catching up. Accepted; the old design's switch to the clock cost 1–6 black frames for the same reason.

### 3.3 The browsers

Both are host services with `Restart=always`, started only once sway is ready: a cog started the moment sway's socket
appeared crashed ("Could not find a usable platform module"). Each unit waits until sway's control socket answers
(`swaymsg -t get_version`) (my call), and restarts with sway (`BindsTo=`).

- **cog flags**, both: `--platform=wl --bg-color=black --webprocess-failure=restart`. Black before the first paint, never
  Firefox's white window. Measured: restarting the standby, Firefox showed 38 black frames then 12 white, cog 8 black.
- **Standby**: starts on `fallback.html` from disk, which moves to `http://127.0.0.1:8091/` as soon as the status app
  answers (built). It never shows an error page; if the status app dies later, the live page keeps its last state and
  its event stream reconnects by itself.
- **Clock**: opens `http://127.0.0.1:8080/?kiosk` (`?kiosk` keeps the app's cursor hiding, master spec R13.2). It
  starts on a start page from disk (`clock-start.html`) that probes the app's `/favicon.ico` every second and moves to
  the clock once it answers, replacing the old favicon wait (my call). **A failed load ends the browser** (my call): a
  small wrapper reads cog's log and stops cog when a load fails; systemd starts it again on its start page. That
  replaces Firefox's error-page reload (`kiosk.cfg`). It's invisible: the clock window is hidden whenever the clock
  isn't live (§4).
- The clock browser **survives deploys**: only the app restarts. The page's own connection drops and comes back; the
  watcher shows the standby meanwhile.
- Firefox's kiosk settings (master spec R13.2) and cog: session restore and swipe navigation don't exist in cog; pinch
  zoom is checked on the touchscreen (§10), and if WebKit zooms the app turns it off in its own page
  (`user-scalable=no`), which works in any browser (my call).
- cog now runs for weeks between reboots rather than restarting on each deploy. Its memory is watched in §10; a nightly
  restart is the fallback if it grows (my call).

### 3.4 Remote access: RustDesk on the host (owner)

- **Package**: the image copies `sweetfiresupportdesk.deb` to `/data/timeclock_system_files/remote/` at install
  (already, `late.sh`); it stays out of git. A boot-time unit installs it on the host when it's newer than the installed
  one, as `remote.sh` did in the container at every start; installing on the host also sets up the package's own system
  service, which it couldn't in the container. No package, no remote access; everything else works.
- **Same RustDesk ID** (owner: the clocks stay in the organization's structure in RustDesk): its settings stay in
  `/data/timeclock_system_files/remote/config`, linked from root's `~/.config/sweetfiresupportdesk` as the kiosk does
  today. main-office keeps its ID with no re-enrolling.
- **Finding the screen**: RustDesk's service asks logind for the active graphical session. sway runs in a real login
  session (systemd `PAMName=login` on terminal 7, `XDG_SESSION_TYPE=wayland`) (my call), so it finds it with no stand-in.
  The container's `loginctl` stand-in comes along only if that doesn't work.
- **Screen sharing**: PipeWire, WirePlumber, xdg-desktop-portal and its wlroots backend as root's user services in that
  session, the backend set to share the one screen without asking (`chooser_type=none`, as today).
- **Input**: sway picks up RustDesk's virtual keyboard and mouse directly. RustDesk's virtual keyboard can't switch
  terminals (`xkb_options srvrkeys:none` for that device only): a remote Ctrl+Alt+F*n* would take the screen from sway
  and freeze the remote picture until someone switched back (my call).
- While sway restarts, RustDesk stays connected but has nothing to show for those seconds.

### 3.5 Terminals

- **Terminal 7: sway.** Above logind's automatic consoles (2–6), so no login prompt ever spawns there.
- **Terminal 1:** Plymouth's during boot, then unused. Its login prompt stays removed (`getty@tty1` masked) and its
  console blank, so Ctrl+Alt+F1 shows black, never text.
- **Terminals 2–6:** text consoles for servicing, unchanged; they log out after 5 minutes idle (owner, §8).

### 3.6 Boot order and the splash hand-off

1. **Watcher**, **status app**: as before.
2. **Display** (sway). Before sway starts, the unit checks in (built, now in the display unit): if a console (2–6) is
   showing, it waits; otherwise it puts terminal 7 in graphics mode and switches to it **while Plymouth still runs**
   (Plymouth answers the switch), then quits Plymouth with `--retain-splash` and puts terminal 1 back in text mode,
   unseen. Quitting Plymouth first leaves terminal 1 in graphics mode with nothing answering switches, and the kernel
   ignores every switch away from such a terminal: the boot froze on the splash for 11 s on main-warehouse until the
   kiosk freed it.
3. **Browsers** and **RustDesk**, once sway answers.

Docker needs no ordering any more (the drop-in that held it behind the standby goes, my call): nothing it starts
touches the screen.

## 4. The watcher (owner: Rust, written once, frozen after thorough testing)

Nothing talks to it: **no protocol, no handshake** (owner). It decides only from what it observes, and acts through
sway's control socket (showing or hiding the clock window) and, for consoles, the kernel's terminal calls.

### 4.1 What it observes

| Observation | How |
| --- | --- |
| Which terminal is showing | `/sys/class/tty/tty0/active`, waited on with `poll` for changes |
| Whether sway's terminal is claimed | `KDGETMODE` on `/dev/tty7`: graphics mode |
| Whether the clock window exists and shows | sway's tree (`GET_TREE` over its control socket): the window with the clock's app id, in the scratchpad or not |
| Keyboard and touch input | every `/dev/input/event*`, read without grabbing; rescanned for hotplug |
| Ctrl+Alt+F*n* presses | the same input events: F*n* down while Ctrl and Alt are held |
| **Clock live** | a process of the clock browser's unit (its cgroup, `timeclock-clock.service`) holds an ESTABLISHED TCP connection to the app's port (8080), **the same connection (socket inode) in two checks at least 1 s apart**: its socket inodes matched in `/proc/net/tcp` and `tcp6`. Per process, not per port: the status app's own favicon probe also connects to 8080. Held, not merely seen: Docker's port proxy accepts a connection before the app listens and closes it when the app refuses, so the start page's probe and the page's reconnect attempts make brief connections while a new app starts (my call, from the review). The clock page keeps its connection open while it works and loses it within seconds when the app stops (to confirm, §10). |
| **Deploy running** (deploy-start mode only) | a running container whose image repository is `coollabsio/coolify-helper`, via the Docker API on `/var/run/docker.sock`, every 2 s. Coolify's `sentinel` container also runs permanently and must not match. |

sway's control socket is a fixed path (`SWAYSOCK`, set by the display unit) (my call). The watcher speaks sway's IPC
directly (a small binary framing over a Unix socket), standard library only.

### 4.2 Rules

- **Wanted window:** the clock when the clock is live and (in deploy-start mode) no deploy is running; the standby
  otherwise.
- **Debounce:** the clock must be not-live for 3 s before the watcher hides it, so a connection blip doesn't flicker. A
  deploy starting (deploy-start mode) hides it at once. Showing the clock is immediate.
- **Act only on sway's terminal.** While a console shows, the watcher leaves windows alone (sway isn't on screen).
- **Consoles:**
  - **A user's switch** to a console: the terminal changed, Ctrl+Alt+F*n* was pressed within the last 0.5 s, and *n*
    is the new terminal (owner agreed). Touch input never counts. The watcher stays hands-off until 2 minutes pass with
    no keyboard or touch input, every input resetting the timer (owner), then switches back to terminal 7.
  - **Anyone else's switch** (`chvt` over SSH): wait 5 s, then switch back to terminal 7.
  - It switches back **even when terminal 7 isn't claimed** (sway down or restarting): the display's check-in waits for
    7 to show while a console does, so otherwise each would wait for the other and the clock would sit on a shell, then
    a login prompt (the first version had the same exception, found on main-warehouse, 2026-10-29). With sway down,
    terminal 7 is a blank console, never text. Its own switches use `VT_ACTIVATE` + `VT_WAITACTIVE` and are remembered,
    so it never mistakes them for anyone else's.
- **Flip mode**, a per-clock setting (owner): `app-stopped` (default, owner) or `deploy-start`, as before.

Switching a terminal never clears it: a console keeps its text and its running commands.

### 4.3 Config

`/etc/timeclock/watcher.conf`, `key = value` lines, every key optional:

```ini
flip = app-stopped              # or deploy-start
display_vt = 7
sway_socket = /run/timeclock-display/sway.sock
clock_app_id = local.timeclock.Clock
clock_unit = timeclock-clock.service
app_port = 8080
idle_return_secs = 120
settle_secs = 5
standby_delay_secs = 3
user_key_window_ms = 500
helper_image = coollabsio/coolify-helper
docker_socket = /var/run/docker.sock
```

`clock_vt`, `standby_vt` and `browser_process` go. It reads the file at start; a change takes effect on restart.

### 4.4 Failure behaviour

- **Status app down:** no effect on the watcher; the standby page keeps its last state or its start page (§3.3).
- **sway down:** the watcher can't reach it and does nothing; terminal 7 drops to a blank console until systemd
  restarts sway (seconds). Visual only (owner).
- **A browser down:** systemd restarts it. The clock's closing shows the standby by itself.
- **Docker down:** "deploy running" reads false.
- **Watcher down:** systemd restarts it (`Restart=always`, 1 s). While it's down the window on screen stays as it is.

### 4.5 Build and tests

- Standard library plus `libc` only; one loop waiting on the terminal file, the input devices and a 100 ms timer. A
  static `x86_64-unknown-linux-musl` binary at `/usr/local/sbin/timeclock-watcher`.
- The decision stays a pure function (observations and remembered state in, action out), unit-tested over every case:
  showing and hiding, debounce, both flip modes, the window already right, no clock window, sway unreachable, a user's
  switch to a console and the idle return, input resetting it, another program's switch and the settle, the return to
  an unclaimed terminal 7, a connection seen once and gone (not live).
- On main-warehouse (§10).

## 5. The status app (owner: Python, editable on the clock)

### 5.1 Pages

- **One page at `GET /`** for both modes, from one HTML file and one CSS file (owner). `GET /events`: Server-Sent
  Events with the mode and the steps as they change. Listens on `127.0.0.1:8091` only (owner).
- The look matches the boot splash (logo, colours), so boot reads as one continuous screen.

### 5.2 Live steps and their sources

The status app collects everything; the watcher collects nothing (owner).

| Mode | Steps | Source |
| --- | --- | --- |
| starting | Starting up | the status app running |
| | Connecting to the network | NetworkManager connectivity |
| | Starting Docker | `docker.service` state |
| | Starting the time clock | the app container started (Docker events) |
| | *the app's own steps* | the app's status file (§6.1) |
| | Opening the clock | the app serving (`/favicon.ico`), then the clock live (§4.1) |
| updating | Preparing an update | Coolify's helper container appeared |
| | Building version X | a new app image appeared; its version label (§6.3) |
| | Stopping the old version | the old app container stopping |
| | Starting version X | the new app container started |
| | *the app's own steps* | the app's status file |
| | Opening the clock | as above |

Unchanged otherwise: a deploy's steps are collected from the moment the helper appears whatever the flip mode;
updating vs starting decided from Docker's events on the clock; the app container found by its compose service label;
"Waiting for setup" before the first deploy. "Clock live" uses the watcher's definition (§4.1), in the status app's own
code; its config gets `clock_unit` in place of `browser_process`.

### 5.3–5.6 Unchanged

The banner (§6.2), punches during a deploy (safe in either flip mode; `app-stopped` the default), config in
`/etc/timeclock/standby.conf`, files under `/opt/timeclock-standby/`, updates by replacing a file over SSH (owner). The
fallback when the status app is down is the standby browser's start page (§3.3).

## 6. The clock app's part (this repository)

### 6.1–6.3 Unchanged

The status file (`/app/persisted_data/status/app.json`, display-ready step text, atomic), the banner
(`banner.json`, read on the 0.5 s tick, ignored after 10 minutes), the image's version label. All built.

### 6.4 Compose

- **No kiosk service.** `docker/kiosk/` and `docker/kiosk-cage-only/` go, with Firefox's policies, `kiosk.cfg`,
  `remote.sh`, the `loginctl` stand-in and `xdpw.conf` (the last two move to the host if §3.4 needs them).
- The app **publishes its port to the clock's own loopback only**: `127.0.0.1:8080:8080` (my call). Nothing outside the
  clock can reach it.
- The app's health check (heartbeats and favicon) stays.

## 7. Timelines

**A deploy, `app-stopped` mode:** the helper appears → the banner shows on the clock, steps start collecting → the old
app stops → the clock page loses its connection; 3 s later the watcher hides the clock window and the standby shows
(updating) → the new app starts and its steps show → it serves → the clock page reconnects and reloads (or, after a
failed load, the clock browser restarts on its start page and loads the clock), hidden → the connection opens → the
watcher shows the clock. The banner file is deleted. No terminal switch, no black.

**`deploy-start` mode:** the same, except the watcher hides the clock as soon as the helper appears.

**Boot:** HP logo → (GRUB hidden) → Plymouth splash → sway (1 black frame measured) → sway's background, the splash's
logo in the same place → standby (starting) with steps → clock.

**Clock browser crash:** its window closes, the standby shows at once → systemd restarts it → start page → the clock
loads, hidden → live → shown.

**sway crash:** a blank console for the seconds until systemd restarts sway → logo → standby → clock. RustDesk's
picture drops for those seconds.

## 8. Hiding Linux

- **GRUB:** `GRUB_TIMEOUT_STYLE=hidden`, `GRUB_TIMEOUT=1`; holding Esc during that second shows the menu. The RAID's
  second EFI partition gets the same config (`sync-esp`). Built.
- **Kernel command line:** `quiet splash loglevel=3`, and `kernel.printk = 3 4 1 3` by sysctl. No `i915.fastboot`:
  kernel 6.9 made fastboot the default on every Intel GPU and dropped the option (built). The mode change from the
  firmware's 800×600 to the panel's mode at driver load is likely a monitor blink; trying GRUB at the panel's mode is a
  later experiment (my call).
- **Plymouth** with the clock's theme, every image of Debian's spinner theme copied in (two-step won't show without its
  prompt images and Plymouth falls back to text; built). The logo moves to the screen's centre (my call), and **sway's
  background is the same logo centred** (`output * bg <logo> center #000000`), so splash → sway changes only the spinner.
  Measured: sway's start is 1 black frame, then the logo until the standby page draws (about 0.5 s warm, 2 s on a cold
  boot).
- **Terminal 1 blank**, `getty@tty1` masked, **console auto-logout** after 5 minutes idle on terminals 2–6 (owner): built,
  unchanged.

## 9. Repository, build, install, update, rollout

- **`timeclock-standby`**: `watcher/`, `status/` (adds `clock-start.html`), `display/` (sway's config, the clock browser's
  wrapper, the check-in and splash hand-off script, the background logo), `systemd/` (display, standby browser, clock
  browser, remote-access install, watcher, status, terminal 1), `plymouth/`. Releases by tag, built by its workflow.
- **This repository**: the submodule; §6.4's compose change and the kiosk directories removed; both images; the
  master spec's kiosk sections (R13) updated to match.
- **Host packages** (both images' preseed, and `install.sh` for clocks installed before): `sway`, `swaybg`, `cog`,
  `pipewire`, `wireplumber`, `xdg-desktop-portal`, `xdg-desktop-portal-wlr`, `dbus-user-session`, `plymouth`,
  `plymouth-themes`, `librsvg2-bin`, `python3`; `cage` and `firefox-esr` no longer (`seatd` only if §3.4's session
  falls back to it). The ISO bakes the latest release and the install fetches it again if it can (built).
- **Updating a clock:** replace a file over SSH and restart its unit (owner).
- **Rollout to a running clock** (main-office, timeclock-dev): the host display and the compose without the kiosk go
  **back to back**. The new compose alone leaves no browser on the screen; the host display alone shows the standby
  until the app publishes its port. Merging to `main` auto-deploys main-office (watch paths `docker/**`), so its host
  display is installed first, at a quiet time, with the owner's go-ahead (my call on the order).

## 10. Verify first, on main-warehouse

main-warehouse is the playground; **main-office is live** and must never be left broken (owner). Measured already
(spike, 2026-10-29): a container's cog on the host's sway; app ids and the scratchpad rule; show, hide, stop and restart
with no black; sway's start with the logo background; sway as root; cog against Firefox. In this order, riskiest first:

1. **RustDesk on the host** with the real package: sway in a login session on terminal 7, RustDesk finding it
   unattended, capturing the standby and the clock, input working, its ID kept from the settings folder, remote
   Ctrl+Alt+F*n* blocked, connected through a deploy.
2. **The clock browser on the host** through `127.0.0.1:8080`: the connection drops within seconds when the app stops
   (Docker's port publishing in between); the page reconnects or reloads when the new app serves, and the reload
   happens before the clock is shown, not on screen; a failed load restarts the browser on its start page; the
   clock-live check sees only the browser's held connection, never the probe's or a refused reconnect's.
3. **Boot**: Plymouth → sway with no text, the logo in the same place, Ctrl+Alt+F*n* from sway and back.
4. **Touch**: goes to the visible window; pinch zoom, swipe and long-press in cog; the number pad and a keyboard
   replugged.
5. A Coolify deploy in each flip mode with the new compose.

Then the whole: a reboot, a deploy in each mode, the clock browser killed, sway killed, the status app killed
mid-deploy, Ctrl+Alt+F2 with the 2-minute return, a console left logged in, the banner's position on every screen, and
cog's memory over a few days.
