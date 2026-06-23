# The Broker Forum (TBF) Browser-Worker — Design & Implementation Spec

**Date:** 2026-06-23  **Status:** Approved (design + capture method)

## Goal
Add a host browser-worker for **The Broker Forum (thebrokersite.com)** — a European
broker marketplace — that logs in with the user's paid-member credentials, searches
queued MPNs, and ingests results as `Sighting(source_type="thebrokersite")`. It mirrors
the existing **ICSource** worker (async, browser-only via Patchright on Xvfb) and reuses
the shared `app/services/search_worker_base/` framework as-is.

## Two phases
- **Phase 1 (this spec — build blind, ships DORMANT):** the full scaffold + DB + ops +
  registration + tests. Needs zero knowledge of thebrokersite's HTML. Mergeable/deployable
  on its own (worker stays idle until creds + selectors exist).
- **Phase 2 (separate, needs a logged-in capture):** finalize the 4 site-specific files'
  selectors (`session_manager`, `search_engine`, `result_parser`, `circuit_breaker`) from
  real authenticated HTML captured on the host, add parser fixtures + tests, then live ramp.

## Global constraints (CLAUDE.md)
- All schema via Alembic; `startup.py` is runtime-only (no DDL). Single head after.
- `db.get(Model, id)`, SQLAlchemy 2.0 style. Loguru, not print. Ruff + mypy clean.
- Every new file gets a header comment (what it does, who calls it, deps).
- Always include tests. `source_type` string is exactly `thebrokersite` everywhere.

## Phase 1 — files to CREATE (copy the ICSource worker; rename Ics→Tbf, ics→tbf, source_type='thebrokersite')

