# Materials Filter Sidebar — Direction B (Commodity-First) + Refinements

**Date:** 2026-06-08
**Branch:** `feat/materials-filter-ux-b` (off main `3e092bfa`, which has the filter rework)
**Status:** Awaiting user review

Builds on the shipped filter rework (PR #228). Reorganizes the materials faceted sidebar into a commodity-first workflow tool and folds in the must-have refinements from the 4-lens UX critique. Counts-incl-`(0)`, typeahead, the 3-group confidence filter, and Condition all carry over unchanged.

## 1. Goals
1. **Commodity-first layout** — category tree at the top drives the view; the selected commodity's deep facets are the focus; secondary controls are one click away.
2. **Trustworthy, legible feedback** — fix the facet-count bug; show a live result count; make the dense rail skimmable.
3. **Fast re-entry / daily-driver ergonomics** — recents, type-to-find, persistent layout, shareable links, no state loss on every click.

## 2. Non-goals (Phase 2 / cut)
- **Phase 2 (separate cycle):** distribution-banded numeric facets, server-side typeahead beyond top-N, "remove X to see N parts" zero-result culprit-naming, saved/named filter views, debounced multi-toggle burst.
- **Cut (do not build):** desktop "Apply" batch button (keep live-apply); hand-maintained range-preset maps; promoting recents/saved-views to a DB table (localStorage only — single-user staging); `role=region` on all 13 groups + every fold (harms AT — reserve for the 2–3 top-level sections); flipping the always-show-`(0)` default.

## 3. Current state (refs)
- Sidebar: `app/templates/htmx/partials/materials/workspace.html` — order today: Manufacturer (HTMX) → Global facets (HTMX) → "All Materials" → Commodity tree (HTMX) → Sub-filters (HTMX) → Data confidence (Alpine `x-for`). Mobile = slide-over drawer + "Show Results".
- Partials: `filters/{tree,subfilters,manufacturers,global,_macros}.html`. Sub-filters render is_primary expanded + "More filters (N)" fold; open-vocab → `typeahead_group`; per-facet fold + typeahead live in **partial-local** `x-data` (lost on reload).
- Alpine `materialsFilter` (`app/static/htmx_app.js`): `commodity, subFilters{}, statuses[], lifecycle[]/rohs[]/condition[]/hasDatasheet, q, page`; URL-persisted; `clearSubFilters()` (specs only); `activeFilterCount`; `selectCommodity()` wipes `subFilters` only. Bundled Alpine plugins: confirm `@alpinejs/persist`, `@alpinejs/focus`, `@alpinejs/collapse`, `htmx-ext-alpine-morph`/`@alpinejs/morph` in the Vite entry before relying on them (§7 verifies).
- Counts: `faceted_search_service.get_facet_counts()` (the bug, §4.1). Results partial `filters/.../list.html` (or the faceted partial) receives `total`.

## 4. Design

### 4.1 BUG FIX — facet-count self-exclusion (do first)
`get_facet_counts(db, commodity, active_filters)` currently narrows the base card set by **all** `active_filters` (via `_apply_facet_filters`), then counts every spec_key against that set. So a facet's own selection collapses its sibling values to `(0)` and multi-select-within-a-facet reads as deselecting.

**Correct semantics (OR-within-facet, AND-across-facets):** a facet's value counts must be computed against the set narrowed by **every other** active facet, **excluding that facet's own selection**.

**Implementation:** in `get_facet_counts`, split the spec_keys:
- Keys **not** present in `active_filters`: count once against the set narrowed by all of `active_filters` (current behavior — correct for them).
- Each key **present** in `active_filters` (including its `_min`/`_max` pair): count against the set narrowed by `active_filters` **minus that key's own entries**. One extra grouped query per actively-filtered facet (active facets are few).
Treat `_min`/`_max` for a numeric `spec_key` as belonging to that key (exclude both when self-counting). Manufacturer/global facets are not in `sub_filters` so are unaffected. Return shape unchanged.

**Test (`tests/test_facet_counts_self_exclusion.py`):** two cards, interface SATA and SAS, same commodity; with `active_filters={"interface":["SATA"]}`, assert `counts["interface"]` shows **both** `SATA:1` and `SAS:1` (sibling not collapsed), while a *different* facet (e.g. form_factor) is correctly narrowed to the SATA card only. **Run against Postgres**, not just SQLite (per the project's SQLite-masks-PG trap).

### 4.2 Layout reorg (Direction B) — `workspace.html`
New top→bottom order inside `.p-3`:
1. **Sticky summary band** (`position: sticky; top:0`, white bg + hairline border): when filters active → `<N> active · Clear all · Copy link`; when none → a single muted hint line (`Filters`). `Clear all` = `clearAllFilters()` (resets everything **except** `commodity` + keeps default confidence). `Copy link` copies `window.location.href`, flashes "Copied".
2. **Recents strip + type-to-find** (§4.5), then the **Category tree** (existing `tree` HTMX container) — moved to top.
3. **Selected commodity's facets** — existing `#subfilters-container`, rendered right under the tree. When no commodity: quiet single-line hint "Pick a category to filter by its specs."
4. **"More attributes"** — a collapsed-by-default disclosure wrapping the existing `#manufacturer-filter-container` + `#global-filter-container` (Manufacturer + Lifecycle/RoHS/Condition/Datasheet). Header shows a shared active-count badge (§4.6) summing manufacturer+lifecycle+rohs+condition+datasheet selections. Containers still `hx-trigger="load"` (load while hidden).
5. **Data confidence** — pinned at bottom, collapsed-by-default disclosure; header shows active-count badge when narrowed.
6. Mobile "Show Results" button gains the **live result count** (§4.3) next to the active count.

### 4.3 Live result count
- The faceted results partial already receives `total`. Render a persistent line at the **top of the results pane**: `<N> parts` (or `<N> <Commodity> parts` when a commodity is selected), re-rendered every `filters-changed` cycle (it's in the swapped partial). Not gated on `total > limit` (today's footer is).
- Add an `sr-only` `aria-live="polite"` region announcing "`<N>` parts match" on swap; set `aria-busy` on `#materials-results` during the request.

### 4.4 Preserve sub-filter state across reloads (root-cause: hoist state)
Every `filters-changed` re-GETs `#subfilters-container` and `innerHTML`-swaps it, destroying partial-local `x-data` (fold `moreOpen`, per-facet `expanded`, typeahead `taSearch`, scroll). In B the facets are the primary surface, so this is the load-bearing fix.
- **Hoist UI state into the persistent `materialsFilter` component:** add a `ui` object — `ui.moreOpen` (bool), `ui.moreAttrsOpen` (bool), `ui.confidenceOpen` (bool), `ui.facetExpanded` (`{[spec_key]: bool}`), `ui.facetSearch` (`{[spec_key]: string}`). Re-point the macros: `checkbox_group`/`typeahead_group`/`subfilters.html` bind `expanded`/`taSearch`/`moreOpen` to `ui.*[spec_key]` on the **parent** component instead of partial-local `x-data`. Re-rendering then re-binds to surviving state.
- **Swap strategy:** set `#subfilters-container` `hx-swap="morph:innerHTML"` (alpine-morph, if bundled — else `innerHTML`; hoisted state survives either way) to minimize reflow + preserve scroll. If morph isn't bundled, hoisting alone satisfies the requirement; morph is an enhancement.

### 4.5 Recents + type-to-find (re-entry accelerators)
Pinned under the summary, above the tree:
- **Recents:** 4–6 most-recent commodity pills, `$persist`ed (or manual `localStorage`) list, deduped/capped, pushed in `selectCommodity()`. One click re-enters. Render `get_display_name`.
- **Type-to-find:** a "Jump to category…" input (`x-model`) that client-side-filters the rendered tree buttons by substring (`x-show`); Enter selects a single match. Reuses the `manufacturers.html` typeahead pattern.

### 4.6 Density / a11y / persistence (touch the rail once)
- **Two header styles** (rail-section vs facet-group), **one disclosure affordance** (full-width chevron row, reusing the tree pattern + `@alpinejs/collapse` if bundled else `x-show`+`x-cloak`), **one count format** (bare right-aligned `tabular-nums`; parens reserved for inline disclosure-label counts like "More attributes (3)").
- **Shared active-count badge** component (muted pill, `bg-gray-100 text-gray-600`; `brand-100/brand-700` when active), shown only when count>0, on collapsed sections.
- **Dim-during-reload:** give `#subfilters-container`/`#manufacturer-filter-container`/`#global-filter-container` a dimmed (`opacity-50 pointer-events-none`) state bound to their `hx-indicator` instead of going blank on `commodity-changed`.
- **Zero-row convention:** keep "show `(0)`, greyed + disabled" for the **selected commodity's** sub-filter values (terminal signal); keep **hide-zero** in the navigational tree + global facets. Add a greyed/disabled style to 0-count sub-filter rows (don't waste a click).
- **Selected-state visibility:** active tree row + active facet row use `border-l-[3px] bg-brand-100 text-brand-800 font-semibold` (brand-50 is ~white; invisible today).
- **Persist chrome (not state):** `$persist` (or manual localStorage) the `ui.moreOpen/moreAttrsOpen/confidenceOpen` + category-group open/closed. Filter selections stay URL-bound (unchanged).
- **a11y:** mobile drawer gets `x-trap.noscroll` (focus trap), focus-return-to-trigger on close, `@keydown.escape.window` close, `role="dialog" aria-modal aria-label`. Top-level disclosures (More attributes, Data confidence) get `aria-controls` → panel `id`, panel `role="region" aria-labelledby`. Do **not** add `role=region` to the 13 groups/per-facet folds (plain `aria-controls`/`aria-expanded` only). After `commodity-changed` resolves, `scrollIntoView({block:"nearest"})` the subfilters container + move focus to its heading (`tabindex=-1`).

### 4.7 Alpine state additions (`htmx_app.js`)
- `ui: { moreOpen, moreAttrsOpen:false, confidenceOpen:false, facetExpanded:{}, facetSearch:{} }` (persist the first three + category-group state).
- `recentCommodities: []` (persisted).
- `clearAllFilters()`: reset `subFilters={}`, `lifecycle/rohs/condition=[]`, `hasDatasheet=false`, `statuses=[...DEFAULT_STATUSES]`, `q=''`; **keep** `commodity`; then `applyFilters()`.
- `copyLink()`: `navigator.clipboard.writeText(location.href)` + transient "Copied" flag.
- `attributesActiveCount` getter (manufacturer+lifecycle+rohs+condition+datasheet) for the More-attributes badge.
- `selectCommodity()`: also push to `recentCommodities`.

## 5. Data model / backend
- **Only** `get_facet_counts` changes (§4.1). No schema/migration. Everything else is template + Alpine + CSS.

## 6. Testing
- `tests/test_facet_counts_self_exclusion.py` — §4.1, **Postgres-verified**.
- `tests/test_faceted_routes.py` additions — results partial renders the live `<N> parts` line at the top (not only the footer); subfilters render hoisted `ui.` bindings (no partial-local `x-data` for folds); workspace renders the summary band + "More attributes" + recents/jump box + reordered structure (tree before sub-filters before More-attributes before confidence).
- Existing faceted/confidence/condition tests stay green; update any that assert the old sidebar order or the old `clearSubFilters` "Clear all" label.
- `pre-commit run --all-files` before push.

## 7. Build sequence (units = stacked commits)
1. **Facet-count self-exclusion fix** + Postgres-verified test (standalone, shippable alone).
2. **Verify bundled Alpine plugins** (`$persist`/`focus`/`collapse`/`morph`) in the Vite entry; add any missing that §4.4/4.6 rely on (or fall back as noted).
3. **Live result count** (+ aria-live/aria-busy) in the results partial.
4. **Hoist sub-filter UI state** to `materialsFilter.ui` + re-point macros + morph swap (§4.4).
5. **Layout reorg** (§4.2): reorder workspace.html; summary band + clearAllFilters + copyLink; "More attributes" wrapper; confidence collapsible.
6. **Recents + type-to-find** (§4.5).
7. **Density/a11y/persistence pass** (§4.6): headers/affordance/count format, dim-on-reload, zero-row styling, selected-state, focus-trap/aria, persist chrome.
8. Docs (`APP_MAP_INTERACTIONS.md` — new sidebar order + behaviors) + `pre-commit --all-files`.

## 8. Open items to confirm
1. **Data confidence collapsed by default** at the bottom — yes? (Recommended yes; it's lowest-priority.)
2. **Active-filter chips** — keep the detailed removable chips in the **results header** (current), with only the compact `N active · Clear all` summary in the sidebar? (Recommended yes — no duplication.) Or move chips into the sidebar summary too?
3. **Recents cap** — 5? And reset on "Clear all"? (Recommended: cap 5, recents persist across Clear-all since they're navigation history, not filters.)
