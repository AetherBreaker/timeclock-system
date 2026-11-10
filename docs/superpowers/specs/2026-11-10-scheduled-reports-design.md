# Scheduled jobs and the emailed payroll reports: design

Agreed with the owner in conversation on 2026-11-10. Decisions marked *(owner)* are theirs; *(my call)* marks a choice
made while writing this, open to change.

## 1. Why

Payroll needs the week's hours without anyone exporting them by hand. The backend will build the payroll report as Excel
workbooks on a schedule and email them. At the same time the backend's scheduling moves off APScheduler onto a small
runner of our own, with every job declared in one place in the code, so what runs and when is visible by reading one file.

## 2. Goals, non-goals, anti-goals

**Design philosophy** *(owner)*: everything here is built to be reused, like a framework, so similar things follow the
same rules because they stand on the same foundation. A job is a `JobBase` subclass, a schedule is a registry entry, a
report is tabs declared on one report builder. A new job or report adds a declaration, not its own machinery; a rule
that applies to more than one of them lives in the shared layer.

Goals:

- One module in the backend declares every scheduled job: a **static table** and a **dynamic table** *(owner)*.
- Schedules are cron lines, validated so a mistake stops the backend at startup instead of silently never running
  *(owner: robust, cron-style)*.
- The weekly payroll report: Excel workbooks, sent by email *(owner: guaranteed)*; recipients and times vary per entry
  and are written in the code, not environment variables *(owner, 2026-11-09)*.
- A report run missed because the backend was down catches up after a restart *(owner)*.

Non-goals:

- Jobs invented at runtime. The dynamic table's entries can change while running, but each one starts as a definition
  in the code *(owner)*.
- Editing schedules or recipients from the corrections page or the database.
- Payroll previews in the page's test mode (a separate TODO item).

Anti-goals:

- A report sent twice for the same week, or a week silently not sent.
- An ordinary redeploy or `docker stop` counted as a crash (the bug `scheduler_config.py` patches today must not return).

## 3. The registry

One module (the builder names it, e.g. `timeclock_backend/schedule.py`) holds both tables. Nothing else adds entries.

An entry has:

- `name`: unique across both tables, checked at startup.
- `schedule`: `Cron("<5-field cron line>", tz=...)` or `At(<aware datetime>)` (a one-off time); a dynamic entry may also
  have no schedule (idle). `tz` defaults to `SETTINGS.tz` (America/New_York).
- `job`: a job instance (§3.1), built with that entry's settings (a report's groups and recipients, a database).
- `catch_up`: whether a missed run is made up after a restart (§5). Off unless the entry says so.

**Static table** *(owner)*: a tuple of entries fixed at startup. Every cron line is parsed with cronsim at import, so a
bad line raises and the backend doesn't start.

**Dynamic table** *(owner)*: a tuple of entries declared the same way, whose definition can change while the backend
runs: its schedule or arguments changed, the entry removed, or replaced entirely. The runner exposes `replace(name, entry)`
and `remove(name)`. *(My call)*: only a name declared in the dynamic table can be replaced or removed; replacing an
unknown name, or a static entry, raises. A replacement is validated the same way as a declaration.

At startup the runner logs each entry's name, its schedule in words (cronsim's `explain()`) and its next run *(my call)*.

### 3.1 Jobs *(owner)*

Following the ScheduledReportAggregator's job pattern (`jobs/base.py`, `jobs/bal_sheet_job.py`):

- Every job is a class in its own module, subclassing a shared `JobBase`, with all of its own logic defined on it, and a
  `main` method the runner calls with the scheduled time it's running for.
- Shared behaviour lives in `JobBase` as hooks and a wrapper around `main`, not in each job: the runner's failure
  alert, retry and catch-up recording (§4, §5) go through it, so a later shared concern (timing, a lock) is added in one
  place.
- Every job module ends with an `if __name__ == "__main__":` block that initializes aeth_ext and runs the job's `main`
  once, immediately, for smoke tests. The report's block runs with its registry entry's settings for last week; the
  automatic clock-out's block runs one pass on the test database only, since a pass on production writes punches
  *(my call)*.
