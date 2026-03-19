# Faceted Materials Search — Design Spec

**Date:** 2026-03-19
**Status:** Draft
**Scope:** 4 sub-projects — data foundation, data population, faceted UI, AI search integration

---

## Problem

The Materials tab has 743K+ cards with a simple search bar and commodity pills. Users can't filter by technical specs (DDR type, capacitance, form factor, etc.). Categories were inaccurate (now being corrected via AI batch job). There's no structured spec data to power faceted filtering.

## Solution

A Newegg/Digi-Key-style faceted search with:
1. Left side panel with 2-level commodity tree
2. Commodity-specific sub-filters (checkboxes, ranges, booleans)
3. AI-assisted natural language search that pre-selects commodity + filters
4. Fast facet counts via denormalized index table

---

## Data Model

### New column on `material_cards`

```
specs_structured  JSONB  nullable  default NULL
```

Source of truth for all structured specs. Each key stores value + provenance:

```json
{
  "ddr_type": {"value": "DDR4", "source": "digikey_api", "confidence": 0.99, "updated_at": "2026-03-19T..."},
  "capacity_gb": {"value": 16, "source": "haiku_extraction", "confidence": 0.85, "updated_at": "2026-03-19T..."}
}
```

### New table: `commodity_spec_schemas`

Metadata registry — defines what specs each commodity has. Drives filter UI and data validation.

| Column | Type | Description |
|--------|------|-------------|
| id | serial PK | |
| commodity | varchar(100) | "dram", "capacitors" — matches `material_cards.category` |
| spec_key | varchar(100) | "ddr_type", "capacitance" |
| display_name | varchar(100) | "DDR Type", "Capacitance" |
| data_type | varchar(20) | "enum", "numeric", "boolean" |
| unit | varchar(20) | Display unit — "GB", "pF", "MHz" |
| canonical_unit | varchar(20) | Storage unit after normalization — always pF not uF/nF |
| enum_values | JSONB | `["DDR3", "DDR4", "DDR5"]` for enum types |
| numeric_range | JSONB | `{"min": 0, "max": 1000000}` for validation |
| sort_order | integer | Display order in filter sidebar |
| is_filterable | boolean | Show in facet sidebar? |
| is_primary | boolean | Show in card/list view summary? |

Unique constraint: `(commodity, spec_key)`

### New table: `material_spec_facets`

Denormalized, typed, indexed projection for fast faceted queries.

| Column | Type | Description |
|--------|------|-------------|
| id | serial PK | |
| material_card_id | integer FK | References `material_cards.id` ON DELETE CASCADE |
| category | varchar(100) | Denormalized from `material_cards.category` |
| spec_key | varchar(100) | "ddr_type", "capacitance_pf" |
| value_text | varchar(255) | For enum/string values: "DDR4", "X7R" |
| value_numeric | double precision | For range-filterable values: 100000000 (pF) |
| value_unit | varchar(20) | Canonical unit: "pF", "MHz" |

Unique constraint: `(material_card_id, spec_key)`

**Why not just JSONB?** Range queries (`capacitance > 100uF`) can't use GIN indexes. Facet count aggregations on extracted JSONB are 10-20x slower. The facet table gives 2-10ms queries vs 50-200ms on raw JSONB at 743K rows.

### Indexes on `material_spec_facets`

```sql
CREATE INDEX ix_msf_category_key ON material_spec_facets (category, spec_key);
CREATE INDEX ix_msf_category_key_text ON material_spec_facets (category, spec_key, value_text);
CREATE INDEX ix_msf_key_numeric ON material_spec_facets (spec_key, value_numeric) WHERE value_numeric IS NOT NULL;
CREATE INDEX ix_msf_key_text_card ON material_spec_facets (spec_key, value_text, material_card_id);
CREATE INDEX ix_msf_card ON material_spec_facets (material_card_id);
```

### New table: `material_spec_conflicts`

Audit log for when sources disagree on a spec value.

