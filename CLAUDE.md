# CLAUDE.md — AvailAI

Operating manual for Claude Code sessions in this repo. Read top to bottom on a cold start. Long-form architecture lives in `docs/APP_MAP_*.md`; this file is the lean operating contract.

## 1. What this is

AvailAI — electronic-component sourcing engine and CRM for Trio Supply Chain Solutions. FastAPI + SQLAlchemy 2.0 + PostgreSQL 16 + HTMX 2 + Alpine.js 3 + Jinja2 + Tailwind. Hosted on a single DigitalOcean droplet at `app.availai.net`. Deployed via Docker Compose.

- **Python:** 3.12 (pinned in `Dockerfile`, `ruff.toml`, `pyproject.toml [tool.mypy]`).
- **Compose services:** `app, db, redis, caddy, db-backup`. `enrichment-worker` exists but is permanently disabled (`docker-compose.yml:113` — replaced by AI-based enrichment).
- **Architecture deep-dives:** `docs/APP_MAP_ARCHITECTURE.md`, `docs/APP_MAP_DATABASE.md`, `docs/APP_MAP_INTERACTIONS.md`. Update the relevant doc after any code change.
- **Do-not-touch list:** `STABLE.md` in repo root (entry points, auth, critical routers, the `AVAIL_OPP_TABLE_V2` feature flag, rollback SOP).

## 2. Hard rules — read before touching anything

These are blast-radius rules. Violate them and you break production.

1. **HTMX-not-React is load-bearing.** Server returns HTML fragments; HTMX swaps them; Alpine.js holds local state via `Alpine.store(...)`. Never introduce React, Vue, Svelte, or any client-side router. No JSON-driven SPA patterns.
2. **All schema changes go through Alembic.** Never use `Base.metadata.create_all()`. Never run raw DDL outside a migration (no `CREATE/ALTER/DROP` in `startup.py`, services, routers, scripts). Migrations live in `alembic/versions/` (130 files; `alembic.ini` at repo root).
3. **Migration workflow:** edit model → `alembic revision --autogenerate -m "..."` → review the generated file → test upgrade/downgrade/upgrade → commit migration with the model change. After creating one, run `alembic heads` and merge if multiple heads.
4. **`startup.py` is runtime-only.** FTS triggers, seed data, ANALYZE, idempotent backfills. Nothing that creates/alters/drops tables, columns, indexes, or constraints.
5. **Deploy via `./deploy.sh`** (commits, pushes, builds with `--no-cache`, recreates app container, runs 30×2s health-check loop). Never bare `docker compose up -d --build` — Docker layer cache silently keeps stale templates and code, producing "code didn't update" bugs. Use `./deploy.sh --no-commit` to redeploy current branch without committing.
6. **Status values come from `app/constants.py` StrEnums.** 26 enums (`RequisitionStatus`, `RequirementStatus`, `OfferStatus`, `UserRole`, …). Never hardcode `"open"`, `"sent"`, `"archived"`, etc. — the strings change, the enums don't.
7. **Files listed in `STABLE.md` are frozen** unless the task explicitly calls them out. Re-read `STABLE.md` before editing `app/main.py`, `app/startup.py`, `app/config.py`, `app/database.py`, `app/dependencies.py`, the auth/requisitions/crm/rfq routers, or the named frontend assets.
8. **`AVAIL_OPP_TABLE_V2` gates `/requisitions2` rendering.** Default true. Flippable via `.env` + `docker compose restart app` (≈30s). Don't remove the flag or its `{% else %}` branches without explicit approval — see `STABLE.md:38-60`.
9. **Hosted-CLI environment.** No GUI, no browser preview, no screenshots. Don't suggest commands that assume a desktop. Verify UI changes via `curl`, log inspection, or by asking the user to test.
10. **Root-cause fixes only.** No band-aids, no half-measures, no `try/except: pass`. If you find a band-aid PR, default is to close it and write the real fix.

## 3. Workflow — how to work in this repo

