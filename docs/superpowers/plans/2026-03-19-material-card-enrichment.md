# Material Card Enrichment — Populating Commodity Tags, Sub-Filters & AI Descriptions

**Date:** 2026-03-19
**Goal:** Enrich 743K+ MaterialCards with accurate commodity categories, structured specs (sub-filter data), AI descriptions, lifecycle/RoHS/cross-refs, and ongoing part discovery at ≥95% confidence
**Branch:** `claude/material-tab-project-0Z2XD`
**Status:** v3 — see `/root/.claude/plans/warm-mixing-badger.md` for latest approved plan

---

## Audit Findings (v1 → v2 Changes)

### Issues Found in v1

1. **Phase 1 overstated raw_data richness.** Sighting `raw_data` is the flat connector result dict (not a separate blob). It contains `description` and `manufacturer` but **zero structured specs** (no RoHS, lifecycle, package, pin count, or electrical parameters). The v1 claim of "specs from product pages" in Tier 1 was wrong — connectors don't extract specs.

2. **Connectors are throwing away data.** DigiKey, Mouser, and OEMSecrets API responses contain RoHS status, lifecycle, lead time, full pricing tiers, specs/attributes — but parsers only extract ~12 fields and drop the rest. This is the biggest missed opportunity.

3. **Nexar GraphQL doesn't query specs.** `FULL_QUERY` and `AGGREGATE_QUERY` omit `part.specs`, `part.parameters`, `part.descriptions`, `part.datasheets`, `part.rohs`, `part.lifecycle`. These fields exist in the Nexar schema but are never requested.

4. **Category taxonomy is misaligned.** `specialty_detector.py` uses coarse categories ("memory", "processors", "servers") while `fix_categories.py` uses granular ones ("dram", "cpu", "motherboards"). The `material_enrichment_service.py` imports from `specialty_detector` so it writes the WRONG categories. Must unify before enrichment.

5. **Batch metadata persistence is fragile.** Current `tagging_ai_batch.py` writes batch tracking to `/tmp/` which doesn't survive container restarts. Need DB-backed tracking for production reliability.

6. **No incremental enrichment.** Plan was all one-time scripts. New cards created after enrichment runs would remain unenriched. Need to wire into the search flow for ongoing enrichment.

7. **`specs_structured` column doesn't exist yet.** Phase 3 depends on Sub-Project 1 (migration + tables). We can still extract specs into `specs_summary` (Text, already exists) as an interim step.

---

## Current State (Verified)

| Field | Column Exists | Coverage | Source |
|-------|--------------|----------|--------|
| `description` | Yes (String 1000) | ~80% | Gradient AI from MPN only |
| `category` | Yes (String 255) | ~40% | Mixed coarse + granular |
| `manufacturer` | Yes (String 255) | ~60% | Connectors + AI |
| `specs_summary` | Yes (Text) | ~0% | Never populated |
| `package_type` | Yes (String 100) | ~0% | Never populated |
| `lifecycle_status` | Yes (String 50) | ~0% | Never populated |
| `rohs_status` | Yes (String 50) | ~0% | Never populated |
| `pin_count` | Yes (Integer) | ~0% | Never populated |
| `datasheet_url` | Yes (String 1000) | ~0% | Never populated |
| `cross_references` | Yes (JSONB) | ~0% | Never populated |
| `specs_structured` | **No — needs migration** | 0% | Sub-Project 1 |

---

## Data Sources (Verified — What's Actually Available)

### Tier 1 — Existing Sighting Data (FREE, already fetched)
| Source | What's Actually in raw_data | What's NOT in raw_data |
|--------|---------------------------|----------------------|
| **DigiKey** | `description` (DetailedDescription), manufacturer, vendor_sku, click_url | RoHS, lifecycle, specs, package, lead time, full pricing — **dropped by parser** |
| **Mouser** | `description`, manufacturer, vendor_sku, click_url | RoHS, MSL, specs, lead time, full pricing — **dropped by parser** |
| **Nexar** | `description` + `category` (aggregate only), manufacturer | specs, parameters, datasheets, rohs, lifecycle — **never queried** |
| **Element14** | `description` (displayName), manufacturer | specs, lifecycle — **dropped by parser** |
| **OEMSecrets** | manufacturer, `datasheet_url`, `moq` | description (not mapped!), specs, lifecycle — **dropped** |
| **BrokerBin** | `description`, `condition`, `country`, `age_in_days` | No structured specs available |
| **eBay** | `ebay_title`, `ebay_condition`, `ebay_image` | No structured specs |
| **NetComponents** | `description`, `region`, `country`, `price_breaks` | No structured specs |
| **ICSource** | `description`, `in_stock` | No structured specs |

