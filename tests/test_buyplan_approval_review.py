"""test_buyplan_approval_review.py — Click-in review screen + gated approve/reject.

Covers the buy-plan approval workflow wired to the per-user approval right:

1. The click-in review screen (GET /v2/partials/buy-plans/{id}) surfaces the data an
   approver needs to decide — line items, vendor offer, qty, unit cost, customer, SO#/PO#,
   status badge — and shows the Approve/Reject controls ONLY when the viewer holds the
   can_approve_buy_plans right (hidden otherwise).
2. POST .../approve is gated by require_buyplan_approver: a non-approver gets 403 and the
   plan is untouched.
3. Approve flips the plan to ACTIVE, stamps approved_by/approved_at, and writes a
   BUYPLAN_APPROVED ActivityLog row scoped to the plan.
4. Reject requires a reason: a blank reason is refused (400) and the plan stays pending;
   a real reason sends it back to draft, records the reason, and writes BUYPLAN_REJECTED.
5. The list/hub surfaces a pending-approval badge.

Called by: pytest
Depends on: app.routers.htmx_views, app.services.buyplan_workflow, app.dependencies,
            app.models, conftest (client, db_session, test_user, sales_user, test_requisition)
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import ActivityType, BuyPlanLineStatus, BuyPlanStatus, SOVerificationStatus
from app.models import User
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.intelligence import ActivityLog
from app.models.quotes import Quote

# ── Helpers ───────────────────────────────────────────────────────────


def _make_quote(db: Session, req_id: int) -> Quote:
    q = Quote(requisition_id=req_id, quote_number=f"Q-{uuid.uuid4().hex[:8]}", status="draft")
    db.add(q)
    db.flush()
    return q


def _make_pending_plan(db: Session, req_id: int, submitter: User, **kw) -> BuyPlan:
    q = _make_quote(db, req_id)
    defaults = dict(
        quote_id=q.id,
        requisition_id=req_id,
        status=BuyPlanStatus.PENDING.value,
        so_status=SOVerificationStatus.PENDING.value,
        sales_order_number="SO-12345",
        customer_po_number="PO-99",
        total_cost=1000.00,
        total_revenue=1500.00,
        total_margin_pct=33.33,
        submitted_by_id=submitter.id,
    )
    defaults.update(kw)
    plan = BuyPlan(**defaults)
    db.add(plan)
    db.flush()
    line = BuyPlanLine(
        buy_plan_id=plan.id,
        quantity=10,
        unit_cost=100.00,
        unit_sell=150.00,
        status=BuyPlanLineStatus.AWAITING_PO,
    )
    db.add(line)
    db.flush()
    return plan


def _grant(db: Session, user: User) -> None:
    user.can_approve_buy_plans = True
    db.add(user)
    db.flush()


def _approve_route(plan_id: int) -> str:
    return f"/v2/partials/buy-plans/{plan_id}/approve"


# ── 1. Click-in review screen ─────────────────────────────────────────


def test_review_screen_surfaces_decision_data(
    client: TestClient, db_session: Session, test_user, sales_user, test_requisition
):
    """The detail/review view surfaces SO#, customer PO#, status, and line items."""
    plan = _make_pending_plan(db_session, test_requisition.id, sales_user)
    db_session.commit()

    resp = client.get(f"/v2/partials/buy-plans/{plan.id}")
    assert resp.status_code == 200
    body = resp.text
    assert f"Buy Plan #{plan.id}" in body
    assert "SO-12345" in body  # sales order number surfaced
    assert "PO-99" in body  # customer PO surfaced
    assert "Line Items" in body  # line items table present


def test_review_hides_approve_controls_for_non_approver(
    client: TestClient, db_session: Session, test_user, sales_user, test_requisition
):
    """A viewer WITHOUT the approval right sees no Approve/Reject banner controls."""
    plan = _make_pending_plan(db_session, test_requisition.id, sales_user)
    db_session.commit()  # default client user = test_user (buyer, no approval right)

    body = client.get(f"/v2/partials/buy-plans/{plan.id}").text
    assert "This plan needs your approval" not in body
    assert _approve_route(plan.id) not in body


def test_review_shows_approve_controls_for_approver(
    client: TestClient, db_session: Session, test_user, sales_user, test_requisition
):
    """A viewer holding the approval right sees the approval banner + Approve/Reject."""
    plan = _make_pending_plan(db_session, test_requisition.id, sales_user)
    _grant(db_session, test_user)  # the client's user gains the right
    db_session.commit()

    body = client.get(f"/v2/partials/buy-plans/{plan.id}").text
    assert "This plan needs your approval" in body
    assert _approve_route(plan.id) in body
    assert "Approve Buy Plan" in body  # approve modal heading present
    assert "Reject Buy Plan" in body  # reject modal heading present


# ── 2. Gate: 403 for non-approvers ────────────────────────────────────


def test_approve_post_403_for_non_approver(db_session: Session, test_user, sales_user, test_requisition, monkeypatch):
    """POST approve by a user without the right → 403 (require_buyplan_approver), and
    the plan is left untouched."""
    from app.database import get_db
    from app.main import app

    plan = _make_pending_plan(db_session, test_requisition.id, sales_user)
    db_session.commit()

    # The REAL require_buyplan_approver runs against test_user (no right) → 403.
    monkeypatch.setattr("app.dependencies.require_user", lambda request, db: test_user)
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        c = TestClient(app)
        resp = c.post(_approve_route(plan.id), data={"action": "approve"})
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(get_db, None)

    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.PENDING.value  # untouched
    assert plan.approved_by_id is None


