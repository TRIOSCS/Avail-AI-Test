# Materials Filter-Tree Spec Matrix

> Generated 2026-06-05 via multi-agent design pass (7 agents). Blueprint for the materials-tab faceted filter tree: global filters + trust layer + per-commodity parametric specs, mapped onto `CommoditySpecSchema`/`MaterialSpecFacet`. ~326 specs across ~50 commodities.


## Layer 1 — Global filters (apply to all parts)

- **Commodity / Category (the tree itself)** — `MaterialCard.category (lowercased, trimmed) → mapped to COMMODITY_TREE parent groups in app/services/commodity_registry.py` · Collapsible 2-level tree in the left rail (parent group > sub-category). Single-select sub-category drives commodity scoping; each node shows a live count from get_commodity_counts(). This is the spine of the whole filter layer — selecting a commodity is what unlocks the per-commodity parametric sub_filters.
  - _category is the only facet that changes which OTHER facets exist (parametric specs are commodity-scoped). It must be a tree, not a flat enum, because the taxonomy is already 2-level in COMMODITY_TREE. Already wired: get_commodity_counts() + tree.html partial exist._
- **Brand (dual-brand: OEM label OR actual maker — was "Manufacturer")** — `MaterialCard.brand (OEM label, indexed, migration 097) + MaterialCard.manufacturer (actual maker, indexed)` · ONE combined searchable checkbox facet (multi-select, OR-within, OR across BOTH columns). Top ~20 by deduped card count via get_manufacturer_options(commodity) (UNION ALL over both columns, COUNT(DISTINCT id) — a card with brand == manufacturer counts once; commodity scope applies inside both branches), with a type-ahead search box for the long tail. Heading renamed Manufacturer → Brand (display-only). Lives as a global facet ABOVE the commodity-specific sub_filters so it works even with no commodity selected. Result rows render `brand · manufacturer` ("IBM · Seagate Technology") when both set and distinct.
  - _Brand/maker is the single most common buyer narrowing dimension (cross/alternate sourcing, AVL compliance) and matches the buyer mental model: filtering "IBM" must match an IBM-labeled drive actually made by Seagate (brand=IBM via desc/fru evidence) AND filtering "Seagate Technology" must match the same card. Wire format is UNCHANGED — search_materials_faceted() still accepts manufacturers=[...] via the sub_filters 'manufacturers' key the router pops out (the OR predicate is a strict superset of the old single-column match, so old bookmarks keep working; no renames, no redirects). Writes are ladder-arbitrated via spec_tiers.set_brand/set_manufacturer — see SPEC_DUAL_BRAND_FILTERS + APP_MAP_INTERACTIONS writers table._
- **Lifecycle Status** — `MaterialCard.lifecycle_status (indexed; values active | nrnd | eol | obsolete | ltb per the model comment)` · Checkbox facet (multi-select, OR-within) with normalized display labels: Active, NRND, EOL, Obsolete, LTB. Counts per value. A primary, always-visible global facet.
  - _Obsolescence risk is a top-tier buyer concern for an electronic-component sourcing engine (last-time-buy planning, redesign avoidance). The column exists and is indexed. GAP TO CLOSE: lifecycle_status values are currently free-form strings — add a constants.LifecycleStatus StrEnum and a @validates normalizer so the facet has a stable closed value set (avoids 'NRND'/'nrfnd'/'Not Recommended' fragmenting the facet)._
- **RoHS Compliance** — `MaterialCard.rohs_status (values compliant | non-compliant | exempt per model comment)` · Checkbox facet (multi-select): Compliant, Non-compliant, Exempt, plus an implicit 'Unknown' bucket (NULL). Counts per value.
  - _RoHS is a hard regulatory gate for most buyers; the column already exists. Same normalization caveat as lifecycle — back it with a constants enum so values don't fragment._
- **REACH Compliance** — `DOES NOT EXIST as a column yet — derive from enrichment_provenance / specs_structured in the interim; recommend adding MaterialCard.reach_status (compliant | svhc_present | unknown) via Alembic` · Checkbox facet (Compliant, SVHC present, Unknown) once the column exists. Until then, do NOT expose it — an empty/always-'Unknown' facet is worse than no facet.
  - _REACH/SVHC is the regulatory twin of RoHS that buyers ask for, but unlike RoHS it has no backing column. Honest gap: this is a small migration (one nullable column + enum), populated from the distributor-parametric tier (DigiKey/Mouser expose REACH status). Flagging it rather than faking it is the root-cause-correct call._
- **Package / Case** — `MaterialCard.package_type (free-form: QFP-64, BGA-256, 0603, ...)` · GLOBAL has-it toggle / coarse grouping only at the global level (e.g. 'has package data'); the precise package picker belongs in the per-commodity sub_filters where enum_values are curated (capacitors 0402/0603/..., ICs QFP/BGA/...). Do not render a global free-text package facet — the cardinality is enormous and uncurated.
  - _package_type is global but its value space is only meaningful within a commodity (an 0603 means nothing for a CPU). The per-commodity 'package' spec in commodity_seeds.json already curates enum_values; keep the precise control there. Globally, only a coverage/has-data signal is useful._
- **Mounting (SMD / Through-Hole)** — `NOT a MaterialCard column — currently a per-commodity facet ('mounting' spec_key with SMD/through-hole/press-fit in capacitors, resistors, inductors, diodes, mosfets, connectors)` · Render inside the commodity sub_filters as it is today (checkbox facet). Recommend ALSO promoting it to a global facet by adding a MaterialSpecFacet row with a reserved global spec_key (or a MaterialCard.mounting column) so it filters across commodities.
  - _Mounting is conceptually global (SMT vs TH line selection) but is physically modeled per-commodity. Promoting it to a true global facet requires either a denormalized column or a convention where 'mounting' facets across all commodities are unioned. Recommendation: add MaterialCard.mounting (enum) so it's a clean global checkbox; the per-commodity 'mounting' specs become redundant and can be dropped to avoid double-modeling._
- **Has Datasheet** — `Derived: MaterialCard.datasheet_url IS NOT NULL` · Boolean toggle ('Has datasheet'). When on, adds WHERE datasheet_url IS NOT NULL. No counts needed beyond the resulting total.
  - _A buyer vetting parts wants only parts with verifiable documentation. It's a free filter — purely derived from an existing column, no new storage. Wire it as a top-level boolean in search_materials_faceted() (a new has_datasheet param) rather than via the facet table._

## Layer 2 — Trust / data-quality filters (the differentiator)

- **Enrichment Status (trust tier)** — `MaterialCard.enrichment_status (indexed; constants.MaterialEnrichmentStatus: unenriched | verified | web_sourced | ai_inferred | not_found)` · Multi-select checkbox facet rendered as a TRUST LADDER (ordered, color-coded): Verified (distributor-authoritative), Web-sourced, AI-inferred (flagged), Not found, Unenriched. Counts per tier. Default selection on first load = Verified + Web-sourced (the trustworthy set), with one click to widen. This is its own pinned section at the TOP of the filter rail, above commodity, because it qualifies the trustworthiness of every other facet value.
  - _This is the app's distinctive value. In a normal parts catalog every spec is implicitly 'true'; here, a spec's value is only as trustworthy as the tier that produced it. A buyer about to send an RFQ or commit a buy needs to filter to data they can stand behind. The plumbing already exists end-to-end: search_materials_faceted(statuses=[...]) and the legacy verified_only boolean are both implemented, the column is indexed, and the worker stamps the tier. Promoting it to a first-class, default-on filter (not a buried 'verified only' checkbox) is what turns provenance into a buyer-facing trust control._
- **Reconfirm Needed (AI guess flag)** — `MaterialCard.enrichment_provenance->>'reconfirm_needed' (boolean True, set only on ai_inferred cards in authoritative_enrichment_service.enrich_card)` · Boolean toggle with two modes: 'Hide unconfirmed AI guesses' (default ON → exclude rows where provenance.reconfirm_needed is true) and an inverse 'Show only items needing reconfirmation' for a data-steward review queue. Pair the inverse mode with a count badge so a reviewer can burn down the queue.
  - _ai_inferred is the only tier the system itself does not trust — it explicitly tags those cards reconfirm_needed=True so they are 'never mistaken for verified data' (per the enrich_card comment). Surfacing that flag as a filter does two jobs: protects buyers (hide guesses by default) and gives data stewards a worklist (show only guesses). Implementation note: provenance is JSONB, so add a generated/indexed predicate or a small boolean column reconfirm_needed mirrored on write — filtering on a JSONB key at scale wants an expression index._
- **Has Verified Specs** — `Composite: enrichment_status == 'verified' AND specs_enriched_at IS NOT NULL (and/or EXISTS a MaterialSpecFacet row for the card)` · Boolean toggle ('Has verified specs'). When on, restricts to cards in the verified tier that have actually completed the parametric spec-extraction pass — i.e. cards whose facet values are present AND came from the authoritative tier.
  - _enrichment_status == verified means the CORE fields were authoritative, but parametric facets are populated by a separate spec pass (specs_enriched_at gates it; only ~22 of 1,859 cards have facets today). A buyer filtering parametrically needs to distinguish 'verified core, no specs yet' from 'verified core WITH verified specs they can filter and trust'. This filter is the bridge between the trust tier and the parametric facets — without it, parametric filtering silently drops the 99% of cards that simply haven't had specs extracted yet, which looks like 'no results' rather than 'not yet enriched'._

## Layer 3 — Operational / sourcing filters

- **Demand (search count)** — `MaterialCard.search_count` · Numeric range slider (Min searches) plus quick chips: 'Any', 'Searched 1+', 'Hot (top decile)'. Default sort is already search_count DESC, so this filter pairs naturally with the existing ordering.
  - _search_count is the app's native demand signal — it already drives both result ordering and the enrichment worker's batch priority (select_batch orders by search_count DESC). Exposing it lets buyers/sales focus on parts the org actually quotes, and surfaces high-demand-but-unenriched parts worth attention. Cheap: existing indexed-ish column, top-level WHERE search_count >= N._
- **Recently Sourced / Searched** — `MaterialCard.last_searched_at` · Date-bucket chips (multi-select OR): Last 7d, 30d, 90d, Older. Implemented as last_searched_at >= now() - interval.
  - _Sales/buyers want parts with live, current demand vs stale history when prioritizing proactive outreach or buy plans. Column already exists; no new storage. (Recently QUOTED is a richer signal but lives in the quotes/offers tables — defer to a join-backed filter rather than over-claiming it as a MaterialCard field.)_
- **Availability / Stock** — `Derived from MaterialVendorHistory (count of vendor rows, min last_price, last_qty) — NOT a MaterialCard column` · Boolean 'Has vendor sightings / stock seen' toggle, plus optional 'Has price' toggle. Backed by EXISTS (SELECT 1 FROM material_vendor_history WHERE material_card_id = card.id). The faceted route already left-joins vendor stats (count, min price, currency) for display, so the data is on hand.
  - _Buyers narrow to parts they can actually buy. True real-time stock isn't tracked on the card, so be honest: model this as 'has any vendor sighting' / 'has a recorded price' rather than a fake live-stock number. The vendor-stats aggregation already runs in materials_faceted_partial; promote it from display-only to a filterable EXISTS predicate._
