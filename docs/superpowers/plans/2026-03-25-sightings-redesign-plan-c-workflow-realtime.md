# Sightings Redesign Plan C: Workflow Actions + Real-Time

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add inline workflow actions (log activity, advance status, per-vendor RFQ, batch operations), auto-progress sourcing status on RFQ send/offer approval, email preview before send, cross-requirement vendor overlap, parallel batch refresh, SSE live updates, and visual polish (skeletons, transitions, responsive breakpoints).

**Architecture:** Phase 4 adds 7 new endpoints to `sightings.py` plus auto-progress hooks in the existing send-inquiry, offer-approve, and HTMX offer-approve flows. Phase 5 wires the existing `SSEBroker` into all mutation endpoints and adds a page-level SSE listener in the workspace template. All new endpoints follow the existing pattern: form data in, HTML partial out, with `_oob_toast()` for feedback. Batch operations are capped at `MAX_BATCH_SIZE = 50`.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, PostgreSQL 16, Jinja2, Alpine.js, Tailwind CSS, HTMX, SSE

**Spec:** `docs/superpowers/specs/2026-03-25-sightings-page-redesign.md`
**Depends on:** Plan A (Foundation) and Plan B (Visual Triage) must be completed first. **Critical precondition:** Plan A Task 1 must have added `SOURCING_TRANSITIONS` under `"requirement"` in `status_machine.py`. Without it, `require_valid_transition("requirement", ...)` silently allows any transition and Tasks 4, 5, 6, 8 will not enforce valid status transitions.

**Note on mock paths:** This codebase uses lazy imports inside function bodies (e.g., `from ..email_service import send_batch_rfq` inside `sightings_send_inquiry`). Mocks must target the source module (`app.email_service.send_batch_rfq`) not the importing module.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `app/routers/sightings.py` | Modify | 7 new endpoints, auto-progress in send-inquiry, parallel batch-refresh |
| `app/routers/crm/offers.py` | Modify | Auto-progress hook in `approve_offer` |
| `app/routers/htmx_views.py` | Modify | Auto-progress hook in HTMX offer approve |
| `app/schemas/sightings.py` | Modify | Add request schemas for new endpoints |
| `app/services/sse_broker.py` | Read-only | Existing `broker.publish()` API |
| `app/services/status_machine.py` | Read-only | `require_valid_transition("requirement", ...)` |
| `app/services/activity_service.py` | Read-only | `log_rfq_activity()` with `requirement_id` param |
| `app/database.py` | Read-only | `SessionLocal` for parallel batch-refresh |
| `app/constants.py` | Read-only | `SourcingStatus` enum |
| `app/templates/htmx/partials/sightings/_quick_actions.html` | Create | Inline log note/call form |
| `app/templates/htmx/partials/sightings/preview.html` | Create | Email preview step |
| `app/templates/htmx/partials/sightings/list.html` | Modify | SSE connection, disconnect banner |
| `app/templates/htmx/partials/sightings/detail.html` | Modify | Status dropdown, per-vendor RFQ, overlap badges, skeleton, transitions |
| `app/templates/htmx/partials/sightings/table.html` | Modify | Batch action bar buttons, multi-select badge, responsive breakpoints |
| `app/templates/htmx/partials/sightings/vendor_modal.html` | Modify | Email preview step (Alpine multi-step) |
| `app/static/htmx_app.js` | Modify | SSE reconnect logic, skeleton/transition helpers |
| `tests/test_sightings_router.py` | Modify | ~51 new tests |

---

### Task 1: Request Schemas for New Endpoints

**Files:** `app/schemas/sightings.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_sightings_router.py`, add:

```python
from app.schemas.sightings import (
    LogActivityRequest,
    AdvanceStatusRequest,
    BatchAssignRequest,
    BatchStatusRequest,
    BatchNotesRequest,
    PreviewInquiryRequest,
)


class TestSightingsSchemas:
    """Verify new request schemas exist and validate correctly."""

    def test_log_activity_defaults(self):
        req = LogActivityRequest(notes="test note")
        assert req.activity_type == "note"
        assert req.vendor_name == ""

    def test_advance_status_required_field(self):
        req = AdvanceStatusRequest(new_status="sourcing")
        assert req.new_status == "sourcing"

    def test_batch_assign_max_size(self):
        import pytest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            BatchAssignRequest(requirement_ids=list(range(51)), buyer_id=1)

    def test_batch_status_max_size(self):
        import pytest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            BatchStatusRequest(requirement_ids=list(range(51)), status="sourcing")

    def test_batch_notes_max_size(self):
        import pytest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            BatchNotesRequest(requirement_ids=list(range(51)), notes="hello")

    def test_preview_inquiry_requires_fields(self):
        req = PreviewInquiryRequest(
            requirement_ids=[1, 2],
            vendor_names=["Acme"],
            email_body="Hello",
        )
        assert len(req.requirement_ids) == 2
```

- [ ] **Step 2: Run test to verify it fails**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSightingsSchemas -v
```

Expected: ImportError — schemas don't exist yet.

- [ ] **Step 3: Write minimal implementation**

In `app/schemas/sightings.py`, add after the existing `SightingsListParams`:

```python
from pydantic import Field, field_validator


class LogActivityRequest(BaseModel):
    """Form data for inline activity logging."""
    activity_type: str = Field(default="note", pattern="^(note|call|email)$")
    notes: str = Field(min_length=1, max_length=2000)
    vendor_name: str = ""


class AdvanceStatusRequest(BaseModel):
    """Form data for status advancement."""
    new_status: str


class BatchAssignRequest(BaseModel):
    """Batch assign buyer to requirements."""
    requirement_ids: list[int] = Field(max_length=50)
    buyer_id: int

    @field_validator("requirement_ids")
    @classmethod
    def validate_not_empty(cls, v):
        if not v:
            raise ValueError("requirement_ids must not be empty")
        return v


class BatchStatusRequest(BaseModel):
    """Batch status change."""
    requirement_ids: list[int] = Field(max_length=50)
    status: str

    @field_validator("requirement_ids")
    @classmethod
    def validate_not_empty(cls, v):
        if not v:
            raise ValueError("requirement_ids must not be empty")
        return v


class BatchNotesRequest(BaseModel):
    """Batch notes to multiple requirements."""
    requirement_ids: list[int] = Field(max_length=50)
    notes: str = Field(min_length=1, max_length=2000)

    @field_validator("requirement_ids")
    @classmethod
    def validate_not_empty(cls, v):
        if not v:
            raise ValueError("requirement_ids must not be empty")
        return v


class PreviewInquiryRequest(BaseModel):
    """Preview email before sending."""
    requirement_ids: list[int]
    vendor_names: list[str]
    email_body: str = Field(min_length=1)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSightingsSchemas -v
```

- [ ] **Step 5: Commit**

```bash
git add app/schemas/sightings.py tests/test_sightings_router.py
git commit -m "feat(sightings): add request schemas for workflow action endpoints"
```

---

### Task 2: Inline Log Activity Endpoint

**Files:** `app/routers/sightings.py`, `tests/test_sightings_router.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_sightings_router.py`, add:

```python
class TestSightingsLogActivity:
    """POST /v2/partials/sightings/{id}/log-activity"""

    def test_log_note_returns_200(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/log-activity",
            data={"type": "note", "notes": "Called vendor", "vendor_name": ""},
        )
        assert resp.status_code == 200

    def test_log_note_creates_activity(self, client, db_session):
        from app.models.intelligence import ActivityLog
        _, r, _ = _seed_data(db_session)
        client.post(
            f"/v2/partials/sightings/{r.id}/log-activity",
            data={"type": "note", "notes": "Test note"},
        )
        activity = db_session.query(ActivityLog).filter(
            ActivityLog.requirement_id == r.id,
            ActivityLog.activity_type == "note",
        ).first()
        assert activity is not None
        assert activity.notes == "Test note"

    def test_log_call_with_vendor(self, client, db_session):
        from app.models.intelligence import ActivityLog
        _, r, _ = _seed_data(db_session)
        client.post(
            f"/v2/partials/sightings/{r.id}/log-activity",
            data={"type": "call", "notes": "Spoke with rep", "vendor_name": "Good Vendor"},
        )
        activity = db_session.query(ActivityLog).filter(
            ActivityLog.requirement_id == r.id,
            ActivityLog.activity_type == "call",
        ).first()
        assert activity is not None
        assert "Good Vendor" in activity.notes

    def test_empty_notes_returns_400(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/log-activity",
            data={"type": "note", "notes": ""},
        )
        assert resp.status_code == 400

    def test_invalid_type_returns_400(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/log-activity",
            data={"type": "invalid", "notes": "hello"},
        )
        assert resp.status_code == 400

    def test_missing_requirement_returns_404(self, client, db_session):
        resp = client.post(
            "/v2/partials/sightings/99999/log-activity",
            data={"type": "note", "notes": "hello"},
        )
        assert resp.status_code == 404

    def test_returns_updated_detail(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/log-activity",
            data={"type": "note", "notes": "My new note"},
        )
        assert "My new note" in resp.text

    def test_log_email_type(self, client, db_session):
        from app.models.intelligence import ActivityLog
        _, r, _ = _seed_data(db_session)
        client.post(
            f"/v2/partials/sightings/{r.id}/log-activity",
            data={"type": "email", "notes": "Sent follow-up"},
        )
        activity = db_session.query(ActivityLog).filter(
            ActivityLog.requirement_id == r.id,
            ActivityLog.activity_type == "email",
        ).first()
        assert activity is not None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSightingsLogActivity -v
