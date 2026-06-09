# CLAUDE.md — AvailAI

**AvailAI** — electronic-component sourcing engine and CRM for Trio Supply Chain Solutions.
**Stack:** FastAPI + SQLAlchemy 2.0 + PostgreSQL 16 + HTMX 2.x + Alpine.js 3.x + Jinja2 + Tailwind CSS 3.x.
**Deploy:** Docker Compose (app, enrichment-worker, db, redis, caddy, db-backup) on DigitalOcean.
**Version:** `APP_VERSION` constant in `app/config.py` (currently `3.1.0`).

It searches supplier APIs in parallel (BrokerBin, Nexar, DigiKey, Mouser, OEMSecrets,
Element14, Sourcengine, eBay, AI web search, email mining), tracks vendor intelligence,
enriches companies/contacts (Apollo), automates RFQ workflows via Microsoft Graph,
mines inboxes with Claude AI, and runs a full CRM (companies, quotes, buy plans,
customer matching).

---

## Authoritative Maps — read before exploring code

- `docs/APP_MAP_ARCHITECTURE.md` — stack, infra, project structure
- `docs/APP_MAP_DATABASE.md` — models, tables, relationships
- `docs/APP_MAP_INTERACTIONS.md` — service interactions, data flows, integration patterns
- `docs/BRANCH_AND_CI_WORKFLOW.md` — branch naming/lifecycle, the changed-files
  formatting gate (do NOT bundle unrelated drift), and quarantine-before-delete
  branch hygiene (`scripts/branch-cleanup.sh`)

These are the canonical reference. CLAUDE.md only points at them. **After any code
change, update the relevant APP_MAP doc(s) in the same PR.**

---

## CODE RULES

