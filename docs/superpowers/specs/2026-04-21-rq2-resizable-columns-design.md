# Requisitions Resizable Columns, Split, and Tooltips — Design

**Date:** 2026-04-21
**Scope:** `/requisitions2` page (both panels)
**Stack:** HTMX 2.x + Alpine.js 3.x + Jinja2 + Tailwind (no React)
**Backend impact:** none (purely frontend)

---

## Problem

On the reqs page, column values are cut off with `…` ellipsis. The user cannot see full names, customers, or long MPNs without clicking into the detail panel. Needs a solution that does not cut off — the user should be able to resize columns or hover to see full values.

Affected columns today:
- Left list (`_table_rows.html`): Name hard-capped at `max-w-[180px]`, Customer at `max-w-[140px]`
- Detail header (`_detail_panel.html`): title `truncate`
- Parts tab table: MPN column can overflow; whole table uses `overflow-x-auto`

---

## Goals

1. No `…` cutoff that hides information from the user
2. User can drag column edges to resize — both tables
3. User can drag the split divider to rebalance left/right panels
4. Hover a truncated cell and see full value in a tooltip (only when actually truncated)
5. Preferences persist per-user via `localStorage`
6. Survives HTMX swaps (SSE refresh, action-triggered table re-renders)
7. Reusable: components built so follow-up PRs can adopt on `parts`, `materials`, `vendors`, `customers`, `excess`, `quotes` tables without rewriting infrastructure

## Non-goals

