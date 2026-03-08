# Task Board Feature Plan (Replacing Q&A Sub-Tab)

## Overview
Replace the Q&A sub-tab with a **pipeline-style task board** per requisition, plus a
**"My Tasks" sidebar widget** showing the logged-in buyer's tasks across all reqs.
AI provides priority scoring and risk alerts.

## Pipeline Stages
Requisitions progress through 4 stages:
1. **New** — just received, no sourcing activity yet
2. **Sourcing** — actively searching, sending RFQs, collecting offers
3. **Quoted** — quote built and sent to customer, awaiting response
4. **Won/Lost** — terminal: customer placed PO (Won) or declined (Lost)

Tasks are generated per stage. The board shows tasks grouped by the req's current
pipeline stage.

## Data Model: `RequisitionTask`

New table `requisition_tasks`:
- `id` (PK)
- `requisition_id` (FK → requisitions, required)
- `title` (String 255, required) — short task description
- `description` (Text, nullable) — detail/notes
- `task_type` (String 20) — `sourcing`, `sales`, `general`
- `status` (String 20, default `todo`) — `todo`, `in_progress`, `done`
- `priority` (Integer, default 2) — 1=low, 2=medium, 3=high
- `ai_priority_score` (Float, nullable) — AI-computed urgency 0.0-1.0
- `ai_risk_flag` (String 255, nullable) — AI risk alert text
- `assigned_to_id` (FK → users, nullable)
- `created_by` (FK → users, nullable)
- `source` (String 20, default `manual`) — `manual` | `system` | `ai`
- `source_ref` (String 100, nullable) — e.g. `offer:123`, `rfq:456`
- `due_at` (DateTime, nullable)
- `completed_at` (DateTime, nullable)
- `created_at`, `updated_at` (timestamps)

Indexes: `(requisition_id, status)`, `(assigned_to_id, status)`, `(status, due_at)`

## API Endpoints

### Per-requisition tasks: `/api/requisitions/{req_id}/tasks`
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/tasks` | List tasks (filter: status, type, assignee) |
| POST | `/tasks` | Create task |
| PUT | `/tasks/{id}` | Update task fields |
| PATCH | `/tasks/{id}/status` | Quick status change (drag-drop / auto-close) |
| DELETE | `/tasks/{id}` | Delete task |

### Cross-req "My Tasks": `/api/tasks/mine`
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/mine` | All tasks assigned to current user, sorted by AI priority + due date |
| GET | `/mine/summary` | Counts by status + overdue count (for sidebar badge) |

## Auto-Generated Tasks (source='system')

Triggered from existing service functions:
1. **New requirement added** → "Source {mpn} — find vendors" (sourcing, priority 2)
2. **New offer received** → "Review offer from {vendor} for {mpn}" (sourcing, priority 2)
3. **RFQ sent** → "Awaiting response from {vendor}" (sourcing, priority 1, due +3 days)
4. **No RFQ response after 3 days** → "Follow up RFQ to {vendor}" (sourcing, priority 3)
5. **Quote created** → "Send quote to {customer}" (sales, priority 3)
6. **Quote expiring in 2 days** → "Quote expires soon — follow up" (sales, priority 3)

## Auto-Close Logic

System marks tasks `done` + sets `completed_at` when:
- "Review offer" → offer status changes from `pending_review`
- "Source {mpn}" → at least one offer exists for that MPN
- "Awaiting response" → vendor reply parsed
- "Send quote" → quote status = `sent`
- "Follow up RFQ" → vendor responds OR task manually closed

## AI Features

### Priority Scoring (runs on task list load or periodic refresh)
- Computes `ai_priority_score` (0.0-1.0) based on:
  - Due date proximity (higher = more urgent)
  - Customer importance / deal size
  - Time since last activity on the req
  - Number of stale/unreviewed offers
- Tasks sorted by this score in "My Tasks" sidebar

### Risk Alerts (runs as background job)
- Sets `ai_risk_flag` text on tasks when:
  - "No activity in 3+ days" on active req
  - "Quote expires tomorrow"
  - "Offer price increasing vs last quote"
  - "All RFQs unanswered — try different vendors"
- Shown as warning badge on task card

## Frontend

### 1. Pipeline Board (replaces Q&A sub-tab)
Sub-tab renamed: `qa` → `tasks`

Layout: 4 pipeline columns matching req stages:
```
[New]          [Sourcing]       [Quoted]        [Won/Lost]
│ Source MPN   │ Review offer   │ Follow up     │ ✓ PO received
│ Find vendor  │ Follow up RFQ  │ Quote expires │
│ + Add task   │ + Add task     │ + Add task    │
```

- Cards show: title, type badge (teal=sourcing, blue=sales, gray=general),
  assignee initials, due date, priority dot (red/yellow/green), risk flag icon
- System-generated cards have subtle "auto" label
- Drag-and-drop between columns (HTML5 drag API)
- "+" inline form at bottom of each column (no modal)
- Click card → expand inline for editing
- Filter bar: All | Sourcing | Sales | General

### 2. My Tasks Sidebar Widget
Collapsible sidebar on the left side of the main view:
- Toggle button visible at all times (with badge count of pending tasks)
- When expanded (250px wide): task list grouped by urgency
  - Overdue (red header)
  - Due Today (amber header)
  - Upcoming (default)
  - No due date
- Each task card links to its requisition (click → drill-down opens)
- Compact card: title + req name + due date + priority dot

## Files to Create/Modify

### New files:
- `app/models/task.py` — RequisitionTask model
- `app/schemas/task.py` — Pydantic request/response schemas
- `app/services/task_service.py` — CRUD, auto-gen, auto-close, AI scoring
- `app/routers/task.py` — API endpoints
- `alembic/versions/065_requisition_tasks.py` — migration
- `tests/test_task_service.py` — service tests
- `tests/test_routers_task.py` — API endpoint tests

### Modified files:
- `app/models/__init__.py` — export RequisitionTask
- `app/main.py` — register task router
- `app/static/app.js` — pipeline board + My Tasks sidebar
- `app/static/styles.css` — pipeline board + sidebar CSS
- `app/templates/index.html` — sidebar HTML container + sub-tab label

### Untouched:
- Knowledge system (facts, AI insights, auto-capture) stays intact
- Q&A JS functions remain in code but sub-tab no longer links to them
- Knowledge API endpoints unchanged

## Implementation Order
1. Model + migration
2. Schemas
3. Service layer (CRUD + auto-gen + auto-close)
4. API router + register
5. Tests
6. Frontend: pipeline board CSS + JS
7. Frontend: My Tasks sidebar
8. AI priority scoring + risk alerts
9. Commit + push + deploy
