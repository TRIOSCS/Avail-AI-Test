# STABLE.md — Registry of critical/stable files

Do not refactor these without explicit approval. Changes here can break startup, auth, or core flows.

## Core app
- `app/main.py` — FastAPI app, middleware, router includes, health check
- `app/startup.py` — Boot-time migrations and setup (no DDL; FTS, seeds, backfills)
- `app/config.py` — Settings from env
- `app/database.py` — Session factory

## Auth & dependencies
- `app/dependencies.py` — require_user, require_buyer, auth middleware

## Encryption (at-rest secrets)

- `app/utils/encrypted_type.py` — `EncryptedText` SQLAlchemy type + `build_fernet()`
  key derivation for `users.refresh_token` / `access_token` / `password_hash`.
- `app/services/credential_service.py` — the **same** `ENCRYPTION_SALT` keys
  `api_sources.credentials` (supplier API keys; graceful env-var fallback on a decrypt miss).
- **`SECRET_KEY` + `ENCRYPTION_SALT` are jointly load-bearing.** Both feed the Fernet
  key (PBKDF2). Changing either orphans every encrypted cell. When `ENCRYPTION_SALT` is
  unset, the hard-coded legacy salts (`_LEGACY_SALT`, `_LEGACY_CREDENTIAL_SALT`) stand in,
  so `SECRET_KEY` alone is load-bearing in that mode. Never change these casually.
- **Rotate the salt** with `python -m app.management.rotate_encryption_salt` (`--dry-run`
  first; idempotent/resumable). Full procedure: `docs/PRE_ROLLOUT_CHECKLIST.md` Gate 4.

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

## Deploy hygiene

- **Droplet's local `main` must be fast-forward-only with `origin/main` before any deploy.** Before running `./deploy.sh`, resync: `git fetch origin && git checkout main && git merge --ff-only origin/main`. If that merge is not fast-forward-able, stop and investigate — someone's local work has diverged from the remote and pushing will either silently fail (non-fast-forward rejection) or risk rewinding `origin/main`. `deploy.sh` currently swallows non-fast-forward rejections; a follow-up PR will make it fail loudly and/or auto-resync.
- When working on a feature branch on the droplet, prefer `./deploy.sh --no-commit` (rebuild + push-skipped) so the deploy reflects local code without attempting to push a stale `main`.

## Known tech debt

- **`app/static/htmx_app.js` — `htmx:afterSwap` Alpine.initTree gate uses a hardcoded ID allowlist** (`lead-drawer-content`, `rq2-table`). When future HTMX-swapped regions contain Alpine directives (`x-*`), they must be added manually to this list or their directives won't re-bind after swap. Fragile. Future refactor idea: trigger `initTree` whenever the swap target subtree contains any element with an `x-*` attribute, so new regions get covered automatically. Not urgent — Alpine's own MutationObserver handles most cases, and the allowlist is a belt-and-suspenders fallback. Captured 2026-04-21 during the opportunity-table v2 merge.

## Opportunity Table (/requisitions2)

The `/requisitions2` split-workspace left panel renders the v2 opportunity
table (status dots, urgency accents, deal-value typography, coverage meter,
aggregated MPN chip row, truncation tooltips, hover action rail). This is the
sole rendering — the former opportunity-table-v2 feature flag and the legacy
5-col fallback have been retired.

**Token set:** `app/static/styles.css` `:root { --opp-* }` variables for
dot colors, urgency border/text, coverage fill, text primary/secondary/
tertiary, separator. Component classes: `.opp-status-dot`, `.opp-status-label`,
`.opp-time--{24h,72h,normal}`, `.opp-deal--tier-{primary-500,primary-400,tertiary}`,
`.opp-deal--computed`, `.opp-deal--partial`, `.opp-coverage-seg`,
`.opp-row--urgent-{24h,72h}`, `.opp-col-header`, `.opp-chip-row`,
`.opp-chip-more`, `.opp-name-cell`, `.opp-action-rail*`, `.truncate-tip`.

**Spec:** `docs/superpowers/specs/2026-04-21-opportunity-table-merged-design.md`.
