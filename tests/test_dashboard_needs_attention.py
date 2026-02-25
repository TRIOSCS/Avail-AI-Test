"""Tests for GET /api/dashboard/needs-attention and /hot-offers endpoints."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import (
    ActivityLog,
    Company,
    CustomerSite,
    Offer,
    Quote,
    Requisition,
    User,
)


class TestNeedsAttention:
    """10 test cases covering the needs-attention endpoint."""

    def _make_company(self, db, owner, name="Acme Corp", **kw):
        c = Company(
            name=name,
            is_active=kw.pop("is_active", True),
            is_strategic=kw.pop("is_strategic", False),
            account_owner_id=owner.id,
            created_at=datetime.now(timezone.utc),
            **kw,
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

    def _make_site(self, db, company, name="HQ", owner_id=None):
        s = CustomerSite(
            company_id=company.id,
            site_name=name,
            owner_id=owner_id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(s)
        db.flush()
        return s

    def _make_activity(self, db, user, company, days_ago=0, activity_type="email_sent"):
        a = ActivityLog(
            user_id=user.id,
            company_id=company.id,
            activity_type=activity_type,
            channel="email" if "email" in activity_type else "phone",
            created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        )
        db.add(a)
        db.flush()
        return a

    # 1. No companies → empty list
    def test_no_companies_returns_empty(self, client):
        resp = client.get("/api/dashboard/needs-attention")
        assert resp.status_code == 200
        assert resp.json() == []

    # 2. User with 1 stale company → returned
    def test_stale_company_returned(self, client, db_session, test_user):
        c = self._make_company(db_session, test_user)
        self._make_activity(db_session, test_user, c, days_ago=10)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention?days=7")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["company_name"] == "Acme Corp"
        assert data[0]["days_since_contact"] == 10

    # 3. User with 1 recently-contacted company → not returned
    def test_active_company_not_returned(self, client, db_session, test_user):
        c = self._make_company(db_session, test_user)
        self._make_activity(db_session, test_user, c, days_ago=2)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention?days=7")
        assert resp.json() == []

    # 4. days=30 → only companies inactive 31+ days
    def test_custom_days_parameter(self, client, db_session, test_user):
        c1 = self._make_company(db_session, test_user, name="Stale Corp")
        self._make_activity(db_session, test_user, c1, days_ago=35)

        c2 = self._make_company(db_session, test_user, name="Active Corp")
        self._make_activity(db_session, test_user, c2, days_ago=15)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention?days=30")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["company_name"] == "Stale Corp"

    # 5. Deactivated company excluded
    def test_inactive_company_excluded(self, client, db_session, test_user):
        self._make_company(db_session, test_user, name="Dead Corp", is_active=False)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention")
        assert resp.json() == []

    # 6. Other user's companies not shown
    def test_other_users_companies_hidden(self, client, db_session, test_user, sales_user):
        self._make_company(db_session, sales_user, name="Their Corp")
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention")
        assert resp.json() == []

    # 7. Strategic flag included correctly
    def test_strategic_flag(self, client, db_session, test_user):
        self._make_company(db_session, test_user, name="VIP Corp", is_strategic=True)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["is_strategic"] is True

    # 8. Open req count aggregation
    def test_open_req_count(self, client, db_session, test_user):
        c = self._make_company(db_session, test_user)
        site = self._make_site(db_session, c)

        for i in range(2):
            req = Requisition(
                name=f"REQ-{i}",
                customer_site_id=site.id,
                status="active",
                created_by=test_user.id,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(req)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["open_req_count"] == 2

    # 9. Open quote value aggregation
    def test_open_quote_value(self, client, db_session, test_user):
        c = self._make_company(db_session, test_user)
        site = self._make_site(db_session, c)

        req = Requisition(
            name="REQ-Q",
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
            quote_number="Q-001",
            status="sent",
            subtotal=12500.00,
            sent_at=datetime.now(timezone.utc) - timedelta(days=3),
            created_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["open_quote_value"] == 12500.00

    # 10. Sort order: most stale first
    def test_sort_most_stale_first(self, client, db_session, test_user):
        c1 = self._make_company(db_session, test_user, name="Very Stale")
        self._make_activity(db_session, test_user, c1, days_ago=30)

        c2 = self._make_company(db_session, test_user, name="Somewhat Stale")
        self._make_activity(db_session, test_user, c2, days_ago=10)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention?days=7")
        data = resp.json()
        assert len(data) == 2
        assert data[0]["company_name"] == "Very Stale"
        assert data[1]["company_name"] == "Somewhat Stale"

    # 11. Received-only activity doesn't count as outreach
    def test_received_activity_not_counted(self, client, db_session, test_user):
        c = self._make_company(db_session, test_user)
        self._make_activity(db_session, test_user, c, days_ago=1, activity_type="email_received")
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention?days=7")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["company_name"] == "Acme Corp"

    # 12. Site-owner-only visibility: user owns site but not company.account_owner_id
    def test_site_owner_visibility(self, client, db_session, test_user, sales_user):
        """User who owns a site (but not company.account_owner_id) sees company."""
        c = Company(
            name="Shared Corp",
            is_active=True,
            account_owner_id=sales_user.id,  # company owned by someone else
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(c)
        db_session.flush()
        # test_user owns a site under this company
        s = CustomerSite(
            company_id=c.id,
            site_name="Branch A",
            owner_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["company_name"] == "Shared Corp"

    def test_stale_company_tz_aware_outreach(self, db_session, test_user, client):
        """When last outreach already has tzinfo (else branch in tz handling).

        Directly calls needs_attention() with a mock db that returns
        tz-aware datetimes from the activity query, bypassing SQLite's
        naive datetime behavior.
        """
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from app.routers.dashboard import needs_attention

        aware_dt = datetime.now(timezone.utc) - timedelta(days=60)
        assert aware_dt.tzinfo is not None  # sanity check

        company = SimpleNamespace(id=1, name="TZ Corp", is_active=True, is_strategic=False)
        # Combined row from subquery join: company_id, last_at, channel
        combined_row = SimpleNamespace(company_id=1, last_at=aware_dt, channel="email")

        # Build mock query chains for each db.query() call
        def make_chain(result):
            q = MagicMock()
            q.filter.return_value = q
            q.group_by.return_value = q
            q.order_by.return_value = q
            q.subquery.return_value = MagicMock(c=MagicMock())
            q.join.return_value = q
            q.all.return_value = result
            return q

        mock_db = MagicMock()
        mock_db.query.side_effect = [
            make_chain([]),               # CustomerSite subquery (owned_company_ids)
            make_chain([company]),        # Company query
            make_chain([]),               # Subquery (latest_sub) — returns subquery obj
            make_chain([combined_row]),   # Join query (latest_rows) — last_at + channel
            make_chain([]),               # Site query (empty)
        ]

        result = needs_attention(days=30, db=mock_db, user=SimpleNamespace(id=1))

        assert len(result) == 1
        assert result[0]["company_name"] == "TZ Corp"
        assert result[0]["days_since_contact"] >= 59


class TestHotOffers:
    """Tests for GET /api/dashboard/hot-offers endpoint."""

    def test_no_offers_returns_empty(self, client):
        """Empty DB → empty list."""
        resp = client.get("/api/dashboard/hot-offers")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_recent_offer_returned(self, client, db_session, test_user):
        """An active offer within the window appears in results."""
        req = Requisition(
            name="REQ-HOT-1", customer_name="Acme", status="open",
            created_by=test_user.id, created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        offer = Offer(
            requisition_id=req.id, vendor_name="Arrow", mpn="LM317T",
            qty_available=1000, unit_price=0.50, entered_by_id=test_user.id,
            status="active", created_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        db_session.add(offer)
        db_session.commit()

        resp = client.get("/api/dashboard/hot-offers?days=7")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["vendor_name"] == "Arrow"
        assert data[0]["mpn"] == "LM317T"
        assert data[0]["age_label"] == "2h ago"

    def test_old_offer_excluded(self, client, db_session, test_user):
        """Offer older than the window is excluded."""
        req = Requisition(
            name="REQ-HOT-2", customer_name="Acme", status="open",
            created_by=test_user.id, created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        offer = Offer(
            requisition_id=req.id, vendor_name="Mouser", mpn="NE555",
            qty_available=500, unit_price=0.25, entered_by_id=test_user.id,
            status="active", created_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        db_session.add(offer)
        db_session.commit()

        resp = client.get("/api/dashboard/hot-offers?days=7")
        assert resp.json() == []

    def test_age_label_just_now(self, client, db_session, test_user):
        """Offer created < 1 hour ago shows 'just now'."""
        req = Requisition(
            name="REQ-HOT-3", customer_name="Acme", status="open",
            created_by=test_user.id, created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        offer = Offer(
            requisition_id=req.id, vendor_name="DigiKey", mpn="ATmega328P",
            qty_available=100, unit_price=2.50, entered_by_id=test_user.id,
            status="active", created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        db_session.add(offer)
        db_session.commit()

        resp = client.get("/api/dashboard/hot-offers?days=7")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["age_label"] == "just now"

    def test_age_label_days_ago(self, client, db_session, test_user):
        """Offer created 3 days ago shows '3d ago'."""
        req = Requisition(
            name="REQ-HOT-4", customer_name="Acme", status="open",
            created_by=test_user.id, created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        offer = Offer(
            requisition_id=req.id, vendor_name="Arrow", mpn="LM7805",
            qty_available=200, unit_price=None, entered_by_id=test_user.id,
            status="active", created_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
        db_session.add(offer)
        db_session.commit()

        resp = client.get("/api/dashboard/hot-offers?days=7")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["age_label"] == "3d ago"
        assert data[0]["unit_price"] is None

    def test_days_filter(self, client, db_session, test_user):
        """The days parameter controls the window."""
        req = Requisition(
            name="REQ-HOT-5", customer_name="Acme", status="open",
            created_by=test_user.id, created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        offer = Offer(
            requisition_id=req.id, vendor_name="Arrow", mpn="LM317T",
            qty_available=1000, unit_price=0.50, entered_by_id=test_user.id,
            status="active", created_at=datetime.now(timezone.utc) - timedelta(days=20),
        )
        db_session.add(offer)
        db_session.commit()

        # 7 days: excluded
        resp = client.get("/api/dashboard/hot-offers?days=7")
        assert resp.json() == []

        # 30 days: included
        resp = client.get("/api/dashboard/hot-offers?days=30")
        assert len(resp.json()) == 1
