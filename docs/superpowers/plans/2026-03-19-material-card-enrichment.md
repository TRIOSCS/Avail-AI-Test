# Material Card Enrichment — Populating Commodity Tags, Sub-Filters & AI Descriptions

**Date:** 2026-03-19
**Goal:** Enrich 743K+ MaterialCards with accurate commodity categories, structured specs (sub-filter data), and AI-generated descriptions at ≥95% confidence
**Branch:** `claude/material-tab-project-0Z2XD`

---

## Current State

- **MaterialCard columns available:** `category`, `description`, `specs_summary`, `manufacturer`, `package_type`, `lifecycle_status`, `rohs_status`, `pin_count`, `datasheet_url`, `cross_references`
- **New columns needed:** `specs_structured` (JSONB) — from Sub-Project 1 design spec
- **Category coverage:** ~40% populated, mix of coarse ("servers", "memory") and granular ("capacitors", "dram")
- **Description coverage:** ~80% populated (Gradient AI enrichment), but many are generic 1-liners
- **Structured specs:** 0% — `specs_structured` column doesn't exist yet
- **Existing enrichment:** `material_enrichment_service.py` does MPN → description + category via Gradient/Anthropic
- **Existing batch infra:** `tagging_ai_batch.py` (607 lines) handles Anthropic Batch API lifecycle, `BatchQueue` helper available

---

## Data Sources (Priority Order)

### Tier 1 — Authoritative APIs (confidence: 0.95-0.99)
| Source | What It Gives Us | How to Access |
|--------|-----------------|---------------|
| **Nexar/Octopart** | description, category, shortDescription, manufacturer, specs from product pages | GraphQL `part.shortDescription`, `part.category.name`, REST v4 detailed specs |
| **DigiKey** | DetailedDescription, manufacturer, package, specs tables | Already stored in `sighting.raw_data`, also live API v4 |
| **Mouser** | Description, manufacturer, specs | Already stored in `sighting.raw_data`, also live API |
| **Element14** | displayName, manufacturer, stock | Already stored in `sighting.raw_data` |

### Tier 2 — Aggregators (confidence: 0.85-0.95)
| Source | What It Gives Us | How to Access |
|--------|-----------------|---------------|
| **OEMSecrets** | datasheet_url, manufacturer, descriptions from 140+ distributors | Already stored in `sighting.raw_data` |
| **Sourcengine** | manufacturer, basic descriptions | Already stored in `sighting.raw_data` |

### Tier 3 — AI Extraction (confidence: 0.80-0.95)
| Source | What It Gives Us | How to Access |
|--------|-----------------|---------------|
| **Claude Haiku** (batch) | category, description, structured specs from MPN + manufacturer + existing description | Anthropic Batch API (50% cheaper) |
| **Claude Sonnet** (verification) | High-confidence validation pass on Haiku results, rich descriptions | Direct API or Batch API |

### Tier 4 — Supplemental (confidence: 0.60-0.80)
| Source | What It Gives Us | How to Access |
|--------|-----------------|---------------|
| **BrokerBin** | description (short), condition | Already stored in `sighting.raw_data` |
| **eBay** | ebay_title (often has specs embedded) | Already stored in `sighting.raw_data` |

---

## Enrichment Pipeline — 5 Phases

### Phase 1: Mine Existing Sighting Data (Free — Zero API Cost)
**Script:** `scripts/enrich_from_sightings.py`

We have 743K+ MaterialCards with linked Sightings that already contain raw_data from DigiKey, Mouser, Nexar, OEMSecrets, etc. This data was fetched but never extracted back onto the card.

**Steps:**
1. Query all MaterialCards joined to their Sightings
2. For each card, collect all sighting.raw_data blobs grouped by source_type
3. Extract and merge (highest-confidence source wins):
   - `description` — prefer DigiKey DetailedDescription > Mouser Description > Nexar shortDescription > OEMSecrets > BrokerBin
   - `manufacturer` — prefer authorized sources (DigiKey, Mouser) > aggregators
   - `category` — map from Nexar `category.name` or infer from DigiKey/Mouser category fields
   - `datasheet_url` — from OEMSecrets (they provide it directly)
   - `package_type` — parse from description or raw_data fields
4. Only overwrite if incoming confidence > existing confidence
5. Track provenance: set `enrichment_source = "{source_type}_sighting"`