```

Expected: 404/405 — endpoint does not exist yet.

- [ ] **Step 3: Write minimal implementation**

In `app/routers/sightings.py`, add after the `sightings_assign_buyer` endpoint:

```python
@router.post("/v2/partials/sightings/{requirement_id}/log-activity", response_class=HTMLResponse)
async def sightings_log_activity(
    request: Request,
    requirement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Log an inline note, call, or email activity on a requirement."""
    form = await request.form()
    activity_type = form.get("type", "note")
    notes = form.get("notes", "").strip()
    vendor_name = form.get("vendor_name", "").strip()

    if activity_type not in ("note", "call", "email"):
        raise HTTPException(status_code=400, detail="type must be note, call, or email")
    if not notes:
        raise HTTPException(status_code=400, detail="notes required")

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    full_notes = f"[{vendor_name}] {notes}" if vendor_name else notes
    activity = ActivityLog(
        user_id=user.id,
        activity_type=activity_type,
        channel="manual",
        requisition_id=requirement.requisition_id,
        requirement_id=requirement_id,
        notes=full_notes,
    )
    db.add(activity)
    db.commit()

    return await sightings_detail(request, requirement_id, db, user)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSightingsLogActivity -v
```

- [ ] **Step 5: Commit**

```bash
git add app/routers/sightings.py tests/test_sightings_router.py
git commit -m "feat(sightings): add inline log-activity endpoint"
```

---

### Task 3: Quick Actions Template

**Files:** `app/templates/htmx/partials/sightings/_quick_actions.html`, `app/templates/htmx/partials/sightings/detail.html`

- [ ] **Step 1: Create the quick actions partial**

Create `app/templates/htmx/partials/sightings/_quick_actions.html`:

```html
{# Inline quick action form for logging notes, calls, or emails.
   Called by: detail.html (include)
   Depends on: POST /v2/partials/sightings/{id}/log-activity
   Context: requirement
#}

<div x-data="{ expanded: false, actType: 'note', notes: '', vendor: '', submitting: false }"
     class="mb-4">
  <button @click="expanded = !expanded"
          class="flex items-center gap-1.5 text-xs font-medium text-gray-500 hover:text-gray-700 transition-colors">
    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/>
    </svg>
    Log Activity
  </button>

  <div x-show="expanded" x-collapse x-cloak
       class="mt-2 p-3 bg-gray-50 rounded-lg border border-gray-200">
    <form hx-post="/v2/partials/sightings/{{ requirement.id }}/log-activity"
          hx-target="#sightings-detail"
          hx-swap="innerHTML transition:true"
          @htmx:after-request="expanded = false; notes = ''; vendor = ''"
          class="space-y-2">

      <div class="flex gap-2">
        <select name="type" x-model="actType"
                class="text-xs rounded border-gray-200 py-1 px-2 bg-white">
          <option value="note">Note</option>
          <option value="call">Call</option>
          <option value="email">Email</option>
        </select>
        <input type="text" name="vendor_name" x-model="vendor"
               placeholder="Vendor (optional)"
               class="text-xs rounded border-gray-200 py-1 px-2 flex-1 min-w-0">
      </div>

      <textarea name="notes" x-model="notes" rows="2"
                placeholder="What happened?"
                class="w-full text-sm rounded border-gray-200 py-1.5 px-2 resize-none focus:border-brand-400 focus:ring-1 focus:ring-brand-200 outline-none"></textarea>

      <div class="flex justify-end gap-2">
        <button type="button" @click="expanded = false"
                class="text-xs text-gray-500 hover:text-gray-700 px-2 py-1">
          Cancel
        </button>
        <button type="submit" :disabled="!notes.trim() || submitting"
                class="text-xs font-medium text-white bg-brand-500 hover:bg-brand-600 rounded px-3 py-1 disabled:bg-gray-300 transition-colors">
          Save
        </button>
      </div>
    </form>
  </div>
</div>
```

- [ ] **Step 2: Include in detail.html**

In `app/templates/htmx/partials/sightings/detail.html`, add before the activity timeline section:

```html
{% include "htmx/partials/sightings/_quick_actions.html" %}
```

- [ ] **Step 3: Verify visually by running existing detail test**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSightingsDetailPartial -v
```

Expected: All PASS (include is additive, no breaking change).

- [ ] **Step 4: Commit**

```bash
git add app/templates/htmx/partials/sightings/_quick_actions.html app/templates/htmx/partials/sightings/detail.html
git commit -m "feat(sightings): add inline quick actions partial for activity logging"
```

---

### Task 4: Advance Status Endpoint

**Files:** `app/routers/sightings.py`, `tests/test_sightings_router.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_sightings_router.py`, add:

```python
class TestSightingsAdvanceStatus:
    """PATCH /v2/partials/sightings/{id}/advance-status"""

    def test_advance_open_to_sourcing(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.patch(
            f"/v2/partials/sightings/{r.id}/advance-status",
            data={"new_status": "sourcing"},
        )
        assert resp.status_code == 200
        db_session.refresh(r)
        assert r.sourcing_status == "sourcing"

    def test_advance_sourcing_to_offered(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        r.sourcing_status = "sourcing"
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/sightings/{r.id}/advance-status",
            data={"new_status": "offered"},
        )
        assert resp.status_code == 200
        db_session.refresh(r)
        assert r.sourcing_status == "offered"

    def test_invalid_transition_returns_409(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.patch(
            f"/v2/partials/sightings/{r.id}/advance-status",
            data={"new_status": "won"},
        )
        assert resp.status_code == 409

    def test_creates_activity_log(self, client, db_session):
        from app.models.intelligence import ActivityLog
        _, r, _ = _seed_data(db_session)
        client.patch(
            f"/v2/partials/sightings/{r.id}/advance-status",
            data={"new_status": "sourcing"},
        )
        activity = db_session.query(ActivityLog).filter(
            ActivityLog.requirement_id == r.id,
            ActivityLog.activity_type == "status_change",
        ).first()
        assert activity is not None

    def test_missing_requirement_returns_404(self, client, db_session):
        resp = client.patch(
            "/v2/partials/sightings/99999/advance-status",
            data={"new_status": "sourcing"},
        )
        assert resp.status_code == 404

    def test_empty_status_returns_400(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.patch(
            f"/v2/partials/sightings/{r.id}/advance-status",
            data={"new_status": ""},
        )
        assert resp.status_code == 400

    def test_same_status_noop(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.patch(
            f"/v2/partials/sightings/{r.id}/advance-status",
            data={"new_status": "open"},
        )
        assert resp.status_code == 200

    def test_archived_is_terminal(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        r.sourcing_status = "archived"
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/sightings/{r.id}/advance-status",
            data={"new_status": "open"},
        )
        assert resp.status_code == 409

    def test_returns_updated_detail(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.patch(
            f"/v2/partials/sightings/{r.id}/advance-status",
            data={"new_status": "sourcing"},
        )
        assert resp.status_code == 200

    def test_advance_quoted_to_won(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        r.sourcing_status = "quoted"
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/sightings/{r.id}/advance-status",
            data={"new_status": "won"},
        )
        assert resp.status_code == 200
        db_session.refresh(r)
        assert r.sourcing_status == "won"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSightingsAdvanceStatus -v
```

Expected: 405 Method Not Allowed — endpoint does not exist yet.

- [ ] **Step 3: Write minimal implementation**

In `app/routers/sightings.py`, add the import for `require_valid_transition` at the top:

```python
from ..services.status_machine import require_valid_transition
```

Then add the endpoint after `sightings_log_activity`:

```python
@router.patch("/v2/partials/sightings/{requirement_id}/advance-status", response_class=HTMLResponse)
async def sightings_advance_status(
    request: Request,
    requirement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Advance the sourcing status of a requirement with transition validation."""
    form = await request.form()
    new_status = form.get("new_status", "").strip()

    if not new_status:
        raise HTTPException(status_code=400, detail="new_status required")

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    old_status = requirement.sourcing_status
    require_valid_transition("requirement", old_status, new_status)

    requirement.sourcing_status = new_status
    if old_status != new_status:
        activity = ActivityLog(
            user_id=user.id,
            activity_type="status_change",
            channel="manual",
            requisition_id=requirement.requisition_id,
            requirement_id=requirement_id,
            notes=f"Status changed: {old_status} -> {new_status}",
        )
        db.add(activity)
    db.commit()

    return await sightings_detail(request, requirement_id, db, user)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSightingsAdvanceStatus -v
```

- [ ] **Step 5: Commit**

```bash
git add app/routers/sightings.py tests/test_sightings_router.py
git commit -m "feat(sightings): add advance-status endpoint with transition validation"
```

---

### Task 5: Auto-Progress Sourcing Status on RFQ Send

**Files:** `app/routers/sightings.py`, `tests/test_sightings_router.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_sightings_router.py`, add:

```python
from unittest.mock import AsyncMock, patch


class TestAutoProgressOnRFQSend:
    """Auto-progress sourcing status after successful RFQ send."""

    def test_open_advances_to_sourcing(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        assert r.sourcing_status == "open"
        with patch("app.email_service.send_batch_rfq", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = [{"ok": True}]
            client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": str(r.id),
                    "vendor_names": "Good Vendor",
                    "email_body": "Please quote",
                },
            )
        db_session.refresh(r)
        assert r.sourcing_status == "sourcing"

    def test_already_sourcing_stays_sourcing(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        r.sourcing_status = "sourcing"
        db_session.commit()
        with patch("app.email_service.send_batch_rfq", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = [{"ok": True}]
            client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": str(r.id),
                    "vendor_names": "Good Vendor",
                    "email_body": "Please quote",
                },
            )
        db_session.refresh(r)
        assert r.sourcing_status == "sourcing"

    def test_offered_not_overridden(self, client, db_session):
        """Never go backwards — offered is ahead of sourcing."""
        _, r, _ = _seed_data(db_session)
        r.sourcing_status = "offered"
        db_session.commit()
        with patch("app.email_service.send_batch_rfq", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = [{"ok": True}]
            client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": str(r.id),
                    "vendor_names": "Good Vendor",
                    "email_body": "Please quote",
                },
            )
        db_session.refresh(r)
        assert r.sourcing_status == "offered"

    def test_failed_send_no_progress(self, client, db_session):
        """Email failure must not advance status."""
        _, r, _ = _seed_data(db_session)
        assert r.sourcing_status == "open"
        with patch("app.email_service.send_batch_rfq", new_callable=AsyncMock) as mock_send:
            mock_send.side_effect = Exception("SMTP error")
            client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": str(r.id),
                    "vendor_names": "Good Vendor",
                    "email_body": "Please quote",
                },
            )
        db_session.refresh(r)
        assert r.sourcing_status == "open"

    def test_logs_auto_progress_activity(self, client, db_session):
        from app.models.intelligence import ActivityLog
        _, r, _ = _seed_data(db_session)
        with patch("app.email_service.send_batch_rfq", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = [{"ok": True}]
            client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": str(r.id),
                    "vendor_names": "Good Vendor",
                    "email_body": "Please quote",
                },
            )
        activity = db_session.query(ActivityLog).filter(
            ActivityLog.requirement_id == r.id,
            ActivityLog.activity_type == "status_change",
        ).first()
        assert activity is not None
        assert "Auto-set to sourcing" in activity.notes

    def test_won_not_overridden(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        r.sourcing_status = "won"
        db_session.commit()
        with patch("app.email_service.send_batch_rfq", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = [{"ok": True}]
            client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": str(r.id),
                    "vendor_names": "Good Vendor",
                    "email_body": "Please quote",
                },
            )
        db_session.refresh(r)
        assert r.sourcing_status == "won"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestAutoProgressOnRFQSend -v
```

Expected: `test_open_advances_to_sourcing` FAILS — status remains "open".

- [ ] **Step 3: Write minimal implementation**

In `app/routers/sightings.py`, modify the `sightings_send_inquiry` endpoint. Add to the top-level imports:

```python
from ..constants import SourcingStatus
```

Replace the `try` block inside `sightings_send_inquiry` (lines 473-498) with:

```python
    try:
        from ..email_service import send_batch_rfq

        results = await send_batch_rfq(
            token=token,
            db=db,
            user_id=user.id,
            requisition_id=requisition_id,
            vendor_groups=vendor_groups,
        )
        sent_count = len(results)

        # AUTO-PROGRESS: only on confirmed send success, forward-only
        for r in requirements:
            if r.sourcing_status == SourcingStatus.OPEN:
                r.sourcing_status = SourcingStatus.SOURCING
                db.add(ActivityLog(
                    user_id=user.id,
                    activity_type="status_change",
                    channel="system",
                    requisition_id=r.requisition_id,
                    requirement_id=r.id,
                    notes="Auto-set to sourcing after RFQ send",
                ))

        for r in requirements:
            for vn in vendor_names:
                log = ActivityLog(
                    user_id=user.id,
                    activity_type="rfq_sent",
                    channel="email",
                    requisition_id=r.requisition_id,
                    requirement_id=r.id,
                    notes=f"RFQ sent to {vn}",
                )
                db.add(log)
    except Exception:
        logger.warning("RFQ send failed", exc_info=True)
        failed_vendors = vendor_names
```

- [ ] **Step 4: Run test to verify it passes**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestAutoProgressOnRFQSend -v
```

- [ ] **Step 5: Commit**

```bash
git add app/routers/sightings.py tests/test_sightings_router.py
git commit -m "feat(sightings): auto-progress sourcing status to sourcing after RFQ send"
```

---

### Task 6: Auto-Progress on Offer Approval

**Files:** `app/routers/crm/offers.py`, `app/routers/htmx_views.py`, `tests/test_sightings_router.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_sightings_router.py`, add a helper and test:

```python
def _seed_offer(db_session, requirement, vendor_name="Good Vendor"):
    """Create a pending_review offer for testing."""
    from app.models.offers import Offer
    offer = Offer(
        requisition_id=requirement.requisition_id,
        requirement_id=requirement.id,
        vendor_name=vendor_name,
        status="pending_review",
        mpn=requirement.primary_mpn,
        qty=100,
        unit_price=1.50,
    )
    db_session.add(offer)
    db_session.commit()
    return offer


class TestAutoProgressOnOfferApproval:
    """Auto-progress sourcing status when offer is approved."""

    def test_open_advances_to_offered(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        offer = _seed_offer(db_session, r)
        resp = client.put(f"/api/offers/{offer.id}/approve")
        assert resp.status_code == 200
        db_session.refresh(r)
        assert r.sourcing_status == "offered"

    def test_sourcing_advances_to_offered(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        r.sourcing_status = "sourcing"
        db_session.commit()
        offer = _seed_offer(db_session, r)
        resp = client.put(f"/api/offers/{offer.id}/approve")
        assert resp.status_code == 200
        db_session.refresh(r)
        assert r.sourcing_status == "offered"

    def test_quoted_not_overridden(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        r.sourcing_status = "quoted"
        db_session.commit()
        offer = _seed_offer(db_session, r)
        resp = client.put(f"/api/offers/{offer.id}/approve")
        assert resp.status_code == 200
        db_session.refresh(r)
        assert r.sourcing_status == "quoted"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestAutoProgressOnOfferApproval -v
```

Expected: `test_open_advances_to_offered` FAILS — status remains "open".

- [ ] **Step 3: Write minimal implementation**

Create a shared helper in `app/services/sourcing_auto_progress.py`:

```python
"""sourcing_auto_progress.py — Forward-only auto-progress for sourcing status.

Called by: routers/sightings.py, routers/crm/offers.py, routers/htmx_views.py
Depends on: models (Requirement, ActivityLog), constants (SourcingStatus)
"""

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import SourcingStatus
from ..models.intelligence import ActivityLog
from ..models.sourcing import Requirement

# Ordered progression — higher index = further along
_STATUS_ORDER = [
    SourcingStatus.OPEN,
    SourcingStatus.SOURCING,
    SourcingStatus.OFFERED,
    SourcingStatus.QUOTED,
    SourcingStatus.WON,
]


def auto_progress_sourcing(
    db: Session,
    requirement_id: int,
    target_status: str,
    reason: str,
    user_id: int | None = None,
) -> bool:
    """Advance sourcing status forward-only. Returns True if status changed.

    Never overrides a status that is already at or beyond the target.
    Never goes backwards.
    """
    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        return False

    current = requirement.sourcing_status
    if current == target_status:
        return False

    # Check ordering — only advance forward
    try:
        current_idx = _STATUS_ORDER.index(current)
        target_idx = _STATUS_ORDER.index(target_status)
    except ValueError:
        # current or target is archived/lost — don't auto-progress
        return False

    if current_idx >= target_idx:
        return False

    requirement.sourcing_status = target_status
    db.add(ActivityLog(
        user_id=user_id,
        activity_type="status_change",
        channel="system",
        requisition_id=requirement.requisition_id,
        requirement_id=requirement_id,
        notes=reason,
    ))
    logger.info(
        "Auto-progressed requirement {} from {} to {}: {}",
        requirement_id, current, target_status, reason,
    )
    return True
```

In `app/routers/crm/offers.py`, add after the `approve_offer` endpoint's `db.commit()` (around line 612):

```python
    # Auto-progress sourcing status on offer approval
    if offer.requirement_id:
        from ..services.sourcing_auto_progress import auto_progress_sourcing
        auto_progress_sourcing(
            db, offer.requirement_id, "offered",
            "Auto-set to offered after offer approval",
            user_id=user.id,
        )
        db.commit()
```

In `app/routers/htmx_views.py`, find the HTMX offer approve block (around line 1872-1882 where `action == "approve"`), add after `db.commit()`:

```python
    # Auto-progress sourcing status on offer approval
    if offer.requirement_id:
        from ..services.sourcing_auto_progress import auto_progress_sourcing
        auto_progress_sourcing(
            db, offer.requirement_id, "offered",
            "Auto-set to offered after offer approval",
            user_id=user.id,
        )
        db.commit()
```

Also update `sightings_send_inquiry` in `app/routers/sightings.py` to use the shared helper instead of inline logic:

```python
        # AUTO-PROGRESS: only on confirmed send success, forward-only
        from ..services.sourcing_auto_progress import auto_progress_sourcing
        for r in requirements:
            auto_progress_sourcing(
                db, r.id, SourcingStatus.SOURCING,
                "Auto-set to sourcing after RFQ send",
                user_id=user.id,
            )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestAutoProgressOnOfferApproval -v
```

- [ ] **Step 5: Commit**

```bash
git add app/services/sourcing_auto_progress.py app/routers/crm/offers.py app/routers/htmx_views.py app/routers/sightings.py tests/test_sightings_router.py
git commit -m "feat: extract sourcing auto-progress service and hook into offer approval"
```

---

### Task 7: Batch Assign Endpoint

**Files:** `app/routers/sightings.py`, `tests/test_sightings_router.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_sightings_router.py`, add:

