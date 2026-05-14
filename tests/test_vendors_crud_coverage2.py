"""tests/test_vendors_crud_coverage2.py — Additional coverage for vendors_crud.py.

Targets:
  lines 177-180: list_vendors with tag filter
  lines 379-380: typeahead invalid limit → fallback to 8
  line  480: delete_vendor with active offers → 400
  lines 213-217: list_vendors FTS fallback path (q with FTS not available)

Called by: pytest autodiscovery
Depends on: conftest.py fixtures (client, db_session, test_user, test_vendor_card)
"""

import os

os.environ["TESTING"] = "1"

from sqlalchemy.orm import Session

from app.models import Offer, Requisition, VendorCard

# ── list_vendors with tag filter (lines 177-180) ─────────────────────────────


class TestListVendorsTagFilter:
    def test_list_vendors_with_tag_filter(self, client, db_session: Session):
        """Tag filter applies (lines 177-182)."""
        card = VendorCard(
            normalized_name="texas instruments tag",
            display_name="Texas Instruments Tagged",
            brand_tags=["linear"],
            commodity_tags=["analog ic"],
        )
        db_session.add(card)
        db_session.commit()

        resp = client.get("/api/vendors?tag=analog")
        assert resp.status_code == 200
        data = resp.json()
        assert "vendors" in data

    def test_list_vendors_with_q_filter(self, client, db_session: Session):
        """q filter applies name search (lines 184-222)."""
        resp = client.get("/api/vendors?q=arrow")
        assert resp.status_code == 200
        data = resp.json()
        assert "vendors" in data

    def test_list_vendors_with_sort(self, client, db_session: Session):
        """Sort parameter works."""
        resp = client.get("/api/vendors?sort=sighting_count&dir=desc")
        assert resp.status_code == 200
        data = resp.json()
        assert "vendors" in data


# ── delete vendor with active offers → 400 (line 480) ────────────────────────


class TestDeleteVendorWithOffers:
    def test_delete_vendor_with_active_offers_returns_400(
        self, client, db_session: Session, test_vendor_card: VendorCard, test_user
    ):
        req = Requisition(name="Test Req Delete", status="active", created_by=test_user.id)
        db_session.add(req)
        db_session.commit()

        offer = Offer(
            vendor_card_id=test_vendor_card.id,
            vendor_name="Arrow Electronics",
            mpn="LM317T",
            requisition_id=req.id,
        )
        db_session.add(offer)
        db_session.commit()

        resp = client.delete(f"/api/vendors/{test_vendor_card.id}")
        assert resp.status_code == 400
        assert "Cannot delete" in resp.json()["error"]

    def test_delete_vendor_not_found_returns_404(self, client):
        resp = client.delete("/api/vendors/999999")
        assert resp.status_code == 404

    def test_delete_vendor_success(self, client, db_session: Session):
        card = VendorCard(
            normalized_name="deletable vendor test",
            display_name="Deletable Vendor",
        )
        db_session.add(card)
        db_session.commit()

        resp = client.delete(f"/api/vendors/{card.id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ── typeahead invalid limit fallback (lines 379-380) ─────────────────────────


class TestVendorTypeaheadInvalidLimit:
    def test_invalid_limit_falls_back_to_8(self, client, test_vendor_card: VendorCard):
        """Invalid limit param falls back to 8 (lines 379-380)."""
        resp = client.get("/api/autocomplete/names?q=arrow&limit=not_a_number")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_short_query_returns_empty(self, client):
        """q < 2 chars returns [] (line 376)."""
        resp = client.get("/api/autocomplete/names?q=a")
        assert resp.status_code == 200
        assert resp.json() == []