- **Use subagents for multi-step tasks.** Run independent work in parallel.
- **Pipeline for any non-trivial change:** brainstorm → plan → TDD → execute → simplify → review → verify. Skip a step only when truly redundant.
- **Pre-commit is auto-installed for both `pre-commit` and `pre-push` stages** (see `.pre-commit-config.yaml`). The pre-push gate runs the full suite against `--all-files` to catch branch-wide drift. Don't bypass with `--no-verify`; fix the root cause.
- **Verify before claiming done.** Run the actual command, read the actual output, then claim success. Type-check and tests verify code; for UI changes you can't browser-test, say so explicitly.
- **Update `docs/APP_MAP_*.md`** after the change lands; it's the second source of truth for future sessions.
- **Memory references with line numbers can be stale.** Re-read the file before acting on a memory that cites `file.py:123`.

## 4. Repo layout

```
availai/
├── app/                  # 22 top-level .py modules + subdirs
│   ├── main.py           # FastAPI app, middleware stack, router includes, exception handlers
│   ├── startup.py        # Runtime ops (FTS triggers, seeds, ANALYZE) — NO DDL
│   ├── config.py         # Pydantic Settings (env vars, MVP_MODE, feature flags)
│   ├── database.py       # SQLAlchemy engine, SessionLocal, UTCDateTime type
│   ├── dependencies.py   # require_user / require_buyer / require_admin / require_fresh_token
│   ├── constants.py      # 26 StrEnums — the only valid source for status strings
│   ├── shared_constants.py # JUNK_DOMAINS, JUNK_EMAIL_PREFIXES — import, don't duplicate
│   ├── scheduler.py      # APScheduler coordinator
│   ├── search_service.py # Multi-source search orchestrator (uses its own write session)
│   ├── email_service.py  # Microsoft Graph: batch RFQ send, inbox monitor, AI parse
│   ├── enrichment_service.py
│   ├── scoring.py        # 6-factor sighting / vendor / lead scoring
│   ├── vendor_utils.py   # fuzzy_score_vendor() — rapidfuzz wrapper
│   ├── rate_limit.py     # slowapi config
│   ├── http_client.py / connector_status.py / evidence_tiers.py / company_utils.py / file_utils.py / logging_config.py / template_env.py
│   ├── cache/decorators.py # @cached_endpoint(prefix, ttl_hours, key_params)
│   ├── connectors/       # 10 external API modules. `sources.py` houses NexarConnector + BrokerBinConnector; standalone modules: digikey, mouser, oemsecrets, element14, sourcengine, ebay, apollo, ai_live_web, email_mining
│   ├── jobs/             # APScheduler job definitions (inbox_monitor, requirement_refresh, …)
│   ├── models/           # SQLAlchemy ORM
│   ├── routers/          # HTTP entry points (htmx_views.py serves /v2/*)
│   ├── schemas/          # 25 Pydantic schema modules (NOT one monolith). errors.py defines the canonical error shape
│   ├── services/         # Business logic (search_worker_base, ics_worker, nc_worker, response_parser, …)
│   ├── static/           # htmx_app.js (Alpine bootstrap, stores), styles.css, dist/ (Vite build)
│   ├── templates/        # Jinja2; htmx/partials/ for HTMX fragments; documents/ for PDFs
│   └── utils/            # claude_client, graph_client, normalization (MPN), encrypted_type, …
├── alembic/versions/     # 130 migration files
├── tests/                # 471 test_*.py files across e2e/, frontend/, scripts/, test_models/, test_services/, ux_mega/, test_scripts/
├── scripts/              # 24 ops utilities (enrich_*, data_cleanup, *_backfill, weekly_cleanup.sh, nightly_tests.sh, …)
├── docs/                 # APP_MAP_* and specs
├── specs/                # UI specs (specs/ui/)
└── (Caddyfile, deploy.sh, docker-compose.yml, docker-compose.local.yml, Dockerfile, .pre-commit-config.yaml, ruff.toml, pyproject.toml, pytest.ini)
```

## 5. Major subsystems

