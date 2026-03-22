# Quote Builder — Design Spec

## Problem

Users have requisitions with 10-80+ line items (requirements), each with multiple vendor offers. They need to evaluate each line — comparing offers against customer specs, pricing history, and market context — then build a quote with human judgment on every line. The current flow (select offers from Offers tab → "Create Quote") is too flat for this complexity. There's no structured decision workspace.

## Solution

A full-screen two-panel Quote Builder modal launched from the Parts tab. Master-detail layout: left panel is a compact progress tracker of all requirements, right panel is a focused decision workspace for the active line. All data loads upfront. Export to Excel (for customer VLOOKUP) and TRIO PDF quote form.

---

## Architecture

### Stack
- **Frontend**: HTMX + Alpine.js + Tailwind CSS (no React, no new dependencies)
- **Backend**: FastAPI endpoints, openpyxl for Excel export
- **Data**: Existing Quote, QuoteLine, Requirement, Offer models — no migrations needed

### Data Flow
1. User selects requirements on Parts tab → clicks "Build Quote"
2. Backend loads all selected requirements + their offers + pricing history in one query (`joinedload`)
3. Modal opens with data embedded as JSON in `x-data` attribute
4. Right panel is pure Alpine.js reactivity — clicking a row updates `activeIdx`, no server round-trip
5. Save POSTs to existing `POST /api/requisitions/{req_id}/quote` endpoint (no business logic duplication)
6. Excel/PDF export endpoints stream file downloads after save

### Field Mapping Notes
- `Requirement.primary_mpn` is exposed as `mpn` in the builder context (shorter, consistent with quote line items)
- `Offer.parse_confidence` is exposed as `confidence` in the builder context (may be `None` for manually-entered offers — display as "—" when null)
- The existing `QuoteLineItem` schema uses `unit_cost`/`unit_sell`/`margin` but the `create_quote` handler internally builds dicts with `cost_price`/`sell_price`/`margin_pct`. The builder will use its own `QuoteBuilderSaveRequest` schema that maps to the correct field names expected by the handler, or the builder router will call the quote creation logic directly rather than proxying through the schema mismatch.

### New Files

| File | Purpose | Est. Lines |
|---|---|---|
| `app/schemas/quote_builder.py` | Pydantic schemas for builder endpoints | ~60 |
| `app/services/quote_builder_service.py` | Data loading, smart defaults, Excel generation | ~150 |
| `app/routers/quote_builder.py` | 4 endpoints (modal open, save/revise, Excel export, PDF export) | ~200 |
| `app/templates/htmx/partials/quote_builder/modal.html` | Two-panel modal template | ~250 |
| `app/templates/htmx/partials/shared/quote_builder_shell.html` | Full-screen modal wrapper | ~40 |
| `tests/test_quote_builder.py` | Unit + endpoint tests | ~200 |

### Modified Files

| File | Change |
|---|---|
| `app/templates/htmx/base.html` | Add `{% include "htmx/partials/shared/quote_builder_shell.html" %}` |
| `app/templates/htmx/partials/requisitions/tabs/parts.html` | Add "Build Quote" button + row checkboxes |
| `app/static/htmx_app.js` | Add `Alpine.data('quoteBuilder', ...)` component (~200 lines) |
| `app/main.py` | Register `quote_builder` router |
| `app/static/styles.css` | Add quote builder animations (~20 lines) |

---

## API Endpoints

### GET `/v2/partials/quote-builder/{req_id}`
Opens the builder modal. Accepts optional `requirement_ids` query param (comma-separated) for subset selection.

**Returns**: HTMLResponse — renders `quote_builder/modal.html` with JSON-embedded data.

