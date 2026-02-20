#!/bin/sh
# Copy static files to shared volume for Caddy direct serving (runs as root)
if [ -d /srv/static ]; then
    cp -r app/static/* /srv/static/
fi

# Drop to non-root user for the app process
exec runuser -u appuser -- "$@"
