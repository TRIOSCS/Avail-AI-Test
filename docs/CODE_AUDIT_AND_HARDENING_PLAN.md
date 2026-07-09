# Code Audit & Hardening Plan — July 2026

**Source:** Full-codebase review by a 7-agent fleet (architecture, security, database,
frontend, tests, conventions/typing, infra/ops) across ~161k lines of Python,
333 templates, 233 migrations, and 1,055 test files. The five highest-stakes findings
were independently re-verified against the code before inclusion.

**How to use this doc:** work top-down. Each phase is a set of independent PR-sized
work items. Check items off in this doc in the same PR that fixes them.

---

## Phase 0 — Verified live bugs (fix immediately, ~1 PR each)

These are confirmed defects shipping today, not style issues.

- [x] **P0.1 — Unawaited async coroutines: all 4 "Refresh AI insights" buttons are no-ops.**
  `app/routers/htmx_views.py:1644, 1676, 1708, 1738` call `generate_insights(db, id)` /
  `generate_vendor_insights(...)` etc. without `await` — the coroutine is created and
  discarded; the surrounding `try/except` never fires; the endpoint re-renders stale
  cache. Fix: `await` all four. Add regression tests asserting the service is awaited
  (e.g. `AsyncMock` + `assert_awaited_once`).

- [x] **P0.2 — Duplicate-column migration breaks fresh-DB `alembic upgrade head`.**
  `alembic/versions/5c6736d6381f_add_screenshot_path_ai_summary_root_.py:34-42`
  unconditionally re-adds `offers.excess_line_item_id` + FK already created by its
  ancestor `d1a2b3c4e5f6_add_excess_phase4_columns.py` (which is guarded). On a fresh
  DB this raises `DuplicateColumn`. Fix: add the same `_column_exists` guard (or delete
  the redundant ops). Verify with upgrade → downgrade → upgrade on a throwaway Postgres.

- [x] **P0.3 — Whole-page wipe on quote detail scroll.**
  `app/templates/htmx/partials/quotes/detail.html:370` — pricing-history lazy-load
  `div` has `hx-trigger="revealed"` but no `hx-target`, so it inherits
  `hx-target="this"` from `#main-content` (`htmx/base.html:50`) and replaces the whole
  page. Every sibling lazy-load carries a "LANDMINE" guard comment; this one was missed.
  Fix: add `hx-target="this"`. Add a dead-ends E2E case.

- [x] **P0.4 — Dangling `asyncio.create_task`: SSE events can be GC'd mid-flight.**
  `app/email_service.py:1605`, `app/services/prepayment_notifications.py:122` —
  task results unreferenced; `sighting-updated` events silently lost. Fix: hold refs
  (`_bg_tasks.add(task); task.add_done_callback(_bg_tasks.discard)`).

