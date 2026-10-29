# Timeclock system design (2026-10-26)

The design for version 1 of the fingerprint time clock: the clock app (this repository), timeclock-backend,
the shared schema package, the patched libfprint, and the changes this needs in aeth_ext and
devkit-container. Implementation plans are written straight from this document, including the plans for the
dependency repositories, which get no spec of their own.

- **R*n*** cites item *n* of the Decided list in [the requirements doc](2026-10-23-timeclock-requirements.md),
  each approved by the owner. Where this spec restates one, the requirements doc wins on any conflict, except
  where a later owner decision supersedes it: R23.6, R30 and R30.1 (in part), R31.1 and R41.3 (owner, 2026-10-27);
  R27.3 and R27.4 (in part: fingers, and clocks removing prints; owner, 2026-10-28,
  [the fingerprint menu design](2026-10-28-fingerprint-menu-design.md)); R29's server address (§9.3).
- **(owner, 2026-10-27)** marks a later owner decision. **(spec call)** marks a decision made while writing
  this spec, and **(my call)** one made by the coordinating session on 2026-10-27; neither is the owner's,
  and the owner reviews both. A builder may revisit either when the code argues for it; anything else here is
  settled.
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
- PIN login with a manager override (R23), fingerprint registration from the clock (R23.2, R23.4), and
  PIN-only login for admins and fingerprint-exempt employees (§6.4; owner, 2026-10-27).
- A fingerprint menu: re-register, add or remove one of your own prints, each change approved by an admin
  (§6.4; owner, 2026-10-28).
- An admin menu on the clock, reached from an admin's own punch menu, whose one item adds an employee
  (details, PIN, optional fingerprint), working offline (§6.4; owner, 2026-10-27).
- Optional `phone` and `email` on every employee, for the later notification system (§4.2, §14).
- One auditable hours function: worked time, paid break time and unpaid time, with a daily paid break
  allowance (§4.5; owner, 2026-10-27).
- The weekly-hours display after a punch (R31), with a notice once the week passes the weekly threshold,
  through an alert system that later alert types extend (§6.4; owner, 2026-10-27).
- Offline punching and registration with later sync (R27); clock-to-clock sharing on the LAN (R28).
- The daily automatic clock-out of forgotten shifts, recorded run by run (R41, §8.4).
- Business hours, with the reader asleep outside them and tap-to-wake (R40).
- The rules framework with no rule kinds yet (R34).
- Deployment through Coolify, crash alerts, central logging, healthchecks.io monitoring.

**Not in version 1:** reports and exports (payroll exports CSV by hand with SQL; Deferred 3), QuickBooks
(Deferred 1), an employee phone page (Deferred 2), database backups (Deferred 4), an admin interface beyond
adding employees at a clock (the owner edits everything else in an SQL editor, R30), employee schedules
(R45.1), per-clock business hours (R40.3), the parallel version 2 matcher (R36.2), any rule kind (R34), host
settings configured from the database (R42), the notification system (SMS and email, §14). Deferred with
the admin menu (owner, 2026-10-27; `TODO.md`): per-clock settings (UI scaling first), an on-screen
keyboard, sharing new employees over the LAN before LAN sharing (§13 step 5), toggling `fingerprint_exempt`
from a clock, a resolution tool for an employee the database refused (§6.6), a PIN length rule, editing or
deactivating employees from a clock, and warnings about possible duplicate people (names repeat; spouses can
share a phone or email).

**Anti-goals.** Version 1 fails, even if it works, if:

- a punch is lost: to a restart, an outage, a crash, or a re-send that overwrites or resurrects a record;
- a punch or fingerprint ends up filed under a different person, including a clock-created employee's (§6.6);
- two clocks, or a clock and the database, hand out the same employee number (§6.4);
- an admin is created from a clock (admins create employees and managers only; owner, 2026-10-27);
- the weekly hours threshold blocks or restricts anyone (owner, 2026-10-27);
- the clock depends on the tunnel, the VPS or the log server to start or keep running;
- a fingerprint image or a plain PIN is ever stored, logged, alerted or shown;
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

The submodule pins lag (`backend/` at v0.3.0, `schema/` at v0.4.0, 2026-10-28): work on those repositories in
their own checkouts beside this one (`../timeclock-backend`, `../timeclock-schema`). The clock installs
timeclock-schema from SFTPyPI, never from the submodule.

## 4. Data model (timeclock-schema)

### 4.1 Package

`timeclock_schema` is a plain library (no Docker, no `[tool.docker]`), depending only on pydantic. Both the
clock and the backend pin it by version from SFTPyPI; releases go through `poe release` (R37.5). It holds:

- `models`: a pydantic model per table row, per LAN message (§6.7) and for the notification payload (§4.4).
  Serialized fingerprint templates are `SecretBytes` everywhere (R15).
- `shifts`: the read-side logic, pure functions over punches and corrections (§4.5), the hours function
  among them. The clock uses it for its menu, guard and totals; the backend uses it for the automatic
  clock-out. It exists once, in Python; nothing reimplements it in SQL.
- `rules`: the rule-kind registry and the row model (§4.6).
- `migrations/NNNN_<name>.sql`: package data, applied in order by the backend (§8.3). The production
  database holds real rows (2026-10-28), so a released migration file is never edited and every change goes
  in a new file; `0002_fingerprint_fingers.sql` (schema 0.6.0) is the first.

Timestamps are aware UTC `datetime`s in Python, `timestamptz` in Postgres and ISO 8601 UTC text in the
clock's SQLite (R24). Times of day (business hours, auto clock-out) are `datetime.time` / Postgres `time`
with no date and no zone (R41.3), interpreted in `settings.tz` (R41.4). Zones and DST use the standard
library's `zoneinfo`, whose tzdata matches PostgreSQL's; python-dateutil isn't used, unless calendar-month
arithmetic is ever needed (its `relativedelta`; my call).

### 4.2 Tables (schema `public`)

Data tables live in `public`, configuration in `config` (R41.3), and the backend's own records in `backend`
(§4.3). Column lists are the minimum; the builder adds indexes as needed (at least `punches (employee_id,
at)`).

**`groups`** (edited in place): `id bigint identity PK`, `name text unique`. The first migration seeds one
group, `default`, so employees can be added at once (spec call). An employee belongs to exactly one group
(spec call; R34 doesn't say whether membership is single or multiple, and single keeps rule precedence
trivial).

**`employees`** (edited in place, R30.1): `id bigint generated by default as identity PK`, `first_name text`,
`middle_name text null`, `last_name text` (schema 0.5.0; the clock shows first and last, `punch_log` all three),
`pin_username text unique` (digits only, check constraint), `pin_hash text` (`crypt(pin, gen_salt('bf'))`,
R23.1; a check accepts only a bcrypt hash, so a plain PIN typed by hand is refused), `role employee_role
default 'employee'`, `active boolean default true`, `group_id bigint FK groups default <default group>`, `phone text null`, `email text
null`, `fingerprint_exempt boolean default false`, `updated_at timestamptz` (set by trigger). Employees are
referred to everywhere by `id`, never by name. An employee who leaves is set `active = false`, since their
punches reference the row: inactive employees can't log in or punch, and their prints drop out of matching
(spec call).

