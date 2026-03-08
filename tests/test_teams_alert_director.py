"""Tests for director daily digest Teams DM job.

Covers: digest sent to managers, skips non-managers, block queries,
empty digest, AI cleanup, error isolation.

Called by: pytest
Depends on: conftest fixtures
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.jobs.teams_alert_jobs import _send_director_digest
from app.models.auth import User
from app.models.crm import Company
from app.models.offers import Contact
from app.models.sourcing import Requisition
from app.models.teams_alert_config import TeamsAlertConfig


@pytest.fixture()
def manager_user(db_session):
    """A manager-role user."""
    user = User(
        email="mgr@trioscs.com",
        name="Test Manager",
        role="manager",
        azure_id="az-mgr-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_digest_sent_to_manager(db_session, manager_user):
    """Digest sent to active manager."""
    with (
        patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock, return_value=True) as mock,
        patch("app.jobs.teams_alert_jobs._ai_clean_briefing", new_callable=AsyncMock, return_value=None),
    ):
        asyncio.get_event_loop().run_until_complete(_send_director_digest(manager_user, db_session))
    mock.assert_called_once()
    msg = mock.call_args[0][2]
    assert "AVAIL Brief" in msg


def test_digest_skips_disabled_manager(db_session, manager_user):
    """Digest skipped for manager with alerts_enabled=False."""
    config = TeamsAlertConfig(user_id=manager_user.id, alerts_enabled=False)
    db_session.add(config)
    db_session.commit()

    with patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock) as mock:
        asyncio.get_event_loop().run_until_complete(_send_director_digest(manager_user, db_session))
    mock.assert_not_called()


def test_block1_idle_deals(db_session, manager_user, test_user):
    """Block 1 shows idle deals with offers status."""
    req = Requisition(
        name="REQ-IDLE",
        customer_name="BigCorp",
        status="offers",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc) - timedelta(days=5),
        updated_at=datetime.now(timezone.utc) - timedelta(days=3),
    )
    db_session.add(req)
    db_session.commit()

    with (
        patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock, return_value=True) as mock,
        patch("app.jobs.teams_alert_jobs._ai_clean_briefing", new_callable=AsyncMock, return_value=None),
    ):
        asyncio.get_event_loop().run_until_complete(_send_director_digest(manager_user, db_session))
    msg = mock.call_args[0][2]
    assert "DEALS NEEDING ATTENTION" in msg
    assert "REQ-IDLE" in msg


def test_block2_response_times(db_session, manager_user, test_user, test_requisition):
    """Block 2 shows per-AM response time averages."""
    contact = Contact(
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        contact_type="rfq",
        vendor_name="Arrow",
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    db_session.add(contact)
    db_session.commit()

    with (
        patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock, return_value=True) as mock,
        patch("app.jobs.teams_alert_jobs._ai_clean_briefing", new_callable=AsyncMock, return_value=None),
    ):
        asyncio.get_event_loop().run_until_complete(_send_director_digest(manager_user, db_session))
    msg = mock.call_args[0][2]
    assert "RESPONSE TIMES" in msg


def test_block3_workload_snapshot(db_session, manager_user, test_user):
    """Block 3 shows per-AM workload (open reqs, quotes, offers)."""
    req = Requisition(
        name="REQ-WORK",
        customer_name="TestCo",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()

    with (
        patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock, return_value=True) as mock,
        patch("app.jobs.teams_alert_jobs._ai_clean_briefing", new_callable=AsyncMock, return_value=None),
    ):
        asyncio.get_event_loop().run_until_complete(_send_director_digest(manager_user, db_session))
    msg = mock.call_args[0][2]
    assert "WORKLOAD" in msg


def test_block4_stale_accounts(db_session, manager_user, test_user):
    """Block 4 shows accounts with no recent contact."""
    co = Company(
        name="Dormant Inc",
        account_owner_id=test_user.id,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()

    with (
        patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock, return_value=True) as mock,
        patch("app.jobs.teams_alert_jobs._ai_clean_briefing", new_callable=AsyncMock, return_value=None),
    ):
        asyncio.get_event_loop().run_until_complete(_send_director_digest(manager_user, db_session))
    msg = mock.call_args[0][2]
    assert "STALE ACCOUNTS" in msg
    assert "Dormant Inc" in msg


def test_empty_digest_all_clear(db_session, manager_user):
    """All-clear message when nothing to report."""
    with (
        patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock, return_value=True) as mock,
        patch("app.jobs.teams_alert_jobs._ai_clean_briefing", new_callable=AsyncMock, return_value=None),
    ):
        asyncio.get_event_loop().run_until_complete(_send_director_digest(manager_user, db_session))
    msg = mock.call_args[0][2]
    assert "all clear" in msg


def test_ai_cleanup_used_when_available(db_session, manager_user, test_user):
    """AI cleanup is called to rewrite the raw briefing."""
    req = Requisition(
        name="REQ-AI",
        customer_name="TestCo",
        status="offers",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc) - timedelta(days=5),
        updated_at=datetime.now(timezone.utc) - timedelta(days=3),
    )
    db_session.add(req)
    db_session.commit()

    with (
        patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock, return_value=True),
        patch(
            "app.jobs.teams_alert_jobs._ai_clean_briefing", new_callable=AsyncMock, return_value="AI cleaned message"
        ) as ai_mock,
    ):
        asyncio.get_event_loop().run_until_complete(_send_director_digest(manager_user, db_session))
    ai_mock.assert_called_once()


def test_digest_event_type(db_session, manager_user):
    """Digest uses 'director_digest' event type."""
    with (
        patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock, return_value=True) as mock,
        patch("app.jobs.teams_alert_jobs._ai_clean_briefing", new_callable=AsyncMock, return_value=None),
    ):
        asyncio.get_event_loop().run_until_complete(_send_director_digest(manager_user, db_session))
    # Check event_type argument
    assert mock.call_args[0][3] == "director_digest"
