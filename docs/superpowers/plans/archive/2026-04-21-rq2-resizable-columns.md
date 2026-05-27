# Requisitions Resizable Columns + Split + Tooltips — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** End visible column cutoff on `/requisitions2` by adding a resizable split divider (reused), resizable table columns (new reusable component), and hover tooltips on truncated cells (new reusable directive).

**Architecture:** Frontend-only change. Reuse existing `splitPanel` Alpine component. Add two new reusable Alpine primitives to `app/static/htmx_app.js`: `resizableTable` component (drag handles + `<colgroup>` widths persisted to `localStorage`, with HTMX swap recovery) and `x-truncate-tip` directive (DOM-appended tooltip that only fires when text is actually clipped). Templates wired via Jinja2 include partials. No backend, router, schema, or migration changes.

**Tech Stack:** HTMX 2.x + Alpine.js 3.x + Jinja2 + Tailwind CSS + Playwright E2E.

**Spec:** `docs/superpowers/specs/2026-04-21-rq2-resizable-columns-design.md`

---

## File Inventory

**Modify:**
- `app/static/htmx_app.js` — add `resizableTable` Alpine component (~line 275, near existing `splitPanel`) + `x-truncate-tip` directive (before `Alpine.start()`)
- `app/static/styles.css` — add `.resizable-cols`, `th.resizable`, `.col-resize-handle`, `.truncate-tip` CSS rules
- `app/templates/requisitions2/page.html` — replace split wrapper with `splitPanel`, add `x-data="resizableTable(...)"` on `#rq2-table`
- `app/templates/requisitions2/_table.html` — add `<colgroup>`, `resizable-cols` class, handles on `<th>`, reset menu
- `app/templates/requisitions2/_table_rows.html` — remove `max-w-[180px]` and `max-w-[140px]`, add `x-truncate-tip` on Name + Customer cells
- `app/templates/requisitions2/_detail_panel.html` — wrap parts table in `resizableTable`, add colgroup/handles, add tooltips to header + MPN cells
- `docs/APP_MAP_INTERACTIONS.md` — document new `resizableTable` component and `x-truncate-tip` directive

**Create:**
- `e2e/requisitions2-resize.spec.ts` — Playwright E2E covering split drag, column drag, swap survival, tooltip show/hide, reset.

**Not touched:** routers, models, schemas, migrations, other templates, `requisitions2.js`.

---

## Execution Order

Task flow follows TDD where feasible: write one Playwright E2E test per behavior, run it to confirm fail, build the minimum to pass, then next test. Pure behavioral code (drag handlers, tooltip math) is verified via E2E, not unit tests — this codebase has no Vitest suite for htmx_app.js components and setting one up is out of scope.

Tasks 1–4 build the split divider (lowest risk, reuses existing code). Tasks 5–9 build `resizableTable` (core new component). Tasks 10–12 build tooltips. Task 13 wires the right parts table. Tasks 14–16 finish polish, docs, and deploy verification.

---

## Task 1: Create E2E test file and split divider persistence test

**Files:**
- Create: `e2e/requisitions2-resize.spec.ts`

- [ ] **Step 1: Create the E2E test file with a failing split-persistence test**

Write this to `e2e/requisitions2-resize.spec.ts`:

