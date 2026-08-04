# Task Assignee + Uniform Authz Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every task is created with an assignee, and one gate (creator | assignee | admin | account-owner-for-customer-tasks) governs edit/delete/complete on every task surface.

**Architecture:** All enforcement lands in `app/services/task_service.py` (fat services); routers only translate `ValueError`→422 and gate refusals→403. The requisition board gains a title+due edit reusing the CRM edit pattern. One data-only backfill migration.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 sync, Jinja2+HTMX, Alembic, pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-03-task-assignee-authz-design.md` (committed alongside this plan).
- TDD: every behavior change shows its test red first (paste the red output in the commit message body or PR notes).
- Run pytest with cwd INSIDE the worktree, `/root/availai/.venv/bin/python -m pytest`.
- `pre-commit run --files <changed>` before every commit; hooks may rewrite files — re-add and re-commit.
- Loguru only; header docstring on any new file; no TBDs.
- Migration revision ids ≤32 chars. Claim the number in `MIGRATION_NUMBERS_IN_FLIGHT.txt` in the same commit as the migration.
- Do not touch prod DB / .env / containers. Migration round-trip uses a THROWAWAY postgres:16 container.

---

### Task 1: Service-layer required assignee

**Files:**
- Modify: `app/services/task_service.py` (create_task:57, create_requisition_task:97, create_personal_task:182, create_company_task:390, create_contact_task:418, create_vendor_task:476, auto_create_task:621)
- Test: `tests/test_task_assignee_required.py` (new)

**Interfaces:**
- Produces: the five manual `create_*` fns take `assigned_to_id: int` (still keyword; `None` raises `ValueError("Assignee is required")`). `auto_create_task` returns `None` (warn log) when its default-assignee chain resolves to nobody.

- [ ] **Step 1: Write failing tests** (new file, header docstring; use existing fixtures `db_session`, plus a requisition/user built the way `tests/test_ownership_sites_scope.py:_make_user` does — copy that helper):

```python
@pytest.mark.parametrize("fn,extra", [
    (task_service.create_requisition_task, {}),           # + requisition_id
    (task_service.create_company_task, {}),               # + company_id
    (task_service.create_contact_task, {}),               # + site_contact_id
    (task_service.create_vendor_task, {}),                # + vendor_card_id
])
def test_manual_create_requires_assignee(db_session, scene, fn, extra):
    with pytest.raises(ValueError, match="Assignee is required"):
        fn(db_session, title="T", assigned_to_id=None, created_by=scene.user.id, **scene.parent_kwargs(fn), **extra)

def test_auto_create_skips_when_no_default_assignee(db_session):
    # requisition_id that doesn't resolve -> _default_auto_assignee None -> skip
    out = task_service.auto_create_task(db_session, requisition_id=999999,
                                        title="T", task_type="general", source_ref="offer:1")
    assert out is None
```

- [ ] **Step 2: Run — expect FAIL** (`ValueError` not raised; auto path currently persists an unassigned task).
- [ ] **Step 3: Implement.** In each of the five manual creates, first line of body:

```python
    if assigned_to_id is None:
        raise ValueError("Assignee is required")
```

Keep the parameter keyword-typed `assigned_to_id: int | None = None` (callers that omit it get the error, mypy stays green). In `auto_create_task`, after the existing default resolution:

```python
    if assigned_to_id is None:
        assigned_to_id = _default_auto_assignee(db, requisition_id)
    if assigned_to_id is None:
        logger.warning("auto_create_task: no resolvable assignee for req {} ({}); skipping", requisition_id, source_ref)
        return None
```

- [ ] **Step 4: Run — expect PASS.** Also run `tests/test_ownership_service.py tests/test_services_ownership.py` plus every test file that imports task_service (`grep -rl task_service tests/`) to catch callers that passed None.
- [ ] **Step 5: pre-commit + commit** `feat(tasks): require an assignee on every manual task create; auto-tasks skip when unresolvable`

### Task 2: Board form + endpoint enforce assignee

**Files:**
- Modify: `app/routers/htmx/requisitions.py:1343-1357` (create endpoint), `app/templates/htmx/partials/requisitions/tabs/tasks.html:53-58` (assignee select)
- Test: `tests/test_task_assignee_required.py` (extend)

**Interfaces:**
- Consumes: Task 1's ValueError.
- Produces: POST `/api/requisitions/{req_id}/tasks` → 422 `"Assignee is required"` when the field is empty/invalid.

- [ ] **Step 1: Failing test** — authed client POST to the create route with `title=X` and `assigned_to_id=""` expects 422 (today: 200 + unassigned task). Follow the auth/client pattern of the nearest existing test hitting this route (`grep -rn "tasks" tests/ | grep requisitions`).
- [ ] **Step 2: Run — expect FAIL (200 today).**
- [ ] **Step 3: Implement.** In the endpoint, after title validation:

```python
    assigned_to_id = _parse_int_or_none(form.get("assigned_to_id"))
    if assigned_to_id is None:
        raise HTTPException(422, "Assignee is required")
