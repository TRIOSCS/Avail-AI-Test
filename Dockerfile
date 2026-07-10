# Stage 1: Build frontend with Vite
FROM node:26-alpine@sha256:725aeba2364a9b16beae49e180d83bd597dbd0b15c47f1f28875c290bfd255b9 AS builder
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
# Per-deploy cache-bust: BUILD_COMMIT is unique each deploy and this RUN consumes it, so
# every layer below (the source COPYs + the Vite build) ALWAYS re-runs with fresh
# templates/static — fresh Tailwind CSS guaranteed — while `npm ci` above stays cached.
# This is what lets deploy.sh build WITHOUT --no-cache (apt/pip/npm ci all cache) yet
# never ship a stale template.
ARG BUILD_COMMIT=unknown
RUN echo "$BUILD_COMMIT" > /build/.build_commit
COPY vite.config.js tailwind.config.js postcss.config.js ./
COPY app/static/ app/static/
COPY app/templates/ app/templates/
RUN npm run build

# Stage 2: Python application
FROM python:3.14-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1

WORKDIR /app

# System deps — rarely change, cached as base layer
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc python3-dev curl tini \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 libffi-dev shared-mime-info \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 fonts-unifont \
    && rm -rf /var/lib/apt/lists/*

# P1.4 (docs/CODE_AUDIT_AND_HARDENING_PLAN.md): the GitHub CLI (`gh`, previously
# installed here unpinned from apt) and headless Chromium (previously installed
# below via `python -m patchright install chromium`) were REMOVED from this
# internet-facing prod image. Verified: grep of app/ found no runtime consumer —
# the only `gh`-shelling code is dev tooling (scripts/branch-cleanup.sh,
# scripts/worktree-guard.sh, run by developers/CI, never by the `app` or
# `enrichment-worker` processes), and the only patchright/Chromium consumers
# (app/services/{ics,nc,tbf}_worker/session_manager.py) run as separate host
# systemd units from /root/availai/.venv against system Google Chrome — see
# scripts/setup_nc_worker.sh / setup_tbf_worker.sh — NOT inside this container.
# Both now live in Dockerfile.tooling (built/run manually, never part of
# docker-compose orchestration) with `gh` pinned to an exact release.

# Install Python deps early — only re-runs when requirements.txt changes
COPY requirements.txt .
# NOTE: `|| true` is scoped to the apt cleanup ONLY — a failed pip install must
# fail the build (it used to cover the whole chain, masking dependency failures).
RUN pip install --no-cache-dir -r requirements.txt \
    && { apt-get purge -y gcc python3-dev && apt-get autoremove -y || true; }

# NOTE: no Chromium binary is installed here (P1.4 — see comment above the `gh`
# removal). The `patchright` *package* stays in requirements.txt/the image only
# because it's the same shared lockfile the host tbf/nc/ics workers install
# from; nothing that runs inside this image ever calls `start_browser()`.

# Bake git commit hash — placed here so it only busts cache for cheap layers below
ARG BUILD_COMMIT=unknown
ENV BUILD_COMMIT=${BUILD_COMMIT}

# Copy app code, scripts, and Alembic (for migrations at runtime)
COPY app/ app/
COPY scripts/ scripts/
COPY alembic.ini .
COPY alembic/ alembic/

# Overlay Vite build output from stage 1
COPY --from=builder /build/app/static/dist/ app/static/dist/

# Copy entrypoint
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

# Create non-root user for running the app process
RUN useradd -r -u 1000 -m appuser \
    && chown -R appuser:appuser /app \
    && mkdir -p /var/log/avail && chown appuser:appuser /var/log/avail \
    && mkdir -p /app/uploads/tickets /app/uploads/avatars && chown -R appuser:appuser /app/uploads
# NOTE: /app/uploads is a named volume (see docker-compose.yml). Docker seeds a
# *fresh* volume from this image dir, so creating it appuser-owned here makes new
# volumes writable by the runtime user (trouble-ticket screenshots + profile
# avatars). An EXISTING
# root-owned volume is NOT re-seeded from the image, so docker-entrypoint.sh
# re-asserts `chown -R appuser:appuser /app/uploads` on every start (TT-0002),
# and app/startup.py fails fast at boot if the dir still isn't writable.

# P1.4 hardening — USER/entrypoint reconciliation (studied docker-entrypoint.sh
# before deciding; do not "fix" this without re-reading it):
# A blanket final `USER appuser` directive was considered and REJECTED. Docker's
# USER applies to the ENTRYPOINT+CMD process tree as a whole, not just CMD, so
# it would make `tini`/docker-entrypoint.sh itself start as appuser. That
# breaks two things the entrypoint does that genuinely require root:
#   1. `chown -R appuser:appuser /app/uploads` (entrypoint ~line 27, TT-0002) —
#      re-asserted on every start because an EXISTING (pre-fix or otherwise
#      root-owned) named volume isn't re-seeded from the image; a non-root
#      process can't chown files it doesn't own.
#   2. `runuser -u appuser -- alembic upgrade head` then
#      `exec runuser -u appuser -- "$@"` (entrypoint ~lines 45,54) — `runuser`
#      ESCALATES DOWN from root; a non-root PID 1 cannot use it to become a
#      different user at all, so migrations would fail outright.
# Both are exactly what "do not ship a change that breaks the migration step"
# rules out, so this stays root-at-entrypoint / drop-privileges-inside (the
# current, working behavior) rather than a half-measure. The actual app
# process (uvicorn) already runs as `appuser`, never root, by the time it
# serves traffic. The one real gap this leaves: `docker compose exec app <cmd>`
# with no `--user` flag defaults to root. Until the migration step is pulled
# out of the entrypoint into a separate root-capable init container (a real
# architecture change — tracked as a P1.4 follow-up, not a band-aid to bolt on
# here), operators wanting a non-root shell should run
# `docker compose exec --user appuser app bash` explicitly.
ENTRYPOINT ["tini", "--", "./docker-entrypoint.sh"]
# No --forwarded-allow-ips here: uvicorn safe-defaults to 127.0.0.1 when the
# image is run standalone. docker-compose.yml overrides `command:` to trust
# the compose network where Caddy fronts the app.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