- **`role`** (owner, 2026-10-27; replaces R23.6's `is_manager`): enum `employee_role`, declared lowest to
  highest, `employee` < `manager` < `admin`, so SQL can test `role >= 'manager'`. An admin may do everything
  a manager may. Managers and admins approve overrides; nobody overrides for themselves (§6.4).
- **`id`** is `BY DEFAULT`, not `ALWAYS`, so a clock can supply the number it chose (§6.4); hand-added rows
  still take the next value from the sequence. The username stays unique; it and the primary key are the
  only duplicates the database refuses (owner, 2026-10-27).
- **`phone`**: `+1` followed by ten digits (owner: US numbers, one stored format); **`email`**: something,
  `@`, something, a dot, something (owner: format checked; the pattern is my call). Check constraints enforce
  both, so hand-typed rows follow them too (my call).
- **`fingerprint_exempt`** (owner, 2026-10-27; the name is my call, so it isn't confused with
  `fingerprint_bypasses.pin_only`): the employee logs in by PIN alone, with no fingerprint step and no override (§6.4).
  Changed only in the database for now; a clock toggle comes later (`TODO.md`).

**`fingerprints`** (plain rows; the owner deletes one to force re-registration, R27.3, R27.4): `id uuid PK`
(made by the clock), `employee_id bigint FK employees ON DELETE CASCADE`, `finger finger` (enum, `left_thumb` …
`right_little`), `template bytea` (libfprint's serialized `FpPrint`), `registered_at timestamptz`,
`instance_id text`, `db_login text default session_user`. At most one per finger per employee, `UNIQUE
(employee_id, finger)`; clocks add, replace and remove prints through the fingerprint menu (0002; owner,
2026-10-28, [its design](2026-10-28-fingerprint-menu-design.md)).

**`fingerprint_changes`** (append-only, 0002): one row per print added, replaced or removed at a clock: `id uuid
PK`, `employee_id`, `finger`, `action` (`add` | `replace` | `remove`), `approved_by bigint FK employees null`
and `pin_only boolean null` (both null exactly when no admin approved, which only an `add` may be), `at`,
`instance_id`, `received_at`, `db_login`. Inserted by the clock and synced like bypasses; clocks don't mirror it.

**`db_login`** (on `fingerprints`, `punches` and `fingerprint_bypasses`; R26.2): the database
login that inserted the row, which identifies the clock without trusting its `INSTANCE_ID`. The clocks'
column-level `INSERT` grants leave it out (§4.4), so a clock can't set it and the default always applies. A mismatch with
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

**`fingerprint_bypasses`** (append-only, R23.5; renamed from `overrides`, owner, 2026-10-27): one row per
PIN login that bypassed a fingerprint, so daily reports can list them later. `id uuid PK` (made by the
clock), `punch_id uuid FK punches null` (the punch it authorized; null for a login that punched nothing),
`employee_id bigint FK`, `manager_id bigint FK null` (the approving manager or admin; null when nobody
approved), `reason bypass_reason`, `pin_only boolean null` (an override's manager authorized by PIN alone;
null otherwise), `at timestamptz`, `instance_id text`, `received_at timestamptz default now()`, `db_login
text default session_user`. `reason` (owner, 2026-10-27):

- `override_no_fingerprint`, `override_fingerprint_skipped`: a manager override (§6.4); `punch_id`,
  `manager_id` and `pin_only` are set. Written with its punch, in the same local transaction and the same
  sync transaction. An override that's never used to punch isn't recorded (spec call).
- `admin_pin_login`: an admin logged in by PIN, including the Admin button's PIN reconfirm and a PIN 30 s
  rescan (my call).
- `exempt_pin_login`: a `fingerprint_exempt` employee logged in by PIN, including a PIN 30 s rescan.

A check ties the nullable columns to `reason`. Inserted by the clock and synced like punches, so it works
offline; clocks don't mirror it.

**`rules`** (edited in place, R34): `id bigint identity PK`, `kind text`, `params jsonb default '{}'`,
`group_id bigint FK null`, `employee_id bigint FK null` (check: exactly one is set), `on_violation
rule_action` (`refuse` | `warn` | `accept`, R34.2), `updated_at timestamptz` (set by trigger); `params`
must be a JSON object.

**`schema_migrations`**: `version int PK`, `name text`, `applied_at timestamptz` (§8.3).

**View `punch_log`** (R30.1): every punch joined to its employee's current name, with the punch's latest
correction (action, new values) and its `db_login` beside it. It's what the owner reads and exports to CSV; it applies no
shift logic.

### 4.3 Tables (schema `config`, edited in place; schema `backend`)

**`config.business_hours`** (R40.3): `id bigint identity PK`, `weekday smallint` (ISO, 1 = Monday … 7 =
Sunday), `opens time`, `closes time`, checks `opens < closes` and `closes < '24:00'` (the settings' two times
are checked `< '24:00'` too). Nothing is unique per weekday; a weekday with
no rows is closed. Seeded empty, so a new install's reader sleeps until hours are added (tap-to-wake still
works; spec call).

**`config.settings`** (R41.3): one row (`id boolean PK default true check (id)`), seeded with the defaults:

- `auto_clock_out_enabled boolean default true`; `auto_clock_out_run_time time` and
  `auto_clock_out_stamp_time time`, both default 23:00 in the backend's local time (owner, 2026-10-27);
  `auto_clock_out_frequency` (enum, `daily` its only value, room for later ones).
- There is no rule that the stamp time be at or before the run time, anywhere (owner, 2026-10-27; supersedes
  R41.3's validation): the owner may set a later stamp, and §8.4 says how it runs.
- `lunches_enabled boolean default true`, `breaks_enabled boolean default true` (owner, 2026-10-27):
  business-wide; off hides the start button, and the end button stays (§6.4). Per-group switches may come
  before the first release (`TODO.md`), so nothing ties these to other columns.
- The daily paid break allowance (default 30 minutes) and the weekly hours alert threshold (default 40 hours)
  (owner, 2026-10-27; §4.5, §6.11). Column names and types are left to the builder.
- `subtract_lunch` and `subtract_breaks` are removed (owner, 2026-10-27; supersedes R31.1): the hours
  function's rules replace them (§4.5).

**`backend.auto_clock_out_runs`** (§8.4; owner's design, 2026-10-27): one row per scheduled run,
`id bigint identity PK`, `scheduled_for timestamptz` (the run's instant), `scheduled_tz text` (the zone it
was projected in), `stamp timestamptz`, `ran_at timestamptz null` (set by the run's own transaction) and
`closed int null` (its clock-out count; check: `ran_at` and `closed` are both null or both set). Run history
is stored as UTC instants; the scheduled time keeps its zone (owner).

- **Projection** (SQL, `backend.project_auto_clock_out_runs()`): with `daily` the only frequency, one row
  per local day from today to today + 6 in the backend's zone, each from that day's run and stamp times
  through PostgreSQL's own `AT TIME ZONE` (owner; no custom resolver). On a DST change a skipped 02:30 runs
  at 03:30 and a repeated time at its second occurrence; the same holds for the stamp. A day that already
  holds a row (run, or missed and awaiting catch-up) gets no new one; today's row is projected even when its
  time has passed, so the backend runs it at once. Nothing is projected without a settings row or a zone, or
  while disabled.
- **Reprojection:** every `config.settings` change, and every new or changed zone, prunes the unrun future
  rows and reprojects them in the same transaction (triggers). Rows whose time has passed are history and
  never change. The backend also calls the projection at the start of every pass (§8.4), extending it as
  the days pass. An advisory lock serializes projections; runs don't take it.
- **`backend.environment`** (`id boolean PK`, `tz text`): one row, the backend's `TZ`, written at every
  start (§8.4). Nothing is projected before the first.

### 4.4 Roles and notifications

Two databases in the one server, identical in layout (R26.6): `timeclock` (production) and
`timeclock_test` (everything timeclock-dev and any manual testing touches). Everything in §4 exists in both.

- **`postgres`** (superuser, password from `POSTGRES_PASSWORD`): owns everything, runs migrations, and is
  what the owner uses in the SQL editor (spec call).
- **`timeclock_clock_access`** (no login; created by the first migration if absent, since roles are
  server-wide): `SELECT` on `employees`, `groups`, `fingerprints`, `punches`, `punch_corrections`, `rules`,
  `config.business_hours`, `config.settings`; column-level `INSERT` on `punches`,
  `fingerprint_bypasses` and `fingerprints`, every column except `db_login`, `received_at` and
  `punches.automatic` (only the backend makes automatic clock-outs); column-level `INSERT (id, first_name,
  middle_name, last_name, pin_username, pin_hash, role, group_id, phone, email)` on `employees`, so
  `fingerprint_exempt`, `active` and `updated_at` take their defaults; `DELETE` on `fingerprints` and
  column-level `INSERT` on `fingerprint_changes` (0002); `USAGE` on schema `config`; nothing on schema
  `backend`; nothing else (R26.2). `SELECT (id)` on `fingerprint_bypasses` and `fingerprint_changes` lets
  `ON CONFLICT (id) DO NOTHING` infer its target. The database refuses a clock login inserting `role = 'admin'`, so a modified clock can't create an
  admin either (my call; how, likely a trigger checking the inserting role, is left to the builder).
  Migrations grant to this role, so the same plain SQL serves both databases.
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
2. Sort by effective `at`. A shift runs from a `clock_in` to the next `clock_in`.
3. Walk the events with this state machine; an event that isn't valid from the current state is an
   anomaly: skipped for state and totals, reported to the caller (payroll sees it in `punch_log`). This is
   R41.2: of two consecutive punches of the same type, the oldest counts, automatic or not. A late-synced real
   `clock_out` after an automatic one is skipped, and an automatic one after a real one is skipped.

| State | Valid punches | Next state |
| --- | --- | --- |
| off | clock_in | working |
| working | clock_out / break_start / lunch_start | off / on break / at lunch |
| on break | break_end / clock_out | working / off |
| at lunch | lunch_end / clock_out | working / off |

A `clock_out` from a break or lunch ends it implicitly (spec call: it lets the automatic clock-out close
a shift left on break, and spares an employee a second tap).

Outputs: current state, valid next punches and the open shift (if any). The clock passes only punches stamped
at or before now when it works out state and which punches it offers (owner, 2026-10-27), since the
automatic clock-out may be stamped after it runs (§8.4). With step 3's rule, a hand clock-out before a
future-stamped automatic one wins, and the automatic one becomes the ignored repeat. Accepted: an employee
who clocks out and back in before the stamp has that new shift ended at the stamp.

**Hours** (owner, 2026-10-27). Every rule and formula for totalling hours lives in one Python function in
this module, so it's easy to audit; nothing else totals hours.

- **Input:** any number of one employee's punches with their corrections, the settings, the zone (local days
  need it), the range to total, and `now`, until which an open shift, break or lunch runs. A shift crossing
  the range's edge is split there, so a week splits a shift at Monday 00:00 (R41.5).
- **Output**, three separate totals: worked time (shift time less every lunch and break), paid break time
  (not included in worked time), and unpaid time (lunch, plus break time over the allowance).
- **Rules:** a punch stamped after `now` doesn't count yet (owner, 2026-10-27; §8.4 may write one); the
  function applies this itself, so the clock's state filter above and the totals can't disagree (my call).
  Lunch is always unpaid. Breaks are paid up to the daily paid break allowance (§4.3); break time
  over the day's allowance is treated as lunch, so unpaid. A day is the local calendar day, and a break
  counts toward the day it started.
- **Separate from alerts:** the function computes totals; the alert system (§6.11) and the screens read them.
- **Non-goals:** delivering any total by SMS or email (§14), payroll exports (Deferred 3).
- **Anti-goal:** a second implementation of any hours rule, in SQL or in the clock.
- **Left to the builder:** the function's name, signature and return type; how a range edge clips a break
  whose day lies partly outside the range; tests.

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

- Debian 13, no desktop (R3.1). Production clocks install from `image/production/` (it asks the clock's name
  and number, erases the internal drives, and mirrors two drives with RAID 1;
  `docs/production-clock-setup.md`); timeclock-dev from `image/dev-device/`. Wired Ethernet is the norm; wifi
  works when a clock can't stay wired.
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
  exponentially, 1 s up to 30 s, reset by a successful capture. A reader failing continuously logs a warning
  on the streak's first failure and is fatal, as a lost reader is, once the streak passes about 3 minutes
  (owner, 2026-10-27). `FP_DEVICE_ERROR_TOO_HOT` (shouldn't happen
  with the patch) → 60 s, the reader line red with "Reader paused", and an alert, since it means the patch isn't
  working on that clock (owner, 2026-10-27). After any capture, pause 1 s before re-arming (owner, 2026-10-27: down
  from 2 s), so the tail of one press isn't read as the next (seen at 0.54 s in the spike). `identify_sync` returns
  only after the finger lifts, but a call cancelled while a finger rests after its match (the re-arm, a gallery or
  mode change) returns before the lift; the pause keeps that finger from being read again at once. After an early
  result (below) a mode change doesn't cut the pause short, and registration waits for a finger already down (the
  approving admin's) to lift before it starts, so that finger is never enrolled as the new print's first press.
- **Finger status** (`notify::finger-status`, identify and enroll alike): libfprint's flags, finger present and
  waiting for a finger, are posted on every change; a call that ends with a finger still reported clears it. In
  libfprint 1.94.100 an enroll waits for the lift between presses and reports it, but completes before the last
  press lifts and doesn't report that lift. Each registration press is posted with whether it counted (a retry
  doesn't). The once-a-minute re-arm's millisecond gap isn't shown.
- **Login results arrive early** (owner, 2026-10-29): `identify_sync` returns only after the finger lifts (the device
  deactivates on finger-off), so the reader passes libfprint's match callback, which `fpi_device_identify_report`
  calls the moment the scan is matched, and the scanner posts the result from there: the screen acts while the finger
  is still down. The result is posted once; a cancel after it (a mode change, closing time, the re-arm) isn't a
  failed read.
- **Reader line** (owner, 2026-10-29, [the QoL design](2026-10-29-clock-qol-design.md) §4; replaces the 2026-10-27
  icon): bottom-left on every screen, icon, status, then message; icon 48 px, text 20 px. It reports the reader
  alone; each screen keeps its own error line for who scanned, roles, PINs and refused punches. Icon and status are
  live from the reader, never on a timer but the failed read's 1 s floor:

  | Reader | Icon | Status |
  | --- | --- | --- |
  | Waiting for a finger | Yellow | "Ready" |
  | Finger down, being read | Blue | "Keep your finger down" |
  | Read succeeded, finger still down | Green, until the lift | "Lift your finger" |
  | Login scan read but matched nobody, finger still down | Orange, until the lift | "Lift your finger" |
  | Read failed | Red, until the reader waits for a finger again, at least 1 s | "Couldn't read that, try again" |
  | Paused itself (too hot) | Red | "Reader paused" |
  | Not taking scans (the gap after a scan) | Dim | None |
  | Asleep outside business hours (§6.3) | Dim | "Tap to wake the reader" |

  The message lingers. A failed read's text moves there when its red ends, and clears on a screen change, a
  successful read, a newer message, or, on the idle screen only, after 15 s. "Scan successful" (any read that
  matched someone, or any counted registration press) and "Not recognized, try again" (a login scan that matched
  nobody) last 3 s or until replaced; a screen change clears them only onto a screen asking for a finger (idle, the
  finger prompts, the manager, admin and approval prompts, registering). Typing on a pad or in the form isn't a
  screen change. Messages show only where a finger does something: idle, punch menu, confirmation, the finger
  prompts, every PIN pad, registering.
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
the scanner is in ready mode; outside, closed. A scan in progress at closing finishes first (R40.2): the reader stays
open while a finger is down.

Outside business hours the idle screen says "Tap to scan". A tap, or anything else that needs a scan
(the PIN path's fingerprint step, a manager override, registration), opens the reader for 30 s, then
closes it again (R40.2).

### 6.4 Screens and flows

The UI is one NiceGUI page for the kiosk; it carries the company's colors (yellow `#F9E60D`, red
`#ED1A2C`, near-black `#161514`, white) and logo (R39). The logo SVGs are read at runtime from
`persisted_data/branding/` on the device (R39.1; a missing file renders the page without it and logs a
warning; spec call). Large touch targets, landscape layout from 1024×600 up; no particular screen model
(R7.1). Typed input uses an on-page number pad that also accepts keyboard input (R25); text (the add-employee
form) is typed on a plugged-in keyboard, which must work when plugged in at any time, with no restart of the
app or the kiosk (owner, 2026-10-27; verified on the clock, and the kiosk image fixed if hotplugged input
isn't picked up). A keyboard's number pad always types digits: the kiosk sets the XKB option `numpad:mac`, since cage
starts every keyboard with Num Lock off, including one that drops off USB and comes back (owner, 2026-10-29; the Num
Lock key and light still toggle but no longer matter). The on-screen keyboard is deferred (`TODO.md`). The release
version shows small and dim in the very bottom-left corner, under the reader line (owner, 2026-10-29). The page
turns off text selection and
the long-press menu (R13.2). On the kiosk only (`KIOSK_URL` carries `?kiosk`), it hides the mouse cursor until a
mouse moves, and again on a touch or after a minute without mouse movement; touch never shows it. Other browsers,
such as one through an SSH tunnel, always show it (owner, 2026-10-27). Every screen but idle returns to
idle after 2 minutes without a touch or keystroke, discarding anything unsaved (R45.4). Hours are shown
exact, truncated (never rounded) to two decimal places (owner, 2026-10-27). "Hours this week" is worked plus
paid break time (owner, 2026-10-27; §4.5).

- **Idle.** Logo, date and time, "Place your finger on the reader" (or "Tap to scan" after hours), a "Use
  PIN" button, and a database status icon: green while connected, red while not, with a small timestamp of
  the last contact with the database. A scan's result shows on the reader line (§6.2), not in the title (owner,
  2026-10-29); the title still changes for its other news.
- **Action menu** (R45). The employee's name, current state ("Clocked in since 8:02 AM"), hours this week,
  and one button per valid punch (§4.5), less a lunch or break start while `lunches_enabled` or
  `breaks_enabled` is off; an end button always shows (owner, 2026-10-27). Clock-in always takes a tap
  (R45.1); the layout leaves room for a scheduled time later. A punch button tapped more than 30 s after the
  scan asks for a new scan that must match the same employee; a scan matching someone else opens their menu
  instead, with no punch (R45.2). A new scan while the menu is open switches to that person's menu. A
  `refuse` rule shows its message here and records nothing.
  - **Admin button** (owner, 2026-10-27): on the menu of an employee whose role is `admin`, near the bottom,
    well away from the punch buttons. Other roles never see it. Tapping it asks the admin to reconfirm by
    their own fingerprint or PIN before the admin menu opens; otherwise it follows the menu's normal rules.
- **Confirmation** (R31). "Clocked out at 5:01 PM", hours this week, any `warn` rule messages, any alert
  notice (§6.11); back to idle after 5 s, or at once on a double tap anywhere (owner, 2026-10-27). Hours this
  week use the local copy, so offline they miss other clocks' unsynced punches (R27.1).
- **PIN login** (R23). Username, then password, on the number pad; verified against the local `pin_hash`
  with `bcrypt` off the event loop (spec call: the `bcrypt` package checks pgcrypto's `$2a$` hashes). A typed
  PIN is a `SecretStr` until it's hashed or checked; usernames are plain (owner, 2026-10-27). No lockout
  after wrong PINs, though for admins and `fingerprint_exempt` employees a guessed PIN alone is enough to
  punch (§14 lists the lockout as a follow-up). Then:
  - admin, or `fingerprint_exempt` → straight to the action menu, with no fingerprint step, no "register
    your fingerprint" prompt and no override (owner, 2026-10-27). Each such login is recorded in
    `fingerprint_bypasses` (§4.2). An exempt employee's 30 s rescan asks for the PIN again, and they may
    still enrol a print (both my call). An admin's 30 s rescan takes their fingerprint or their PIN (my
    call); a PIN rescan is recorded like the login;
  - no registered print → "Register your fingerprint" or "Manager override" (R23.2);
  - registered print → "Scan your finger" (confirm mode; must match this employee) or "Manager override"
    (R23.3).

  **A finger on every PIN pad** (owner, 2026-10-29): on any PIN pad, a recognized finger does what it would have
  done on the screen before Use PIN, discarding typed digits. The login pad opens that person's menu; the admin
  reconfirm opens the admin menu for the admin's own finger, anyone else's menu otherwise; the rescan records the
  punch for the person's own finger, anyone else's menu otherwise; the manager override and admin approval take a
  manager's or admin's finger and otherwise go back to their prompt with "A manager must authorize this" or "An admin
  must approve this", as a PIN would; the Fingerprints login opens that person's options (changes still need an
  admin's approval). The new employee's PIN pad isn't a login and ignores a finger. An unrecognized finger leaves a
  pad and its digits alone.
- **Registration** (R23.4, R27.2). The employee picks which finger, then five presses with progress, 30 s
  without a press cancels. The new print is stored locally with a UUID, sent to the other clocks (§6.7),
  queued for sync, and added to the gallery; then the employee's action menu opens (spec call: they've just
  proven the finger).
- **Fingerprint menu** (owner, 2026-10-28; [its design](2026-10-28-fingerprint-menu-design.md)): a gear on the
  idle screen and the action menu opens it. After a PIN login, a person re-registers, adds or removes one of
  their own prints, each change approved by an admin's finger or PIN just before it runs and logged in
  `fingerprint_changes`. A re-register removes the old print only once the new one has enrolled.
- **Manager override** (R23.5). An active manager or admin scans, or enters their PIN login, which doesn't
  also need their fingerprint. Nobody overrides for themselves (owner, 2026-10-27; a manager without a print
  registers one instead). A manager added on a clock can't approve overrides until the database has
  accepted them (owner, 2026-10-27). It authorizes exactly one punch; every further punch, and the 30 s
  re-authentication, needs a fresh override unless the employee gives a fingerprint (R45.3). The bypass row
  is written with that punch, `pin_only` set when the manager used a PIN.
- **Admin menu** (owner, 2026-10-27): one item, "Add employee", and a way back, built so more items
  (per-clock settings) slot in later.
- **Add employee** (owner, 2026-10-27), working online or offline. A clock that has never synced knows no
  admin, so it can't reach this; no extra rule is needed.
  1. *Details:* first and last name (required), middle name (optional); phone (optional; the field draws the mask
     `(###) ###-####` and keeps the digits alone, at most ten, so backspace removes digits only; fewer than ten fails
     Next, a leading `1` isn't special-cased (owner, 2026-10-29); stored as `+15551234567`); email (optional, format checked,
     §4.2); role, Employee or Manager; group, from the clock's mirrored groups. Errors show beside their field
     on submit; cancel discards the form.
  2. *PIN* (required): typed, then typed again to confirm (my call); digits only, any length. A mismatch
     clears both and asks again. One validation point checks the PIN, so a length rule is easy to add later.
     The employee is saved locally when the PIN is accepted, and nothing before (my call).
  3. *Fingerprint* (optional, no override): the registration flow with a "Skip" button; skipping needs no
     approval, since the admin is logged in. Leaving the screen by cancel or timeout counts as a skip (my
     call). Skipping doesn't set `fingerprint_exempt`; the employee enrols later through the normal flow.
  4. *Done:* the new employee's name and number (their username), then idle.

  The new employee can log in and punch on this clock as soon as the PIN step finishes. **Employee numbers:**
  clock *n* (`CLOCK_NUMBER`, §9.1) hands out *n*×10000 + 1 to *n*×10000 + 9999 (clock 1: 10001–19999): one
  more than the highest number in its block among the employees it knows, mirrored and its own unsynced
  ones; a full block refuses with a message. The username is the number written as digits, so usernames
  can't collide between clocks either. Hand-added employees take the sequence, normally 1–9999; nothing stops
  one landing inside a clock's block, and the database then refuses whichever row arrives second (§6.6).

### 6.5 Local store

SQLite in WAL mode at `persisted_data/timeclock.sqlite3`, written only by the app. It mirrors the tables a
clock reads (§4.4) plus sync bookkeeping: a `synced` flag on punches, fingerprint bypasses, fingerprints and
employees recorded at this clock, each punch's and print's origin (`local`, `peer` for one received over the
LAN, `db`), the prints removed here that the database may still hold, and the unpushed `fingerprint_changes`
rows. The models are stored as JSON with no version, so a breaking schema change needs care (`TODO.md`); a
print stored before `finger` existed is dropped when the store opens. Kept (R26.4, R41):

- punches and corrections for the current week (Monday 00:00 local, R33),
- every punch of a shift still open at the rollover, back to its clock-in, until its closing punch arrives,
- every unsynced record, whatever its age, including an employee added here, which survives the mirror
  refresh that otherwise replaces `employees` wholesale,
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
   open shift held locally, and every open shift the clock doesn't have, however old (owner, 2026-10-27: a
   clock offline across Monday 00:00 must still offer a clock-out for a shift begun elsewhere); all
   employees, groups, fingerprints, rules and config. Replace the local copies, keeping unsynced records.
4. Push unsynced records oldest first, in this order: employees, punches (one transaction each with its
   bypass row, if any), print removals, prints (one transaction each, deleting the print it replaces first,
   since `UNIQUE (employee_id, finger)` would refuse the new one), `fingerprint_changes` rows, then bypasses
   that punched nothing. Every insert is `INSERT ... ON CONFLICT (id) DO NOTHING` (R38) except an employee's,
   since a taken id there is a refusal (below); a print whose finger the employee already has on file is
   refused too. Then mark them synced.
5. Wait for notifications (§4.4) and new local records; apply or push each as it arrives.

Any error drops the connection and returns to step 1, except the database refusing one pushed record: that
record stays on the clock, is alerted once, and is retried on every push without blocking later records;
the owner corrects it at the clock (owner, 2026-10-27). A notification for a punch older than the local
window that touches no open shift is ignored.

**Clock-created employees** (owner, 2026-10-27, except where marked):

- A punch, fingerprint bypass or fingerprint for an employee still unsynced is held: stored, unsynced
  and not sent, until the first push after its employee is accepted. The employee doesn't notice.
- Accepted, the employee is marked synced; the mirror then holds the database's copy, and the database's
  notifications tell the other clocks. A lost acknowledgement (inserted, but the clock never heard back) is
  recognised when the row returns through the mirror with the same `id` and a matching `pin_hash` (my call).
- **Refused** (the number or username is taken): the refused-record rule above. The employee and everything
  held for them stay usable on that clock, which keeps the number until a resolution tool exists
  (`TODO.md`); the database's employee with that number can use every other clock (my call). Holding
  the dependants is what keeps the refused employee's hours from being filed under the database's owner of
  that number.

**Left to the builder** (admin menu and adding employees): layouts, wording and component structure within
the existing UI's style; the exact phone parsing and email pattern within §4.2's rules; how the local store
marks unsynced employees and keeps them through mirror refreshes; how the database refuses a clock-inserted
admin; whether `CLOCK_NUMBER` is validated in `ClockSettings` or at startup (the clock doesn't start without
a valid one either way); test strategy.

### 6.7 LAN sharing (R28)

When the clock records a punch, a print, a print removal or an employee, it sends the record to every other
clock, best effort, over WireGuard directly (§11.3). Transport (spec call): a small asyncio TCP server bound to the
clock's tunnel address on port 8791, one JSON message per connection (a pydantic `LanRecord`, extending
`timeclock_schema.wire`'s `LanPunch` and `LanFingerprint`, with the sender's `INSTANCE_ID`; the message
shapes are the builder's, owner 2026-10-28). The sender opens a connection to each peer's tunnel address
with a 2 s timeout and doesn't retry (no catch-up between clocks). A receiver inserts into SQLite ignoring
duplicate UUIDs, marks it synced-elsewhere (only the recording clock syncs to PostgreSQL), and updates
state and gallery. WireGuard authenticates the sender; the server also drops connections from addresses
outside the clocks' tunnel addresses. A clock mid-deploy ignores a message kind it doesn't know.

What's shared (owner, 2026-10-28):

- **Punches.**
- **New prints**, from every registration path; a re-register's new print names the print it replaces.
- **Print removals.**
- **Employees added at a clock** (§6.4), PIN hash included, never a PIN.
- Not shared: fingerprint bypasses and `fingerprint_changes` rows. They change nothing another clock shows.

How a receiver applies them (owner, 2026-10-28):

- **A removal or replacement** drops the old print at once and leaves a note that it was removed elsewhere,
  so a database refresh can't bring the print back before the recording clock pushes the change. The note
  clears once the database no longer has the print. The receiver never pushes the removal itself.
- **A shared employee** works on the receiver as on the clock that added them: usable at once, kept through
  database refreshes, their records held until the database accepts them, and unable to approve overrides
  until then (§6.4, §6.6). The receiver recognises acceptance as the adding clock recognises a lost
  acknowledgement: the database holds that number with the same `pin_hash`. If it holds a different person
  under the number, the receiver keeps holding, so its records for the shared employee are never filed
  under that person.

Known issues, accepted (owner, 2026-10-28), both from best effort with no catch-up:

- A clock that missed a shared employee still takes their punches and prints, and shows them once the
  database has the employee.
- If clock A removes or replaces a print that clock B recorded but hasn't pushed, and B misses A's message,
  B's late push brings a removed print back, or is refused for a replaced one (the finger is taken), leaving
  the old print matching on B only. It takes a database outage and B down at the same moment.

**Networks** (owner, 2026-10-28): the clocks find each other by broadcast on their local network (§11.3), so
only clocks on one network share. The first production clocks sit behind two gateways in the same building,
to be bridged or unified later; until then they don't share, and each works alone through a hub outage.
Having the hub introduce clocks on different networks to each other (WireGuard NAT traversal) is deferred
(`TODO.md`).

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
- **Page handlers.** NiceGUI only logs an exception from a button, tap or key handler, so the page routes
  each one to a fatal shutdown with an alert (owner, 2026-10-27).
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

### 6.11 Alerts (owner, 2026-10-27; the shape is my call)

Other kinds of alert are coming, so the system extends at three points, registered in code; adding to one
changes neither the others nor the mechanism. It reads totals from the hours function (§4.5) and computes
none itself.

1. **Alert types**, any condition. A *threshold* type has a measure and a limit, and supports lead-time
   hooks. An *event* type fires when something happens. A *scheduled* type runs on a timetable (later, e.g.
   a daily report of `fingerprint_bypasses` with reason `admin_pin_login`). Adding a type registers it.
2. **Actions**, the delivery channels. Adding one changes no alert type.
3. **Hooks** bind an alert type, plus a lead time before the limit for a threshold type (0 = at the limit;
   e.g. 30 minutes, 1 hour, 5 hours before), to one or more actions.

Alerts are evaluated in two places, sharing the same types and hooks:

- **On the clock, at each punch:** for actions shown on the clock itself. The clock fires each threshold hook
  whose threshold (limit less lead time) the employee's measure has reached.
- **In the backend, through its scheduler** (§8.4; owner, 2026-10-27): the alert system is part of the
  scheduler, not beside it. A threshold usually comes due mid-shift, not at a punch. So for every employee
  on the clock, the backend works out when each threshold hook will be reached and schedules a job for that
  moment, re-planning when a punch for them arrives. Scheduled types are scheduler jobs on their timetable.
  These fire the backend's actions (SMS and email, once the notification system exists, §14).

Version 1 registers one type, one action and one hook, all on the clock:

- **Weekly hours** (threshold type): the measure is this week's worked plus paid break time (Monday–Sunday
  local, R33), the limit the weekly hours alert threshold (§4.3).
