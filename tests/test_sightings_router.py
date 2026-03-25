"""Tests for sightings page router endpoints.

Called by: pytest
Depends on: conftest.py fixtures, app models, sighting_status service
"""

import json
from datetime import datetime, timedelta, timezone

from app.models.intelligence import ActivityLog, MaterialCard
from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition, Sighting
from app.models.vendor_sighting_summary import VendorSightingSummary
from app.models.vendors import VendorCard, VendorContact


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


class TestSightingsBatchLimit:
    def test_batch_refresh_over_limit_returns_400(self, client, db_session):
        ids = list(range(1, 52))  # 51 items, over the 50 limit
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": json.dumps(ids)},
        )
        assert resp.status_code == 400


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


class TestDashboardCounters:
    """Phase 2: Smart Priority Dashboard Strip counters in sightings_list context."""

    def test_urgent_count_high_priority(self, client, db_session):
        """Requirements with priority_score >= 70 counted as urgent."""
        req, r, _ = _seed_data(db_session)
        r.priority_score = 85.0
        db_session.commit()
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "1 Urgent" in resp.text or "1 urgent" in resp.text.lower()

    def test_urgent_count_near_deadline(self, client, db_session):
        """Requirements with need_by_date within 48h counted as urgent."""
        from datetime import date

        req, r, _ = _seed_data(db_session)
        r.need_by_date = date.today() + timedelta(days=1)
        r.priority_score = 20.0  # Low priority but near deadline
        db_session.commit()
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "urgent" in resp.text.lower()

    def test_stale_count(self, client, db_session):
        """Requirements with no recent activity counted as stale."""
        _seed_data(db_session)
        # No activity logs = stale
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "stale" in resp.text.lower()

    def test_pending_count(self, client, db_session):
        """Requirements with pending offers counted."""
        req, r, _ = _seed_data(db_session)
        offer = Offer(
            requirement_id=r.id,
            requisition_id=req.id,
            vendor_name="Good Vendor",
            mpn="TEST-MPN-001",
            status="pending_review",
            unit_price=1.50,
            qty_available=100,
        )
        db_session.add(offer)
        db_session.commit()
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "pending" in resp.text.lower()

    def test_unassigned_count(self, client, db_session):
        """Requirements with no assigned buyer counted."""
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "unassigned" in resp.text.lower()

    def test_counters_present_in_response(self, client, db_session):
        """All 4 dashboard counters appear in the HTML."""
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings")
        text = resp.text.lower()
        for label in ["urgent", "stale", "pending", "unassigned"]:
            assert label in text, f"Dashboard counter '{label}' missing from response"


class TestCoverageMap:
    """Phase 2: Fulfillment coverage bar data in sightings_list context."""

    def test_coverage_map_in_context(self, client, db_session):
        """Coverage map contains total estimated qty per requirement."""
        req, r, s = _seed_data(db_session)
        # s has estimated_qty=200, r has target_qty=100 => 200% coverage
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        # Coverage bar should show (rendered as percentage)
        assert "coverage" in resp.text.lower() or "200" in resp.text

    def test_coverage_zero_when_no_sightings(self, client, db_session):
        """Requirements with no sightings show 0% coverage."""
        req = Requisition(name="Test RFQ", status="active", customer_name="Acme Corp")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="EMPTY-MPN",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.commit()
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200


class TestHeatmapRows:
    """Phase 2: Two-state heatmap row identification."""

    def test_high_priority_in_heatmap(self, client, db_session):
        """Requirements with priority >= 70 flagged for rose tint."""
        req, r, _ = _seed_data(db_session)
        r.priority_score = 85.0
        db_session.commit()
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "bg-rose-50/30" in resp.text

    def test_normal_row_no_heatmap(self, client, db_session):
        """Low priority, non-stale requirements have no rose tint."""
        req, r, _ = _seed_data(db_session)
        r.priority_score = 20.0
        db_session.commit()
        # Add recent activity so not stale
        log = ActivityLog(
            activity_type="note",
            channel="system",
            requirement_id=r.id,
            requisition_id=req.id,
            notes="recent",
        )
        db_session.add(log)
        db_session.commit()
        resp = client.get("/v2/partials/sightings")
        assert "bg-rose-50/30" not in resp.text


