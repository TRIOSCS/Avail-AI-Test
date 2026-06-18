# Prospecting Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the two highest-value prospecting follow-ups from the 2026-06-17 consolidation: move on-demand enrichment off the request path (background + polling), and make the card grid stay consistent after claim/dismiss/release (remove cards that leave the active filter + keep the stats KPIs live).

**Architecture:** Both phases are server-rendered HTMX. Phase 1 spawns a fire-and-forget background task (existing `safe_background_task`) that owns its own DB session, stamps an `enrich_status` into `enrichment_data`, and the detail page polls a status endpoint until done. Phase 2 returns out-of-band (OOB) HTMX fragments from the grid action routes so a single response can both update/remove the acted-on card and refresh the stats panel.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 (sync), HTMX, Alpine, Jinja, Tailwind (brand palette). Tests: pytest + FastAPI TestClient against SQLite (conftest fixtures).

## Global Constraints

- HTMX-only UI (no React/SPA). Follow existing prospecting template patterns.
- No DB migration in this work — the alembic chain is contended by concurrent sessions; do not add one. `buyer_ready_score` stays a Python composite (`build_priority_snapshot`).
- Routers stay thin; orchestration lives in services (`app/services/prospect_*`).
- Background coroutines MUST open their own `SessionLocal` (never the request-scoped `db`) per `safe_background_task`'s contract.
- Prospect write routes require `require_buyer` (agent excluded) — keep it.
- "Buyer ready" has exactly one definition: `app/services/prospect_priority.build_priority_snapshot`.
- Run the full suite (`pytest`, xdist) before declaring done; `pre-commit run --files <changed>` must pass.

---

## File Structure

- `app/services/prospect_free_enrichment.py` — add `run_enrichment_job(prospect_id)` orchestrator (own session, status stamping, free enrichment + warm-intro). One responsibility: "do a full enrichment pass for one prospect, recording status."
- `app/routers/htmx_views.py` — rewrite `enrich_prospect_htmx` (spawn bg + return poller); add `enrich_status_partial` (GET poll endpoint); add OOB logic to claim/dismiss/release grid path; add `_prospect_stats_ctx` + `_grid_action_response` helpers.
- `app/templates/htmx/partials/prospecting/enrich_status.html` — NEW: poller / done-loader / error states.
- `app/templates/htmx/partials/prospecting/detail.html` — add `#enrich-status-zone`; Enrich button targets it; resume poller if already running.
- `app/templates/htmx/partials/prospecting/stats.html` — wrap in `#prospect-stats` is done in list.html; stats grid unchanged.
- `app/templates/htmx/partials/prospecting/list.html` — give the stats container a stable id `#prospect-stats`.
- `app/templates/htmx/partials/prospecting/_action_oob.html` — NEW: optional card + OOB stats wrapper for grid actions.
- `app/templates/htmx/partials/prospecting/_card.html` — grid action buttons carry the active filter via `hx-vals` so the route can decide removal.
- `tests/test_prospecting_tab.py` — add Phase 1 + Phase 2 tests.

---

## PHASE 1 — Background enrichment + polling

`enrich_status` lives in `enrichment_data["enrich_status"]` ∈ {`running`, `done`, `error`}.

### Task 1.1: `run_enrichment_job` service orchestrator

**Files:**
- Modify: `app/services/prospect_free_enrichment.py`
- Test: `tests/test_prospecting_tab.py`

**Interfaces:**
- Produces: `async def run_enrichment_job(prospect_id: int) -> None` — opens its own `SessionLocal`; runs `run_free_enrichment(prospect_id, db=session)` then `detect_warm_intros`/`generate_one_liner`; sets `enrichment_data["enrich_status"]="done"` on success, `"error"` on any failure; always commits + closes. Never raises (safe for fire-and-forget).

- [ ] Step 1: Write failing test `test_run_enrichment_job_marks_done` — seed a prospect, patch `run_free_enrichment` (AsyncMock) + warm-intro fns, `await run_enrichment_job(p.id)`, assert `enrichment_data["enrich_status"] == "done"`.
- [ ] Step 2: Write failing test `test_run_enrichment_job_marks_error` — patch `run_free_enrichment` to raise, assert status `"error"`, no exception propagates.
- [ ] Step 3: Run both, verify they fail (function missing).
- [ ] Step 4: Implement `run_enrichment_job` (own `SessionLocal`, try/except, status stamping).
- [ ] Step 5: Run tests → pass.
- [ ] Step 6: Commit.

