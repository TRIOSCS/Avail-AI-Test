"""test_buyplan_v3_po_verify.py — Tests for V3 PO verification scanning.

Validates that verify_po_sent_v3() correctly searches buyer Outlook sent
folders via Graph API, marks lines as verified, handles errors gracefully,
and auto-completes plans when all PO lines are verified.

Called by: pytest
Depends on: conftest fixtures (db_session, client, test_user), buyplan_workflow
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, Offer, Quote, Requirement, Requisition, User
from app.models.buy_plan import (
    BuyPlan,
    BuyPlanLine,
    BuyPlanLineStatus,
    BuyPlanStatus,
    SOVerificationStatus,
)
from app.services.buyplan_workflow import verify_po_sent_v3

# ── Helpers ──────────────────────────────────────────────────────────


def _make_plan_with_lines(
    db: Session,
    buyer: User,
    po_numbers: list[str | None],
    *,
    line_status: str = BuyPlanLineStatus.pending_verify.value,
    plan_status: str = BuyPlanStatus.active.value,
) -> BuyPlan:
    """Create a BuyPlan with lines, each having a po_number from the list."""
    # Create required parent objects
    company = Company(
        name="PO Test Corp",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(company)
    db.flush()

    site = CustomerSite(
        company_id=company.id,
        site_name="PO Test HQ",
        contact_name="Test Contact",
        contact_email="test@potestcorp.com",
    )
    db.add(site)
    db.flush()

    requisition = Requisition(
        name="REQ-PO-TEST",
        customer_name="Test Corp",
        status="open",
        created_by=buyer.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(requisition)
    db.flush()

    requirement = Requirement(
        requisition_id=requisition.id,
        primary_mpn="TEST-MPN-001",
        target_qty=100,
        target_price=1.00,
        created_at=datetime.now(timezone.utc),
    )
    db.add(requirement)
    db.flush()

    offer = Offer(
        requisition_id=requisition.id,
        vendor_name="Test Vendor",
        mpn="TEST-MPN-001",
        qty_available=500,
        unit_price=0.80,
        entered_by_id=buyer.id,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db.add(offer)
    db.flush()

    quote = Quote(
        requisition_id=requisition.id,
        customer_site_id=site.id,
        quote_number="Q-PO-TEST",
        status="sent",
        line_items=[],
        subtotal=100.00,
        total_cost=80.00,
        total_margin_pct=20.0,
        created_by_id=buyer.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(quote)
    db.flush()

    plan = BuyPlan(
        quote_id=quote.id,
        requisition_id=requisition.id,
        status=plan_status,
        so_status=SOVerificationStatus.approved.value,
        created_at=datetime.now(timezone.utc),
    )
    db.add(plan)
    db.flush()

    for po_num in po_numbers:
        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=requirement.id,
            offer_id=offer.id,
            quantity=100,
            unit_cost=0.80,
            status=line_status,
            po_number=po_num,
            buyer_id=buyer.id if po_num else None,
            po_confirmed_at=datetime.now(timezone.utc) if po_num else None,
        )
        db.add(line)

    db.flush()
    db.refresh(plan)
    return plan


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_po_found_in_sent_folder(db_session: Session, test_user: User):
    """PO found in buyer's sent folder — line marked verified."""
    plan = _make_plan_with_lines(db_session, test_user, ["PO-12345"])

    mock_messages = [
        {
            "id": "msg-001",
            "subject": "PO-12345 Order Confirmation",
            "toRecipients": [{"emailAddress": {"address": "vendor@example.com"}}],
            "sentDateTime": "2026-03-15T10:30:00Z",
        }
    ]

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="mock-token"),
        patch("app.utils.graph_client.GraphClient") as MockGC,
    ):
        mock_client = AsyncMock()
        mock_client.search_sent_messages = AsyncMock(return_value=mock_messages)
        MockGC.return_value = mock_client

        results = await verify_po_sent_v3(plan, db_session)

    assert "PO-12345" in results
    assert results["PO-12345"]["verified"] is True
    assert results["PO-12345"]["recipient"] == "vendor@example.com"
    assert results["PO-12345"]["sent_at"] == "2026-03-15T10:30:00Z"
    assert results["PO-12345"]["reason"] is None

    # Line should be marked verified
    line = plan.lines[0]
    assert line.status == BuyPlanLineStatus.verified.value
    assert line.po_verified_at is not None


