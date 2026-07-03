"""test_approvals_hub_tabs.py — the Approvals hub 3-tab decide console (Phase 3).

The Approvals module at /v2/approvals is now three org-wide gate tabs — Buy Plan (BUY_PLAN
engine gate) / PO Approval (per-line PENDING_VERIFY, not engine-backed) / Prepayment
(PREPAYMENT engine gate) — served by routers/htmx/approvals_hub.py. Covers:
  - the 3-pill shell renders with all three tab URLs + the lazy-body guard;
  - each tab's pending rows (+ the PO tab's 3-action Verify / Send-back / Cancel row);
  - the origin=approvals_hub re-render branch on all three decide actions (approve /
    verify-po / prepay-decide) returns the matching tab body, not the full plan detail;
  - the Sales-Order origination surface relocated off the /v2/partials/approvals prefix;
  - /v2/buy-plans no longer redirects (the Buy Plans hub's real home).

Called by: pytest
Depends on: conftest (db_session, test_user), app.routers.htmx.{approvals_hub,buy_plans},
            app.services.approvals, app.models.*, app.constants.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import (
    ApprovalGateType,
    ApprovalRecipientStatus,
    ApprovalRequestStatus,
    ApprovalSubjectType,
    BuyPlanLineStatus,
    BuyPlanStatus,
    OfferStatus,
    SOVerificationStatus,
)
from app.database import get_db
from app.dependencies import (
    require_buyplan_approver,
    require_buyplan_po_approver,
    require_user,
)
from app.models import Offer, Requirement, User
from app.models.approvals import ApprovalRequest, ApprovalStep, ApprovalStepRecipient
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.quality_plan import Prepayment
from app.models.quotes import Quote
from app.models.sourcing import Requisition
from app.models.vendors import VendorCard

# ── Fixtures / builders ──────────────────────────────────────────────────


@pytest.fixture()
def hub_client(db_session: Session, test_user: User):
    """TestClient authed as test_user, granted both decide rights, with the two plain-
    function approver gates overridden so the decide POSTs resolve to test_user."""
    from app.main import app

    test_user.can_approve_buy_plans = True
    test_user.can_approve_purchase_orders = True
    db_session.commit()

    app.dependency_overrides[get_db] = lambda: (yield db_session)  # type: ignore[misc]
    app.dependency_overrides[require_user] = lambda: test_user
    app.dependency_overrides[require_buyplan_approver] = lambda: test_user
    app.dependency_overrides[require_buyplan_po_approver] = lambda: test_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in (get_db, require_user, require_buyplan_approver, require_buyplan_po_approver):
            app.dependency_overrides.pop(dep, None)


def _req_quote(db: Session, user: User) -> tuple[Requisition, Quote, Requirement]:
    req = Requisition(
        name=f"REQ-{uuid.uuid4().hex[:6]}",
        customer_name="AcmeCo",
        status="active",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    rq = Requirement(requisition_id=req.id, primary_mpn="LM317", created_at=datetime.now(timezone.utc))
    db.add(rq)
    db.flush()
    q = Quote(
        requisition_id=req.id,
        quote_number=f"Q-{uuid.uuid4().hex[:8]}",
        line_items=[],
        status="sent",
        created_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(q)
    db.flush()
    return req, q, rq


def _plan(db: Session, req: Requisition, q: Quote, *, status: str) -> BuyPlan:
    bp = BuyPlan(
        requisition_id=req.id,
        quote_id=q.id,
        status=status,
        so_status=SOVerificationStatus.APPROVED.value,
        submitted_by_id=req.created_by,
        total_cost=1000.0,
        total_revenue=2000.0,
        total_margin_pct=50.0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(bp)
    db.flush()
    return bp


def _pending_verify_line(db: Session, bp: BuyPlan, rq: Requirement, user: User) -> BuyPlanLine:
    vc = VendorCard(normalized_name=f"vc-{uuid.uuid4().hex[:8]}", display_name="Acme Dist")
    db.add(vc)
    db.flush()
    off = Offer(
        requirement_id=rq.id,
        vendor_card_id=vc.id,
        vendor_name="Acme Dist",
        vendor_name_normalized="acme dist",
        mpn="LM317",
        normalized_mpn="LM317",
        unit_price=1.0,
        status=OfferStatus.ACTIVE.value,
    )
    db.add(off)
    db.flush()
    line = BuyPlanLine(
        buy_plan_id=bp.id,
        requirement_id=rq.id,
        offer_id=off.id,
        quantity=100,
        unit_cost=1.0,
        unit_sell=2.0,
        buyer_id=user.id,
        status=BuyPlanLineStatus.PENDING_VERIFY.value,
        po_number="PO-9",
        po_confirmed_at=datetime.now(timezone.utc),
    )
    db.add(line)
    db.flush()
    return line


def _pending_buy_plan_request(db: Session, bp: BuyPlan, user: User) -> ApprovalRequest:
    ar = ApprovalRequest(
        gate_type=ApprovalGateType.BUY_PLAN,
        status=ApprovalRequestStatus.REQUESTED,
        subject_type=ApprovalSubjectType.BUY_PLAN,
        subject_id=bp.id,
        requested_by_id=user.id,
        owner_id=user.id,
    )
    db.add(ar)
    db.flush()
    step = ApprovalStep(request_id=ar.id, seq=1, rule="any", status="pending")
    db.add(step)
    db.flush()
    db.add(ApprovalStepRecipient(step_id=step.id, user_id=user.id, status=ApprovalRecipientStatus.PENDING))
    db.flush()
    return ar


def _pending_prepay_request(db: Session, bp: BuyPlan, user: User) -> tuple[ApprovalRequest, Prepayment]:
    vc = VendorCard(normalized_name=f"vc-{uuid.uuid4().hex[:8]}", display_name="WireVendor")
    db.add(vc)
    db.flush()
    pp = Prepayment(
        buy_plan_id=bp.id, vendor_card_id=vc.id, total_incl_fees=2500, currency="USD", created_by_id=user.id
    )
    db.add(pp)
    db.flush()
    ar = ApprovalRequest(
        gate_type=ApprovalGateType.PREPAYMENT,
        status=ApprovalRequestStatus.REQUESTED,
        subject_type=ApprovalSubjectType.PREPAYMENT,
        subject_id=pp.id,
        requested_by_id=user.id,
        owner_id=user.id,
    )
    db.add(ar)
    db.flush()
    step = ApprovalStep(request_id=ar.id, seq=1, rule="any", status="pending")
    db.add(step)
    db.flush()
    db.add(ApprovalStepRecipient(step_id=step.id, user_id=user.id, status=ApprovalRecipientStatus.PENDING))
    db.commit()
    return ar, pp


# ── Shell + tab rendering ────────────────────────────────────────────────


def test_shell_renders_three_tabs(hub_client: TestClient):
    r = hub_client.get("/v2/partials/approvals")
    assert r.status_code == 200
    for key in ("buy-plan", "po-approval", "prepayment"):
        assert f"?tab={key}" in r.text
    assert 'hx-target="#ap-hub-body"' in r.text


def test_shell_defaults_to_buy_plan_tab(hub_client: TestClient):
    r = hub_client.get("/v2/partials/approvals")
    assert "/v2/partials/approvals/buy-plan" in r.text  # lazy body loads buy-plan by default


def test_unknown_tab_404s(hub_client: TestClient):
    assert hub_client.get("/v2/partials/approvals/bogus").status_code == 404


def test_buy_plan_tab_lists_pending(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.PENDING.value)
    _pending_buy_plan_request(db_session, bp, test_user)
    db_session.commit()

    r = hub_client.get("/v2/partials/approvals/buy-plan")
    assert r.status_code == 200
    assert f"Plan #{bp.id}" in r.text
    assert "Approve" in r.text  # inline decide affordance for an eligible recipient


def test_po_approval_tab_has_three_action_row(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _pending_verify_line(db_session, bp, rq, test_user)
    db_session.commit()

    r = hub_client.get("/v2/partials/approvals/po-approval")
    assert r.status_code == 200
    assert "Pending POs" in r.text
    # All three outcomes render inline: Verify + Send back (verify-po) + Cancel (re-source).
    assert "Verify" in r.text and "Send back" in r.text
    assert f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/verify-po" in r.text
    assert f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/resource" in r.text  # the shared re-source macro
    assert "Re-source" in r.text


def test_prepayment_tab_lists_pending(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    _pending_prepay_request(db_session, bp, test_user)

    r = hub_client.get("/v2/partials/approvals/prepayment")
    assert r.status_code == 200
    assert "WireVendor" in r.text
    assert "Approve" in r.text


def _rich_prepay_request(
    db: Session,
    bp: BuyPlan,
    rq: Requirement,
    user: User,
    *,
    amount: str = "20002.38",
    line_cost: str = "2000.00",
    line_qty: int = 10,
    test_report_sent: bool = False,
) -> tuple[ApprovalRequest, Prepayment, BuyPlanLine]:
    """A fully-populated pending prepayment (legal beneficiary, PO line, SO#, remarks)
    so the tab can be exercised as the real cash-approval surface it is."""
    bp.sales_order_number = "SO-3321"
    vc = VendorCard(
        normalized_name=f"vc-{uuid.uuid4().hex[:8]}",
        display_name="WireVendor Display",
        legal_name="Northwind Components LLC",
    )
    db.add(vc)
    db.flush()
    line = BuyPlanLine(
        buy_plan_id=bp.id,
        requirement_id=rq.id,
        quantity=line_qty,
        unit_cost=Decimal(line_cost),
        status=BuyPlanLineStatus.PENDING_VERIFY.value,
        po_number="PO-7788",
    )
    db.add(line)
    db.flush()
    pp = Prepayment(
        buy_plan_id=bp.id,
        buy_plan_line_id=line.id,
        vendor_card_id=vc.id,
        total_incl_fees=Decimal(amount),
        currency="USD",
        payment_method="wire",
        test_report_sent=test_report_sent,
        buyer_remarks="Vendor requires 50% upfront before build slot",
        created_by_id=user.id,
    )
    db.add(pp)
    db.flush()
    ar = ApprovalRequest(
        gate_type=ApprovalGateType.PREPAYMENT,
        status=ApprovalRequestStatus.REQUESTED,
        subject_type=ApprovalSubjectType.PREPAYMENT,
        subject_id=pp.id,
        amount=Decimal(amount),
        currency="USD",
        requested_by_id=user.id,
        owner_id=user.id,
    )
    db.add(ar)
    db.flush()
    step = ApprovalStep(request_id=ar.id, seq=1, rule="any", status="pending")
    db.add(step)
    db.flush()
    db.add(ApprovalStepRecipient(step_id=step.id, user_id=user.id, status=ApprovalRecipientStatus.PENDING))
    db.commit()
    return ar, pp, line


def test_prepayment_pending_row_shows_full_decision_context(
    hub_client: TestClient, db_session: Session, test_user: User
):
    """The pending prepayment row surfaces everything a manager needs to authorise cash:
    legal beneficiary, a 2-decimal currency amount + PO delta, the drill-through link, the
    PO#/SO#, the requester, remarks, and the LOUD test-report warning (findings #1/#5/#6/#9)."""
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    _rich_prepay_request(db_session, bp, rq, test_user)

    r = hub_client.get("/v2/partials/approvals/prepayment")
    assert r.status_code == 200
    body = r.text
    assert "Northwind Components LLC" in body  # beneficiary (legal name wins the chain)
    assert "USD 20,002.38" in body  # 2-decimal amount honouring currency (finding #9)
    assert "20,000.00" in body and "+2.38" in body  # PO total + signed delta (finding #1)
    assert "PO-7788" in body and "SO-3321" in body
    assert f'hx-get="/v2/partials/buy-plans/{bp.id}"' in body  # wired drill-through (finding #9)
    assert "Test report NOT sent to management" in body  # loud warning (finding #5)
    assert "Vendor requires 50% upfront" in body  # buyer remarks (finding #6)
    assert "Test Buyer" in body  # requester name


def test_prepayment_resolved_row_is_self_documenting(hub_client: TestClient, db_session: Session, test_user: User):
    """A resolved prepayment row documents who approved it, for how much, on which PO
    (finding #7)."""
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    ar, _pp, _line = _rich_prepay_request(db_session, bp, rq, test_user)
    # Approve it directly (flip request + recipient decision) so it lands in Recently-resolved.
    now = datetime.now(timezone.utc)
    ar.status = ApprovalRequestStatus.APPROVED.value
    ar.resolved_at = now
    recip = db_session.query(ApprovalStepRecipient).join(ApprovalStep).filter(ApprovalStep.request_id == ar.id).one()
    recip.status = ApprovalRecipientStatus.APPROVED.value
    recip.decided_at = now
    db_session.commit()

    r = hub_client.get("/v2/partials/approvals/prepayment")
    assert r.status_code == 200
    body = r.text
    assert "Recently resolved" in body
    assert "Approved by Test Buyer" in body  # approved-by (decider name)
    assert "USD 20,002.38" in body  # amount
    assert "PO-7788" in body  # the PO it prepaid


# ── origin=approvals_hub re-render for all three decide actions ───────────


def test_verify_po_origin_approvals_hub_rerenders_po_tab(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _pending_verify_line(db_session, bp, rq, test_user)
    db_session.commit()

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/verify-po",
            data={"action": "approve", "origin": "approvals_hub"},
        )
    assert r.status_code == 200
    assert "Pending POs" in r.text  # the PO Approval tab body, NOT the full plan detail
    assert "Line Items" not in r.text
    db_session.expire(line)
    assert line.status == BuyPlanLineStatus.VERIFIED.value


def test_approve_origin_approvals_hub_rerenders_buy_plan_tab(
    hub_client: TestClient, db_session: Session, test_user: User
):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.PENDING.value)
    _pending_buy_plan_request(db_session, bp, test_user)
    db_session.commit()

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/approve",
            data={"action": "approve", "origin": "approvals_hub"},
        )
    assert r.status_code == 200
    # Re-renders the Buy Plan tab body (the plan now tracked as active), NOT the plan detail.
    assert f"Plan #{bp.id}" in r.text and "Buy Plans" in r.text and "Line Items" not in r.text
    db_session.expire(bp)
    assert bp.status == BuyPlanStatus.ACTIVE.value


