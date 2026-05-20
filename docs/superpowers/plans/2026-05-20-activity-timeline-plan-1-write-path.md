# Activity Timeline — Plan 1: Unify the Write Path

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the requisition Activity tab a single canonical write path so logged events reliably appear, and fix the tag bug that drops every outbound RFQ.

**Architecture:** Generalize the existing requisition-aware `log_rfq_activity()` in `app/services/activity_service.py` into a canonical `log_activity()` writer; keep `log_rfq_activity()` as a delegating alias. Add a `get_requisition_activities()` read helper and wire it into the Activity tab route, replacing the inlined query. Fix the stale sent-folder tag regex. No schema migration — all changes reuse existing `activity_log` columns.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0, pytest (in-memory SQLite), Loguru.

**Spec:** `docs/superpowers/specs/2026-05-20-activity-timeline-design.md` (build step 1).

---

### Task 1: Canonical `ActivityType` enum

Define the canonical event-type constants so every writer uses the same strings instead of raw literals (per CLAUDE.md: "Always use StrEnum constants, never raw strings").

**Files:**
- Modify: `app/constants.py` (append a new StrEnum, matching the existing StrEnum definitions in that file)
- Test: `tests/test_activity_write_path.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_activity_write_path.py`:

```python
"""test_activity_write_path.py — Tests for the unified activity write path.

Covers the ActivityType enum, log_activity() canonical writer, the
log_rfq_activity() delegating alias, requisition_id on email/call logging,
and get_requisition_activities().

Called by: pytest
Depends on: app/constants.py, app/services/activity_service.py, conftest.py
"""

from app.constants import ActivityType


def test_activity_type_values_fit_column():
    """Every canonical activity_type value fits the activity_log.activity_type
    String(20) column."""
    for member in ActivityType:
        assert len(member.value) <= 20, f"{member.value} exceeds 20 chars"


def test_activity_type_has_expected_members():
    assert ActivityType.RFQ_SENT == "rfq_sent"
    assert ActivityType.STATUS_CHANGED == "status_changed"
    assert ActivityType.OFFER_STATUS_CHANGED == "offer_status_changed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py -v --override-ini="addopts="`
Expected: FAIL — `ImportError: cannot import name 'ActivityType' from 'app.constants'`

- [ ] **Step 3: Append the enum to `app/constants.py`**

Add at the end of the file (use the `StrEnum` import already present in that file):

```python
class ActivityType(StrEnum):
    """Canonical activity_log.activity_type values. All <= 20 chars (column width)."""

    RFQ_SENT = "rfq_sent"
    EMAIL_RECEIVED = "email_received"
    CALL_LOGGED = "call_logged"
    STATUS_CHANGED = "status_changed"
    OFFER_CREATED = "offer_created"
    OFFER_STATUS_CHANGED = "offer_status_changed"
    SIGHTING_ADDED = "sighting_added"
    SALES_NOTE = "sales_note"
    TASK_COMPLETED = "task_completed"
    ASSIGNMENT_CHANGED = "assignment_changed"
    REQ_ARCHIVED = "req_archived"
```

If `StrEnum` is not already imported in `app/constants.py`, add `from enum import StrEnum` with the existing imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py -v --override-ini="addopts="`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add app/constants.py tests/test_activity_write_path.py
git commit -m "feat: add canonical ActivityType enum for activity timeline"
```

---

### Task 2: `log_activity()` canonical writer

Generalize the existing `log_rfq_activity()` (`app/services/activity_service.py:657-696`) into `log_activity()`; make `log_rfq_activity()` delegate so the existing caller at `sightings.py:990` keeps working unchanged.

**Files:**
- Modify: `app/services/activity_service.py:657-696`
- Test: `tests/test_activity_write_path.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_activity_write_path.py`:

```python
from app.constants import ActivityType
from app.models import ActivityLog
from app.services.activity_service import log_activity, log_rfq_activity


def test_log_activity_sets_requisition_id(db_session, test_requisition, test_user):
    record = log_activity(
        db_session,
        activity_type=ActivityType.STATUS_CHANGED,
        channel="system",
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="Status changed from active to sourcing",
    )
    assert record.id is not None
    assert record.requisition_id == test_requisition.id
    assert record.activity_type == "status_changed"
    assert record.channel == "system"
    assert record.notes == "Status changed from active to sourcing"


def test_log_rfq_activity_delegates_to_log_activity(db_session, test_requisition, test_user):
    record = log_rfq_activity(
        db=db_session,
        rfq_id=test_requisition.id,
        activity_type="status_change",
        description="legacy call path",
        user_id=test_user.id,
    )
    assert record.requisition_id == test_requisition.id
    assert record.notes == "legacy call path"
    rows = db_session.query(ActivityLog).filter_by(requisition_id=test_requisition.id).all()
    assert len(rows) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py -v --override-ini="addopts="`
Expected: FAIL — `ImportError: cannot import name 'log_activity'`

- [ ] **Step 3: Replace `log_rfq_activity()` with `log_activity()` + alias**

In `app/services/activity_service.py`, replace the whole `log_rfq_activity` function (lines 657-696) with:

```python
def log_activity(
    db: Session,
    *,
    activity_type: str,
    channel: str = "system",
    requisition_id: int | None = None,
    requirement_id: int | None = None,
    user_id: int | None = None,
    company_id: int | None = None,
    vendor_card_id: int | None = None,
    vendor_contact_id: int | None = None,
    description: str | None = None,
    summary: str | None = None,
    occurred_at: datetime | None = None,
    details: dict | None = None,
) -> ActivityLog:
    """Canonical activity-log writer — every event source routes through this.

    Resolves company_id from the requisition (requisition -> customer_site ->
    company) when not supplied, so the row links to both the req and its company.
    Always sets requisition_id/requirement_id so the row appears on the req
    Activity tab.

    Called by: log_rfq_activity (alias), system-event hooks, webhook handlers.
    """
    if company_id is None and requisition_id:
        from ..models.crm import CustomerSite
        from ..models.sourcing import Requisition

        req = db.get(Requisition, requisition_id)
        if req and req.customer_site_id:
            site = db.get(CustomerSite, req.customer_site_id)
            if site:
                company_id = site.company_id

    record = ActivityLog(
        user_id=user_id,
        activity_type=activity_type,
        channel=channel,
        requisition_id=requisition_id,
        requirement_id=requirement_id,
        company_id=company_id,
        vendor_card_id=vendor_card_id,
        vendor_contact_id=vendor_contact_id,
        notes=description,
        summary=summary,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        details=details,
    )
    db.add(record)
    db.flush()

    if company_id:
        _update_last_activity({"type": "company", "id": company_id}, db)
    if vendor_card_id:
        _update_last_activity({"type": "vendor", "id": vendor_card_id}, db)

    logger.info(
        f"Activity logged: {activity_type} -> req {requisition_id} (channel={channel})"
    )
    return record


def log_rfq_activity(
    db: Session,
    rfq_id: int,
    activity_type: str,
    description: str,
    metadata: dict | None = None,
    user_id: int | None = None,
    requirement_id: int | None = None,
) -> ActivityLog:
    """Backward-compatible alias for log_activity() — see that function.

    Kept so existing callers (e.g. routers/sightings.py) need no change.
    """
    return log_activity(
        db,
        activity_type=activity_type,
        channel="system",
        requisition_id=rfq_id,
        requirement_id=requirement_id,
        user_id=user_id,
        description=description,
        details=metadata,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py tests/test_sightings_log_activity.py -v --override-ini="addopts="`
Expected: PASS — new tests pass and the existing `test_sightings_log_activity.py` still passes (proves the alias keeps `sightings.py:990` working).

- [ ] **Step 5: Commit**