### Tier 2 — Connector Upgrades (Requires code changes, then future searches yield richer data)
| Source | Fields We Should Add to Parser |
|--------|-------------------------------|
| **Nexar GraphQL** | `part.specs`, `part.descriptions`, `part.datasheets`, `part.rohs`, `part.lifecycle` |
| **DigiKey v4** | RoHS, lifecycle, specs/attributes, full PriceBreaks, MOQ, packaging |
| **Mouser** | RoHS, MSL, lead time, full PriceBreaks |

### Tier 3 — AI Extraction (API cost, high accuracy)
| Source | Best For |
|--------|---------|
| **Claude Haiku** (batch) | Category classification, basic spec extraction from MPN/description |
| **Claude Sonnet** (batch) | Rich descriptions, complex spec inference, cross-reference identification |

---

## Revised Pipeline — 6 Phases

### Phase 0: Fix Category Taxonomy Alignment (Pre-requisite)
**File:** `app/services/specialty_detector.py` + `app/services/material_enrichment_service.py`

The system has TWO conflicting category lists:
- `specialty_detector.py` COMMODITY_MAP: coarse ("memory", "processors", "servers") — 23 categories
- `fix_categories.py` VALID_CATEGORIES: granular ("dram", "cpu", "motherboards") — 45 categories
- `material_enrichment_service.py` imports from specialty_detector → **writes wrong categories**

**Fix:**
1. Update `COMMODITY_MAP` in specialty_detector.py to use the 45-category granular taxonomy
2. Update `COMMODITY_DISPLAY_NAMES` to match
3. Update `material_enrichment_service.py` VALID_CATEGORIES to use the 45-category list directly
4. Add migration script to reclassify existing coarse categories → granular equivalents
5. Tests

**No API cost. ~1 hour dev.**

---

### Phase 1: Mine Existing Sighting Data (Free — Zero API Cost)
**Script:** `scripts/enrich_from_sightings.py`

Extract descriptions, manufacturers, and datasheet URLs from existing sighting raw_data. These are real API responses from DigiKey, Mouser, etc. that were never pulled back onto the MaterialCard.

**What we CAN extract (verified):**
- `description` — available from DigiKey, Mouser, Nexar (aggregate), Element14, BrokerBin, NetComponents, ICSource
- `manufacturer` — available from all authorized sources
- `datasheet_url` — available from OEMSecrets sightings only
- `category` — available from Nexar aggregate sightings only (limited)

**What we CANNOT extract (no data exists):**
- Structured specs (voltage, capacitance, etc.) — connectors don't capture these
- Package type, lifecycle, RoHS — connectors drop these fields
- Pin count, cross-references — never fetched

**Steps:**
1. Query MaterialCards LEFT JOIN Sightings, grouped by material_card_id
2. For each card, rank sighting descriptions by source priority:
   DigiKey > Mouser > Element14 > Nexar > NetComponents > BrokerBin > ICSource > eBay title
