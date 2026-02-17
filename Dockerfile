FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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