- Redesign of the reqs page layout
- Changing which columns appear (column chooser is future work)
- Mobile-first drag UX (keyboard access included, touch inherits splitPanel's existing touch handlers)
- Retrofitting other tables in this PR (component is reusable, rollout separate)

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│ Filters / Bulk bar                                           │
├───────────────────────────┬──┬──────────────────────────────┤
│  Left list panel          │║ │  Right detail panel          │
│  resizableTable('rq2-list')│║ │  resizableTable('rq2-parts') │
│  <colgroup><col><col>...   │║ │  <colgroup><col><col>...     │
│  handles on <th> borders   │║ │  handles on <th> borders     │
│                           │║ │                              │
│                           │║◄── splitPanel('rq2', 40)       │
└───────────────────────────┴──┴──────────────────────────────┘
         tooltips (x-truncate-tip) on any truncated cell
```

**Single persistence model:** `localStorage` keys (direct, per existing codebase convention — `@alpinejs/persist` plugin is loaded but unused):
- `avail_split_rq2` → number (percent, 20-70)
- `avail_table_cols_rq2-list` → `{colKey: px}`
- `avail_table_cols_rq2-parts` → `{colKey: px}`

---

## Component 1: Split divider — reuse existing

`Alpine.data('splitPanel', (panelId, defaultPct) => {...})` already exists at `app/static/htmx_app.js:211-274`. Used on `/sourcing/workspace.html:63`. No new JS.

**Changes in `requisitions2/page.html`:**
- Wrap the split region in `<div id="split-rq2" x-data="splitPanel('rq2', 40)">`
- Remove hard constraint `w-2/5 min-w-[300px] max-w-[500px]` on left panel; bind `:style="'width:' + leftWidth + '%'"` instead
- Add `min-w-0` on right panel (so its children can shrink below content width)
- Insert divider element with `@mousedown="startResize($event)"` and `@touchstart.prevent="startTouchResize($event)"`
- A11y: `role="separator"`, `tabindex="0"`, left/right arrow keys nudge 2% at a time

Constraints: 20–70% (enforced by existing splitPanel). Container id `split-rq2` required by splitPanel's `getElementById`.

---

## Component 2: `resizableTable` — new, reusable

**Location:** `app/static/htmx_app.js`, registered alongside existing `splitPanel` (~line 275, inside the same `alpine:init` scope).

**API:**
```js
Alpine.data('resizableTable', (tableKey, defaults) => ({
  widths: {}, _resizing: null, _storageKey: 'avail_table_cols_' + tableKey,
  init(),              // read localStorage, merge with defaults
  colStyle(key),       // returns 'width:Npx;min-width:Npx'
  startColResize(e, key),  // mousedown → document mousemove/mouseup until drop
  autoFitCol(key),     // double-click handle → clear saved width
  resetAll(),          // clear all widths, remove localStorage key
}))
```

**Drag math:** `newWidth = max(40, startWidth + (clientX - startX))`. Stored in pixels (users think in fixed widths, survives window resize gracefully). Minimum 40px prevents users losing columns.

**HTMX swap safety:** When `#rq2-table` is swapped (SSE refresh at `page.html:90`), the colgroup inside gets replaced. The component's `init()` attaches an `htmx:afterSwap` listener on `this.$el` that re-assigns `this.widths = { ...this.widths }` — this forces Alpine to re-evaluate `:style` bindings on the fresh `<col>` elements. Templates do NOT need to add `@htmx:after-swap` attributes; the component owns this concern.

**Placement requirement (critical):** `x-data="resizableTable(...)"` MUST sit on an element that is NOT the swap target or inside it. In this codebase:
- **Left list:** SSE, sort, and pagination all swap `#rq2-table` with `hx-swap="innerHTML"` (see `page.html:88-90`, `_table.html:26`). The `x-data` therefore goes on `#rq2-table` itself in `page.html`, NOT on the inner wrapper in `_table.html`. The partial renders the `<colgroup>` + `<table>` inside and consumes `colStyle` from the parent Alpine scope.
- **Right parts table:** The detail panel is swapped into `#rq2-detail` on row click (`_table_rows.html:11`). The `x-data` therefore goes on the parts-table wrapper inside `_detail_panel.html` — because when `#rq2-detail` is replaced, a fresh Alpine scope is created anyway and the `init()` reads saved widths from localStorage. Columns appear immediately with the correct widths; no swap-recovery gymnastics needed for this panel.

Result: the left table needs the `htmx:afterSwap` re-assignment trick (because `x-data` persists across swaps inside it); the right parts table doesn't (because its `x-data` is re-created fresh from localStorage each time a different requisition is opened).

**Consumer contract:**
1. Root wrapper: `x-data="resizableTable('rq2-list', {select:36, name:200, ...})"`
2. Table: `<table class="resizable-cols">` (adds `table-layout: fixed`)
3. `<colgroup>` with one `<col :style="colStyle('key')">` per column
4. Each resizable `<th class="resizable">` ends with `<span class="col-resize-handle" @mousedown="startColResize($event,'key')" @dblclick="autoFitCol('key')"></span>`
5. Last column omits the handle (no column to its right)
6. Optional reset menu invokes `resetAll()`

**CSS in `styles.css`:**
```css
table.resizable-cols { table-layout: fixed; }
th.resizable { position: relative; }
.col-resize-handle {
  position: absolute; top: 0; right: 0; width: 6px; height: 100%;
  cursor: col-resize; user-select: none; background: transparent;
  transition: background 0.15s;
}
.col-resize-handle:hover, .col-resize-handle:active {
  background: rgb(99 146 204 / 0.4);
}
```

---

## Component 3: `x-truncate-tip` — new directive

**Location:** `app/static/htmx_app.js`, registered via `Alpine.directive('truncate-tip', ...)` before `Alpine.start()`.

**Behavior:**
- On `mouseenter`, check `el.scrollWidth > el.clientWidth`. If text fits, do nothing.
- If truncated, append a `<div class="truncate-tip">` to `document.body`, fill with `el.textContent.trim()` (or `data-tip-text` override), position with `getBoundingClientRect` math (prefer above, flip below if off-screen, clamp horizontally).
- On `mouseleave` / `focusout`, remove the tip.
- No external dependency (not using `x-anchor` — that plugin targets persistent popovers, not ephemeral hover tips).

**CSS in `styles.css`:**
```css
.truncate-tip {
  position: fixed; z-index: 9999; max-width: 420px;
  padding: 0.4rem 0.6rem; font-size: 0.8rem; line-height: 1.3;
  color: #fff; background: rgba(17,24,39,0.95);
  border-radius: 0.375rem; box-shadow: 0 4px 12px rgba(0,0,0,0.15);
  pointer-events: none; white-space: normal; word-wrap: break-word;
  opacity: 0; transition: opacity 0.12s ease-out;
}
.truncate-tip.visible { opacity: 1; }
```

**A11y:** keep a standard `title` attribute on the underlying element (or its container) as a fallback for screen readers; the visual tooltip is an enhancement, not the only accessibility path.

**Touch:** tap-and-hold fires `mouseenter`; acceptable baseline for hosted CLI environment. Native `title` has no touch UX either.

---

## Component 4: Template changes

**`requisitions2/page.html`** (for left list `x-data`)
- On `#rq2-table` add `x-data="resizableTable('rq2-list', {select:36, name:200, status:110, customer:160, count:60})"` (this is the swap target, so `x-data` must live here, not inside `_table.html`)

**`requisitions2/_table.html`**
- Add `<colgroup>` with five `<col :style="colStyle('...')">` (colStyle comes from parent `#rq2-table` Alpine scope)
- Add `class="resizable-cols"` to `<table>`
- Add `class="resizable"` and handle `<span>` to each `<th>` except the last
- Add a small `⋯` icon button in the last header cell (or a compact utility row above the table) showing a dropdown with "Reset columns" → `resetAll()`. Discoverable via visible affordance, not right-click.

**`requisitions2/_table_rows.html`**
- Remove `max-w-[180px]` (Name) and `max-w-[140px]` (Customer) — column widths now control
- Keep `truncate` on inner `<span>`
- Add `x-truncate-tip` to Name and Customer cells

**`requisitions2/_detail_panel.html`**
- Add `x-truncate-tip` to header `<h2>` (req.name) and customer `<p>`
- Wrap parts table in `<div x-data="resizableTable('rq2-parts', {mpn:180, qty:90, price:110, status:110, actions:90})">` (wrapper is inside `_detail_panel.html` — which is itself the swap content of `#rq2-detail`, so a fresh Alpine scope is created on each requisition open; init() reads localStorage)
- Add `<colgroup>`, `resizable-cols`, `resizable` class + handle on `<th>` (except last)
- Add `x-truncate-tip` on MPN cells

**`requisitions2/page.html`**
- Change split wrapper to `<div id="split-rq2" x-data="splitPanel('rq2', 40)">`
- Remove `w-2/5 min-w-[300px] max-w-[500px]` on left panel; add `:style="'width:' + leftWidth + '%'"` + `flex-shrink-0 overflow-hidden`
- Add divider element with drag handlers + keyboard access
- Add `min-w-0` to right panel

**`app/static/htmx_app.js`**
- Add `Alpine.data('resizableTable', ...)` near line 275
- Add `Alpine.directive('truncate-tip', ...)` before `Alpine.start()`

**`app/static/styles.css`**
- Add `.resizable-cols`, `.col-resize-handle`, `th.resizable`, `.truncate-tip` rules

**Not touched:** `_filters.html`, `_bulk_bar.html`, `_modal.html`, `_inline_cell.html`, `_detail_empty.html`, `_single_row.html`, any router or backend code, any migration.

---

## Testing

**Playwright E2E** (`e2e/requisitions2-resize.spec.ts`):
1. Load `/requisitions2`, open a requisition
2. Drag split divider right, reload page, assert ratio persists
3. Drag Name column wider, reload, assert `avail_table_cols_rq2-list` in localStorage, assert width applied
4. Hover a long name, assert `.truncate-tip.visible` appears with full text
5. Hover a short name (fits), assert no `.truncate-tip` appears
6. Trigger SSE table refresh, assert column widths survive the swap
7. Double-click handle, assert column resets to default
8. Right-click header → Reset columns, assert all widths cleared

**No unit tests** — pure DOM + localStorage behavior, fully covered by E2E.

**Manual QA:**
- Keyboard: tab to divider, left/right arrows nudge ratio
- Window resize: pixel widths hold, divider percent holds
- Min-width: drag column to minimum, can't go below 40px
- Mobile/narrow: splitPanel's existing touch handlers work; layout usable down to ~700px

---

## Memory / docs updates

Per `feedback_update_app_map.md`: update `docs/APP_MAP_INTERACTIONS.md` to document:
- `resizableTable` Alpine component API and consumer contract
- `x-truncate-tip` directive usage
- `splitPanel` used on `/requisitions2` (extending existing usage list)

---

## Rollout

Single PR, deployed via `./deploy.sh` (with `--no-cache` per `feedback_deploy_cache.md`). No feature flag, no migration, no data backfill. Immediately reversible by reverting the commit.

## Follow-up (separate PRs, not this spec)

- Parts list (`htmx/partials/parts/list.html`) — adopt `resizableTable`
- Materials list — adopt
- Vendors, customers, excess, quotes — adopt
- Column chooser (show/hide columns) — deeper redesign, out of scope