3. Pick best description (longest from highest-priority authorized source)
4. Pick manufacturer from highest-priority authorized source
5. Pick datasheet_url from any OEMSecrets sighting
6. Only overwrite NULL or empty fields (don't clobber existing enrichment)
7. Set `enrichment_source = 'sighting_extraction'`

**Expected yield:** Description improvements on ~100-200K cards (those with sightings from authorized sources). Manufacturer fill on ~50-100K cards. Datasheet URLs on a smaller subset.
**Confidence:** 0.95-0.99 (this is verbatim distributor data)

---

### Phase 2: AI Category + Description Enrichment (Haiku Batch)
**Script:** `scripts/enrich_batch.py`
**Combined Phase** — do category AND description in one pass (saves tokens vs two separate passes)

Classify cards into 45-category taxonomy AND generate descriptions in a single Batch API call per batch of 50 MPNs.

**Steps:**
1. Query cards needing work:
   - `category IS NULL` OR `category = 'other'` OR category is coarse/legacy
   - OR `description IS NULL` OR `LENGTH(description) < 30`
2. Assemble context per card: MPN + manufacturer + best sighting description (from Phase 1)
3. Submit to Anthropic Batch API (Haiku) in batches of 50 MPNs per request
4. Prompt returns per-MPN:
   ```json
   {
     "mpn": "M393A2K43DB3-CWE",
     "category": "dram",
     "category_confidence": 0.97,
     "description": "16GB DDR4-3200 ECC Registered DIMM...",
     "description_confidence": 0.93,
     "manufacturer": "Samsung",
     "package_type": "RDIMM"
   }
   ```
5. Apply with confidence gates:
   - Category: apply if ≥ 0.90, flag for review if 0.80-0.90
   - Description: apply if ≥ 0.90 AND longer than existing description
   - Manufacturer: apply only if card has no manufacturer AND confidence ≥ 0.95
   - Package type: apply if ≥ 0.90
6. Track in DB (not /tmp/) — create `EnrichmentBatch` model or reuse `DiscoveryBatch`

**Validation:** Category must be in 45-item VALID_CATEGORIES. Package must be reasonable length.
**Expected yield:** 400-600K cards with category + description
**Cost:** ~$25-40 via Batch API (combined pass is more efficient than separate)

---

### Phase 3: Structured Spec Extraction (Haiku Batch)
**Script:** `scripts/enrich_specs_batch.py`

Two paths depending on whether Sub-Project 1 tables exist:

**Path A — Sub-Project 1 tables exist:**
Write to `specs_structured` JSONB + `material_spec_facets` via `spec_write_service.record_spec()`

**Path B — Interim (Sub-Project 1 not built yet):**
Write to `specs_summary` (Text, already exists) as structured text. Example:
> "DDR Type: DDR4 | Capacity: 16GB | Speed: 3200MHz | ECC: Yes | Form Factor: RDIMM"

This is parseable later when `specs_structured` column is added.

**Steps:**
1. Group cards by category (from Phase 2)
2. For each commodity, load spec schema (from design spec, hardcoded until DB table exists)
3. Batch 50 cards of the SAME commodity per request (commodity-specific prompt)
4. Haiku extracts specs with per-field confidence
5. Only write fields with confidence ≥ 0.85
6. Normalize units (uF→pF, kΩ→Ω, GB→GB) using existing `normalization.py` patterns

**Priority commodities (top 15 by card count):**
dram, capacitors, resistors, connectors, ssd, hdd, inductors, microcontrollers, power_supplies, diodes, mosfets, flash, cpu, gpu, network_cards

**Expected yield:** 100-300K cards with structured specs
**Cost:** ~$30-50 via Batch API

---

### Phase 4: Sonnet Description Upgrade (High-Value Cards Only)
**Script:** `scripts/enrich_descriptions_sonnet.py`

Sonnet-quality descriptions for high-visibility cards only (not all 743K).

**Target cards (prioritized):**
1. Cards linked to active requirements (buyer is looking at them)
2. Cards with search_count ≥ 5 (frequently searched)
3. Cards with category in top 15 commodities AND description < 80 chars

**Steps:**
1. Query ~20-50K high-priority cards
2. Assemble full context: MPN, manufacturer, category, specs (Phase 3), sighting descriptions
3. Submit to Sonnet Batch API
4. Generate:
   - Rich 2-3 sentence technical description
   - `specs_summary` (key specs, concise)
5. Apply if confidence ≥ 0.90

**Expected yield:** 20-50K cards with professional descriptions
**Cost:** ~$30-50 via Batch API (smaller volume, higher quality)

---

### Phase 5: Connector Enrichment Upgrade (Ongoing — Future Data)
**Files:** `app/connectors/sources.py`, `digikey.py`, `mouser.py`

Upgrade connectors so ALL FUTURE searches capture richer data. This doesn't help existing cards but prevents the "data thrown away" problem going forward.

**Changes:**
1. **Nexar GraphQL** — Add to FULL_QUERY and AGGREGATE_QUERY:
   ```graphql
   part {
     specs { attribute { name } displayValue }
     descriptions { text creditString }
     bestDatasheet { url }
     category { name parentId }
   }
   ```
2. **DigiKey** — Capture into raw_data: `RoHSStatus`, `LifecycleStatus`, `PackagingType`, `MinimumOrderQuantity`, full `StandardPricing` array, `Parameters` array
3. **Mouser** — Capture into raw_data: `ROHSStatus`, `LeadTime`, full `PriceBreaks` array

4. **Search service** — After sighting creation, auto-populate MaterialCard fields from the enriched connector data (lifecycle_status, rohs_status, package_type, datasheet_url)

**No API cost (same API calls, just extract more fields). ~3 hours dev.**

---

### Phase 6: Cross-Verification (Sonnet Spot-Check)
**Script:** `scripts/verify_enrichment.py`

Same as v1 Phase 5 — stratified sampling + Sonnet web search verification.

**Additional improvement:** Verify BEFORE making enrichment visible to users. Run verification on a staging flag, only flip to "verified" after accuracy targets met.

**Steps:**
1. Sample 500 cards stratified by category
2. Sonnet + web_search cross-references each card's category, description, specs
3. Score: correct / incorrect / uncertain per field
4. Targets: category ≥ 95%, description ≥ 95%, specs ≥ 90%
5. If below target, identify failure patterns → re-run targeted correction on that category

**Cost:** ~$12-15

---

## Revised Implementation Order

```
Phase 0: Fix taxonomy alignment         → ~1 hour dev, $0
Phase 1: Mine sighting data             → ~2 hours dev, $0
Phase 2: Category + description (Haiku) → ~3 hours dev, ~$35
Phase 3: Structured specs (Haiku)       → ~3 hours dev, ~$40
Phase 4: Sonnet descriptions (top cards)→ ~2 hours dev, ~$40
Phase 5: Connector upgrades (ongoing)   → ~3 hours dev, $0
Phase 6: Cross-verification             → ~1 hour dev, ~$15
```

**Total estimated API cost: ~$130**
**Total dev time: ~15 hours across sessions**

---

## Task Checklist

### Phase 0: Fix Category Taxonomy
- [ ] Unify `specialty_detector.py` COMMODITY_MAP → 45-category granular taxonomy
- [ ] Update `COMMODITY_DISPLAY_NAMES` to match
- [ ] Update `material_enrichment_service.py` to use unified categories
- [ ] Create `scripts/reclassify_coarse_categories.py` to fix existing coarse → granular
- [ ] Tests for category mapping

### Phase 1: Mine Existing Sighting Data
- [ ] Create `scripts/enrich_from_sightings.py`
- [ ] Build source-priority ranking for descriptions
- [ ] Extract: description, manufacturer, datasheet_url from raw_data
- [ ] Only fill NULL/empty fields (don't clobber)
- [ ] Add `--dry-run` flag (default on)
- [ ] Progress logging every 1000 cards
- [ ] Test with 100-card sample
- [ ] Write tests for extraction logic

### Phase 2: AI Category + Description (Combined)
- [ ] Create `scripts/enrich_batch.py` using Batch API
- [ ] Combined category + description extraction in single pass
- [ ] Use 45-category taxonomy
- [ ] Confidence-gated application (≥0.90 category, ≥0.90 description)
- [ ] DB-backed batch tracking (not /tmp/)
- [ ] Handle Batch API lifecycle (submit → poll → apply) using chunked processing pattern
- [ ] Write tests

### Phase 3: Structured Spec Extraction
- [ ] Create `scripts/enrich_specs_batch.py`
- [ ] Per-commodity prompt templates (top 15 commodities)
- [ ] Path A: write to specs_structured JSONB (if column exists)
- [ ] Path B: write to specs_summary Text (interim)
- [ ] Unit normalization
- [ ] Enum validation
- [ ] Write tests

### Phase 4: Sonnet Description Upgrade
- [ ] Create `scripts/enrich_descriptions_sonnet.py`
- [ ] Target high-priority cards only (active requirements, high search_count)
- [ ] Full context assembly (specs + sightings + cross-refs)
- [ ] Sonnet Batch API submission
- [ ] Write tests

### Phase 5: Connector Upgrades
- [ ] Add specs/rohs/lifecycle/datasheets to Nexar GraphQL queries
- [ ] Capture dropped fields in DigiKey parser → raw_data
- [ ] Capture dropped fields in Mouser parser → raw_data
- [ ] Auto-populate MaterialCard from enriched connector data in search_service
- [ ] Write tests

### Phase 6: Cross-Verification
- [ ] Create `scripts/verify_enrichment.py`
- [ ] Stratified sampling by category
- [ ] Sonnet + web_search verification
- [ ] Accuracy report generation
- [ ] Targeted re-correction for failure patterns
- [ ] Write tests

---

## Architecture Decisions

1. **Phase 0 is non-negotiable.** Two conflicting taxonomies means every enrichment pass would write inconsistent categories. Fix once, then all downstream phases use the same 45 categories.

2. **Combined category + description pass (Phase 2)** saves ~30% tokens vs separate passes. The model already has the MPN context loaded — asking for both outputs in one call is more efficient.

3. **Batch API with DB tracking.** The existing `/tmp/` persistence in `tagging_ai_batch.py` doesn't survive container restarts. Use `DiscoveryBatch` model or create `EnrichmentBatch` for reliability.

4. **Phase 4 is Sonnet but limited scope.** Sonnet costs 5x Haiku. Running it on all 743K cards would be ~$300+. Limiting to 20-50K high-visibility cards keeps cost at ~$40 while covering the cards users actually see.

5. **Phase 5 (connector upgrades) prevents future debt.** Without this, every new search still throws away rich data. One-time code change, zero ongoing cost.

6. **Interim specs_summary (Phase 3 Path B)** lets us extract and store specs NOW without waiting for Sub-Project 1 migration. Text format is parseable when we add the JSONB column later.

---

## Safety & Rollback

- All scripts have `--dry-run` flag (default on)
- All writes batched with `db.commit()` per 100 cards (not one giant transaction)
- `enrichment_source` tracks provenance per script
- Rollback per phase: `UPDATE material_cards SET field = NULL WHERE enrichment_source = 'phase_X_source'`
- Phase 6 verification runs BEFORE enrichment is visible to users
- Connector upgrades (Phase 5) are additive — existing fields unchanged, new fields added to raw_data
