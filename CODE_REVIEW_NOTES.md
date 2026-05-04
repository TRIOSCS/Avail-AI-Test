# AvailAI — Full Code Review Notes

**Branch:** `claude/code-review-notes-f6Cg7`
**Reviewed at:** 2026-05-04
**Base commit:** `fbba111` (`test: fix 55 parallel test failures caused by app.database module reload`)
**Scope:** Whole codebase — security, backend, database/migrations, tests, frontend, devops/infra
**Reviewers:** 6 specialist agents run in parallel (security-engineer, code-reviewer, data-engineer, test-engineer, frontend-engineer, devops-engineer)

---

## Executive Summary

| Area | Findings | Critical | High | Medium | Low/Info |
|------|----------|----------|------|--------|----------|
| Security | 23 | 2 | 4 | 8 | 9 |
| Backend code quality | 36 | 0 | 11 | 14 | 11 |
| Database & migrations | 25 | 0 | 3 | 14 | 8 |
| Tests & coverage | 15 | 2 | 5 | 6 | 2 |
| Frontend & templates | 18 | 2 | 5 | 7 | 4 |
| DevOps & infra | 22 | 5 | 7 | 6 | 4 |
| **Total** | **139** | **11** | **35** | **55** | **38** |

### Top 10 things to fix first

1. **CRIT-DEVOPS-1** — `docker-entrypoint.sh:31` swallows failed Alembic migrations with a `WARNING` and starts the app anyway. Remove the `if !` guard so a failed migration exits non-zero.
2. **CRIT-DEVOPS-2** — `deploy.sh:18` does `git add -A`. Risk of committing `.env` / debug files. Stage explicit paths instead.
3. **CRIT-DEVOPS-3** — `Dockerfile:67` and `docker-compose.local.yml:15` use `--forwarded-allow-ips "*"`. Lock to the Caddy container CIDR.
4. **CRIT-DEVOPS-4** — `.github/workflows/deploy.yml` deploys on `release: published` without requiring CI to pass first. Wire `workflow_run` or branch protection.
5. **CRIT-DEVOPS-5** — `.github/workflows/security.yml:38` runs Bandit with `|| true`. Make it block.
6. **CRIT-SEC-1** — Agent service account is seeded as `UserRole.ADMIN` (`app/startup.py:168`). `require_buyer` does not block it. Demote to `BUYER` and add the explicit guard.
7. **CRIT-SEC-2** — `ENABLE_PASSWORD_LOGIN` is an undocumented auth bypass that defaults the seeded user to `admin`. Add to `.env.example` with a warning; default role should be `buyer`.
8. **CRIT-FE-1** — `app/templates/requisitions2/page.html` has no CSRF listener; every `hx-post/patch/delete` from that page will be rejected (or, worse, accepted if CSRF is misconfigured).
9. **CRIT-TEST-1** — `pytest-xdist` is missing from `requirements-dev.txt` even though `pytest.ini` mandates `-n auto`. A clean install of dev dependencies cannot run the test suite.
10. **CRIT-TEST-2** — The "fix" for the PostgreSQL `app/database.py` branch (commit `fbba111`) does not actually exercise that branch — the new tests assert on hardcoded kwargs passed to a mock. Lines 37–50 of `app/database.py` are still effectively untested.

---

## 1. Security (23 findings)

### Critical

- **CRIT-SEC-1 — Agent service account has ADMIN role, allowing privilege escalation via `x-agent-key` header.**
  `app/startup.py:168`, `app/dependencies.py:54–58` — Agent user seeded with `role=UserRole.ADMIN`. `require_admin` and `require_settings_access` block the agent email explicitly, but `require_buyer` only checks `user.role`, so it lets the agent through. A leaked `AGENT_API_KEY` becomes an admin-level user.
  *Fix:* seed agent as `UserRole.BUYER` (or new `AGENT`); mirror the explicit `agent@availai.local` block in `require_buyer`.

- **CRIT-SEC-2 — `ENABLE_PASSWORD_LOGIN=true` is an undocumented persistent auth bypass.**
  `app/routers/auth.py:203–233`, `app/startup.py:65–66` — Flag is not in `.env.example`. The seeded user defaults to `admin`. Login form is served without auth.
  *Fix:* add `ENABLE_PASSWORD_LOGIN=false` to `.env.example` with a banner; default seeded role to `buyer`; gate behind a Compose profile or a startup assertion in production.

### High

- **HIGH-SEC-1 — `{{ title_attr|safe }}` builds an HTML attribute from data values.**
  `app/templates/htmx/partials/shared/_macros.html:150,158` — Today the values are server-side ints, but the pattern is structurally unsafe.
  *Fix:* drop `|safe`; let Jinja auto-escape attribute values.

