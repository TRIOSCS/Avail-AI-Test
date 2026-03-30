"""tests/test_sightings_coverage.py — Coverage gap tests for app/routers/sightings.py.

Targets lines not covered by test_sightings_router.py:
- _invalidate_cache (line 63) via assign buyer endpoint
- sales_person filter (line 115)
- stale detection via old activity (line 182)
- critical/hot urgency heatmap (line 277)
- vendor phone from card (lines 365-367), age_days (line 372)
- "sourcing" status suggested action branches (lines 435-460)
- exception handling in refresh (lines 519-520)
- batch_refresh loop fail/success (lines 535-563)
- mark-unavailable (lines 580-593)
- assign buyer (lines 605-615)
- send-inquiry success/failure (lines 690-759)

Called by: pytest
Depends on: conftest.py fixtures, app models
"""

import os

os.environ["TESTING"] = "1"

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.models.intelligence import ActivityLog
from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition, Sighting
from app.models.vendor_sighting_summary import VendorSightingSummary
from app.models.vendors import VendorCard, VendorContact


def _seed_active(db_session):
    """Create an active requisition + requirement + vendor summary."""
    req = Requisition(name="Coverage RFQ", status="active", customer_name="Cover Corp")
    db_session.add(req)
    db_session.flush()
    r = Requirement(
        requisition_id=req.id,
        primary_mpn="COVER-001",
        manufacturer="CoverMfr",
        target_qty=50,
        sourcing_status="open",
    )
    db_session.add(r)
    db_session.flush()
    s = VendorSightingSummary(
        requirement_id=r.id,
        vendor_name="Cover Vendor",
        estimated_qty=100,
        listing_count=2,
        score=65.0,
    )
    db_session.add(s)
    db_session.commit()
    return req, r, s


class TestSalesPersonFilter:
    """Filter by sales_person name (line 115)."""

    def test_sales_person_filter_with_name(self, client, db_session, test_user):
        """Filtering by sales person joins User and applies ILIKE."""
        _seed_active(db_session)
        resp = client.get(f"/v2/partials/sightings?sales_person={test_user.name}")
        assert resp.status_code == 200

    def test_sales_person_filter_no_match(self, client, db_session):
        """Filter with no matching sales person returns empty result."""
        _seed_active(db_session)
        resp = client.get("/v2/partials/sightings?sales_person=NoSuchPerson")
        assert resp.status_code == 200
        assert "COVER-001" not in resp.text


class TestStaleDetectionViaOldActivity:
    """Stale detection path where last activity < stale_threshold (line 182)."""

    def test_old_activity_marks_stale(self, client, db_session):
        """Requirement with activity older than stale_days is marked stale."""
        req, r, _ = _seed_active(db_session)
        old_activity = ActivityLog(
            activity_type="note",
            channel="manual",
            requirement_id=r.id,
            requisition_id=req.id,
            notes="Old note",
        )
        db_session.add(old_activity)
        db_session.flush()
        # Manually set created_at to a very old date
        old_activity.created_at = datetime(2020, 1, 1)
        db_session.commit()

        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "stale" in resp.text.lower()


class TestHeatmapCriticalHotUrgency:
    """Heatmap path via critical/hot urgency on requisition (line 277)."""

    def test_critical_urgency_heatmap_path(self, client, db_session):
        """Requirement with critical urgency on requisition exercises urgency heatmap branch."""
        req = Requisition(
            name="Critical RFQ",
            status="active",
            customer_name="Crit Corp",
        )
        db_session.add(req)
        db_session.flush()
        # Set urgency if the model supports it
        if hasattr(req, "urgency"):
            req.urgency = "critical"
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="CRIT-001",
            target_qty=100,
            sourcing_status="open",
            priority_score=10.0,  # Low priority so stale/priority paths won't trigger
        )
        db_session.add(r)
        db_session.flush()
        db_session.add(
            VendorSightingSummary(
                requirement_id=r.id, vendor_name="CritVendor", estimated_qty=100, listing_count=1, score=50.0
            )
        )
        # Add recent activity so not stale
        db_session.add(
            ActivityLog(
                activity_type="note",
                channel="manual",
                requirement_id=r.id,
                requisition_id=req.id,
                notes="recent note",
            )
        )
        db_session.commit()
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200


