# Timeclock system design (2026-10-26)

The design for version 1 of the fingerprint time clock: the clock app (this repository), timeclock-backend,
the shared schema package, the patched libfprint, and the changes this needs in aeth_ext and
devkit-container. Implementation plans are written straight from this document, including the plans for the
dependency repositories, which get no spec of their own.

- **R*n*** cites item *n* of the Decided list in [the requirements doc](2026-10-23-timeclock-requirements.md),
  each approved by the owner. Where this spec restates one, the requirements doc wins on any conflict.
- **(spec call)** marks a decision made while writing this spec, not by the owner. A builder may revisit a
  spec call when the code argues for it; anything else here is settled.
- Plan docs, tests and the spec handoff are not sources of intent (`AGENTS.md`).

## 1. Intent

The office needs to track when employees clock in and out. The boss requires fingerprints, so a clock that
lets anyone punch for anyone (a PIN pad, a badge) doesn't meet the need. The clocks are cheap PCs with a
touchscreen and the DigitalPersona U.are.U 4500 readers already on the shelf, deployed and maintained
entirely from the repository through Coolify, with nothing done by hand on a device.

Punches go into one PostgreSQL database on the company's VPS, so an employee can clock in at one clock and
out at another. A clock keeps working when the internet or the VPS is down: it records punches locally and
syncs them later. Payroll is checked by hand, so the system favors never losing a punch over preventing
every mix-up.

There's a deadline. Foundations get built properly now, while the shape is easy to change; extras wait.
"Anything that needs more than a few lines of code and a function or two isn't worth the time right now"
applies to everything not listed in §2.

## 2. Goals, non-goals, anti-goals

**Version 1 does:**

- Fingerprint punching: clock in/out, break start/end, lunch start/end (R35), with the screen offering only
  the punches valid from the employee's current state (R45).
- PIN login with a manager override (R23), fingerprint registration from the clock (R23.2, R23.4).
- The weekly-hours display after a punch (R31).
- Offline punching and registration with later sync (R27); clock-to-clock sharing on the LAN (R28).
- The daily automatic clock-out of forgotten shifts (R41).
- Business hours, with the reader asleep outside them and tap-to-wake (R40).
- The rules framework with no rule kinds yet (R34).
- Deployment through Coolify, crash alerts, central logging, healthchecks.io monitoring.

**Not in version 1:** reports and exports (payroll exports CSV by hand with SQL; Deferred 3), QuickBooks
(Deferred 1), an employee phone page (Deferred 2), database backups (Deferred 4), an admin interface (the
owner uses an SQL editor, R30), employee schedules (R45.1), per-clock business hours (R40.3), the parallel
version 2 matcher (R36.2), any rule kind (R34), host settings configured from the database (R42).

**Anti-goals.** Version 1 fails, even if it works, if:

- a punch is lost: to a restart, an outage, a crash, or a re-send that overwrites or resurrects a record;
- the clock depends on the tunnel, the VPS or the log server to start or keep running;
- a fingerprint image or a plain PIN is ever stored, logged or alerted;
- anything retries a failed scan in a tight loop (the spike spun 3.3 million times, §6.2);
- the reader silently stops working while the screen still looks ready (§6.2);
- deploying or reinstalling a clock takes manual changes on the device beyond its one-time install (R10).

## 3. System overview

```text
 Office LAN (one or more clocks)                         VPS (Coolify)
 ┌──────────────── clock host (Debian 13) ─────────┐     ┌──────────────────────────────────────────┐
 │ kiosk   cage + Firefox ── http://app:8080 ──┐   │     │ Coolify ── coolify network ──┐           │
 │ app     NiceGUI, scanner thread, SQLite  ◄──┘   │     │ coolify-tunnel (sshd, :2223) ◄─┼── ssh -R  │
 │         WireGuard spoke (fixed port, R28.2) ════╪═════╪═► WireGuard hub                │           │
 │ discovery  host network, LAN broadcast (R28.3)  │     │   ├─ central log server (spoke, :9020)     │
 │ systemd: ssh -N -R to coolify-tunnel (R29) ─────┼─────┼─► ├─ timeclock-backend (spoke)             │
 └─────────────────────────────────────────────────┘     │   │    relay :5432 ──► postgres (private)  │
        ▲ direct WireGuard between clocks on the LAN      │   │    scheduler: daily auto clock-out     │
        ▼ (R28)                                           └──────────────────────────────────────────┘
```

| Repository | Role |
| --- | --- |
| `timeclock-system` (this one) | The clock: app container, kiosk container, discovery service, libfprint patch, install image. |
| `timeclock-backend` (submodule at `backend/`) | Relay, PostgreSQL sidecar, migrations on startup, scheduler (R37). |
| `timeclock-schema` (new; submodule at `schema/`) | Shared pydantic models, shift logic, SQL migrations; published to SFTPyPI (R37.2, R37.5). |
| `aeth_ext` | Logging, alerts, shutdown; changes in §10. |
| `devkit-container` | Image template, entrypoint, supervisor, WireGuard spoke; changes in §11. |
| `wireguard-hub` | Peer table and firewall rules; configuration in §12. |
| `coolify-tunnel` | Coolify's SSH path to each clock host (R29); one registry line per clock. |

The repository and package names `timeclock-schema` / `timeclock_schema` and the submodule path `schema/`
are spec calls. Create the GitHub remote before running setup-project, or `GIT_REPO` renders empty.

## 4. Data model (timeclock-schema)

### 4.1 Package

`timeclock_schema` is a plain library (no Docker, no `[tool.docker]`), depending only on pydantic. Both the
clock and the backend pin it by version from SFTPyPI; releases go through `poe release` (R37.5). It holds:

- `models`: a pydantic model per table row, per LAN message (§6.7) and for the notification payload (§4.4).
  Serialized fingerprint templates are `SecretBytes` everywhere (R15).
- `shifts`: the read-side logic, pure functions over punches and corrections (§4.5). The clock uses it for
  its menu, guard and totals; the backend uses it for the automatic clock-out. It exists once, in Python;
  nothing reimplements it in SQL.
- `rules`: the rule-kind registry and the row model (§4.6).
- `migrations/NNNN_<name>.sql`: package data, applied in order by the backend (§8.3). A released migration
  file is never edited; changes go in a new file.

Timestamps are aware UTC `datetime`s in Python, `timestamptz` in Postgres and ISO 8601 UTC text in the
clock's SQLite (R24). Times of day (business hours, auto clock-out) are `datetime.time` / Postgres `time`
with no date and no zone (R41.3), interpreted in `settings.tz` (R41.4).

### 4.2 Tables (schema `public`)

Data tables live in `public`, configuration in `config` (R41.3). Column lists are the minimum; the builder
adds indexes as needed (at least `punches (employee_id, at)`).

**`groups`** (edited in place): `id bigint identity PK`, `name text unique`. The first migration seeds one
group, `default`, so employees can be added at once (spec call). An employee belongs to exactly one group
(spec call; R34 doesn't say whether membership is single or multiple, and single keeps rule precedence
trivial).

**`employees`** (edited in place, R30.1): `id bigint identity PK`, `name text`, `pin_username text unique`
(digits only, check constraint), `pin_hash text` (`crypt(pin, gen_salt('bf'))`, R23.1), `is_manager boolean
default false` (R23.6), `active boolean default true`, `group_id bigint FK groups default <default group>`,
`updated_at timestamptz` (set by trigger). Employees are referred to everywhere by `id`, never by name.
An employee who leaves is set `active = false`, since their punches reference the row: inactive employees
can't log in or punch, and their prints drop out of matching (spec call).

**`fingerprints`** (plain rows; the owner deletes one to force re-registration, R27.3, R27.4): `id uuid PK`
(made by the clock), `employee_id bigint FK employees ON DELETE CASCADE`, `template bytea` (libfprint's
serialized `FpPrint`), `registered_at timestamptz`, `instance_id text`, `db_login text default
session_user`. Several per employee are allowed; registration enrolls one (R27.3).

**`db_login`** (on `fingerprints`, `punches` and `overrides`; R26.2): the database login that inserted the
row, which identifies the clock without trusting its `INSTANCE_ID`. The clocks' column-level `INSERT`
grants leave it out (§4.4), so a clock can't set it and the default always applies. A mismatch with
`instance_id` isn't rejected, since a misconfigured clock would then never sync; it's visible in
`punch_log` instead (spec call).

**`punches`** (append-only, R38): `id uuid PK` (made by the recording clock), `employee_id bigint FK
employees`, `kind punch_kind` (enum: `clock_in`, `clock_out`, `break_start`, `break_end`, `lunch_start`,
`lunch_end`), `at timestamptz`, `instance_id text` (the recording clock's `INSTANCE_ID`; `backend` for
automatic clock-outs, `admin` by convention for punches the owner adds by hand), `automatic boolean default
false` (R41.1), `received_at timestamptz default now()` (server receipt time, for spotting late syncs; spec
call), `db_login text default session_user`. The clocks can only insert (§4.4), so to them the log is
append-only. The owner's admin account
(`postgres`) can still update or delete rows to fix bugs (R38.1); no trigger blocks it, at least until the
project is stable (§14).

