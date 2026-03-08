# Task Board Feature Plan (Replacing Q&A Sub-Tab)

## Overview
Replace the Q&A sub-tab in each requisition's drill-down with a Kanban-style task board. Tasks cover sourcing, sales, and general communication — auto-generated from system events plus manual creation.

## Data Model: `RequisitionTask`

New table `requisition_tasks`:
- `id` (PK)
- `requisition_id` (FK → requisitions, required)
- `title` (String 255, required) — short task description
- `description` (Text, nullable) — optional detail/notes
- `task_type` (String 20) — `sourcing`, `sales`, `general`
- `status` (String 20, default `todo`) — `todo`, `in_progress`, `done`
- `priority` (Integer, default 2) — 1=low, 2=medium, 3=high
- `assigned_to_id` (FK → users, nullable) — single assignee
- `created_by` (FK → users, nullable)
- `source` (String 20, default `manual`) — `manual` | `system` | `ai`
- `source_ref` (String 100, nullable) — e.g. `offer:123`, `rfq:456` for auto-generated
- `due_at` (DateTime, nullable)
- `completed_at` (DateTime, nullable)
- `created_at`, `updated_at` (timestamps)

Indexes: `(requisition_id, status)`, `(assigned_to_id, status)`

## API Endpoints (`/api/requisitions/{req_id}/tasks`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/tasks` | List tasks (filter by status, type, assignee) |
| POST | `/tasks` | Create task |
| PUT | `/tasks/{id}` | Update task (title, status, assignee, priority, etc.) |
| PATCH | `/tasks/{id}/status` | Quick status change (drag-drop) |
| DELETE | `/tasks/{id}` | Delete task |

## Service Layer

`app/services/task_service.py`:
- `create_task()` — CRUD
- `update_task()` — CRUD
- `update_task_status()` — status transition + set `completed_at` when → done
- `delete_task()` — CRUD
- `get_tasks()` — query by requisition + filters
- `auto_generate_tasks()` — called from event hooks

## Auto-Generated Tasks

Hook into existing service functions to create tasks:
1. **New offer received** → "Review offer from {vendor} for {mpn}" (sourcing)
2. **RFQ sent, no response after 3 days** → "Follow up on RFQ to {vendor}" (sourcing)
3. **Quote created** → "Send quote to customer" (sales)
4. **Quote expires in 2 days** → "Quote expiring: follow up with customer" (sales)
5. **New requirement added** → "Source {mpn} — find vendors" (sourcing)

Auto-tasks have `source='system'` and `source_ref` pointing to the triggering entity.

## Frontend: Kanban Board UI

Replace `_renderDdQA()` with `_renderDdTasks()`:
- Three columns: **To Do** | **In Progress** | **Done**
- Each column is a scrollable card list
- Cards show: title, type badge (colored), assignee avatar/initials, due date, priority dot
- System-generated cards have a subtle "auto" indicator
- Drag-and-drop between columns (using native HTML5 drag API)
- "+" button at top of To Do column opens inline form (no modal)
- Click card to expand inline for editing
- Filter bar: All | Sourcing | Sales | General (reuse existing filter pill style)
- Column headers show count

Sub-tab rename: `qa` → `tasks`

## Files to Create/Modify

### New files:
- `app/models/task.py` — RequisitionTask model
- `app/schemas/task.py` — Pydantic schemas
- `app/services/task_service.py` — business logic
- `app/routers/task.py` — API endpoints
- `alembic/versions/065_requisition_tasks.py` — migration

### Modified files:
- `app/models/__init__.py` — add RequisitionTask export
- `app/main.py` — register task router
- `app/static/app.js` — replace Q&A rendering with Kanban board
- `app/static/styles.css` — Kanban board CSS
- `app/templates/index.html` — rename sub-tab label if needed

### Untouched:
- Knowledge system stays intact (facts, AI insights, auto-capture still work)
- Knowledge API endpoints unchanged
- Only the Q&A sub-tab UI is replaced; knowledge entries with `entry_type='question'` still exist in DB

## Implementation Order
1. Model + migration
2. Schemas + service
3. Router + register
4. Frontend Kanban UI
5. Auto-generation hooks
6. Commit + push + deploy

## Testing
- Unit tests for task CRUD service
- API endpoint tests for all 5 routes
- Frontend is manual testing (vanilla JS, no test framework)