- **HIGH-SEC-2 — Vendor email HTML rendered through `nh3.clean()` then `|safe`.**
  `app/templates/htmx/partials/emails/thread_viewer.html:59`, allowlist in `app/template_env.py:107–147` — `class` is allowed on every element; URL schemes need to be re-verified per `nh3` version. Vendor email is fully attacker-controlled.
  *Fix:* remove `class` from the wildcard allowlist; pin `nh3`; consider sandboxed `<iframe srcdoc>` for email bodies.

- **HIGH-SEC-3 — Unescaped wildcards in ILIKE patterns.**
  `app/routers/htmx_views.py:3436, 7030` — `term = f"%{q.strip()}%"` for brand/commodity/manufacturer search. Not SQLi (parameterized) but `%`/`_` from the user forces full-table scans.
  *Fix:* call `escape_like()` and pass `escape='!'` to `ilike()`, or use `SearchBuilder.safe`.

- **HIGH-SEC-4 — Graph webhook `validationToken` echo is unauthenticated.**
  `app/routers/v13_features/activity.py:49–51, 88–90` — Required by Graph protocol; mitigate at edge.
  *Fix:* IP-allowlist Microsoft ranges in Caddy; alert on unexpected validation events.

### Medium

- **MED-SEC-1** — `/auth/status` (`app/routers/auth.py:261–302`) has no auth dependency and returns user PII / M365 connection state. Add `Depends(require_user)`.
- **MED-SEC-2** — Session cookie `httponly` not set explicitly (`app/main.py:246–252`). Don't rely on Starlette default. Also `same_site="lax"` does not protect GET-based logout (see MED-SEC-7).
- **MED-SEC-3** — Session `max_age=86400` (24h), no idle timeout. Reduce to ~8h with sliding `last_seen`.
- **MED-SEC-4** — Hardcoded customer-specific defaults in `app/config.py:164,165,194` (`stock_sale_vendor_names`, `stock_sale_notify_emails`, `own_domains`). Replace with placeholders, document required env vars.
- **MED-SEC-5** — `.env.example:60` ships with `ADMIN_EMAILS=mkhoury@trioscs.com`. Replace with `admin@yourcompany.com`.
- **MED-SEC-6** — CSP allows `unsafe-inline` + `unsafe-eval` and several CDN origins (`app/main.py:328–329`). XSS protection is effectively zero. CDN scripts in `requisitions2/page.html` and `login.html` mostly lack SRI hashes.
- **MED-SEC-7** — `GET /auth/logout` is registered (`app/routers/auth.py:171–175`) — CSRF-logout via `<img src=…>`. Make logout POST-only.
- **MED-SEC-8** — Public `/docs` and `/redoc` (`app/main.py:162–168`). Set `docs_url=None, redoc_url=None` in production or gate behind admin.

### Low / Info

- **LOW-SEC-1** — Verify `/v2/partials/customers/lookup` is truly read-only before keeping its CSRF exemption (`app/main.py:274`).
- **LOW-SEC-2** — Graph error text leaks via `resp.text[:300]` returned to caller (`app/utils/graph_client.py:240`).
- **LOW-SEC-3** — `_build_html_body()` in `app/email_service.py:32–35` does `\n → <br>` without HTML-escaping the plain text first. Outbound HTML-injection vector.
- **LOW-SEC-4** — ACS webhook reflects `validationCode` without IP restriction (`app/routers/v13_features/activity.py:138–140`).
- **LOW-SEC-5** — Agent user is re-seeded on every boot regardless of operator intent (`app/startup.py:73, 154–177`).
- **INFO-SEC-1** — HSTS missing `preload` directive (`app/main.py:368–369`).
- **INFO-SEC-2** — `/metrics` 403 response includes `request_id` — minor probe-correlation aid.
- **INFO-SEC-3** — `ENCRYPTION_SALT` falls back to a static legacy salt when unset (`app/config.py:47`). Require non-empty in production.
- **INFO-SEC-4** — `SameSite=Lax` does not protect GET-based actions (relates to MED-SEC-7).

---

## 2. Backend code quality (36 findings)

### High

- **HIGH-BE-1 — God file `app/routers/htmx_views.py` is 10,024 lines** with 244 functions, 249 routes, 377 direct DB ops. Split into ~8–12 domain routers.
- **HIGH-BE-2 — Top god files (>700 lines):** `htmx_views.py` (10024), `search_service.py` (2114), `routers/requisitions/requirements.py` (1757), `routers/sightings.py` (1312), `email_service.py` (1277), `services/knowledge_service.py` (1257), `services/excess_service.py` (1153), `routers/crm/offers.py` (1017), `startup.py` (1008), `jobs/email_jobs.py` (1006). `search_service.py` and `email_service.py` should become packages.
- **HIGH-BE-3 — Blocking sync `httpx.get/post` inside async APScheduler job.**
  `app/services/eight_by_eight_service.py:150,189,236` called from async `_job_poll_8x8_cdrs` at `app/jobs/eight_by_eight_jobs.py:42`. Freezes the event loop for the full pagination window.
  *Fix:* `httpx.AsyncClient` + `await`, or `asyncio.to_thread`.