**Context**:
```python
{
    "req": Requisition,
    "lines": [
        {
            "requirement_id": int,
            "mpn": str,
            "manufacturer": str,
            "target_qty": int,
            "target_price": float | None,
            "customer_pn": str | None,
            "date_codes": str | None,
            "condition": str | None,
            "packaging": str | None,
            "firmware": str | None,
            "hardware_codes": str | None,
            "sale_notes": str | None,
            "need_by_date": str | None,
            "offers": [
                {
                    "id": int,
                    "vendor_name": str,
                    "unit_price": float,
                    "qty_available": int,
                    "lead_time": str | None,
                    "date_code": str | None,
                    "condition": str | None,
                    "packaging": str | None,
                    "moq": int | None,
                    "confidence": float | None,  # mapped from Offer.parse_confidence; null for manual offers
                    "notes": str | None,
                }
            ],
            "offer_count": int,
            "status": "decided" | "needs_review" | "no_offers",
            "selected_offer_id": int | None,
            "pricing_history": {
                "avg_price": float,
                "price_range": [float, float],
                "recent": [{"quote_number", "date", "cost", "sell", "margin", "result"}]
            } | None,
        }
    ],
    "customer_name": str,
    "has_customer_site": bool,
}
```

### POST `/v2/partials/quote-builder/{req_id}/save` (new endpoint)
The builder gets its own save endpoint rather than reusing the existing `POST /api/requisitions/{req_id}/quote` directly, because the existing `QuoteLineItem` schema has field name mismatches (`unit_cost`/`unit_sell` vs `cost_price`/`sell_price`). The builder save endpoint accepts the builder's own schema and calls the quote creation logic internally with correct field mappings.

**Save payload** (JSON, from Alpine.js `fetch()`):
```json
{
    "lines": [
        {
            "requirement_id": 1,
            "offer_id": 42,
            "mpn": "LM358DR",
            "manufacturer": "TI",
            "qty": 500,
            "cost_price": 0.2400,
            "sell_price": 0.3100,
            "margin_pct": 22.6,
            "lead_time": "2 weeks",
            "date_code": "2023+",
            "condition": "new",
            "packaging": "tape & reel",
            "moq": 100,
            "material_card_id": 7,
            "notes": "buyer confirmed stock"
        }
    ],
    "payment_terms": "Net 30",
    "shipping_terms": "FCA",
    "validity_days": 7,
    "notes": ""
}
```

**Response** (JSON): `{"ok": true, "quote_id": 123, "quote_number": "Q-0042"}`

**Re-save behavior**: If `quote_id` is already set in Alpine state (i.e., user already saved once), the builder save endpoint handles revision directly rather than delegating to the existing revise endpoint (which copies old line items blindly and accepts no payload). The builder save endpoint will: (1) mark the existing quote as `revised`, (2) create a new Quote with the builder's current payload data, same `quote_number` but incremented `revision`, status `draft`. This avoids the two-step revise-then-update dance and keeps the logic clean in one transaction.

### GET `/v2/partials/quote-builder/{req_id}/export/excel?quote_id={id}`
Generates styled Excel file via openpyxl. Validates quote belongs to requisition.

**Columns**: MPN, Manufacturer, Qty, Unit Price (Sell), Extended Price, Lead Time, Date Codes, Condition, Packaging, MOQ, Vendor.

**Styling**: Brand blue header row (`#3D6895`), white bold font, auto-sized columns, number formatting on price columns.

**Response**: `StreamingResponse` with `Content-Disposition: attachment`.

### GET `/v2/partials/quote-builder/{req_id}/export/pdf?quote_id={id}`
Delegates to existing `generate_quote_report_pdf()` from `document_service.py`.

---

## UI Layout

### Full-Screen Modal Shell

Separate from the existing `max-w-lg` modal. Listens on `@open-quote-builder.window` / `@close-quote-builder.window`. Renders at `z-[60]` (above global modal at `z-50`).

**Toast z-index note**: The global toast is at `z-50`, so it will be hidden behind the builder. The builder uses its own inline notification area (top bar) for save feedback (quote number, success/error messages) rather than relying on the global toast.

**Escape key isolation**: The builder's Escape handler uses `$event.stopPropagation()` to prevent the global modal from also responding. If the global modal is open when the builder opens, it remains unchanged underneath.

