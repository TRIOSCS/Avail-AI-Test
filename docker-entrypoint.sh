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

# Ensure the upload subdirs on the `uploads` named volume are owned by appuser
# (TT-0002): trouble-ticket screenshots AND profile avatars. The Dockerfile
# chowns them at build time, but Docker seeds a *fresh* volume from the image dir
# only — an EXISTING (e.g. root-owned, pre-this-fix) volume keeps its old
# ownership across deploys/restarts, so the non-root app process can't write.
# Re-asserting it here (as root, before we drop to appuser below) makes the fix
# durable on every container start.
mkdir -p /app/uploads/tickets /app/uploads/avatars
chown -R appuser:appuser /app/uploads

# Copy static files to shared volume for Caddy direct serving (runs as root)
if [ -d /srv/static ]; then
    # Clean stale files from previous builds (hashed names change each build)
    rm -rf /srv/static/assets /srv/static/.vite
    # Remove legacy raw source files no longer served directly
    rm -f /srv/static/app.js /srv/static/crm.js /srv/static/styles.css
    # Copy fresh Vite build output
    cp -r app/static/dist/* /srv/static/
fi

# Run database migrations before starting the app (DB is healthy via depends_on).
# A failed migration is fatal: starting the app against an un-migrated schema
# risks data corruption and masks the real problem. `alembic upgrade head` is
# idempotent — it is a no-op when the schema is already current, so a non-zero
# exit always means a genuine failure.
echo "Running alembic upgrade head..."
if ! runuser -u appuser -- alembic upgrade head 2>&1; then
    echo "ERROR: alembic upgrade head failed — refusing to start the app." >&2
    echo "The database schema is not at head. Investigate the migration failure" >&2
    echo "before restarting; do not start the app against a stale schema." >&2
    exit 1
fi
echo "Alembic: migrations complete."

# Drop to non-root user and start the app
exec runuser -u appuser -- "$@"
