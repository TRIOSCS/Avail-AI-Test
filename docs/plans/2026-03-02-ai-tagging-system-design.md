# AI Tagging System — Design Document

**Date:** 2026-03-02
**Status:** Approved

## Purpose

Automatically classify 757K+ MaterialCards with brand and commodity tags, then propagate those tags to customers and vendors based on interaction frequency. Enables filtering vendors/customers by what they buy/sell.

## Architecture

### New Tables (migration 042)

- **tags** — canonical brand/commodity names (unique on name+type, supports parent_id hierarchy)
- **material_tags** — links MaterialCard→Tag with confidence + source tracking
- **entity_tags** — links Company/CustomerSite/VendorCard→Tag with interaction counts + two-gate visibility
- **tag_threshold_config** — configurable min_count + min_percentage gates per entity/tag type

### Classification Waterfall

1. Existing `manufacturer` column → source='existing_data', confidence=0.95
2. Prefix lookup table (~100+ manufacturer prefixes) → source='prefix_lookup', confidence=0.7-0.9
3. Nexar API batch enrichment → source='nexar', confidence=0.95
4. Claude Haiku AI fallback → source='ai_classified', confidence varies

### Two-Gate Visibility

EntityTags only become `is_visible=True` when BOTH gates pass:
- **Gate 1:** interaction_count >= min_count (from tag_threshold_config)
- **Gate 2:** interaction_count / total_entity_interactions >= min_percentage

### Live Propagation Hooks

Surgical additions to existing endpoints (requirement creation, sighting creation, offer logging, RFQ sending) call `propagate_tags_to_entity()` to increment EntityTag counts.

### Commodity Taxonomy

46 seeded commodity tags across 7 categories: Semiconductors, Passives, Electromechanical, PC Server/Infrastructure, and Other.

## Key Design Decisions

1. **Relational entity_tags table replaces JSON `brand_tags`/`commodity_tags` columns** — existing JSON columns kept in sync during transition, deprecated later.
2. **Tests in `tests/` directory** (not `app/tests/`) — matches existing codebase pattern.
3. **Migration 042** — follows `041_add_notifications`.
4. **Customer propagation targets both Company AND CustomerSite** — requisitions link to sites, but company-level rollup is useful for reporting.
5. **Scheduler uses `@_traced_job` pattern** with manual `SessionLocal()` session management.
6. **Nexar integration reuses existing `NexarConnector`** class in `app/connectors/sources.py`.

## Phases

1. Data model + migration + commodity seed
2. Classification service + prefix lookup + visibility calculator
3. Bulk backfill (existing 757K cards)
4. Live propagation hooks
5. API endpoints + query support
6. Nexar + AI backfill (remaining unclassified)
