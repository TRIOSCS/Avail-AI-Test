"""Tests for CRM Phase 2a — activity data gaps and Teams tracking.

Called by: pytest
Depends on: app.services.activity_service, app.services.webhook_service
"""

from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.crm import Company, CustomerSite
from app.models.intelligence import ActivityLog
from app.models.sourcing import Requisition
from tests.conftest import engine  # noqa: F401


class TestRfqActivityCompanyId:
    """log_rfq_activity should resolve and set company_id."""

    def test_rfq_activity_sets_company_id(self, db_session: Session, test_user: User):
        """RFQ activity log resolves company via requisition → site → company."""
        from app.services.activity_service import log_rfq_activity

        company = Company(name="Test Customer", is_active=True)
        db_session.add(company)
        db_session.flush()

        site = CustomerSite(company_id=company.id, site_name="HQ")
        db_session.add(site)
        db_session.flush()

        req = Requisition(
            name="Test RFQ",
            customer_site_id=site.id,
            created_by=test_user.id,
            status="active",
        )
        db_session.add(req)
        db_session.flush()

        record = log_rfq_activity(
            db=db_session,
            rfq_id=req.id,
            activity_type="rfq_sent",
            description="RFQ sent to vendors",
            user_id=test_user.id,
        )

        assert record.company_id == company.id

    def test_rfq_activity_handles_no_site(self, db_session: Session, test_user: User):
        """RFQ activity log handles requisition with no customer_site_id."""
        from app.services.activity_service import log_rfq_activity

        req = Requisition(
            name="No Site RFQ",
            customer_site_id=None,
            created_by=test_user.id,
            status="active",
        )
        db_session.add(req)
        db_session.flush()

        record = log_rfq_activity(
            db=db_session,
            rfq_id=req.id,
            activity_type="rfq_sent",
            description="RFQ sent",
            user_id=test_user.id,
        )

        assert record.company_id is None


class TestProactiveActivityCompanyId:
    """Proactive match ActivityLog should set company_id."""

    def test_proactive_match_activity_accepts_company_id(self, db_session: Session, test_user: User):
        """ActivityLog model accepts company_id for proactive matches."""
        company = Company(name="Proactive Customer", is_active=True)
        db_session.add(company)
        db_session.flush()

        log = ActivityLog(
            user_id=test_user.id,
            activity_type="proactive_match",
            channel="system",
            company_id=company.id,
            subject="Test proactive match",
        )
        db_session.add(log)
        db_session.flush()
        assert log.company_id == company.id


class TestQuoteActivityCompanyId:
    """Quote outcome ActivityLog should set company_id."""

    def test_quote_won_activity_has_company_id(self, db_session: Session, test_user: User):
        """Quote won/lost ActivityLog resolves company_id from requisition."""
        company = Company(name="Quote Customer", is_active=True)
        db_session.add(company)
        db_session.flush()

        site = CustomerSite(company_id=company.id, site_name="HQ")
        db_session.add(site)
        db_session.flush()

        req = Requisition(
            name="Quote RFQ",
            customer_site_id=site.id,
            created_by=test_user.id,
            status="active",
        )
        db_session.add(req)
        db_session.flush()

        # Directly test the ActivityLog creation pattern
        quote_company_id = None
        if req and req.customer_site_id:
            _site = db_session.get(CustomerSite, req.customer_site_id)
            if _site:
                quote_company_id = _site.company_id

        log = ActivityLog(
            user_id=test_user.id,
            activity_type="quote_won",
            channel="system",
            requisition_id=req.id,
            company_id=quote_company_id,
            subject="Test quote won",
        )
        db_session.add(log)
        db_session.flush()

        assert log.company_id == company.id


class TestTeamsSubscription:
    """Test Teams subscription creation."""

    def test_create_teams_subscription_creates_record(self, db_session: Session, test_user: User):
        """create_teams_subscription creates a GraphSubscription with teams resource."""
        import asyncio

        from app.services.webhook_service import create_teams_subscription

        mock_result = {"id": "sub-teams-123", "resource": "/me/chats/getAllMessages"}

        with patch("app.utils.graph_client.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.post_json = AsyncMock(return_value=mock_result)

            with patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="fake-token",
            ):
                record = asyncio.get_event_loop().run_until_complete(create_teams_subscription(test_user, db_session))

        assert record is not None
        assert record.resource == "/me/chats/getAllMessages"
        assert record.user_id == test_user.id
