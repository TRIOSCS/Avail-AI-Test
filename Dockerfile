# Stage 1: Build frontend with Vite
FROM node:20-alpine AS builder
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY vite.config.js ./
COPY app/static/ app/static/
RUN npm run build

# Stage 2: Python application
FROM python:3.12-slim

WORKDIR /app

# System deps for thefuzz (C extension) and WeasyPrint (PDF rendering)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc python3-dev curl \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 libffi-dev shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y gcc python3-dev && apt-get autoremove -y || true

# Copy app code
COPY app/ app/

# Overlay Vite build output from stage 1
COPY --from=builder /build/app/static/dist/ app/static/dist/

# Copy migration scripts
COPY migrate_*.py .

# Copy entrypoint
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

# Create non-root user for running the app process
RUN useradd -r -u 1000 -m appuser \
    && chown -R appuser:appuser /app \
    && mkdir -p /var/log/avail && chown appuser:appuser /var/log/avail

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
