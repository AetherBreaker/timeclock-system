# Todo

Deferred features, lowest effort to track. Each needs its own design before it's built.

- **On-screen keyboard** for the clock's text fields (very low priority; a physical keyboard is plugged in
  when needed).
- **Per-clock settings** in the admin menu, starting with UI scaling.
- **Toggle PIN-only login** for an employee from the clock (for now it's set only in the database).
- **Resolution tool** for a clock-created employee the database refused: renumber the employee and their records
  on the unsynced clock so they go through.
- **Hub introduction for LAN sharing:** clocks on different networks (separate gateways) can't find each other by
  broadcast. The hub sees each clock's public address and could tell clocks with the same `lan` value where the
  others are, so they connect directly through their gateways (WireGuard NAT traversal) and keep sharing through a
  hub outage. Needs both routers to cooperate and both internet connections up; a clock restarted during a hub
  outage loses the link. Shelved 2026-10-28 for a future upgrade.
- **Per-group lunch and break switches**, possibly before the first release, depending on the owner's boss.
- **PIN length rule**, if one is wanted later.
