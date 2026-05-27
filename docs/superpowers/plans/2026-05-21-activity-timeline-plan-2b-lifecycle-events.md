# Activity Timeline — Plan 2b: Lifecycle Events

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Task completion, requisition assignment, requisition archive/unarchive, and sales-note edits each write an `activity_log` row through the canonical `log_activity()` writer, so these lifecycle events appear on the requisition Activity tab.

**Architecture:** Continues build step 2 (the non-offer half; offers are Plan 2a). Adds `log_activity()` calls at task/assignment/archive/note mutation points. The two existing **manual** `ActivityLog` writes in `claim_requisition()` / `unclaim_requisition()` (raw `activity_type` strings, not enum) are migrated onto `log_activity()` + the `ActivityType` enum. One additive enum member, `REQ_UNARCHIVED`, is added (archive and unarchive are distinct timeline events). No schema migration.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0, pytest (in-memory SQLite), Loguru.

**Spec:** `docs/superpowers/specs/2026-05-20-activity-timeline-design.md` (build step 2, non-offer portion). Sightings are deferred to Plan 4 (batch aggregation belongs with the curation layer) per the design decision of 2026-05-21.

**Branch:** Create `feat/activity-timeline-2b` off `feat/activity-timeline-2a` (Plan 2a) — or off `main` if Plan 1 + 2a have merged.

---

## Conventions for every task

**The canonical writer** — `log_activity()` in `app/services/activity_service.py`:
`log_activity(db, *, activity_type, channel="system", requisition_id=None, requirement_id=None, user_id=None, company_id=None, vendor_card_id=None, vendor_contact_id=None, description=None, summary=None, occurred_at=None, details=None) -> ActivityLog`. It calls `db.flush()`, not `db.commit()` — the caller commits.

**Rules for every task:**
- Imports: add `from <...>constants import ActivityType` and `from <...>services.activity_service import log_activity` — **match each file's existing import style and depth**. Check whether either name is already imported before adding. **`app/routers/htmx_views.py` defines a route function literally named `log_activity`** — in that file, import the service as `from ..services.activity_service import log_activity as _log_activity` and call `_log_activity(...)`, OR use a function-local import; do NOT shadow the route function. Verify with `grep -n "def log_activity" app/routers/htmx_views.py`.
- **Verify every function name, line number, and variable name against the current file before editing.** This plan's site list came from an automated survey that proved partly inaccurate during Plan 2a (wrong function names). Treat the names below as starting points: confirm with `grep`, and if a named function does not exist, find the real mutation point by searching for the field assignment (`task.status =`, `claimed_by_id`, `.status = RequisitionStatus.ARCHIVED`, `sale_notes =`). If a site genuinely cannot be found, report NEEDS_CONTEXT.
- The `log_activity()` call goes **after** the mutation, and the enclosing handler's existing `db.commit()` persists it (`log_activity` only flushes). For bulk-`.update()` sites, see Task 4's pattern.
- TDD: write the failing test first, run it, confirm it fails for the expected reason, then implement.
- Tests run: `TESTING=1 PYTHONPATH=/root/availai pytest <file> -v --override-ini="addopts="`. A widespread failure set that also appears on a `git stash` baseline is pre-existing xdist test-pollution — report it, don't try to fix it.
- Loguru not print; Ruff clean; follow existing patterns. Each task ends in its own commit. Do not push or open a PR until the plan owner approves.

**Shared test helper** — Task 1 creates `tests/test_lifecycle_activity_logging.py` with this header + helper; later tasks append to the same file:

```python
"""test_lifecycle_activity_logging.py — lifecycle events write activity_log rows.

Covers Plan 2b: task_completed, assignment_changed, req_archived/req_unarchived,
and sales_note events route through activity_service.log_activity().

Called by: pytest
Depends on: app/services/activity_service.py, app/constants.py, conftest.py
"""

from app.constants import ActivityType
from app.models import ActivityLog


def _activity_rows(db, requisition_id, activity_type):
    return (
        db.query(ActivityLog)
        .filter(
            ActivityLog.requisition_id == requisition_id,
            ActivityLog.activity_type == activity_type,
        )
        .all()
    )
```

---

### Task 1: `task_completed`

When a requisition task is marked done, log a `task_completed` activity. There are two independent code paths (the route handler does **not** delegate to the service) — instrument both.