- **Search engine.** `app/search_service.py` orchestrates parallel queries via `asyncio.gather()` across **9 active `BaseConnector` subclasses**: DigiKey, Mouser, OEMSecrets, Element14, Sourcengine, eBay, AI live web (standalone modules), plus Nexar and BrokerBin (both inside `app/connectors/sources.py`). Results dedup by MPN+vendor, scored by 6 weighted factors, persisted as Sightings + MaterialCard upserts. Uses a separate write session — caller's ORM objects are stale after `search_requirement()` returns; call `db.expire(requirement)` before rendering. Two more `.py` files live alongside in `app/connectors/` but aren't search connectors: `apollo.py` (CRM enrichment) and `email_mining.py` (inbox scraping). Total directory: 11 `.py` files (10 modules + `__init__.py`); 9 of them are search connectors.
- **RFQ pipeline.** `app/email_service.py` sends batch RFQs via Microsoft Graph (subject tagged `[AVAIL-{id}]`); `app/jobs/inbox_monitor.py` polls every 30 min; `app/services/response_parser.py` runs replies through Claude; confidence ≥0.8 auto-creates Offers, 0.5–0.8 flags for review.
- **NetComponents (NC) worker.** Browser-driven scraping via Patchright (Chromium baked into the Docker image at `Dockerfile:39-40`). `app/services/nc_worker/` + `app/connectors/`. Throttled by `NC_MAX_DAILY_SEARCHES`.
- **8x8 phone analytics.** Work-call telemetry pulled via 8x8 PBX API. Env vars `EIGHT_BY_EIGHT_*` + `_ENABLED` flag (off by default). Tests under `tests/test_8x8_*`.
- **Email mining.** Inbox scraping for vendor offers, gated by `EMAIL_MINING_ENABLED`. Pipeline lives in `app/services/` + jobs.
- **Enrichment.** AI-driven vendor/customer enrichment. `app/enrichment_service.py` orchestrates; batch utilities in `scripts/enrich_*.py`. The `enrichment-worker` compose service is intentionally disabled.
- **Click-to-call (Azure Communication).** `requirements.txt` pulls `azure-communication-callautomation` + `-identity`.

## 6. Conventions

- **Logging:** use Loguru. `from loguru import logger`. Never `print()`. Structured context auto-injects `request_id`.
- **DB lookups:** `db.get(Model, id)` for PK reads — SQLAlchemy 2.0 style. Never `db.query(Model).get(id)`.
- **Pydantic config:** `model_config = ConfigDict(...)`. Never `class Config:`.
- **Status strings:** import from `app/constants.py`. Period.
- **Schemas:** 25 modules in `app/schemas/`, not a monolith. Error shape is defined in `app/schemas/errors.py:9-13` and emitted by the three `@app.exception_handler` decorators in `app/main.py` (`StarletteHTTPException`, `RequestValidationError`, generic `Exception`). Tests assert `response.json()["error"]`, not `["detail"]`.
- **Response shapes:** error → `{"error": str, "status_code": int, "request_id": str}`; list → `{"items": [...], "total": int, "limit": int, "offset": int}`; HTMX → `HTMLResponse` from Jinja2.
- **Routers stay thin.** HTTP only. Business logic belongs in `app/services/`.
- **MPN handling:** `normalize_mpn()` lives in `app/utils/normalization.py` (search `def normalize_mpn`); `strip_packaging_suffixes()` lives in `app/services/search_worker_base/mpn_normalizer.py:39`. The `@validates` hook on Requirement auto-uppercases identifiers. Use the `|sub_mpns` Jinja filter for substitutes (handles legacy `["str"]` and current `[{"mpn":..., "manufacturer":...}]`).
- **Vendor matching:** `fuzzy_score_vendor()` from `app/vendor_utils.py`. Don't inline rapidfuzz.
- **Caching:** `@cached_endpoint("prefix", ttl_hours=N, key_params=[...])` from `app/cache/decorators.py`.
- **Junk filtering:** `JUNK_DOMAINS` / `JUNK_EMAIL_PREFIXES` from `app/shared_constants.py`. Don't duplicate.
- **Direct DOM HTML writes** (`el.inner` + `HTML` property): prefer reactive Alpine bindings or `htmx.ajax()`. Existing uses (email-parse, paste-offer, parsed-results, trouble-report) only clear DOM; don't add new ones.
- **New file headers:** every new module gets a docstring covering what it does, what calls it, and what it depends on. Convention is universal across `app/routers/` and `app/services/`.
- **`Alpine.store(...)`** is the state pattern (never `_x_dataStack`). The toast store is `$store.toast` with `message`/`type`/`show` properties — `show` is a boolean, never a method call.
- **Mypy is permissive.** `pyproject.toml [tool.mypy]` sets `no_strict_optional = true` and overrides `app.routers.*, app.services.*, app.connectors.*, app.jobs.*, app.schemas.*` plus three named utils (`claude_client, llm_router, vendor_helpers`) and five core modules (`app.enrichment_service, app.email_service, app.search_service, app.scoring, app.startup`) to `ignore_errors = true`. Mypy mostly checks core modules. Don't add `# type: ignore` without a comment, and don't claim "mypy passed" as proof a service-layer change is type-safe.

