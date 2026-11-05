# Shift corrections page: design

2026-11-05. A web page where two office staff amend, void and add punches and edit a few employee fields. Every item
below is decided; **(owner)** marks the owner's decisions, **(my call)** marks choices made while designing that the
owner accepted. Mockups of every screen (dark by default, a light toggle): `.superpowers/brainstorm/35655-1793901848/
content/page-layout-v2.html` (shifts) and `employees-layout.html` (employees), served by the brainstorming companion.
This document stands on its own; the mockups only show the layout.

## 1. Why

Today the owner fixes punches by hand in DBeaver as `postgres`, over SSH. Nothing records who made a correction, nothing
stops someone correcting their own punches, and the main person who will do this work has no technical background.

Success: that person fixes the common problems (automatic clock-outs, double punches, stray punches, forgotten shifts)
alone; every change says who made it, when and why; nobody can change their own punches or permissions; and the page's
shifts and hours always match what payroll sees.

**Non-goals:** an API of any kind (owner: "absolutely not an API"); phones or tablets (office PCs only, owner);
managers editing punches, ever (owner); creating or renaming groups (later, owner); editing the allowlist from the page
(DBeaver only, owner); reports and exports.

**Anti-goals (a failure even if it works):** technical fields confusing the main user by default; an edit that leaves a
hidden ignored punch that resurfaces when something else changes; the page's shifts or hours disagreeing with payroll's;
anyone changing their own punches, role, group, exemption or active status.

## 2. Shape

- **Where it runs:** its own service in timeclock-backend's compose file on the VPS, beside the backend, not inside the
  backend process: a bug in the public-facing part can't take down the relay or the automatic clock-out, and a tunnel
  outage (which restarts the backend container) doesn't take the page down (my call). It needs only the database, over
  the compose network `timeclock-db`, and Coolify's proxy for HTTPS.
- **Address:** public HTTPS on a company subdomain, through Coolify's proxy (owner: no WireGuard on office PCs, no IP
  lock since the office IP changes unpredictably). The subdomain isn't chosen yet; it's a setting, and only the Zoho
  registration waits for it.
- **Tools:** NiceGUI pages (owner). The **shift editor is one custom JavaScript (Vue) component** loaded by NiceGUI, so
  the draft lives in the browser and editing keeps working through a dropped connection; everything else uses NiceGUI's
  own widgets (owner). Python owns sign-in, the database, the shift rules, saving and live updates. NiceGUI loads plain
  `.js`/`.vue` component files without a build step; add a bundler only if a component needs an outside library.
- **Shift rules in JavaScript:** the editor previews hours with a small JavaScript copy of the rules, connected or not,
  so hours update instantly while dragging (owner). Python re-checks on save and its numbers are the ones written (§6.9).
  A test runs the JavaScript and Python rules over the same cases and fails on any difference, so drift is caught
  automatically (owner).
