# Task 2 Report — Vendor Tasks + Migration 142

**Date:** 2026-06-24
**Branch:** feat/rubric-h1-vendor-parity
**Status:** DONE — 12/12 vendor task tests + 23 static analysis = 35 total, all green

---

## TDD Evidence

### RED Phase
```
TESTING=1 PYTHONPATH=$(pwd) /root/availai/.venv/bin/pytest tests/test_vendor_tasks.py -p no:cacheprovider -q --override-ini="addopts="
```
```
ERROR collecting tests/test_vendor_tasks.py
ImportError: cannot import name 'create_vendor_task' from 'app.services.task_service'
```
All 12 tests were blocked at import — confirmed RED.

### GREEN Phase
```
TESTING=1 PYTHONPATH=$(pwd) /root/availai/.venv/bin/pytest tests/test_vendor_tasks.py tests/test_static_analysis.py -p no:cacheprovider -q --override-ini="addopts="
```
```
35 passed, 2 warnings in 4.90s
```

One intermediate failure was caught during GREEN: `test_static_analysis.py::test_inline_button_sizing_does_not_grow` failed because `_vendor_task_form.html` Cancel button used `px-3 py-1` inline classes. Fixed to `.btn .btn-sm .btn-secondary` per project convention.

---

## Files Changed

### `alembic/versions/142_vendor_task_cols.py` (new)
- Drops `ck_task_has_parent` CHECK, adds `vendor_card_id` FK → `vendor_cards.id` (CASCADE)
  and `vendor_contact_id` FK → `vendor_contacts.id` (CASCADE).
- Indexes: `ix_rt_vendor_card_status`, `ix_rt_vendor_contact_status`.
- Recreates CHECK: all 5 parents — `requisition_id IS NOT NULL OR company_id IS NOT NULL OR
  site_contact_id IS NOT NULL OR vendor_card_id IS NOT NULL OR vendor_contact_id IS NOT NULL`.
- Downgrade: drops vendor columns + indexes, restores original 3-column CHECK.
- Chains onto `141_reclaim_cooldown`. Single head verified.

### `app/models/task.py` (modified)
- Added `vendor_card_id`, `vendor_contact_id` columns + `vendor_card`/`vendor_contact` relationships.
- Extended `CheckConstraint` expression to include new columns.
- Two new indexes added to `__table_args__`.

### `app/services/task_service.py` (modified)
- `create_vendor_task(db, *, vendor_card_id, title, ...)` — creates vendor-scoped task.
- `get_open_tasks_for_vendor_card(db, vendor_card_id)` — open tasks ordered by due_at asc (nulls last).
- `_is_crm_task_authorized` extended — vendor tasks allow any authenticated user for
  complete/edit; delete enforced separately (admin-only in the route).

### `app/routers/htmx_views.py` (modified)
- New routes: `GET /v2/partials/vendors/{id}/tasks`, `GET .../tasks/add-form`,
  `POST /v2/partials/vendors/{id}/tasks`.
- `vendor_tab` route: added `tasks` to valid_tabs + branch that renders `_vendor_tasks.html`.
- `complete_task_endpoint`: added vendor_card_id branch → renders `_vendor_tasks.html`.
- `delete_task_endpoint`: vendor task delete requires `user.role == admin`; re-renders
  `_vendor_tasks.html`. Non-admin gets 403. "Not a CRM task" guard extended to allow vendor tasks.
- `task_edit_form` and `edit_task_endpoint`: guard extended; vendor branch re-renders `_vendor_tasks.html`.

### `app/templates/htmx/partials/vendors/detail.html` (modified)
- Added `('tasks', 'Tasks')` to tab loop.

### `app/templates/htmx/partials/vendors/tabs/_vendor_tasks.html` (new)
- Open tasks list partial (mirrors `_account_tasks.html` structure with vendor-scoped URLs).
- Container id: `vendor-tasks-{vendor_id}` (used as HTMX swap target).

### `app/templates/htmx/partials/vendors/tabs/_vendor_task_form.html` (new)
- Inline add-task form. POSTs to `/v2/partials/vendors/{vendor_id}/tasks`.
- Buttons use `.btn .btn-sm` (passes static analysis ratchet).

### `MIGRATION_NUMBERS_IN_FLIGHT.txt` (modified)
- Claimed 142 on `feat/rubric-h1-vendor-parity`.

### `tests/test_vendor_tasks.py` (new)
12 tests covering:
- `test_vendor_card_only_satisfies_check` — vendor_card_id alone passes ck_task_has_parent
- `test_task_with_no_parent_still_raises` — all-NULL still violates CHECK
- `test_vendor_task_create` — service creates task with vendor_card_id, appears in list
- `test_vendor_task_excludes_done` — done tasks filtered out
- `test_vendor_task_complete` — completed_at set, zero ActivityLog rows written
- `test_get_vendor_tasks_tab` — GET returns 200 with `vendor-tasks-` container
- `test_post_vendor_task_creates_and_returns_list` — POST creates and renders list
- `test_post_vendor_task_missing_title` — validation error rendered
- `test_complete_vendor_task_no_activity_log` — HTTP complete writes no ActivityLog
- `test_vendor_task_delete_admin` — admin DELETE removes task
- `test_vendor_task_delete_nonadmin` — non-admin non-owner gets 403
- `test_migration_142_roundtrip` — ScriptDirectory chain + revision content validation