### Non-Negotiables
- **Stack is HTMX + Alpine.js + Jinja2 — NOT React.** Reject React/SPA patterns; if a problem seems to need React, you're modeling it wrong. Server-render + HTMX swap.
- **No band-aids, no half-measures.** Root-cause fixes only, even if interim breakage shows. Workarounds get the PR closed.
- **Quality > speed. Zero ambiguity in designs.** Resolve every decision in the spec; no TBDs, no "options A/B".
- **Hosted CLI only.** No GUI, no screenshots, no desktop-dependent suggestions, no browser visual companion.
- **Before pushing big PRs:** run `pre-commit run --all-files` (local git-hook scope is narrower than CI and bites otherwise).
- **Deploy is `./deploy.sh` only.** It passes a unique `--build-arg BUILD_COMMIT=<sha>-<ts>` that the Dockerfile consumes right before the source COPYs in BOTH stages, so templates/static/Vite always rebuild fresh while apt/pip/npm-ci cache (~30s deploys; PR #211 — no more `--no-cache`). Bare `docker compose up -d --build` (without that build-arg) ships stale templates. After deploy, verify any new Tailwind classes actually appear in built CSS.

- Always write tests with any new code. Don't ask, just include them.
- Give exact file paths for everything.
- Never use placeholder comments like "# rest of code here" — give complete code.
- Keep responses under 150 lines. Break big tasks into steps.
- Simple beats clever. 20 readable lines > 10 clever lines.
- Use Loguru for logging, never print().
- Use Ruff for linting.
- Use Alembic for database migrations. Always include rollback steps.

## Standing Workflow Rules

### Execution Model
- Always use subagent-driven execution for multi-step tasks — never ask, never offer inline
- Maximize parallel subagents for all independent work — never serialize what can parallelize
- Run the full skill pipeline on every task: brainstorm → plan → TDD → execute → simplify → review → verify (this order is canonical)
- Never skip a step because it seems "overkill" — use every available tool and skill aggressively
- Fix ALL review findings immediately — never defer as "lower priority" or "MVP acceptable"

### UI Guardrails
- Never add, remove, or rearrange UI elements without explicit user approval
- Follow existing codebase patterns — find a working example before creating new UI conventions

### Code Anti-Patterns (never introduce — in addition to Coding Conventions section)
- `innerHTML` → use `htmx.ajax()` or Alpine reactive binding
- Pydantic `class Config` → use `model_config = ConfigDict()`
- Alpine `_x_dataStack` → use `Alpine.store()`
- `db.query(Model).get(id)` → use `db.get(Model, id)`
- Anything with a literal `"` inside a **double-quoted** Alpine attribute (`x-data`/`@event`/`:bind`) → the `"` closes the attribute early and breaks Alpine init for the whole component (dead handlers, often only a console warning). Two common triggers: (a) prose / JS `//` comments — move them to a Jinja `{# #}` comment *outside* the attribute; (b) `{{ ...|tojson }}` — `tojson` is Markup-safe so a trailing `|e` is a **no-op**; embed it in a **single-quoted** attribute instead (`x-data='{{ x|tojson }}'`, tojson escapes `'`).
- Inner htmx container that should swap into **itself** but omits `hx-target` → under `<main id="main-content" hx-target="this">` it inherits `hx-target="this"` (resolving to `#main-content`) and **replaces the whole page** on its `load`/trigger. Always set an explicit `hx-target` (e.g. `hx-target="this"`) on faceted/lazy-load sub-containers.
- htmx event filter `[condition]` **not** hugging the event name → `keyup[cond]` is valid, but `keyup changed delay:800ms[cond]` (filter trailing a modifier) throws `htmx:syntax:error` and silently disables the trigger. The `[filter]` must immediately follow the event.

### Linear Development
- Memory references specific code (line numbers, function names)? Verify against current files before acting
- Plans or specs with line numbers? Verify those lines are still correct before editing
- Never mix old patterns with new — if the codebase has moved to a new pattern, follow the new one
- Always read the actual codebase before making changes — never rely on cached assumptions

### PR Reviews
- Run ALL pr-review-toolkit agents on every PR: comment-analyzer, pr-test-analyzer, type-design-analyzer, silent-failure-hunter, code-simplifier, code-reviewer
- Also run feature-dev:code-reviewer

---

## Project Layout

Top-level `app/` modules of note (see APP_MAP_ARCHITECTURE for the full tree):

- `main.py` — FastAPI app, router registration, middleware, lifespan
- `config.py` — Pydantic Settings (env vars, `APP_VERSION`, `MVP_MODE`)
- `database.py` — SQLAlchemy engine, `SessionLocal`, `UTCDateTime` type
- `dependencies.py` — auth deps: `require_user`, `require_admin`, `require_buyer`, `require_fresh_token`
- `constants.py` — status `StrEnum`s — always use these, never raw strings
- `shared_constants.py` — `JUNK_DOMAINS`, `JUNK_EMAIL_PREFIXES`
- `startup.py` — runtime ops only (triggers, seeds, ANALYZE) — **no DDL**
- `scheduler.py` / `jobs/` — APScheduler coordinator and job definitions
- `search_service.py` — requirement search orchestrator (all supplier sources)
- `email_service.py` — Graph API batch RFQ, inbox monitor, AI reply parsing
- `enrichment_service.py` — customer/vendor enrichment orchestrator
- `scoring.py` — sighting buyer-usefulness multi-factor scoring
- `evidence_tiers.py` — data-provenance tier tags for sightings/offers
- `vendor_utils.py` — `fuzzy_score_vendor()` (rapidfuzz wrapper) — use this, never inline fuzzy
- `http_client.py` — shared singleton `httpx.AsyncClient`s (connection pooling for outbound)
- `rate_limit.py` — shared rate limiter (Redis-backed, in-memory fallback)
- `prometheus_metrics.py` — ASGI metrics middleware + `/metrics` endpoint
- `connector_status.py` — logs which supplier connectors are enabled/disabled at startup
- `models/` `schemas/` `routers/` `services/` `connectors/` `cache/` `utils/` `management/`
- `config/routing_maps.json` — brand→commodity inference maps (used by buy-plan scoring)
- `templates/` (Jinja2) `static/` (Vite-built assets in `static/dist/`)

`app/management/` holds one-off CLI commands (e.g. `python -m app.management.reenrich`).

Migrations live at repo-root `alembic/versions/`, **not** under `app/`.
Tests live in `tests/` (unit/integration) and `tests/e2e/` (Playwright).

---

## Auth Model

OAuth2 via Azure AD — `app/routers/auth.py` handles login/callback/logout.
Session middleware stores `user_id` in an HTTP-only cookie (15-min expiry).
`require_fresh_token` re-validates with a 15-min buffer.

Permission dependencies: `require_user` (any login), `require_buyer` (search/RFQ),
`require_admin` (settings, user management).

---

## Coding Conventions

### Database
- Use `db.get(Model, id)`, **not** `db.query(Model).get(id)` (SQLAlchemy 2.0 style).
- Status values: always use `StrEnum` constants from `app/constants.py`, never raw strings
  (e.g. `RequisitionStatus.OPEN`, `RequirementStatus.FOUND`).
- Keep routers thin (HTTP only); put business logic in `app/services/`.

### Search & Matching
- Vendor matching: `fuzzy_score_vendor()` from `app/vendor_utils.py` (rapidfuzz wrapper). Never inline fuzzy logic.
- MPN dedup: `strip_packaging_suffixes()` from `app/services/search_worker_base/mpn_normalizer.py`.
- **`search_requirement()` uses a separate write session** — the caller's ORM objects are
  stale after it returns. Call `db.expire(requirement)` before rendering templates.

### MPN Normalization
- `normalize_mpn()` uppercases, strips noise, returns `None` for MPNs < 3 chars.
- `@validates` on `Requirement` auto-uppercases `primary_mpn`, `customer_pn`, `oem_pn` on save.
- Use the `|sub_mpns` Jinja2 filter to display substitutes (handles string and dict forms).

### Substitutes Format
- Canonical: `[{"mpn": "ABC123", "manufacturer": "TI"}, ...]` (list of dicts).
- Legacy rows may hold plain strings `["ABC123"]` — always handle both.
- Write paths: use `parse_substitute_mpns()` from `app/utils/normalization.py`.

### Shared Constants
- Junk email domains/prefixes: import `JUNK_DOMAINS` / `JUNK_EMAIL_PREFIXES` from `app/shared_constants.py`. Don't duplicate.

### Caching
```python
from app.cache.decorators import cached_endpoint

@cached_endpoint("vendor_list", ttl_hours=24, key_params=["supplier"])
async def get_vendors(supplier: str): ...
```

### Logging
- Always Loguru (`from loguru import logger`), never `print()`. request_id context is auto-injected.

### Frontend
- Navigation is HTMX-driven: `<a hx-get>` → server HTML fragment → swap into `#main-content`. No client routing, no JSON.
- State is Alpine.js via `Alpine.store()` (persisted with the `@persist` plugin).
- Toast: `$store.toast` has `message`, `type`, `show` (boolean) — set them directly; `show` is not a method.
- Plugin/extension lists: see `app/static/htmx_app.js` and `app/templates/base.html`.
- **Before editing any template**, trace `router → view function → template_response()`.
  Routers can render a partial whose path doesn't match the router name — follow the router.

### Response Formats
- JSON errors: `{"error": "...", "status_code": 400, "request_id": "..."}` — tests check `["error"]`, not `["detail"]`.
- List responses: `{"items": [...], "total": N, "limit": N, "offset": N}` — not a plain array.
- HTMX responses: `HTMLResponse` from Jinja2. Schemas in `app/schemas/`.

### Files
- Every new file needs a header comment: what it does, what calls it, what it depends on.

---

## Database & Migration Rules

### ABSOLUTE RULES — NEVER VIOLATE
1. **ALL schema changes go through Alembic.** Never use raw DDL in startup.py, services, routers, or scripts.
2. **Never use Base.metadata.create_all() for schema changes.**
3. **Never run raw SQL against production** outside of a migration.
4. **Migration workflow — every time:**
   a. Make model change in `app/models/`
   b. Run: `alembic revision --autogenerate -m "description"`
   c. REVIEW the generated migration
   d. Test: upgrade → downgrade → upgrade
   e. Commit migration with model change

5. **After creating a migration, ALWAYS run `alembic heads`** to verify a single head. If multiple heads: `alembic merge heads -m "merge_description"`. Data-only migrations (no schema changes) use `op.get_bind()` + raw SQL via `text()`.

6. **startup.py is for runtime operations ONLY:**
   - FTS triggers (PostgreSQL-specific)
   - Seed data (system_config defaults)
   - ANALYZE on hot tables
   - Idempotent backfill queries
   - Count triggers (PG-specific)
   - NOTHING that creates, alters, or drops tables/columns/indexes/constraints

---

## Commands

### Docker
```bash
docker compose up -d              # Start all containers
docker compose logs -f app        # Tail app logs
docker compose ps                 # Show status
docker compose down               # Stop everything
```

### Deploy
```bash
./deploy.sh                       # From main: commit + push + rebuild + health-check + verify
./deploy.sh --no-commit           # Rebuild current branch without committing/pushing
```
`deploy.sh` must run from `main` unless `--no-commit`. It builds with a unique
`BUILD_COMMIT` build-arg (consumed before the source COPYs in both Dockerfile stages, so
templates/static/Vite always rebuild fresh while apt/pip/npm-ci cache — no `--no-cache`),
rebuilds + recreates **both** the `app` and `enrichment-worker` containers (they share
`requirements.txt`, so the worker must not lag the app on a dependency bump), waits for
the app health check, verifies the deployed build tag on both, checks that Tailwind
classes in templates exist in the built CSS bundle, and restarts the host `nc`/`ics`
worker systemd units (they run from `/root/availai` outside docker, so they'd otherwise
keep running stale code after a deploy).
Never use bare `docker compose up -d --build` (without `--build-arg BUILD_COMMIT=...`) —
it ships stale templates.

### Python lint / type check
```bash
ruff check app/                   # Lint
ruff format app/                  # Auto-format
mypy app/                         # Type check
```
Pre-commit hooks: ruff, ruff-format, mypy, docformatter, detect-private-key.

### Dependencies (pip-tools lockfiles)

Python deps are **pinned lockfiles** compiled from hand-authored sources, so every
install (CI + deploy) is reproducible.

- **Edit** `requirements.in` (prod) / `requirements-dev.in` (dev) — never the `.txt`.
- **Recompile** the locks, then commit BOTH the `.in` and the regenerated `.txt`:
  ```bash
  pip-compile --no-header --no-strip-extras requirements.in
  pip-compile --no-header --no-strip-extras requirements-dev.in
  ```
- `requirements.txt` (prod lock) is what the Docker image + `deploy.sh` install;
  `requirements-dev.txt` adds the dev/test tools. CI fails if a `.txt` drifts from
  its `.in` (the "Verify requirements lockfiles are in sync" step).
- To bump a dep: change the constraint in the `.in`, recompile, run the suite. To
  refresh transitive pins to newest: `pip-compile --upgrade ...`.
- **Dependabot pip PRs** edit the files textually and can't run pip-compile, so they
  fail the sync gate. `.github/workflows/dependabot-lockfile-sync.yml` auto-recompiles
  the locks and pushes the corrected `.txt` back to the PR (set a `DEPENDABOT_LOCKFILE_TOKEN`
  PAT secret to make that push re-trigger CI / turn the checks green).

### Migrations
```bash
alembic upgrade head                              # Apply pending
alembic downgrade -1                              # Rollback one
alembic revision --autogenerate -m "description"  # New migration
alembic current / alembic history / alembic heads
```

### Tests
`pytest.ini` sets `asyncio_mode=auto`, `-n auto` (xdist parallel), a 30s per-test
timeout, and ignores `tests/e2e/`. Always set `TESTING=1` (disables scheduler and
real API calls); tests use in-memory SQLite. `conftest.py` provides fixtures and the
test `engine`.

```bash
# During dev — changed module only
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_<module>.py -v

# Before commit — full suite
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v

# Before PR only — coverage
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q

# Skip slow tests
TESTING=1 PYTHONPATH=/root/availai pytest -m "not slow" -v

# Single file without xdist parallelism
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_foo.py -v --override-ini="addopts="
```
Never add `--cov` to iterative dev runs — only before a PR. Mock lazy imports at the
source module, not the import site.

### Frontend / E2E
```bash
npm run dev          # Vite dev server (localhost:5173)
npm run build        # Production build → app/static/dist/ (runs bundle smoke test)
npm run lint         # ESLint
npm run test:frontend  # Vitest
npx playwright test --project=workflows   # E2E (also: dead-ends, visual, accessibility)
```

---

## MVP Mode

`config.py: mvp_mode` gates Dashboard, Enrichment, Teams, Task Manager. Core MVP scope:
Requisitions, Customers, Vendors, Sourcing Engine.

---

## Observability

- **Sentry** — initialized in `app/main.py` lifespan only when `SENTRY_DSN` is set
  (FastAPI, httpx, Loguru, SQLAlchemy integrations). A `before_send` hook scrubs
  sensitive data — extend it, never bypass it, when adding new sensitive fields.
- **Prometheus** — `app/prometheus_metrics.py` is a pure ASGI middleware exposing
  `http_requests_total`, `http_request_duration_seconds`, and `http_requests_inprogress`
  at `GET /metrics` (token-gated). It replaced `prometheus-fastapi-instrumentator` (which
  hard-pinned `starlette<1.0.0`); keep it streaming-safe (don't consume response bodies)
  so it composes with SSE endpoints.
- **Logging** — Loguru with auto-injected `request_id` context (see Logging convention).

---

## Configuration

All config via `.env` (see `.env.example`). Key groups: Azure OAuth (`AZURE_CLIENT_ID`,
`AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`, `AZURE_REDIRECT_URI`), Anthropic
(`ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`), `DATABASE_URL`, `REDIS_URL`, supplier API keys
(feature disabled if unset — e.g. `NEXAR_CLIENT_ID`, `BROKERBIN_API_KEY`,
`DIGIKEY_CLIENT_ID`, `MOUSER_API_KEY`), feature flags (`MVP_MODE`, `EMAIL_MINING_ENABLED`,
etc.), email (`MICROSOFT_GRAPH_ENDPOINT`, `SMTP_FROM`), and observability (`SENTRY_DSN` —
optional).

---

## Triggers

- "new feature" = make a plan first, don't just start coding
- "bug" / "error" = ask for the full error message before fixing
- "refactor" = check what's stable first
- "quick" / "just" = warn about hidden complexity

## Safety

- WARN before DROP, DELETE, or bulk data changes; include backup + rollback steps.
- For production, verify a backup exists first. The `db-backup` service runs `pg_dump`
  every 6 hours; manual restore via `scripts/restore.sh`.

---

## Skill Usage Guide

Invoke skills proactively — don't ask. Triggers:

| Trigger | Skill |
|---|---|
| Adding/modifying FastAPI routes, deps, middleware | `fastapi` |
| Writing SQLAlchemy 2.0 models, queries, relationships, fixtures | `sqlalchemy` |
| Adding HTMX attributes, partials, inline edits, lazy-loads, modals | `htmx` |
| Editing Jinja2 templates, macros, custom filters, partials | `jinja2` |
| Touching `vite.config.js`, JS/CSS entry points, Vitest specs | `vite` |
| Building/styling UI (HTMX + Alpine + Tailwind) | `frontend-design` |
| Writing/fixing pytest tests, fixtures, mocks | `pytest` |
| Adding/invalidating Redis cache, `@cached_endpoint` | `redis` |
| Fixing pre-commit lint errors, ruff rules, noqa | `ruff` |
| Fixing mypy errors, adding type annotations | `mypy` |
| Writing/extending Playwright E2E specs | `playwright` |
| Empty states, first-run flows, onboarding nudges | `designing-onboarding-paths`, `orchestrating-feature-adoption` |
| Funnel events, activity tracking, activation metrics | `mapping-conversion-events`, `instrumenting-product-metrics` |
| Page copy, CTAs, microcopy | `crafting-page-messaging`, `tuning-landing-journeys` |
| SEO/meta/structured data on public pages | `inspecting-search-coverage`, `adding-structured-signals` |