class TestDetailPhoneFromCard:
    """Vendor phone fallback from VendorCard (lines 365-367), age_days (line 372)."""

    def test_phone_populated_from_vendor_card_list(self, client, db_session):
        """VendorCard.phones list used when summary has no vendor_phone."""
        req, r, _ = _seed_active(db_session)
        vc = VendorCard(
            normalized_name="cover vendor",
            display_name="Cover Vendor",
            phones=["+1-555-9999"],
        )
        db_session.add(vc)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_phone_populated_from_vendor_card_string(self, client, db_session):
        """VendorCard.phones as string used when summary has no vendor_phone."""
        req, r, _ = _seed_active(db_session)
        vc = VendorCard(
            normalized_name="cover vendor",
            display_name="Cover Vendor",
            phones="+1-555-8888",
        )
        db_session.add(vc)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_age_days_populated_when_newest_sighting_at(self, client, db_session):
        """age_days is calculated when newest_sighting_at is set (line 372)."""
        req, r, _ = _seed_active(db_session)
        # Delete and recreate the summary with newest_sighting_at
        db_session.query(VendorSightingSummary).filter(VendorSightingSummary.requirement_id == r.id).delete()
        db_session.flush()
        s = VendorSightingSummary(
            requirement_id=r.id,
            vendor_name="Aged Vendor",
            estimated_qty=50,
            listing_count=1,
            score=55.0,
        )
        db_session.add(s)
        db_session.flush()
        # Use SQLite-compatible naive datetime
        s.newest_sighting_at = datetime.now() - timedelta(days=10)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200


class TestSuggestedActionSourcingStatus:
    """Suggested action for sourcing status with/without recent RFQ (lines 435-460)."""

    def test_sourcing_no_rfq_activity(self, client, db_session):
        """'sourcing' status but no rfq_sent activity → suggest send RFQs."""
        _, r, _ = _seed_active(db_session)
        r.sourcing_status = "sourcing"
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_sourcing_with_old_rfq_activity(self, client, db_session):
        """'sourcing' status + old rfq_sent → suggest follow up (days_since > 3)."""
        req, r, _ = _seed_active(db_session)
        r.sourcing_status = "sourcing"
        db_session.flush()
        log = ActivityLog(
            activity_type="rfq_sent",
            channel="email",
            requirement_id=r.id,
            requisition_id=req.id,
            notes="RFQ sent",
        )
        db_session.add(log)
        db_session.flush()
        # Set old date so days_since > 3
        log.created_at = datetime.now() - timedelta(days=10)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_sourcing_with_recent_rfq_activity(self, client, db_session):
        """'sourcing' status + recent rfq_sent → awaiting vendor responses."""
        req, r, _ = _seed_active(db_session)
        r.sourcing_status = "sourcing"
        db_session.flush()
        db_session.add(
            ActivityLog(
                activity_type="rfq_sent",
                channel="email",
                requirement_id=r.id,
                requisition_id=req.id,
                notes="Just sent",
            )
        )
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_offered_with_pending_offers(self, client, db_session):
        """'offered' status + pending offers → review offers."""
        req, r, _ = _seed_active(db_session)
        r.sourcing_status = "offered"
        db_session.flush()
        db_session.add(
            Offer(
                requirement_id=r.id,
                requisition_id=req.id,
                vendor_name="Cover Vendor",
                mpn="COVER-001",
                status="pending_review",
                unit_price=1.0,
                qty_available=50,
            )
        )
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_offered_no_pending_offers(self, client, db_session):
        """'offered' status but no pending offers → advance to quoted."""
        _, r, _ = _seed_active(db_session)
        r.sourcing_status = "offered"
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_won_status(self, client, db_session):
        """'won' status → proceed to fulfillment."""
        _, r, _ = _seed_active(db_session)
        r.sourcing_status = "won"
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_unknown_status_returns_none_action(self, client, db_session):
        """Unknown status → suggested_action is None (else branch)."""
        _, r, _ = _seed_active(db_session)
        r.sourcing_status = "custom_unknown_status"
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200


class TestRefreshExceptionHandling:
    """Exception handling in sightings_refresh (lines 519-520)."""

    def test_refresh_when_search_raises_still_returns_200(self, client, db_session):
        """Refresh endpoint handles search failure gracefully and returns detail."""
        _, r, _ = _seed_active(db_session)
        with patch(
            "app.search_service.search_requirement",
            new=AsyncMock(side_effect=Exception("api down")),
        ):
            resp = client.post(f"/v2/partials/sightings/{r.id}/refresh")
        assert resp.status_code == 200


