# STABLE.md — Registry of critical/stable files

Do not refactor these without explicit approval. Changes here can break startup, auth, or core flows.

## Core app
- `app/main.py` — FastAPI app, middleware, router includes, health check
- `app/startup.py` — Boot-time migrations and setup (no DDL; FTS, seeds, backfills)
- `app/config.py` — Settings from env
- `app/database.py` — Session factory

## Auth & dependencies
- `app/dependencies.py` — require_user, require_buyer, auth middleware

## Critical routers (thin entry points; logic in services)
- `app/routers/auth.py`
- `app/routers/requisitions/` (core, requirements, attachments)
- `app/routers/task.py`
- `app/routers/crm/` (companies, offers, quotes, buy_plans, etc.)
- `app/routers/rfq.py`

## Frontend (single-page app)
- `app/templates/index.html`
- `app/static/app.js`
- `app/static/crm.js`
- `app/static/styles.css`

When changing any listed file, run tests and do a quick smoke check before committing.