```python
class TestBatchAssign:
    """POST /v2/partials/sightings/batch-assign"""

    def test_assigns_buyer(self, client, db_session, test_user):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": json.dumps([r.id]), "buyer_id": str(test_user.id)},
        )
        assert resp.status_code == 200
        db_session.refresh(r)
        assert r.assigned_buyer_id == test_user.id

    def test_assigns_multiple(self, client, db_session, test_user):
        req, r1, _ = _seed_data(db_session)
        r2 = Requirement(
            requisition_id=req.id, primary_mpn="MPN-002",
            target_qty=50, sourcing_status="open",
        )
        db_session.add(r2)
        db_session.commit()
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": json.dumps([r1.id, r2.id]), "buyer_id": str(test_user.id)},
        )
        assert resp.status_code == 200
        db_session.refresh(r1)
        db_session.refresh(r2)
        assert r1.assigned_buyer_id == test_user.id
        assert r2.assigned_buyer_id == test_user.id

    def test_empty_ids_returns_400(self, client, db_session, test_user):
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": json.dumps([]), "buyer_id": str(test_user.id)},
        )
        assert resp.status_code == 400

    def test_over_max_batch_returns_400(self, client, db_session, test_user):
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": json.dumps(list(range(51))), "buyer_id": str(test_user.id)},
        )
        assert resp.status_code == 400

    def test_returns_toast(self, client, db_session, test_user):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": json.dumps([r.id]), "buyer_id": str(test_user.id)},
        )
        assert "toast" in resp.text.lower() or "Assigned" in resp.text

    def test_unassign_with_empty_buyer(self, client, db_session, test_user):
        _, r, _ = _seed_data(db_session)
        r.assigned_buyer_id = test_user.id
        db_session.commit()
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": json.dumps([r.id]), "buyer_id": ""},
        )
        assert resp.status_code == 200
        db_session.refresh(r)
        assert r.assigned_buyer_id is None

    def test_nonexistent_ids_skipped(self, client, db_session, test_user):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": json.dumps([r.id, 99999]), "buyer_id": str(test_user.id)},
        )
        assert resp.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestBatchAssign -v
```

- [ ] **Step 3: Write minimal implementation**

In `app/routers/sightings.py`, add after `sightings_advance_status`:

```python
MAX_BATCH_SIZE = 50


@router.post("/v2/partials/sightings/batch-assign", response_class=HTMLResponse)
async def sightings_batch_assign(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Batch assign a buyer to multiple requirements."""
    form = await request.form()
    req_ids_raw = form.get("requirement_ids", "[]")
    buyer_id_str = form.get("buyer_id", "")

    requirement_ids = json.loads(req_ids_raw) if isinstance(req_ids_raw, str) else []
    buyer_id = int(buyer_id_str) if buyer_id_str else None

    if not requirement_ids:
        raise HTTPException(status_code=400, detail="requirement_ids required")
    if len(requirement_ids) > MAX_BATCH_SIZE:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_BATCH_SIZE} requirements per batch")

    requirements = db.query(Requirement).filter(Requirement.id.in_(requirement_ids)).all()
    for r in requirements:
        r.assigned_buyer_id = buyer_id
    db.commit()

    msg = f"Assigned {len(requirements)} requirement{'s' if len(requirements) != 1 else ''}."
    return _oob_toast(msg)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestBatchAssign -v
```

- [ ] **Step 5: Commit**

```bash
git add app/routers/sightings.py tests/test_sightings_router.py
git commit -m "feat(sightings): add batch-assign endpoint"
```

---

### Task 8: Batch Status Endpoint

**Files:** `app/routers/sightings.py`, `tests/test_sightings_router.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_sightings_router.py`, add:

```python
class TestBatchStatus:
    """POST /v2/partials/sightings/batch-status"""

    def test_updates_status(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([r.id]), "status": "sourcing"},
        )
        assert resp.status_code == 200
        db_session.refresh(r)
        assert r.sourcing_status == "sourcing"

    def test_skips_invalid_transitions(self, client, db_session):
        """open -> won is invalid; should be skipped, not crash."""
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([r.id]), "status": "won"},
        )
        assert resp.status_code == 200
        db_session.refresh(r)
        assert r.sourcing_status == "open"  # unchanged
        assert "skipped" in resp.text.lower() or "0" in resp.text

    def test_mixed_valid_invalid(self, client, db_session):
        req, r1, _ = _seed_data(db_session)
        r2 = Requirement(
            requisition_id=req.id, primary_mpn="MPN-002",
            target_qty=50, sourcing_status="sourcing",
        )
        db_session.add(r2)
        db_session.commit()
        # Both to "offered": r1 (open->offered) invalid, r2 (sourcing->offered) valid
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([r1.id, r2.id]), "status": "offered"},
        )
        assert resp.status_code == 200
        db_session.refresh(r1)
        db_session.refresh(r2)
        assert r1.sourcing_status == "open"  # unchanged (invalid)
        assert r2.sourcing_status == "offered"  # changed (valid)

    def test_empty_ids_returns_400(self, client, db_session):
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([]), "status": "sourcing"},
        )
        assert resp.status_code == 400

    def test_over_max_returns_400(self, client, db_session):
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps(list(range(51))), "status": "sourcing"},
        )
        assert resp.status_code == 400

    def test_creates_activity_logs(self, client, db_session):
        from app.models.intelligence import ActivityLog
        _, r, _ = _seed_data(db_session)
        client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([r.id]), "status": "sourcing"},
        )
        activity = db_session.query(ActivityLog).filter(
            ActivityLog.requirement_id == r.id,
            ActivityLog.activity_type == "status_change",
        ).first()
        assert activity is not None

    def test_empty_status_returns_400(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([r.id]), "status": ""},
        )
        assert resp.status_code == 400

    def test_report_message(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([r.id]), "status": "sourcing"},
        )
        assert "1" in resp.text

    def test_all_same_status_noop(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([r.id]), "status": "open"},
        )
        assert resp.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestBatchStatus -v
```

- [ ] **Step 3: Write minimal implementation**

In `app/routers/sightings.py`, add after `sightings_batch_assign`:

```python
@router.post("/v2/partials/sightings/batch-status", response_class=HTMLResponse)
async def sightings_batch_status(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Batch status change with per-requirement transition validation."""
    from ..services.status_machine import validate_transition

    form = await request.form()
    req_ids_raw = form.get("requirement_ids", "[]")
    new_status = form.get("status", "").strip()

    requirement_ids = json.loads(req_ids_raw) if isinstance(req_ids_raw, str) else []

    if not requirement_ids:
        raise HTTPException(status_code=400, detail="requirement_ids required")
    if not new_status:
        raise HTTPException(status_code=400, detail="status required")
    if len(requirement_ids) > MAX_BATCH_SIZE:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_BATCH_SIZE} requirements per batch")

    requirements = db.query(Requirement).filter(Requirement.id.in_(requirement_ids)).all()
    updated = 0
    skipped = 0

    for r in requirements:
        if r.sourcing_status == new_status:
            skipped += 1
            continue
        if not validate_transition("requirement", r.sourcing_status, new_status, raise_on_invalid=False):
            skipped += 1
            continue
        old_status = r.sourcing_status
        r.sourcing_status = new_status
        db.add(ActivityLog(
            user_id=user.id,
            activity_type="status_change",
            channel="manual",
            requisition_id=r.requisition_id,
            requirement_id=r.id,
            notes=f"Batch status change: {old_status} -> {new_status}",
        ))
        updated += 1

    db.commit()

    total = updated + skipped
    msg = f"Updated {updated} of {total} requirements."
    if skipped:
        msg += f" {skipped} skipped (invalid transition or unchanged)."
    return _oob_toast(msg, "warning" if skipped else "success")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestBatchStatus -v
```

- [ ] **Step 5: Commit**

```bash
git add app/routers/sightings.py tests/test_sightings_router.py
git commit -m "feat(sightings): add batch-status endpoint with transition validation"
```

---

### Task 9: Batch Notes Endpoint

**Files:** `app/routers/sightings.py`, `tests/test_sightings_router.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_sightings_router.py`, add:

```python
class TestBatchNotes:
    """POST /v2/partials/sightings/batch-notes"""

    def test_creates_activity_per_requirement(self, client, db_session):
        from app.models.intelligence import ActivityLog
        req, r1, _ = _seed_data(db_session)
        r2 = Requirement(
            requisition_id=req.id, primary_mpn="MPN-002",
            target_qty=50, sourcing_status="open",
        )
        db_session.add(r2)
        db_session.commit()
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps([r1.id, r2.id]), "notes": "Batch note"},
        )
        assert resp.status_code == 200
        count = db_session.query(ActivityLog).filter(
            ActivityLog.activity_type == "note",
            ActivityLog.notes == "Batch note",
        ).count()
        assert count == 2

    def test_empty_notes_returns_400(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps([r.id]), "notes": ""},
        )
        assert resp.status_code == 400

    def test_empty_ids_returns_400(self, client, db_session):
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps([]), "notes": "hello"},
        )
        assert resp.status_code == 400

    def test_over_max_returns_400(self, client, db_session):
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps(list(range(51))), "notes": "hello"},
        )
        assert resp.status_code == 400

    def test_returns_toast_with_count(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps([r.id]), "notes": "Test note"},
        )
        assert "1" in resp.text

    def test_nonexistent_ids_excluded_from_count(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps([r.id, 99999]), "notes": "Test note"},
        )
        assert resp.status_code == 200

    def test_whitespace_only_notes_returns_400(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps([r.id]), "notes": "   "},
        )
        assert resp.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestBatchNotes -v
```

- [ ] **Step 3: Write minimal implementation**

In `app/routers/sightings.py`, add after `sightings_batch_status`:

```python
@router.post("/v2/partials/sightings/batch-notes", response_class=HTMLResponse)
async def sightings_batch_notes(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Add a note to multiple requirements at once."""
    form = await request.form()
    req_ids_raw = form.get("requirement_ids", "[]")
    notes = form.get("notes", "").strip()

    requirement_ids = json.loads(req_ids_raw) if isinstance(req_ids_raw, str) else []

    if not requirement_ids:
        raise HTTPException(status_code=400, detail="requirement_ids required")
    if not notes:
        raise HTTPException(status_code=400, detail="notes required")
    if len(requirement_ids) > MAX_BATCH_SIZE:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_BATCH_SIZE} requirements per batch")

    requirements = db.query(Requirement).filter(Requirement.id.in_(requirement_ids)).all()
    for r in requirements:
        db.add(ActivityLog(
            user_id=user.id,
            activity_type="note",
            channel="manual",
            requisition_id=r.requisition_id,
            requirement_id=r.id,
            notes=notes,
        ))
    db.commit()

    msg = f"Note added to {len(requirements)} requirement{'s' if len(requirements) != 1 else ''}."
    return _oob_toast(msg)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestBatchNotes -v
```