def test_approve_service_permissionerror_maps_to_403(
    client: TestClient, db_session: Session, test_user, sales_user, test_requisition
):
    """Defense-in-depth: if the dependency is satisfied but the service-level approval
    check fires (PermissionError), the route maps it to 403, not 400. Simulated by
    overriding the dependency to a user who lacks the right."""
    from app.dependencies import require_buyplan_approver
    from app.main import app

    plan = _make_pending_plan(db_session, test_requisition.id, sales_user)
    db_session.commit()  # test_user has NO approval right

    app.dependency_overrides[require_buyplan_approver] = lambda: test_user
    try:
        resp = client.post(_approve_route(plan.id), data={"action": "approve"})
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(require_buyplan_approver, None)

    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.PENDING.value  # untouched


# ── 3. Approve sets state + writes audit ──────────────────────────────


def test_approve_sets_state_and_writes_activity(
    client: TestClient, db_session: Session, test_user, sales_user, test_requisition
):
    """Approve → ACTIVE + approved_by/approved_at + BUYPLAN_APPROVED ActivityLog."""
    from app.dependencies import require_buyplan_approver
    from app.main import app

    plan = _make_pending_plan(db_session, test_requisition.id, sales_user)
    _grant(db_session, test_user)
    db_session.commit()

    app.dependency_overrides[require_buyplan_approver] = lambda: test_user
    try:
        resp = client.post(_approve_route(plan.id), data={"action": "approve", "notes": "ship it"})
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.pop(require_buyplan_approver, None)

    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.ACTIVE.value
    assert plan.approved_by_id == test_user.id
    assert plan.approved_at is not None

    row = (
        db_session.query(ActivityLog)
        .filter(ActivityLog.buy_plan_id == plan.id, ActivityLog.activity_type == str(ActivityType.BUYPLAN_APPROVED))
        .one()
    )
    assert row.user_id == test_user.id


# ── 4. Reject requires a reason ───────────────────────────────────────


def test_reject_without_reason_is_refused(
    client: TestClient, db_session: Session, test_user, sales_user, test_requisition
):
    """Reject with a blank reason → 400 and the plan stays pending (server-side guard,
    not just the client-side `required` attribute)."""
    from app.dependencies import require_buyplan_approver
    from app.main import app

    plan = _make_pending_plan(db_session, test_requisition.id, sales_user)
    _grant(db_session, test_user)
    db_session.commit()

    app.dependency_overrides[require_buyplan_approver] = lambda: test_user
    try:
        resp = client.post(_approve_route(plan.id), data={"action": "reject", "notes": "   "})
        assert resp.status_code == 400
    finally:
        app.dependency_overrides.pop(require_buyplan_approver, None)

    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.PENDING.value


def test_reject_with_reason_sends_back_to_draft_and_audits(
    client: TestClient, db_session: Session, test_user, sales_user, test_requisition
):
    """Reject with a reason → DRAFT, reason recorded, BUYPLAN_REJECTED ActivityLog."""
    from app.dependencies import require_buyplan_approver
    from app.main import app

    plan = _make_pending_plan(db_session, test_requisition.id, sales_user)
    _grant(db_session, test_user)
    db_session.commit()

    app.dependency_overrides[require_buyplan_approver] = lambda: test_user
    try:
        resp = client.post(_approve_route(plan.id), data={"action": "reject", "notes": "margin too thin"})
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.pop(require_buyplan_approver, None)

    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.DRAFT.value
    assert plan.approval_notes == "margin too thin"

    row = (
        db_session.query(ActivityLog)
        .filter(ActivityLog.buy_plan_id == plan.id, ActivityLog.activity_type == str(ActivityType.BUYPLAN_REJECTED))
        .one()
    )
    assert row.user_id == test_user.id


# ── 5. Badge surfacing ────────────────────────────────────────────────


def test_pending_badge_renders_on_review(
    client: TestClient, db_session: Session, test_user, sales_user, test_requisition
):
    """The review screen renders the status badge for the pending-approval state."""
    plan = _make_pending_plan(db_session, test_requisition.id, sales_user)
    db_session.commit()

    body = client.get(f"/v2/partials/buy-plans/{plan.id}").text
    # status_badge renders the value capitalised inside a .badge span.
    assert "Pending" in body
    assert "badge" in body


def test_rejected_draft_blocker_distinguishes_from_fresh(db_session: Session, test_user, sales_user, test_requisition):
    """A rejected plan returns to DRAFT but the hub blocker marks it 'rejected —
    resubmit' (via approved_at), distinguishing it from a never-submitted draft."""
    from datetime import datetime, timezone

    from app.services.buyplan_hub import _compute_blocker

    fresh = _make_pending_plan(db_session, test_requisition.id, sales_user, status=BuyPlanStatus.DRAFT.value)
    rejected = _make_pending_plan(db_session, test_requisition.id, sales_user, status=BuyPlanStatus.DRAFT.value)
    rejected.approved_at = datetime.now(timezone.utc)
    rejected.approval_notes = "no"
    db_session.flush()

    assert _compute_blocker(fresh) == "ready to submit"
    assert _compute_blocker(rejected) == "rejected — resubmit"
