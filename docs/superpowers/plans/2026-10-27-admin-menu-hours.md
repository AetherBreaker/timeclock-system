# Admin menu, adding employees and hours: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task (the
> owner chose inline execution, 2026-10-27). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** build design step 4: schema 0.4.0 (contact fields, `fingerprint_exempt`, `fingerprint_bypasses`, clock-chosen
employee numbers, the hours function, the break allowance and weekly threshold, the alert registry), the backend's bump to
it, and the clock's admin menu, add-employee flow, PIN-only login and weekly-hours notice.

**Architecture:** timeclock-schema owns every rule both sides share: the SQL (0001 revised in place), the models, one
hours function in `shifts`, and the alert types and hooks in a new `alerts` module. The clock adds a pending-employee
table to its SQLite store, a few new screens to the controller's state machine, and pushes employees first in sync.

**Tech Stack:** Python 3.14, pydantic 2, PostgreSQL 18 (plpgsql), SQLite, NiceGUI 3.18, psycopg 3, bcrypt.

**Spec:** `docs/superpowers/specs/2026-10-26-timeclock-design.md` (§2, §4.2–§4.5, §6.4–§6.6, §6.11, §9.1, §13 step 4).

## Global Constraints

- 2-space indent, PEP 758 `except A, B:`, no `from __future__ import annotations`, comments dense (`AGENTS.md`).
- `0001_initial.sql` is edited in place; there is no `0002` (owner, 2026-10-27).
- Admins are never created from a clock, and the database refuses a clock login inserting `role = 'admin'`.
- The weekly threshold never blocks or restricts anyone.
- Nothing but the hours function totals hours; no second implementation in SQL or in the clock.
- A plain PIN is a `SecretStr` until hashed or checked; never logged, stored, alerted or shown.
- Employee numbers: clock *n* hands out *n*×10000+1 … *n*×10000+9999; the username is the number as digits.
- Hours shown: worked + paid break, Decimal, truncated to two places (existing `text.format_hours`).
- Conventional Commits ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; on a branch, run targeted tests
  while working and the full suite once at the end of each repository's work.
- Test databases: `TIMECLOCK_SCHEMA_TEST_DSN` and `TIMECLOCK_TEST_PG_DSN` =
  `postgresql://postgres:test@127.0.0.1:55433/postgres` (container `timeclock-clock-pg`); backend tests use
  `TIMECLOCK_BACKEND_TEST_DSN` on a throwaway `timeclock-backend-pg` (port 55432, `--rm`).

## Decisions this plan makes (builder's calls the spec leaves open)

- Settings columns: `paid_break_minutes integer default 30 check (>= 0)` and
  `weekly_hours_alert numeric(6,2) default 40 check (> 0)` (hours). Integers and a decimal, not `interval`, so the
  owner types plain numbers and nothing loads as a float.
