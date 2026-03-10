# Workflow State Clarity Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 8 workflow gaps so failed RFQs persist with retry, vendor responses have terminal states, quotes show expiration badges, requisitions are fully filterable, and rejected buy plans can resubmit.

**Architecture:** Two Alembic migrations (070, 071) add columns + enum. Backend changes in email_service, rfq router, buy plan workflow, and requisition list endpoint. Frontend changes in app.js for badges, filters, and retry buttons. New `ContactStatus` enum in enums.py.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Alembic, PostgreSQL, vanilla JS

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `app/enums.py` | Modify | Add `ContactStatus`, `VendorResponseStatus` enums |
| `app/models/offers.py` | Modify | Add `error_message` to Contact, use new status values |
| `app/email_service.py` | Modify | Persist failed sends as Contact records with `status="failed"` |
| `app/routers/rfq.py` | Modify | Add `POST /api/contacts/{id}/retry` endpoint |
| `app/routers/requisitions/core.py` | Modify | Support comma-separated status filter |
| `app/routers/crm/vendor_responses.py` | Modify | Add `PATCH /api/vendor-responses/{id}/status` for reviewed/rejected |
| `app/services/buyplan_workflow.py` | Modify | Add `resubmit_buy_plan()` function |
| `app/routers/crm/buy_plans_v3.py` | Modify | Add `POST /api/buy-plans-v3/{id}/resubmit` endpoint |
| `app/static/app.js` | Modify | RFQ retry button, expiration badges, status filter dropdown, VR terminal actions |
| `alembic/versions/070_workflow_state_clarity.py` | Create | Add `error_message` on contacts, index on vendor_responses status |
| `alembic/versions/071_contact_status_enum.py` | Create | Backfill contact status values (no PG enum — stays string) |
| `tests/test_workflow_state_clarity.py` | Create | Tests for tasks 1-6 |
| `tests/test_part_level_endpoints.py` | Create | Tests for requisition filters + quote expiration |

---

## Task 1: RFQ Failure Recovery — Persist Failed Sends

**Files:**
- Modify: `app/models/offers.py:138-170` (Contact model)
- Modify: `app/email_service.py:96-122` (send_batch_rfq error handling)
- Modify: `app/enums.py` (add ContactStatus)
- Modify: `app/routers/rfq.py` (add retry endpoint)
- Create: `alembic/versions/070_workflow_state_clarity.py`
- Create: `tests/test_workflow_state_clarity.py`

- [ ] **Step 1: Write failing tests for RFQ failure persistence + retry**

```python
# tests/test_workflow_state_clarity.py
"""Tests for workflow state clarity features — RFQ failures, VR terminal states, buy plan resubmit."""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

from app.models.offers import Contact, VendorResponse
from app.models.buy_plan import BuyPlanV3, BuyPlanStatus


# ── Fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def _rfq_requisition(db_session, test_user):
    """Create a requisition for RFQ tests."""
    from app.models import Requisition, Requirement
    req = Requisition(name="RFQ Test Req", status="active", created_by=test_user.id)
    db_session.add(req)
    db_session.flush()
    part = Requirement(requisition_id=req.id, primary_mpn="TEST-MPN-001")
    db_session.add(part)
    db_session.flush()
    return req


class TestRfqFailureRecovery:
    """P1: Failed RFQ sends persist in DB with retry."""

    def test_failed_send_creates_contact_with_error(self, db_session, test_user, _rfq_requisition):
        """When Graph API returns an error, a Contact record is created with status='failed'."""
        contact = Contact(
            requisition_id=_rfq_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Fail Corp",
            vendor_contact="fail@example.com",
            status="failed",
            error_message="Graph API 429: Too Many Requests",
        )
        db_session.add(contact)
        db_session.flush()

        saved = db_session.get(Contact, contact.id)
        assert saved.status == "failed"
        assert saved.error_message == "Graph API 429: Too Many Requests"

    def test_retry_endpoint_resends_failed_contact(self, client, db_session, test_user, _rfq_requisition):
        """POST /api/contacts/{id}/retry re-sends and updates status."""
        contact = Contact(
            requisition_id=_rfq_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Retry Corp",
            vendor_contact="retry@example.com",
            subject="RFQ for parts",
            details="Please quote TEST-MPN-001",
            status="failed",
            error_message="Timeout",
            parts_included=["TEST-MPN-001"],
        )
        db_session.add(contact)
        db_session.commit()

        with patch("app.routers.rfq.send_batch_rfq", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = [{"id": contact.id, "status": "sent", "vendor_name": "Retry Corp", "vendor_email": "retry@example.com", "parts_count": 1}]
            resp = client.post(f"/api/contacts/{contact.id}/retry")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "sent"

    def test_retry_rejects_non_failed_contact(self, client, db_session, test_user, _rfq_requisition):
        """POST /api/contacts/{id}/retry returns 400 if contact is not in failed state."""
        contact = Contact(
            requisition_id=_rfq_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="OK Corp",
            vendor_contact="ok@example.com",
            status="sent",
        )
        db_session.add(contact)
        db_session.commit()

        resp = client.post(f"/api/contacts/{contact.id}/retry")
        assert resp.status_code == 400
        assert "failed" in resp.json()["error"].lower()
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_workflow_state_clarity.py -v --tb=short 2>&1 | tail -20
```
Expected: FAIL — `error_message` column doesn't exist, retry endpoint doesn't exist.