```typescript
/**
 * requisitions2-resize.spec.ts — E2E tests for resizable split, columns, and tooltips.
 * Called by: npx playwright test e2e/requisitions2-resize.spec.ts
 * Depends on: running app server with test auth bypass (TESTING=1)
 */
import { test, expect, Page } from '@playwright/test';

const REQS_URL = '/requisitions2';

async function clearLayout(page: Page) {
  await page.goto(REQS_URL);
  await page.evaluate(() => {
    localStorage.removeItem('avail_split_rq2');
    localStorage.removeItem('avail_table_cols_rq2-list');
    localStorage.removeItem('avail_table_cols_rq2-parts');
  });
}

test.describe('Requisitions split divider', () => {
  test('split divider is draggable and position persists after reload', async ({ page }) => {
    await clearLayout(page);
    await page.goto(REQS_URL);

    const divider = page.locator('[role="separator"][aria-label="Resize panels"]');
    await expect(divider).toBeVisible();

    const box = await divider.boundingBox();
    if (!box) throw new Error('divider not visible');

    // Drag divider 150px to the right
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + 150, box.y + box.height / 2, { steps: 10 });
    await page.mouse.up();

    const savedPct = await page.evaluate(() => localStorage.getItem('avail_split_rq2'));
    expect(savedPct).not.toBeNull();
    const pct = Number(savedPct);
    expect(pct).toBeGreaterThan(40);
    expect(pct).toBeLessThanOrEqual(70);

    // Reload; assert the saved width is applied
    await page.reload();
    const leftPanel = page.locator('#split-rq2 > div').first();
    const style = await leftPanel.getAttribute('style');
    expect(style).toContain(`width: ${pct}%`);
  });
});
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `cd /root/availai && npx playwright test e2e/requisitions2-resize.spec.ts --project=workflows --reporter=list`

Expected: FAIL — `[role="separator"][aria-label="Resize panels"]` not found (element does not exist yet).

- [ ] **Step 3: Commit the failing test**

```bash
git add e2e/requisitions2-resize.spec.ts
git commit -m "test(requisitions2): failing E2E for resizable split divider"
```

---

## Task 2: Wire `splitPanel` into `requisitions2/page.html`

**Files:**
- Modify: `app/templates/requisitions2/page.html:93-108`

- [ ] **Step 1: Replace the split-screen wrapper**

Edit `app/templates/requisitions2/page.html`. Replace the block starting at line 93 (`{# ── Split-screen workspace ────────────────────────────── #}`) and ending at line 108 (`</div>` closing the flex row) with:

```html
      {# ── Split-screen workspace ────────────────────────────── #}
      <div id="split-rq2"
           class="flex flex-1 overflow-hidden min-h-0"
           x-data="splitPanel('rq2', 40)">

        {# Left panel: compact requisition list #}
        <div class="border-r border-gray-200 flex flex-col bg-white flex-shrink-0 overflow-hidden"
             :style="'width:' + leftWidth + '%'">
          <div id="rq2-table" class="flex-1 overflow-y-auto">
            {% include "requisitions2/_table.html" %}
          </div>
        </div>

        {# Resizable divider #}
        <div class="w-1 bg-gray-200 hover:bg-brand-400 cursor-col-resize flex-shrink-0 transition-colors"
             role="separator" aria-label="Resize panels" tabindex="0"
             @mousedown="startResize($event)"
             @touchstart.prevent="startTouchResize($event)"
             @keydown.left.prevent="leftWidth = Math.max(20, leftWidth - 2); localStorage.setItem('avail_split_rq2', leftWidth)"
             @keydown.right.prevent="leftWidth = Math.min(70, leftWidth + 2); localStorage.setItem('avail_split_rq2', leftWidth)"></div>

        {# Right panel: detail view #}
        <div id="rq2-detail" class="flex-1 overflow-y-auto bg-white min-w-0">
          {% include "requisitions2/_detail_empty.html" %}
        </div>

      </div>
```

Key removals: `w-2/5 min-w-[300px] max-w-[500px]` on left panel (splitPanel owns width now).
Key additions: `id="split-rq2"` (required by splitPanel's `getElementById`), `min-w-0` on right panel (so parts tables inside can shrink below content width).

- [ ] **Step 2: Deploy the change**

Run: `cd /root/availai && ./deploy.sh --no-commit`

Expected: build succeeds, app restarts, logs show no errors.

- [ ] **Step 3: Run the E2E test and confirm it passes**

Run: `cd /root/availai && npx playwright test e2e/requisitions2-resize.spec.ts --project=workflows --reporter=list`

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add app/templates/requisitions2/page.html
git commit -m "feat(requisitions2): wire splitPanel for resizable left/right divider"
```

---

## Task 3: Add `resizableTable` Alpine component (definition only, no consumers yet)

**Files:**
- Modify: `app/static/htmx_app.js` (add after existing `splitPanel` component ~line 275)

- [ ] **Step 1: Add the component definition**

Open `app/static/htmx_app.js`. Find the existing `splitPanel` component (search for `Alpine.data('splitPanel'`). Immediately after its closing `}));` (around line 274), insert:

```javascript
/**
 * resizableTable — Alpine component for user-resizable table columns.
 * Renders <colgroup> widths, manages drag handles on <th> right borders,
 * persists widths to localStorage, and recovers on HTMX swap.
 *
 * Usage:
 *   <div id="rq2-table" x-data="resizableTable('rq2-list', {name:200, status:110})">
 *     <table class="resizable-cols">
 *       <colgroup>
 *         <col :style="colStyle('name')">
 *         <col :style="colStyle('status')">
 *       </colgroup>
 *       <thead><tr>
 *         <th class="resizable">Name
 *           <span class="col-resize-handle"
 *                 @mousedown="startColResize($event,'name')"
 *                 @dblclick="autoFitCol('name')"></span>
 *         </th>
 *         <th>Status</th> {# last col, no handle #}
 *       </tr></thead>
 *     </table>
 *   </div>
 *
 * Called by: requisitions2 templates (page.html, _detail_panel.html).
 * Reusable across any data table.
 */
