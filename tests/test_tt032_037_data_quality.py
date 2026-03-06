"""Tests for TT-032 + TT-037: Data quality filters on hot offers and attention feed.

Verifies that test/seed data pollution is filtered out:
- Hot offers with inflated prices (>$10,000) are excluded
- Hot offers where all items link to the same requisition are excluded
- Attention feed items with numeric-only titles are excluded

Called by: pytest
Depends on: app/routers/dashboard/briefs.py, app/routers/dashboard/overview.py, conftest fixtures
"""

from datetime import datetime, timedelta, timezone

from app.models import Company, CustomerSite, Offer, Requisition


class TestHotOffersDataQuality:
    """Tests for price cap and same-requisition filtering in /api/dashboard/hot-offers."""

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
        """Offers with unit_price > $10,000 are excluded (TT-037 inflated prices)."""
        req1 = self._make_req(db_session, test_user, "REQ-A")
        req2 = self._make_req(db_session, test_user, "REQ-B")
        self._make_offer(db_session, req1.id, price=50_000.00, vendor="Junk")
        self._make_offer(db_session, req2.id, price=5.00, vendor="Good")
        db_session.commit()

        resp = client.get("/api/dashboard/hot-offers")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["vendor_name"] == "Good"

    def test_all_same_requisition_filtered(self, client, db_session, test_user):
        """All offers linking to same requisition are suspicious batch — filtered out (TT-037)."""
        req = self._make_req(db_session, test_user, "REQ-SPAM")
        for i in range(5):
            self._make_offer(db_session, req.id, price=1.00 + i, vendor=f"Vendor-{i}")
        db_session.commit()

        resp = client.get("/api/dashboard/hot-offers")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 0

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
        """Offer at $10,001 is excluded."""
        req1 = self._make_req(db_session, test_user, "REQ-A")
        req2 = self._make_req(db_session, test_user, "REQ-B")
        self._make_offer(db_session, req1.id, price=10_001.00)
        self._make_offer(db_session, req2.id, price=5.00)
        db_session.commit()

        resp = client.get("/api/dashboard/hot-offers")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["unit_price"] == 5.0


class TestAttentionFeedNumericTitles:
    """Tests for numeric-only title filtering in /api/dashboard/attention-feed (TT-032)."""

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
        """Requisitions with numeric-only names are filtered from attention feed (TT-032)."""
        # Numeric-only name — should be filtered
        self._make_req(db_session, test_user, name="21702", deadline="ASAP", days_ago=1)
        # Normal name — should appear
        self._make_req(db_session, test_user, name="REAL-REQ", deadline="ASAP", days_ago=1)
        db_session.commit()

        resp = client.get("/api/dashboard/attention-feed")
        assert resp.status_code == 200
        items = resp.json()
        titles = [i["title"] for i in items]
        assert "21702" not in titles
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
        """Multiple numeric-only titled items are all filtered."""
        for n in ["11111", "22222", "33333"]:
            self._make_req(db_session, test_user, name=n, deadline="ASAP", days_ago=1)
        db_session.commit()

        resp = client.get("/api/dashboard/attention-feed")
        assert resp.status_code == 200
        items = resp.json()
        for item in items:
            assert not item["title"].isdigit(), f"Numeric title '{item['title']}' should be filtered"

    def test_stale_account_with_numeric_name_filtered(self, client, db_session, test_user):
        """Companies with numeric-only names are also filtered from attention feed."""
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
        assert "99999" not in titles