- [ ] **Step 5: Commit**

```bash
git add app/routers/sightings.py tests/test_sightings_router.py
git commit -m "feat(sightings): add batch-notes endpoint"
```

---

### Task 10: Email Preview Endpoint

**Files:** `app/routers/sightings.py`, `app/templates/htmx/partials/sightings/preview.html`, `tests/test_sightings_router.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_sightings_router.py`, add:

```python
class TestPreviewInquiry:
    """POST /v2/partials/sightings/preview-inquiry"""

    def test_returns_preview_html(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={
                "requirement_ids": str(r.id),
                "vendor_names": "Good Vendor",
                "email_body": "Please quote these parts",
            },
        )
        assert resp.status_code == 200
        assert "Good Vendor" in resp.text
        assert "Please quote" in resp.text

    def test_empty_body_returns_400(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={
                "requirement_ids": str(r.id),
                "vendor_names": "Good Vendor",
                "email_body": "",
            },
        )
        assert resp.status_code == 400

    def test_missing_vendor_returns_400(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={
                "requirement_ids": str(r.id),
                "vendor_names": "",
                "email_body": "Hello",
            },
        )
        assert resp.status_code == 400

    def test_shows_no_email_warning(self, client, db_session):
        """Vendor with no email should show warning in preview."""
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={
                "requirement_ids": str(r.id),
                "vendor_names": "Unknown Vendor",
                "email_body": "Hello",
            },
        )
        assert resp.status_code == 200
        assert "no email" in resp.text.lower() or "No email" in resp.text
```

- [ ] **Step 2: Run test to verify it fails**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestPreviewInquiry -v
```

- [ ] **Step 3: Write minimal implementation**

Add endpoint in `app/routers/sightings.py`:

```python
@router.post("/v2/partials/sightings/preview-inquiry", response_class=HTMLResponse)
async def sightings_preview_inquiry(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Render email preview per vendor before sending."""
    form = await request.form()
    requirement_ids = [int(x) for x in form.getlist("requirement_ids") if x.strip().isdigit()]
    vendor_names = [v for v in form.getlist("vendor_names") if v.strip()]
    email_body = form.get("email_body", "").strip()

    if not requirement_ids or not vendor_names or not email_body:
        raise HTTPException(
            status_code=400,
            detail="requirement_ids, vendor_names, and email_body required",
        )

    requirements = db.query(Requirement).filter(Requirement.id.in_(requirement_ids)).all()
    parts = [{"mpn": r.primary_mpn, "qty": r.target_qty} for r in requirements]

    # Batch lookup vendor emails
    normalized_names = [normalize_vendor_name(vn) for vn in vendor_names]
    cards = db.query(VendorCard).filter(VendorCard.normalized_name.in_(normalized_names)).all()
    card_map = {c.normalized_name: c for c in cards}

    card_ids = [c.id for c in cards]
    contacts = db.query(VendorContact).filter(VendorContact.vendor_card_id.in_(card_ids)).all() if card_ids else []
    contact_map = {c.vendor_card_id: c for c in contacts}

    previews = []
    for vn in vendor_names:
        card = card_map.get(normalize_vendor_name(vn))
        vendor_email = ""
        if card:
            contact = contact_map.get(card.id)
            if contact and contact.email:
                vendor_email = contact.email

        previews.append({
            "vendor_name": vn,
            "vendor_email": vendor_email,
            "subject": f"RFQ — {len(requirements)} part{'s' if len(requirements) != 1 else ''}",
            "body": email_body,
            "parts": parts,
        })

    ctx = {
        "request": request,
        "previews": previews,
        "requirement_ids": requirement_ids,
    }
    return templates.TemplateResponse("htmx/partials/sightings/preview.html", ctx)
```

Create `app/templates/htmx/partials/sightings/preview.html`:

```html
{# Email preview before sending RFQs.
   Called by: vendor_modal.html step 2 (via POST /v2/partials/sightings/preview-inquiry)
   Depends on: HTMX, Alpine.js
   Context: previews (list of {vendor_name, vendor_email, subject, body, parts}), requirement_ids
#}

<div class="p-4 space-y-4">
  <h3 class="text-lg font-semibold text-gray-900">Preview Emails</h3>
  <p class="text-xs text-gray-500">Review before sending to {{ previews|length }} vendor{{ 's' if previews|length != 1 else '' }}.</p>

  <div class="space-y-3 max-h-96 overflow-y-auto">
    {% for p in previews %}
    <div class="border border-gray-200 rounded-lg p-3">
      <div class="flex items-center justify-between mb-2">
        <span class="text-sm font-medium text-gray-700">{{ p.vendor_name }}</span>
        {% if p.vendor_email %}
        <span class="text-xs text-gray-400">{{ p.vendor_email }}</span>
        {% else %}
        <span class="text-xs text-amber-600 font-medium">No email on file</span>
        {% endif %}
      </div>
      <div class="text-xs text-gray-500 mb-1">
        <span class="font-medium">Subject:</span> {{ p.subject }}
      </div>
      <div class="text-xs text-gray-500 mb-2">
        <span class="font-medium">Parts:</span>
        {% for part in p.parts %}
        {{ part.mpn }} ({{ part.qty }} pcs){{ ', ' if not loop.last else '' }}
        {% endfor %}
      </div>
      <div class="text-xs text-gray-600 bg-gray-50 rounded p-2 whitespace-pre-wrap">{{ p.body }}</div>
    </div>
    {% endfor %}
  </div>

  <div class="flex justify-end gap-2 pt-2 border-t border-gray-100">
    <button @click="$dispatch('preview-back')"
            class="px-4 py-2 text-sm font-medium text-gray-600 hover:bg-gray-50 rounded-lg transition-colors">
      Back
    </button>
    <button @click="$dispatch('preview-confirm')"
            class="px-4 py-2 text-sm font-medium text-white bg-brand-500 hover:bg-brand-600 rounded-lg transition-colors">
      Send {{ previews|length }} Email{{ 's' if previews|length != 1 else '' }}
    </button>
  </div>
</div>
```

- [ ] **Step 4: Run test to verify it passes**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestPreviewInquiry -v
```

- [ ] **Step 5: Commit**

```bash
git add app/routers/sightings.py app/templates/htmx/partials/sightings/preview.html tests/test_sightings_router.py
git commit -m "feat(sightings): add email preview endpoint and template"
```

---

### Task 11: Vendor Modal Preview Step

**Files:** `app/templates/htmx/partials/sightings/vendor_modal.html`

- [ ] **Step 1: Add Alpine multi-step flow to vendor modal**

In `app/templates/htmx/partials/sightings/vendor_modal.html`, wrap the existing content in a step system. Replace the opening `x-data` line (line 7) with:

```html
<div class="p-4" x-data="{
  step: 1,
  selectedVendors: new Set({{ suggested_vendors|map(attribute='normalized_name')|list|tojson }}),
  emailBody: '',
  cleaning: false,
  previewHtml: '',
  toggleVendor(name) {
    if (this.selectedVendors.has(name)) this.selectedVendors.delete(name);
    else this.selectedVendors.add(name);
  },
  async loadPreview() {
    const form = new FormData();
    {{ requirement_ids|tojson }}.forEach(id => form.append('requirement_ids', id));
    this.selectedVendors.forEach(v => form.append('vendor_names', v));
    form.append('email_body', this.emailBody);
    const resp = await fetch('/v2/partials/sightings/preview-inquiry', {method: 'POST', body: form});
    this.previewHtml = await resp.text();
    this.step = 2;
  }
}" @preview-back.window="step = 1"
   @preview-confirm.window="
    const form = new FormData();
    {{ requirement_ids|tojson }}.forEach(id => form.append('requirement_ids', id));
    selectedVendors.forEach(v => form.append('vendor_names', v));
    form.append('email_body', emailBody);
    htmx.ajax('POST', '/v2/partials/sightings/send-inquiry', {values: Object.fromEntries(form), target: '#sightings-table'});
    $dispatch('close-modal');
  ">
```

Wrap the existing compose form in `<div x-show="step === 1">...</div>`.

After the compose form (before the closing `</div>` of the main container), add:

```html
  {# Step 2: Preview #}
  <div x-show="step === 2" x-html="previewHtml" x-cloak></div>
```

Replace the "Send to N Vendors" button's `@click` handler with:

```html
@click="loadPreview()"
```

- [ ] **Step 2: Verify existing modal test still works**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -k "vendor_modal or send_inquiry" -v
```

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/sightings/vendor_modal.html
git commit -m "feat(sightings): add email preview step to vendor modal"
```

---

### Task 12: Cross-Requirement Vendor Overlap

**Files:** `app/routers/sightings.py`, `app/templates/htmx/partials/sightings/detail.html`, `tests/test_sightings_router.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_sightings_router.py`, add:

```python
class TestVendorOverlap:
    """Cross-requirement vendor overlap badges in detail panel."""

    def test_overlap_in_context(self, client, db_session):
        req, r1, _ = _seed_data(db_session)
        r2 = Requirement(
            requisition_id=req.id, primary_mpn="MPN-002",
            target_qty=50, sourcing_status="open",
        )
        db_session.add(r2)
        db_session.flush()
        # Same vendor on both requirements
        s2 = VendorSightingSummary(
            requirement_id=r2.id, vendor_name="Good Vendor",
            estimated_qty=100, listing_count=1, score=50.0, tier="OK",
        )
        db_session.add(s2)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r1.id}/detail")
        assert resp.status_code == 200
        # The overlap badge should mention the other requirement
        assert "other" in resp.text.lower() or "also on" in resp.text.lower() or "overlap" in resp.text.lower()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestVendorOverlap -v
```

- [ ] **Step 3: Write minimal implementation**

In `app/routers/sightings.py`, modify `sightings_detail()` to compute overlap counts. Add before the `ctx` dict construction:

```python
    # Cross-requirement vendor overlap
    vendor_names_list = [s.vendor_name for s in summaries]
    overlap_counts = {}
    if vendor_names_list:
        overlap_raw = (
            db.query(
                VendorSightingSummary.vendor_name,
                sqlfunc.count(sqlfunc.distinct(VendorSightingSummary.requirement_id)),
            )
            .join(Requirement, VendorSightingSummary.requirement_id == Requirement.id)
            .join(Requisition, Requirement.requisition_id == Requisition.id)
            .filter(
                Requisition.status == RequisitionStatus.ACTIVE,
                VendorSightingSummary.vendor_name.in_(vendor_names_list),
            )
            .group_by(VendorSightingSummary.vendor_name)
            .having(sqlfunc.count(sqlfunc.distinct(VendorSightingSummary.requirement_id)) > 1)
            .all()
        )
        overlap_counts = {row[0]: row[1] - 1 for row in overlap_raw}  # subtract current req
```

Add `"overlap_counts": overlap_counts` to the `ctx` dict.

In `app/templates/htmx/partials/sightings/detail.html`, in each vendor row, add the overlap badge:

```html
{% if overlap_counts.get(s.vendor_name) %}
<span class="text-[10px] text-blue-500 bg-blue-50 rounded px-1.5 py-0.5">
  Also on {{ overlap_counts[s.vendor_name] }} other req{{ 's' if overlap_counts[s.vendor_name] != 1 else '' }}
</span>
{% endif %}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestVendorOverlap -v
```

- [ ] **Step 5: Commit**

```bash
git add app/routers/sightings.py app/templates/htmx/partials/sightings/detail.html tests/test_sightings_router.py
git commit -m "feat(sightings): add cross-requirement vendor overlap badges"
```

---

### Task 13: Parallelize Batch-Refresh

**Files:** `app/routers/sightings.py`, `tests/test_sightings_router.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_sightings_router.py`, add:

```python
class TestParallelBatchRefresh:
    """Batch refresh uses asyncio.gather with per-task sessions."""

    def test_batch_refresh_still_works(self, client, db_session):
        """Basic regression — batch refresh returns success toast."""
        _, r, _ = _seed_data(db_session)
        with patch("app.search_service.search_requirement", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = []
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([r.id])},
            )
        assert resp.status_code == 200
        assert "1" in resp.text

    def test_concurrent_failures_reported(self, client, db_session):
        req, r1, _ = _seed_data(db_session)
        r2 = Requirement(
            requisition_id=req.id, primary_mpn="MPN-002",
            target_qty=50, sourcing_status="open",
        )
        db_session.add(r2)
        db_session.commit()
        call_count = 0
        async def _mock_search(req_obj, db):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Search failed")
            return []

        with patch("app.search_service.search_requirement", side_effect=_mock_search):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([r1.id, r2.id])},
            )
        assert resp.status_code == 200
        assert "failed" in resp.text.lower() or "1" in resp.text
```

- [ ] **Step 2: Run test to verify it fails**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestParallelBatchRefresh -v
```

Expected: Tests may pass or fail depending on current behavior. The key change is the parallel implementation.

- [ ] **Step 3: Write minimal implementation**

In `app/routers/sightings.py`, add `asyncio` to imports:

```python
import asyncio
```

Replace the `sightings_batch_refresh` endpoint body with:

```python
@router.post("/v2/partials/sightings/batch-refresh", response_class=HTMLResponse)
async def sightings_batch_refresh(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Refresh sightings for multiple requirements using parallel search."""
    from ..database import SessionLocal
    from ..search_service import search_requirement

    form = await request.form()
    req_ids_raw = form.get("requirement_ids", "[]")
    requirement_ids = json.loads(req_ids_raw) if isinstance(req_ids_raw, str) else []

    if not requirement_ids:
        return _oob_toast("No requirements selected.", "warning")
    if len(requirement_ids) > MAX_BATCH_SIZE:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_BATCH_SIZE} requirements per batch")

    sem = asyncio.Semaphore(5)

    async def _refresh_one(req_id: int) -> bool:
        async with sem:
            task_db = SessionLocal()
            try:
                req_obj = task_db.get(Requirement, int(req_id))
                if not req_obj:
                    return False
                await search_requirement(req_obj, task_db)
                return True
            except Exception:
                logger.warning("Batch refresh failed for requirement %s", req_id, exc_info=True)
                return False
            finally:
                task_db.close()

    results = await asyncio.gather(*[_refresh_one(rid) for rid in requirement_ids])
    success = sum(1 for r in results if r)
    failed = len(results) - success

    msg = f"Refreshed {success}/{success + failed} requirements."
    if failed:
        msg += f" {failed} failed."
    return _oob_toast(msg, "warning" if failed else "success")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestParallelBatchRefresh -v