Package `app/services/tbf_worker/` (template: `app/services/ics_worker/`):
- `__init__.py` — re-export TbfConfig, TbfSessionManager, save_tbf_sightings.
- `__main__.py` — `asyncio.run(main())`.
- `config.py` — `TbfConfig` reading env: `TBF_USERNAME`, `TBF_PASSWORD` (member login, no account#),
  `TBF_MAX_DAILY_SEARCHES`=50, `TBF_MAX_HOURLY_SEARCHES`=10, `TBF_MIN_DELAY_SECONDS`=180,
  `TBF_MAX_DELAY_SECONDS`=600, `TBF_TYPICAL_DELAY_SECONDS`=300, `TBF_DEDUP_WINDOW_DAYS`=7,
  `TBF_BUSINESS_HOURS_START`=8, `TBF_BUSINESS_HOURS_END`=18, `TBF_BROWSER_PROFILE_DIR`=/root/tbf_browser_profile,
  `TBF_SEARCH_TIMEOUT_SECONDS`=150, `TBF_BREAKER_COOLDOWN_MINUTES`=30. Keep attribute names ICS-identical.
- `scheduler.py`, `human_behavior.py`, `mpn_normalizer.py`, `monitoring.py` — thin copies of the ICS
  equivalents (re-exports of the shared base; `monitoring` component_name='thebrokersite_worker').
- `queue_manager.py` — thin wrapper: `QueueManager(queue_model=TbfSearchQueue, source_type='thebrokersite',
  dedup_window_days=..., log_prefix='TBF')`; expose `enqueue_for_tbf_search`, `recover_stale_searches`,
  `claim_next_queued_item`, `get_next_queued_item`, `reclaim_stuck_searches`, `mark_status`,
  `mark_completed`, `get_queue_stats`.
- `ai_gate.py` — standalone module copying `ics_worker/ai_gate.py` (in-memory cache; reuse ICS prompt;
  query `TbfSearchQueue.status=='pending'`; fail-open to 'queued'). NO classification_cache DB table.
- `sighting_writer.py` — `save_tbf_sightings(db, queue_item, tbf_sightings)`: dedup by
  (vendor_name_normalized, mpn_matched, qty_available) over source_type='thebrokersite';
  `Sighting(source_type='thebrokersite', confidence=0.6 if in_stock else 0.3, currency, vendor_email/phone,
  raw_data={region,country,inventory_type,uploaded_date,vendor_company_id,supplier_product_url,price_breaks})`;
  `apply_to_fresh_sightings(db, req, created)` BEFORE commit; `rebuild_vendor_summaries_from_sightings` AFTER.
- `worker.py` — async `main()` loop copied from `ics_worker/worker.py` (NOT nc_worker — that's HTTP-hybrid):
  startup recover_stale → ai_gate → claim → search → parse → save → log → mark_completed → heartbeat every tick;
  SIGTERM/SIGINT graceful; `asyncio.wait_for(search_part, TBF_SEARCH_TIMEOUT_SECONDS)`; SESSION_EXPIRED→requeue.
- **Phase-2 STUBS (scaffold now, selectors filled in Phase 2 — mark each with `# TODO(phase2): real selector`):**
  - `session_manager.py` — `TbfSessionManager` (Patchright persistent context, channel='chrome', headless=False,
    DISPLAY guard; `start/login/check_session_health/ensure_session/stop`, `is_logged_in`). Login/marker selectors stubbed.
  - `search_engine.py` — `search_part(page, mpn) -> {'html','url','duration_ms','status_code'}`; capture outerHTML.
    Search route/field/submit/results-wait selectors stubbed.
  - `result_parser.py` — `@dataclass TbfSighting(part_number, manufacturer, date_code, description, quantity,
    price, currency, vendor_name, vendor_email, vendor_phone, vendor_company_id, country, region, in_stock,
    is_authorized, uploaded_date, supplier_product_url)`; `parse_results_html(html)->list[TbfSighting]` via
    BeautifulSoup, defensive per-row skip. Column selectors stubbed (return [] until Phase 2).
  - `circuit_breaker.py` — `CircuitBreaker(CircuitBreakerBase)` + `check_page_health(page)`; expired/anti-scrape
    markers stubbed (default to base empty-streak/error trips).
- `README.md` — copy ics README; document the 3-week ramp + creds + `journalctl -u avail-tbf-worker`.

Models (template: `app/models/nc_*` — NC, for the COMPOUND dedup constraint):
- `app/models/tbf_search_queue.py` — `TbfSearchQueue` (table `tbf_search_queue`), copy `nc_search_queue.py`:
  compound `UniqueConstraint('requirement_id','normalized_mpn', name='uq_tbf_queue_requirement_mpn')`,
  `ix_tbf_queue_poll (status,priority,created_at) WHERE status='queued'`,
  `ix_tbf_queue_dedup (normalized_mpn, last_searched_at DESC) WHERE status='completed'`.
- `app/models/tbf_search_log.py` — `TbfSearchLog` (table `tbf_search_log`), copy `nc_search_log.py`.
- `app/models/tbf_worker_status.py` — `TbfWorkerStatus` singleton (table `tbf_worker_status`,
  `CheckConstraint('id = 1')`, UTCDateTime(timezone=True)), copy `nc_worker_status.py`.
- Register all three in `app/models/__init__.py`.

Migration:
- `alembic/versions/130_tbf_search_tables.py` — `revision='130_tbf_search_tables'`,
  `down_revision='129_drop_bid_tables'` (verified single head). Create the 3 tables (+ the constraint/indexes),
  then `op.execute("INSERT INTO tbf_worker_status (id) VALUES (1)")`. Full reversible `downgrade()`.
  Append a claim line to `MIGRATION_NUMBERS_IN_FLIGHT.txt`. Test upgrade→downgrade→upgrade; `alembic heads`==1.

Ops:
- `deploy/avail-tbf-worker.service` — copy `deploy/avail-nc-worker.service`; ExecStart
  `/root/availai/.venv/bin/python -m app.services.tbf_worker.worker`; EnvironmentFile `.env.tbf-worker`;
  Requires/After `avail-xvfb.service`; DISPLAY=:99; Restart=always RestartSec=300; MemoryMax=2G CPUQuota=50%.
- `scripts/setup_tbf_worker.sh` — copy `scripts/setup_nc_worker.sh` (Xvfb+Chrome, venv, `patchright install chrome`,
  mkdir profile + /var/log/avail-tbf, install+enable unit, chmod 600 .env.tbf-worker).
- `.env.tbf-worker.example` — committed template (real file host-side, chmod 600, never committed).
- `deploy.sh` — add `avail-tbf-worker` to the host-worker restart loop.

Registration:
- `app/constants.py` — add `'thebrokersite'` to `BROWSER_WORKER_SOURCES`.
- `app/services/connector_service.py` — add `'thebrokersite'` to `_BROWSER`; REMOVE from `_PLANNED`.
- `app/data/api_sources.json` — thebrokersite entry: set env_vars `['TBF_USERNAME','TBF_PASSWORD']`, rewrite
  setup_notes to the live ICS-style note.
- `app/search_service.py` — import + call `enqueue_for_tbf_search` at BOTH fanout sites (per-call best-effort
  block AND the spec-code AVL loop), mirroring the ICS/NC calls exactly.
- `app/jobs/worker_liveness_jobs.py` — add `('The Broker Forum', TbfWorkerStatus)` to the checks tuple.
- `app/routers/admin/system.py` — add TbfWorkerStatus + tbf queue stats to `/api/admin/workers/status`.
- `app/startup.py` — add `seed_tbf_worker_status_singleton(db)` called from `seed_browser_workers()` (idempotent;
  migration INSERT is primary). `seed_browser_worker_sources` auto-flips the source LIVE via BROWSER_WORKER_SOURCES.
- `app/services/sourcing_leads.py` — add `'thebrokersite'` to the broker-tier sets (reliability base 72) so TBF
  sightings score/aggregate like NC/ICS.

## Phase 1 tests (`tests/test_tbf_worker.py`, TESTING=1 / in-memory SQLite)
- `TbfConfig` env defaults.
- `queue_manager` enqueue + compound (requirement_id, normalized_mpn) dedup + claim atomicity.
- `save_tbf_sightings`: synthetic `TbfSighting` list → Sighting rows with source_type='thebrokersite',
  dedup, `apply_to_fresh_sightings` gating.
- worker_status singleton seeded (migration).
- (Parser/search/login selector tests are Phase 2, against real HTML fixtures.)

## Phase 2 (later, after a logged-in capture)
Capture login/search/results/logged-out HTML on the host (user's creds in `.env.tbf-worker`); fill the 4
stub files' selectors; add `tests/fixtures/tbf_*.html` + parser assertions; tune currency (EUR/USD/GBP);
encode circuit-breaker markers; live ramp from 50/day. Open questions (login mechanism, auth marker, currency,
result layout, vendor-contact surfacing, rate-limit behavior) resolve from the capture.

## Cost / safety
Free (membership); conservatively rate-limited; ships dormant (idle until creds + selectors). Credentials live
only in the host `.env.tbf-worker` (chmod 600), never in the DB or git.
