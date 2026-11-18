# Manual payroll report send — design

2026-11-18. Builds on the report recipients design of the same day (`backend.report_recipients`, timeclock-schema 0026,
backend v0.21.0). Calls marked *(owner)* are the owner's; *(my call)* are mine, open to the owner's correction.

## Intent

Corrections often land after Monday's scheduled payroll email has gone out. A developer needs to rebuild and resend
that week's report by hand, to only the addresses they pick from the enrolled list. The selection is not saved
*(owner)*.

- **Goals:** one dialog on the corrections page that sends a chosen week's payroll workbooks now (last week by
  default), to chosen enrolled addresses, and shows whether it went out.
- **Non-goals:** reports other than the payroll report; editing the enrolled list on the page; a send-history view;
  retrying a manual send automatically.
- **Anti-goals:**
  - payroll emailed to an address that isn't enrolled
  - a manual send that silently doesn't happen
  - a report built from test data that looks real
  - two emails from one double click

## Approaches

1. **A request row in the database** — picked *(owner)*. The page inserts a request listing the chosen addresses. The
   database refuses it unless the requester is a developer and every address is enrolled. The backend picks it up through
   a listener, sends, and writes the outcome onto the row, which the page reads back. Developer-only and enrolled-only
   are enforced below the page, every manual send is recorded, and a request made while the backend restarts is picked up
   when it returns.
2. A bare `NOTIFY` carrying the request. Any database login can notify, the clocks' included, so the backend would have
   to re-verify everything. Nothing is recorded, and a request is lost if the backend is down.
3. An HTTP endpoint on the backend. This gives direct feedback, but it's a second server in the backend with its own
   secret and checks, and it keeps no record.

The page reads the enrolled list itself and sends the chosen addresses in its request *(owner)*.

## Who and where

- Developers only *(owner)*: accounts whose `backend.editors.developer` is true in production.
- *(My call)* The button sits in the gear menu, as a new "Reports (developers only)" section under "Database
  (developers only)": **Send payroll report…**. Non-developers never see the section.
- It follows the page's database *(owner)*. Switched to the test database, the dialog reads the test database's
  enrolled list, the request goes into the test database, and the backend builds from test data.
- *(My call)* A test send is marked everywhere a copy could travel, because the test database holds a copy of
  production's punches and an unmarked workbook would look real:
  - the subject starts with `TEST — `
  - attachment file names and workbook titles start with `TEST `
  - the body says it was built from test data

## The dialog

```
Send the payroll report
  ‹  Nov 9 to 15, 2026  ›   Last week
[Select all]
☐ jacob.ogden@sweetfiretobacco.com
☐ payroll@example.com
                         [Cancel]  [Send to 0]
Sent to 1 address at 10:42 AM          ← or "Waiting for the backend…" / "Failed: <reason>"
```

- **The week:** a week selector like the shift list's *(owner)*: back and forward arrows around the week's label
  (`text.week_label`, "Nov 9 to 15, 2026"), and a link button that returns to the default.
  - It opens on the last full Monday-to-Sunday week in America/New_York, the week Monday's run covers.
  - The request carries the shown week's Monday, so what the selector shows is what gets sent, even across midnight.
  - *(My call)* Only ended weeks: the forward arrow stops at last week, and the link reads **Last week** where the
    shift list's reads "This week". The database refuses an unended week anyway, since its report would be partial.