**`punch_corrections`** (append-only, R38.1): `id uuid PK default gen_random_uuid()`, `punch_id uuid FK
punches`, `action correction_action` (`void` | `amend`), `new_at timestamptz null`, `new_kind punch_kind
null`, `note text`, `recorded_at timestamptz default now()`. A check constraint requires `new_at` or
`new_kind` for `amend` and neither for `void`. For each punch the newest correction wins, so a later `amend`
can restore a voided punch. Only the owner writes corrections; to add a missed punch, the owner inserts a
punch with `instance_id = 'admin'`.

**`overrides`** (append-only, R23.5): `id uuid PK`, `punch_id uuid FK punches` (the single punch it
authorized), `employee_id bigint FK`, `manager_id bigint FK`, `reason override_reason`
(`no_fingerprint` | `fingerprint_skipped`), `pin_only boolean` (the manager authorized by PIN alone),
`at timestamptz`, `instance_id text`, `received_at timestamptz default now()`, `db_login text default
session_user`. Written with its punch, in
the same local transaction and the same sync transaction. An override that's never used to punch isn't
recorded (spec call).

**`rules`** (edited in place, R34): `id bigint identity PK`, `kind text`, `params jsonb default '{}'`,
`group_id bigint FK null`, `employee_id bigint FK null` (check: exactly one is set), `on_violation
rule_action` (`refuse` | `warn` | `accept`, R34.2).

**`schema_migrations`**: `version int PK`, `name text`, `applied_at timestamptz` (§8.3).

**View `punch_log`** (R30.1): every punch joined to its employee's current name, with the punch's latest
correction (action, new values) and its `db_login` beside it. It's what the owner reads and exports to CSV; it applies no
shift logic.

### 4.3 Tables (schema `config`, edited in place)

**`config.business_hours`** (R40.3): `id bigint identity PK`, `weekday smallint` (ISO, 1 = Monday … 7 =
Sunday), `opens time`, `closes time`, check `opens < closes`. Nothing is unique per weekday; a weekday with
no rows is closed. Seeded empty, so a new install's reader sleeps until hours are added (tap-to-wake still
works; spec call).

**`config.settings`** (R41.3): one row (`id boolean PK default true check (id)`),
`auto_clock_out_enabled boolean default true`, `auto_clock_out_run_time time`,
`auto_clock_out_stamp_time time`, `subtract_lunch boolean default true`, `subtract_breaks boolean default
false` (R31.1). Seeded with both times at 23:00:00 for the owner to change (spec call).
A stamp time later than the run time fails validation in the pydantic model; the backend then skips the
run and alerts (R41.3).

### 4.4 Roles and notifications

Two databases in the one server, identical in layout (R26.6): `timeclock` (production) and
`timeclock_test` (everything timeclock-dev and any manual testing touches). Everything in §4 exists in both.

- **`postgres`** (superuser, password from `POSTGRES_PASSWORD`): owns everything, runs migrations, and is
  what the owner uses in the SQL editor (spec call).
- **`timeclock_clock_access`** (no login; created by the first migration if absent, since roles are
  server-wide): `SELECT` on `employees`, `groups`, `fingerprints`, `punches`, `punch_corrections`, `rules`,
  `config.business_hours`, `config.settings`; column-level `INSERT` on `punches`, `overrides` and
  `fingerprints`, every column except `db_login`, `received_at` and `punches.automatic` (only the backend
  makes automatic clock-outs); `USAGE` on schema `config`; nothing else (R26.2). Migrations grant to this
  role, so the same plain SQL serves both databases.
- **`timeclock_clocks`** and **`timeclock_test_clocks`** (no login, both members of
  `timeclock_clock_access`): `CONNECT` on `timeclock` goes only to the first and on `timeclock_test` only to
  the second, with `CONNECT` revoked from `PUBLIC` on both. The backend sets these up at every startup
  (§8.3). A dev login therefore can't write a permanent test punch into production.
- **One login per clock** (R26.2), which the owner creates by hand in the SQL editor when adding the clock,
  and pastes its password into the clock's Coolify environment (§9.3):
  `CREATE ROLE timeclock_clock_3 LOGIN PASSWORD '<random>' IN ROLE timeclock_clocks;` (timeclock-dev's is
  `timeclock_clock_dev` `IN ROLE timeclock_test_clocks`). To cut one clock off:
  `ALTER ROLE timeclock_clock_3 NOLOGIN;`, then
  `SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = 'timeclock_clock_3';` for its open
  connection. No passwords live in the backend or the repositories.

**Notifications** (R26.4, R30, R40.3): one trigger function on every table a clock mirrors (`employees`,
`groups`, `fingerprints`, `punches`, `punch_corrections`, `rules`, `config.business_hours`,
`config.settings`), after each row insert, update and delete, sends `NOTIFY timeclock, '<json>'` with
`{"table": ..., "op": ..., "id": ...}`. Payloads carry only the ID, never a row, so a 3.3 KB template never
approaches the 8,000-byte limit (spec call). The receiver fetches the row by ID (a fetch that finds nothing
means it was deleted); for `config.*` it re-fetches the whole table.

### 4.5 Shift logic (`timeclock_schema.shifts`)

Input: one employee's punches and their corrections. Steps:

1. Apply corrections: drop voided punches; replace `at`/`kind` from the newest `amend`.
2. Sort by effective `at`. A shift runs from a `clock_in` to the next `clock_in`. Within one shift, a real
   `clock_out` overrides an automatic one, in either order (R41.2): drop the automatic one.
3. Walk the events with this state machine; an event that isn't valid from the current state is an
   anomaly: skipped for state and totals, reported to the caller (payroll sees it in `punch_log`).

| State | Valid punches | Next state |
| --- | --- | --- |
| off | clock_in | working |
| working | clock_out / break_start / lunch_start | off / on break / at lunch |
| on break | break_end / clock_out | working / off |
| at lunch | lunch_end / clock_out | working / off |

A `clock_out` from a break or lunch ends it implicitly (spec call: it lets the automatic clock-out close
a shift left on break, and spares an employee a second tap).

Outputs: current state, valid next punches, open shift (if any), and worked time over any local-time window,
with a shift crossing Monday 00:00 split at midnight (R41.5). Lunch time is subtracted from worked time and
break time isn't, by default; `config.settings` switches each separately (R31.1), so the functions take both
switches as arguments.

### 4.6 Rules (`timeclock_schema.rules`)

A registry maps `kind` to a pydantic params model and a check function `(employee state, proposed punch,
history) -> violation | None`. The row model validates `params` with the kind's model, as a discriminated
union on `kind` (R34.1). For an employee, an employee-level row of a kind replaces the group's rows of that
kind (spec call). At punch time the clock runs every applicable rule: any `refuse` blocks the punch with
the rule's message, `warn` shows the message on the confirmation and accepts, `accept` records silently
(R34.2). A row that fails validation (unknown kind, bad params) is skipped and alerted once per row and
`updated_at` (spec call: refusing every punch over one bad row would stop the office).

Version 1 registers no kinds, so every existing row fails validation; the registry and the punch-time hook
are tested with a test-only kind.

## 5. Clock host

- Debian 13, no desktop (R3.1), installed from `image/dev-device/`. Wired Ethernet only.
- `unattended-upgrades`, security only, rebooting at 03:00 when needed (R42): set in `preseed.cfg.in`
  (`pkgsel/update-policy`) plus an apt config file under `files/` for the reboot; takes effect from the next
  image rebuild. Don't rebuild the image as part of this work.
- Time zone America/New_York and NTP come from the image. Containers share the host clock, so punch
  timestamps come from it; they don't depend on the host's zone (§9).
- libfprint's hwdb rule from the host's `libfprint-2-2` package enables USB autosuspend for the reader. If a
  future host image drops that package, autosuspend has to be enabled another way.
- The systemd reverse tunnel to coolify-tunnel and its key are part of the one-time install (R29,
  `docs/dev-device-setup.md`).

## 6. Clock app (timeclock-system)

### 6.1 Process structure

One process, started by devkit-container's entrypoint as uid 999 after the reader startup script (R14.7).

1. The entry point defines `PROJECT_NAME`, calls aeth_ext's `initialize()` (§6.9), and loads the app's
   settings (§9) and the local SQLite store.
2. It starts the scanner thread (§6.2), then calls NiceGUI's `ui.run` (uvicorn on uvloop, R5, R12) with
   `log_config=None`, uvicorn's signal capture disabled, and never `native=True`.
3. On the running loop it installs aeth_ext's loop exception handler (R16.6) and starts the async tasks:
   database sync (§6.6), LAN sharing (§6.7), business-hours control (§6.3) and the heartbeat (§6.2).
