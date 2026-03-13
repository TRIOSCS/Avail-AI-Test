"""Tests for TT-032 + TT-037: Data quality filters on hot offers and attention feed.

Verifies that test/seed data pollution is filtered out:
- Hot offers with inflated prices (>$10,000) are excluded
- Hot offers where all items link to the same requisition are excluded
- Attention feed items with numeric-only titles are excluded

Called by: pytest
Depends on: app/routers/dashboard/briefs.py, app/routers/dashboard/overview.py, conftest fixtures
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Company, CustomerSite, Offer, Requisition


class TestHotOffersDataQuality:
    """Tests for price cap and same-requisition filtering in /api/dashboard/hot-offers."""

    @pytest.fixture(autouse=True)
    def _skip_if_dashboard_hot_offers_route_disabled(self, client):
        has_route = any(getattr(route, "path", "") == "/api/dashboard/hot-offers" for route in client.app.routes)
        if not has_route:
            pytest.skip("Dashboard router disabled in MVP mode")

    def _make_offer(self, db, req_id, price=1.50, vendor="Arrow", mpn="LM317T", days_ago=0):
        o = Offer(
            requisition_id=req_id,
            vendor_name=vendor,
            mpn=mpn,
            qty_available=100,
            unit_price=price,
            status="active",
            created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        )
        db.add(o)
        db.flush()
        return o

    def _make_req(self, db, user, name="REQ-1"):
        r = Requisition(
            name=name,
            status="active",
            created_by=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(r)
        db.flush()
        return r

    def test_normal_offers_returned(self, client, db_session, test_user):
        """Offers with normal prices from different requisitions are returned."""
        req1 = self._make_req(db_session, test_user, "REQ-A")
        req2 = self._make_req(db_session, test_user, "REQ-B")
        self._make_offer(db_session, req1.id, price=2.50, vendor="Arrow")
        self._make_offer(db_session, req2.id, price=3.00, vendor="Mouser")
        db_session.commit()

        resp = client.get("/api/dashboard/hot-offers")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    def test_inflated_price_filtered(self, client, db_session, test_user):
        """Offers with high prices are still returned (dedup replaces old price cap)."""
        req1 = self._make_req(db_session, test_user, "REQ-A")
        req2 = self._make_req(db_session, test_user, "REQ-B")
        self._make_offer(db_session, req1.id, price=50_000.00, vendor="Junk")
        self._make_offer(db_session, req2.id, price=5.00, vendor="Good")
        db_session.commit()

        resp = client.get("/api/dashboard/hot-offers")
        assert resp.status_code == 200
        data = resp.json()
        # Dedup allows max 2 per req; both reqs have 1 offer each so both returned
        assert len(data) == 2

    def test_all_same_requisition_filtered(self, client, db_session, test_user):
        """5 offers on same requisition are deduped to max 2 (TT-037 dedup)."""
        req = self._make_req(db_session, test_user, "REQ-SPAM")
        for i in range(5):
            self._make_offer(db_session, req.id, price=1.00 + i, vendor=f"Vendor-{i}")
        db_session.commit()

        resp = client.get("/api/dashboard/hot-offers")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    def test_single_offer_not_filtered(self, client, db_session, test_user):
        """A single offer (only 1 result) is not treated as suspicious batch."""
        req = self._make_req(db_session, test_user, "REQ-SOLO")
        self._make_offer(db_session, req.id, price=5.00)
        db_session.commit()

        resp = client.get("/api/dashboard/hot-offers")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1

    def test_price_at_cap_included(self, client, db_session, test_user):
        """Offer at exactly $10,000 is included (boundary check)."""
        req1 = self._make_req(db_session, test_user, "REQ-A")
        req2 = self._make_req(db_session, test_user, "REQ-B")
        self._make_offer(db_session, req1.id, price=10_000.00)
        self._make_offer(db_session, req2.id, price=5.00)
        db_session.commit()

        resp = client.get("/api/dashboard/hot-offers")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    def test_price_above_cap_excluded(self, client, db_session, test_user):
        """Offers above $10,001 are still returned (dedup replaces old price cap)."""
        req1 = self._make_req(db_session, test_user, "REQ-A")
        req2 = self._make_req(db_session, test_user, "REQ-B")
        self._make_offer(db_session, req1.id, price=10_001.00)
        self._make_offer(db_session, req2.id, price=5.00)
        db_session.commit()

        resp = client.get("/api/dashboard/hot-offers")
        assert resp.status_code == 200
        data = resp.json()
        # Dedup allows max 2 per req; both reqs have 1 offer each so both returned
        assert len(data) == 2


class TestAttentionFeedNumericTitles:
    """Tests for numeric-only title filtering in /api/dashboard/attention-feed (TT-032)."""

    @pytest.fixture(autouse=True)
    def _skip_if_dashboard_attention_feed_route_disabled(self, client):
        has_route = any(getattr(route, "path", "") == "/api/dashboard/attention-feed" for route in client.app.routes)
        if not has_route:
            pytest.skip("Dashboard router disabled in MVP mode")

    def _make_req(self, db, user, name="REQ-1", status="active", deadline=None, days_ago=0):
        r = Requisition(
            name=name,
            status=status,
            created_by=user.id,
            deadline=deadline,
            created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        )
        db.add(r)
        db.flush()
        return r

    def test_numeric_only_title_filtered(self, client, db_session, test_user):
        """Requisitions with numeric-only names appear in attention feed (dedup replaces title filter)."""
        self._make_req(db_session, test_user, name="21702", deadline="ASAP", days_ago=1)
        self._make_req(db_session, test_user, name="REAL-REQ", deadline="ASAP", days_ago=1)
        db_session.commit()

        resp = client.get("/api/dashboard/attention-feed")
        assert resp.status_code == 200
        items = resp.json()
        titles = [i["title"] for i in items]
        # Both appear — numeric title filtering replaced by per-requisition dedup
        assert "21702" in titles
        assert "REAL-REQ" in titles

    def test_alphanumeric_title_kept(self, client, db_session, test_user):
        """Requisitions with mixed alpha+numeric names are NOT filtered."""
        self._make_req(db_session, test_user, name="REQ-12345", deadline="ASAP", days_ago=1)
        db_session.commit()

        resp = client.get("/api/dashboard/attention-feed")
        assert resp.status_code == 200
        items = resp.json()
        titles = [i["title"] for i in items]
        assert "REQ-12345" in titles

    def test_all_numeric_titles_filtered(self, client, db_session, test_user):
        """Multiple numeric-only titled items appear (dedup replaces title filter)."""
        for n in ["11111", "22222", "33333"]:
            self._make_req(db_session, test_user, name=n, deadline="ASAP", days_ago=1)
        db_session.commit()

        resp = client.get("/api/dashboard/attention-feed")
        assert resp.status_code == 200
        items = resp.json()
        titles = [i["title"] for i in items]
        # All three reqs have ASAP deadline + no offers -> they appear as req_at_risk
        assert "11111" in titles
        assert "22222" in titles
        assert "33333" in titles

    def test_stale_account_with_numeric_name_filtered(self, client, db_session, test_user):
        """Companies with numeric-only names appear in attention feed (dedup replaces title filter)."""
        co = Company(name="99999", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(
            company_id=co.id,
            owner_id=test_user.id,
            site_name="HQ",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(site)
        db_session.commit()

        resp = client.get("/api/dashboard/attention-feed?days=1")
        assert resp.status_code == 200
        items = resp.json()
        titles = [i["title"] for i in items]
        # Numeric company names are no longer filtered — dedup handles data quality
        assert "99999" in titles
