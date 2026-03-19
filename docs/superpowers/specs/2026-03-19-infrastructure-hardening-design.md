# Infrastructure Hardening & Acceleration Plan

**Date:** 2026-03-19
**Scope:** 8 improvements to stability, performance, maintainability, cost, and UX
**Constraint:** All changes must slot into existing patterns — no architectural changes, no breaking changes

---

## Overview

An audit of the AvailAI codebase identified 8 areas where existing infrastructure is underutilized, code is duplicated, or drop-in improvements are available. This spec defines each improvement in priority order, with the hard rule that everything flows into what's already built.

---

## 1. Critical Safety — `safe_background_task()` Wrap

### Problem
~19 raw `asyncio.create_task()` calls across 10 files with zero error isolation. If any background enrichment, tagging, or notification task throws an unhandled exception, it can crash the event loop or surface as an unhandled promise rejection.

### Solution
Replace every `asyncio.create_task(coro)` with `safe_background_task(coro, "task_name")`. The function already exists in `app/utils/async_helpers.py`, is tested, but is never called in production.

### Files to Modify
- `app/routers/tagging_admin.py` — 10 calls
- `app/routers/materials.py` — 1 call
- `app/routers/crm/companies.py` — 1 call
- `app/routers/crm/sites.py` — 1 call
- `app/routers/crm/offers.py` — 1 call
- `app/routers/vendor_contacts.py` — 1 call
- `app/routers/prospect_suggested.py` — 1 call
- `app/services/buyplan_notifications.py` — 1 call
- `app/main.py` — 1 call (startup cache warming)
- `app/search_service.py` — 1 call

### Risk
Near zero. `safe_background_task` is a strict superset of `asyncio.create_task` with error catching added. Behavior is identical on success, safer on failure.

---

## 2. Drop-in Performance — `rapidfuzz` and `orjson`

### 2a: `rapidfuzz` Swap

**Problem:** `thefuzz[speedup]` is 10-100x slower than `rapidfuzz` for fuzzy string matching.

**Solution:** Replace `thefuzz[speedup]==0.22.1` with `rapidfuzz` in `requirements.txt`. Update 5 import sites:
- `app/utils/vendor_helpers.py` — conditional import with fallback
- `app/routers/vendors_crud.py` — `from thefuzz import fuzz`
- `app/company_utils.py` — `from thefuzz import fuzz`
- `app/services/auto_dedup_service.py` — conditional import
- `app/vendor_utils.py` — 2 occurrences of `from thefuzz import fuzz`

`rapidfuzz` provides API-compatible `fuzz.ratio()`, `fuzz.partial_ratio()`, `fuzz.token_sort_ratio()` on the same 0-100 scale. Existing thresholds (82% in vendor dedup) work unchanged.

### 2b: `orjson` Swap (Caching Layer Only)

**Problem:** Stdlib `json` is 3-10x slower than `orjson`. The caching layer does the heaviest JSON serialization.

**Solution:** Add `orjson` to `requirements.txt`. Create `app/utils/json_helpers.py`:
```python
import orjson

def dumps(obj) -> str:
    return orjson.dumps(obj).decode()

def loads(s):
    return orjson.loads(s)
```

Replace `import json` with `from app.utils import json_helpers as json` in:
- `app/cache/decorators.py` (cache key hashing + serialization)
- `app/cache/intel_cache.py` (Redis/PG read/write)

Only the caching hot path — not a global replacement. No frontend impact.

### Risk
Low. Both are drop-in compatible. Tests catch any edge cases.

---

## 3. Cache Invalidation on Writes

### Problem
11 write operations commit to the database but don't invalidate cached list views. Users see stale data until TTL expires (30 seconds for requisitions, 2 hours for materials).

### Solution
Add `invalidate_prefix()` calls after `db.commit()` in each missing location. The function already exists and is used correctly in requisitions and companies.

### Requisitions (2 missing)
- `claim_requisition_endpoint()` in `requisitions/core.py` → `invalidate_prefix("req_list")`
- `unclaim_requisition_endpoint()` in `requisitions/core.py` → `invalidate_prefix("req_list")`

### Materials (9 missing)
All in `app/routers/materials.py`, each gets `invalidate_prefix("material_list")` after `db.commit()`:
- `get_material()` (manufacturer inference side-effect)
- `get_material_by_mpn()` (manufacturer inference side-effect)
- `update_material()`
- `enrich_material()`
- `delete_material()`
- `restore_material()`
- `merge_material_cards()`
- `backfill_manufacturers()`
- `import_stock_list_standalone()`

### Risk
Near zero. Worst case is a few extra Redis SCAN + DELETE operations. Upside is instant data freshness.

---

## 4. SearchBuilder Utility

### Problem
13 files, ~45 invocations of the same `escape_like()` + `ILIKE` pattern. One file (vendors_crud.py) has the FTS-with-fallback cascade. Every new searchable entity requires copy-pasting ~20 lines.

### Solution
New file `app/utils/search_builder.py`:

```python
class SearchBuilder:
    def __init__(self, q: str):
        self.q = q.strip()
        self.safe = escape_like(self.q)

    def ilike_filter(self, *columns, prefix=False):
        """Returns an or_() filter across columns using ILIKE."""
        pattern = f"{self.safe}%" if prefix else f"%{self.safe}%"
        return or_(*[col.ilike(pattern) for col in columns])

    def fts_or_fallback(self, query, model, fallback_columns, min_len=3):
        """Try FTS on model.search_vector, fall back to ILIKE."""
        if not self.q or len(self.q) < min_len or not hasattr(model, 'search_vector'):
            return query.filter(self.ilike_filter(*fallback_columns))
        try:
            fts_query = query.filter(
                model.search_vector.isnot(None),
                sqltext("search_vector @@ plainto_tsquery('english', :q)"),
            ).params(q=self.q).order_by(
                sqltext("ts_rank(search_vector, plainto_tsquery('english', :q)) DESC")
            ).params(q=self.q)
            if fts_query.count() > 0:
                return fts_query
            return query.filter(self.ilike_filter(*fallback_columns))
        except (ProgrammingError, OperationalError):
            return query.filter(self.ilike_filter(*fallback_columns))
```

### Initial Migration (5 highest-duplication files)
- `app/routers/vendors_crud.py`
- `app/routers/materials.py`
- `app/routers/crm/companies.py`
- `app/services/global_search_service.py`
- `app/routers/htmx_views.py`

Remaining files adopt gradually. No behavior change — same SQL generated.

### New Files
- `app/utils/search_builder.py`
- `tests/test_search_builder.py`

### Risk
Low. Mechanical migration — same SQL output, validated by existing tests.

---

## 5. Jinja2 Component Macro Library

### Problem
Zero macros in the project. Status badges copy-pasted in 10+ templates with identical color maps. Button styles hardcoded in 14+ templates. Filter pills, urgency badges, and stat cards all duplicated.

### Solution
New file `app/templates/htmx/partials/shared/_macros.html` with these macros:

### `status_badge(value, status_map=None)`
Consolidates 10+ copies of `{% set status_colors = {...} %}` + `<span>` pattern. Default color map covers active/draft/won/lost/archived/rejected/reviewed/flagged. Callers can override.

### `risk_badge(level)`
Consolidates low_risk/medium_risk/high_risk/unknown from sourcing leads and safety review.

### `urgency_badge(value)`
Consolidates critical/hot/normal with SVG icons from requisition rows and part headers.

### `btn_primary(text, **attrs)`, `btn_secondary(text, **attrs)`, `btn_danger(text, **attrs)`
Consolidates 50+ lines of duplicated button HTML. Passes through `hx_*` attributes for HTMX.

### `filter_pill(label, value, current, hx_get, hx_target)`
Consolidates active/inactive toggle pills from quotes, requisitions, materials, vendors lists.

### `stat_card(label, value, subtitle=None)`
Consolidates metric display cards from buy plans, quotes preview, prospecting stats.

### Usage Pattern
```jinja2
{% from "htmx/partials/shared/_macros.html" import status_badge, btn_primary %}
{{ status_badge(req.status) }}
{{ btn_primary("Save", hx_post="/api/save", hx_target="#main") }}
```

### Initial Adoption (5 templates)
- `partials/requisitions/req_row.html`
- `partials/requisitions/detail_header.html`
- `partials/buy_plans/detail.html`
- `partials/sourcing/lead_card.html`
- `partials/quotes/list.html`

### New Files
- `app/templates/htmx/partials/shared/_macros.html`

### Risk
Low. Macros generate identical HTML. Visual regression caught by loading the pages.

---

## 6. Batch API Expansion

### Problem
Claude Batch API gives 50% cost savings but is only used in 2 places (email parsing, tag classification). Of the 25+ real-time Claude calls, 4 high-volume non-interactive services are strong candidates for batch conversion.

### Candidates for Batch Conversion

#### Material enrichment (`material_enrichment_service.py`)
- Currently: `claude_structured()` per-material on demand
- Batch: Collect unenriched materials, submit in batches of 100-500, process via scheduler

#### Vendor contact enrichment (`vendor_contacts.py`)
- Currently: `claude_json()` per-contact inline
- Batch: Queue new contacts, batch-submit, apply on completion

#### Email signature parsing (`signature_parser.py`)
- Currently: `claude_json()` per-signature as emails arrive
- Batch: Accumulate over 5-minute window, batch-submit, apply results

#### Knowledge service (`knowledge_service.py` — 5 calls)
- Currently: Multiple `claude_structured()` for background knowledge extraction
- Batch: Already background analysis, perfect for async batch

### What We Do NOT Convert
- User-facing calls (global search intent, HTMX responses) — need real-time
- Calls using `web_search` tool — Batch API doesn't support tools
- Calls using extended thinking — Batch API doesn't support this

### Pattern for Each Conversion
1. Add "pending" queue (database column or Redis list)
2. Scheduler job collects pending items → `claude_batch_submit()`
3. Scheduler job polls `claude_batch_results()` → applies completed results
4. Keep real-time fallback for single urgent items (user clicks "enrich now")