4. Shutdown is aeth_ext's (R21): its callback sets the uvicorn server's `should_exit`; a THREADED-phase,
   non-required callback cancels the scanner's `Gio.Cancellable`.

Blocking work other than libfprint (SQLite, bcrypt) runs in `asyncio.to_thread`; failures there are
handled explicitly, since the global hooks don't see exceptions held in futures (§6.9).

### 6.2 Scanner thread

All libfprint calls happen on one daemon thread with the blocking `_sync` API; nothing else runs GLib's main
loop (R11.1). The thread keeps the `FPrint.Context` referenced for the process's lifetime (dropping it
segfaults) and opens the 4500 (`uru4000`). The event loop sends it commands through a queue; it posts
results back with `loop.call_soon_threadsafe`. Changing mode cancels the current call through the shared
`Gio.Cancellable`, which ends a waiting capture at once.

Modes:

- **closed**: device closed, so the reader autosuspends and its LED rests (R40.1).
- **ready**: `identify_sync` against the gallery, in a loop. Results: match (employee, print), no match,
  retry.
- **confirm(employee)**: one identify for the 30 s rescan and the PIN path's fingerprint step (R45.2,
  R23.3), reporting which employee matched, if any.
- **enroll(employee)**: `enroll_sync` with its 5 stages, reporting each stage for the progress display.

Rules every path follows:

- **Never spin** (anti-goal): `FPrint.DeviceRetry` → wait 0.5 s, then re-arm. Any other error → back off
  exponentially, 1 s up to 30 s, reset by a successful capture. `FP_DEVICE_ERROR_TOO_HOT` (shouldn't happen
  with the patch) → 60 s. After any capture, re-arm only once `finger-status` shows no finger, or after 2 s,
  so the tail of one press isn't read as the next (seen at 0.54 s in the spike).
- **Finger touch** (`notify::finger-status`) shows "Scanning…" at once; a resting thumb can take up to 16 s.
- **Reader gone** (removed, or open fails) → fatal shutdown; Docker's restart and the startup script pick
  it up when it's back (R14.3, R14.7).
- **Hung thread** (spec call; the owner left the mechanism open): in ready mode the thread cancels and re-arms
  its wait every 60 s. It records the start time of every blocking libfprint call; the heartbeat task
  stops writing aeth_ext's heartbeat while a call has been running longer than 90 s. A hung thread then
  turns the heartbeat stale, devkit-container's health check fails after 180 s, and the existing fail
  ping alerts a technician. Nothing restarts automatically (declined by the owner).

**The gallery** (R36.4): every active employee's prints, deserialized once and kept in memory, ordered by
the employee's most recent punch at this clock (identify stops at the first match). Rebuilt when
employees or fingerprints change.

**The matcher boundary** (R36.3): everything goes through one function, `identify(...) -> Match | NoMatch |
Retry`, which nothing bypasses. Version 1 calls libfprint's `identify_sync(gallery)`, which captures and
matches in one call. Version 2 will capture with an empty gallery (libfprint still returns the scanned
print) and match in Rust; prints stay in libfprint's serialized form, so it needs no re-enrollment.

### 6.3 Business hours and tap-to-wake

A task computes the clock's current window from its local copy of `config.business_hours` in
`settings.tz`, through the single lookup that per-clock hours would later change (R40.3). Inside a window
the scanner is in ready mode; outside, closed. A scan in progress at closing finishes first (R40.2).

Outside business hours the idle screen says "Tap to scan". A tap, or anything else that needs a scan
(the PIN path's fingerprint step, a manager override, registration), opens the reader for 30 s, then
closes it again (R40.2).

### 6.4 Screens and flows

The UI is one NiceGUI page for the kiosk; it carries the company's colors (yellow `#F9E60D`, red
`#ED1A2C`, near-black `#161514`, white) and logo (R39). The logo SVGs are read at runtime from
`persisted_data/branding/` on the device (R39.1; a missing file renders the page without it and logs a
warning; spec call). Large touch targets, landscape layout from 1024×600 up; no particular screen model
(R7.1). Typed input uses an on-page number pad that also accepts keyboard input (R25). The page turns off
text selection and the long-press menu (R13.2).

- **Idle.** Logo, date and time, "Place your finger on the reader" (or "Tap to scan" after hours), a "Use
  PIN" button, and a database status icon: green while connected, red while not, with a small timestamp of
  the last contact with the database. No match:
  "Not recognized: try again or use your PIN", 3 s. Retry: "Couldn't read that, try again".
- **Action menu** (R45). The employee's name, current state ("Clocked in since 8:02 AM"), hours this week,
  and one button per valid punch (§4.5). Clock-in always takes a tap (R45.1); the layout leaves room for a
  scheduled time later. A punch button tapped more than 30 s after the scan asks for a new scan that must
  match the same employee; a scan matching someone else opens their menu instead, with no punch (R45.2).
  A new scan while the menu is open switches to that person's menu; untouched, it closes after 2 minutes
  (R45.4). A `refuse` rule shows its message here and records nothing.
- **Confirmation** (R31). "Clocked out at 5:01 PM", hours this week, any `warn` rule messages; back to idle
  after 5 s. Hours this week use the local copy, so offline they miss other clocks' unsynced punches
  (R27.1).
- **PIN login** (R23). Username, then password, on the number pad; verified against the local `pin_hash`
  with `bcrypt` off the event loop (spec call: the `bcrypt` package checks pgcrypto's `$2a$` hashes). No
  lockout after wrong PINs: punching still needs a fingerprint or an override. Then:
  - no registered print → "Register your fingerprint" or "Manager override" (R23.2);
  - registered print → "Scan your finger" (confirm mode; must match this employee) or "Manager override"
    (R23.3).
- **Registration** (R23.4, R27.2). Five presses with progress, 30 s without a press cancels. The new print
  is stored locally with a UUID, sent to the other clocks (§6.7), queued for sync, and added to the gallery;
  then the employee's action menu opens (spec call: they've just proven the finger).
- **Manager override** (R23.5). A manager (active, `is_manager`) scans, or enters their PIN login, which
  doesn't also need their fingerprint. The override can't come from the employee themselves (spec call: a
  manager without a print registers one instead). It authorizes exactly one punch; every further punch, and
  the 30 s re-authentication, needs a fresh override unless the employee gives a fingerprint (R45.3). The
  override row is written with that punch, `pin_only` set when the manager used a PIN.

### 6.5 Local store

SQLite in WAL mode at `persisted_data/timeclock.sqlite3`, written only by the app. It mirrors the tables a
clock reads (§4.4) plus sync bookkeeping: a `synced` flag on punches, overrides and fingerprints recorded at
this clock. Kept (R26.4, R41):

- punches and corrections for the current week (Monday 00:00 local, R33),
- every punch of a shift still open at the rollover, back to its clock-in, until its closing punch arrives,
- every unsynced record, whatever its age,
- employees, groups, fingerprints, rules and config in full.

At Monday 00:00 local the clock prunes synced punches and corrections from before the new week, except
open shifts. Punch timestamps come from the host clock, stored UTC.

The guard (R26.3) is the menu itself: only punches valid from the employee's current state (§4.5) are
offered, computed from the local store, and the recording path re-checks before inserting.

### 6.6 Database sync

One async task owns a `psycopg` 3 async connection (spec call; `psycopg[binary]` if it ships Python 3.14
wheels, otherwise `psycopg` plus `libpq5` in the final window) to `TIMECLOCK_DB_HOST:5432`, the backend's
relay (§8.1), with the database and login from `TIMECLOCK_DB_NAME` and `TIMECLOCK_DB_USER` (§9.1). Nothing
in the clock's code knows which database it's using (R26.6). TCP keepalives (idle 30 s, interval 10 s, 3 probes) detect a dead
tunnel in about a minute (spec call). Loop:

1. Connect; on failure retry with backoff from 1 s to 60 s. The idle screen's status icon turns red while
   disconnected (§6.4); the last-contact time is the last successful exchange with the database.
2. `LISTEN timeclock`.
3. Catch up (R26.4, R41): fetch the current week's punches and corrections plus everything since the oldest
   open shift held locally; all employees, groups, fingerprints, rules and config. Replace the local copies.
4. Push unsynced records oldest first, one transaction per punch with its override: `INSERT ... ON CONFLICT
   (id) DO NOTHING` (R38), then mark them synced.
5. Wait for notifications (§4.4) and new local records; apply or push each as it arrives.

Any error drops the connection and returns to step 1. A notification for a punch older than the local
window that touches no open shift is ignored.

### 6.7 LAN sharing (R28)

On each punch and each fingerprint registration, the clock sends the record to every other clock, best
effort, over WireGuard directly (§11.3). Transport (spec call): a small asyncio TCP server bound to the
clock's tunnel address on port 8791, one JSON message per connection (a pydantic `LanRecord`: a punch or a
fingerprint, with the sender's `INSTANCE_ID`). The sender opens a connection to each peer's tunnel address
with a 2 s timeout and doesn't retry (no catch-up between clocks). A receiver inserts into SQLite ignoring
duplicate UUIDs, marks it synced-elsewhere (only the recording clock syncs to PostgreSQL), and updates
state and gallery. WireGuard authenticates the sender; the server also drops connections from addresses
outside the clocks' tunnel addresses. Overrides aren't shared; they change no state.

