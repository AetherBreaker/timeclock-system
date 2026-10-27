# syntax=docker/dockerfile:1

# ---- Builder stage ----
FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim AS builder

WORKDIR /app

# Sees neither git ARG, so its layers stay cached across releases: heavy, tag-independent steps
# (e.g. compiling a C library) go here; COPY any repo file they need from the build context.
# !window builder-cached:
# libfprint's build needs (uru4000 only, which needs OpenSSL); this window sits above the template's own compiler and
# git installs, and curl fetches the release tarball.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gcc git pkg-config meson ninja-build \
  libglib2.0-dev libgusb-dev libssl-dev libgirepository1.0-dev gobject-introspection \
  && rm -rf /var/lib/apt/lists/*
COPY docker/libfprint/uru4000-no-heat-model.patch /tmp/libfprint.patch
# The pinned 1.94.100 release (spec §7), patched so an always-waiting reader never trips the heat model; a changed
# tarball or a patch that doesn't apply fails the build.
RUN mkdir /tmp/libfprint \
  && curl -fsSL -o /tmp/libfprint.tar.gz \
  https://gitlab.freedesktop.org/libfprint/libfprint/-/archive/v1.94.100/libfprint-v1.94.100.tar.gz \
  && echo "edc90e02f330a7595ceaf37f2c6ec32ed43541347fe936d3273b0bb2524fd19c  /tmp/libfprint.tar.gz" | sha256sum -c - \
  && tar -xzf /tmp/libfprint.tar.gz -C /tmp/libfprint --strip-components=1 \
  && git -C /tmp/libfprint apply /tmp/libfprint.patch \
  && meson setup /tmp/libfprint/build /tmp/libfprint --prefix=/opt/libfprint --libdir=lib -Ddrivers=uru4000 \
  -Dintrospection=true -Ddoc=false -Dgtk-examples=false -Dinstalled-tests=false -Dudev_rules=disabled -Dudev_hwdb=disabled \
  && meson install -C /tmp/libfprint/build \
  && rm -rf /tmp/libfprint /tmp/libfprint.tar.gz /tmp/libfprint.patch
# !end builder-cached

ARG GIT_TAG
ARG GIT_REPO

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Install git (required for uv to fetch git-based dependencies)
RUN apt-get update && apt-get install -y --no-install-recommends git \
  && rm -rf /var/lib/apt/lists/*

# System packages that deps need to compile; the repo isn't cloned yet, so COPY a needed file from the build context.
# !window builder-setup:
# PyGObject and pycairo build from source: PyPI has no Linux wheels for either (spec R11.2).
RUN apt-get update && apt-get install -y --no-install-recommends gcc pkg-config libgirepository-2.0-dev libcairo2-dev \
  && rm -rf /var/lib/apt/lists/*
# !end builder-setup

# Clone only the dependency manifest files first so the dep install layer
# can be cached independently of source code changes.
RUN git clone --depth 1 --branch "${GIT_TAG}" "${GIT_REPO}" /tmp/repo \
  && mv /tmp/repo/pyproject.toml /tmp/repo/uv.lock /app/

# Install the dependencies without the project itself, from the frozen lockfile. This
# brings in devkit-container (a runtime dependency of every devkit-managed Docker project),
# whose binary answers the build-time pyproject questions below and is the entrypoint in
# the final stage. The version is the one uv.lock names; setup-project rendered this file
# from that same version.
RUN --mount=type=cache,target=/root/.cache/uv \
  uv sync --frozen --no-dev --no-install-project

# A second sync adds the `app` extra when pyproject declares one; the layer is a no-op
# otherwise. Cached as long as pyproject.toml/uv.lock don't change.
RUN --mount=type=cache,target=/root/.cache/uv \
  extras=$(/app/.venv/bin/devkit-container app-extra) \
  && uv sync --frozen --no-dev --no-install-project $extras

# Now bring in the source tree, then the readme the wheel build reads, at the same
# relative path (`project.readme` may point into a subdirectory). The tree moves first so
# a readme inside it comes along and never pre-creates `/app/src`, which would
# make `mv` nest the tree. Only a missing readme is tolerated; a failing helper is not.
RUN mv /tmp/repo/src /app/src \
  && readme_file=$(/app/.venv/bin/devkit-container readme) \
  && if [ -n "${readme_file}" ] && [ -f "/tmp/repo/${readme_file}" ]; then \
  mkdir -p "/app/$(dirname "${readme_file}")" \
  && mv "/tmp/repo/${readme_file}" "/app/${readme_file}"; \
  fi \
  && rm -rf /tmp/repo

# Install the project itself as a non-editable wheel so the source tree is not
# required at runtime.
RUN --mount=type=cache,target=/root/.cache/uv \
  extras=$(/app/.venv/bin/devkit-container app-extra) \
  && uv sync --frozen --no-dev --no-editable $extras

# Project additions to the builder stage; setup-project renders the template around this window.
# !window builder:
# !end builder

# ---- Final stage ----
FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim

# Setup a non-root user. /app stays root-owned: the code is read-only to the app, which
# writes only to its mounted persisted dirs (or temp dirs).
RUN groupadd --system --gid 999 nonroot \
  && useradd --system --gid 999 --uid 999 --create-home nonroot

# Wireguard mode: the tools the entrypoint shells out to, so they version with the binary.
RUN apt-get update && apt-get install -y --no-install-recommends wireguard-tools iproute2 \
  && rm -rf /var/lib/apt/lists/*

# Project additions to the final stage; setup-project renders the template around this window.
# !window final:
# Runtime libraries for PyGObject, pycairo and the libfprint build; Debian's libfprint-2-2 stays out (spec §7).
RUN apt-get update && apt-get install -y --no-install-recommends libgirepository-2.0-0 libcairo2 libgusb2 \
  gir1.2-gusb-1.0 gir1.2-glib-2.0 \
  && rm -rf /var/lib/apt/lists/*
COPY --from=builder /opt/libfprint /opt/libfprint
# The linker and PyGObject find the patched library and its FPrint-2.0 typelib.
RUN echo /opt/libfprint/lib > /etc/ld.so.conf.d/libfprint.conf && ldconfig
ENV GI_TYPELIB_PATH=/opt/libfprint/lib/girepository-1.0
# !end final

WORKDIR /app

# Prevents Python from writing pyc files.
ENV PYTHONDONTWRITEBYTECODE=1
# Keeps Python from buffering stdout and stderr to avoid situations where
# the application crashes without emitting any logs due to buffering.
ENV PYTHONUNBUFFERED=1
# Enable Python optimizations (removes assert statements and sets __debug__ to False)
ENV PYTHONOPTIMIZE=1

# Copy the virtual environment from the builder stage; it carries the entrypoint binary.
COPY --from=builder /app/.venv /app/.venv

# The entrypoint reads the project's pyproject.toml.
COPY --from=builder /app/pyproject.toml /app/pyproject.toml

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"

# The entrypoint checks every required_persisted_dir is bind-mounted, chowns them to
# nonroot, drops privileges, and execs the project's run-app-* script.
ENTRYPOINT ["/app/.venv/bin/devkit-container", "run"]
