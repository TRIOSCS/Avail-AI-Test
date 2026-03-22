# Unified Requisition Entry Form

**Date:** 2026-03-22
**Status:** Approved

## Context

Currently there are two separate flows for creating requisitions:
1. **Import modal (2-step):** Paste/upload → AI parses → full modal swap to 14-column editable table → save
2. **Direct create modal (1-step):** Manual text list (MPN, Qty per line) → save

Problems:
- The two-step import flow swaps the entire modal content, losing the user's context
- The direct create form only supports MPN + Qty — no manufacturer, no specs
- Manufacturer is missing from the import preview table entirely (data quality gap)
- `customerPicker()` is copy-pasted (~70 lines) across both modals
- Row deletion in the preview table has an index-gap bug (import-save loop stops at gaps)
- No per-row validation feedback

## Scope

1. **Unified modal** replacing both import and create modals
2. **Column-visibility table** replacing accordion rows (Tier 1 + Tier 2 columns)
3. **Inline AI parse** — JSON response, Alpine builds rows (no page swap)
4. **Extract shared components** — customerPicker, deadline widget, close button
5. **Fix the row deletion index bug** via Alpine-managed arrays
6. **Per-row validation** with visual feedback
7. **Deprecate** `import_modal.html`, `import_preview.html`, `create_modal.html`

## Design

### 1. Modal Layout

```
┌─────────────────────────────────────────────────────┐
│ New Requisition                                  [×] │
├─────────────────────────────────────────────────────┤
│ Name* [____________] Customer [__________] Deadline  │
├─────────────────────────────────────────────────────┤
│ [Paste] [Upload]   ← tab toggle                     │
│ ┌─textarea, 3 rows────────────────────────────────┐ │
│ │ Paste parts here...                              │ │
│ └──────────────────────────────────────────────────┘ │
│ [Process with AI ▶]                                  │
├─────────────────────────────────────────────────────┤
│ 12 parts  [Show all columns ▾]          [+ Add row] │
│ ┌────────────────────────────────────────────────┐  │
│ │ MPN         │ Mfr        │ Qty │ Cond │  $  │×│  │
│ │ LM358DR     │ TI         │ 500 │ NEW  │  —  │×│  │
│ │ STM32F407   │ ST         │ 100 │ NEW  │  —  │×│  │
│ └────────────────────────────────────────────────┘  │
│ max-h-[45vh] overflow-y-auto                        │
├─────────────────────────────────────────────────────┤
│             [Cancel]  [Create Requisition (12 parts)]│
└─────────────────────────────────────────────────────┘
```

**Top zone** — Req metadata (name, customer, deadline) pinned at top.

**Input zone** — Paste textarea (3 rows, compact) + file upload tab toggle. After AI processing, textarea replaced with status strip: `"12 parts parsed from paste • [Re-parse ↺]"`. Paste content preserved in Alpine state for re-processing.

**Parts zone** — Column-visibility table with scrollable body (`max-h-[45vh]`). Rows managed by Alpine `x-for` over `parts[]` array.

**Footer** — Cancel + Create button pinned at bottom. Create button shows count and validation state.

### 2. Column Visibility (Two Tiers)

**Tier 1 — always visible (no horizontal scroll):**

| Column | Width | Type | Notes |
|--------|-------|------|-------|
| MPN | flex | text input | Required, monospace |
| Manufacturer | flex | text input + typeahead | Required |
| Qty | 60px | number input | Default 1 |
| Condition | 80px | select | NEW / REFURB / USED badge |
| Target $ | 70px | number input | Optional |
| × | 24px | delete button | Removes row |

**Tier 2 — toggled via "Show all columns" button (adds columns to the right):**

| Column | Type | Notes |
|--------|------|-------|
| Brand | text input | Optional (dual-label) |
| Customer PN | text input | Customer's internal part # |
| Date Codes | text input | e.g. "2024+" |
| Packaging | select | tape & reel / tray / tube / bulk |
| Firmware | text input | Version string |
| Hardware | text input | Revision codes |
| Need By | date input | Line-item deadline |
| Sales Notes | text input | Short notes (not textarea) |

Substitutes are NOT a column — they're handled via the structured sub input (per-row, shown when Tier 2 is visible, below each row or as a sub-section).

**"Show all columns" toggle** is a single button above the table. When active, all Tier 2 columns appear. The table gets horizontal scroll if needed. Toggle state persists in Alpine (not localStorage — modal-scoped).

### 3. AI Parse Flow (Inline, JSON)

**Backend change:** `import-parse` route returns JSON instead of HTML.

```python
@router.post("/v2/partials/requisitions/import-parse")
async def requisition_import_parse(...):
    # ... existing parse logic ...
    return JSONResponse({
        "requirements": requirements,  # list of dicts
        "inferred_name": name,
        "inferred_customer": customer_name,
    })
```

**Frontend:** Alpine.js calls via `fetch()` with `FormData` (supports file upload):

