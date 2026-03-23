"""Tests for sightings page router endpoints.

Called by: pytest
Depends on: conftest.py fixtures, app models, sighting_status service
"""

import json

from app.models.sourcing import Requirement, Requisition, Sighting
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


class TestSightingsWorkspace:
    def test_returns_200(self, client, db_session):
        resp = client.get("/v2/partials/sightings/workspace")
        assert resp.status_code == 200

    def test_contains_split_panel(self, client, db_session):
        resp = client.get("/v2/partials/sightings/workspace")
        assert "sightings-table" in resp.text
        assert "sightings-detail" in resp.text


class TestSightingsEmptyState:
    def test_list_empty_db(self, client, db_session):
        """List endpoint with no data returns 200."""
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200

    def test_list_search_no_match(self, client, db_session):
        """Search filter with no matching MPN returns empty."""
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings?q=NONEXISTENT-XYZ")
        assert resp.status_code == 200
        assert "TEST-MPN-001" not in resp.text


class TestSightingsFilters:
    def test_search_by_mpn(self, client, db_session):
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings?q=TEST-MPN")
        assert resp.status_code == 200
        assert "TEST-MPN-001" in resp.text

    def test_search_by_customer(self, client, db_session):
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings?q=Acme")
        assert resp.status_code == 200
        assert "TEST-MPN-001" in resp.text

    def test_group_by_manufacturer(self, client, db_session):
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings?group_by=manufacturer")
        assert resp.status_code == 200
        assert "TestMfr" in resp.text

    def test_group_by_brand(self, client, db_session):
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings?group_by=brand")
        assert resp.status_code == 200

    def test_assigned_mine_empty(self, client, db_session):
        """Assigned=mine with no assigned requirements returns empty."""
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings?assigned=mine")
        assert resp.status_code == 200
        assert "TEST-MPN-001" not in resp.text

    def test_sort_by_mpn(self, client, db_session):
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings?sort=mpn&dir=asc")
        assert resp.status_code == 200

    def test_invalid_sort_defaults_gracefully(self, client, db_session):
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings?sort=invalid_col")
        assert resp.status_code == 200


class TestSightingsRefresh:
    def test_returns_200(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(f"/v2/partials/sightings/{r.id}/refresh")
        assert resp.status_code == 200

    def test_404_for_missing(self, client, db_session):
        resp = client.post("/v2/partials/sightings/99999/refresh")
        assert resp.status_code == 404


class TestSightingsMarkUnavailable:
    def test_marks_sightings_unavailable(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        s = Sighting(
            requirement_id=r.id,
            vendor_name="Good Vendor",
            mpn_matched="TEST-MPN-001",
        )
        db_session.add(s)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-unavailable",
            data={"vendor_name": "Good Vendor"},
        )
        assert resp.status_code == 200

    def test_400_without_vendor_name(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-unavailable",
            data={},
        )
        assert resp.status_code == 400

    def test_noop_when_no_matching_sightings(self, client, db_session):
        """No matching sightings for vendor returns 200 (no-op)."""
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-unavailable",
            data={"vendor_name": "Nonexistent Vendor"},
        )
        assert resp.status_code == 200


class TestSightingsAssignBuyer:
    def test_assigns_buyer(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.patch(
            f"/v2/partials/sightings/{r.id}/assign",
            data={"assigned_buyer_id": "1"},
        )
        assert resp.status_code == 200

    def test_unassigns_buyer(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.patch(
            f"/v2/partials/sightings/{r.id}/assign",
            data={"assigned_buyer_id": ""},
        )
        assert resp.status_code == 200

    def test_404_for_missing(self, client, db_session):
        resp = client.patch(
            "/v2/partials/sightings/99999/assign",
            data={"assigned_buyer_id": "1"},
        )
        assert resp.status_code == 404


class TestSightingsBatchRefresh:
    def test_returns_200(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": json.dumps([r.id])},
        )
        assert resp.status_code == 200

    def test_empty_list(self, client, db_session):
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": "[]"},
        )
        assert resp.status_code == 200

    def test_nonexistent_ids(self, client, db_session):
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": json.dumps([99999])},
        )
        assert resp.status_code == 200


class TestSightingsVendorModal:
    def test_returns_200(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/vendor-modal?requirement_ids={r.id}")
        assert resp.status_code == 200

    def test_empty_ids(self, client, db_session):
        resp = client.get("/v2/partials/sightings/vendor-modal?requirement_ids=")
        assert resp.status_code == 200

    def test_nonexistent_ids(self, client, db_session):
        resp = client.get("/v2/partials/sightings/vendor-modal?requirement_ids=99999")
        assert resp.status_code == 200


class TestSightingsSendInquiry:
    def test_400_without_params(self, client, db_session):
        resp = client.post("/v2/partials/sightings/send-inquiry", data={})
        assert resp.status_code == 400

    def test_400_missing_body(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/send-inquiry",
            data={"requirement_ids": str(r.id), "vendor_names": "Acme"},
        )
        assert resp.status_code == 400
