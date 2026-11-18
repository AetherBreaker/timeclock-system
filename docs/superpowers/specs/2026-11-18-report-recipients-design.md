# Report recipients from the database — design

2026-11-18. A small change. Supersedes, for recipients only, the 2026-11-09 call that a report entry's recipients are
written in the code (scheduled-reports design §2). Schedules stay in the code.

## Intent

The weekly payroll report's recipient list changes without a code change or a release: the job reads it from the
database at each run *(owner)*.

- **Goal:** edit the list by hand in SQL; the edit applies from the next run, with no restart or release.
- **Non-goals:** a page to edit the list; schedules from the database; recipients taken from employee records.
- **Anti-goals:** a run that quietly sends to nobody; recipient addresses reaching the clocks.

## Approaches

1. **A recipients table in schema `backend`, edited by hand** — picked *(owner)*. The smallest change, any address
   allowed, and nothing in `backend` reaches a clock. An edit is an `INSERT`/`DELETE`, not a click.
2. A "gets the payroll report" flag on employees, edited on the existing employee page. A UI almost for free, but only
   employees can receive the report, and employee rows are copied to every clock.
3. Approach 1 plus an editing section on the corrections page. Click-to-edit, but a new page section and permissions
   for a list that changes a few times a year; it can be added on top of 1 later.

## Design

**Table** (timeclock-schema migration 0026; 0025 is v0.17.0's):

```sql
CREATE TABLE backend.report_recipients (
  report text NOT NULL,  -- the registry entry's name, e.g. 'payroll_report'
  email  text NOT NULL CHECK (email = lower(email) AND email ~ E'^[^@\\s]+@[^@\\s]+[.][^@\\s]+$'),
  PRIMARY KEY (report, email)
);
```

- Keyed by entry name, so separate report entries (say Office and Warehouse) each have their own list.
- Like `backend.editors` (0014): edited by hand, only production's rows are read, the test database's rows do nothing,
  and there are no grants, since the backend's own login is the only user (as 0021).
- *(My call)* The address check is `backend.editors`' minus its 63-byte limit, which exists only because an editor's
  email is also a role name.
- *(My call)* The migration seeds `('payroll_report', '<today's address in schedule.py>')`, so the first run after the
  release sends where it sends today.

**Job** (timeclock-backend):

- `PayrollReportJob` loses its `recipients` argument. At the start of `main`, before building any workbook, it reads
  `SELECT email FROM backend.report_recipients WHERE report = <its entry name> ORDER BY email` from its own database
  (production). The runner already sets the job's `name` to its entry's name.
- *(My call)* No rows: the run raises, so it fails, alerts and retries like any failed run, never sending to nobody.
- Read at each run, so no settings listener: the list doesn't change the schedule (unlike the automatic clock-out's).
- The module's `__main__` smoke test reads the same table; it runs the job outside the runner, so it sets the job's
  `name` itself.
- `schedule.py` drops the recipient from its entry and from the "placeholder recipient and schedule" comment; the
  schedule stays a placeholder.

**Rollout:** schema release (v0.18.0), then a backend release that pins it; the backend applies 0026 at startup.
Production gets it through release-and-pin.

**Editing the list** (production database `timeclock`):

```sql
INSERT INTO backend.report_recipients VALUES ('payroll_report', 'someone@example.com');
DELETE FROM backend.report_recipients WHERE report = 'payroll_report' AND email = 'someone@example.com';
```

## Left to the builder

Tests, the empty-list error's wording, and where the read sits in `main` as long as it comes before the workbooks.