```

pass `assigned_to_id=assigned_to_id` through. In `tasks.html` replace the select body:

```html
        <select name="assigned_to_id" class="px-3 py-1.5 border border-brand-200 rounded-lg text-sm input-focus">
          {% for u in users %}
          <option value="{{ u.id }}"{{ ' selected' if u.id == user.id else '' }}>{{ u.name }}</option>
          {% endfor %}
        </select>
```

(`user` is in ctx via `_base_ctx` — verify once in the tab-render endpoint.)
- [ ] **Step 4: Run — PASS; also re-run the board's existing create tests.**
- [ ] **Step 5: pre-commit + commit** `feat(tasks): requisition board requires an assignee (form default = current user, 422 backstop)`

### Task 3: One gate everywhere (board complete/delete + vendor delete)

**Files:**
- Modify: `app/services/task_service.py:518` (rename `_is_crm_task_authorized` → `is_task_mutation_authorized`; keep signature `(db, task, user_id, is_admin)`), `app/routers/htmx/archive.py` (two importers + delete vendor special-case), `app/routers/htmx/requisitions.py:1373-1421`
- Test: `tests/test_task_mutation_authz.py` (new)

**Interfaces:**
- Produces: `task_service.is_task_mutation_authorized(db, task, user_id, is_admin) -> bool` — the only mutation gate.

- [ ] **Step 1: Failing tests:** (a) uninvolved user WITH requisition access → POST board complete → 403 (today 200); (b) same for DELETE board task; (c) creator (non-admin) deletes their own vendor task via `/v2/partials/tasks/{id}` → 200 (today 403); (d) creator/assignee/admin still succeed on board complete/delete.
- [ ] **Step 2: Run — expect (a)(b)(c) FAIL.**
- [ ] **Step 3: Implement.** Rename + update the two `archive.py` imports; in `archive.py` delete endpoint remove the `if is_vendor_task and not is_crm_task:` admin-only branch so every path uses the gate. In both board endpoints, after the existing IDOR check:

```python
    if not is_task_mutation_authorized(db, task, user.id, is_admin=(user.role == UserRole.ADMIN)):
        raise HTTPException(403, "Only the task's creator, assignee, or an admin can modify it")
```

(import `UserRole` from `...constants` if not present; import the gate alongside the existing task_service imports at line 44).
- [ ] **Step 4: Run new file + `tests/test_routers_vendor_contacts.py` + any archive/CRM task tests (`grep -rln "partials/tasks" tests/`). PASS.**
- [ ] **Step 5: pre-commit + commit** `feat(tasks): one mutation gate everywhere — board complete/delete gated, vendor delete opens to creator`

### Task 4: Requisition-board edit (title + due)

**Files:**
- Create: `app/templates/htmx/partials/requisitions/tabs/_task_edit_form.html`
- Modify: `app/routers/htmx/requisitions.py` (3 new endpoints after the delete endpoint), `app/templates/htmx/partials/requisitions/tabs/_task_row.html` (pencil button next to the delete button — read the full file first and mirror its hover-reveal classes)
- Test: `tests/test_req_task_edit.py` (new)

**Interfaces:**
- Consumes: `is_task_mutation_authorized` (Task 3).
- Produces: `GET /api/requisitions/{req_id}/tasks/{task_id}/row` (row re-render, used by Cancel), `GET .../edit-form`, `POST .../edit`.

- [ ] **Step 1: Failing tests:** creator GET edit-form → 200 containing `name="title"`; uninvolved user → 403; POST edit with new title+due → 200, row contains new title, DB updated; POST empty title → 200 re-rendering the edit form with `Title is required.`; task from another requisition (IDOR) → 404.
- [ ] **Step 2: Run — FAIL (404s: routes don't exist).**
- [ ] **Step 3: Implement.** All three endpoints follow the complete-endpoint skeleton (req 404 → access → task IDOR 404 → gate 403). POST edit parses due via the existing `_parse_task_due_date`, sets `task.title`/`task.due_at` directly and commits (empty due clears — same explicit-set rationale as `archive.py` edit), returns `_task_row.html` with ctx `{req, t}`. GET edit-form / error branch render the new partial with ctx `{req, t, error}`. The partial is row-shaped (outerHTML-swaps `#task-{{ t.id }}`):