**Sites (verify names/lines with `grep -n "def mark_task_done\|def complete_task\|TaskStatus.DONE\|status = .*DONE" app/routers/htmx_views.py app/services/task_service.py`):**
- `app/routers/htmx_views.py` — `mark_task_done()`: sets `task.status = TaskStatus.DONE` (~line 9628). In scope: `db`, `user.id`, `task.requisition_id`, `task.requirement_id`.
- `app/services/task_service.py` — `complete_task()`: sets the same (~line 194). In scope: `db`, `user_id` (param — verify), `task.requisition_id`, `task.requirement_id`.

Do **not** instrument `reopen_task()` — reopening is not a completion event.

**Files:**
- Modify: `app/routers/htmx_views.py`
- Modify: `app/services/task_service.py`
- Test: `tests/test_lifecycle_activity_logging.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_lifecycle_activity_logging.py` with the header + `_activity_rows` helper shown in the Conventions section, then append:

```python
def test_complete_task_logs_task_completed(db_session, test_requisition, test_user):
    """Completing a task writes a task_completed activity row on its requisition."""
    from app.models import RequisitionTask  # verify the model name/location
    from app.services.task_service import complete_task

    task = RequisitionTask(
        requisition_id=test_requisition.id,
        title="Follow up with vendor",
        created_by=test_user.id,
    )
    db_session.add(task)
    db_session.flush()

    complete_task(db=db_session, task_id=task.id, user_id=test_user.id)
    db_session.commit()

    rows = _activity_rows(db_session, test_requisition.id, ActivityType.TASK_COMPLETED)
    assert len(rows) == 1
```

Before writing, **verify**: the task model's real class name and module (`grep -rn "class .*Task" app/models/`), its required non-nullable fields (adjust the constructor), and the real signature of `complete_task()` (param names/order). Rewrite the test to match. If `complete_task` requires the user to be the task assignee, set `assigned_to_id=test_user.id` on the task.

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_lifecycle_activity_logging.py -v --override-ini="addopts="`
Expected: FAIL — `assert len(rows) == 1` fails (no `task_completed` row).

- [ ] **Step 3: Instrument `complete_task()` in `app/services/task_service.py`**

Add imports (`from ..constants import ActivityType`, `from .activity_service import log_activity` — match file style). Immediately after the `task.status = ...DONE` assignment (and `completed_at` set), before the function's commit/return, insert:

```python
    log_activity(
        db,
        activity_type=ActivityType.TASK_COMPLETED,
        requisition_id=task.requisition_id,
        requirement_id=task.requirement_id,
        user_id=user_id,
        description=f"Task completed: {task.title}",
        details={"task_id": task.id},
    )
