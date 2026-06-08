# Stage 1: Build frontend with Vite
FROM node:26-alpine@sha256:144769ec3f32e8ee36b3cfde91e82bee25d9367b20f31a151f3f7eea3a2a8541 AS builder
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
FROM python:3.14-slim@sha256:c845af9399020c7e562969a13689e929074a10fd057acd1b1fad06a2fb068e97

WORKDIR /app

# System deps — rarely change, cached as base layer
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc python3-dev curl tini \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 libffi-dev shared-mime-info \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 fonts-unifont \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI for trouble report filing
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps early — only re-runs when requirements.txt changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y gcc python3-dev && apt-get autoremove -y || true

# Install Chromium for Playwright/patchright (self-heal site testing)
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN python -m patchright install chromium

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

# Copy entrypoints
COPY docker-entrypoint.sh .
COPY enrichment-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh enrichment-entrypoint.sh

# Create non-root user for running the app process
RUN useradd -r -u 1000 -m appuser \
    && chown -R appuser:appuser /app \
    && mkdir -p /var/log/avail && chown appuser:appuser /var/log/avail \
    && mkdir -p /app/fix_queue && chown appuser:appuser /app/fix_queue

ENTRYPOINT ["tini", "--", "./docker-entrypoint.sh"]
# No --forwarded-allow-ips here: uvicorn safe-defaults to 127.0.0.1 when the
# image is run standalone. docker-compose.yml overrides `command:` to trust
# the compose network where Caddy fronts the app.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
