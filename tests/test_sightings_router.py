"""Tests for sightings page router endpoints.

Called by: pytest
Depends on: conftest.py fixtures, app models, sighting_status service
"""

from app.models.sourcing import Requirement, Requisition
from app.models.vendor_sighting_summary import VendorSightingSummary


def _seed_data(db_session):
    """Create requisition + requirement + sighting for testing."""
    req = Requisition(name="Test RFQ", status="active", customer_name="Acme Corp")
    db_session.add(req)
    db_session.flush()
    r = Requirement(
        requisition_id=req.id,
        primary_mpn="TEST-MPN-001",
        manufacturer="TestMfr",
        target_qty=100,
        sourcing_status="open",
    )
    db_session.add(r)
    db_session.flush()
    s = VendorSightingSummary(
        requirement_id=r.id,
        vendor_name="Good Vendor",
        estimated_qty=200,
        listing_count=2,
        score=75.0,
        tier="Good",
    )
    db_session.add(s)
    db_session.commit()
    return req, r, s


class TestSightingsListPartial:
    def test_returns_200(self, client, db_session):
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200

    def test_contains_requirement_mpn(self, client, db_session):
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings")
        assert "TEST-MPN-001" in resp.text

    def test_filter_by_status(self, client, db_session):
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings?status=open")
        assert resp.status_code == 200
        assert "TEST-MPN-001" in resp.text

    def test_filter_by_status_excludes(self, client, db_session):
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings?status=won")
        assert "TEST-MPN-001" not in resp.text

    def test_pagination_defaults(self, client, db_session):
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings?page=1")
        assert resp.status_code == 200


class TestSightingsDetailPartial:
    def test_returns_200(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_contains_vendor_name(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert "Good Vendor" in resp.text

    def test_404_for_missing(self, client, db_session):
        resp = client.get("/v2/partials/sightings/99999/detail")
        assert resp.status_code == 404
