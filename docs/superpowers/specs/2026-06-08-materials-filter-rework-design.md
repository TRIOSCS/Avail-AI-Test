# Materials Filter Rework — Design Spec

**Date:** 2026-06-08
**Branch:** `worktree-materials-filter-rework`
**Status:** Awaiting user review

## 1. Goals

1. **Simplify the Data-confidence filter** from 7 raw tiers to 3 user-facing groups, default all-on, moved to the bottom of the sidebar.
2. **Split two conflated commodity families:** Memory ≠ Storage, Connectors ≠ Electromechanical.
3. **Make per-commodity sub-filters deep** (e.g. HDD: interface incl. SCSI, form factor, RPM, capacity, **usage class** = PC vs enterprise) and curated — "deep without noisy."
4. **Show a count next to every fixed-vocabulary filter value, including `(0)`** (e.g. `SATA (500)`, `SCSI (0)`), designed for a catalog of thousands. Filters are **not** gated on whether data currently exists.

## 2. Non-goals (explicitly out of scope)

- The **MPN→datasheet / OEM cross-reference enrichment** that would *populate* specs for OEM spare-part numbers. Filters are built to display real counts as data lands; backfilling spec data is a separate track.
- Classifying the ~1,013 `NULL`-category cards. Category **normalization** (variant strings → canonical keys) IS in scope; inventing categories for null rows is not.
- Intra-commodity *subtype-driven facet swapping* (e.g. showing different facets for a relay vs a motor *within* one commodity). Commodity-level gating already separates HDD from SSD, which covers the stated need. Noted as a future enhancement.

## 3. Current state (grounded references)

- **Taxonomy:** `COMMODITY_TREE: dict[str, list[str]]` — `app/services/commodity_registry.py:20-39`. Parent groups are display-only; child keys are `material_cards.category` values (lowercased). Display labels in `_DISPLAY_NAMES` (`:42-91`). Reverse lookup `_PARENT_LOOKUP` (`:94-97`) is rebuilt from the dict.
- **Sub-filter definitions:** rows in `commodity_spec_schemas` (model `CommoditySpecSchema`, `app/models/faceted_search.py`), seeded **idempotently** from `app/data/commodity_seeds.json` by `seed_commodity_schemas()` (`commodity_registry.py:156-201`) at boot. **All 48 tree commodities already have seed blocks.** The same JSON also feeds the AI spec-extraction prompt via `get_batch_spec_schema()` (`:131-153`).
- **⚠️ Seed gotcha:** `seed_commodity_schemas()` only **inserts missing** `(commodity, spec_key)` pairs — it **never updates** an existing row. Changing an existing seed (new `enum_values`, renamed `display_name`, etc.) requires deleting the DB row first.
- **Spec values storage:** source of truth = `material_cards.specs_structured` (JSONB); queryable projection = `material_spec_facets` (one row per card×spec_key, `value_text`/`value_numeric`). All faceting queries hit `material_spec_facets`.
- **Option sourcing (the key change point):** `get_subfilter_options()` (`app/services/faceted_search_service.py:303-373`). For `enum`, it currently returns **only observed** values: `option["values"] = text_map.get(spec_key, [])`. For `boolean`, it returns `["true","false"]` **only if** facet rows back it. → This is exactly why an unobserved value like `SCSI` never renders today.
- **Counts:** `get_facet_counts()` (`:84-124`) returns `{spec_key: {value: count}}`, **already context-aware** (narrowed by active filters). Passed to the template alongside options by route `materials_filters_sub_partial` (`htmx_views.py:7175-7204`).
- **Macros:** `app/templates/htmx/partials/materials/filters/_macros.html` — `checkbox_group` shows a count only `{% if counts and val in counts %}` (so 0s are hidden) and folds value lists >6 behind "Show all (N)".
- **Sidebar:** `app/templates/htmx/partials/materials/workspace.html`. Data-confidence block is hardcoded at the **top** (`:35-62`) via a Jinja `trust_tiers` set; sub-filters render last (`:104-115`).
- **Alpine state:** `materialsFilter` in `app/static/htmx_app.js:550+`. `statuses` default `['verified','web_sourced']` (`:559`), `DEFAULT_STATUSES` (`:577`), `TRUST_TIERS` (`:568-576`), chips driven by `nonDefaultStatuses` (`:586-588`). Backend contract = `statuses` CSV of enum values; sub-filters = `sf_<key>` params / `sub_filters` JSON.