```bash
git add app/services/activity_service.py tests/test_activity_write_path.py
git commit -m "feat: add canonical log_activity() writer, log_rfq_activity delegates"
```

---

### Task 3: Add `requisition_id` to email/call auto-logging

`log_email_activity` / `log_call_activity` (`app/services/activity_service.py:143-249`) take no requisition scope, so auto-logged rows land with `requisition_id=NULL`. Add optional params (default `None` → fully backward compatible).

**Files:**
- Modify: `app/services/activity_service.py:143-249`
- Test: `tests/test_activity_write_path.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_activity_write_path.py`:

```python
from app.services.activity_service import log_call_activity, log_email_activity


def test_log_email_activity_accepts_requisition_id(db_session, test_requisition, test_user):
    record = log_email_activity(
        user_id=test_user.id,
        direction="sent",
        email_addr="vendor@example.com",
        subject="RFQ [ref:%d]" % test_requisition.id,
        external_id="msg-req-001",
        contact_name="Vendor Rep",
        db=db_session,
        requisition_id=test_requisition.id,
    )
    assert record is not None
    assert record.requisition_id == test_requisition.id


def test_log_call_activity_accepts_requisition_id(db_session, test_requisition, test_user):
    record = log_call_activity(
        user_id=test_user.id,
        direction="outbound",
        phone="+15551234567",
        duration_seconds=120,
        external_id="call-req-001",
        contact_name="Vendor Rep",
        db=db_session,
        requisition_id=test_requisition.id,
    )
    assert record is not None
    assert record.requisition_id == test_requisition.id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py -k requisition_id -v --override-ini="addopts="`
Expected: FAIL — `TypeError: log_email_activity() got an unexpected keyword argument 'requisition_id'`

- [ ] **Step 3: Add the parameters**

In `app/services/activity_service.py`, in `log_email_activity` (signature ends at line 150 with `db: Session,`), add two params after `db: Session,`:

```python
    db: Session,
    requisition_id: int | None = None,
    requirement_id: int | None = None,
) -> ActivityLog | None:
```

Then in its `ActivityLog(...)` constructor (lines 166-181), add these two lines before the closing paren:

```python
        requisition_id=requisition_id,
        requirement_id=requirement_id,
```

Apply the identical change to `log_call_activity`: add the two params after `subject: str | None = None,` in the signature, and add the same two lines to its `ActivityLog(...)` constructor (lines 222-238).

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py tests/test_services_activity.py -v --override-ini="addopts="`
Expected: PASS — new tests pass and existing `test_services_activity.py` still passes (defaults keep old callers working).

- [ ] **Step 5: Commit**

```bash
git add app/services/activity_service.py tests/test_activity_write_path.py
git commit -m "feat: add optional requisition_id to email/call activity logging"
```

---

### Task 4: Fix the sent-folder tag regex

`_AVAIL_TAG_RE` (`app/jobs/email_jobs.py:774`) matches only `[AVAIL-(\d+)]`, but RFQ send tags subjects `[ref:{id}]` (`app/email_service.py:95`). Every outbound RFQ is therefore logged with `requisition_id=NULL`. Fix the regex to match both formats, matching `email_service.py:450`.

**Files:**
- Modify: `app/jobs/email_jobs.py:773-774`
- Test: `tests/test_activity_write_path.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_activity_write_path.py`:

```python
from app.jobs.email_jobs import _AVAIL_TAG_RE


def test_avail_tag_re_matches_ref_format():
    """The sent-folder scan must recognise the [ref:N] tag that RFQ send writes."""
    assert _AVAIL_TAG_RE.search("Quote request RE part [ref:4321]").group(1) == "4321"


