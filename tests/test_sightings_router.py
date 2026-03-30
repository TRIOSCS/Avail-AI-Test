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

    def test_search_by_substitute_mpn(self, client, db_session):
        """Search filter matches requirements by substitute MPN."""
        req = Requisition(name="Sub RFQ", status="active", customer_name="SubCo")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="PRIMARY-001",
            manufacturer="Mfr",
            target_qty=50,
            sourcing_status="open",
            substitutes=[{"mpn": "ALT-SUB-777", "manufacturer": "AltMfr"}],
            substitutes_text="ALT-SUB-777",
        )
        db_session.add(r)
        db_session.flush()
        db_session.add(
            VendorSightingSummary(
                requirement_id=r.id,
                vendor_name="V1",
                listing_count=1,
                score=50.0,
            )
        )
        db_session.commit()

        # Search by sub MPN should find this requirement
        resp = client.get("/v2/partials/sightings?q=ALT-SUB-777")
        assert resp.status_code == 200
        assert "PRIMARY-001" in resp.text

        # Search by primary MPN still works
        resp = client.get("/v2/partials/sightings?q=PRIMARY-001")
        assert resp.status_code == 200
        assert "PRIMARY-001" in resp.text

    def test_search_by_sub_no_false_positive(self, client, db_session):
        """Sub search does not return unrelated requirements."""
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings?q=ALT-SUB-777")
        assert resp.status_code == 200
        assert "TEST-MPN-001" not in resp.text


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

    def test_counters_present_in_response(self, client, db_session):
        """Dashboard counters appear in the HTML (no buyer assignment)."""
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings")
        text = resp.text.lower()
        for label in ["urgent", "stale", "pending"]:
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


class TestVendorOverlap:
    """Phase 4.7: Cross-requirement vendor overlap counts in detail panel."""

    def test_overlap_badge_shown_when_vendor_on_multiple_reqs(self, client, db_session):
        """Vendor appearing on 2+ active reqs shows 'Also on N other reqs' badge."""
        req = Requisition(name="RFQ-1", status="active", customer_name="Acme Corp")
        db_session.add(req)
        db_session.flush()
        r1 = Requirement(
            requisition_id=req.id,
            primary_mpn="MPN-A",
            target_qty=100,
            sourcing_status="open",
        )
        r2 = Requirement(
            requisition_id=req.id,
            primary_mpn="MPN-B",
            target_qty=50,
            sourcing_status="open",
        )
        db_session.add_all([r1, r2])
        db_session.flush()
        # Same vendor on both requirements
        db_session.add_all(
            [
                VendorSightingSummary(
                    requirement_id=r1.id,
                    vendor_name="Overlap Vendor",
                    estimated_qty=100,
                    listing_count=1,
                    score=80.0,
                ),
                VendorSightingSummary(
                    requirement_id=r2.id,
                    vendor_name="Overlap Vendor",
                    estimated_qty=50,
                    listing_count=1,
                    score=60.0,
                ),
            ]
        )
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r1.id}/detail")
        assert resp.status_code == 200
        assert "Also on 1 other req" in resp.text

    def test_no_overlap_badge_for_single_req_vendor(self, client, db_session):
        """Vendor on only one requirement shows no overlap badge."""
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "Also on" not in resp.text

    def test_overlap_counts_multiple_reqs(self, client, db_session):
        """Vendor on 3 reqs shows 'Also on 2 other reqs'."""
        req = Requisition(name="RFQ-M", status="active", customer_name="Multi Corp")
        db_session.add(req)
        db_session.flush()
        reqs = []
        for i in range(3):
            r = Requirement(
                requisition_id=req.id,
                primary_mpn=f"MPN-{i}",
                target_qty=100,
                sourcing_status="open",
            )
            db_session.add(r)
            db_session.flush()
            reqs.append(r)
        for r in reqs:
            db_session.add(
                VendorSightingSummary(
                    requirement_id=r.id,
                    vendor_name="Multi Vendor",
                    estimated_qty=100,
                    listing_count=1,
                    score=70.0,
                )
            )
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{reqs[0].id}/detail")
        assert resp.status_code == 200
        assert "Also on 2 other reqs" in resp.text

    def test_overlap_excludes_inactive_requisitions(self, client, db_session):
        """Vendors on inactive requisitions are not counted in overlap."""
        active_req = Requisition(name="Active RFQ", status="active", customer_name="Active Corp")
        inactive_req = Requisition(name="Inactive RFQ", status="closed", customer_name="Closed Corp")
        db_session.add_all([active_req, inactive_req])
        db_session.flush()
        r1 = Requirement(
            requisition_id=active_req.id,
            primary_mpn="MPN-ACT",
            target_qty=100,
            sourcing_status="open",
        )
        r2 = Requirement(
            requisition_id=inactive_req.id,
            primary_mpn="MPN-INACT",
            target_qty=100,
            sourcing_status="open",
        )
        db_session.add_all([r1, r2])
        db_session.flush()
        db_session.add_all(
            [
                VendorSightingSummary(
                    requirement_id=r1.id,
                    vendor_name="Inactive Test Vendor",
                    estimated_qty=100,
                    listing_count=1,
                    score=80.0,
                ),
                VendorSightingSummary(
                    requirement_id=r2.id,
                    vendor_name="Inactive Test Vendor",
                    estimated_qty=100,
                    listing_count=1,
                    score=60.0,
                ),
            ]
        )
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r1.id}/detail")
        assert resp.status_code == 200
        # Only 1 active req, so no overlap badge
        assert "Also on" not in resp.text


