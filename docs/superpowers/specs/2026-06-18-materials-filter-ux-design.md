# Materials Filter UX Improvements — Design Spec

**Date:** 2026-06-18
**Status:** Approved direction (user); grounded against current code.
**Sub-project of:** the 6-month "filters built out" program
(`project_filters_buildout_program_2026_06_17`), acting on the 2026-06-18
distributor-filter deep-research findings.

## Goal

Adopt three verified distributor-filter best-practices our materials filter tree
does not yet have, each grounded in research (DigiKey/Newegg/Nexar + NN/g):

- **P5** — order enum facet *overflow* values numerically (NN/g: numeric values low-to-high).
- **P3** — search-within long fixed-vocabulary enum facets (NN/g: long lists need search).
- **P2** — clickable **common-value chips** on numeric facets (Newegg/DigiKey bucket dense
  numeric ranges into multi-select brackets; raw min/max is slower for the common case).

**Explicitly NOT in scope** (decided, no ambiguity):

- **P1 (In-Stock / Has-Price toggles)** — ALREADY SHIPPED in `workspace.html` under the
  "Sourcing signals" fold (`toggleSourcingFlag('hasStock'|'hasPrice')`, URL-persisted,
  applied-filter chips). No work. Their intentional count-less design (the fold is static to
  avoid a per-facet vendor-history `EXISTS`) is preserved.
- **Multi-tier Condition grades** — data-gated; `MaterialCard.condition` is mostly NULL and we
  do not yet know what condition strings real inventory carries. Defer until measured.
- **New facet keys / enrichment** — separate program (vendor-API enrichment branch).

## Current code (the contract we build within)

- **Facet options:** `get_subfilter_options(db, commodity)` in
  `app/services/faceted_search_service.py:545`. Per `CommoditySpecSchema` row: enum w/
  `enum_values` → `widget="checkbox"` (full canonical list + overflow); enum w/o → `widget=
  "typeahead"` (top-`TOP_N=12` by count + search box); numeric → `widget="range"` with
  `option["range"]={"min","max"}`; boolean → Yes/No.
- **Filter application:** `_apply_facet_filters(...)` lines 77-115. Key shapes:
  `"{spec_key}_min"` → `value_numeric >= v`; `"{spec_key}_max"` → `value_numeric <= v`;
  `"{spec_key}": [vals]` (list) → `value_text.in_(vals)`. Each is an `EXISTS`-via-IN subquery
  on `MaterialSpecFacet` scoped to `(category==commodity, spec_key)`.
- **Facet counts:** `get_facet_counts(...)` lines 138-208 — counts **`value_text` only**, with
  pass-1 (all-filters) + pass-2 (OR-within self-exclusion for actively-filtered enum keys).
- **Templates:** `app/templates/htmx/partials/materials/filters/_macros.html`
  (`checkbox_group`, `range_input`, `boolean_toggle`, `typeahead_group`, `render_subfilter`
  dispatch) and `subfilters.html` (primary vs "More filters" disclosure).
- **Alpine:** `app/static/htmx_app.js` `materialsFilter()` — `subFilters {}`,
  `toggleFilter(key,val)`, `setRange(key,which,val)`, `clearSubFilters()`, `ui.facetSearch {}`,
  URL sync in `syncToURL`/`syncFromURL`, `applyFilters()`.
- Numeric spec values live in `MaterialSpecFacet.value_numeric`; enum/text in `value_text`.

## P5 — Numeric-aware overflow sort

**Change:** `get_subfilter_options` fixed-vocab branch (lines 618-621) appends unexpected
observed values via `sorted(observed)` (lexical: `"1210"` before `"205"`). Replace with a
natural-sort key (split into numeric/text runs) so numeric-ish overflow orders correctly. Seed
`enum_values` keep their curated order (size/value-ascending by construction); only the appended
overflow is natural-sorted.

**Helper:** `_natural_sort_key(s: str) -> tuple` (module-level), splits on digit runs and keys
numbers as ints. Pure function, unit-testable.

## P3 — Search-within for long fixed-vocab enums