```
Fixed overlay (inset-0, z-[60])
  Backdrop (bg-brand-900/60, backdrop-blur-sm)
  Modal shell (m-4, rounded-xl, shadow-2xl, flex flex-col)
    Top bar (flex-shrink-0) — title, customer name, close button
    Two-panel body (flex, flex-1, min-h-0)
      Left panel (w-[320px], border-r)
      Right panel (flex-1)
    Bottom bar (flex-shrink-0) — totals, actions
```

Entry animation: `scale-95 opacity-0` → `scale-100 opacity-100` over 300ms.

### Left Panel — Progress Tracker (320px fixed)

**Progress header**: Progress bar (emerald fill) + "12/47 decided, 3 skipped" counter.

**Filter pills**: All | Has Offers | Needs Review | Decided | Skipped

**Requirement rows** (scrollable):
- 3px left-edge brand indicator on active row
- Status dot: emerald (decided), amber (needs review), gray (no offers), brand-blue (has offers), slate (skipped)
- MPN (mono font, truncated), qty + target price below
- Offer count badge (right side)

**Keyboard hint bar** (bottom): `j`/`k` Navigate, `1`-`9` Select offer, `Enter` Confirm

### Right Panel — Decision Workspace (flex-1)

Four sections, vertically scrollable:

**1. Customer Specs Card** (bg-gray-50, compact grid)
- 2x4 grid: Qty Needed, Target Price, Date Codes, Condition, Packaging, Firmware, HW Codes, Customer PN
- Buyer/sale notes below if present
- 10px uppercase labels, 14px mono values

**2. Offers Table** (compact-table style)
- Radio-select column + Vendor, Unit Price, Qty Available, Lead Time, Date Codes, Condition, MOQ, Confidence
- Selected row: `row-selected` class (brand left-edge border + light bg)
- Rows where qty < target_qty: `opacity-50` with amber badge "Qty low"
- Duplicate vendor offers: subtle badge to flag same-source comparisons
- **Price spread bar** below table: emerald→amber→rose gradient with dot markers per offer

**3. Pricing History** (collapsible, `x-collapse`)
- Last 3 prior quotes: quote number, date, cost, sell, margin %, won/lost badge
- Color-coded margin (emerald/amber/rose)

**4. Decision Area** (visually distinct card)
- State: `bg-brand-50 border-brand-200` (undecided) → `bg-emerald-50/30 border-emerald-300` (decided)
- Selected offer summary: vendor, cost, qty
- Sell price input with `$` prefix, large margin display (2xl bold, color-coded)
- Extended cost / extended sell / line profit stats
- Qty mismatch warning (amber badge if offer qty < target qty)
- Line notes input
- "Confirm & Next" / "Update & Next" button (advances to next undecided line)
- "Skip" button — marks line as explicitly skipped (distinct from undecided)
- "Undo" link for decided/skipped lines

### Bottom Bar — Totals & Actions

**Running totals** (only decided lines counted):
- Decided count: "12/47"
- Total Cost, Total Sell, Blended Margin (color-coded)

**Actions**:
- Bulk markup input: "Apply __% markup to all decided lines"
- Save Quote button (disabled if no customer site linked, or 0 decided lines)
- Excel export (disabled until saved)
- TRIO PDF export (disabled until saved)

---

## Smart Defaults

When the builder opens:
- **1 offer** → auto-selected as `decided`, sell price = offer price (user will adjust)
- **Multiple offers** → `needs_review`, no offer pre-selected
- **0 offers** → `no_offers`, skippable

## Refinements

### 1. No Customer Site Guard
Amber banner at top of modal: "Link a customer to this requisition before saving." Save button disabled. User can still prepare pricing.

### 2. Unsaved Changes Warning
Esc/X with unsaved decisions triggers confirm dialog: "You have 12 unsaved line decisions. Close anyway?"

### 3. Bulk Markup Tool
Input in bottom bar. Enter 25% → sets sell price = cost * 1.25 on all decided lines that don't already have a manually-set sell price. A `sell_price_manual` boolean flag per line in Alpine state tracks whether the user has explicitly edited the sell price. Set to `true` when the user modifies the sell price input, `false` on initial auto-fill from offer price. Bulk markup only applies to lines where `sell_price_manual === false`.