```

- [ ] **Step 5: Commit**

```bash
git add app/routers/sightings.py tests/test_sightings_router.py
git commit -m "feat(sightings): parallelize batch-refresh with asyncio.gather and per-task sessions"
```

---

### Task 14: Per-Vendor RFQ Button in Detail Panel

**Files:** `app/templates/htmx/partials/sightings/detail.html`

- [ ] **Step 1: Add per-vendor RFQ button**

In `app/templates/htmx/partials/sightings/detail.html`, in each vendor row's actions area, add:

```html
<button @click="$dispatch('open-modal', {
  url: '/v2/partials/sightings/vendor-modal?requirement_ids={{ requirement.id }}&preselect={{ s.vendor_name|urlencode }}'
})"
        title="Send RFQ to {{ s.vendor_name }}"
        class="p-1 text-gray-400 hover:text-brand-500 transition-colors">
  <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
          d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"/>
  </svg>
</button>
```

- [ ] **Step 2: Add preselect support to vendor modal endpoint**

In `app/routers/sightings.py`, modify `sightings_vendor_modal()` to accept `preselect` param. Add to the function signature:

```python
    preselect: str = "",
```

Pass `"preselect": preselect` in the `ctx` dict.

In `app/templates/htmx/partials/sightings/vendor_modal.html`, update the `selectedVendors` initialization:

```html
selectedVendors: new Set({% if preselect %}['{{ preselect }}']{% else %}{{ suggested_vendors|map(attribute='normalized_name')|list|tojson }}{% endif %}),
```

(The template receives `preselect` from context.)

- [ ] **Step 3: Verify the modal endpoint still works**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -k "vendor_modal" -v
```

- [ ] **Step 4: Commit**

```bash
git add app/routers/sightings.py app/templates/htmx/partials/sightings/detail.html app/templates/htmx/partials/sightings/vendor_modal.html
git commit -m "feat(sightings): add per-vendor RFQ button with preselect in vendor modal"
```

---

### Task 15: SSE Publish from Mutation Endpoints

**Files:** `app/routers/sightings.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_sightings_router.py`, add:

```python
class TestSSEPublish:
    """Mutation endpoints publish SSE events."""

    def test_log_activity_publishes(self, client, db_session):
        with patch("app.routers.sightings.broker") as mock_broker:
            mock_broker.publish = AsyncMock()
            _, r, _ = _seed_data(db_session)
            client.post(
                f"/v2/partials/sightings/{r.id}/log-activity",
                data={"type": "note", "notes": "Test"},
            )
            mock_broker.publish.assert_called()

    def test_advance_status_publishes(self, client, db_session):
        with patch("app.routers.sightings.broker") as mock_broker:
            mock_broker.publish = AsyncMock()
            _, r, _ = _seed_data(db_session)
            client.patch(
                f"/v2/partials/sightings/{r.id}/advance-status",
                data={"new_status": "sourcing"},
            )
            mock_broker.publish.assert_called()

    def test_batch_assign_publishes(self, client, db_session, test_user):
        with patch("app.routers.sightings.broker") as mock_broker:
            mock_broker.publish = AsyncMock()
            _, r, _ = _seed_data(db_session)
            client.post(
                "/v2/partials/sightings/batch-assign",
                data={"requirement_ids": json.dumps([r.id]), "buyer_id": str(test_user.id)},
            )
            mock_broker.publish.assert_called()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSSEPublish -v
```

- [ ] **Step 3: Write minimal implementation**

In `app/routers/sightings.py`, add the broker import at the top:

```python
from ..services.sse_broker import broker
```

Add a helper function after `_oob_toast`:

```python
async def _publish_sighting_event(user_id: int, event: str, requirement_id: int):
    """Publish an SSE event for sighting changes."""
    await broker.publish(
        f"user:{user_id}",
        event,
        json.dumps({"requirement_id": requirement_id}),
    )
```

Add calls at the end of each mutation endpoint (before `return`):

In `sightings_log_activity`, before `return`:
```python
    await _publish_sighting_event(user.id, "sighting-updated", requirement_id)
```

In `sightings_advance_status`, before `return`:
```python
    await _publish_sighting_event(user.id, "sighting-updated", requirement_id)
```

In `sightings_batch_assign`, before `return`:
```python
    for r in requirements:
        await _publish_sighting_event(user.id, "sighting-updated", r.id)
```

In `sightings_batch_status`, before `return`:
```python
    for r in requirements:
        await _publish_sighting_event(user.id, "sighting-updated", r.id)
```

In `sightings_batch_notes`, before `return`:
```python
    for r in requirements:
        await _publish_sighting_event(user.id, "sighting-updated", r.id)
```

In `sightings_send_inquiry`, inside the `try` block after `db.commit()`:
```python
    for r in requirements:
        await _publish_sighting_event(user.id, "sighting-updated", r.id)
```

In `sightings_refresh`, before `return`:
```python
    await _publish_sighting_event(user.id, "sighting-updated", requirement_id)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSSEPublish -v
```

- [ ] **Step 5: Commit**