```

Match indentation; use the real actor variable (`user_id` or `user.id`) and the real title attribute.

- [ ] **Step 4: Instrument `mark_task_done()` in `app/routers/htmx_views.py`**

Add imports — use the `log_activity as _log_activity` alias (or a function-local import) because `htmx_views.py` has a route named `log_activity`. After `task.status = ...DONE`, before `db.commit()`, insert the same call (using `_log_activity`, `user.id`):

```python
    _log_activity(
        db,
        activity_type=ActivityType.TASK_COMPLETED,
        requisition_id=task.requisition_id,
        requirement_id=task.requirement_id,
        user_id=user.id,
        description=f"Task completed: {task.title}",
        details={"task_id": task.id},
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_lifecycle_activity_logging.py tests/test_task_service.py -v --override-ini="addopts="`
Expected: PASS — new test passes; existing task tests still pass.

- [ ] **Step 6: Commit**

```bash
git add app/routers/htmx_views.py app/services/task_service.py tests/test_lifecycle_activity_logging.py
git commit -m "feat: log task_completed activity on task completion"
```

---

### Task 2: `assignment_changed` — migrate claim/unclaim + add batch assign

`claim_requisition()` and `unclaim_requisition()` in `app/services/requirement_status.py` already write `ActivityLog` rows **directly**, with raw `activity_type` strings (`"requisition_claimed"` / `"requisition_unclaimed"`) that are not in the `ActivityType` enum. Migrate both onto `log_activity()` + `ActivityType.ASSIGNMENT_CHANGED`, with the claim/unclaim direction in `description`/`details`. Then add coverage to `batch_assign()`.

**Sites (verify with `grep -n "def claim_requisition\|def unclaim_requisition\|def batch_assign\|claimed_by_id\|activity_type=" app/services/requirement_status.py app/routers/requisitions/core.py`):**
- `app/services/requirement_status.py` — `claim_requisition()`: sets `claimed_by_id`; has a manual `ActivityLog(...)` block (~lines 150-157). **Replace** that block with a `log_activity()` call.
- `app/services/requirement_status.py` — `unclaim_requisition()`: sets `claimed_by_id=None`; has a manual `ActivityLog(...)` block (~lines 175-182). **Replace** it.
- `app/routers/requisitions/core.py` — `batch_assign()`: does a bulk `.update({"claimed_by_id": ...})`. **No rows are loaded.** Before the `.update()`, capture the affected requisition ids with a lightweight `SELECT` (`ids = [r.id for r in q.with_entities(Requisition.id).all()]` — adapt to the query variable in scope), run the existing `.update()`, then loop `log_activity()` per id.

**Files:**
- Modify: `app/services/requirement_status.py`
- Modify: `app/routers/requisitions/core.py`
- Test: `tests/test_lifecycle_activity_logging.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lifecycle_activity_logging.py`:

```python
def test_claim_requisition_logs_assignment_changed(db_session, test_requisition, test_user):
    """Claiming a requisition writes an assignment_changed activity row."""
    from app.services.requirement_status import claim_requisition

    claim_requisition(test_requisition, test_user, db_session)
    db_session.commit()

    rows = _activity_rows(db_session, test_requisition.id, ActivityType.ASSIGNMENT_CHANGED)
    assert len(rows) == 1
    # the legacy raw-string activity_type must no longer be written
    legacy = (
        db_session.query(ActivityLog)
        .filter(ActivityLog.activity_type == "requisition_claimed")
        .all()
    )
    assert legacy == []
```

Before writing, **verify** the `claim_requisition()` signature (the survey says `claim_requisition(requisition, buyer, db)` — confirm param order/names) and adjust the call.

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_lifecycle_activity_logging.py -k assignment -v --override-ini="addopts="`
Expected: FAIL — the row exists but with `activity_type="requisition_claimed"`, so the `ActivityType.ASSIGNMENT_CHANGED` query returns 0 and/or the `legacy == []` assertion fails.

- [ ] **Step 3: Migrate `claim_requisition()` and `unclaim_requisition()`**

Add imports (`from ..constants import ActivityType`, `from .activity_service import log_activity` — match file style). In `claim_requisition()`, **delete** the manual `ActivityLog(...)` + `db.add(...)` block and replace it with:

```python
    log_activity(
        db,
        activity_type=ActivityType.ASSIGNMENT_CHANGED,
        requisition_id=locked.id,
        user_id=buyer.id,
        description=f"Requisition claimed by {buyer.name or buyer.email}",
        details={"action": "claimed", "claimed_by_id": buyer.id},
    )
```

(Use the real requisition variable — the survey shows `locked` — and the real actor param `buyer`.) In `unclaim_requisition()`, replace its manual block with:

```python
    log_activity(
        db,
        activity_type=ActivityType.ASSIGNMENT_CHANGED,
        requisition_id=requisition.id,
        user_id=<actor id in scope, or None>,
        description="Requisition unclaimed",
        details={"action": "unclaimed"},
    )
```

Verify what actor variable `unclaim_requisition()` has in scope; if none, pass `user_id=None`.

- [ ] **Step 4: Instrument `batch_assign()` in `app/routers/requisitions/core.py`**

Add imports (`from ...constants import ActivityType`, `from ...services.activity_service import log_activity` — match style). Locate the bulk `.update({"claimed_by_id": ...})`. Immediately **before** the `.update()`, capture the target ids; **after** the `.update()`, loop:

```python
    target_ids = [row.id for row in q.with_entities(Requisition.id).all()]
    count = q.update({"claimed_by_id": payload.owner_id}, synchronize_session=False)
    for rid in target_ids:
        log_activity(
            db,
            activity_type=ActivityType.ASSIGNMENT_CHANGED,
            requisition_id=rid,
            user_id=user.id,
            description=f"Requisition assignment changed (batch) — owner {payload.owner_id}",
            details={"action": "batch_assigned", "claimed_by_id": payload.owner_id},
        )
```

Adapt `q`, `payload.owner_id`, and `user.id` to the real variable names in that handler. Keep the existing `.update()` call and its return value.

- [ ] **Step 5: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_lifecycle_activity_logging.py tests/test_requisitions_core_coverage.py -v --override-ini="addopts="`
Expected: PASS — new test passes; existing requisition tests still pass. If an existing test asserted the old `"requisition_claimed"` string, update that test to expect `ActivityType.ASSIGNMENT_CHANGED` (the migration intentionally changes the value — `grep -rn "requisition_claimed\|requisition_unclaimed" tests/` and fix any such assertions, listing them in the commit).

- [ ] **Step 6: Commit**

```bash
git add app/services/requirement_status.py app/routers/requisitions/core.py tests/test_lifecycle_activity_logging.py
git commit -m "feat: route claim/unclaim/batch-assign through log_activity (assignment_changed)"
```

---

### Task 3: `req_archived` / `req_unarchived` — single-row + loop sites + enum member

Add an additive `REQ_UNARCHIVED` enum member, then instrument the archive/unarchive sites that operate on loaded ORM rows.

**Files:**
- Modify: `app/constants.py`
- Modify: `app/routers/requisitions/core.py`
- Modify: `app/routers/htmx_views.py`
- Test: `tests/test_lifecycle_activity_logging.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lifecycle_activity_logging.py`:

```python
def test_toggle_archive_logs_req_archived(client, db_session, test_requisition):
    """Archiving a requisition writes a req_archived activity row."""
    resp = client.put(f"/api/requisitions/{test_requisition.id}/archive")
    assert resp.status_code == 200, resp.text
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.REQ_ARCHIVED)
    assert len(rows) == 1