- **Internal Part vs Standard MPN** — `MaterialCard.is_internal_part (boolean, server_default false)` · Tri-state segmented control: All / Standard MPNs only / Internal parts only. Default = Standard MPNs only on the buyer catalog view (internal/custom PNs aren't sourceable externally and are excluded from enrichment anyway).
  - _is_internal_part already exists and the enrichment worker explicitly EXCLUDES internal parts from enrichment (select_batch filters is_internal_part.is_(False)). So internal parts will never have facets or trust tiers — surfacing the distinction prevents buyer confusion ('why is this part unenriched?') and lets internal-BOM workflows isolate custom PNs._
- **Has Cross-References / Substitutes** — `MaterialCard.cross_references (JSONB list of {mpn, manufacturer})` · Boolean toggle 'Has alternates / crosses'. WHERE jsonb_array_length(cross_references) > 0 (guard NULL → treat as 0).
  - _Cross-references are gold for sourcing — if the primary MPN is EOL/unavailable, a buyer immediately wants parts that already carry known alternates. Column exists (JSONB). Implementation note: use a Postgres expression index on jsonb_array_length(cross_references) if this filter is hot; the crosses_section.html partial already consumes this field so the data is real._

## UI rendering
RAIL LAYOUT (left filter rail, HTMX-driven, no JSON/SPA): top-to-bottom = (1) TRUST LADDER section pinned at top — enrichment_status checkboxes color-coded with counts, default-on = Verified + Web-sourced, plus the 'Hide AI guesses' and 'Has verified specs' toggles; (2) COMMODITY TREE — collapsible 2-level tree (parent group > sub-category) with per-node counts, single-select sub-category; (3) GLOBAL FACETS — Manufacturer (searchable checkbox, top-20 + type-ahead), Lifecycle, RoHS, Has-datasheet toggle; (4) OPERATIONAL — Demand slider, Recently-searched chips, Has-stock/Has-price/Has-crosses toggles, Internal-vs-standard segmented control; (5) COMMODITY-SPECIFIC SUB_FILTERS — only appears once a commodity is selected, populated by get_subfilter_options(commodity).

RENDERING RULES: primary specs (is_primary) render as removable CHIPS in a horizontal bar above the result list (top 1-3 per commodity per the seeds); numeric specs = range sliders seeded from the actual min/max returned by get_subfilter_options (numeric_map), labeled with unit + normalized to canonical_unit; enums = checkbox facets with live counts from get_facet_counts(); booleans = single toggles, and per the existing service ONLY render the toggle when facet rows actually back the spec (subfilters.html already guards this). Combine logic: AND across different facets, OR within a single facet's values — this is exactly what _apply_facet_filters already does (each spec_key gets its own .in_() subquery, ANDed together). Reuse the existing mechanism end-to-end: sub_filters JSON dict on the wire ({spec_key: [values], spec_key_min: n, spec_key_max: n, manufacturers: [...]}), parsed by _parse_filter_json, the router popping 'manufacturers' out, statuses passed as a comma-string. Each facet change fires an hx-get to /v2/partials/materials/faceted with hx-target on the result container (NOT inheriting the page-level hx-target='this', or it replaces the whole page — set an explicit hx-target on the faceted sub-container). Show live result counts and per-value counts everywhere; gray out (don't hide) zero-count values so the buyer sees what exists. Follow the existing partials: tree.html, manufacturers.html, subfilters.html, _macros.html — do not invent new conventions. Every facet value should display a count; the trust ladder values should additionally show a small provenance icon/color so the buyer reads trust at a glance.

COVERAGE-AWARE EMPTY STATES: because only ~22 of 1,859 cards have facets, parametric filtering will frequently return near-empty. The rail must show a coverage banner per commodity ('X of Y parts have verified specs') and, when a parametric filter yields few/zero results, offer a 'these parts haven't been spec-enriched yet' nudge rather than a bare 'no results' — otherwise the feature reads as broken.

## Population strategy
TRUST-GATED, NEVER-HALLUCINATE BACKFILL, populated in strict confidence order mirroring the existing enrich_card cascade (verified → web_sourced → ai_inferred → not_found).

TIER 1 — DISTRIBUTOR-PARAMETRIC (highest confidence, the only auto-trusted source): the verified tier already pulls structured parameters from DigiKey/Mouser/Element14/OEMSecrets/Nexar (SOURCE_ORDER in authoritative_enrichment_service). These connectors return typed parametric fields — extract them into MaterialSpecFacet rows tagged provenance source=<distributor>, confidence=1.0. Map each distributor parameter to the commodity's spec_key via a per-commodity mapping table, normalize units to canonical_unit, write through spec_write_service. Only these render under the default-on Verified trust gate.

TIER 2 — DATASHEET / WEB (behind the trust gate): for web_sourced-tier cards, parse the datasheet/authoritative pages for parametric values; write facets tagged source=web_search with the web result's confidence. Surface only when the buyer widens the trust ladder past Verified.

TIER 3 — AI-INFERRED (last, always flagged): only when Tiers 1-2 yield nothing, write facets from Opus inference tagged source=claude_opus_inferred AND keep enrichment_provenance.reconfirm_needed=True (already set in enrich_card). Excluded by the default 'Hide AI guesses' toggle; never count toward 'Has verified specs'. Never write a spec without provenance; never let a guess masquerade as Tier 1.

WIRING INTO THE PACED WORKER: add a per-commodity spec-extraction pass as a second phase inside run_one_batch (or a sibling stage) gated on specs_enriched_at IS NULL. After enrich_card resolves a card's tier, run the spec pass using get_batch_spec_schema()[commodity] as the target spec list; extract Tier-1 values first from the already-fetched distributor results (no extra API call), fall to Tier-2/3 only under the same web_daily_cap budget and circuit breaker already pacing the worker. Stamp specs_enriched_at on completion so it never re-processes. Order by search_count DESC (reuse existing demand priority) so high-demand parts get facets first. This closes the 22-of-1,859 gap incrementally, no big-bang backfill, no fabricated data: a facet exists only if a real source produced it, and its trust tier is always recorded and filterable.

SCHEMA/CONSTANTS PREREQUISITES (root-cause, via Alembic with rollback — no DDL in startup.py): (a) add constants.LifecycleStatus + RoHS/REACH StrEnums and @validates normalizers so global enum facets don't fragment; (b) add MaterialCard.reach_status and MaterialCard.mounting columns and drop the duplicated per-commodity 'mounting' specs; (c) mirror provenance.reconfirm_needed to an indexed predicate (expression index or boolean column) for fast filtering; (d) add expression indexes for jsonb_array_length(cross_references) and the has-datasheet predicate if hot. startup.py stays runtime-only (ANALYZE the facet table, seed distributor-parameter mapping defaults).

## Layer 4 — Per-commodity parametric specs


### Passives
_Designed against existing commodity_seeds.json conventions: capacitance pF, voltage V, resistance ohms, inductance nH (kept as-is for inductors), current A; package enum reuses chip-size codes + through-hole; mounting enum [SMD, through-hole, press-fit]. distributor-parametric (DigiKey/Mouser/Nexar structured params) is the verified high-confidence tier and is the source for nearly all passive parametrics. is_primary chips chosen as the 1-3 attributes a buyer narrows by first. Three already-seeded commodities (capacitors, resistors, inductors) were reviewed and EXTENDED. Normalization caveat: inductance canonical_unit stays nH (matches seed) but the numeric normalizer MUST scale uH/mH/H down to nH. Tolerance: capacitors seed uses '±X%' style while resistors seed uses bare 'X%' — I standardized NEW tolerance enums to '±X%' and recommend backfilling resistors for consistency (flagged, not auto-applied). For fuses/oscillators/filters (unseeded) all specs are fresh. AI-inferred avoided entirely for passives because distributor parametric coverage is excellent; datasheet fallback noted only for ESR/DCR/isolation/insertion-loss-class fields distributors sometimes omit._


#### `capacitors` — already seeded — review/extend
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| capacitance | Capacitance | numeric | pF | ★ | distributor-parametric | Existing seed kept. The #1 attribute every buyer filters by; DigiKey/Mouser expo |
| voltage_rating | Voltage Rating (V) | numeric | V | ★ | distributor-parametric | Existing seed kept. Rated DC voltage — a hard fit/derating constraint; buyers na |
| dielectric | Dielectric | enum |  | ★ | distributor-parametric | C0G, NP0, X7R, X5R, X6S, X7S, X7T, Y5V, Z5U |
| capacitor_type | Type | enum |  |  | distributor-parametric | MLCC / Ceramic, Aluminum Electrolytic, Tantalum, Polymer, Film, Supercapacitor, Mica, Trimmer / Variable |
| tolerance | Tolerance | enum |  |  | distributor-parametric | ±1%, ±2%, ±5%, ±10%, ±20%, +80%/-20% |
| package | Package / Size | enum |  |  | distributor-parametric | 0201, 0402, 0603, 0805, 1206, 1210, 1812, 2220, radial, axial, SMD can, through-hole |
| mounting | Mounting | enum |  |  | distributor-parametric | SMD, through-hole, press-fit |

#### `resistors` — already seeded — review/extend
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| resistance | Resistance | numeric | ohms | ★ | distributor-parametric | Existing seed kept. The headline value; normalize mOhm/ohm/kOhm/MOhm to ohms so  |
| power_rating | Power Rating (W) | numeric | W | ★ | distributor-parametric | Existing seed kept, promoted to primary. Power dissipation is a primary derating |
| tolerance | Tolerance | enum |  |  | distributor-parametric | ±0.01%, ±0.05%, ±0.1%, ±0.25%, ±0.5%, ±1%, ±2%, ±5%, ±10% |
| resistor_type | Type | enum |  |  | distributor-parametric | Thick Film, Thin Film, Metal Film, Carbon Film, Wirewound, Metal Foil, Current Sense / Shunt, Array / Network, Variable / Potentiometer |
| temperature_coefficient | Temp Coefficient (ppm/°C) | numeric | ppm/°C |  | distributor-parametric | NEW. TCR is the key stability spec for precision resistors; buyers gate on <=25  |
| package | Package / Size | enum |  |  | distributor-parametric | 0201, 0402, 0603, 0805, 1206, 1210, 2010, 2512, axial, through-hole |
| mounting | Mounting | enum |  |  | distributor-parametric | SMD, through-hole, press-fit |

#### `inductors` — already seeded — review/extend
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| inductance | Inductance | numeric | nH | ★ | distributor-parametric | Existing seed kept. Headline value; canonical_unit stays nH (matches seed) but n |
| current_rating | Current Rating (A) | numeric | A | ★ | distributor-parametric | Existing seed kept, promoted to primary. Rated (Irms) current is a hard thermal/ |
| inductor_type | Type | enum |  |  | distributor-parametric | Ferrite Bead, Power / Shielded, RF / Wirewound, Multilayer, Molded, Coupled / Common-Mode, Air Core, Toroidal |
| dcr_mohm | DC Resistance (mΩ) | numeric | mOhm |  | distributor-parametric | NEW. DCR drives I²R loss/efficiency in power converters and is a primary selecti |
| saturation_current | Saturation Current (A) | numeric | A |  | distributor-parametric | NEW. Isat (where L drops ~30%) is the real limit in switching supplies, distinct |
| shielding | Shielding | enum |  |  | distributor-parametric | Shielded, Semi-Shielded, Unshielded |
| package | Package / Size | enum |  |  | distributor-parametric | 0201, 0402, 0603, 0805, 1206, 1210, 1812, radial, toroid, through-hole |
| mounting | Mounting | enum |  |  | distributor-parametric | SMD, through-hole, press-fit |