---

---

## Code-Review Fix Pass — 2026-06-24

### Finding 1 (Important) — Edit form broken for vendor tasks

**Root cause:** `task_edit_form` handler always returned `_task_edit_form.html` which
hardcodes `#contact-tasks-{task.site_contact_id}` as the HTMX target. For vendor tasks
both FK columns are `None`, yielding `#contact-tasks-None` (nonexistent DOM element).

**Fix:**
- Created `app/templates/htmx/partials/vendors/tabs/_vendor_task_edit_form.html` — edit
  form using `hx-target="#vendor-tasks-{vendor_id}"` and cancel URL
  `/v2/partials/vendors/{vendor_id}/tasks`.
- Modified `task_edit_form` handler (line ~14820): detects `is_vendor_task`, resolves
  `vendor_id` (direct from `vendor_card_id` or via `VendorContact.vendor_card_id`),
  renders the vendor template instead of the customer one.

### Finding 2 (Important) — vendor_contact-only task has no re-render path

**Root cause:** `complete_task_endpoint` and `delete_task_endpoint` both had
`if task.vendor_card_id:` branches but no `elif task.vendor_contact_id:` branch.
A task with only `vendor_contact_id` fell through to `return HTMLResponse("")`.

**Fix:**
- Added `if task.vendor_contact_id:` branch in `complete_task_endpoint` — walks
  `VendorContact.vendor_card_id` to find the parent, re-renders `_vendor_tasks.html`.
- Same fix in `delete_task_endpoint` — also captures `vendor_contact_id` before deletion.
- Also fixed `edit_task_endpoint` (which had the same gap).

### Finding 3 (Minor) — Migration docstring says "4-column CHECK"

**Fix:** Changed `alembic/versions/142_vendor_task_cols.py` docstring from
"restores original 4-column CHECK" → "restores original 3-column CHECK".

### Test additions
- `TestVendorTaskEditForm.test_vendor_task_edit_form_renders` — asserts response
  contains `vendor-tasks-{id}` and NOT `contact-tasks-None`.
- `TestVendorContactOnlyTaskEndpoints.test_vendor_contact_task_complete_rerenders`
- `TestVendorContactOnlyTaskEndpoints.test_vendor_contact_task_delete_rerenders`

### Test run
```
TESTING=1 PYTHONPATH=$(pwd) /root/availai/.venv/bin/pytest tests/test_vendor_tasks.py -p no:cacheprovider -q
15 passed, 18 warnings in 11.02s
```

### Files changed
- `alembic/versions/142_vendor_task_cols.py` — docstring fix (3-column, not 4)
- `app/routers/htmx_views.py` — `task_edit_form`, `complete_task_endpoint`,
  `delete_task_endpoint`, `edit_task_endpoint` vendor_contact_id branches
- `app/templates/htmx/partials/vendors/tabs/_vendor_task_edit_form.html` — new template
- `tests/test_vendor_tasks.py` — 3 new test cases (fixture `vendor_contact` also fixed:
  `name` → `full_name`, added `source="manual"`)

---

---

## Code-Review Fix Pass 2 — innerHTML Cancel Button — 2026-06-24

### Finding (Important) — Add form Cancel uses banned innerHTML

**Root cause:** `_vendor_task_form.html` Cancel button used
`onclick="document.getElementById(...).innerHTML = ''"` — a banned pattern
per CLAUDE.md (`innerHTML → use htmx.ajax() or Alpine reactive binding`).

**Fix:** Replaced the `onclick` with `hx-get="/v2/partials/vendors/{{ vendor_id }}/tasks"` +
`hx-target="#vendor-tasks-{{ vendor_id }}"` + `hx-swap="outerHTML"`. Matches the
edit form cancel button exactly (same endpoint, same target).

### File changed
- `app/templates/htmx/partials/vendors/tabs/_vendor_task_form.html` — Cancel button

### Test run
```
TESTING=1 PYTHONPATH=$(pwd) pytest tests/test_vendor_tasks.py tests/test_static_analysis.py -p no:cacheprovider -q
38 passed, 18 warnings in 10.54s
```
Ruff: All checks passed.

### Commit
`a473584c` — fix: replace innerHTML cancel with htmx-native hx-get in vendor task add form

---

## Summary (15 lines)

- **Status:** DONE
- **Commit:** `301875b1` — feat: vendor tasks + migration 142 (vendor parity)
- **Test summary:** 35/35 pass (12 new vendor task tests + 23 static analysis)
- **Migration revision id:** `142_vendor_task_cols` (24 chars, ≤ 32 limit)
- **No ActivityLog invariant:** maintained — `complete_crm_task` never calls `log_activity`
- **Admin gate:** enforced in `delete_task_endpoint` via `user.role != UserRole.ADMIN` → 403
- **Concerns:** None. All pre-commit hooks pass (ruff, ruff-format, docformatter, mypy).