```javascript
async parseWithAI() {
    this.parsing = true;
    this.parseError = null;
    if (this.parsing) return; // Guard against double-click
    this.parsing = true;
    this.parseError = null;
    const fd = new FormData();
    if (this.inputMode === 'paste') {
        fd.append('raw_text', this.rawText);
    } else {
        const fileInput = this.$refs.fileInput;
        if (fileInput && fileInput.files[0]) fd.append('file', fileInput.files[0]);
    }
    fd.append('name', this.reqName);
    fd.append('customer_name', this.customerName);
    try {
        const resp = await fetch('/v2/partials/requisitions/import-parse?format=json', {
            method: 'POST', body: fd, credentials: 'same-origin'
        });
        const data = await resp.json();
        if (data.error) {
            this.parseError = data.error;
        } else {
            // Assign stable IDs + map parser field names
            data.requirements.forEach(r => {
                r._id = crypto.randomUUID();
                if (r.notes && !r.sale_notes) r.sale_notes = r.notes;
            });
            this.parts.push(...data.requirements);
            if (data.inferred_name && !this.reqName) this.reqName = data.inferred_name;
            // inferred_customer only pre-fills the search input, not the selection
            this.parsed = true;
        }
    } catch (e) {
        this.parseError = 'Server error — try again.';
    } finally {
        this.parsing = false;
    }
}
```

**Post-parse UI:** Textarea replaced with status strip. Focus moves to first MPN cell via `$nextTick`.

**Re-parse behavior:** The "Re-parse" link in the status strip calls `resetParse()` which sets `parsed = false` (showing the textarea again with content preserved) but does NOT clear `parts[]`. User can edit the paste text and re-process — new results **replace** existing parts (not append). The `parseWithAI()` method clears `parts` before pushing when re-parsing: `if (this.parsed) this.parts = [];`.

**Error handling — three tiers:**
1. HTTP error/timeout → `parseError` shown inline, paste content preserved
2. Zero parts extracted → `parseError` with suggestion to check format
3. Partial garbage (some rows with empty MPN) → rows pushed but invalid ones get red border

### 4. Form Submission

**Hidden inputs synced by Alpine.** The `import-save` backend contract stays unchanged — it reads `reqs[N].field` indexed form fields.

The editable table rows use stable UUIDs as keys (`:key="part._id"`) to prevent Alpine DOM re-use bugs on deletion. The hidden inputs use index-based names for the backend contract:

```html
<!-- Editable table rows — stable key -->
<template x-for="(part, i) in parts" :key="part._id">
  <tr><!-- visible inputs bound to part.mpn, part.manufacturer, etc. --></tr>
</template>

<!-- Hidden inputs for form submission — index-based names -->
<template x-for="(part, i) in parts" :key="'hidden-' + i">
  <div>
    <input type="hidden" :name="`reqs[${i}].primary_mpn`" :value="part.mpn">
    <input type="hidden" :name="`reqs[${i}].manufacturer`" :value="part.manufacturer">
    <input type="hidden" :name="`reqs[${i}].target_qty`" :value="part.qty || 1">
    <input type="hidden" :name="`reqs[${i}].brand`" :value="part.brand || ''">
    <input type="hidden" :name="`reqs[${i}].condition`" :value="part.condition || ''">
    <input type="hidden" :name="`reqs[${i}].target_price`" :value="part.target_price || ''">
    <input type="hidden" :name="`reqs[${i}].customer_pn`" :value="part.customer_pn || ''">
    <input type="hidden" :name="`reqs[${i}].date_codes`" :value="part.date_codes || ''">
    <input type="hidden" :name="`reqs[${i}].packaging`" :value="part.packaging || ''">
    <input type="hidden" :name="`reqs[${i}].firmware`" :value="part.firmware || ''">
    <input type="hidden" :name="`reqs[${i}].hardware_codes`" :value="part.hardware_codes || ''">
    <input type="hidden" :name="`reqs[${i}].need_by_date`" :value="part.need_by_date || ''">
    <input type="hidden" :name="`reqs[${i}].sale_notes`" :value="part.sale_notes || ''">
  </div>
</template>
```

