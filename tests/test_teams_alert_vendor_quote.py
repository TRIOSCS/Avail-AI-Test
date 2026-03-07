"""Tests for vendor quote Teams DM alerts.

Covers: alert fires on high-confidence quoted VRs, skips low confidence,
skips non-quoted, dedup via teams_alert_sent_at, batching per buyer.

Called by: pytest
Depends on: conftest fixtures
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.email_service import _fire_vendor_quote_alerts
from app.models.offers import Contact, VendorResponse
from app.models.sourcing import Requisition


def _make_vr(db, req_id, contact_id, classification="quoted", confidence=0.9, parsed_data=None, alert_sent=None):
    """Helper: create a VendorResponse."""
    vr = VendorResponse(
        requisition_id=req_id,
        contact_id=contact_id,
        vendor_name="Arrow Electronics",
        vendor_email="sales@arrow.com",
        subject="RE: RFQ LM317T",
        body="We can offer...",
        classification=classification,
        confidence=confidence,
        parsed_data=parsed_data or {"mpn": "LM317T", "unit_price": 0.45},
        status="parsed",
        teams_alert_sent_at=alert_sent,
        created_at=datetime.now(timezone.utc),
    )
    db.add(vr)
    db.flush()
    return vr


def test_alert_fires_on_quoted_high_confidence(db_session, test_user, test_requisition):
    """Alert fires when classification=quoted and confidence >= 0.8."""
    contact = Contact(
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        contact_type="rfq",
        vendor_name="Arrow",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(contact)
    db_session.flush()

    vr = _make_vr(db_session, test_requisition.id, contact.id)
    db_session.commit()

    with patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock, return_value=True) as mock:
        asyncio.get_event_loop().run_until_complete(_fire_vendor_quote_alerts([vr], db_session))
    mock.assert_called_once()
    assert vr.teams_alert_sent_at is not None


def test_skipped_on_low_confidence(db_session, test_user, test_requisition):
    """No alert when confidence < 0.8."""
    contact = Contact(
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        contact_type="rfq",
        vendor_name="Arrow",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(contact)
    db_session.flush()

    vr = _make_vr(db_session, test_requisition.id, contact.id, confidence=0.6)
    db_session.commit()

    with patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock) as mock:
        asyncio.get_event_loop().run_until_complete(_fire_vendor_quote_alerts([vr], db_session))
    mock.assert_not_called()


def test_skipped_on_non_quoted(db_session, test_user, test_requisition):
    """No alert when classification != 'quoted'."""
    contact = Contact(
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        contact_type="rfq",
        vendor_name="Arrow",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(contact)
    db_session.flush()

    vr = _make_vr(db_session, test_requisition.id, contact.id, classification="ooo")
    db_session.commit()

    with patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock) as mock:
        asyncio.get_event_loop().run_until_complete(_fire_vendor_quote_alerts([vr], db_session))
    mock.assert_not_called()


def test_dedup_prevents_double_send(db_session, test_user, test_requisition):
    """No alert when teams_alert_sent_at is already set."""
    contact = Contact(
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        contact_type="rfq",
        vendor_name="Arrow",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(contact)
    db_session.flush()

    vr = _make_vr(db_session, test_requisition.id, contact.id, alert_sent=datetime.now(timezone.utc))
    db_session.commit()

    with patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock) as mock:
        asyncio.get_event_loop().run_until_complete(_fire_vendor_quote_alerts([vr], db_session))
    mock.assert_not_called()


def test_skipped_when_no_contact_user(db_session, test_requisition):
    """No alert when VR has no contact_id."""
    vr = _make_vr(db_session, test_requisition.id, None)
    db_session.commit()

    with patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock) as mock:
        asyncio.get_event_loop().run_until_complete(_fire_vendor_quote_alerts([vr], db_session))
    mock.assert_not_called()


def test_message_contains_vendor_and_mpn(db_session, test_user, test_requisition):
    """Message includes vendor name and MPN."""
    contact = Contact(
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        contact_type="rfq",
        vendor_name="Arrow",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(contact)
    db_session.flush()

    vr = _make_vr(db_session, test_requisition.id, contact.id)
    db_session.commit()

    with patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock, return_value=True) as mock:
        asyncio.get_event_loop().run_until_complete(_fire_vendor_quote_alerts([vr], db_session))
    msg = mock.call_args[0][2]  # third positional arg = message
    assert "Arrow" in msg
    assert "LM317T" in msg


def test_batched_alert_for_multiple_quotes(db_session, test_user, test_requisition):
    """Multiple quotes for same buyer get consolidated into one message."""
    contact = Contact(
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        contact_type="rfq",
        vendor_name="Arrow",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(contact)
    db_session.flush()

    vr1 = _make_vr(db_session, test_requisition.id, contact.id)
    vr2 = _make_vr(db_session, test_requisition.id, contact.id)
    vr2.vendor_name = "Mouser"
    db_session.commit()

    with patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock, return_value=True) as mock:
        asyncio.get_event_loop().run_until_complete(_fire_vendor_quote_alerts([vr1, vr2], db_session))
    # Should be called once (batched)
    assert mock.call_count == 1
    msg = mock.call_args[0][2]
    assert "2 new quotes" in msg
