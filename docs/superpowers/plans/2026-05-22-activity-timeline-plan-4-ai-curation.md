# Activity Timeline — Plan 4: AI Curation Layer

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the Activity timeline *curated* — log search-batch sightings as one aggregated event, mark inherently-meaningful events `is_meaningful=True` for free, AI-score the high-volume/free-text event types (`sighting_added`, `email_received`), and default the requisition Activity tab to meaningful events with a "show all" toggle.

**Architecture:** Hybrid curation per the spec. (1) `sighting_added`: one aggregated `activity_log` row per `search_requirement()` run (not one per sighting), written in the search worker's write-session with `user_id=NULL`. (2) Rule-based: `log_activity()` sets `is_meaningful=True` for inherently-meaningful `activity_type`s; `log_call_activity()` does the same for calls. (3) AI-scored: the existing `activity_quality_service` quality pass is extended — via an explicit allow-list — to score `sighting_added` and `email_received`, with a per-type prompt branch. (4) `get_requisition_activities()` gains a `meaningful_only` filter (default on, shows `is_meaningful IS true OR IS null` so freshly-logged rows are not hidden during the ≤15-min scoring lag); the Activity tab route gets a `show_all` toggle. No schema migration — `quality_score`/`quality_classification`/`is_meaningful`/`quality_assessed_at` already exist on `ActivityLog`.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0, pytest, Loguru, Anthropic Claude (`claude_structured`).

**Spec:** `docs/superpowers/specs/2026-05-20-activity-timeline-design.md` (build step 4 + "Hybrid AI curation").

**Branch:** Create `feat/activity-timeline-4` off `feat/activity-timeline-3` (or `main` if Plans 1–3 merged).

---

### Task 1: Aggregated `sighting_added` logging

Write ONE `sighting_added` `activity_log` row per `search_requirement()` run — "N sightings added from <sources>" — not one per sighting (batch aggregation per the spec).

**Context (verify line numbers against the live file before editing):**
- `app/search_service.py` — `search_requirement()` (~lines 268-495). Inside it, a dedicated write-session block (`write_db = _WriteSession()`, ~324). `write_req = write_db.get(Requirement, req_id)`. `succeeded_sources` (a `set[str]`) computed ~335-339. `sightings = _save_sightings(...)` returns the persisted list ~340. `write_db.commit()` ~390. `write_db.expunge(...)` ~411.
- No user context in this function — the aggregated row is automated: `user_id=None`, `channel="system"`.
- `Requirement.requisition_id` is NOT NULL; `write_req.requisition_id` and `write_req.id` are both in scope.

**Files:**
- Modify: `app/search_service.py`
- Test: `tests/test_search_service.py` (or a focused new test file — match where search tests live)

- [ ] **Step 1: Write the failing test**

Add a test that runs `search_requirement()` for a requirement with a stubbed connector layer that yields a few results, then asserts exactly ONE `ActivityLog` row exists with `activity_type == "sighting_added"`, `requisition_id` = the requirement's requisition, `requirement_id` = the requirement, and `details["count"]` equal to the number of sightings persisted.

Before writing: read `search_requirement()` and an existing `tests/test_search_service.py` test to copy the established way search is driven in tests (how connectors/`_save_sightings` are stubbed so no network call happens). The test MUST drive the real `search_requirement` and assert the real row. If a search run cannot be unit-tested, instead test the smaller helper you extract in Step 3 (see below) directly, and note that.

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_service.py -k sighting_added -v --override-ini="addopts="`
Expected: FAIL — no `sighting_added` row.

- [ ] **Step 2: Confirm the failure reason**

Confirm the failure is "no activity row" (sightings persist, but nothing logs).

- [ ] **Step 3: Implement the aggregated write**

In `app/search_service.py`, import the writer: `from .services.activity_service import log_activity` and `from .constants import ActivityType` (match the file's import style; check existing imports).

In `search_requirement()`, after `write_db.commit()` (~line 390) succeeds and before `write_db.expunge(...)` (~411), insert — using `write_db` so it shares the write transaction:

```python
        if sightings:
            sources = sorted(succeeded_sources)
            log_activity(
                write_db,
                activity_type=ActivityType.SIGHTING_ADDED,
                requisition_id=write_req.requisition_id,
                requirement_id=write_req.id,
                user_id=None,
                channel="system",
                description=(
                    f"{len(sightings)} sighting(s) added"
                    + (f" from {', '.join(sources)}" if sources else "")
                ),
                details={"count": len(sightings), "sources": sources},
            )
            write_db.commit()
