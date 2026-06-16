# Connector-description harvest — design

**Date:** 2026-06-16
**Status:** approved (brainstorm) — pending written-spec review
**Author:** Claude (with mkhoury)
**Program:** Enrichment-source expansion, Approach A ("harvest-first"). This is sub-project #1 of 5
(others: eBay-title mining, Nexar-deep fields, Intel/AMD ARK CPU decoder, Lenovo PSREF — each its
own spec → plan → build).

## Problem

The connector pipeline (`app/connectors/*`) already fetches rich enrichment data on every
connector-enrichment call — DigiKey/Mouser return `description` (DetailedDescription),
`lifecycle_status`, `package_type`, `pin_count`, `rohs_status`; element14 returns
`description`/`package_type`/`rohs_status`; OEMSecrets returns `category`/`lifecycle_status`/
`datasheet_url`; Nexar returns `description`/`category`. But `app/services/enrichment.py::_try_connector_config`
keeps only `manufacturer` and a crude `category` (the description truncated to `[:200]`) and
**discards everything else**. We pay for this data and throw the enrichment away.

## Goal & scope

Harvest the already-fetched connector data into the F1 spec ladder so it serves all three program
goals: **coverage** (categorize uncategorized cards from the description), **facet depth**
(structured fields + parsed-description facets), and partially **cross-ref** (datasheet_url
captured for the downstream datasheet sub-project).

**In scope (v1):** the dedicated **enrichment path** — `enrich_material_card` →
`_apply_enrichment_to_card`, which already writes to the card.

**Out of scope (deliberate follow-ups):**
- The higher-volume **pricing-search path** (`search_service`) fetches the same data; harvesting
  there means opportunistic writes during a search — a larger surface, its own sub-project.
- Net-new external sources (covered by the other Approach-A sub-projects).

## Resolved decisions

1. **`connector_desc` tier = 84** — a distributor's description is more authoritative than the
   card's own description (`desc_parse` 83) but is still grammar-parsed, so it sits above
   `desc_parse` and below the deterministic decoders (`mpn_decode` 85). Mirrors `partsurfer_desc`
   (also 84; ties are not load-bearing per the ladder's run-order-independence).
2. **v1 = enrichment path only** (see scope).

## Design

### A. Structured fields → vendor-API tier (authoritative, not parsed)

The structured fields are authoritative distributor data, so they record at the **connector's own
vendor-API tier** (already registered: `digikey_api`/`mouser_api`/`element14_api`/`oemsecrets_api`/
`nexar_api` = 90), NOT at a description tier.

- Map each connector result field → its canonical facet key, then `record_spec`:
  - `pin_count` → `pin_count`
  - `package_type` → `package`
  - `lifecycle_status` → `lifecycle`
  - `rohs_status` → `rohs`
  - (`datasheet_url` is **stored on the card** for the datasheet sub-project, not a facet.)
- Implementation MUST confirm the canonical facet-key names against the seeded commodity schemas
  (`app/data/commodity_seeds.json`) — `record_spec` gates on schema membership, so an off-schema key
  is a silent no-op (acceptable: a key only sticks where the commodity defines it).
- Source string = the connector's existing `SOURCE_TIER` key (the implementation maps
  `config["name"]` → the `*_api` source). Confidence = `0.95` (authoritative structured field).

### B. Description prose → `connector_desc` (categorize + fill)

Feed the connector `description` to the existing `categorize_and_record` (the proven
`partsurfer_desc`/FRU-desc pattern):

- `categorize_and_record(db, card, description=<connector desc>, source="connector_desc",
  confidence=CONNECTOR_DESC_CONFIDENCE)` — categorizes an **uncategorized** card (coverage) and
  fills facets (depth) in one per-card `begin_nested()` SAVEPOINT. Fill-only: a no-op on an
  already-categorized card's category (it still fills facets via the ladder).
- New constant `CONNECTOR_DESC_CONFIDENCE = 0.90` + `CONNECTOR_DESC_SOURCE = "connector_desc"`
  in `app/services/desc_extractor/_common.py` (next to the `DESC_*`/`PARTSURFER_DESC_*` constants).

