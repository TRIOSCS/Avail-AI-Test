#!/bin/sh
# Validate required environment variables
MISSING=""
for var in DATABASE_URL SESSION_SECRET AZURE_CLIENT_ID AZURE_TENANT_ID; do
    eval val=\$$var
    if [ -z "$val" ]; then
        MISSING="$MISSING $var"
    fi
done
if [ -n "$MISSING" ]; then
    echo "WARNING: Missing required env vars:$MISSING"
fi
if [ "$SESSION_SECRET" = "change-me-to-random-string" ]; then
    echo "ERROR: SESSION_SECRET is still the default — refusing to start"
    echo "Run: openssl rand -hex 32  and set SESSION_SECRET in .env"
    exit 1
fi

# Copy static files to shared volume for Caddy direct serving (runs as root)
if [ -d /srv/static ]; then
    # Clean stale files from previous builds (hashed names change each build)
    rm -rf /srv/static/assets /srv/static/.vite
    # Remove legacy raw source files no longer served directly
    rm -f /srv/static/app.js /srv/static/crm.js /srv/static/styles.css
    # Copy fresh Vite build output
    cp -r app/static/dist/* /srv/static/
fi

# Run database migrations before starting the app (DB is healthy via depends_on)
echo "Running alembic upgrade head..."
if ! runuser -u appuser -- alembic upgrade head; then
    echo "ERROR: alembic upgrade head failed — refusing to start app."
    exit 1
fi
echo "Alembic: migrations applied."

# Drop to non-root user and start the app
exec runuser -u appuser -- "$@"