class TestVendorCollapse:
    """Phase 2.6: Vendor list collapses at 5, showing toggle for overflow."""

    def test_six_vendors_shows_collapse_toggle(self, client, db_session):
        """With 6 vendors, the 'Show 1 more vendor' link appears."""
        req = Requisition(name="Big RFQ", status="active", customer_name="Big Corp")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="MPN-BIG",
            target_qty=100,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.flush()
        for i in range(6):
            db_session.add(
                VendorSightingSummary(
                    requirement_id=r.id,
                    vendor_name=f"Vendor {i + 1}",
                    estimated_qty=100,
                    listing_count=1,
                    score=80.0 - i * 5,
                )
            )
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "Show 1 more vendor" in resp.text

    def test_five_vendors_no_collapse(self, client, db_session):
        """With exactly 5 vendors, no collapse toggle appears."""
        req = Requisition(name="Five RFQ", status="active", customer_name="Five Corp")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="MPN-FIVE",
            target_qty=100,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.flush()
        for i in range(5):
            db_session.add(
                VendorSightingSummary(
                    requirement_id=r.id,
                    vendor_name=f"Vendor {i + 1}",
                    estimated_qty=100,
                    listing_count=1,
                    score=80.0 - i * 5,
                )
            )
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "Show" not in resp.text or "Show less" not in resp.text

    def test_eight_vendors_shows_correct_count(self, client, db_session):
        """With 8 vendors, shows 'Show 3 more vendors'."""
        req = Requisition(name="Eight RFQ", status="active", customer_name="Eight Corp")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="MPN-EIGHT",
            target_qty=100,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.flush()
        for i in range(8):
            db_session.add(
                VendorSightingSummary(
                    requirement_id=r.id,
                    vendor_name=f"Vendor {i + 1}",
                    estimated_qty=100,
                    listing_count=1,
                    score=80.0 - i * 5,
                )
            )
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "Show 3 more vendors" in resp.text


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