- **Confirmation notice** (action): a line on the confirmation screen (§6.4), e.g. "You've worked over 40
  hours this week".
- **Hook:** weekly hours, lead time 0, the confirmation notice.

Not in version 1: event and scheduled types, the backend's scheduled evaluation, and SMS and email actions,
all of which come with the notification system (§14); version 1's design leaves room for them. The
threshold never blocks or restricts anyone (§2). Left to the builder: the code structure (no classes are
prescribed), where the shared type and hook definitions live (timeclock-schema is the natural home, since
both the clock and the backend use them), the notice's wording and placement, tests.

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

Deployed on the VPS; v0.5.0 on schema 0.6.0 (2026-10-28).

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
  migrations (§8.3); start the relay (§8.1); start the scheduler and the
  `LISTEN` task (§8.4); `await SHUTDOWN`; close the relay, `scheduler.shutdown(wait=False)` from the loop
  (never from a THREADED callback); `await SHUTDOWN_COMPLETE`. One THREADED callback, a closure, pauses the
  scheduler (ScheduledInvoiceProcessor's `freeze_scheduler`, `shutdown_hooks.py:60-112`).

### 8.1 Relay (R26.1)

An asyncio server on `0.0.0.0:5432` inside the backend container. It accepts a connection only from a source
address inside the hub's network (`RELAY_ALLOWED_CIDR`, default `10.8.0.0/24`; spec call), since the
container also sits on the VPS's shared `coolify` network. For each connection it opens one to
`timeclock-postgres:5432` (`POSTGRES_HOST`) and copies bytes both ways until either side closes, then closes both; TCP keepalives on
both sockets; no protocol parsing. Connection errors are normal, handled per connection, and logged at
warning, never fatal. Postgres's own password authentication still applies, so the relay only adds reach.

### 8.2 PostgreSQL sidecar (R26.5)

An unlisted service in the backend's compose (setup-project leaves unlisted services alone), named
`timeclock-postgres`, since Coolify's own database already uses `postgres` on the shared network:

- `image: postgres:18.6`, `restart: unless-stopped`.
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

`0001_initial.sql` creates `pgcrypto`, the enums, every table, view, function, trigger and seed row of §4,
the schemas `config` and `backend`, the role `timeclock_clock_access` if absent, and its grants. Being plain
SQL, it runs unchanged in both databases, as every later migration does; released files are never edited
(§4.1).

### 8.4 Scheduler and the automatic clock-out (R37, R41)

- **Shape:** `scheduler_config.py` copied from ScheduledReportAggregator, not ScheduledInvoiceProcessor: RA's
  executor callback swallows aeth_ext's exit-nudge `KeyboardInterrupt` (RA `scheduler_config.py:128-139`)
  so a `docker stop` mid-job isn't reported as fatal. Drop RA's invoice-specific quiet-job patterns.
  `MemoryJobStore`, `job_defaults = {"misfire_grace_time": 60, "coalesce": True}`,
  `timezone=SETTINGS.tz`.
- **Schedule from the database** (owner's design, 2026-10-27): the times live in
  `backend.auto_clock_out_runs` (§4.3), projected 7 days ahead by SQL, DST included, so the table answers
  "was a run missed?" and the backend computes no times itself.
- **Zone** (owner, 2026-10-27): the backend accepts any `TZ` zoneinfo accepts and, at every start after the
  migrations, writes to `backend.environment` an equivalent name PostgreSQL knows (the name itself, its
  canonical zone, or another link to that zone; tzdata's link data). A zone with none is fatal at startup.
  An unchanged zone writes nothing, so it fires no reprojection. Compose sets `TZ=America/New_York`, Eastern
  with DST, not a fixed EST (owner).
- **Both databases:** everything below runs once per database, each with its own `config.settings`, runs
  table, `LISTEN` connection and job (R26.6).
- **A pass**, in order: extend the projection; run the due rows oldest first; schedule the next pass. Due
  means time passed, no `ran_at`, `scheduled_for` within 14 days of now, and only while
  `auto_clock_out_enabled`; one `now()` per pass splits due rows from upcoming ones. Passes run at startup,
  on each `config.settings` notification and on each reconnect of the backend's own `LISTEN timeclock`
  connection, and as the job; a lock per database serializes them.
- **The job:** one one-shot `DateTrigger` per database at the next unrun row, with no misfire grace (a
  dropped one-shot job would never reschedule itself). Its wait is measured from the database's clock
  (`min(scheduled_for) - now()`), with a 1 s floor, so a backend clock ahead of the server's can't fire it
  early. No row ahead removes the job.
- **A run** (R41.1): under the run advisory lock it locks its row, loads the 14 days of punches and
  corrections before the row's stamp, works out each employee's state with `timeclock_schema.shifts`, and
  for every employee not `off` whose last punch is before the stamp, inserts a `clock_out` at the stamp with
  `automatic = true`, `instance_id = 'backend'` and a new UUID. The clock-outs and the row's
  `ran_at`/`closed` commit in one transaction. A stamp later than the run time lands on the run's own day,
  and is written at run time even though it's still in the future (owner, 2026-10-27); the clocks count it
  only once its time comes (§4.5). The clocks receive them like any punch and prune those open shifts (R41).
- **Off and on** (owner, 2026-10-27): while off, nothing is projected, owed or run, and windows missed while
  off are never run later, however long it was off. Re-enabled after today's run time, today's row still
  runs. Rows missed while it was on are caught up within 14 days on re-enable, each with its own stamp.
  Nobody is ever credited an extra day; real late work is fixed by payroll amending punches.
- **Errors:** every lock wait is bounded at 1 minute (`lock_timeout`), so an owner's SQL-editor transaction
  left open, or a second backend, alerts instead of stalling. A database error stops the pass at that run
  (a newer run closing a shift first would leave the older run nothing to close), alerts, and retries in 10
  minutes. Any other exception is fatal to the whole process, relay included (project policy: only bugs
  escape).
- **Alerts** (§6.11; owner, 2026-10-27): the same scheduler runs the alert system's backend half, with
  threshold-hook jobs per employee on the clock and scheduled alert types as timetabled jobs. Not in version 1.

## 9. Configuration and deployment

### 9.1 The clock's compose (`docker/compose.yaml`)

The file exists but is empty. Don't delete it: setup-project and `docker-pin` search breadth-first and would
then edit `backend/docker/compose.yaml`. Give it a block-form `services:` key first; with
`[tool.docker].services = ["timeclock-system"]` and `wireguard = true` committed, setup-project then offers
to add the service and renders the Dockerfile from the template. The clock's `[tool.docker]`:
`services = ["timeclock-system"]`, `required_persisted_dirs = ["persisted_data"]`, `wireguard = true`,
`startup_scripts = ["timeclock-reader-access"]`.

Services (R14, as amended by R28). The compose file holds `timeclock-system` and `kiosk` today; `discovery`, the
published WireGuard port and `WG_LISTEN_PORT` arrive with LAN sharing (§13 step 5).

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
  (R13.2), with Firefox autoconfig (`kiosk.cfg`) for any preference policies can't set. `kiosk.cfg` also reloads a
  Firefox error page once the app serves (it asks for `/favicon.ico` every 30 s): after a reboot Docker starts the kiosk
  beside the app, ignoring `depends_on`, so Firefox can load before the app serves. cage runs with `-s`, so Ctrl+Alt+F*n* reaches the host's text consoles for
  debugging at the clock. The kiosk builds from the deployed checkout, not `GIT_TAG`.
- **`discovery`** (unlisted): the app's `build:` block, same `GIT_REPO` / `GIT_TAG` (so `docker-pin` pins it
  too, and the build cache makes the second build cheap), `entrypoint: ["/app/.venv/bin/devkit-container",
  "discover"]`, `network_mode: host`, the same persisted bind mount, `WG_LISTEN_PORT`,
  `restart: unless-stopped` (§11.3).

The clock's environment, set per clock in its Coolify application:

| Variable | Value |
| --- | --- |
| `INSTANCE_ID` | names the clock and its location in alerts, logs and the database; `dev` on timeclock-dev (R16.2, R3.3) |
| `CLOCK_NUMBER` | a whole number from 1 to 99, unique per clock, picking its employee-number block (§6.4); separate from `INSTANCE_ID`, which names the clock's location (owner, 2026-10-27). The clock refuses to start if it's missing or out of range |
| `HEARTBEAT_SLUG` | `timeclock-system-${INSTANCE_ID:?}` (R14.6) |
| `TZ` | `America/New_York` (R41.4) |
| `LOG_HISTORY_RETENTION_DAYS` | `14` (R44) |
| `LOG_CONN_HOST` | the log server's tunnel address |
| `ALERTS_EMAIL`, `ALERTS_EMAIL_PWD`, `ALERTS_RECIPIENTS` | as on other projects |
| `PINGKEY` | optional: the healthchecks.io ping key, set by the owner by hand; empty means no heartbeat ping, as on timeclock-dev (owner, 2026-10-28) |
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

The listed `timeclock-backend` service on networks `coolify` and `timeclock-db`, plus the `timeclock-postgres`
sidecar (§8.2). Environment: `HEARTBEAT_SLUG=timeclock-backend`, the alert variables, the four fetched-mode `WG_*`
variables, `WG_DISCONNECTED_LIMIT_SECS=1` (no tolerate, §8), `TZ=America/New_York`, `POSTGRES_PASSWORD`,
and `LOG_CONN_HOST=central-log-server` (on the VPS it reaches the log server
over the `coolify` network, not the tunnel).

### 9.3 Adding a clock

One-time, per device; the full procedure is `docs/production-clock-setup.md`:

1. Install from the production image (§5). The installer asks the clock's name (its `INSTANCE_ID`, so the
   hostname is `timeclock-system-<name>`) and its `CLOCK_NUMBER`, before it erases anything.
2. Enroll the clock in the hub by hand (§12): its key, its row (with `lan` once LAN sharing exists), its
   forward rules, then a hub release-and-pin. Release commands never run from a script.
3. `uv run image/production/new_clock.py timeclock-system-<name>.local` checks the enrolment against the
   pinned hub release, then does the rest, each step skipped when already done: the clock's coolify-tunnel
   port and key line (R29), the Coolify server at `<hostname>-tunnel:<port>` (Coolify allows one server per
   address, so an ssh rule in Coolify's container sends every `*-tunnel` name to coolify-tunnel; supersedes
   R29's `coolify-tunnel:<port>`), the database login with a random password (§4.4), and the Coolify
   application with its environment (§9.1), the shared variables copied from timeclock-dev's except
   `PINGKEY` and the Pushover pair (owner, 2026-10-28).
4. In Coolify: set `PINGKEY`, turn on Consistent Container Names (the API can't), deploy.

## 10. aeth_ext changes (R16)

Done: released as 10.0.0 (aeth_ext is at 10.1.0, 2026-10-28), and the log server is a spoke. The rest of this
section records what was built against aeth_ext 9.0.2 (`D:\SFT Software Projects\SFT Workspace\aeth_ext`); paths below are under
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

devkit-container 2.3.1 (2026-10-28; `D:\SFT Software Projects\SFT Workspace\devkit-container`; Rust; tests with
`cargo test`; released with `uv run devkit release <bump>`, which needs the Windows wheel too). §11.1 and §11.2
are done; §11.3 isn't started. Code
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

wireguard-hub 1.5.5 (2026-10-28; `D:\SFT Software Projects\SFT Workspace\wireguard-hub`): the rows and forward
rules for timeclock-backend, the central log server, timeclock-system-dev and timeclock-system-main-office are
in; the `lan` field isn't. Its owner-uncommitted `TODO.md` entry (drop the "NET_ADMIN only" line from the hub
design) is left alone.

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

Approved order; steps 3–6 as the owner set them on 2026-10-27. Plans for the dependency repositories are
short and written for subagents, unless a change is small enough that subagents would cost more than they
save. Each repository's own `AGENTS.md` and
`.claude/CLAUDE.md` apply there; on a branch, run targeted tests while working and the full suite once at the
end (`AGENTS.md`).

1. **Done. In parallel:**
   - devkit-container §11.1 and §11.2, released together (the clock image needs 11.1);
   - aeth_ext §10, released as 10.0.0;
   - timeclock-schema (§4): create the GitHub remote, then the repo with `uv init --no-workspace` and
     setup-project, the models, shift logic, rules registry and `0001_initial.sql`, release 0.1.0; add it
     here as a submodule at `schema/` (now at 0.3.1, with §8.4's runs table);
   - wireguard-hub: rows for the backend, the log server and the first clock, and their forward rules (§12);
     the log server's spoke switch (§10.7) once its row exists.
2. **timeclock-backend** (§8): deployed on the VPS (v0.5.0, 2026-10-28). With the database up, install
   DBeaver Community on the owner's workstation and set up its SSH connection to the VPS (R30.2).
3. **The clock app and kiosk** (§5–§7, §9.1, without `discovery`): deployed on timeclock-dev against the test
   database (R26.6), and on the first production clock, timeclock-system-main-office, against `timeclock`
   (v0.5.0, 2026-10-28).
4. **Admin menu, adding employees and hours**, planned and built from this spec, in timeclock-schema
   (0001 revised in place, release 0.4.0), the backend where it's touched, and the clock: the admin menu,
   adding employees, contact fields and PIN-only login (§4.2, §4.4, §6.4–§6.6, §9.1); the hours function,
   break allowance and settings (§4.3, §4.5); the alert system with the weekly-hours alert (§6.11). Built and
   merged (clock PR #2, schema 0.4.0); the USB keyboard hotplug check on the kiosk is still open.
   - **The fingerprint menu** (owner, 2026-10-28; [its design](2026-10-28-fingerprint-menu-design.md)):
     done, schema 0.6.0 with `0002`, backend v0.5.0, clock v0.5.0.
5. **LAN sharing:** devkit-container §11.3 with `discover`, the hub's `lan` field (§12), the `discovery`
   service and clock-to-clock sharing (§6.7), sharing new employees included. The clocks already work
   through the hub without it. Deferred until a second clock was close (owner, 2026-10-27); begun
   2026-10-28.
6. **Follow-ups** (§14).

Verify on the hardware, at the step that makes each possible (not in long VM runs; say what's being
installed before installing it):

- the patched 1.94.100 build enrolls and identifies on the 4500 (step 3); the soak test (a reader left
  waiting for over 10 minutes never reports `TOO_HOT`) is skipped (owner, 2026-10-27);
- the reader autosuspends once the app closes it (`power/runtime_status` reads `suspended`) while the process
  keeps its `FPrint.Context` (step 3);
- `LISTEN`/`NOTIFY` works through the relay over WireGuard (steps 2–3);
- a Coolify Compose deployment of app and kiosk on timeclock-dev, and each kiosk lockdown item on the
  touchscreen (step 3);
- with the uplink unplugged: punches are recorded, the kiosk stays up, and everything syncs on reconnect
  (step 3);
- a keyboard plugged into the running clock works in the add-employee form, with no restart (step 4);
- two clocks share punches and registrations with the hub unreachable (step 5; needs a second device, e.g.
  the EliteDesk 800 G1).

## 14. Follow-ups and known issues

`TODO.md` lists the deferred features, each needing its own design before it's built, and later fixes and checks
(a crash record that names its error, the local store across schema changes, refusing a new print that matches one
already on file).

- **Notification system** (owner, 2026-10-27): the backend will need one for email and SMS, using the
  employees' optional `phone` and `email` (§4.2). Its first users are the weekly-hours alert and its early
  warnings, as new actions on §6.11's alert system.
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

- Can the production clocks share one network before the gateways are bridged: the second clock on the first
  gateway's network (cable, switch or wifi), or a private cable between the clocks' second network ports? Until
  one of these, LAN sharing has nothing to share in production (§6.7).

## Appendix A. Files verified on the hardware (2026-10-26)

From the spikes on timeclock-dev (EliteDesk 800 G3 Mini, Debian 13.7). The kiosk files are the starting point
for `docker/kiosk/`; the reader script is the logic `timeclock-reader-access` ports to Python.

**Kiosk** (R13, R14.2). cage runs Firefox ESR alone under `seatd-launch`, in a privileged container with its
own udevd; no host networking. Touch arrives as real touch events; a replugged touchscreen works again,
including on another USB port. The DBus and `xdg_surface` warnings in its log are cosmetic.

Remote access (owner, 2026-10-27): the kiosk also runs RustDesk (the owner's custom client, Sweet Fire Support Desk)
unattended on cage's Wayland session, beside cage and never in its way (`docker/kiosk/remote.sh`); a failure only costs
remote access. The client package is kept out of git (its password is baked in) and installed at every start from
`/data/timeclock_system_files/remote/`, where its settings, and so its ID, persist. The image adds the screen-sharing
stack (session bus, PipeWire, `xdg-desktop-portal-wlr` set to share the one screen without asking) and a stand-in
`loginctl` reporting cage's session, since RustDesk's service needs a logind session. X11 also worked; cage was kept
since a RustDesk failure can't affect the app. The cage-only kiosk stays in `docker/kiosk-cage-only/` as a backup.
The Dockerfile below is the cage-only one.

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