## 4. Design

### 4.1 Family taxonomy split

Replace `COMMODITY_TREE` with the corrected, reordered groups (child keys unchanged → **zero data migration**; `material_cards.category` untouched):

```python
COMMODITY_TREE: dict[str, list[str]] = {
    "Passives": ["capacitors", "resistors", "inductors", "transformers", "fuses", "oscillators", "filters"],
    "Semiconductors — Discrete": ["diodes", "transistors", "mosfets", "thyristors"],
    "Semiconductors — ICs": ["analog_ic", "logic_ic", "power_ic"],
    "Memory": ["dram", "flash"],
    "Processors & Programmable": ["microcontrollers", "cpu", "microprocessors", "dsp", "fpga", "asic", "gpu"],
    "Storage & Drives": ["ssd", "hdd"],
    "Power & Energy": ["power_supplies", "voltage_regulators", "batteries"],
    "Connectors, Interconnects & Cables": ["connectors", "cables", "sockets"],
    "Electromechanical": ["relays", "switches", "motors"],
    "Optoelectronics & Display": ["leds", "displays", "optoelectronics"],
    "Sensors & RF": ["sensors", "rf"],
    "IT / Server Hardware": ["motherboards", "network_cards", "raid_controllers", "server_chassis", "fans_cooling", "networking"],
    "Misc": ["enclosures", "tools_accessories", "other"],
}
```

Changes vs current: `Memory & Storage` → **Memory** (dram, flash) + **Storage & Drives** (ssd, hdd); `Connectors & Electromechanical` → **Connectors, Interconnects & Cables** (connectors, cables, sockets) + **Electromechanical** (relays, switches, **motors** moved out of `Misc`). `_DISPLAY_NAMES` needs no changes (motors already mapped). The 1-hour tree-partial cache must be busted / app restarted so the sidebar reflects the new groups.

**Invariant test:** every child key in `COMMODITY_TREE` must equal a key in `_DISPLAY_NAMES` **and** a key in `commodity_seeds.json` (and have no duplicates). Add `tests/test_commodity_tree_invariant.py`.

### 4.2 Category normalization (prerequisite for filters to be honest)

Free-text `material_cards.category` variants (e.g. `solid state drives - ssd`, `connectors, interconnects`, `memory - modules, cards`) never match a canonical key, so those cards are invisible to every family/filter.

- **Canonical vocabulary** = the 48 `COMMODITY_TREE` child keys.
- Add `app/services/category_normalizer.py` with `CATEGORY_ALIASES: dict[str, str]` (variant → canonical key) and `normalize_category(raw: str) -> str | None` (lowercase/trim, exact alias hit, else canonical-key passthrough, else `None`).
- **Build the alias map** from the live DB during implementation: enumerate all distinct `lower(trim(category))` values, map each obvious variant to its canonical key. Unmappable / genuinely-unknown strings → leave `category` unchanged (do not force-map).
- **Backfill:** idempotent management command `scripts/normalize_categories.py` with `--dry-run` (prints from→to counts) and `--apply`. Run on staging (single-user, acceptable-risk per project norms).
- **Forward:** call `normalize_category()` at the card write/enrichment path so new imports land canonical. (Locate the single category-write site during implementation; wrap it.)
- NULL categories are left as-is (out of scope, §2).

### 4.3 Per-commodity deep filters

**Curation principles (apply to all enum facets):**
1. Every `enum` facet that is a **fixed vocabulary** must declare a **complete canonical `enum_values`** list, ordered most-common-first. This list — not observed data — drives what renders (see §4.4).
2. An `enum` facet with **no `enum_values`** is treated as **open-ended** → typeahead + top-N (see §4.5). (Current example: connectors `series`.)
3. Order facets by `sort_order`; `is_primary: true` marks the headline facets shown expanded (rest fold under "More filters", §4.6).
4. Keep continuous-but-clustered quantities as **numeric ranges** (capacity, speed, wattage), not value dumps.

