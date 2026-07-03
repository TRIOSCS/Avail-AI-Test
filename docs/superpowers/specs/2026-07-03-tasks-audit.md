# Tasks Module — Read-Only Audit & Rework Plan

**Date:** 2026-07-03
**Scope:** The user-facing Task Manager (bottom-nav "Tasks" → `/v2/my-day`) and every
surface that creates/reads/mutates `RequisitionTask` rows (CRM account/contact, vendor
card, requisition detail tab, part comms tab). Excludes unrelated `TaskStatus`
background-job code.
**Method:** Static trace of routers → services → templates on the live code. No code
was modified.

---

## 1. Architecture as-built (map)

The "Tasks" nav item is historically named **My Day**. The table is
`requisition_tasks` (model `RequisitionTask`, `app/models/task.py`) — a single polymorphic
task row that may hang off a requisition, requirement, company, site-contact, vendor card,
or vendor contact (`CHECK ck_task_has_parent`, `app/models/task.py:97`).

**Surfaces that touch tasks (six, inconsistent):**

| Surface | Route(s) | Create form fields | Row actions | Status |
|---|---|---|---|---|
| Tasks page (My Day) | `GET /v2/partials/my-day` (`app/routers/htmx_views.py:1859`) | *none* (read-only) | complete only | works (read) |
| CRM account | `app/routers/htmx/archive.py:201` | title + due | complete/edit/snooze/delete | works |
| CRM contact | `app/routers/htmx/archive.py:274` | title + due | complete/edit/snooze/delete | works |
| Vendor card | `app/routers/htmx/archive.py:754` | title + due | complete/edit/snooze/delete | works |
| Part comms tab | `POST /v2/partials/parts/{id}/tasks` (`app/routers/htmx/parts.py:908`) | title + notes + assignee + due | done/reopen | **due-date bug** |
| Requisition "Tasks" tab | render `app/routers/htmx/requisitions.py:1071`; form posts `/api/requisitions/{id}/tasks*` | title + type + priority + assignee + due | complete/delete | **ALL MUTATIONS DEAD (404)** |

The mutation endpoints for the first four live in `app/routers/htmx/archive.py`
(`complete_task_endpoint`, `delete_task_endpoint`, `task_edit_form`, `edit_task_endpoint`,
`snooze_task_endpoint`). Service logic is `app/services/task_service.py`. Access to the
Tasks page is gated by `AccessKey.MY_DAY` (`app/access_paths.py:48`,
`app/routers/htmx_views.py:1862`).

Note: the docstrings across `app/models/task.py:10`, `app/schemas/task.py:3`,
`app/services/task_service.py:7` all say "Called by: routers/task.py" — **that router
does not exist.** This stale reference is the fingerprint of the dead code below.

---

## 2. Findings (ranked by severity)

### CRITICAL

#### C1 — The requisition "Tasks" tab is completely non-functional for every mutation
`app/templates/htmx/partials/requisitions/tabs/tasks.html:23` (create),
`:83` (complete), `:151` (delete) all target `/api/requisitions/{req.id}/tasks…`.
**No such endpoints exist** — an exhaustive grep of `app/routers/**` finds only
`/api/requirements/{requirement_id}/tasks` (requirement/part level,
`app/routers/requisitions/requirements.py:1358,1423`). The tab *renders* existing tasks
(read path at `requisitions.py:1071` works) but:
- "Add task" → `POST /api/requisitions/{id}/tasks` → 404/405 (HTMX silently no-ops on
  non-2xx; the user sees nothing happen).
- Complete checkbox → 404. Delete → 404.

So the richest task-creation form in the app (the only one with type + priority +
assignee, `tasks.html:35-62`) is dead, and every existing task on that tab is
un-completable and un-deletable from there.
**Fix:** Add the missing HTML-returning endpoints
(`POST /api/requisitions/{id}/tasks`, `.../tasks/{id}/complete`,
`DELETE .../tasks/{id}`) that create/mutate a requisition-scoped `RequisitionTask` and
return the swapped row fragment; OR (lighter) repoint the tab at the existing
`archive.py` `/v2/partials/tasks/{id}/…` infra plus a new requisition create endpoint.
Recommend building the three endpoints — this is a whole intended surface. Add tests.

---

### HIGH

#### H1 — `create_part_task` binds a raw string to a timestamptz column (PG/SQLite-masked)
`app/routers/htmx/parts.py:933`: `due_at=form.get("due_date") or None` passes the bare
HTML-date string (e.g. `"2026-07-10"`) straight into `RequisitionTask.due_at`, a
`UTCDateTime` (TIMESTAMP WITH TIME ZONE) column. `UTCDateTime.process_bind_param`
(`app/database.py`) **deliberately passes non-`datetime` values through unchanged**
(`if not isinstance(value, datetime): return value`). Consequences:
- **PostgreSQL:** the string is cast server-side at the session timezone → the stored
  instant is *not* UTC-normalized (wrong day near midnight), silently.