def test_prepay_decide_origin_approvals_hub_rerenders_prepayment_tab(
    hub_client: TestClient, db_session: Session, test_user: User
):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    ar, _pp = _pending_prepay_request(db_session, bp, test_user)

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/approvals/prepay-requests/{ar.id}/decide",
            data={"action": "approve", "origin": "approvals_hub"},
        )
    assert r.status_code == 200
    assert "Line Items" not in r.text  # the Prepayment tab body, not the full plan detail
    db_session.expire(ar)
    assert ar.status == ApprovalRequestStatus.APPROVED.value


# ── SEE-ALL / SEE-MINE scope toggle (all three tabs) ─────────────────────


def _other_user(db: Session) -> User:
    u = User(
        email=f"other-{uuid.uuid4().hex[:6]}@t.com",
        name="Other Owner",
        role="sales",
        azure_id=f"az-{uuid.uuid4().hex[:8]}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def test_scope_toggle_present_on_all_tabs(hub_client: TestClient):
    for tab in ("buy-plan", "po-approval", "prepayment"):
        r = hub_client.get(f"/v2/partials/approvals/{tab}")
        assert r.status_code == 200
        assert f"/v2/partials/approvals/{tab}?scope=all" in r.text
        assert f"/v2/partials/approvals/{tab}?scope=mine" in r.text


def test_buy_plan_scope_mine_filters_to_own_plans(hub_client: TestClient, db_session: Session, test_user: User):
    my_req, my_q, _ = _req_quote(db_session, test_user)
    mine = _plan(db_session, my_req, my_q, status=BuyPlanStatus.ACTIVE.value)
    other = _other_user(db_session)
    o_req, o_q, _ = _req_quote(db_session, other)
    theirs = _plan(db_session, o_req, o_q, status=BuyPlanStatus.ACTIVE.value)
    db_session.commit()

    all_txt = hub_client.get("/v2/partials/approvals/buy-plan?scope=all").text
    assert f"Plan #{mine.id}" in all_txt and f"Plan #{theirs.id}" in all_txt

    mine_txt = hub_client.get("/v2/partials/approvals/buy-plan?scope=mine").text
    assert f"Plan #{mine.id}" in mine_txt
    assert f"Plan #{theirs.id}" not in mine_txt


def test_buy_plan_non_pending_plan_is_a_tracking_row(hub_client: TestClient, db_session: Session, test_user: User):
    """A non-pending (active) plan renders as a status-only tracking row — its status
    badge, no stray Approve button (no open decidable request)."""
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    db_session.commit()

    r = hub_client.get("/v2/partials/approvals/buy-plan")
    assert r.status_code == 200
    assert f"Plan #{bp.id}" in r.text
    assert "active" in r.text.lower()  # lifecycle status badge (tracking signal)
    assert "Approve" not in r.text  # nothing decidable → no decide affordance
    assert "View" in r.text


def test_po_approval_scope_mine_filters_to_own_plan_lines(hub_client: TestClient, db_session: Session, test_user: User):
    my_req, my_q, my_rq = _req_quote(db_session, test_user)
    my_bp = _plan(db_session, my_req, my_q, status=BuyPlanStatus.ACTIVE.value)
    _pending_verify_line(db_session, my_bp, my_rq, test_user)
    other = _other_user(db_session)
    o_req, o_q, o_rq = _req_quote(db_session, other)
    o_bp = _plan(db_session, o_req, o_q, status=BuyPlanStatus.ACTIVE.value)
    _pending_verify_line(db_session, o_bp, o_rq, other)
    db_session.commit()

    all_txt = hub_client.get("/v2/partials/approvals/po-approval?scope=all").text
    assert f"/buy-plans/{my_bp.id}/lines/" in all_txt and f"/buy-plans/{o_bp.id}/lines/" in all_txt

    mine_txt = hub_client.get("/v2/partials/approvals/po-approval?scope=mine").text
    assert f"/buy-plans/{my_bp.id}/lines/" in mine_txt
    assert f"/buy-plans/{o_bp.id}/lines/" not in mine_txt


def test_prepayment_scope_mine_filters_to_own_requests(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    _pending_prepay_request(db_session, bp, test_user)  # requested_by/owner = test_user
    # A prepay request owned by someone else (still org-wide visible under scope=all).
    other = _other_user(db_session)
    vc = VendorCard(normalized_name=f"vc-{uuid.uuid4().hex[:8]}", display_name="OtherVendor")
    db_session.add(vc)
    db_session.flush()
    pp = Prepayment(
        buy_plan_id=bp.id, vendor_card_id=vc.id, total_incl_fees=999, currency="USD", created_by_id=other.id
    )
    db_session.add(pp)
    db_session.flush()
    ar = ApprovalRequest(
        gate_type=ApprovalGateType.PREPAYMENT,
        status=ApprovalRequestStatus.REQUESTED,
        subject_type=ApprovalSubjectType.PREPAYMENT,
        subject_id=pp.id,
        requested_by_id=other.id,
        owner_id=other.id,
    )
    db_session.add(ar)
    db_session.commit()

    all_txt = hub_client.get("/v2/partials/approvals/prepayment?scope=all").text
    assert "WireVendor" in all_txt and "OtherVendor" in all_txt

    mine_txt = hub_client.get("/v2/partials/approvals/prepayment?scope=mine").text
    assert "WireVendor" in mine_txt
    assert "OtherVendor" not in mine_txt


def test_decide_preserves_scope_on_rerender(hub_client: TestClient, db_session: Session, test_user: User):
    """Verifying a PO from the SEE-MINE view re-renders the PO tab still scoped to
    mine."""
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _pending_verify_line(db_session, bp, rq, test_user)
    db_session.commit()

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/verify-po",
            data={"action": "approve", "origin": "approvals_hub", "hub_scope": "mine"},
        )
    assert r.status_code == 200
    # The re-rendered body's toggle still reflects the mine scope (its "all" link targets mine-off).
    assert "/v2/partials/approvals/po-approval?scope=mine" in r.text
    assert "sc: 'mine'" in r.text  # Alpine toggle initialised to the preserved scope


# ── Sales-Order origination relocation + hub home ────────────────────────


def test_sales_order_new_relocated_off_approvals_prefix(hub_client: TestClient):
    """Origination moved to the Buy Plans hub prefix; the old Approvals path is gone."""
    assert hub_client.get("/v2/partials/buy-plans/sales-orders/new").status_code == 200
    assert hub_client.get("/v2/partials/approvals/sales-orders/new").status_code == 404


def test_buy_plans_no_longer_redirects(hub_client: TestClient):
    r = hub_client.get("/v2/buy-plans", follow_redirects=False)
    assert r.status_code == 200