```bash
git add app/routers/sightings.py tests/test_sightings_router.py
git commit -m "feat(sightings): publish SSE events from all mutation endpoints"
```

---

### Task 16: SSE Live Updates in Workspace Template

**Files:** `app/templates/htmx/partials/sightings/list.html`

- [ ] **Step 1: Add SSE connection to workspace**

In `app/templates/htmx/partials/sightings/list.html`, add the SSE connection on the workspace container. Inside the main wrapper div:

```html
{# SSE Live Updates — auto-refresh detail panel on server events #}
<div hx-ext="sse" sse-connect="/api/events/stream" id="sightings-sse">
  {# Listen for sighting updates and refresh detail panel if it matches the current selection #}
  <div sse-swap="sighting-updated" hx-swap="none"
       hx-on::sse-message="
         try {
           const data = JSON.parse(event.data);
           const currentId = $store.sightingSelection && $store.sightingSelection.current;
           if (data.requirement_id && data.requirement_id == currentId) {
             htmx.ajax('GET', '/v2/partials/sightings/' + data.requirement_id + '/detail', {target: '#sightings-detail'});
           }
         } catch(e) {}
       ">
  </div>
</div>
```

- [ ] **Step 2: Verify existing workspace test passes**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSightingsListPartial -v
```

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/sightings/list.html
git commit -m "feat(sightings): add SSE live update listener to workspace"
```

---

### Task 17: SSE Disconnect Banner

**Files:** `app/templates/htmx/partials/sightings/list.html`, `app/static/htmx_app.js`

- [ ] **Step 1: Add disconnect banner HTML**

In `app/templates/htmx/partials/sightings/list.html`, add after the SSE connection div:

```html
{# SSE Disconnect Banner — shown after 30s without connection #}
<div x-data="{ disconnected: false, timer: null }"
     @htmx:sse-open.window="disconnected = false; clearTimeout(timer)"
     @htmx:sse-error.window="timer = setTimeout(() => disconnected = true, 30000)"
     x-show="disconnected" x-cloak x-transition
     class="bg-amber-50 border-b border-amber-200 px-4 py-2 flex items-center justify-between">
  <span class="text-xs text-amber-700">
    <svg class="w-3.5 h-3.5 inline mr-1" fill="currentColor" viewBox="0 0 20 20">
      <path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd"/>
    </svg>
    Live updates paused
  </span>
  <button @click="window.location.reload()"
          class="text-xs font-medium text-amber-700 hover:text-amber-900 underline">
    Refresh
  </button>
</div>
```

- [ ] **Step 2: Verify no test regressions**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v --timeout=30
```

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/sightings/list.html
git commit -m "feat(sightings): add SSE disconnect banner with 30s timeout"
```

---

### Task 18: Loading Skeletons

**Files:** `app/templates/htmx/partials/sightings/detail.html`

- [ ] **Step 1: Add skeleton loading state**

In `app/templates/htmx/partials/sightings/detail.html`, at the very top of the file, add a skeleton that shows while the real content loads. Wrap in HTMX loading states:

```html
{# Skeleton displayed during HTMX request for detail panel #}
<div id="sightings-detail-skeleton" class="htmx-indicator p-4 space-y-4 animate-pulse">
  {# Header skeleton #}
  <div class="flex items-center gap-3">
    <div class="h-6 w-32 bg-gray-200 rounded"></div>
    <div class="h-5 w-20 bg-gray-200 rounded-full"></div>
  </div>
  {# Vendor rows skeleton — 5 rows #}
  {% for _ in range(5) %}
  <div class="flex items-center gap-3 py-2">
    <div class="h-4 w-28 bg-gray-200 rounded"></div>
    <div class="h-4 w-16 bg-gray-200 rounded"></div>
    <div class="h-4 w-12 bg-gray-200 rounded"></div>
    <div class="flex-1"></div>
    <div class="h-4 w-8 bg-gray-200 rounded"></div>
  </div>
  {% endfor %}
  {# Timeline skeleton #}
  <div class="h-4 w-24 bg-gray-200 rounded mt-4"></div>
  {% for _ in range(3) %}
  <div class="flex gap-2 py-1">
    <div class="h-3 w-3 bg-gray-200 rounded-full mt-0.5"></div>
    <div class="h-3 w-48 bg-gray-200 rounded"></div>
  </div>
  {% endfor %}
</div>
```

Also add an `hx-indicator="#sightings-detail-skeleton"` attribute on the table rows that trigger detail loads.

- [ ] **Step 2: Verify no test regressions**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSightingsDetailPartial -v
```

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/sightings/detail.html
git commit -m "feat(sightings): add loading skeleton for detail panel"
```

---

### Task 19: Crossfade Transitions

**Files:** `app/static/htmx_app.js`, `app/templates/htmx/partials/sightings/detail.html`, `app/templates/htmx/partials/sightings/table.html`

- [ ] **Step 1: Add transition CSS classes**

In `app/static/htmx_app.js`, add the following CSS injection at the top of the file (or in `styles.css` if preferred):