- **HIGH-BE-4 — `asyncio.run()` inside FastAPI `BackgroundTasks` closures.**
  `app/routers/htmx_views.py:743, 1069, 1173, 1224, 2950`; `app/routers/requisitions/requirements.py:493`. Creates a fresh loop per call; serializes searches and breaks shared connection pools.
- **HIGH-BE-5 — Business logic in routers.** `htmx_views.py` re-fetches/recomputes presentation fields (`req.offer_count = len(req.offers)`) and embeds cron-style background loops (`htmx_views.py:725–749`). Belongs in a service layer DTO.
- **HIGH-BE-6 — Raw status string comparisons** (StrEnum violations) in 18 sites. Notably `app/routers/crm/companies.py:137` checks `Requisition.status == "won"` but `RequisitionStatus` has no `WON` — the comparison is silently always False on current data. Other sites: `requisition_list_service.py`, `routers/requisitions/core.py`, `services/sourcing_score.py`, `services/avail_score_service.py`, `htmx_views.py:7761`, `services/requirement_status.py:44`.
- **HIGH-BE-7 — Inline rapidfuzz** bypassing `fuzzy_score_vendor()` in `app/services/auto_dedup_service.py:68,97` (thresholds 92/98) and `app/routers/vendors_crud.py:60,69` (threshold 80).
- **HIGH-BE-8 — Duplicated MPN normalizer** at `app/services/sourcing_leads.py:59–62` collides with the canonical `app/utils/normalization.py:339`. Different semantics → dedup mismatches.
- **HIGH-BE-9 — Module-level mutable caches with no eviction** in `services/webhook_service.py:35`, `services/email_threads.py:30`, `services/admin_service.py:90`, `services/credential_service.py:170`, `services/presence_service.py:11`, `services/ai_part_normalizer.py:33`, `routers/sightings.py:48`, `services/search_worker_base/monitoring.py:25`. Will not survive worker restart and diverges across uvicorn workers.
- **HIGH-BE-10 — Duplicate cache machinery.** `routers/sightings.py:48–68` rolls its own TTL cache; `app/cache/decorators.py` and `app/cache/intel_cache.py` already exist.
- **HIGH-BE-11 — 1163 `db.query(...)` call sites** still using SQLAlchemy 1.x style. Many `db.query(X).filter_by(id=…).first()` should be `db.get(X, id)`. Plus rule violation: CLAUDE.md mandates 2.0 style.

### Medium

- **MED-BE-1** — `db.query(...).get(id)` is currently absent (passed). Don't regress.
- **MED-BE-2** — 18 router `HTTPException(detail=…)` raises rely on the global handler to rename `detail` → `error`. Document explicitly or wrap in a helper.
- **MED-BE-3** — N+1 risk: list endpoints iterate `req.requirements`, `req.offers` without `selectinload` (`htmx_views.py:463, 9835, 9856`; `requisitions/requirements.py:334,939,960,986,1011,1226`; `crm/clone.py:38,71`; `services/quote_builder_service.py:35`).
- **MED-BE-4** — Magic numbers (fuzzy thresholds 92/98/80, batch sizes 1000, limits 5000/10000/50000, timeouts 10/15/20s, lookback `days=180`, retry delays `[1,2,4]`, RFQ batch timeout 24h). Promote to `app/config.py`.
- **MED-BE-5** — Mutation routes commit without `try/except`/`db.rollback()`. Audit `app/database.py:get_db` to confirm rollback-on-exception semantics.
- **MED-BE-6** — Commit-then-best-effort patterns in `email_service.py:222, 240–241, 1186–1187`. Side effects can fail silently after the main commit.
- **MED-BE-7** — Silent exception swallowing in `routers/htmx_views.py:744–745, 2508–2510`; `services/ics_worker/search_engine.py:50,131`; `services/tagging_ai_batch.py:546–547`; `services/tagging_ai_triage.py:309–310`. Add structured logs or surfaceable warnings.
- **MED-BE-8** — `time.sleep(...)` (8 calls) in `app/services/nc_worker/worker.py`. Confirm thread/process isolation; add a `# runs in dedicated thread` assertion to prevent accidental async use.
- **MED-BE-9** — Deprecated `asyncio.get_event_loop()` pattern in `email_service.py:1172–1185`. Use `asyncio.get_running_loop()` (or only call from `async def`).
- **MED-BE-10** — Pydantic v2 `model_config = ConfigDict()` style is consistently followed (passed). Don't regress.
- **MED-BE-11** — `os.environ.get(...)` reads outside `app/config.py` in `services/ics_worker/config.py:17,18,27` and `nc_worker/config.py:17–28`. Funnel through `Settings`.
- **MED-BE-12** — `_is_htmx` helper is defined locally in `htmx_views.py:106` and duplicated across routers. Move to `app/dependencies.py`.
- **MED-BE-13** — `app/main.py:35–49` has two `if not os.environ.get("TESTING"):` blocks. Coalesce.
- **MED-BE-14** — `app/main.py:139–141` references `_is_testing` after `yield`; if startup raises before line 113 it's `UnboundLocalError` on shutdown. Initialize `_is_testing = False` at top of `lifespan`.

