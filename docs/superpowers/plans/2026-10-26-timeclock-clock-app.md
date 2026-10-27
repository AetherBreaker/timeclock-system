# Clock app and kiosk (build step 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for
> tracking. **Implementers run at medium effort** (dispatch each task's subagent with `effort: "medium"`):
> everything a task needs is pinned here. Where this plan and your instinct differ, follow the plan and
> report the disagreement. Check each step off in this file as it lands; the owner reads it while work runs.

**Goal:** The fingerprint clock in this repo: the app container (NiceGUI page, scanner thread on a patched
libfprint, local SQLite, sync to PostgreSQL through the backend's relay) and the kiosk container (cage +
Firefox), deployed by Coolify to timeclock-dev against the test database.

**Architecture:** One Python process. A daemon scanner thread owns libfprint behind a `Reader` protocol (real
backend on Linux, a fake in tests). A `Clock` service owns the domain rules over a `LocalStore` (SQLite) and
`timeclock_schema`'s models and shift logic. `DatabaseSync` mirrors PostgreSQL into the store and pushes
local records. A `KioskController` holds the screen state machine and drives the reader's mode; the NiceGUI
page only renders it. aeth_ext owns logging, signals, alerts and the shutdown.

**Tech Stack:** Python 3.14, NiceGUI 3.18 (uvicorn 0.54, uvloop), PyGObject 3.58 + libfprint 1.94.100
(patched, built in the image), psycopg 3.3 (async), bcrypt 5, SQLite, aeth_ext 10, devkit-container 2.3,
timeclock-schema 0.2, Docker Compose on Coolify, cage + Firefox ESR.

**Spec:** `D:\SFT Software Projects\SFT Workspace\timeclock-system\docs\superpowers\specs\2026-10-26-timeclock-design.md`
§5, §6 (not §6.7), §7, §9.1 (not `discovery`), §9.3, §13 step 3, Appendix A. "R*n*" = Decided item *n* in
`2026-10-23-timeclock-requirements.md` beside it. Read the spec section a task cites before starting it.

**Depends on (released or deployed first):** aeth-ext 10.0.0, devkit-container 2.3.0 and timeclock-schema
0.2.0 on SFTPyPI (Tasks 1–14 need only these); the hub release with `timeclock-system-dev` at `10.8.0.26`
(wireguard-hub plan); timeclock-backend deployed with both databases (Task 15 only).

## Global Constraints

- Work on branch `feat/clock-app` off `main`; the first commit adds this plan file. Stage explicit paths only.
- Run everything under `uv run` (`AGENTS.md`). Commands are Git Bash on the workstation unless marked
  "on timeclock-dev" (run there over `ssh timeclock-dev`).
- Style: 2-space indent, line length 135, Google docstrings, no `from __future__ import annotations` (ruff
  bans it), PEP 758 `except A, B:` is valid. Comments carry reasoning, densely. No helper of ≤ 4 lines or
  single use unless a lint rule forces it (`AGENTS.md`).
- Conventional Commits, scope = module (`feat(scanner): …`); end each message with the attribution line your
  session's instructions give (currently `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`).
- Tests: targeted tests while working (`uv run pytest tests/test_x.py -v`); the full `uv run pytest` once, in
  Task 14. Tests don't define intent (`AGENTS.md`); rewrite a test a later task makes obsolete.
- Tests never need the reader or a live VPS. The scanner is tested with `FakeReader` (Task 6). Sync tests need a
  throwaway PostgreSQL 18 in Docker Desktop: **say you're pulling `postgres:18` before you pull it**, then
  (PowerShell) `docker run --rm -d --name timeclock-clock-pg -e POSTGRES_PASSWORD=test -p 127.0.0.1:55433:5432 postgres:18`
  and `$env:TIMECLOCK_TEST_PG_DSN = "postgresql://postgres:test@127.0.0.1:55433/postgres"`. Those tests skip
  without the variable.
- Floors: `aeth-ext>=10.0.0`, `devkit-container>=2.3.0`, `timeclock-schema>=0.2.0` (all from SFTPyPI).
- Use `timeclock_schema`'s names exactly as its plan defines them (`2026-10-26-timeclock-schema.md` in this
  folder, Tasks 2–6 Produces blocks): models `Employee`, `Group`, `Fingerprint`, `Punch`, `PunchCorrection`,
  `Override`, `RuleRow`, `BusinessHours`, `Settings`; enums `PunchKind`, `OverrideReason`, `RuleAction`;
  `REVEAL`; `wire.Notification`, `wire.NOTIFY_CHANNEL`; `shifts.build_timeline`, `worked_time`, `week_bounds`,
  `EmployeeState`, `Timeline`; `rules.validate_rule`, `applicable_rules`, `evaluate`, `RuleContext`,
  `RuleValidationError`, `ValidatedRule`; `migrations.load_migrations`.
- Time: wall-clock times are aware UTC (`datetime.now(UTC)`); local time only through aeth_ext's
  `settings.tz` (R41.4). Never `datetime.now()` without a zone, never the host zone.
- Fingerprint templates stay `SecretBytes` (R15). Never log a template, a PIN or a `pin_hash`.
- Hardware (timeclock-dev): test on the real device, not long VM runs; say what you'll install before
  installing it; never rebuild the install image. Steps marked **Owner** are the owner's (Coolify, secrets,
  the database through DBeaver); stop and ask for them. `.env` holds live credentials: never print it.
- The whole spec's "(spec call)" items are already decided for this plan; don't reopen them.

## Review Focus

1. **A finger left on the reader after a capture** (a resting thumb, or the tail of a press, seen at 0.54 s in
   the spike). The scanner must not re-capture until the finger lifts or 2 s pass. Test:
   `test_rearm_waits_for_finger_off` (Task 6).
2. **Offline, then the connection comes back mid-push.** Every local punch reaches PostgreSQL exactly once;
   a re-push after a dropped connection inserts nothing new. Tests: `test_push_is_idempotent` and
   `test_reconnects_after_the_connection_is_killed` (Task 8).
3. **A shift open across Monday 00:00 local** (clock-in Sunday 22:00). The weekly prune keeps the clock-in,
   so Monday's clock-out is offered and accepted. Test: `test_prune_keeps_an_open_shift` (Task 5).
4. **The owner edits or deletes rows in DBeaver, and the clock's own pushed punch echoes back as a
   notification.** One local row per punch; a deleted row disappears locally. Tests:
   `test_echo_of_own_punch_keeps_one_row`, `test_owner_delete_removes_the_local_row` (Task 8).
5. **Business hours end mid-interaction.** The reader stays open until the screen is back at idle and no
   finger has touched for 16 s. Test: `test_reader_closes_only_when_idle_after_hours` (Task 9).

## File Structure

| Path | Responsibility |
| --- | --- |
| `pyproject.toml` | dependencies, scripts, `[tool.docker]`, lint config |
| `src/timeclock_system/__main__.py` | aeth_ext constants, `run_app()` |
| `src/timeclock_system/settings.py` | `ClockSettings` (env) |
| `src/timeclock_system/reader_access.py` | startup script: give the reader to uid 999 (R14.7) |
| `src/timeclock_system/store.py` | `LocalStore`: SQLite mirror, unsynced queue, prune |
| `src/timeclock_system/hours.py` | business-hours lookup (R40.3) |
| `src/timeclock_system/clock.py` | `Clock`: punches, PINs, overrides, registration, rules, gallery, LAN seams |
| `src/timeclock_system/scanner.py` | scanner thread, `Reader` protocol, events, backoff, watchdog |
| `src/timeclock_system/fprint_reader.py` | libfprint `Reader` (Linux only) and the hardware probe CLI |
| `src/timeclock_system/sync.py` | `DatabaseSync`: catch-up, push, notifications, status |
| `src/timeclock_system/heartbeat.py` | heartbeat file, gated on the scanner watchdog |
| `src/timeclock_system/crash_alerts.py` | crash-alert evidence files (R20) |
| `src/timeclock_system/ui/text.py` | every on-screen string and formatter |
| `src/timeclock_system/ui/controller.py` | `KioskController`: screens, timers, reader mode |
| `src/timeclock_system/ui/page.py` | NiceGUI page rendering the controller |
| `src/timeclock_system/app.py` | wiring, tasks, shutdown callbacks, `ui.run` |
| `docker/Dockerfile` | devkit template; `builder-cached`, `builder-setup` and `final` windows (Task 12) |
| `docker/libfprint/uru4000-no-heat-model.patch` | R40.4 patch |
| `docker/compose.yaml` | app + kiosk services |
| `docker/kiosk/` | kiosk image: `Dockerfile`, `kiosk.sh`, `policies.json`, `autoconfig.js`, `kiosk.cfg` |
| `image/dev-device/…` | unattended-upgrades and the hostname prompt (file edits only) |
| `tests/…` | one test module per source module |

---

### Task 1: Project configuration and the rendered Docker files

**Files:**
- Modify: `pyproject.toml`, `docker/compose.yaml` (empty today; **don't delete it**: setup-project would then
  find and edit `backend/docker/compose.yaml`)
- Create: `src/timeclock_system/__main__.py` (replace the docstring-only file)
- Rendered by setup-project: `docker/Dockerfile`, `docker/compose.yaml`

**Interfaces:**
- Produces: console scripts `run-app-timeclock-system = "timeclock_system.__main__:run_app"` and
  `timeclock-reader-access = "timeclock_system.reader_access:main"`; `[tool.docker]` with `services =
  ["timeclock-system"]`, `required_persisted_dirs = ["persisted_data"]`, `wireguard = true`,
  `startup_scripts = ["timeclock-reader-access"]`; the template's empty `builder-setup`, `builder` and `final`
  windows in `docker/Dockerfile`.
- Produces (`__main__.py`): `PROJECT_NAME = "timeclock-system"`, `REQUIRE_LOG_SERVER = False` (R16.3),
  `SHUTDOWN_ON_LOG_REJECTION = False` (R16.5); `def run_app() -> None` (body in Task 11; until then
  `raise NotImplementedError("wired in Task 11")`).

- [x] **Step 1: Branch and plan commit** — `git switch -c feat/clock-app`, then
  `git add docs/superpowers/plans/2026-10-26-timeclock-clock-app.md && git commit -m "docs(plans): add the clock app plan"`.
- [x] **Step 2: Edit `pyproject.toml`** (keep everything not listed; tombi formatting as the file has it):

```toml
[project]
  description = "Fingerprint time clock: the clock app and its kiosk"
  dependencies = [
    "aeth-ext>=10.0.0",
    "bcrypt>=5.0",
    "devkit-container>=2.3.0",
    "nicegui>=3.18,<4",
    "psycopg[binary]>=3.3.6",
    "PyGObject>=3.58.1; sys_platform == 'linux'",  # built in the image (builder-setup window); not on Windows
    "timeclock-schema>=0.2.0",
    "uvicorn>=0.54,<0.55",                         # pinned: app.py replaces Server.capture_signals (R21)
    "uvloop>=0.23; sys_platform == 'linux'",
  ]

  [project.scripts]
    run-app-timeclock-system = "timeclock_system.__main__:run_app"
    timeclock-reader-access  = "timeclock_system.reader_access:main"

[build-system]
  requires      = ["uv_build>=0.11.2,<0.12.0"]
  build-backend = "uv_build"

[tool.docker]
  services                = ["timeclock-system"]
  required_persisted_dirs = ["persisted_data"]
  wireguard               = true
  startup_scripts         = ["timeclock-reader-access"]
```

