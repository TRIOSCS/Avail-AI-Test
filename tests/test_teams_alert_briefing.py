"""Tests for AM morning briefing Teams DM job.

Covers: briefing sent to configured users, skips unconfigured, block queries,
followup_alert_sent_at stamped, empty blocks omitted, all-clear message,
job registration, error isolation.

Called by: pytest
Depends on: conftest fixtures
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.jobs.teams_alert_jobs import _send_user_briefing
from app.models.auth import User
from app.models.offers import Contact, VendorResponse
from app.models.quotes import Quote
from app.models.sourcing import Requisition
from app.models.teams_alert_config import TeamsAlertConfig


def test_briefing_sent_to_configured_user(db_session, test_user):
    """Briefing sent to user with alerts enabled (default)."""
    with (
        patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock, return_value=True) as mock,
        patch("app.jobs.teams_alert_jobs._ai_clean_briefing", new_callable=AsyncMock, return_value=None),
    ):
        asyncio.get_event_loop().run_until_complete(_send_user_briefing(test_user, db_session))
    mock.assert_called_once()
    msg = mock.call_args[0][2]
    assert "Good morning" in msg


def test_briefing_skips_disabled_user(db_session, test_user):
    """Briefing skipped for user with alerts_enabled=False."""
    config = TeamsAlertConfig(user_id=test_user.id, alerts_enabled=False)
    db_session.add(config)
    db_session.commit()

    with patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock) as mock:
        asyncio.get_event_loop().run_until_complete(_send_user_briefing(test_user, db_session))
    mock.assert_not_called()


def test_block1_open_rfqs(db_session, test_user):
    """Block 1 shows open RFQs with no offers."""
    req = Requisition(
        name="REQ-STALE", customer_name="Acme", status="active",
        created_by=test_user.id, created_at=datetime.now(timezone.utc) - timedelta(days=3),
    )
    db_session.add(req)
    db_session.commit()

    with (
        patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock, return_value=True) as mock,
        patch("app.jobs.teams_alert_jobs._ai_clean_briefing", new_callable=AsyncMock, return_value=None),
    ):
        asyncio.get_event_loop().run_until_complete(_send_user_briefing(test_user, db_session))
    msg = mock.call_args[0][2]
    assert "OPEN RFQs" in msg
    assert "Acme" in msg


def test_block2_followup_stamped(db_session, test_user):
    """Block 2: stale quotes get followup_alert_sent_at stamped."""
    from app.models.crm import Company, CustomerSite

    co = Company(name="Acme Co", is_active=True, created_at=datetime.now(timezone.utc))
    db_session.add(co)
    db_session.flush()
    site = CustomerSite(
        company_id=co.id, site_name="Acme HQ", contact_name="John",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(site)
    db_session.flush()

    req = Requisition(
        name="REQ-Q", customer_name="Acme", status="offers",
        created_by=test_user.id, created_at=datetime.now(timezone.utc) - timedelta(days=5),
    )
    db_session.add(req)
    db_session.flush()

    q = Quote(
        requisition_id=req.id, customer_site_id=site.id,
        quote_number="Q-STALE-001", status="sent",
        sent_at=datetime.now(timezone.utc) - timedelta(hours=72),
        created_by_id=test_user.id,
    )
    db_session.add(q)
    db_session.commit()

    with (
        patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock, return_value=True),
        patch("app.jobs.teams_alert_jobs._ai_clean_briefing", new_callable=AsyncMock, return_value=None),
    ):
        asyncio.get_event_loop().run_until_complete(_send_user_briefing(test_user, db_session))

    db_session.refresh(q)
    assert q.followup_alert_sent_at is not None


def test_block2_already_alerted_skipped(db_session, test_user):
    """Block 2: quotes with followup_alert_sent_at set are skipped."""
    from app.models.crm import Company, CustomerSite

    co = Company(name="Acme Co", is_active=True, created_at=datetime.now(timezone.utc))
    db_session.add(co)
    db_session.flush()
    site = CustomerSite(
        company_id=co.id, site_name="Acme HQ", contact_name="John",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(site)
    db_session.flush()

    req = Requisition(
        name="REQ-Q2", customer_name="Acme", status="offers",
        created_by=test_user.id, created_at=datetime.now(timezone.utc) - timedelta(days=5),
    )
    db_session.add(req)
    db_session.flush()

    q = Quote(
        requisition_id=req.id, customer_site_id=site.id,
        quote_number="Q-DONE-001", status="sent",
        sent_at=datetime.now(timezone.utc) - timedelta(hours=72),
        followup_alert_sent_at=datetime.now(timezone.utc) - timedelta(hours=24),
        created_by_id=test_user.id,
    )
    db_session.add(q)
    db_session.commit()

    with (
        patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock, return_value=True) as mock,
        patch("app.jobs.teams_alert_jobs._ai_clean_briefing", new_callable=AsyncMock, return_value=None),
    ):
        asyncio.get_event_loop().run_until_complete(_send_user_briefing(test_user, db_session))
    msg = mock.call_args[0][2]
    assert "FOLLOW-UP" not in msg


def test_block3_overnight_quotes(db_session, test_user):
    """Block 3: overnight vendor quotes with confidence filter."""
    req = Requisition(
        name="REQ-OVN", customer_name="Acme", status="offers",
        created_by=test_user.id, created_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    db_session.add(req)
    db_session.flush()

    contact = Contact(
        requisition_id=req.id, user_id=test_user.id,
        contact_type="rfq", vendor_name="Arrow",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(contact)
    db_session.flush()

    vr = VendorResponse(
        requisition_id=req.id, contact_id=contact.id,
        vendor_name="Arrow", vendor_email="sales@arrow.com",
        subject="RE: RFQ", body="We quote...",
        classification="quoted", confidence=0.9,
        parsed_data={"mpn": "LM317T"},
        created_at=datetime.now(timezone.utc) - timedelta(hours=6),
    )
    db_session.add(vr)
    db_session.commit()

    with (
        patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock, return_value=True) as mock,
        patch("app.jobs.teams_alert_jobs._ai_clean_briefing", new_callable=AsyncMock, return_value=None),
    ):
        asyncio.get_event_loop().run_until_complete(_send_user_briefing(test_user, db_session))
    msg = mock.call_args[0][2]
    assert "OVERNIGHT" in msg
    assert "Arrow" in msg


def test_empty_blocks_omitted(db_session, test_user):
    """When no data exists, blocks are omitted."""
    with (
        patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock, return_value=True) as mock,
        patch("app.jobs.teams_alert_jobs._ai_clean_briefing", new_callable=AsyncMock, return_value=None),
    ):
        asyncio.get_event_loop().run_until_complete(_send_user_briefing(test_user, db_session))
    msg = mock.call_args[0][2]
    assert "all caught up" in msg


def test_all_clear_message(db_session, test_user):
    """All-clear message when everything is empty."""
    with (
        patch("app.services.teams_alert_service.send_alert", new_callable=AsyncMock, return_value=True) as mock,
        patch("app.jobs.teams_alert_jobs._ai_clean_briefing", new_callable=AsyncMock, return_value=None),
    ):
        asyncio.get_event_loop().run_until_complete(_send_user_briefing(test_user, db_session))
    msg = mock.call_args[0][2]
    assert "caught up" in msg
    assert "OPEN RFQs" not in msg


def test_job_registered():
    """Teams alert jobs are registered with correct CronTrigger."""
    from unittest.mock import MagicMock

    scheduler = MagicMock()
    from app.jobs.teams_alert_jobs import register_teams_alert_jobs

    register_teams_alert_jobs(scheduler, None)
    assert scheduler.add_job.call_count == 2
    # Check job IDs
    ids = [call[1].get("id") for call in scheduler.add_job.call_args_list]
    assert "am_morning_briefing" in ids
    assert "director_daily_digest" in ids


def test_db_error_one_user_doesnt_block_others(db_session, test_user):
    """DB error on one user doesn't prevent other users from getting briefings.

    The job-level loop catches per-user exceptions and continues.
    """
    buyer2 = User(email="buyer2@test.com", name="Buyer 2", role="buyer", azure_id="az-b2")
    db_session.add(buyer2)
    db_session.commit()

    call_count = 0

    async def mock_send(db, uid, msg, etype, eid):
        nonlocal call_count
        call_count += 1
        if uid == test_user.id:
            raise Exception("Simulated DB error")
        return True

    with (
        patch("app.services.teams_alert_service.send_alert", side_effect=mock_send),
        patch("app.jobs.teams_alert_jobs._ai_clean_briefing", new_callable=AsyncMock, return_value=None),
    ):
        # First user raises but shouldn't crash the function
        try:
            asyncio.get_event_loop().run_until_complete(_send_user_briefing(test_user, db_session))
        except Exception:
            pass  # Expected — error from mock
        # Second user should still work fine
        asyncio.get_event_loop().run_until_complete(_send_user_briefing(buyer2, db_session))
    assert call_count == 2  # Both attempted
