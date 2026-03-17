# Plan: Part-Number-Centric Split-Panel Requisitions UI

## What we're building
A resizable two-panel layout where the **left panel** shows all part numbers (requirements) across active requisitions in a configurable table, and the **right panel** shows detail tabs for the selected part.

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

## Decisions (confirmed by user)
1. **Default scope**: Active requisitions only, with toggle to include archived
2. **Tasks**: Separate Task model — title, notes, assigned_to, assigned_by, status (pending/done), due_date, linked to requisition + requirement
3. **Column prefs**: Saved per-user in database (persists across devices)

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
- Column picker (gear icon) — show/hide columns, saved per-user in DB
- Click a row → right panel populates with that part's detail
- Highlight selected row
- Offset-based pagination (Prev/Next)
- Default: only parts from active requisitions (toggle for archived)

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
- Notes (free text between sales and buyer)
- Tasks with: title, notes, assigned_to, assigned_by, status (pending/done), due_date
- Create task form inline
- Mark done button on each task

## Implementation Steps

### Step 1: New Task model + user_preferences column + migration
**Files**: `app/models/tasks.py`, `app/models/__init__.py`, new Alembic migration

Task model:
- id (PK)
- title (string, required)
- notes (text, optional)
- assigned_to_id (FK → users)
- assigned_by_id (FK → users)
- requisition_id (FK → requisitions)
- requirement_id (FK → requirements, optional)
- status (string: pending/done)
- due_date (date, optional)
- completed_at (datetime, optional)
- created_at (datetime)

User model addition:
- parts_column_prefs (JSON) — list of visible column keys

Migration: creates tasks table + adds parts_column_prefs to users.

### Step 2: Backend — Parts list endpoint
**File**: `app/routers/htmx_views.py`

- New route: `GET /v2/partials/parts`
- Query: Requirement joined to Requisition (WHERE requisition.status = 'active' by default)
- Subqueries: offer count, best price per requirement
- Filter params: q, requisition_name, customer, brand, status, owner, date_from, date_to, include_archived
- Sort params: sort, dir
- Pagination: offset, limit
- Reads user.parts_column_prefs for visible columns
- Returns HTML partial
- ~100 lines

### Step 3: Backend — Part detail tab endpoints
**File**: `app/routers/htmx_views.py`

- `GET /v2/partials/parts/{requirement_id}/tab/offers` — offers table
- `GET /v2/partials/parts/{requirement_id}/tab/sourcing` — sightings table
- `GET /v2/partials/parts/{requirement_id}/tab/activity` — activity timeline
- `GET /v2/partials/parts/{requirement_id}/tab/comms` — notes + tasks list + create form
- `POST /v2/partials/parts/{requirement_id}/tasks` — create task
- `POST /v2/partials/parts/tasks/{task_id}/done` — mark task done
- `POST /v2/partials/parts/column-prefs` — save column preferences
- Each tab ~40-60 lines

### Step 4: Frontend — Split panel workspace template
**New file**: `app/templates/htmx/partials/parts/workspace.html`

- CSS Grid layout with resizable divider (Alpine.js for drag logic)
- Left: filter bar + parts table + pagination
- Right: tab bar + detail content area
- Empty state for right panel when no part selected
- Divider saves width to localStorage (instant UX, no DB roundtrip for layout)
- ~100 lines

### Step 5: Frontend — Column picker
**Inline in workspace.html or small partial**

- Gear icon dropdown with checkboxes for each column
- On save, POST to /v2/partials/parts/column-prefs, then re-fetch table
- ~40 lines

### Step 6: Frontend — Detail tab templates
**New files**:
- `app/templates/htmx/partials/parts/tabs/offers.html`
- `app/templates/htmx/partials/parts/tabs/sourcing.html`
- `app/templates/htmx/partials/parts/tabs/activity.html`
- `app/templates/htmx/partials/parts/tabs/comms.html`
- Each ~50-70 lines

### Step 7: Wire up routing + nav
**Files**: `app/routers/htmx_views.py`, `app/templates/htmx/base.html`, `app/routers/auth.py`

- `GET /v2/requisitions` loads the split-panel workspace
- Update sidebar nav link
- Update auth redirect to `/v2/requisitions`

### Step 8: Tests
**New file**: `tests/test_htmx_parts_workspace.py`

- Parts list: filters, sorting, pagination, active-only default
- Part detail tabs: each returns correct HTML
- Task CRUD: create, mark done
- Column prefs: save and respect visible columns
- Empty state: right panel shows message when no part selected
- ~25 tests

## Files that will change/be created
- `app/models/tasks.py` — NEW (Task model)
- `app/models/__init__.py` — add Task import
- `app/models/users.py` — add parts_column_prefs column
- `alembic/versions/xxx_add_tasks_and_column_prefs.py` — NEW migration
- `app/routers/htmx_views.py` — new endpoints (~250 lines)
- `app/templates/htmx/partials/parts/workspace.html` — NEW
- `app/templates/htmx/partials/parts/tabs/offers.html` — NEW
- `app/templates/htmx/partials/parts/tabs/sourcing.html` — NEW
- `app/templates/htmx/partials/parts/tabs/activity.html` — NEW
- `app/templates/htmx/partials/parts/tabs/comms.html` — NEW
- `app/templates/htmx/base.html` — nav link update
- `app/routers/auth.py` — redirect update
- `tests/test_htmx_parts_workspace.py` — NEW

## What stays unchanged
- Existing requisitions2 and v2 requisitions code stays until this is working
- All existing API endpoints unchanged
- No changes to existing models (only additions)
