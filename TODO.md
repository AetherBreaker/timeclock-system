# TODO

## Later: after the database setup and the first production clock

- [ ] **Employees without a PIN set their own at the clock.** Allow an `employees` row with no `pin_hash`. When someone
  types that username at a clock, the clock prompts them to set a PIN, then pushes it up to the database. This makes
  adding new admins (and others) by hand much easier: add the row in DBeaver with no PIN, and the person sets it at the
  clock. The owner accepts the security trade-off: until the PIN is set, whoever types that username first sets it.