**Change (UI only):** in `_macros.html` `checkbox_group`, when `values|length > 12`, render a
search `<input>` bound to `ui.facetSearch['{{ spec_key }}']` (identical to `typeahead_group`'s
box) and add to each label an `x-show` that matches the value against the search text
(case-insensitive `includes`). Below 12 values: unchanged (the "Show all" toggle still applies
for >6). No backend, no new state (`ui.facetSearch` already exists).

## P2 — Common-value chips on numeric facets

Add a multi-select chip row of the most common discrete values above each numeric facet's
min/max range, mirroring Newegg's bucketed enums. Range inputs remain for the long tail.

1. **Predicate** — `_apply_facet_filters`: new key shape `"{spec_key}__vals"` whose value is a
   list of numbers → `value_numeric.in_(values)`, deriving `spec_key = key[:-len("__vals")]`.
   OR-within-facet; AND-across. This branch MUST be checked BEFORE the generic
   `isinstance(values, list)` branch — that branch would otherwise capture the `__vals` key and
   wrongly match `value_text` with the un-stripped key. Guard non-empty list.
2. **Options** — `get_subfilter_options`: for numeric specs, add a grouped query of the top-N
   (`NUMERIC_CHIP_N = 8`) `value_numeric` values by distinct-card count for the commodity, and
   set `option["chips"] = [{"value": v, "count": c}, ...]` (ordered by the natural numeric value
   ascending for display). Keep `option["range"]`. Numeric specs with no facet rows → no chips.
3. **Counts** — `get_facet_counts`: add a numeric-value count path so chips show live counts incl
   self-exclusion. Mirror the `value_text` passes against `value_numeric` for numeric spec_keys
   that are chip-eligible; return under the spec_key as `{str(value): count}` (string-keyed to
   match template lookups). Counts must reflect all other active filters (pass 1) and exclude the
   facet's own selection when actively filtered (pass 2), exactly like the enum path.
4. **Alpine** — `toggleNumericChip(spec_key, value)`: maintain `subFilters[spec_key + '__vals']`
   as a numeric array (add/remove, delete key when empty); call `applyFilters()`. Include
   `*__vals` keys in `syncToURL`/`syncFromURL` (comma-joined numbers) and clear them in
   `clearSubFilters()`. The applied-filter chip strip and active-count include them.
5. **Template** — `_macros.html`: extend the numeric branch of `render_subfilter` to render a
   chip row (reuse the boolean-toggle button styling) above `range_input`, one button per
   `opt.chips` entry showing value + `(count)`, `:class` reflecting membership in
   `subFilters[spec_key + '__vals']`, `@click="toggleNumericChip(spec_key, value)"`. Omit the
   chip row when `opt.chips` is empty.

## Error handling

- All new predicates guard empty/missing lists (no-op, like the existing list branch).
- Non-numeric chip values can never arrive: chips are server-rendered from `value_numeric`; the
  Alpine setter only pushes those values; `syncFromURL` coerces to numbers and drops NaN.
- `get_facet_counts` numeric path is additive — `value_text` counting is untouched, so enum/
  boolean facets are unaffected.

## Testing (TDD per task)

- **P5:** unit-test `_natural_sort_key` ordering; `get_subfilter_options` overflow ordering with
  a seeded fixed-vocab enum + numeric-ish observed overflow.
- **P3:** frontend lint + build; a rendering assertion that the search input appears only when
  `>12` values (template test or e2e console-error check on a high-cardinality commodity, e.g.
  connectors).
- **P2:** `_apply_facet_filters` `__vals` → `value_numeric IN` (SQLite harness); chip options
  computed + ordered; `get_facet_counts` numeric counts with pass-2 self-exclusion; Alpine toggle
  (jsdom/frontend test if the suite covers it, else e2e); end-to-end render of chips with counts.
- Reuse existing faceted-search test modules; verify against live Postgres after deploy (numeric
  grouping is a DB-specific query — guard against the SQLite-masks-Postgres class).

## Success criteria

- Long enum facets (connectors' 19 types, storage form factors) get a working search box.
- Overflow enum values sort numerically.
- Numeric facets show clickable common-value chips with live counts that filter via
  `value_numeric IN (...)`, OR-within / AND-across, URL-persisted, in applied-filter chips.
- Full suite green; live-verified on real Postgres; no console errors on the materials page.