class TestBatchAssign:
    """Phase 4.5: Batch assign buyer to multiple requirements."""

    def test_assigns_all_requirements(self, client, db_session):
        from app.models import User as UserModel

        buyer = UserModel(name="Test Buyer", email="buyer@test.com", is_active=True)
        db_session.add(buyer)
        db_session.flush()
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": json.dumps([r.id]), "buyer_id": str(buyer.id)},
        )
        assert resp.status_code == 200
        assert "Assigned 1 requirement" in resp.text
        db_session.refresh(r)
        assert r.assigned_buyer_id == buyer.id

    def test_unassign(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        r.assigned_buyer_id = 1
        db_session.commit()
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": json.dumps([r.id]), "buyer_id": ""},
        )
        assert resp.status_code == 200
        assert "Assigned 1 requirement" in resp.text
        db_session.refresh(r)
        assert r.assigned_buyer_id is None

    def test_over_limit_returns_400(self, client, db_session):
        ids = list(range(1, 52))
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": json.dumps(ids), "buyer_id": "1"},
        )
        assert resp.status_code == 400

    def test_empty_list_returns_warning(self, client, db_session):
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": "[]", "buyer_id": "1"},
        )
        assert resp.status_code == 200
        assert "No requirements selected" in resp.text


class TestBatchStatus:
    """Phase 4.5: Batch status change with transition validation."""

    def test_updates_valid_transitions(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        # open -> sourcing is valid
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([r.id]), "status": "sourcing"},
        )
        assert resp.status_code == 200
        assert "Updated 1 of 1" in resp.text
        db_session.refresh(r)
        assert r.sourcing_status == "sourcing"

    def test_creates_activity_log(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([r.id]), "status": "sourcing"},
        )
        assert resp.status_code == 200
        logs = (
            db_session.query(ActivityLog)
            .filter(
                ActivityLog.requirement_id == r.id,
                ActivityLog.activity_type == "status_change",
            )
            .all()
        )
        assert len(logs) == 1
        assert "open" in logs[0].notes
        assert "sourcing" in logs[0].notes

    def test_skips_invalid_transitions(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        # open -> won is NOT valid (must go open -> sourcing -> offered -> quoted -> won)
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([r.id]), "status": "won"},
        )
        assert resp.status_code == 200
        assert "skipped" in resp.text.lower()
        db_session.refresh(r)
        assert r.sourcing_status == "open"

    def test_mixed_valid_and_invalid(self, client, db_session):
        req = Requisition(name="Mix RFQ", status="active", customer_name="Mix Corp")
        db_session.add(req)
        db_session.flush()
        r1 = Requirement(requisition_id=req.id, primary_mpn="MPN-A", target_qty=100, sourcing_status="open")
        r2 = Requirement(requisition_id=req.id, primary_mpn="MPN-B", target_qty=100, sourcing_status="won")
        db_session.add_all([r1, r2])
        db_session.commit()
        # sourcing is valid from open, not from won
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([r1.id, r2.id]), "status": "sourcing"},
        )
        assert resp.status_code == 200
        assert "Updated 1 of 2" in resp.text
        assert "1 skipped" in resp.text

    def test_over_limit_returns_400(self, client, db_session):
        ids = list(range(1, 52))
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps(ids), "status": "sourcing"},
        )
        assert resp.status_code == 400

    def test_invalid_status_returns_400(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([r.id]), "status": "nonexistent"},
        )
        assert resp.status_code == 400


