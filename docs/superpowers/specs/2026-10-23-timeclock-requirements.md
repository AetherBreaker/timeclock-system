# Timeclock requirements (draft)

Short requirements list, to be expanded into a full design spec once the main decisions are settled.

- **Decided:** things you've said or approved.
- **Proposed:** my recommendations. None are approved yet.
- **Open:** not yet discussed or decided.

## Purpose

1. Track employee clock-in and clock-out at our office.
2. Clock-in uses fingerprints (boss's requirement).

## Decided

1. Fingerprint reader drivers and SDK cost nothing: no subscriptions, no license fees.
2. Readers: the DigitalPersona U.are.U 4500s already on the shelf.
3. The time clock runs on Linux.
4. Written in Python, targeting 3.14 (per `pyproject.toml`). Custom Rust bindings are acceptable if needed.
5. uvloop as the event loop. Blocking work on threads is fine.
6. A touchscreen at the clock, where employees can see the hours they've accumulated this week and other data.
7. The touchscreen must be cheap.
8. Hardware: probably an HP EliteDesk 800 G3 or similar, wiped for Linux.
9. Browser: Firefox.
10. The device is a Docker host that our Coolify server deploys to. The whole stack, display included, runs in containers. Startup and maintenance are controlled from the repo, with no manual changes on the device.
11. Fingerprints through libfprint (open source; it has supported the 4500 for years), called from Python via PyGObject.
    1. All libfprint calls on one dedicated thread, using its blocking (`_sync`) calls. Nothing else in the process runs GLib's main loop.
    2. PyGObject is installed from PyPI for Python 3.14, not from a distro package, which is built for the distro's own Python.
12. UI: NiceGUI served locally, shown full-screen in Firefox kiosk mode.
13. Display: cage runs Firefox as the only app on screen, in a privileged container started with `seatd-launch`.
14. Deployment: Coolify's Docker Compose type with two services, app and kiosk.
    1. Both on host networking, both with `restart: unless-stopped`.
    2. The kiosk waits for the app's health check.
    3. Coolify's proxy set to None on this server.
15. Store fingerprint templates only, never images.
16. Use aeth-ext's systems for logging, shutdown handling, crash alerts, and similar.
17. The project is managed by aeth-devkit.
18. Docker is managed by devkit-container.
19. If a dependency has a shortcoming that blocks us, modifying that dependency is an option. devkit-container is the most likely to need changes.

## Proposed

1. Host OS: Debian 13, no desktop.
2. A fallback for people whose fingerprints read poorly: a PIN or a manager override.
3. Store timestamps in UTC; show them in local time.
4. Typed input (a PIN, for example) comes from a number pad, either built into the page or a physical one. The system provides no on-screen keyboard.
5. Touchscreen: Waveshare 10.1" HDMI LCD (B) with case, about $111.

## Open

1. QuickBooks: Online or Desktop, which payroll provider or plan, and whether and how the clock sends hours to it.
2. Employees checking hours from their phones: our own page, QuickBooks Workforce, or only at the clock.
3. Where punch records live: on the device, or on a server; backups.
4. Where the Coolify server is, and how it reaches the device (LAN, Cloudflare Tunnel, Tailscale).
5. Which state(s) employees are in, for biometric privacy laws (consent, retention, deletion).
6. Admin tasks (enrolling employees, correcting punches, running reports): who does them, and where (at the clock or a separate page).
7. What "other data" the clock screen shows besides weekly hours.
8. Breaks and lunches, overtime, pay periods, rounding rules.
9. How many employees, and how many clocks.
10. Reports and exports needed.
