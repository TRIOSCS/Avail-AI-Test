FROM python:3.12-slim

WORKDIR /app

# System deps for thefuzz (C extension) and WeasyPrint (PDF rendering)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc python3-dev \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 libffi-dev shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y gcc python3-dev && apt-get autoremove -y || true

# Install Node.js + terser for JS minification (lightweight, no full Node app)
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && npm install -g terser \
    && apt-get purge -y npm && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /root/.npm

# Copy app code
COPY app/ app/

# Minify JS assets (~40-50% size reduction)
RUN terser app/static/app.js -o app/static/app.js -c -m \
    && terser app/static/crm.js -o app/static/crm.js -c -m

# Copy migration scripts
COPY migrate_*.py .

# Run the app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