Alpine.data('resizableTable', (tableKey, defaults) => ({
    widths: {},
    _resizing: null,
    _storageKey: 'avail_table_cols_' + tableKey,
    _defaults: defaults,

    init() {
        const saved = JSON.parse(localStorage.getItem(this._storageKey) || '{}');
        this.widths = { ...this._defaults, ...saved };
        // HTMX swaps inside this element replace <colgroup>; re-apply widths afterwards
        this.$el.addEventListener('htmx:afterSwap', () => {
            this.widths = { ...this.widths };
        });
    },

    colStyle(key) {
        const w = this.widths[key];
        return w ? `width:${w}px;min-width:${w}px` : '';
    },

    startColResize(e, key) {
        e.preventDefault();
        e.stopPropagation();
        const th = e.target.closest('th');
        const startWidth = this.widths[key] || (th ? th.offsetWidth : 100);
        this._resizing = { key, startX: e.clientX, startWidth };
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';

        const onMove = (ev) => {
            if (!this._resizing) return;
            const dx = ev.clientX - this._resizing.startX;
            this.widths[this._resizing.key] = Math.max(40, this._resizing.startWidth + dx);
        };
        const onUp = () => {
            this._resizing = null;
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            localStorage.setItem(this._storageKey, JSON.stringify(this.widths));
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
        };
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    },

    autoFitCol(key) {
        delete this.widths[key];
        this.widths = { ...this._defaults, ...this.widths };
        localStorage.setItem(this._storageKey, JSON.stringify(this.widths));
    },

    resetAll() {
        this.widths = { ...this._defaults };
        localStorage.removeItem(this._storageKey);
    },
}));
```

- [ ] **Step 2: Rebuild assets and deploy**

Run: `cd /root/availai && ./deploy.sh --no-commit`

Expected: build succeeds. No consumers yet so no visible change.

- [ ] **Step 3: Verify component is registered**

Run: `cd /root/availai && npx playwright test e2e/requisitions2-resize.spec.ts --project=workflows --reporter=list -g "split"` (split test still passes, regression check).

Then manually verify in a headed session or via evaluate:
```bash
npx playwright test --headed --project=workflows e2e/requisitions2-resize.spec.ts -g "split"
```
(optional smoke check — not required for this task)

Expected: existing split test still passes.

- [ ] **Step 4: Commit**

```bash
git add app/static/htmx_app.js
git commit -m "feat(ui): add reusable resizableTable Alpine component"
```

---

## Task 4: Add CSS for resizable tables

**Files:**
- Modify: `app/static/styles.css` (append at end)

- [ ] **Step 1: Append CSS rules**

Append to the bottom of `app/static/styles.css`:

```css
/* Resizable table columns — consumed by Alpine resizableTable component */
table.resizable-cols { table-layout: fixed; }
th.resizable { position: relative; }
.col-resize-handle {
    position: absolute;
    top: 0;
    right: 0;
    width: 6px;
    height: 100%;
    cursor: col-resize;
    user-select: none;
    background: transparent;
    transition: background 0.15s;
    z-index: 1;
}
.col-resize-handle:hover,
.col-resize-handle:active {
    background: rgb(99 146 204 / 0.4);
}
```

- [ ] **Step 2: Deploy**

Run: `cd /root/availai && ./deploy.sh --no-commit`

Expected: build succeeds.

- [ ] **Step 3: Commit**

```bash
git add app/static/styles.css
git commit -m "feat(ui): add CSS for resizable-cols tables and drag handles"
```

---

## Task 5: E2E test for left-list column resize

**Files:**
- Modify: `e2e/requisitions2-resize.spec.ts` (append new test)

- [ ] **Step 1: Append the failing column-resize test**

Append this `test.describe` block to `e2e/requisitions2-resize.spec.ts`:

```typescript
test.describe('Requisitions left-list columns', () => {
  test('Name column resize persists to localStorage and survives reload', async ({ page }) => {
    await clearLayout(page);
    await page.goto(REQS_URL);

    // Wait for the list table to render at least one header
    const nameHeader = page.locator('#rq2-table th').filter({ hasText: 'Name' });
    await expect(nameHeader).toBeVisible({ timeout: 10000 });

    // Grab the resize handle inside the Name header
    const handle = nameHeader.locator('.col-resize-handle');
    await expect(handle).toBeVisible();

    const hBox = await handle.boundingBox();
    if (!hBox) throw new Error('handle not visible');

    // Drag 80px right
    await page.mouse.move(hBox.x + hBox.width / 2, hBox.y + hBox.height / 2);
    await page.mouse.down();
    await page.mouse.move(hBox.x + 80, hBox.y + hBox.height / 2, { steps: 10 });
    await page.mouse.up();

    // Saved widths contain name and it's larger than default 200
    const saved = await page.evaluate(() =>
      JSON.parse(localStorage.getItem('avail_table_cols_rq2-list') || '{}')
    );
    expect(saved.name).toBeGreaterThan(200);

    // Reload — assert same width re-applied to <col>
    const savedName = saved.name;
    await page.reload();
    const col = page.locator('#rq2-table colgroup col').nth(1); // 0=select, 1=name
    const style = await col.getAttribute('style');
    expect(style).toContain(`width:${savedName}px`);
  });

  test('columns survive HTMX swap (sort reorder)', async ({ page }) => {
    await clearLayout(page);
    await page.goto(REQS_URL);

    // Set a custom width directly via localStorage, then reload
    await page.evaluate(() => {
      localStorage.setItem('avail_table_cols_rq2-list',
        JSON.stringify({ name: 260, status: 110, customer: 160, select: 36, count: 60 }));
    });
    await page.reload();

    // Click Name header link to re-sort (triggers HTMX swap of #rq2-table)
    const nameLink = page.locator('#rq2-table thead a', { hasText: 'Name' });
    if (await nameLink.count() > 0) {
      await nameLink.first().click();
      // Wait for HTMX swap to settle
      await page.waitForTimeout(400);
      const col = page.locator('#rq2-table colgroup col').nth(1);
      const style = await col.getAttribute('style');
      expect(style).toContain('width:260px');
    }
  });
});
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `cd /root/availai && npx playwright test e2e/requisitions2-resize.spec.ts --project=workflows --reporter=list -g "left-list"`

