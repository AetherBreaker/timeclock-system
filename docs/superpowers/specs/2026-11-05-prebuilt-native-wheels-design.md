# Prebuilt native wheels — design

*(owner)* marks what the owner decided or approved; *(my call)* is open to the owner's veto.

## Why

Every clock deploy compiles the patched libfprint, PyGObject and pycairo from source. On a 2026-11-05 deploy that was
about 140 s: 95 s installing build packages, 16 s compiling libfprint, about 17 s compiling PyGObject and pycairo, and
12 s more build packages. Each compile uses whatever the base image and Debian ship that day, so two clocks on one
release can run different builds. And a build reads the Dockerfile and `docker/libfprint/` patches from `main`, not from
the release tag, so a merged patch change reaches any clock that rebuilds, released or not.

## Goals

- libfprint (patched), PyGObject and pycairo are built once, as wheels, published to SFTPyPI, and installed by uv from
  `uv.lock` like any other dependency *(owner)*.
- A libfprint change reaches clocks only through timeclock-system's `uv.lock` and `poe release-and-pin` *(owner)*.
- Only what changed is built: a version SFTPyPI already has is never rebuilt *(owner)*.
- Releasing the wheels is `poe release` *(owner)*.
- A deploy compiles nothing.
- Done before the registration-quality work *(owner)*.

## Non-goals