### Task 1.2: rewrite `enrich_prospect_htmx` to spawn + return poller

**Files:**
- Modify: `app/routers/htmx_views.py` (`enrich_prospect_htmx`)
- Create: `app/templates/htmx/partials/prospecting/enrich_status.html`
- Modify: `app/templates/htmx/partials/prospecting/detail.html`
- Test: `tests/test_prospecting_tab.py`

**Interfaces:**
- POST `/v2/partials/prospecting/{id}/enrich` → require_buyer. If `enrich_status == "running"`, do not re-spawn. Else set `running`, commit, `safe_background_task(run_enrichment_job(id))`, return `enrich_status.html` (poller) — 200. Target from the button: `#enrich-status-zone` (innerHTML).
- `enrich_status.html` states: running → a `<div hx-get=".../enrich-status" hx-trigger="every 2s" hx-target="this" hx-swap="outerHTML">` spinner; done → `<div hx-get=".../{id}" hx-trigger="load" hx-target="#main-content" hx-swap="innerHTML">` one-shot reload (poller gone); error → message + a retry button (re-POST enrich).

- [ ] Step 1: Write failing test `test_enrich_spawns_bg_and_returns_poller` — patch `run_enrichment_job` (AsyncMock); POST enrich; assert 200, response contains the poll URL `enrich-status`, and DB `enrich_status == "running"`, and `run_enrichment_job` was awaited/scheduled.
- [ ] Step 2: Write failing test `test_enrich_while_running_does_not_respawn` — set `enrich_status="running"`; POST enrich; assert `run_enrichment_job` NOT called again (still returns poller).
- [ ] Step 3: Run, verify fail.
- [ ] Step 4: Implement route + `enrich_status.html` (running branch) + detail.html `#enrich-status-zone` + button retarget.
- [ ] Step 5: Run tests → pass.
- [ ] Step 6: Commit.

### Task 1.3: `enrich_status` poll endpoint

**Files:**
- Modify: `app/routers/htmx_views.py` (new `enrich_status_partial`)
- Test: `tests/test_prospecting_tab.py`

**Interfaces:**
- GET `/v2/partials/prospecting/{id}/enrich-status` → require_buyer → renders `enrich_status.html` for the current `enrich_status` (running → keep polling; done → one-shot detail reloader + success toast; error → error fragment + warning toast). 404 if prospect missing.

- [ ] Step 1: Write failing test `test_enrich_status_running_keeps_polling` — `enrich_status="running"` → response contains `every 2s` / poll URL.
- [ ] Step 2: Write failing test `test_enrich_status_done_reloads_detail` — `enrich_status="done"` → response contains a `hx-get` to the detail URL targeting `#main-content`, NOT the `every 2s` poller; HX-Trigger has a success toast.
- [ ] Step 3: Write failing test `test_enrich_status_error` — `enrich_status="error"` → error text + warning toast.
- [ ] Step 4: Run, verify fail. Implement route + the done/error branches of `enrich_status.html`.
- [ ] Step 5: Run tests → pass. Commit.

### Task 1.4: route ordering + integration check

**Files:** `app/routers/htmx_views.py`