```

Notes: skip the row entirely when `sightings` is empty (no noise for zero-result searches). `log_activity` flushes; the explicit `write_db.commit()` persists it. Verify the real local variable names (`sightings`, `succeeded_sources`, `write_req`, `write_db`) against the live function and adapt. If the post-commit/pre-expunge window is structured differently than described, place the call at the equivalent point — after sightings are durably committed, before the write session is torn down.

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_service.py -k "sighting" -v --override-ini="addopts="`
Expected: PASS — new test passes; existing search tests still pass. (Broad-run xdist pollution — verify against a `git stash` baseline; report but proceed.)

- [ ] **Step 5: Commit**

```bash
git add app/search_service.py tests/test_search_service.py
git commit -m "feat: log one aggregated sighting_added activity per search batch"
```

---

### Task 2: Rule-based `is_meaningful` for inherently-meaningful events

Inherently-meaningful events get `is_meaningful=True` at write time — free, deterministic, no AI.

**Files:**
- Modify: `app/services/activity_service.py`
- Test: `tests/test_activity_write_path.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_activity_write_path.py`:

```python
def test_log_activity_marks_meaningful_types(db_session, test_requisition, test_user):
    """Inherently-meaningful activity types are flagged is_meaningful=True at write time."""
    rec = log_activity(
        db_session,
        activity_type=ActivityType.STATUS_CHANGED,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="status changed",
    )
    assert rec.is_meaningful is True


def test_log_activity_leaves_ai_scored_types_unflagged(db_session, test_requisition, test_user):
    """AI-scored types (sighting_added) are left is_meaningful=None for the quality pass."""
    rec = log_activity(
        db_session,
        activity_type=ActivityType.SIGHTING_ADDED,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="12 sightings added",
    )
    assert rec.is_meaningful is None
```

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py -k "meaningful" -v --override-ini="addopts="`
Expected: FAIL — `is_meaningful` is `None` for the status-changed row (`log_activity` does not set it yet).

- [ ] **Step 2: Confirm the failure reason**

- [ ] **Step 3: Implement the rule-based flag**

In `app/services/activity_service.py`, near the top (after imports), add this module-level set built from the `ActivityType` enum (do NOT use raw strings — CLAUDE.md rule):

```python
# Activity types that are inherently meaningful — flagged is_meaningful=True at
# write time (cheap, deterministic). The high-volume / free-text types
# (sighting_added, email_received) are deliberately excluded: they are left
# is_meaningful=None for the AI quality-scoring pass to classify. call events
# are flagged in log_call_activity (they are not written via log_activity).
_RULE_MEANINGFUL_TYPES: frozenset[str] = frozenset(
    {
        ActivityType.RFQ_SENT,
        ActivityType.STATUS_CHANGED,
        ActivityType.OFFER_CREATED,
        ActivityType.OFFER_STATUS_CHANGED,
        ActivityType.ASSIGNMENT_CHANGED,
        ActivityType.TASK_COMPLETED,
        ActivityType.REQ_ARCHIVED,
        ActivityType.REQ_UNARCHIVED,
    }
)
```

Then in `log_activity()`'s `ActivityLog(...)` constructor, add:

```python
        is_meaningful=True if activity_type in _RULE_MEANINGFUL_TYPES else None,
