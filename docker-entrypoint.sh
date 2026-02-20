#!/bin/sh
# Copy static files to shared volume for Caddy direct serving
if [ -d /srv/static ] && [ -w /srv/static ]; then
    cp -r app/static/* /srv/static/
fi

exec "$@"
