"""
test_buyplan_v3_po_verify.py — Tests for PO verification scanning.

Tests verify_po_sent() which scans buyer Outlook sent folders for PO emails.
Mocks Graph API interactions to test verification logic in isolation.

Called by: pytest
Depends on: app.services.buyplan_workflow.verify_po_sent, conftest fixtures
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Offer, Quote, Requirement, Requisition, User
from app.models.buy_plan import (
    BuyPlanLine,
    BuyPlanLineStatus,
    BuyPlanStatus,
    BuyPlan,
)
from app.services.buyplan_workflow import verify_po_sent


# ── Helpers ──────────────────────────────────────────────────────────


def _make_plan(db: Session, buyer: User, quote: Quote, requisition: Requisition) -> BuyPlan:
    """Create a BuyPlan with no lines (caller adds lines)."""
    plan = BuyPlan(
        quote_id=quote.id,
        requisition_id=requisition.id,
        status=BuyPlanStatus.active.value,
        created_at=datetime.now(timezone.utc),
    )
    db.add(plan)
    db.flush()
    return plan


def _make_line(
    db: Session,
    plan: BuyPlan,
    buyer: User | None = None,
    po_number: str | None = None,
    status: str = BuyPlanLineStatus.pending_verify.value,
) -> BuyPlanLine:
    """Create a BuyPlanLine attached to plan."""
    line = BuyPlanLine(
        buy_plan_id=plan.id,
        quantity=100,
        status=status,
        po_number=po_number,
        buyer_id=buyer.id if buyer else None,
    )
    db.add(line)
    db.flush()
    return line


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_po_found(db_session, test_user, test_quote, test_requisition):
    """When Graph API finds PO emails, line status changes to verified."""
    plan = _make_plan(db_session, test_user, test_quote, test_requisition)
    line = _make_line(db_session, plan, buyer=test_user, po_number="PO-12345")
    db_session.commit()
    db_session.refresh(plan)

    mock_client = MagicMock()
    mock_client.search_sent_messages = AsyncMock(return_value=[{"id": "msg-1", "subject": "PO-12345"}])

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_client),
    ):
        results = await verify_po_sent(plan, db_session)

    assert len(results) == 1
    assert results[0]["found"] is True
    assert results[0]["message_count"] == 1
    assert results[0]["po_number"] == "PO-12345"
    # Line should now be verified
    db_session.refresh(line)
    assert line.status == BuyPlanLineStatus.verified.value
    assert line.po_verified_at is not None


@pytest.mark.asyncio
async def test_po_not_found(db_session, test_user, test_quote, test_requisition):
    """When Graph API returns no messages, found=False and status unchanged."""
    plan = _make_plan(db_session, test_user, test_quote, test_requisition)
    line = _make_line(db_session, plan, buyer=test_user, po_number="PO-99999")
    db_session.commit()
    db_session.refresh(plan)

    mock_client = MagicMock()
    mock_client.search_sent_messages = AsyncMock(return_value=[])

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_client),
    ):
        results = await verify_po_sent(plan, db_session)

    assert len(results) == 1
    assert results[0]["found"] is False
    assert results[0]["message_count"] == 0
    db_session.refresh(line)
    assert line.status == BuyPlanLineStatus.pending_verify.value


@pytest.mark.asyncio
async def test_graph_error(db_session, test_user, test_quote, test_requisition):
    """When GraphClient raises an exception, error is captured in results."""
    plan = _make_plan(db_session, test_user, test_quote, test_requisition)
    _make_line(db_session, plan, buyer=test_user, po_number="PO-ERR")
    db_session.commit()
    db_session.refresh(plan)

    mock_client = MagicMock()
    mock_client.search_sent_messages = AsyncMock(side_effect=RuntimeError("Graph API timeout"))

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_client),
    ):
        results = await verify_po_sent(plan, db_session)

    assert len(results) == 1
    assert results[0]["found"] is False
    assert "error" in results[0]
    assert "Graph API timeout" in results[0]["error"]


@pytest.mark.asyncio
async def test_all_verified_auto_completes(db_session, test_user, test_quote, test_requisition):
    """When all PO lines are verified, plan auto-completes."""
    plan = _make_plan(db_session, test_user, test_quote, test_requisition)
    line1 = _make_line(db_session, plan, buyer=test_user, po_number="PO-001")
    line2 = _make_line(db_session, plan, buyer=test_user, po_number="PO-002")
    db_session.commit()
    db_session.refresh(plan)

    mock_client = MagicMock()
    mock_client.search_sent_messages = AsyncMock(return_value=[{"id": "msg-1"}])

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_client),
    ):
        results = await verify_po_sent(plan, db_session)

    assert len(results) == 2
    assert all(r["found"] for r in results)
    db_session.refresh(plan)
    assert plan.status == BuyPlanStatus.completed.value
    assert plan.completed_at is not None


@pytest.mark.asyncio
async def test_no_buyer_skips(db_session, test_user, test_quote, test_requisition):
    """Line without buyer_id gets skipped with reason."""
    plan = _make_plan(db_session, test_user, test_quote, test_requisition)
    _make_line(db_session, plan, buyer=None, po_number="PO-NOBUY")
    db_session.commit()
    db_session.refresh(plan)

    results = await verify_po_sent(plan, db_session)

    assert len(results) == 1
    assert results[0]["skipped"] is True
    assert results[0]["reason"] == "no_buyer"
    assert results[0]["found"] is False


@pytest.mark.asyncio
async def test_no_token_skips(db_session, test_user, test_quote, test_requisition):
    """When get_valid_token returns None, line is skipped."""
    plan = _make_plan(db_session, test_user, test_quote, test_requisition)
    _make_line(db_session, plan, buyer=test_user, po_number="PO-NOTOK")
    db_session.commit()
    db_session.refresh(plan)

    with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value=None):
        results = await verify_po_sent(plan, db_session)

    assert len(results) == 1
    assert results[0]["skipped"] is True
    assert results[0]["reason"] == "no_token"
    assert results[0]["found"] is False