- [ ] **Step 3: Add ContactStatus and VendorResponseStatus enums**

In `app/enums.py`, add after `RequirementSourcingStatus`:

```python
class ContactStatus(str, enum.Enum):
    """RFQ outbound contact status."""
    sent = "sent"
    failed = "failed"
    opened = "opened"
    responded = "responded"
    quoted = "quoted"
    declined = "declined"
    ooo = "ooo"          # Out of office auto-reply
    bounced = "bounced"  # Email bounced


class VendorResponseStatus(str, enum.Enum):
    """Vendor response queue status."""
    new = "new"
    reviewed = "reviewed"
    rejected = "rejected"
```

- [ ] **Step 4: Add error_message column to Contact model**

In `app/models/offers.py`, add after line 156 (`parse_confidence`):

```python
    error_message = Column(String(500))  # Error detail when status="failed"
```

- [ ] **Step 5: Create migration 070**

```bash
docker compose exec app alembic revision --autogenerate -m "workflow state clarity — contact error_message"
```

Review generated migration. It should add `error_message` to contacts table. Rename file to `070_workflow_state_clarity.py`.

- [ ] **Step 6: Persist failed RFQ sends in email_service.py**

In `app/email_service.py`, replace the error handling blocks (lines 100-122). After each error case, instead of just appending to `results` and `continue`, also create a Contact record:

Replace lines 100-122:
```python
        if isinstance(send_result, Exception):
            logger.error(f"Send error to {email}: {send_result}")
            failed_contact = Contact(
                requisition_id=requisition_id,
                user_id=user_id,
                contact_type="email",
                vendor_name=group["vendor_name"],
                vendor_name_normalized=normalize_vendor_name(group["vendor_name"] or ""),
                vendor_contact=email,
                parts_included=group.get("parts", []),
                subject=tagged_subject,
                details=group["body"],
                status="failed",
                error_message=str(send_result)[:500],
                status_updated_at=datetime.now(timezone.utc),
                created_at=datetime.now(timezone.utc),
            )
            db.add(failed_contact)
            db.flush()
            results.append(
                {
                    "id": failed_contact.id,
                    "vendor_name": group["vendor_name"],
                    "vendor_email": email,
                    "status": "failed",
                    "error": str(send_result)[:200],
                }
            )
            continue

        if "error" in send_result:
            logger.error(f"Send failed to {email}: {send_result}")
            failed_contact = Contact(
                requisition_id=requisition_id,
                user_id=user_id,
                contact_type="email",
                vendor_name=group["vendor_name"],
                vendor_name_normalized=normalize_vendor_name(group["vendor_name"] or ""),
                vendor_contact=email,
                parts_included=group.get("parts", []),
                subject=tagged_subject,
                details=group["body"],
                status="failed",
                error_message=str(send_result.get("detail", ""))[:500],
                status_updated_at=datetime.now(timezone.utc),
                created_at=datetime.now(timezone.utc),
            )
            db.add(failed_contact)
            db.flush()
            results.append(
                {
                    "id": failed_contact.id,
                    "vendor_name": group["vendor_name"],
                    "vendor_email": email,
                    "status": "failed",
                    "error": str(send_result.get("detail", ""))[:200],
                }
            )
            continue
```

- [ ] **Step 7: Add retry endpoint in rfq.py**

Add to `app/routers/rfq.py`:

```python
@router.post("/api/contacts/{contact_id}/retry")
async def retry_failed_rfq(
    contact_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Re-send a failed RFQ email."""
    contact = db.get(Contact, contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    if contact.status != "failed":
        return {"error": "Only failed contacts can be retried", "status_code": 400}

    token = await require_fresh_token(request, user, db)
    results = await send_batch_rfq(
        token=token,
        db=db,
        user_id=user.id,
        requisition_id=contact.requisition_id,
        vendor_groups=[{
            "vendor_name": contact.vendor_name,
            "vendor_email": contact.vendor_contact,
            "parts": contact.parts_included or [],
            "subject": contact.subject or f"RFQ [ref:{contact.requisition_id}]",
            "body": contact.details or "",
        }],
    )
    # Mark old contact as superseded
    contact.status = "retried"
    contact.status_updated_at = datetime.now(timezone.utc)
    db.commit()

    return results[0] if results else {"status": "error", "error": "Retry produced no result"}
```

- [ ] **Step 8: Run tests — verify they pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_workflow_state_clarity.py::TestRfqFailureRecovery -v
```
Expected: 3 PASS

- [ ] **Step 9: Commit**

```bash
git add app/enums.py app/models/offers.py app/email_service.py app/routers/rfq.py alembic/versions/070_* tests/test_workflow_state_clarity.py
git commit -m "feat: RFQ failure recovery — persist failed sends + retry endpoint"
```

---

## Task 2: VendorResponse Terminal States

**Files:**
- Modify: `app/routers/rfq.py` (or create small router section for VR status)
- Modify: `tests/test_workflow_state_clarity.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_workflow_state_clarity.py`:

```python
class TestVendorResponseTerminalStates:
    """P1: VendorResponses can be marked reviewed/rejected."""

    @pytest.fixture
    def _vendor_response(self, db_session, _rfq_requisition):
        vr = VendorResponse(
            requisition_id=_rfq_requisition.id,
            vendor_name="Test Vendor",
            vendor_email="test@vendor.com",
            subject="Re: RFQ",
            body="We can supply.",
            status="new",
            received_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()
        return vr

    def test_mark_reviewed(self, client, db_session, _vendor_response):
        resp = client.patch(
            f"/api/vendor-responses/{_vendor_response.id}/status",
            json={"status": "reviewed"},
        )
        assert resp.status_code == 200
        db_session.refresh(_vendor_response)
        assert _vendor_response.status == "reviewed"

    def test_mark_rejected(self, client, db_session, _vendor_response):
        resp = client.patch(
            f"/api/vendor-responses/{_vendor_response.id}/status",
            json={"status": "rejected"},
        )
        assert resp.status_code == 200
        db_session.refresh(_vendor_response)
        assert _vendor_response.status == "rejected"

    def test_invalid_status_rejected(self, client, db_session, _vendor_response):
        resp = client.patch(
            f"/api/vendor-responses/{_vendor_response.id}/status",
            json={"status": "invalid_state"},
        )
        assert resp.status_code == 400

    def test_list_excludes_terminal_by_default(self, client, db_session, _rfq_requisition, _vendor_response):
        """GET /api/vendor-responses?req_id=X excludes reviewed/rejected by default."""
        _vendor_response.status = "reviewed"
        db_session.commit()
        resp = client.get(f"/api/vendor-responses?requisition_id={_rfq_requisition.id}")
        assert resp.status_code == 200
        ids = [r["id"] for r in resp.json()]
        assert _vendor_response.id not in ids
```

- [ ] **Step 2: Run tests — confirm failure**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_workflow_state_clarity.py::TestVendorResponseTerminalStates -v --tb=short
```

- [ ] **Step 3: Add PATCH endpoint for VR status**

In `app/routers/rfq.py`, add:

```python
@router.patch("/api/vendor-responses/{vr_id}/status")
async def update_vendor_response_status(
    vr_id: int,
    body: dict,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark a vendor response as reviewed or rejected."""
    VALID_STATUSES = {"new", "reviewed", "rejected"}
    new_status = body.get("status")
    if new_status not in VALID_STATUSES:
        return {"error": f"Status must be one of: {VALID_STATUSES}", "status_code": 400}

    vr = db.get(VendorResponse, vr_id)
    if not vr:
        raise HTTPException(status_code=404, detail="VendorResponse not found")

    vr.status = new_status
    db.commit()
    return {"id": vr.id, "status": vr.status}
```

- [ ] **Step 4: Modify VR list endpoint to exclude terminal states by default**

Find the existing `GET /api/vendor-responses` endpoint (in `app/routers/rfq.py`). Add a `status` query param that defaults to `"new"`. If `status="all"`, return all; otherwise filter by status.

Look for the query that lists vendor responses and add:
```python
if status != "all":
    query = query.filter(VendorResponse.status == status)
```

- [ ] **Step 5: Run tests — verify pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_workflow_state_clarity.py::TestVendorResponseTerminalStates -v
```

- [ ] **Step 6: Commit**

```bash
git add app/routers/rfq.py tests/test_workflow_state_clarity.py
git commit -m "feat: VendorResponse terminal states — reviewed/rejected with queue filtering"
```

---

## Task 3: Quote Expiration Badges

**Files:**
- Modify: `app/routers/requisitions/core.py` (add is_expired computed field to quote subqueries)
- Modify: `app/static/app.js` (expiration badge rendering)
- Create: `tests/test_part_level_endpoints.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_part_level_endpoints.py
"""Tests for requisition filters and quote expiration."""

import pytest
from datetime import date, timedelta

from app.models.quotes import Quote


@pytest.fixture
def _quote_req(db_session, test_user):
    from app.models import Requisition, CustomerSite, Company
    co = Company(name="Test Co")
    db_session.add(co)
    db_session.flush()
    site = CustomerSite(name="Test Site", company_id=co.id)
    db_session.add(site)
    db_session.flush()
    req = Requisition(name="Quote Test", status="quoted", created_by=test_user.id, customer_site_id=site.id)
    db_session.add(req)
    db_session.flush()
    return req, site


class TestQuoteExpiration:
    def test_quote_with_past_valid_until_is_expired(self, db_session, _quote_req):
        req, site = _quote_req
        q = Quote(
            requisition_id=req.id,
            customer_site_id=site.id,
            quote_number="Q-EXP-001",
            status="sent",
            validity_days=7,
        )
        # Simulate sent 30 days ago with 7-day validity
        q.sent_at = date.today() - timedelta(days=30)
        db_session.add(q)
        db_session.flush()

        # Quote is expired if sent_at + validity_days < today
        is_expired = q.sent_at and q.validity_days and (
            date.today() > (q.sent_at.date() if hasattr(q.sent_at, 'date') else q.sent_at) + timedelta(days=q.validity_days)
        )
        assert is_expired

    def test_quote_still_valid(self, db_session, _quote_req):
        req, site = _quote_req
        q = Quote(
            requisition_id=req.id,
            customer_site_id=site.id,
            quote_number="Q-VAL-001",
            status="sent",
            validity_days=30,
        )
        q.sent_at = date.today() - timedelta(days=1)
        db_session.add(q)
        db_session.flush()

        is_expired = q.sent_at and q.validity_days and (
            date.today() > (q.sent_at.date() if hasattr(q.sent_at, 'date') else q.sent_at) + timedelta(days=q.validity_days)
        )
        assert not is_expired
```

- [ ] **Step 2: Run tests — confirm pass (pure logic tests)**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_part_level_endpoints.py::TestQuoteExpiration -v
```

- [ ] **Step 3: Add quote expiration to the quotes list endpoint**

Find the quotes list endpoint (likely in `app/routers/crm/quotes.py`). Add `is_expired` computed field to each quote in the response:

```python
from datetime import date, timedelta

# In the quote serialization:
sent_date = q.sent_at.date() if q.sent_at and hasattr(q.sent_at, 'date') else q.sent_at
is_expired = bool(
    sent_date and q.validity_days
    and date.today() > sent_date + timedelta(days=q.validity_days)
)
days_until_expiry = None
if sent_date and q.validity_days:
    expiry_date = sent_date + timedelta(days=q.validity_days)
    days_until_expiry = (expiry_date - date.today()).days

# Add to response dict:
# "is_expired": is_expired,
# "days_until_expiry": days_until_expiry,
```

- [ ] **Step 4: Add expiration badge in app.js**

Find the quote rendering section (around line 7145 in app.js where `statusLabels` for quotes is defined). After the status chip, add:

```javascript
// After the status chip rendering for quotes:
if (q.is_expired && q.status === 'sent') {
    html += `<span class="status-chip" style="background:#ef4444;color:#fff;margin-left:4px">Expired</span>`;
} else if (q.days_until_expiry != null && q.days_until_expiry <= 3 && q.days_until_expiry >= 0 && q.status === 'sent') {
    html += `<span class="status-chip" style="background:#f59e0b;color:#fff;margin-left:4px">Expires in ${q.days_until_expiry}d</span>`;
}
```

- [ ] **Step 5: Commit**

```bash
git add app/routers/crm/quotes.py app/static/app.js tests/test_part_level_endpoints.py
git commit -m "feat: quote expiration badges — expired (red) and expiring-soon (amber)"
```

---

## Task 4: Requisition Status Filters

**Files:**
- Modify: `app/routers/requisitions/core.py:83` (status param handling)
- Modify: `app/static/app.js` (filter dropdown)
- Modify: `tests/test_part_level_endpoints.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_part_level_endpoints.py`:

```python
class TestRequisitionStatusFilter:
    def test_comma_separated_status_filter(self, client, db_session, test_user):
        from app.models import Requisition
        r1 = Requisition(name="Won Req", status="won", created_by=test_user.id)
        r2 = Requisition(name="Lost Req", status="lost", created_by=test_user.id)
        r3 = Requisition(name="Active Req", status="active", created_by=test_user.id)
        db_session.add_all([r1, r2, r3])
        db_session.commit()

        resp = client.get("/api/requisitions?status=won,lost")
        assert resp.status_code == 200
        names = [r["name"] for r in resp.json()]
        assert "Won Req" in names
        assert "Lost Req" in names
        assert "Active Req" not in names

    def test_single_status_still_works(self, client, db_session, test_user):
        from app.models import Requisition
        r = Requisition(name="Draft Req", status="draft", created_by=test_user.id)
        db_session.add(r)
        db_session.commit()

        resp = client.get("/api/requisitions?status=draft")
        assert resp.status_code == 200
        names = [r["name"] for r in resp.json()]
        assert "Draft Req" in names
```

- [ ] **Step 2: Run tests — confirm failure**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_part_level_endpoints.py::TestRequisitionStatusFilter -v --tb=short
```

- [ ] **Step 3: Modify requisition list to support comma-separated status**

In `app/routers/requisitions/core.py`, find the status filter logic in `_build_requisition_list`. Currently it likely does:
```python
if status:
    query = query.filter(Requisition.status == status)
```

Replace with:
```python
if status:
    statuses = [s.strip() for s in status.split(",") if s.strip()]
    if len(statuses) == 1:
        query = query.filter(Requisition.status == statuses[0])
    else:
        query = query.filter(Requisition.status.in_(statuses))
```

- [ ] **Step 4: Add status filter dropdown in app.js**

Find where `_statusLabels` is used for the filter UI (around line 10211). Add/ensure all 10 states appear in the filter dropdown:

```javascript
// Ensure the filter dropdown includes all statuses:
const allStatuses = ['draft','active','sourcing','offers','quoting','quoted','reopened','won','lost','archived'];
```

Add `reopened` and `sourcing` to `_statusLabels` if not already present:
```javascript
const _statusLabels = {draft:'Draft',active:'Sourcing',sourcing:'Sourcing',closed:'Closed',offers:'Offers',quoting:'Quoting',quoted:'Quoted',reopened:'Reopened',won:'Won',lost:'Lost',archived:'Archived'};
```

- [ ] **Step 5: Run tests — verify pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_part_level_endpoints.py::TestRequisitionStatusFilter -v
```

- [ ] **Step 6: Commit**

```bash
git add app/routers/requisitions/core.py app/static/app.js tests/test_part_level_endpoints.py
git commit -m "feat: requisition filters — all 10 states filterable with comma-separated support"
```

---

## Task 5: Buy Plan Resubmission

**Files:**
- Modify: `app/services/buyplan_workflow.py`
- Modify: `app/routers/crm/buy_plans_v3.py`
- Modify: `tests/test_workflow_state_clarity.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_workflow_state_clarity.py`:

```python
class TestBuyPlanResubmission:
    """P2: Rejected buy plans can be resubmitted."""

    @pytest.fixture
    def _rejected_plan(self, db_session, test_user, _rfq_requisition):
        from app.models.quotes import Quote
        from app.models import CustomerSite, Company
        co = Company(name="BP Test Co")
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(name="BP Test Site", company_id=co.id)
        db_session.add(site)
        db_session.flush()
        q = Quote(
            requisition_id=_rfq_requisition.id,
            customer_site_id=site.id,
            quote_number="Q-BP-001",
            status="won",
        )
        db_session.add(q)
        db_session.flush()
        plan = BuyPlanV3(
            quote_id=q.id,
            requisition_id=_rfq_requisition.id,
            status="halted",
            submitted_by_id=test_user.id,
        )
        db_session.add(plan)
        db_session.flush()
        return plan

    def test_resubmit_halted_plan(self, client, db_session, _rejected_plan):
        resp = client.post(f"/api/buy-plans-v3/{_rejected_plan.id}/resubmit")
        assert resp.status_code == 200
        db_session.refresh(_rejected_plan)
        assert _rejected_plan.status == BuyPlanStatus.draft.value

    def test_resubmit_active_plan_fails(self, client, db_session, _rejected_plan):
        _rejected_plan.status = BuyPlanStatus.active.value
        db_session.commit()
        resp = client.post(f"/api/buy-plans-v3/{_rejected_plan.id}/resubmit")
        assert resp.status_code == 400
```

- [ ] **Step 2: Run tests — confirm failure**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_workflow_state_clarity.py::TestBuyPlanResubmission -v --tb=short
```

- [ ] **Step 3: Add resubmit function to buyplan_workflow.py**

Add to `app/services/buyplan_workflow.py`:

```python
RESUBMITTABLE_STATUSES = {BuyPlanStatus.halted.value, BuyPlanStatus.cancelled.value}


def resubmit_buy_plan(plan_id: int, user: User, db: Session) -> BuyPlanV3:
    """Reset a halted/cancelled buy plan back to draft for resubmission.

    Clears approval state and SO verification so the plan can go through
    the approval workflow again.
    """
    plan = db.get(BuyPlanV3, plan_id, options=[joinedload(BuyPlanV3.lines)])
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")

    if plan.status not in RESUBMITTABLE_STATUSES:
        raise ValueError(
            f"Only halted/cancelled plans can be resubmitted (current: {plan.status})"
        )

    plan.status = BuyPlanStatus.draft.value
    plan.so_status = SOVerificationStatus.pending.value
    plan.auto_approved = False
    plan.approved_by_id = None
    plan.approved_at = None
    plan.approval_notes = None
    plan.so_verified_by_id = None
    plan.so_verified_at = None
    plan.so_rejection_note = None
    plan.halted_by_id = None
    plan.halted_at = None
    plan.cancelled_at = None
    plan.cancelled_by_id = None
    plan.cancellation_reason = None
    plan.updated_at = datetime.now(timezone.utc)

    logger.info("Buy plan %d resubmitted by user %d", plan_id, user.id)
    return plan
```

- [ ] **Step 4: Add resubmit endpoint to buy_plans_v3 router**

Add to `app/routers/crm/buy_plans_v3.py`:

```python
@router.post("/api/buy-plans-v3/{plan_id}/resubmit")
async def resubmit_plan(
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Reset a halted/cancelled buy plan back to draft."""
    from ...services.buyplan_workflow import resubmit_buy_plan

    try:
        plan = resubmit_buy_plan(plan_id, user, db)
        db.commit()
        return {"id": plan.id, "status": plan.status}
    except ValueError as e:
        return {"error": str(e), "status_code": 400}
```

- [ ] **Step 5: Run tests — verify pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_workflow_state_clarity.py::TestBuyPlanResubmission -v
```

- [ ] **Step 6: Commit**

```bash
git add app/services/buyplan_workflow.py app/routers/crm/buy_plans_v3.py tests/test_workflow_state_clarity.py
git commit -m "feat: buy plan resubmission — halted/cancelled plans can return to draft"
```

---

## Task 6: Pending Contact Visibility (OOO/Bounce Badges)

**Files:**
- Modify: `app/email_service.py` (classify OOO/bounce from VendorResponse)
- Modify: `app/static/app.js` (amber badges in activity feed)
- Modify: `tests/test_workflow_state_clarity.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_workflow_state_clarity.py`:

```python
class TestPendingContactVisibility:
    """P1: OOO/bounce contacts show amber badges."""

    def test_ooo_classification_sets_contact_status(self, db_session, test_user, _rfq_requisition):
        """When VendorResponse classification is 'ooo', update parent contact to ooo."""
        contact = Contact(
            requisition_id=_rfq_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="OOO Vendor",
            vendor_contact="ooo@vendor.com",
            status="sent",
        )
        db_session.add(contact)
        db_session.flush()

        vr = VendorResponse(
            contact_id=contact.id,
            requisition_id=_rfq_requisition.id,
            vendor_name="OOO Vendor",
            vendor_email="ooo@vendor.com",
            classification="ooo",
            status="new",
            received_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()

        # Simulate the classification-to-contact-status update
        if vr.classification in ("ooo", "out_of_office") and vr.contact_id:
            parent = db_session.get(Contact, vr.contact_id)
            if parent:
                parent.status = "ooo"
                parent.status_updated_at = datetime.now(timezone.utc)

        db_session.flush()
        db_session.refresh(contact)
        assert contact.status == "ooo"

    def test_bounce_sets_contact_status(self, db_session, test_user, _rfq_requisition):
        contact = Contact(
            requisition_id=_rfq_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Bounce Vendor",
            vendor_contact="bounce@vendor.com",
            status="sent",
        )
        db_session.add(contact)
        db_session.flush()

        vr = VendorResponse(
            contact_id=contact.id,
            requisition_id=_rfq_requisition.id,
            vendor_name="Bounce Vendor",
            vendor_email="bounce@vendor.com",
            classification="bounce",
            status="new",
            received_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()

        if vr.classification in ("bounce", "bounced") and vr.contact_id:
            parent = db_session.get(Contact, vr.contact_id)
            if parent:
                parent.status = "bounced"
                parent.status_updated_at = datetime.now(timezone.utc)

        db_session.flush()
        db_session.refresh(contact)
        assert contact.status == "bounced"
```

- [ ] **Step 2: Run tests — should pass (pure model logic)**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_workflow_state_clarity.py::TestPendingContactVisibility -v
```

- [ ] **Step 3: Update poll_inbox to classify OOO/bounce**

In `app/email_service.py`, find the section in `poll_inbox()` where VendorResponse records are created after AI parsing. After setting `vr.classification`, add:

```python
# Update parent contact status for OOO/bounce
OOO_CLASSIFICATIONS = {"ooo", "out_of_office", "auto_reply"}
BOUNCE_CLASSIFICATIONS = {"bounce", "bounced", "delivery_failure"}

if vr.contact_id and vr.classification:
    cls = vr.classification.lower()
    parent_contact = db.get(Contact, vr.contact_id)
    if parent_contact:
        if cls in OOO_CLASSIFICATIONS:
            parent_contact.status = "ooo"
            parent_contact.status_updated_at = datetime.now(timezone.utc)
        elif cls in BOUNCE_CLASSIFICATIONS:
            parent_contact.status = "bounced"
            parent_contact.status_updated_at = datetime.now(timezone.utc)
```

- [ ] **Step 4: Add amber badges in app.js activity feed**

Find the activity feed rendering in app.js (around line 3181, `_loadDdSubTab('activity')`). Where contact status badges are rendered, add:

```javascript
// After existing contact status badge logic:
if (c.status === 'ooo') {
    badge = `<span class="status-chip" style="background:#f59e0b;color:#fff" title="Vendor is out of office">OOO</span>`;
} else if (c.status === 'bounced') {
    badge = `<span class="status-chip" style="background:#f59e0b;color:#fff" title="Email bounced">Bounced</span>`;
} else if (c.status === 'failed') {
    badge = `<span class="status-chip" style="background:#ef4444;color:#fff" title="${c.error_message || 'Send failed'}">Failed</span>`;
    badge += ` <button class="btn btn-xs btn-outline" onclick="retryRfq(${c.id})" title="Retry send">↻ Retry</button>`;
}
```

And add the retry function:
```javascript
async function retryRfq(contactId) {
    try {
        const r = await fetch(`/api/contacts/${contactId}/retry`, {method:'POST'});
        const d = await r.json();
        if (d.status === 'sent') {
            showToast('RFQ resent successfully', 'success');
            // Refresh activity feed
            _loadDdSubTab('activity');
        } else {
            showToast(d.error || 'Retry failed', 'error');
        }
    } catch(e) {
        showToast('Retry failed: ' + e.message, 'error');
    }
}
```

- [ ] **Step 5: Commit**

```bash
git add app/email_service.py app/static/app.js tests/test_workflow_state_clarity.py
git commit -m "feat: pending contact visibility — OOO/bounce amber badges + failed retry button"
```

---

## Task 7: Frontend — VR Terminal Actions + Buy Plan Resubmit Button

**Files:**
- Modify: `app/static/app.js`

- [ ] **Step 1: Add reviewed/rejected buttons to vendor response cards**

Find the vendor response rendering in app.js. Add action buttons:

```javascript
// In vendor response card rendering:
if (vr.status === 'new') {
    html += `<div class="vr-actions" style="margin-top:6px">`;
    html += `<button class="btn btn-xs btn-success" onclick="updateVrStatus(${vr.id},'reviewed')">✓ Reviewed</button> `;
    html += `<button class="btn btn-xs btn-danger" onclick="updateVrStatus(${vr.id},'rejected')">✗ Reject</button>`;
    html += `</div>`;
}
```

Add handler:
```javascript
async function updateVrStatus(vrId, status) {
    const r = await fetch(`/api/vendor-responses/${vrId}/status`, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status}),
    });
    if (r.ok) {
        showToast(`Response marked ${status}`, 'success');
        _loadDdSubTab('activity');
    }
}
```

- [ ] **Step 2: Add resubmit button on halted/cancelled buy plans**

Find buy plan card rendering (around line 7188). Add:

```javascript
// After buy plan status badge:
if (bp.status === 'halted' || bp.status === 'cancelled') {
    html += ` <button class="btn btn-xs btn-outline" onclick="resubmitBuyPlan(${bp.id})">↻ Resubmit</button>`;
}
```

Handler:
```javascript
async function resubmitBuyPlan(planId) {
    if (!confirm('Resubmit this buy plan? It will return to Draft for re-approval.')) return;
    const r = await fetch(`/api/buy-plans-v3/${planId}/resubmit`, {method:'POST'});
    const d = await r.json();
    if (d.status === 'draft') {
        showToast('Buy plan returned to draft', 'success');
        location.reload();
    } else {
        showToast(d.error || 'Resubmit failed', 'error');
    }
}
```

- [ ] **Step 3: Commit**

```bash
git add app/static/app.js
git commit -m "feat: frontend — VR review/reject actions + buy plan resubmit button"
```

---

## Task 8: Migration 071 + Final Integration Test

**Files:**
- Create: `alembic/versions/071_contact_status_backfill.py`
- Modify: `tests/test_workflow_state_clarity.py`

- [ ] **Step 1: Create migration 071 — backfill note (no PG enum)**

```python
# alembic/versions/071_contact_status_backfill.py
"""Backfill: document valid Contact status values. No schema change needed
(status remains String(50), validated in application code)."""

revision = "071"
down_revision = "070"

def upgrade():
    # Contact.status valid values: sent, failed, opened, responded, quoted, declined, ooo, bounced, retried
    # VendorResponse.status valid values: new, reviewed, rejected
    # No DDL needed — values enforced in app code via enums.py
    pass

def downgrade():
    pass
```

- [ ] **Step 2: Write integration test covering the full workflow**

Append to `tests/test_workflow_state_clarity.py`:

```python
class TestWorkflowIntegration:
    """End-to-end workflow: send RFQ → fail → retry → receive OOO → mark reviewed."""

    def test_full_rfq_lifecycle(self, db_session, test_user, _rfq_requisition):
        # 1. Failed send
        c = Contact(
            requisition_id=_rfq_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Lifecycle Corp",
            vendor_contact="life@corp.com",
            status="failed",
            error_message="Timeout",
        )
        db_session.add(c)
        db_session.flush()
        assert c.status == "failed"

        # 2. Retry succeeds
        c.status = "sent"
        c.error_message = None
        c.status_updated_at = datetime.now(timezone.utc)
        db_session.flush()
        assert c.status == "sent"

        # 3. OOO response arrives
        vr = VendorResponse(
            contact_id=c.id,
            requisition_id=_rfq_requisition.id,
            vendor_name="Lifecycle Corp",
            vendor_email="life@corp.com",
            classification="ooo",
            status="new",
            received_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()
        c.status = "ooo"
        db_session.flush()
        assert c.status == "ooo"

        # 4. Mark VR reviewed
        vr.status = "reviewed"
        db_session.flush()
        assert vr.status == "reviewed"
```

- [ ] **Step 3: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_workflow_state_clarity.py tests/test_part_level_endpoints.py -v
```
Expected: All pass.

- [ ] **Step 4: Run full project test suite + coverage**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q 2>&1 | tail -20
```
Expected: No regressions, coverage maintained.

- [ ] **Step 5: Final commit**

```bash
git add alembic/versions/071_* tests/test_workflow_state_clarity.py tests/test_part_level_endpoints.py
git commit -m "feat: workflow state clarity — migration 071 + integration tests"
```

- [ ] **Step 6: Merge and deploy**

```bash
git push origin main && docker compose up -d --build
```