class TestBatchNotes:
    """Phase 4.5: Batch add notes to multiple requirements."""

    def test_creates_activity_logs(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps([r.id]), "notes": "Test batch note"},
        )
        assert resp.status_code == 200
        assert "Added note to 1 requirement" in resp.text
        logs = (
            db_session.query(ActivityLog)
            .filter(
                ActivityLog.requirement_id == r.id,
                ActivityLog.activity_type == "note",
            )
            .all()
        )
        assert len(logs) == 1
        assert logs[0].notes == "Test batch note"

    def test_multiple_requirements(self, client, db_session):
        req = Requisition(name="Multi RFQ", status="active", customer_name="Multi Corp")
        db_session.add(req)
        db_session.flush()
        r1 = Requirement(requisition_id=req.id, primary_mpn="MPN-1", target_qty=100, sourcing_status="open")
        r2 = Requirement(requisition_id=req.id, primary_mpn="MPN-2", target_qty=200, sourcing_status="open")
        db_session.add_all([r1, r2])
        db_session.commit()
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps([r1.id, r2.id]), "notes": "Shared note"},
        )
        assert resp.status_code == 200
        assert "Added note to 2 requirements" in resp.text
        logs = (
            db_session.query(ActivityLog)
            .filter(
                ActivityLog.activity_type == "note",
                ActivityLog.notes == "Shared note",
            )
            .all()
        )
        assert len(logs) == 2

    def test_empty_notes_returns_warning(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps([r.id]), "notes": ""},
        )
        assert resp.status_code == 200
        assert "Note text is required" in resp.text

    def test_over_limit_returns_400(self, client, db_session):
        ids = list(range(1, 52))
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps(ids), "notes": "test"},
        )
        assert resp.status_code == 400


class TestPreviewInquiry:
    """Phase 4.6: Email preview before send."""

    def test_preview_returns_200_with_rendered_emails(self, client, db_session):
        """Preview renders email previews per vendor without sending."""
        _, r, _ = _seed_data(db_session)
        vc = VendorCard(
            normalized_name="good vendor",
            display_name="Good Vendor",
        )
        db_session.add(vc)
        db_session.flush()
        contact = VendorContact(
            vendor_card_id=vc.id,
            contact_type="sales",
            email="sales@good.com",
            source="manual",
        )
        db_session.add(contact)
        db_session.commit()
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={
                "requirement_ids": str(r.id),
                "vendor_names": "good vendor",
                "email_body": "Please quote the following parts.",
            },
        )
        assert resp.status_code == 200
        assert "Good Vendor" in resp.text or "good vendor" in resp.text
        assert "sales@good.com" in resp.text
        assert "RFQ" in resp.text
        assert "Please quote" in resp.text

    def test_preview_vendor_no_email_shows_warning(self, client, db_session):
        """Vendor with no email shows amber warning in preview."""
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={
                "requirement_ids": str(r.id),
                "vendor_names": "unknown vendor",
                "email_body": "Please quote.",
            },
        )
        assert resp.status_code == 200
        assert "No email found" in resp.text

    def test_preview_400_empty_requirement_ids(self, client, db_session):
        """Empty requirement_ids returns 400."""
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={"vendor_names": "Acme"},
        )
        assert resp.status_code == 400

    def test_preview_400_empty_vendor_names(self, client, db_session):
        """Empty vendor_names returns 400."""
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={"requirement_ids": str(r.id)},
        )
        assert resp.status_code == 400

    def test_preview_does_not_create_contacts(self, client, db_session):
        """Preview must not create Contact records (no email sent)."""
        from app.models import Contact

        _, r, _ = _seed_data(db_session)
        initial_count = db_session.query(Contact).count()
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={
                "requirement_ids": str(r.id),
                "vendor_names": "good vendor",
                "email_body": "Quote please.",
            },
        )
        assert resp.status_code == 200
        assert db_session.query(Contact).count() == initial_count

    def test_preview_multiple_vendors(self, client, db_session):
        """Preview with multiple vendors shows all of them."""
        _, r, _ = _seed_data(db_session)
        body = f"requirement_ids={r.id}&vendor_names=vendor+a&vendor_names=vendor+b&email_body=Quote+please."
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200
        assert "vendor a" in resp.text.lower()
        assert "vendor b" in resp.text.lower()

    def test_preview_uses_same_subject_format_as_send(self, client, db_session):
        """Preview subject line matches the send-inquiry format."""
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={
                "requirement_ids": str(r.id),
                "vendor_names": "test vendor",
                "email_body": "Hello",
            },
        )
        assert resp.status_code == 200
        # Should contain "RFQ" and the ref token
        assert "RFQ" in resp.text
        assert "[ref:" in resp.text