The other clocks and their tunnel addresses come from devkit-container's cached peer table
(`persisted_data/wireguard/peers.toml`): every row sharing this clock's `lan` value, the clock finding its
own row by `persisted_data/wireguard/public_key` (§11.3). The table is re-read when the file changes. A
clock that has never reached the hub has no cache and no tunnel address yet; the server retries binding
every 30 s.

### 6.8 Crash-alert stopgap (R20)

The clock saves every crash alert as evidence; nothing is resent, since the clock can't tell which alerts
went out (R20). A THREADED-phase shutdown callback, registered as a plain function (§6.9), runs only when
the shutdown kind is FATAL: it renders `get_current_fatal_trails()` (`errors/shutdown.py:458`) with a UTC
timestamp, the program name and the host name into one JSON line, appended to
`persisted_data/crash_alerts/crash-alerts.<n>.jsonl`. Five files of at most 200 lines each, oldest file
reused first (spec call). It must finish well inside the fatal budget (1 s): one small append, no network.
No aeth_ext change is needed. Replaced later by aeth_ext's hold-and-resend (TODO #21).

### 6.9 aeth_ext wiring

- **Entry point.** `__main__.py` defines the constants aeth_ext reads by AST (§10 intro):
  `PROJECT_NAME = "timeclock-system"`, `REQUIRE_LOG_SERVER = False` (§10.3),
  `SHUTDOWN_ON_LOG_REJECTION = False` (§10.5). `import aeth_ext` needs `ALERTS_EMAIL_PWD` in the
  environment. `[project.scripts]` gets the single `run-app-timeclock-system` (devkit-container `run` needs
  exactly one `run-app-*`) and the reader startup script (§6.10); `pyproject.toml` gets a `[build-system]`
  (`uv_build`, like the backend).
- **Order.** `initialize(logging="socket", install_signal_handlers=True)` runs first, with `asyncio=False`
  (uvicorn brings uvloop itself, R5), then the scanner thread, then `ui.run(..., log_config=None)`.
  Otherwise uvicorn's `dictConfig` closes aeth_ext's handlers.
- **Signals (R21).** uvicorn's signal capture is replaced by a no-op (`Server.capture_signals`); pin uvicorn,
  since an upgrade could rename it. aeth_ext's signal ladder drives the shutdown.
- **Shutdown callbacks**, registered as plain functions or closures: aeth_ext holds bound methods weakly and
  silently drops one whose owner is collected (`errors/shutdown.py:522-584`). THREADED phase only: set the
  uvicorn server's `should_exit`; cancel the scanner's `Gio.Cancellable` (non-required); the crash-alert
  writer (§6.8). Nothing in the INTERRUPT phase.