**Explicit marquee changes (directly satisfy the HDD example):**

- **`hdd`** — `interface` enum_values → `["SATA", "SAS", "SCSI", "NVMe", "FC", "IDE/PATA"]` (adds **SCSI** etc. so they show with `(0)`); `form_factor` → `["3.5\"", "2.5\"", "1.8\""]`; keep `rpm`, `capacity_gb`. **Add** `usage_class` enum (`is_primary: true`): `["Desktop / Client", "NAS", "Enterprise / Datacenter", "Surveillance", "Network Video Recorder"]`. **Add** `recording_tech` enum `["CMR", "SMR"]` (advanced). `sort_order`: capacity 1, interface 2, form_factor 3, rpm 4, usage_class 5, recording_tech 6.
- **`ssd`** — keep capacity/form_factor/interface/nand_type. **Add** `usage_class` enum (`is_primary: true`): `["Client / Consumer", "Enterprise — Read Intensive", "Enterprise — Mixed Use", "Enterprise — Write Intensive", "Datacenter / Boot"]`. Expand `form_factor` → `["2.5\"", "M.2 2280", "M.2 2230", "M.2 22110", "U.2", "U.3", "mSATA", "AIC (PCIe card)", "EDSFF E1.S", "EDSFF E3.S"]`; `interface` → `["SATA", "SAS", "NVMe PCIe 3.0", "NVMe PCIe 4.0", "NVMe PCIe 5.0", "U.2", "SCSI"]`.
- **`dram`** — already strong; expand `ddr_type` to include `["DDR", "DDR2", "DDR3", "DDR3L", "DDR4", "DDR5", "LPDDR4", "LPDDR5"]`; keep ecc/form_factor/capacity_gb/speed_mhz.
- **`connectors`** — **repurpose the existing `connector_type` spec_key** (do NOT rename it — renaming orphans existing facet rows and `sf_connector_type` URL params): set `is_primary: true`, `display_name: "Connector Family / Type"`, and expand `enum_values` → `["Rectangular / Headers", "Circular", "D-Sub", "Terminal Block", "Card Edge", "RJ45 / Modular", "USB", "HDMI / DisplayPort", "Coaxial / RF", "Fiber Optic", "Backplane / High-Speed", "FFC/FPC", "Power", "PCIe", "M.2", "SATA", "SAS", "JST", "Molex"]`. Keep `pin_count` and `pitch_mm` **numeric** (range) — leaving `pitch_mm` numeric avoids a data_type flip that would orphan existing `value_numeric` data. Keep `mounting`, `gender`. `series` stays open-ended (typeahead, §4.5).

**Completeness audit (all other commodities):** a pass to ensure each `enum` facet's `enum_values` is canonical-complete (most already are — see `commodity_seeds.json`). Guidance values: research synthesis appendix (`docs/superpowers/specs/2026-06-08-materials-filter-research-appendix.md`, generated from the research run). No facet removals except optionally demoting known-noisy display-only specs (e.g. `transformers.turns_ratio`) to `is_filterable: false` — listed for confirmation, not auto-applied.

**Seed-change mechanics:** because `seed_commodity_schemas()` never updates existing rows (§3 gotcha), any **modified** existing seed (e.g. `hdd.interface` enum_values, `connectors.connector_type`) needs a one-time reconcile. Implement `reseed_changed_schemas()` (delete-then-insert for rows whose serialized definition differs from the seed) invoked once via an Alembic data migration `xxxx_reseed_commodity_schemas` (revision id ≤32 chars). New `(commodity, spec_key)` rows (e.g. `hdd.usage_class`) are picked up by the normal idempotent seeder.

### 4.4 Show every fixed-vocab value with a count incl. `(0)`

**`get_subfilter_options()` change** (`faceted_search_service.py:303-373`):
- Load `enum_values` from the schema row (already on `CommoditySpecSchema`).
- For `data_type == "enum"`:
  - If `schema.enum_values` is non-empty (**fixed vocab**): `option["values"] = schema.enum_values + [v for v in observed if v not in schema.enum_values]` (canonical order first, then any unexpected observed values appended). `option["widget"] = "checkbox"`.
  - Else (**open vocab**): `option["values"] = observed_sorted_by_count_desc[:TOP_N]`; `option["widget"] = "typeahead"`; `option["total_distinct"] = len(observed)`.