- Patterns, shared by the SQL checks and the models: phone `^\+1[0-9]{10}$`; email `^[^@\s]+@[^@\s]+\.[^@\s]+$`
  (valid in both Python and PostgreSQL's ARE).
- Admin refusal: a `BEFORE INSERT` trigger on `employees` raises `check_violation` (so the clock treats it as a refused
  record, not a lost connection) when `role = 'admin'` and the inserting `current_user` is a member of
  `timeclock_clock_access` and not a superuser (a superuser is a member of every role).
- `fingerprint_bypasses` check: override reasons need `punch_id`, `manager_id` and `pin_only`; login reasons need
  `manager_id` and `pin_only` null, and `punch_id` is set when the login's PIN authorized a punch (a 30 s rescan), null
  otherwise.
- Hours function: `total_hours(...)` returns `HoursTotals(worked, paid_break, unpaid)`. Each break is split at the point
  its day's remaining allowance runs out (earliest breaks of the day are paid first); the paid head and unpaid tail are
  then clipped to the range and the shift's counted span. So a range edge clips a break's parts, never re-assigns its
  allowance. `build_timeline` gains `as_of`, dropping punches whose effective time is later, used by the clock and the
  hours function alike; `worked_time` is removed.
- Alerts: `timeclock_schema.alerts` holds `ThresholdType`, `Hook`, registries `ALERT_TYPES` / `HOOKS`,
  `AlertContext` and `fired_hooks(ctx) -> tuple[Firing, ...]`. Action handlers live with their side: the clock's
  `confirmation_notice` in the controller. Event and scheduled types join later as new members of `AlertType`.
- Clock store: clock-created employees live in `pending_employees` until the database accepts them, merged over the
  mirror on load (a pending id or username hides the mirror row that shares it). Rows of a pending id that didn't come
  from this clock are hidden from `punches()`, `corrections()` and `fingerprints()`, so the database's holder of a
  refused number can't be filed under the clock's employee or log in as them here. Unsynced records of a pending
  employee are held out of the push queues.
- The add-employee form's typed values are kept on the screen in fields excluded from equality, so typing doesn't
  redraw the form (which would drop focus); errors do redraw it.
- Admin menu "Back" returns to idle. The Done screen stays until OK (or the 2-minute timeout), since the admin must read
  the number.

## Review Focus

1. A clock-created employee the database refuses: their punches, bypasses and prints stay held; the database's employee
   with that number never shows on this clock's menu or gallery (tests in Tasks 4, 5).
2. A lost acknowledgement: the employee is inserted, the connection drops before the clock hears back; the next catch-up
   finds the same id and `pin_hash` and settles it, with no refusal alert (Task 5).
3. Breaks across the allowance boundary, across midnight and across the week's Monday 00:00 edge; a future-stamped
   automatic clock-out inside the range (Task 2).
4. A finger touch or tick while the admin types in the form doesn't redraw it and lose the typed text (Task 6).
5. A clock login inserting an admin is refused; the owner (`postgres`) still can (Task 1).

---

### Task 1: schema SQL and models

**Files:** `schema/src/timeclock_schema/migrations/0001_initial.sql`, `enums.py`, `models.py`;
tests `schema/tests/test_sql.py`, `test_models.py`.

**Produces:** `BypassReason` (`override_no_fingerprint`, `override_fingerprint_skipped`, `admin_pin_login`,
`exempt_pin_login`) replacing `OverrideReason`; `FingerprintBypass` replacing `Override` (`punch_id`, `manager_id`,
`pin_only` optional, validator mirroring the SQL check); `Employee.phone: str | None`, `email: str | None`,
`fingerprint_exempt: bool = False`; `PHONE_PATTERN`, `EMAIL_PATTERN` in `models`; `Settings.paid_break_minutes: int = 30`,
`weekly_hours_alert: Decimal = Decimal(40)`, `subtract_*` removed.

- [x] **Step 1: failing SQL tests** in `test_sql.py`: seed row has the new settings and no `subtract_*`; an employee
  insert with an explicit `id` works and a later default insert still takes the sequence; phone and email checks accept
  `+15551234567` / `a@b.co` and refuse `5551234567` / `a@b`; `fingerprint_exempt` defaults false; a clock login inserts an
  employee (manager) with its own id, is refused an admin (`CheckViolation`) and can't set `fingerprint_exempt`; `postgres`
  inserts an admin; the bypass check (override row without `punch_id` refused, login row with `manager_id` refused, login
  row without punch accepted); the clock login inserts bypasses with `ON CONFLICT (id) DO NOTHING`; every table loads into
  its model (update the existing test for the rename).
- [x] **Step 2: run them** — `uv run pytest tests/test_sql.py` in `schema/`. Expected: the new tests FAIL.
- [x] **Step 3: revise 0001** per the decisions above; rename the `overrides` table, enum and grants; header comment
  names the 2026-10-27 revision.
- [x] **Step 4: failing model tests** in `test_models.py`: patterns, bypass validator, settings defaults.
- [x] **Step 5: models and enums**; run `uv run pytest` in `schema/`. Expected: PASS.
- [x] **Step 6: commit** `feat(migrations): add contact fields, fingerprint_exempt, fingerprint_bypasses and hours settings`.

### Task 2: the hours function

**Files:** `schema/src/timeclock_schema/shifts.py`; test `schema/tests/test_shifts.py`.

**Produces:** `build_timeline(punches, corrections=(), *, as_of: datetime | None = None)`;
`HoursTotals(worked: timedelta, paid_break: timedelta, unpaid: timedelta)`;
`total_hours(punches, corrections, *, settings: Settings, tz: ZoneInfo, start, end, now) -> HoursTotals`.
`worked_time` is deleted.

- [x] **Step 1: failing tests**: a shift with a 20-minute break → all paid; two breaks 20 + 20 → 30 paid, 10 unpaid; lunch
  always unpaid; break over the allowance in one piece splits at the allowance; a break started 23:50 local and ended
  00:20 counts against the first day; allowance resets the next local day; a range starting mid-break clips the paid head
  and unpaid tail, not the allocation; an open break runs to `now`; a punch stamped after `now` (an automatic clock-out)
  doesn't count; `paid_break_minutes = 0` makes every break unpaid; `build_timeline(..., as_of=)` drops later punches; naive
  inputs raise `ValueError`.
- [x] **Step 2: run** `uv run pytest tests/test_shifts.py`. Expected: new tests FAIL (ImportError for `total_hours`).
- [x] **Step 3: implement.** Port the existing tests of `worked_time` to `total_hours(...).worked` + `paid_break` etc.
- [x] **Step 4: run**. Expected: PASS.
- [x] **Step 5: commit** `feat(shifts): one hours function with a daily paid break allowance`.

### Task 3: the alert registry, and release 0.4.0

**Files:** `schema/src/timeclock_schema/alerts.py`; test `schema/tests/test_alerts.py`.

**Produces:** `AlertContext(employee_id, punches, corrections, settings, tz, now)`; `ThresholdType(key, measure, limit)`;
`Hook(alert, lead: timedelta, actions: tuple[str, ...])`; `Firing(hook, measure, limit)`;
`fired_hooks(ctx, hooks=HOOKS) -> tuple[Firing, ...]`; `WEEKLY_HOURS = "weekly_hours"`,
`CONFIRMATION_NOTICE = "confirmation_notice"`; one registered type and one hook (lead 0).

- [x] **Step 1: failing tests**: weekly measure = worked + paid break for the week containing `now`; the hook fires at
  exactly the limit and above, not below; a hook with a 1 h lead fires an hour early; registering a duplicate type key
  raises.
- [x] **Step 2: run**. Expected: FAIL.
- [x] **Step 3: implement**; README mentions the alert module if it lists modules.
- [x] **Step 4: full suite, pyright, ruff** in `schema/`. Expected: clean.
- [x] **Step 5: commit, PR, merge (rebase), release 0.4.0** (`poe release minor` from the main session).

### Task 4: backend bump

- [x] **Step 1:** `timeclock-schema>=0.4.0`, relock, full backend suite against a fresh `timeclock-backend-pg`.
- [x] **Step 2:** commit `chore(deps): require timeclock-schema 0.4.0`, PR, merge, release (minor), pin it here.

### Task 5: clock domain, store and sync

**Files:** `src/timeclock_system/settings.py`, `store.py`, `clock.py`, `sync.py`, `app.py`, `docker/compose.yaml`,
`docs/dev-device-setup.md`; tests `tests/test_settings.py`, `test_store.py`, `test_clock.py`, `test_sync.py`.

**Produces:**
- `ClockSettings.clock_number: int` (alias `CLOCK_NUMBER`, 1–99, required).
- Store: `insert_pending_employee(e)`, `pending_employees() -> list[Employee]`, `accept_employee(e)` (pending → mirror in
  one transaction), `replace_employees(rows)` (replace the mirror and settle pending rows whose id and `pin_hash` match);
  `insert_punch(p, *, origin, bypass=None)`, `insert_bypass(b)`, `unsynced_bypasses()`, `mark_bypass_synced(id)`;
  unsynced queues skip pending employees.
- Clock: `MenuInfo.is_admin`; `MenuInfo.hours_this_week` = worked + paid; `PunchOutcome.firings`; `groups()`;
  `pin_only_login(e) -> bool` (admin or exempt); `record_pin_login(e)`; `record_punch(..., bypass_reason=)` for a PIN
  rescan; `add_employee(NewEmployee, pin) -> Employee`; `normalize_phone(raw) -> str` (raises `ValueError`);
  `check_new_pin(pin) -> bool`; `EmployeeBlockFull(PunchRefused)`; `override_manager` refuses pending employees.
- Sync: employees pushed first (plain insert, refusal alerted once), login bypasses after punches.

- [ ] **Step 1: failing tests** for each Produces item, including Review Focus 1 and 2: a refused employee's punch is
  never pushed; a mirror employee sharing a pending id is hidden from menu and gallery; a lost ack settles without an
  alert; next number with an empty block, after the clock's own pending one, and full block; phone formats.
- [ ] **Step 2: run** targeted. Expected: FAIL.
- [ ] **Step 3: implement**; compose gets `CLOCK_NUMBER=${CLOCK_NUMBER:?}`; the setup doc's env table gets the row.
- [ ] **Step 4: run** targeted. Expected: PASS.
- [ ] **Step 5: commit** `feat(clock): add clock-created employees, PIN-only login and fingerprint bypasses`.

### Task 6: screens

**Files:** `src/timeclock_system/ui/controller.py`, `page.py`, `text.py`; tests `tests/test_controller.py`, `test_page.py`.

**Produces:** screens `AdminReconfirm`, `AdminMenu`, `AddEmployeeDetails`, `NewPin`, `NewFingerprint`, `EmployeeAdded`;
`PinEntry.purpose: OverrideRequest | PinRecheck | None`; entry points `admin()`, `add_employee()`, `edit_details(**)`,
`submit_details()`, `skip_fingerprint()`; `Confirmation.notices`.

- [ ] **Step 1: failing tests**: admin PIN login → menu with a bypass row; exempt likewise; Admin button only for admins;
  reconfirm by finger and by PIN (PIN recorded; another person's PIN refused); a scan by someone else on reconfirm opens
  their menu; the details form's errors per field; editing a field doesn't change screen equality (Review Focus 4); PIN
  mismatch clears both; save happens only after the PIN; skip, cancel and enrol paths reach the Done screen; the weekly
  notice shows on the confirmation at 40 h; an admin/exempt rescan accepts a PIN.
- [ ] **Step 2: run** targeted. Expected: FAIL.
- [ ] **Step 3: implement**.
- [ ] **Step 4: full suite, pyright, ruff**. Expected: clean.
- [ ] **Step 5: commit** `feat(ui): admin menu, add-employee flow and the weekly-hours notice`.

### Task 7: hardware check and hand-off

- [ ] **Step 1:** rebuild the demo image on timeclock-dev and run it; the owner plugs a keyboard into the running clock and
  types into the form (§13). If it isn't picked up, fix the kiosk image.
- [ ] **Step 2:** PR for the clock; report to the owner.
