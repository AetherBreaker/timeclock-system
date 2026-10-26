# Timeclock requirements (draft)

Short requirements list, to be expanded into a full design spec once the main decisions are settled.

- **Decided:** things you've said or approved.
- **Proposed:** my recommendations. None are approved yet.
- **Open:** not yet discussed or decided.
- **Deferred:** set aside for later; not part of this round of decisions.

## Purpose

1. Track employee clock-in and clock-out at our office.
2. Clock-in uses fingerprints (boss's requirement).

## Decided

1. Fingerprint reader drivers and SDK cost nothing: no subscriptions, no license fees.
2. Readers: the DigitalPersona U.are.U 4500s already on the shelf.
3. The time clock runs on Linux.
    1. Host OS: Debian 13, no desktop.
    2. The install image doesn't hardcode a hostname; each device's is set at install (`timeclock-dev` is only the test device's name).
4. Written in Python, targeting 3.14 (per `pyproject.toml`). Custom Rust bindings are acceptable if needed.
5. uvloop as the event loop. Blocking work on threads is fine.
6. A touchscreen at the clock, where employees can see the hours they've accumulated this week and other data.
7. The touchscreen must be cheap.
    1. The design stays touchscreen-agnostic (any USB multitouch screen) until one turns up that doesn't work as is. No model is specified.
8. Hardware: probably an HP EliteDesk 800 G3 or similar, wiped for Linux.
9. Browser: Firefox.
10. The device is a Docker host that our Coolify server deploys to. The whole stack, display included, runs in containers. Startup and maintenance are controlled from the repo, with no manual changes on the device.
11. Fingerprints through libfprint (open source; it has supported the 4500 for years), called from Python via PyGObject.
    1. All libfprint calls on one dedicated thread, using its blocking (`_sync`) calls. Nothing else in the process runs GLib's main loop.
    2. PyGObject is installed from PyPI for Python 3.14, not from a distro package, which is built for the distro's own Python.
12. UI: NiceGUI served locally, shown full-screen in Firefox kiosk mode.
13. Display: cage runs Firefox as the only app on screen, in a privileged container started with `seatd-launch`.
    1. The kiosk is a compose service not listed in `[tool.docker].services`, with its own Dockerfile, so aeth_devkit and devkit-container leave it alone (the one exception to Decided 18). Only the kiosk is privileged; the app container stays minimal.
    2. Lockdown: Firefox policies baked into the kiosk image turn off pinch zoom, swipe-back and the session-restore prompt; the page turns off text selection and the long-press (context) menu.
14. Deployment: Coolify's Docker Compose type with two services, app and kiosk.
    1. Both with `restart: unless-stopped`. Neither uses host networking: the app is on the `coolify` network as the template requires (and its WireGuard interface stays inside its own namespace); the kiosk joins the same network and opens the app by service name. No ports published to the host.
    2. The kiosk runs its own `systemd-udevd` (default rules only) plus `udevadm trigger` before cage, so a replugged touchscreen is detected without host networking. Confirmed on the EliteDesk (2026-10-26): kernel device events reach the kiosk's namespace, and a replugged touchscreen works again, including on a different USB port.
    3. The app does not detect a replugged fingerprint reader: it exits when it loses the reader, and the restart picks it up. The same udevd approach can be added later if wanted.
    4. The kiosk waits for the app's health check.
    5. Coolify's proxy set to None on this server.
    6. Each machine's compose environment sets `HEARTBEAT_SLUG=timeclock-system-${INSTANCE_ID:?}`, so each clock has its own healthchecks.io check. This goes through compose rather than aeth_ext so devkit-container's supervisor, which reads the slug as-is, pings the same check.
    7. Reader access: the app container mounts `/dev/bus/usb` with device rule `c 189:* rmw`. A `[tool.docker].startup_scripts` entry (runs as root before the app) finds the reader in `/sys` by USB ID `05ba:000a` and chowns its device file to user 999. If the reader isn't present, the script exits nonzero and startup fails (fingerprints are mandatory); Docker's restart retries until it's plugged in.
15. Store fingerprint templates only, never images. Serialized templates are always held in pydantic `SecretBytes`, so tracebacks and alerts show them masked.
16. Use aeth-ext's systems for logging, shutdown handling, crash alerts, and similar. Blocking changes needed in aeth_ext:
    1. Timeouts on the email and Pushover alert sends (aeth_ext todo #16), plus a quick no-wait route check that skips sending when there's no network route. The check only if it stays a few lines.
    2. An `INSTANCE_ID` environment variable that, when set, is appended to the project name with a hyphen (`timeclock-system-3`, `timeclock-system-front-desk`). Intended as an integer, but text is allowed; restricted to letters, digits, `-` and `_` (the log server uses the name as a folder), and rejected at startup otherwise. Used for the log server program name and alert subjects.
    3. An opt-out from requiring the log server at startup (aeth_ext todo #14). Requiring it stays the default (e.g. `require_log_server=True`), which suits deployments where a technician is present. Opting out means no `RuntimeError` when the startup probe fails: records buffer and are delivered once the server is reachable. timeclock-system opts out. Behavior after startup is unchanged.
    4. Socket logging never blocks the code that logs (aeth_ext todo #20). `HandshakeSocketHandler.emit` only assigns the ID and records history, then wakes a background sender thread that owns the socket, handshake, replay and sending. Proposed approach: the history buffer serves as the queue (send everything after `last_sent_id`), so no separate bounded queue. Shutdown gets a bounded flush; `connect_and_verify()` keeps working synchronously at startup.
    5. An opt-out from shutting down when the log server rejects the client's logging config (aeth_ext todo #14). Shutting down stays the default. Opting out means the rejection sends one alert (not one per reconnect; cleared after the next successful handshake), the connection is dropped, records stay in history, and the app keeps running. timeclock-system opts out.
    6. Global crash hooks: aeth_ext installs `sys.excepthook`, `threading.excepthook`, and an asyncio loop exception handler. An uncaught exception anywhere is fatal: logged through aeth_ext's logging, alerted, and a fatal shutdown started. No chaining to Python's default hook (nothing extra to stderr; `docker logs` is deliberately unused). Safeguards: `KeyboardInterrupt` passes through (aeth_ext's exit nudge); exceptions a decorator already reported are skipped; quiet once shutdown has started. The loop handler must be set on the running loop, since uvicorn creates the loop itself. On by default for every aeth_ext consumer, released as a major version (10.0.0).
    7. The central log server's deployment becomes a WireGuard spoke (`[tool.docker].wireguard = true` in aeth_ext's own project, compose re-rendered by setup-project). Configuration only: the server already listens on `0.0.0.0`.
17. The project is managed by aeth-devkit.
18. Docker is managed by devkit-container.
    1. Non-blocking: better support for running permanently with `WG_TOLERATE_DISCONNECTED=1` (code or documentation changes; to be determined). The flag works today.
    2. Blocking: the Dockerfile template moves both stages to `ghcr.io/astral-sh/uv:python3.14-trixie-slim` (Debian 13, GLib 2.84, needed by PyGObject ≥ 3.51), for every project; no per-project base image setting. A new `builder-setup` window in the builder stage, after the `git` install and before the manifest clone and first `uv sync`, for system packages dependencies need to compile. timeclock-system puts PyGObject/pycairo build deps there and its runtime libraries (libfprint, typelibs, cairo) in the existing `final` window.
    3. Blocking: multiple peers per spoke (the hub plus the other clocks, from the hub's peer table), a fixed listen port, and applying discovered LAN endpoints from a shared file with handshake confirmation and revert (Decided 28). LAN sharing is in the first version.
19. If a dependency has a shortcoming that blocks us, modifying that dependency is an option. devkit-container is the most likely to need changes.
20. Crash alerts: timeclock-system saves every crash alert to a file, rotating across more than two files, as evidence of the crash. Saved alerts aren't resent (the clock can't tell which ones went out). A stopgap until aeth_ext gets hold-and-resend (non-blocking, with its in-memory send record, aeth_ext todo #21).
21. aeth_ext owns SIGINT/SIGTERM. timeclock-system disables uvicorn's signal capture and registers an aeth_ext shutdown callback that sets the uvicorn server's `should_exit`. Handled in the app; no aeth_ext change.
22. Logs reach the central log server over WireGuard through the existing hub. Each clock and the log server are spokes; the hub's firewall rules connect only the clocks to the log server's port. Both run with `WG_TOLERATE_DISCONNECTED=1`, so neither depends on the tunnel to start or keep running.
23. Fingerprints are the enforced way to clock in. PIN clock-in is supported, but requires a manager override.
    1. A PIN login is a username and a password, both numeric for now. PIN passwords are stored as bcrypt hashes (pgcrypto, set in SQL with `crypt('1234', gen_salt('bf'))`); clocks verify against the hash, never holding plain PINs.
    2. PIN entry for an employee with no registered fingerprint prompts them to register one (this is how enrollment starts). A manager override button skips registration and allows that single clock in/out.
    3. PIN entry for an employee with a registered fingerprint still asks for the fingerprint before letting them through. A manager override button allows that single clock in/out without it.
    4. Registering a fingerprint needs only the employee's PIN login, no override. Someone registering under another person's login would be noticed quickly, since the real employee couldn't clock in on their own.
    5. A manager override is authorized by the manager's fingerprint or the manager's PIN login (a manager's PIN doesn't also require their fingerprint). Every override is logged to an overrides table in PostgreSQL; an override authorized by PIN alone sets a flag on its record, so those are visible. Override records sync like punches (UUID, stored locally while offline), and the clocks' restricted role can insert into that table.
    6. Managers are marked by an `is_manager` boolean on the employee row (default false), which reaches the clocks with the rest of the employee data, so overrides work offline.
24. Timestamps are stored in UTC and converted to local time only for display and reports (daylight-saving safe).
25. Typed input (PINs, for example) uses a number pad drawn on the page. The page also accepts keyboard input, so a physical keypad can be added later without changes.
26. Employees can use more than one clock (e.g. in at one, out at another), so punch records live in a shared external database, not on each clock: PostgreSQL, hosted by our Coolify server on the VPS (not a third-party host).
    1. Clocks reach PostgreSQL through a relay: timeclock-backend (a devkit-managed WireGuard spoke) forwards raw TCP from the tunnel to PostgreSQL. Clocks speak the Postgres protocol directly.
    2. Clocks connect with a restricted role: `INSERT` on punches and `SELECT` on what they display; no `UPDATE`, `DELETE` or schema rights.
    3. A clock-out without a matching clock-in is rejected. It's a guard checked at the clock from what that clock knows, not a perfect check: the database accepts whatever syncs, and mix-ups from offline punches are tolerated (payroll is checked by hand).
    4. Each clock keeps a local SQLite copy of the current week's punches, plus any unsynced punches beyond that, and checks the guard and computes totals from it. It stays current through PostgreSQL `LISTEN`/`NOTIFY`: a trigger on the punches table notifies on insert, and each clock listens over its connection through the relay. On every (re)connect the clock re-fetches the week to catch up on missed notifications. The local copy rolls over at week end.
    5. PostgreSQL runs as an unlisted service in timeclock-backend's compose (official `postgres` image, on a private network shared only with the backend app), not as Coolify's built-in database resource, so its configuration lives in the repository. The backend runs the schema's migrations on startup (tables, triggers, the clocks' restricted role).
27. A clock that can't reach the database still accepts punches: it stores them locally (on its persisted volume) and syncs them once the connection is back.
    1. Known issue, not coded around: while offline, a clock's weekly totals only include its own punches and what it last synced, so punches made at another clock in the meantime are missing.
    2. Fingerprint registration also works without the database: the new fingerprint is stored locally (with a UUID) and synced once the connection is back, like a punch. The clocks' restricted role can insert registered fingerprints.
    3. An employee can have several registered prints; registration enrolls one, so adding another finger later is UI work only. When a print stops matching, the owner removes it, and the employee's next PIN entry prompts a new registration.
    4. Fingerprint rows aren't append-only: the owner deletes one in SQL, the delete notifies the clocks, and a clock that was offline re-fetches the full list on reconnect. (A delayed re-send could only bring back a print the database hadn't yet confirmed, and deletions come much later.)
28. Clocks on the same LAN share punches and newly registered fingerprints directly over WireGuard, so each clock sees the others' punches and enrollments even while the hub or internet is down.
    1. On each punch or fingerprint registration, a clock sends it to every other clock, best effort. Receivers insert into their local SQLite copy, ignoring duplicate UUIDs. No catch-up between clocks; only the recording clock syncs a record to PostgreSQL.
    2. The hub's peer table lists every clock (public key, `/32` tunnel address) without LAN endpoints. Each clock listens on a fixed WireGuard port published on its host (e.g. `51821/udp`).
    3. LAN addresses can't be reserved, so a small discovery service (an unlisted compose service with host networking) broadcasts `key K at <host LAN IP>:<port>` over UDP every ~30 s, listens for the others, ignores keys not in the hub's peer table, and writes the key → address map to a file shared with the app container.
    4. devkit-container's supervisor applies a changed address with `wg set ... endpoint`, then confirms it by a fresh handshake within a few seconds (`latest-handshakes`); with no handshake it reverts to the previous address. WireGuard's endpoint roaming covers address changes between announcements.
    5. Which rows of the hub's peer table are a clock's LAN peers comes from a new optional per-row field, `lan` (e.g. `lan = "office"`): a spoke peers directly with every other row sharing its value. Spokes on older devkit-container versions ignore the field. Only clocks get one, since a direct peer's `/32` would cut that address's route through the hub.
29. The Coolify server (on the VPS) reaches each clock's host through a persistent reverse SSH tunnel, pos-tunnel's pattern without its relay. The VPS end is [coolify-tunnel](https://github.com/AetherBreaker/coolify-tunnel), a forwarding-only `sshd` container in its own repository and Coolify application, shared with other projects' devices. It's published on port 2223, with its host key from a Coolify secret; its `authorized_keys` is the registry of devices and their ports. Each host runs a systemd service, `ssh -N -R 0.0.0.0:<port>:localhost:22`, with its own key and automatic reconnect; its key may only open its own port (`permitlisten`, no shell, no local forwards). Each device has a fixed port (22001, 22002, ...). Coolify adds each clock as a server at `coolify-tunnel:<port>` over the `coolify` network. Nothing is configured on the VPS host. The systemd unit and key are part of each host's one-time install.
30. Admin tasks (adding employees, correcting punches) are done by the owner directly in PostgreSQL with an SQL editor for now; a streamlined interface may come later. Any change to employee data is pushed to the clocks (`NOTIFY` on the employees table, like punches), and each clock keeps a local copy of the employee list.
    1. Employee rows are edited in place; only punches and overrides are append-only. Everything else refers to an employee by an internal ID, never by name, so a name change shows on all their records. Employee IDs are auto-incrementing integers (only the owner creates employees). Punches store only the ID; a `punch_log` view joins in the current name, so old punches show the name as it is now, and CSV exports read from it.
    2. The owner's SQL editor connects through SSH to the VPS, so the WireGuard hub needs no workstation peer.
31. After a punch, the clock screen confirms it and shows the employee's hours this week; nothing more for now. The screen is designed so more information can be added later.
    1. Hours worked subtract lunch time and count break time, by default. Two switches in `config.settings` toggle the subtraction of each, lunches and breaks, separately.
32. Pydantic is used heavily throughout, for validation and as the shape of stored and exchanged data.
33. The week runs Monday to Sunday (weekly totals, and the week of punches each clock keeps).
34. Employees belong to groups. Rules (e.g. break requirements: how many breaks, how long) are set on groups and can be overridden for specific employees. Each rule is a row in a SQL table. There are no rules at launch; the goal is that rules can be created in SQL without changing the code.
    1. A rule row has a `kind` column naming the rule type and its parameters as JSON, each kind validated by a pydantic model (a mistyped row fails validation rather than being ignored). New rows and changed values of a known kind need no code change; a new kind of rule needs code once.
    2. Each rule row specifies what the clock does when it's broken: refuse the punch, warn but accept, or accept silently (left for payroll review). Changing that is a row edit.
35. Breaks and lunches are their own punch types (start and end of each), alongside clock in and clock out.
36. Scale: about 2 clocks and under 40 employees at launch, but fingerprint matching is planned for growth (fast for snappiness and scalability).
    1. Version 1 (proof of concept) matches with libfprint's own identify.
    2. Version 2 replaces it with a parallel matcher built from a fork of `fprint-bozorth3` (pure-Rust bozorth3, with `fprint-fp3` for reading libfprint's stored templates), reviewed for safety and pinned. Its scores are compared against libfprint's on real 4500 scans before it's trusted.
    3. Matching sits behind a single function (e.g. `identify(scan) -> employee | None`) that nothing else bypasses, and fingerprints are stored in libfprint's serialized form, so version 2's matcher is a drop-in with no re-enrollment.
    4. Version 1 keeps all stored fingerprints deserialized in memory (refreshed on the employee push) and orders them by most recent punch at this clock, since libfprint's identify stops at the first match.
37. timeclock-backend (the server side, deployed by Coolify on the VPS) is a git submodule of this repository at `backend/`. In version 1 it runs the database relay (26.1), PostgreSQL (26.5) and a simple scheduler, shaped like ScheduledInvoiceProcessor's and ScheduledReportAggregator's (`scheduler_config.py`: an apscheduler 3.x `AsyncIOScheduler` subclass with aeth_ext fatal-error handling and quiet-job log filtering; jobs on `CronTrigger`s in the configured time zone).
    1. This repository is the client (the clock). timeclock-backend is a separate public repository, included here as a submodule.
    2. The data models shared by the clock and the backend (punches, employees, rules, overrides) are a shared schema package in its own repository, included here as a submodule; both the clock and the backend depend on it.
    3. The schema repository also holds the SQL migrations, next to the models they correspond to. The backend runs them on startup.
    4. Every repository in the project is public unless something forces otherwise.
    5. The schema package is published to SFTPyPI with the standard devkit release task (`poe release`); the clock and the backend depend on it by version. The SQL migrations ship inside the package, and the backend runs them from there. The submodule here is for editing the schema next to the clock; builds never read it, and the backend has no schema submodule.
38. Punches are an append-only log of events (clock in/out, break start/end, lunch start/end), each with a UUID and the recording clock's `INSTANCE_ID`; shifts are worked out when reading. Syncing inserts with `ON CONFLICT DO NOTHING` on the UUID, so re-sends never duplicate.
    1. Rows are never updated or deleted. Edits and deletions are themselves appended events that reference the UUID they correct or void, and every read applies them. This keeps the original values, and a delayed re-send can't resurrect or overwrite anything.
39. The UI carries the company's branding: its colors and logo. The logo files are `SFT-Logo-June-2022-color.svg` and `SFT-Logo-June-2022-black.svg`, kept in the project folder but out of git (ignored by `*.svg`) until it's decided whether they can be public (the color logo uses yellow `#F9E60D`, red `#ED1A2C`, near-black `#161514` and white).
    1. The logo files aren't part of the image: the owner puts them in a persisted data directory on each device, and the GUI loads them from there at runtime, so they survive every deploy. They may move into the repository later.
40. The fingerprint reader stays ready (waiting for a finger) during business hours. libfprint's generic overheating model, which would make an always-waiting clock refuse scans about two-thirds of the time, is turned off for the 4500: its `uru4000` driver sets `temp_hot_seconds = -1`, the opt-out the match-on-chip drivers use. The model isn't based on the 4500 (a 2021 precaution "for bad/old devices"), and HID rates the reader's waiting state (blue glow, 120 mA) with no time limit. This means building a patched libfprint into the image instead of using Debian's package.
    1. Outside business hours the clock stops waiting, so the reader sleeps (USB autosuspend) and its LED rests. Business hours are configurable in the database, in each device's local time.
    2. Outside business hours, a tap on the screen wakes the reader: it opens and waits about 30 s for a finger, then closes so the reader sleeps again. Anything else that needs a scan (the PIN path's fingerprint step, a manager override, registration) wakes it the same way. A scan in progress when business hours end finishes before the reader closes.
    3. One schedule shared by every clock: a `config.business_hours` table (41.3), one row per opening window (ID, weekday, open time, close time), nothing unique per weekday; a weekday with no rows is closed. Any insert, update or delete notifies the clocks, which re-fetch the whole table (and on every reconnect) and keep it locally. Per-clock hours may come later, so a single lookup in the clock picks the rows that apply; adding them is then a nullable `instance_id` column, with that lookup preferring the clock's own rows over the shared ones.
    4. The patch is a file in this repository, applied during the app's image build to libfprint's upstream 1.94.100 release (pinned, checksum-verified; it carries the `uru4000` buffer-overflow fixes), building only the `uru4000` driver; the final stage gets the library and the `FPrint-2.0` typelib. libfprint therefore compiles on every deploy. If that makes deploys too slow, the fallback is a fork repository that publishes prebuilt binaries, which the image downloads instead.
41. Shifts still open at the week rollover stay in each clock's local copy (their punches back to the clock-in) until the closing punch arrives, through the notification or the reconnect catch-up; the catch-up fetches the current week plus everything since the oldest open shift the clock holds.
    1. A daily job in the backend's scheduler runs at `auto_clock_out_run_time` and closes every open shift whose last punch is before `auto_clock_out_stamp_time` on the run date, appending a clock-out stamped at that time and flagged automatic. Shifts whose last punch is later wait for the next day's run. The clocks receive it like any punch.
    2. Within one shift (from a clock-in to the next clock-in), a real clock-out overrides an automatic one, in either order, so a clock-out synced late from an offline clock wins.
    3. Configuration tables live in a Postgres `config` schema: `config.business_hours` (40.3) and a one-row `config.settings` holding `auto_clock_out_enabled` (on), `auto_clock_out_run_time` and `auto_clock_out_stamp_time`. Those times, and business hours' open and close times, are Postgres `time` values (no date, no zone; `datetime.time` in pydantic). A stamp time later than the run time fails validation: the job skips that run and alerts.
    4. Local time everywhere comes from aeth_ext's `settings.tz` (the `TZ` environment variable), which compose sets explicitly to `America/New_York` on the backend and the clocks; never the host's time zone (the VPS runs UTC).
    5. Weekly totals split a shift that crosses Monday 00:00 at midnight.
42. The clock hosts install Debian security updates automatically (`unattended-upgrades`, security only), rebooting at 03:00 when an update needs it. Set in the install image's preseed, so it applies from the next image rebuild. A later version should make settings like these configurable from the database.
43. Images are built on each device (Coolify's default). Revisit together with the libfprint fallback (40.4) if deploys get slow.
44. The clock's `log_history_retention_days` is 14. An outage never lasts longer than a week: the offices have backup generators and Starlink as backup internet.
45. Punching: the employee scans first; the screen then shows only the punches valid from their current state (e.g. clocked in: clock out, start break, start lunch), and a tap records one.
    1. Clock-in is never automatic: it always takes a tap on "Clock in", so the same screen can later show the employee's scheduled time (schedules aren't in version 1; the screen leaves room for them).
    2. Buddy-punch guard: a punch button tapped more than 30 s after the scan asks for a new scan, which must match the same employee; a scan matching someone else opens their menu instead, with no punch. This covers every punch button.
    3. An employee who got in by PIN plus manager override needs a fresh manager override for every punch, including re-authenticating after 30 s, unless they provide a fingerprint.
    4. A new scan while a menu is open switches to that person's menu. A menu nobody touches closes after 2 minutes.

## Proposed

None.

## Open

None in this doc. The spec handoff lists the remaining open questions.

## Deferred

1. QuickBooks: Online or Desktop, which payroll provider or plan, and whether and how the clock sends hours to it.
2. Employees checking hours from their phones: our own page, QuickBooks Workforce, or only at the clock.
3. Reports and exports: decided once the foundation is built and payroll's preferred shape is known. Until then, payroll uses manual SQL exports to CSV, outside the spec.
4. Database backups (e.g. off-VPS storage): not a requirement for version 1; slotted in later.