Expected: FAIL — `.col-resize-handle` not found (no handles rendered in templates yet).

- [ ] **Step 3: Commit the failing test**

```bash
git add e2e/requisitions2-resize.spec.ts
git commit -m "test(requisitions2): failing E2E for column resize + swap survival"
```

---

## Task 6: Wire `resizableTable` on `#rq2-table` in `page.html` + update `_table.html`

**Files:**
- Modify: `app/templates/requisitions2/page.html` (add `x-data` to `#rq2-table`)
- Modify: `app/templates/requisitions2/_table.html` (colgroup, handles, classes)

- [ ] **Step 1: Add `x-data` to `#rq2-table` in `page.html`**

Edit the `#rq2-table` div in `page.html` (the `<div id="rq2-table"...>` you placed in Task 2):

```html
<div id="rq2-table"
     class="flex-1 overflow-y-auto"
     x-data="resizableTable('rq2-list', {select:36, name:200, status:110, customer:160, count:60})">
  {% include "requisitions2/_table.html" %}
</div>
```

Rationale (per spec): the swap target is `#rq2-table` with `hx-swap="innerHTML"`, so `x-data` must live on the target itself to persist across swaps.

- [ ] **Step 2: Rewrite the table header region in `_table.html`**

Replace the block from line 8 (`{% if requisitions %}`) through line 44 (`</table></div>`) with:

```html
{% if requisitions %}
<div class="overflow-x-auto">
  <table class="resizable-cols min-w-full divide-y divide-gray-200 text-sm">
    <colgroup>
      <col :style="colStyle('select')">
      <col :style="colStyle('name')">
      <col :style="colStyle('status')">
      <col :style="colStyle('customer')">
      <col :style="colStyle('count')">
    </colgroup>
    <thead class="bg-gray-50">
      <tr>
        <th class="resizable px-3 py-2">
          <input aria-label="Select all requisitions" type="checkbox"
                 class="rounded border-gray-300 text-brand-500 focus:ring-brand-400"
                 x-on:change="toggleAll($event.target.checked, {{ requisitions | map(attribute='id') | list | tojson }})">
          <span class="col-resize-handle"
                @mousedown="startColResize($event,'select')"
                @dblclick="autoFitCol('select')"></span>
        </th>
        {% set columns = [
          ('name', 'Name'),
          ('status', 'Status'),
          ('customer_name', 'Customer'),
        ] %}
        {% set col_keys = ['name', 'status', 'customer'] %}
        {% for col_key, col_label in columns %}
        <th class="resizable px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
          <a hx-get="/requisitions2/table?sort={{ col_key }}&order={{ 'asc' if filters.sort.value == col_key and filters.order.value == 'desc' else 'desc' }}"
             hx-target="#rq2-table"
             hx-include="#rq2-filters"
             hx-push-url="/requisitions2"
             class="inline-flex items-center gap-1 hover:text-brand-600 cursor-pointer transition-colors">
            {{ col_label }}
            {% if filters.sort.value == col_key %}
              <span class="text-brand-500">{{ '▲' if filters.order.value == 'asc' else '▼' }}</span>
            {% endif %}
          </a>
          <span class="col-resize-handle"
                @mousedown="startColResize($event,'{{ col_keys[loop.index0] }}')"
                @dblclick="autoFitCol('{{ col_keys[loop.index0] }}')"></span>
        </th>
        {% endfor %}
        <th class="px-3 py-2 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">#</th>
      </tr>
    </thead>
    <tbody id="rq2-rows" class="divide-y divide-gray-100">
      {% include "requisitions2/_table_rows.html" %}
    </tbody>
  </table>
</div>
{% else %}
```

Key changes:
- Added `<colgroup>` with 5 `<col :style="colStyle(...)">` matching the 5 header cells.
- Added `class="resizable-cols"` to `<table>` (enables `table-layout: fixed`).
- Added `class="resizable"` and handle span to every `<th>` except the last `#` column.
- `col_keys` helper lets the loop emit the correct stored key per column.

Keep the else branch (`{% else %}` through the end of the `_table.html` file) as-is.

- [ ] **Step 3: Deploy and run E2E**

Run: `cd /root/availai && ./deploy.sh --no-commit`

Then: `cd /root/availai && npx playwright test e2e/requisitions2-resize.spec.ts --project=workflows --reporter=list`

Expected: all three tests pass (split, column resize, swap survival).

- [ ] **Step 4: Commit**

```bash
git add app/templates/requisitions2/page.html app/templates/requisitions2/_table.html
git commit -m "feat(requisitions2): enable resizable columns on left list table"
```

---

## Task 7: Remove hard `max-w-*` caps in `_table_rows.html`

**Files:**
- Modify: `app/templates/requisitions2/_table_rows.html:25-39`

- [ ] **Step 1: Remove max-width caps; keep truncate via column width**

Edit the Name cell (line 25-34) and Customer cell (line 38) in `_table_rows.html`. Replace:

```html
  <td class="px-3 py-2">
    <div class="flex items-center gap-1.5">
      <span class="text-sm font-medium text-gray-900 truncate max-w-[180px]">{{ req.name }}</span>
      {% if req.urgency == 'hot' %}
      <span class="inline-flex h-4 px-1 text-[10px] font-semibold rounded bg-amber-50 text-amber-700">HOT</span>
      {% elif req.urgency == 'critical' %}
      <span class="inline-flex h-4 px-1 text-[10px] font-semibold rounded bg-rose-50 text-rose-700">CRIT</span>
      {% endif %}
    </div>
  </td>
```

with:

```html
  <td class="px-3 py-2 overflow-hidden">
    <div class="flex items-center gap-1.5 min-w-0">
      <span class="text-sm font-medium text-gray-900 truncate block flex-1 min-w-0">{{ req.name }}</span>
      {% if req.urgency == 'hot' %}
      <span class="flex-shrink-0 inline-flex h-4 px-1 text-[10px] font-semibold rounded bg-amber-50 text-amber-700">HOT</span>
      {% elif req.urgency == 'critical' %}
      <span class="flex-shrink-0 inline-flex h-4 px-1 text-[10px] font-semibold rounded bg-rose-50 text-rose-700">CRIT</span>
      {% endif %}
    </div>
  </td>
```

And replace the Customer cell (line 38):

```html
  <td class="px-3 py-2 text-sm text-gray-600 truncate max-w-[140px]">{{ req.customer_display or '—' }}</td>
```

with:

```html
  <td class="px-3 py-2 text-sm text-gray-600 overflow-hidden">
    <span class="truncate block">{{ req.customer_display or '—' }}</span>
  </td>
```

Why `overflow-hidden` on `<td>` + `truncate` on inner `<span>`: with `table-layout: fixed`, the `<td>` needs `overflow-hidden` for the colgroup width to actually constrain its children; the inner `<span class="truncate">` handles the ellipsis.

- [ ] **Step 2: Deploy and visually verify**

Run: `cd /root/availai && ./deploy.sh --no-commit`

Run regression: `cd /root/availai && npx playwright test e2e/requisitions2-resize.spec.ts --project=workflows --reporter=list`

Expected: all tests still pass. Manual check via a quick evaluate: load `/requisitions2`, confirm long names still truncate but now respect column width rather than the old 180px cap.

- [ ] **Step 3: Commit**

```bash
git add app/templates/requisitions2/_table_rows.html
git commit -m "feat(requisitions2): drop hard max-w caps; column widths control truncation"
```

---

## Task 8: E2E test for truncation tooltip

**Files:**
- Modify: `e2e/requisitions2-resize.spec.ts` (append)

- [ ] **Step 1: Append failing tooltip test**

Append to `e2e/requisitions2-resize.spec.ts`:

```typescript
test.describe('Truncation tooltips', () => {
  test('tooltip appears on hover over truncated cell and disappears on leave', async ({ page }) => {
    await clearLayout(page);
    // Force the Name column narrow so even short names may truncate
    await page.goto(REQS_URL);
    await page.evaluate(() => {
      localStorage.setItem('avail_table_cols_rq2-list',
        JSON.stringify({ name: 80, status: 110, customer: 160, select: 36, count: 60 }));
    });
    await page.reload();

    // Find any row's Name cell
    const nameCell = page.locator('#rq2-rows tr').first().locator('td').nth(1).locator('span').first();
    await expect(nameCell).toBeVisible({ timeout: 10000 });

    // Hover — tooltip should appear IF the text is actually truncated
    await nameCell.hover();
    await page.waitForTimeout(200);

    const tip = page.locator('.truncate-tip.visible');
    const nameText = (await nameCell.textContent())?.trim() || '';
    const scrollOverflow = await nameCell.evaluate(
      (el: HTMLElement) => el.scrollWidth > el.clientWidth
    );

    if (scrollOverflow) {
      await expect(tip).toBeVisible();
      await expect(tip).toHaveText(nameText);
    } else {
      await expect(tip).toHaveCount(0);
    }

    // Move away — tooltip should vanish
    await page.mouse.move(0, 0);
    await page.waitForTimeout(150);
    await expect(page.locator('.truncate-tip.visible')).toHaveCount(0);
  });
});
```

- [ ] **Step 2: Run and confirm failure**

Run: `cd /root/availai && npx playwright test e2e/requisitions2-resize.spec.ts --project=workflows --reporter=list -g "Truncation"`

Expected: FAIL — directive `x-truncate-tip` not defined, no tooltip ever appears.

- [ ] **Step 3: Commit failing test**

```bash
git add e2e/requisitions2-resize.spec.ts
git commit -m "test(requisitions2): failing E2E for truncation tooltip"
```

---

## Task 9: Add `x-truncate-tip` directive + tooltip CSS

**Files:**
- Modify: `app/static/htmx_app.js` (register directive before `Alpine.start()`)
- Modify: `app/static/styles.css` (append tooltip rules)

- [ ] **Step 1: Add directive to `htmx_app.js`**

Find `Alpine.start()` in `app/static/htmx_app.js` (usually near the bottom of the file). Immediately BEFORE that line, insert:

```javascript
/**
 * x-truncate-tip — Show a tooltip with full text when the element is visually truncated.
 * Detects real overflow (scrollWidth > clientWidth) on hover; no tooltip when text fits.
 * Usage: <span class="truncate" x-truncate-tip>{{ long_value }}</span>
 */
Alpine.directive('truncate-tip', (el) => {
    let tip = null;

    const show = () => {
        if (el.scrollWidth <= el.clientWidth) return;
        const text = el.textContent.trim();
        if (!text) return;

        tip = document.createElement('div');
        tip.className = 'truncate-tip';
        tip.textContent = text;
        document.body.appendChild(tip);

        const r = el.getBoundingClientRect();
        // Measure before making visible
        const tr = tip.getBoundingClientRect();
        let top = r.top - tr.height - 6;
        if (top < 4) top = r.bottom + 6;
        let left = r.left + (r.width - tr.width) / 2;
        left = Math.max(4, Math.min(left, window.innerWidth - tr.width - 4));
        tip.style.top = top + 'px';
        tip.style.left = left + 'px';
        requestAnimationFrame(() => tip && tip.classList.add('visible'));
    };

    const hide = () => {
        if (tip) { tip.remove(); tip = null; }
    };

    el.addEventListener('mouseenter', show);
    el.addEventListener('mouseleave', hide);
    el.addEventListener('focusout', hide);
});
```

- [ ] **Step 2: Append tooltip CSS to `styles.css`**

Append to `app/static/styles.css`:

```css
/* Truncation tooltip — appended to document.body by x-truncate-tip directive */
.truncate-tip {
    position: fixed;
    z-index: 9999;
    max-width: 420px;
    padding: 0.4rem 0.6rem;
    font-size: 0.8rem;
    line-height: 1.3;
    color: #fff;
    background: rgba(17, 24, 39, 0.95);
    border-radius: 0.375rem;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
    pointer-events: none;
    white-space: normal;
    word-wrap: break-word;
    opacity: 0;
    transition: opacity 0.12s ease-out;
}
.truncate-tip.visible {
    opacity: 1;
}
```

- [ ] **Step 3: Deploy**

Run: `cd /root/availai && ./deploy.sh --no-commit`

Expected: build succeeds.

- [ ] **Step 4: Commit**

```bash
git add app/static/htmx_app.js app/static/styles.css
git commit -m "feat(ui): add x-truncate-tip directive and tooltip styles"
```

---

## Task 10: Apply `x-truncate-tip` on Name and Customer cells

**Files:**
- Modify: `app/templates/requisitions2/_table_rows.html`

- [ ] **Step 1: Add the directive to the two truncated cells**

Edit `app/templates/requisitions2/_table_rows.html`. In the Name cell `<span>` (from Task 7 output):

```html
<span class="text-sm font-medium text-gray-900 truncate block flex-1 min-w-0"
      x-truncate-tip>{{ req.name }}</span>
```

In the Customer cell inner `<span>` (from Task 7):

```html
<span class="truncate block" x-truncate-tip>{{ req.customer_display or '—' }}</span>
```

- [ ] **Step 2: Deploy and run tooltip E2E**

Run: `cd /root/availai && ./deploy.sh --no-commit`

Run: `cd /root/availai && npx playwright test e2e/requisitions2-resize.spec.ts --project=workflows --reporter=list`

Expected: all tests pass — split, column resize, swap survival, truncation tooltip.

- [ ] **Step 3: Commit**

```bash
git add app/templates/requisitions2/_table_rows.html
git commit -m "feat(requisitions2): show tooltip on truncated Name and Customer cells"
```

---

## Task 11: Wire `resizableTable` + tooltips on the detail panel parts table

**Files:**
- Modify: `app/templates/requisitions2/_detail_panel.html`

- [ ] **Step 1: Add tooltip to header title and customer line**

Edit the header section (lines 25-27). Replace:

```html
<h2 class="text-base font-semibold text-gray-900 truncate">{{ req.name }}</h2>
<p class="text-xs text-gray-500 mt-0.5">{{ req.customer_display or 'No customer' }}</p>
```

with:

```html
<h2 class="text-base font-semibold text-gray-900 truncate" x-truncate-tip>{{ req.name }}</h2>
<p class="text-xs text-gray-500 mt-0.5 truncate" x-truncate-tip>{{ req.customer_display or 'No customer' }}</p>
```

- [ ] **Step 2a: Wrap parts table in `resizableTable` and add colgroup + handles**

Replace the parts-table block in `_detail_panel.html` (the `{% if requirements %}` ... `{% endif %}` block around lines 138-179) with:

```html
{% if requirements %}
<div class="overflow-x-auto"
     x-data="resizableTable('rq2-parts', {mpn:180, qty:90, price:110, status:110, actions:90})">
  <table class="resizable-cols min-w-full divide-y divide-gray-200 text-sm">
    <colgroup>
      <col :style="colStyle('mpn')">
      <col :style="colStyle('qty')">
      <col :style="colStyle('price')">
      <col :style="colStyle('status')">
      <col :style="colStyle('actions')">
    </colgroup>
    <thead class="bg-gray-50">
      <tr>
        <th class="resizable px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">
          MPN
          <span class="col-resize-handle"
                @mousedown="startColResize($event,'mpn')"
                @dblclick="autoFitCol('mpn')"></span>
        </th>
        <th class="resizable px-4 py-2 text-right text-xs font-medium text-gray-500 uppercase">
          Qty
          <span class="col-resize-handle"
                @mousedown="startColResize($event,'qty')"
                @dblclick="autoFitCol('qty')"></span>
        </th>
        <th class="resizable px-4 py-2 text-right text-xs font-medium text-gray-500 uppercase">
          Target $
          <span class="col-resize-handle"
                @mousedown="startColResize($event,'price')"
                @dblclick="autoFitCol('price')"></span>
        </th>
        <th class="resizable px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">
          Status
          <span class="col-resize-handle"
                @mousedown="startColResize($event,'status')"
                @dblclick="autoFitCol('status')"></span>
        </th>
        <th class="px-4 py-2 text-right text-xs font-medium text-gray-500 uppercase"></th>
      </tr>
    </thead>
    <tbody class="divide-y divide-gray-100">
      {% for r in requirements %}
      <tr class="hover:bg-gray-50">
        <td class="px-4 py-2 font-mono text-gray-800 text-xs overflow-hidden">
          <span class="truncate block" x-truncate-tip>{{ r.primary_mpn }}</span>
        </td>
        <td class="px-4 py-2 text-right text-gray-600">{{ '{:,}'.format(r.target_qty) if r.target_qty else '—' }}</td>
        <td class="px-4 py-2 text-right text-gray-600">{{ '${:,.2f}'.format(r.target_price) if r.target_price else '—' }}</td>
        <td class="px-4 py-2">
          <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full bg-gray-100 text-gray-600">
            {{ r.sourcing_status or 'open' }}
          </span>
        </td>
        <td class="px-4 py-2 text-right">
          <a href="/v2/search?q={{ r.primary_mpn }}"
             class="inline-flex items-center gap-1 text-xs font-medium text-brand-500 hover:text-brand-600">
            <svg class="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
              <path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/>
            </svg>
            Search
          </a>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% else %}
<div class="px-5 py-8 text-center text-sm text-gray-400">
  No parts added to this requisition yet.
</div>
{% endif %}
```

- [ ] **Step 2b: Deploy**

Run: `cd /root/availai && ./deploy.sh --no-commit`

- [ ] **Step 3: Regression E2E**

Run: `cd /root/availai && npx playwright test e2e/requisitions2-resize.spec.ts --project=workflows --reporter=list`

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add app/templates/requisitions2/_detail_panel.html
git commit -m "feat(requisitions2): resizable columns + tooltips on detail parts table"
```

---

## Task 12: Add "Reset columns" affordance to left list header

**Files:**
- Modify: `app/templates/requisitions2/_table.html`

- [ ] **Step 1: Add overflow menu to the last header cell**

In `_table.html`, find the last header cell:

```html
<th class="px-3 py-2 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">#</th>
```

Replace with:

```html
<th class="px-3 py-2 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">
  <div class="flex items-center justify-end gap-1">
    <span>#</span>
    <div class="relative" x-data="{ open: false }">
      <button @click.stop="open = !open" @click.outside="open = false"
              aria-label="Column options"
              class="inline-flex h-5 w-5 items-center justify-center rounded hover:bg-gray-200 transition-colors">
        <svg class="h-3 w-3 text-gray-400" fill="currentColor" viewBox="0 0 20 20">
          <path d="M10 6a2 2 0 110-4 2 2 0 010 4zM10 12a2 2 0 110-4 2 2 0 010 4zM10 18a2 2 0 110-4 2 2 0 010 4z"/>
        </svg>
      </button>
      <div x-show="open" x-cloak x-transition
           class="absolute right-0 top-full mt-1 w-40 rounded-lg border border-gray-200 bg-white py-1 shadow-lg z-20">
        <button @click="resetAll(); open = false"
                class="flex w-full items-center gap-2 px-3 py-2 text-xs text-left text-gray-700 hover:bg-gray-50">
          Reset columns
        </button>
      </div>
    </div>
  </div>
</th>
```

Note: the button calls `resetAll()` which is available from the parent `#rq2-table` Alpine scope (resizableTable).

- [ ] **Step 2: E2E test for reset**

Append to `e2e/requisitions2-resize.spec.ts`:

```typescript
test('Reset columns button clears saved widths', async ({ page }) => {
  await clearLayout(page);
  await page.goto(REQS_URL);
  await page.evaluate(() =>
    localStorage.setItem('avail_table_cols_rq2-list',
      JSON.stringify({ name: 300, status: 110, customer: 160, select: 36, count: 60 }))
  );
  await page.reload();

  // Open menu and click Reset
  await page.locator('[aria-label="Column options"]').click();
  await page.locator('button', { hasText: 'Reset columns' }).click();

  const saved = await page.evaluate(() =>
    localStorage.getItem('avail_table_cols_rq2-list')
  );
  expect(saved).toBeNull();
});
```

- [ ] **Step 3: Deploy and run**

Run: `cd /root/availai && ./deploy.sh --no-commit && npx playwright test e2e/requisitions2-resize.spec.ts --project=workflows --reporter=list`

Expected: all tests pass including Reset.

