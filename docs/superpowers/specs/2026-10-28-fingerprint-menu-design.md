# Fingerprint menu: design

2026-10-28. Changes timeclock-system (the clock) and timeclock-schema (a migration), and needs a timeclock-backend
release to apply that migration.

## Why

Admins and fingerprint-exempt employees log in by PIN alone and are never prompted to register a print. They may log
in by fingerprint if they choose, but they have no way to put one on file. Nobody can replace a print that stops
scanning, add a second finger, or remove a print.

The owner's concern throughout is **buddy punching**: one person's finger registered on another person's account lets
them punch for each other. So every change to an existing person's prints needs an admin's approval, given for that one
change.

## What the owner decided

**The gear.** A gear icon sits in the top-left corner of the idle screen and of the punch menu (the screen with the
clock in/out buttons), and nowhere else; the version label sits just right of it. Tapping it opens a menu whose only
item, for now, is **Fingerprints**.

**The Fingerprints flow.** A visit ends with the first action that succeeds.

1. The person logs in by PIN username and PIN, so the clock knows whose prints these are.
2. They choose an action:
   - **Re-register a finger**: replaces one of their prints with a new one of the same finger;
   - **Add a finger**: registers a finger they don't have on file yet;
   - **Remove a finger**: deletes one of their prints.
3. Only then does the clock show what that action needs: their prints by finger for re-register and remove, the
   fingers they don't have on file for add. They pick one.
4. An admin approves **that exact action**, immediately before it runs. The approval screen names the action, the
   finger and the person, e.g. "Admin: approve removing Ann Lee's left index print". The admin approves with their own
   finger or PIN. An admin changing their own prints goes through this step too: their PIN login at step 1 doesn't
   count as the approval.
5. The action runs (re-register and add take the usual five touches). When it succeeds, the clock returns to the idle
   screen, where people log in and punch as usual; another action means another visit. When the touches fail, the clock
   returns to step 2, still logged in as that person, and whatever they choose next needs a new approval.

**Who approves:** admins only. Managers can't.

**Finger labels.** Every print is labelled with which finger it is, chosen from the ten fingers at every registration,
including the automatic first registration and the add-employee flow. One print per finger per person.

**The automatic first registration stays as it is today, plus the finger choice.** A regular employee with no print on
file gets it after a PIN login, before any punch buttons, with **Register**, **Manager override** and **Cancel**. It
needs no admin approval: a first print that isn't the employee's own can be spotted in other ways. Admins and
fingerprint-exempt employees never get it; they register through the gear menu if they want a print.

**Removing a regular employee's last print** is allowed with approval. They then get the automatic first registration
at their next PIN login.

**The database change is a migration** (`0002`), not an edit to `0001`: production now holds real rows.

## Calls I made (the owner approved these)

1. **The gear on the punch menu:** tapping it leaves the menu as **Cancel** would, then opens the gear menu. The
   Fingerprints flow still asks for a PIN login there, whoever the punch menu was for.
2. **Finger names:** left thumb, left index, left middle, left ring, left little, and the same for the right hand. The
   database stores them as an enum, e.g. `left_index`.
3. **A change log:** each approved change writes a row to a new `fingerprint_changes` table: the person, the finger,
   the action (`add`, `replace`, `remove`), the approving admin, whether they approved by PIN or by finger (as
   `fingerprint_bypasses.pin_only` records for overrides), when, and which clock. Clocks may only insert into it, as
   with punches. The automatic first registration and the add-employee flow log `add` with no approving admin.
4. **Re-register keeps the old print until the new one enrols.** If the five touches fail or are abandoned, nothing
   changes.
5. **Logging in to the gear menu isn't recorded as a PIN login.** That record exists to audit punches; the change log
   covers this flow.

## Constraints the builder must respect

- **Clocks can't delete prints today**; their grant on `fingerprints` is insert-only. The migration gives them `DELETE`
  on it. The table's change trigger already notifies on `DELETE`, and a clock that's told about a row that's gone
  already deletes its local copy, so other clocks follow.
- **Removals made while offline** take effect on that clock at once (the print stops matching there) and reach the
  database when it reconnects, like its other unsynced records.
- **Re-register is a delete plus an insert of the same finger**, so with one print per finger, the database must apply
  both in one transaction, delete first.
- **The clock's local store keeps fingerprints as JSON of the schema's `Fingerprint` model.** Adding a required
  `finger` field means any print stored locally before the upgrade won't load (the TODO.md item on the local store and
  schema changes). Neither database holds a print today, and the production clock holds none locally, so this can't
  bite now. The builder should still not let it crash a clock: either make the upgrade path read old rows, or confirm
  every clock is empty of prints before deploying.
- **The migration** adds `finger` as `NOT NULL` with no default, plus a unique constraint on `(employee_id, finger)`.
  Since no prints exist, it can require the table to be empty and stop with a clear error otherwise.
- **Deploy order:** timeclock-schema release, then timeclock-backend (it applies migrations at startup), then the clock.
  A clock still on the old code would push prints without a finger, which the database refuses.

## Non-goals

- Anything else in the gear menu.
- Managers approving fingerprint changes.
- Approval for the automatic first registration.
- Changing how a print is matched or how a punch uses it.

## Left to the builder

Screen layouts and wording; how the finger picker looks; how the local store represents an unsynced removal; the exact
migration SQL and grants; how a print refused by the database (for example, two offline clocks registering the same
finger for the same person) is reported, following the existing refused-record handling.
