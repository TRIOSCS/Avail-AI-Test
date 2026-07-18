"""test_prepayment_request_ui.py — the prepayment request entry point (Task 3).

Covers the buyer-facing request surface added on top of create_prepayment:
  - GET  /v2/partials/prepayments/new?line_id=... renders the modal, prefilled with the
    line's amount (unit_cost*qty) and vendor;
  - POST /v2/partials/prepayments (form-encoded) creates a Prepayment linked to the line
    and returns 200 + an HX-Trigger success toast;
  - can_request_prepayment gates button visibility on ownership + cut-PO state.

Called by: pytest
Depends on: conftest (db_session, test_user, client — authed as the owning buyer),
            app.routers.prepayments, app.dependencies.can_request_prepayment,
            app.models.* , app.constants.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.constants import (
    ApprovalGateType,
    ApprovalRequestStatus,
    ApprovalSubjectType,
    BuyPlanLineStatus,
    UserRole,
)
from app.dependencies import can_request_prepayment
from app.models import Offer, Requirement, User
from app.models.approvals import ApprovalRequest
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.quality_plan import Prepayment
from app.models.quotes import Quote
from app.models.sourcing import Requisition
from app.models.vendors import VendorCard

# ── Builders ─────────────────────────────────────────────────────────────


def _plan_with_line(db: Session, owner: User) -> tuple[BuyPlan, BuyPlanLine]:
    """A buy plan owned by *owner* with one cut PO line (amount = 10*2 = 20)."""
    req = Requisition(
        name=f"REQ-{uuid.uuid4().hex[:6]}",
        customer_name="AcmeCo",
        status="active",
        created_by=owner.id,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    rq = Requirement(requisition_id=req.id, primary_mpn="LM317", created_at=datetime.now(UTC))
    db.add(rq)
    db.flush()
    q = Quote(
        requisition_id=req.id,
        quote_number=f"Q-{uuid.uuid4().hex[:8]}",
        line_items=[],
        status="sent",
        created_by_id=owner.id,
        created_at=datetime.now(UTC),
    )
    db.add(q)
    db.flush()
    bp = BuyPlan(
        requisition_id=req.id,
        quote_id=q.id,
        status="active",
        so_status="approved",
        created_at=datetime.now(UTC),
    )
    db.add(bp)
    db.flush()
    vc = VendorCard(normalized_name=f"vc-{uuid.uuid4().hex[:8]}", display_name="AcmeVendor")
    db.add(vc)
    db.flush()
    off = Offer(
        requirement_id=rq.id,
        vendor_card_id=vc.id,
        vendor_name="AcmeVendor",
        vendor_name_normalized="acmevendor",
        mpn="LM317",
        normalized_mpn="LM317",
        unit_price=10.0,
    )
    db.add(off)
    db.flush()
    line = BuyPlanLine(
        buy_plan_id=bp.id,
        requirement_id=rq.id,
        offer_id=off.id,
        quantity=2,
        unit_cost=10.0,
        buyer_id=owner.id,
        status=BuyPlanLineStatus.PENDING_VERIFY.value,
        po_number="PO-2024",
        po_confirmed_at=datetime.now(UTC),
    )
    db.add(line)
    db.flush()
    return bp, line


def _seed_prepayment_on_line(db: Session, line: BuyPlanLine, owner: User, *, status: ApprovalRequestStatus) -> None:
    """Seed a live prepayment (REQUESTED/APPROVED) on *line* so prepay_state reflects
    it."""
    pp = Prepayment(
        buy_plan_id=line.buy_plan_id,
        buy_plan_line_id=line.id,
        total_incl_fees=Decimal("20.00"),
        currency="USD",
        created_by_id=owner.id,
    )
    db.add(pp)
    db.flush()
    db.add(
        ApprovalRequest(
            gate_type=ApprovalGateType.PREPAYMENT,
            status=status,
            subject_type=ApprovalSubjectType.PREPAYMENT,
            subject_id=pp.id,
            requested_by_id=owner.id,
            owner_id=owner.id,
        )
    )
    db.flush()


def _seed_approver(db: Session) -> User:
    """An unlimited prepayment approver so create routing succeeds."""
    u = User(
        email=f"approver-{uuid.uuid4().hex[:6]}@trioscs.com",
        name="PP Approver",
        role="manager",
        azure_id=f"az-{uuid.uuid4().hex[:8]}",
        is_active=True,
        can_approve_prepayments=True,
        prepayment_approval_limit=None,
        created_at=datetime.now(UTC),
    )
    db.add(u)
    db.flush()
    return u


# ── Modal ────────────────────────────────────────────────────────────────


def test_request_modal_prefills_amount_from_line(client, db_session: Session, test_user: User):
    _bp, line = _plan_with_line(db_session, test_user)
    db_session.commit()

    r = client.get(f"/v2/partials/prepayments/new?line_id={line.id}", headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    assert "Request prepayment" in r.text
    assert "20.00" in r.text  # amount (unit_cost 10 * qty 2) prefilled into total_incl_fees
    assert "AcmeVendor" in r.text  # vendor prefilled from line.offer.vendor_card
    assert "PO-2024" in r.text
    assert "name='currency'" in r.text  # currency select (USD default; finding #9)
    assert "name='vendor_name'" in r.text  # hidden payee-snapshot fallback (finding #3)


def test_request_modal_shows_po_mpn_so_readonly(client, db_session: Session, test_user: User):
    """#3: the modal confirms the exact PO before authorising cash — PO#, MPN, and the
    plan#·SO# render read-only at the top."""
    bp, line = _plan_with_line(db_session, test_user)
    bp.sales_order_number = "SO-8899"
    db_session.commit()

    r = client.get(f"/v2/partials/prepayments/new?line_id={line.id}", headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    assert "PO-2024" in r.text  # PO#
    assert "LM317" in r.text  # MPN from the line's requirement
    assert f"Plan #{bp.id}" in r.text
    assert "SO-8899" in r.text  # sales-order number


def test_request_modal_threads_origin_and_deviation_confirm(client, db_session: Session, test_user: User):
    """#12/#2: an approvals_hub-origin modal carries the origin hidden field + targets
    the hub body, and the >5% deviation client-confirm markup is present."""
    _bp, line = _plan_with_line(db_session, test_user)
    db_session.commit()

    r = client.get(
        f"/v2/partials/prepayments/new?line_id={line.id}&origin=approvals_hub&hub_scope=mine",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200, r.text
    assert "name='origin'" in r.text and "approvals_hub" in r.text  # threaded origin (#12)
    assert "#ap-hub-body" in r.text  # posts back to the hub body, not #main-content
    assert "data-deviates" in r.text and "0.05" in r.text  # >5% deviation confirm (#2)


# ── HTMX create ──────────────────────────────────────────────────────────


def test_htmx_create_makes_prepayment_linked_to_line(client, db_session: Session, test_user: User):
    _seed_approver(db_session)
    _bp, line = _plan_with_line(db_session, test_user)
    db_session.commit()

    r = client.post(
        "/v2/partials/prepayments",
        data={
            "buy_plan_id": line.buy_plan_id,
            "buy_plan_line_id": line.id,
            "payment_method": "wire",
            "total_incl_fees": "20002.38",
            "test_report_sent": "true",
            "buyer_remarks": "ok",
        },
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200, r.text
    assert "HX-Trigger" in r.headers  # success toast
    assert "showToast" in r.headers["HX-Trigger"]

    pp = db_session.query(Prepayment).filter_by(buy_plan_line_id=line.id).one()
    assert pp.total_incl_fees == Decimal("20002.38")
    assert pp.test_report_sent is True


def test_htmx_create_from_approvals_hub_rerenders_po_tab(client, db_session: Session, test_user: User):
    """#12: submitting the modal with origin=approvals_hub re-renders the workspace
    Purchase Orders tab body (not the plan detail); its list lazy-reloads so the new
    prepayment's state paints fresh."""
    _seed_approver(db_session)
    _bp, line = _plan_with_line(db_session, test_user)
    db_session.commit()

    r = client.post(
        "/v2/partials/prepayments",
        data={
            "buy_plan_id": line.buy_plan_id,
            "buy_plan_line_id": line.id,
            "payment_method": "wire",
            "total_incl_fees": "20.00",
            "origin": "approvals_hub",
            "hub_scope": "all",
        },
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200, r.text
    body = r.text
    assert "/v2/partials/approvals/purchase-orders/list" in body  # the workspace PO tab body
    assert "Line Items" not in body  # NOT the full plan detail
    assert "showToast" in r.headers.get("HX-Trigger", "")


# ── Plan-detail line pill + badge (#10/#11) ───────────────────────────────


def test_detail_line_without_prepayment_shows_live_button(client, db_session: Session, test_user: User):
    """Control: a cut PO line with no prepayment renders the live request button."""
    bp, _line = _plan_with_line(db_session, test_user)
    db_session.commit()

    r = client.get(f"/v2/partials/buy-plans/{bp.id}", headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    assert "prepayments/new" in r.text  # live request button present
    assert "Prepay requested" not in r.text


def test_detail_line_with_pending_prepayment_shows_pill_and_badge(client, db_session: Session, test_user: User):
    """#10/#11: a cut PO line with a live prepayment shows the amber 'Prepayment
    pending' badge in the status cell AND replaces the live button with a non-
    interactive pill."""
    bp, line = _plan_with_line(db_session, test_user)
    _seed_prepayment_on_line(db_session, line, test_user, status=ApprovalRequestStatus.REQUESTED)
    db_session.commit()

    r = client.get(f"/v2/partials/buy-plans/{bp.id}", headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    body = r.text
    assert "Prepayment pending" in body  # status-cell badge (#11)
    assert "Prepay requested" in body  # pill replacing the live button (#10)
    assert "prepayments/new" not in body  # live button suppressed


def test_htmx_create_duplicate_pending_returns_error_toast(client, db_session: Session, test_user: User):
    _seed_approver(db_session)
    _bp, line = _plan_with_line(db_session, test_user)
    db_session.commit()

    payload = {
        "buy_plan_id": line.buy_plan_id,
        "buy_plan_line_id": line.id,
        "payment_method": "wire",
        "total_incl_fees": "50.00",
        "test_report_sent": "false",
    }
    first = client.post("/v2/partials/prepayments", data=payload, headers={"HX-Request": "true"})
    assert first.status_code == 200, first.text

    second = client.post("/v2/partials/prepayments", data=payload, headers={"HX-Request": "true"})
    # Duplicate-pending guard → honest error toast (200 + HX-Reswap none), not a silent 500.
    assert second.status_code == 200
    assert second.headers.get("HX-Reswap") == "none"
    assert "error" in second.headers.get("HX-Trigger", "")
    assert db_session.query(Prepayment).filter_by(buy_plan_line_id=line.id).count() == 1


# ── Button visibility ────────────────────────────────────────────────────


def test_can_request_prepayment_true_for_owner_on_cut_po(db_session: Session, test_user: User):
    _bp, line = _plan_with_line(db_session, test_user)
    db_session.commit()
    assert can_request_prepayment(test_user, line) is True


def test_can_request_prepayment_false_without_po(db_session: Session, test_user: User):
    _bp, line = _plan_with_line(db_session, test_user)
    line.po_number = None
    line.status = BuyPlanLineStatus.AWAITING_PO.value
    db_session.commit()
    assert can_request_prepayment(test_user, line) is False


def test_can_request_prepayment_false_for_restricted_non_owner(db_session: Session, test_user: User):
    _bp, line = _plan_with_line(db_session, test_user)  # owned by test_user
    stranger = User(
        email=f"sales-{uuid.uuid4().hex[:6]}@trioscs.com",
        name="Stranger Sales",
        role=UserRole.SALES.value,
        azure_id=f"az-{uuid.uuid4().hex[:8]}",
        created_at=datetime.now(UTC),
    )
    db_session.add(stranger)
    db_session.commit()
    assert can_request_prepayment(stranger, line) is False