**Expected yield:** 200-400K cards enriched (every card that's been searched at least once has sightings)
**Confidence:** 0.90-0.99 (this is real API data, just never extracted)

---

### Phase 2: AI Category Classification (Haiku Batch — Low Cost)
**Script:** `scripts/enrich_categories_batch.py`
**Builds on:** existing `fix_categories.py` pattern + `tagging_ai_batch.py` Batch API infra

Classify ALL cards into the 45-category taxonomy from the faceted search design spec.

**Steps:**
1. Query all cards where `category IS NULL` OR `category = 'other'` OR category is coarse/legacy
2. Group into batches of 100 MPNs (Batch API format)
3. Submit to Anthropic Batch API (Haiku, 50% cost savings)
4. Prompt includes: MPN, manufacturer, existing description (if any)
5. Response: `{mpn, category, confidence}` — only apply if confidence ≥ 0.90
6. Cards with confidence 0.80-0.90 → flag for Phase 4 Sonnet verification
7. Cards with confidence < 0.80 → leave as "other", flag for manual review

**Validation:** Category must be in the 45-item `VALID_CATEGORIES` list from the design spec
**Expected yield:** 300-500K cards categorized
**Cost:** ~$15-25 via Batch API (743K cards × ~80 tokens each × $0.25/MTok input)

---

### Phase 3: AI Structured Spec Extraction (Haiku Batch)
**Script:** `scripts/enrich_specs_batch.py`
**Depends on:** Sub-Project 1 tables (`commodity_spec_schemas`, `material_spec_facets`) existing in DB

Extract commodity-specific structured specs for each card based on its category.

**Steps:**
1. Query `commodity_spec_schemas` to get the spec schema for each commodity
2. For each commodity group, batch cards with that category
3. Prompt includes: MPN, manufacturer, description, and the exact spec fields to extract
4. Haiku extracts: `{mpn, specs: {ddr_type: "DDR4", capacity_gb: 16, ...}, confidence_per_field: {...}}`
5. Write via `spec_write_service.record_spec()` (normalize units, validate enums, upsert facets)
6. Fields with confidence < 0.85 → skip (don't write bad data)

**Example prompt for DRAM cards:**
```
For each DRAM module, extract these specs from the MPN and description:
- ddr_type: one of DDR3, DDR4, DDR5, DDR5X, LPDDR4, LPDDR5
- capacity_gb: number (e.g., 8, 16, 32, 64)
- speed_mhz: number (e.g., 2400, 3200, 4800, 5600)
- ecc: true/false
- form_factor: one of DIMM, SO-DIMM, UDIMM, RDIMM, LRDIMM
```

**Expected yield:** 100-300K cards with structured specs (cards with meaningful category)
**Cost:** ~$20-40 via Batch API (larger prompts due to schema context)

---

### Phase 4: AI Description Generation (Sonnet — High Quality)
**Script:** `scripts/enrich_descriptions_batch.py`

Generate rich, professional descriptions for cards that have poor or missing descriptions.

**Steps:**
1. Query cards where:
   - `description IS NULL` or `description = ''`
   - `description` is < 30 chars (too short to be useful)
   - `enrichment_source = 'gradient_ai'` AND description is generic (optional upgrade pass)
2. For each card, assemble all available context:
   - MPN, manufacturer, category (from Phase 2)
   - Structured specs (from Phase 3)
   - Best sighting descriptions (from Phase 1)
   - Cross-references
3. Submit to Sonnet (higher quality than Haiku for prose):
   - Batch API for bulk, direct API for priority cards
   - Prompt: "Write a professional 2-3 sentence technical description..."
4. Output includes:
   - `description` (2-3 sentences, technical, no hallucinated specs)
   - `specs_summary` (key electrical specs, concise)
   - `confidence` (self-rated 0-1)
5. Only apply if confidence ≥ 0.90

**Example output:**
> "Samsung M393A2K43DB3-CWE is a 16GB DDR4-3200 ECC Registered DIMM designed for enterprise server and workstation applications. Features 2Rx8 memory organization, 1.2V operating voltage, and CAS latency of CL22. Compatible with Intel Purley/Whitley and AMD Rome/Milan platforms."

**Expected yield:** 200-400K cards with rich descriptions
**Cost:** ~$50-80 via Batch API (Sonnet, longer outputs)

---

### Phase 5: Cross-Verification & Confidence Scoring (Sonnet Spot-Check)
**Script:** `scripts/verify_enrichment.py`

Random-sample verification to validate the pipeline hit ≥95% accuracy.

**Steps:**
1. Sample 500 cards stratified by category (proportional to category size)
2. For each sampled card, send to Sonnet with web_search enabled:
   - "Verify these attributes for MPN {mpn}: category={cat}, description={desc}, specs={specs}"
   - Sonnet searches live web to cross-reference
3. Score each field: correct / incorrect / uncertain
4. Compute per-field accuracy:
   - category accuracy target: ≥ 95%
   - description accuracy target: ≥ 95% (no hallucinated specs)
   - structured spec accuracy target: ≥ 90% per field
5. If any field < target, identify failure patterns and re-run targeted correction

**Cost:** ~$10-15 (500 cards × Sonnet + web_search)
**Output:** Verification report with accuracy metrics per category and field

---

## Implementation Order

```
Phase 1: Mine sightings (free)          → ~3 hours dev, 0 API cost
Phase 2: Category classification        → ~2 hours dev, ~$20 API cost
    ↓ (requires Sub-Project 1 tables)
Phase 3: Structured spec extraction     → ~3 hours dev, ~$30 API cost
Phase 4: AI descriptions (Sonnet)       → ~2 hours dev, ~$65 API cost
Phase 5: Cross-verification             → ~1 hour dev, ~$12 API cost
```

**Total estimated API cost: ~$125**
**Total dev time: ~11 hours across sessions**

---

## Task Checklist

### Phase 1: Mine Existing Sighting Data
- [ ] Create `scripts/enrich_from_sightings.py`
- [ ] Build source-specific extractors for each connector's raw_data shape
- [ ] Implement merge logic (highest confidence source wins)
- [ ] Add dry-run mode (log what would change without writing)
- [ ] Add progress logging (every 1000 cards)
- [ ] Test with 100-card sample, validate extractions
- [ ] Run full extraction
- [ ] Write tests for extraction logic

### Phase 2: Category Classification
- [ ] Create `scripts/enrich_categories_batch.py` using Batch API
- [ ] Use 45-category taxonomy from faceted search design spec
- [ ] Implement confidence-gated application (≥0.90 only)
- [ ] Handle Batch API lifecycle (submit → poll → apply)
- [ ] Add fallback to real-time Haiku for small batches
- [ ] Run on all uncategorized/coarse cards
- [ ] Write tests

### Phase 3: Structured Spec Extraction (after Sub-Project 1 tables exist)
- [ ] Create `scripts/enrich_specs_batch.py`
- [ ] Build per-commodity prompt templates from `commodity_spec_schemas`
- [ ] Integrate with `spec_write_service.record_spec()`
- [ ] Unit normalization (uF→pF, kΩ→Ω, etc.)
- [ ] Enum validation against schema
- [ ] Run per-commodity extraction batches
- [ ] Write tests

### Phase 4: AI Description Generation
- [ ] Create `scripts/enrich_descriptions_batch.py`
- [ ] Assemble full context per card (specs, sightings, cross-refs)
- [ ] Use Sonnet Batch API for quality prose
- [ ] Confidence-gated application
- [ ] Generate `specs_summary` alongside description
- [ ] Run on all cards with missing/weak descriptions
- [ ] Write tests

### Phase 5: Cross-Verification
- [ ] Create `scripts/verify_enrichment.py`
- [ ] Stratified sampling by category
- [ ] Sonnet + web_search verification
- [ ] Accuracy report generation
- [ ] Targeted re-correction for failure patterns
- [ ] Write tests

---

## Architecture Decisions

1. **Scripts, not services** — These are one-time/periodic enrichment runs, not real-time features. Scripts in `scripts/` keep the service layer clean.

2. **Batch API first** — 50% cost reduction, no rate limits, same quality. Use `tagging_ai_batch.py` patterns for lifecycle management.

3. **Confidence gates everywhere** — Never write data below threshold. Bad data is worse than no data for the faceted search UI.

4. **Phase 1 is free and first** — Mining existing sighting data costs nothing and provides the highest-confidence data (it came from authoritative APIs). This also gives Haiku better context in Phases 2-3.

5. **Sonnet for descriptions, Haiku for classification** — Classification is a constrained task (pick from 45 options). Descriptions need nuance and accuracy. Match model capability to task complexity.

6. **Incremental, not all-at-once** — Each phase can run independently. If budget runs out, Phase 1+2 alone dramatically improve the materials tab.

---

## Safety & Rollback

- All scripts have `--dry-run` flag (default on)
- All writes are batched with `db.commit()` per batch (not one giant transaction)
- `enrichment_source` field tracks which script wrote the data
- Rollback: `UPDATE material_cards SET category = NULL, description = NULL WHERE enrichment_source = 'script_name'`
- Phase 5 verification catches systematic errors before they're visible to users