Also: `[tool.uv.sources]` gains `aeth-ext = [{ index = "SFTPyPI" }]` and `timeclock-schema = [{ index = "SFTPyPI" }]`;
`[tool.ruff.lint.isort]` gains `known-third-party = ["aeth_ext", "timeclock_schema"]`; `[tool.pyright]` gains
`pythonPlatform = "Linux"` (the app only runs on Linux; Windows dev runs see Linux stubs, e.g. `os.chown`) and
`ignore = ["src/timeclock_system/fprint_reader.py"]` with the comment `# needs PyGObject and libfprint, which only exist in the image`,
and its `exclude` adds `"schema"` if the schema plan's submodule exists.

- [x] **Step 3: `__main__.py`** with the three constants and the placeholder `run_app` (module docstring:
  `"""Entry point; stays import-light so initialize() runs before anything else."""`).
- [x] **Step 4: Lock** — `uv lock && uv sync`. Expected: resolves aeth-ext 10.x, devkit-container 2.3.x and
  timeclock-schema 0.2.x from SFTPyPI; PyGObject and uvloop are skipped on Windows.
- [x] **Step 5: Give compose a services key** — write `docker/compose.yaml` as the single line `services:`.
- [x] **Step 6: Commit** (setup-project refuses an uncommitted `[tool.docker]`):
  `git add pyproject.toml uv.lock docker/compose.yaml src/timeclock_system/__main__.py && git commit -m "chore(docker): configure the clock as a devkit WireGuard service"`.
- [x] **Step 7: Render** — `uv run poe setup-project --dry-run`, read it, then `uv run poe setup-project -y`.
  Expected: it adds the `timeclock-system` service to compose (container name, build args with
  `GIT_REPO: https://github.com/AetherBreaker/timeclock-system.git`, the `/app/persisted_data` volume, the
  WireGuard env keys, `cap_add`, the two-file healthcheck) and replaces `docker/Dockerfile` with the 2.3.0
  template (trixie, three empty windows), and commits. If it leaves compose alone with "no top-level
  services", check Step 5's file and rerun.
- [x] **Step 8: Verify** — `grep -n "trixie\|!window" docker/Dockerfile` shows both trixie `FROM`s and the
  `builder-setup`, `builder` and `final` windows; `uv run ruff check` and `uv run pyright` are clean.

---

### Task 2: Settings and the reader-access startup script

**Files:**
- Create: `src/timeclock_system/settings.py`, `src/timeclock_system/reader_access.py`
- Test: `tests/test_settings.py`, `tests/test_reader_access.py`

**Interfaces:**
- Produces (`settings.py`): `class ClockSettings(aeth_ext.settings.BaseSettings)` with
  `db_host: str = "10.8.0.24"` (alias `TIMECLOCK_DB_HOST`), `db_port: int = 5432` (`TIMECLOCK_DB_PORT`),
  `db_name: str` (`TIMECLOCK_DB_NAME`, required), `db_user: str` (`TIMECLOCK_DB_USER`, required),
  `db_password: SecretStr` (`TIMECLOCK_DB_PASSWORD`, required); an after-validator raising
  `ValueError("INSTANCE_ID must be set on a clock")` when aeth_ext's `instance_id` is None; properties
  `store_path` (`persisted_dir_loc / "timeclock.sqlite3"`), `branding_dir` (`/ "branding"`),
  `crash_alerts_dir` (`/ "crash_alerts"`), `heartbeat_path` (`log_loc_folder / "heartbeat.txt"`); and
  `def conninfo(self) -> str` via `psycopg.conninfo.make_conninfo(host=, port=, dbname=, user=, password=,
  connect_timeout=10, keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
  application_name=f"timeclock-system-{instance_id}")` (keepalives: §6.6).
- Produces (`reader_access.py`): `READER_VENDOR = "05ba"`, `READER_PRODUCT = "000a"`, `NONROOT_UID = 999`;
  `def find_reader_nodes(sysfs: Path = Path("/sys/bus/usb/devices"), devfs: Path = Path("/dev/bus/usb")) -> list[Path]`
  (for each dir whose `idVendor`/`idProduct` files read `05ba`/`000a`, the node
  `devfs / f"{busnum:03d}" / f"{devnum:03d}"` from its `busnum`/`devnum` files; unreadable dirs skipped);
  `def main() -> int` (chown each node to 999:999; return 0 if any, else print
  `reader-access: fingerprint reader not found` to stderr and return 1). It runs as root before aeth_ext
  initializes, so plain `print` is right (devkit-container logs it). Port of Appendix A's shell script.

- [x] **Step 1: Failing tests.** `test_reader_access.py` builds a fake sysfs under `tmp_path` (dirs `1-1` with
  `idVendor=05ba`, `idProduct=000a`, `busnum=1`, `devnum=7`; `1-2` with `idVendor=046d`; `usb1` with no id
  files) and patches `os.chown` with `monkeypatch.setattr(os, "chown", fake, raising=False)` (Windows has none):

```python
def test_finds_only_the_4500(tmp_path): assert find_reader_nodes(sysfs, devfs) == [devfs / "001" / "007"]
def test_main_chowns_to_999_and_returns_0(...): calls == [(devfs / "001" / "007", 999, 999)] and rc == 0
def test_main_without_reader_returns_1_and_says_so(capsys, ...): rc == 1 and "fingerprint reader not found" in capsys.readouterr().err
```

`test_settings.py` (env via `monkeypatch.setenv`; `ClockSettings()` constructed directly):
`test_requires_instance_id` (no `INSTANCE_ID` → `ValidationError`), `test_defaults_and_paths`
(`db_host == "10.8.0.24"`, `store_path.name == "timeclock.sqlite3"`, `heartbeat_path.name == "heartbeat.txt"`),
`test_conninfo_carries_keepalives` (`"keepalives_idle=30"` and `"application_name=timeclock-system-dev"` in it).
- [x] **Step 2: Run them; they fail.** `uv run pytest tests/test_settings.py tests/test_reader_access.py -v`
- [x] **Step 3: Implement both modules** per the Interfaces block (`main` reads the default paths).
- [x] **Step 4: Run them; they pass**, plus `ruff check` and `pyright`.
- [x] **Step 5: Commit** `feat(settings): add the clock settings and the reader-access startup script`.

---

### Task 3: Local store