- [x] **P0.5 — The mypy CI gate is hollow.**
  Two configs coexist: `mypy.ini` (bare run → 566 errors in 158 files) vs
  `pyproject.toml [tool.mypy]` with `ignore_errors = true` for `app.routers.*`,
  `app.services.*`, `app.connectors.*`, `app.jobs.*`, `app.schemas.*` — which is what
  pre-commit/CI uses (`--config-file=pyproject.toml`) → "0 errors". This is what let
  P0.1 ship (mypy's `unused-coroutine` catches it). Fix: unify into `pyproject.toml`,
  delete `mypy.ini`, remove `ignore_errors` at least for `app.routers.*`, enable
  `check_untyped_defs = true`, burn down errors module-group by module-group
  (273 `no-any-return`, 81 `attr-defined`, 73 `union-attr`, 46 `arg-type`, …).
  **Fixed:** `mypy.ini` deleted; `pyproject.toml [tool.mypy]` is the single config.
  `ignore_errors` is gone everywhere; every module is now checked with
  `check_untyped_defs = true`, and `unused-coroutine` + `unused-awaitable` are enforced
  across the whole tree (verified: deleting an `await` in `app/routers/htmx_views.py` or
  discarding a `safe_background_task` Task fails the gate). Enabling the gate immediately
  caught two real bug families: (1) `app/services/attachment_parser.py` called
  `claude_structured(tool_schema=...)` — a stale kwarg + tool-definition wrapper that
  raised `TypeError` on every call and silently disabled AI column detection (fixed,
  regression test added); (2) 17 fire-and-forget `await safe_background_task(...)` sites
  discarded their Task while the helper held no strong ref — the P0.4 GC bug at scale
  (fixed once inside `app/utils/async_helpers.py`; call sites now `_ = await ...`).
  Fully re-enabled, zero suppressions: `app.schemas.*`, `app.connectors.*`, `app.cache.*`,
  `app.enrichment_service`, `app.scoring`, `app.utils.llm_router`, `app.utils.claude_client`.
  **Honest remaining debt** (per-module `disable_error_code` lists in `pyproject.toml`,
  all rooted in untyped `Column(...)` declarative models — migrate to `Mapped[]` to burn
  down): `app.routers.*` ~1,380 (arg-type 638, assignment 377, union-attr 232, …);
  `app.services.*` ~1,370 (assignment 540, arg-type 482, attr-defined 101, …);
  `app.search_service` ~100; `app.management.*` ~92; `app.models.*` ~56 (var-annotated 46);
  `app.email_service` ~50; `app.jobs.*` ~24; `app.startup` ~10; `app.utils.vendor_helpers` ~7.
  Also not yet enabled globally: `warn_return_any` (~273 `no-any-return` from the old
  `mypy.ini` wish-list). Dangerous codes stay live everywhere: `unused-coroutine`,
  `unused-awaitable`, `call-arg`, `no-redef`, `name-defined`, `unused-ignore`, `return`.
  Note: the pre-commit hook env (mypy 1.15.0, no project deps) and a full-deps run
  (mypy 2.1.0) disagree on which errors exist; cross-env-sensitive suppressions use
  `# type: ignore[code, unused-ignore]` so both stay green.

- [x] **P0.6 — `"complete"` vs `"completed"` status-string landmine.**
  `app/services/prospect_scheduler.py:260` sets `batch.status = "complete"` while
  `PendingBatchStatus.COMPLETED` is `"completed"`. Audit consumers of
  `DiscoveryBatch.status`, pick one value, add a `DiscoveryBatchStatus` StrEnum (see P2.5).
  Fixed: added `DiscoveryBatchStatus` (`running`/`completed`/`failed`) to
  `app/constants.py`; the only reader (`get_next_discovery_slice`'s "already ran this
  month" filter, same file) and the only writers now agree on `COMPLETED`. Caveat: any
  pre-existing production rows persisted with the old `"complete"` literal will no
  longer match the filter — no data migration was written for this (out of scope here);
  ops should check `SELECT count(*) FROM discovery_batches WHERE status = 'complete'`
  before relying on rotation continuity.

---

## Phase 1 — Security & data-safety hardening (~1 week)

- [ ] **P1.1 — Authenticate the ACS webhook.**
  `app/routers/v13_features/activity.py:139-185` accepts unauthenticated
  `CallCompleted`/`CallDisconnected` events (Graph/Teams webhooks validate HMAC
  `clientState`; ACS doesn't). Forged events pollute CRM call-activity timelines.
  Fix: shared-secret query param minted at Event Grid subscription time, compared with
  `hmac.compare_digest`, mirroring `webhook_service.validate_notifications`.

- [ ] **P1.2 — Scrub request bodies in Sentry `before_send`.**
  `app/main.py:72-106` scrubs headers/query-string/locals but never
  `event["request"]["data"]`. An unhandled exception in
  `PUT /api/sources/{name}/credentials` (`sources.py:753`) or `POST /auth/login`
  (plaintext `password` form field) ships raw secrets to Sentry SaaS. Fix: recursively
  mask keys matching `_SENSITIVE_VARS` in `request.data`, or set
  `max_request_body_size="never"`.

- [ ] **P1.3 — Rollback safety in deploy workflow.**
  `.github/workflows/deploy.yml:124-132` — on failed health check it unconditionally
  restores the pre-deploy dump, silently discarding all writes made during the deploy
  window, and recreates *all* services. Fix: take a pre-rollback safety dump first
  (pattern already exists in `scripts/restore.sh:174-214`); scope
  `docker compose up -d` to `app enrichment-worker`.

- [ ] **P1.4 — Container hardening.**
  - Add `USER appuser` as final `Dockerfile` directive (currently root-by-default for
    any `exec`/entrypoint override; `docker-entrypoint.sh` `runuser` stays for the
    migration step).
  - Move `gh` CLI (unpinned!) + Chromium/Playwright stack (`Dockerfile:33-49`) out of
    the internet-facing prod image into a separate tooling image.
  - Redis auth: `--requirepass ${REDIS_PASSWORD}` in `docker-compose.yml:49-70` +
    update `REDIS_URL`.
  - `stop_grace_period: 60s` for `enrichment-worker` (`docker-compose.yml:112-142`) —
    its graceful-shutdown handler (`worker.py:46`) is currently killed at 10s mid-batch.

- [ ] **P1.5 — Backup verification on a schedule.**
  Nothing ever runs `scripts/restore.sh --verify`; a corrupt backup could sit unnoticed
  for the full 30-day retention. Add a weekly systemd timer (or Actions cron over SSH)
  that verifies the newest dump and alerts on failure.

- [ ] **P1.6 — Rename `scripts/deploy.sh` → `scripts/bootstrap-server.sh`.**
  Two files named `deploy.sh` with wildly different behavior (one runs
  `docker compose down`); an operator mixup takes prod down.

---

## Phase 2 — Guardrails so classes of bugs can't recur (~1 week)

- [ ] **P2.1 — Extend ruff config** (`ruff.toml`, currently only E/F/W/I), in order:
  `ASYNC` (48 real hits, 0 false positives), `RUF006` (dangling tasks → P0.4),
  `T20` (with `"app/management/*" = ["T201"]` per-file-ignore),
  `B904` (121 missing `raise … from` — mechanical sweep first),
  `RUF100` (113 dead `# noqa` — `--fix` sweep first),
  `BLE001` (with per-file-ignores for the supplier fan-out orchestrators where broad
  catch is deliberate), then `UP` after a one-time `--fix` (733 `UP017`, 38 `UP032`).

- [ ] **P2.2 — Build the Docker image in CI.**
  `.github/workflows/ci.yml` never runs `docker build`; the first-ever build of a
  release image is the production deploy (`deploy.yml:96`). Add
  `docker compose build app` as a required PR gate.

- [ ] **P2.3 — Shard the CI `test` job.**
  The 22.7k-test suite needs ~24 min on a 2-vCPU runner; the timeout has been
  re-tuned three times as the suite grew (15 → 25 → 40, `ci.yml:32`) and will be
  outgrown again. Split into a 2-3 way matrix (`pytest-split` or directory
  buckets) so wall-clock drops to ~10 min and the timeout stops being a moving
  target. Pairs with P6.1 — retiring assertion-theater tests shrinks the
  runtime this sharding has to carry.

- [ ] **P2.4 — CI lint against assertion-theater tests.**
  Simple AST check flagging any test whose only assertion is a bare
  `status_code == 200` / `is not None`. (See P5.1 for the backfill.)

- [ ] **P2.5 — StrEnum enforcement.**
  - Add `SearchQueueStatus` (`queued/searching/completed/gated_out/pending` written raw
    in `search_worker_base/ai_gate.py:195-255`, `queue_manager.py:219-346`) and
    `DiscoveryBatchStatus` to `app/constants.py`; migrate call sites.
  - Enforce the existing-but-unused `OfferCondition` enum — 10+ sites hardcode `"new"`
    (`htmx/offers.py:221,282`, `htmx/requisitions.py:673`, `sightings.py:1394,2980,3027,3057`,
    `ai_offer_service.py:320`, `offer_qualification.py:133,196,235`, `schemas/crm.py:201`).
    Add a validator so raw strings can't slip past.
  - Pre-commit grep hook rejecting `\.status\s*=\s*"` outside `app/constants.py`.

- [ ] **P2.6 — Event-loop protection.**
  - Move the 14 blocking file-I/O sites in `async def` to `anyio` (worst:
    `tagging_ai_batch.py:128-458`, `tagging_ai_triage.py:233-246` — large JSONL files;
    also fixes the 2 unclosed file handles at `tagging_ai_batch.py:437`,
    `tagging_ai_triage.py:228`).
  - `htmx/requisitions.py:554` — run `openpyxl.load_workbook` in
    `anyio.to_thread.run_sync`; add an upload size cap.

- [ ] **P2.7 — Startup/health-check decoupling.**
  `app/main.py:124` runs ~20 sequential backfills/`ANALYZE` before `/health` can
  answer; on a prod-sized DB this can exceed both the compose healthcheck (~80s) and
  `deploy.sh`'s ~60s loop → false-failed deploys. Fix: split liveness (immediate) from
  readiness; add partial indexes on backfill `IS NULL` predicates; gate
  `_analyze_hot_tables` (`startup.py:1002-1005`) behind a since-last-deploy marker.

---

## Phase 3 — Performance (~1 week)

- [ ] **P3.1 — Index `requirements.assigned_buyer_id`** (`models/sourcing.py:163`, no
  index anywhere). Filtered on every buyer's default sightings board
  (`sightings.py:413,585`) and the offers alert source. One migration + `__table_args__`.

- [ ] **P3.2 — Batch the CSV contact-import lookups.**
  `htmx/companies.py:1025-1050` does up to ~2,000 sequential queries per 1,000-row
  import. Pre-fetch `CustomerSite` rows and `(site_id, email)` pairs in two queries,
  mirroring the batched pattern already used at `companies.py:837-840`.

- [ ] **P3.3 — Bulk `require_requisition_access`.**
  6 batch endpoints in `sightings.py` (1163, 1228, 1280, 1346, 2492, 2641) call it
  per-item in loops (up to 50 sequential `db.get()` for SALES/TRADER users). Add
  `require_requisition_access_bulk()` (single `IN (...)` select), reuse the documented
  `_manageable_company_ids` pattern.

- [ ] **P3.4 — (Opportunistic) batch phone-match `db.get()` chains** in
  `activity_service.py:244-320` if ever used for bulk reconciliation; bounded and fine
  today.

---

## Phase 4 — Structural refactors (staged, ~3-4 weeks)

Do these after Phases 0-2 so the new guardrails protect the refactor.

- [ ] **P4.1 — Fix inverted layering (services importing router privates).**
  `buyer_affinity_service.py:153`, `quote_builder_service.py:216,276`,
  `health_monitor.py:150` lazily import `_private` helpers from routers. Move the
  helpers into services (`vendor_reachability.py`, `pricing_history.py`,
  `connector_registry.py`); both sides import the service. Also fixes the two
  cross-router imports (`htmx/offers.py:59`, `htmx/archive.py:45-46`).

- [ ] **P4.2 — Extract business logic from routers (quick, self-contained).**
  - CSV import (~450 lines): `companies.py:620-1089` → `services/company_import_service.py`.
  - Offer ingestion: `offers.py:190-301` → consolidate into existing
    `services/ai_offer_service.py`.

- [ ] **P4.3 — Split the god files along their audited seams** (one PR per split;
  re-export from a package `__init__.py` so callers don't all change at once):
  - `routers/htmx/companies.py` (5,234 lines) → ~8 modules: import, saved views,
    contacts, tags/segments, merge, custom fields, sites, detail-tab render.
  - `services/buyplan_workflow.py` (1,855) → `buyplan_approval / buyplan_lines /
    buyplan_po / buyplan_reports`.
  - `routers/htmx_views.py` (2,063) → `htmx/my_day.py, email_views.py,
    insights_views.py, search_views.py`.
  - `routers/htmx/offers.py` (1,905) → `offers_crud / rfq_compose / follow_ups /
    reply_handling`.

- [ ] **P4.4 — Shared fuzzy-dedup helper.** `vendor_duplicates.py:51-75` and
  `company_utils.py:154-227` copy-paste the rapidfuzz fallback loop; extract
  `fuzzy_dedup_scan(rows, normalize_fn, threshold, limit)`.

- [ ] **P4.5 — `spec_tiers.recategorize()` entry point** so
  `management/cleanup_known_bad.py:173` (direct `card.category` write) can go through
  the ladder like everything else.

- [ ] **P4.6 — Hoist needless function-local imports** (~180 across the big htmx
  routers; verified no real cycles). Fold into each P4.3 split rather than standalone.

---

## Phase 5 — Frontend consolidation (~1 week)

- [ ] **P5.1 — `lazy_body(id, url)` macro** so the `hx-target` guard (P0.3's root
  cause) is enforced structurally, not by "LANDMINE" comments. Migrate
  `approvals_hub.html`, `buy_plans/hub.html`, `settings/index.html`, `sightings/list.html`.

- [ ] **P5.2 — Kill the `fetch()` violations in `htmx_app.js`** (~16 sites).
  Convert `fetchCompanies()` (:1637) and `searchVendors()` (:2391) to server-rendered
  `hx-get` debounced dropdowns (pattern: `materials/workspace.html`); wrap the 5
  JSON-POST sites (trouble tickets, call-outcome, outreach, quote-builder save) in one
  `postJSON()` helper over `htmx.ajax`.

- [ ] **P5.3 — Empty-state dedup.** 11 templates hand-roll the markup that
  `shared/empty_state.html` already provides (vendors/list, requisitions/list,
  emails/*, follow_ups, offers/review_queue, proactive, prospecting, rfq_compose,
  search/full_results, vendors/contacts_list).

- [ ] **P5.4 — Single-quote the `tojson` attributes** in `quote_builder/modal.html:10`
  and `requisitions/rfq_compose.html:44` (latent Alpine-breakage per CLAUDE.md).

- [ ] **P5.5 — Replace `_x_dataStack` in `tests/e2e/test_navigation_smoke.py:38`**
  with an `Alpine.store('nav')` read or `data-current-view` attribute.

---

## Phase 6 — Test-suite trustworthiness (ongoing, start now)

- [ ] **P6.1 — Retrofit the 542 status-200-only tests** (14.3% of the nightly/coverage
  files; suite-wide 971/18,709). Seed matching + non-matching rows, assert rendered
  content. Start with `test_coverage_nightly_2026_06_30.py:211-277` (sourcing filters)
  and the `test_htmx_views_nightly{1..30}.py` series. Gate recurrence via P2.4.

- [ ] **P6.2 — Close the Postgres blind spot.** 59 modules use
  ILIKE/JSONB/tsvector/pg_trgm; only 3 test files use `requires_postgres`. Priority:
  `vendor_duplicates.py` (pg_trgm ranking — currently "tested" via a full ORM-chain
  mock at `test_vendor_duplicates.py:159-188`) and `faceted_search_service.py:430-608`
  (FTS ranking — zero real coverage). Track the checklist in `docs/APP_MAP_DATABASE.md`.

- [ ] **P6.3 — Replace whole-session `MagicMock()` tests** (11 files) with real
  `db_session` SQLite fixtures where expressible; keep mocks only for PG-only branches.

- [ ] **P6.4 — De-flake `test_circuit_breaker.py:78-82`** (50ms margin under xdist);
  inject a fake monotonic clock.

- [ ] **P6.5 — Direct unit tests for `can_review_qp_sales_section` /
  `can_review_qp_purchasing_section`** (`dependencies.py:382-400`, zero direct tests).

---

## Explicitly verified clean (no action needed)

- SQL injection: none (only guarded `text()` with hardcoded identifiers + bound params)
- CSRF: double-submit middleware correctly scoped; `/metrics` fails closed
- XSS: all `|safe` piped through `nh3` or template-author literals
- SSRF: datasheet fetcher blocks private IPs per redirect hop
- Path traversal: realpath containment on all file-serving endpoints
- IDOR: ownership-scoped deps throughout; 404-not-403 on denial
- No `db.query(Model).get()`, no Pydantic `class Config`, no bare `except:`, no
  `print()` outside `app/management/`, single Alembic head, all timestamps `UTCDateTime`,
  `search_requirement()` stale-session contract honored at both call sites

## Sequencing summary

```
Week 1:  Phase 0 (all)  +  P1.1, P1.2
Week 2:  Phase 1 rest   +  P2.1, P2.2, P2.4
Week 3:  P2.3, P2.5-P2.7  +  Phase 3
Weeks 4-6: Phase 4 (one god-file split per PR)  ∥  Phase 5
Ongoing: Phase 6 (fold P6.1 retrofits into every PR touching those areas)
```