| Column | Type | Description |
|--------|------|-------------|
| id | serial PK | |
| material_card_id | integer FK | |
| spec_key | varchar(100) | |
| existing_value | varchar(255) | |
| existing_source | varchar(50) | |
| existing_confidence | double precision | |
| incoming_value | varchar(255) | |
| incoming_source | varchar(50) | |
| incoming_confidence | double precision | |
| resolution | varchar(20) | "kept_existing", "overwrote", "flagged" |
| resolved_by | varchar(50) | "auto" or user email |
| created_at | timestamp | |

---

## Commodity Taxonomy

Two-level tree. 11 parent groups (display only), ~45 sub-categories (filterable).

### Passives
- capacitors, resistors, inductors, transformers, fuses, oscillators, filters

### Semiconductors — Discrete
- diodes, transistors, mosfets, thyristors

### Semiconductors — ICs
- analog_ic, logic_ic, power_ic

### Processors & Programmable
- microcontrollers, microprocessors, dsp, fpga, asic, gpu

### Memory & Storage
- dram, flash, ssd, hdd

### Connectors & Electromechanical
- connectors, cables, relays, switches, sockets

### Power & Energy
- power_supplies, voltage_regulators, batteries

### Optoelectronics & Display
- leds, displays, optoelectronics

### Sensors & RF
- sensors, rf

### IT / Server Hardware
- motherboards, network_cards, raid_controllers, server_chassis, fans_cooling

### Misc
- motors, enclosures, tools_accessories, other

Parent groups are stored in a Python config dict (not a table) since they rarely change. Each sub-category maps to `material_cards.category`.

---

## Commodity-Specific Sub-Filters

Defined in `commodity_spec_schemas`. Top 15 commodities at launch:

### DRAM
- ddr_type (enum: DDR3, DDR4, DDR5, DDR5X, LPDDR4, LPDDR5)
- capacity_gb (numeric, range)
- speed_mhz (enum: common MT/s values)
- ecc (boolean)
- form_factor (enum: DIMM, SO-DIMM, UDIMM, RDIMM, LRDIMM)

### Capacitors
- capacitance (numeric, range, canonical unit: pF)
- voltage_rating (numeric, range, unit: V)
- dielectric (enum: X7R, X5R, C0G, Y5V, NP0)
- tolerance (enum: ±1%, ±5%, ±10%, ±20%)
- package (enum: 0402, 0603, 0805, 1206, 1210, through-hole)

### Resistors
- resistance (numeric, range, canonical unit: ohms)
- power_rating (numeric, range, unit: W)
- tolerance (enum: 0.1%, 1%, 5%)
- package (enum: 0402, 0603, 0805, 1206, through-hole)

### HDD
- capacity_gb (numeric, range)
- rpm (enum: 5400, 7200, 10000, 15000)
- form_factor (enum: 2.5", 3.5")
- interface (enum: SATA, SAS, NVMe)

