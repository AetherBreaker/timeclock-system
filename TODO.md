# TODO

A deferred item needs its own design before it's built.

1. [ ] **Employees without a PIN set their own at the clock.** Allow an `employees` row with no `pin_hash`. When someone
   types that username at a clock, the clock prompts them to set a PIN, then pushes it up to the database. This makes
   adding new admins (and others) by hand much easier: add the row in DBeaver with no PIN, and the person sets it at the
   clock. The owner accepts the security trade-off: until the PIN is set, whoever types that username first sets it.
2. [ ] *Deferred.* **Refuse a print that's already registered.** At registration, check the new print against every
   stored print except the one it replaces, and refuse a match. Deferred 2026-10-28: admin approval of adds and
   re-registrations mostly covers it. libfprint doesn't expose its matcher (`fpi_print_bz3_match`) to Python. Either
   register with five identify touches against the stored prints and build the template from them, or compile NBIS
   bozorth3 into a small C extension. Still open: which prints count (inactive employees, the person's own other
   fingers), what the refusal says, and whether it sends an alert.
3. [ ] **Registration reads its presses in one reader session** (owner, 2026-11-06). Each press is its own
   `identify_sync([])`: the uru4000 powers up, captures, waits for the lift and powers down, then the 1.0 s
   `finger_off_pause` runs. `enroll_sync` keeps the reader on, re-arms on the lift it detects, and reports each press on
   its own through its progress callback (the "last scanned print", built as identify's probe), but stops after 5 good
   presses (`IMG_ENROLL_STAGES`; only a libfprint patch changes it). Options: enroll in rounds of 5, another round for
   replaced presses, cancelled once the registration ends (recommended: no libfprint release); or patch libfprint for
   one session until cancelled, which saves only a restart per 5 presses over rounds. Either needs a dev round.
4. [ ] *Deferred.* **On-screen keyboard** for the clock's text fields (very low priority; a physical keyboard is plugged
   in when needed).
5. [ ] *Deferred.* **Per-clock settings** in the admin menu, starting with UI scaling.
6. [ ] *Deferred.* **UI Mirror Mode** Relies upon the item above coming first. Mirrors the position of everything in the
   UI, so objects on the left and right sides swap places.
7. [ ] *Deferred.* **Toggle PIN-only login** for an employee from the clock (for now it's set only in the database).
8. [ ] *Deferred.* **Resolution tool** for a clock-created employee the database refused: renumber the employee and
   their records on the unsynced clock so they go through.
9. [ ] *Deferred.* **Hub introduction for LAN sharing:** clocks on different networks (separate gateways) can't find
   each other by broadcast. The hub sees each clock's public address and could tell clocks with the same `lan` value
   where the others are, so they connect directly through their gateways (WireGuard NAT traversal) and keep sharing
   through a hub outage. Needs both routers to cooperate and both internet connections up; a clock restarted during a
   hub outage loses the link. Shelved 2026-10-28 for a future upgrade.
10. [ ] *Deferred.* **Per-group lunch and break switches**, possibly before the first release, depending on the owner's
    boss.
11. [ ] *Deferred.* **Testing and debug menus.** For example, tapping the reader icon a few times opens a verbose log of
    the reader's scans, for troubleshooting someone's fingerprint. It can include a capture mode showing each press's
    picture with the matcher's detail points circled (failed presses included), in memory only, never saved or logged.
    Samples on 2026-10-29 showed ridge cut-offs at the window's left edge counted as detail points, and a faint line
    across the middle of every press.
12. [ ] *On hold.* **Turn the screen off after hours, waking on touch** (owner, 2026-10-29). Designed 2026-11-11 (the
    TODO wave's spec, §9, and its decisions log). On hold 2026-11-12 until the owner investigates a report of a clock
    not quite waking early in the morning. Its `asleep` file is also the screen recorder's signal: recording pauses while
    it exists (the recorder's spec, 2026-11-13, §3). A clip that runs on after the clock sleeps gets no frames once the
    screen is off, which the recorder would count as a freeze; screen off has to account for that.
13. [ ] *Deferred.* **Payroll previews in the punch-editing page's test mode** (owner, 2026-11-05): while the page is
    switched to the test database, buttons beside the shift editor's save button preview the result in each format
    payroll will receive by email. Waits on the automatic payroll emails, which don't exist yet.
14. [ ] *Deferred.* **The clock prompts for a new PIN** (owner, 2026-11-05): the shift-corrections page's Employees menu
    gets a "prompt for a new PIN" flag. The next time a clock knows it's really that employee (a fingerprint scan, or a
    login with their current PIN), it asks them to set a new one. For someone without a fingerprint, an editor first
    sets a PIN on the page, gives it to them, and sets the flag; they log in with it and reach the prompt. Clocks never
    get permission to change PINs: they insert the new hash into a dedicated insert-only table, and a database trigger
    applies it (and clears the flag) only while the employee's flag is set, refusing it otherwise the way it refuses a
    clock-made admin. The first version of the page only sets a PIN directly. Related: "Employees without a PIN" above
    could reuse this prompt.
15. [ ] *Deferred.* **Text someone who walks away mid-break** (owner, 2026-11-13). Waits on text-message alerts, which
    don't exist yet. When someone scans while on break or at lunch and their punch menu closes without a punch (cancelled
    or timed out), the system sends a text. Since 2026-10-28, 11 menus opened on a break or lunch closed that way, and one
    of those lunches ran 10 hours. Still open: who gets the text (the employee's phone is on
    file, or a manager), its wording, and whether a menu cancelled to check hours counts.
16. [ ] *Deferred.* **Record around opening and closing** (owner, 2026-11-13): once screen off (12) lands, the screen
    recorder also records a short while before business hours start and after they end, where a problem with the
    screen and sleep is most likely. The `asleep` file would carry the next opening time.
17. [ ] *Deferred.* **Mouse clicks get their position** (owner, 2026-11-13): a mouse that reports only movement can't
    give the screen recorder a click's position, so its clicks have no ring. The clock itself would report the exact
    position of the clicks it receives.