- [ ] **Step 4: Commit**

```bash
git add app/templates/requisitions2/_table.html e2e/requisitions2-resize.spec.ts
git commit -m "feat(requisitions2): add 'Reset columns' menu in left list header"
```

---

## Task 13: Update `APP_MAP_INTERACTIONS.md`

**Files:**
- Modify: `docs/APP_MAP_INTERACTIONS.md`

- [ ] **Step 1: Locate the Alpine components section**

Run: `cd /root/availai && grep -n "splitPanel\|Alpine component\|## " docs/APP_MAP_INTERACTIONS.md | head -40`

Expected: identifies the section documenting Alpine components / UI primitives.

- [ ] **Step 2: Add documentation for the new pieces**

In the appropriate section of `docs/APP_MAP_INTERACTIONS.md` (near existing Alpine component docs — adapt to whatever structure that file uses), append:

```markdown
### resizableTable (app/static/htmx_app.js)

Reusable Alpine component for user-resizable table columns. Persists column widths per-user to `localStorage` under `avail_table_cols_<tableKey>`. Survives HTMX swaps via `htmx:afterSwap` listener.

**Usage:**
- Root: `x-data="resizableTable('<tableKey>', {colKey: defaultPx, ...})"` on element OUTSIDE the HTMX swap target (or directly on the swap target, so it persists across swaps)
- Table: `<table class="resizable-cols">` (enables `table-layout: fixed`)
- Columns: `<colgroup><col :style="colStyle('colKey')">...</colgroup>`
- Handles: `<th class="resizable">...<span class="col-resize-handle" @mousedown="startColResize($event,'colKey')" @dblclick="autoFitCol('colKey')"></span></th>` (skip on last column)
- Reset: call `resetAll()` from any descendant

**Used by:** `/requisitions2` (left list table `rq2-list`, detail parts table `rq2-parts`).

### x-truncate-tip directive (app/static/htmx_app.js)

Alpine directive that shows a DOM-appended tooltip only when the target element's text is visually clipped (`scrollWidth > clientWidth`). Positions with fixed coords; flips above/below based on viewport room.

**Usage:** `<span class="truncate" x-truncate-tip>{{ potentially_long_text }}</span>`

**Used by:** `/requisitions2` (Name + Customer cells in list, header + MPN cells in detail).
```

- [ ] **Step 3: Commit**

```bash
git add docs/APP_MAP_INTERACTIONS.md
git commit -m "docs(app-map): document resizableTable component and x-truncate-tip directive"
```

---

## Task 14: Full regression + deploy

**Files:** none (verification only)

- [ ] **Step 1: Run the full E2E suite for this feature**

Run: `cd /root/availai && npx playwright test e2e/requisitions2-resize.spec.ts --project=workflows --reporter=list`

Expected: ALL tests pass (split, column resize, swap survival, tooltip show/hide, reset).

- [ ] **Step 2: Run full backend test suite for regression**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short -q`

Expected: all tests pass (change is frontend-only so this should be a no-op safety check).

- [ ] **Step 3: Run lint**

Run: `cd /root/availai && ruff check app/ && npm run lint`

Expected: no new issues.

- [ ] **Step 4: Final deploy (with commit)**

Run: `cd /root/availai && ./deploy.sh`

Expected: commit includes any remaining changes, push succeeds, app rebuilds, logs show no errors.

- [ ] **Step 5: Manual smoke in a real session**

Open `/requisitions2`:
1. Drag the split divider — left panel resizes, ratio persists across reloads
2. Hover a long Name — tooltip appears with full text
3. Hover a short Name — no tooltip
4. Drag the Name column handle — width grows, persists across reload
5. Click a requisition — right panel loads, parts table columns are resizable
6. Double-click a column handle — width resets to default
7. Click ⋯ menu → Reset columns — all widths reset
8. Trigger SSE refresh (wait, or force via another tab) — column widths survive
9. Keyboard focus the divider, use arrows — divider moves, ratio persists

Expected: every step behaves as described.

---

## Self-Review Notes

**Spec coverage:**
- Goal 1 (no cutoff): Tasks 7, 10, 11 (remove hard caps + tooltips).
- Goal 2 (column resize): Tasks 3, 6, 11.
- Goal 3 (split resize): Tasks 1, 2.
- Goal 4 (tooltips): Tasks 8, 9, 10, 11.
- Goal 5 (persistence): built into components; E2E covers in Tasks 1, 5, 8, 12.
- Goal 6 (HTMX swap survival): Task 5 second test; Task 3 component logic.
- Goal 7 (reusable): component API in Task 3; documentation in Task 13.

**No placeholders:** every code block is complete and compilable. No "TBD" or "similar to above" references.

**Type consistency:** component method names (`colStyle`, `startColResize`, `autoFitCol`, `resetAll`) used identically across all consumer templates and the E2E test assertions. `localStorage` keys (`avail_split_rq2`, `avail_table_cols_rq2-list`, `avail_table_cols_rq2-parts`) consistent across code, tests, and `clearLayout` helper.

---

## Execution Options

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks.

**2. Inline Execution** — execute tasks in this session using `executing-plans`, batch execution with checkpoints.
