#!/bin/sh
# Copy static files to shared volume for Caddy direct serving (runs as root)
if [ -d /srv/static ]; then
    # Clean stale files from previous builds (hashed names change each build)
    rm -rf /srv/static/assets /srv/static/.vite
    # Remove legacy raw source files no longer served directly
    rm -f /srv/static/app.js /srv/static/crm.js /srv/static/styles.css
    # Copy fresh Vite build output
    cp -r app/static/dist/* /srv/static/
fi

# Drop to non-root user for the app process
exec runuser -u appuser -- "$@"