```html
<div id="task-{{ t.id }}" class="p-3 bg-white rounded-lg border border-brand-300">
  <form hx-post="/api/requisitions/{{ req.id }}/tasks/{{ t.id }}/edit"
        hx-target="#task-{{ t.id }}" hx-swap="outerHTML" class="flex items-center gap-2">
    <input type="text" name="title" value="{{ t.title }}" required aria-label="Task title"
           class="flex-1 px-2 py-1 text-sm border border-gray-300 rounded input-focus">
    <input type="date" name="due_at" aria-label="Due date"
           value="{{ t.due_at.strftime('%Y-%m-%d') if t.due_at else '' }}"
           class="px-2 py-1 text-sm border border-gray-300 rounded input-focus">
    <button type="submit" class="btn btn-primary btn-sm">Save</button>
    <button type="button" class="btn btn-secondary btn-sm"
            hx-get="/api/requisitions/{{ req.id }}/tasks/{{ t.id }}/row"
            hx-target="#task-{{ t.id }}" hx-swap="outerHTML">Cancel</button>
  </form>
  {% if error %}<p class="mt-1 text-xs text-rose-600">{{ error }}</p>{% endif %}
</div>
```

Pencil in `_task_row.html` (before the delete button, same hover-reveal treatment):

```html
  <button hx-get="/api/requisitions/{{ req.id }}/tasks/{{ t.id }}/edit-form"
          hx-target="#task-{{ t.id }}" hx-swap="outerHTML"
          class="flex-shrink-0 opacity-0 group-hover:opacity-100 text-gray-400 hover:text-brand-600 transition-all"
          title="Edit task" aria-label="Edit task">
    <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
      <path stroke-linecap="round" stroke-linejoin="round" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/>
    </svg>
  </button>
```

- [ ] **Step 4: Run — PASS; headless-verify the pencil renders (htmx attrs present in row HTML).**
- [ ] **Step 5: pre-commit + commit** `feat(tasks): requisition-board task edit (title + due), gate-protected`

### Task 5: Backfill migration 204

**Files:**
- Create: `alembic/versions/204_backfill_task_assignee.py`
- Modify: `MIGRATION_NUMBERS_IN_FLIGHT.txt` (append claim line, same commit)

- [ ] **Step 1:** migration (header docstring; `revision = "204_backfill_task_assignee"`, `down_revision = "203_outreach_recipient_email"`):

```python
def upgrade() -> None:
    op.execute(
        "UPDATE requisition_tasks SET assigned_to_id = created_by "
        "WHERE assigned_to_id IS NULL AND created_by IS NOT NULL"
    )

def downgrade() -> None:
    # Data-only backfill; the pre-backfill NULL set is not recoverable. No-op.
    pass
```

Claim line: `204  feat/task-assignee-authz  Data-only backfill: requisition_tasks.assigned_to_id = created_by where NULL (app layer now requires an assignee at create; column stays nullable for user-deletion SET NULL). Downgrade documented no-op. Chains onto 203_outreach_recipient_email.`
- [ ] **Step 2:** `pytest tests/test_migration_chain.py tests/test_migration_numbers_in_flight.py` → PASS (single head 204).
- [ ] **Step 3:** Round-trip on throwaway PG16 (`docker run --rm -d --name task_authz_rt -e POSTGRES_PASSWORD=x -p 55440:5432 postgres:16`, `DATABASE_URL=postgresql://postgres:x@localhost:55440/postgres alembic upgrade head`, seed one NULL-assignee task with a creator + one with neither, `downgrade -1`, `upgrade head`, assert the first got created_by and the second stayed NULL; then remove the container). Paste the run output in the PR notes.
- [ ] **Step 4: pre-commit + commit** `feat(tasks): migration 204 — backfill task assignee from creator`

### Task 6: Full verification + push

- [ ] Run the targeted suite: the three new test files + every file touched in Tasks 1-5's Step-4 lists, `-n auto`. All green.
- [ ] `pre-commit run --all-files` (big-PR rule). Fix and re-commit if hooks rewrite.
- [ ] Push `feat/task-assignee-authz`. Do NOT open/merge a PR — report branch + head SHA + the real test tails.

## Self-review (done at write time)

Spec coverage: R1→Tasks 1-2+5, R2→Tasks 3-4; error handling→Tasks 2-4 steps; testing→each task. No placeholders; gate name `is_task_mutation_authorized` consistent across Tasks 3-4; migration id 26 chars.
