# AvailAI — Full Code Review Notes (Re-verified)

**Original review:** 2026-05-04 (branch `claude/code-review-notes-f6Cg7`, base commit `fbba111`)
**Re-verified against `main`:** 2026-05-21 at `0de51dc1` (`Merge pull request #116 from TRIOSCS/fix/npm-audit-overrides`)
**Scope:** Whole codebase — security, backend, database/migrations, tests, frontend, devops/infra
**Re-verification method:** All 11 Critical and all 35 High findings (46 total) were individually re-checked against current `main`. The 55 Medium and 38 Low/Info findings received a lighter spot-check pass; unless explicitly noted, they are carried over from the 2026-05-04 review and were not individually re-verified.

---

## Executive Summary

### Original counts (2026-05-04)

| Area | Findings | Critical | High | Medium | Low/Info |
|------|----------|----------|------|--------|----------|
| Security | 23 | 2 | 4 | 8 | 9 |
| Backend code quality | 36 | 0 | 11 | 14 | 11 |
| Database & migrations | 25 | 0 | 3 | 14 | 8 |
| Tests & coverage | 15 | 2 | 5 | 6 | 2 |
| Frontend & templates | 18 | 2 | 5 | 7 | 4 |
| DevOps & infra | 22 | 5 | 7 | 6 | 4 |
| **Total** | **139** | **11** | **35** | **55** | **38** |

### Re-verification status of the 46 Critical + High findings (as of 2026-05-21)

| Status | Critical | High | Total |
|--------|----------|------|-------|
| ✅ FIXED | 2 | 4 | **6** |
| ◐ PARTIAL | 2 | 2 | **4** |
| ⚠️ STILL OPEN | 7 | 29 | **36** |
| **Total** | **11** | **35** | **46** |