- **Loop handler.** NiceGUI's `app.on_startup` calls `install_loop_exception_handler()` (§10.6).
- **Not wrapped:** the code that calls `ui.run` is never wrapped in `handle_fatal_exc_sync`, since aeth_ext's
  exit nudge (`KeyboardInterrupt`) would count as fatal (TODO #15). Work in `asyncio.to_thread` and
  executors handles its own exceptions; the global hooks don't see exceptions held in futures.
- **Heartbeat.** The app's own task writes the file every 60 s with `send_heartbeat_async(
  settings.log_loc_folder / "heartbeat.txt", tz=settings.tz)` and no ping arguments, so it only writes the
  file; the supervisor owns the healthchecks.io pings (`DEVKIT_SUPERVISED_PING`). It skips the write while
  the scanner is hung (§6.2). devkit-container treats the file as stale after 180 s.
- **Settings from compose.** aeth_ext's settings are read from the environment at import, so the clock's
  compose sets `LOG_HISTORY_RETENTION_DAYS=14` (R44), `TZ`, `INSTANCE_ID` and the log server address
  (§9). The alert host name is the container's, so compose sets `hostname: timeclock-system-${INSTANCE_ID}`.

### 6.10 Reader access and the image

- `[tool.docker].startup_scripts = ["timeclock-reader-access"]` runs as root before the app (R14.7). Startup
  scripts must be `[project.scripts]` names, so it's a Python port of the spike's shell version (Appendix A):
  find `05ba:000a` in `/sys/bus/usb/devices`, chown its `/dev/bus/usb/BBB/DDD` node to
  999:999, exit nonzero if it isn't there. The supervisor sends healthchecks.io's `/fail` ping when a startup
  script fails (devkit-container `src/run.rs:111-114`), so a missing reader alerts someone. The screen stays
  blank meanwhile (the kiosk waits on the app's health check; accepted, R14.7).
- Builder-setup window: `gcc pkg-config libgirepository-2.0-dev libcairo2-dev` for PyGObject/pycairo, and
  libfprint's build (§7). Final window: libfprint's runtime dependencies, the built library and
  `FPrint-2.0` typelib (§7), `libgirepository-2.0-0`, `libcairo2`. PyGObject comes from PyPI (R11.2).

## 7. Patched libfprint (R40, R40.4)

- The patch, `docker/libfprint/uru4000-no-heat-model.patch`, adds `dev_class->temp_hot_seconds = -1;` to
  the `uru4000` driver's class init: the opt-out the match-on-chip drivers use.
- In the `builder-cached` window (§11.1), the build `COPY`s the patch from the build context (repo files
  from the clone aren't available in any window), downloads libfprint's upstream 1.94.100 release tarball,
  checks a pinned SHA-256, applies the patch (failing the build if it doesn't apply), and builds with meson
  into a prefix such as `/opt/libfprint`: only the `uru4000` driver, introspection on, docs and examples off.
  Build dependencies are unverified; the builder finds them from libfprint's meson files and Debian's
  package. The build context is Coolify's checkout of the deployed branch, while the app comes from
  `GIT_TAG`; both are the same commit in normal use.
- The final window `COPY --from=builder`s the prefix and installs libfprint's runtime libraries, and the
  image sets `GI_TYPELIB_PATH` and the linker path so PyGObject finds `FPrint-2.0.typelib` and the library.
  Debian's `libfprint-2-2` and `gir1.2-fprint-2.0` are not installed in the image.
- That window sits above the template's `GIT_TAG`/`GIT_REPO` build arguments, so Docker reuses the libfprint
  build between deploys and recompiles it only when the window changes, or when the device's build cache is
  pruned. If a fresh build still makes deploys too slow, the fallback is a fork repository that
  publishes prebuilt binaries for the image to download (R40.4, §14).
- The hardware tests ran on 1.94.9; the first deploy re-checks the reader on 1.94.100 (§13).

## 8. timeclock-backend

Today it's a heartbeat-only skeleton (`src/timeclock_backend/__main__.py:22-37`, which never calls
`initialize()`), depending on `aeth-ext>=9.0.2` and `devkit-container>=2.2.0`, with `[tool.docker]`
`wireguard = false`, `supervise = false`. Its `v0.1.0` tag has no GitHub release and isn't on SFTPyPI, so
`poe docker-pin` can't pin it until the first real `poe release`.

- **Dependencies:** `aeth-ext[async]>=10` (`initialize(asyncio=True)` imports uvloop), `apscheduler>=3.11,<4`,
  the same Postgres driver as the clock (§6.6), `timeclock-schema`.
- **`[tool.docker] wireguard = true`**, then re-run setup-project (it re-renders compose and the
  Dockerfile: WireGuard env, `cap_add: NET_ADMIN`, the two-file health check).
- **No tolerate** (R37.6): the backend shares the VPS with the hub, so a WireGuard failure there means a
  misconfiguration or a problem with the server, and the backend stops and alerts. Without
  `WG_TOLERATE_DISCONNECTED`, a boot without a handshake already exits with healthchecks.io's `/fail` ping.
  `WG_DISCONNECTED_LIMIT_SECS=1` makes a running backend give up as soon as the tunnel counts as
  Disconnected: WireGuard renews handshakes only every 2 minutes, so that's about 150 s after the tunnel
  dies (`WG_STALE_SECS` can't go lower). Docker's restart then retries, and each boot fails and alerts again
  until the tunnel works.
- **Entry point**, following ScheduledReportAggregator's (`__main__.py:20-50`, `startup.py:121-200`):
  `PROJECT_NAME = "timeclock-backend"`, `REQUIRE_LOG_SERVER = False`, `SHUTDOWN_ON_LOG_REJECTION = False`
  (spec call: a log-server outage mustn't take the relay down); `initialize(asyncio=True, logging="socket")`,
  a deferred import, `asyncio.run(main())`, `KeyboardInterrupt` swallowed, exit code from `SHUTDOWN.kind`.
- **`main()`**, in order: the heartbeat task (`run_heartbeat_async`, as aeth_ext's log server does,
  `central_log_server/startup.py:120-128`); connect to Postgres, retrying while the sidecar starts; run
  migrations and set the clock role's password (§8.3); start the relay (§8.1); start the scheduler and the
  `LISTEN` task (§8.4); `await SHUTDOWN`; close the relay, `scheduler.shutdown(wait=False)` from the loop
  (never from a THREADED callback); `await SHUTDOWN_COMPLETE`. One THREADED callback, a closure, pauses the
  scheduler (ScheduledInvoiceProcessor's `freeze_scheduler`, `shutdown_hooks.py:60-112`).

### 8.1 Relay (R26.1)

An asyncio server on `0.0.0.0:5432` inside the backend container. It accepts a connection only from a source
address inside the hub's network (`RELAY_ALLOWED_CIDR`, default `10.8.0.0/24`; spec call), since the
container also sits on the VPS's shared `coolify` network. For each connection it opens one to
`postgres:5432` and copies bytes both ways until either side closes, then closes both; TCP keepalives on
both sockets; no protocol parsing. Connection errors are normal, handled per connection, and logged at
warning, never fatal. Postgres's own password authentication still applies, so the relay only adds reach.

### 8.2 PostgreSQL sidecar (R26.5)

An unlisted service in the backend's compose (setup-project leaves unlisted services alone):

- `image: postgres:18` (pin the minor in the tag), `restart: unless-stopped`.
- `POSTGRES_DB=timeclock`, `POSTGRES_PASSWORD` from a Coolify secret. The image creates `timeclock` on
  first start; the backend creates `timeclock_test` at startup if it's missing (`CREATE DATABASE` can't run
  inside a transaction).
- Data at `/data/timeclock_backend_postgres` bind-mounted to `/var/lib/postgresql` (Postgres 18 images keep
  their data in a versioned folder below it).
- A private network `timeclock-db`, shared only with the backend app, which also stays on `coolify`.
- `ports: ["127.0.0.1:5432:5432"]`, the VPS's loopback only, for the owner's SQL editor over SSH (R30.2;
  spec call). Nothing else publishes it.
- A `pg_isready` health check; the backend `depends_on` it being healthy.

### 8.3 Migrations and the clock role

At startup, as `postgres`, for each of `timeclock` and `timeclock_test` in turn, under a session advisory
lock: create `schema_migrations` if absent, then apply each `timeclock_schema` migration not yet recorded,
in order, each in one transaction with its `schema_migrations` row. Then the group roles (§4.4): create
`timeclock_clocks` and `timeclock_test_clocks` if absent, make both members of `timeclock_clock_access`,
and apply the per-database `CONNECT` grants and the revoke from `PUBLIC`. The clock logins themselves are
the owner's, by hand. A failed migration is fatal: alert and exit; Docker restarts it and the clocks keep
buffering.

`0001_initial.sql` creates `pgcrypto`, the enums, every table, view, trigger and seed row of §4, the schema
`config`, the role `timeclock_clock_access` if absent, and its grants. Being plain SQL, it runs unchanged
in both databases.

### 8.4 Scheduler and the automatic clock-out (R37, R41)

- **Shape:** `scheduler_config.py` copied from ScheduledReportAggregator, not ScheduledInvoiceProcessor: RA's
  executor callback swallows aeth_ext's exit-nudge `KeyboardInterrupt` (RA `scheduler_config.py:128-139`)
  so a `docker stop` mid-job isn't reported as fatal. Drop RA's invoice-specific quiet-job patterns.
  `MemoryJobStore`, `job_defaults = {"misfire_grace_time": 60, "coalesce": True}`,
  `timezone=SETTINGS.tz`.
- **Time zone:** pass `timezone=SETTINGS.tz` to every `CronTrigger` explicitly; the scheduler's zone doesn't
  reach trigger objects, and a bare trigger falls back to the container's zone, UTC (R41.4).
- **Errors:** any exception escaping a job is fatal to the whole process, relay included. The job catches
  what it expects: invalid settings (alert, skip the run, R41.3) and database errors (alert, skip; the next
  run retries). Only bugs escape.
- **Both databases:** everything below runs once per database, each with its own `config.settings`, its own
  `LISTEN` connection and its own job (R26.6).
- **Schedule from the database:** the backend holds its own `LISTEN timeclock` connection. On a
  `config.settings` notification it re-reads the row and reschedules the job with a new
  `CronTrigger(hour, minute, second, timezone)`; `auto_clock_out_enabled = false` pauses it.
- **Catch-up** (spec call): at startup and after every reschedule, if today's run time has already passed,
  the job runs once for today. The job is idempotent, and without this a restart across the run time
  silently skips that day, since jobs live in memory with a 60 s grace.
- **The job** (R41.1): stamp = today's date in `SETTINGS.tz` at `auto_clock_out_stamp_time`, as UTC. It
  loads the last 14 days of punches and corrections (spec call), works out each employee's state with
  `timeclock_schema.shifts`, and for every employee not `off` whose last punch is before the stamp, inserts
  a `clock_out` at the stamp with `automatic = true`, `instance_id = 'backend'` and a new UUID, all in one
  transaction. The clocks receive them like any punch and prune those open shifts (R41).

## 9. Configuration and deployment

### 9.1 The clock's compose (`docker/compose.yaml`)

The file exists but is empty. Don't delete it: setup-project and `docker-pin` search breadth-first and would
then edit `backend/docker/compose.yaml`. Give it a block-form `services:` key first; with
`[tool.docker].services = ["timeclock-system"]` and `wireguard = true` committed, setup-project then offers
to add the service and renders the Dockerfile from the template. The clock's `[tool.docker]`:
`services = ["timeclock-system"]`, `required_persisted_dirs = ["persisted_data"]`, `wireguard = true`,
`startup_scripts = ["timeclock-reader-access"]`.

Services (R14, as amended by R28):

- **`timeclock-system`** (listed; rendered by setup-project, edited by hand where noted):
  `restart: unless-stopped`, `hostname: timeclock-system-${INSTANCE_ID:?}` (the alert host name),
  `/data/timeclock_system_files` → `/app/persisted_data`, `/dev/bus/usb` mounted with
  `device_cgroup_rules: ["c 189:* rmw"]` (R14.7), `ports: ["51821:51821/udp"]` (the WireGuard listen port,
  R28.2; the only published port), network `coolify`, and the template's `cap_add` and health check.
- **`kiosk`** (unlisted): `build: {context: ., dockerfile: docker/kiosk/Dockerfile}`, `privileged: true`,
  `/dev:/dev`, `KIOSK_URL=http://timeclock-system:8080`, `depends_on` the app with
  `condition: service_healthy` (R14.4), network `coolify`, `restart: unless-stopped`. The Dockerfile and
  `kiosk.sh` are the ones verified on the hardware (Appendix A); `policies.json` in
  `/usr/lib/firefox-esr/distribution/` turns off pinch zoom, swipe-back and the session-restore prompt
  (R13.2), with Firefox autoconfig for any preference policies can't set. The kiosk builds from the deployed
  checkout, not `GIT_TAG`.
- **`discovery`** (unlisted): the app's `build:` block, same `GIT_REPO` / `GIT_TAG` (so `docker-pin` pins it
  too, and the build cache makes the second build cheap), `entrypoint: ["/app/.venv/bin/devkit-container",
  "discover"]`, `network_mode: host`, the same persisted bind mount, `WG_LISTEN_PORT`,
  `restart: unless-stopped` (§11.3).

The clock's environment, set per clock in its Coolify application:

| Variable | Value |
| --- | --- |
| `INSTANCE_ID` | the clock's number, from 1; `dev` on timeclock-dev (R16.2, R3.3) |
| `HEARTBEAT_SLUG` | `timeclock-system-${INSTANCE_ID:?}` (R14.6) |
| `TZ` | `America/New_York` (R41.4) |
| `LOG_HISTORY_RETENTION_DAYS` | `14` (R44) |
| `LOG_CONN_HOST` | the log server's tunnel address |
| `ALERTS_EMAIL`, `ALERTS_EMAIL_PWD`, `ALERTS_RECIPIENTS`, `PINGKEY` | as on other projects |
| `WG_PRIVATE_KEY`, `WG_HUB_URL`, `WG_HUB_REPO`, `WG_HUB_TOKEN` | fetched mode |
| `WG_TOLERATE_DISCONNECTED` | `1` (R22) |
| `WG_HANDSHAKE_TIMEOUT_SECS` | `10` (§11.2) |
| `WG_LISTEN_PORT` | `51821` (§11.3) |
| `TIMECLOCK_DB_HOST` | the backend's tunnel address |
| `TIMECLOCK_DB_NAME`, `TIMECLOCK_DB_USER`, `TIMECLOCK_DB_PASSWORD` | `timeclock`, the clock's own login (e.g. `timeclock_clock_3`) and its password; on timeclock-dev `timeclock_test`, `timeclock_clock_dev` (R26.2, R26.6) |

