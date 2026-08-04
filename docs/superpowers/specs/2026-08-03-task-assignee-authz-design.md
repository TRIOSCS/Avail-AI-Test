# Task Assignee + Uniform Mutation Authz — Design

Approved in conversation 2026-08-03 (walkthrough + two recorded decisions).

## Requirements

1. **Every task has an assignee from creation.** No surface may create an
   unassigned task.
2. **The creator can always edit and delete their task**, under one uniform
   gate across all task surfaces.

## Decisions (locked with the user)

- **Permission model:** the existing shared gate, applied everywhere —
  **creator, assignee, admin, plus account owner for customer-scoped tasks**
  (i.e. today's `_is_crm_task_authorized` semantics, made universal).
- **Enforcement:** **app-layer required + one-time backfill.** The DB column
  stays nullable because `assigned_to_id` is FK `ondelete=SET NULL` — user
  deletion must keep working (tasks become unassigned, rare + admin-visible).
  Existing NULL-assignee rows are backfilled to their creator; rows whose
  creator is also NULL stay as-is (nothing to infer).
- **Requisition-board edit scope:** title + due date only, matching the CRM
  task edit form. Reassign/re-prioritize = delete + recreate. Extend later if
  missed.

## Current state (verified)

- Five create surfaces. Account/contact/vendor forms default assignee to the
  current user and the endpoints fall back `_safe_int(assigned_to_id) or
  user.id`; My Day self-assigns; auto-tasks default to
  `coalesce(claimed_by_id, created_by)` of the requisition. **Only the
  requisition Task board** (`app/templates/htmx/partials/requisitions/tabs/tasks.html`)
  offers an explicit `Unassigned` option accepted by
  `create_requisition_task_endpoint` (`app/routers/htmx/requisitions.py:1327`).
- Authz today: CRM/vendor task complete/edit/delete use
  `_is_crm_task_authorized` (`app/services/task_service.py:518`) — EXCEPT
  vendor-task **delete** which is admin-only (`app/routers/htmx/archive.py`,
  delete_task_endpoint). Requisition-board complete/delete
  (`app/routers/htmx/requisitions.py:1373/1401`) are gated only by
  `require_requisition_access` — any user with requisition access may mutate
  any task; there is **no requisition-board edit** at all. My Day
  complete/reopen are assignee-only (creator == assignee there; unchanged).

## Changes

1. **Service guard:** the five manual `create_*` functions in
   `app/services/task_service.py` require `assigned_to_id: int`; `None` →
   `ValueError("Assignee is required")`. `auto_create_task` keeps its default
   chain but **skips creation** (returns None, warn log) when the default
   resolves to nobody (deleted requisition edge).
2. **Board form:** drop the `Unassigned` option, pre-select the current user;
   endpoint 422s on missing/invalid assignee.
3. **Gate everywhere:** rename `_is_crm_task_authorized` →
   `is_task_mutation_authorized` (public). Apply to requisition-board
   complete + delete (on top of existing IDOR + access checks). Vendor-task
   delete drops the admin-only special case for the same gate.
4. **Board edit:** GET edit-form + POST edit endpoints (title + due only) and
   a row-shaped edit partial; pencil button on task rows; gate-protected;
   Cancel restores the row via a new GET row endpoint.
5. **Backfill:** data-only migration 204
   `UPDATE requisition_tasks SET assigned_to_id = created_by WHERE
   assigned_to_id IS NULL AND created_by IS NOT NULL`; no schema change;
   downgrade is a documented no-op. Number claimed in
   MIGRATION_NUMBERS_IN_FLIGHT.txt same commit; round-tripped on throwaway PG.

## Error handling

- Create without assignee: board endpoint → HTTP 422 "Assignee is required"
  (service ValueError is the backstop for any other caller).
- Unauthorized mutation: HTTP 403 with the surface's existing message style.
- Edit validation (empty title / bad date): re-render the edit form with the
  inline error, matching the CRM edit pattern.

## Testing

TDD throughout: service-level required-assignee + gate-decision tests; router
tests for the 422, the 403s (uninvolved-user on board complete/delete, creator
on vendor delete), the new edit endpoints (form render, save, cancel,
validation error, IDOR); migration chain test + PG round-trip. Full targeted
suite green before PR; CI runs the whole suite.

## Out of scope

DB NOT NULL, reassignment UI, priority edit, My Day changes, bulk actions.