### Auth & permissions
Azure AD OAuth2 flow lives in `app/routers/auth.py`. Session middleware stores `user_id` in HTTP-only cookies (15-min expiry). Dependencies: `require_user`, `require_buyer`, `require_admin`, `require_fresh_token` (15-min buffer for Graph calls).

## 7. Testing

- **Invocation:** `TESTING=1 pytest tests/...`. PYTHONPATH is not required (CI sets it for paranoia, but pytest finds `tests/conftest.py` regardless).
- **Engine:** in-memory SQLite via `StaticPool` (`tests/conftest.py`, search `TEST_DB_URL = "sqlite://"`). `RATE_LIMIT_ENABLED=false` and `REDIS_URL=""` are set at import time. `TESTING=1` short-circuits the scheduler, real API calls, and live AI search across `app/main.py`, `app/startup.py` (search `TESTING mode — skipping`), `app/search_service.py`, `app/utils/graph_client.py` (retries → 0).
- **Parallelism / timeouts:** `pytest.ini` sets `addopts = -n auto --timeout=30 --timeout-method=thread`, ignores `tests/e2e/` and `tests/test_browser_e2e.py`.
- **Coverage:** CI floor is **50%** (`--cov-fail-under=50` in `.github/workflows/ci.yml`, search `--cov-fail-under`), not 100%. Don't add `--cov` to dev iteration runs — only before PR.
- **Markers:** `slow` and `integration` are registered. `slow` is currently used in only ~6 of 471 files — don't rely on `-m "not slow"` to materially shrink the suite.
- **Single-file run with xdist disabled:** `TESTING=1 pytest tests/test_foo.py -v --override-ini="addopts="`.
- **E2E (pytest, not Playwright TS):** `tests/e2e/test_*.py`. Run with `pytest tests/e2e/ --headed`.
- **Playwright projects** (defined in `playwright.config.ts:28-39`): `api, auth, smoke, data-validation, accessibility, visual, dead-ends, workflows, requisitions2-resize, requisitions2-visuals`. Invoke via `npx playwright test --project=<name>`.
- **Frontend tests:** Node native test runner — `npm run test:frontend` (unit + e2e). Vitest available via `npm run test:vitest`. HTML validation: `npm run test:html`.

## 8. Build, deploy, ops

### Local dev
```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d   # hot-reload + ports 8000/6379
npm run dev                                                              # Vite watch mode
docker compose logs -f app
```

### Migrations
```bash
alembic revision --autogenerate -m "description"   # then REVIEW the generated file
alembic upgrade head
alembic downgrade -1
alembic heads                                       # ensure single head
```

### Deploy
```bash
./deploy.sh                 # commit + push + build --no-cache + recreate + health check
./deploy.sh --no-commit     # rebuild current branch without committing
```
**Pre-deploy checklist:** droplet's local `main` is fast-forward-only with `origin/main` (`git fetch && git checkout main && git merge --ff-only origin/main`). If it isn't, stop and investigate — `deploy.sh` currently swallows non-fast-forward rejection.

**deploy.sh safety nets:** Step 5 round-trips `BUILD_COMMIT` through the running container (`docker compose exec app printenv BUILD_COMMIT`) and exits non-zero if the deployed value doesn't match what was just built — this catches stale-image deploys where Docker thinks it built but the container didn't pick up the new layer. Step 6 scans `/app/app/templates/` for Tailwind utility patterns (`bg|text|border|hover:bg|hover:text-…-N`) and reports any class missing from the compiled CSS bundle; **currently warn-only — a missing class still ships.**