- **SQLite (tests):** stored as a bare string; on read `process_result_value` /
  template code does `t.due_at.strftime(...)` / `t.due_at.date()` → `AttributeError`
  on the My Day list (`_results.html:56`) and the comms tab (`comms.html:67,69`).
- No format validation, no 24h rule (unlike CRM endpoints).

This is exactly the "SQLite masks PG-invalid writes" trap. **Fix:** parse to a datetime
before assignment, mirroring the CRM endpoints
(`date.fromisoformat` → `datetime.combine(..., min.time()).replace(tzinfo=utc)`,
`archive.py:225-226`). Best: extract one shared `_parse_due_date(form_value)` helper and
use it in all create/edit endpoints.

#### H2 — Due-date bucketing & filtering use the UTC calendar day (no user timezone)
`_task_due_state` (`app/template_env.py:319-330`) computes overdue/"due today" against
`now_utc.date()`, and `my_day_partial` filters `due=='today'` with
`t.due_at.date() == now.date()` (`app/routers/htmx_views.py:1899`) — both in UTC. No user
/ display timezone is threaded anywhere. For any non-UTC user a task due late-evening
local flips a calendar day, so "Overdue" and "Due today" are wrong by the UTC offset.
**Fix:** thread a configured display timezone (e.g. `America/*`) into the bucketing +
filter predicates + rendering; convert `now` and `due_at` to local before `.date()`.

#### H3 — "Due today" filter and the results grouping contradict each other
On My Day, selecting **Due today** filters to `due_at.date() == now.date()`
(`htmx_views.py:1899`, which *includes* tasks due earlier today), but the results
template buckets any task with `due <= now_utc` under **Overdue**
(`_task_due_state`, `template_env.py:328`). So a task due 9am (now 2pm) matches the
"Due today" filter yet is rendered under the **Overdue** heading — the filter label and
the visible group disagree. **Fix:** make the filter and the bucket share one predicate
(define "today" and "overdue" once, consume both places).

---

### MEDIUM

#### M1 — Large dead-code surface in `task_service.py` (AI scoring never runs)
None of these are called anywhere outside the module (verified by grep):
`score_tasks_with_ai` (`:726`), `apply_simple_scoring` (`:820`),
`compute_simple_priority` (`:772`), `get_waiting_on_tasks` (`:236`),
`update_task_status` (`:180`), `get_tasks` (`:78`), `task_to_response` (`:668`).
Consequently the `ai_priority_score` / `ai_risk_flag` columns
(`app/models/task.py:53-54`) are **written by nothing**. The only UI that reads them is
the dead requisition tab (`tasks.html:122-135`), so those badges never render.
~200 lines of service + two model columns are inert. **Fix:** delete, or — if AI/heuristic
priority is a wanted feature — wire `apply_simple_scoring` into `my_day_partial` and
surface the score/flag on the live Tasks list (product decision; see rework plan).

#### M2 — Dead request/response schemas + un-enforced 24h rule
`app/schemas/task.py` (`TaskCreate`, `TaskUpdate`, `TaskComplete`, `TaskStatusUpdate`,
`_require_due_at_24h`) is entirely unused — it names `routers/task.py`, which doesn't
exist. The 24h-minimum due-date validation these encode is therefore **not enforced** on
any real create path; only the belt-and-suspenders check in `create_task`
(`task_service.py:55-58`) fires, and only for `source="manual"` calls that pass a
`due_at` — the HTMX CRM/part endpoints don't go through it. So "min 24h" is effectively
dead too, and inconsistent with what the UI allows. **Fix:** delete the dead schemas; if
the 24h floor is intended, enforce it in the live endpoints (or drop the rule entirely —
a task manager that forbids "due today" is user-hostile).

#### M3 — `completion_note` is never collected (dead field + dead prompt)
Every completion path passes `completion_note=""`: `complete_crm_task`
(`archive.py:386`, My Day + CRM), `complete_task` (`parts.py:957`). No UI ever prompts
"How was this task resolved?" though the model
(`app/models/task.py:65`) and `TaskComplete.completion_note` (min_length=1) promise it.
**Fix:** either add a completion-note capture (e.g. optional note on the My Day complete)
or drop the field + schema.

#### M4 — N+1 on the My Day list
`get_my_tasks` (`task_service.py:97-113`) does no eager loading, and `_results.html`
touches `t.company` (`:63`), `t.site_contact.full_name` (`:70`), `t.requisition.name`
(`:72`) per row → one extra query per relationship per task. The requisition tab already
does the right thing (`joinedload(RequisitionTask.assignee)`, `requisitions.py:1074`).
**Fix:** add `joinedload(company, site_contact, requisition)` (+ assignee) to
`get_my_tasks`.

