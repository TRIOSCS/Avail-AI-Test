# Vendor-API Parametric Enrichment — Design Spec

**Date:** 2026-06-17
**Status:** Design approved; pending spec review → implementation plan.
**Sub-project of:** the 6-month "filters built out" enrichment program (see
`project_filters_buildout_program_2026_06_17` memory).

## Goal

Build out the **deep materials filters** (capacitance / voltage / tolerance / package /
resistance / etc.) for the cards that matter, by harvesting **structured parametric specs**
from the distributor APIs TRIO already has working keys for — **Mouser and Element14** —
**demand-ordered** and **paced within their free daily quotas**.

## Why (measured context)

- Free enrichment is tapped at **~10% category coverage**: TRIO's own master has no category
  for ~90% of cards, and the descriptions for those cards are just the MPN (decoders +
  leading-token categorizer measured ~0 incremental lift).
- The **demanded subset is ~100% catalog-able**: top-demand cards (qty ≥ 1000, ~288k) are
  standard distributor parts (ceramic caps, chip resistors, MOSFETs, DRAM). MPNs are 99.8% clean.
- A live **Mouser probe hit 7/8** of top-demand MPNs — but the connector returns only
  manufacturer + distributor category + prose description and **discards the parametric
  `ProductAttributes`**. The parametrics exist in the raw API response; we just don't map them.
- Access reality: only **Mouser + Element14** are credentialed in the app's DB credential
  store. **Nexar** is plan-capped at 2,000 lookups (exhausted). **DigiKey** is not in the
  credential store (the `.env` keys 401'd on a direct probe). So this sub-project uses
  **Mouser + Element14**; restoring DigiKey / upgrading Nexar are optional throughput add-ons,
  out of scope here.
- Demand signal is loaded: `material_cards.sourced_qty_90d` populated on 717,486 cards (96.5%),
  so demand-ordered selection is available now.

## Scope

**In:** structured parametric specs + category from Mouser & Element14, written via the F1
ladder, run as a demand-ordered quota-paced bulk backfill.
**Out (explicitly):** Nexar plan upgrade, DigiKey credential restoration, Claude/web paid
enrichment, the OEM-spare/used/mechanical long tail (no distributor catalog data exists for it).
Clay (company/contact data only — wrong domain for parts).

## Architecture — three components

### 1. Connector parametric extension (the core new code)

The connectors (`app/connectors/mouser.py`, `element14.py`) already extract a few cross-part
core attributes (package, pin_count, lifecycle, rohs) via a named-parameter helper
(`digikey_parameter`/`generic_attribute` in `_core_attrs.py`) and return manufacturer +
distributor category + description. They **discard** commodity-specific parametrics.

**Change:** extend each connector's `_parse` to also extract commodity-specific parametrics
from the raw `ProductAttributes` (Mouser) / parameters (Element14) into a normalized
`specs: dict[str, value]` on each result dict, keyed by our **seeded spec keys**
(`app/data/commodity_seeds.json`, 49 commodities; e.g. capacitors →
`capacitance`/`voltage`/`dielectric`/`tolerance`/`package`; resistors →
`resistance`/`power`/`tolerance`/`package`).

The vendor attribute **names** vary per distributor/commodity, so the mapping is a
**per-commodity alias table** (vendor-attribute-name → seeded-spec-key), built by a defined
harvest step (NOT a guess): pull ~20 real responses per top-demand commodity, list the
attribute names Mouser/Element14 return, and map them to seeded keys. Values pass through the
same enum/unit normalization `record_spec` enforces; unmappable attributes are dropped (kept
observable in a `dropped` field, mirroring `DecodeResult`).

### 2. Vendor-API spec writer

A dedicated writer module `app/services/vendor_spec_enrich.py` (distinct from the existing
connector-desc *description* harvest, which writes at tier 84) that, given a card + its
connector result(s):
- writes category via `set_category(card, category, source, confidence)` and each parametric
  via `record_spec(...)` at the **existing** source tiers `mouser_api` / `element14_api`
  (= tier **90** in `spec_tiers.SOURCE_TIER`, above `connector_desc` 84 and `mpn_decode` 85 —
  distributor parametrics are high-trust). The F1 ladder arbitrates; run order is not
  load-bearing; no per-writer pre-gates.
- maps the distributor's category string → our commodity taxonomy (reuse/extend the existing
  category-normalization map; e.g. Mouser "Multilayer Ceramic Capacitors" → `capacitors`).

### 3. Demand-ordered, quota-paced bulk backfill CLI

`app/management/backfill_vendor_specs.py`, mirroring `backfill_oem_crosswalk.py`:
- **Selection:** cards needing enrichment (no category, or category-but-missing-facets),
  `sourced_qty_90d DESC NULLS LAST, id` — highest-demand first.
- **Pacing:** per-day request cap per connector (default tuned under Mouser ~1k/day +
  Element14), tracked via date-keyed Redis counters (`intel:vendor_api:{source}:calls:{date}`,
  same `intel_cache.incr_count` pattern as `enrichment_worker:web_calls`); stop at the cap,
  resume next invocation. Respects the connectors' existing circuit breakers + semaphores.
- **Resumable + idempotent:** re-running re-selects still-unenriched cards; `record_spec` at a
  fixed tier is idempotent. Per-chunk commit so a mid-run failure keeps completed work.
- **Metered:** request counts per source per day (the levers are quota, not dollars).
- Run on the host via `scripts/mgmt.sh backfill_vendor_specs [--apply] [--limit N]`
  (dry-run default, like the other backfills).

## Data flow