```

(`activity_type` is the function's parameter; `StrEnum` members compare equal to their string values, and a plain string passed by a caller still matches. Leaving non-listed types as `None` preserves the unscored state for the AI pass.)

- [ ] **Step 4: Flag calls in `log_call_activity`**

`log_call_activity()` builds its `ActivityLog` directly (not via `log_activity`). A logged call is inherently meaningful. In `log_call_activity()`'s `ActivityLog(...)` constructor, add `is_meaningful=True`. (Do NOT change `log_email_activity` — inbound email is AI-scored, stays `None`.)

- [ ] **Step 5: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py tests/test_services_activity.py -v --override-ini="addopts="`
Expected: PASS — new tests pass; existing activity tests still pass.

- [ ] **Step 6: Commit**

```bash
git add app/services/activity_service.py tests/test_activity_write_path.py
git commit -m "feat: flag inherently-meaningful activity types is_meaningful at write time"
```

---

### Task 3: Extend AI quality scoring to `sighting_added` + `email_received`

The quality pass (`app/services/activity_quality_service.py`, run by the job in `app/jobs/quality_jobs.py`) currently selects rows by an `event_type NOT IN ("email")` deny-list. Replace that with an explicit allow-list keyed on `activity_type`, and branch the scoring prompt so `sighting_added` rows are scored from their `details` payload.

**Files:**
- Modify: `app/services/activity_quality_service.py`
- Test: `tests/test_activity_quality_service.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_activity_quality_service.py` (it already mocks `claude_structured` — reuse that pattern). Add a test that creates an unscored `ActivityLog` with `activity_type="sighting_added"` and a `details` dict, runs `score_unscored_activities(db, ...)` with a mocked `claude_structured` returning a valid quality result, and asserts the sighting row got `quality_assessed_at` set and `is_meaningful` populated. Add a second test for an `activity_type="email_received"` row.

Before writing: read `score_unscored_activities` and `score_activity` and the existing tests to copy the `claude_structured` mock pattern and the `QUALITY_SCHEMA` result shape.

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_quality_service.py -k "sighting or email_received" -v --override-ini="addopts="`
Expected: FAIL — the sighting/email rows are not selected for scoring, or the prompt builder produces nothing usable for them.

- [ ] **Step 2: Confirm the failure reason**

- [ ] **Step 3: Replace the selection deny-list with an allow-list**

In `score_unscored_activities()`, change the selection query so it scores exactly the AI-scored types. Replace the `event_type` filter with:

```python
    _AI_SCORED_TYPES = ("sighting_added", "email_received")
    ...
        .filter(ActivityLog.quality_assessed_at.is_(None))
        .filter(ActivityLog.activity_type.in_(_AI_SCORED_TYPES))
        .filter(ActivityLog.created_at >= <existing 7-day cutoff>)
```

Keep the existing `quality_assessed_at IS NULL` and 7-day-recency filters and the existing `batch_size` / ordering / Claude-error-abort behavior. Define `_AI_SCORED_TYPES` as a module-level constant (use the `ActivityType` enum values). This change means: only `sighting_added` and `email_received` rows are AI-scored; everything else relies on the Task-2 rule-based flag (or stays unscored, which is fine — the timeline default treats `NULL` as visible).

- [ ] **Step 4: Branch the scoring prompt by `activity_type`**

In `score_activity()`, the prompt is currently built from `event_type`/`subject`/`notes`/`duration_seconds`/`contact_name`. Add a branch: when `log.activity_type == "sighting_added"`, build the prompt from `log.details` (the aggregated payload `{"count": N, "sources": [...]}`) and `log.notes` — describe it as "a batch of N vendor sightings added from <sources> for this requirement" and ask the model to judge whether the batch is meaningful enough to surface on the timeline. For `email_received` keep using `subject`/`notes` (the email content). For all other types keep the existing prompt logic unchanged. Keep the SAME `QUALITY_SCHEMA` result shape (`quality_score`, `quality_classification`, `is_meaningful`, `summary`) — only the prompt *text* differs per type. If the system prompt's classification enum does not fit sightings, either widen the allowed classification values or pass a type-appropriate system prompt for the sighting branch — keep it minimal and within the existing `claude_structured` call shape.

- [ ] **Step 5: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_quality_service.py tests/test_quality_jobs_coverage.py -v --override-ini="addopts="`
Expected: PASS — new tests pass; existing quality tests still pass. If an existing test asserted the old `event_type`-deny-list selection behavior, update it to the allow-list and note it in the commit.