- **The addresses:** the shown database's `backend.report_recipients` rows for `payroll_report`, sorted, all unticked
  *(my call: the point is choosing targets, so "everyone" isn't the one-click default)*.
- **Select all** ticks every box *(owner)*. *(My call)* While every box is ticked, it reads **Select none** and
  clears them.
- **Send:** reads "Send to N" and is disabled at 0 *(my call)*. Pressing it disables it until the outcome arrives, so a
  double click can't make two requests *(my call)*.
- **Several requests for one week may wait at once** *(owner)*. Developers may each send their own at the same time, so
  nothing below the page refuses a second request while another is waiting.
- **Test database:** a line under the title says the report is built from test data and its subject starts with
  `TEST — ` *(my call)*.
- **No enrolled addresses:** the dialog says so and offers no Send.
- **The outcome:** "Waiting for the backend…", then "Sent to N address(es) at <time>" (honouring the 24-hour
  preference), or "Failed: <error>".
  - *(My call)* While the dialog is open it re-reads its request row every 2 seconds until the row is sent or failed.
  - Closing the dialog early abandons the display only, never the send; the row and a failure alert still record the
    outcome.
- **A refused insert** (an address un-enrolled meanwhile, a lost developer mark) shows the database's message as the
  failure.

## Database (timeclock-schema migration 0027)

**Table `backend.report_sends`**, one row per manual send request:

| Column | Type | Meaning |
|---|---|---|
| `id` | identity | |
| `report` | text | the registry entry's name (`payroll_report`) |
| `week_start` | date | the week's Monday |
| `recipients` | text[] | the chosen addresses |
| `requested_by` | text | the badge (an editor's email), stamped by the database |
| `requested_at` | timestamptz | `now()` |
| `status` | text | `pending` → `sending` → `sent` or `failed` |
| `finished_at` | timestamptz | null until sent or failed |
| `error` | text | the failure, else null |

**What the database enforces on insert** (a `BEFORE INSERT` trigger or equivalent):
- `recipients` is non-empty, has no duplicates, and every address is in `backend.report_recipients` for `report`.
- `week_start` is a Monday, and its week has ended in America/New_York (`backend.environment.tz`).
- `requested_by` is `current_user`, `status` is `pending`, and `finished_at`/`error` are null, whatever the insert said.
- The badge is stamped from `current_user`, as 0015 stamps `db_login`. **A `SECURITY DEFINER` function would stamp its
  owner instead**, so the insert itself, or any checking trigger, runs as the badge.

**Developers only, below the page:**
- A new server-wide role, `timeclock_report_senders`, created if missing as 0014 creates `timeclock_web`.
- 0014's `backend.sync_editor_badge()` is replaced so that in production it grants this role to a badge whose row has
  `developer = true` (`WITH INHERIT TRUE, SET FALSE`) and revokes it otherwise.
- The migration backfills today's developer badges, in production only, as the trigger itself does.
- Roles are server-wide, so a production developer badge holds the role in the test database too. That is how
  test-database requests are checked.

**Grants to `timeclock_report_senders`, in both databases:**
- `SELECT` on `backend.report_recipients` and on `backend.report_sends`.
- `INSERT (report, week_start, recipients)` on `backend.report_sends`, plus `USAGE` on its id sequence if the identity
  needs it.
- Nothing else. The page's login gains nothing until it takes a badge, as today.

**The ping:** an `AFTER INSERT` trigger runs `pg_notify('report_sends', id)`. This is a channel of its own, so clocks,
which listen on `timeclock`, never hear it *(my call)*. Notifying needs no privilege, so a ping only means "look at the
table".

## Backend

**A listener per database** (production and test), started beside the settings listeners in `startup.py`. It listens on
`report_sends`. On every connect or reconnect, and on every ping, it takes pending rows oldest first, one at a time:
- **Claim:** set `status = 'sending'` with a `FOR UPDATE SKIP LOCKED` pick, so a ping burst can't send a row twice.
- **No expiry** *(owner)*: a waiting request is sent however long the backend was away.
- **Re-check before sending** *(my call)*: every address must still be enrolled for `report`, and `requested_by` must
  still hold `timeclock_report_senders`. If not, the row fails with the reason and nothing is sent. Without this, an
  address removed while a request waited could still get payroll.
- **Build and send:** the same workbooks and email as the scheduled job.
  - Source database: the row's own database.
  - Groups: those of the registry entry named by `report`.
  - Week: `week_start` to `week_start + 6`.
  - To: exactly the row's `recipients`.
  - In the test database: the `TEST` markings under "Who and where".
  - *(My call)* The body gains a line saying who sent it by hand and when, so recipients know it's a corrected resend.
- **Outcome:** set `sent` or `failed`, with `finished_at`. A failure also stores `error` and alerts, as a scheduled
  failure does *(my call: the person may have closed the dialog)*. There's no automatic retry; they can press Send
  again.
- **Unknown report:** a `report` that names no registry entry fails with that error.

**At startup**, rows left in `sending` (the backend died mid-send) become `failed` with "interrupted by a restart; send
again". They are never resent automatically, since the email may already have gone *(my call)*.

**Reuse:**
- The scheduled job and the manual send share one build-and-send path. The only differences are where the recipients
  come from (the table versus the request) and the subject prefix.
- The scheduled run's `backend.scheduled_runs` record is untouched, so a manual send never stops or repeats Monday's
  run.

## Page

- The page reads and inserts as the signed-in badge through `editor_transaction`, in the database it shows.
- The section and dialog appear only when `signin.developer`; the database checks again.

## Rollout

Two releases, in order:
1. Schema release (v0.19.0, migration 0027).
2. Backend release with release-and-pin. The backend applies 0027 at startup.

Try it first on the test database (the demo snapshot) before a real send.

## Left to the builder

- Module layout, the dialog's styling within the page's theme, and the page's text strings (in `text.py` with the
  rest).
- The trigger-versus-function shape inside the stated contract.
- How the listener is structured beside `listen_for_settings`.
- Tests.