### 4. Qty Mismatch Warning
Amber badge in decision area when selected offer's qty < requirement's target qty: "Offer has 200 pcs, need 500."

### 5. Duplicate Vendor Highlight
If multiple offers share the same vendor, show a subtle duplicate badge so user knows they're comparing same-source options.

### 6. Skip Action
Explicit "Skip" button per line. Status = `skipped` (slate dot). Progress shows: "12 decided, 3 skipped, 32 remaining." Skipped lines excluded from totals.

### 7. Stay Open After Save
After save: show quote number in top bar inline notification, keep export buttons active, allow continued editing. Re-saving creates a new revision of the same quote (via revise endpoint) — the quote number stays the same, revision increments. Closing is always explicit via X button or Escape.

### 8. Totals Only Count Decided Lines
Bottom bar totals reflect only decided lines, not the full requisition.

---

## Keyboard Shortcuts

| Key | Action |
|---|---|
| `j` / `ArrowDown` | Next requirement in filtered list |
| `k` / `ArrowUp` | Previous requirement |
| `1`-`9` | Select offer by position in table |
| `Tab` | Focus sell price input |
| `Enter` (in sell price) | Confirm decision + advance to next |
| `s` | Skip current line |
| `f` | Cycle through filters |
| `Escape` | Close modal (with unsaved changes warning) |

Navigation keys only active when not focused on an input field.

---

## Visual Design

### Colors
- Brand palette (steel-blue): brand-50 through brand-900
- Status: emerald (decided), amber (needs review), gray (no offers), brand-blue (has offers), slate (skipped)
- Margin: emerald ≥25%, amber ≥15%, rose <15%

### Typography
1. MPN: `text-sm font-mono font-medium text-gray-900`
2. Margin %: `text-2xl font-bold tabular-nums`
3. Sell price input: `text-sm font-mono border-2`
4. Section headings: `text-[10px] font-semibold uppercase tracking-wider text-brand-400`
5. Metadata labels: `text-[10px] text-gray-400 uppercase tracking-wide`

### Animations
- Modal entry: scale + fade, 300ms
- Right panel content change: slide-right fade, 200ms
- Offer selection: radio dot scale-in, 150ms
- Decision confirm: row flashes green (0.8s keyframe), progress bar grows (500ms ease-out)
- Decision area border: brand→emerald color shift, 300ms
- Status dot color morph: 300ms

### CSS Additions (styles.css)
```css
@keyframes qbPanelSlide { from { opacity:0; transform:translateX(8px); } to { opacity:1; transform:translateX(0); } }
.qb-panel-enter { animation: qbPanelSlide 0.2s ease-out; }

@keyframes qbDecisionFlash { 0% { background-color: rgba(16,185,129,0.15); } 100% { background-color: transparent; } }
.qb-decision-flash { animation: qbDecisionFlash 0.8s ease-out; }

.qb-list::-webkit-scrollbar { width: 4px; }
.qb-list::-webkit-scrollbar-track { background: transparent; }
.qb-list::-webkit-scrollbar-thumb { background: #b7c7d8; border-radius: 2px; }
```

---

## What's Explicitly Out of Scope

- No drag-to-reorder lines
- No resizable panels (fixed 320px left / flex right)
- No inline editing of offer data (offers are read-only source-of-truth)
- No multi-select offers per line (one offer per requirement)
- No mobile support (desktop-only, blocking message under 1024px)
- No virtual scrolling (50-200 rows render fine natively)
- No external component libraries (AG Grid, Handsontable, etc.)

---

## Dependencies

- **openpyxl** — already installed, used for Excel export
- No new frontend or backend dependencies

---

## Integration Points

- **Existing Quote system**: Save creates a Quote record visible in the Quotes tab automatically
- **Existing PDF export**: TRIO PDF reuses `generate_quote_report_pdf()` from `document_service.py`
- **Existing pricing history**: Reuses `_preload_last_quoted_prices` helper
- **Parts tab**: "Build Quote" button added alongside existing bulk actions