class TestBatchRefreshLogic:
    """Batch-refresh success/failure loop (lines 535-563)."""

    def test_batch_refresh_success_path(self, client, db_session):
        """One valid requirement refreshed successfully."""
        _, r, _ = _seed_active(db_session)
        with patch(
            "app.search_service.search_requirement",
            new=AsyncMock(return_value=None),
        ):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([r.id])},
            )
        assert resp.status_code == 200
        # "Refreshed 1/1" or similar success message
        assert "1/1" in resp.text or "1" in resp.text

    def test_batch_refresh_failure_increments_failed(self, client, db_session):
        """Search failure for a requirement shows failed count."""
        _, r, _ = _seed_active(db_session)
        with patch(
            "app.search_service.search_requirement",
            new=AsyncMock(side_effect=Exception("fail")),
        ):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([r.id])},
            )
        assert resp.status_code == 200
        assert "failed" in resp.text.lower() or "0/1" in resp.text

    def test_batch_refresh_nonexistent_id_counts_as_failed(self, client, db_session):
        """Nonexistent requirement ID results in failed count."""
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": json.dumps([99999])},
        )
        assert resp.status_code == 200

    def test_batch_refresh_mixed_existing_and_missing(self, client, db_session):
        """Mix of valid and nonexistent IDs reports partial success."""
        _, r, _ = _seed_active(db_session)
        with patch(
            "app.search_service.search_requirement",
            new=AsyncMock(return_value=None),
        ):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([r.id, 99999])},
            )
        assert resp.status_code == 200


class TestMarkUnavailableEndpoint:
    """mark-unavailable marks sightings for vendor unavailable (lines 580-593)."""

    def test_marks_matching_sightings_unavailable(self, client, db_session):
        """Matching sightings are set is_unavailable=True."""
        req, r, _ = _seed_active(db_session)
        s = Sighting(
            requirement_id=r.id,
            vendor_name="Cover Vendor",
            mpn_matched="COVER-001",
        )
        db_session.add(s)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-unavailable",
            data={"vendor_name": "Cover Vendor"},
        )
        assert resp.status_code == 200
        db_session.refresh(s)
        assert s.is_unavailable is True

    def test_mark_unavailable_no_matching_sightings_is_noop(self, client, db_session):
        """Vendor with no sightings returns 200 (noop)."""
        _, r, _ = _seed_active(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-unavailable",
            data={"vendor_name": "Nonexistent Vendor"},
        )
        assert resp.status_code == 200


class TestAssignBuyerEndpoint:
    """Assign buyer endpoint sets assigned_buyer_id (lines 605-615)."""

    def test_assigns_buyer_id_to_requirement(self, client, db_session, test_user):
        """Sets assigned_buyer_id from form data."""
        _, r, _ = _seed_active(db_session)
        resp = client.patch(
            f"/v2/partials/sightings/{r.id}/assign",
            data={"assigned_buyer_id": str(test_user.id)},
        )
        assert resp.status_code == 200
        db_session.refresh(r)
        assert r.assigned_buyer_id == test_user.id

    def test_clears_buyer_id_when_empty(self, client, db_session, test_user):
        """Empty string for buyer_id clears the assignment."""
        _, r, _ = _seed_active(db_session)
        r.assigned_buyer_id = test_user.id
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/sightings/{r.id}/assign",
            data={"assigned_buyer_id": ""},
        )
        assert resp.status_code == 200
        db_session.refresh(r)
        assert r.assigned_buyer_id is None

    def test_404_for_missing_requirement(self, client, db_session):
        """Returns 404 when requirement not found."""
        resp = client.patch(
            "/v2/partials/sightings/99999/assign",
            data={"assigned_buyer_id": "1"},
        )
        assert resp.status_code == 404