- **Repos touched:** timeclock-schema (migrations and models), timeclock-backend (the page service, its database login),
  timeclock-system (the clock's inactive-employee change, §10; submodule pins; this spec). Running clocks select named
  columns and the schema models ignore extra fields, so new columns and tables don't disturb them.
- **Look:** dark mode by default (owner), the SFT palette and logo the clock already uses (`--sft-yellow` primary
  buttons with black text, `SFT-Logo-June-2022-color.svg`), Material icons. Plain words, 12-hour times, hours as h:mm.
- **Branding and theme live in CSS files, not code** (owner): colors (dark and light, including the flags, banners
  and timeline), fonts, the logo and the icons are all set in CSS, so a new look is a CSS edit, with no constants to
  hunt for in Python or JavaScript. The Python pages and the shift editor component use only class names and CSS
  variables. So Quasar's brand colors are set through its `--q-*` CSS variables in the theme file, not `ui.colors()`;
  the logo comes from CSS (e.g. a `background-image` or `content: url()`); and each icon is a class whose glyph the CSS
  picks. The page's name is a setting (my call).

## 3. Sign-in

1. **"Sign in using Zoho"** (Zoho Accounts OIDC, an app registered in Zoho's API console; separate from Zoho Directory,
   whose free tier is full) with the company's Zoho Mail accounts (owner). Any Zoho account can authenticate, so the
   page then requires the verified email Zoho reports to be on the allowlist and refuses everything else (owner). The
   button uses Zoho's branding and official logo (owner).
2. **Allowlist:** a table in the **production** database, edited by hand in DBeaver: an email, an optional employee
   number, and a developer mark (owner). The test database has the same table (same schema) but nothing reads it
   (owner). A blank employee number never matches anyone, so that account has no self-edit block (owner).
3. **Checked on every page load and every save,** so deleting a row cuts off an open session at once (owner).
4. **Session limits:** a sign-in ends after 8 hours, or 30 minutes without activity; both are environment variables
   (owner). An unsaved draft ends with the sign-in (owner). The sign-in screen then says why ("signed out after 30
   minutes without activity; unsaved changes were discarded").
5. **Refused accounts** see one message ("can't use this page; if it should have access, ask to have it added") and a
   button to sign in with another account, nothing else.
6. **Zoho's reply is checked in full** (owner, 2026-11-05): signed by Zoho, meant for this page, not expired, and
   answering the sign-in this page started; only an email Zoho reports as verified counts.
7. **One gate on the server** checks the sign-in on every page load, every action and every save (owner, 2026-11-05).
   The server picks the badge from its own record of the sign-in, never from anything the browser sends, so a forged
   request gets a connection that can do nothing.

## 4. Badges: who the database thinks you are

The database already stamps each row with the account that wrote it (`db_login DEFAULT session_user`); each clock has
its own login, and DBeaver writes "postgres". The owner wants each allowlisted account to have its own database
identity (owner):

1. **One badge per allowlisted account:** a database role named after the email, so `db_login` shows the email. Adding
   an allowlist row creates it and deleting the row removes it, automatically (owner). Roles belong to the server, not
   one database, so the same badge works in `timeclock_test` and the test database creates no accounts of its own
   (owner).
2. **The page's own login can do nothing by itself.** The page connects with one login (its password a Coolify
   variable) and does each request's work as the signed-in account's badge (`SET ROLE`) (owner).
3. **`db_login` defaults move from `session_user` to `current_user`** on the tables the page writes: switching roles
   changes the latter. Clocks, DBeaver and the backend never switch roles, so their rows stamp exactly as before
   (owner).
4. **Badge permissions:** add punches and corrections, update the employee fields of §9, and write the new tables this
   design needs. Never update or delete punches or corrections, never delete anything.
5. **The database itself enforces the rules,** so a bug in the page can't skip them (owner): a badge can't add or
   correct a punch of the employee its allowlist row names, and can't change that employee's role, group, fingerprint
   exemption or active status (§9). The block finds the employee number by looking up the current role in the
   allowlist.
6. **Test database:** it's a snapshot of production, so the self-edit block there uses the production row's employee
   number and works the same (owner). How the test database's own check learns that number is the builder's call
   (e.g. the page passes it for each transaction; production must never rely on a page-supplied value).
7. **Known limit:** anyone who takes over the page process could act as any listed account. That's true of any design
   where one page serves several people.
8. **The page's login works only from the page** (owner, 2026-11-05): its password is long, random and kept only in
   the page's settings, and the database refuses that login from anywhere but the page's own container. Its password
   would otherwise be a way around Zoho for anyone who can reach the database, such as a clock on the tunnel.

## 5. How shifts are worked out

1. **Shifts aren't stored.** The page works them out from the punches every time it shows them, with
   `timeclock_schema.shifts.build_timeline` (owner, my call accepted). Nothing depends on a clock-in staying put.
2. **A shift runs from a clock-in to the next clock-out,** exactly as the hours count it (owner). A punch the hours
   ignore (an anomaly in `build_timeline`) that falls inside a shift's span stays in that shift and flags it, so a
   double clock-in is one shift with an extra clock-in, not two (owner). There's no "evidence" rule guessing intent:
   any guess would make the page differ from the hours.
3. **Stray punches:** anomalies outside any shift (a lone clock-out, break or lunch punches while clocked out) form
   their own entry; several in a row share one (owner).
4. So the page's shifts are always the hours' shifts (`weekly_hours`, `shift_summary`, `total_hours`), and a forgotten
   clock-out with no automatic clock-out shows as one long flagged shift that's fixed in one editor.
5. **Flags:** red, needs fixing (the shift holds an ignored punch, or a stray entry); orange, an automatic clock-out
   nobody has reviewed (an automatic punch with no correction); grey, changed by hand, or "automatic clock-out, fixed";
   blue, working now (open and current) (owner).

## 6. Editing shifts

1. **Draft, then save:** every change to a shift is built up in the editor and saved together, in one transaction, so
   a shift is never half-fixed (owner).
2. **The page works out the records** (owner): a changed time or type becomes an amend, a removed punch a void, a new
   punch an insert. The user never sees "amend" or "void", so every editor's changes are recorded the same way.
3. **Automatic clock-outs:** fixing one is changing its time (an amend), so one record tells the story and the shift
   still shows it ended with an automatic clock-out (owner). The general rule: change a punch that's wrong, add one
   only when one is missing, remove one only when it shouldn't exist. **Mark as correct** clears the orange flag
   without changing the time, and the history records who confirmed it (owner). How it's recorded is the builder's
   call (an amend to the same time satisfies `punch_corrections`' check).
4. **No ignored punches left behind:** saving is refused while the shift still holds a punch the hours would ignore,
   or when the change would make another punch of that employee ignored (owner).
5. **Two of the same punch** (a double clock-in; a stray clock-out moved in beside the shift's own) become a choice in
   that slot: keep one, or keep a single punch at another time you type; the ones not kept are voided (owner).
6. **Every punch has a menu** to change its type (a wrong button) or remove it (owner).
7. **Joins and splits.** A change that joins two shifts (a Clock out that was meant as Start break) asks for
   confirmation. Once confirmed, the editor redraws as the joined shift, where everything can still be adjusted; saving
   commits the changes to both at once (owner). A change that splits a shift (adding a forgotten Monday clock-out, so
   Tuesday's extra clock-in starts its own shift; turning a Lunch start and end into a Clock out and Clock in) asks for
   confirmation the same way. Once confirmed, the editor shows both shifts one above the other, each with its own day,
   timeline, sections, hours and strays, with the week total below; saving commits both at once (owner). In general the
   editor shows however many shifts the draft works out to, so its draft is a list of shifts, not one.
8. **Stray punches:** a stray entry has its own row and its own small editor (remove it, change its type, add the
   missing punch, or move it into a shift that day). Every shift editor also has a "Stray punches this day" section
   with "Move into this shift", which puts the stray in its slot as a choice (§6.5), saved with that shift (owner).
9. **The final check:** on save, Python recomputes the hours. If they differ from the JavaScript preview, a dialog
   says the page found a fault in itself, asks the user to tell the owner (naming the employee and day), and offers to
   save with the server's numbers, which are the correct ones (owner). The contact's name is a setting (my call). No
   automatic alert (owner).
10. **Notes:** one optional note per save (owner).
11. **Who and when:** every correction and every added punch records the account that made it and when (owner). The
    history shows them.
12. **Your own shifts open read-only,** with a note that another editor has to change them (owner).
13. **Add shift** opens the same editor empty, with employee and date pickers on top, and any number of breaks and
    lunches (owner). A shift that overlaps one the employee already has is refused, with a link to that shift (my
    call). Your own name isn't in the picker.

### 6.1 Live updates and conflicts

- The database already announces every change (`pg_notify('timeclock', …)` on punches and corrections, which the
  clocks listen to), so the shift list and any open editor update live (owner).
- **Incompatible:** someone else changed or removed a punch that the draft also changes or removes. That punch is
  marked with both values ("Theirs: 5:00 PM, saved 10:14 AM · Yours: 5:05 PM") and the user keeps theirs or mine;
  the rest of the draft stays; Save stays off until they pick (owner).
- **Compatible** (e.g. a clock sends in a late break): the draft is reapplied on top, the change is pointed out, and
  Save asks for confirmation (owner). The usual refusals (§6.4) still apply.

### 6.2 A dropped connection

- The shift editor keeps working: editing, dragging and the preview carry on, and the draft is kept in browser storage
  for that account, surviving a reload, until it's saved, discarded or the sign-in ends (owner).
- Save is disabled while the connection is out, with a note beside it: the connection is interrupted, you can keep
  editing, and you can save once it's back (owner). A "Reconnecting" chip shows in the top bar.
- The shift list doesn't work while disconnected, for now (owner).

## 7. The shift list

- Opens on the current week (Monday to Sunday, as the hours count it), every employee sorted by last name, each with a
  week total (owner, mockup). Employees with no punches that week are left out (my call). Arrows and "This week" change the week; Employee, Group and Role
  filters combine (owner).
- Each employee shows their ID beside the name: always the username they type at the clock (`pin_username`), never the
  database id (owner).
- Columns: day, clock in, clock out, breaks (count and total), lunch, worked, flags. "Worked" leaves out breaks and
  lunch, as the hours views do. An open shift shows "working" and hours "so far".
- An attention bar counts this week's flagged shifts by kind; clicking a count, or the "Only shifts that need attention"
  switch, filters to them (owner).
- A stray entry is its own row (owner). Your own shifts are greyed with a lock (owner). Clicking a row opens its editor.

## 8. The shift editor

- **Header:** employee name and ID, the day, flags; a "Needs fixing" banner in plain words when the shift holds a
  problem ("Two clock-ins, at 7:01 AM and 7:04 AM. The hours count from the first…") (owner). An unreviewed automatic
  clock-out gets an orange banner with **Mark as correct**.
- **Timeline:** the shift drawn as a bar that zooms to fit it, with a marker per punch; dragging a marker changes that
  time and the field below follows, and typing moves the marker. A changed punch leaves a hollow marker where it was; a
  problem punch's marker is red (owner).
- **Sections:** Shift (clock in, clock out), Breaks and Lunches as start-to-end pairs with durations, each with an Add
  button for any number (my call, owner accepted). A changed field turns blue with "Was 11:00 PM" and an Undo.
- **Side panel:** hours before and after for the shift (worked, paid breaks, lunch) and the employee's week; "Your
  changes" in plain words; the note; Discard and Save.
- **History:** every earlier change to the shift: when, who, what changed, the note.

## 9. Employees menu

- **What it edits** (owner): first, middle and last name; phone and email (stored as now: `+1` and ten digits, and the
  `EMAIL_PATTERN` format; phones display as (555) 555-0142); group (move between existing groups only); role, admin
  included; fingerprint exemption; active; and the PIN. The username isn't editable, and fingerprints are listed for
  reference only (registering and removing stays at the clock) (my calls).
- **Your own record:** you can change your own name, contact details and PIN, but not your role, group, fingerprint
  exemption or active status; those fields show locked with a note (owner). The database enforces it (§4.5).
- **Change log:** employee rows are updated in place, so every change records who, when, old value, new value and the
  optional note, and the editor shows it as history. A PIN change is logged as "PIN changed", never the PIN or its
  hash (owner).
- **Same pattern as shifts:** a list (search by name or ID, Group and Role filters, a "Show switched-off employees"
  switch, sorted by last name); an editor where changes turn blue, are listed under "Your changes" with one optional
  note, and save together; live updates with the same conflict rules (owner).
- **No-fingerprint tag:** a red tag, counted in a bar above the list, for anyone with no fingerprint registered who
  isn't exempt, since they can't clock in with a finger (my idea, owner accepted).
- **Switching Active off** explains what it does before saving (can't clock in at any clock; shifts and history stay).
- **This editor uses NiceGUI's own widgets,** so it doesn't keep working while disconnected: it waits, Save is off, and
  a note says the changes are kept until the connection is back (my call, owner accepted).
- **Setting a PIN:** its own dialog, typed twice, digits only (as the clock's `check_new_pin`), never shown or logged
  once set; "Make one up for me" picks a random PIN and shows it once to pass on. It's saved the moment Set PIN is
  pressed, separately from other changes, so a PIN never sits in a draft (my calls, owner accepted). Hashed with bcrypt
  `$2a$` like the clock (pgcrypto's `crypt()` can't check `$2b$`) and checked by `employees.pin_hash`'s constraint.
  Clocks pick up the new hash through the existing notifications; one that's offline keeps the old PIN until it
  reconnects.
- The clock prompting an employee for a new PIN is not in this version: it's a TODO item, with its database shape
  (owner). This version only sets a PIN directly.

## 10. Inactive employees at the clock (timeclock-system)

Part of this design (owner). Today an inactive employee is shut out completely: `Clock` refuses their PIN login
(`clock.py` around line 313), drops their prints from the reader's gallery (around line 601) and refuses every punch
(around line 403). Someone switched off mid-shift can't even clock out; the shift stays open until the automatic
clock-out (which closes inactive employees' shifts too).

**New rule (owner):** an inactive employee can't clock in. While they still have an open shift, their PIN login and
fingerprints keep working and they can start and end breaks and lunches and clock out. Once they clock out, they're
shut out completely. Inactive managers and admins still can't authorize overrides or open the admin menu.

The gallery has to change when such an employee clocks out (their prints leave it) and when an open-shift employee is
switched off (their prints stay until then). Build on `set_gallery` as it is on `main` since `03dd257` (a reordered
gallery no longer restarts the identify). Like any clock change, it ships in a clock release and is tested on the dev
clock first.

## 11. Settings bubble and technical details

- **The gear** on every page opens a bubble on the same page (no page load) (owner):
  - Dark mode (on by default), 24-hour time, and Show technical details: open to everyone, remembered per browser
    (owner).
  - A Real/Test database switch, shown only to accounts marked developer, remembered per account (owner).
- **Technical details** add (owner, my field list accepted): per punch, its id, clock (`instance_id`), the account that
  wrote it (`db_login`) and when the server received it; under "Your changes", the exact records the save will write;
  in history, each correction's id and account; under each employee (list and editors), database id, username, group
  and its id, role, fingerprint exemption, fingerprints registered, active, contact details, last update and who added
  them; in the shift list, a Clock column and a punch count noting how many are ignored, automatic or corrected; in the
  employee editor, stored values and each fingerprint's id, finger, time and clock.

## 12. The test database

- A developer's database switch (§11) moves the whole page to `timeclock_test`, which is a snapshot of production,
  fast-forwarded now and then (owner). Every page then shows a striped "TEST DATABASE" banner with a shortcut back, and
  the top bar's yellow line turns orange (owner). Sign-in, badges and the self-edit block work as in production (§4.6).
- Later, in test mode only: buttons beside Save that preview the result in each format payroll will receive by email.
  They wait on the payroll emails, which don't exist yet (owner; in `TODO.md`).

## 13. Settings (environment)

The builder names them. At least: the public address (and so the Zoho redirect URL), the Zoho client id and secret,
the two session limits (8 h, 30 min), the contact's name for §6.9, the page's database password, and the session
secret. Both databases are reached through the page's one login.

## 14. Schema changes, as decided

The builder designs tables, columns and mechanisms; what's fixed:

- The allowlist table (§3.2) and the badge machinery (§4), with `db_login` stamped from `current_user` on the tables the
  page writes.
- Corrections gain who wrote them (`punch_corrections` has no `db_login` today). Hand-added punches need a place for
  the save's note, since `punches` has none; one shape is a row per save holding the note, account and time, that
  corrections and added punches point at, but that's the builder's call.
- `shift_summary`'s `corrected` column counts a punch as hand-added only when `db_login = 'postgres'`; it must count the
  page's punches too. Views that show correction notes (`punch_log`) must show the page's notes.
- The employee change log (§9).
- Page-added punches still need an `instance_id` (NOT NULL): the builder picks a fixed value that reads as "added on the
  page".

## 15. Later, not in this version

- The clock prompting for a new PIN (`TODO.md`), and the related "employees without a PIN" item.
- Payroll previews in test mode (`TODO.md`).
- Creating and renaming groups (owner).
- A dedicated menu for joining shifts, if ever (owner).

## 16. Left to the builder

Table and column names; how badges are created and removed; how the page passes the signed-in account to the database
and keeps a connection's role from leaking between requests; how Mark as correct and the change log are recorded; how
the test database's self-edit check gets the employee number (§4.6); routes and page structure; session storage, CSRF
and cookie settings; how the shift editor component is split up; the test layout, including the JavaScript-versus-Python
drift test; how the new service is built and deployed alongside the backend (the backend's image and `[tool.docker]`
settings), and its release through `poe release-and-pin`.