@pytest.mark.asyncio
async def test_verify_po_not_found(db_session: Session, test_user: User):
    """PO not found in sent folder — line stays in pending_verify."""
    plan = _make_plan_with_lines(db_session, test_user, ["PO-99999"])

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="mock-token"),
        patch("app.utils.graph_client.GraphClient") as MockGC,
    ):
        mock_client = AsyncMock()
        mock_client.search_sent_messages = AsyncMock(return_value=[])
        MockGC.return_value = mock_client

        results = await verify_po_sent_v3(plan, db_session)

    assert results["PO-99999"]["verified"] is False
    assert results["PO-99999"]["reason"] == "not_found_in_sent"

    line = plan.lines[0]
    assert line.status == BuyPlanLineStatus.pending_verify.value


@pytest.mark.asyncio
async def test_verify_po_graph_error(db_session: Session, test_user: User):
    """Graph API error handled gracefully — returns error reason."""
    plan = _make_plan_with_lines(db_session, test_user, ["PO-ERR-001"])

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="mock-token"),
        patch("app.utils.graph_client.GraphClient") as MockGC,
    ):
        mock_client = AsyncMock()
        mock_client.search_sent_messages = AsyncMock(side_effect=RuntimeError("Graph 503"))
        MockGC.return_value = mock_client

        results = await verify_po_sent_v3(plan, db_session)

    assert results["PO-ERR-001"]["verified"] is False
    assert "graph_error" in results["PO-ERR-001"]["reason"]
    assert "Graph 503" in results["PO-ERR-001"]["reason"]

    # Line should stay unchanged
    line = plan.lines[0]
    assert line.status == BuyPlanLineStatus.pending_verify.value


@pytest.mark.asyncio
async def test_verify_po_all_verified_auto_completes(db_session: Session, test_user: User):
    """All PO lines verified — plan auto-completes."""
    plan = _make_plan_with_lines(db_session, test_user, ["PO-A", "PO-B"])

    mock_messages = [
        {
            "id": "msg-x",
            "subject": "PO Sent",
            "toRecipients": [{"emailAddress": {"address": "v@example.com"}}],
            "sentDateTime": "2026-03-15T12:00:00Z",
        }
    ]

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="mock-token"),
        patch("app.utils.graph_client.GraphClient") as MockGC,
    ):
        mock_client = AsyncMock()
        mock_client.search_sent_messages = AsyncMock(return_value=mock_messages)
        MockGC.return_value = mock_client

        results = await verify_po_sent_v3(plan, db_session)

    assert results["PO-A"]["verified"] is True
    assert results["PO-B"]["verified"] is True

    # Plan should be auto-completed
    assert plan.status == BuyPlanStatus.completed.value
    assert plan.completed_at is not None


@pytest.mark.asyncio
async def test_verify_po_no_buyer_skips_line(db_session: Session, test_user: User):
    """Line without buyer_id skipped gracefully."""
    plan = _make_plan_with_lines(db_session, test_user, ["PO-NOBUYER"])
    # Remove buyer from the line
    line = plan.lines[0]
    line.buyer_id = None
    db_session.flush()

    results = await verify_po_sent_v3(plan, db_session)

    assert results["PO-NOBUYER"]["verified"] is False
    assert results["PO-NOBUYER"]["reason"] == "no_buyer"


def test_verify_po_endpoint(client, db_session: Session, test_user: User):
    """GET /api/buy-plans/{plan_id}/verify-po returns verification results."""
    plan = _make_plan_with_lines(db_session, test_user, ["PO-EP-001"])
    db_session.commit()

    mock_messages = [
        {
            "id": "msg-ep",
            "subject": "PO-EP-001",
            "toRecipients": [{"emailAddress": {"address": "vendor@test.com"}}],
            "sentDateTime": "2026-03-16T08:00:00Z",
        }
    ]

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="mock-token"),
        patch("app.utils.graph_client.GraphClient") as MockGC,
    ):
        mock_client = AsyncMock()
        mock_client.search_sent_messages = AsyncMock(return_value=mock_messages)
        MockGC.return_value = mock_client

        resp = client.get(f"/api/buy-plans/{plan.id}/verify-po")

    assert resp.status_code == 200
    data = resp.json()
    assert data["plan_id"] == plan.id
    assert "verifications" in data
    assert data["verifications"]["PO-EP-001"]["verified"] is True