#### `transformers` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| transformer_type | Type | enum |  | ★ | distributor-parametric | Power, Gate Drive, Signal / Audio, Pulse, Current Sense, Ethernet / LAN / PoE, RF / Balun, Isolation, Flyback, Toroidal |
| turns_ratio | Turns Ratio | enum |  | ★ | distributor-parametric | 1:1, 1:1.1, 1:1.5, 1:2, 2:1, 1:3, 1:4, 1:5, 1:10, Center-Tapped, Multiple / Custom |
| power_rating | Power Rating (W) | numeric | W | ★ | distributor-parametric | For power transformers this is the headline sizing constraint; buyers filter by  |
| primary_voltage | Primary Voltage (V) | numeric | V |  | distributor-parametric | Input/primary winding voltage is a hard fit constraint for power/isolation trans |
| secondary_voltage | Secondary Voltage (V) | numeric | V |  | distributor-parametric | Output/secondary winding voltage is the other half of the fit constraint buyers  |
| isolation_voltage | Isolation Voltage (Vrms) | numeric | Vrms |  | distributor-parametric | Dielectric/isolation rating (Hipot) is a safety-critical selection spec for isol |
| mounting | Mounting | enum |  |  | distributor-parametric | SMD, through-hole, chassis, DIN rail |

#### `fuses` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| current_rating | Current Rating (A) | numeric | A | ★ | distributor-parametric | The defining spec of a fuse — buyers select by trip current first. Always presen |
| voltage_rating | Voltage Rating (V) | numeric | V | ★ | distributor-parametric | Max interrupt voltage is a hard safety constraint that must meet/exceed circuit  |
| fuse_type | Type | enum |  | ★ | distributor-parametric | Cartridge, Chip / SMD, Blade / Automotive, PTC Resettable, Thermal Cutoff, Glass, Ceramic, Subminiature |
| response_time | Response Time | enum |  |  | distributor-parametric | Fast Acting, Medium / Time Lag, Slow Blow, Very Fast Acting |
| breaking_capacity | Breaking Capacity (A) | numeric | A |  | distributor-parametric | Interrupt/breaking rating (max fault current safely cleared) is a safety-complia |
| package | Package / Size | enum |  |  | distributor-parametric | 0402, 0603, 0805, 1206, 1812, 2410, 5x20mm, 6.3x32mm, blade, through-hole |
| mounting | Mounting | enum |  |  | distributor-parametric | SMD, through-hole, holder / cartridge, panel |