```

Before writing, **verify** the archive route path/method (`grep -n "def toggle_archive" -B3 app/routers/requisitions/core.py`) and whether it is a true toggle (archives if active, unarchives if archived). Adjust the request. If a second request would unarchive, optionally add a follow-up assertion for `ActivityType.REQ_UNARCHIVED`.

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_lifecycle_activity_logging.py -k archive -v --override-ini="addopts="`
Expected: FAIL — `ImportError` for `ActivityType.REQ_UNARCHIVED` (not defined yet) or `assert len(rows) == 1` fails.

- [ ] **Step 3: Add the `REQ_UNARCHIVED` enum member**

In `app/constants.py`, in the `ActivityType` StrEnum, add after `REQ_ARCHIVED`:

```python
    REQ_UNARCHIVED = "req_unarchived"
```

(`req_unarchived` is 14 chars — fits the `activity_type` `String(20)` column.)

- [ ] **Step 4: Instrument the loaded-row archive sites**

For each site below, add imports (match file style; use the `_log_activity` alias in `htmx_views.py`). After the status mutation, before the handler's `db.commit()`, insert a `log_activity()` / `_log_activity()` call with `activity_type=ActivityType.REQ_ARCHIVED` when archiving and `ActivityType.REQ_UNARCHIVED` when unarchiving:

```python
        log_activity(
            db,
            activity_type=ActivityType.REQ_ARCHIVED,  # or REQ_UNARCHIVED
            requisition_id=<req>.id,
            user_id=user.id,
            description="Requisition archived",  # or "Requisition unarchived"
        )
```

Sites (verify each with grep):
- `app/routers/requisitions/core.py` — `toggle_archive()`: it sets `RequisitionStatus.ARCHIVED` (and, if a toggle, `ACTIVE` on the other branch). Log `REQ_ARCHIVED` on the archive branch and `REQ_UNARCHIVED` on the unarchive branch.
- `app/routers/htmx_views.py` — `archive_part()` (~line 9722, sets parent req to ARCHIVED): log `REQ_ARCHIVED`.
- `app/routers/htmx_views.py` — the unarchive handler (~line 9743, sets `RequisitionStatus.ACTIVE`): log `REQ_UNARCHIVED`.
- `app/routers/htmx_views.py` — `bulk_part_actions()` (~line 1567): this loops over loaded `reqs`. For each requisition whose status is set to `ARCHIVED` in the loop, add a `_log_activity(... REQ_ARCHIVED ...)` call inside the loop.

