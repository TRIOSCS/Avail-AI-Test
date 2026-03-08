"""Tests for new requisition Teams DM alerts to buyers.

Covers: all buyers alerted, creator skipped, disabled users skipped,
message content, graceful skip when no buyers.

Called by: pytest
Depends on: conftest fixtures
"""

import asyncio
from unittest.mock import AsyncMock, patch

from app.models.auth import User
from app.models.teams_alert_config import TeamsAlertConfig
from app.services.teams_alert_service import send_alert_to_role


def test_all_buyers_receive_alert(db_session, test_user):
    """All active buyers with alerts_enabled receive the alert."""
    buyer2 = User(email="buyer2@test.com", name="Buyer 2", role="buyer", azure_id="az-b2")
    db_session.add(buyer2)
    db_session.commit()

    with patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock, return_value=True) as mock:
        count = asyncio.get_event_loop().run_until_complete(
            send_alert_to_role(db_session, "buyer", "New RFQ: Acme — REQ-001", "new_requisition", "1")
        )
    assert count == 2
    assert mock.call_count == 2


def test_creator_not_self_alerted(db_session, test_user):
    """Creator (if buyer) does not receive self-alert."""
    with patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock, return_value=True) as mock:
        count = asyncio.get_event_loop().run_until_complete(
            send_alert_to_role(
                db_session, "buyer", "New RFQ: Acme — REQ-001", "new_requisition", "1", exclude_user_id=test_user.id
            )
        )
    assert count == 0
    assert mock.call_count == 0


def test_disabled_users_skipped(db_session, test_user):
    """Users with alerts_enabled=False are skipped."""
    config = TeamsAlertConfig(user_id=test_user.id, alerts_enabled=False)
    db_session.add(config)
    db_session.commit()

    with patch("app.services.teams_alert_service._try_graph_dm", new_callable=AsyncMock, return_value=False):
        count = asyncio.get_event_loop().run_until_complete(
            send_alert_to_role(db_session, "buyer", "New RFQ", "new_requisition", "1")
        )
    assert count == 0


def test_message_contains_customer_and_name(db_session, test_user):
    """Message includes customer name and req name."""
    with patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock, return_value=True) as mock:
        asyncio.get_event_loop().run_until_complete(
            send_alert_to_role(db_session, "buyer", "New RFQ: Acme Corp — REQ-042", "new_requisition", "42")
        )
    msg = mock.call_args[0][2]
    assert "Acme Corp" in msg
    assert "REQ-042" in msg


def test_graceful_skip_no_buyers(db_session):
    """Graceful skip when no buyers have alerts configured."""
    # No buyer users exist at all (test_user fixture not loaded)
    with patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock) as mock:
        count = asyncio.get_event_loop().run_until_complete(
            send_alert_to_role(db_session, "buyer", "New RFQ", "new_requisition", "1")
        )
    assert count == 0
    assert mock.call_count == 0