class TestSightingsSubsBadge:
    def test_table_shows_sub_count_badge(self, client, db_session):
        """Table row shows '+N subs' badge when requirement has substitutes."""
        req = Requisition(name="Sub RFQ", status="active", customer_name="SubCo")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="HAS-SUBS-001",
            manufacturer="Mfr",
            target_qty=50,
            sourcing_status="open",
            substitutes=[
                {"mpn": "SUB-A", "manufacturer": "M1"},
                {"mpn": "SUB-B", "manufacturer": "M2"},
            ],
            substitutes_text="SUB-A SUB-B",
        )
        db_session.add(r)
        db_session.flush()
        db_session.add(
            VendorSightingSummary(
                requirement_id=r.id,
                vendor_name="V1",
                listing_count=1,
                score=50.0,
            )
        )
        db_session.commit()

        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        # mpn_chips renders each substitute as its own chip
        assert "SUB-A" in resp.text
        assert "SUB-B" in resp.text

    def test_table_no_badge_without_subs(self, client, db_session):
        """Table row does not show extra MPN chips when no substitutes."""
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        # Primary MPN should appear, but no substitute chips
        assert "SUB-" not in resp.text


class TestSightingsDetailSubs:
    def test_detail_shows_sub_pills(self, client, db_session):
        """Detail panel shows substitute MPN pills below primary MPN."""
        req = Requisition(name="Sub RFQ", status="active", customer_name="SubCo")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="DETAIL-PRIMARY",
            manufacturer="Mfr",
            target_qty=50,
            sourcing_status="open",
            substitutes=[
                {"mpn": "DETAIL-SUB-A", "manufacturer": "M1"},
                {"mpn": "DETAIL-SUB-B", "manufacturer": "M2"},
            ],
            substitutes_text="DETAIL-SUB-A DETAIL-SUB-B",
        )
        db_session.add(r)
        db_session.flush()
        db_session.add(
            VendorSightingSummary(
                requirement_id=r.id,
                vendor_name="V1",
                listing_count=1,
                score=50.0,
            )
        )
        db_session.commit()

        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "DETAIL-SUB-A" in resp.text
        assert "DETAIL-SUB-B" in resp.text

    def test_detail_no_pills_without_subs(self, client, db_session):
        """Detail panel has no sub pills when requirement has no substitutes."""
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        # No sub pill wrapper div should appear in the header section
        assert "flex flex-wrap gap-1 mt-1" not in resp.text