def test_avail_tag_re_matches_legacy_format():
    assert _AVAIL_TAG_RE.search("Quote request [AVAIL-99]").group(1) == "99"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py -k avail_tag -v --override-ini="addopts="`
Expected: FAIL — `test_avail_tag_re_matches_ref_format` fails with `AttributeError: 'NoneType' object has no attribute 'group'`

- [ ] **Step 3: Fix the regex**

In `app/jobs/email_jobs.py`, replace lines 773-774:

```python
# Regex to extract requisition ID from [AVAIL-123] tags in email subjects
_AVAIL_TAG_RE = re.compile(r"\[AVAIL-(\d+)\]")
```

with:

```python
# Regex to extract requisition ID from RFQ subject tags.
# Matches both [ref:123] (current, written by email_service.send_batch_rfq)
# and [AVAIL-123] (legacy). Mirrors the pattern in email_service.py:450.
_AVAIL_TAG_RE = re.compile(r"\[(?:ref:|AVAIL-)(\d+)\]")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py -k avail_tag -v --override-ini="addopts="`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add app/jobs/email_jobs.py tests/test_activity_write_path.py
git commit -m "fix: sent-folder scan now recognises [ref:N] RFQ tag, not just [AVAIL-N]"
```

---

### Task 5: `get_requisition_activities()` read helper

Add the missing requisition-scoped query helper so the route does not inline a query.

**Files:**
- Modify: `app/services/activity_service.py` (add after `get_user_activities`, ~line 287)
- Test: `tests/test_activity_write_path.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_activity_write_path.py`:

```python
from app.services.activity_service import get_requisition_activities


def test_get_requisition_activities_returns_scoped_rows(db_session, test_requisition, test_user):
    log_activity(
        db_session,
        activity_type=ActivityType.STATUS_CHANGED,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="first",
    )
    log_activity(
        db_session,
        activity_type=ActivityType.RFQ_SENT,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="second",
    )
    rows = get_requisition_activities(db_session, test_requisition.id)
    assert len(rows) == 2
    assert all(r.requisition_id == test_requisition.id for r in rows)


def test_get_requisition_activities_excludes_other_reqs(db_session, test_requisition, test_user):
    log_activity(
        db_session,
        activity_type=ActivityType.STATUS_CHANGED,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="mine",
    )
    assert get_requisition_activities(db_session, 999999) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py -k get_requisition -v --override-ini="addopts="`
Expected: FAIL — `ImportError: cannot import name 'get_requisition_activities'`

- [ ] **Step 3: Add the helper**

In `app/services/activity_service.py`, add immediately after `get_user_activities` (ends ~line 287):

```python
def get_requisition_activities(
    db: Session, requisition_id: int, limit: int = 200
) -> list[ActivityLog]:
    """Get the full activity timeline for a requisition, newest first.

    Backs the requisition Activity tab. Uses the ix_activity_requisition index.
    """
    return (
        db.query(ActivityLog)
        .filter(ActivityLog.requisition_id == requisition_id)
        .order_by(ActivityLog.created_at.desc())
        .limit(limit)
        .all()
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py -k get_requisition -v --override-ini="addopts="`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/activity_service.py tests/test_activity_write_path.py
git commit -m "feat: add get_requisition_activities() timeline read helper"
```

---

### Task 6: Wire the read helper into the Activity tab route

Replace the inlined `ActivityLog` query in the Activity tab branch with `get_requisition_activities()`.

**Files:**
- Modify: `app/routers/htmx_views.py:1259-1278`
- Modify: `docs/APP_MAP_INTERACTIONS.md` (activity flow note)
- Test: `tests/test_activity_write_path.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_activity_write_path.py`:

```python
def test_activity_tab_renders_logged_event(client, db_session, test_requisition, test_user):
    """An event written via log_activity() appears on the requisition Activity tab."""
    log_activity(
        db_session,
        activity_type=ActivityType.STATUS_CHANGED,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="Status changed from active to sourcing",
    )
    db_session.commit()
    resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/activity")
    assert resp.status_code == 200
    assert "Status changed from active to sourcing" in resp.text
    assert "No activity recorded yet" not in resp.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py -k activity_tab -v --override-ini="addopts="`
Expected: FAIL — response still shows "No activity recorded yet" OR the assertion on the rendered note fails (the inlined query path predates this test data path; confirm the failure reason in output before proceeding).

- [ ] **Step 3: Replace the inlined query**

In `app/routers/htmx_views.py`, the `else:  # activity` branch (lines 1259-1278) currently reads:

