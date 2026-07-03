"""test_prepayment.py — Tests for prepayment_service and POST /v2/prepayments.

Verifies:
- A $400 prepayment spawns a REQUESTED approval request routed to all three
  eligible approvers (one with limit=1000, two unlimited).
- A $2500 prepayment is routed only to the two unlimited approvers (limit
  filter excludes the capped approver).
- The POST /v2/prepayments route returns 200 + approval request id.
- Unauthenticated requests return 401.

Called by: pytest
Depends on: app.services.prepayment_service, app.routers.prepayments,
            app.services.approvals, app.models.quality_plan (Prepayment),
            conftest fixtures (db_session, client, unauthenticated_client)
"""

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.constants import (
    ApprovalGateType,
    ApprovalRecipientStatus,
    ApprovalRequestStatus,
    ApprovalSubjectType,
    PaymentMethod,
)
from app.models.approvals import ApprovalStep, ApprovalStepRecipient
from app.models.auth import User

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_approver(
    db: Session,
    *,
    email: str,
    name: str,
    azure_id: str,
    limit: Decimal | None,
) -> User:
    """Seed a user with can_approve_prepayments=True and optional limit."""
    u = User(
        email=email,
        name=name,
        role="manager",
        azure_id=azure_id,
        is_active=True,
        can_approve_prepayments=True,
        prepayment_approval_limit=limit,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_buy_plan(db: Session, requester: User):
    """Seed the minimum buy-plan graph needed for FK constraints."""
    from app.models import Company, CustomerSite, Quote, Requisition
    from app.models.buy_plan import BuyPlan

    company = Company(
        name="Test Co",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(company)
    db.flush()

    site = CustomerSite(company_id=company.id, site_name="HQ")
    db.add(site)
    db.flush()

    req = Requisition(
        name="REQ-PP-001",
        customer_name="Test Co",
        status="active",
        created_by=requester.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()

    quote = Quote(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number="Q-PP-001",
        status="sent",
        line_items=[],
        subtotal=Decimal("1000.00"),
        total_cost=Decimal("800.00"),
        total_margin_pct=Decimal("20.00"),
        created_by_id=requester.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(quote)
    db.flush()

    bp = BuyPlan(
        quote_id=quote.id,
        requisition_id=req.id,
        status="draft",
        so_status="pending",
    )
    db.add(bp)
    db.flush()
    return bp


def _make_po_line(db: Session, buy_plan):
    """Seed a cut-PO line on *buy_plan* (po_number set, PENDING_VERIFY) so a prepayment
    request against it passes create_prepayment's line validation."""
    from app.constants import BuyPlanLineStatus
    from app.models.buy_plan import BuyPlanLine

    line = BuyPlanLine(
        buy_plan_id=buy_plan.id,
        status=BuyPlanLineStatus.PENDING_VERIFY.value,
        unit_cost=10.0,
        quantity=10,
        po_number="PO-PP-1",
        po_confirmed_at=datetime.now(timezone.utc),
    )
    db.add(line)
    db.flush()
    return line


# ── Service tests ─────────────────────────────────────────────────────────────


def test_create_prepayment_400_routes_to_all_three(db_session: Session, test_user: User):
    """$400 prepayment → routed to all three eligible approvers (limit=1000, NULL,
    NULL)."""
    from app.services.prepayment_service import create_prepayment

    myrna = _make_approver(
        db_session,
        email="myrna@trioscs.com",
        name="Myrna",
        azure_id="az-myrna",
        limit=Decimal("1000.00"),
    )
    mike = _make_approver(
        db_session,
        email="mike@trioscs.com",
        name="Mike",
        azure_id="az-mike",
        limit=None,
    )
    marcus = _make_approver(
        db_session,
        email="marcus@trioscs.com",
        name="Marcus",
        azure_id="az-marcus",
        limit=None,
    )
    db_session.commit()

    buy_plan = _make_buy_plan(db_session, test_user)
    line = _make_po_line(db_session, buy_plan)
    db_session.commit()

    prepayment, request = create_prepayment(
        db_session,
        buy_plan_id=buy_plan.id,
        buy_plan_line_id=line.id,
        vendor_card_id=None,
        payment_method=PaymentMethod.WIRE,
        total_incl_fees=Decimal("400.00"),
        test_report_sent=False,
        buyer_remarks="Please rush",
        created_by=test_user,
    )
    db_session.commit()

    # Prepayment persisted
    assert prepayment.id is not None
    assert prepayment.total_incl_fees == Decimal("400.00")
    assert prepayment.payment_method == PaymentMethod.WIRE

    # Approval request spawned
    assert request is not None
    assert request.gate_type == ApprovalGateType.PREPAYMENT
    assert request.status == ApprovalRequestStatus.REQUESTED
    assert request.subject_type == ApprovalSubjectType.PREPAYMENT
    assert request.subject_id == prepayment.id
    assert request.amount == Decimal("400.00")
    # Currency contract: defaults to the prepayment's USD.
    assert request.currency == "USD"

    # Routed to all three (Myrna qualifies because 400 <= 1000)
    recipients = (
        db_session.query(ApprovalStepRecipient)
        .join(ApprovalStep, ApprovalStepRecipient.step_id == ApprovalStep.id)
        .filter(ApprovalStep.request_id == request.id)
        .all()
    )
    recipient_ids = {r.user_id for r in recipients}
    assert recipient_ids == {myrna.id, mike.id, marcus.id}
    assert all(r.status == ApprovalRecipientStatus.PENDING for r in recipients)


def test_create_prepayment_2500_excludes_capped_approver(db_session: Session, test_user: User):
    """$2500 prepayment → routed only to unlimited approvers (Myrna's $1000 limit
    excluded)."""
    from app.services.prepayment_service import create_prepayment

    myrna = _make_approver(
        db_session,
        email="myrna2@trioscs.com",
        name="Myrna2",
        azure_id="az-myrna2",
        limit=Decimal("1000.00"),
    )
    mike = _make_approver(
        db_session,
        email="mike2@trioscs.com",
        name="Mike2",
        azure_id="az-mike2",
        limit=None,
    )
    marcus = _make_approver(
        db_session,
        email="marcus2@trioscs.com",
        name="Marcus2",
        azure_id="az-marcus2",
        limit=None,
    )
    db_session.commit()

    buy_plan = _make_buy_plan(db_session, test_user)
    line = _make_po_line(db_session, buy_plan)
    db_session.commit()

    _prepayment, request = create_prepayment(
        db_session,
        buy_plan_id=buy_plan.id,
        buy_plan_line_id=line.id,
        vendor_card_id=None,
        payment_method=PaymentMethod.CC,
        total_incl_fees=Decimal("2500.00"),
        test_report_sent=True,
        buyer_remarks=None,
        created_by=test_user,
    )
    db_session.commit()

    recipients = (
        db_session.query(ApprovalStepRecipient)
        .join(ApprovalStep, ApprovalStepRecipient.step_id == ApprovalStep.id)
        .filter(ApprovalStep.request_id == request.id)
        .all()
    )
    recipient_ids = {r.user_id for r in recipients}
    # Myrna excluded (2500 > 1000), Mike + Marcus included (no limit)
    assert myrna.id not in recipient_ids
    assert recipient_ids == {mike.id, marcus.id}


# ── Route tests ───────────────────────────────────────────────────────────────


def test_post_prepayments_returns_200_with_request_id(db_session: Session, client, test_user: User):
    """POST /v2/prepayments → 200 + JSON with approval_request_id."""
    _make_approver(
        db_session,
        email="approver@trioscs.com",
        name="Approver",
        azure_id="az-approver-route",
        limit=None,
    )
    db_session.commit()

    buy_plan = _make_buy_plan(db_session, test_user)
    line = _make_po_line(db_session, buy_plan)
    db_session.commit()

    resp = client.post(
        "/v2/prepayments",
        json={
            "buy_plan_id": buy_plan.id,
            "buy_plan_line_id": line.id,
            "vendor_card_id": None,
            "payment_method": PaymentMethod.WIRE,
            "total_incl_fees": "500.00",
            "test_report_sent": False,
            "buyer_remarks": "Test route",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "approval_request_id" in data
    assert isinstance(data["approval_request_id"], int)
    assert data["approval_request_id"] > 0


def test_post_prepayments_unauth_returns_401(db_session: Session, unauthenticated_client):
    """POST /v2/prepayments without auth → 401."""
    resp = unauthenticated_client.post(
        "/v2/prepayments",
        json={
            "buy_plan_id": 1,
            "vendor_card_id": None,
            "payment_method": PaymentMethod.WIRE,
            "total_incl_fees": "100.00",
            "test_report_sent": False,
            "buyer_remarks": None,
        },
    )
    assert resp.status_code == 401
