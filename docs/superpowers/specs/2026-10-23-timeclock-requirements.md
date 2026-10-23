# Timeclock requirements (draft)

Short requirements list, to be expanded into a full design spec once the main decisions are settled.

- **Decided:** things you've said.
- **Proposed:** my recommendations. None are approved yet.
- **Open:** not yet discussed or decided.

## Purpose

- Track employee clock-in and clock-out at our office.
- Clock-in uses fingerprints (boss's requirement).

## Decided

- Fingerprint reader drivers and SDK cost nothing: no subscriptions, no license fees.
- Readers: the DigitalPersona U.are.U 4500s already on the shelf.
- The time clock runs on Linux.
- Written in Python, targeting 3.14 (per `pyproject.toml`). Custom Rust bindings are acceptable if needed.
- uvloop as the event loop. Blocking work on threads is fine.
- A touchscreen at the clock, where employees can see the hours they've accumulated this week and other data.
- The touchscreen must be cheap.
- Hardware: probably an HP EliteDesk 800 G3 or similar, wiped for Linux.
- Browser: Firefox.
- The device is a Docker host that our Coolify server deploys to. The whole stack, display included, runs in containers. Startup and maintenance are controlled from the repo, with no manual changes on the device.

## Proposed

- Fingerprints through libfprint (open source; the stock Debian package supports the 4500), called from Python via PyGObject.
  - All libfprint calls on one dedicated thread, using its blocking (`_sync`) calls. Nothing else in the process runs GLib's main loop.
  - Debian 13's `python3-gi` is built for Python 3.13, so PyGObject is installed from PyPI for 3.14.
- UI: NiceGUI served locally, shown full-screen in Firefox kiosk mode.
- Display: cage runs Firefox as the only app on screen, in a privileged container started with `seatd-launch`.
- Deployment: Coolify's Docker Compose type with two services, app and kiosk.
  - Both on host networking, both with `restart: unless-stopped`.
  - The kiosk waits for the app's health check.
  - Coolify's proxy set to None on this server.
- Host OS: Debian 13, no desktop.
- Store fingerprint templates only, never images.
- A fallback for people whose fingerprints read poorly: a PIN or a manager override.
- Store timestamps in UTC; show them in local time.
- Any typed input uses a number pad built into the page; there's no on-screen keyboard from the system.
- Touchscreen: Waveshare 10.1" HDMI LCD (B) with case, about $111.

## Open

- QuickBooks: Online or Desktop, which payroll provider or plan, and whether and how the clock sends hours to it.
- Employees checking hours from their phones: our own page, QuickBooks Workforce, or only at the clock.
- Where punch records live: on the device, or on a server; backups.
- Where the Coolify server is, and how it reaches the device (LAN, Cloudflare Tunnel, Tailscale).
- Which state(s) employees are in, for biometric privacy laws (consent, retention, deletion).
- Admin tasks (enrolling employees, correcting punches, running reports): who does them, and where (at the clock or a separate page).
- What "other data" the clock screen shows besides weekly hours.
- Breaks and lunches, overtime, pay periods, rounding rules.
- How many employees, and how many clocks.
- Reports and exports needed.
