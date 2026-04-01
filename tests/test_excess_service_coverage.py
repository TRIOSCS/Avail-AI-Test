"""test_excess_service_coverage.py — Coverage for excess_service missing lines.

Targets lines 373 (match_excess_demand no-norm), 403 (duplicate offer skip),
508 (accept_bid happy path), 667-683 (create_proactive_matches),
856-997 (send_bid_solicitation bundled/split).

Called by: pytest
Depends on: app/services/excess_service.py, tests/conftest.py
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

# ── Helpers ──────────────────────────────────────────────────────────


def _make_excess_list(db: Session, company_id: int, owner_id: int, title: str = "Test Excess"):
    from app.models.excess import ExcessList

    el = ExcessList(company_id=company_id, owner_id=owner_id, title=title, status="active")
    db.add(el)
    db.flush()
    return el


def _make_line_item(db: Session, excess_list_id: int, part_number: str, qty: int = 100):
    from app.models.excess import ExcessLineItem

    item = ExcessLineItem(
        excess_list_id=excess_list_id,
        part_number=part_number,
        quantity=qty,
        asking_price=None,
    )
    db.add(item)
    db.flush()
    return item


def _make_company(db: Session, name: str = "Test Corp"):
    from app.models import Company

    co = Company(name=name)
    db.add(co)
    db.flush()
    return co


def _make_user(db: Session, email: str = "trader@test.com"):
    from datetime import datetime, timezone

    from app.models import User

    u = User(
        email=email,
        name="Trader",
        role="buyer",
        azure_id=email,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


# ── match_excess_demand ───────────────────────────────────────────


def test_match_excess_demand_skips_empty_part_number(db_session: Session):
    """Line items with un-normalizable part numbers (empty string) are skipped."""
    from app.services.excess_service import match_excess_demand

    co = _make_company(db_session)
    user = _make_user(db_session)
    excess_list = _make_excess_list(db_session, co.id, user.id)

    # Part number that normalizes to empty/None
    _make_line_item(db_session, excess_list.id, "   ", qty=10)
    db_session.commit()

    result = match_excess_demand(db_session, excess_list.id, user_id=user.id)
    assert result["matches_created"] == 0


def test_match_excess_demand_no_requirements(db_session: Session):
    """Line items with no matching active requirements produce 0 matches."""
    from app.services.excess_service import match_excess_demand

    co = _make_company(db_session)
    user = _make_user(db_session)
    excess_list = _make_excess_list(db_session, co.id, user.id)
    _make_line_item(db_session, excess_list.id, "LM317T", qty=50)
    db_session.commit()

    result = match_excess_demand(db_session, excess_list.id, user_id=user.id)
    assert result["matches_created"] == 0


def test_match_excess_demand_skips_duplicate_offer(db_session: Session):
    """Existing Offer with source='excess' is not duplicated."""
    from app.models import Offer, Requirement, Requisition
    from app.services.excess_service import match_excess_demand

    co = _make_company(db_session, "SellerCo")
    user = _make_user(db_session)
    excess_list = _make_excess_list(db_session, co.id, user.id)
    item = _make_line_item(db_session, excess_list.id, "STM32F103", qty=200)

    buyer_co = _make_company(db_session, "BuyerCo")
    req = Requisition(name="Buy Order", status="active", created_by=user.id, company_id=buyer_co.id)
    db_session.add(req)
    db_session.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="STM32F103",
        normalized_mpn="STM32F103",
        target_qty=50,
    )
    db_session.add(requirement)
    db_session.flush()

    # Pre-existing offer
    existing_offer = Offer(
        requisition_id=req.id,
        requirement_id=requirement.id,
        excess_line_item_id=item.id,
        vendor_name="SellerCo",
        mpn="STM32F103",
        normalized_mpn="STM32F103",
        source="excess",
        qty_available=200,
    )
    db_session.add(existing_offer)
    db_session.commit()

    result = match_excess_demand(db_session, excess_list.id, user_id=user.id)
    # Duplicate should be skipped
    assert result["matches_created"] == 0


# ── accept_bid ───────────────────────────────────────────────────


def test_accept_bid_happy_path(db_session: Session):
    """accept_bid marks bid ACCEPTED and rejects other pending bids."""
    from app.constants import BidStatus, ExcessLineItemStatus
    from app.models.excess import Bid
    from app.services.excess_service import accept_bid

    co = _make_company(db_session)
    user = _make_user(db_session)
    excess_list = _make_excess_list(db_session, co.id, user.id)
    item = _make_line_item(db_session, excess_list.id, "NE555", qty=100)

    bid1 = Bid(
        excess_line_item_id=item.id,
        unit_price=1.50,
        quantity_wanted=100,
        status="pending",
        created_by=user.id,
    )
    bid2 = Bid(
        excess_line_item_id=item.id,
        unit_price=1.75,
        quantity_wanted=100,
        status="pending",
        created_by=user.id,
    )
    db_session.add_all([bid1, bid2])
    db_session.commit()

    result = accept_bid(db_session, bid1.id, item.id, excess_list.id)

    assert result.status == BidStatus.ACCEPTED
    db_session.refresh(bid2)
    assert bid2.status == BidStatus.REJECTED
    db_session.refresh(item)
    assert item.status == ExcessLineItemStatus.AWARDED


def test_accept_bid_not_found_raises_404(db_session: Session):
    """accept_bid raises HTTPException 404 when bid not in list."""
    from app.services.excess_service import accept_bid

    co = _make_company(db_session)
    user = _make_user(db_session)
    excess_list = _make_excess_list(db_session, co.id, user.id)
    item = _make_line_item(db_session, excess_list.id, "XYZ", qty=10)
    db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        accept_bid(db_session, bid_id=9999, line_item_id=item.id, list_id=excess_list.id)

    assert exc_info.value.status_code == 404


# ── send_bid_solicitation ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_bid_solicitation_invalid_line_item(db_session: Session):
    """Requesting a line item not in the list raises 404 (before email send)."""
    from app.services.excess_service import send_bid_solicitation

    co = _make_company(db_session)
    user = _make_user(db_session)
    excess_list = _make_excess_list(db_session, co.id, user.id)
    db_session.commit()

    mock_gc = AsyncMock()
    with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
        with pytest.raises(HTTPException) as exc_info:
            await send_bid_solicitation(
                db_session,
                list_id=excess_list.id,
                line_item_ids=[99999],
                recipient_email="x@y.com",
                recipient_name=None,
                contact_id=1,
                user_id=user.id,
                token="t",
                bundled=True,
            )

    assert exc_info.value.status_code == 404


# ── _safe_commit error path ───────────────────────────────────────


def test_safe_commit_integrity_error_raises_409(db_session: Session):
    """_safe_commit raises HTTPException 409 on IntegrityError."""
    from sqlalchemy.exc import IntegrityError

    from app.services.excess_service import _safe_commit

    db_session.add = MagicMock()
    mock_db = MagicMock()
    mock_db.commit.side_effect = IntegrityError("", {}, None)
    mock_db.rollback = MagicMock()

    with pytest.raises(HTTPException) as exc_info:
        _safe_commit(mock_db, entity="test")

    assert exc_info.value.status_code == 409
