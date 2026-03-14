# Buy Plan V4 Unification Plan

## Overview
Unify V1 (JSON line_items on `buy_plans` table) and V3 (structured `buy_plans_v3` + `buy_plan_lines` tables) into a single V4 system. V4 reuses the V3 data model (it's already the right design) but renames tables to `buy_plans` / `buy_plan_lines`, drops the V1 model entirely, and adds a full HTMX/Alpine frontend under `/v2/buy-plans`.

## What V4 Keeps from Each Version

**From V3 (keep everything):**
- BuyPlanV3 header + BuyPlanLine structured tables
- Dual approval tracks (manager + ops SO verification)
- Per-line status machine (awaiting_po → pending_verify → verified)
- AI scoring, builder, flags
- Split lines, buyer assignment, PO tracking
- Token-based approval, favoritism detection, case reports
- All workflow functions (submit, approve, verify_so, confirm_po, verify_po, flag_issue, check_completion)
- Notification service

**From V1 (nothing new — V3 already absorbed everything useful):**
- The V1→V3 migration (076) already converted all V1 data
- The V1 compat shim in `routers/crm/buy_plans.py` can be removed

## Steps

### Step 1: Rename V3 → V4 (model layer)
**Files:** `app/models/buy_plan.py`, `app/models/__init__.py`

- Rename class `BuyPlanV3` → `BuyPlan` (keep table name `buy_plans_v3` — no schema change needed yet, table rename is risky and unnecessary)
- Update enum/class docstrings to say "V4"
- Remove old `BuyPlan` class from `app/models/quotes.py` (the V1 model)
- Update `app/models/__init__.py` imports

### Step 2: Update service layer references
**Files:** `app/services/buyplan_workflow.py`, `app/services/buyplan_builder.py`, `app/services/buyplan_scoring.py`, `app/services/buy_plan_v3_service.py`, `app/services/buyplan_v3_notifications.py`

- Replace all `BuyPlanV3` references with `BuyPlan`
- Keep all business logic exactly as-is

### Step 3: Rename API router (V3 → unified)
**Files:** `app/routers/crm/buy_plans_v3.py` → keep file, update paths

- Change API paths from `/api/buy-plans-v3/...` to `/api/buy-plans/...`
- Remove old V1 compat router `app/routers/crm/buy_plans.py`
- Update `app/main.py` router registration

### Step 4: Update schemas
**Files:** `app/schemas/buy_plan.py`

- Rename `BuyPlanV3Submit` → `BuyPlanSubmit`, `BuyPlanV3Approval` → `BuyPlanApproval`, `BuyPlanV3Response` → `BuyPlanResponse`, etc.
- Keep all fields and validators as-is

### Step 5: HTMX/Alpine frontend — List view
**New file:** `app/templates/htmx/partials/buy_plans/list.html`

- Status filter tabs (All, Draft, Pending, Active, Completed, Cancelled)
- "My Only" toggle
- Sortable table: Customer, Quote, Lines, Total, Margin, Status, SO Status, Submitted By, Date
- Each row clicks to detail view via hx-get
- Search bar with debounce

### Step 6: HTMX/Alpine frontend — Detail view
**New file:** `app/templates/htmx/partials/buy_plans/detail.html`

- Header: customer, quote#, SO#, financials, status badges
- AI summary + flags (color-coded by severity)
- Context-sensitive action bar based on status:
  - **Draft**: Submit button (opens SO# form)
  - **Pending**: Approve / Reject buttons (manager only)
  - **Active**: Halt / Cancel buttons; line-level PO entry
  - **Halted/Cancelled**: Reset to Draft button
- Line items table with per-line actions:
  - **awaiting_po**: PO# input + ship date → confirm
  - **pending_verify**: Approve/Reject PO (ops only)
  - **issue**: Issue badge + note
  - **verified**: Green checkmark
- SO verification panel (ops only, when active)
- Offer comparison modal (per-requirement)

### Step 7: HTMX/Alpine frontend — Submit modal
**New file:** `app/templates/htmx/partials/buy_plans/submit_modal.html`

- SO# input (required)
- Customer PO# input (optional)
- Salesperson notes textarea
- Line edits section (optional vendor swaps)

### Step 8: HTMX view router endpoints
**File:** `app/routers/htmx_views.py`

Add endpoints:
- `GET /v2/buy-plans` — full page entry
- `GET /v2/partials/buy-plans` — list partial
- `GET /v2/partials/buy-plans/{plan_id}` — detail partial
- `POST /v2/partials/buy-plans/{plan_id}/submit` — submit form handler (returns updated detail)
- `POST /v2/partials/buy-plans/{plan_id}/approve` — approve/reject (returns updated detail)
- `POST /v2/partials/buy-plans/{plan_id}/verify-so` — SO verification
- `POST /v2/partials/buy-plans/{plan_id}/lines/{line_id}/confirm-po` — PO confirm
- `POST /v2/partials/buy-plans/{plan_id}/lines/{line_id}/verify-po` — PO verify
- `POST /v2/partials/buy-plans/{plan_id}/lines/{line_id}/issue` — flag issue
- `POST /v2/partials/buy-plans/{plan_id}/cancel` — cancel plan

### Step 9: Add sidebar nav link
**File:** `app/templates/htmx/base.html`

- Add "Buy Plans" link under Command Center section

### Step 10: Tests
**New file:** `tests/test_buy_plan_v4.py`

- Test model rename (BuyPlan replaces BuyPlanV3)
- Test all workflow transitions: draft→submit→approve→po_confirm→po_verify→complete
- Test auto-approve path
- Test rejection + resubmit
- Test SO verification
- Test issue flagging
- Test cancel + reset to draft
- Test HTMX list/detail endpoints return HTML

### Step 11: Clean up old code
- Remove `app/routers/crm/buy_plans.py` (V1 compat shim)
- Remove `BuyPlan` class from `app/models/quotes.py`
- Remove V1 notification service `app/services/buyplan_notifications.py`
- Remove V1 PO service `app/services/buyplan_po.py`
- Update any remaining imports

## Not Changing
- **Database tables**: `buy_plans_v3` and `buy_plan_lines` table names stay — renaming tables in production is unnecessary risk for zero benefit
- **Business logic**: All workflow, scoring, builder, notification logic stays exactly as-is
- **JSON API**: The `/api/buy-plans/...` endpoints remain as JSON APIs (for mobile/future use); HTMX endpoints are separate under `/v2/partials/buy-plans/...`