#### M5 — `get_my_tasks(status="done")` is unbounded
When the My Day **Done** filter is selected, `get_my_tasks` returns *every* done task
ever assigned to the user with no LIMIT/pagination (`task_service.py:104-113`). A
long-tenured user could load thousands of rows in one fragment. **Fix:** cap "done" to a
recent window (e.g. completed within 30 days) or paginate.

#### M6 — Vendor-scoped tasks have no ownership gate
`_is_crm_task_authorized` (`task_service.py:421-422`) returns `True` for *any*
vendor-scoped task, so **any authenticated user** can complete/edit/snooze **any** vendor
task (delete still requires admin, `archive.py:461`). Documented as intentional, but it
means vendor tasks are effectively unowned. Not a customer-data leak (vendor tasks only),
but confirm it's the desired policy; if not, gate on creator/assignee/admin like CRM
tasks.

---

### LOW / UX

#### L1 — Auto-generated tasks are mostly unassigned → invisible on the Tasks page
`on_requirement_added`, `on_offer_received`, `on_email_offer_parsed`, `on_bid_due_soon`
(`task_service.py:530-603`) create tasks with `assigned_to_id=None` (only
`on_buy_plan_assigned` sets a buyer). But every Tasks-page query filters
`assigned_to_id == user.id` (`get_my_tasks`, `task_service.py:104`). So the auto-sourcing
tasks the system generates never appear on anyone's My Day — they surface only on the
(broken, C1) requisition tab. The auto-generation machinery feeds a void. **Fix:** assign
auto-tasks to the requisition owner/creator, and/or add an "unassigned on my reqs" lane.

#### L2 — Tasks page is nearly view-only; completion has no undo
My Day exposes only a complete checkbox (`_results.html:39-47`). No create, snooze, edit,
reopen, priority change, or todo→in_progress from the Tasks page — those exist only on
CRM/vendor/part surfaces. `reopen_task` exists but is wired only to the part comms tab
(`parts.py:975`). So a mis-clicked "complete" on My Day cannot be undone there.
**Fix:** add snooze + reopen (and ideally quick edit) affordances to the My Day row.

#### L3 — No create-task affordance on the Tasks page
Users on the Tasks page cannot add a personal/standalone to-do; every task must originate
from a req/company/contact/vendor. `ck_task_has_parent` even forbids a parentless task.
**Fix (needs product):** allow a lightweight self-assigned task (e.g. parent = a
"personal" scope, or relax the constraint) with a "New task" button on My Day.

#### L4 — "Medium" priority is invisible; CRM forms can't set priority
`task_priority_badge` (`_macros.html:193`) renders a badge only for High (3) and Low (1)
— Medium (2) shows nothing. CRM account/contact/vendor create forms
(`_account_task_form.html`, etc.) capture only title + due and hard-code priority 2
(`create_company_task` default, `task_service.py:275`). Since most tasks are therefore
medium-with-no-picker, the My Day **Medium** priority filter (`list.html:56`) is visually
meaningless and priority is uneditable from CRM. **Fix:** render a subtle Medium badge and
add a priority (and assignee) control to the CRM/My-Day create forms.

#### L5 — Inconsistent date formatting + an in-template datetime anti-pattern
Due dates render three different ways: `%b %-d` (My Day, `_results.html:56`),
`%m/%d/%y` (comms, `comms.html:69`), `%b %d` (req tab, `tasks.html:147`). Worse,
`comms.html:67` does `task.due_at.date() < today` arithmetic *inside the template* — the
exact pattern `_task_due_state` was created to centralize — and it will raise if
`due_at` is a string (H1). **Fix:** route all rows through `task_due_state` + one shared
date filter.

#### L6 — Requisition-tab delete button can never appear (moot, but signals no test)
`tasks.html:156` uses `opacity-0 group-hover:opacity-100`, but the parent row
(`tasks.html:77-79`) has no `group` class, so the delete button stays at opacity 0.
Cosmetic and moot (the endpoint is dead, C1), but it confirms this tab shipped untested.

#### L7 — Four near-duplicate create functions
`create_task`, `create_company_task`, `create_contact_task`, `create_vendor_task`
(`task_service.py:35,268,298,358`) are copy-paste bodies differing only in which FK is
set, plus two more inline `RequisitionTask(...)` constructions in `parts.py:926` and
`requirements.py:1438`. **Fix:** one `create_task(scope=..., **fields)` with a scope enum.

#### L8 — Likely-dead JSON endpoints
`GET/POST /api/requirements/{id}/tasks` (`requirements.py:1358,1423`) have no template or
JS caller (the comms tab uses the HTMX `/v2/partials/parts/{id}/tasks` route instead).
Verify and remove if confirmed dead.

---

## 3. What's solid — leave alone

