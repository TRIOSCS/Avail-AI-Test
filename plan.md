# Plan: Part-Number-Centric Split-Panel Requisitions UI

## What we're building
A resizable two-panel layout where the **left panel** shows all part numbers (requirements) across requisitions in a configurable table, and the **right panel** shows detail tabs for the selected part.

## Layout

```
┌─────────────────────────────────────────────────────────────┐
│ Sidebar │  LEFT PANEL (resizable)  ║  RIGHT PANEL           │
│         │                          ║                         │
│         │  Part numbers table      ║  Tabs:                  │
│         │  - Filterable            ║  [Offers] [Sourcing]    │
│         │  - Sortable columns      ║  [Activity] [Comms]     │
│         │  - Configurable columns  ║                         │
│         │  - Horiz scroll          ║  Detail content for     │
│         │  - Click to select →     ║  selected part number   │
│         │                          ║                         │
│         │  ◄══ drag bar ══►        ║                         │
└─────────────────────────────────────────────────────────────┘
```

## Left Panel — Parts Table

**Primary entity**: `Requirement` (one row per part number)

**Default columns** (user can show/hide via column picker):
- MPN (primary_mpn)
- Brand
- Qty Needed (target_qty)
- Target Price
- Sourcing Status (sourcing_status)
- Requisition Name (from parent requisition)
- Customer (from parent requisition)
- Offers count
- Best Price (lowest active offer)
- Owner (requisition claimed_by)
- Created

**Filters** (above the table):
- Requisition name
- Customer name
- Brand
- Sourcing status (open/sourcing/offered/quoted/won/lost)
- Owner
- Date range

**Behaviors**:
- Horizontal scroll for wide tables
- Column picker (gear icon) — show/hide columns, save preference to localStorage
- Click a row → right panel populates with that part's detail
- Highlight selected row
- Offset-based pagination (Prev/Next)

## Right Panel — Part Detail Tabs

Shows when a part is selected. Empty state message when nothing selected.

**Tab 1: Offers** (default)
- Table of all offers for this requirement (historical + active)
- Columns: Vendor, Price, Qty, Date Code, Condition, Lead Time, Status, Created
- Button to add offer manually

**Tab 2: Sourcing**
- Sightings/leads for this part number
- Source type, vendor, price, qty, confidence score
- Link to run new search

**Tab 3: Activity**
- ActivityLog entries linked to the parent requisition
- Buyer efforts: calls, emails, notes
- Timeline format

**Tab 4: Communications**
- Notes between sales and buyer
- Task assignment (sales → buyer, buyer → sales)
- Reuses ActivityLog with activity_type in ("note", "task")

## Implementation Steps

### Step 1: Backend — Parts list endpoint
**File**: `app/routers/htmx_views.py`

- New route: `GET /v2/partials/parts`
- Query: Requirement joined to Requisition, with subqueries for offer count + best price
- Filter params: q, requisition_name, customer, brand, status, owner, date_from, date_to
- Sort params: sort, dir
- Pagination: offset, limit
- Returns HTML partial (the table rows)
- ~80 lines

### Step 2: Backend — Part detail tab endpoints
**File**: `app/routers/htmx_views.py`

- `GET /v2/partials/parts/{requirement_id}/tab/offers` — offers table
- `GET /v2/partials/parts/{requirement_id}/tab/sourcing` — sightings table
- `GET /v2/partials/parts/{requirement_id}/tab/activity` — activity timeline
- `GET /v2/partials/parts/{requirement_id}/tab/comms` — notes + tasks
- Each ~40-60 lines

### Step 3: Frontend — Split panel workspace template
**New file**: `app/templates/htmx/partials/parts/workspace.html`

- CSS Grid layout with resizable divider
- Left: filter bar + parts table + pagination
- Right: tab bar + detail content area
- Empty state for right panel when no part selected
- ~80 lines

### Step 4: Frontend — Resizable divider JS
**Inline in workspace.html** (Alpine.js x-data)

- mousedown on divider → track mousemove → adjust grid-template-columns
- Save width ratio to localStorage
- Restore on page load
- ~30 lines

### Step 5: Frontend — Column picker
**New file**: `app/templates/htmx/partials/parts/column_picker.html`

- Gear icon dropdown with checkboxes for each column
- Save to localStorage
- On change, re-fetch table with visible columns as param
- ~40 lines

### Step 6: Frontend — Detail tab templates
**New files**:
- `app/templates/htmx/partials/parts/tabs/offers.html` — offer rows table
- `app/templates/htmx/partials/parts/tabs/sourcing.html` — sightings table
- `app/templates/htmx/partials/parts/tabs/activity.html` — timeline
- `app/templates/htmx/partials/parts/tabs/comms.html` — notes + tasks
- Each ~50-70 lines

### Step 7: Wire up routing + nav
**Files**: `app/routers/htmx_views.py`, `app/templates/htmx/base.html`

- `GET /v2/requisitions` loads the split-panel workspace (change existing route)
- Update sidebar nav link
- Update auth redirect to `/v2/requisitions`

### Step 8: Tests
**New file**: `tests/test_htmx_parts_workspace.py`

- Parts list: filters, sorting, pagination
- Part detail tabs: each returns correct HTML
- Column picker: respects visible columns param
- Empty state: right panel shows message when no part selected
- ~20 tests

## Data model
No new models or migrations needed. Existing models support everything:
- `Requirement` → part numbers
- `Offer` → offers (via requirement_id)
- `Sighting` → sourcing leads (via requirement_id)
- `ActivityLog` → buyer activity (via requisition_id)
- `ActivityLog` with type "note"/"task" → communications

## What stays unchanged
- The existing requisitions2 and v2 requisitions code stays in place until this is working
- No model changes, no migrations
- All existing API endpoints unchanged

## Open questions for user
1. Should the left panel show ALL parts across ALL requisitions, or only parts from active requisitions by default?
2. For the Communications tab — should tasks have a "done/pending" status, or just be notes with an assignee?
3. Should the column picker save per-user in the DB (persists across devices) or just localStorage (simpler, this device only)?