- *(My call, differing from the aggregator)*: a job is an ordinary class, one instance per registry entry, not a
  singleton, since two report entries can share a class with different groups and recipients.

## 4. The runner

Replaces APScheduler: `scheduler_config.py` and the `apscheduler` dependency go. Built on cronsim (owner, 2026-11-10),
one asyncio task per entry that sleeps until the entry's next time, runs the job, and repeats. Replacing or removing a
dynamic entry cancels its task's wait (never a running job) and starts the new definition.

Cron times come from cronsim on aware datetimes in the entry's zone, so daylight saving behaves as cron does: a time
in the spring-forward gap runs at the gap's end, a time in the fall-back hour runs once. Sleeps are re-checked against
the wall clock on waking, so a late wake or a clock change can't run a job early or skip one silently.

*(Owner)*:

- **Overlap**: if an entry's previous run is still going when its next time arrives, that time is skipped and logged.
  Not for an `At` entry *(my call)*: the automatic clock-out replaces its own entry while running, so a new `At` time
  that comes due during the run waits for the run to finish, then runs (skipping it would lose the clock-out's only
  reschedule).
- **A late run** (the loop busy, a wake more than 60 s late) of an entry without `catch_up` is skipped and logged, as
  today. An `At` entry always runs, however late, since nothing would reschedule it (as the automatic clock-out's job
  does today).