### SSD
- capacity_gb (numeric, range)
- form_factor (enum: 2.5", M.2, U.2, mSATA)
- interface (enum: SATA, NVMe, SAS)
- read_speed_mbps (numeric, range)

### Connectors
- pin_count (numeric, range)
- pitch_mm (numeric, range)
- mounting (enum: through-hole, SMD, press-fit)
- gender (enum: male, female, genderless)
- series (enum: distinct values from data)

### Motherboards
- socket (enum: LGA1700, AM5, LGA4677, LGA1151, etc.)
- form_factor (enum: ATX, mATX, EATX, Mini-ITX)
- chipset (enum: distinct from data)
- ram_slots (numeric, range)

### CPU
- socket (enum)
- core_count (numeric, range)
- clock_speed_ghz (numeric, range)
- tdp_watts (numeric, range)
- architecture (enum: distinct from data)

### Power Supplies
- wattage (numeric, range)
- form_factor (enum: ATX, SFX, 1U server, 2U server, redundant)
- efficiency (enum: 80+ Bronze, Silver, Gold, Platinum, Titanium)

### GPU
- memory_gb (numeric, range)
- memory_type (enum: GDDR5, GDDR6, GDDR6X, HBM2, HBM3)
- interface (enum: PCIe 3.0, PCIe 4.0, PCIe 5.0)

### Inductors
- inductance (numeric, range, canonical unit: nH)
- current_rating (numeric, range, unit: A)
- package (enum)

### Diodes
- type (enum: rectifier, zener, Schottky, TVS)
- voltage (numeric, range, unit: V)
- current (numeric, range, unit: A)
- package (enum)

### MOSFETs
- type (enum: N-channel, P-channel)
- vds (numeric, range, unit: V)
- rds_on (numeric, range, unit: mOhm)
- id_max (numeric, range, unit: A)
- package (enum)

### Microcontrollers
- core (enum: ARM Cortex-M0, M3, M4, M7, RISC-V, AVR, PIC)
- flash_kb (numeric, range)
- ram_kb (numeric, range)
- clock_mhz (numeric, range)
- package (enum)

### Network Cards
- speed (enum: 1GbE, 10GbE, 25GbE, 40GbE, 100GbE)
- ports (numeric, range)
- interface (enum: PCIe, OCP, LOM)
- controller (enum: Intel, Broadcom, Mellanox)

Remaining ~30 commodities added incrementally by inserting rows into `commodity_spec_schemas` — no code changes needed.

---

## Frontend Architecture

### Layout

Two-column workspace replacing the current single-column list.

- **Left panel** (272px, sticky): commodity tree + dynamic sub-filters
- **Right area** (flex-1): search bar + results table + pagination
- **Bottom nav**: unchanged (10 tabs)

### New files

| File | Purpose |
|------|---------|
| `materials/workspace.html` | Layout shell — Alpine.js component, two-column grid |
| `materials/filters/tree.html` | Commodity tree partial (loaded once, cached 1hr) |
| `materials/filters/subfilters.html` | Dynamic sub-filters partial (loaded per commodity) |
| `materials/filters/_macros.html` | Reusable Jinja macros: checkbox_group, range_input, boolean_toggle |
| `materials/results.html` | Results table partial (loaded on every filter change) |

### State Management

Single Alpine.js `materialsFilter()` component at workspace level:
- Owns: `commodity`, `subFilters` (dict), `q`, `page`, `panelOpen`
- URL is canonical source of truth
- Filter params serialized as `?commodity=dram&sf_ddr_type=DDR4,DDR5&sf_capacity_min=8`
- `popstate` listener restores state from URL (back button works)
- `x-init="syncFromURL()"` handles deep links/bookmarks

Sub-filter params use `sf_` prefix — server parses generically without knowing commodity in advance.

### Filter interaction

**Desktop:** Instant-apply. Clicking a checkbox or changing a range immediately fires an HTMX request. Optimistic UI — filter visually toggles before results load.

**Mobile:** Batch-apply. Side panel is a left-slide drawer with backdrop. User selects filters, taps "Show Results". Floating "Filters" button with active count badge.

### Sub-filter rendering

Hybrid approach — server-driven schema + reusable Jinja macros:

1. User clicks commodity → HTMX loads `/v2/partials/materials/filters/sub?commodity=dram`
2. Server reads `commodity_spec_schemas` for "dram"
3. For each schema row, queries actual data for distinct values / min-max ranges
4. Renders using macros: `checkbox_group(key, label, options)`, `range_input(key, label, min, max, unit)`, `boolean_toggle(key, label)`
5. Each macro emits Alpine-bound inputs that update `subFilters` and call `applyFilters()`

### Facet counts

Counts shown next to each filter option (e.g., "DDR4 (1,250)"). When a filter is active, counts for OTHER filters adjust to reflect the intersection.

Query pattern on `material_spec_facets`:
```sql
SELECT spec_key, value_text, count(DISTINCT material_card_id)
FROM material_spec_facets
WHERE category = 'dram'
  AND material_card_id IN (subquery for active filters)
GROUP BY spec_key, value_text
```

Cached in Redis with 60s TTL, keyed on `facets:{category}:{filter_hash}`.

Updated counts returned via `hx-swap-oob` on every filter change.

### Performance

- Skeleton loading on results (animated gray bars in table row shape)
- Search input debounced at 400ms
- Checkbox/range filters: zero debounce, instant
- Commodity tree: loaded once, cached 1hr server-side
- Facet counts: Redis cached 60s TTL
- GIN index on `specs_structured` for ad-hoc queries
- B-tree indexes on `material_spec_facets` for faceted queries

---

## Data Pipeline

### Spec Write Service

Single entry point: `app/services/spec_write_service.py`

```
record_spec(db, card_id, spec_key, value, source, confidence) → None
```

1. Look up `commodity_spec_schemas` for this card's commodity + spec_key
2. Normalize unit (e.g., "100uF" → 100000000 pF)
3. Validate enum values against `enum_values` list
4. Conflict resolution vs existing value (see below)
5. Write to `specs_structured` JSONB on MaterialCard
6. Upsert into `material_spec_facets`
7. Log conflicts to `material_spec_conflicts`

### Source Priority

| Priority | Source | Rationale |
|----------|--------|-----------|
| 1 (highest) | DigiKey/Nexar/Mouser API | Manufacturer-authoritative |
| 2 | Newegg/Octopart scrape | Structured, usually accurate |
| 3 | AI extraction (Haiku) | Good baseline, hallucination risk |
| 4 (lowest) | Vendor sighting free-text | Unstructured, highest error rate |

### Conflict Resolution

- Higher priority source overwrites lower → log change
- Equal priority → keep higher confidence → log
- Lower priority source → keep existing → log
- Exception: new confidence ≥ 0.95 AND existing confidence < 0.80 → overwrite regardless of priority
- Close confidence (within 0.1) from different sources → flag for human review

### Data Sources

**Source 1: AI Extraction (Haiku batch)**
- Reads `commodity_spec_schemas` to know what to extract
- Batches of 50 cards per API call
- Covers all cards with descriptions
- Confidence scores per field

**Source 2: Newegg Scraper**
- Match by MPN → MaterialCard
- Parse structured spec tables from product pages
- Best for IT/consumer parts (DRAM, SSD, HDD, GPU, CPU, motherboards)

**Source 3: Vendor API Enrichment**
- Parse structured fields from DigiKey/Nexar/Mouser API responses
- Already available in `raw_data` JSON on Sighting records
- Extract on sighting creation

---

## AI Search Integration

When user types 3+ words (natural language), Haiku interprets the query and returns:
- Suggested commodity
- Suggested sub-filter values

The UI then:
1. Pre-selects the commodity in the tree
2. Pre-checks relevant sub-filters
3. Shows an "AI interpreted: DRAM, DDR5, 16GB+" chip (dismissible)
4. User can adjust any pre-selected filter

---

## Sub-Projects

### Sub-Project 1: Data Foundation
- `specs_structured` JSONB column + migration
- `commodity_spec_schemas` table + seed top 15 commodities
- `material_spec_facets` table + indexes
- `material_spec_conflicts` table
- Spec write service (normalize, validate, conflict resolve, facet sync)
- Tests

### Sub-Project 2: Data Population
- AI batch extraction (Haiku) for all cards with descriptions
- Vendor API enrichment from existing sightings
- Newegg scraper MVP (top commodities)
- Data quality validation

### Sub-Project 3: Faceted Search UI
- Two-column workspace layout
- Commodity tree
- Dynamic sub-filters (macros)
- Results partial with facet-aware queries
- URL state sync, Alpine.js component
- Mobile drawer
- Redis-cached facet counts
- Skeleton loading, optimistic UI

### Sub-Project 4: AI Search Integration
- Haiku query → commodity + sub-filter pre-selection
- "AI interpreted" chip UX
- Feedback loop

Dependencies: SP1 → SP2 → SP3 → SP4 (sequential)

---

## Out of Scope

- Full-text search within specs (just structured filters)
- User-defined custom filters
- Saved filter presets / bookmarks (URL bookmarks cover this)
- Comparison view (side-by-side parts)
- Price alerts on filtered results