#### `oscillators` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| frequency | Frequency (MHz) | numeric | MHz | ★ | distributor-parametric | The defining parameter — buyers select an oscillator/crystal by frequency first. |
| oscillator_type | Type | enum |  | ★ | distributor-parametric | Crystal (XTAL), XO (Standard), TCXO, VCXO, OCXO, VCTCXO, MEMS, Ceramic Resonator, SAW, Programmable |
| frequency_stability | Frequency Stability (ppm) | numeric | ppm | ★ | distributor-parametric | Total stability over temp is the key quality differentiator (a buyer needing <=± |
| supply_voltage | Supply Voltage (V) | enum |  |  | distributor-parametric | 1.8V, 2.5V, 2.8V, 3.0V, 3.3V, 5.0V, 1.6V-3.6V |
| load_capacitance | Load Capacitance (pF) | numeric | pF |  | distributor-parametric | For crystals/resonators, load cap must match the host circuit or frequency pulls |
| output_type | Output Type | enum |  |  | distributor-parametric | HCMOS, LVCMOS, LVPECL, LVDS, CML, Clipped Sine, Sine Wave |
| package | Package / Size | enum |  |  | distributor-parametric | 2016 (2.0x1.6mm), 2520 (2.5x2.0mm), 3225 (3.2x2.5mm), 5032 (5.0x3.2mm), 7050 (7.0x5.0mm), HC-49, DIP-8, DIP-14, through-hole |

#### `filters` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| filter_type | Filter Type | enum |  | ★ | distributor-parametric | EMI / EMC, Low Pass, High Pass, Band Pass, Band Reject / Notch, SAW, BAW, Ceramic, LC, Crystal, Common Mode Choke, Feedthrough, Diplexer |
| center_frequency | Center / Cutoff Frequency (MHz) | numeric | MHz | ★ | distributor-parametric | Center frequency (band-pass) or cutoff (LP/HP) is the headline tuning parameter  |
| bandwidth | Bandwidth (MHz) | numeric | MHz | ★ | distributor-parametric | Passband width is the key selectivity spec for RF/band-pass filters; designers g |
| impedance | Impedance (Ω) | enum |  |  | distributor-parametric | 50Ω, 75Ω, 100Ω, 150Ω, 200Ω, 300Ω, Other |
| insertion_loss | Insertion Loss (dB) | numeric | dB |  | distributor-parametric | Passband insertion loss is a core RF-filter quality spec buyers minimize. Distri |
| current_rating | Current Rating (A) | numeric | A |  | distributor-parametric | For EMI/power-line filters and common-mode chokes, rated current is a hard sizin |
| package | Package / Size | enum |  |  | distributor-parametric | 0402, 0603, 0805, 1206, 1210, 1806, 2220, module, through-hole |

### Semiconductors — Discrete + Semiconductors — ICs
_Read all three ground-truth files: app/data/commodity_seeds.json (existing 16-commodity seed set), app/models/faceted_search.py (CommoditySpecSchema/MaterialSpecFacet shape), app/services/commodity_registry.py (COMMODITY_TREE taxonomy + idempotent seeder).

IMPORTANT IMPLEMENTATION NOTE for whoever writes these into commodity_seeds.json: the live CommoditySpecSchema model and existing seeds use canonical_unit (storage/normalization unit) alongside unit (display). This output schema only carries 'unit', so when persisting set canonical_unit equal to unit for every numeric spec here (V, A, mOhm, nC, MHz, kHz, channels, bits, W) — that matches existing convention (e.g. capacitance pF/pF, vds V/V). Also add sort_order in array order and is_filterable=true.

DESIGN CONVENTIONS FOLLOWED:
- Matched existing seed style: numeric specs carry a unit; enums carry enum_values; shared mounting enum [SMD, through-hole, press-fit]; package modeled as enum. The two pre-existing discrete seeds (diodes, mosfets) had EMPTY package enum_values and lacked populatable_from — I populated standard case codes and reviewed/extended them.
- populatable_from leans on 'distributor-parametric' (verified DigiKey/Mouser/Nexar tier = highest confidence) for nearly all electrical/package specs, since these commodities have rich structured parametric coverage. Only hFE-min (transistors) and holding-current (thyristors) are 'datasheet' tier because distributors report them inconsistently. NO 'ai-inferred' specs were used — every proposed spec is reliably populatable from structured sources, deliberately avoiding hallucination risk in the enrichment worker.

REVIEW/EXTEND of the two seeded commodities:
- diodes: extended Type enum (+switching, fast-recovery, PIN, varactor, bridge); clarified voltage->Reverse Voltage Vr and current->Forward Current If; ADDED vf_max and zener_voltage; populated package enum_values; promoted voltage+current to is_primary alongside type.
- mosfets: extended channel_type enum (+dual/complementary); PROMOTED rds_on to is_primary (dominant figure-of-merit, previously non-primary); ADDED vgs_th and qg_max; populated package enum_values; clarified Vds display name.

NEW commodities (transistors, thyristors, analog_ic, logic_ic, power_ic): fresh sets of 7-8 specs each with 2-3 headline primaries. For the three IC sub-categories, 'function' is the indispensable primary because these are broad buckets a buyer can only enter by role; voltage + current/channels are the secondary primaries. NOTE: MOSFETs are deliberately excluded from the transistors 'transistor_type' enum since they are their own sub-category — no overlap/double-counting.

ENRICHMENT-WORKER GUIDANCE: spec_keys are stable snake_case identifiers safe for MaterialSpecFacet.spec_key. Voltage-pair specs (vout_min/vout_max, supply_voltage_min/max) intentionally model adjustable ranges as two numeric facets so range-slider faceting works on adjustable parts. No numeric_range bounds were specified for new specs (left for the seeder default / null) to avoid clamping legitimate outliers; tighten later from observed data distributions. Sub-category keys match COMMODITY_TREE exactly: diodes, transistors, mosfets, thyristors, analog_ic, logic_ic, power_ic._


#### `diodes` — already seeded — review/extend
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| type | Diode Type | enum |  | ★ | distributor-parametric | rectifier, Schottky, zener, TVS, switching, fast-recovery, PIN, varactor, bridge |
| voltage | Reverse Voltage Vr (V) | numeric | V | ★ | distributor-parametric | Repetitive reverse voltage (Vrrm) is the primary electrical rating buyers filter |
| current | Forward Current If (A) | numeric | A | ★ | distributor-parametric | Average rectified forward current (Io/If) — the second core rating. Range slider |
| vf_max | Forward Voltage Vf (V) | numeric | V |  | distributor-parametric | Vf at rated current drives conduction loss; key selection criterion especially f |
| zener_voltage | Zener Voltage Vz (V) | numeric | V |  | distributor-parametric | The defining parameter for Zener diodes (null for non-Zener). New addition — buy |
| package | Package / Case | enum |  |  | distributor-parametric | SOD-123, SOD-323, SOD-523, SOT-23, SMA, SMB, SMC, DO-214AC, DO-214AA, DO-214AB, DO-41, DO-201, DO-35, TO-220, TO-263 |
| mounting | Mounting | enum |  |  | distributor-parametric | SMD, through-hole, press-fit |

#### `transistors` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| transistor_type | Transistor Type | enum |  | ★ | distributor-parametric | BJT NPN, BJT PNP, JFET, IGBT, Darlington, RF/microwave, digital/bias, phototransistor |
| vceo | Collector-Emitter Voltage Vceo (V) | numeric | V | ★ | distributor-parametric | Primary voltage rating for BJT/IGBT (Vces for IGBT maps here). Range slider; cor |
| ic_max | Collector Current Ic (A) | numeric | A | ★ | distributor-parametric | Continuous collector current — the second core rating. Range slider; present on  |
| power_dissipation | Power Dissipation Pd (W) | numeric | W |  | distributor-parametric | Max device power dissipation distinguishes small-signal from power transistors;  |
| hfe_min | DC Gain hFE (min) | numeric |  |  | datasheet | Minimum current gain matters for BJT biasing. Dimensionless; distributors someti |
| ft_mhz | Transition Frequency fT (MHz) | numeric | MHz |  | distributor-parametric | Bandwidth/speed selector separating RF/switching from general-purpose BJTs. Dist |
| package | Package / Case | enum |  |  | distributor-parametric | SOT-23, SOT-223, SOT-323, SOT-89, TO-92, TO-126, TO-220, TO-247, TO-263, TO-3P, DPAK, D2PAK |
| mounting | Mounting | enum |  |  | distributor-parametric | SMD, through-hole, press-fit |

#### `mosfets` — already seeded — review/extend
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| channel_type | Channel | enum |  | ★ | distributor-parametric | N-channel, P-channel, dual N-channel, dual P-channel, complementary |
| vds | Drain-Source Voltage Vds (V) | numeric | V | ★ | distributor-parametric | Breakdown voltage is the primary rating buyers narrow by. Kept from existing see |
| rds_on | Rds(on) (mΩ) | numeric | mOhm | ★ | distributor-parametric | On-resistance is THE figure of merit for power MOSFETs — promoted to primary (ex |
| id_max | Drain Current Id (A) | numeric | A |  | distributor-parametric | Continuous drain current rating. Kept from existing seed. |
| vgs_th | Gate Threshold Vgs(th) (V) | numeric | V |  | distributor-parametric | Logic-level vs standard-gate selection (e.g. <2.5V threshold). New addition — di |
| qg_max | Gate Charge Qg (nC) | numeric | nC |  | distributor-parametric | Total gate charge drives switching loss and drive sizing in SMPS designs. New ad |
| package | Package / Case | enum |  |  | distributor-parametric | SOT-23, SOT-223, SOT-23-6, DPAK, D2PAK, TO-220, TO-247, TO-251, TO-252, PowerPAK SO-8, DFN, QFN, LFPAK |
| mounting | Mounting | enum |  |  | distributor-parametric | SMD, through-hole, press-fit |

#### `thyristors` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| thyristor_type | Thyristor Type | enum |  | ★ | distributor-parametric | SCR, TRIAC, DIAC, GTO, SIDAC, SCR module |
| vdrm | Off-State Voltage Vdrm (V) | numeric | V | ★ | distributor-parametric | Peak repetitive off-state/blocking voltage is the primary rating (Vdrm/Vrrm). Ra |
| it_rms | On-State Current It(RMS) (A) | numeric | A | ★ | distributor-parametric | RMS on-state current is the second core rating buyers size by. Range slider; dis |
| igt_max | Gate Trigger Current Igt (mA) | numeric | mA |  | distributor-parametric | Gate trigger current determines drive-circuit compatibility (sensitive-gate vs s |
| ith_holding | Holding Current Ih (mA) | numeric | mA |  | datasheet | Holding current matters for latch behavior in low-current loads. Less consistent |
| package | Package / Case | enum |  |  | distributor-parametric | TO-92, TO-220, TO-220AB, TO-251, TO-252, TO-247, D2PAK, DPAK, SOT-223, TO-263, module |
| mounting | Mounting | enum |  |  | distributor-parametric | SMD, through-hole, press-fit |

#### `analog_ic` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| function | Function | enum |  | ★ | distributor-parametric | op-amp, comparator, instrumentation amp, ADC, DAC, voltage reference, analog switch/mux, current sense amp, PGA, sample-and-hold, video amp, audio amp |
| channels | Channels | numeric | channels | ★ | distributor-parametric | Number of amps/channels per package (single/dual/quad) is a top filter for op-am |
| supply_voltage_max | Max Supply Voltage (V) | numeric | V | ★ | distributor-parametric | Operating supply range defines rail compatibility (single 3.3V vs ±18V). Range s |
| bandwidth_mhz | Gain-Bandwidth / Bandwidth (MHz) | numeric | MHz |  | distributor-parametric | GBW for amplifiers and sample rate context for converters — key performance scre |
| resolution_bits | Resolution (bits) | numeric | bits |  | distributor-parametric | ADC/DAC resolution (8/10/12/16/24-bit) is the defining converter spec (null for  |
| interface | Digital Interface | enum |  |  | distributor-parametric | SPI, I2C, parallel, analog, LVDS, none |
| package | Package / Case | enum |  |  | distributor-parametric | SOT-23-5, SOT-23-6, SC-70, SOIC-8, SOIC-14, MSOP-8, MSOP-10, TSSOP, QFN, DFN, WLCSP, DIP-8, LQFP |

#### `logic_ic` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| function | Logic Function | enum |  | ★ | distributor-parametric | gate, buffer/driver, flip-flop, latch, counter, shift register, multiplexer, decoder/encoder, comparator, level shifter, transceiver, FIFO |
| logic_family | Logic Family | enum |  | ★ | distributor-parametric | 74HC, 74HCT, 74LVC, 74LV, 74AHC, 74AHCT, 74AUP, 74LS, 74ALS, 74F, CD4000, AUC, AXC |
| supply_voltage_min | Min Supply Voltage (V) | numeric | V |  | distributor-parametric | Lower rail bound for low-voltage operation (1.65V/1.8V parts). Range slider; dis |
| supply_voltage_max | Max Supply Voltage (V) | numeric | V |  | distributor-parametric | Upper rail bound — pairs with min to define operating window for rail matching.  |
| channels | Number of Elements / Bits | numeric | bits |  | distributor-parametric | Gates per package or bit width (e.g. 1/2/4/8-bit transceiver) is a common narrow |
| max_freq_mhz | Max Frequency (MHz) | numeric | MHz |  | distributor-parametric | Max toggle/clock frequency separates high-speed from general logic. Distributor  |
| output_type | Output Type | enum |  |  | distributor-parametric | push-pull, open-drain, open-collector, tri-state |
| package | Package / Case | enum |  |  | distributor-parametric | SOT-23-5, SOT-23-6, SC-70, SOIC-14, SOIC-16, TSSOP-14, TSSOP-16, TSSOP-20, TSSOP-24, QFN, DIP-14, DIP-16, WLCSP |

#### `power_ic` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| function | PMIC Function | enum |  | ★ | distributor-parametric | LDO regulator, buck (step-down), boost (step-up), buck-boost, switching controller, battery charger, load switch, power MUX / OR-ing, hot-swap, supervisor / reset, gate driver, PMIC (multi-rail), isolated DC-DC |
| vin_max | Max Input Voltage Vin (V) | numeric | V | ★ | distributor-parametric | Max input rating defines bus compatibility (e.g. 36V automotive vs 6V portable)  |
| iout_max | Max Output Current (A) | numeric | A | ★ | distributor-parametric | Output current capability is the core sizing parameter for regulators/load switc |
| vout_min | Min Output Voltage (V) | numeric | V |  | distributor-parametric | Lower output bound; with vout_max defines the adjustable/fixed rail range buyers |
| vout_max | Max Output Voltage (V) | numeric | V |  | distributor-parametric | Upper output bound — pairs with vout_min to bracket the target rail. Distributor |
| switching_freq_khz | Switching Frequency (kHz) | numeric | kHz |  | distributor-parametric | Switching frequency trades off efficiency vs solution size for switchers (null f |
| topology | Topology / Output Config | enum |  |  | distributor-parametric | fixed, adjustable, single-output, dual-output, multi-output, synchronous, non-synchronous |
| package | Package / Case | enum |  |  | distributor-parametric | SOT-23-5, SOT-23-6, SOT-223, SOT-89, SOIC-8, MSOP-8, MSOP-10, TSSOP, QFN, DFN, PowerPAK, TO-220, TO-263, WLCSP |

### Processors & Programmable
_Scope: parametric specs only; global attrs (manufacturer, lifecycle_status, package_type, rohs_status, pin_count, datasheet_url, description) deliberately excluded per the global-filters agent ownership. Units/enums follow existing commodity_seeds.json conventions. numeric_range omitted from spec objects since the output schema does not accept it per-spec (it lives on CommoditySpecSchema and can be added at seed-write time: e.g. MCU supply_voltage 1.0-5.5, MPU/DSP/ASIC voltage down to 0.5-0.8 floor, GPU tdp up to ~1000W).

Confidence/source strategy by tier:
- MCU, microprocessors, DSP, FPGA: best distributor-parametric coverage (DigiKey/Mouser/Nexar expose Core, Speed, Memory, I/O as structured params) - should populate at verified tier with high confidence.
- CPU, GPU: broker/server-market parts; distributor parametrics are thin, so most specs are title-parse (family, core/memory) or datasheet (TDP, socket). Architecture/cooling flagged ai-inferred - must be confidence-flagged by the enrichment worker.
- ASIC: intentionally minimal. True ASICs have NO distributor parametric tier; only coarse application/process/interface filters are honestly populatable, mostly ai-inferred or datasheet. Do not over-engineer this commodity.

Recommended changes to the 3 SEEDED commodities:
1. microcontrollers: collapse the 5 boolean peripheral facets (has_uart/has_spi/has_i2c/has_usb/has_can) into ONE multi-select 'peripherals' enum - cleaner facet UI, extensible, maps to distributor 'Peripherals/Connectivity'. Add 'gpio_count'. Expand 'core' enum (Cortex-M0+, M33, 8051, MSP430, Xtensa). Promote flash_kb to primary.
2. cpu: POPULATE the empty 'socket' and 'architecture' enum_values (they shipped empty, making the facets unusable). Expand 'family'. Promote family to primary.
3. gpu: expand memory_type (HBM2e/HBM3e), interface (SXM4/SXM5/OAM for datacenter), family (Instinct/Arc/L-series); raise tdp ceiling to ~1000W; add 'cooling'. Promote gpu_family + memory_gb to primary.

Unit notes for normalization: FPGA logic_elements uses canonical 'KLE' (thousands of logic elements, normalizes LE vs LUT counts); FPGA block_ram uses 'Kb' (kilobit) - DISTINCT from MCU/memory 'KB' (kilobyte); the enrichment worker must not conflate them. Clock units kept as MHz for MCU/MPU/DSP and GHz for CPU to match each market's convention.

File touchpoints for implementation: add the 4 new commodities (microprocessors, dsp, fpga, asic) and the revised 3 to app/data/commodity_seeds.json; seed_commodity_schemas() in app/services/commodity_registry.py is idempotent and skips existing (commodity, spec_key) pairs, so re-seeding only inserts new rows - the boolean->enum MCU change and any enum_values backfill on existing rows will need an Alembic data migration, not just a seed re-run._


#### `microcontrollers` — already seeded — review/extend
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| core | Core | enum |  | ★ | distributor-parametric | ARM Cortex-M0, ARM Cortex-M0+, ARM Cortex-M3, ARM Cortex-M4, ARM Cortex-M7, ARM Cortex-M33, RISC-V, AVR, PIC, 8051, MSP430, Xtensa |
| flash_kb | Flash (KB) | numeric | KB | ★ | distributor-parametric | Program memory size is a top sizing constraint; DigiKey/Mouser expose 'Program M |
| ram_kb | RAM (KB) | numeric | KB |  | distributor-parametric | On-chip SRAM size; standard distributor parametric ('RAM Size'). Common secondar |
| clock_mhz | Max Clock (MHz) | numeric | MHz |  | distributor-parametric | Max core speed; distributor parametric. Renamed to 'Max Clock' for clarity vs ty |
| supply_voltage | Supply Voltage (V) | numeric | V |  | distributor-parametric | Operating voltage range (1.0-5.5V); distributor parametric. Keep range bound as  |
| gpio_count | I/O Count | numeric | pins |  | distributor-parametric | Number of programmable I/O — a real design constraint distributors expose as 'Nu |
| peripherals | Peripherals | enum |  |  | distributor-parametric | UART, SPI, I2C, USB, CAN, CAN-FD, Ethernet, I2S, SDIO, ADC, DAC, PWM |

#### `cpu` — already seeded — review/extend
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| family | Family | enum |  | ★ | title-parse | Xeon, Xeon Scalable, Core i-series, Ryzen, EPYC, Threadripper, Atom, Pentium, Celeron, ARM |
| socket | Socket | enum |  | ★ | datasheet | LGA1700, LGA1200, LGA1151, LGA2066, LGA3647, LGA4189, LGA4677, AM4, AM5, SP3, SP5, TR4, sTRX4 |
| core_count | Core Count | numeric | cores | ★ | title-parse | Core count is a primary performance/sizing dimension and is almost always in the |
| clock_speed_ghz | Base Clock (GHz) | numeric | GHz |  | title-parse | Base frequency; commonly in title. Renamed to 'Base Clock' to disambiguate from  |
| tdp_watts | TDP (W) | numeric | W |  | datasheet | Thermal/power budget for chassis selection; from datasheet/ARK, not usually in t |
| architecture | Architecture / Generation | enum |  |  | ai-inferred | Sapphire Rapids, Ice Lake, Cascade Lake, Skylake, Cooper Lake, Emerald Rapids, Zen 2, Zen 3, Zen 4, Zen 5 |

#### `microprocessors` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| core | Core | enum |  | ★ | distributor-parametric | ARM Cortex-A53, ARM Cortex-A55, ARM Cortex-A7, ARM Cortex-A9, ARM Cortex-A72, ARM Cortex-A78, RISC-V, PowerPC, MIPS, x86 |
| core_count | Core Count | numeric | cores | ★ | distributor-parametric | Single vs multi-core (1/2/4) is a primary sizing decision; distributor 'Number o |
| max_clock_mhz | Max Clock (MHz) | numeric | MHz |  | distributor-parametric | Max core speed; distributor parametric ('Speed'). MHz keeps it consistent with M |
| supply_voltage | Supply Voltage (V) | numeric | V |  | distributor-parametric | Core/IO voltage; distributor parametric. Lower floor than MCUs since MPUs run su |
| graphics | Integrated Graphics | boolean |  |  | datasheet | Whether the MPU has an on-chip GPU/display controller — a real selection criteri |
| memory_interface | Memory Interface | enum |  |  | distributor-parametric | DDR3, DDR3L, DDR4, LPDDR2, LPDDR3, LPDDR4 |

#### `dsp` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| core | Core / Family | enum |  | ★ | distributor-parametric | TI C6000, TI C5000, TI C2000, ADI SHARC, ADI Blackfin, ADI TigerSHARC, ARM Cortex-M (DSP ext), Xtensa HiFi |
| max_clock_mhz | Max Clock (MHz) | numeric | MHz | ★ | distributor-parametric | Throughput proxy and primary perf metric for DSP; distributor 'Speed' parametric |
| data_width_bits | Data Width (bits) | enum |  |  | distributor-parametric | 16-bit, 24-bit, 32-bit, 64-bit |
| arithmetic | Arithmetic | enum |  |  | datasheet | Fixed-point, Floating-point, Fixed/Floating |
| ram_kb | On-chip RAM (KB) | numeric | KB |  | distributor-parametric | On-chip memory size; distributor 'RAM Size'. Common secondary sizing narrow. |
| supply_voltage | Supply Voltage (V) | numeric | V |  | distributor-parametric | Core/IO voltage; distributor parametric. Standard power-domain filter. |

#### `fpga` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| family | Family | enum |  | ★ | title-parse | Artix, Kintex, Virtex, Spartan, Zynq, Versal, Cyclone, Arria, Stratix, Agilex, MAX, ECP5, iCE40, PolarFire, IGLOO |
| logic_elements | Logic Elements (K) | numeric | KLE | ★ | distributor-parametric | Logic capacity (LUTs/logic cells, in thousands) is THE FPGA sizing metric; distr |
| io_count | User I/O | numeric | pins |  | distributor-parametric | Number of user I/O — a hard pin-budget constraint distributors expose as 'Number |
| block_ram_kb | Block RAM (Kb) | numeric | Kb |  | distributor-parametric | Embedded block memory (in kilobits) — common sizing narrow; distributor 'Total R |
| has_hard_processor | Hard Processor (SoC) | boolean |  |  | datasheet | Whether the device is an SoC FPGA with a hard ARM core (Zynq, SmartFusion) — a m |
| speed_grade | Speed Grade | enum |  |  | title-parse | -1, -2, -3, -6, -7, -8 |
| transceiver_count | High-Speed Transceivers | numeric | count |  | distributor-parametric | Number of multi-gigabit transceivers (GTs/GTH) drives connectivity-heavy designs |

#### `asic` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| application | Application | enum |  | ★ | ai-inferred | Crypto/Mining, AI/ML Accelerator, Networking/Switch, Video/Codec, Automotive, Custom/Other |
| process_node_nm | Process Node (nm) | enum |  |  | datasheet | 3, 5, 7, 10, 14, 16, 28, 40, 65, 90 |
| interface | Host Interface | enum |  |  | datasheet | PCIe, Ethernet, USB, SPI, Memory-mapped, Proprietary |
| supply_voltage | Core Voltage (V) | numeric | V |  | datasheet | Core supply voltage — a basic power-domain filter that generalizes across ASIC t |

#### `gpu` — already seeded — review/extend
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| gpu_family | Family | enum |  | ★ | title-parse | GeForce, RTX, Quadro, Radeon, Radeon Pro, Tesla, A-series, H-series, L-series, Instinct, Arc |
| memory_gb | Memory (GB) | numeric | GB | ★ | title-parse | VRAM size is a primary sizing/price driver and almost always in the title. |
| memory_type | Memory Type | enum |  |  | datasheet | GDDR5, GDDR6, GDDR6X, HBM2, HBM2e, HBM3, HBM3e |
| interface | Interface | enum |  |  | datasheet | PCIe 3.0, PCIe 4.0, PCIe 5.0, SXM4, SXM5, OAM |
| tdp_watts | TDP (W) | numeric | W |  | datasheet | Power/cooling budget; from datasheet. EXTEND: current datacenter parts reach ~70 |
| cooling | Cooling | enum |  |  | ai-inferred | Active, Passive, Liquid, Blower |

### MEMORY & STORAGE + IT / SERVER HARDWARE
_Read ground truth: app/data/commodity_seeds.json (16 commodities seeded), app/models/faceted_search.py (CommoditySpecSchema/MaterialSpecFacet shape), app/services/commodity_registry.py (COMMODITY_TREE). Designed/extended 10 sub-categories across the two requested parent groups.

SEEDED (reviewed + extended): dram, flash, ssd, hdd, motherboards, network_cards. NEW (fresh design): raid_controllers, server_chassis, fans_cooling, networking. (power_supplies/cpu/gpu are also seeded but live in other parent groups, so not re-touched here.)

NOTE ON canonical_unit: the StructuredOutput schema only accepts 'unit' per spec, so canonical units are documented inline in each numeric spec's 'why'. When writing to commodity_seeds.json, set canonical_unit equal to the stated unit for every numeric spec (the JSON file DOES support canonical_unit and the seeder reads it).

Recommended file change: edit /root/availai/.claude/worktrees/enrichment-worker/app/data/commodity_seeds.json to (a) overwrite the 6 seeded entries above with the extended spec sets and (b) add 4 new top-level keys (raid_controllers, server_chassis, fans_cooling, networking). The seed loader (commodity_registry.py: seed_commodity_schemas) is idempotent on (commodity, spec_key); it ONLY inserts missing pairs, so it will NOT update changed enum_values/display_name/is_primary/units on already-seeded rows. To apply the reviews to existing DB rows you need either a one-time data migration (Alembic, op.get_bind() + text() UPDATEs per the DB rules) or to extend seed_commodity_schemas to upsert. Net-new spec_keys (e.g. dram.rank, ssd.endurance_dwpd) WILL seed automatically.

Key design decisions worth flagging for review:
1. flash.capacity unit changed GB to Gb (gigabits); raw flash density is spec'd in bits, so keeping GB would 8x-corrupt parametric ingestion. This is a breaking rename of capacity_gb to capacity; needs a migration if any flash facets already exist.
2. network_cards.ports (numeric) recommended changed to port_count (enum) since NICs ship in discrete 1/2/4-port SKUs (better facet UX). Also a rename/type change.
3. Heavy use of populatable_from='ai-inferred' for marketing/series-derived narrows (ssd.use_class, hdd.drive_class, raid mode/controller_chip, nic.card_function, networking.vendor_coding); these are genuinely useful buyer filters but are NOT in distributor parametric feeds, so the enrichment worker must populate them with low-confidence flagging and never present them as verified.
4. Interface enums for ssd/hdd/raid/nic were upgraded from bare SATA/SAS/NVMe to speed-qualified values (SAS-12G, NVMe PCIe 4.0) because buyers in an OEM/FRU-heavy catalog filter on the speed grade, not just the protocol family.
5. dram: promoted ecc to primary; added rank + registered (the two biggest server-memory compatibility narrows the FRU-heavy list demands). Dropped non-standard 'DDR5X' from seeded enum.

Per-commodity I kept to 5-9 specs with 1-3 is_primary headline chips, per the design rules. All units/enum styles follow the existing seeds (package codes, V/GB/W/MHz canonical units)._


#### `dram` — already seeded — review/extend
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| ddr_type | DDR Type | enum |  | ★ | distributor-parametric | DDR2, DDR3, DDR3L, DDR4, DDR5, LPDDR3, LPDDR4, LPDDR4X, LPDDR5, FBDIMM |
| capacity_gb | Capacity (GB) | numeric | GB | ★ | distributor-parametric | Module size is a top buyer filter. Range 1-256 widened max to 256GB (DDR5 LRDIMM |
| speed_mhz | Speed (MHz) | numeric | MHz |  | distributor-parametric | Buyers match speed grade (e.g. 3200, 4800). Numeric range 800-8400. Title-parse  |
| ecc | ECC | boolean |  | ★ | distributor-parametric | Promoted to primary: ECC vs non-ECC is a hard server-vs-desktop divider and a co |
| form_factor | Form Factor | enum |  |  | distributor-parametric | DIMM, SO-DIMM, UDIMM, RDIMM, LRDIMM, FBDIMM, Mini-DIMM |
| rank | Rank | enum |  |  | distributor-parametric | 1Rx8, 1Rx4, 2Rx8, 2Rx4, 4Rx4, 8Rx4 |
| registered | Registered/Buffered | enum |  |  | distributor-parametric | Unbuffered, Registered, Load-Reduced, Fully-Buffered |
| voltage | Voltage (V) | numeric | V |  | datasheet | NEW. 1.2V vs 1.35V (DDR3L) vs 1.5V distinguishes low-voltage SKUs; useful narrow |

#### `flash` — already seeded — review/extend
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| capacity | Capacity | numeric | Gb | ★ | distributor-parametric | IMPROVEMENT: raw flash density is normally specified in gigabits (Gb), not GB. R |
| interface | Interface | enum |  | ★ | distributor-parametric | SPI, Quad SPI, Octal SPI, Parallel NAND, Parallel NOR, eMMC, UFS, ONFI, Toggle |
| flash_type | Flash Type | enum |  | ★ | distributor-parametric | NAND, NOR, Serial NOR, Serial NAND, Managed NAND |
| cell_type | Cell Type | enum |  |  | datasheet | SLC, MLC, TLC, QLC |
| package | Package | enum |  |  | distributor-parametric | SOIC-8, SOIC-16, WSON-8, TSOP-48, BGA-63, BGA-132, BGA-153, VFBGA, USON, DIP |
| voltage | Voltage (V) | numeric | V |  | datasheet | Keep seeded. 1.8V vs 3.3V supply rail is a hard design constraint buyers filter  |
| temp_grade | Temperature Grade | enum |  |  | datasheet | Commercial, Industrial, Automotive (AEC-Q100), Extended |

#### `ssd` — already seeded — review/extend
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| capacity_gb | Capacity (GB) | numeric | GB | ★ | distributor-parametric | Keep. Headline SSD filter. Numeric range slider; recommend min 16, max 30720 (30 |
| form_factor | Form Factor | enum |  | ★ | distributor-parametric | 2.5", 3.5", M.2 2280, M.2 2242, M.2 22110, U.2, U.3, E1.S, E1.L, E3.S, mSATA, PCIe AIC |
| interface | Interface | enum |  | ★ | distributor-parametric | SATA III, SAS-12G, SAS-24G, NVMe PCIe 3.0, NVMe PCIe 4.0, NVMe PCIe 5.0 |
| endurance_dwpd | Endurance (DWPD) | numeric | DWPD |  | datasheet | NEW. Drive Writes Per Day separates read-intensive (1 DWPD) from write/mixed-use |
| nand_type | NAND Type | enum |  |  | datasheet | SLC, MLC, eMLC, TLC, 3D TLC, QLC, 3D QLC |
| use_class | Use Class | enum |  |  | ai-inferred | Read-Intensive, Mixed-Use, Write-Intensive, Boot, Consumer |
| read_speed_mbps | Read Speed (MB/s) | numeric | MB/s |  | datasheet | Keep seeded. Sequential read is a performance narrow. Range 100-14000 (PCIe 5.0) |

#### `hdd` — already seeded — review/extend
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| capacity_gb | Capacity (GB) | numeric | GB | ★ | distributor-parametric | Keep. Headline HDD filter. Range 80-24000 (24TB nearline) to cover enterprise dr |
| rpm | RPM | enum |  | ★ | distributor-parametric | 5400, 5900, 7200, 10000, 15000 |
| interface | Interface | enum |  | ★ | distributor-parametric | SATA III, SAS-6G, SAS-12G, SAS-24G, FC, SCSI, IDE/PATA |
| form_factor | Form Factor | enum |  |  | distributor-parametric | 2.5", 3.5", 1.8" |
| drive_class | Drive Class | enum |  |  | ai-inferred | Enterprise/Nearline, NAS, Surveillance, Desktop, Mobile, Datacenter |
| sector_size | Sector/Format | enum |  |  | datasheet | 512n, 512e, 4Kn, 520, 528 |
| encryption | Encryption | enum |  |  | datasheet | None, SED, FIPS 140-2, ISE/Instant Secure Erase |

#### `motherboards` — already seeded — review/extend
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| socket | CPU Socket | enum |  | ★ | distributor-parametric | LGA1700, LGA1851, LGA1200, LGA1151, LGA2066, LGA3647, LGA4189, LGA4677, LGA7529, AM4, AM5, SP3, SP5, sTRX4, sWRX8 |
| form_factor | Form Factor | enum |  | ★ | distributor-parametric | ATX, mATX, E-ATX, Mini-ITX, SSI-EEB, SSI-CEB, Proprietary/OEM |
| chipset | Chipset | enum |  |  | distributor-parametric | Intel C621, Intel C621A, Intel C741, Intel Z790, Intel B760, Intel W680, AMD X670, AMD B650, AMD WRX90, AMD SP3 |
| memory_type | Memory Type | enum |  |  | distributor-parametric | DDR3, DDR4, DDR5, DDR4 RDIMM, DDR5 RDIMM |
| ram_slots | RAM Slots | numeric | slots |  | distributor-parametric | Keep. Slot count (server boards hit 16-32) is a real narrow. Widen range to 1-32 |
| max_memory_gb | Max Memory (GB) | numeric | GB |  | datasheet | Keep. Range 8-12288 to cover dual-socket EPYC/Xeon capacity. Buyers filter on me |
| socket_count | Socket Count | enum |  |  | distributor-parametric | 1, 2, 4, 8 |
| pcie_gen | PCIe Generation | enum |  |  | datasheet | Gen3, Gen4, Gen5 |

#### `network_cards` — already seeded — review/extend
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| speed | Port Speed | enum |  | ★ | distributor-parametric | 1GbE, 10GbE, 25GbE, 40GbE, 50GbE, 100GbE, 200GbE, 400GbE |
| port_count | Port Count | enum |  | ★ | distributor-parametric | 1, 2, 4, 8 |
| connector_type | Connector Type | enum |  | ★ | distributor-parametric | RJ45, SFP, SFP+, SFP28, QSFP+, QSFP28, QSFP-DD, OSFP, LC Fiber |
| interface | Host Interface | enum |  |  | distributor-parametric | PCIe 3.0 x8, PCIe 4.0 x8, PCIe 4.0 x16, PCIe 5.0 x16, OCP 2.0, OCP 3.0, LOM, Mezzanine |
| controller | Controller | enum |  |  | distributor-parametric | Intel, Broadcom, Mellanox/NVIDIA, Marvell/QLogic, Chelsio, Solarflare/Xilinx, Realtek |
| media_type | Media Type | enum |  |  | distributor-parametric | Copper, Fiber, Copper/Fiber, DAC |
| card_function | Card Function | enum |  |  | ai-inferred | Ethernet NIC, InfiniBand HCA, Converged (CNA), Fibre Channel HBA, SmartNIC/DPU |

#### `raid_controllers` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| interface | Drive Interface | enum |  | ★ | distributor-parametric | SATA, SAS-6G, SAS-12G, SAS-24G, NVMe, SAS/SATA, Tri-Mode |
| raid_levels | RAID Levels | enum |  | ★ | datasheet | JBOD/HBA, 0, 1, 5, 6, 10, 50, 60 |
| port_count | Internal Ports | numeric | ports |  | distributor-parametric | Port/lane count (8i, 16i, 24i) governs how many drives attach, a routine narrow. |
| cache_mb | Cache (MB) | numeric | MB |  | datasheet | Onboard cache (0/512/1024/2048/4096/8192MB) separates entry HBAs from high-end R |
| host_interface | Host Interface | enum |  |  | distributor-parametric | PCIe 3.0 x8, PCIe 4.0 x8, PCIe 4.0 x16, PCIe 5.0 x8, Mezzanine, OCP, Proprietary/Integrated |
| controller_chip | Controller Family | enum |  |  | ai-inferred | Broadcom/LSI MegaRAID, Broadcom/LSI SAS HBA, Microchip/Adaptec, Marvell, Areca, Intel VROC |
| mode | Mode | enum |  |  | ai-inferred | Hardware RAID, IT/HBA Mode, IR Mode, Tri-Mode |

#### `server_chassis` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| rack_units | Rack Units (U) | enum |  | ★ | title-parse | 1U, 2U, 3U, 4U, 5U, Tower, Blade, Multi-node |
| drive_bays | Drive Bays | numeric | bays | ★ | distributor-parametric | Hot-swap bay count (e.g. 8/12/24/36) is a core capacity narrow buyers select on. |
| bay_form_factor | Bay Form Factor | enum |  | ★ | title-parse | 3.5" LFF, 2.5" SFF, EDSFF E1.S, EDSFF E3.S, Mixed, M.2 only |
| socket_support | Socket Support | enum |  |  | datasheet | Intel LGA3647, Intel LGA4189, Intel LGA4677, AMD SP3, AMD SP5, Single, Dual, Barebone |
| psu_config | PSU Configuration | enum |  |  | datasheet | Single, 1+1 Redundant, 2+2 Redundant, Redundant Hot-Swap, None |
| psu_wattage | PSU Wattage (W) | numeric | W |  | datasheet | Installed PSU wattage (550-3000W) is a power-budget narrow when chassis ships wi |
| backplane_type | Backplane | enum |  |  | ai-inferred | SATA/SAS, SAS Expander, NVMe, Tri-Mode, Direct-Attach, Passive |

#### `fans_cooling` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| cooling_type | Cooling Type | enum |  | ★ | title-parse | Case/Chassis Fan, CPU Air Cooler, CPU Heatsink (passive), AIO Liquid, Cold Plate/Liquid Block, Blower, Fan Module/FRU |
| fan_size_mm | Fan Size (mm) | enum |  | ★ | distributor-parametric | 40, 60, 80, 92, 120, 140, 200 |
| connector | Connector / Pins | enum |  | ★ | distributor-parametric | 2-pin, 3-pin, 4-pin PWM, 6-pin (server), Molex, Proprietary/FRU |
| voltage | Voltage (V) | enum |  |  | distributor-parametric | 5V, 12V, 24V, 48V |
| airflow_cfm | Airflow (CFM) | numeric | CFM |  | datasheet | Airflow rating differentiates quiet vs high-static-pressure server fans, a perfo |
| max_rpm | Max Speed (RPM) | numeric | RPM |  | datasheet | Max RPM (server fans hit 15-25k) is a performance/noise narrow buyers use. Range |
| bearing_type | Bearing Type | enum |  |  | datasheet | Sleeve, Ball, Dual Ball, Fluid Dynamic (FDB), Maglev |

#### `networking` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| device_type | Device Type | enum |  | ★ | title-parse | Switch, Router, Transceiver/Optic, DAC/AOC Cable, Firewall, Access Point, Module/Line Card, Patch Panel |
| port_speed | Port Speed | enum |  | ★ | distributor-parametric | 100M, 1GbE, 2.5GbE, 10GbE, 25GbE, 40GbE, 100GbE, 200GbE, 400GbE |
| form_factor | Form Factor / Connector | enum |  | ★ | distributor-parametric | RJ45, SFP, SFP+, SFP28, QSFP+, QSFP28, QSFP-DD, OSFP, 1U Rack, 2U Rack, Desktop |
| port_count | Port Count | numeric | ports |  | distributor-parametric | Number of ports on a switch (8/24/48/64) is a core sizing narrow. Numeric range  |
| reach | Optic Reach | enum |  |  | distributor-parametric | SR (short), LR (long), ER, ZR, DAC (passive), AOC, BiDi, CWDM/DWDM |
| vendor_coding | Vendor Coding | enum |  |  | ai-inferred | Cisco, Juniper, Arista, Mellanox/NVIDIA, HPE, Dell, Generic/Coded, OEM Original |
| managed | Management | enum |  |  | datasheet | Managed, Smart/Web-Managed, Unmanaged |

### Connectors & Electromechanical + Power & Energy
_Ground truth read: app/data/commodity_seeds.json (existing 16-commodity specs), app/models/faceted_search.py (CommoditySpecSchema/MaterialSpecFacet shape), app/services/commodity_registry.py (COMMODITY_TREE taxonomy + idempotent seed loader).

Conventions followed for cross-commodity consistency: mounting enum standardized on the existing ["SMD","through-hole","press-fit"] base and only EXTENDED (never contradicted) where a sub-category genuinely needs panel-mount/DIN-rail/etc; package enums use distributor-style values like other seeds; 5-8 specs each with 1-3 is_primary headline chips. Did NOT include global core attrs (manufacturer, lifecycle_status, package_type, rohs_status, datasheet_url, description) since those are owned by the global-filters agent. Note: pin_count/pitch_mm are retained as PER-COMMODITY numeric facets for connectors/sockets because they are the load-bearing range filters there (stored in material_spec_facets for range queries, distinct from the global scalar pin_count column).

populatable_from rationale: electromechanical (connectors/cables/relays/switches/sockets) and power (PSU/regulators/batteries) parts have EXCELLENT DigiKey/Mouser/Nexar parametric coverage, so nearly everything is 'distributor-parametric' (highest-confidence verified tier). Only batteries.operating_temp_max is flagged 'datasheet' since temperature is less consistently in parametric feeds. NO 'ai-inferred' specs proposed for these commodities; all values are reliably available from structured distributor data without hallucination risk, which is the correct outcome for the enrichment worker.

SEEDED commodities reviewed & extended:
- connectors: split the over-flattened connector_type (brands JST/Molex + interfaces USB/HDMI were mixed) into a cleaner physical-family list; promoted pitch_mm to primary; extended mounting+gender enums; ADDED orientation, current_rating, rows; REPLACED the empty-valued open-ended 'series' enum (un-facetable, high cardinality) with 'rows'.
- power_supplies: biggest gap was the PC/server-only assumption. ADDED psu_class top-level facet (AC-DC/DIN-rail/adapter/DC-DC) and output_current; converted input_voltage from a rarely-used numeric slider to an input_voltage_type enum; extended form_factor and psu_connector_type for industrial + modern (12VHPWR) parts; promoted output_voltage to primary.

UNSEEDED (fresh sets): cables, relays, switches, sockets, voltage_regulators, batteries. Each leads with a *_type family enum as the headline first cut, mirroring how distributors structure these categories. contact_configuration / contact-form enums for relays & switches use the standard SPST/SPDT/DPDT pole-throw notation buyers recognize.

Schema note: the StructuredOutput spec object does not accept canonical_unit, so unit is reported alone here; when written to app/data/commodity_seeds.json each numeric spec should set canonical_unit == unit (per existing seed convention). For batteries.capacity_mah and cables.length the canonical_unit normalizes Ah->mAh and ft/in/m->mm respectively.

Implementation note (not in scope to change, but flagged): these belong in app/data/commodity_seeds.json keyed by the lowercased category strings already present in COMMODITY_TREE; seed_commodity_schemas() in app/services/commodity_registry.py is idempotent and inserts only missing (commodity, spec_key) rows, so adding new commodities and extending existing ones is safe. No model or migration change needed; CommoditySpecSchema already supports every field used here._


#### `connectors` — already seeded — review/extend
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| connector_type | Connector Type | enum |  | ★ | distributor-parametric | Header/Wire-to-Board, Wire-to-Wire, Board-to-Board, USB, RJ45/Modular, HDMI, DisplayPort, PCIe Card Edge, D-Sub, Circular/M-series, JST, Molex, Terminal Block, FPC/FFC, Card Edge, RF/Coaxial, Barrel/Power, Backplane |
| pin_count | Positions / Contacts | numeric | pins | ★ | distributor-parametric | Number of positions/contacts is a hard requirement for board fitment. Distributo |
| pitch_mm | Pitch | numeric | mm | ★ | distributor-parametric | Contact spacing determines mechanical/PCB compatibility (2.54mm vs 1.27mm vs 0.5 |
| mounting | Mounting | enum |  |  | distributor-parametric | SMD, through-hole, press-fit, panel-mount, cable/free-hanging |
| gender | Gender | enum |  |  | distributor-parametric | male/plug, female/receptacle, genderless, hermaphroditic |
| orientation | Orientation | enum |  |  | distributor-parametric | vertical/straight, right-angle, horizontal |
| current_rating | Current Rating (per contact) | numeric | A |  | distributor-parametric | Per-contact current rating matters for power connectors and is a real buyer cons |
| rows | Number of Rows | enum |  |  | distributor-parametric | 1, 2, 3, 4 |

#### `cables` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| cable_type | Cable Type | enum |  | ★ | distributor-parametric | Jumper/Pre-crimped, Ribbon (IDC), Coaxial/RF, USB, Ethernet/RJ45, Power Cord, Fiber Optic, FFC/FPC, Multi-conductor, D-Sub, SATA/SAS, Custom Assembly |
| length | Length | numeric | mm | ★ | distributor-parametric | Cable length is almost always a hard requirement and the primary range filter fo |
| conductor_count | Number of Conductors | numeric | conductors | ★ | distributor-parametric | Conductor/position count defines the cable's connectivity. Distributors expose ' |
| wire_gauge_awg | Wire Gauge (AWG) | numeric | AWG |  | distributor-parametric | AWG gates current capacity and physical fit. Standard parametric attribute; nume |
| shielding | Shielding | enum |  |  | distributor-parametric | Unshielded, Foil, Braid, Foil + Braid, Spiral |
| end_a_termination | End A Connector | enum |  |  | distributor-parametric | Free End/Unterminated, Header/Socket, USB-A, USB-C, RJ45, D-Sub, Ring/Spade Terminal, JST, Molex, Other |
| end_b_termination | End B Connector | enum |  |  | distributor-parametric | Free End/Unterminated, Header/Socket, USB-A, USB-C, RJ45, D-Sub, Ring/Spade Terminal, JST, Molex, Other |

#### `relays` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| relay_type | Relay Type | enum |  | ★ | distributor-parametric | Electromechanical (EMR), Solid State (SSR), Reed, Automotive, Latching, Signal, Power, Time Delay |
| coil_voltage | Coil / Control Voltage | numeric | V | ★ | distributor-parametric | Coil voltage must match the control circuit, a hard buyer requirement and the mo |
| contact_configuration | Contact Form | enum |  | ★ | distributor-parametric | SPST-NO (1A), SPST-NC (1B), SPDT (1C), DPST, DPDT (2C), 3PDT, 4PDT |
| switching_current | Switching/Contact Current | numeric | A |  | distributor-parametric | Max contact current the relay can switch, a load-side requirement. Standard para |
| switching_voltage | Switching/Contact Voltage | numeric | V |  | distributor-parametric | Max load voltage on the contacts (e.g. 250VAC). Distinct from coil voltage; a re |
| coil_type | Coil Type | enum |  |  | distributor-parametric | DC, AC, Non-Latching, Single Latching, Dual Latching |
| mounting | Mounting | enum |  |  | distributor-parametric | through-hole, SMD, panel-mount, DIN-rail, socketable/plug-in, chassis |

#### `switches` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| switch_type | Switch Type | enum |  | ★ | distributor-parametric | Tactile, Pushbutton, Toggle, Slide, Rocker, Rotary, DIP, Detect, Key/Keylock, Snap-Action/Limit, Thumbwheel |
| contact_configuration | Circuit / Contact Form | enum |  | ★ | distributor-parametric | SPST-NO, SPST-NC, SPDT, DPST, DPDT, 3PDT, 4PDT |
| actuator_type | Actuator | enum |  |  | distributor-parametric | Round Button, Flat Button, Lever, Plunger, Paddle, Rocker, Knob, Slide, Standard |
| current_rating | Current Rating | numeric | A | ★ | distributor-parametric | Contact current rating gates load capacity, a hard buyer requirement and primary |
| voltage_rating | Voltage Rating | numeric | V |  | distributor-parametric | Max switching voltage. Standard parametric attribute paired with current rating  |
| mounting | Mounting | enum |  |  | distributor-parametric | through-hole, SMD, panel-mount, PCB, snap-in, chassis |
| ip_rating | IP / Sealing Rating | enum |  |  | distributor-parametric | None, IP40, IP54, IP65, IP67, IP68 |

#### `sockets` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| socket_type | Socket Type | enum |  | ★ | distributor-parametric | IC Socket (DIP), IC Socket (PLCC), IC Socket (PGA), BGA Socket, CPU Socket, Relay Socket, Pin/Receptacle Strip, ZIF, Test/Burn-in, Fuse Holder |
| pin_count | Positions / Pins | numeric | pins | ★ | distributor-parametric | Number of contacts the socket accepts is a hard fit requirement and primary rang |
| pitch_mm | Pitch | numeric | mm | ★ | distributor-parametric | Contact pitch must match the device it seats (2.54mm DIP vs fine-pitch). Primary |
| mounting | Mounting | enum |  |  | distributor-parametric | through-hole, SMD, press-fit, panel-mount, DIN-rail |
| rows | Number of Rows | enum |  |  | distributor-parametric | 1, 2, 3, 4 |
| contact_plating | Contact Plating | enum |  |  | distributor-parametric | Gold, Tin, Tin-Lead, Selective Gold, Nickel |

#### `power_supplies` — already seeded — review/extend
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| psu_class | Supply Class | enum |  | ★ | distributor-parametric | AC-DC (Enclosed), AC-DC (Open Frame), AC-DC (DIN Rail), AC-DC (External/Adapter), ATX/PC, Server/Redundant, DC-DC Converter, Module/On-Board |
| wattage | Output Power | numeric | W | ★ | distributor-parametric | Total output power is the dominant buyer requirement. Kept from existing seed; r |
| output_voltage | Output Voltage | numeric | V | ★ | distributor-parametric | Output rail voltage is a hard requirement (5V/12V/24V/48V). Kept from seed; prom |
| output_current | Output Current | numeric | A |  | distributor-parametric | Output current rating, paired with voltage, defines the supply. New addition; di |
| input_voltage_type | Input Type | enum |  |  | distributor-parametric | AC (Universal 85-264V), AC 120V, AC 230V, AC 3-Phase, DC Input |
| efficiency | 80 PLUS Efficiency | enum |  |  | distributor-parametric | 80+ White, 80+ Bronze, 80+ Silver, 80+ Gold, 80+ Platinum, 80+ Titanium |
| form_factor | Form Factor | enum |  |  | distributor-parametric | ATX, SFX, TFX, Flex ATX, 1U, 2U, Enclosed, Open Frame, DIN Rail, Brick, Wall Adapter |
| psu_connector_type | Output Connector | enum |  |  | distributor-parametric | ATX 24-pin, EPS 8-pin, PCIe 6-pin, PCIe 8-pin, 12VHPWR, Barrel Jack, Screw Terminal, Molex, SATA, Wire Leads |

#### `voltage_regulators` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| regulator_type | Regulator Type | enum |  | ★ | distributor-parametric | LDO (Linear), Switching Buck, Switching Boost, Buck-Boost, Inverting, Switching Controller, Charge Pump, Reference |
| output_voltage | Output Voltage | numeric | V | ★ | distributor-parametric | Fixed/nominal output voltage is the core spec a buyer targets (1.8V/3.3V/5V). Pr |
| output_current_max | Output Current (max) | numeric | A | ★ | distributor-parametric | Max output/load current defines the part's capacity, a hard buyer requirement. D |
| input_voltage_max | Input Voltage (max) | numeric | V |  | distributor-parametric | Max input voltage gates which supply rail the regulator can accept. Standard par |
| output_config | Output Configuration | enum |  |  | distributor-parametric | Fixed, Adjustable, Sequencer/Multi-output |
| number_of_outputs | Number of Outputs | numeric | outputs |  | distributor-parametric | Single vs multi-rail regulators/PMICs. Distributors expose 'Number of Outputs';  |
| package | Package | enum |  |  | distributor-parametric | SOT-23, SOT-223, SOT-89, TO-220, TO-263 (D2PAK), DPAK, SOIC, QFN, DFN, module |
| mounting | Mounting | enum |  |  | distributor-parametric | SMD, through-hole, press-fit |

#### `batteries` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| chemistry | Chemistry | enum |  | ★ | distributor-parametric | Lithium (Li-MnO2), Lithium-Ion, LiPo, LiFePO4, Alkaline, NiMH, NiCd, Lead-Acid, Lithium Thionyl Chloride (Li-SOCl2), Silver Oxide, Zinc-Air |
| rechargeable | Rechargeable | boolean |  | ★ | distributor-parametric | Primary vs secondary (rechargeable) is a fundamental buyer toggle. Boolean facet |
| nominal_voltage | Nominal Voltage | numeric | V | ★ | distributor-parametric | Nominal voltage (1.5V/3V/3.7V/12V) is a hard application requirement and primary |
| capacity_mah | Capacity | numeric | mAh |  | distributor-parametric | Capacity in mAh (normalize Ah to mAh) is the key runtime metric buyers range-fil |
| form_factor | Form / Size | enum |  |  | distributor-parametric | Coin/Button (CR2032), Coin/Button (Other), AA, AAA, AAAA, C, D, 9V, 18650, 21700, Prismatic, Pouch, PCB Mount |
| termination | Termination | enum |  |  | distributor-parametric | Standard Contacts, PC Pins (through-hole), SMD, Solder Tabs, Wire Leads, Spring Contacts, Connector |
| operating_temp_max | Max Operating Temp | numeric | C |  | datasheet | Upper operating temperature matters for industrial/automotive battery selection. |

### OPTOELECTRONICS & DISPLAY; SENSORS & RF; MISC
_Designed against app/data/commodity_seeds.json conventions: numeric specs carry a display unit; enum specs carry enum_values; booleans bare; 1-2 headline specs flagged is_primary; package/mounting enum styles reused from existing seeds. (Note: the seeds file also stores canonical_unit + numeric_range, but this output schema only accepts unit/enum_values, so I omit those two persistence-only fields here; on seed-write set canonical_unit=unit and add numeric_range per the why notes.) None of these 9 sub-categories are seeded yet (verified absent from commodity_seeds.json) -> already_seeded=false for all. populatable_from realism: leds/displays/sensors/rf map cleanly to DigiKey/Mouser/Nexar parametric tables (distributor-parametric, highest confidence). optoelectronics is a catch-all (photodiodes/optocouplers/IR/laser) so it leans on a coarse subtype enum + cross-cutting electrical params. MISC (motors, enclosures, tools_accessories, other) are NOT electronic-component parametric domains and have thin distributor coverage; per instructions enclosures/tools_accessories/other kept deliberately minimal (1-3 mostly-enum specs, datasheet/title-parse/ai-inferred), motors slightly richer since motors do get some Mouser/DigiKey parametric coverage. No spec duplicates the GLOBAL core attrs (manufacturer, category, lifecycle_status, package_type, rohs_status, pin_count, datasheet_url, description) owned by the global-filters agent. Recommend the enrichment worker never auto-commit ai-inferred specs without the low-confidence flag._


#### `leds` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| color | Color | enum |  | ★ | distributor-parametric | Red, Green, Blue, Yellow, Amber, Orange, White, Warm White, Cool White, RGB, Infrared (IR), Ultraviolet (UV), Bi-color |
| led_type | LED Type | enum |  | ★ | distributor-parametric | Standard, High Brightness, Lighting / Illumination, Addressable (RGB IC), COB, Indicator, IR Emitter, UV Emitter |
| forward_voltage | Forward Voltage (Vf) | numeric | V |  | distributor-parametric | Drives series-resistor / driver selection; a standard DigiKey/Mouser numeric par |
| forward_current | Forward Current (If) | numeric | mA |  | distributor-parametric | Test/rated drive current; commonly filtered when matching driver capability. can |
| luminous_intensity | Luminous Intensity | numeric | mcd |  | distributor-parametric | Brightness binning for indicators; exposed as a parametric range by distributors |
| wavelength | Wavelength | numeric | nm |  | distributor-parametric | Critical for IR/UV/sensing LEDs; distributor parametric tables list dominant/pea |
| viewing_angle | Viewing Angle | numeric | deg |  | distributor-parametric | Beam spread (2theta-half); a real selection criterion for optics/indicators, pre |
| package | Package / Size | enum |  |  | distributor-parametric | 0402, 0603, 0805, 1206, 3528, 5050, PLCC-2, PLCC-4, PLCC-6, 5mm THT, 3mm THT, COB, through-hole |
| mounting | Mounting | enum |  |  | distributor-parametric | SMD, through-hole, panel/chassis |

#### `displays` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| display_technology | Display Technology | enum |  | ★ | distributor-parametric | LCD (Character), LCD (Graphic), LCD (TFT), OLED, E-Paper / EPD, LED (7-Segment), LED (Dot Matrix), VFD |
| diagonal_size | Diagonal Size | numeric | in | ★ | distributor-parametric | Physical screen size is a headline mechanical/spec filter; listed in inches by d |
| resolution | Resolution | enum |  |  | distributor-parametric | 16x2, 20x4, 128x64, 128x32, 240x320, 320x240, 480x272, 800x480, 1024x600, 1280x800, 1920x1080 |
| interface | Interface | enum |  |  | distributor-parametric | Parallel, SPI, I2C, RGB, MIPI DSI, LVDS, HDMI, UART |
| color_depth | Color Depth | enum |  |  | distributor-parametric | Monochrome, Grayscale, 8-bit (256), 16-bit (65K), 18-bit (262K), 24-bit (16.7M) |
| backlight | Backlight | enum |  |  | distributor-parametric | LED White, LED RGB, EL, None, Reflective |
| touch | Touchscreen | enum |  |  | distributor-parametric | None, Resistive, Capacitive |
| operating_voltage | Operating Voltage | numeric | V |  | datasheet | Supply-rail compatibility; sometimes parametric, often only on the datasheet. ca |

#### `optoelectronics` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| opto_type | Device Type | enum |  | ★ | distributor-parametric | Optocoupler / Optoisolator, Photodiode, Phototransistor, Photointerrupter, IR Receiver, Laser Diode, Light Sensor (Ambient), Fiber Optic Tx/Rx, Solar Cell |
| channels | Number of Channels | numeric | channels |  | distributor-parametric | Channel count is the standard optocoupler/isolator parametric and a real narrowi |
| isolation_voltage | Isolation Voltage | numeric | V | ★ | distributor-parametric | Headline safety/compliance spec for optoisolators (Viso, Vrms); a primary buyer  |
| ctr | Current Transfer Ratio (CTR) | numeric | % |  | distributor-parametric | Defines optocoupler gain/drive matching; listed parametrically as a min %. canon |
| wavelength | Wavelength | numeric | nm |  | distributor-parametric | Peak/operating wavelength for photodiodes, IR receivers, laser diodes. canonical |
| output_type | Output Type | enum |  |  | distributor-parametric | Transistor, Darlington, Logic Gate, Triac/SCR, MOSFET, IGBT Driver, Analog |
| mounting | Mounting | enum |  |  | distributor-parametric | SMD, through-hole, panel/chassis |

#### `sensors` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| sensor_type | Sensor Type | enum |  | ★ | distributor-parametric | Temperature, Humidity, Pressure, Accelerometer, Gyroscope, IMU, Magnetic / Hall, Proximity, Ambient Light, Current, Gas / Air Quality, Optical / Image, Force / Load, Flow, Position / Encoder |
| output_type | Output Type | enum |  | ★ | distributor-parametric | Analog Voltage, Analog Current (4-20mA), I2C, SPI, PWM, Digital (Switch), Frequency, UART, CAN |
| measurement_range | Measurement Range | enum |  |  | datasheet | See datasheet |
| accuracy | Accuracy | numeric | % |  | datasheet | Accuracy/error band is a common quality filter; usually datasheet-only, occasion |
| supply_voltage | Supply Voltage | numeric | V |  | distributor-parametric | Rail compatibility filter; a standard parametric column across sensor families.  |
| operating_temp_max | Max Operating Temp | numeric | C |  | distributor-parametric | Industrial/automotive grade screening; max operating temperature is parametric a |
| mounting | Mounting | enum |  |  | distributor-parametric | SMD, through-hole, module / board, panel/chassis |

#### `rf` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| rf_type | Device Type | enum |  | ★ | distributor-parametric | RF Transceiver, RF Receiver, RF Transmitter, RF Amplifier (PA/LNA), RF Mixer, RF Filter, Antenna, RF Switch, RF Module, Balun, Attenuator, VCO/PLL |
| protocol | Protocol / Standard | enum |  | ★ | distributor-parametric | Bluetooth / BLE, Wi-Fi, Zigbee, LoRa, Thread, Cellular (LTE/5G), GPS/GNSS, NFC/RFID, Sub-GHz (ISM), UWB, Proprietary |
| frequency | Frequency | numeric | MHz | ★ | distributor-parametric | Operating / center frequency is the core RF spec; normalized to MHz for slider u |
| gain | Gain | numeric | dB |  | distributor-parametric | Gain (amplifiers) / antenna gain (dBi) is a standard parametric used to size a l |
| output_power | Output Power | numeric | dBm |  | distributor-parametric | Tx output power (P1dB / max) determines range and regulatory class; a real filte |
| impedance | Impedance | enum |  |  | distributor-parametric | 50 ohm, 75 ohm |
| supply_voltage | Supply Voltage | numeric | V |  | distributor-parametric | Rail compatibility for active RF parts; a standard parametric column. canonical_ |
| mounting | Mounting | enum |  |  | distributor-parametric | SMD, through-hole, module / board, connector / cable |

#### `motors` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| motor_type | Motor Type | enum |  | ★ | distributor-parametric | DC Brushed, DC Brushless (BLDC), Stepper, Servo, AC Induction, Gear Motor, Vibration, Linear Actuator |
| rated_voltage | Rated Voltage | numeric | V | ★ | distributor-parametric | Operating voltage is a headline drive-compatibility filter; parametric on DigiKe |
| rated_speed | Rated Speed | numeric | RPM |  | distributor-parametric | No-load / rated speed is a common selection value listed parametrically. canonic |
| torque | Torque | numeric | mNm |  | datasheet | Rated/holding torque sizes the mechanical load; often datasheet, units vary so n |
| shaft_diameter | Shaft Diameter | numeric | mm |  | datasheet | Mechanical fit to gears/couplers; a concrete narrowing dimension. canonical_unit |
| mounting | Mounting / Frame | enum |  |  | distributor-parametric | NEMA 8, NEMA 11, NEMA 14, NEMA 17, NEMA 23, NEMA 34, Flange, Face, PCB |

#### `enclosures` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| enclosure_type | Enclosure Type | enum |  | ★ | distributor-parametric | Box / General Purpose, Handheld, Wall Mount, DIN Rail, Rack Mount, Project Box, PCB / Card Cage, Junction Box |
| material | Material | enum |  |  | distributor-parametric | ABS Plastic, Polycarbonate, Aluminum, Steel, Stainless Steel, Die-Cast |
| ip_rating | IP / NEMA Rating | enum |  |  | distributor-parametric | IP20, IP54, IP65, IP66, IP67, IP68, NEMA 1, NEMA 4, NEMA 4X, NEMA 12 |

#### `tools_accessories` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| tool_type | Type | enum |  | ★ | ai-inferred | Soldering, Hand Tool, Test Probe / Lead, Heat Shrink, Cable Tie / Management, Hardware / Fastener, Cleaning / Chemical, Adhesive / Tape, Antistatic / ESD, Other Accessory |
| material | Material | enum |  |  | ai-inferred | Plastic / Nylon, Steel, Stainless Steel, Brass, Aluminum, Rubber / Silicone, Other |

#### `other` — NEW (unseeded)
| spec_key | display | type | unit | primary | source | enum / why |
|---|---|---|---|---|---|---|
| form | Form | enum |  | ★ | ai-inferred | Component, Module / Board, Cable / Wire, Mechanical / Hardware, Consumable, Kit / Assembly, Unknown |