```python
    else:  # activity
        from ..models.intelligence import ActivityLog
        from ..models.offers import Contact as RfqContact

        contacts = (
            db.query(RfqContact)
            .filter(RfqContact.requisition_id == req_id)
            .order_by(RfqContact.created_at.desc())
            .all()
        )
        activities = (
            db.query(ActivityLog)
            .filter(ActivityLog.requisition_id == req_id)
            .order_by(ActivityLog.created_at.desc())
            .all()
        )
        ctx["contacts"] = contacts
        ctx["activities"] = activities
        ctx["req"] = req
        return templates.TemplateResponse("htmx/partials/requisitions/tabs/activity.html", ctx)
```

Replace it with:

```python
    else:  # activity
        from ..models.offers import Contact as RfqContact
        from ..services.activity_service import get_requisition_activities

        contacts = (
            db.query(RfqContact)
            .filter(RfqContact.requisition_id == req_id)
            .order_by(RfqContact.created_at.desc())
            .all()
        )
        ctx["contacts"] = contacts
        ctx["activities"] = get_requisition_activities(db, req_id)
        ctx["req"] = req
        return templates.TemplateResponse("htmx/partials/requisitions/tabs/activity.html", ctx)
```

(The `from ..models.intelligence import ActivityLog` import is removed because it is now unused — ruff would flag it.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py -v --override-ini="addopts="`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Update the APP_MAP doc**

In `docs/APP_MAP_INTERACTIONS.md`, find the activity-logging / activity-tab section and add a note: the requisition Activity tab reads via `activity_service.get_requisition_activities()`; all writers route through `activity_service.log_activity()` (with `log_rfq_activity()` as a delegating alias).

- [ ] **Step 6: Run the full activity test suite + lint, then commit**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -k activity -v
ruff check app/services/activity_service.py app/routers/htmx_views.py app/jobs/email_jobs.py app/constants.py
git add app/routers/htmx_views.py docs/APP_MAP_INTERACTIONS.md tests/test_activity_write_path.py
git commit -m "feat: requisition Activity tab reads via get_requisition_activities()"
```

Expected: all activity-tagged tests pass; ruff reports no errors.

---

## Self-Review

**Spec coverage (build step 1 — "Unify the write path"):**
- `log_activity()` single write path → Task 2 ✓
- `log_rfq_activity` delegates (DRY) → Task 2 ✓
- `requisition_id`/`requirement_id` on email/call logging → Task 3 ✓
- Confirm/fix subject-tag mismatch → Task 4 (confirmed a real bug; fixed) ✓
- `get_requisition_activities()` read helper → Task 5 ✓
- Wire into `htmx_views.py` → Task 6 ✓
- Canonical enum constants (CLAUDE.md non-negotiable) → Task 1 ✓

**Placeholder scan:** none — every code step shows complete code; the one APP_MAP step is documentation prose, not code.

**Type consistency:** `log_activity()` signature in Task 2 is the single source; Tasks 5-6 call it with a subset of those exact kwargs. `get_requisition_activities(db, requisition_id, limit=200)` defined in Task 5, called as `get_requisition_activities(db, req_id)` in Task 6 — consistent. `_AVAIL_TAG_RE` keeps its name (Task 4) so no caller changes.

**Scope:** Plan 1 is foundational only — it makes the feed reliable for events that already log (status changes) and for outbound RFQs (tag fix). Wiring the other 12 system event types is Plan 2; inbound-email bridge is Plan 3; AI curation Plan 4; 8x8 enablement Plan 5; frontend polish Plan 6.

**No migration:** confirmed — every change reuses existing `activity_log` columns.