- [ ] **Step 6: Commit**

```bash
git add app/services/activity_quality_service.py tests/test_activity_quality_service.py
git commit -m "feat: AI-score sighting_added and email_received activity events"
```

---

### Task 4: `meaningful_only` filter on the read helper + Activity-tab "show all" toggle

`get_requisition_activities()` returns every row. Default the requisition Activity tab to meaningful events, with a toggle to reveal the rest.

**Files:**
- Modify: `app/services/activity_service.py`
- Modify: `app/routers/htmx_views.py`
- Modify: `app/templates/htmx/partials/requisitions/tabs/activity.html`
- Test: `tests/test_activity_write_path.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_activity_write_path.py`:

```python
def test_get_requisition_activities_meaningful_only_filter(db_session, test_requisition, test_user):
    """meaningful_only hides is_meaningful=False rows but keeps True and None (unscored)."""
    log_activity(db_session, activity_type=ActivityType.STATUS_CHANGED,
                 requisition_id=test_requisition.id, user_id=test_user.id, description="meaningful")
    unscored = log_activity(db_session, activity_type=ActivityType.SIGHTING_ADDED,
                            requisition_id=test_requisition.id, user_id=test_user.id, description="unscored")
    noise = log_activity(db_session, activity_type=ActivityType.SIGHTING_ADDED,
                         requisition_id=test_requisition.id, user_id=test_user.id, description="noise")
    noise.is_meaningful = False
    db_session.flush()

    curated = get_requisition_activities(test_requisition.id, db_session, meaningful_only=True)
    assert noise.id not in {r.id for r in curated}      # is_meaningful=False hidden
    assert unscored.id in {r.id for r in curated}       # is_meaningful=None still shown
    all_rows = get_requisition_activities(test_requisition.id, db_session, meaningful_only=False)
    assert noise.id in {r.id for r in all_rows}         # show-all reveals it
```

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py -k meaningful_only -v --override-ini="addopts="`
Expected: FAIL — `get_requisition_activities()` has no `meaningful_only` parameter (`TypeError`).

- [ ] **Step 2: Confirm the failure reason**

- [ ] **Step 3: Add the `meaningful_only` filter**

In `app/services/activity_service.py`, change `get_requisition_activities` to:

```python
def get_requisition_activities(
    requisition_id: int, db: Session, limit: int = 200, meaningful_only: bool = True
) -> list[ActivityLog]:
    """Get the activity timeline for a requisition, newest first.

    meaningful_only (default True) hides events the AI quality pass classified as
    not meaningful (is_meaningful=False); rows that are meaningful (True) or not
    yet scored (None) are kept, so freshly-logged events appear immediately.
    """
    q = db.query(ActivityLog).filter(ActivityLog.requisition_id == requisition_id)
    if meaningful_only:
        q = q.filter(
            (ActivityLog.is_meaningful.is_(True)) | (ActivityLog.is_meaningful.is_(None))
        )
    return q.order_by(ActivityLog.created_at.desc()).limit(limit).all()
```

- [ ] **Step 4: Wire the route toggle**

In `app/routers/htmx_views.py`, the requisition Activity tab branch (`else: # activity`, ~lines 1261-1274): read a `show_all` signal from the request query params (e.g. `request.query_params.get("show_all") == "1"`), pass `meaningful_only=not show_all` to `get_requisition_activities(req_id, db, meaningful_only=...)`, and add `ctx["show_all"] = show_all` so the template can render the toggle. Verify the handler has `request` in scope; if not, read whichever request object the tab handler already uses for query params.