- For `data_type == "boolean"`: **always** `option["values"] = ["true","false"]` (drop the "only if backed" gate) so the toggle always shows with counts incl 0.
- `numeric` unchanged (range inputs; no per-value counts).

**Macro change** (`_macros.html` `checkbox_group`): always render the count, defaulting to 0:
```jinja
<span class="text-[11px] text-gray-400 tabular-nums">{{ counts[val] if (counts and val in counts) else 0 }}</span>
```
Fixed-vocab values render in **canonical (seed) order** (stable; common values first). The existing ">6 ⇒ Show all (N)" fold still applies so long canonical lists stay tidy. 0-count values render normally and remain selectable (consistent with multi-select; selecting one simply yields no rows).

**Counts stay context-aware** (Newegg/Mouser behavior): `get_facet_counts` already narrows by other active filters; no change needed. `subfilters.html` already passes `facet_counts.get(spec_key, {})`.

### 4.5 High-cardinality facets → search + top-N

- **Manufacturer** already uses `get_manufacturer_options(limit=20)` (top-20 by count) — keep; add a client-side search box above the list (filters the rendered top-N; "+N more" hint from `total_distinct`).
- **Open-ended enums** (`widget == "typeahead"`, e.g. connectors `series`): new macro `typeahead_group(spec_key, display_name, values, counts, total_distinct)` — a search `<input>` (Alpine-filtered) over the top-N observed values with counts, and a "+N more" affordance. No `(0)` dump.
- `TOP_N = 12` constant in `faceted_search_service.py`.

### 4.6 Noise control — relevance gating + essential/advanced fold

- **Relevance gating** is automatic: facets are commodity-scoped, so RPM (an `hdd` spec_key) only appears when HDD is selected; it never shows on SSD. No work needed beyond the taxonomy/seed correctness.
- **Essential vs advanced fold** in `subfilters.html`: render facets with `is_primary: true` (or `sort_order <= 4`) expanded; collapse the remainder under an Alpine `x-data="{ moreOpen: false }"` "More filters (N)" toggle. Keeps deep facets available without a wall of controls.

### 4.7 Data-confidence simplification (3 groups, default all, bottom)

**Backend contract unchanged** — `statuses` stays a CSV of the 7 enum values; the 3 groups are a UI grouping that expands to enum values.

- **Group → member tiers:**
  - **Trusted** → `verified, web_sourced, oem_sourced` (dot: emerald)
  - **AI-inferred** → `ai_inferred` (dot: amber)
  - **No data** → `not_catalogued, not_found, unenriched` (dot: gray)
- **Template** (`workspace.html`): replace the 7-row `trust_tiers` set with a 3-group structure. Each checkbox `:checked="confidenceGroupChecked('<group>')"`, `@change="toggleConfidenceGroup('<group>')"`. **Move the entire block from the top (`:35-62`) to the bottom** of `.p-3` — after the `#subfilters-container` (`:115`), before the mobile "Show Results" button.
- **Alpine** (`htmx_app.js`):
  - `statuses` default → all 7; `DEFAULT_STATUSES` → all 7.
  - Replace `TRUST_TIERS` with `CONFIDENCE_GROUPS = [{key:'trusted', label:'Trusted', dot:'bg-emerald-500', tiers:[...3]}, {key:'ai_inferred', label:'AI-inferred', dot:'bg-amber-500', tiers:['ai_inferred']}, {key:'no_data', label:'No data', dot:'bg-gray-400', tiers:[...3]}]`.
  - `confidenceGroupChecked(groupKey)` → every member tier ∈ `statuses`.
  - `toggleConfidenceGroup(groupKey)` → if all members present, remove all; else add all (dedup).
  - Chips: show one chip per **checked group** **only when narrowed** (`statuses` ≠ all 7); removing a chip unchecks that group. When all 7 selected (default) → no chips. Replace `nonDefaultStatuses`/`statusLabel` usage accordingly; update `activeFilterCount` to add the number of checked groups when narrowed, else 0.
  - Edge: unchecking all groups ⇒ empty `statuses` ⇒ backend applies no confidence filter (shows everything). Acceptable; documented.
  - `syncFromURL`/`pushURL` "clean URL when default" logic still works (default is now all 7). Legacy `verified_only`/`web_sourced` mapping retained.