- [ ] **Step 5: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_lifecycle_activity_logging.py tests/test_requisitions_core_coverage.py -v --override-ini="addopts="`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/constants.py app/routers/requisitions/core.py app/routers/htmx_views.py tests/test_lifecycle_activity_logging.py
git commit -m "feat: log req_archived/req_unarchived from loaded-row archive paths"
```

---

### Task 4: `req_archived` — bulk-update archive sites

`bulk_archive()` and `batch_archive_by_ids()` in `app/routers/requisitions/core.py` archive via a bare `.update({"status": "archived"})` with no rows loaded. Capture the affected ids before the update, then log per id — same pattern as Task 2's `batch_assign`.

**Files:**
- Modify: `app/routers/requisitions/core.py`
- Test: `tests/test_lifecycle_activity_logging.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lifecycle_activity_logging.py`:

```python
def test_batch_archive_logs_req_archived(client, db_session, test_requisition):
    """Batch-archiving requisitions by id writes a req_archived row for each."""
    resp = client.post("/api/requisitions/batch-archive", json={"ids": [test_requisition.id]})
    assert resp.status_code == 200, resp.text
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.REQ_ARCHIVED)
    assert len(rows) == 1
```

Before writing, **verify** the batch-archive route — exact path, method, and request body shape (`grep -n "def batch_archive_by_ids\|def bulk_archive" -B3 app/routers/requisitions/core.py`). Pick whichever of the two endpoints is cleanly drivable via the test client and adjust the request to its real contract. (Both get instrumented in Step 3 regardless of which the test drives.)

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_lifecycle_activity_logging.py -k batch_archive -v --override-ini="addopts="`
Expected: FAIL — no `req_archived` row.

- [ ] **Step 3: Instrument both bulk-archive handlers**

In `bulk_archive()` and `batch_archive_by_ids()`, immediately before the bulk `.update()`, capture the ids being archived; after the `.update()`, loop `log_activity()`:

```python
    target_ids = [row.id for row in q.with_entities(Requisition.id).all()]
    count = q.update({"status": RequisitionStatus.ARCHIVED}, synchronize_session=False)
    for rid in target_ids:
        log_activity(
            db,
            activity_type=ActivityType.REQ_ARCHIVED,
            requisition_id=rid,
            user_id=user.id,
            description="Requisition archived (bulk)",
        )
```

Adapt `q` and `user.id` to each handler's real variable names. Keep the existing `.update()` and its return value. (Imports were added to this file in Task 2/3; reuse them.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_lifecycle_activity_logging.py tests/test_requisitions_core_coverage.py -v --override-ini="addopts="`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routers/requisitions/core.py tests/test_lifecycle_activity_logging.py
git commit -m "feat: log req_archived from bulk-archive endpoints"
```

---

### Task 5: `sales_note` + APP_MAP doc

When a requirement's `sale_notes` field is edited, log a `sales_note` activity. Two sites.

**Sites (verify with `grep -n "def update_requirement\|def save_part_notes\|sale_notes" app/routers/requisitions/requirements.py app/routers/htmx_views.py`):**
- `app/routers/requisitions/requirements.py` — `update_requirement()`: sets `r.sale_notes = ...` (~line 654). In scope: `db`, `user.id`, the requirement `r`, the requisition (resolved earlier in the handler). Log only when `sale_notes` actually changed.
- `app/routers/htmx_views.py` — `save_part_notes()`: sets `req.sale_notes = ...` (~line 9572). `requisition_id` may need to be read off the requirement object — verify.

**Files:**
- Modify: `app/routers/requisitions/requirements.py`
- Modify: `app/routers/htmx_views.py`
- Modify: `docs/APP_MAP_INTERACTIONS.md`
- Test: `tests/test_lifecycle_activity_logging.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lifecycle_activity_logging.py`:

```python
def test_save_part_notes_logs_sales_note(client, db_session, test_requisition):
    """Editing a requirement's sale notes writes a sales_note activity row."""
    requirement = test_requisition.requirements[0]
    resp = client.patch(
        f"/v2/partials/parts/{requirement.id}/notes",
        data={"sale_notes": "Customer wants expedited quote"},
    )
    assert resp.status_code == 200, resp.text
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.SALES_NOTE)
    assert len(rows) == 1