```javascript
// Sightings crossfade transition styles
const sightingsStyles = document.createElement('style');
sightingsStyles.textContent = `
  .sightings-fade { transition: opacity 150ms ease; }
  .sightings-fade.htmx-swapping { opacity: 0; }
  .sightings-fade.htmx-settling { opacity: 1; }
  .sightings-slide-down { animation: sightingsSlideDown 300ms ease; }
  @keyframes sightingsSlideDown {
    from { opacity: 0; transform: translateY(-8px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .sightings-slide-up { animation: sightingsSlideUp 200ms ease; }
  @keyframes sightingsSlideUp {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }
`;
document.head.appendChild(sightingsStyles);
```

- [ ] **Step 2: Apply crossfade class to detail panel swap target**

In `app/templates/htmx/partials/sightings/detail.html`, ensure the outer wrapper has:

```html
<div id="sightings-detail" class="sightings-fade">
```

In `app/templates/htmx/partials/sightings/table.html`, add the slide-up animation class to the action bar:

```html
<div id="sightings-action-bar" class="sightings-slide-up" x-show="$store.sightingSelection.count > 0">
```

- [ ] **Step 3: Verify no test regressions**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v --timeout=30
```

- [ ] **Step 4: Commit**

```bash
git add app/static/htmx_app.js app/templates/htmx/partials/sightings/detail.html app/templates/htmx/partials/sightings/table.html
git commit -m "feat(sightings): add crossfade and slide transitions"
```

---

### Task 20: Multi-Select "N on Other Pages" Badge

**Files:** `app/templates/htmx/partials/sightings/table.html`

- [ ] **Step 1: Add the badge to the action bar**

In `app/templates/htmx/partials/sightings/table.html`, in the multi-select action bar, add:

```html
{# "N on other pages" badge #}
<span x-data="{ get visibleCount() {
  let count = 0;
  document.querySelectorAll('[data-req-checkbox]').forEach(el => {
    if ($store.sightingSelection.has(parseInt(el.value))) count++;
  });
  return count;
}}"
      x-show="$store.sightingSelection.count > visibleCount"
      x-cloak
      class="text-xs text-gray-500 ml-2">
  + <span x-text="$store.sightingSelection.count - visibleCount"></span> on other pages
</span>
```

- [ ] **Step 2: Add `data-req-checkbox` attribute to row checkboxes**

Ensure each row checkbox in the table has:

```html
<input type="checkbox" data-req-checkbox :value="requirement.id" ...>
```

- [ ] **Step 3: Verify no test regressions**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v --timeout=30
```

- [ ] **Step 4: Commit**

```bash
git add app/templates/htmx/partials/sightings/table.html
git commit -m "feat(sightings): add 'N on other pages' badge for multi-select"
```

---

### Task 21: Responsive Breakpoints

**Files:** `app/templates/htmx/partials/sightings/table.html`, `app/templates/htmx/partials/sightings/list.html`

- [ ] **Step 1: Add responsive column hiding**

In `app/templates/htmx/partials/sightings/table.html`, add Tailwind responsive classes to table headers and cells:

For columns to hide at 1024-1279px (Customer, Sales, Priority):

```html
{# Customer column header #}
<th class="hidden xl:table-cell ...">Customer</th>

{# Sales column header #}
<th class="hidden xl:table-cell ...">Sales</th>

{# Priority column header #}
<th class="hidden xl:table-cell ...">Priority</th>
```

Apply matching `hidden xl:table-cell` to the corresponding `<td>` cells in each row.

- [ ] **Step 2: Add stacked layout below 1024px**

In `app/templates/htmx/partials/sightings/list.html`, add responsive split panel behavior:

```html
{# Split panel: side-by-side on lg+, stacked below #}
<div class="flex flex-col lg:flex-row ...">
  <div class="w-full lg:w-1/2 xl:w-3/5 ...">
    {# Table panel #}
  </div>
  <div class="w-full lg:w-1/2 xl:w-2/5 ...">
    {# Detail panel #}
  </div>
</div>
```

- [ ] **Step 3: Add card layout below 640px**

In `app/templates/htmx/partials/sightings/table.html`, add a card view that replaces the table on small screens:

```html
{# Mobile card view — visible below sm breakpoint #}
<div class="sm:hidden space-y-2">
  {% for r in requirements %}
  <div class="bg-white border border-gray-200 rounded-lg p-3 cursor-pointer"
       hx-get="/v2/partials/sightings/{{ r.id }}/detail"
       hx-target="#sightings-detail">
    <div class="flex items-center justify-between">
      <span class="font-mono text-sm font-medium text-gray-900">{{ r.primary_mpn }}</span>
      {% include "htmx/partials/shared/status_badge.html" with status=r.sourcing_status %}
    </div>
    <div class="flex items-center gap-3 mt-1 text-xs text-gray-500">
      <span>{{ r.target_qty }} pcs</span>
      {% if coverage_map.get(r.id) is not none %}
      <span>{{ ((coverage_map[r.id] / r.target_qty) * 100)|round|int }}% covered</span>
      {% endif %}
    </div>
  </div>
  {% endfor %}
</div>

{# Desktop table — hidden below sm breakpoint #}
<table class="hidden sm:table w-full ...">
  {# existing table markup #}
</table>
```

- [ ] **Step 4: Verify no test regressions**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v --timeout=30
```

- [ ] **Step 5: Commit**

```bash
git add app/templates/htmx/partials/sightings/table.html app/templates/htmx/partials/sightings/list.html
git commit -m "feat(sightings): add responsive breakpoints with column hiding and card layout"
```

---

### Task 22: Batch Action Bar Buttons in Table

**Files:**
- Modify: `app/templates/htmx/partials/sightings/table.html`
- Modify: `app/routers/sightings.py` — add `all_buyers` to `sightings_list()` context

- [ ] **Step 0: Add `all_buyers` to the table context**

In `app/routers/sightings.py`, in `sightings_list()`, add `all_buyers` to the context dict (use the cached version from Task 7 of Plan A):

```python
    all_buyers = _get_cached(
        "all_buyers", 300,
        lambda: db.query(User.id, User.name).filter(User.is_active.is_(True)).all()
    )
```

Add `"all_buyers": all_buyers` to the `ctx` dict passed to the table template.

- [ ] **Step 1: Add batch action bar with assign/status/notes buttons**

In `app/templates/htmx/partials/sightings/table.html`, replace or enhance the existing action bar to include all three batch operations:

```html
{# Batch action bar — visible when items selected #}
<div id="sightings-action-bar"
     x-show="$store.sightingSelection.count > 0"
     x-transition x-cloak
     class="sightings-slide-up sticky bottom-0 bg-white border-t border-gray-200 px-4 py-3 flex items-center gap-3 shadow-lg z-10">

  <span class="text-sm font-medium text-gray-700">
    <span x-text="$store.sightingSelection.count"></span> selected
  </span>

  {# Batch Assign #}
  <div x-data="{ showAssign: false }" class="relative">
    <button @click="showAssign = !showAssign"
            class="text-xs font-medium text-gray-600 hover:text-gray-900 bg-gray-100 hover:bg-gray-200 rounded px-3 py-1.5 transition-colors">
      Assign
    </button>
    <div x-show="showAssign" @click.outside="showAssign = false" x-cloak
         class="absolute bottom-full mb-1 left-0 bg-white border border-gray-200 rounded-lg shadow-lg p-2 min-w-48">
      <select id="batch-buyer-select" class="text-xs rounded border-gray-200 w-full mb-2">
        <option value="">Unassign</option>
        {# all_buyers loaded via hx-get when popover opens, or passed from sightings_list context #}
        {% for b in all_buyers|default([]) %}
        <option value="{{ b.id }}">{{ b.name }}</option>
        {% endfor %}
      </select>
      <button @click="
        const form = new FormData();
        form.append('requirement_ids', JSON.stringify($store.sightingSelection.array));
        form.append('buyer_id', document.getElementById('batch-buyer-select').value);
        htmx.ajax('POST', '/v2/partials/sightings/batch-assign', {values: Object.fromEntries(form)});
        showAssign = false;
      " class="w-full text-xs font-medium text-white bg-brand-500 hover:bg-brand-600 rounded px-3 py-1.5">
        Apply
      </button>
    </div>
  </div>

  {# Batch Status #}
  <div x-data="{ showStatus: false }" class="relative">
    <button @click="showStatus = !showStatus"
            class="text-xs font-medium text-gray-600 hover:text-gray-900 bg-gray-100 hover:bg-gray-200 rounded px-3 py-1.5 transition-colors">
      Status
    </button>
    <div x-show="showStatus" @click.outside="showStatus = false" x-cloak
         class="absolute bottom-full mb-1 left-0 bg-white border border-gray-200 rounded-lg shadow-lg p-2 min-w-40">
      {% for status in ['open', 'sourcing', 'offered', 'quoted', 'won', 'lost', 'archived'] %}
      <button @click="
        const form = new FormData();
        form.append('requirement_ids', JSON.stringify($store.sightingSelection.array));
        form.append('status', '{{ status }}');
        htmx.ajax('POST', '/v2/partials/sightings/batch-status', {values: Object.fromEntries(form)});
        showStatus = false;
      " class="block w-full text-left text-xs px-3 py-1.5 hover:bg-gray-50 rounded capitalize">
        {{ status }}
      </button>
      {% endfor %}
    </div>
  </div>

  {# Batch Notes #}
  <div x-data="{ showNotes: false, batchNote: '' }" class="relative">
    <button @click="showNotes = !showNotes"
            class="text-xs font-medium text-gray-600 hover:text-gray-900 bg-gray-100 hover:bg-gray-200 rounded px-3 py-1.5 transition-colors">
      Add Note
    </button>
    <div x-show="showNotes" @click.outside="showNotes = false" x-cloak
         class="absolute bottom-full mb-1 left-0 bg-white border border-gray-200 rounded-lg shadow-lg p-2 min-w-64">
      <textarea x-model="batchNote" rows="2" placeholder="Note for all selected..."
                class="w-full text-xs rounded border-gray-200 mb-2 resize-none"></textarea>
      <button @click="
        const form = new FormData();
        form.append('requirement_ids', JSON.stringify($store.sightingSelection.array));
        form.append('notes', batchNote);
        htmx.ajax('POST', '/v2/partials/sightings/batch-notes', {values: Object.fromEntries(form)});
        showNotes = false;
        batchNote = '';
      " :disabled="!batchNote.trim()"
         class="w-full text-xs font-medium text-white bg-brand-500 hover:bg-brand-600 rounded px-3 py-1.5 disabled:bg-gray-300">
        Add Note
      </button>
    </div>
  </div>

  {# Existing: Send to Vendors + Batch Refresh #}
  <div class="flex-1"></div>

  <button @click="$dispatch('open-modal', {
    url: '/v2/partials/sightings/vendor-modal?requirement_ids=' + $store.sightingSelection.array.join(',')
  })" class="text-xs font-medium text-white bg-brand-500 hover:bg-brand-600 rounded px-3 py-1.5 transition-colors">
    Send to Vendors
  </button>

  <button @click="
    const form = new FormData();
    form.append('requirement_ids', JSON.stringify($store.sightingSelection.array));
    htmx.ajax('POST', '/v2/partials/sightings/batch-refresh', {values: Object.fromEntries(form)});
  " class="text-xs font-medium text-gray-600 hover:text-gray-900 bg-gray-100 hover:bg-gray-200 rounded px-3 py-1.5 transition-colors">
    Refresh
  </button>
</div>
```

- [ ] **Step 2: Verify no test regressions**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v --timeout=30
```

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/sightings/table.html
git commit -m "feat(sightings): add batch assign/status/notes buttons to action bar"
```

---

### Task 23: Status Dropdown in Detail Panel

**Files:** `app/templates/htmx/partials/sightings/detail.html`

- [ ] **Step 1: Add status advancement dropdown**

In `app/templates/htmx/partials/sightings/detail.html`, in the detail panel header (next to buyer assignment), add:

```html
{# Status dropdown — advance via PATCH endpoint #}
<div x-data="{ open: false }" class="relative inline-block">
  <button @click="open = !open"
          class="inline-flex items-center gap-1 text-xs font-medium px-2 py-1 rounded
                 {% if requirement.sourcing_status == 'open' %}bg-gray-100 text-gray-600
                 {% elif requirement.sourcing_status == 'sourcing' %}bg-blue-50 text-blue-600
                 {% elif requirement.sourcing_status == 'offered' %}bg-amber-50 text-amber-600
                 {% elif requirement.sourcing_status == 'quoted' %}bg-purple-50 text-purple-600
                 {% elif requirement.sourcing_status == 'won' %}bg-green-50 text-green-600
                 {% elif requirement.sourcing_status == 'lost' %}bg-red-50 text-red-600
                 {% else %}bg-gray-100 text-gray-500{% endif %}
                 hover:ring-1 hover:ring-gray-300 transition-all">
    <span class="capitalize">{{ requirement.sourcing_status }}</span>
    <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
    </svg>
  </button>
  <div x-show="open" @click.outside="open = false" x-cloak x-transition
       class="absolute left-0 top-full mt-1 bg-white border border-gray-200 rounded-lg shadow-lg py-1 min-w-32 z-20">
    {% for status in ['open', 'sourcing', 'offered', 'quoted', 'won', 'lost', 'archived'] %}
    {% if status != requirement.sourcing_status %}
    <button hx-patch="/v2/partials/sightings/{{ requirement.id }}/advance-status"
            hx-vals='{"new_status": "{{ status }}"}'
            hx-target="#sightings-detail"
            hx-swap="innerHTML transition:true"
            @click="open = false"
            class="block w-full text-left text-xs px-3 py-1.5 hover:bg-gray-50 capitalize">
      {{ status }}
    </button>
    {% endif %}
    {% endfor %}
  </div>
</div>
```

- [ ] **Step 2: Verify no test regressions**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSightingsDetailPartial -v
```

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/sightings/detail.html
git commit -m "feat(sightings): add status advancement dropdown to detail panel header"
```

---

### Task 24: Final Integration Test

**Files:** `tests/test_sightings_router.py`

- [ ] **Step 1: Run all sightings tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v --timeout=30
```

Expected: All tests pass (existing + ~51 new tests).

- [ ] **Step 2: Run full test suite for regression check**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --timeout=30
```

- [ ] **Step 3: Lint check**

```bash
cd /root/availai && ruff check app/routers/sightings.py app/schemas/sightings.py app/services/sourcing_auto_progress.py
```

- [ ] **Step 4: Commit any fixes**

```bash
git add -u
git commit -m "fix: address lint and test issues from Plan C implementation"
```