**Files:**
- Create: `src/timeclock_system/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `timeclock_schema.models` (all row models), `REVEAL`.
- Produces: `Origin = Literal["local", "peer", "db"]`; `MirrorTable = Literal["employees", "groups", "rules",
  "business_hours"]`; `class LocalStore` — synchronous, one `sqlite3` connection (`check_same_thread=False`,
  WAL, `foreign_keys=OFF`) guarded by a `threading.Lock`; callers on the event loop use `asyncio.to_thread`:
  - `__init__(self, path: Path)`, `close(self) -> None`
  - `replace_table(self, table: MirrorTable, rows: Sequence[TimeclockModel]) -> None`;
    `upsert(self, table: MirrorTable, row) -> None`; `delete(self, table: MirrorTable, row_id: int) -> None`;
    `load(self, table: MirrorTable) -> list[...]` (rows validated back into their model)
  - `set_settings(self, s: Settings) -> None`; `get_settings(self) -> Settings` (`Settings()` if none stored)
  - `insert_punch(self, p: Punch, *, origin: Origin, override: Override | None = None) -> bool` — `False` if the
    id exists; `synced = 0` only for `origin == "local"`; the override goes in the same transaction
  - `upsert_db_punch(self, p: Punch) -> None` (updates the stored JSON if the id exists, keeping its origin
    and synced flag; else inserts with origin `db`); `delete_punch(self, punch_id: UUID) -> None`
  - `replace_db_punches(self, punches: Sequence[Punch]) -> None` — deletes origin-`db` rows, inserts the given
    ones as `db` unless the id already exists (local or peer copies win)
  - `punches(self, employee_id: int | None = None) -> list[Punch]`
  - `upsert_correction(self, c: PunchCorrection)`, `delete_correction(self, cid: UUID)`,
    `replace_corrections(self, cs: Sequence[PunchCorrection])`, `corrections(self, employee_id: int | None = None) -> list[PunchCorrection]`
    (by joining on the punch's employee)
  - `insert_fingerprint(self, f: Fingerprint, *, origin: Origin) -> bool`; `upsert_db_fingerprint(self, f)`;
    `delete_fingerprint(self, fid: UUID)`; `replace_db_fingerprints(self, fs)`; `fingerprints(self) -> list[Fingerprint]`
  - `unsynced_punches(self) -> list[tuple[Punch, Override | None]]` (oldest `at` first);
    `unsynced_fingerprints(self) -> list[Fingerprint]`; `mark_punch_synced(self, pid: UUID)`;
    `mark_fingerprint_synced(self, fid: UUID)`
  - `prune(self, before: datetime, keep_from: Mapping[int, datetime]) -> int` — deletes synced punches with
    `at < before` unless `at >= keep_from[employee_id]`, then corrections whose punch is gone; returns rows deleted
  - `get_meta(self, key: str) -> str | None`; `set_meta(self, key: str, value: str) -> None`
- Tables (each stores the model as `model_dump_json(context=REVEAL)` in a `json` column, plus the columns the
  queries need): `employees(id INTEGER PK, json)`, `groups(id INTEGER PK, json)`, `rules(id INTEGER PK, json)`,
  `business_hours(id INTEGER PK, json)`, `settings(id INTEGER PK CHECK (id = 1), json)`,
  `punches(id TEXT PK, employee_id INTEGER NOT NULL, at TEXT NOT NULL, origin TEXT NOT NULL, synced INTEGER NOT NULL, json)`,
  `overrides(id TEXT PK, punch_id TEXT NOT NULL, json)`, `punch_corrections(id TEXT PK, punch_id TEXT NOT NULL, json)`,
  `fingerprints(id TEXT PK, employee_id INTEGER NOT NULL, origin TEXT NOT NULL, synced INTEGER NOT NULL, json)`,
  `meta(key TEXT PK, value TEXT)`. `at` is ISO 8601 UTC text, so it sorts.

- [x] **Step 1: Failing tests** (`T = datetime(2026, 10, 27, 12, tzinfo=UTC)`; small factories in the module):

```python
def test_round_trip_keeps_secrets(store): store.insert_fingerprint(fp(b"\x00tmpl"), origin="local") ; store.fingerprints()[0].template.get_secret_value() == b"\x00tmpl"
def test_insert_punch_ignores_duplicate_ids(store): first True, second False, len(store.punches()) == 1
def test_local_punch_and_override_are_unsynced_together(store): unsynced_punches() == [(p, o)]; after mark_punch_synced → []
def test_peer_and_db_punches_are_never_pushed(store): unsynced_punches() == []
def test_replace_db_punches_keeps_local_and_peer_copies(store): local p1 + peer p2 + db p3; replace_db_punches([p1db, p4]) → ids == {p1, p2, p4}, p1 still origin local
def test_upsert_db_punch_updates_an_existing_local_row(store): owner amend of at; the row keeps origin local, synced 1
def test_prune_deletes_old_synced_punches_but_not_unsynced_or_kept(store): returns the count; unsynced old punch survives; keep_from survivor stays
def test_prune_drops_orphaned_corrections(store)
def test_settings_default_when_missing(store): store.get_settings() == Settings()
def test_meta_round_trip(store)
def test_naive_timestamps_never_stored(store): every stored at ends with "Z" or "+00:00"
```

- [x] **Step 2: Run; they fail.** `uv run pytest tests/test_store.py -v`
- [x] **Step 3: Implement `store.py`** per the Interfaces block.
- [x] **Step 4: Run; they pass**, plus `ruff check` and `pyright`.
- [x] **Step 5: Commit** `feat(store): add the local SQLite store`.

---

### Task 4: Business hours

**Files:**
- Create: `src/timeclock_system/hours.py`
- Test: `tests/test_hours.py`

**Interfaces:**
- Consumes: `BusinessHours`.
- Produces: `def applicable_hours(rows: Iterable[BusinessHours]) -> tuple[BusinessHours, ...]` — the single
  lookup per-clock hours would change later (R40.3); today it returns every row. Docstring says exactly that.
  `def is_open(rows: Iterable[BusinessHours], at: datetime, tz: ZoneInfo) -> bool` — converts `at` to local,
  true when some applicable row has `weekday == local.isoweekday()` and `opens <= local.time() < closes`;
  naive `at` → `ValueError`.

- [x] **Step 1: Failing tests:** `test_open_inside_a_window`, `test_closes_is_exclusive_and_opens_inclusive`,
  `test_weekday_is_iso_monday_one` (2026-10-26 is a Monday: a weekday-1 row opens it),
  `test_two_windows_in_a_day` (08–12 and 13–17: 12:30 closed), `test_no_rows_means_closed`,
  `test_uses_local_time_across_dst` (2026-11-01, America/New_York: 08:00 local is 13:00 UTC → open for an
  08–17 Sunday row), `test_naive_at_rejected`.
- [x] **Step 2: Run; they fail.** — **Step 3: Implement.** — **Step 4: Run; they pass** (+ ruff, pyright).
- [x] **Step 5: Commit** `feat(hours): add the business-hours lookup`.

---

### Task 5: The Clock service

**Files:**
- Create: `src/timeclock_system/clock.py`
- Create: `src/timeclock_system/scanner.py` holding only `@dataclass(frozen=True) class GalleryPrint`:
  `print_id: UUID`, `employee_id: int`, `template: bytes` (Task 6 adds the rest of the module)
- Test: `tests/test_clock.py`

**Interfaces:**
- Consumes: `LocalStore` (Task 3), `hours.applicable_hours` (Task 4), `timeclock_schema` models, `shifts`, `rules`.
- Produces:
  - `ChangeTable = Literal["employees", "groups", "fingerprints", "punches", "punch_corrections", "rules", "business_hours", "settings"]`
  - `@dataclass(frozen=True) class OverrideGrant`: `manager_id: int`, `reason: OverrideReason`, `pin_only: bool`
  - `@dataclass(frozen=True) class MenuInfo`: `employee_id: int`, `name: str`, `state: EmployeeState`,
    `since: datetime | None` (the last applied event's time when not off), `valid_next: tuple[PunchKind, ...]`,
    `hours_this_week: timedelta`
  - `@dataclass(frozen=True) class PunchOutcome`: `punch: Punch`, `warnings: tuple[str, ...]`, `hours_this_week: timedelta`
  - `class PunchRefused(Exception)` with `message: str`
  - `GalleryPrint` (in `scanner.py`, see Files): what `Clock.gallery` returns and the scanner consumes.
  - `class Clock`:
    - `__init__(self, store: LocalStore, *, instance_id: str, tz: ZoneInfo, alert: Callable[[str, str], None], now: Callable[[], datetime] = lambda: datetime.now(UTC))`
    - `async def load(self) -> None` (caches employees, groups, rules, business hours, settings from the store)
    - `def notify_changed(self, tables: Iterable[ChangeTable]) -> None` (reloads the affected caches, then calls
      every change listener with `frozenset(tables)`; reloads run synchronously — the caches are small)
    - `def add_change_listener(self, cb: Callable[[frozenset[ChangeTable]], None]) -> None`
    - `def add_record_listener(self, cb: Callable[[Punch | Fingerprint], None]) -> None` — **LAN seam**: called
      on the loop after every local punch or registration is stored; Task 11 wires `sync.wake` here, step 4
      adds the LAN sender
    - `async def insert_peer_record(self, record: Punch | Fingerprint) -> bool` — **LAN seam**: stores with origin
      `peer` (never pushed), ignores a known UUID, then `notify_changed`; returns whether it was new
    - `def employee(self, employee_id: int) -> Employee | None` (active or not); `def business_hours(self) -> tuple[BusinessHours, ...]`;
      `def settings(self) -> Settings`
    - `async def verify_pin(self, username: str, password: str) -> Employee | None` — active employee with that
      `pin_username` and `bcrypt.checkpw(password.encode(), pin_hash.encode())` run in `asyncio.to_thread`
    - `async def has_fingerprint(self, employee_id: int) -> bool`
    - `def override_manager(self, manager: Employee, employee_id: int) -> bool` — `manager.active and
      manager.is_manager and manager.id != employee_id` (a manager can't override for themselves, spec call)
    - `async def menu_info(self, employee_id: int) -> MenuInfo`
    - `async def record_punch(self, employee_id: int, kind: PunchKind, *, override: OverrideGrant | None = None) -> PunchOutcome`
    - `async def register_fingerprint(self, employee_id: int, template: bytes) -> Fingerprint`
    - `async def gallery(self) -> list[GalleryPrint]`
    - `async def catch_up_since(self) -> datetime` — `min(week_start, clock_in.at of every open shift held locally)`
    - `async def prune_week(self) -> int`
    - `async def run_week_rollover(self) -> None` — prunes at start, then sleeps until the next Monday 00:00 local
      (`week_bounds(now, tz)[1]` + 1 s) and prunes, forever
- Behavior pinned:
  - Timeline: `build_timeline(store.punches(employee_id), store.corrections(employee_id))`. Hours this week:
    `worked_time(timeline, *week_bounds(now, tz), now=now, subtract_lunch=s.subtract_lunch, subtract_breaks=s.subtract_breaks)` (R31.1).
  - `record_punch`: unknown or inactive employee → `PunchRefused("Unknown employee")`; `kind not in
    timeline.valid_next` → `PunchRefused("That punch isn't available right now")` (the guard, R26.3, re-checked
    here); rules: `evaluate(applicable_rules(<validated rules>, employee_id=, group_id=), RuleContext(...))`, every
    `REFUSE` violation → `PunchRefused("; ".join(messages))` and nothing stored; `WARN` messages become
    `warnings`. Then `Punch(id=uuid4(), employee_id, kind, at=now, instance_id)`, and with a grant
    `Override(id=uuid4(), punch_id=punch.id, employee_id, manager_id, reason, pin_only, at=now, instance_id)`;
    `store.insert_punch(..., origin="local", override=...)`; record listeners; `notify_changed({"punches"})`.
  - Rules cache: each `RuleRow` through `validate_rule`; a `RuleValidationError` is skipped and alerted once per
    `(row.id, row.updated_at)` with `alert(f"Bad rule row {row.id}", str(err))` (§4.6).
  - `gallery`: every fingerprint of an active employee, ordered by that employee's latest punch recorded at
    this clock (`instance_id == self.instance_id`), newest first; employees with none last, then by print id
    (R36.4). `GalleryPrint(print_id=f.id, employee_id=f.employee_id, template=f.template.get_secret_value())`.
  - `prune_week`: `keep_from` = each employee's open shift's `clock_in.at` (R41); `before` = this week's start.

- [x] **Step 1: Failing tests** (store on `tmp_path`; employees seeded with
  `bcrypt.hashpw(b"1234", bcrypt.gensalt(rounds=6, prefix=b"2a"))`, like pgcrypto's `$2a$06$`; a fixed `now`):

```python
async def test_verify_pin_accepts_pgcrypto_style_hash(): (await clock.verify_pin("12", "1234")).id == 1 ; wrong → None
async def test_verify_pin_refuses_inactive(): None
async def test_menu_info_for_a_clocked_in_employee(): state WORKING, since == T, valid_next == (CLOCK_OUT, BREAK_START, LUNCH_START)
async def test_record_punch_stores_unsynced_and_notifies(): outcome.punch.instance_id == "dev"; store.unsynced_punches()[0][0] == outcome.punch; listener got the punch
async def test_record_punch_refuses_invalid_kind(): PunchRefused on CLOCK_OUT while off; nothing stored
async def test_override_is_stored_with_its_punch(): unsynced_punches()[0][1].pin_only is True and .reason is FINGERPRINT_SKIPPED
async def test_refuse_rule_blocks_and_warn_rule_warns(): register a test kind (as the schema plan's tests do) and unregister after
async def test_bad_rule_row_alerts_once(): two loads of the same bad row → alert called once; after updated_at changes → twice
async def test_hours_this_week_subtracts_lunch_by_default(): 8 h shift with 30 min lunch → 7 h 30 min
async def test_gallery_orders_by_latest_punch_here(): employee who punched here most recently first; inactive employee's print absent
async def test_override_manager_rules(): manager ok; non-manager False; self False; inactive manager False
async def test_register_fingerprint_stores_local_unsynced(): unsynced_fingerprints() has it; record listener called
async def test_insert_peer_record_is_never_pushed_and_dedupes(): True then False; unsynced_punches() == []
async def test_prune_keeps_an_open_shift(): clock-in 2026-11-08 22:00 America/New_York (Sunday); prune on Monday → kept; Monday CLOCK_OUT accepted
async def test_catch_up_since_reaches_back_to_an_open_shift()
```

- [x] **Step 2: Run; they fail.** `uv run pytest tests/test_clock.py -v`
- [x] **Step 3: Implement `clock.py`** per the Interfaces block. Store calls go through `asyncio.to_thread`.
- [x] **Step 4: Run; they pass** (+ ruff, pyright).
- [x] **Step 5: Commit** `feat(clock): add the clock service`.

---

### Task 6: Scanner thread

**Files:**
- Modify: `src/timeclock_system/scanner.py` (Task 5 created it with `GalleryPrint`)
- Test: `tests/test_scanner.py` (defines `FakeReader`)

**Interfaces:**
- Consumes: `GalleryPrint` (Task 5).
- Produces:
  - Events (`@dataclass(frozen=True)`): `Matched(print_id: UUID, employee_id: int)`, `NoMatch()`,
    `Retry(message: str)`, `FingerDown()`, `EnrollStage(done: int, total: int)`, `Enrolled(template: bytes)`,
    `EnrollFailed(reason: Literal["timeout", "error"])`; `ScannerEvent` = their union
  - `class ReaderMode(StrEnum)`: `CLOSED = "closed"`, `READY = "ready"`, `ENROLL = "enroll"`
  - Exceptions a `Reader` raises: `ScanCancelled`, `RetryScan(message)`, `ReaderGone(message)`, `ReaderTooHot`,
    `ReaderFailure(message)`
  - `class CancelToken(Protocol)`: `def cancel(self) -> None`
  - `class Reader(Protocol)` — every method blocks and runs only on the scanner thread:
    `new_cancel() -> CancelToken`; `open() -> None`; `close() -> None`;
    `identify(gallery: Sequence[GalleryPrint], cancel: CancelToken, on_finger: Callable[[], None]) -> UUID | None`
    (the matched print id, or None); `enroll(cancel: CancelToken, on_stage: Callable[[int, int], None]) -> bytes`
    (the serialized template); `wait_finger_off(timeout: float) -> None`
  - `@dataclass(frozen=True) class ScannerTimings`: `retry_pause=0.5`, `backoff_start=1.0`, `backoff_max=30.0`,
    `too_hot_pause=60.0`, `rearm_every=60.0`, `finger_off_timeout=2.0`, `enroll_idle_timeout=30.0` (seconds; §6.2,
    R40.2). Tests pass small values.
  - `class Scanner`: `__init__(self, reader: Reader, post: Callable[[ScannerEvent], None], on_fatal: Callable[[str], None], *, timings: ScannerTimings = ScannerTimings(), monotonic: Callable[[], float] = time.monotonic)`;
    `start()` (daemon thread named `scanner`); `stop()`; `set_mode(mode: ReaderMode)`; `set_gallery(prints: Sequence[GalleryPrint])`;
    property `mode`; `busy_seconds() -> float` (how long the current blocking reader call has run; 0.0 when none).
- The one matcher boundary (R36.3): `Scanner` calls `reader.identify` in exactly one method,
  `_identify_once(self) -> Matched | NoMatch | Retry`; version 2 replaces the `Reader`'s `identify`.
- Thread loop, pinned (`post` and `on_fatal` are called on the scanner thread):
  - **CLOSED**: close the reader if open; wait for a mode change.
  - **READY**: open if needed; one identify against the current gallery, with a `threading.Timer(rearm_every, token.cancel)`
    (the re-arm that feeds the watchdog); `on_finger` posts `FingerDown`. Result: match → `Matched` (employee from
    the gallery entry); None → `NoMatch`; then `wait_finger_off(finger_off_timeout)`. `ScanCancelled` → loop
    silently (timer, mode change, gallery change or stop). `RetryScan` → post `Retry(message)`, pause
    `retry_pause`. `ReaderTooHot` → post `Retry("The reader is warming down, try again in a minute")`, pause
    `too_hot_pause`. `ReaderFailure` → pause the current backoff (starts at `backoff_start`, doubles, capped at
    `backoff_max`, reset by any successful identify). `ReaderGone` → `on_fatal(message)`, then the thread ends.
    **No path loops without one of these pauses or a blocking call** (anti-goal: the spike spun 3.3M times).
  - **ENROLL**: open if needed; `enroll` with a timer of `enroll_idle_timeout` that cancels, restarted on every
    stage; each stage posts `EnrollStage(done, total)`; success posts `Enrolled(template)`; the timer firing
    posts `EnrollFailed("timeout")`; any other error posts `EnrollFailed("error")` (and `ReaderGone` is fatal as
    above). Then the mode becomes READY by itself.
  - `set_mode`/`set_gallery`/`stop` cancel the current call's token. Every blocking reader call records its
    start time for `busy_seconds()`. Pauses wait on an event that a mode change or `stop` sets, so they end early.

- [x] **Step 1: Failing tests.** `FakeReader` (test module): scripted outcomes per `identify` call (a deque of
  `UUID | None | Exception`); with the deque empty, `identify` blocks until its token is cancelled, then raises
  `ScanCancelled`; records `open`/`close`/`identify` calls and `wait_finger_off` timeouts; `enroll` reports
  stages from a script. Events are collected in a `queue.Queue`; helpers wait with a 2 s timeout.

```python
def test_ready_posts_match_and_waits_for_finger_off(): Matched(print_id=a, employee_id=1); wait_finger_off called with 2.0 (default timings)
def test_rearm_waits_for_finger_off(): wait_finger_off is called between the match and the next identify (Review Focus 1)
def test_no_match_posts_no_match()
def test_retry_posts_retry_and_pauses(): with retry_pause=0.2, the next identify starts ≥ 0.2 s later
def test_failures_back_off_instead_of_spinning(): every identify raises ReaderFailure; backoff_start=0.01, backoff_max=0.04; after 0.5 s fewer than 20 identify calls
def test_too_hot_posts_retry_and_pauses_long()
def test_reader_gone_calls_on_fatal_and_stops()
def test_closed_mode_closes_the_reader(): set_mode(CLOSED) → close called; no identify afterwards
def test_mode_change_cancels_a_waiting_identify(): blocking identify ends within 0.5 s of set_mode(CLOSED)
def test_rearms_every_rearm_period(): rearm_every=0.1, empty script → identify called ≥ 3 times in 0.5 s, nothing posted
def test_busy_seconds_reports_a_running_call(): > 0.15 after 0.2 s of a blocking identify; 0.0 once closed
def test_enroll_posts_stages_then_enrolled_and_returns_to_ready()
def test_enroll_idle_timeout_posts_enroll_failed()
def test_set_gallery_restarts_identify_with_the_new_gallery()
def test_finger_down_is_posted()
```

- [x] **Step 2: Run; they fail.** `uv run pytest tests/test_scanner.py -v`
- [x] **Step 3: Implement `scanner.py`.**
- [x] **Step 4: Run; they pass** (+ ruff, pyright). Run it three times in a row; it must not flake.
- [x] **Step 5: Commit** `feat(scanner): add the scanner thread with backoff and watchdog`.

---

### Task 7: The libfprint reader and its probe

**Files:**
- Create: `src/timeclock_system/fprint_reader.py` (excluded from pyright in Task 1; runs only in the image)

**Interfaces:**
- Consumes: `Reader`, `CancelToken`, `GalleryPrint`, the exceptions (Task 6).
- Produces: `class FprintReader` implementing `Reader`. Its constructor raises `ReaderGone` when no `uru4000`
  device is found. `python -m timeclock_system.fprint_reader probe-identify | soak MINUTES | closed-hold SECONDS`
  for the hardware checks in Task 12.
- Pinned libfprint use (PyGObject, `gi.require_version("FPrint", "2.0")`; imports inside the module, which is
  never imported on Windows):
  - `self._ctx = FPrint.Context.new(); self._ctx.enumerate()`; device = the one with `get_driver() == "uru4000"`.
    **Keep `self._ctx` for the object's life**: dropping the Context segfaults (spike).
  - `new_cancel()` → `Gio.Cancellable()`; `open()` → `open_sync(None)`; `close()` → `close_sync(None)` (only if open).
  - `identify`: deserialize each template once (`FPrint.Print.deserialize(bytes)`, cached by print id);
    `match, _probe = device.identify_sync(prints, cancel, None, None)`; map `match` back to its print id by
    identity in the list; connect `notify::finger-status` for the call's duration and call `on_finger` when
    `device.get_finger_status() & FPrint.FingerStatusFlags.PRESENT`.
  - `enroll`: `template = FPrint.Print.new(device)`; `device.enroll_sync(template, cancel, progress_cb, None)`,
    where `progress_cb(device, completed_stages, print_, user_data, error)` calls
    `on_stage(completed_stages, device.get_nr_enroll_stages())`; return `bytes(print.serialize())`.
  - `wait_finger_off(timeout)`: poll `get_finger_status()` every 50 ms until `PRESENT` is clear or `timeout` passes.
  - Errors (`GLib.Error` as `e`): `e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED)` → `ScanCancelled`;
    domain `GLib.quark_to_string(FPrint.device_retry_quark())` → `RetryScan(e.message)`; domain of
    `FPrint.device_error_quark()` with code `FPrint.DeviceError.REMOVED` → `ReaderGone`, `TOO_HOT` →
    `ReaderTooHot`; anything else → `ReaderFailure(e.message)`. (The domains' strings are
    `"fp - device - retry - quark"` and `"fp - device - error - quark"`: compare through the quark functions, not
    literals.) If `FPrint.device_retry_quark` doesn't exist in this PyGObject, use `FPrint.DeviceRetry.quark()`;
    Task 12's probe prints which one works.
  - The probe CLI (`argparse`, prints plain lines): `probe-identify` enrolls one finger (5 presses, printing
    stages), prints the template size, then identifies 3 times printing match/no-match and seconds taken;
    `soak MINUTES` keeps identify waiting with the 60 s re-arm, printing every result and any `ReaderTooHot`, and
    a final `too_hot_count=N`; `closed-hold SECONDS` opens, closes, then sleeps holding the Context (for the
    autosuspend check).

- [x] **Step 1: Write `fprint_reader.py`** per the block. No unit tests (it needs the reader); Task 12 runs it.
- [x] **Step 2: Verify** — `uv run ruff check src/timeclock_system/fprint_reader.py`; `uv run python -c "import ast,sys; ast.parse(open('src/timeclock_system/fprint_reader.py').read())"`.
- [x] **Step 3: Commit** `feat(fprint_reader): add the libfprint reader and its hardware probe`.

---

### Task 8: Database sync

**Files:**
- Create: `src/timeclock_system/sync.py`
- Test: `tests/test_sync.py` (PostgreSQL; skips without `TIMECLOCK_TEST_PG_DSN`)

**Interfaces:**
- Consumes: `Clock` (Task 5), `LocalStore` (Task 3), `wire.Notification`, `wire.NOTIFY_CHANNEL`.
- Produces: `@dataclass(frozen=True) class DbStatus`: `connected: bool`, `last_contact: datetime | None`;
  `class DatabaseSync`: `__init__(self, clock: Clock, store: LocalStore, conninfo: str, *, now: Callable[[], datetime] = …)`;
  `status: DbStatus` (property); `def wake(self) -> None` (new local record: push soon; `asyncio.Event`);
  `async def run(self) -> None` (forever).
- Loop, pinned (§6.6):
  1. `psycopg.AsyncConnection.connect(conninfo, autocommit=True, row_factory=dict_row)`; failure → status
     disconnected, back off 1, 2, 4 … 60 s, retry. Catch `psycopg.Error`, `OSError`, `TimeoutError` only;
     anything else is a bug and propagates.
  2. `LISTEN timeclock`.
  3. Catch-up: `since = await clock.catch_up_since()`; `SELECT … FROM punches WHERE at >= %(since)s`;
     `SELECT … FROM punch_corrections WHERE punch_id IN (SELECT id FROM punches WHERE at >= %(since)s)`;
     full `employees`, `groups`, `fingerprints`, `rules`, `config.business_hours`, `config.settings`. Store via
     `replace_db_punches`, `replace_corrections`, `replace_table`, `replace_db_fingerprints`, `set_settings`; then
     `clock.notify_changed(<all tables>)`. Explicit column lists (the clocks can't `SELECT *` on `overrides`;
     elsewhere lists keep `extra="ignore"` meaningful).
  4. Push: per `(punch, override)` from `unsynced_punches()`, one `async with conn.transaction()`:
     `INSERT INTO punches (id, employee_id, kind, at, instance_id) VALUES (%(id)s, %(employee_id)s, %(kind)s::punch_kind, %(at)s, %(instance_id)s) ON CONFLICT (id) DO NOTHING`,
     and the override likewise (`reason::override_reason`); then `mark_punch_synced`. Fingerprints: the five
     insertable columns, `ON CONFLICT (id) DO NOTHING`, then `mark_fingerprint_synced`. Never name `db_login`,
     `received_at` or `automatic` (the grants refuse them).
  5. Wait: `notifies(timeout=1.0, stop_after=50)` into a list (no queries while the generator runs), then apply
     each, then push if `wake` was set, then `SELECT 1` if 30 s passed since the last exchange. Every successful
     exchange sets `last_contact` (also saved with `store.set_meta("last_contact", iso)` and loaded at start).
- Applying a notification (`Notification.model_validate_json(n.payload)`): for `punches`, `punch_corrections`,
  `employees`, `groups`, `fingerprints`, `rules`, fetch the row by id; none → delete locally; else upsert
  (`upsert_db_punch`, `upsert_correction`, `upsert`, `upsert_db_fingerprint`). A punch with `at` before the last
  `since` that isn't already stored is ignored. `config.business_hours` and `config.settings` → re-fetch the
  table. Then `clock.notify_changed({table})` (map `config.business_hours` → `"business_hours"`,
  `config.settings` → `"settings"`). A notification for any other table is ignored, without an error: the
  schema's `Notification.table` is a plain `str`, so a newer migration's table reaches clocks that don't
  know it yet. Test: `test_notification_for_unknown_table_is_ignored` (a payload with `"table": "future_table"`
  changes nothing locally and raises nothing).
- `def add_status_listener(self, cb: Callable[[DbStatus], None]) -> None`: called whenever `connected` flips.
  (The page polls `status` too; the listener is for anything that wants the edge.)

- [ ] **Step 1: Failing tests.** Fixtures, as the schema plan's `tests/test_sql.py` does: an autocommit admin
  connection creates `tcc_<uuid4 hex>`, applies every `load_migrations()` file in its own transaction, creates
  `CREATE ROLE tcc_clock_<hex> LOGIN PASSWORD 'pw' IN ROLE timeclock_clock_access`, yields the clock's DSN, and
  drops both (`DROP DATABASE … WITH (FORCE)`, then the role). A seeding helper inserts employee "Ann"
  (`crypt('1234', gen_salt('bf'))`) as `postgres`. `run()` runs as a task; helpers poll the store for up to 5 s.

```python
async def test_catch_up_mirrors_employees_and_settings()
async def test_pushes_a_local_punch_with_its_override(): rows in punches and overrides; db_login == the clock role; local row synced
async def test_push_is_idempotent(): after the first push, reset it with a second sqlite3 connection to the store file (UPDATE punches SET synced = 0; commit) and wake → still one row in punches (Review Focus 2)
async def test_reconnects_after_the_connection_is_killed(): pg_terminate_backend for the clock role; a punch recorded meanwhile arrives after reconnect; status went disconnected then connected (Review Focus 2)
async def test_notification_inserts_another_clocks_punch(): insert as postgres with instance_id "2" → appears locally with origin db
async def test_echo_of_own_punch_keeps_one_row(): (Review Focus 4)
async def test_owner_delete_removes_the_local_row(): DELETE FROM punches as postgres → gone locally (Review Focus 4)
async def test_owner_update_of_settings_refetches(): UPDATE config.settings SET subtract_breaks = true → clock.settings().subtract_breaks
async def test_last_contact_survives_a_restart(): new DatabaseSync over the same store reports the saved last_contact
```

- [ ] **Step 2: Run; they fail** (container up, variable set). `uv run pytest tests/test_sync.py -v`
- [ ] **Step 3: Implement `sync.py`.**
- [ ] **Step 4: Run; they pass**; without the variable they skip (+ ruff, pyright).
- [ ] **Step 5: Commit** `feat(sync): mirror and push through the backend's relay`.