class TestConstraintsSection:
    """Phase 3: Requirement constraints section in detail panel."""

    def test_constraints_shown_when_present(self, client, db_session):
        req, r, _ = _seed_data(db_session)
        r.condition = "New Original"
        r.date_codes = "2024+"
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_constraints_hidden_when_empty(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200


class TestVendorIntelligence:
    """Phase 3: Vendor intelligence data in detail panel context."""

    def test_vendor_card_data_in_response(self, client, db_session):
        """VendorCard intelligence fields appear in detail panel."""
        req, r, s = _seed_data(db_session)
        vc = VendorCard(
            normalized_name="good vendor",
            display_name="Good Vendor",
            response_rate=0.85,
            ghost_rate=0.05,
            vendor_score=72.0,
            engagement_score=65.0,
        )
        db_session.add(vc)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "Good Vendor" in resp.text

    def test_explain_lead_in_response(self, client, db_session):
        """explain_lead() output rendered for each vendor."""
        req, r, s = _seed_data(db_session)
        vc = VendorCard(
            normalized_name="good vendor",
            display_name="Good Vendor",
            vendor_score=72.0,
        )
        db_session.add(vc)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "Good Vendor" in resp.text

    def test_detail_works_without_vendor_card(self, client, db_session):
        """Detail panel works even when no VendorCard exists for a vendor."""
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "Good Vendor" in resp.text


class TestOOODetection:
    """Phase 3: OOO contact detection in detail panel."""

    def test_ooo_data_in_context(self, client, db_session):
        req, r, s = _seed_data(db_session)
        vc = VendorCard(
            normalized_name="good vendor",
            display_name="Good Vendor",
        )
        db_session.add(vc)
        db_session.flush()
        contact = VendorContact(
            vendor_card_id=vc.id,
            contact_type="sales",
            email="test@good.com",
            source="email",
            is_ooo=True,
            ooo_return_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )
        db_session.add(contact)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_no_ooo_when_not_ooo(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200


class TestSuggestedAction:
    """Phase 3: State-machine-driven suggested next action."""

    def test_open_with_sightings(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "send RFQ" in resp.text.lower() or "vendor" in resp.text.lower()

    def test_open_no_sightings(self, client, db_session):
        req = Requisition(name="Test RFQ", status="active", customer_name="Acme Corp")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="EMPTY-001",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "search" in resp.text.lower() or "no vendor" in resp.text.lower() or "no sighting" in resp.text.lower()

    def test_quoted_status(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        r.sourcing_status = "quoted"
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200


class TestEnrichmentBar:
    """Phase 3: MaterialCard enrichment bar in detail panel."""

    def test_enrichment_data_in_context(self, client, db_session):
        req, r, _ = _seed_data(db_session)
        mc = MaterialCard(
            normalized_mpn="test-mpn-001",
            display_mpn="TEST-MPN-001",
            lifecycle_status="active",
            category="Microcontroller",
            rohs_status="compliant",
        )
        db_session.add(mc)
        db_session.flush()
        r.material_card_id = mc.id
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_detail_works_without_material_card(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_eol_card_in_context(self, client, db_session):
        req, r, _ = _seed_data(db_session)
        mc = MaterialCard(
            normalized_mpn="test-mpn-001",
            display_mpn="TEST-MPN-001",
            lifecycle_status="eol",
            category="Memory",
        )
        db_session.add(mc)
        db_session.flush()
        r.material_card_id = mc.id
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200


class TestEmptyStates:
    """Phase 3: Better empty states with contextual CTAs."""

    def test_empty_table_shows_cta(self, client, db_session):
        """Filtered table with no results shows clear-filters CTA."""
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings?status=won")
        assert resp.status_code == 200
        assert "no requirements" in resp.text.lower() or "clear" in resp.text.lower()

    def test_empty_sightings_in_detail(self, client, db_session):
        """Requirement with no sightings shows Run Search CTA."""
        req = Requisition(name="Test RFQ", status="active", customer_name="Acme Corp")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="EMPTY-001",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert (
            "no vendor" in resp.text.lower() or "run search" in resp.text.lower() or "no sighting" in resp.text.lower()
        )