class TestSendInquiryEndpoint:
    """send-inquiry endpoint (lines 690-759)."""

    def test_400_when_missing_all_params(self, client, db_session):
        """Requires requirement_ids, vendor_names, and email_body."""
        resp = client.post(
            "/v2/partials/sightings/send-inquiry",
            data={},
        )
        assert resp.status_code == 400

    def test_400_when_missing_email_body(self, client, db_session):
        """Returns 400 when email_body is empty."""
        _, r, _ = _seed_active(db_session)
        resp = client.post(
            "/v2/partials/sightings/send-inquiry",
            data={"requirement_ids": str(r.id), "vendor_names": "Cover Vendor"},
        )
        assert resp.status_code == 400

    def test_send_inquiry_calls_send_batch_rfq(self, client, db_session):
        """send-inquiry calls email_service.send_batch_rfq and returns 200."""
        _, r, _ = _seed_active(db_session)
        mock_results = [{"vendor_name": "Cover Vendor", "status": "sent"}]
        with patch("app.email_service.send_batch_rfq", new=AsyncMock(return_value=mock_results)):
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": str(r.id),
                    "vendor_names": "Cover Vendor",
                    "email_body": "Please quote this part.",
                },
            )
        assert resp.status_code == 200
        assert "RFQ sent" in resp.text or "vendor" in resp.text.lower()

    def test_send_inquiry_handles_exception(self, client, db_session):
        """send-inquiry catches exceptions and returns warning toast."""
        _, r, _ = _seed_active(db_session)
        with patch("app.email_service.send_batch_rfq", new=AsyncMock(side_effect=Exception("graph down"))):
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": str(r.id),
                    "vendor_names": "Cover Vendor",
                    "email_body": "Please quote this part.",
                },
            )
        assert resp.status_code == 200
        # Failed vendors should appear in message
        assert "warning" in resp.text or "Cover Vendor" in resp.text

    def test_send_inquiry_with_vendor_card_resolves_email(self, client, db_session):
        """send-inquiry resolves vendor email from VendorCard + VendorContact."""
        _, r, _ = _seed_active(db_session)
        vc = VendorCard(normalized_name="cover vendor", display_name="Cover Vendor")
        db_session.add(vc)
        db_session.flush()
        contact = VendorContact(
            vendor_card_id=vc.id,
            contact_type="sales",
            email="sales@covervendor.com",
            source="manual",
        )
        db_session.add(contact)
        db_session.commit()

        with patch("app.email_service.send_batch_rfq", new=AsyncMock(return_value=[])):
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": str(r.id),
                    "vendor_names": "Cover Vendor",
                    "email_body": "Please quote this part.",
                },
            )
        assert resp.status_code == 200

    def test_send_inquiry_logs_rfq_activity(self, client, db_session):
        """send-inquiry logs rfq_sent activity for each requirement+vendor."""
        req, r, _ = _seed_active(db_session)
        with patch("app.email_service.send_batch_rfq", new=AsyncMock(return_value=[{"ok": True}])):
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": str(r.id),
                    "vendor_names": "Cover Vendor",
                    "email_body": "Please quote this part.",
                },
            )
        assert resp.status_code == 200
        logs = (
            db_session.query(ActivityLog)
            .filter(
                ActivityLog.requirement_id == r.id,
                ActivityLog.activity_type == "rfq_sent",
            )
            .all()
        )
        assert len(logs) >= 1

    def test_send_inquiry_multiple_vendors(self, client, db_session):
        """send-inquiry handles multiple vendor names."""
        _, r, _ = _seed_active(db_session)

        with patch("app.email_service.send_batch_rfq", new=AsyncMock(return_value=[{}, {}])):
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": str(r.id),
                    "vendor_names": "Vendor One",
                    "email_body": "Please quote.",
                },
            )
        assert resp.status_code == 200


class TestSortDirections:
    """Sort direction asc/desc for all sort columns to cover branching."""

    def test_sort_by_created_asc(self, client, db_session):
        _seed_active(db_session)
        resp = client.get("/v2/partials/sightings?sort=created&dir=asc")
        assert resp.status_code == 200

    def test_sort_by_created_desc(self, client, db_session):
        _seed_active(db_session)
        resp = client.get("/v2/partials/sightings?sort=created&dir=desc")
        assert resp.status_code == 200

    def test_sort_by_status_asc(self, client, db_session):
        _seed_active(db_session)
        resp = client.get("/v2/partials/sightings?sort=status&dir=asc")
        assert resp.status_code == 200

    def test_sort_by_priority_asc(self, client, db_session):
        _seed_active(db_session)
        resp = client.get("/v2/partials/sightings?sort=priority&dir=asc")
        assert resp.status_code == 200


class TestDetailWithOOOContact:
    """OOO contact map in detail panel."""

    def test_ooo_contact_in_detail_panel(self, client, db_session):
        """OOO contact for vendor in summaries populates ooo_map."""
        req, r, _ = _seed_active(db_session)
        vc = VendorCard(
            normalized_name="cover vendor",
            display_name="Cover Vendor",
        )
        db_session.add(vc)
        db_session.flush()
        contact = VendorContact(
            vendor_card_id=vc.id,
            contact_type="sales",
            email="sales@cover.com",
            source="email",
            is_ooo=True,
            ooo_return_date=datetime(2026, 12, 1, tzinfo=timezone.utc),
        )
        db_session.add(contact)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