---

### Task 9: Screen text and the kiosk controller

**Files:**
- Create: `src/timeclock_system/ui/__init__.py` (docstring), `src/timeclock_system/ui/text.py`,
  `src/timeclock_system/ui/controller.py`
- Test: `tests/test_controller.py`

**Interfaces:**
- Consumes: `Clock`, `MenuInfo`, `PunchOutcome`, `PunchRefused`, `OverrideGrant` (Task 5); scanner events and
  `ReaderMode` (Task 6); `hours.is_open` (Task 4).
- Produces (`ui/text.py`), exact copy:
  `IDLE_READY = "Place your finger on the reader"`, `IDLE_ASLEEP = "Tap to scan"`, `SCANNING = "Scanning…"`,
  `NOT_RECOGNIZED = "Not recognized: try again or use your PIN"`, `RETRY = "Couldn't read that, try again"`,
  `USE_PIN = "Use PIN"`, `ENTER_USERNAME = "Enter your PIN username"`, `ENTER_PIN = "Enter your PIN"`,
  `WRONG_PIN = "Incorrect PIN"`, `SCAN_YOUR_FINGER = "Scan your finger"`,
  `NOT_YOUR_FINGER = "That fingerprint doesn't match. Try again."`, `REGISTER_PROMPT = "Register your fingerprint"`,
  `REGISTER = "Register"`, `REGISTERING = "Press your finger on the reader ({done} of {total})"`,
  `REGISTRATION_TIMED_OUT = "Registration timed out"`, `REGISTRATION_FAILED = "Registration failed, try again"`,
  `RESCAN = "Scan your finger to confirm"`, `MANAGER_PROMPT = "Manager: scan your finger or enter your PIN"`,
  `NOT_A_MANAGER = "A manager must authorize this"`, `MANAGER_OVERRIDE = "Manager override"`, `CANCEL = "Cancel"`;
  `PUNCH_LABELS: Mapping[PunchKind, str]` = Clock in / Clock out / Start break / End break / Start lunch / End lunch;
  `CONFIRMATIONS: Mapping[PunchKind, str]` = "Clocked in at {time}" / "Clocked out at {time}" / "Break started at
  {time}" / "Break ended at {time}" / "Lunch started at {time}" / "Lunch ended at {time}";
  `STATE_LABELS: Mapping[EmployeeState, str]` = "Clocked out" / "Clocked in since {time}" / "On break since
  {time}" / "At lunch since {time}";
  `def format_clock_time(at: datetime, tz: ZoneInfo) -> str` (`"8:02 AM"`; build it from `hour % 12 or 12`, since
  `%-I` fails on Windows); `def format_hours(td: timedelta) -> str` (`"7.50 h"`, two decimals);
  `def format_db_status(status: DbStatus, tz: ZoneInfo) -> str` (`"Database connected · last contact 8:02 AM"`,
  `"Database offline · last contact 7:15 AM"`, `"… · last contact never"`).
