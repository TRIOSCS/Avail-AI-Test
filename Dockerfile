# Stage 1: Build frontend with Vite
FROM node:20-alpine AS builder
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY vite.config.js tailwind.config.js postcss.config.js ./
COPY app/static/ app/static/
COPY app/templates/ app/templates/
RUN npm run build

# Stage 2: Python application
FROM python:3.12-slim

WORKDIR /app

# System deps — rarely change, cached as base layer
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc python3-dev curl tini \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 libffi-dev shared-mime-info \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 fonts-unifont \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps early — only re-runs when requirements.txt changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y gcc python3-dev && apt-get autoremove -y || true

# Install Chromium for Playwright/patchright (self-heal site testing)
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN python -m patchright install chromium

# Copy app code and Alembic (for migrations at runtime)
COPY app/ app/
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
    && mkdir -p /app/fix_queue && chown appuser:appuser /app/fix_queue

ENTRYPOINT ["tini", "--", "./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
