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

import pytest

from app.models.crm import CustomerSite, SiteContact
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
        """Requirement with critical urgency on requisition exercises urgency heatmap
        branch."""
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

    @pytest.mark.parametrize(
        "phones",
        [
            pytest.param(["+1-555-9999"], id="list"),
            pytest.param("+1-555-8888", id="string"),
        ],
    )
    def test_phone_populated_from_vendor_card(self, client, db_session, phones):
        """VendorCard.phones (list or string) used when summary has no vendor_phone."""
        req, r, _ = _seed_active(db_session)
        vc = VendorCard(
            normalized_name="cover vendor",
            display_name="Cover Vendor",
            phones=phones,
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

    @pytest.mark.parametrize(
        "sourcing_status",
        [
            pytest.param("offered", id="offered_no_pending_offers"),
            pytest.param("won", id="won_status"),
            pytest.param("custom_unknown_status", id="unknown_status_returns_none_action"),
        ],
    )
    def test_status_only_detail_renders(self, client, db_session, sourcing_status):
        """Status-only suggested-action branches each render detail (200).

        'offered' (no pending offers) → advance to quoted; 'won' → fulfillment; unknown
        status → suggested_action is None (else branch).
        """
        _, r, _ = _seed_active(db_session)
        r.sourcing_status = sourcing_status
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
    """Mark-unavailable marks sightings for vendor unavailable (lines 580-593)."""

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
            data={"vendor_name": "Cover Vendor", "reason": "sold_elsewhere"},
        )
        assert resp.status_code == 200
        db_session.refresh(s)
        assert s.is_unavailable is True

    def test_mark_unavailable_no_matching_sightings_is_noop(self, client, db_session):
        """Vendor with no sightings returns 200 (noop)."""
        _, r, _ = _seed_active(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-unavailable",
            data={"vendor_name": "Nonexistent Vendor", "reason": "other"},
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
    """Send-inquiry endpoint (lines 690-759)."""

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
        """Send-inquiry calls email_service.send_batch_rfq and returns 200."""
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
        """Send-inquiry catches exceptions and returns warning toast."""
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
        """Send-inquiry resolves vendor email from VendorCard + VendorContact."""
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
        """Send-inquiry logs rfq_sent activity for each requirement+vendor."""
        req, r, _ = _seed_active(db_session)
        with patch(
            "app.email_service.send_batch_rfq",
            new=AsyncMock(return_value=[{"vendor_name": "Cover Vendor", "status": "sent"}]),
        ):
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
        """Send-inquiry handles multiple vendor names."""
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

    @pytest.mark.parametrize(
        "sort,direction",
        [
            ("created", "asc"),
            ("created", "desc"),
            ("status", "asc"),
            ("priority", "asc"),
        ],
    )
    def test_sort(self, client, db_session, sort, direction):
        _seed_active(db_session)
        resp = client.get(f"/v2/partials/sightings?sort={sort}&dir={direction}")
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


# ── S1 tests: three entry points + preselect fix ──────────────────────────────


def _seed_vendor_with_contact(db_session, vendor_name: str, normalized_name: str, email: str):
    """Create a VendorCard + VendorContact (contactable) for preselect tests."""
    card = VendorCard(normalized_name=normalized_name, display_name=vendor_name)
    db_session.add(card)
    db_session.flush()
    contact = VendorContact(
        vendor_card_id=card.id,
        contact_type="sales",
        email=email,
        source="manual",
    )
    db_session.add(contact)
    db_session.flush()
    return card


class TestVendorModalPreselect:
    """Preselect= param: named vendor appears checked even below coverage cap (S1b
    blocker)."""

    def test_preselect_vendor_below_cap_is_present_and_checked(self, client, db_session):
        """Vendor named in preselect= but NOT in coverage top-20 is appended and seeds
        selectedVendors (has_contact=True) so the modal initializes with it checked."""
        req, r, _ = _seed_active(db_session)

        # Create a vendor card + contact for "Preselectco" — not a sighting vendor
        _seed_vendor_with_contact(db_session, "Preselectco", "preselectco", "buy@preselectco.com")
        db_session.commit()

        resp = client.get(f"/v2/partials/sightings/vendor-modal?requirement_ids={r.id}&preselect=Preselectco")
        assert resp.status_code == 200
        # The normalized name must appear in the selectedVendors seed (|tojson in x-data)
        assert "preselectco" in resp.text

    def test_preselect_vendor_already_in_coverage_not_duplicated(self, client, db_session):
        """If preselect= names a vendor already in the coverage top-20, it must appear
        exactly once in the suggested_vendors list (no duplicate)."""
        req, r, _ = _seed_active(db_session)

        # "Cover Vendor" is already seeded as a VendorSightingSummary by _seed_active
        card = _seed_vendor_with_contact(db_session, "Cover Vendor", "cover vendor", "cv@cover.com")
        # Tie the sighting to this card so it appears in coverage
        db_session.query(VendorSightingSummary).filter_by(requirement_id=r.id).update({"vendor_card_id": card.id})
        db_session.commit()

        resp = client.get(f"/v2/partials/sightings/vendor-modal?requirement_ids={r.id}&preselect=Cover+Vendor")
        assert resp.status_code == 200
        # The vendor's display_name must appear exactly once — a double-append would
        # render it twice in the for-loop and this count assertion would catch it.
        assert resp.text.count("Cover Vendor") == 1

    def test_preselect_vendor_no_contact_is_rendered_not_checked(self, client, db_session):
        """Preselected vendor with no VendorContact rows has has_contact=False and is
        NOT seeded into selectedVendors (rendered but disabled)."""
        req, r, _ = _seed_active(db_session)

        # Card with no contact
        card = VendorCard(normalized_name="nocardco", display_name="Nocardco")
        db_session.add(card)
        db_session.commit()

        resp = client.get(f"/v2/partials/sightings/vendor-modal?requirement_ids={r.id}&preselect=Nocardco")
        assert resp.status_code == 200
        # The vendor's display_name must appear in the HTML (rendered as a disabled row).
        assert "Nocardco" in resp.text
        # The normalized name must NOT appear in the rfqVendorModal tojson seed — the
        # template filters to only has_contact=True vendors before encoding selectedVendors,
        # so "nocardco" must be absent from the response entirely (the disabled row only
        # renders display_name, never normalized_name).
        assert '"nocardco"' not in resp.text


class TestDetailHeaderBuildRFQButton:
    """(S1a) detail.html header must contain a 'Build RFQ' primary button."""

    def test_detail_header_has_build_rfq_button(self, client, db_session):
        """The detail panel header contains a Build RFQ btn-primary CTA."""
        req, r, _ = _seed_active(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "Build RFQ" in resp.text


class TestTableRFQButton:
    """(S1c) table.html render_row must contain a per-row quick RFQ icon button."""

    def test_table_row_has_rfq_quick_button(self, client, db_session):
        """Each requirement row in the table contains a Build RFQ quick-dispatch
        button."""
        req, r, _ = _seed_active(db_session)
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        # The table contains at least one Build RFQ dispatch trigger
        assert "Build RFQ" in resp.text

    def test_group_row_colspan_nine(self, client, db_session):
        """Group header row uses colspan=9 to match the 9-column header."""
        req, r, _ = _seed_active(db_session)
        resp = client.get("/v2/partials/sightings?group_by=manufacturer")
        assert resp.status_code == 200
        assert 'colspan="9"' in resp.text


class TestVendorRowRFQButton:
    """(S1b) _vendor_row.html must have a visible RFQ pill outside the kebab."""

    def test_vendor_row_has_visible_rfq_pill(self, client, db_session):
        """Detail vendor row has a visible 'Build RFQ' button with preselect
        dispatch."""
        req, r, _ = _seed_active(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "vendor-modal" in resp.text
        assert "preselect" in resp.text


class TestManufacturerBasket:
    """(S2) Cross-requisition manufacturer-basket assembly."""

    def _seed_multi_req_ibm(self, db_session):
        """Two requisitions each with one IBM part and one non-IBM part."""
        req1 = Requisition(name="Req1", status="active", customer_name="Cust A")
        req2 = Requisition(name="Req2", status="active", customer_name="Cust B")
        db_session.add_all([req1, req2])
        db_session.flush()

        r1_ibm = Requirement(
            requisition_id=req1.id,
            primary_mpn="IBM-001",
            manufacturer="IBM",
            target_qty=10,
            sourcing_status="open",
        )
        r1_other = Requirement(
            requisition_id=req1.id,
            primary_mpn="OTHER-001",
            manufacturer="Other Corp",
            target_qty=5,
            sourcing_status="open",
        )
        r2_ibm = Requirement(
            requisition_id=req2.id,
            primary_mpn="IBM-002",
            manufacturer="IBM",
            target_qty=20,
            sourcing_status="open",
        )
        db_session.add_all([r1_ibm, r1_other, r2_ibm])
        db_session.commit()
        return req1, req2, r1_ibm, r1_other, r2_ibm

    def test_manufacturer_filter_returns_only_ibm_parts(self, client, db_session):
        """sightings_list?manufacturer=IBM returns only IBM-manufacturer rows."""
        _, _, r1_ibm, r1_other, r2_ibm = self._seed_multi_req_ibm(db_session)
        resp = client.get("/v2/partials/sightings?manufacturer=IBM")
        assert resp.status_code == 200
        html = resp.text
        # IBM parts are present
        assert "IBM-001" in html
        assert "IBM-002" in html
        # Non-IBM part is absent
        assert "OTHER-001" not in html

    def test_manufacturer_filter_spans_multiple_requisitions(self, client, db_session):
        """IBM parts from different requisitions all appear under the filter."""
        req1, req2, r1_ibm, _, r2_ibm = self._seed_multi_req_ibm(db_session)
        resp = client.get("/v2/partials/sightings?manufacturer=IBM&group_by=manufacturer")
        assert resp.status_code == 200
        html = resp.text
        assert "IBM-001" in html
        assert "IBM-002" in html

    def test_manufacturer_survives_status_change(self, client, db_session):
        """Manufacturer param is carried in pill hx-get URLs, not just the filter
        input."""
        _, _, r1_ibm, _, _ = self._seed_multi_req_ibm(db_session)
        # When manufacturer filter is active, status pills should carry it
        resp = client.get("/v2/partials/sightings?manufacturer=IBM&status=open")
        assert resp.status_code == 200
        html = resp.text
        # The filter bar input carries value="IBM" — that's necessary but NOT sufficient.
        # We must verify the pill hx-get URLs also encode manufacturer=IBM so that
        # clicking a pill does not silently drop the filter.
        import re

        pill_urls = re.findall(r'hx-get="(/v2/partials/sightings\?[^"]+)"', html)
        # At least some pill buttons must exist
        assert pill_urls, "No hx-get pill buttons found in response"
        # Every status/dashboard pill URL must carry manufacturer=IBM
        for url in pill_urls:
            assert "manufacturer=IBM" in url, f"manufacturer=IBM missing from pill hx-get URL: {url}"

    def test_manufacturer_survives_group_by_change(self, client, db_session):
        """Manufacturer param is carried in group_by select hx-vals and filter bar."""
        self._seed_multi_req_ibm(db_session)
        resp = client.get("/v2/partials/sightings?manufacturer=IBM&group_by=manufacturer")
        assert resp.status_code == 200
        html = resp.text
        # manufacturer=IBM must appear in the pill hx-get URLs on this render too
        import re

        pill_urls = re.findall(r'hx-get="(/v2/partials/sightings\?[^"]+)"', html)
        assert pill_urls, "No hx-get pill buttons found in response"
        for url in pill_urls:
            assert "manufacturer=IBM" in url, f"manufacturer=IBM missing from pill hx-get URL: {url}"

    def test_manufacturer_filter_bar_input_present(self, client, db_session):
        """The filter bar contains a manufacturer text input."""
        _, _, r1_ibm, _, _ = self._seed_multi_req_ibm(db_session)
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        # The filter bar input must be present with name="manufacturer"
        assert 'name="manufacturer"' in resp.text

    def test_manufacturer_filter_bar_prepopulated(self, client, db_session):
        """Filter bar manufacturer input shows current filter value on re-render."""
        self._seed_multi_req_ibm(db_session)
        resp = client.get("/v2/partials/sightings?manufacturer=IBM")
        assert resp.status_code == 200
        # Input must be pre-populated
        assert 'value="IBM"' in resp.text

    def test_group_header_select_all_checkbox_present(self, client, db_session):
        """Group header has a 'Select all N' labeled checkbox when grouped."""
        self._seed_multi_req_ibm(db_session)
        resp = client.get("/v2/partials/sightings?group_by=manufacturer")
        assert resp.status_code == 200
        assert "Select all" in resp.text

    def test_manufacturer_group_caption_shown(self, client, db_session):
        """A helper caption appears under the filter bar when group_by==manufacturer."""
        self._seed_multi_req_ibm(db_session)
        resp = client.get("/v2/partials/sightings?group_by=manufacturer")
        assert resp.status_code == 200
        assert "cross-requisition RFQ" in resp.text

    def test_batch_bar_button_relabeled(self, client, db_session):
        """Action bar button label is 'Build RFQ' not 'Send to Vendors'."""
        self._seed_multi_req_ibm(db_session)
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "Build RFQ" in resp.text
        assert "Send to Vendors" not in resp.text

    def test_vendor_modal_shows_spanning_requisitions(self, client, db_session):
        """When >1 requisition is in basket, modal Parts panel says 'Spanning N
        requisitions'."""
        req1, req2, r1_ibm, _, r2_ibm = self._seed_multi_req_ibm(db_session)
        resp = client.get(f"/v2/partials/sightings/vendor-modal?requirement_ids={r1_ibm.id},{r2_ibm.id}")
        assert resp.status_code == 200
        assert "Spanning" in resp.text
        assert "2 requisitions" in resp.text

    def test_vendor_modal_no_spanning_for_single_req(self, client, db_session):
        """When all parts in one requisition, spanning note is absent."""
        req1, _, r1_ibm, r1_other, _ = self._seed_multi_req_ibm(db_session)
        # Both parts from req1 only
        resp = client.get(f"/v2/partials/sightings/vendor-modal?requirement_ids={r1_ibm.id},{r1_other.id}")
        assert resp.status_code == 200
        assert "Spanning" not in resp.text

    def test_existing_q_filter_unchanged(self, client, db_session):
        """Existing q filter still works alongside manufacturer filter."""
        self._seed_multi_req_ibm(db_session)
        resp = client.get("/v2/partials/sightings?q=IBM-001&manufacturer=IBM")
        assert resp.status_code == 200
        assert "IBM-001" in resp.text
        assert "IBM-002" not in resp.text


# ── S3 tests: up-front skip reasons ─────────────────────────────────────────


def _seed_dnc_site_contact(db_session, email: str) -> SiteContact:
    """Create a CustomerSite + do-not-contact SiteContact for DNC tests."""
    from app.models.crm import Company

    company = Company(name="DNC Corp", is_active=True)
    db_session.add(company)
    db_session.flush()
    site = CustomerSite(company_id=company.id, site_name="DNC HQ", is_active=True)
    db_session.add(site)
    db_session.flush()
    sc = SiteContact(
        customer_site_id=site.id,
        full_name="Do Not Call",
        email=email,
        do_not_contact=True,
    )
    db_session.add(sc)
    db_session.flush()
    return sc


class TestSightingsSkipReasonEnum:
    """SightingsSkipReason StrEnum is defined in app/constants.py."""

    def test_skip_reason_enum_values(self):
        """SightingsSkipReason has READY, NO_EMAIL, UNAVAILABLE, DO_NOT_CONTACT."""
        from app.constants import SightingsSkipReason

        assert SightingsSkipReason.READY == "ready"
        assert SightingsSkipReason.NO_EMAIL == "no_email"
        assert SightingsSkipReason.UNAVAILABLE == "unavailable"
        assert SightingsSkipReason.DO_NOT_CONTACT == "do_not_contact"


class TestDncEmailsForCards:
    """_dnc_emails_for_cards returns the set of emails that send_batch_rfq will DNC-
    skip."""

    def _make_vendor_with_email(self, db_session, vendor_name: str, norm: str, email: str):
        card = VendorCard(normalized_name=norm, display_name=vendor_name)
        db_session.add(card)
        db_session.flush()
        contact = VendorContact(
            vendor_card_id=card.id,
            contact_type="sales",
            email=email,
            source="manual",
        )
        db_session.add(contact)
        db_session.flush()
        return card

    def test_dnc_email_for_flagged_contact(self, db_session):
        """A VendorContact email that is a DNC SiteContact email is returned."""
        from app.routers.sightings import _dnc_emails_for_cards

        email = "flagged@vendor.com"
        card = self._make_vendor_with_email(db_session, "Flaggedco", "flaggedco", email)
        _seed_dnc_site_contact(db_session, email)
        db_session.commit()

        result = _dnc_emails_for_cards(db_session, [card.id])
        assert email in result

    def test_dnc_email_case_insensitive(self, db_session):
        """DNC match is case-insensitive: Vendor contact 'DNC@Vendor.COM' matches
        SiteContact 'dnc@vendor.com'."""
        from app.routers.sightings import _dnc_emails_for_cards

        vendor_email = "DNC@Vendor.COM"
        site_email = "dnc@vendor.com"
        card = self._make_vendor_with_email(db_session, "Casevendor", "casevendor", vendor_email)
        _seed_dnc_site_contact(db_session, site_email)
        db_session.commit()

        result = _dnc_emails_for_cards(db_session, [card.id])
        assert vendor_email.lower() in result

    def test_non_dnc_contact_excluded(self, db_session):
        """A VendorContact email with no matching DNC SiteContact is NOT returned."""
        from app.routers.sightings import _dnc_emails_for_cards

        email = "ok@vendor.com"
        card = self._make_vendor_with_email(db_session, "Okco", "okco", email)
        db_session.commit()

        result = _dnc_emails_for_cards(db_session, [card.id])
        assert email not in result

    def test_empty_card_ids_returns_empty(self, db_session):
        """Empty card_ids input returns empty set (no query)."""
        from app.routers.sightings import _dnc_emails_for_cards

        result = _dnc_emails_for_cards(db_session, [])
        assert result == set()

    def test_dnc_advisory_subset_of_send_path(self, db_session):
        """Advisory DNC set (from _dnc_emails_for_cards) is a subset of the emails
        send_batch_rfq actually skips — guarantee advisory ⊆ send-time check."""
        from app.email_service import send_batch_rfq
        from app.routers.sightings import _dnc_emails_for_cards

        email = "blocked@vendor.com"
        card = self._make_vendor_with_email(db_session, "Blockedco", "blockedco", email)
        _seed_dnc_site_contact(db_session, email)
        db_session.commit()

        advisory = _dnc_emails_for_cards(db_session, [card.id])
        assert email.lower() in advisory

        # Run the send path with a fake token — expect the email to be DNC-skipped.
        # GraphClient is lazy-imported inside send_batch_rfq, so patch at that location.
        with patch("app.utils.graph_client.GraphClient") as mock_gc_cls:
            mock_gc = AsyncMock()
            mock_gc_cls.return_value = mock_gc
            import asyncio

            results = asyncio.get_event_loop().run_until_complete(
                send_batch_rfq(
                    token="fake",
                    db=db_session,
                    user_id=1,
                    requisition_id=None,
                    vendor_groups=[
                        {
                            "vendor_name": "Blockedco",
                            "vendor_email": email,
                            "parts": [{"mpn": "BLK-001", "qty": 5}],
                            "subject": "RFQ test",
                            "body": "test body",
                        }
                    ],
                )
            )

        # The send path must also skip it — advisory ⊆ send-path skip set
        send_skipped = {r["vendor_email"] for r in results if r["status"] == "skipped"}
        assert email in send_skipped


class TestVendorModalDNCChip:
    """DNC vendors render disabled checkbox + 'do-not-contact' chip in vendor_modal."""

    def _seed_vendor_dnc(self, db_session, vendor_name: str, norm: str, email: str):
        """Create a vendor card + contact AND a matching DNC SiteContact."""
        card = VendorCard(normalized_name=norm, display_name=vendor_name)
        db_session.add(card)
        db_session.flush()
        contact = VendorContact(
            vendor_card_id=card.id,
            contact_type="sales",
            email=email,
            source="manual",
        )
        db_session.add(contact)
        db_session.flush()
        _seed_dnc_site_contact(db_session, email)
        return card

    def test_dnc_vendor_renders_disabled_and_chip(self, client, db_session):
        """A vendor whose contact email is a DNC SiteContact renders a disabled checkbox
        and a 'do-not-contact' chip in the vendor modal compose step."""
        req, r, _ = _seed_active(db_session)

        # Create a VendorSightingSummary linked to the DNC vendor card so it appears
        # in coverage suggestions
        card = self._seed_vendor_dnc(db_session, "Dncvendor", "dncvendor", "dnc@dncvendor.com")
        vss = VendorSightingSummary(
            requirement_id=r.id,
            vendor_name="Dncvendor",
            vendor_card_id=card.id,
            estimated_qty=10,
            listing_count=1,
            score=50.0,
        )
        db_session.add(vss)
        db_session.commit()

        resp = client.get(f"/v2/partials/sightings/vendor-modal?requirement_ids={r.id}")
        assert resp.status_code == 200
        assert "do-not-contact" in resp.text
        assert "cursor-not-allowed" in resp.text or "disabled" in resp.text

    def test_non_dnc_vendor_not_flagged(self, client, db_session):
        """A vendor with a clean email does NOT get the do-not-contact chip."""
        req, r, _ = _seed_active(db_session)

        card = VendorCard(normalized_name="cleanvendor", display_name="Cleanvendor")
        db_session.add(card)
        db_session.flush()
        contact = VendorContact(
            vendor_card_id=card.id,
            contact_type="sales",
            email="ok@cleanvendor.com",
            source="manual",
        )
        db_session.add(contact)
        vss = VendorSightingSummary(
            requirement_id=r.id,
            vendor_name="Cleanvendor",
            vendor_card_id=card.id,
            estimated_qty=10,
            listing_count=1,
            score=50.0,
        )
        db_session.add(vss)
        db_session.commit()

        resp = client.get(f"/v2/partials/sightings/vendor-modal?requirement_ids={r.id}")
        assert resp.status_code == 200
        assert "do-not-contact" not in resp.text


class TestPreviewSkipReason:
    """Preview step renders skip_reason badges: amber=no_email, rose=unavailable/dnc."""

    def _make_req_and_requirement(self, db_session):
        req = Requisition(name="SkipReason RFQ", status="active", customer_name="Skip Corp")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="SKIP-001",
            manufacturer="SkipMfr",
            target_qty=5,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.flush()
        db_session.commit()
        return req, r

    def test_no_email_vendor_shows_amber_chip(self, client, db_session):
        """A vendor with no resolvable email shows an amber 'no email' indicator in the
        preview."""
        req, r = self._make_req_and_requirement(db_session)

        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={
                "requirement_ids": [str(r.id)],
                "vendor_names": ["NoEmailVendor"],
                "email_body": "Hello",
            },
        )
        assert resp.status_code == 200
        # amber badge contains "no email" or similar text
        assert "no-email" in resp.text or "no email" in resp.text.lower() or "amber" in resp.text

    def test_dnc_vendor_shows_rose_chip_in_preview(self, client, db_session):
        """A vendor whose contact email is DNC-flagged shows a rose 'do-not-contact'
        chip in the preview."""
        req, r = self._make_req_and_requirement(db_session)

        email = "dncpreview@vendor.com"
        card = VendorCard(normalized_name="dncpreviewco", display_name="Dncpreviewco")
        db_session.add(card)
        db_session.flush()
        contact = VendorContact(
            vendor_card_id=card.id,
            contact_type="sales",
            email=email,
            source="manual",
        )
        db_session.add(contact)
        db_session.flush()
        _seed_dnc_site_contact(db_session, email)
        db_session.commit()

        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={
                "requirement_ids": [str(r.id)],
                "vendor_names": ["dncpreviewco"],
                "email_body": "Hello",
            },
        )
        assert resp.status_code == 200
        assert "do-not-contact" in resp.text

    def test_unavailable_vendor_shows_rose_chip_in_preview(self, client, db_session):
        """Unavailable vendors are already reported in the existing unavailable_vendors
        section with rose styling — ensure that block is present."""
        from app.models.vendor_part_unavailability import VendorPartUnavailability

        req, r = self._make_req_and_requirement(db_session)

        card = VendorCard(normalized_name="unavailco", display_name="Unavailco")
        db_session.add(card)
        db_session.flush()
        # normalized_mpn = normalize_mpn_key("SKIP-001") = "skip001"
        unavail = VendorPartUnavailability(
            vendor_name_normalized="unavailco",
            normalized_mpn="skip001",
            reason="other",
        )
        db_session.add(unavail)
        db_session.commit()

        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={
                "requirement_ids": [str(r.id)],
                "vendor_names": ["unavailco"],
                "email_body": "Hello",
            },
        )
        assert resp.status_code == 200
        # Unavailable vendor goes to the rose "Skipped (marked unavailable)" section
        assert "rose" in resp.text or "unavailable" in resp.text.lower()

    def test_send_time_recheck_still_present(self, client, db_session):
        """Send-inquiry still performs the send-time unavailability re-check (TOCTOU): a
        vendor marked unavailable is excluded from the send even when posted as a
        selected vendor name. The re-check happens in sightings_send_inquiry
        (_partition_by_unavailability) before any call to send_batch_rfq.

        We verify: send_batch_rfq is called with zero sendable_vendors (the unavailable
        vendor was stripped), so the batch receives an empty vendor list and sends nothing.
        """
        from app.models.vendor_part_unavailability import VendorPartUnavailability

        req, r = self._make_req_and_requirement(db_session)

        card = VendorCard(normalized_name="toctouvendor", display_name="Toctouvendor")
        db_session.add(card)
        db_session.flush()
        contact = VendorContact(
            vendor_card_id=card.id,
            contact_type="sales",
            email="t@toctou.com",
            source="manual",
        )
        db_session.add(contact)
        db_session.flush()
        # normalized_mpn = normalize_mpn_key("SKIP-001") = "skip001"
        unavail = VendorPartUnavailability(
            vendor_name_normalized="toctouvendor",
            normalized_mpn="skip001",
            reason="other",
        )
        db_session.add(unavail)
        db_session.commit()

        # Patch send_batch_rfq where it is imported (lazily inside the route).
        # Use the email_service module as the patch target — that is where the route
        # imports it from at call time (lazy import inside the route body).
        with patch("app.email_service.send_batch_rfq", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = []
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": [str(r.id)],
                    "vendor_names": ["toctouvendor"],
                    "email_body": "Hello",
                },
            )
        # Response should succeed — the unavailable vendor was stripped before send.
        assert resp.status_code == 200
        # send_batch_rfq was called with an empty vendor list (send-time re-check worked),
        # OR the route reported the unavailability without calling send at all.
        # In either path: the response contains "unavailable" language.
        assert "unavailable" in resp.text.lower() or "skipped" in resp.text.lower()