### Low

- **LOW-BE-1** — Mixed log format styles (f-strings vs `{}` placeholders) in `email_service.py` and `services/`. Pick one for grep-able analytics.
- **LOW-BE-2** — Re-imports inside hot loops (`email_service.py:1172`, `htmx_views.py:726,732`). Hoist to module top.
- **LOW-BE-3** — `app/main.py:49` uses `%s` placeholder with Loguru — verify it formats correctly (Loguru defaults to `{}`-style).
- **LOW-BE-4** — Dozens of local imports inside route functions in `htmx_views.py` indicate structural coupling. Document or refactor.
- **LOW-BE-5** — Manual JS-template-literal escaping in `htmx_views.py:2513–2516`. Use `json.dumps()` instead.
- **LOW-BE-6** — Fire-and-forget `loop.create_task(...)` in `email_service.py:1179–1185` without a tracking set. Tasks may be GC'd mid-flight.
- **LOW-BE-7** — Header comments missing on `services/sourcing_leads.py`, `services/auto_dedup_service.py` per CLAUDE.md rule.
- **LOW-BE-8** — Dead code: `app/scheduler.py:50–56` re-exports `_utc, get_valid_token, refresh_user_token` from `token_manager` ("backward-compatible"). Delete after audit.
- **LOW-BE-9** — Underscore-prefixed `_connector_status` at `main.py:121` escapes via `app.state` — drop the underscore.
- **LOW-BE-10** — No `print()` calls in production paths (passed).
- **LOW-BE-11** — No bare `except:` clauses (passed).

---

## 3. Database & migrations (25 findings)

### High

- **HIGH-DB-1 — `Requirement.material_card` uses `lazy="joined"`** (`app/models/sourcing.py:127`). Forces a JOIN on every bulk requirement load, doubling query cost on requisition list pages.
  *Fix:* drop `lazy="joined"`; use `selectinload(Requirement.material_card)` only on pages that need the card.
- **HIGH-DB-2 — Pervasive `Column(DateTime)` instead of `UTCDateTime`** across all 73 models — `Requisition`, `VendorCard`, `Offer`, `BuyPlan`, `BuyPlanLine`, `ActivityLog`, `EnrichmentRun`. Two outliers (`Sighting.source_searched_at`, `Quote.followup_alert_sent_at`) use `DateTime(timezone=True)`.
  *Fix:* mandate `UTCDateTime` in code review; migrate critical `created_at`/`updated_at` columns to `TIMESTAMPTZ`.
- **HIGH-DB-3 — `search_service.py` inserts `Sighting` rows one-by-one** (`app/search_service.py:1199–1226`). 10 sources × ~50 results × `db.flush()` for scoring → up to 500 row-inserts per search.
  *Fix:* `bulk_insert_mappings` after scoring, or `INSERT … ON CONFLICT DO NOTHING`.

### Medium

- **MED-DB-1** — Orphaned `buy_plans` (V1) table has no SQLAlchemy mapping (`models/quotes.py:114`); autogenerate won't clean it. Add an explicit DROP migration.
- **MED-DB-2** — `Requisition.@validates("status")` and `Offer._validate_status` only log warnings instead of raising (`models/sourcing.py:82–90`, `models/offers.py:107–116`). Inconsistent with `TroubleTicket`/`BuyPlan` validators that raise. SQLite tests bypass DB CHECK; bad statuses pass through.
- **MED-DB-3** — `activity_log.quote_id` FK has no index (`models/intelligence.py:272`). Quote-detail timelines full-scan.
- **MED-DB-4** — `material_cards.deleted_at` indexed as full B-tree (`alembic/versions/079_add_index_material_cards_deleted_at.py:20`). Use a partial `WHERE deleted_at IS NULL` index on `normalized_mpn` instead.
- **MED-DB-5** — String status columns missing CHECK constraints: `activity_log.activity_type/channel/event_type/direction`, `enrichment_run.status`, `contact.status`, `excess_lists.status`, `excess_line_items.status`, `site_contacts.contact_status`. Add via migration.
- **MED-DB-6** — `ExcessList.owner_id`, `Bid.sent_by`, `Bid.created_by` use `ondelete="RESTRICT"` (`models/excess.py:42,117,157`). Inconsistent with rest of codebase (SET NULL). Deleting a user fails silently.
- **MED-DB-7 (HIGH severity in source) — Deleting a `Company` cascades to `CustomerPartHistory`** (`models/purchase_history.py:37`, `ondelete="CASCADE"`). Erases all purchase history that drives proactive matching. *Promote to High.* Change to SET NULL or guard with soft-delete.
- **MED-DB-8** — `ProactiveDoNotOffer` cascades on `company_id` (`models/intelligence.py:225`). Re-creating a company silently loses suppressions. Change to SET NULL.
- **MED-DB-9** — `MaterialCard.deleted_at` not filtered in upsert paths (`routers/materials.py:466,478`; `routers/crm/quotes.py:576`; `routers/crm/offers.py:199`). Soft-deleted cards can be accidentally resurrected. Add `.filter(MaterialCard.deleted_at.is_(None))`.
- **MED-DB-10** — `ProspectContact.confidence` is `String(10)` (`models/enrichment.py:140`); other `confidence` columns are `Float`. Rename to `confidence_band` or split.
- **MED-DB-11** — `Quote.line_items`, `ProactiveOffer.line_items`, `BuyPlan.ai_flags` are `JSON`, not `JSONB` (`models/quotes.py:35`, `intelligence.py:178`, `buy_plan.py:97`). Migrate to `JSONB` for index/operator support.
- **MED-DB-12** — `PendingBatch.batch_id` is `Column(String)` with no length (`models/pipeline.py:62`). Cap at `String(100)`.
- **MED-DB-13** — `Requisition` has 5 collection relationships defaulting to `lazy="select"`. Document N+1 risk; force `selectinload` on list endpoints.
- **MED-DB-14** — `startup._backfill_proactive_offer_qty` loads ALL `proactive_offers` rows on every boot (`startup.py:772–773`). Add a `system_config` flag for one-shot completion.

