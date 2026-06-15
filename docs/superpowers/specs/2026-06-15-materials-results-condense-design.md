# Materials search results — condense & clarify (design spec)

**Date:** 2026-06-15
**Surface:** `app/templates/htmx/partials/materials/list.html` (the results table rendered
by the `/v2/partials/materials/faceted` endpoint when a user searches/filters on
`/v2/materials`).
**Goal:** Make the returned results more condensed (less wasted space), cleaner / easier
to read, and make the result-count label read clearly as a *match count*.

## Problem

The results table renders **9 columns** (`MPN · Description · Manufacturer · Category ·
Lifecycle · Status · Vendors · Best Price · Last Searched`), forcing horizontal scroll and
crowding. Best Price shows 4 decimals (`$42.5000`) for every part. The count header reads
`1,234 parts`, which doesn't make clear it's "how many matched the current search/filters".

## Decisions (all resolved — no options left open)

### 1. Result-count label
Replace `{{total}} {{commodity_display}} parts` with a match-framed phrase:

- Visible: **`N results[ in <commodity>][ · matching "<q>"]`** (singular `result` when `N == 1`).
- SR (`aria-live`): **`N result[s][ in <commodity>][ matching <q>]`**.
- `commodity_display` and `q` are already passed to the template; render server-side.

### 2. Column consolidation: 9 → 7 columns
New header row: **`MPN · Description · Manufacturer · Status · Vendors · Best Price · Last Seen`**

- **Drop the standalone Category column.** Fold `category` as a muted sub-line
  (`text-[11px] text-gray-400`, reuse existing arbitrary value) under the manufacturer in
  the Manufacturer cell. Renders nothing when `category` is empty.
- **Merge Lifecycle + Status into one "Status" cell.** One `flex flex-wrap` badge group:
  enrichment-trust badge (VERIFIED / WEB-SOURCED / OEM-SOURCED / AI GUESS / NOT FOUND /
  NOT CATALOGUED) **then** lifecycle badge **then** condition badge. `--` only when all
  three are absent. No badge palettes, links, titles, or colors change — only their cell
  grouping. **No data is removed; nothing in the sidebar filters changes.**
- "Last Searched" header → **"Last Seen"** (shorter); cell value/format unchanged.

### 3. Best Price formatting
`$%.2f` when `_best_price >= 1`, else `$%.4f` (keep sub-dollar precision for passives).
Aligns with the existing `_vendor_row.html` "best price seen" convention. Currency prefix
logic unchanged.

### 4. Density
- Spec pills (`_primary_specs`): tighten gap and leading; keep max 3, same data/tooltip.
- Add a **scoped** `.compact-table--dense` modifier in `app/static/styles.css` (td vertical
  padding 6px → 4px, th 8px → 6px) and apply `compact-table compact-table--dense` to *this*
  table only. **Do not modify the shared `.compact-table` class** — sightings/parts/quotes
  reuse it.

## Out of scope / unchanged
- HTMX row click-through (`hx-get` → detail), endpoints, `MaterialCard` fields/queries.
- The detail card (`detail.html`), sidebar filters, FRU section, pagination, empty states.
- No new arbitrary Tailwind values (reuse `max-w-[160px]`, `text-[10px]`, `text-[11px]`).

## Tests
- Update `tests/test_filter_ux_live_count.py` to assert the new "results" copy + singular/
  plural + `aria-live` still present.
- Add a render test: merged Status cell shows trust + lifecycle + condition together;
  Category renders as the muted sub-line under Manufacturer; header has **no** standalone
  `Category`/`Lifecycle` `<th>`; Best Price shows 2 decimals for a ≥$1 price and 4 for a
  sub-dollar price; count reads `… matching "<q>"` when `q` is present.

## Files
- `app/templates/htmx/partials/materials/list.html` — edit (count, headers, cells).
- `app/static/styles.css` — add scoped `.compact-table--dense`.
- `tests/test_filter_ux_live_count.py` — update count assertions.
- `tests/test_materials_results_condense.py` — new render tests.
- `docs/APP_MAP_INTERACTIONS.md` — note the 7-column results layout + merged Status cell.