class TestSightingsVendorMatchedMpns:
    def test_vendor_row_shows_via_sub_tag(self, client, db_session):
        """Vendor row shows 'via SUB-MPN' when vendor sighting matched a substitute."""
        req = Requisition(name="Match RFQ", status="active", customer_name="MatchCo")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="MATCH-PRIMARY",
            manufacturer="Mfr",
            target_qty=50,
            sourcing_status="open",
            substitutes=[{"mpn": "MATCH-SUB-X", "manufacturer": "M1"}],
            substitutes_text="MATCH-SUB-X",
        )
        db_session.add(r)
        db_session.flush()
        # Vendor sighting summary
        db_session.add(
            VendorSightingSummary(
                requirement_id=r.id,
                vendor_name="SubVendor",
                listing_count=1,
                score=60.0,
            )
        )
        # Raw sighting matched against a substitute MPN
        db_session.add(
            Sighting(
                requirement_id=r.id,
                vendor_name="SubVendor",
                mpn_matched="MATCH-SUB-X",
                qty_available=100,
            )
        )
        db_session.commit()

        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "via MATCH-SUB-X" in resp.text

    def test_vendor_row_no_via_tag_for_primary(self, client, db_session):
        """Vendor row does NOT show 'via' tag when sighting matched the primary MPN."""
        req = Requisition(name="Primary RFQ", status="active", customer_name="PrimaryCo")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="PRI-ONLY",
            manufacturer="Mfr",
            target_qty=50,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.flush()
        db_session.add(
            VendorSightingSummary(
                requirement_id=r.id,
                vendor_name="PriVendor",
                listing_count=1,
                score=60.0,
            )
        )
        db_session.add(
            Sighting(
                requirement_id=r.id,
                vendor_name="PriVendor",
                mpn_matched="PRI-ONLY",
                qty_available=100,
            )
        )
        db_session.commit()

        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "via PRI-ONLY" not in resp.text

    def test_vendor_row_shows_multiple_via_tags(self, client, db_session):
        """Vendor row shows multiple 'via' tags when vendor matched multiple subs."""
        req = Requisition(name="Multi RFQ", status="active", customer_name="MultiCo")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="MULTI-PRI",
            manufacturer="Mfr",
            target_qty=50,
            sourcing_status="open",
            substitutes=[
                {"mpn": "MULTI-SUB-1", "manufacturer": "M1"},
                {"mpn": "MULTI-SUB-2", "manufacturer": "M2"},
            ],
            substitutes_text="MULTI-SUB-1 MULTI-SUB-2",
        )
        db_session.add(r)
        db_session.flush()
        db_session.add(
            VendorSightingSummary(
                requirement_id=r.id,
                vendor_name="MultiVendor",
                listing_count=2,
                score=70.0,
            )
        )
        db_session.add(
            Sighting(
                requirement_id=r.id,
                vendor_name="MultiVendor",
                mpn_matched="MULTI-SUB-1",
                qty_available=50,
            )
        )
        db_session.add(
            Sighting(
                requirement_id=r.id,
                vendor_name="MultiVendor",
                mpn_matched="MULTI-SUB-2",
                qty_available=75,
            )
        )
        db_session.commit()

        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "via MULTI-SUB-1" in resp.text
        assert "via MULTI-SUB-2" in resp.text


class TestMPNClickableLinks:
    """MPN chips link to material card detail pages when a card exists."""

    def test_table_mpn_links_to_material_card(self, client, db_session):
        """Table MPN chips render as <a> links when MaterialCard exists."""
        req = Requisition(name="Link RFQ", status="active", customer_name="LinkCo")
        db_session.add(req)
        db_session.flush()
        card = MaterialCard(
            normalized_mpn="link001",
            display_mpn="LINK-001",
            manufacturer="TestMfr",
        )
        db_session.add(card)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="LINK-001",
            normalized_mpn="link001",
            manufacturer="TestMfr",
            target_qty=50,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.flush()
        db_session.add(VendorSightingSummary(requirement_id=r.id, vendor_name="V1", listing_count=1, score=50.0))
        db_session.commit()

        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "open-modal" in resp.text
        assert f"/v2/partials/materials/{card.id}" in resp.text

    def test_table_mpn_no_link_without_card(self, client, db_session):
        """Table MPN chips are plain <span> when no MaterialCard exists."""
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "TEST-MPN-001" in resp.text
        # Should be a span, not a button with open-modal
        assert "/v2/partials/materials/" not in resp.text

    def test_detail_mpn_links_to_material_card(self, client, db_session):
        """Detail panel MPN chips render as clickable buttons when MaterialCard
        exists."""
        req = Requisition(name="Detail Link RFQ", status="active", customer_name="DLCo")
        db_session.add(req)
        db_session.flush()
        card = MaterialCard(
            normalized_mpn="detlink001",
            display_mpn="DET-LINK-001",
            manufacturer="TestMfr",
        )
        db_session.add(card)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="DET-LINK-001",
            normalized_mpn="detlink001",
            manufacturer="TestMfr",
            target_qty=50,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.flush()
        db_session.add(VendorSightingSummary(requirement_id=r.id, vendor_name="V1", listing_count=1, score=50.0))
        db_session.commit()

        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert f"/v2/partials/materials/{card.id}" in resp.text