Each parsed requirement from the AI also gets a `_id` assigned: `data.requirements.forEach(r => r._id = crypto.randomUUID())`.
```

This fixes the row deletion index-gap bug because Alpine's `x-for` always renders contiguous indices from 0 to `parts.length - 1`, regardless of which rows were deleted.

Substitutes per row are serialized as comma-separated MPN strings (matching the existing `import-save` contract which splits on commas). The structured `[{mpn, manufacturer}]` format is built server-side by `parse_substitute_mpns()`:
```html
<input type="hidden" :name="`reqs[${i}].substitutes`" :value="(part.substitutes || []).map(s => s.mpn).join(', ')">
```
Sub manufacturers are passed as a parallel hidden field:
```html
<input type="hidden" :name="`reqs[${i}].sub_manufacturers`" :value="(part.substitutes || []).map(s => s.manufacturer).join(', ')">
```
The `import-save` route zips these into structured dicts before calling `parse_substitute_mpns()`.

### 5. State Management

Single root `x-data="unifiedReqModal()"` with nested components:

```javascript
function unifiedReqModal() {
    return {
        // Input zone
        inputMode: 'paste',    // 'paste' | 'upload'
        rawText: '',
        parsing: false,
        parsed: false,
        parseError: null,

        // Parts
        parts: [],             // [{mpn, manufacturer, qty, brand, condition, ...}]
        showAllColumns: false,
        saving: false,

        // Metadata
        reqName: '',
        customerSiteId: '',
        customerName: '',
        deadline: '',
        urgency: 'normal',

        // Methods
        async parseWithAI() { ... },
        addBlankPart() { this.parts.push({_id: crypto.randomUUID(), mpn:'', manufacturer:'', qty:1, condition:'new', sale_notes:'', ...}); },
        removePart(i) { this.parts.splice(i, 1); },
        resetParse() { this.parsed = false; this.parseError = null; },

        get validParts() { return this.parts.filter(p => p.mpn && p.manufacturer); },
        get hasErrors() { return this.parts.some(p => p.mpn && !p.manufacturer); },
    };
}
```

`customerPicker()` remains as a nested Alpine component on the customer field div.

### 6. Validation

**Per-row visual feedback:**
- Rows with empty manufacturer (but non-empty MPN) get a red left border
- Manufacturer input shows red border when empty and MPN is filled
- Validation runs on blur from manufacturer field + on submit attempt

**Submit button:**
- Shows part count: `"Create Requisition (12 parts)"`
- If validation errors: amber state with `"Create Requisition (10 OK · 2 need manufacturer)"`
- Blocked from submitting when any row has MPN but no manufacturer

**On submit with errors:** Auto-scroll to first invalid row, focus the manufacturer input.

### 7. Shared Component Extraction

**`customerPicker()`** — Extract from inline `<script>` to a reusable pattern. Either:
- Move to `htmx_app.js` as a global Alpine component
- Or create `app/templates/htmx/partials/shared/customer_picker.html` Jinja2 partial with included script

**Deadline/ASAP widget** — Extract to Jinja2 macro in `_macros.html`:
```jinja2
{% macro deadline_asap_widget(deadline_value='', urgency_value='normal') %}
  ...
{% endmacro %}
```

**Modal close button** — Already trivial, extract to macro.

### 8. Manual Entry Flow

User clicks "+ Add row" → blank row appended to `parts[]` with all Tier 1 fields visible and editable. If "Show all columns" is active, Tier 2 fields are also visible. The manufacturer typeahead works on each row's manufacturer input.

For structured sub input: when Tier 2 is visible, each row shows a small "Subs" area below the main columns (or as a column) with the existing structured sub-row pattern from `tabs/parts.html`.

### 9. File Upload

Same modal, same parse path. Tab toggle switches between paste textarea and file drop zone. After file is selected + "Process with AI" clicked, the same `parseWithAI()` method fires with the file in FormData. Results populate the same `parts[]` array.

## What This Replaces

| Old | New |
|-----|-----|
| `import_modal.html` | Unified modal template |
| `import_preview.html` | Alpine-rendered rows (delete this file) |
| `create_modal.html` | Unified modal template (delete this file) |
| `import-form` GET route | Returns unified modal |
| `create-form` GET route | Redirects to unified modal or deprecated |
| `import-parse` POST route | Returns JSON instead of HTML |
| `import-save` POST route | Unchanged (hidden inputs match existing contract) |
| `create` POST route | Deprecated (unified modal uses import-save) |

## What Does NOT Change

- `import-save` backend logic — same indexed form field parsing
- `freeform_parser_service.py` — AI parser unchanged
- Manufacturer typeahead endpoint — reused as-is
- `resolve_material_card()` — unchanged
- `parse_substitute_mpns()` — unchanged

## Files Changed

| File | Change |
|------|--------|
| Create: `app/templates/htmx/partials/requisitions/unified_modal.html` | New unified form template |
| Create: `app/templates/htmx/partials/shared/customer_picker.html` | Extracted customer picker partial |
| Modify: `app/templates/htmx/partials/shared/_macros.html` | Add deadline_asap_widget macro |
| Modify: `app/routers/htmx_views.py` | Update import-parse to return JSON, update import-form to return unified modal, deprecate create-form |
| Delete: `app/templates/htmx/partials/requisitions/import_modal.html` | Replaced by unified modal |
| Delete: `app/templates/htmx/partials/requisitions/import_preview.html` | Replaced by Alpine rows |
| Delete: `app/templates/htmx/partials/requisitions/create_modal.html` | Replaced by unified modal |

## Testing

- Unified modal renders with all zones (metadata, input, parts table, footer)
- Paste + Process: AI returns JSON, rows populate inline
- File upload + Process: same flow, rows populate
- Manual "+ Add row": blank row appears with editable fields
- Column visibility toggle: Tier 2 columns show/hide
- Row deletion: removes from Alpine array, indices stay contiguous
- Manufacturer validation: red border on empty, blocked submit
- Submit: hidden inputs match `reqs[N].field` format, import-save creates requisition
- Error handling: parse errors shown inline, paste content preserved
- Customer picker: works from extracted partial (no duplication)
- Deadline/ASAP: works from extracted macro