### C. Ladder registration

- `app/services/spec_tiers.py::SOURCE_TIER` — add `"connector_desc": 84` (above `desc_parse` 83).
- `alembic/versions/096_spec_provenance.py` — add the `connector_desc → 84` arm to the
  `_SOURCE_TIER_SQL_CASE` snapshot (the `test_migration_096_spec_provenance.py` sync test asserts
  the CASE matches `SOURCE_TIER` key-for-key). No live-DB effect (runtime tier is Python
  `tier_for()`; the migration already ran — same as the `partsurfer_desc` precedent). **No new
  migration.**

### D. Wiring & flag

- Widen `_try_connector_config`'s return to carry the raw structured fields + full `description`
  (not just the truncated `category`). Backward-compatible: existing `manufacturer`/`category`/
  `source`/`confidence` keys unchanged.
- `_apply_enrichment_to_card` gains a harvest step (A + B above), gated by a new
  `settings.connector_desc_harvest_enabled: bool = True` (mirrors `partsurfer_desc_enabled`).
- Reuses existing infra only — no new module, no new external calls (the data is already fetched).

### Data flow

`enrich_material_card(mpn)` → `_try_connector_config` (now returns full fields) →
`_apply_enrichment_to_card`: (1) existing manufacturer/category apply, (2) **NEW** structured-field
facets at vendor tier 90, (3) **NEW** `categorize_and_record(source="connector_desc")` over the
description. All writes arbitrated by the F1 ladder.

## Error handling

- Every write inside the existing per-card `begin_nested()` SAVEPOINT (in `categorize_and_record`
  and around the structured-facet writes) — a constraint/`DataError`/`IntegrityError` on one field
  rolls back only that write and is logged; never poisons the batch session.
- Malformed/missing fields are skipped (the connectors already normalize via `_core_attrs`).
- The ladder rejects anything that loses to a higher prior — a vendor structured field (90) and a
  `connector_desc` facet (84) can never clobber `manual`/`trio_source`/decoder values.

## Testing

- A connector result with `description` + structured fields → assert: structured facets land at the
  vendor tier (90), the description categorizes an uncategorized card + fills facets at
  `connector_desc` (84), the ladder beats the card's own `desc_parse` (83) but loses to
  `mpn_decode` (85).
- Off-schema structured field (e.g. `pin_count` on a `dram` card whose schema lacks it) → silent
  no-op, no error.
- Ignored-manufacturer / empty-description / no-result → no-op (no facets, no crash).
- Flag OFF → no harvest; flag ON → harvest runs.
- `datasheet_url` stored on the card; not written as a facet.
- Migration-096 snapshot sync test passes with the new `connector_desc` arm.

## Rollback

Flag off (`CONNECTOR_DESC_HARVEST_ENABLED=false`) disables it instantly; revert the commit to
remove. No migration to downgrade. Recorded facets remain valid (correctly tiered); the ladder
makes them harmless even if the source is later disabled.

## Files

- `app/services/enrichment.py` — widen `_try_connector_config` return; harvest step in
  `_apply_enrichment_to_card`.
- `app/services/desc_extractor/_common.py` — `CONNECTOR_DESC_SOURCE` / `CONNECTOR_DESC_CONFIDENCE`.
- `app/services/spec_tiers.py` — `SOURCE_TIER["connector_desc"] = 84`.
- `alembic/versions/096_spec_provenance.py` — `connector_desc → 84` CASE arm (test sync only).
- `app/config.py` — `connector_desc_harvest_enabled: bool = True`.
- `tests/test_connector_desc_harvest.py` (new) — the cases above.
- `docs/APP_MAP_INTERACTIONS.md` — enrichment-writers / tier table + the harvest step.

## Follow-ups (not this sub-project)

- Search-path harvest (`search_service`) — opportunistic enrichment during pricing search.
- `datasheet_url` consumption — the datasheet PDF→facet sub-project.
- The remaining Approach-A sub-projects (eBay-title mining, Nexar-deep fields, ARK CPU decoder,
  Lenovo PSREF).