Each clock is one Coolify application (Docker Compose type) on its own server. The server's proxy is None
(R14.5). Compose deploys stop the old containers before starting new ones, which blanks the screen and
stops punches: deploy outside shift changes. The owner puts the logo files in
`/data/timeclock_system_files/branding/` on each device (R39.1).

### 9.2 The backend's compose

The listed `timeclock-backend` service on networks `coolify` and `timeclock-db`, plus the `postgres` sidecar
(§8.2). Environment: `HEARTBEAT_SLUG=timeclock-backend`, the alert variables, the four fetched-mode `WG_*`
variables, `WG_DISCONNECTED_LIMIT_SECS=1` (no tolerate, §8), `TZ=America/New_York`, `POSTGRES_PASSWORD`,
and `LOG_CONN_HOST=central-log-server` (on the VPS it reaches the log server
over the `coolify` network, not the tunnel).

### 9.3 Adding a clock

One-time, per device:

1. Install from the image. The image no longer hardcodes a hostname: the installer asks for it (R3.2;
   `preseed.cfg.in` stops answering `netcfg/get_hostname`; spec call). Applies from the next image rebuild.
2. `sudo coolify-tunnel-setup <port>` with the next free port (22001 is timeclock-dev's, so clocks start at
   22002); add its line to
   coolify-tunnel's `docker/authorized_keys`, then redeploy coolify-tunnel (R29, `docs/dev-device-setup.md`).
3. In Coolify: the server at `coolify-tunnel:<port>`, Coolify's key in root's `authorized_keys`, Proxy None.
4. Enroll the clock in the hub (§12): its row with `lan`, its forward rules, a hub release.
5. Create the clock's database login in the SQL editor with a random password (§4.4).
6. Create the clock's Coolify application with its environment (§9.1), that password included; deploy.

## 10. aeth_ext changes (R16)

aeth_ext 9.0.2 (`D:\SFT Software Projects\SFT Workspace\aeth_ext`); paths below are under
`src/aeth_ext/`. Its own `AGENTS.md` and `.claude/CLAUDE.md` bind the work (2-space indent, no
`from __future__ import annotations`, `IsPydantic` for pydantic dataclasses, PEP 758). Anything that trips
the one-shot global `SHUTDOWN` is tested in an `-O` subprocess (`tests/errors/_optimized_scenarios.py`).
aeth_ext's module-level settings are built at import, so every new setting is read from the environment,
never from an app's Settings subclass (TODO #18). Socket-client parameters come from ALL-CAPS constants in
the app's `__main__.py` (`logging/init.py:69-130`), so every new one needs a default; a new
`socket_client.toml` `runtime://` key must be registered in both `configure_shared_socket_logging_client`
(`logging/setup.py:616-619`) and `get_default_socket_logging_configs` (`:507-510`). One release covers all
of §10 as 10.0.0 (`uv run poe release major`), since the crash hooks are on by default for every consumer
(R16.6).

### 10.1 Bounded alert sends (R16.1, TODO #16)

- New setting `ALERTS_SEND_TIMEOUT_SECS` (default 10). `utils.batch_send_emails` gains a `timeout`
  parameter defaulting to it and passes it to `SMTP(...)` (`utils.py:234`, unbounded today); Pushover
  (`send_alert_push.py:24,100`) uses the same setting.
- `alert()` (`errors/err_handling.py:88-109`) runs both sends on a short-lived daemon thread and joins it
  for at most twice the timeout plus a second; past that it gives up on the send. That bounds what the
  socket timeouts can't, a stalled DNS lookup, and matters because `_handle_fatal` alerts before it
  requests the shutdown (`:141-151`).
- No pre-send network check (R16.1): every alert is attempted, and the send thread's deadline bounds the
  wait.

### 10.2 `INSTANCE_ID` (R16.2)

- `BaseSettings` field `instance_id: str | None`, alias `INSTANCE_ID`, pattern `^[A-Za-z0-9_-]+$`; an empty
  value is unset (`env_ignore_empty`). A bad value fails at `import aeth_ext` with pydantic's error.
- When set, `<PROJECT_NAME>-<INSTANCE_ID>` replaces the project name in `_resolve_program_name`
  (`errors/err_handling.py:55-79`: alert subject and body) and in the socket `program_name` registration
  (`logging/setup.py:616`) and `get_default_socket_logging_configs` (`:507`). The program name keys the
  client history folder, the server's log folder and its ID registry, so each clock gets its own.
- Not applied to `TaggedLogRecord.project_name` (its `libname` matching, `logging/bases.py:244`, compares it
  to path parts) or to log file names, which already sit inside the per-instance folder (spec call).

### 10.3 Optional log server at startup (R16.3, TODO #14)

`configure_shared_socket_logging_client` gains `require_log_server: bool = True`. False skips the
`RuntimeError` after a failed probe (`logging/setup.py:608-614`); the handler buffers as the probe's own
warning says. The app opts out with `REQUIRE_LOG_SERVER = False` in `__main__.py`. TODO #14's retry and
default flip aren't part of this.

### 10.4 Non-blocking socket logging (R16.4, TODO #20)

Today `HandshakeSocketHandler.emit` does everything under the handler lock on the caller's thread:
reconnect with backoff, handshake, a 5 s ack read, backlog replay and a `sendall` with a 30 s timeout
(`central_log_server/client/__init__.py:465-530, 360-463`).

The history buffer is the queue (R16.4): there's no second queue of records.

- **`emit`** filters, records (`RecordDurability.record`: ID and history append), then rings a doorbell: a
  token on a `queue.SimpleQueue`, the one wake-up that's safe inside a signal handler (`Event.set()` and
  `Queue.put()` take locks the interrupted code may hold). Nothing else runs on the caller's thread.
- **A daemon sender thread** sleeps on the doorbell and owns the socket: connect with backoff, handshake,
  ack read, replay, sending and emergency-mode bookkeeping (`record_success` / `record_failure` /
  `maybe_enter`). Its shape follows `ThreadedQueueDrainer` in the same file (`:907-1211`), and it reuses the
  handler's own `_send_handshake` and `_replay_backlog`. After each handshake, replay starts from the
  server ack's `last_record_id`, as today (`resolve_backlog`, `client/durability.py:99-113`). On each ring
  it sends `history.find_after(last_sent_id)`, marking each sent; a send error drops the socket and the
  thread reconnects after its backoff. The server doesn't dedupe (`server/id_registry.py:100-116`), so
  nothing is sent twice: `last_sent_id` only moves forward.
- **History fixes**, because spills move every entry to disk, sent or not (`client/history.py:442-484`), and
  shutdown retires the in-memory list (`:334-388`), so the sender often reads from disk:
  - `find_after` takes the buffer's existing `_lock` (`:205`); unlocked today (`:486-508`), it would race
    `append` once a second thread calls it;
  - `_search_disk` scans every retained day after the hint, not the hint's date ±1 (`:510-539`): today an
    outage longer than two days silently skips the middle days, and the clock retains 14;
  - `find_after(None)`, a program the server has never seen (a new `INSTANCE_ID`), returns all retained
    history, disk included, not just memory.
- **`connect_and_verify()`** still runs synchronously at startup, before the thread starts, then hands the
  socket over.
- **Tests:** four test files exercise this handler (`tests/central_log_server/test_client.py`,
  `test_client_server_integration.py`, `test_client_transport_reentrancy.py`, `tests/logging/test_setup.py`);
  those that expect `emit` to deliver synchronously wait for the sender instead.
- **Shutdown:** `_finish_shutdown` / `close()` (`:310-318, 538-557`) signal the thread, flush within what's
  left of the transport's budget (`LOGGING_TRANSPORT_PRIORITY`), and join; `close()` is idempotent and
  bounded, since `logging.shutdown` calls it again at exit. On a fatal shutdown, durability still comes from
  the INTERRUPT-phase history write-through.
- The ack-read-failure alert (`:396-400`) fires on every reconnect today; it latches like 10.5's alert.
- The sender thread reports its own failures with `emergency_diagnostic`, never the logger.

### 10.5 Config rejection without shutdown (R16.5, TODO #14)

- Handler keyword `shutdown_on_rejection: bool = True`, a `runtime://` key registered in both places
  (§10 intro), set by the app with `SHUTDOWN_ON_LOG_REJECTION = False`.
- It covers both paths that call `trigger_shutdown` today: `HandshakeAck(ok=False)` in `_send_handshake`
  (`:411-421`) and `ApplyFailure` in `_watch_apply_result` (`:441-447`). Opted out: one alert, latched until
  the next `ApplySuccess` (not the next ok ack: an ok-ack-then-ApplyFailure loop would otherwise alert every
  reconnect), the connection dropped, records kept in history, the normal backoff retrying.
- `connect_and_verify`'s startup `RuntimeError` on a rejection (`:357-358`) honors it too (spec call). The
  drainers' parallel paths are unchanged.

### 10.6 Global crash hooks (R16.6)

