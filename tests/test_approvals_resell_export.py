"""test_approvals_resell_export.py — CSV export for the Approvals console lists AND the
Resell offers / outreach data (UX-audit gap: a manager could not pull these for
reporting).

Covers the new GET export endpoints:
  - Approvals hub (GET /v2/partials/approvals/{tab}/export): the Buy Plans / Sales Orders
    tracking list (buy-plan) + the Prepayment and PO Approval "Recently resolved" audit
    feeds — same require_user auth + SEE-ALL/SEE-MINE scope as the console tab body.
  - Resell detail (GET /v2/partials/resell/{list_id}/offers|outreach/export): the
    competing-broker Offers tab + the Outreach tracker — same owner-only gate as the tabs.

Each endpoint: 200 + text/csv + attachment header, a header row + one row per matching
record, scope/owner scoping, auth parity, and the export anchors rendering with
hx-boost="false".

Called by: pytest
Depends on: conftest (client, unauthenticated_client, db_session, test_user, test_company),
            app.models.* , app.constants.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import (
    ActivityType,
    ApprovalGateType,
    ApprovalRecipientStatus,
    ApprovalRequestStatus,
    ApprovalSubjectType,
    BuyPlanStatus,
    ExcessListStatus,
    ExcessOfferStatus,
    ExcessOutreachStatus,
    OfferLineMatchStatus,
    SOVerificationStatus,
)
from app.models import Company, User, VendorCard
from app.models.approvals import ApprovalRequest, ApprovalStep, ApprovalStepRecipient
from app.models.buy_plan import BuyPlan
from app.models.excess import (
    ExcessLineItem,
    ExcessList,
    ExcessOffer,
    ExcessOfferLine,
    ExcessOutreach,
)
from app.models.intelligence import ActivityLog
from app.models.quality_plan import Prepayment
from app.models.quotes import Quote
from app.models.sourcing import Requirement, Requisition


def _parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def _assert_attachment(resp, *, filename_contains: str) -> None:
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    assert filename_contains in disposition


# ══════════════════════════ Approvals hub builders ══════════════════════════


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


def _plan(db: Session, req: Requisition, q: Quote, *, status: str = BuyPlanStatus.ACTIVE.value) -> BuyPlan:
    bp = BuyPlan(
        requisition_id=req.id,
        quote_id=q.id,
        status=status,
        so_status=SOVerificationStatus.APPROVED.value,
        sales_order_number=f"SO-{uuid.uuid4().hex[:4]}",
        submitted_by_id=req.created_by,
        total_cost=1000.0,
        total_revenue=2000.0,
        total_margin_pct=50.0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(bp)
    db.flush()
    return bp


def _resolved_prepay(db: Session, bp: BuyPlan, user: User, *, beneficiary: str = "Northwind LLC") -> Prepayment:
    """A PREPAYMENT request in the resolved (approved) state — lands in Recently-
    resolved."""
    vc = VendorCard(
        normalized_name=f"vc-{uuid.uuid4().hex[:8]}",
        display_name="WireVendor",
        legal_name=beneficiary,
    )
    db.add(vc)
    db.flush()
    pp = Prepayment(
        buy_plan_id=bp.id,
        vendor_card_id=vc.id,
        total_incl_fees=Decimal("2500.00"),
        currency="USD",
        wire_reference="FT-EXPORT-1",
        created_by_id=user.id,
    )
    db.add(pp)
    db.flush()
    now = datetime.now(timezone.utc)
    ar = ApprovalRequest(
        gate_type=ApprovalGateType.PREPAYMENT,
        status=ApprovalRequestStatus.APPROVED.value,
        subject_type=ApprovalSubjectType.PREPAYMENT,
        subject_id=pp.id,
        amount=Decimal("2500.00"),
        currency="USD",
        requested_by_id=user.id,
        owner_id=user.id,
        resolved_at=now,
    )
    db.add(ar)
    db.flush()
    step = ApprovalStep(request_id=ar.id, seq=1, rule="any", status="approved")
    db.add(step)
    db.flush()
    db.add(
        ApprovalStepRecipient(
            step_id=step.id,
            user_id=user.id,
            status=ApprovalRecipientStatus.APPROVED.value,
            decided_at=now,
        )
    )
    db.flush()
    return pp


def _po_history(db: Session, bp: BuyPlan, user: User) -> ActivityLog:
    """A durable PO_LINE_VERIFIED activity — the PO Approval Recently-resolved feed."""
    log = ActivityLog(
        user_id=user.id,
        activity_type=ActivityType.PO_LINE_VERIFIED,
        channel="system",
        buy_plan_id=bp.id,
        subject="PO-EXPORT verified",
        notes="PO-EXPORT verified by buyer",
        occurred_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    db.add(log)
    db.flush()
    return log


def _other_user(db: Session) -> User:
    u = User(
        email=f"other-{uuid.uuid4().hex[:6]}@t.com",
        name="Other Owner",
        role="buyer",
        azure_id=f"az-{uuid.uuid4().hex[:8]}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


# ══════════════════════════ Approvals hub — buy-plan ══════════════════════════


def test_buy_plan_export_is_csv_attachment(client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    _plan(db_session, req, q)
    db_session.commit()

    resp = client.get("/v2/partials/approvals/buy-plan/export")
    _assert_attachment(resp, filename_contains="approvals_buy_plans_all.csv")


def test_buy_plan_export_header_and_one_row_per_plan(client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q)
    db_session.commit()

    rows = _parse_csv(client.get("/v2/partials/approvals/buy-plan/export").text)

    assert rows[0] == ["Plan ID", "Customer", "Sales Order", "Status", "Value"]
    assert len(rows) == 2  # header + one plan
    body = "\n".join(",".join(r) for r in rows[1:])
    assert str(bp.id) in body
    assert "AcmeCo" in body
    assert bp.sales_order_number in body
    assert BuyPlanStatus.ACTIVE.value in body


def test_buy_plan_export_scope_mine_filters_to_own_plans(client: TestClient, db_session: Session, test_user: User):
    my_req, my_q, _ = _req_quote(db_session, test_user)
    mine = _plan(db_session, my_req, my_q)
    other = _other_user(db_session)
    o_req, o_q, _ = _req_quote(db_session, other)
    theirs = _plan(db_session, o_req, o_q)
    db_session.commit()

    # Compare the Plan ID column (rows[i][0]) exactly — a bare substring check can false-match
    # a small id inside a random SO number or the value cell.
    all_ids = {r[0] for r in _parse_csv(client.get("/v2/partials/approvals/buy-plan/export?scope=all").text)[1:]}
    assert {str(mine.id), str(theirs.id)} <= all_ids

    resp = client.get("/v2/partials/approvals/buy-plan/export?scope=mine")
    assert "approvals_buy_plans_mine.csv" in resp.headers["content-disposition"]
    mine_ids = {r[0] for r in _parse_csv(resp.text)[1:]}
    assert str(mine.id) in mine_ids
    assert str(theirs.id) not in mine_ids


# ══════════════════════════ Approvals hub — prepayment ══════════════════════════


def test_prepayment_resolved_export_row(client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q)
    pp = _resolved_prepay(db_session, bp, test_user, beneficiary="Northwind Components LLC")
    db_session.commit()

    resp = client.get("/v2/partials/approvals/prepayment/export")
    _assert_attachment(resp, filename_contains="approvals_prepayments_resolved_all.csv")
    rows = _parse_csv(resp.text)

    assert rows[0][0] == "Prepayment ID" and "Beneficiary" in rows[0] and "Wire Reference" in rows[0]
    assert len(rows) == 2  # header + one resolved prepayment
    body = "\n".join(",".join(r) for r in rows[1:])
    assert str(pp.id) in body
    assert "Northwind Components LLC" in body  # beneficiary (legal name)
    assert "FT-EXPORT-1" in body  # wire reference
    assert "Test Buyer" in body  # decided-by (test_user)


def test_prepayment_export_scope_mine_filters_to_own(client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q)
    mine = _resolved_prepay(db_session, bp, test_user, beneficiary="Mine Payee LLC")
    # A resolved prepayment owned/requested by someone else — org-wide visible under scope=all.
    other = _other_user(db_session)
    theirs = _resolved_prepay(db_session, bp, other, beneficiary="Their Payee LLC")
    db_session.commit()

    all_body = client.get("/v2/partials/approvals/prepayment/export?scope=all").text
    assert str(mine.id) in all_body and str(theirs.id) in all_body

    mine_body = client.get("/v2/partials/approvals/prepayment/export?scope=mine").text
    assert "Mine Payee LLC" in mine_body
    assert "Their Payee LLC" not in mine_body


# ══════════════════════════ Approvals hub — po-approval ══════════════════════════


def test_po_approval_history_export_row(client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q)
    _po_history(db_session, bp, test_user)
    db_session.commit()

    resp = client.get("/v2/partials/approvals/po-approval/export")
    _assert_attachment(resp, filename_contains="approvals_po_resolved.csv")
    rows = _parse_csv(resp.text)

    assert rows[0] == ["Plan ID", "Outcome", "Description", "Actor", "Note", "Resolved Date"]
    assert len(rows) == 2  # header + one history event
    body = "\n".join(",".join(r) for r in rows[1:])
    assert str(bp.id) in body
    assert "verified" in body  # outcome kind
    assert "PO-EXPORT verified" in body  # label
    assert "Test Buyer" in body  # actor


# ══════════════════════════ Approvals hub — auth / 404 / buttons ══════════════════════════


@pytest.mark.parametrize("tab", ["buy-plan", "prepayment", "po-approval"])
def test_approvals_export_unauthenticated_rejected(unauthenticated_client: TestClient, tab: str):
    resp = unauthenticated_client.get(f"/v2/partials/approvals/{tab}/export", follow_redirects=False)
    assert resp.status_code in (401, 403)


def test_approvals_export_unknown_tab_404(client: TestClient):
    assert client.get("/v2/partials/approvals/bogus/export").status_code == 404


def test_buy_plan_tab_renders_export_anchor(client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    _plan(db_session, req, q)
    db_session.commit()

    html = client.get("/v2/partials/approvals/buy-plan").text
    assert "Export CSV" in html
    assert 'hx-boost="false"' in html
    assert "/v2/partials/approvals/buy-plan/export?scope=all" in html


def test_prepayment_tab_renders_export_anchor(client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q)
    _resolved_prepay(db_session, bp, test_user)
    db_session.commit()

    html = client.get("/v2/partials/approvals/prepayment").text
    assert "Export CSV" in html
    assert 'hx-boost="false"' in html
    assert "/v2/partials/approvals/prepayment/export?scope=all" in html


def test_po_approval_tab_renders_export_anchor(client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q)
    _po_history(db_session, bp, test_user)
    db_session.commit()

    html = client.get("/v2/partials/approvals/po-approval").text
    assert "Export CSV" in html
    assert 'hx-boost="false"' in html
    assert "/v2/partials/approvals/po-approval/export?scope=all" in html


# ══════════════════════════ Resell fixtures ══════════════════════════


@pytest.fixture()
def trader_user(db_session: Session) -> User:
    """The list owner — a trader (can post + owns the list = can offer it out)."""
    user = User(
        email="x-trader@trioscs.com",
        name="Ex Trader",
        role="trader",
        azure_id=f"x-az-{uuid.uuid4().hex[:8]}",
        m365_connected=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def posted_list(db_session: Session, trader_user: User, test_company: Company) -> ExcessList:
    """A posted (collecting) list owned by the trader, with one line."""
    el = ExcessList(
        title="X surplus caps",
        company_id=test_company.id,
        owner_id=trader_user.id,
        status=ExcessListStatus.COLLECTING,
        total_line_items=1,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(el)
    db_session.flush()
    db_session.add(
        ExcessLineItem(
            excess_list_id=el.id,
            part_number="GRM188R",
            quantity=1000,
            condition="Used",
            asking_price=Decimal("1.00"),
        )
    )
    db_session.commit()
    db_session.refresh(el)
    return el


def _own(user: User):
    """Override require_user to *user* (the owner).

    Returns a cleanup callable.
    """
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: user
    return lambda: app.dependency_overrides.pop(require_user, None)


def _buyer_card(db: Session, name: str) -> VendorCard:
    vc = VendorCard(normalized_name=name.lower(), display_name=name)
    db.add(vc)
    db.flush()
    return vc


# ══════════════════════════ Resell — offers export ══════════════════════════


def test_offers_export_is_csv_attachment_with_rows(
    client: TestClient, db_session: Session, trader_user: User, posted_list: ExcessList
):
    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id).first()
    buyer = _buyer_card(db_session, "Broker Alpha")
    offer = ExcessOffer(
        excess_list_id=posted_list.id,
        submitted_by=trader_user.id,
        offerer_vendor_card_id=buyer.id,
        scope="per_line",
        status=ExcessOfferStatus.OPEN,
    )
    db_session.add(offer)
    db_session.flush()
    db_session.add(
        ExcessOfferLine(
            offer_id=offer.id,
            excess_line_item_id=line.id,
            mpn_raw="GRM188R",
            quantity=250,
            unit_price=Decimal("0.9000"),
            match_status=OfferLineMatchStatus.MATCHED,
        )
    )
    db_session.commit()

    restore = _own(trader_user)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/offers/export")
        _assert_attachment(resp, filename_contains=f"resell_offers_list_{posted_list.id}.csv")
        rows = _parse_csv(resp.text)
        assert rows[0][0] == "Offer ID" and "Broker" in rows[0] and "Condition" in rows[0]
        assert len(rows) == 2  # header + one offer line
        body = "\n".join(",".join(r) for r in rows[1:])
        assert "Broker Alpha" in body  # buyer (broker) name — owner-only view
        assert "GRM188R" in body
        assert "250" in body  # qty
        assert "0.9000" in body  # unit price
        assert "Used" in body  # condition (from matched line item)
        assert "open" in body  # status
    finally:
        restore()


def test_offers_export_take_all_row(
    client: TestClient, db_session: Session, trader_user: User, posted_list: ExcessList
):
    buyer = _buyer_card(db_session, "Broker Whole")
    db_session.add(
        ExcessOffer(
            excess_list_id=posted_list.id,
            submitted_by=trader_user.id,
            offerer_vendor_card_id=buyer.id,
            scope="take_all",
            take_all_total_price=Decimal("5000.00"),
            status=ExcessOfferStatus.OPEN,
        )
    )
    db_session.commit()

    restore = _own(trader_user)
    try:
        rows = _parse_csv(client.get(f"/v2/partials/resell/{posted_list.id}/offers/export").text)
        assert len(rows) == 2  # header + one take-all summary row
        body = "\n".join(",".join(r) for r in rows[1:])
        assert "Broker Whole" in body
        assert "take_all" in body
        assert "5000.00" in body  # lump take-all total
    finally:
        restore()


def test_offers_export_owner_gated(client: TestClient, db_session: Session, posted_list: ExcessList):
    """A non-owner (default buyer client) cannot export the private offers → 403."""
    resp = client.get(f"/v2/partials/resell/{posted_list.id}/offers/export")
    assert resp.status_code == 403


def test_offers_tab_renders_export_anchor(
    client: TestClient, db_session: Session, trader_user: User, posted_list: ExcessList
):
    restore = _own(trader_user)
    try:
        html = client.get(f"/v2/partials/resell/{posted_list.id}/offers").text
        assert "Export CSV" in html
        assert 'hx-boost="false"' in html
        assert f"/v2/partials/resell/{posted_list.id}/offers/export" in html
    finally:
        restore()


# ══════════════════════════ Resell — outreach export ══════════════════════════


def test_outreach_export_is_csv_attachment_with_rows(
    client: TestClient, db_session: Session, trader_user: User, posted_list: ExcessList
):
    buyer = _buyer_card(db_session, "Reach Buyer")
    db_session.add(
        ExcessOutreach(
            excess_list_id=posted_list.id,
            target_vendor_card_id=buyer.id,
            submitted_by=trader_user.id,
            channel="phone",
            status=ExcessOutreachStatus.BID,
            sent_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    restore = _own(trader_user)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/outreach/export")
        _assert_attachment(resp, filename_contains=f"resell_outreach_list_{posted_list.id}.csv")
        rows = _parse_csv(resp.text)
        assert rows[0] == ["Buyer", "Line", "Channel", "Sent By", "Status", "Sent At", "Last Activity"]
        assert len(rows) == 2  # header + one outreach touch
        body = "\n".join(",".join(r) for r in rows[1:])
        assert "Reach Buyer" in body
        assert "phone" in body  # channel
        assert "bid" in body  # status
        assert "Whole list" in body  # no per-line item
        assert "Ex Trader" in body  # sent-by
    finally:
        restore()


def test_outreach_export_owner_gated(client: TestClient, db_session: Session, posted_list: ExcessList):
    """The tracker is the owner's private board → a non-owner export gets 403."""
    resp = client.get(f"/v2/partials/resell/{posted_list.id}/outreach/export")
    assert resp.status_code == 403


def test_outreach_tab_renders_export_anchor(
    client: TestClient, db_session: Session, trader_user: User, posted_list: ExcessList
):
    restore = _own(trader_user)
    try:
        html = client.get(f"/v2/partials/resell/{posted_list.id}/outreach").text
        assert "Export CSV" in html
        assert 'hx-boost="false"' in html
        assert f"/v2/partials/resell/{posted_list.id}/outreach/export" in html
    finally:
        restore()