```
backfill_vendor_specs (demand-ordered select)
  → for each card (within daily quota):
      MouserConnector.search(mpn) / Element14Connector.search(mpn)   [extended _parse]
        → result.specs {seeded_key: value} + category + manufacturer
      → vendor_spec_enrich: set_category + record_spec at mouser_api/element14_api (tier 90)
        → F1 ladder arbitrates (won't clobber tier>90; beats decoders/desc at ≤85)
      → commit per chunk; increment per-source daily request counter
```

## Error handling

- Connector errors (rate-limit/auth/network) already raise typed `ConnectorError`s with
  breakers; the backfill catches per-card, logs, leaves the card for the next run (no poison).
- A 429 / quota-exhaustion on a source disables that source for the rest of the day's run
  (the cap is hit) — the other source continues.
- `record_spec` enum/unit validation rejects bad values per-spec (kept in `dropped`,
  observable); never poisons the card or the batch.
- Per-chunk commit; a failed commit rolls back only that chunk.

## Testing

- **Connector extension:** unit tests with recorded Mouser/Element14 response fixtures →
  assert `specs` maps the right seeded keys per commodity (caps/resistors/MOSFETs/DRAM), and
  unmappable attributes land in `dropped`, not `specs`.
- **Writer:** tests that category + specs write at tier 90 and the ladder correctly
  keeps/overrides vs lower/higher tiers (e.g. won't clobber `manual`/`trio_source` 95; beats
  `mpn_decode` 85).
- **Backfill CLI:** dry-run reports would-enrich counts; demand-ordered selection; daily-cap
  stops + resumes; idempotent re-run; per-source metering.
- Reuse the SQLite test harness; mock connector `search` (no live API in tests).

## Build sequence (each TDD, measure-gated)

1. **Harvest step (measure):** pull ~20 real Mouser + Element14 responses per top-demand
   commodity; produce the per-commodity attribute-name → seeded-key alias tables. Output is a
   data artifact the extension consumes.
2. **Connector parametric extension** (Mouser first, then Element14) + fixture tests.
3. **Vendor-API spec writer** (tier-90 ladder writes + category normalization) + tests.
4. **Backfill CLI** (demand-ordered, quota-paced, resumable, metered) + tests.
5. **Dry-run → small live batch (e.g. 200 top-demand cards) → verify** facets land + the
   materials filter coverage rises (via `enrichment_coverage_report`) → **full paced campaign**.

## Success criteria

- The materials filter facets (capacitance/voltage/tolerance/package/resistance/…) become
  populated for the high-demand commodity subset, measured by the rising facet-coverage in
  `enrichment_coverage_report`, demand-weighted.
- Spend stays $0 (free quotas); throughput bounded by Mouser+Element14 daily caps, demand-first
  so value lands early; campaign completes the high-demand subset well within the 6-month window.

## Revision 1 — 2026-06-17, post-harvest (SUPERSEDES the Mouser-ProductAttributes parts above)

The Task-2 harvest (measure gate, run before any code) invalidated the core assumption that
**Mouser** returns structured parametrics. Measured reality:
- **Mouser `ProductAttributes` = only `Packaging` + `Standard Pack Qty`** — no capacitance/voltage/etc.
  Mouser's search API does not carry structured parametrics.
- **Mouser `Description` is rich + consistent** — e.g. `"...MLCC...16V 0.1uF X7R 0402 10%"`,
  `"...Thick Film Resistors - SMD 0402 Zero ohms 5%"`. Carries capacitance/voltage/dielectric/
  package/tolerance/resistance in prose. Good quota (no rate-limit across 16 calls).
- **Element14 `attributes` ARE structured parametrics** (`Capacitance: 0.1`, `Capacitance
  Tolerance: ± 10%`, …) — clean, but **hard rate limit** (throttled after ~2-3 calls) + some MPN
  misses → low-throughput supplement only.

**Revised source strategy (approved):**
1. **Backbone — Mouser description parsing.** No Mouser connector change. Mouser's category +
   description already flow through the shipped `connector_desc` harvest (F1 ladder tier 84).
   The work is to **extend `app/services/desc_extractor/` to passive commodities** (capacitors,
   resistors, mosfets, and the other high-demand non-storage commodities) so the grammar parses
   capacitance/voltage/dielectric/package/tolerance/resistance from the description. This
   upgrades the EXISTING harvest path for free — no new tier, writes at `connector_desc` 84
   (or the card's own `desc_parse` 83 when the card already carries the distributor description).
2. **Supplement — Element14 structured attributes.** Extend `element14.py:_parse` to map its
   `attributes` (`attributeLabel`/`attributeValue`) → seeded facet keys, written at tier 90
   (`element14_api`). Run only on a bounded top-demand slice within its rate limit.

**Component changes vs. the original plan:**
- Original Component 1 (extend **Mouser** `_parse` for ProductAttributes) → **DROPPED** (no data).
- New Component 1a: **passive-commodity `desc_extractor` modules** + register them in the
  extractor's dispatch + `SPEC_COMMODITIES`. (Reuses the storage/memory module pattern.)
- New Component 1b: **Element14 `_parse` attribute mapping** (the supplement).
- Component 2 (writer) + Component 3 (backfill CLI) stand. The backfill, for a Mouser-sourced
  card, just ensures the category+description are present so the (now passive-aware) desc grammar
  fires; for the Element14 slice it writes structured specs at tier 90.

## Open decisions — all resolved

- Connectors: **Mouser + Element14** (the credentialed, working ones). ✓
- Output: **deep parametrics + category** (not category-only). ✓
- Throughput: **free quotas, demand-ordered, paced**; DigiKey/Nexar deferred. ✓
- Ladder tier: **existing `mouser_api`/`element14_api` = 90** (no new tier). ✓
- Run vehicle: **dedicated bulk backfill CLI**, not the paced worker (the extended connectors
  still benefit the worker as a free side-effect). ✓