- `initialize(..., install_crash_hooks: bool = True)` installs them after
  logging is set up (Rich's `install()` replaces `sys.excepthook`, `logging/setup.py:335,625`), and only
  under `-O`, like the rest of fatal handling, so development keeps Python's tracebacks.
- `sys.excepthook` and `threading.excepthook` route to `_handle_fatal`, passing the exception explicitly:
  inside `sys.excepthook` (and for a task exception reported from `__del__`) `sys.exc_info()` is empty, and
  `_extract_rich_traceback` (`errors/err_handling.py:112-122`) and `render_exception_image`
  (`errors/traceback_image.py:86-99`) must take the exception instead (`Traceback.from_exception`).
- `threading.excepthook` ignores `SystemExit`, as the stdlib hook does. `KeyboardInterrupt` returns
  silently from both (aeth_ext's exit nudge).
- A public `install_loop_exception_handler()`, called on the running loop (uvicorn creates its own): a
  context with an `exception` is fatal; one without is logged as an error (spec call).
- All three stay quiet once `SHUTDOWN` is set. That also covers exceptions a decorator already reported:
  `_handle_fatal` always starts the shutdown (`:151`), so no per-exception marker is needed (exceptions
  can't be weak-referenced, and `ExceptionTrail` holds none).
- No chaining to Python's default hooks; nothing to stderr.
- TODO #15 (decorators treat the exit nudge as fatal) isn't part of this; the app simply doesn't wrap
  `ui.run` in `handle_fatal_exc_sync`.

### 10.7 The log server as a WireGuard spoke (R16.7, R22)

- aeth_ext's `pyproject.toml` `[tool.docker]` (`:100-115`): `wireguard = true`; re-run setup-project, which
  re-renders compose and the Dockerfile (WireGuard env, `cap_add: NET_ADMIN`, the healthcheck with the
  tunnel file, `wireguard-tools`).
- Compose: `WG_TOLERATE_DISCONNECTED=1` (R22) and `restart: unless-stopped` instead of `on-failure:3`
  (spec call: without both, a hub outage ends in devkit-container's give-up and the log server stays down
  for good, taking the VPS's other consumers with it). Keep the `central-log-server` network alias.
- Enroll it in the hub (§12). The clocks reach it at its tunnel address (`LOG_CONN_HOST`, §9).

## 11. devkit-container changes (R18)

devkit-container 2.2.0 today (`D:\SFT Software Projects\SFT Workspace\devkit-container`; Rust; tests with
`cargo test`; released with `uv run devkit release <bump>`, which needs the Windows wheel too). Code
comments citing "spec N" point at two older documents; new code cites "timeclock design §11.n". The
template is rendered by aeth-devkit's setup-project from the locked devkit-container, so projects pick up
template changes only when they re-run `setup-project` or `docker-pin` after bumping the lock.

### 11.1 Trixie, and the `builder-setup` and `builder-cached` windows (R18.2, blocking)

- `python/devkit_container/template.Dockerfile`: both `FROM` lines become
  `ghcr.io/astral-sh/uv:python3.14-trixie-slim` (lines 4 and 63 today). No per-project setting.
- Insert `# !window builder-setup:` / `# !end builder-setup` after the `git` install (line 19) and before
  the manifest clone (line 21), with a one-line comment: system packages that dependencies need to compile;
  repo files aren't available here (the clone comes later), so a window that needs one `COPY`s it from the
  build context. Window names are free-form, so aeth-devkit needs no change (it already keeps windows,
  ≥ 15.1.1); the template's smoke helper (`tests/common/mod.rs:243-276`) is unaffected by comment markers.
- Insert `# !window builder-cached:` / `# !end builder-cached` right after the builder stage's
  `WORKDIR /app` and before its `ARG GIT_TAG` / `ARG GIT_REPO` lines (R18.2, owner 2026-10-26), for heavy
  steps that don't depend on the release. An `ARG` declared above a `RUN` becomes part of that `RUN`'s
  environment and so of its cache key, so every new tag re-runs every `RUN` below it, used or not; commands in
  this window see neither argument and stay cached across releases (verified with a two-build test).
- Docs: `README.md` says "two `# !window` regions" (line 14); the `todo.md` Rust-toolchain item names
  bookworm.
- Release as a minor version. Other projects move to trixie on their next re-render; tunnel-probe's final
  window hard-codes Microsoft's `debian/12` apt repo and will need `debian/13` then (§14, not part of this
  work).

### 11.2 Permanent `WG_TOLERATE_DISCONNECTED=1` (R18.1)

- **Health check.** In wireguard mode the compose health check also requires a fresh tunnel heartbeat
  (`compose.template.yaml:48-60`), so a clock that boots or restarts while the hub is unreachable is
  unhealthy, and the kiosk, which waits for a healthy app (R14.4), never starts: the screen stays blank
  through the whole outage while the app could punch offline. With `WG_TOLERATE_DISCONNECTED=1` in its
  environment, the `healthcheck` subcommand (`src/healthcheck.rs`) treats the tunnel heartbeat file as
  satisfied and judges the app's heartbeat alone (R18.1). The supervisor's pings still report a stale
  tunnel to healthchecks.io, so a technician is still told.
- The Disconnected repair logs a line on every poll (`supervisor.rs:351,356`), about 2,900 a day offline,
  into `devkit-container.log`, which is never rotated. Log the first repair of a disconnection, then at most
  once an hour, plus the reconnect.
- `README.md` (lines 79-80, 149) presents tolerate as an emergency switch; document permanent use.
- Configuration only, in the clock's compose: `WG_HANDSHAKE_TIMEOUT_SECS=10`, so an offline boot delays
  the app by seconds, not a minute.

### 11.3 LAN peers (R18.3, R28)

Fetched mode only (environment mode has no peer table; spec call). The bundle a spoke fetches already holds
every row of the hub's peer table; what's missing is which rows are this spoke's LAN peers. A direct peer's
`/32` overrides the hub route in both directions, so only clocks may become direct peers, never the log
server or the backend.

- **Selector** (R28.5): a new optional per-row field in the hub's table, `lan` (e.g. `lan = "office"`). A
  spoke whose own row has one peers directly with every other row sharing the value. Only clock rows get
  one. Spokes on older versions ignore it: the Rust parser reads only the fields it knows
  (`bundle.rs:78-107`), camera-footage-fs reads the cache with plain lookups, and the Windows spoke agent
  reads `.conf` files, which don't change. `schema` stays 1.
- **Settings** (new env vars; not `WG_PEER_*`, a prefix reserved for environment mode and refused alongside
  `WG_HUB_URL`): `WG_LISTEN_PORT` (fixed listen port, set in `link_up_with_key` so `down_up` keeps it; the
  compose `ports:` mapping must match).
- **Peer set**: `select` / `Effective` (`bundle.rs:291-366`) gain the LAN-peer set, included in equality
  and `diff`, so a hub release that enrolls a clock re-applies instead of reporting "no change for this
  spoke". `apply_commands` adds each as `wg set wg0 peer K allowed-ips <addr>/32 persistent-keepalive 25`
  with no endpoint (the hub's `10.8.0.0/24` route already covers the address); `reapply_commands` adds and
  removes; `down_up` re-adds them with their last confirmed endpoints, so the `DownUp` repair, which runs
  exactly when the hub is unreachable, doesn't cut clock-to-clock traffic.
- **Health** stays judged by the hub's handshake only. A LAN-peer command failure is its own command kind:
  logged, never Broken, never a hub Reason.
- **Public key**: at boot the entrypoint writes the interface's public key to
  `persisted_data/wireguard/public_key` (0644) for the discovery service.
- **Endpoint file**: `persisted_data/wireguard/lan-endpoints.json`, `{"<public key>": "<ip>:<port>", ...}`,
  written atomically by discovery. The supervisor stats it on its 250 ms wakes and applies an entry only
  when the key is in the LAN-peer set, the address differs from the peer's current endpoint, and the peer
  has had no handshake in the last 180 s (a live session roams by itself, and WireGuard doesn't re-handshake
  just because an endpoint changed). It runs `wg set wg0 peer K endpoint <ip>:<port>`, then watches
  `latest-handshakes` for K on the 250 ms wakes, never blocking the loop: a handshake newer than the set
  within 5 s confirms it; otherwise it reverts to the previous endpoint, or, if there was none, removes and
  re-adds the peer without one. A reverted address is retried only when the file changes again.
- **Discovery** (R28.3): a `devkit-container discover` subcommand, so both halves of the protocol share the
  peer-table code. Run as its own compose service from the app's image with `network_mode: host`
  and the app's persisted directory mounted. Every 30 s it broadcasts
  `{"key": "<own public key>", "endpoint": "<host LAN IP>:<WG_LISTEN_PORT>"}` over UDP to
  `255.255.255.255:51822` (spec call), listens on the same port, ignores its own key and keys that aren't
  its LAN peers in the cached `peers.toml`, and rewrites the endpoint file when the map changes. Entries
  don't expire; a wrong one is caught by the confirm/revert.
- **Tests**: unit tests for the peer set and diff, the endpoint file, and the confirm/revert state machine.
  Fetched-mode smoke tests need new releases in the private fixture repo `wireguard-hub-smoke`, which the
  owner creates.

## 12. WireGuard hub configuration

wireguard-hub 1.5.2 (`D:\SFT Software Projects\SFT Workspace\wireguard-hub`). Its owner-uncommitted
`TODO.md` entry (drop the "NET_ADMIN only" line from the hub design) is left alone.

The hub's design doc (`docs/superpowers/specs/2026-09-14-hub-fetched-peer-config-design.md`) says every
spoke talks only to the hub and only the hub publishes ports; this design supersedes both.

- **`lan` field** (R28.5): add `lan` to `PEER_KEYS` (`src/wireguard_hub/peers.py:24`) as an optional
  string, with tests; `render_conf` ignores it, so the Windows spokes' `.conf` files don't change. Release.
- **Peer rows** in `src/wireguard_hub/peers.toml`: one per clock (named `timeclock-system-<INSTANCE_ID>`,
  with `lan = "office"`), `timeclock-backend`, and aeth_ext's central log server (R16.7), with `/32`
  addresses allocated from `.24` up as the README describes. A row's own `endpoint` field is the hub
  endpoint that spoke dials, not the spoke's address; clocks leave it unset. timeclock-dev's row,
  `timeclock-system-dev`, gets `lan = "dev"`, never the production clocks' value, so its test punches and
  prints can't reach a real clock over the LAN (spec call); a second test device for LAN tests shares
  `"dev"`.
- **Forward rules** in `rules.v4`, one line per clock per flow (the tests require every CIDR to be a peer
  or hub address): clock → log server TCP 9020 (R22), clock → backend TCP 5432 (R26.1).
- Each clock's private key, hub URL, repo and token go in that clock's Coolify environment (§9).

## 13. Build order and verification

Approved order. Plans for the dependency repositories are short and written for subagents, unless a change
is small enough that subagents would cost more than they save. Each repository's own `AGENTS.md` and
`.claude/CLAUDE.md` apply there; on a branch, run targeted tests while working and the full suite once at the
end (`AGENTS.md`).

1. **In parallel:**
   - devkit-container §11.1 and §11.2, released together (the clock image needs 11.1);
   - aeth_ext §10, released as 10.0.0;
   - timeclock-schema (§4): create the GitHub remote, then the repo with `uv init --no-workspace` and
     setup-project, the models, shift logic, rules registry and `0001_initial.sql`, release 0.1.0; add it
     here as a submodule at `schema/`;
   - wireguard-hub: rows for the backend, the log server and the first clock, and their forward rules (§12);
     the log server's spoke switch (§10.7) once its row exists.
2. **timeclock-backend** (§8), deployed on the VPS. With the database up, install DBeaver Community on the
   owner's workstation and set up its SSH connection to the VPS (R30.2).
3. **The clock app and kiosk** on timeclock-dev (§5–§7, §9.1, without `discovery`), against the test
   database (R26.6). The kiosk files start
   from Appendix A. Once they're committed here, clean up the test leftovers: the `kiosk-test` container,
   the stopped `tc-app`, the `kiosk-test` and `apptest` images, the `tc-net` network,
   `/root/fp-test-template.bin` and `/tmp/fpdata/`.
4. **LAN sharing:** devkit-container §11.3 with `discover`, the hub's `lan` field (§12), the `discovery`
   service and clock-to-clock sharing (§6.7). The clocks already work through the hub without it.

Verify on the hardware, at the step that makes each possible (not in long VM runs; say what's being
installed before installing it):

- the patched 1.94.100 build enrolls and identifies on the 4500, and a reader left waiting for over 10 minutes
  never reports `TOO_HOT` (step 3);
- the reader autosuspends once the app closes it (`power/runtime_status` reads `suspended`) while the process
  keeps its `FPrint.Context` (step 3);
- `LISTEN`/`NOTIFY` works through the relay over WireGuard (steps 2–3);
- a Coolify Compose deployment of app and kiosk on timeclock-dev, and each kiosk lockdown item on the
  touchscreen (step 3);
- with the uplink unplugged: punches are recorded, the kiosk stays up, and everything syncs on reconnect
  (step 3);
- two clocks share punches and registrations with the hub unreachable (step 4; needs a second device, e.g.
  the EliteDesk 800 G1).

## 14. Follow-ups and known issues

- libfprint fork with prebuilt binaries, if a fresh compile is still too slow despite the cached window
  (R40.4).
- Once the project is stable, revisit blocking updates and deletes on punches and corrections for every
  account, the owner's included (R38.1).
- Per-clock business hours (R40.3); employee schedules on the action menu (R45.1); a lockout after
  repeated wrong PINs.
- Review whether an employee will ever need more than one group (§4.2). If so: a join table replacing
  `employees.group_id`, a migration, and a precedence rule for conflicting group rules.
- Host settings such as the update policy and reboot time, configured from the database (R42).
- Deferred 1–4: QuickBooks, an employee phone page, reports and exports, database backups.
- aeth_ext TODO #21 (hold-and-resend), which replaces §6.8; TODO #15 (decorators treat the exit nudge as
  fatal).
- devkit-container: cloning submodules in the image build (dropped from this round); restart on unhealthy
  stays declined.
- tunnel-probe's final window hard-codes Microsoft's `debian/12` apt repo and needs `debian/13` on its next
  re-render (§11.1).
- coolify-tunnel's README still names the old `timeclock-tunnel-setup` script; a DNS-only `coolify-tunnel.`
  record could replace `tunnels.sweetfiretobacco.com` for the tunnel.
- Detecting a replugged reader with udevd, as the kiosk does (R14.3).
- Version 2 matcher (R36.2); bozorth3 rebuilds each gallery print's table on every comparison, and caching it
  needs a patch.
- Known: offline totals miss other clocks' punches (R27.1); every deploy blanks the screen (§9.1); short
  numeric PINs can be brute-forced offline from a stolen clock (accepted).
- devkit-templates puts a run-modes comment inside `[tool.coverage.lcov]` when the docker keys already
  exist; left alone.

## 15. Open questions

None.

## Appendix A. Files verified on the hardware (2026-10-26)

From the spikes on timeclock-dev (EliteDesk 800 G3 Mini, Debian 13.7). The kiosk files are the starting point
for `docker/kiosk/`; the reader script is the logic `timeclock-reader-access` ports to Python.

**Kiosk** (R13, R14.2). cage runs Firefox ESR alone under `seatd-launch`, in a privileged container with its
own udevd; no host networking. Touch arrives as real touch events; a replugged touchscreen works again,
including on another USB port. The DBus and `xdg_surface` warnings in its log are cosmetic.

```dockerfile
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    cage seatd udev firefox-esr libgl1-mesa-dri libegl-mesa0 fonts-dejavu-core ca-certificates \
  && rm -rf /var/lib/apt/lists/*
COPY kiosk.sh /kiosk.sh
ENTRYPOINT ["/kiosk.sh"]
```

```sh
#!/bin/sh
set -eu
/usr/lib/systemd/systemd-udevd --daemon
udevadm trigger --action=add
udevadm settle --timeout=10 || true
export XDG_RUNTIME_DIR=/run/kiosk
mkdir -p -m 700 "$XDG_RUNTIME_DIR"
exec seatd-launch -- cage -- firefox-esr --kiosk "${KIOSK_URL}"
```

The spike ran it as `docker run --network tc-net --privileged -v /dev:/dev -e KIOSK_URL=http://tc-app:8080
kiosk-test`.

**Reader access** (R14.7), run as root; without it, libfprint as uid 999 gets "Access denied (insufficient
permissions)":

```sh
# Give the 4500 (05ba:000a) to uid 999; fail startup without it.
found=""
for d in /sys/bus/usb/devices/*; do
  [ "$(cat "$d/idVendor" 2>/dev/null)" = 05ba ] && [ "$(cat "$d/idProduct" 2>/dev/null)" = 000a ] || continue
  node=$(printf '/dev/bus/usb/%03d/%03d' "$(cat "$d/busnum")" "$(cat "$d/devnum")")
  chown 999:999 "$node" && found=1
done
[ -n "$found" ] || { echo "reader-access: fingerprint reader not found" >&2; exit 1; }
```

**App container spike** (superseded by the devkit template, §11.1, but it fixes the package lists):
PyGObject 3.58.1 from PyPI builds on Python 3.14.9 in `ghcr.io/astral-sh/uv:python3.14-trixie-slim` in
about 12 s with `gcc pkg-config libgirepository-2.0-dev libcairo2-dev`; runtime needs
`libgirepository-2.0-0 libcairo2` plus libfprint and its typelib. It fails on bookworm (PyGObject ≥ 3.51
needs GLib ≥ 2.80). NiceGUI runs uvicorn on uvloop. The reader was reachable as uid 999 with
`-v /dev/bus/usb:/dev/bus/usb --device-cgroup-rule='c 189:* rmw'` plus the script above.