### Low

- **LOW-DB-1** — Several recent migrations use slug IDs (`restructure_substitutes_json`, etc.) instead of hex prefixes. Standardize.
- **LOW-DB-2** — `Requirement.assigned_buyer_id` no index (`models/sourcing.py:122`).
- **LOW-DB-3** — `BuyPlanLine.requirement_id` is nullable but semantically required.
- **LOW-DB-4** — `_backfill_material_cards` does not re-link to soft-deleted cards (correct, but undocumented).
- **LOW-DB-5** — `AvailScoreSnapshot.bonus_amount`, `MultiplierScoreSnapshot.bonus_amount` use `Float` for money. `Numeric(10,2)`.
- **LOW-DB-6** — `VendorSightingsSummary.avg_price`, `best_price` are `Float`. `Numeric(12,4)`.
- **LOW-DB-7** — `startup._backfill_material_cards` and `_backfill_ticket_defaults` ORM-load all matching rows without `LIMIT` on every boot. Add batch size + `system_config` flag.
- **LOW-DB-8** — `startup.py:187` log message says "DDL failed" for what is mostly DML. Misleading on incident response.

---

## 4. Tests & coverage (15 findings)

### Critical

- **CRIT-TEST-1 — `pytest-xdist` is not in `requirements-dev.txt`** despite `pytest.ini:7` having `addopts = -n auto`. A clean install cannot run the test suite.
  *Fix:* add `pytest-xdist>=3.5.0` to `requirements-dev.txt`.
- **CRIT-TEST-2 — `app/database.py` PostgreSQL branch (lines 37–50) is still effectively untested.** The post-`fbba111` tests in `tests/test_database_coverage.py:189–223` call `sqlalchemy.create_engine(...)` directly with hardcoded kwargs and assert on those kwargs — they do not import the production module path with a postgres URL.
  *Fix:* patch `app.config.settings.database_url` to a postgres URL inside a try/finally and re-import `app.database`, or assert against the live module's engine kwargs.

### High

- **HIGH-TEST-1 — Two reachable functions excluded with `# pragma: no cover`** in `app/services/tagging_ai_classify.py:44, 87` (`classify_parts_with_ai`, `_apply_ai_results`). Tests already exercise them.
  *Fix:* remove the pragmas.
