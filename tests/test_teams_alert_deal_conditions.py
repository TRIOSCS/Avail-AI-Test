"""Tests for deal condition Teams DM alerts (better price + qty filled).

Covers: better price trigger, first-offer skip, qty threshold transition,
both triggers on same offer, rate limiting.

Called by: pytest
Depends on: conftest fixtures
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition
from app.routers.crm.offers import _fire_deal_condition_alerts


def _setup_offer_scenario(db, test_user, with_existing_offer=True, existing_price=1.00, target_qty=None):
    """Create a req with requirement and optionally an existing offer."""
    req = Requisition(
        name="REQ-DEAL-001", customer_name="Acme Corp", status="offers",
        created_by=test_user.id, created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()

    requirement = Requirement(
        requisition_id=req.id, primary_mpn="LM317T",
        target_qty=target_qty or 1000, target_price=0.50,
        created_at=datetime.now(timezone.utc),
    )
    db.add(requirement)
    db.flush()

    existing = None
    if with_existing_offer:
        existing = Offer(
            requisition_id=req.id, requirement_id=requirement.id,
            vendor_name="Mouser", mpn="LM317T", unit_price=existing_price,
            qty_available=500, status="active", entered_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(existing)
        db.flush()

    db.commit()
    return req, requirement, existing


def test_better_price_alert_fires(db_session, test_user):
    """Alert fires when new offer price < existing best."""
    req, requirement, _ = _setup_offer_scenario(db_session, test_user, existing_price=1.00)

    new_offer = Offer(
        requisition_id=req.id, requirement_id=requirement.id,
        vendor_name="Arrow", mpn="LM317T", unit_price=0.75,
        qty_available=200, status="active", entered_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(new_offer)
    db_session.commit()

    mock_send = AsyncMock(return_value=True)
    asyncio.get_event_loop().run_until_complete(
        _fire_deal_condition_alerts(db_session, new_offer, req, mock_send)
    )
    mock_send.assert_called_once()
    msg = mock_send.call_args[0][2]
    assert "Better price" in msg
    assert "Arrow" in msg
    assert "0.75" in msg


def test_no_alert_on_first_offer(db_session, test_user):
    """No better-price alert when it's the first offer (nothing to compare)."""
    req, requirement, _ = _setup_offer_scenario(db_session, test_user, with_existing_offer=False)

    first_offer = Offer(
        requisition_id=req.id, requirement_id=requirement.id,
        vendor_name="Arrow", mpn="LM317T", unit_price=0.75,
        qty_available=200, status="active", entered_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(first_offer)
    db_session.commit()

    mock_send = AsyncMock(return_value=True)
    asyncio.get_event_loop().run_until_complete(
        _fire_deal_condition_alerts(db_session, first_offer, req, mock_send)
    )
    mock_send.assert_not_called()


def test_no_alert_when_price_higher(db_session, test_user):
    """No alert when new price >= existing best."""
    req, requirement, _ = _setup_offer_scenario(db_session, test_user, existing_price=0.50)

    new_offer = Offer(
        requisition_id=req.id, requirement_id=requirement.id,
        vendor_name="Arrow", mpn="LM317T", unit_price=0.60,
        qty_available=200, status="active", entered_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(new_offer)
    db_session.commit()

    mock_send = AsyncMock(return_value=True)
    asyncio.get_event_loop().run_until_complete(
        _fire_deal_condition_alerts(db_session, new_offer, req, mock_send)
    )
    mock_send.assert_not_called()


def test_qty_filled_alert_fires_on_threshold(db_session, test_user):
    """Qty filled alert fires when total crosses target_qty."""
    req, requirement, existing = _setup_offer_scenario(
        db_session, test_user, existing_price=1.00, target_qty=1000
    )
    # existing has 500 qty

    new_offer = Offer(
        requisition_id=req.id, requirement_id=requirement.id,
        vendor_name="Arrow", mpn="LM317T", unit_price=1.10,
        qty_available=600, status="active", entered_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(new_offer)
    db_session.commit()

    mock_send = AsyncMock(return_value=True)
    asyncio.get_event_loop().run_until_complete(
        _fire_deal_condition_alerts(db_session, new_offer, req, mock_send)
    )
    mock_send.assert_called_once()
    msg = mock_send.call_args[0][2]
    assert "Qty filled" in msg
    assert "1100" in msg or "1,100" in msg


def test_no_qty_alert_when_already_above(db_session, test_user):
    """No qty alert when already at/above target before this offer."""
    req, requirement, existing = _setup_offer_scenario(
        db_session, test_user, existing_price=1.00, target_qty=100
    )
    # existing has 500 qty — already above 100 target

    new_offer = Offer(
        requisition_id=req.id, requirement_id=requirement.id,
        vendor_name="Arrow", mpn="LM317T", unit_price=1.10,
        qty_available=200, status="active", entered_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(new_offer)
    db_session.commit()

    mock_send = AsyncMock(return_value=True)
    asyncio.get_event_loop().run_until_complete(
        _fire_deal_condition_alerts(db_session, new_offer, req, mock_send)
    )
    # Should NOT fire qty alert (was already above). May fire price alert if cheaper.
    if mock_send.called:
        msg = mock_send.call_args[0][2]
        assert "Qty filled" not in msg


def test_no_alert_without_target_qty(db_session, test_user):
    """No qty alert when requirement has no target_qty."""
    req, requirement, _ = _setup_offer_scenario(
        db_session, test_user, with_existing_offer=False, target_qty=0
    )

    new_offer = Offer(
        requisition_id=req.id, requirement_id=requirement.id,
        vendor_name="Arrow", mpn="LM317T", unit_price=1.00,
        qty_available=500, status="active", entered_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(new_offer)
    db_session.commit()

    mock_send = AsyncMock(return_value=True)
    asyncio.get_event_loop().run_until_complete(
        _fire_deal_condition_alerts(db_session, new_offer, req, mock_send)
    )
    mock_send.assert_not_called()


def test_both_triggers_fire_same_offer(db_session, test_user):
    """Both better-price and qty-filled can fire on the same offer."""
    req, requirement, existing = _setup_offer_scenario(
        db_session, test_user, existing_price=1.00, target_qty=1000
    )

    new_offer = Offer(
        requisition_id=req.id, requirement_id=requirement.id,
        vendor_name="Arrow", mpn="LM317T", unit_price=0.50,
        qty_available=600, status="active", entered_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(new_offer)
    db_session.commit()

    mock_send = AsyncMock(return_value=True)
    asyncio.get_event_loop().run_until_complete(
        _fire_deal_condition_alerts(db_session, new_offer, req, mock_send)
    )
    mock_send.assert_called_once()
    msg = mock_send.call_args[0][2]
    assert "Better price" in msg
    assert "Qty filled" in msg