### 4.8 Condition global facet  — ⚠️ CONFIRM AT REVIEW (touches data model)

Highest-value broker filter; cross-cutting, so a `MaterialCard` column, not a per-commodity spec.
- Migration (rev id ≤32 chars): add `material_cards.condition` `String(20)`, nullable, indexed. Canonical values: `New, Recertified, Refurbished, Used, Pulled, Unknown`.
- `get_global_facet_counts()` + `global.html`: render Condition like lifecycle/RoHS, counts incl 0.
- `search_materials_faceted()` + route + Alpine (`condition: []`, URL param, chips): OR-within, same pattern as `lifecycle`.
- Data source: backfill from offer/sighting provenance where present; otherwise null. **This is the one item separable from the core** — if cut, remove §4.8 entirely with no impact on §4.1-4.7.

## 5. Data model changes

- **None required for §4.1-4.7** (taxonomy = constant; filters = config; normalization = data backfill).
- Only **§4.8 (Condition)** adds a column. Alembic revision ids ≤32 chars; verify against live PG, not just SQLite (project norm).

## 6. Testing

- `tests/test_commodity_tree_invariant.py` — every child key ∈ `_DISPLAY_NAMES` ∩ seeds; Memory/Storage and Connectors/Electromechanical are separate groups; motors ∈ Electromechanical.
- `tests/test_subfilter_canonical_values.py` — `get_subfilter_options` returns full canonical `enum_values` (incl. unobserved) for fixed-vocab enums; appends unexpected observed; boolean always `["true","false"]`; open enum → `widget=="typeahead"` capped at TOP_N.
- `tests/test_facet_zero_counts.py` (route/macro) — a fixed-vocab value with no data renders with `(0)` and is selectable; HDD shows `SCSI` even with zero SCSI rows.
- `tests/test_category_normalizer.py` — alias map maps known variants; idempotent; unmapped strings pass through unchanged; dry-run mutates nothing.
- `tests/test_reseed_changed_schemas.py` — modified seed (e.g. hdd.interface) is reconciled; unchanged rows untouched; new rows inserted.
- `tests/test_materials_confidence_groups.py` (or JS-level) — group toggle adds/removes member tiers; default = all 7; narrowed chips correct.
- Run `pre-commit run --all-files` before pushing (project norm).

## 7. Docs

Update in the same PR (project norm): `docs/APP_MAP_DATABASE.md` (Condition column if included; category normalization), `docs/APP_MAP_INTERACTIONS.md` (filter sidebar order, confidence groups, counts-incl-0, typeahead). Note the new `category_normalizer` and `reseed_changed_schemas` utilities.

## 8. Build sequence

1. **Taxonomy split** (`COMMODITY_TREE` edit) + invariant test. Self-contained, shippable alone.
2. **Show-all-values + counts-incl-0**: `get_subfilter_options` + `_macros.html` + boolean-always + tests.
3. **Marquee seed curation** (hdd/ssd usage_class + interface incl. SCSI, connectors family, dram) + `reseed_changed_schemas` migration + completeness audit.
4. **Typeahead + manufacturer search** (open-enum widget + macro).
5. **Essential/advanced fold** in `subfilters.html`.
6. **Category normalization** (normalizer + dry-run/apply command + forward hook) + tests.
7. **Data-confidence 3-group + move to bottom** (template + Alpine) + tests.
8. **(Optional, confirm) Condition global facet** (migration + service + template + Alpine).
9. Docs + `pre-commit --all-files` + full suite.

## 9. Open items to confirm at review

1. **Condition global facet** (§4.8) — include now or defer? (Only item touching the data model.)
2. **Demote noisy display-only specs** (e.g. `transformers.turns_ratio`) to non-filterable — yes/list/no?
3. **Sidebar group order** (§4.1) — confirm the proposed order.