- **A failed run** (any exception the job doesn't handle) alerts through aeth_ext's `alert(..., wait=False)`, is logged
  with its traceback, and doesn't stop the backend or the entry's later runs. Today such an error is fatal and restarts
  the container.
- **Retrying a failed run** *(owner: every 15 minutes, up to a cap, so a failure can't flood an inbox overnight)*: an
  entry may set a retry: an interval and a cap on retries. A failed run is retried at that interval until it succeeds,
  the cap is reached, or the entry's next scheduled time arrives. It alerts on the first failure and once more if it
  gives up; retries in between only log. The report retries every 15 minutes, at most 4 times, so the last try is an
  hour after the first failure *(cap: my call)*. The cap also bounds the worst case of an email that went out but whose
  run then failed to be recorded: at most 4 repeats, not one every 15 minutes all night.
- **Shutdown**: the runner registers with aeth_ext's shutdown like the scheduler does now, stops all waits, and gives a
  running job until the shutdown budget to finish. A `KeyboardInterrupt`/`CancelledError` from aeth_ext's exit nudge
  inside a job is a normal stop, never an alert.

## 5. Catching up a missed run *(owner)*

A new table records each `catch_up` entry's last completed run *(my call on shape)*:

```sql
CREATE TABLE backend.scheduled_runs (
  name          text PRIMARY KEY,   -- the entry's name
  period        text NOT NULL,        -- the period the last completed run covered (see below)
  scheduled_for timestamptz NOT NULL, -- the scheduled time the last completed run was for
  completed_at  timestamptz NOT NULL
);
```

- It lives in the production database only (`timeclock`) *(my call)*: it's the backend's own bookkeeping and the names
  are unique across the registry. The migration still applies to both databases, as every migration does.
- A run is recorded only when its job returns without error: the report's email is sent first, then the row is written.
  A failed run leaves the row unchanged *(owner)*.
- A `catch_up` entry names its **period**: a function from a scheduled time to the stretch of work that run covers. For
  the report it's the week's Monday; the default is the scheduled time itself. The row stores the period of the last
  completed run (`period text NOT NULL` beside `scheduled_for`) *(my call)*, so changing a report's cron within a week
  can't send that week again.
- At startup, for each `catch_up` entry: if the period of the most recent scheduled time before now (cronsim with
  `reverse=True`) differs from the row's `period`, the job runs once for that time. Only the latest missed time runs:
  two missed weeks send one report *(owner)*.
- An entry with no row (new, renamed, or present when this first ships) gets a row for its most recent past time
  without running, so a deploy never sends a past week's report on the spot; its first run is its next scheduled time
  *(my call)*.
- A job is told the scheduled time it's running for (not the wall-clock time), so a catch-up reports the right week.
- The migration ships through a schema release and a backend release-and-pin, like every migration.

The automatic clock-out keeps its own catch-up through `backend.auto_clock_out_runs` and doesn't use this table.

## 6. The automatic clock-out as a dynamic entry

Its behaviour stays as it is (spec §8.4 of the timeclock design); only how it's scheduled changes.

- It becomes a job class (§3.1) whose `main` is today's pass (`sync_auto_clock_out`). The dynamic table declares one
  entry per database in `DATABASES` (`auto_clock_out:timeclock`, `auto_clock_out:timeclock_test`), each with its own
  instance, idle at declaration.
- `sync_auto_clock_out` takes the runner instead of the scheduler. Where it called `add_job(..., DateTrigger(next_at))`
  it calls `replace(name, entry with At(next_at))`; where it called `remove_job`, it makes the entry idle (`remove` for a
  declared dynamic entry leaves it declared and idle).
- The `_MIN_WAIT` workaround for APScheduler dropping a job re-added at its own firing time goes, if the runner doesn't
  share the problem (it shouldn't: a replaced entry's new wait starts fresh).
- The settings listener, `_PASS_LOCKS`, the lookback, the retry after a database error and its alert are unchanged.

## 7. The payroll report job

### 7.1 An entry *(owner)*

Each report entry in the static table names its schedule, its database (production), which groups it covers (named
groups, or every group), and its recipients. Office and Warehouse can go to the same people in one entry or to
different people as separate entries. `catch_up` is on.

The first entry is a placeholder until the owner settles the real ones *(owner)*: every group in one email, Mondays at
06:00 New York time (`"0 6 * * MON"`) *(time: my call)*, to `jacob.ogden@sweetfiretobacco.com` only (the owner's work
address, already public in aeth_ext's alert defaults).

### 7.2 A run *(owner)*

1. The week: the most recent full Monday-to-Sunday week, New York time, ending before the scheduled time the run is for.
2. One workbook per covered group, built as in §8. A group added later gets its own workbook in an every-group entry
   without a code change.
3. One email per entry, all the run's workbooks attached, sent with aeth_ext's `prepare_email_message` and
   `batch_send_emails` (owner) through the same Zoho mailbox and `ALERTS_*` settings the alerts use (owner). Sending is
   blocking SMTP, so it runs in a worker thread (`asyncio.to_thread`); the relay must not stall.
4. Workbooks are written to a temporary directory (the helper attaches files by path) and deleted after the send.
5. A failure (database, building, sending) alerts and isn't recorded (owner), and retries as §4 says. If every retry
   fails, the week is caught up only by a restart before the next scheduled time; the alerts say so.

*(My calls)*:

- Subject: `<Group(s)> Timeclock Report <Mon> to <Sun>`; body: one plain-text line saying which week and which groups
  are attached. Both are placeholders: the owner reviews the first smoke-test email and settles the wording *(owner)*.
- A covered group with no punches that week still gets a workbook, filled by §8.1's rule: Weekly Hours and Shift
  Summary show each active employee with zeroes, Punch Log is headers only.
- An employee with no group appears in no workbook today. An every-group entry adds a `No Group` workbook when such an
  employee has punches that week; a named-group entry ignores them.
- A named group that doesn't exist at run time is a failed run (alert), not a silently smaller email.

## 8. The report's shape

Defined in one place: one query per tab, with its column labels and display values, so the next round of the owner's
changes is a small edit *(owner)*. The shape below is the owner's rulings from reviewing the exports (2026-11-09).
The reviewed prototype is `build/reports/export.py` (local, not committed); the backend version should produce the same
files.

- One workbook per group; tabs **Punch Log**, **Shift Summary**, **Weekly Hours**, in that order (owner).
- Filename and workbook title (document properties): `<Group> Timeclock Report <YYYY-MM-DD> to <YYYY-MM-DD>` (the
  week's Monday and Sunday) (owner asked for group and week in both; the wording is mine).
- Every value is written as a string cell (`write_string`, never `write`) and every column carries Excel's Text format
  (`@`), so Excel converts nothing (owner); "number stored as text" warnings are suppressed, the header row is bold and
  frozen, columns sized to fit (my calls).
- Timestamps are naive local time, America/New_York at the offset in force at that instant, `YYYY-MM-DD HH:MM:SS`
  (owner; whole seconds my call). True/false reads `Yes`/`No` (owner). Headers are title case without underscores (owner).
- No ids, employee ids, or anything naming the clock a punch came from (owner).

| Tab | Source | Columns, in order |
| --- | --- | --- |
| Punch Log | `punch_log`, `at` within the week | Employee Name, Punch Type, Time, Correction Note, Corrected At |
| Shift Summary | `shift_summary`, `week_of` = the Monday | Employee Name, Day, Clock In, Clock Out, Corrected, Shift Length, Hours, Breaks, Unpaid Breaks, Lunches, Break Count, Lunch Count, Breaks Over Limit, Break Overage, Break Times (Minutes Over Limit), Lunch Times |
| Weekly Hours | `weekly_hours`, `week_of` = the Monday | Employee Name, Hours, Breaks, Lunches, Unpaid Breaks |

Punch Type values (owner asked for readable values; the wording follows the clock's own confirmations): `clock_in`
Clocked In, `clock_out` Clocked Out, `break_start` Break Started, `break_end` Break Ended, `lunch_start` Lunch Started,
`lunch_end` Lunch Ended. Group membership is `employees.group_id`.

### 8.1 Rows for people who didn't work *(owner's rule of thumb; the mechanism approved by the owner)*

The owner's rule: where a report's expectation is one row per employee (or per employee per day), an active employee
who didn't work still gets that row, with zeroes, not blanks. Where it's one row per event (a punch), no event means no
row. It's a rule for every report, so it's built once, not per job:

- Each tab declares its **grain**: what one row stands for. `PER_EVENT` (rows come only from the query), `PER_EMPLOYEE`,
  or `PER_EMPLOYEE_DAY` (each employee on each day of the period). A new report picks a grain; it never writes its own
  fill logic.
- Each column declares its **kind**, which sets both its display and its "nothing happened" value: duration `0:00`,
  count `0`, yes/no `No`, timestamp and text empty (a time that didn't happen has no zero), employee name and day
  filled from the row it stands for.
- The report builder takes the query's rows, lays out the grain's expected rows (the covered employees, times the
  period's days for `PER_EMPLOYEE_DAY`), and adds a zero row, by column kind, for each expected row the query didn't
  produce. A day with shifts keeps one row per shift.
- Expected employees: the active employees covered (by group), plus anyone with rows in the period even if inactive
  now *(my call)*. Every day of the period counts, weekends included (owner: "one line per day in the period").

Applied: **Punch Log** is `PER_EVENT`; **Shift Summary** is `PER_EMPLOYEE_DAY`; **Weekly Hours** is `PER_EMPLOYEE`.
Sorting: punches by time; Shift Summary by day, then name, then clock-in; Weekly Hours by name *(my call for Shift
Summary: zero rows have no clock-in to sort by)*.

## 9. Testing

Regular pytest plus simple smoke tests; no ongoing test-database routine is needed *(owner)*.

- pytest: the runner (cron and `At` timing on a controllable clock, daylight-saving days, overlap, late runs, retry and
  its cap, replace/remove, shutdown mid-run), registry validation (bad cron, duplicate names, replacing an undeclared
  name), catch-up against the local test Postgres (missed week runs once, no row records without running, a cron change
  within a week doesn't resend), the automatic clock-out's existing tests moved onto the runner, and the workbooks' shape
  (tabs, headers, every cell a string with the Text format) from seeded data.
- Smoke tests through each job module's `__main__` block (§3.1): the report job sends last week's workbooks to the
  placeholder address, where the owner checks the workbooks and the email's wording; the automatic clock-out runs one
  pass on the test database.
- After the release: the startup log lists every entry and its next run, and the automatic clock-out logs its next
  pass for both databases.

## 10. Left to the builder

Module and class names; the entry and schedule types' exact shape; how the runner's tasks are organised; test
structure. Dependencies to add: `cronsim`, `xlsxwriter`; to remove: `apscheduler`.