- **Tasks-page access & isolation.** Gated by `AccessKey.MY_DAY` (`access_paths.py:48`)
  and the query filters strictly by `assigned_to_id == user.id` — no cross-user task
  leakage on the Tasks page. This is the important authz property and it's correct.
- **`UTCDateTime` type decorator** (`app/database.py`) is sound; the CRM create/edit
  endpoints parse dates to UTC-midnight datetimes correctly (`archive.py:225-226`,
  `:590-591`). H1 is a *caller* bug, not a type-system bug.
- **`auto_create_task` idempotency** via `_find_open_task_by_ref` (dedup by
  `(requisition_id, source_ref)`, `task_service.py:472-510`) is clean, and
  `auto_create_resell_followup_task` keys on `source_ref + assignee` regardless of status
  so reloads never duplicate.
- **Bid-due scheduler job** (`app/jobs/task_jobs.py`) is well-bounded: active statuses
  only, 2-day horizon, cap 20/run, skips non-ISO deadlines.
- **Indexing** on `requisition_tasks` is comprehensive (`model/task.py:87-96`:
  assignee/status, status/due, creator/status, per-parent/status) — queries are covered.
- **`_task_due_state`** centralizing naive→aware coercion is the right pattern; it just
  needs a user timezone (H2).
- **The `archive.py` CRM/vendor mutation set** (complete/edit/snooze/delete) is coherent,
  authz-gated (`_is_crm_task_authorized`), and re-renders the correct parent fragment.

---

## 4. Recommended rework plan (phased, each phase independently shippable)

**Phase 1 — Stop the bleeding (correctness).**
- C1: build `POST /api/requisitions/{id}/tasks`, `.../tasks/{id}/complete`,
  `DELETE .../tasks/{id}` (HTML-returning), wire the requisition tab, add tests.
- H1: fix `create_part_task` due-date parsing; extract a shared `_parse_due_date` helper
  and use it in every create/edit endpoint.
- H3: unify the "today"/"overdue" predicate so the My Day filter and grouping agree.
- Ships as: "Requisition task board + part due-dates work again."

**Phase 2 — Timezone correctness.**
- H2: thread a display timezone through `_task_due_state`, the `my_day_partial` due
  filters, and all date rendering. One helper, consumed everywhere. Add tests at a
  non-UTC offset crossing midnight.

**Phase 3 — Delete dead weight (or decide to use it).**
- M1/M2/M3/L8: remove the dead scoring/waiting-on/`task_to_response`/`get_tasks`/
  `update_task_status` functions, the dead `schemas/task.py`, the unused JSON endpoints,
  and (if AI scoring is not wanted) the `ai_priority_score`/`ai_risk_flag` columns via a
  migration. If AI/heuristic priority *is* wanted, instead wire `apply_simple_scoring`
  into `my_day_partial` and surface it — but pick one; don't leave it half-built.
- Fix the stale "Called by: routers/task.py" docstrings.

**Phase 4 — Make the Tasks page a real worklist.**
- M4 (eager-load) + M5 (bound "done").
- L1: assign auto-generated tasks to the requisition owner so they surface.
- L2: add snooze + reopen (+ quick edit) to the My Day row.
- L3: add a "New task" create affordance (needs the `ck_task_has_parent` product
  decision).

**Phase 5 — Consistency & simplification polish.**
- L4 (Medium badge + priority/assignee on CRM forms), L5 (one date filter, kill the
  in-template math), L6 (drop the broken `group-hover`), L7 (consolidate the four create
  functions), M6 (confirm/close the vendor-task authz policy).

---

## 5. Top 5 by severity (executive summary)

1. **[CRITICAL] Requisition "Tasks" tab is dead** — create/complete/delete all POST to
   `/api/requisitions/{id}/tasks*` endpoints that don't exist
   (`requisitions/tabs/tasks.html:23,83,151`). Every button 404s; the whole surface is
   non-functional.
2. **[HIGH] `create_part_task` binds a raw date string** to a timestamptz column
   (`parts.py:933`) — `UTCDateTime` passes strings through unnormalized; wrong-TZ on PG,
   `AttributeError` on SQLite/tests. Classic PG-vs-SQLite mask.
3. **[HIGH] Timezone-wrong due buckets** — overdue/"due today" computed on the UTC
   calendar day with no user timezone (`template_env.py:319`, `htmx_views.py:1899`).
4. **[HIGH] Filter/grouping contradiction** — "Due today" filter includes earlier-today
   tasks, but the list renders them under **Overdue** (`htmx_views.py:1899` vs
   `template_env.py:328`).
5. **[MEDIUM] ~200 lines of dead task-service code** — AI scoring, waiting-on,
   `task_to_response`, `get_tasks`, `update_task_status` are never called; the
   `ai_priority_*` columns are written by nothing (`task_service.py:668-834`).
