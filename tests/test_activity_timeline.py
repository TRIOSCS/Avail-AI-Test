"""Tests for Unified Activity Timeline (migration 058).

Tests timeline query functions, direction/event_type population,
and the new /api/activity/account and /api/activity/contact endpoints.

Depends on: tests/conftest.py fixtures (test_user, test_company, test_customer_site, client)
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import ActivityLog, Company, CustomerSite, SiteContact, User
from app.services.activity_service import (
    get_account_timeline,
    get_contact_timeline,
    get_last_outbound_activity,
    log_call_activity,
    log_email_activity,
)
from tests.conftest import engine  # noqa: F401

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def site_contact(db_session: Session, test_customer_site: CustomerSite) -> SiteContact:
    """A site contact for timeline tests."""
    sc = SiteContact(
        customer_site_id=test_customer_site.id,
        full_name="Timeline Contact",
        email="timeline@acme.com",
    )
    db_session.add(sc)
    db_session.commit()
    db_session.refresh(sc)
    return sc


@pytest.fixture()
def company_activities(db_session: Session, test_user: User, test_company: Company) -> list[ActivityLog]:
    """Create a mix of activities for a company."""
    now = datetime.now(timezone.utc)
    activities = []
    for i, (atype, chan, dirn, etype) in enumerate(
        [
            ("email_sent", "email", "outbound", "email"),
            ("email_received", "email", "inbound", "email"),
            ("call_logged", "phone", "outbound", "call"),
            ("call_logged", "phone", "inbound", "call"),
            ("note", "manual", None, "note"),
        ]
    ):
        a = ActivityLog(
            user_id=test_user.id,
            activity_type=atype,
            channel=chan,
            company_id=test_company.id,
            direction=dirn,
            event_type=etype,
            summary=f"Test activity {i}",
            contact_name=f"Contact {i}",
            created_at=now - timedelta(hours=i),
        )
        db_session.add(a)
        activities.append(a)
    db_session.commit()
    for a in activities:
        db_session.refresh(a)
    return activities


@pytest.fixture()
def contact_activities(
    db_session: Session,
    test_user: User,
    test_company: Company,
    test_customer_site: CustomerSite,
    site_contact: SiteContact,
) -> list[ActivityLog]:
    """Activities linked to a specific site contact."""
    now = datetime.now(timezone.utc)
    activities = []
    for i, (atype, chan) in enumerate(
        [
            ("email_sent", "email"),
            ("call_logged", "phone"),
        ]
    ):
        a = ActivityLog(
            user_id=test_user.id,
            activity_type=atype,
            channel=chan,
            company_id=test_company.id,
            customer_site_id=test_customer_site.id,
            site_contact_id=site_contact.id,
            direction="outbound",
            event_type="email" if chan == "email" else "call",
            summary=f"Contact activity {i}",
            created_at=now - timedelta(hours=i),
        )
        db_session.add(a)
        activities.append(a)
    db_session.commit()
    for a in activities:
        db_session.refresh(a)
    return activities


# ═══════════════════════════════════════════════════════════════════════
#  TestGetAccountTimeline
# ═══════════════════════════════════════════════════════════════════════


class TestGetAccountTimeline:
    def test_returns_activities_for_company(self, db_session, test_company, company_activities):
        items, total = get_account_timeline(db_session, test_company.id)
        assert total == 5
        assert len(items) == 5

    def test_empty_when_no_activities(self, db_session, test_company):
        items, total = get_account_timeline(db_session, test_company.id)
        assert total == 0
        assert items == []

    def test_filters_by_channel(self, db_session, test_company, company_activities):
        items, total = get_account_timeline(db_session, test_company.id, channel=["email"])
        assert total == 2
        assert all(a.channel == "email" for a in items)

    def test_filters_by_direction(self, db_session, test_company, company_activities):
        items, total = get_account_timeline(db_session, test_company.id, direction="outbound")
        assert total == 2
        assert all(a.direction == "outbound" for a in items)

    def test_filters_by_date_range(self, db_session, test_company, company_activities):
        now = datetime.now(timezone.utc)
        items, total = get_account_timeline(
            db_session,
            test_company.id,
            date_from=now - timedelta(hours=1, minutes=30),
            date_to=now + timedelta(hours=1),
        )
        # Activities at offsets 0h and 1h ago should match (not 2h+)
        assert total == 2

    def test_pagination(self, db_session, test_company, company_activities):
        items, total = get_account_timeline(db_session, test_company.id, limit=2, offset=0)
        assert total == 5
        assert len(items) == 2

        items2, total2 = get_account_timeline(db_session, test_company.id, limit=2, offset=2)
        assert total2 == 5
        assert len(items2) == 2
        assert items[0].id != items2[0].id


# ═══════════════════════════════════════════════════════════════════════
#  TestGetContactTimeline
# ═══════════════════════════════════════════════════════════════════════


class TestGetContactTimeline:
    def test_returns_activities_for_contact(self, db_session, site_contact, contact_activities):
        items, total = get_contact_timeline(db_session, site_contact.id)
        assert total == 2
        assert len(items) == 2

    def test_empty_when_no_activities(self, db_session, site_contact):
        items, total = get_contact_timeline(db_session, site_contact.id)
        assert total == 0
        assert items == []

    def test_filters_by_channel(self, db_session, site_contact, contact_activities):
        items, total = get_contact_timeline(db_session, site_contact.id, channel=["email"])
        assert total == 1
        assert items[0].channel == "email"


# ═══════════════════════════════════════════════════════════════════════
#  TestGetLastOutboundActivity
# ═══════════════════════════════════════════════════════════════════════


class TestGetLastOutboundActivity:
    def test_returns_most_recent_outbound(self, db_session, test_company, company_activities):
        result = get_last_outbound_activity(db_session, test_company.id)
        assert result is not None
        assert result.direction == "outbound"

    def test_returns_none_when_no_outbound(self, db_session, test_user, test_company):
        # Only inbound activity
        a = ActivityLog(
            user_id=test_user.id,
            activity_type="email_received",
            channel="email",
            company_id=test_company.id,
            direction="inbound",
            event_type="email",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(a)
        db_session.commit()

        result = get_last_outbound_activity(db_session, test_company.id)
        assert result is None

    def test_canonical_call_logged_matches_via_direction(self, db_session, test_user, test_company):
        """A canonical call_logged row is matched as outbound purely by the direction
        column."""
        a = ActivityLog(
            user_id=test_user.id,
            activity_type="call_logged",
            channel="phone",
            company_id=test_company.id,
            direction="outbound",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(a)
        db_session.commit()

        result = get_last_outbound_activity(db_session, test_company.id)
        assert result is not None
        assert result.activity_type == "call_logged"
        assert result.direction == "outbound"

    def test_call_logged_inbound_is_not_outbound(self, db_session, test_user, test_company):
        """An inbound call_logged row must not be returned as outbound activity."""
        a = ActivityLog(
            user_id=test_user.id,
            activity_type="call_logged",
            channel="phone",
            company_id=test_company.id,
            direction="inbound",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(a)
        db_session.commit()

        result = get_last_outbound_activity(db_session, test_company.id)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
#  TestDirectionEventTypePopulation
# ═══════════════════════════════════════════════════════════════════════


class TestDirectionEventTypePopulation:
    def test_log_email_sets_direction_and_event_type(self, db_session, test_user):
        record = log_email_activity(
            user_id=test_user.id,
            direction="sent",
            email_addr="nobody@example.com",
            subject="Test",
            external_id="test-ext-001",
            contact_name="Nobody",
            db=db_session,
        )
        assert record is not None
        assert record.direction == "outbound"
        assert record.event_type == "email"

    def test_log_call_sets_direction_and_event_type(self, db_session, test_user):
        record = log_call_activity(
            user_id=test_user.id,
            direction="inbound",
            phone="+15551234567",
            duration_seconds=60,
            external_id="test-call-001",
            contact_name="Caller",
            db=db_session,
        )
        assert record is not None
        assert record.direction == "inbound"
        assert record.event_type == "call"

    def test_summary_is_populated(self, db_session, test_user):
        record = log_email_activity(
            user_id=test_user.id,
            direction="sent",
            email_addr="test@example.com",
            subject="Hello",
            external_id="test-ext-002",
            contact_name="Bob",
            db=db_session,
        )
        assert record is not None
        assert record.summary == "Email to Bob"


# ═══════════════════════════════════════════════════════════════════════
#  TestTimelineEndpoints
# ═══════════════════════════════════════════════════════════════════════


class TestTimelineEndpoints:
    def test_account_timeline_returns_200(self, client, test_company, company_activities):
        resp = client.get(f"/api/activity/account/{test_company.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data
        assert data["total"] == 5

    def test_account_timeline_404_for_unknown(self, client):
        resp = client.get("/api/activity/account/999999")
        assert resp.status_code == 404

    def test_contact_timeline_returns_200(self, client, site_contact, contact_activities):
        resp = client.get(f"/api/activity/contact/{site_contact.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert data["total"] == 2


# ═══════════════════════════════════════════════════════════════════════
#  TestRequisitionActivityTabTimeline
#  Plan 6 / Task 2 — unified date-grouped chronological timeline.
# ═══════════════════════════════════════════════════════════════════════


class TestRequisitionActivityTabTimeline:
    def test_renders_unified_timeline(self, client, db_session, test_requisition, test_user):
        """The requisition Activity tab renders one date-grouped chronological timeline
        of ActivityLog rows — no legacy 'RFQ History' section."""
        now = datetime.now(timezone.utc)
        db_session.add(
            ActivityLog(
                user_id=test_user.id,
                activity_type="rfq_sent",
                channel="email",
                requisition_id=test_requisition.id,
                summary="RFQ emailed to Acme Components",
                occurred_at=now,
            )
        )
        db_session.add(
            ActivityLog(
                user_id=test_user.id,
                activity_type="offer_created",
                channel="system",
                requisition_id=test_requisition.id,
                notes="Offer created from vendor reply",
                occurred_at=now - timedelta(minutes=5),
            )
        )
        db_session.commit()

        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/activity")
        assert resp.status_code == 200
        body = resp.text

        # Both events render.
        assert "RFQ emailed to Acme Components" in body
        assert "Offer created from vendor reply" in body
        # A date-group header is present.
        assert "Today" in body
        # The legacy two-section layout is gone.
        assert "RFQ History" not in body