- [ ] **Step 5: Add the template toggle**

In `app/templates/htmx/partials/requisitions/tabs/activity.html`, in the Activity Log section header, add a small toggle link: when `show_all` is false it links to the same activity-tab partial with `show_all=1` ("Show all events"); when true it links back without it ("Show meaningful only"). Use an `hx-get` to the activity tab partial URL targeting the activity tab content container, matching the file's existing HTMX patterns (find an existing `hx-get` in the requisition partials to mirror attributes/target). Keep it minimal — Plan 6 does full timeline polish. Do not add, remove, or rearrange other UI elements.

- [ ] **Step 6: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py -v --override-ini="addopts="`
Expected: PASS (all tests in the file).

- [ ] **Step 7: Commit**

```bash
git add app/services/activity_service.py app/routers/htmx_views.py app/templates/htmx/partials/requisitions/tabs/activity.html tests/test_activity_write_path.py
git commit -m "feat: requisition Activity tab defaults to meaningful events with show-all toggle"
```

---

### Task 5: APP_MAP doc + full activity suite

- [ ] **Step 1: Update the APP_MAP doc**

In `docs/APP_MAP_INTERACTIONS.md`, extend the activity-logging section: search batches log one aggregated `sighting_added` row; `log_activity()` flags inherently-meaningful types `is_meaningful=True` at write time; the quality-scoring pass AI-scores `sighting_added` + `email_received`; the requisition Activity tab defaults to meaningful events (`meaningful_only`, `is_meaningful` True-or-unscored) with a `show_all` toggle. Match the doc's prose style.

- [ ] **Step 2: Run the full activity suite + lint**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -k "activity or quality or sighting or search_service" -v --override-ini="addopts="
ruff check app/search_service.py app/services/activity_service.py app/services/activity_quality_service.py app/routers/htmx_views.py
```
Expected: activity/quality/sighting/search tests pass; ruff clean. (Pre-existing xdist pollution under broad runs — verify against a baseline; report but do not fix unrelated failures.)

- [ ] **Step 3: Commit**

```bash
git add docs/APP_MAP_INTERACTIONS.md
git commit -m "docs: APP_MAP — activity timeline AI curation layer"
```

---

## Self-Review

**Spec coverage (build step 4 — "AI curation layer"):**
- Aggregated `sighting_added` (one row per search batch, not 12) → Task 1 ✓
- Rule-based `is_meaningful=True` for inherently-meaningful types → Task 2 ✓
- AI-scored `quality_score`/`is_meaningful` for `sighting_added` + `email_received` → Task 3 ✓
- Timeline defaults to `is_meaningful` with "show all" → Task 4 ✓

**Resolved ambiguity (rigor rule):** the three-state `is_meaningful` — `meaningful_only` filters out only `is_meaningful IS False`; `True` and `None` (not-yet-scored) are both shown, so an event is never invisible during the ≤15-min gap before the quality job runs. "Show all" drops the filter entirely.

**Placeholder scan:** none — every code step shows complete code. Task 1's `details` payload is `{"count", "sources"}` only (no `top_score`) to stay unambiguous; Task 3's sighting prompt reads exactly those keys.

**Type consistency:** `log_activity()` keyword signature unchanged (the `is_meaningful` derivation is internal). `get_requisition_activities()` gains `meaningful_only: bool = True` — Task 4 Step 4's route call and the Plan-1 callers (which omit it) both stay valid via the default. `_AI_SCORED_TYPES` (Task 3) and `_RULE_MEANINGFUL_TYPES` (Task 2) are disjoint and together cover the canonical enum minus the call/note paths handled by their own writers.

**No migration:** confirmed — `quality_score`/`quality_classification`/`is_meaningful`/`quality_assessed_at` already exist on `ActivityLog`.

**Scope:** Plan 4 is curation logic + a minimal toggle. Full timeline visual polish (source icons, date grouping, chronological merge) is Plan 6. 8x8 call enablement is Plan 5.
