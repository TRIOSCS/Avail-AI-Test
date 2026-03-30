# Materials Parts Library — "Light the Grid"

**Date:** 2026-03-30
**Status:** Draft

## Problem

The Materials page has a fully-built UI (faceted browse, search, detail views, tabs) but is non-functional because:
- 16 material cards exist, 0 enriched, 0 with categories → browse/filter empty
- Enrichment pipeline exists but is disabled (`material_enrichment_enabled=false`)
- Enrich button on detail page is a placeholder (does nothing)
- Search uses ILIKE only — no full-text search despite `search_vector` TSVECTOR column existing and being populated
- Vendor/sourcing/price tabs have no linked data

## Scope

Four phases, each independently deployable:

---

## Phase 1: Enrich Existing Cards (make browse work)

### 1a. Enable the enrichment job
- Set `MATERIAL_ENRICHMENT_ENABLED=true` in `.env`
- The APScheduler job `_job_material_enrichment` already calls `enrich_pending_cards()` which uses Claude Haiku to classify each card with: description, category, lifecycle_status
- This runs on a schedule and picks up all un-enriched cards

### 1b. Trigger immediate enrichment of all 16 cards
- One-time management command: call `enrich_material_cards()` for all existing card IDs
- Can use the existing `management/reenrich.py` script

### 1c. Wire the Enrich button on detail page
- Replace the placeholder in `enrich_material` endpoint (htmx_views.py:8582)
- Call `enrich_material_cards([material_id], db)` then return the refreshed detail partial
- Show toast on success/failure

**Result:** Commodity tree populates, faceted filters work, lifecycle badges appear, descriptions show.

---

## Phase 2: Upgrade Search (ILIKE → FTS)

### 2a. Add PostgreSQL trigger for search_vector maintenance
- Create Alembic migration with trigger: UPDATE `search_vector` on INSERT/UPDATE of `display_mpn`, `manufacturer`, `description`, `category`
- Weighted: MPN = A, manufacturer = B, description/category = C
- Backfill existing rows in the same migration

### 2b. Use FTS in faceted search service
- In `search_materials_faceted()`: when `q` is provided, use `search_vector @@ plainto_tsquery(q)` instead of ILIKE
- Add `ts_rank()` for relevance ordering when searching
- Fall back to ILIKE for very short queries (< 3 chars)

### 2c. Add pg_trgm similarity for typo tolerance
- `CREATE INDEX idx_mc_trgm ON material_cards USING gin (display_mpn gin_trgm_ops)`
- Use `similarity()` as a secondary ranking factor

**Result:** "DDR5 memory" finds relevant cards. Typos like "STM32F40" still match "STM32F407VGT6".

---

## Phase 3: Integration with Search Pipeline (make it grow)

### 3a. Link MaterialCard to Requirements
- `search_requirement()` already calls `_upsert_material_card()` which creates/updates cards
- Ensure `Requirement.material_card_id` is set after upsert (check current behavior)
- This makes the Sourcing tab on material detail populated

### 3b. Populate vendor history from sightings
- After `_save_sightings()`, upsert `MaterialVendorHistory` rows from sighting data
- This is partially implemented — verify and fix gaps
- This makes the Vendors tab and "Best Price" column populated

### 3c. Record price snapshots
- `record_price_snapshot()` already exists in `services/price_snapshot_service.py`
- Verify it's called during search pipeline
- This makes the Price History tab populated

**Result:** Every search populates the materials library. Cards accumulate vendor history, pricing data, and sourcing links over time.

---

## Phase 4: Polish

### 4a. Cache cross-reference lookups
- `find_crosses()` already saves results to `card.cross_references` JSONB
- Add a check at the top: if `card.cross_references` is non-empty, return cached data
- Add a "Refresh" button that bypasses the cache

### 4b. Auto-enrich new cards on creation
- In `resolve_material_card()` (search_service.py), when a NEW card is created, schedule background enrichment
- Use `safe_background_task()` to fire-and-forget enrichment
- Already partially implemented in `_schedule_background_enrichment()`

### 4c. search_count tracking
- `_upsert_material_card()` already increments `search_count` — verify this works
- Display "Most searched" sort option in the list

**Result:** Cross-references cached, new cards auto-enriched, search counts accurate.

---

## Files Changed (estimated)

| Phase | Files | Description |
|-------|-------|-------------|
| 1a | `.env` | Enable `MATERIAL_ENRICHMENT_ENABLED=true` |
| 1b | one-time script run | Call `enrich_material_cards()` |
| 1c | `app/routers/htmx_views.py` | Wire enrich button to real service |
| 2a | `alembic/versions/` | Migration: FTS trigger + trgm index |
| 2b | `app/services/faceted_search_service.py` | FTS query instead of ILIKE |
| 2c | `alembic/versions/` | trgm index migration |
| 3a | `app/search_service.py` | Verify material_card_id linkage |
| 3b | `app/search_service.py` | Verify MaterialVendorHistory upsert |
| 3c | `app/search_service.py` | Verify price snapshot recording |
| 4a | `app/routers/htmx_views.py` | Cache check in find_crosses |
| 4b | `app/search_service.py` | Background enrichment on card creation |
| 4c | Verify existing code | search_count already incremented |

## Out of Scope
- New UI components (the existing UI is comprehensive)
- Elasticsearch (PostgreSQL FTS + pg_trgm is sufficient for this data size)
- Material merge/dedup UI (future work)
- Customer purchase history sync (depends on external data source)
