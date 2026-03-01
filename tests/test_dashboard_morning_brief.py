"""Tests for GET /api/dashboard/morning-brief endpoint."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.models import (
    ActivityLog,
    Company,
    CustomerSite,
    Offer,
    ProactiveMatch,
    Quote,
    Requirement,
    Requisition,
    User,
)


class TestMorningBrief:
    """4 test cases covering the morning-brief endpoint."""

    def _make_company(self, db: Session, owner: User, name="Test Corp"):
        c = Company(
            name=name,
            is_active=True,
            account_owner_id=owner.id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(c)
        db.flush()
        # Create a site with owner_id so site-level ownership queries find it
        s = CustomerSite(
            company_id=c.id,
            site_name="HQ",
            owner_id=owner.id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(s)
        db.flush()
        return c

    def _make_site(self, db: Session, company: Company, name="HQ", owner_id=None):
        s = CustomerSite(
            company_id=company.id,
            site_name=name,
            owner_id=owner_id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(s)
        db.flush()
        return s

    # 1. Returns stats correctly with mocked Claude
    @patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
    def test_returns_stats_with_mocked_claude(self, mock_claude, client, db_session, test_user):
        mock_claude.return_value = {"text": "Good morning! You have work to do."}

        # Create a company (will be stale — no recent activity)
        self._make_company(db_session, test_user)
        db_session.commit()

        resp = client.get("/api/dashboard/morning-brief")
        assert resp.status_code == 200
        data = resp.json()

        assert data["text"] == "Good morning! You have work to do."
        assert "stats" in data
        assert data["stats"]["stale_accounts"] == 1
        assert data["stats"]["quotes_awaiting"] == 0
        assert data["stats"]["new_proactive_matches"] == 0
        assert data["stats"]["won_this_week"] == 0
        assert data["stats"]["lost_this_week"] == 0
        assert "generated_at" in data

    # 2. Claude failure → stats returned, text is null
    @patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
    def test_claude_failure_returns_null_text(self, mock_claude, client, db_session, test_user):
        mock_claude.side_effect = Exception("API timeout")

        self._make_company(db_session, test_user)
        db_session.commit()

        resp = client.get("/api/dashboard/morning-brief")
        assert resp.status_code == 200
        data = resp.json()

        assert data["text"] is None
        assert data["stats"]["stale_accounts"] == 1

    # 3. Stats computation: stale_accounts, quotes, proactive matches
    @patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
    def test_stats_computation_accuracy(self, mock_claude, client, db_session, test_user):
        mock_claude.return_value = {"text": "Brief."}

        # 2 companies: 1 stale, 1 recently contacted
        c1 = self._make_company(db_session, test_user, name="Stale Corp")
        c2 = self._make_company(db_session, test_user, name="Active Corp")
        # Recent activity on c2 only
        a = ActivityLog(
            user_id=test_user.id,
            company_id=c2.id,
            activity_type="email_sent",
            channel="email",
            created_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        db_session.add(a)

        # Create site + quote awaiting response
        site = self._make_site(db_session, c1)
        req = Requisition(
            name="REQ-BRIEF",
            customer_site_id=site.id,
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        q = Quote(
            requisition_id=req.id,
            customer_site_id=site.id,
            quote_number="Q-BRIEF-001",
            status="sent",
            subtotal=5000,
            sent_at=datetime.now(timezone.utc) - timedelta(days=3),
            created_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(q)

        # Won quote this week
        q2 = Quote(
            requisition_id=req.id,
            customer_site_id=site.id,
            quote_number="Q-BRIEF-002",
            status="sent",
            result="won",
            result_at=datetime.now(timezone.utc) - timedelta(days=1),
            subtotal=3000,
            sent_at=datetime.now(timezone.utc) - timedelta(days=5),
            created_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(q2)

        # Proactive match
        # Need an offer and requirement for the proactive match
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="STM32F407",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.flush()

        offer = Offer(
            requisition_id=req.id,
            requirement_id=item.id,
            vendor_name="Arrow",
            mpn="STM32F407",
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.flush()

        pm = ProactiveMatch(
            offer_id=offer.id,
            requirement_id=item.id,
            requisition_id=req.id,
            customer_site_id=site.id,
            salesperson_id=test_user.id,
            mpn="STM32F407",
            status="new",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(pm)
        db_session.commit()

        resp = client.get("/api/dashboard/morning-brief")
        data = resp.json()
        stats = data["stats"]

        assert stats["stale_accounts"] == 1  # c1 has no activity
        assert stats["quotes_awaiting"] == 1  # q is sent with no result
        assert stats["new_proactive_matches"] == 1
        assert stats["won_this_week"] == 1
        assert stats["lost_this_week"] == 0

    # 4. Empty portfolio — no companies owned
    @patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
    def test_empty_portfolio(self, mock_claude, client, db_session, test_user):
        mock_claude.return_value = {"text": "No accounts yet."}

        resp = client.get("/api/dashboard/morning-brief")
        assert resp.status_code == 200
        data = resp.json()
        stats = data["stats"]

        assert stats["stale_accounts"] == 0
        assert stats["quotes_awaiting"] == 0
        assert stats["new_proactive_matches"] == 0
        assert stats["won_this_week"] == 0
        assert stats["lost_this_week"] == 0
