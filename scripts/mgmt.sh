#!/usr/bin/env bash
# Run an AvailAI app.management one-shot CLI on the HOST against the dockerized DB.
#
# What: cd to the repo root, point DATABASE_URL at the published Postgres
#   (rewrites the .env "@db:" docker hostname to "@127.0.0.1:" so host-run CLIs
#   reach the container's mapped 127.0.0.1:5432), then exec
#   `.venv/bin/python -m app.management.<module> [args...]`. The DB password stays
#   inside .env and never appears on the command line.
# Usage: scripts/mgmt.sh <module-short-name> [args...]
#   e.g. scripts/mgmt.sh import_demand_telemetry --apply
#        scripts/mgmt.sh ingest_source_data --files '/root/source_ingest/LSC1__*.csv' --apply
# Called by: an operator (or Claude Code via the Bash(scripts/mgmt.sh:*) allow-rule).
# Depends on: .env (DATABASE_URL), .venv (host venv kept in sync by deploy.sh),
#   the app.management package.
set -euo pipefail
cd "$(dirname "$0")/.."
export DATABASE_URL="$(grep '^DATABASE_URL=' .env | cut -d= -f2- | sed 's#@db:#@127.0.0.1:#')"
mod="${1:?usage: scripts/mgmt.sh <app.management module short name> [args...]}"
shift
exec .venv/bin/python -m "app.management.$mod" "$@"