### New Files
- Possibly `app/services/batch_queue.py` (thin helper for pending → submitted → completed lifecycle)

### Risk
Medium. Each conversion needs careful TDD testing. Real-time fallback ensures nothing breaks if batch processing is delayed. Roll out one service at a time.

---

## 7. SSE Expansion

### Problem
The SSE broker is well-built (channel pub/sub, bounded queues, async generators) but only used for sourcing progress. Users have no real-time feedback for background operations.

### Solution
Wire the broker into background operations. One SSE connection per session on the base layout, events routed by type.

### Events to Add

#### Enrichment completion
- Trigger: `safe_background_task` wraps finish enriching vendor/company/material
- Publish: `broker.publish(f"user:{user_id}", "enrichment_complete", {"entity_type": "vendor", "entity_id": 123})`
- Frontend: HTMX SSE listener refreshes enrichment section on detail page

#### Batch API results ready (depends on Section 6)
- Trigger: Batch poll job finds completed results
- Publish: `broker.publish(f"user:{user_id}", "batch_complete", {"batch_type": "materials", "count": 42})`
- Frontend: Toast notification — "42 materials enriched"
- **Note:** This event requires Section 6 (Batch API Expansion) to be implemented first. Implement enrichment_complete, quote_updated, and rfq_response events independently; add batch_complete after Section 6 lands.

#### Quote/offer status changes
- Trigger: Offer created or quote status changes
- Publish: `broker.publish(f"user:{user_id}", "quote_updated", {"quote_id": 456})`
- Frontend: Status badge refresh via HTMX swap if on that page

#### RFQ response received
- Trigger: Email mining matches vendor reply to requisition
- Publish: `broker.publish(f"user:{user_id}", "rfq_response", {"req_id": 789})`
- Frontend: Badge count update, toast notification

### Frontend Integration
- `htmx-ext-sse` is already loaded and registered
- Add `sse-connect="/api/events/stream"` to `base.html`
- Alpine `$store.toast` already handles notifications — SSE events trigger it
- Detail pages add `hx-trigger="sse:enrichment_complete"` for section refreshes

### New Files
- `app/routers/events.py` (SSE endpoint using `broker.listen()`)

### Risk
Low-medium. SSE is additive — if connection drops, pages still work via normal HTMX. Add events incrementally, enrichment notification first as proving ground.

---

## 8. ICS/NC Worker Consolidation

### Problem
ICS and NC workers have nearly identical implementations. Both duplicate config, monitoring, circuit breaker, session management, and result parsing. Changes to one require mirroring in the other.

### Current File Locations
- `app/services/ics_worker/` — 12 modules: `worker.py`, `config.py`, `monitoring.py`, `session_manager.py`, `circuit_breaker.py`, `queue_manager.py`, `scheduler.py`, `search_engine.py`, `result_parser.py`, `mpn_normalizer.py`, `human_behavior.py`, `ai_gate.py`, `sighting_writer.py`
- `app/services/nc_worker/` — 12 modules: identical file structure to ICS
- `app/services/search_worker_base/` — 4 modules: `config.py`, `monitoring.py`, `human_behavior.py`, `scheduler.py` (partial extraction already started)

### Solution
Collapse shared logic into `search_worker_base/`. Reduce ICS and NC to thin configuration + parsing overrides.

### Consolidated into `search_worker_base/`
- Session lifecycle (connect, keepalive, reconnect)
- Circuit breaker logic (failure counting, open/close/half-open states)
- Result polling and batch dequeue
- Monitoring and health check endpoints
- Error handling and retry logic

### Stays in Each Worker
- API credentials and endpoint URLs
- Response parsing (different data shapes per service)
- Service-specific auth flows, pagination, quirks

### Pattern
```python
class ICSWorker(SearchWorkerBase):
    source_name = "ics"
    base_url = settings.ics_api_url

    def parse_results(self, raw_response) -> list[dict]:
        # ICS-specific response parsing
```

### Rollout
- Migrate ICS first, verify in production
- Then migrate NC
- Do not merge into single worker process — stay as separate deployables

### New Files
- `tests/test_search_worker_base.py`

### Risk
Medium. Workers are critical sourcing path. Full test coverage on base class before migrating either worker.

---

## Dropped from Original Plan

**Prompt caching rollout** — Audit confirmed `cache_system=True` is already the default on all 25+ Claude calls. Already working; no action needed.

---

## Success Criteria

1. Zero unprotected `asyncio.create_task()` calls remain
2. All fuzzy matching uses `rapidfuzz`, all cache serialization uses `orjson`
3. Every write operation that modifies cached data calls `invalidate_prefix()`
4. SearchBuilder covers the 5 highest-duplication search files
5. Macro library eliminates copy-paste in the 5 highest-duplication templates
6. At least 2 additional services use Batch API (material enrichment + one more)
7. SSE delivers at least enrichment-complete and batch-complete events to frontend
8. ICS and NC workers share a single base with no duplicated logic
9. 100% test coverage maintained — no regression
10. All existing tests pass — test updates limited to import paths and search pattern changes from SearchBuilder migration
