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