- [ ] Step 1: Ensure `/v2/partials/prospecting/{id}/enrich-status` is registered (the `{prospect_id}` GET catch-all must not shadow it — it won't, different suffix, but the `prospecting/stats` precedence comment applies; place the new GET before the `{prospect_id}` detail route or confirm FastAPI specificity). Add a test `test_enrich_status_route_not_shadowed` hitting it returns the poller not the detail.
- [ ] Step 2: Run the prospecting test files → all pass. Commit.

---

## PHASE 2 — Live grid consistency (OOB card removal + stats refresh)

After a grid claim/dismiss/release, the acted-on card is removed if its new status leaves the active filter, and the stats KPI panel refreshes — in one OOB response. Detail-page actions are unchanged (they return the full detail).

### Task 2.1: stats context helper + stable stats id

**Files:**
- Modify: `app/routers/htmx_views.py` (extract `_prospect_stats_ctx(db) -> dict`)
- Modify: `app/templates/htmx/partials/prospecting/list.html` (stats container id `#prospect-stats`)
- Test: `tests/test_prospecting_tab.py`

**Interfaces:**
- Produces: `_prospect_stats_ctx(db) -> {"total","buyer_ready","call_now","claimed"}` (the canonical computation currently inline in `prospecting_stats`). `prospecting_stats` route uses it.

- [ ] Step 1: Refactor `prospecting_stats` to use `_prospect_stats_ctx`; give the list.html stats container `id="prospect-stats"`. Existing `test_stats_panel_renders_canonical_labels` still passes.
- [ ] Step 2: Run → pass. Commit.

### Task 2.2: filter-aware grid actions (card removal + OOB stats)

**Files:**
- Modify: `app/routers/htmx_views.py` (claim/dismiss/release grid branch)
- Create: `app/templates/htmx/partials/prospecting/_action_oob.html`
- Modify: `app/templates/htmx/partials/prospecting/_card.html` (action buttons send `flt_status` via hx-vals)
- Test: `tests/test_prospecting_tab.py`

**Interfaces:**
- `_card.html` grid Claim/Dismiss buttons add `hx-vals='{"flt_status": "{{ status }}"}'` (active filter; empty string = default All).
- Grid action routes read form `flt_status`; compute `visible = _status_visible_under_filter(new_status, flt_status)` where default (`""`) shows {suggested, claimed}, else shows only the named status.
- `_action_oob.html`: renders the updated `_card.html` only if `visible`, plus always `<div id="prospect-stats" hx-swap-oob="innerHTML">{stats grid}</div>`. When not visible, the card target (#prospect-{id}, outerHTML) receives empty → removed.

- [ ] Step 1: Write failing test `test_dismiss_in_suggested_filter_removes_card` — POST dismiss with `flt_status="suggested"`, `HX-Target: prospect-<id>` (grid); assert response does NOT contain `id="prospect-<id>"` card body (removed) but DOES contain `id="prospect-stats"` OOB.
- [ ] Step 2: Write failing test `test_claim_in_all_filter_keeps_card` — POST claim with `flt_status=""`; claimed is visible under All → response contains the updated card (now "Claimed") + OOB stats.
- [ ] Step 3: Write failing test `test_detail_action_unchanged` — POST dismiss with `HX-Target: main-content` → returns full detail (no OOB stats wrapper). (Guards the detail path.)
- [ ] Step 4: Run, verify fail. Implement `_status_visible_under_filter`, `_action_oob.html`, route branching (grid → `_action_oob.html`; detail → detail.html as today), and `_card.html` hx-vals.
- [ ] Step 5: Run tests → pass. Commit.

### Task 2.3: regression sweep

- [ ] Step 1: Run all prospecting test files + the htmx route files I touched → green.
- [ ] Step 2: Full suite (xdist). Commit any fixes.

---

## Deferred (considered, intentionally not in this pass — with rationale)

- **Persist `buyer_ready_score`/`is_buyer_ready` columns (SQL sort/stats):** needs a migration; the alembic chain is contended by concurrent sessions and the O(N) python path is negligible at current pool size. Revisit when the pool exceeds ~1k prospects (e.g., after the SFDC import) or when the chain quiets.
- **Wire `apply_historical_bonus` into discovery scoring:** inert until SFDC import exists (no near-term date); the year-cutoff bug is already fixed. Wire it when SF history data lands so it can be validated against real data.
- **Warm-intro lookup pg_trgm index:** the leading-wildcard ILIKE seq-scans `sightings`; once enrichment is backgrounded (Phase 1) this is off the request path, and it needs a migration. Revisit with Phase-2-deferred migration batch.

---

## Self-Review

- Spec coverage: Phase 1 (deferred background-enrich) ✓; Phase 2 (grid consistency, the "low" UX finding) ✓; deferred items documented ✓.
- Placeholder scan: none — each task names files, interfaces, and test names.
- Type consistency: `run_enrichment_job(prospect_id:int)->None`, `_prospect_stats_ctx(db)->dict`, `_status_visible_under_filter(new_status, flt_status)->bool` used consistently.