- Produces (`ui/controller.py`): constants `RESCAN_AFTER_SECS = 30` (R45.2), `IDLE_CLOSE_SECS = 120` (R45.4,
  for every non-idle screen), `CONFIRM_SECS = 5`, `MESSAGE_SECS = 3`, `WAKE_SECS = 30` (R40.2),
  `SCAN_IN_PROGRESS_SECS = 16` (the spike's slowest resting thumb); screen dataclasses (frozen):
  `Idle(message: str | None = None, until: float | None = None)`,
  `Menu(info: MenuInfo, auth: OverrideGrant | None, opened_at: float, touched_at: float, notice: str | None = None)`
  (`auth=None` means fingerprint), `Rescan(employee_id: int, kind: PunchKind, auth: OverrideGrant | None, touched_at: float, message: str | None = None)`,
  `Confirmation(text: str, hours: str, warnings: tuple[str, ...], until: float)`,
  `OverrideRequest(employee_id: int, reason: OverrideReason, pending_kind: PunchKind | None)`,
  `PinEntry(field: Literal["username", "password"], username: str, digits: str, override: OverrideRequest | None, touched_at: float, error: str | None = None)`,
  `PinScan(employee_id: int, touched_at: float, message: str | None = None)`,
  `RegisterPrompt(employee_id: int, touched_at: float)`,
  `Registering(employee_id: int, done: int, total: int, touched_at: float)`,
  `OverridePrompt(request: OverrideRequest, touched_at: float, message: str | None = None)`; `Screen` = their union.
  `class ReaderPort(Protocol)`: `def set_mode(self, mode: ReaderMode) -> None`.
  `class KioskController`: `__init__(self, clock: Clock, reader: ReaderPort, *, tz: ZoneInfo, monotonic: Callable[[], float] = time.monotonic, wall_now: Callable[[], datetime] = …)`;
  properties `screen: Screen`, `reader_awake: bool`; `add_listener(cb: Callable[[], None])`; async entry points
  `on_scanner_event(ev)`, `tap()`, `use_pin()`, `digit(d: str)`, `backspace()`, `enter()`, `punch(kind)`,
  `register()`, `request_override()`, `cancel()`, `tick()`. Every entry point runs under one `asyncio.Lock`,
  then recomputes the reader mode and notifies listeners.
- Transitions (R23, R40.2, R45; text constants from `ui/text.py`):

| Screen | Event | Result |
| --- | --- | --- |
| Idle | `Matched(e)` | `Menu(menu_info(e), auth=None, opened_at=touched_at=now)` |
| Idle | `NoMatch` / `Retry` | `Idle(NOT_RECOGNIZED / RETRY, until=now+3)` |
| Idle | `FingerDown` | `Idle(SCANNING)`, and remember `now` as the last touch |
| Idle | `tap` | wake the reader until `now+30` |
| Idle, Confirmation | `use_pin` | `PinEntry("username", "", "", override=None)` |
| Menu, Confirmation | `Matched(e)` | e's `Menu`, fresh `opened_at` (R45.4: a new scan switches) |
| Menu | `punch(k)` ≤ 30 s after `opened_at` | `record_punch(e, k, override=auth)` → `Confirmation(CONFIRMATIONS[k] at punch time, format_hours, warnings, now+5)`; `PunchRefused` → same Menu with `notice=message` |
| Menu | `punch(k)` > 30 s after `opened_at` | `Rescan(e, k, auth)` |
| Rescan | `Matched(e)` same employee | record `k` with no override → Confirmation |
| Rescan | `Matched(other)` | other's `Menu` |
| Rescan | `NoMatch` / `Retry` | same screen, `message=NOT_RECOGNIZED / RETRY` |
| Rescan, PinScan | `request_override` | `OverridePrompt(OverrideRequest(e, FINGERPRINT_SKIPPED if has_fingerprint else NO_FINGERPRINT, pending_kind=k or None))` |
| PinEntry | `digit` / `backspace` | edit `digits` (max 12) |
| PinEntry "username" | `enter` | `PinEntry("password", username=digits, digits="")` |
| PinEntry "password", login | `enter` | `verify_pin` → None: `PinEntry("username", error=WRONG_PIN)`; has print: `PinScan(e)`; none: `RegisterPrompt(e)` |
| PinEntry "password", override | `enter` | `verify_pin` → a manager who passes `override_manager`: grant with `pin_only=True`; else `OverridePrompt(…, message=NOT_A_MANAGER)` |
| PinScan | `Matched(e)` same | `Menu(e, auth=None)` |
| PinScan | `Matched(other)` | same screen, `message=NOT_YOUR_FINGER` |
| RegisterPrompt | `register` | `Registering(e, 0, 5)`, reader → ENROLL |
| RegisterPrompt | `request_override` | `OverridePrompt(OverrideRequest(e, NO_FINGERPRINT, None))` |
| Registering | `EnrollStage(d, t)` | update; `Enrolled(tpl)` → `register_fingerprint` → `Menu(e, auth=None)`; `EnrollFailed("timeout"/"error")` → `Idle(REGISTRATION_TIMED_OUT / REGISTRATION_FAILED, until=now+3)` |
| OverridePrompt | `Matched(m)` | `override_manager(m, e)` → grant (`pin_only=False`); else same screen, `message=NOT_A_MANAGER` |
| OverridePrompt | `use_pin` | `PinEntry("username", override=request)` |
| (grant obtained) | | `pending_kind` set → record it with the grant → Confirmation; else `Menu(e, auth=grant)` |
| any | `cancel` | `Idle()` (reader back to READY first if it was ENROLL) |
| any non-Idle, non-Confirmation | `tick`, untouched 120 s | `Idle()` |
| Confirmation / Idle with message | `tick` past `until` | `Idle()` |

  A Menu with an override grant allows exactly one punch (the punch path above consumes it; after it the screen
  is a Confirmation), so every further punch needs a new override or a fingerprint (R45.3).
- Reader mode after every entry point: ENROLL while `Registering`; else READY when `is_open(business_hours, now)`,
  or `now < wake_until`, or the screen is anything but `Idle`; else CLOSED, but only once the screen is a plain
  `Idle()` and no `FingerDown` came in the last 16 s (R40.2: a scan in progress finishes first). Call
  `reader.set_mode` only when the mode changes. `reader_awake` = mode isn't CLOSED (the page shows
  `IDLE_ASLEEP` when it's false).

- [ ] **Step 1: Failing tests.** A real `Clock` over a `tmp_path` store (seeded: employee 1 Ann with a print,
  employee 2 Bob without one, employee 3 Mia a manager with a print; business hours Mon–Sun 00:00–23:59 unless a
  test clears them), a `FakeReaderPort` recording `set_mode` calls, and fake clocks (`monotonic`, `wall_now`)
  the test advances:

```python
async def test_match_opens_the_menu()
async def test_no_match_shows_message_then_clears(): after tick past 3 s → Idle()
async def test_punch_within_30s_records_and_confirms(): Confirmation.text == "Clocked in at 8:02 AM"
async def test_punch_after_30s_needs_a_rescan_of_the_same_employee()
async def test_rescan_by_someone_else_opens_their_menu_without_punching()
async def test_new_scan_switches_the_menu()
async def test_menu_closes_after_two_minutes_untouched()
async def test_refused_punch_shows_notice_and_stores_nothing()
async def test_pin_login_without_print_prompts_registration()
async def test_pin_login_with_print_requires_that_employees_finger()
async def test_wrong_pin_returns_to_username_with_error()
async def test_registration_happy_path_ends_in_the_menu(): reader set to ENROLL then READY
async def test_registration_timeout_shows_message()
async def test_override_by_manager_finger_allows_one_punch(): override row pin_only False; a second punch needs a new override
async def test_override_by_manager_pin_sets_pin_only()
async def test_employee_cannot_override_for_themselves(): Mia logs in by PIN, overrides with her own finger → NOT_A_MANAGER
async def test_rescan_with_override_records_the_pending_punch()
async def test_after_hours_idle_is_asleep_until_tapped(): no hours → CLOSED; tap → READY; after 30 s → CLOSED
async def test_reader_closes_only_when_idle_after_hours(): hours end while a Menu is open → stays READY; back to Idle and 16 s without FingerDown → CLOSED (Review Focus 5)
async def test_pin_scan_after_hours_opens_the_reader()
```

- [ ] **Step 2: Run; they fail.** `uv run pytest tests/test_controller.py -v`
- [ ] **Step 3: Implement `ui/text.py` and `ui/controller.py`.**
- [ ] **Step 4: Run; they pass** (+ ruff, pyright).
- [ ] **Step 5: Commit** `feat(ui): add the kiosk controller and screen text`.

---

### Task 10: The NiceGUI page

**Files:**
- Create: `src/timeclock_system/ui/page.py`
- Test: `tests/test_page.py`

**Interfaces:**
- Consumes: `KioskController`, screens, `ui/text.py` (Task 9); `DbStatus` (Task 8).
- Produces: `def build_root(controller: KioskController, status: Callable[[], DbStatus], *, tz: ZoneInfo, branding_dir: Path) -> Callable[[], None]`
  — the root function for `ui.run(root=…)`.
- Pinned rendering (R39, R13.2, R25, §6.4):
  - CSS variables `--sft-yellow: #F9E60D; --sft-red: #ED1A2C; --sft-black: #161514; --sft-white: #FFFFFF`;
    page background `--sft-black`, text white, punch buttons yellow with black text, errors and the offline icon
    red, the connected icon `#2E7D32`. Touch targets at least 64 px tall; landscape from 1024×600 up.
  - `ui.add_css("body { user-select: none; -webkit-user-select: none; touch-action: manipulation; }")` and
    `ui.add_head_html('<script>document.addEventListener("contextmenu", e => e.preventDefault());</script>')`.
  - Logo: if `branding_dir / "SFT-Logo-June-2022-color.svg"` exists, `app.add_media_files("/branding", branding_dir)`
    once and show it; if not, render without it and log one warning (R39.1).
  - One `@ui.refreshable` section renders `controller.screen` (Idle: logo, date/time, `IDLE_READY` or
    `IDLE_ASLEEP` or the message, a `USE_PIN` button; Menu: name, `STATE_LABELS`, hours, one button per
    `info.valid_next` in that order, `notice` in red, `CANCEL`; Rescan/PinScan/OverridePrompt: prompt, message,
    `MANAGER_OVERRIDE` where the table allows it, `CANCEL`; PinEntry: prompt, masked digits for the password, a
    3×4 number pad `1–9, ⌫, 0, ✓` plus `ui.keyboard` mapping digits/Backspace/Enter to the same calls (R25);
    Registering: `REGISTERING` with done/total; Confirmation: text, hours, warnings). The listener calls
    `refresh()`.
  - A status line bottom-right: a filled circle green or red and `format_db_status(status(), tz)`, refreshed by
    `ui.timer(1.0, …)`; `ui.timer(0.5, controller.tick)`; a tap anywhere on the Idle screen calls `controller.tap()`.

- [ ] **Step 1: Failing tests** with `async with nicegui.testing.user_simulation(build_root(...)) as user:`,
  then `await user.open("/")`, `user.find("1").click()`, `await user.should_see(...)` (a `Clock` over a
  `tmp_path` store, a recording reader port, `status=lambda: DbStatus(False, None)`, `branding_dir=tmp_path`):
  `test_idle_shows_the_prompt_and_offline_status` (should_see `IDLE_READY` and "Database offline · last contact
  never"), `test_pin_pad_types_digits` (click 1, 2, ✓ → should_see `ENTER_PIN`), `test_menu_shows_valid_punch_buttons`
  (drive `controller.on_scanner_event(Matched(...))` → should_see "Clock in" and not "Clock out"),
  `test_missing_logo_still_renders`.
- [ ] **Step 2: Run; they fail.** — **Step 3: Implement `ui/page.py`.** — **Step 4: Run; they pass** (+ ruff, pyright).
- [ ] **Step 5: Commit** `feat(ui): add the kiosk page`.

---

### Task 11: Heartbeat, crash alerts and the app wiring

**Files:**
- Create: `src/timeclock_system/heartbeat.py`, `src/timeclock_system/crash_alerts.py`, `src/timeclock_system/app.py`
- Modify: `src/timeclock_system/__main__.py` (`run_app`)
- Test: `tests/test_heartbeat.py`, `tests/test_crash_alerts.py`, `tests/test_main.py`

**Interfaces:**
- Consumes: everything above; aeth_ext 10: `initialize`, `aeth_ext.errors.trigger_shutdown`, `alert`,
  `install_loop_exception_handler`, `aeth_ext.errors.shutdown` (`SHUTDOWN`, `ShutdownKind`, `ShutdownPhase`,
  `register_for_shutdown`, `get_current_fatal_trails`), `aeth_ext.monitoring.send_heartbeat_async`.
- Produces (`heartbeat.py`): `HEARTBEAT_INTERVAL_SECS = 60`, `HUNG_CALL_SECS = 90`;
  `async def heartbeat_loop(path: Path, scanner_busy: Callable[[], float], *, tz: ZoneInfo, write: Callable[..., Awaitable[None]] = send_heartbeat_async, interval: float = 60) -> None`
  — every `interval`, `await write(path, tz=tz)` (no ping arguments: the supervisor owns the pings,
  `DEVKIT_SUPERVISED_PING`) unless `scanner_busy() > HUNG_CALL_SECS`, then the file goes stale and
  devkit-container's 180 s check fails (§6.2).
- Produces (`crash_alerts.py`): `CRASH_FILES = 5`, `LINES_PER_FILE = 200`;
  `class CrashAlertWriter`: `__init__(self, directory: Path, *, program: str, host: str, now: Callable[[], datetime] = …)`,
  `def write(self, trails: tuple[ExceptionTrail, ...]) -> None` — appends one JSON line
  `{"at": <UTC ISO>, "program": …, "host": …, "trails": [{"origin": {"module", "file"}, "entries": [{"module", "category", "file"}, …]}, …]}`
  to `crash-alerts.<n>.jsonl`, `n` = the most recently written file while it has fewer than 200 lines, else the
  next index mod 5, truncated first; `def register_crash_alert_writer(writer: CrashAlertWriter) -> None` —
  `register_for_shutdown(<closure>, phase=ShutdownPhase.THREADED, priority=-10)` where the closure writes only
  when `SHUTDOWN.kind >= ShutdownKind.FATAL` (R20: every crash alert saved, none resent).
- Produces (`app.py`): `def serve() -> None`, in this order:
  1. `settings = ClockSettings.get_settings()`; `store = LocalStore(settings.store_path)`;
     `clock = Clock(store, instance_id=settings.instance_id, tz=settings.tz, alert=lambda r, d: alert(r, d, in_except_block=False, wait=False))`
     (`wait=False`: the loop never waits on a send).
  2. `reader = FprintReader()` (a missing reader raises `ReaderGone`, uncaught: fatal at startup, R14.7).
  3. The scanner posts across threads: `post = lambda ev: loop.call_soon_threadsafe(lambda: asyncio.ensure_future(controller.on_scanner_event(ev)))`,
     where `loop` is a variable of `serve` that the startup hook assigns (`nonlocal`) before `scanner.start()`;
     `on_fatal = lambda m: trigger_shutdown("Fingerprint reader lost", m)`.
  4. `sync = DatabaseSync(clock, store, settings.conninfo())`; `controller = KioskController(clock, scanner, tz=settings.tz)`.
  5. `register_crash_alert_writer(CrashAlertWriter(settings.crash_alerts_dir, program=<PROJECT_NAME-INSTANCE_ID>, host=socket.gethostname()))`;
     `register_for_shutdown(lambda trails: app.shutdown(), phase=ShutdownPhase.THREADED)` (NiceGUI's
     `app.shutdown()` sets uvicorn's `should_exit`); `register_for_shutdown(lambda trails: scanner.stop(), phase=ShutdownPhase.THREADED)`.
     Plain lambdas: aeth_ext holds bound methods weakly.
  6. `app.on_startup(<async hook>)`: `install_loop_exception_handler()`; capture the loop; `await clock.load()`;
     `clock.add_record_listener(lambda r: sync.wake())`; a change listener that, on `employees`/`fingerprints`/`punches`,
     schedules `scanner.set_gallery(await clock.gallery())`; set the gallery once; `scanner.start()`;
     `await controller.tick()` (sets the first reader mode); start the
     tasks `sync.run()`, `heartbeat_loop(settings.heartbeat_path, scanner.busy_seconds, tz=settings.tz)` and
     `clock.run_week_rollover()`, each with a done-callback that calls `trigger_shutdown(f"{name} stopped", repr(exc))`
     if it ended with an exception (the global hooks don't see exceptions held in tasks, §6.9).
  7. `uvicorn.Server.capture_signals = <a contextmanager that only yields>` (R21; uvicorn is pinned for this);
     aeth_ext's signal handlers drive the shutdown.
  8. `ui.run(root=build_root(controller, lambda: sync.status, tz=settings.tz, branding_dir=settings.branding_dir), host="0.0.0.0", port=8080, title="Time Clock", dark=True, reload=False, show=False, viewport="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no", log_config=None, uvicorn_logging_level="warning")`
     (`log_config=None` is passed through to `uvicorn.Config`; without it uvicorn's `dictConfig` closes aeth_ext's
     handlers). Never `native=True`.
- `__main__.run_app()`: `initialize(logging="socket", install_signal_handlers=True)` (asyncio left False: uvicorn
  brings uvloop, R5), then `from timeclock_system.app import serve`, then
  `try: serve() except KeyboardInterrupt: pass` (aeth_ext's exit nudge), then
  `raise SystemExit(1 if SHUTDOWN.kind >= ShutdownKind.FATAL else 0)`. Never wrap `serve()` in
  `handle_fatal_exc_sync` (the exit nudge would count as fatal, TODO #15).

- [ ] **Step 1: Failing tests:**
  `test_heartbeat_writes_each_interval` (fake `write`, `interval=0.05`, ≥ 3 calls in 0.2 s, each with `tz`),
  `test_heartbeat_skips_while_the_scanner_is_hung` (`scanner_busy` returns 91 → no calls);
  `test_crash_alert_line_shape` (a trail from `build_exception_trail(exc)` → one JSON line with those keys),
  `test_crash_alerts_rotate_across_five_files` (1,001 writes → files 0–4 exist, the newest has 1 line, none more
  than 200), `test_writer_callback_only_on_fatal` (call the registered closure with `SHUTDOWN.kind` patched to
  GRACEFUL → nothing; FATAL → one line; capture the closure by patching `crash_alerts.register_for_shutdown`);
  `test_main_constants` (`PROJECT_NAME == "timeclock-system"`, `REQUIRE_LOG_SERVER is False`,
  `SHUTDOWN_ON_LOG_REJECTION is False`).
- [ ] **Step 2: Run; they fail.** — **Step 3: Implement the three modules and `run_app`.**
- [ ] **Step 4: Run; they pass** (+ ruff, pyright).
- [ ] **Step 5: Commit** `feat(app): wire the clock, scanner, sync and page together`.

---

### Task 12: The patched libfprint in the image, and the reader on the hardware

**Files:**
- Create: `docker/libfprint/uru4000-no-heat-model.patch`
- Modify: `docker/Dockerfile` (the `builder-cached`, `builder-setup` and `final` windows only; everything else is
  the template)

**Interfaces:**
- Produces: an image whose venv imports `gi` and finds `FPrint-2.0` from `/opt/libfprint`; Debian's
  `libfprint-2-2` and `gir1.2-fprint-2.0` are not in it (R40.4).

- [ ] **Step 1: The patch**, exactly (verified to apply to v1.94.100 with `git apply --check`):

```diff
--- a/libfprint/drivers/uru4000.c
+++ b/libfprint/drivers/uru4000.c
@@ -1491,6 +1491,10 @@ fpi_device_uru4000_class_init (FpiDeviceUru4000Class *klass)
   dev_class->type = FP_DEVICE_TYPE_USB;
   dev_class->id_table = id_table;
   dev_class->scan_type = FP_SCAN_TYPE_PRESS;
+  /* timeclock-system: an always-waiting clock would trip the generic heat model about 65% of the
+   * time; HID rates the 4500's waiting state with no time limit (timeclock design section 7, R40).
+   * -1 is the opt-out the match-on-chip drivers use. */
+  dev_class->temp_hot_seconds = -1;
 
   img_class->img_open = dev_init;
   img_class->img_close = dev_deinit;
```

- [ ] **Step 2: The `builder-cached` and `builder-setup` windows.** devkit-container 2.3.0's template has a
  `builder-cached` window above the `GIT_TAG`/`GIT_REPO` build arguments, so its layers stay cached across
  releases (an `ARG` declared above a `RUN` changes that `RUN`'s cache key on every new tag); libfprint's
  build goes there so a deploy recompiles it only when the window changes. `builder-setup` keeps one `RUN`
  installing `gcc pkg-config libgirepository-2.0-dev libcairo2-dev` (PyGObject/pycairo, R11.2). The
  `builder-cached` window, pinned: one `RUN` installing `git meson ninja-build libglib2.0-dev libgusb-dev
  libssl-dev libgirepository1.0-dev gobject-introspection` (libfprint with only `uru4000`, which needs OpenSSL;
  `git` because this window sits above the template's own `git` install); then
  `COPY docker/libfprint/uru4000-no-heat-model.patch /tmp/libfprint.patch` (repo files from the clone aren't
  available in any window; the build context is the repo); then one `RUN` that
  `git clone --depth 1 --branch v1.94.100 https://gitlab.freedesktop.org/libfprint/libfprint.git /tmp/libfprint`,
  checks `test "$(git -C /tmp/libfprint rev-parse HEAD)" = 80a4b5ec612892c5c056c48dddaa561452cf37ec`
  (the tag's commit: the pin is the commit hash, since GitLab generates archive tarballs on demand and their
  bytes can change; spec call), `git -C /tmp/libfprint apply /tmp/libfprint.patch`, then
  `meson setup /tmp/libfprint/build /tmp/libfprint --prefix=/opt/libfprint --libdir=lib -Ddrivers=uru4000 -Dintrospection=true -Ddoc=false -Dgtk-examples=false -Dinstalled-tests=false -Dudev_rules=disabled -Dudev_hwdb=disabled`,
  `meson install -C /tmp/libfprint/build`, `rm -rf /tmp/libfprint /tmp/libfprint.patch`. A one-line comment above
  each `RUN` says why. If apt names a missing package or meson a missing dependency, find the trixie package in a
  throwaway `docker run --rm debian:trixie bash -c "apt-get update && apt-cache search <name>"` and adjust; say so
  in the commit body.
- [ ] **Step 3: The `final` window.** One `RUN` installing `libgirepository-2.0-0 libcairo2 libgusb2
  gir1.2-gusb-1.0 gir1.2-glib-2.0`; `COPY --from=builder /opt/libfprint /opt/libfprint`;
  `RUN echo /opt/libfprint/lib > /etc/ld.so.conf.d/libfprint.conf && ldconfig`;
  `ENV GI_TYPELIB_PATH=/opt/libfprint/lib/girepository-1.0`.
- [ ] **Step 4: Build on the workstation** (Docker Desktop; push the branch first, since the template clones
  `GIT_TAG`): `git push -u origin feat/clock-app`, then
  `docker build -f docker/Dockerfile --build-arg GIT_REPO=https://github.com/AetherBreaker/timeclock-system.git --build-arg GIT_TAG=feat/clock-app -t timeclock-system:dev .`
  Expected: success. Then
  `docker run --rm --entrypoint /app/.venv/bin/python timeclock-system:dev -c "import gi; gi.require_version('FPrint', '2.0'); from gi.repository import FPrint; print(FPrint.Context.new())"`
  prints a Context; and `docker run --rm --entrypoint sh timeclock-system:dev -c "dpkg -l | grep -c libfprint-2-2"` prints `0`.
- [ ] **Step 5: Commit** `feat(docker): build the patched libfprint into the image`.
- [ ] **Step 6: On timeclock-dev, the reader checks.** Say first that you're loading the `timeclock-system:dev`
  image onto timeclock-dev (no packages installed). `docker save timeclock-system:dev | ssh timeclock-dev sudo docker load`.
  The spike's `kiosk-test` container doesn't touch the reader; leave it until Task 15. Then, each with
  `ssh -t timeclock-dev sudo docker run --rm -it -v /dev/bus/usb:/dev/bus/usb --device-cgroup-rule='c 189:* rmw' --entrypoint /app/.venv/bin/python timeclock-system:dev -m timeclock_system.fprint_reader …`
  (root in the container, so no chown is needed):
  - `probe-identify`: enroll with 5 presses, then 3 identifies of the same finger match and one of another
    finger doesn't (the owner presses; ask them). Record the template size (about 3,263 bytes) and timings.
  - `soak 11`: leave a finger off for 11 minutes. Expected: `too_hot_count=0` (the patch works; unpatched it
    goes hot at about 236 s).
  - `closed-hold 30`: meanwhile, in a second session,
    `ssh timeclock-dev 'for d in /sys/bus/usb/devices/*; do [ "$(cat $d/idVendor 2>/dev/null)" = 05ba ] && cat $d/power/runtime_status; done'`
    after 5 s. Expected: `suspended` (the reader sleeps while the process holds the Context, R40.1).
  - Resting finger: during `probe-identify`'s identifies, leave the finger on after a match. Expected: no second
    match until it's lifted, or 2 s at most (Review Focus 1). If `get_finger_status()` doesn't update outside a
    capture, change `wait_finger_off` to sleep the full timeout and say so in the commit.
  Write the four results into the PR description (Task 14).

---

### Task 13: Kiosk image and the final compose

**Files:**
- Create: `docker/kiosk/Dockerfile`, `docker/kiosk/kiosk.sh`, `docker/kiosk/policies.json`,
  `docker/kiosk/autoconfig.js`, `docker/kiosk/kiosk.cfg`
- Modify: `docker/compose.yaml`

**Interfaces:**
- Produces: services `timeclock-system` and `kiosk` (no `discovery` and no published port: those come with LAN
  sharing in step 4).

- [ ] **Step 1: Kiosk files.** `kiosk.sh` is Appendix A's script verbatim. `Dockerfile` is Appendix A's with
  paths from the repo root (`COPY docker/kiosk/kiosk.sh /kiosk.sh`) plus
  `COPY docker/kiosk/policies.json /usr/lib/firefox-esr/distribution/policies.json`,
  `COPY docker/kiosk/autoconfig.js /usr/lib/firefox-esr/defaults/pref/autoconfig.js`,
  `COPY docker/kiosk/kiosk.cfg /usr/lib/firefox-esr/kiosk.cfg`, and `RUN chmod 755 /kiosk.sh`.
  `policies.json`: `{"policies": {"DisableTelemetry": true, "DisableFirefoxStudies": true, "DontCheckDefaultBrowser": true, "DisableAppUpdate": true, "NoDefaultBookmarks": true, "OverrideFirstRunPage": "", "OverridePostUpdatePage": ""}}`.
  `autoconfig.js`: `pref("general.config.filename", "kiosk.cfg");` and `pref("general.config.obscure_value", 0);`.
  `kiosk.cfg` (its first line must be a comment): `lockPref` for `apz.allow_zooming` false,
  `browser.gesture.pinch.in` "", `browser.gesture.pinch.out` "", `browser.gesture.swipe.left` "",
  `browser.gesture.swipe.right` "", `widget.disable-swipe-tracker` true,
  `browser.sessionstore.resume_from_crash` false (R13.2; autoconfig because policies can't set every pref).
- [ ] **Step 2: Compose.** Keep every line setup-project wrote for `timeclock-system` and add, by hand:
  `hostname: timeclock-system-${INSTANCE_ID:?}`, `restart: unless-stopped`, a second bind
  `/dev/bus/usb` → `/dev/bus/usb`, `device_cgroup_rules: ["c 189:* rmw"]` (R14.7), and these environment entries
  (list form, next to the template's):
  `INSTANCE_ID=${INSTANCE_ID:?}`, `HEARTBEAT_SLUG=timeclock-system-${INSTANCE_ID:?}` (R14.6),
  `TZ=America/New_York`, `LOG_HISTORY_RETENTION_DAYS=14` (R44), `LOG_CONN_HOST=10.8.0.25`,
  `ALERTS_EMAIL=info@sweetfiretobacco.com`, `ALERTS_EMAIL_PWD=${ALERTS_EMAIL_PWD:?}`,
  `ALERTS_RECIPIENTS=["jacob.ogden@sweetfiretobacco.com"]`, `PINGKEY=${PINGKEY:?}`,
  `WG_TOLERATE_DISCONNECTED=1` (R22), `WG_HANDSHAKE_TIMEOUT_SECS=10` (§11.2), `TIMECLOCK_DB_HOST=10.8.0.24`,
  `TIMECLOCK_DB_NAME=${TIMECLOCK_DB_NAME:?}`, `TIMECLOCK_DB_USER=${TIMECLOCK_DB_USER:?}`,
  `TIMECLOCK_DB_PASSWORD=${TIMECLOCK_DB_PASSWORD:?}`. Add the `kiosk` service:

```yaml
  kiosk:
    build:
      context: .
      dockerfile: docker/kiosk/Dockerfile
    restart: unless-stopped
    privileged: true
    volumes:
      - /dev:/dev
    environment:
      - KIOSK_URL=http://timeclock-system:8080
    depends_on:
      timeclock-system:
        condition: service_healthy
    networks:
      - coolify
```

- [ ] **Step 3: Verify.** `docker compose -f docker/compose.yaml config -q` with dummy values exported for the
  `:?` variables → exit 0. `docker build -f docker/kiosk/Dockerfile -t timeclock-kiosk:dev .` → success.
  `uv run poe setup-project --dry-run` proposes no change to the hand-added keys.
- [ ] **Step 4: Commit** `feat(kiosk): add the kiosk image and the app and kiosk compose services`. Appendix A's
  files are now in the repo, which unblocks the device cleanup in Task 15.

---

### Task 14: Install-image edits, full suite and the release

**Files:**
- Modify: `image/dev-device/preseed.cfg.in`, `image/dev-device/files/late.sh`, `image/dev-device/test.sh`,
  `docs/dev-device-setup.md`
- Create: `image/dev-device/files/52timeclock-unattended`

These edits apply from the next image rebuild. **Don't rebuild or run `test.sh`.**

- [x] **Step 1: Security updates (R42).** `preseed.cfg.in`: add `unattended-upgrades` to the
  `pkgsel/include` line, and `d-i pkgsel/update-policy select unattended-upgrades` after it.
  `files/52timeclock-unattended`:

```text
// Security updates only, rebooting at 03:00 when one needs it (timeclock design §5, R42).
// #clear drops Debian's default origins, which also take point releases.
#clear Unattended-Upgrade::Origins-Pattern;
Unattended-Upgrade::Origins-Pattern {
  "origin=Debian,codename=${distro_codename}-security,label=Debian-Security";
  "origin=Debian,codename=${distro_codename},label=Debian-Security";
};
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "03:00";
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
```

  `late.sh`, after the `90-sync-esp` install line: `install -m 644 "$S/52timeclock-unattended" /etc/apt/apt.conf.d/52timeclock-unattended`.
  `test.sh`'s checks: add `unattended-upgrades` to the `dpkg-query` list and `apt-config dump | grep -E "Automatic-Reboot|Origins-Pattern"`.
- [x] **Step 2: The hostname (R3.2).** In `preseed.cfg.in`, delete the `netcfg/get_hostname` and
  `netcfg/hostname` lines, and add
  `d-i preseed/early_command string grep -q 'netcfg/get_hostname=' /proc/cmdline || { . /usr/share/debconf/confmodule; db_fset netcfg/get_hostname seen false; db_input critical netcfg/get_hostname || true; db_go || true; }`
  with a comment: the install runs at `priority=critical`, which hides the high-priority hostname question, so
  it's asked here at critical priority unless the kernel command line already answers it. `test.sh` installs
  unattended through `-append "auto=true priority=critical partman/confirm=true …"`: add
  `netcfg/get_hostname=timeclock-test` to that string, and make its `hostname` check expect `timeclock-test`.
  The mechanism is unverified until the next rebuild; say so in the commit body.
- [ ] **Step 3: Docs.** (done except the 'Deploying the clock' section) `docs/dev-device-setup.md`: under "What the image sets up", a bullet for security updates
  (03:00 reboot) and change the hostname bullet to "asked during the install; `timeclock-dev` is the test
  device's"; in step 3 "Install", say the installer asks for the hostname. Add a short section "Deploying the
  clock" listing §9.3's steps 2–6 with this plan's compose variables.
- [x] **Step 4: Commit** `feat(image): install security updates and ask for the hostname`.
- [ ] **Step 5: Full checks** — `uv run pytest` (Postgres up, variable set: nothing skipped), `uv run ruff check .`,
  `uv run pyright`. Fix what fails in its task's style.
- [ ] **Step 6: PR** — `git push`, `gh pr create --title "Clock app and kiosk" --body "<tasks 1–14, Task 12's four hardware results, and the Claude Code attribution line>"`.
- [ ] **Step 7 (Owner):** review, run the suite by hand, merge.
- [ ] **Step 8: Release** — on `main`: `uv run poe release-and-pin` (no bump word: publishes 0.1.0 and pins
  compose's `GIT_TAG` to `v0.1.0`). It needs the repo's `UV_INDEX_SFTPYPI_USERNAME`/`UV_INDEX_SFTPYPI_PASSWORD`
  GitHub secrets; if the publish job fails on them, stop and ask the owner (`docker-pin` refuses an incomplete
  release).

---

### Task 15: Deploy to timeclock-dev and verify on the hardware

**Depends on:** Task 14's release; the backend deployed with `timeclock_test` (backend plan); the hub release
with `timeclock-system-dev` at `10.8.0.26`.

- [ ] **Step 1: Clean up the spike's leftovers on timeclock-dev** (Appendix A's files are committed):
  `ssh timeclock-dev 'sudo docker rm -f kiosk-test tc-app; sudo docker rmi kiosk-test apptest timeclock-system:dev; sudo docker network rm tc-net; sudo rm -f /root/fp-test-template.bin; sudo rm -rf /tmp/fpdata'`.
  Expected: each removed (a "No such" message for one already gone is fine).
- [ ] **Step 2 (Owner):** the logo files to `/data/timeclock_system_files/branding/` on timeclock-dev (R39.1).
- [ ] **Step 3 (Owner, DBeaver on `timeclock_test`):** the clock's login and test data:

```sql
CREATE ROLE timeclock_clock_dev LOGIN PASSWORD '<random>' IN ROLE timeclock_test_clocks;
INSERT INTO employees (name, pin_username, pin_hash, is_manager) VALUES
  ('Test Employee', '101', crypt('1234', gen_salt('bf')), false),
  ('Test Manager', '901', crypt('9999', gen_salt('bf')), true);
INSERT INTO config.business_hours (weekday, opens, closes) SELECT d, '00:00', '23:59' FROM generate_series(1, 7) d;
```

- [ ] **Step 4 (Owner, Coolify):** a Docker Compose application on server `timeclock-dev` from
  `AetherBreaker/timeclock-system`, branch `main`, compose file `docker/compose.yaml`. Environment:
  `INSTANCE_ID=dev`, `TIMECLOCK_DB_NAME=timeclock_test`, `TIMECLOCK_DB_USER=timeclock_clock_dev`,
  `TIMECLOCK_DB_PASSWORD` (Step 3's), `ALERTS_EMAIL_PWD`, `PINGKEY`, `WG_PRIVATE_KEY` (timeclock-system-dev's
  from the hub plan), `WG_HUB_URL`, `WG_HUB_REPO`, `WG_HUB_TOKEN` (the backend's values). Deploy, outside shift
  changes (a deploy blanks the screen).
- [ ] **Step 5: Verify the deploy** — `ssh timeclock-dev 'sudo docker ps --format "{{.Names}} {{.Status}}"'`:
  the app `(healthy)` and the kiosk up; the touchscreen shows the idle page with the logo and a green status
  icon. The app's log in Coolify is empty by design (socket logging only); check the central log server's
  `timeclock-system-dev` folder instead.
- [ ] **Step 6: Hardware checks** (the owner at the screen; record each result in the PR or a follow-up comment):
  1. PIN `101`/`1234` → register a print (5 presses) → the menu → Clock in → confirmation with hours; the punch
     appears in `timeclock_test.punch_log` within seconds (LISTEN/NOTIFY and the relay over WireGuard, §13).
  2. Fingerprint → menu → wait 31 s → Clock out asks for a rescan; another person's finger opens their menu.
  3. Manager override by PIN `901`/`9999` for an employee without a print → the override row has `pin_only = true`.
  4. Kiosk lockdown (R13.2): a pinch doesn't zoom; a long press shows no menu; text can't be selected; a
     horizontal swipe doesn't navigate; `sudo docker restart <kiosk container>` → no session-restore prompt.
  5. Offline (Review Focus 2): `ssh timeclock-dev 'sudo iptables -I DOCKER-USER -d $(getent hosts tunnels.sweetfiretobacco.com | cut -d" " -f1) -j DROP'`;
     the status icon turns red within about a minute; punch twice; `sudo docker restart` the app container: it
     comes back healthy and the kiosk shows the page (devkit-container 2.3.0's health check under tolerate);
     then `sudo iptables -D DOCKER-USER …` (same arguments): within a minute the icon is green and both punches
     are in `punch_log` exactly once.
  6. Reader sleep: delete the business-hours rows for today in DBeaver → within a few seconds the idle screen says
     "Tap to scan" and the reader's `power/runtime_status` reads `suspended`; a tap wakes it for 30 s.
- [ ] **Step 7:** Report the results to the owner; nothing to commit unless a check failed (fix on a new branch).