- **HIGH-TEST-2 — E2E `workflows.spec.ts` and `dead-ends.spec.ts` run unauthenticated** and accept `401`/`307` as passing. The whole login → search → RFQ → offer flow has no E2E coverage.
  *Fix:* inject signed Starlette session cookies via `storageState` in `playwright.config.ts` (mirroring `tests/e2e/conftest.py`'s `authed_page`).
- **HIGH-TEST-3 — 2,392 tests assert only a single status code** with no body / DB-state assertion. Examples: `test_sightings_router_coverage3.py::test_batch_refresh_too_many`, `test_sightings_router_coverage3.py::test_advance_status_valid_open_to_sourcing`, `test_archive_system.py::test_archive_single_part_not_found`. Coverage line counts pass while regressions ride through.
- **HIGH-TEST-4 — Tag-propagation loop excluded from coverage** (`app/search_service.py:1309` `# pragma: no cover`, also `:1739, :1749`). Reachable via mocked AI responses.
- **HIGH-TEST-5 — Skipped tests for routes/schemas removed long ago.** `tests/test_tt105_user_validation.py:14` (admin users router), `tests/test_tt026_tt040_fixes.py:19` (sales notifications), `tests/test_req_offer_fields.py:64,118` (RequirementOut/OfferOut). Delete or restore.

### Medium

- **MED-TEST-1** — `event_loop` fixture in `tests/conftest.py:59–64` is deprecated under `pytest-asyncio>=0.23` (which is pinned in `requirements-dev.txt`). Remove; let `asyncio_mode=auto` manage the loop.
- **MED-TEST-2** — Module-level `os.environ["ANTHROPIC_API_KEY"] = "test-key"` in `tests/test_email_parser.py:11` and `tests/test_part_normalizer.py:11`. Use `setdefault` or move into a fixture.
- **MED-TEST-3** — Real `time.sleep(0.15)` in `tests/test_circuit_breaker.py:72`. Patch `time.time` instead.
- **MED-TEST-4** — 29 `tests/test_htmx_views_nightly*.py` files (1,157 test functions). Coverage archaeology — consolidate into `tests/test_htmx_views.py` grouped by router section.
- **MED-TEST-5** — `Base.metadata.drop_all(bind=engine)` autouse fixture (now removed in `fbba111`) was the underlying cause of 55 parallel failures. Add a guard (lint or conftest assertion) to prevent any non-conftest test from calling `drop_all`/`create_all` on the shared engine.
- **MED-TEST-6** — `e2e/accessibility.spec.ts` only audits the unauthenticated login page. Add axe scans of `/v2/requisitions`, `/v2/vendors`, `/v2/materials` after authentication is wired (HIGH-TEST-2).

### Low

- **LOW-TEST-1** — `# pragma: no cover` on reachable duplicate-detection branches in `app/routers/crm/companies.py:443,445`.
- **LOW-TEST-2** — `tests/test_contact_quality.py:181,209` calls `pytest.skip(...)` mid-test because of UNIQUE-constraint violations in setup. Use unique emails per test.

### "100% coverage" claim caveats

- `branch = false` in `.coveragerc` — only line coverage measured.
- `pragma: no cover` is used on reachable hot-path code in `search_service.py`, `tagging_ai_classify.py`, `crm/companies.py`.
- `app/routers/_lookup_helpers.py` has no dedicated test file (only indirect coverage).

---

## 5. Frontend & templates (18 findings)

### Critical

- **CRIT-FE-1 — `requisitions2/page.html` has no CSRF listener.** Loads HTMX from CDN and `requisitions2.js` from disk but does not register the `htmx:configRequest` listener that `htmx_app.js:160–165` does. Every `hx-post/patch/delete` from that page will lack the `x-csrftoken` header. Starlette CSRF middleware will reject — or worse, accept if CSRF is misconfigured.
- **CRIT-FE-2 — Duplicate Alpine component `splitPanel` registered out-of-bundle** in `app/static/js/requisitions2.js`. The file is loaded raw via `<script src=…>` while `page.html` also conditionally loads the Vite bundle. `Alpine.data('splitPanel', ...)` is registered twice; the second wins.

### High

- **HIGH-FE-1 — Raw `fetch()` for HTMX-owned workflows** in `partials/crm/performance_tab.html:31`, `partials/requisitions/tabs/offers.html:62–70`, `partials/shared/trouble_report_form.html:55–71`. No CSRF header, no error rendering, server HTML responses thrown away.
- **HIGH-FE-2 — `innerHTML = html` in `partials/shared/trouble_report_form.html:64`** — explicit CLAUDE.md anti-pattern, plus the surrounding `fetch()`.
- **HIGH-FE-3 — `requisitions2/page.html` loads 6 CDN scripts**, only the first has `integrity`. CDN compromise executes in the user's session.
- **HIGH-FE-4 — Modal missing `aria-labelledby`** in `app/templates/htmx/base.html:65`. WCAG 2.1 SC 4.1.2 requires dialogs to have an accessible name. Require every modal partial to include `id="modal-title"`.
- **HIGH-FE-5 — Login form inputs lack `<label>` elements** (`app/templates/htmx/login.html:57–62`). `placeholder` is not a substitute.
- **HIGH-FE-6 — `performanceCharts` defined as a global `function` inside an inline `<script>`** in `partials/crm/performance_tab.html:28–100`. Re-declared on every HTMX swap; Chart.js canvases accumulate. Also injects Chart.js from CDN per swap.

### Medium

- **MED-FE-1** — Two parallel toast implementations (`base.html:74–85` inline vs. `partials/shared/toast.html`). Different pages get different behavior. `toast.html:9–10` has `x-cloak` duplicated.
- **MED-FE-2** — ~290 arbitrary Tailwind font-size values (`text-[10px]`, `text-[13px]`, etc.). Add tokens to `tailwind.config.js`.
- **MED-FE-3** — `x-show` without `x-cloak` causes flash-of-invisible-content on initial render: `login.html:64,69–70`, `partials/shared/_macros.html:411`, `partials/requisitions/unified_modal.html:62`, `partials/sightings/_vendor_row.html:78`.
- **MED-FE-4** — `vite.config.js:27–31` proxies only `/api`, `/auth`, `/health`. The HTMX page namespace (`/v2/*`, `/requisitions2`) is not proxied, breaking `npm run dev`.
- **MED-FE-5** — `partials/requisitions/tabs/offers.html:62` `fetch()` discards the HTML response and ignores errors.
- **MED-FE-6** — Dark mode coverage near zero (~6 `dark:` class usages across 182 templates). `tailwind.config.js` doesn't even set `darkMode: 'class'`. Decide and commit; remove from CLAUDE.md if not real.
- **MED-FE-7** — Font docs say "DM Sans" but `tailwind.config.js:20` and `styles.css:308` use Aptos/Segoe UI. Fix the docs or the config.

### Low

- **LOW-FE-1** — `<template x-cloak>` is meaningless (`<template>` is already hidden). `toast.html:28,33,38`.
- **LOW-FE-2** — Topbar logo: `hx-get="/v2/partials/parts/workspace"` but `hx-push-url="/v2/requisitions"` (`partials/shared/topbar.html:10`). Refresh from that URL loads a different template.
- **LOW-FE-3** — `requisitions2/page.html:48` loads `htmx.org` synchronously (no `defer`).
- **LOW-FE-4** — `vite.config.js` could fingerprint additional shared chunks (minor).

---

## 6. DevOps & infra (22 findings)

### Critical

- **CRIT-DEVOPS-1 — Migration failure silently swallowed.** `docker-entrypoint.sh:31–33` `if ! runuser -u appuser -- alembic upgrade head; then echo "WARNING ... skipping..."; fi`. Container starts against an un-migrated schema. *Fix:* let `alembic upgrade head` fail loudly so the container exits non-zero.
- **CRIT-DEVOPS-2 — `git add -A` in `deploy.sh:18`.** Risk of committing `.env`, debug files, editor swap files. Stage explicit paths.
- **CRIT-DEVOPS-3 — `--forwarded-allow-ips "*"`** in `Dockerfile:67` and `docker-compose.local.yml:15`. Anyone hitting port 8000 can spoof `X-Forwarded-For`, poisoning IP-based rate limiting and session attribution. Lock to Caddy CIDR.
- **CRIT-DEVOPS-4 — `.github/workflows/deploy.yml:5–7` deploys on `release: published` without requiring CI to pass first.** Use `workflow_run` triggered by green CI on the same SHA, or branch protection.
- **CRIT-DEVOPS-5 — Bandit scan is non-blocking** (`.github/workflows/security.yml:38` ends with `|| true`). Remove `|| true`; gate on `-ll` (medium+).

### High

- **HIGH-DEVOPS-1 — Backups not encrypted at rest or in Spaces.** `scripts/backup.sh`, `scripts/backup-to-spaces.sh`. Add `--sse AES256` and consider `gpg --symmetric` before upload.
- **HIGH-DEVOPS-2 — Base images not digest-pinned.** `Dockerfile:2,12`, `docker-compose.yml:7,51,142,171` — `node:20-alpine`, `python:3.12-slim`, `postgres:16-alpine`, `redis:7-alpine`, `caddy:2-alpine`. With `--no-cache` builds on every deploy, a poisoned upstream layer rolls in. Pin `sha256:` digests + Dependabot `docker` ecosystem.
- **HIGH-DEVOPS-3 — `METRICS_TOKEN` defaults to empty** (`app/config.py:229`); the check at `main.py:305` is `if not token: return` — if unset, `/metrics` is effectively public from the container network. Add to `.env.example`; require non-empty in production.
- **HIGH-DEVOPS-4 — Invalid pip pin syntax** for APScheduler in `requirements.txt:25`: `apscheduler==3.11.2,<4.0` (mixing `==` and `<` without separator). Use `>=3.11.2,<4.0` or `==3.11.2`.
- **HIGH-DEVOPS-5 — Loose lower-bound pins** for `rapidfuzz`, `orjson`, `sse-starlette`, `anthropic`, `azure-communication-callautomation`, `azure-communication-identity` in `requirements.txt:37–48`. A future major release silently rolls in.
- **HIGH-DEVOPS-6 — Dependabot does not cover `npm` or `docker` ecosystems** (`.github/dependabot.yml`).
- **HIGH-DEVOPS-7 — `alembic downgrade base` in PR CI** (`.github/workflows/ci.yml:108–110`) is slow + fragile with 109+ migrations. Use `downgrade -1 && upgrade head` for PRs; full `downgrade base` nightly only.

### Medium

- **MED-DEVOPS-1** — Local `docker-compose.local.yml:15` mounts `app/` read-write; container writes (e.g., `__pycache__`) land on host fs. Add `:delegated` (mac) or document.
- **MED-DEVOPS-2** — `deploy.sh:5` hardcodes `/root/availai`. Replace with `cd "$(git rev-parse --show-toplevel)"`.
- **MED-DEVOPS-3** — Caddy reverse proxy missing `dial_timeout` and `response_header_timeout` (`Caddyfile:8–11`). A hung upstream exhausts the keepalive pool.
- **MED-DEVOPS-4** — `Caddyfile:62` sets `Cache-Control: private, max-age=30` on `/api/companies`, `/api/vendors` — multi-tenant data cached in shared browser sessions. Use `no-store` or push caching to Redis.
- **MED-DEVOPS-5** — `db-backup` service has no healthcheck (`docker-compose.yml:141–168`). A crashed backup loop won't be detected.
- **MED-DEVOPS-6** — `fix_queue` bind-mount (`docker-compose.yml:87`) writes to host without docs. Document or make a named volume.

### Low

- **LOW-DEVOPS-1** — `pyproject.toml:17–31` sets `ignore_errors = true` on 13 critical modules (routers, services, connectors, jobs, schemas). Type-checking is mostly theater.
- **LOW-DEVOPS-2** — `SENTRY_DSN` not in `.env.example` despite `sentry-sdk` in requirements.
- **LOW-DEVOPS-3** — `.githooks/pre-push` not wired in `scripts/setup.sh` / `.devcontainer/post-create.sh`. Inconsistent with `pre-commit` install path.
- **LOW-DEVOPS-4** — Coverage gate is 50% (`.github/workflows/ci.yml:125`) vs CLAUDE.md's 100% target. Raise to 75% as a starting point.

---

## Recommended action plan

1. **Day 1 hot-fix PRs** (one-line/handful changes, no design needed):
   - Add `pytest-xdist` to `requirements-dev.txt` (CRIT-TEST-1).
   - Remove `if !` guard in `docker-entrypoint.sh:31` (CRIT-DEVOPS-1).
   - Replace `git add -A` with explicit paths in `deploy.sh:18` (CRIT-DEVOPS-2).
   - Remove `|| true` from Bandit (CRIT-DEVOPS-5).
   - Remove `GET /auth/logout` registration (MED-SEC-7).
   - Set `docs_url=None, redoc_url=None` in production (MED-SEC-8).
   - Add CSRF listener to `requisitions2/page.html` (CRIT-FE-1).
   - Demote agent user to `BUYER`, add explicit `require_buyer` block (CRIT-SEC-1).
   - Add `ENABLE_PASSWORD_LOGIN=false` + warning to `.env.example`; default seeded role to `buyer` (CRIT-SEC-2).

2. **Week 1 follow-ups** (medium-effort, big leverage):
   - Bound `--forwarded-allow-ips` to Caddy CIDR (CRIT-DEVOPS-3).
   - Wire `workflow_run` on deploy.yml (CRIT-DEVOPS-4).
   - Migration: add CHECK constraints + index on `activity_log.quote_id` + partial index on `material_cards`.
   - Sweep `db.query(X).filter_by(id=…).first()` → `db.get(X, id)`.
   - Replace inline rapidfuzz with `fuzzy_score_vendor()`.
   - Replace raw status string comparisons with StrEnum (start with `companies.py:137` which is currently dead-code).
   - Lock `/auth/status` behind `require_user`.
   - Encrypt backups at rest (`--sse AES256`).

3. **Week 2+ structural work**:
   - Split `app/routers/htmx_views.py` into 8–12 domain routers.
   - Move module-level mutable caches to Redis via `cache/decorators.py`.
   - Convert `BackgroundTasks` `asyncio.run()` callsites to `async def`.
   - Migrate `JSON` → `JSONB` for `Quote.line_items`, `ProactiveOffer.line_items`, `BuyPlan.ai_flags`.
   - Wire authenticated E2E coverage of login → search → RFQ → offer.
   - Consolidate the 29 `test_htmx_views_nightly*.py` files.
   - Pin Docker base images by digest; add Dependabot npm + docker ecosystems.

---

## Notes on methodology

- Six specialist subagents reviewed the codebase in parallel:
  - `security-engineer` — auth, sessions, XSS, SQLi, secrets, CSRF, CSP, headers
  - `code-reviewer` — backend code quality, conventions, anti-patterns
  - `data-engineer` — schema, indexes, migrations, ORM patterns, type discipline
  - `test-engineer` — coverage gaps, flakiness, mocking, fixtures, E2E
  - `frontend-engineer` — Jinja2/HTMX/Alpine.js/Tailwind, accessibility, anti-patterns
  - `devops-engineer` — Docker, Caddy, CI/CD, deploy.sh, backups, dependency hygiene
- Each agent was given concrete grep-able rules (file:line style), not generic advice.
- Findings are de-duplicated where reviewers overlapped.
- Severity grading is the agent's, sanity-checked against impact (data loss, exploit surface, blast radius).
- "Passed" checks (e.g., no bare `except:`, no `print()`, Pydantic v2 styles) are listed too so future regressions are visible.
