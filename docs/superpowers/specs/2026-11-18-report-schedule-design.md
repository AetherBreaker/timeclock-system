# Report schedule from the database — design

2026-11-18. A small change. It supersedes, for schedules, the 2026-11-09 call that a report entry's schedule is written in
the code. Calls marked *(owner)* are the owner's; *(my call)* are mine.

## Intent

The payroll report's send day and time change by editing the database, without a release, as its recipients already do
(`backend.report_recipients`, 0026) *(owner)*.

- **Goals:**
  - one cron line per report, stored in the database
  - the backend reschedules as soon as it changes
  - the payroll report's static entry leaves `schedule.py` *(owner)*
- **Non-goals:** a page to edit the schedule; schedules for anything but reports.
- **Anti-goals:**
  - moving the schedule skips a week's report or sends one twice
  - a mistyped line stops the report silently

## Approaches

1. A weekday column and a time column. Checked by the database and the easiest to edit, but weekly only.
2. **A cron line** — picked *(owner)*. Any pattern, e.g. `'0 13 * * TUE'`. The database can't check it; the backend
   does.
3. Columns on `config.settings`, beside the automatic clock-out's times. That row is copied to every clock and holds
   only one report's schedule.

## Design

**Table** (timeclock-schema migration 0028):

```sql
CREATE TABLE backend.report_schedules (
  report text PRIMARY KEY,  -- the registry entry's name
  cron   text NOT NULL CHECK (btrim(cron) <> '')
);
INSERT INTO backend.report_schedules VALUES ('payroll_report', '0 13 * * TUE');  -- Tuesdays at 1 PM (owner)
```

- Edited by hand; only production's rows are read, as with `backend.report_recipients` and `backend.editors`. There are
  no grants.
- An `AFTER INSERT OR UPDATE OR DELETE` trigger pings `report_schedules` with the report's name. It's a channel of its
  own, so clocks never hear it *(my call)*.
- Times are evaluated in America/New_York, the backend's zone, as today *(my call)*.

**Backend:**
- `schedule.py`: the payroll report becomes a **dynamic** entry with the same job, catch-up and retry. It starts idle
  and is scheduled from the table *(owner: the static entry goes)*.
- **A listener** on production's `report_schedules`, started beside the settings listeners. On every (re)connect and
  every ping, it reads the row for each report entry and hands the runner the new schedule (`Runner.replace`).
  - **A line `Cron` refuses** *(my call)*: alert, and keep the schedule the entry has. At startup there is none, so the
    entry stays idle until a good line arrives.
  - **No row** *(my call)*: alert, and leave the entry idle.
  - **An unchanged line** does nothing, so a reconnect doesn't disturb a waiting or retrying run.
- **Catch-up on every change** *(my call)*:
  - Today the runner checks a `catch_up` entry's latest past time against its record only at start, which a dynamic
    entry's first schedule misses.
  - Every new schedule for a `catch_up` entry now gets that check, as a code change and a restart would. Moving the
    schedule back past this week's time sends a not-yet-sent week now. Moving it later finds the week already recorded
    and sends nothing. No week is skipped and none is sent twice.
- The other users of the payroll entry, the manual send's lookup and the job's `__main__` smoke test, find it among
  the dynamic entries.

**On deploy:**
- Production last recorded the Nov 9–15 week (sent Mon Nov 16).
- The new line's latest past time is Tue Nov 17 1 PM, whose week is that same Nov 9–15, so nothing resends.
- The next report goes out Tue Nov 24 at 1 PM, for Nov 16–22.

**Editing it** (production database `timeclock`):

```sql
UPDATE backend.report_schedules SET cron = '0 6 * * MON' WHERE report = 'payroll_report';
```

A Sunday time reports the week before last, since the current week hasn't finished.

**Rollout:**
1. Schema release (v0.20.0).
2. Backend release-and-pin.
3. The outer repo records both pins, plus v0.22.1's.

## Left to the builder

- Where the listener lives.
- How the runner learns to check catch-up on a replace.
- Tests.
