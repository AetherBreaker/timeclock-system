# TODO

## Later: after the database setup and the first production clock

- [ ] **Employees without a PIN set their own at the clock.** Allow an `employees` row with no `pin_hash`. When someone
  types that username at a clock, the clock prompts them to set a PIN, then pushes it up to the database. This makes
  adding new admins (and others) by hand much easier: add the row in DBeaver with no PIN, and the person sets it at the
  clock. The owner accepts the security trade-off: until the PIN is set, whoever types that username first sets it.
- [ ] **Crash records name the error.** A fatal startup error (2026-10-28: the local store's old-format employee rows)
  left `crash_alerts/*.jsonl` with `"trails": []` and the log with only "Startup stopped", no exception. Record the
  exception and its traceback in both.
- [ ] **The local store survives a schema change.** The clock stores timeclock-schema models as JSON in SQLite with no
  version, so a renamed field, a new required field or an incompatible type change crashes every clock at startup
  (2026-10-28, the name split). Keep the schema version in `meta`. On a change, drop and re-fetch the mirror tables
  (employees, groups, rules, business hours, settings, punch corrections, and synced punches and fingerprints). If an
  unsynced row (pending employee, unsynced punch, fingerprint or bypass) still can't be read, stop with an error naming
  that row; a breaking change to those models needs a conversion step.
- [ ] **Refuse a print that's already registered.** At registration, check the new print against every stored print
  except the one it replaces, and refuse a match. Deferred 2026-10-28: admin approval of adds and re-registrations
  mostly covers it. libfprint doesn't expose its matcher (`fpi_print_bz3_match`) to Python. Either register with five
  identify touches against the stored prints and build the template from them, or compile NBIS bozorth3 into a small C
  extension. Still open: which prints count (inactive employees, the person's own other fingers), what the refusal
  says, and whether it sends an alert.

## Deferred features

Each needs its own design before it's built.

- [ ] **On-screen keyboard** for the clock's text fields (very low priority; a physical keyboard is plugged in when
  needed).
- [ ] **Per-clock settings** in the admin menu, starting with UI scaling.
- [ ] **UI Mirror Mode** Relies upon the item above coming first. Mirrors the position of everything in the UI, so objects on the left and right sides swap places.
- [ ] **Toggle PIN-only login** for an employee from the clock (for now it's set only in the database).
- [ ] **Resolution tool** for a clock-created employee the database refused: renumber the employee and their records on
  the unsynced clock so they go through.
- [ ] **Hub introduction for LAN sharing:** clocks on different networks (separate gateways) can't find each other by
  broadcast. The hub sees each clock's public address and could tell clocks with the same `lan` value where the others
  are, so they connect directly through their gateways (WireGuard NAT traversal) and keep sharing through a hub outage.
  Needs both routers to cooperate and both internet connections up; a clock restarted during a hub outage loses the
  link. Shelved 2026-10-28 for a future upgrade.
- [ ] **Per-group lunch and break switches**, possibly before the first release, depending on the owner's boss.
- [ ] **PIN length rule**, if one is wanted later.
- [ ] **Testing and debug menus.** For example, tapping the reader icon a few times opens a verbose log of the reader's
  scans, for troubleshooting someone's fingerprint. It can include a capture mode showing each press's picture with the
  matcher's detail points circled (failed presses included), in memory only, never saved or logged. Samples on
  2026-10-29 showed ridge cut-offs at the window's left edge counted as detail points, and a faint line across the
  middle of every press.
- [ ] **Turn the screen off after hours, waking on touch** (owner: later, 2026-10-29). sway can power the display off
  (`output * power off`); first check on each clock's monitor that its touch panel keeps working while the screen sleeps,
  and catch the waking touch with an overlay so it doesn't press a key on the page.