- Changes to devkit *(owner)*.
- Rebuilding an unchanged upstream version for a new Debian release (the file name wouldn't change); a later change can
  use wheel build numbers.
- Architectures other than x86-64: all three clocks are x86-64 (checked 2026-11-05).

## The repo: `timeclock-extras` *(owner)*

Public, at `AetherBreaker/timeclock-extras` (the name is free). Not devkit-managed *(owner)*. It holds the libfprint
patches and their tests (moved from timeclock-system's `docker/libfprint/`), the build script, one versions file, a
small pyproject holding the `poe release` task, and the release workflow.

| Package | Version | Contents |
| --- | --- | --- |
| `timeclock-libfprint` | upstream's plus our revision: `1.94.100.post1`, `.post2` at the next patch change *(owner)* | libfprint built with our patches, `uru4000` driver only, with its `FPrint-2.0` typelib; tagged `py3-none-linux_x86_64` |
| `pygobject` | upstream's *(owner)* | upstream's release, built unmodified; `cp314` |
| `pycairo` | upstream's *(owner)* | upstream's release, built unmodified; `cp314` |

PyGObject and pycairo keep their real names because PyGObject depends on pycairo by name. SFTPyPI doesn't mirror PyPI
(its index has no bases, checked 2026-11-05), so publishing them there hides nothing.

Everything builds in `ghcr.io/astral-sh/uv:python3.14-trixie-slim`, the base of timeclock-system's image, named once in
the repo *(my call)*.

Licenses: libfprint and PyGObject are LGPL-2.1-or-later, pycairo LGPL-2.1-only or MPL-1.1. Each wheel carries its
license texts and points to its source: upstream's release, plus, for `timeclock-libfprint`, the repo tag holding the
exact patches *(my call)*.

## Releasing *(owner, unless marked)*

- A change bumps its version in the versions file in the same PR: a libfprint patch change bumps `timeclock-libfprint`
  (no bump command; the bump is part of the change). A PyGObject or pycairo upgrade changes its line.
- `poe release` runs the release workflow on `main` and waits for it, failing if it fails.
- For each package in the versions file, the workflow:
  - skips it if SFTPyPI already has that wheel (same package, version and Python tag);
  - otherwise builds it, checks it, tags the commit `<package>-v<version>`, and publishes it.
- If `timeclock-libfprint`'s version is already published but its patches, tests or build recipe differ from that
  version's tag, the workflow fails and says to bump the version. A patch change can't silently go unpublished.
- The workflow never overwrites a published file *(my call)*: SFTPyPI's index allows overwriting, and an overwritten
  wheel breaks every `uv.lock` that holds its hash.
- Checks before publishing *(my call)*: every test of libfprint's virtual-driver suite, ours included, passes on the
  patched build; and a smoke check that installs the three new wheels in a clean trixie environment and loads `FPrint`
  from `timeclock-libfprint`, not from anywhere else. The unpatched run in `run.sh` stays a tool for whoever writes a
  test, to see it catch its bug; it isn't a release gate (some of our tests pass unpatched, and the heat-model patch has
  none).
- Jobs split as devkit's are *(my call)*: building runs with read-only access and no secrets; a separate job holds the
  SFTPyPI credentials and the tag write. Tag before upload, so a failed upload can be re-run.
- Workflow, job and step names follow AGENTS.md's GitHub Workflow Naming.

## timeclock-system changes

- pyproject: depend directly on `timeclock-libfprint` and `pycairo` (Linux only, like PyGObject), and route
  `timeclock-libfprint`, `pygobject` and `pycairo` to SFTPyPI in `[tool.uv.sources]`. pycairo has to be a direct
  dependency: uv ignores sources for a dependency pulled in only indirectly (tested 2026-11-05; routed alone, pycairo
  still locked from PyPI). `uv.lock` then holds our wheels with their hashes, which uv checks on install.
- Dockerfile:
  - Empty the `builder-cached` and `builder-setup` windows.
  - In `final`, keep the runtime libraries (`libgirepository-2.0-0 libcairo2 libgusb2 gir1.2-gusb-1.0
    gir1.2-glib-2.0`). Drop `COPY --from=builder /opt/libfprint` and its `ldconfig`. Point the linker and GObject at
    `timeclock-libfprint`'s files.
  - Constraint: the `final` window runs before the template copies the environment in, so an `ldconfig` there can't
    see the wheel's library. The image has to find it at run time, e.g. environment variables naming the wheel's
    directories, or a loader in the wheel. The builder chooses and proves it in a built image. My guess: the wheel puts
    its files in its data directory, so they land at a fixed `/app/.venv/lib`.
- Remove `docker/libfprint/` (moved). Update the pyproject and Dockerfile comments that say these compile in the image.
- timeclock-extras becomes a submodule at `extras/`, like backend, host, schema and standby, and joins their entries in
  pyright's, ruff's and tombi's exclude lists *(my call, the established pattern)*.
- TODO.md: remove "Build patched libraries at release" once this lands.
- Later libfprint patch work, such as the registration-quality design's, happens in timeclock-extras.

## A libfprint patch change, end to end *(owner)*

1. A PR in timeclock-extras with the patch and the version bump.
2. Merge it, then `poe release` there.
3. In timeclock-system: `uv lock --upgrade-package timeclock-libfprint`, PR, merge.
4. `poe release-and-pin` in timeclock-system.

## Rollout *(my calls)*

1. Create the repo, give it the SFTPyPI publish credentials as secrets, and `poe release`: `timeclock-libfprint`
   `1.94.100.post1` from today's patches, PyGObject `3.58.1` and pycairo `1.29.2` (today's locked versions).
2. A timeclock-system branch with the changes. Check locally by building the image from the branch (`GIT_TAG` set to
   the branch name; `git clone --branch` accepts a branch). Confirm that nothing compiles, that the three packages come
   from SFTPyPI, and that the app's reader module loads `FPrint` from `timeclock-libfprint`.
3. Test on timeclock-dev before release: build the branch's image on dev under the image name its container runs, and
   recreate the container from Coolify's own compose file there. No repo or Coolify change; the next Coolify deploy
   replaces it. The owner tests a scan.
4. Merge, then `poe release-and-pin` at once. Dev watches `docker/**`, so the merge rebuilds dev from `main` with the old
   pin (v0.8.3), whose lock still compiles PyGObject. That build fails without compilers and leaves dev's running
   container in place. Production watches only `docker/compose.yaml`, so only the pin deploys it.
5. Compare the deploy's duration with today's 140 s of compiling.

After this, a production rollback to a tag from before the change won't build (its lock compiles from source, and the
image no longer can). Roll forward with a new release instead.

## Left to the builder

- How the `timeclock-libfprint` wheel is assembled, and how the image finds its library and typelib.
- The versions file's format, the build script, and the `poe release` task's details.
- The workflow's job layout within the build/publish split.
