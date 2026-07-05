# Unfinished / Unstarted Work Inventory — 2026-07-05

Discovery sweep (6 read-only lenses: code markers, orphaned endpoints, half-built
features/schema, tracked backlog, skipped tests, doc drift). 59 findings. Full per-agent
detail: `subagents/workflows/wf_48b75666-116/journal.jsonl`.

> **Note:** several "module rework" findings are STALE — the discovery agents read the
> backlog, which *understates* completion (a doc-drift finding itself). Prospecting tier-1
> fixes (H8/M12/M15/M16/M18/M19/M8), Resell L2/L3/M6/M9, Tasks My-Day actions, and the
> whole common-sense audit were already SHIPPED. Treat those as done; see the doc-drift row.

## 🔴 REAL BUGS (recurring / user-facing) — fix now, both small
1. **Quarterly email re-verification job crashes on EVERY run.** `email_jobs.py:382` calls `run_email_reverification(db, max_contacts=200)` but the param is `_max_contacts` → `TypeError` re-raised to Sentry each quarterly run. Fix the kwarg (or unschedule the stub job — its body is a no-op).
2. **Dashboard "Refresh insights" button 404s.** `insights_panel.html` posts `/v2/partials/dashboard/0/insights/refresh` (unregistered); the real route is `/v2/partials/dashboard/pipeline-insights/refresh` (`htmx_views.py:1720`). Special-case the panel's refresh URL.

## 🟡 ORPHANED ENDPOINTS — built but unwired (each: finish-the-feature OR delete). Mostly small.
- **Wire-or-delete (real half-features):** per-requirement sightings assign (`sightings.py:1520`), log-phone-call (`offers.py:1749`), vendor-response status control (`offers.py:1918`), vendor contact-nudges panel (`vendors.py:1279`), vendor contact-timeline (`vendors.py:920`), reports_to contact picker (`companies.py:1539`), sourcing lead-feedback, nav follow-up badge.
- **Delete (superseded dupes):** `admin_data_ops` (→ settings/data-ops), `proactive_send_legacy` (→ /v2/proactive/send), `rfq_prepare_panel` (→ rfq-compose), the parallel Approvals REST cluster (5 endpoints), settings api-keys/sources redirect shims.

## 🟠 HALF-BUILT FEATURES — decide build-or-drop
- **Customer contact-enrichment is a permanent no-op stub** still wired into live auto-enrich (`customer_enrichment_service.py:87`, called from `companies.py:467`) → silently adds 0 contacts. Rewire to the live `gather_contacts` path OR delete the dead call + `customer_enrichment_*` settings.
- **Offer attribution lifecycle half-built** — `attribution_status` only ever `active` (expired/converted dead). Build the transitions or collapse to a boolean.
- **CRM Phase-5b Reporting/forecast page never wired** — the forecast rollup engine is dead code. Build the Reporting page or drop the rollups.

## 🔵 DEAD SCHEMA — one decide-then-drop migration
Dead enum members (RiskFlagSeverity/RiskFlagType, QualityPlanStatus IN_REVIEW/APPROVED/REJECTED, QPOrderType.REVISION, SourcingType, Offer.attribution expired/converted) + the ~5 unused columns (QP/Approval "Task-2" offer columns, etc.). Round-trip one reviewed Alembic migration on throwaway PG. (I deferred the column-drops earlier for exactly this consolidation.)

## 🟣 SKIPPED-TEST / COVERAGE GAPS
- Contact-dedup tests unconditionally skipped (merge logic untested). E2E degrades to *skips* on missing data → a broken page reads green. Performance (avail-score) + multiplier scoring APIs MVP-gated + untested. PG-only vendor list/search paths + pg_trgm/jsonb migrations verified nowhere (SQLite masks PG). → add a Postgres-backed CI job + deterministic e2e fixtures.

## 📄 DOC DRIFT (6) — quick, worth doing
Tracking docs understate completion; APP_MAP missing migration 185 (`requirements.outcome_reason`), the CSV-export helper + endpoints, the async-run/self-poller pattern, and the Hot List toggle; CLAUDE.md still names Apollo as an active provider. → one reconciliation pass.

## ⏸ ALREADY IN OUR PLAN (decision-bound)
- **Idea O** (prospecting Claim/Dismiss + manager Assign) — **building now**.
- **Idea I** (buy-plan line editing) — folds into the buy-plan epic (next).
- **Idea C** (score/price hover) — decided (deterministic factor breakdown); build after buy-plan.
- **SP4 manual "Park in prospecting" UI** — never built (auto-sweep backbone exists).
- **Approvals bulk-approve** — parked under "leave Approvals unchanged".

## 🛣 ROADMAP (multi-day / post-go-live)
API-search Phases 1-4 (product core; Phase 1 is the highest-confidence next batch); HIGH-BE-11 (`db.query()`→2.0, ~1,561 callsites — land a lint guard first, migrate in waves); HIGH-SEC-4 (Graph-webhook IP allowlist); CRM redesign Phases 1-6; vendor-API parametric enrichment (MOSFET extractor, blocked on inventory); calendar delta sync; KB insight refresh (disabled for AI cost).

## 🚫 BLOCKED — external / user (no code)
Enrichment-provider replacements + Sourcengine/Explorium/eBay keys; SFDC import + March enrichment recovery; launch config (disable password login, DO Spaces backup, 3 prepay notify keys, datasheet SharePoint).