### CI (`.github/workflows/`)
- `ci.yml` — push/PR to main: pytest with `--cov-fail-under=50`, pre-commit, ruff, alembic validation + smoke, npm build, frontend tests.
- `deploy.yml` — release-triggered: SSH to droplet, backup DB+code, download release zip, rebuild, 12×5s health probe with rollback.
- `security.yml` — push/PR + Mondays 08:00 UTC: bandit, pip-audit, npm audit.

### Environment

Required (deploy fails or auth breaks without these):
```
AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID
ANTHROPIC_API_KEY
SESSION_SECRET                # cookie signing + EncryptedType derivation
DATABASE_URL, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB
APP_URL                       # https://app.availai.net
ADMIN_EMAILS
```

Jointly load-bearing: `ENCRYPTION_SALT` is mixed with `SESSION_SECRET` to derive Fernet keys for `EncryptedType` columns (refresh/access tokens, password hashes). Rotating either after data is encrypted produces `Fernet InvalidToken` and silent `None` reads — see `app/utils/encrypted_type.py`.

Optional (feature gates / data sources):
```
NEXAR_CLIENT_ID/SECRET, BROKERBIN_API_KEY/SECRET, EBAY_CLIENT_ID/SECRET,
DIGIKEY_CLIENT_ID/SECRET, MOUSER_API_KEY, OEMSECRETS_API_KEY,
SOURCENGINE_API_KEY, ELEMENT14_API_KEY, EXPLORIUM_API_KEY,
DO_GRADIENT_API_KEY/MODEL,
EIGHT_BY_EIGHT_*, EIGHT_BY_EIGHT_ENABLED,
NC_USERNAME, NC_PASSWORD, NC_MAX_DAILY_SEARCHES, NC_BROWSER_PROFILE_DIR,
EMAIL_MINING_ENABLED, AVAIL_OPP_TABLE_V2,
REDIS_URL, BACKUP_INTERVAL_HOURS, BACKUP_RETENTION_DAYS,
DO_SPACES_KEY/SECRET/BUCKET/REGION, SPACES_RETENTION_DAYS
```

### Docker stack
- `app` — uvicorn FastAPI on `:8000`, runs as non-root user, `tini` PID-1, `docker-entrypoint.sh` runs migrations on boot.
- `db` — postgres:16-alpine.
- `redis` — redis:7-alpine.
- `caddy` — auto-TLS reverse proxy fronting `app.availai.net`. Blocks `/metrics` (returns 403); HSTS/CSP applied via FastAPI middleware (not Caddy).
- `db-backup` — `pg_dump` every `BACKUP_INTERVAL_HOURS` (default 6), retains `BACKUP_RETENTION_DAYS` (default 30). Optional off-site to DO Spaces.
- `enrichment-worker` — disabled (entrypoint is a no-op echo).

## 9. Toolkit — pick the right one at the right time

Use these aggressively; many run in parallel.

### Process skills (drive *how* you work)
| Trigger | Skill |
|---|---|
| Starting any non-trivial task with unclear intent | `superpowers:brainstorming` |
| Have a spec / requirements, need a step-by-step plan | `superpowers:writing-plans` |
| Plan in hand, executing with checkpoints | `superpowers:executing-plans` |
| Plan has independent steps you can dispatch | `superpowers:subagent-driven-development` |
| 2+ truly independent investigations / fixes | `superpowers:dispatching-parallel-agents` |
| Implementing any feature or bugfix | `superpowers:test-driven-development` |
| Bug, test failure, or unexpected behavior | `superpowers:systematic-debugging` |
| About to claim "done" / "fixed" / "passing" | `superpowers:verification-before-completion` |
| Major work complete, want review before merge | `superpowers:requesting-code-review` |
| Receiving review feedback (especially if it seems wrong) | `superpowers:receiving-code-review` |
| Need to isolate work from current workspace | `superpowers:using-git-worktrees` |
| Branch is done — decide merge / PR / cleanup | `superpowers:finishing-a-development-branch` |

### Domain skills (drive *what* you write)
| Working on | Skill |
|---|---|
| SQLAlchemy 2.0 ORM models, queries, relationships, fixtures | `sqlalchemy` |
| Anthropic SDK / Claude API code (caching, tool use, batch, thinking, model migrations) | `claude-api` |
| Distinctive HTMX + Alpine + Tailwind UI | `frontend-design:frontend-design` |