```

Before writing, **verify** the `save_part_notes()` route path/method and form field name, and how the `test_requisition` fixture exposes its requirement(s). Adjust to match.

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_lifecycle_activity_logging.py -k sales_note -v --override-ini="addopts="`
Expected: FAIL — no `sales_note` row.

- [ ] **Step 3: Instrument both note-edit handlers**

In each handler, after the `sale_notes` assignment and before `db.commit()`, insert (guarding so it only logs when the note actually changed — capture the prior value first):

```python
        log_activity(
            db,
            activity_type=ActivityType.SALES_NOTE,
            requisition_id=<requisition id in scope>,
            requirement_id=<requirement id>,
            user_id=user.id,
            description="Sales note updated",
            details={"requirement_id": <requirement id>},
        )
```

In `htmx_views.py`, use the `_log_activity` alias. Resolve `requisition_id` from the requirement object where it is not directly in scope. Only log when the new `sale_notes` value differs from the old one (skip no-op saves).

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_lifecycle_activity_logging.py -v --override-ini="addopts="`
Expected: PASS — all tests in the file pass.

- [ ] **Step 5: Update the APP_MAP doc**

In `docs/APP_MAP_INTERACTIONS.md`, extend the activity-logging note: task completion, requisition assignment (claim/unclaim/batch), archive/unarchive, and sales-note edits now route through `activity_service.log_activity()` (`ActivityType.TASK_COMPLETED`, `ASSIGNMENT_CHANGED`, `REQ_ARCHIVED`/`REQ_UNARCHIVED`, `SALES_NOTE`). Match the doc's existing style.

- [ ] **Step 6: Run the full lifecycle + activity suite + lint, then commit**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -k "activity or lifecycle or task or archive" -v --override-ini="addopts="
ruff check app/constants.py app/routers/requisitions/core.py app/routers/requisitions/requirements.py app/routers/htmx_views.py app/services/task_service.py app/services/requirement_status.py
git add app/routers/requisitions/requirements.py app/routers/htmx_views.py docs/APP_MAP_INTERACTIONS.md tests/test_lifecycle_activity_logging.py
git commit -m "feat: log sales_note activity on requirement note edits"
```

Expected: lifecycle/activity-tagged tests pass; ruff clean. (Pre-existing xdist pollution failures, if any, are out of scope — confirm against a baseline.)

---

## Self-Review

**Spec coverage (build step 2 — non-offer portion):**
- `task_completed` → Task 1 (both the route and service paths) ✓
- `assignment_changed` → Task 2 (claim + unclaim migrated off raw strings; batch_assign added) ✓
- `req_archived` / `req_unarchived` → Tasks 3-4 (loaded-row sites + bulk-update sites) ✓
- `sales_note` → Task 5 ✓
- `sighting_added` → intentionally deferred to Plan 4 (batch aggregation belongs with the curation layer) ✓
- `offer_created` / `offer_status_changed` → Plan 2a ✓

**Enum extension:** This plan adds one member, `REQ_UNARCHIVED`, to the `ActivityType` enum (Task 3). The spec's canonical list named only `req_archived`; archive and unarchive are genuinely distinct timeline events, so this is a deliberate additive extension (a new string member, no schema change — `req_unarchived` fits the existing `String(20)` column). Flagged here so it is a recorded decision, not silent drift.

**Placeholder scan:** Task 2's `batch_assign` and Task 4's bulk-archive code blocks, and Task 3's archive snippet, use `<...>` placeholders for per-site variable names — each is resolved in the sentence immediately after the block (real query variable, real actor, real id attribute). Every other code step is complete.

**Type consistency:** All calls use the Plan 1 `log_activity()` keyword signature. `ActivityType` members referenced: `TASK_COMPLETED`, `ASSIGNMENT_CHANGED`, `REQ_ARCHIVED`, `SALES_NOTE` (existing) and `REQ_UNARCHIVED` (added in Task 3 before first use in Task 3 Step 4).

**Migration risk:** Task 2 changes the `activity_type` value written by claim/unclaim from `"requisition_claimed"`/`"requisition_unclaimed"` to `"assignment_changed"`. Any existing test or display code keyed on the old strings must be updated — Task 2 Step 5 explicitly greps for and fixes such tests. The DB is intentionally empty (no historical rows to migrate).

**No schema migration:** confirmed — reuses existing `activity_log` columns; the only schema-adjacent change is an additive StrEnum member.