Net movement since the original review is small: the large sourcing-engine repair + CI-unblock cascade (PRs #102, #107, #108, #109, #115, #116, backlog #92–#101) closed the two test/CI infrastructure blockers and meaningfully advanced a handful of others, but the bulk of the security, schema, frontend, and DevOps hardening work remains untouched. The 93 Medium/Low/Info findings are carried over essentially unchanged.

**2026-05-27 update:** four High-tier findings closed in the cleanup cascade. PR #149 (`fix/high-httpx-webhook`) closed HIGH-BE-3 (blocking httpx → AsyncClient + await). PR #150 (`fix/high-test-quality`) closed HIGH-TEST-1 and HIGH-TEST-4 (pragma-no-cover removal on `tagging_ai_classify.py` and `search_service.py` tag-propagation). The final `asyncio.run()` callsite in `htmx_views.py:1144` (HIGH-BE-4's last remaining site) is also gone, moving HIGH-BE-4 from PARTIAL → FIXED for the originally-tracked scope. Other pre-existing `asyncio.run()` callsites in `description_service.py` and `contact_intelligence.py` were not part of HIGH-BE-4's scope.

### Top still-open issues to fix first

1. **CRIT-SEC-1** — Agent service account is *still* seeded as `UserRole.ADMIN` (`app/startup.py:168`) and `require_buyer` (`app/dependencies.py:96`) *still* only checks `user.role` with no explicit `agent@availai.local` block. A leaked `AGENT_API_KEY` is an admin-level user.
2. **CRIT-DEVOPS-1** — `docker-entrypoint.sh:31` *still* swallows failed Alembic migrations with a `WARNING` and starts the app against a possibly un-migrated schema.
3. **CRIT-DEVOPS-3** — `Dockerfile:67` and `docker-compose.local.yml:15` *still* use `--forwarded-allow-ips "*"`, allowing `X-Forwarded-For` spoofing.
4. **CRIT-DEVOPS-4** — `.github/workflows/deploy.yml` *still* deploys on `release: published` with no CI-pass gate.
5. **CRIT-FE-1** — `app/templates/requisitions2/page.html` *still* has no `htmx:configRequest` CSRF listener; `requisitions2.js` does not register one and the page never loads `htmx_app.js`.

---

## Re-verification results — Critical & High findings

Legend: ✅ FIXED · ◐ PARTIAL · ⚠️ STILL OPEN

### Critical (11)

| ID | Status | Note |
|----|--------|------|
| CRIT-SEC-1 | ⚠️ STILL OPEN | Agent still `UserRole.ADMIN` at `startup.py:168`; `require_buyer` (`dependencies.py:96`) has no agent block. |
| CRIT-SEC-2 | ◐ PARTIAL | A `logger.critical` warning was added (`startup.py:38–42`), but `ENABLE_PASSWORD_LOGIN` is still absent from `.env.example` and the seeded role still defaults to `admin` (`startup.py:89`). |
| CRIT-DEVOPS-1 | ⚠️ STILL OPEN | `docker-entrypoint.sh:31` still uses the `if ! … then echo WARNING` guard. |
| CRIT-DEVOPS-2 | ◐ PARTIAL | `deploy.sh` was hardened (must run from `main`, ff-only sync, push verification) but `git add -A` still remains at `deploy.sh:39`. |
| CRIT-DEVOPS-3 | ⚠️ STILL OPEN | `--forwarded-allow-ips "*"` still in `Dockerfile:67` and `docker-compose.local.yml:15`. |
| CRIT-DEVOPS-4 | ⚠️ STILL OPEN | `deploy.yml` still triggers solely on `release: published`; no `workflow_run` / CI gate. |
| CRIT-DEVOPS-5 | ✅ FIXED | A new non-`\|\| true` "Bandit summary" step (`security.yml:43–44`) runs `bandit -r app/ -c pyproject.toml -ll`, which exits non-zero on medium+ findings and blocks the workflow. |
| CRIT-FE-1 | ⚠️ STILL OPEN | No CSRF listener in `requisitions2/page.html` or `requisitions2.js`; page never loads `htmx_app.js`. |
| CRIT-FE-2 | ⚠️ STILL OPEN | `requisitions2.js:16` still registers `Alpine.data('splitPanel', …)`, loaded raw alongside the conditional Vite bundle. |
| CRIT-TEST-1 | ✅ FIXED | `pytest-xdist>=3.0.0` added to `requirements-dev.txt:8` (via the #109 cascade). |
| CRIT-TEST-2 | ⚠️ STILL OPEN | `tests/test_database_coverage.py:196–197` still calls `sqlalchemy.create_engine(...)` directly with hardcoded kwargs; the `app/database.py` PostgreSQL branch (lines ~37–50) remains untested. |

### High (35)

| ID | Status | Note |
|----|--------|------|
| HIGH-SEC-1 | ⚠️ STILL OPEN | `{{ title_attr\|safe }}` still at `_macros.html:158`. |
| HIGH-SEC-2 | ⚠️ STILL OPEN | `thread_viewer.html:59` now uses `\|sanitize_html\|safe`, but `class` is still in the wildcard allowlist (`template_env.py:140`); `nh3` still `>=` pinned. |
| HIGH-SEC-3 | ⚠️ STILL OPEN | Unescaped ILIKE wildcards still at `htmx_views.py:3330, 6924` (`f"%{q.strip()}%"`). |
| HIGH-SEC-4 | ⚠️ STILL OPEN | Graph webhook validation echo — carried over from 2026-05-04 review, not individually re-verified; conservatively open. |
| HIGH-BE-1 | ⚠️ STILL OPEN | `htmx_views.py` is 9,918 lines (was 10,024) — not split. |
| HIGH-BE-2 | ⚠️ STILL OPEN | God files persist (`htmx_views.py` 9918, `search_service.py` 2348, `email_service.py` 1277). |
| HIGH-BE-3 | ✅ FIXED | PR #149 (merged 2026-05-27). `eight_by_eight_service.py` `get_access_token`/`get_extension_map`/`get_cdrs` are now `async def` using `httpx.AsyncClient` + `await`; no blocking calls remain. |
| HIGH-BE-4 | ✅ FIXED | The `htmx_views.py:1144` site cleared during 2026-05-27 cleanup; zero `asyncio.run()` remain in `htmx_views.py` and `requirements.py` (the originally-tracked scope). |
| HIGH-BE-5 | ⚠️ STILL OPEN | Business logic in routers — carried over, not individually re-verified. |
| HIGH-BE-6 | ◐ PARTIAL | `RequisitionStatus.WON` now exists (used in `companies.py:102,104,317`), so the comparison is no longer dead code, but the raw-string `Requisition.status == "won"` at `companies.py:137` is still a StrEnum violation. |
| HIGH-BE-7 | ⚠️ STILL OPEN | Inline rapidfuzz — carried over, not individually re-verified. |
| HIGH-BE-8 | ⚠️ STILL OPEN | `sourcing_leads.py:59` still defines its own `normalize_mpn`, colliding with `utils/normalization.py`. |
| HIGH-BE-9 | ⚠️ STILL OPEN | Module-level mutable caches — carried over, not individually re-verified. |
| HIGH-BE-10 | ⚠️ STILL OPEN | Duplicate cache machinery in `routers/sightings.py` — carried over, not individually re-verified. |
| HIGH-BE-11 | ⚠️ STILL OPEN | ~1,166 `db.query(...)` callsites (was 1,163) — unchanged. |
| HIGH-DB-1 | ⚠️ STILL OPEN | `Requirement.material_card` still `lazy="joined"` (`models/sourcing.py:127`). |
| HIGH-DB-2 | ⚠️ STILL OPEN | `UTCDateTime` adopted in only 2 model files; pervasive `Column(DateTime)` remains. Not confirmed fixed → conservatively open. |
| HIGH-DB-3 | ◐ PARTIAL | An `INSERT … ON CONFLICT DO NOTHING` path was added (`search_service.py:1704+`), but the one-by-one `Sighting(...)` insert still exists at `search_service.py:1358`. |
| HIGH-TEST-1 | ✅ FIXED | PR #150 (merged 2026-05-27). `# pragma: no cover` removed from `classify_parts_with_ai` and `_apply_ai_results` in `tagging_ai_classify.py`. |
| HIGH-TEST-2 | ⚠️ STILL OPEN | `workflows.spec.ts:16,89` still accept `401/307` as passing; E2E still unauthenticated. |
| HIGH-TEST-3 | ⚠️ STILL OPEN | Single-status-code assertion pattern — carried over, not individually re-verified. |
| HIGH-TEST-4 | ✅ FIXED | PR #150 (merged 2026-05-27). Tag-propagation `# pragma: no cover` removed at all three previously-flagged callsites in `search_service.py`. |
| HIGH-TEST-5 | ⚠️ STILL OPEN | Skipped tests still present: `test_req_offer_fields.py:64,118`, `test_tt105_user_validation.py:14`. |
| HIGH-FE-1 | ⚠️ STILL OPEN | Raw `fetch()` still in `performance_tab.html:31`, `offers.html:62`, `trouble_report_form.html:55`. |
| HIGH-FE-2 | ⚠️ STILL OPEN | `innerHTML = html` still at `trouble_report_form.html:64`. |
| HIGH-FE-3 | ⚠️ STILL OPEN | `requisitions2/page.html:48–56` loads 8 CDN scripts; only the first (`htmx.org@2.0.4`) has `integrity`. |
| HIGH-FE-4 | ⚠️ STILL OPEN | `base.html:65` has `role="dialog" aria-modal="true"` but no `aria-labelledby`. |
| HIGH-FE-5 | ⚠️ STILL OPEN | `login.html:57,60` inputs still use `placeholder`, no `<label>`. |
| HIGH-FE-6 | ⚠️ STILL OPEN | `performance_tab.html` still uses inline `fetch()` + global function pattern — carried over, not individually re-verified beyond the `fetch()` site. |
| HIGH-DEVOPS-1 | ⚠️ STILL OPEN | `scripts/backup.sh` / `backup-to-spaces.sh` show no `--sse AES256` / `gpg` encryption. |
| HIGH-DEVOPS-2 | ⚠️ STILL OPEN | Base images still tag-pinned not digest-pinned (`Dockerfile:2,12` — `node:20-alpine`, `python:3.12-slim`). |
| HIGH-DEVOPS-3 | ⚠️ STILL OPEN | `METRICS_TOKEN` still absent from `.env.example`. |
| HIGH-DEVOPS-4 | ⚠️ STILL OPEN | `requirements.txt:25` still `apscheduler==3.11.2,<4.0` (invalid mixed `==`/`<` syntax). |
| HIGH-DEVOPS-5 | ⚠️ STILL OPEN | Loose lower-bound pins remain: `rapidfuzz>=3.0.0`, `orjson>=3.9.0`, `sse-starlette>=1.6.0`, `anthropic>=0.40.0`, `azure-communication-*>=…`. |
| HIGH-DEVOPS-6 | ⚠️ STILL OPEN | `.github/dependabot.yml` still covers only `pip` + `github-actions`; no `npm` or `docker` ecosystems. |
| HIGH-DEVOPS-7 | ⚠️ STILL OPEN | `ci.yml:120` still runs full `alembic downgrade base` on every PR (now with `ALEMBIC_ALLOW_CASCADE`, but still the slow/fragile `downgrade base` path). |

---

## Resolved since original review

The following Critical/High findings are confirmed FIXED on `main` at `0de51dc1`:

- **CRIT-TEST-1** — `pytest-xdist>=3.0.0` is now declared in `requirements-dev.txt:8`. A clean dev install can run the parallel test suite. (Delivered via the #109 CI-unblock cascade.)
- **CRIT-DEVOPS-5** — Bandit now blocks CI. `.github/workflows/security.yml` keeps the JSON-report step non-blocking (`|| true`) but adds a separate `Bandit summary` step (`bandit … -ll`, `if: always()`, no `|| true`) that exits non-zero on medium+ findings and fails the workflow.

Partial progress also landed on **CRIT-SEC-2** (startup `logger.critical` warning), **CRIT-DEVOPS-2** (deploy.sh branch/sync hardening), **HIGH-BE-4** (asyncio.run callsites reduced 5→1), **HIGH-BE-6** (`RequisitionStatus.WON` enum value added), and **HIGH-DB-3** (`ON CONFLICT DO NOTHING` insert path added) — these remain ◐ PARTIAL and keep their full finding bodies below.

---

## Still-open & partial finding details

The detailed bodies below are retained verbatim from the 2026-05-04 review for every finding that is STILL OPEN or PARTIAL. FIXED items (CRIT-TEST-1, CRIT-DEVOPS-5) are compressed into the "Resolved since original review" section above. Finding IDs are preserved for traceability.

## 1. Security (23 findings)

### Critical

- **CRIT-SEC-1 — Agent service account has ADMIN role, allowing privilege escalation via `x-agent-key` header.** ⚠️ STILL OPEN
  `app/startup.py:168`, `app/dependencies.py:54–58` — Agent user seeded with `role=UserRole.ADMIN`. `require_admin` and `require_settings_access` block the agent email explicitly, but `require_buyer` only checks `user.role`, so it lets the agent through. A leaked `AGENT_API_KEY` becomes an admin-level user.
  *Re-verify 2026-05-21:* unchanged — `startup.py:168` still `UserRole.ADMIN`; `require_buyer` at `dependencies.py:96` still has no `agent@availai.local` block.
  *Fix:* seed agent as `UserRole.BUYER` (or new `AGENT`); mirror the explicit `agent@availai.local` block in `require_buyer`.

- **CRIT-SEC-2 — `ENABLE_PASSWORD_LOGIN=true` is an undocumented persistent auth bypass.** ◐ PARTIAL
  `app/routers/auth.py:203–233`, `app/startup.py:65–66` — Flag is not in `.env.example`. The seeded user defaults to `admin`. Login form is served without auth.
  *Re-verify 2026-05-21:* a `logger.critical` warning was added at `startup.py:38–42` when the flag is active outside test mode. Still NOT in `.env.example`; seeded role still defaults to `admin` (`startup.py:89`).
  *Fix:* add `ENABLE_PASSWORD_LOGIN=false` to `.env.example` with a banner; default seeded role to `buyer`; gate behind a Compose profile or a startup assertion in production.

### High

- **HIGH-SEC-1 — `{{ title_attr|safe }}` builds an HTML attribute from data values.** ⚠️ STILL OPEN
  `app/templates/htmx/partials/shared/_macros.html:150,158` — Today the values are server-side ints, but the pattern is structurally unsafe.
  *Fix:* drop `|safe`; let Jinja auto-escape attribute values.

- **HIGH-SEC-2 — Vendor email HTML rendered through sanitizer then `|safe`.** ⚠️ STILL OPEN
  `app/templates/htmx/partials/emails/thread_viewer.html:59` (now `|sanitize_html|safe`), allowlist in `app/template_env.py:107–147` — `class` is allowed on every element (`template_env.py:140`); URL schemes need to be re-verified per `nh3` version. Vendor email is fully attacker-controlled.
  *Fix:* remove `class` from the wildcard allowlist; pin `nh3`; consider sandboxed `<iframe srcdoc>` for email bodies.

- **HIGH-SEC-3 — Unescaped wildcards in ILIKE patterns.** ⚠️ STILL OPEN
  `app/routers/htmx_views.py:3330, 6924` — `term = f"%{q.strip()}%"` for brand/commodity/manufacturer search. Not SQLi (parameterized) but `%`/`_` from the user forces full-table scans.
  *Fix:* call `escape_like()` and pass `escape='!'` to `ilike()`, or use `SearchBuilder.safe`.

- **HIGH-SEC-4 — Graph webhook `validationToken` echo is unauthenticated.** ⚠️ STILL OPEN (not individually re-verified)
  `app/routers/v13_features/activity.py:49–51, 88–90` — Required by Graph protocol; mitigate at edge.
  *Fix:* IP-allowlist Microsoft ranges in Caddy; alert on unexpected validation events.

### Medium

- **MED-SEC-1** — `/auth/status` (`app/routers/auth.py:261–302`) has no auth dependency and returns user PII / M365 connection state. Add `Depends(require_user)`. *(Spot-checked 2026-05-21: still no `Depends(require_user)`; the handler does an internal `get_user` check and returns minimal info when unauthenticated, but still leaks PII to any logged-in session without an explicit guard.)*
- **MED-SEC-2** — Session cookie `httponly` not set explicitly (`app/main.py:246–252`). Don't rely on Starlette default. Also `same_site="lax"` does not protect GET-based logout (see MED-SEC-7).
- **MED-SEC-3** — Session `max_age=86400` (24h), no idle timeout. Reduce to ~8h with sliding `last_seen`.
- **MED-SEC-4** — Hardcoded customer-specific defaults in `app/config.py:164,165,194` (`stock_sale_vendor_names`, `stock_sale_notify_emails`, `own_domains`). Replace with placeholders, document required env vars.
- **MED-SEC-5** — `.env.example:58` still ships with `ADMIN_EMAILS=mkhoury@trioscs.com`. Replace with `admin@yourcompany.com`. *(Spot-checked 2026-05-21: still open.)*
- **MED-SEC-6** — CSP allows `unsafe-inline` + `unsafe-eval` and several CDN origins (`app/main.py:328–329`). XSS protection is effectively zero. CDN scripts in `requisitions2/page.html` and `login.html` mostly lack SRI hashes.
- **MED-SEC-7** — `GET /auth/logout` is registered (`app/routers/auth.py:172`) — CSRF-logout via `<img src=…>`. Make logout POST-only. *(Spot-checked 2026-05-21: still open — both `@router.post` and `@router.get` decorators present.)*
- **MED-SEC-8** — Public `/docs` and `/redoc` (`app/main.py:162–168`). Set `docs_url=None, redoc_url=None` in production or gate behind admin.

### Low / Info

*(Carried over from 2026-05-04 review, not individually re-verified.)*

- **LOW-SEC-1** — Verify `/v2/partials/customers/lookup` is truly read-only before keeping its CSRF exemption (`app/main.py:274`).
- **LOW-SEC-2** — Graph error text leaks via `resp.text[:300]` returned to caller (`app/utils/graph_client.py:240`).
- **LOW-SEC-3** — `_build_html_body()` in `app/email_service.py:32–35` does `\n → <br>` without HTML-escaping the plain text first. Outbound HTML-injection vector.
- **LOW-SEC-4** — ACS webhook reflects `validationCode` without IP restriction (`app/routers/v13_features/activity.py:138–140`).
- **LOW-SEC-5** — Agent user is re-seeded on every boot regardless of operator intent (`app/startup.py:73, 154–177`).
- **INFO-SEC-1** — HSTS missing `preload` directive (`app/main.py:368–369`).
- **INFO-SEC-2** — `/metrics` 403 response includes `request_id` — minor probe-correlation aid.
- **INFO-SEC-3** — `ENCRYPTION_SALT` falls back to a static legacy salt when unset (`app/config.py:47`; `.env.example:55` ships empty). Require non-empty in production.
- **INFO-SEC-4** — `SameSite=Lax` does not protect GET-based actions (relates to MED-SEC-7).

---

## 2. Backend code quality (36 findings)

### High

- **HIGH-BE-1 — God file `app/routers/htmx_views.py` is ~9,918 lines** (was 10,024) with 244 functions, 249 routes, 377 direct DB ops. Split into ~8–12 domain routers. ⚠️ STILL OPEN
- **HIGH-BE-2 — Top god files (>700 lines):** `htmx_views.py` (9918), `search_service.py` (2348), `routers/requisitions/requirements.py`, `routers/sightings.py`, `email_service.py` (1277), `services/knowledge_service.py`, `services/excess_service.py`, `routers/crm/offers.py`, `startup.py`, `jobs/email_jobs.py`. `search_service.py` and `email_service.py` should become packages. ⚠️ STILL OPEN
- **HIGH-BE-3 — Blocking sync `httpx.get/post` inside async APScheduler job.** ✅ FIXED (PR #149, merged 2026-05-27)
  `get_access_token` / `get_extension_map` / `get_cdrs` in `app/services/eight_by_eight_service.py` are now `async def` using `httpx.AsyncClient` + `await`; the APScheduler job no longer blocks the event loop.
- **HIGH-BE-4 — `asyncio.run()` inside FastAPI `BackgroundTasks` closures.** ✅ FIXED (2026-05-27 cleanup cascade)
  Originally `app/routers/htmx_views.py:743, 1069, 1173, 1224, 2950`; `app/routers/requisitions/requirements.py:493`. Re-verified 2026-05-27: zero `asyncio.run()` callsites remain in `htmx_views.py` or `requirements.py`. Other pre-existing callsites in `description_service.py` and `contact_intelligence.py` are outside this finding's original scope.
- **HIGH-BE-5 — Business logic in routers.** `htmx_views.py` re-fetches/recomputes presentation fields and embeds cron-style background loops. Belongs in a service layer DTO. ⚠️ STILL OPEN (not individually re-verified)
- **HIGH-BE-6 — Raw status string comparisons** (StrEnum violations). ◐ PARTIAL
  `app/routers/crm/companies.py:137` checks `Requisition.status == "won"`.
  *Re-verify 2026-05-21:* `RequisitionStatus.WON` now exists and is used correctly elsewhere in `companies.py` (lines 102, 104, 317), so the `:137` comparison is no longer silently-always-False dead code — but the raw string literal is still a StrEnum violation and should use the enum.
- **HIGH-BE-7 — Inline rapidfuzz** bypassing `fuzzy_score_vendor()` in `app/services/auto_dedup_service.py:68,97` and `app/routers/vendors_crud.py:60,69`. ⚠️ STILL OPEN (not individually re-verified)
- **HIGH-BE-8 — Duplicated MPN normalizer** at `app/services/sourcing_leads.py:59` collides with the canonical `app/utils/normalization.py`. Different semantics → dedup mismatches. ⚠️ STILL OPEN
- **HIGH-BE-9 — Module-level mutable caches with no eviction** in `services/webhook_service.py`, `services/email_threads.py`, `services/admin_service.py`, `services/credential_service.py`, `services/presence_service.py`, `services/ai_part_normalizer.py`, `routers/sightings.py`, `services/search_worker_base/monitoring.py`. ⚠️ STILL OPEN (not individually re-verified)
- **HIGH-BE-10 — Duplicate cache machinery.** `routers/sightings.py` rolls its own TTL cache; `app/cache/decorators.py` and `app/cache/intel_cache.py` already exist. ⚠️ STILL OPEN (not individually re-verified)
- **HIGH-BE-11 — ~1,166 `db.query(...)` call sites** (was 1,163) still using SQLAlchemy 1.x style. Many `db.query(X).filter_by(id=…).first()` should be `db.get(X, id)`. ⚠️ STILL OPEN

### Medium

*(Carried over from 2026-05-04 review, not individually re-verified.)*

- **MED-BE-1** — `db.query(...).get(id)` is currently absent (passed). Don't regress.
- **MED-BE-2** — 18 router `HTTPException(detail=…)` raises rely on the global handler to rename `detail` → `error`. Document explicitly or wrap in a helper.
- **MED-BE-3** — N+1 risk: list endpoints iterate `req.requirements`, `req.offers` without `selectinload`.
- **MED-BE-4** — Magic numbers (fuzzy thresholds, batch sizes, limits, timeouts, lookback windows, retry delays). Promote to `app/config.py`.
- **MED-BE-5** — Mutation routes commit without `try/except`/`db.rollback()`. Audit `app/database.py:get_db`.
- **MED-BE-6** — Commit-then-best-effort patterns in `email_service.py`. Side effects can fail silently after the main commit.
- **MED-BE-7** — Silent exception swallowing in `routers/htmx_views.py`, `services/ics_worker/search_engine.py`, `services/tagging_ai_batch.py`, `services/tagging_ai_triage.py`.
- **MED-BE-8** — `time.sleep(...)` (8 calls) in `app/services/nc_worker/worker.py`. Confirm thread/process isolation.
- **MED-BE-9** — Deprecated `asyncio.get_event_loop()` pattern in `email_service.py`. Use `asyncio.get_running_loop()`.
- **MED-BE-10** — Pydantic v2 `model_config = ConfigDict()` style is consistently followed (passed). Don't regress.
- **MED-BE-11** — `os.environ.get(...)` reads outside `app/config.py` in `services/ics_worker/config.py` and `nc_worker/config.py`.
- **MED-BE-12** — `_is_htmx` helper duplicated across routers. Move to `app/dependencies.py`.
- **MED-BE-13** — `app/main.py:35–49` has two `if not os.environ.get("TESTING"):` blocks. Coalesce.
- **MED-BE-14** — `app/main.py:139–141` references `_is_testing` after `yield`; `UnboundLocalError` risk on shutdown.

### Low

*(Carried over from 2026-05-04 review, not individually re-verified.)*

- **LOW-BE-1** — Mixed log format styles (f-strings vs `{}` placeholders).
- **LOW-BE-2** — Re-imports inside hot loops (`email_service.py`, `htmx_views.py`). Hoist to module top.
- **LOW-BE-3** — `app/main.py:49` uses `%s` placeholder with Loguru — verify formatting.
- **LOW-BE-4** — Dozens of local imports inside route functions in `htmx_views.py`.
- **LOW-BE-5** — Manual JS-template-literal escaping in `htmx_views.py`. Use `json.dumps()`.
- **LOW-BE-6** — Fire-and-forget `loop.create_task(...)` in `email_service.py` without a tracking set.
- **LOW-BE-7** — Header comments missing on `services/sourcing_leads.py`, `services/auto_dedup_service.py`.
- **LOW-BE-8** — Dead code: `app/scheduler.py:50–56` re-exports from `token_manager`.
- **LOW-BE-9** — Underscore-prefixed `_connector_status` at `main.py:121` escapes via `app.state`.
- **LOW-BE-10** — No `print()` calls in production paths (passed).
- **LOW-BE-11** — No bare `except:` clauses (passed).

---

## 3. Database & migrations (25 findings)

### High

- **HIGH-DB-1 — `Requirement.material_card` uses `lazy="joined"`** (`app/models/sourcing.py:127`). Forces a JOIN on every bulk requirement load. ⚠️ STILL OPEN
  *Fix:* drop `lazy="joined"`; use `selectinload(Requirement.material_card)` only on pages that need the card.
- **HIGH-DB-2 — Pervasive `Column(DateTime)` instead of `UTCDateTime`** across the models. ⚠️ STILL OPEN
  *Re-verify 2026-05-21:* `UTCDateTime` is adopted in only 2 model files (`sourcing.py`, `crm.py`); the pervasive `Column(DateTime)` pattern remains across the rest. Not confirmed fixed → conservatively open.
  *Fix:* mandate `UTCDateTime` in code review; migrate critical `created_at`/`updated_at` columns to `TIMESTAMPTZ`.
- **HIGH-DB-3 — `search_service.py` inserts `Sighting` rows one-by-one.** ◐ PARTIAL
  *Re-verify 2026-05-21:* an `INSERT … ON CONFLICT DO NOTHING` path now exists (`search_service.py:1704+`), but the one-by-one `Sighting(...)` construction still exists at `search_service.py:1358`.
  *Fix:* route the scoring path through the bulk/`ON CONFLICT` insert as well.

### Medium

*(Carried over from 2026-05-04 review, not individually re-verified. Note MED-DB-7 was flagged for promotion to High in the original review.)*

- **MED-DB-1** — Orphaned `buy_plans` (V1) table has no SQLAlchemy mapping. Add an explicit DROP migration.
- **MED-DB-2** — `Requisition.@validates("status")` and `Offer._validate_status` only log warnings instead of raising. Inconsistent with `TroubleTicket`/`BuyPlan` validators.
- **MED-DB-3** — `activity_log.quote_id` FK has no index. Quote-detail timelines full-scan.
- **MED-DB-4** — `material_cards.deleted_at` indexed as full B-tree. Use a partial `WHERE deleted_at IS NULL` index.
- **MED-DB-5** — String status columns missing CHECK constraints (`activity_log`, `enrichment_run`, `contact`, `excess_lists`, `excess_line_items`, `site_contacts`).
- **MED-DB-6** — `ExcessList.owner_id`, `Bid.sent_by`, `Bid.created_by` use `ondelete="RESTRICT"` — inconsistent with rest of codebase.
- **MED-DB-7 (HIGH severity in source) — Deleting a `Company` cascades to `CustomerPartHistory`** (`models/purchase_history.py:37`, `ondelete="CASCADE"`). Erases purchase history that drives proactive matching. *Promote to High.* Change to SET NULL or guard with soft-delete.
- **MED-DB-8** — `ProactiveDoNotOffer` cascades on `company_id`. Re-creating a company silently loses suppressions.
- **MED-DB-9** — `MaterialCard.deleted_at` not filtered in upsert paths. Soft-deleted cards can be resurrected.
- **MED-DB-10** — `ProspectContact.confidence` is `String(10)`; other `confidence` columns are `Float`.
- **MED-DB-11** — `Quote.line_items`, `ProactiveOffer.line_items`, `BuyPlan.ai_flags` are `JSON`, not `JSONB`.
- **MED-DB-12** — `PendingBatch.batch_id` is `Column(String)` with no length. Cap at `String(100)`.
- **MED-DB-13** — `Requisition` has 5 collection relationships defaulting to `lazy="select"`. Force `selectinload` on list endpoints.
- **MED-DB-14** — `startup._backfill_proactive_offer_qty` loads ALL `proactive_offers` rows on every boot.

### Low

*(Carried over from 2026-05-04 review, not individually re-verified.)*

- **LOW-DB-1** — Several recent migrations use slug IDs instead of hex prefixes. Standardize.
- **LOW-DB-2** — `Requirement.assigned_buyer_id` no index.
- **LOW-DB-3** — `BuyPlanLine.requirement_id` is nullable but semantically required.
- **LOW-DB-4** — `_backfill_material_cards` does not re-link to soft-deleted cards (correct, but undocumented).
- **LOW-DB-5** — `AvailScoreSnapshot.bonus_amount`, `MultiplierScoreSnapshot.bonus_amount` use `Float` for money.
- **LOW-DB-6** — `VendorSightingsSummary.avg_price`, `best_price` are `Float`.
- **LOW-DB-7** — `startup._backfill_material_cards` and `_backfill_ticket_defaults` ORM-load all matching rows without `LIMIT`.
- **LOW-DB-8** — `startup.py:187` log message says "DDL failed" for what is mostly DML.

---

## 4. Tests & coverage (15 findings)

### Critical

- **CRIT-TEST-1 — `pytest-xdist` missing from `requirements-dev.txt`.** ✅ FIXED — now `pytest-xdist>=3.0.0` at `requirements-dev.txt:8` (#109 cascade). See "Resolved since original review".

- **CRIT-TEST-2 — `app/database.py` PostgreSQL branch (lines ~37–50) is still effectively untested.** ⚠️ STILL OPEN
  The post-`fbba111` tests in `tests/test_database_coverage.py` call `sqlalchemy.create_engine(...)` directly with hardcoded kwargs (`:196–197`) and assert on those kwargs — they do not import the production module path with a postgres URL.
  *Re-verify 2026-05-21:* unchanged — the test still constructs an engine directly rather than exercising `app/database.py`'s production branch.
  *Fix:* patch `app.config.settings.database_url` to a postgres URL inside a try/finally and re-import `app.database`, or assert against the live module's engine kwargs.

### High

- **HIGH-TEST-1 — Two reachable functions excluded with `# pragma: no cover`** in `app/services/tagging_ai_classify.py:44, 87` (`classify_parts_with_ai`, `_apply_ai_results`). ✅ FIXED (PR #150, merged 2026-05-27)
  *Fix:* remove the pragmas.
- **HIGH-TEST-2 — E2E `workflows.spec.ts` and `dead-ends.spec.ts` run unauthenticated** and accept `401`/`307` as passing (`workflows.spec.ts:16,89`). The whole login → search → RFQ → offer flow has no E2E coverage. ⚠️ STILL OPEN
  *Fix:* inject signed Starlette session cookies via `storageState` in `playwright.config.ts`.
- **HIGH-TEST-3 — 2,392 tests assert only a single status code** with no body / DB-state assertion. ⚠️ STILL OPEN (not individually re-verified)
- **HIGH-TEST-4 — Tag-propagation loop excluded from coverage** (`app/search_service.py` `# pragma: no cover`, lines now ~1468, 1898, 1908). ✅ FIXED (PR #150, merged 2026-05-27)
- **HIGH-TEST-5 — Skipped tests for routes/schemas removed long ago.** `tests/test_tt105_user_validation.py:14`, `tests/test_req_offer_fields.py:64,118`. ⚠️ STILL OPEN — Delete or restore.

### Medium

*(Carried over from 2026-05-04 review, not individually re-verified.)*

- **MED-TEST-1** — `event_loop` fixture in `tests/conftest.py` is deprecated under `pytest-asyncio>=0.23`.
- **MED-TEST-2** — Module-level `os.environ["ANTHROPIC_API_KEY"] = "test-key"` in `tests/test_email_parser.py` and `tests/test_part_normalizer.py`.
- **MED-TEST-3** — Real `time.sleep(0.15)` in `tests/test_circuit_breaker.py`.
- **MED-TEST-4** — 29 `tests/test_htmx_views_nightly*.py` files (1,157 test functions). Consolidate.
- **MED-TEST-5** — Add a guard against any non-conftest test calling `drop_all`/`create_all` on the shared engine.
- **MED-TEST-6** — `e2e/accessibility.spec.ts` only audits the unauthenticated login page.

### Low

*(Carried over from 2026-05-04 review, not individually re-verified.)*

- **LOW-TEST-1** — `# pragma: no cover` on reachable duplicate-detection branches in `app/routers/crm/companies.py:443,445`.
- **LOW-TEST-2** — `tests/test_contact_quality.py:181,209` calls `pytest.skip(...)` mid-test.

### "100% coverage" claim caveats

- `branch = false` in `.coveragerc` — only line coverage measured.
- `pragma: no cover` is used on reachable hot-path code in `search_service.py`, `tagging_ai_classify.py`, `crm/companies.py`.
- `app/routers/_lookup_helpers.py` has no dedicated test file.

---

## 5. Frontend & templates (18 findings)

### Critical

- **CRIT-FE-1 — `requisitions2/page.html` has no CSRF listener.** ⚠️ STILL OPEN
  Loads HTMX from CDN and `requisitions2.js` from disk but does not register the `htmx:configRequest` listener that `htmx_app.js:161–165` does, and never loads `htmx_app.js`. Every `hx-post/patch/delete` from that page will lack the `x-csrftoken` header.
- **CRIT-FE-2 — Duplicate Alpine component `splitPanel` registered out-of-bundle** in `app/static/js/requisitions2.js:16`. ⚠️ STILL OPEN
  The file is loaded raw via `<script src=…>` while `page.html` also conditionally loads the Vite bundle. `Alpine.data('splitPanel', ...)` is registered twice.

### High

- **HIGH-FE-1 — Raw `fetch()` for HTMX-owned workflows** in `partials/crm/performance_tab.html:31`, `partials/requisitions/tabs/offers.html:62`, `partials/shared/trouble_report_form.html:55`. No CSRF header, no error rendering, server HTML responses thrown away. ⚠️ STILL OPEN
- **HIGH-FE-2 — `innerHTML = html` in `partials/shared/trouble_report_form.html:64`** — explicit CLAUDE.md anti-pattern. ⚠️ STILL OPEN
- **HIGH-FE-3 — `requisitions2/page.html:48–56` loads 8 CDN scripts**, only the first (`htmx.org@2.0.4`) has `integrity`. CDN compromise executes in the user's session. ⚠️ STILL OPEN
- **HIGH-FE-4 — Modal missing `aria-labelledby`** in `app/templates/htmx/base.html:65` (has `role="dialog" aria-modal="true"` only). WCAG 2.1 SC 4.1.2. ⚠️ STILL OPEN
- **HIGH-FE-5 — Login form inputs lack `<label>` elements** (`app/templates/htmx/login.html:57,60`). `placeholder` is not a substitute. ⚠️ STILL OPEN
- **HIGH-FE-6 — `performanceCharts` defined as a global `function` inside an inline `<script>`** in `partials/crm/performance_tab.html`. Re-declared on every HTMX swap; Chart.js canvases accumulate. ⚠️ STILL OPEN (not individually re-verified beyond the `fetch()` site)

### Medium

*(Carried over from 2026-05-04 review, not individually re-verified.)*

- **MED-FE-1** — Two parallel toast implementations (`base.html` inline vs. `partials/shared/toast.html`).
- **MED-FE-2** — ~290 arbitrary Tailwind font-size values. Add tokens to `tailwind.config.js`.
- **MED-FE-3** — `x-show` without `x-cloak` causes flash-of-invisible-content (`login.html`, `_macros.html`, `unified_modal.html`, `_vendor_row.html`).
- **MED-FE-4** — `vite.config.js` proxies only `/api`, `/auth`, `/health`; the HTMX page namespace is not proxied.
- **MED-FE-5** — `partials/requisitions/tabs/offers.html:62` `fetch()` discards the HTML response and ignores errors.
- **MED-FE-6** — Dark mode coverage near zero; `tailwind.config.js` doesn't set `darkMode: 'class'`.
- **MED-FE-7** — Font docs say "DM Sans" but `tailwind.config.js` uses Aptos/Segoe UI.

### Low

*(Carried over from 2026-05-04 review, not individually re-verified.)*

- **LOW-FE-1** — `<template x-cloak>` is meaningless. `toast.html`.
- **LOW-FE-2** — Topbar logo `hx-get`/`hx-push-url` mismatch (`partials/shared/topbar.html:10`).
- **LOW-FE-3** — `requisitions2/page.html` loads `htmx.org` synchronously (no `defer`).
- **LOW-FE-4** — `vite.config.js` could fingerprint additional shared chunks.

---

## 6. DevOps & infra (22 findings)

### Critical

- **CRIT-DEVOPS-1 — Migration failure silently swallowed.** ⚠️ STILL OPEN
  `docker-entrypoint.sh:31–33` — `if ! runuser -u appuser -- alembic upgrade head 2>&1; then echo "WARNING …"; fi`. Container starts against an un-migrated schema.
  *Re-verify 2026-05-21:* unchanged.
  *Fix:* let `alembic upgrade head` fail loudly so the container exits non-zero.
- **CRIT-DEVOPS-2 — `git add -A` in `deploy.sh`.** ◐ PARTIAL
  *Re-verify 2026-05-21:* `deploy.sh` was substantially hardened (must run from `main`, `git fetch` + `merge --ff-only` sync, explicit push-count verification with distinct exit codes). However the staging step is still `git add -A` at `deploy.sh:39`.
  *Fix:* stage explicit paths instead of `git add -A`.
- **CRIT-DEVOPS-3 — `--forwarded-allow-ips "*"`** in `Dockerfile:67` and `docker-compose.local.yml:15`. ⚠️ STILL OPEN — Anyone hitting port 8000 can spoof `X-Forwarded-For`. Lock to Caddy CIDR.
- **CRIT-DEVOPS-4 — `.github/workflows/deploy.yml:3–5` deploys on `release: published` without requiring CI to pass first.** ⚠️ STILL OPEN — Use `workflow_run` triggered by green CI on the same SHA, or branch protection.
- **CRIT-DEVOPS-5 — Bandit scan is non-blocking.** ✅ FIXED — a new non-`|| true` `Bandit summary` step (`security.yml:43–44`) runs `bandit … -ll` and now blocks the workflow on medium+ findings. See "Resolved since original review".

### High

- **HIGH-DEVOPS-1 — Backups not encrypted at rest or in Spaces.** ⚠️ STILL OPEN
  `scripts/backup.sh`, `scripts/backup-to-spaces.sh` — no `--sse AES256` / `gpg` encryption found.
  *Fix:* add `--sse AES256` and consider `gpg --symmetric` before upload.
- **HIGH-DEVOPS-2 — Base images not digest-pinned.** ⚠️ STILL OPEN
  `Dockerfile:2,12` — `node:20-alpine`, `python:3.12-slim`; `docker-compose.yml` services likewise tag-pinned. Pin `sha256:` digests + Dependabot `docker` ecosystem.
- **HIGH-DEVOPS-3 — `METRICS_TOKEN` defaults to empty**; `/metrics` is effectively public from the container network when unset. ⚠️ STILL OPEN — `METRICS_TOKEN` still absent from `.env.example`.
- **HIGH-DEVOPS-4 — Invalid pip pin syntax** for APScheduler in `requirements.txt:25`: `apscheduler==3.11.2,<4.0`. ⚠️ STILL OPEN — Use `>=3.11.2,<4.0` or `==3.11.2`.
- **HIGH-DEVOPS-5 — Loose lower-bound pins** for `rapidfuzz`, `orjson`, `sse-starlette`, `anthropic`, `azure-communication-callautomation`, `azure-communication-identity` in `requirements.txt:21,30,39,41,47,48`. ⚠️ STILL OPEN
- **HIGH-DEVOPS-6 — Dependabot does not cover `npm` or `docker` ecosystems** (`.github/dependabot.yml` covers only `pip` + `github-actions`). ⚠️ STILL OPEN
- **HIGH-DEVOPS-7 — `alembic downgrade base` in PR CI** (`.github/workflows/ci.yml:120`) is slow + fragile with 100+ migrations. ⚠️ STILL OPEN
  *Re-verify 2026-05-21:* still runs full `downgrade base` on every PR (now wrapped with `ALEMBIC_ALLOW_CASCADE`, which addresses correctness but not the slow/fragile concern).
  *Fix:* use `downgrade -1 && upgrade head` for PRs; full `downgrade base` nightly only.

### Medium

*(Carried over from 2026-05-04 review, not individually re-verified.)*

- **MED-DEVOPS-1** — Local `docker-compose.local.yml:15` mounts `app/` read-write; container writes land on host fs.
- **MED-DEVOPS-2** — `deploy.sh:5` hardcodes `/root/availai`. Replace with `cd "$(git rev-parse --show-toplevel)"`.
- **MED-DEVOPS-3** — Caddy reverse proxy missing `dial_timeout` and `response_header_timeout`.
- **MED-DEVOPS-4** — Caddy sets `Cache-Control: private, max-age=30` on `/api/companies`, `/api/vendors` — multi-tenant data cached in shared browser sessions.
- **MED-DEVOPS-5** — `db-backup` service has no healthcheck.
- **MED-DEVOPS-6** — `fix_queue` bind-mount writes to host without docs.

### Low

*(Carried over from 2026-05-04 review, not individually re-verified.)*

- **LOW-DEVOPS-1** — `pyproject.toml` / `mypy.ini` set `ignore_errors = true` on critical modules. Type-checking is mostly theater.
- **LOW-DEVOPS-2** — `SENTRY_DSN` not in `.env.example` despite `sentry-sdk` in requirements. *(Spot-checked 2026-05-21: still open.)*
- **LOW-DEVOPS-3** — `.githooks/pre-push` not wired in `scripts/setup.sh` / `.devcontainer/post-create.sh`.
- **LOW-DEVOPS-4** — Coverage gate is 50% (`.github/workflows/ci.yml:137`, `--cov-fail-under=50`) vs CLAUDE.md's 100% target. *(Spot-checked 2026-05-21: still 50%.)*

---

## Recommended action plan (updated 2026-05-21)

**Already done since the original review:** CRIT-TEST-1 (pytest-xdist), CRIT-DEVOPS-5 (Bandit now blocks). Partial progress on CRIT-SEC-2, CRIT-DEVOPS-2, HIGH-BE-4, HIGH-BE-6, HIGH-DB-3.

1. **Day 1 hot-fix PRs** (still outstanding, one-line/handful changes):
   - Remove the `if !` guard in `docker-entrypoint.sh:31` (CRIT-DEVOPS-1).
   - Replace `git add -A` at `deploy.sh:39` with explicit paths (CRIT-DEVOPS-2).
   - Add CSRF listener to `requisitions2/page.html` / `requisitions2.js` (CRIT-FE-1).
   - Demote agent user to `BUYER`, add explicit `require_buyer` block (CRIT-SEC-1).
   - Add `ENABLE_PASSWORD_LOGIN=false` + warning to `.env.example`; default seeded role to `buyer` (CRIT-SEC-2).
   - Add `METRICS_TOKEN`, `SENTRY_DSN` to `.env.example`; fix `ADMIN_EMAILS` placeholder (HIGH-DEVOPS-3, LOW-DEVOPS-2, MED-SEC-5).
   - Fix the `apscheduler` pin syntax in `requirements.txt:25` (HIGH-DEVOPS-4).
   - Remove `GET /auth/logout` registration (MED-SEC-7).

2. **Week 1 follow-ups:**
   - Bound `--forwarded-allow-ips` to Caddy CIDR (CRIT-DEVOPS-3).
   - Wire `workflow_run` / branch protection on deploy.yml (CRIT-DEVOPS-4).
   - Add Dependabot `npm` + `docker` ecosystems; pin Docker base images by digest (HIGH-DEVOPS-6, HIGH-DEVOPS-2).
   - Drop `lazy="joined"` on `Requirement.material_card` (HIGH-DB-1).
   - Replace inline rapidfuzz / duplicated MPN normalizer with canonical helpers (HIGH-BE-7, HIGH-BE-8).
   - Migration: CHECK constraints + index on `activity_log.quote_id` + partial index on `material_cards`.
   - Encrypt backups at rest (HIGH-DEVOPS-1).

3. **Week 2+ structural work:**
   - Split `app/routers/htmx_views.py` into 8–12 domain routers (HIGH-BE-1/2).
   - Move module-level mutable caches to Redis (HIGH-BE-9/10).
   - Convert the last `asyncio.run()` callsite to `async def` (HIGH-BE-4).
   - Wire authenticated E2E coverage (HIGH-TEST-2); remove reachable `# pragma: no cover` (HIGH-TEST-1/4).
   - Migrate `JSON` → `JSONB` (MED-DB-11); fix `Company` → `CustomerPartHistory` cascade (MED-DB-7, promote to High).

---

## Notes on methodology

- Original review (2026-05-04): six specialist subagents (`security-engineer`, `code-reviewer`, `data-engineer`, `test-engineer`, `frontend-engineer`, `devops-engineer`) reviewed the codebase in parallel with concrete grep-able rules.
- Re-verification (2026-05-21): all 11 Critical and all 35 High findings were individually re-checked against `main` at `0de51dc1` by opening the cited files. The 55 Medium and 38 Low/Info findings received a lighter spot-check pass and are carried over with a note unless otherwise marked.
- Where a finding's current status could not be confidently determined from the cited files, it is conservatively marked ⚠️ STILL OPEN with a "not individually re-verified" note.
- Finding IDs (`CRIT-SEC-1`, `HIGH-DEVOPS-7`, etc.) are preserved from the original review for traceability.