### Aggregate / one-shot toolkits
| Need | Skill |
|---|---|
| Comprehensive multi-agent PR review on the current branch | `pr-review-toolkit:review-pr` |
| Guided feature dev with codebase grounding | `feature-dev:feature-dev` |
| Security review of pending diff | `security-review` |

### Operational skills
| Need | Skill |
|---|---|
| Settings hooks / permissions / env vars | `update-config` |
| Reduce permission prompts in this project | `fewer-permission-prompts` |
| Recommend automations / skills / hooks for this codebase | `claude-code-setup:claude-automation-recommender` |
| Quick commit | `commit-commands:commit` |
| Commit + push + PR | `commit-commands:commit-push-pr` |
| Clean up branches that are gone on remote | `commit-commands:clean_gone` |
| Update CLAUDE.md with session learnings | `claude-md-management:revise-claude-md` |
| Audit & rebuild CLAUDE.md | `claude-md-management:claude-md-improver` |
| Recurring/polled task | `loop` (foreground) or `schedule` (cron-style remote agent) |
| Hook authoring | `hookify:*` (`writing-rules`, `hookify`, `configure`) |
| Tighten recently-changed code | `simplify` |

### Subagents (Agent tool — run in parallel where independent)

| Task | `subagent_type` |
|---|---|
| Locate a file/symbol/keyword | `Explore` (specify quick / medium / very thorough) |
| Plan an implementation strategy | `Plan` |
| Design a feature blueprint with file-by-file build order | `feature-dev:code-architect` |
| Map an unfamiliar feature/subsystem | `feature-dev:code-explorer` |
| Review code for bugs / quality | `feature-dev:code-reviewer` |
| Simplify recently-modified code | `code-simplifier:code-simplifier` |
| Verify accuracy of comments / docstrings | `pr-review-toolkit:comment-analyzer` |
| Find silent failures / inadequate error handling | `pr-review-toolkit:silent-failure-hunter` |
| Audit type design on new types | `pr-review-toolkit:type-design-analyzer` |
| Audit PR test coverage | `pr-review-toolkit:pr-test-analyzer` |
| Review PR adherence to this CLAUDE.md | `pr-review-toolkit:code-reviewer` |
| Open-ended research spanning the codebase | `general-purpose` |
| Claude Code / SDK / API questions | `claude-code-guide` |

### PR review default
Run all pr-review-toolkit specialists (the `pr-review-toolkit:review-pr` skill aggregates them) **plus** `feature-dev:code-reviewer`. Fix every finding immediately; never defer as "lower priority."

## 10. Operational gotchas

- **Tailwind classes added in templates that don't appear in compiled CSS** → check `tailwind.config.js` content globs, the Tailwind safelist, and deploy warnings. Always `--no-cache` deploy when introducing new utility classes.
- **`Fernet InvalidToken` / mysteriously `None` token columns** → `SESSION_SECRET` or `ENCRYPTION_SALT` rotated after data was written. Restore the prior values or re-encrypt.
- **Empty search results** → no supplier API keys set; you can also seed via vendor stock list uploads.
- **`Check Inbox` returns nothing** → reply has to be in the same Graph thread as the RFQ; verify `Mail.Read` is granted and consented in Azure.
- **HTMX-swapped subtree contains Alpine directives that don't bind** → `app/static/htmx_app.js` uses a hardcoded ID allowlist for `htmx:afterSwap` `Alpine.initTree`. Add the new region's id (see `STABLE.md`).

## 11. Project state (current)

- Active branch focus: search session lifecycle, test-coverage hardening, requisitions2 polish.
- Last 30 days: ~110 commits — heavy infrastructure and stability work, no major feature pushes.
- DB on the droplet is intentionally fresh; SFDC import has no scheduled date and is gated on rollout readiness — don't sequence work around it.
- Migration 001 rewrite to explicit DDL snapshot (no `Base.metadata.create_all`) is the current top-priority infra item, blocking five open PRs.

For deeper project context, recent decisions, and roadmap snapshots, check the dev's auto-memory and the open PRs — this section will go stale fast.

---

See also: `docs/APP_MAP_*.md` (architecture), `STABLE.md` (frozen files), `README.md` (operator setup), `LOCAL_SETUP.md` (local dev), `DATA_SOURCES.md` (sourcing reference).
