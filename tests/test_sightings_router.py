"""Tests for sightings page router endpoints.

Called by: pytest
Depends on: conftest.py fixtures, app models, sighting_status service
"""

import json
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from app.constants import ActivityType, UnavailabilityReason
from app.models.intelligence import ActivityLog, MaterialCard
from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition, Sighting
from app.models.vendor_part_unavailability import VendorPartUnavailability
from app.models.vendor_sighting_summary import VendorSightingSummary
from app.models.vendors import VendorCard, VendorContact


class _XDataExtractor(HTMLParser):
    """Collect every x-data attribute value AS THE BROWSER TOKENIZES IT.

    Faithfully reproduces HTML attribute-value termination, so a literal double-quote
    injected by |tojson into a double-quoted x-data attribute shows up here as a
    TRUNCATED value — exactly what breaks Alpine init in the browser.
    """

    def __init__(self):
        super().__init__()
        self.xdata_values: list[str] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name == "x-data" and value:
                self.xdata_values.append(value)


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


def _unav_record(
    db_session,
    vendor_norm="good vendor",
    key="testmpn001",
    reason="sold_elsewhere",
    age_days=0,
    qty_at_mark=None,
    note=None,
    requirement_id=None,
):
    """Durable VendorPartUnavailability record; age_days backdates created_at so the
    temporal-policy window state (active vs expired) is controlled per test."""
    rec = VendorPartUnavailability(
        vendor_name_normalized=vendor_norm,
        normalized_mpn=key,
        reason=reason,
        note=note,
        created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
        qty_at_mark=qty_at_mark,
        requirement_id=requirement_id,
    )
    db_session.add(rec)
    db_session.commit()
    return rec


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

    def test_renders_tab_structure(self, client, db_session):
        """Right pane renders an Alpine tab shell with Vendors (default), Offers, and
        Activity tabs."""
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        body = resp.text
        # Alpine tab state initialised, defaulting to vendors
        assert "x-data=\"{ activeTab: 'vendors' }\"" in body
        # All three tab labels render as buttons
        assert "activeTab = 'vendors'" in body
        assert "activeTab = 'offers'" in body
        assert "activeTab = 'activity'" in body
        # Tab labels render inside the nav buttons (whitespace-tolerant; class string is unique to tab buttons).
        assert re.search(r'border-b-2 transition-colors whitespace-nowrap">\s*Vendors\s*</button>', body)
        assert re.search(r'border-b-2 transition-colors whitespace-nowrap">\s*Offers\s*</button>', body)
        assert re.search(r'border-b-2 transition-colors whitespace-nowrap">\s*Activity\s*</button>', body)
        # Vendors panel still shows vendor data; offers + activity panels host their sections
        assert "Good Vendor" in body
        assert 'id="sightings-offers-panel"' in body
        assert 'id="sightings-activity-section"' in body


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
    def test_marks_sightings_unavailable_with_reason_and_note(self, client, db_session):
        """Mark with a validated reason + note writes the durable record, flags the
        sighting, and the re-rendered detail shows the reason label on the row."""
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
            data={
                "vendor_name": "Good Vendor",
                "reason": "sold_elsewhere",
                "note": "Sold the lot last week",
            },
        )
        assert resp.status_code == 200
        assert "Vendor sold them" in resp.text  # reason label renders on the row
        db_session.expire_all()
        assert db_session.get(Sighting, s.id).is_unavailable is True
        rec = (
            db_session.query(VendorPartUnavailability)
            .filter_by(vendor_name_normalized="good vendor", normalized_mpn="testmpn001")
            .one()
        )
        assert rec.reason == UnavailabilityReason.SOLD_ELSEWHERE
        assert rec.note == "Sold the lot last week"

    def test_400_without_vendor_name(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-unavailable",
            data={},
        )
        assert resp.status_code == 400

    def test_missing_reason_returns_400_json_error(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-unavailable",
            data={"vendor_name": "Good Vendor"},
        )
        assert resp.status_code == 400
        assert "reason" in resp.json()["error"]

    def test_invalid_reason_returns_400_json_error(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-unavailable",
            data={"vendor_name": "Good Vendor", "reason": "gone_fishing"},
        )
        assert resp.status_code == 400
        assert "reason" in resp.json()["error"]
        assert db_session.query(VendorPartUnavailability).count() == 0

    def test_zero_key_mark_returns_400_json_error_without_activity(self, client, db_session):
        """Service ValueError (no derivable MPN keys — CRITICAL-1) maps to a 400 JSON
        error and writes nothing, including no ActivityLog."""
        req = Requisition(name="Keyless RFQ", status="active", customer_name="NoKey Co")
        db_session.add(req)
        db_session.flush()
        r = Requirement(requisition_id=req.id, primary_mpn=None, sourcing_status="open")
        db_session.add(r)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-unavailable",
            data={"vendor_name": "Good Vendor", "reason": "broken"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]
        assert db_session.query(VendorPartUnavailability).count() == 0
        assert (
            db_session.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.VENDOR_UNAVAILABLE).count()
            == 0
        )

    def test_records_via_primary_key_when_no_matching_sightings(self, client, db_session):
        """No matching sightings still records durable knowledge keyed on the
        requirement's primary MPN (200, record written)."""
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-unavailable",
            data={"vendor_name": "Nonexistent Vendor", "reason": "other"},
        )
        assert resp.status_code == 200
        rec = (
            db_session.query(VendorPartUnavailability)
            .filter_by(vendor_name_normalized="nonexistent vendor", normalized_mpn="testmpn001")
            .one()
        )
        assert rec.reason == UnavailabilityReason.OTHER

    def test_success_response_contains_toast_fragment(self, client, db_session):
        """F5: success appends the OOB toast fragment to the re-rendered detail —
        'Marked <vendor> unavailable — <reason label>'."""
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-unavailable",
            data={"vendor_name": "Good Vendor", "reason": "sold_elsewhere"},
        )
        assert resp.status_code == 200
        assert 'id="toast-trigger"' in resp.text
        assert "Marked Good Vendor unavailable" in resp.text
        assert "Vendor sold them" in resp.text
        assert "$store.toast.type='success'" in resp.text

    def test_invalid_reason_htmx_surfaces_specific_message(self, client, db_session):
        """F5: htmx callers see the actionable message as an error toast on the
        re-rendered detail (the global htmx:responseError handler only shows a
        generic line); nothing is written."""
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-unavailable",
            data={"vendor_name": "Good Vendor", "reason": "gone_fishing"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert 'id="toast-trigger"' in resp.text
        assert "must be one of" in resp.text
        assert "$store.toast.type='error'" in resp.text
        assert db_session.query(VendorPartUnavailability).count() == 0

    def test_zero_key_mark_htmx_surfaces_specific_message(self, client, db_session):
        """F5: the service ValueError (no derivable MPN keys) surfaces its actionable
        message to htmx callers; API callers keep the 400 JSON (pinned above)."""
        req = Requisition(name="Keyless RFQ 2", status="active", customer_name="NoKey Co")
        db_session.add(req)
        db_session.flush()
        r = Requirement(requisition_id=req.id, primary_mpn=None, sourcing_status="open")
        db_session.add(r)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-unavailable",
            data={"vendor_name": "Good Vendor", "reason": "broken"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "no MPN keys derivable" in resp.text
        assert "$store.toast.type='error'" in resp.text
        assert db_session.query(VendorPartUnavailability).count() == 0

    def test_suffixed_vendor_name_marks_end_to_end(self, client, db_session):
        """'X, Inc.' marks work end-to-end — the route delegates to the service's
        normalized matching (the old lower(trim()) strict-equality matcher is gone)."""
        _, r, _ = _seed_data(db_session)
        s = Sighting(
            requirement_id=r.id,
            vendor_name="Good Vendor, Inc.",
            vendor_name_normalized=None,  # legacy NULL-norm row
            mpn_matched="TEST-MPN-001",
        )
        db_session.add(s)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-unavailable",
            data={"vendor_name": "Good Vendor, Inc.", "reason": "broken"},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.get(Sighting, s.id).is_unavailable is True
        rec = db_session.query(VendorPartUnavailability).one()
        assert rec.vendor_name_normalized == "good vendor"


class TestSightingsMarkAvailable:
    def test_mark_available_restores_normal_row(self, client, db_session):
        """Clear deletes the record, unflags the sighting, and the re-rendered detail
        has no suppressed (rose) treatment."""
        _, r, _ = _seed_data(db_session)
        s = Sighting(
            requirement_id=r.id,
            vendor_name="Good Vendor",
            mpn_matched="TEST-MPN-001",
            is_unavailable=True,
        )
        db_session.add(s)
        db_session.commit()
        _unav_record(db_session, requirement_id=r.id)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-available",
            data={"vendor_name": "Good Vendor"},
        )
        assert resp.status_code == 200
        assert "bg-rose-50/60" not in resp.text
        db_session.expire_all()
        assert db_session.get(Sighting, s.id).is_unavailable is False
        assert db_session.query(VendorPartUnavailability).count() == 0

    def test_400_without_vendor_name(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(f"/v2/partials/sightings/{r.id}/mark-available", data={})
        assert resp.status_code == 400

    def test_400_when_vendor_normalizes_to_nothing(self, client, db_session):
        """Service ValueError (empty vendor norm — IMPORTANT-4) maps to 400 JSON."""
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-available",
            data={"vendor_name": "Inc."},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]

    def test_success_response_contains_toast_fragment(self, client, db_session):
        """F5: success appends the OOB toast — '<vendor> marked available again'."""
        _, r, _ = _seed_data(db_session)
        _unav_record(db_session, requirement_id=r.id)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-available",
            data={"vendor_name": "Good Vendor"},
        )
        assert resp.status_code == 200
        assert 'id="toast-trigger"' in resp.text
        assert "Good Vendor marked available again" in resp.text
        assert "$store.toast.type='success'" in resp.text

    def test_empty_norm_htmx_surfaces_specific_message(self, client, db_session):
        """F5: htmx callers get the actionable empty-norm message as an error toast
        instead of the generic responseError line."""
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-available",
            data={"vendor_name": "Inc."},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "normalizes to nothing" in resp.text
        assert "$store.toast.type='error'" in resp.text


class TestUnavailableFormModal:
    def test_renders_all_six_reasons_and_caveat(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/unavailable-form?vendor_name=Good%20Vendor")
        assert resp.status_code == 200
        for reason in UnavailabilityReason:
            assert reason.label in resp.text
            assert f'value="{reason.value}"' in resp.text
        # Accepted-limitation caveat copy (condition/variant key collapse)
        assert "all of this vendor's listings of this MPN" in resp.text
        # Submits to the mark-unavailable route
        assert f"/v2/partials/sightings/{r.id}/mark-unavailable" in resp.text

    def test_404_for_missing_requirement(self, client, db_session):
        resp = client.get("/v2/partials/sightings/99999/unavailable-form?vendor_name=X")
        assert resp.status_code == 404

    def test_existing_record_shows_mark_available_exit(self, client, db_session):
        """When a record already exists, the modal is the verify/re-arm affordance and
        carries BOTH actions: re-arm (submit) and 'It's back' (mark-available)."""
        _, r, _ = _seed_data(db_session)
        _unav_record(db_session, note="checked by phone", requirement_id=r.id)
        resp = client.get(f"/v2/partials/sightings/{r.id}/unavailable-form?vendor_name=Good%20Vendor")
        assert resp.status_code == 200
        assert "Currently marked" in resp.text
        assert "checked by phone" in resp.text
        assert f"/v2/partials/sightings/{r.id}/mark-available" in resp.text

    def test_fresh_mark_has_no_mark_available_exit(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/unavailable-form?vendor_name=Good%20Vendor")
        assert resp.status_code == 200
        assert "Currently marked" not in resp.text
        assert f"/v2/partials/sightings/{r.id}/mark-available" not in resp.text


class TestVendorRowThreeStates:
    """The three-state row UI (spec 2026-06-10-vendor-part-unavailability-design.md 'UI'
    section), keyed off the reader-authority rule via unavailable_intel."""

    def test_state1_suppressed_reason_note_age_and_mark_available_only(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        db_session.add(
            Sighting(
                requirement_id=r.id,
                vendor_name="Good Vendor",
                mpn_matched="TEST-MPN-001",
                is_unavailable=True,
            )
        )
        db_session.commit()
        _unav_record(db_session, note="Whole lot went to a competitor", age_days=3, requirement_id=r.id)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        body = resp.text
        # Shipped PR #260 treatment intact
        assert "bg-rose-50/60" in body
        assert "bg-rose-100 text-rose-700" in body
        # Reason label + truncated note + age (spec literals)
        assert "Vendor sold them" in body
        assert 'class="text-rose-400"' in body
        assert 'class="text-rose-300 italic truncate max-w-[28ch] min-w-0"' in body
        assert "Whole lot went to a competitor" in body
        assert "3d ago" in body
        # The only action is Mark available (gray → emerald hover)
        assert "Mark available" in body
        assert "text-gray-400 hover:text-emerald-600" in body
        # Expanded detail carries the What we learned entry
        assert "What we learned:" in body
        assert 'class="text-rose-600 font-medium"' in body
        # Action trio hidden
        assert "Send RFQ" not in body
        assert "Mark Unavail" not in body
        assert "Convert to offer" not in body

    def test_state1_renders_for_contacted_vendor(self, client, db_session):
        """F4 precedence pin: a vendor that was CONTACTED and then marked renders the
        full state-1 UI — rose tint, reason, Mark available. Contacted is a step;
        unavailable is its answer — a mark made after contacting must be visible."""
        from app.models.auth import User
        from app.models.offers import Contact

        req, r, _ = _seed_data(db_session)
        user = User(email="rowpin@example.com", name="Row Pin", role="buyer")
        db_session.add(user)
        db_session.flush()
        db_session.add(
            Contact(
                requisition_id=req.id,
                user_id=user.id,
                contact_type="email",
                vendor_name="Good Vendor",
                parts_included=["TEST-MPN-001"],
                status="sent",
            )
        )
        db_session.add(
            Sighting(
                requirement_id=r.id,
                vendor_name="Good Vendor",
                mpn_matched="TEST-MPN-001",
                is_unavailable=True,
            )
        )
        db_session.commit()
        _unav_record(db_session, reason="bought_by_us", age_days=2, requirement_id=r.id)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        body = resp.text
        assert "bg-rose-50/60" in body  # rose row tint
        assert "bg-rose-100 text-rose-700" in body  # Unavailable pill, not Contacted
        assert "We bought them" in body  # reason label renders
        assert "Mark available" in body  # state-1 action present
        assert "Send RFQ" not in body  # action trio hidden
        assert "Mark Unavail" not in body

    def test_state2_expired_record_renders_advisory_hint_and_verify(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        db_session.add(
            Sighting(
                requirement_id=r.id,
                vendor_name="Good Vendor",
                mpn_matched="TEST-MPN-001",
                is_unavailable=True,  # stale render cache — record expired
            )
        )
        db_session.commit()
        _unav_record(db_session, age_days=45, note="gone", requirement_id=r.id)  # LOT window is 30d
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        body = resp.text
        # Row fully normal — no tint, no rose pill
        assert "bg-rose-50/60" not in body
        assert "bg-rose-100 text-rose-700" not in body
        # Gray italic history hint with lowercased reason label
        assert "Marked unavailable 45d ago" in body
        assert "vendor sold them" in body
        assert "text-gray-400 italic truncate max-w-[36ch] min-w-0" in body
        # Amber verify affordance (amber ONLY on the action link — collision rule)
        assert "Verify availability" in body
        assert "text-amber-600 hover:text-amber-800" in body
        # Expanded grid History entry
        assert "History:" in body
        # Full action trio restored (Mark Unavail doubles as the re-arm)
        assert "Send RFQ" in body
        assert "Mark Unavail" in body
        assert "Convert to offer" in body

    def test_state3_restock_chip_when_surfaced_row_and_active_record(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        db_session.add(
            Sighting(
                requirement_id=r.id,
                vendor_name="Good Vendor",
                mpn_matched="TEST-MPN-001",
                is_unavailable=False,  # override-surfaced (unstamped) row
                qty_available=200,
            )
        )
        db_session.commit()
        _unav_record(db_session, age_days=5, qty_at_mark=100, requirement_id=r.id)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        body = resp.text
        # Normal row, NO tint; bordered emerald-50 chip (distinct from offer-in solid 100)
        assert "bg-rose-50/60" not in body
        assert "Possible restock" in body
        assert "bg-emerald-50 text-emerald-700 border border-emerald-200" in body
        # qty delta old → new in emerald mono + compressed history echo
        assert "100 → 200" in body
        assert 'class="font-mono text-emerald-600"' in body
        assert "text-gray-400 italic truncate max-w-[24ch]" in body
        # Emerald verify link
        assert "Verify restock" in body
        assert "text-emerald-700 hover:text-emerald-900" in body
        # Expanded grid: History + Changed entries
        assert "History:" in body
        assert "Changed:" in body
        # Full action trio restored
        assert "Send RFQ" in body
        assert "Mark Unavail" in body
        assert "Convert to offer" in body

    def test_normal_row_mark_unavail_opens_reason_modal(self, client, db_session):
        """The Mark Unavail button dispatches open-modal to the unavailable-form (no
        more direct hx-confirm POST)."""
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert f"/v2/partials/sightings/{r.id}/unavailable-form?vendor_name=" in resp.text
        assert "mark-unavailable" not in resp.text  # direct POST gone from the row


class TestRfqModalUnavailableExclusion:
    """RFQ vendor modal excludes vendors with an ACTIVE unavailability record on the
    selected requirements' primary MPN keys (alongside the blacklist filter)."""

    def _card(self, db_session, normalized="good vendor", display="Good Vendor", link_summary=None):
        """Create a card; optionally link an existing summary via the vendor_card_id FK
        (the coverage query joins on the FK, not the legacy name join)."""
        card = VendorCard(
            normalized_name=normalized,
            display_name=display,
            is_blacklisted=False,
            engagement_score=50.0,
        )
        db_session.add(card)
        db_session.flush()
        if link_summary is not None:
            link_summary.vendor_card_id = card.id
        db_session.commit()
        return card

    def test_excludes_marked_vendor_for_that_requirement(self, client, db_session):
        _, r, s = _seed_data(db_session)
        self._card(db_session, link_summary=s)
        _unav_record(db_session, requirement_id=r.id)
        resp = client.get(f"/v2/partials/sightings/vendor-modal?requirement_ids={r.id}")
        assert resp.status_code == 200
        assert "Good Vendor" not in resp.text

    def test_still_suggested_for_unrelated_requirement(self, client, db_session):
        _, r, s = _seed_data(db_session)
        card = self._card(db_session, link_summary=s)
        _unav_record(db_session, requirement_id=r.id)  # key testmpn001 only
        req2 = Requisition(name="Other RFQ", status="active", customer_name="Other Co")
        db_session.add(req2)
        db_session.flush()
        r2 = Requirement(
            requisition_id=req2.id,
            primary_mpn="OTHER-MPN-9",
            sourcing_status="open",
        )
        db_session.add(r2)
        db_session.flush()
        db_session.add(
            VendorSightingSummary(
                requirement_id=r2.id,
                vendor_name="Good Vendor",
                listing_count=1,
                score=60.0,
                vendor_card_id=card.id,
            )
        )
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/vendor-modal?requirement_ids={r2.id}")
        assert resp.status_code == 200
        assert "Good Vendor" in resp.text

    def test_expired_record_returns_vendor_to_modal(self, client, db_session):
        """Active-only exclusion: an expired record no longer suppresses suggestions."""
        _, r, s = _seed_data(db_session)
        self._card(db_session, link_summary=s)
        _unav_record(db_session, age_days=45, requirement_id=r.id)  # LOT window is 30d
        resp = client.get(f"/v2/partials/sightings/vendor-modal?requirement_ids={r.id}")
        assert resp.status_code == 200
        assert "Good Vendor" in resp.text

    def test_legacy_suffixed_card_still_excluded(self, client, db_session):
        """Pins the Python-side re-filter behind the SQL notin_ column filter: a legacy
        VendorCard whose normalized_name predates normalize_vendor_name (suffix kept,
        'good vendor inc') slips past the column filter — the canonical re-normalization
        of display_name must still exclude it, or a durably-dead vendor gets
        re-suggested to buyers."""
        _, r, _ = _seed_data(db_session)
        # Legacy card: normalized_name kept the suffix, so notin_({'good vendor'})
        # does NOT filter it at the SQL layer.
        card = self._card(db_session, normalized="good vendor inc", display="Good Vendor, Inc.")
        # Summary FK-linked to the legacy card (the coverage query joins on
        # vendor_card_id, not the vendor_name spelling).
        db_session.add(
            VendorSightingSummary(
                requirement_id=r.id,
                vendor_name="Good Vendor Inc",
                estimated_qty=150,
                listing_count=1,
                score=70.0,
                tier="Good",
                vendor_card_id=card.id,
            )
        )
        db_session.commit()
        _unav_record(db_session, requirement_id=r.id)  # active, canonical norm 'good vendor'
        resp = client.get(f"/v2/partials/sightings/vendor-modal?requirement_ids={r.id}")
        assert resp.status_code == 200
        assert "Good Vendor, Inc." not in resp.text


class TestVendorModalCoverageRanking:
    """Spec Part 2 §1 (bulk RFQ composer): suggested vendors are coverage-ranked over
    VendorSightingSummary joined via the vendor_card_id FK — covered-part count desc,
    then engagement desc.

    Each row renders `N/M parts` with the covered MPNs in title; VSS rows with NULL
    vendor_card_id are excluded by design (known vendors only).
    """

    def _requirements(self, db_session, mpns):
        req = Requisition(name="Coverage RFQ", status="active", customer_name="Cov Co")
        db_session.add(req)
        db_session.flush()
        items = []
        for mpn in mpns:
            r = Requirement(
                requisition_id=req.id,
                primary_mpn=mpn,
                target_qty=10,
                sourcing_status="open",
            )
            db_session.add(r)
            items.append(r)
        db_session.flush()
        return items

    def _vendor(self, db_session, display, engagement=0.0):
        card = VendorCard(
            normalized_name=display.lower(),  # no legal suffixes in test names
            display_name=display,
            is_blacklisted=False,
            engagement_score=engagement,
        )
        db_session.add(card)
        db_session.flush()
        return card

    def _summary(self, db_session, requirement, card=None, vendor_name=None, score=50.0):
        s = VendorSightingSummary(
            requirement_id=requirement.id,
            vendor_name=vendor_name or (card.display_name if card else "Mystery Vendor"),
            listing_count=1,
            score=score,
            vendor_card_id=card.id if card else None,
        )
        db_session.add(s)
        return s

    def _modal(self, client, items):
        ids = ",".join(str(r.id) for r in items)
        return client.get(f"/v2/partials/sightings/vendor-modal?requirement_ids={ids}")

    def _seed_ranking(self, db_session):
        """4 parts; Broadline covers 3 with low engagement, Hotshot covers 1 with
        high."""
        items = self._requirements(db_session, ["CV-MPN-1", "CV-MPN-2", "CV-MPN-3", "CV-MPN-4"])
        broad = self._vendor(db_session, "Broadline Parts", engagement=5.0)
        hot = self._vendor(db_session, "Hotshot Parts", engagement=99.0)
        for r in items[:3]:
            self._summary(db_session, r, broad)
        self._summary(db_session, items[0], hot)
        db_session.commit()
        return items

    def test_coverage_outranks_engagement(self, client, db_session):
        """A vendor covering 3/4 parts ranks above a 1/4 vendor with higher engagement —
        coverage desc is the primary sort key."""
        items = self._seed_ranking(db_session)
        resp = self._modal(client, items)
        assert resp.status_code == 200
        assert "Broadline Parts" in resp.text
        assert "Hotshot Parts" in resp.text
        assert resp.text.index("Broadline Parts") < resp.text.index("Hotshot Parts")

    def test_n_of_m_parts_rendered_with_mpn_title(self, client, db_session):
        """Each suggested row shows `N/M parts` (M = selected part count) and lists the
        covered MPNs in the chip's title attribute."""
        items = self._seed_ranking(db_session)
        resp = self._modal(client, items)
        assert resp.status_code == 200
        assert "3/4 parts" in resp.text
        assert "1/4 parts" in resp.text
        assert 'title="Covers: CV-MPN-1, CV-MPN-2, CV-MPN-3"' in resp.text
        assert 'title="Covers: CV-MPN-1"' in resp.text

    def test_excluded_vendor_absent(self, client, db_session):
        """Unavailability exclusion is preserved under the coverage query."""
        items = self._requirements(db_session, ["CV-MPN-9"])
        card = self._vendor(db_session, "Dodgy Vendor")
        self._summary(db_session, items[0], card)
        db_session.commit()
        _unav_record(db_session, vendor_norm="dodgy vendor", key="cvmpn9", requirement_id=items[0].id)
        resp = self._modal(client, items)
        assert resp.status_code == 200
        assert "Dodgy Vendor" not in resp.text

    def test_null_vendor_card_id_falls_back_to_name_join(self, client, db_session):
        """F10: a VSS row with NULL vendor_card_id (e.g. a summary rebuilt before
        the FK backfill ran) still suggests its vendor via the
        lower(trim(vendor_name)) == normalized_name fallback branch — the FK
        join stays primary, the name join only catches NULL-FK rows."""
        items = self._requirements(db_session, ["CV-MPN-7"])
        self._vendor(db_session, "Phantom Vendor")  # card exists, name matches
        self._summary(db_session, items[0], card=None, vendor_name="Phantom Vendor")
        db_session.commit()
        resp = self._modal(client, items)
        assert resp.status_code == 200
        assert "Phantom Vendor" in resp.text
        assert "1/1 parts" in resp.text
        assert 'title="Covers: CV-MPN-7"' in resp.text

    def test_null_fk_summary_without_matching_card_still_absent(self, client, db_session):
        """The fallback is card-gated: a NULL-FK summary whose name matches no
        VendorCard suggests nothing (the modal suggests known vendors only)."""
        items = self._requirements(db_session, ["CV-MPN-8"])
        self._summary(db_session, items[0], card=None, vendor_name="Unknown Rawname")
        db_session.commit()
        resp = self._modal(client, items)
        assert resp.status_code == 200
        assert "Unknown Rawname" not in resp.text

    def test_fk_and_null_fk_rows_do_not_double_count(self, client, db_session):
        """One FK row + one NULL-FK name-matching row on the SAME requirement still
        count 1 covered part (distinct requirement_id; the OR-join's NULL-FK guard
        prevents cross-matching FK rows by name)."""
        items = self._requirements(db_session, ["CV-MPN-9X"])
        card = self._vendor(db_session, "Split Vendor")
        self._summary(db_session, items[0], card)
        # Different raw spelling (VSS has a UNIQUE on requirement_id+vendor_name)
        # that still hits the lower(trim) fallback branch.
        self._summary(db_session, items[0], card=None, vendor_name="SPLIT VENDOR ")
        db_session.commit()
        resp = self._modal(client, items)
        assert resp.status_code == 200
        assert "1/1 parts" in resp.text
        assert "2/1 parts" not in resp.text

    def test_no_suggested_vendors_shows_empty_state_line(self, client, db_session):
        """F10: an empty suggested list explains itself instead of rendering a
        bare empty box."""
        items = self._requirements(db_session, ["CV-MPN-EMPTY"])
        resp = self._modal(client, items)
        assert resp.status_code == 200
        assert "No vendors with sightings for these parts" in resp.text

    def test_coverage_counts_distinct_requirements(self, client, db_session):
        """Two VSS spellings of one vendor on the same requirement count as 1 covered
        part (count distinct requirement_id), not 2."""
        items = self._requirements(db_session, ["CV-MPN-5"])
        card = self._vendor(db_session, "Dupe Vendor")
        self._summary(db_session, items[0], card, vendor_name="Dupe Vendor")
        self._summary(db_session, items[0], card, vendor_name="Dupe Vendor LLC")
        db_session.commit()
        resp = self._modal(client, items)
        assert resp.status_code == 200
        assert "1/1 parts" in resp.text
        assert "2/1 parts" not in resp.text


class TestVendorAffinityOnDemand:
    """Spec Part 2 §2 (bulk RFQ composer): `GET /v2/partials/sightings/vendor-affinity`
    runs find_vendor_affinity per selected MPN (on a worker thread with its own
    session), merges/dedupes by vendor keeping the highest confidence, drops vendors
    already coverage-suggested or unavailability-excluded, caps at 10, and renders
    checkbox rows that join the modal's existing Alpine selection state.

    find_vendor_affinity is mocked at its SOURCE module (the route imports it lazily) so
    no L1/L2 queries or Anthropic L3 call ever run in tests.
    """

    URL = "/v2/partials/sightings/vendor-affinity"
    PATCH_TARGET = "app.services.vendor_affinity_service.find_vendor_affinity"

    def _requirements(self, db_session, mpns):
        req = Requisition(name="Affinity RFQ", status="active", customer_name="Aff Co")
        db_session.add(req)
        db_session.flush()
        items = []
        for mpn in mpns:
            r = Requirement(
                requisition_id=req.id,
                primary_mpn=mpn,
                target_qty=10,
                sourcing_status="open",
            )
            db_session.add(r)
            items.append(r)
        db_session.commit()
        return items

    def _get(self, client, items):
        ids = ",".join(str(r.id) for r in items)
        return client.get(f"{self.URL}?requirement_ids={ids}")

    @staticmethod
    def _match(name, confidence, reasoning="Vendor supplied 3 other MPN(s) from AffMfr"):
        """Shape returned by find_vendor_affinity (score_affinity_matches output)."""
        return {
            "vendor_name": name,
            "vendor_id": None,
            "mpn_count": 3,
            "manufacturer": "AffMfr",
            "level": 1,
            "confidence": confidence,
            "reasoning": reasoning,
        }

    def test_merges_and_dedupes_keeping_highest_confidence(self, client, db_session):
        """The same vendor returned for two MPNs renders ONCE, with the higher
        confidence; rows sort confidence-desc."""
        from unittest.mock import patch

        items = self._requirements(db_session, ["AF-MPN-1", "AF-MPN-2"])
        per_mpn = {
            "AF-MPN-1": [self._match("Acme Components", 0.40)],
            "AF-MPN-2": [self._match("Acme Components", 0.65), self._match("Beta Parts", 0.50)],
        }
        with patch(self.PATCH_TARGET, side_effect=lambda mpn, db: per_mpn[mpn]) as mock_fva:
            resp = self._get(client, items)
        assert resp.status_code == 200
        assert mock_fva.call_count == 2
        assert resp.text.count("Acme Components") == 1
        assert "65%" in resp.text
        assert "40%" not in resp.text
        # Confidence-desc ordering: Acme (0.65) before Beta (0.50)
        assert resp.text.index("Acme Components") < resp.text.index("Beta Parts")

    def test_drops_already_suggested_vendors(self, client, db_session):
        """Vendors the modal already suggests (same coverage query, recomputed server-
        side) are dropped from the affinity rows."""
        from unittest.mock import patch

        items = self._requirements(db_session, ["AF-MPN-3"])
        card = VendorCard(
            normalized_name="covered vendor",
            display_name="Covered Vendor",
            is_blacklisted=False,
            engagement_score=10.0,
        )
        db_session.add(card)
        db_session.flush()
        db_session.add(
            VendorSightingSummary(
                requirement_id=items[0].id,
                vendor_name="Covered Vendor",
                listing_count=1,
                score=60.0,
                vendor_card_id=card.id,
            )
        )
        db_session.commit()
        matches = [self._match("Covered Vendor", 0.70), self._match("Fresh Vendor", 0.45)]
        with patch(self.PATCH_TARGET, return_value=matches):
            resp = self._get(client, items)
        assert resp.status_code == 200
        assert "Fresh Vendor" in resp.text
        assert "Covered Vendor" not in resp.text

    def test_drops_excluded_vendors(self, client, db_session):
        """Vendors with an ACTIVE unavailability record on a selected MPN key are
        dropped."""
        from unittest.mock import patch

        items = self._requirements(db_session, ["AF-MPN-4"])
        _unav_record(db_session, vendor_norm="dead vendor", key="afmpn4", requirement_id=items[0].id)
        matches = [self._match("Dead Vendor", 0.70), self._match("Live Vendor", 0.45)]
        with patch(self.PATCH_TARGET, return_value=matches):
            resp = self._get(client, items)
        assert resp.status_code == 200
        assert "Live Vendor" in resp.text
        assert "Dead Vendor" not in resp.text

    def test_renders_chip_confidence_and_selection_wiring(self, client, db_session):
        """Each row: bordered indigo 'affinity' chip, confidence %, reasoning in title,
        and a checkbox wired to the modal's isSelected/toggleVendor Alpine state."""
        from unittest.mock import patch

        items = self._requirements(db_session, ["AF-MPN-5"])
        matches = [self._match("Gamma Supply", 0.55, reasoning="Vendor shares commodity tags (3 matching tag(s))")]
        with patch(self.PATCH_TARGET, return_value=matches):
            resp = self._get(client, items)
        assert resp.status_code == 200
        assert "Gamma Supply" in resp.text
        assert "border border-indigo-200 bg-indigo-50 text-indigo-700" in resp.text
        assert ">affinity</span>" in resp.text
        assert "55%" in resp.text
        assert 'title="Vendor shares commodity tags (3 matching tag(s))"' in resp.text
        # Same Alpine selection mechanism as the modal's existing rows (normalized key)
        assert 'isSelected("gamma supply")' in resp.text
        assert 'toggleVendor("gamma supply")' in resp.text

    def test_caps_at_ten_rows(self, client, db_session):
        from unittest.mock import patch

        items = self._requirements(db_session, ["AF-MPN-6", "AF-MPN-7"])
        per_mpn = {
            "AF-MPN-6": [self._match(f"Vendor Six {i}", 0.30 + i / 100) for i in range(8)],
            "AF-MPN-7": [self._match(f"Vendor Seven {i}", 0.30 + i / 100) for i in range(8)],
        }
        with patch(self.PATCH_TARGET, side_effect=lambda mpn, db: per_mpn[mpn]):
            resp = self._get(client, items)
        assert resp.status_code == 200
        assert resp.text.count(">affinity</span>") == 10

    def test_response_has_no_suggest_button(self, client, db_session):
        """No-duplicate pin: the swap replaces the button with the rows, so the
        response itself must never re-render 'Suggest more vendors'."""
        from unittest.mock import patch

        items = self._requirements(db_session, ["AF-MPN-8"])
        with patch(self.PATCH_TARGET, return_value=[self._match("Delta Parts", 0.40)]):
            resp = self._get(client, items)
        assert resp.status_code == 200
        assert "Suggest more vendors" not in resp.text

    def test_empty_requirement_ids_returns_200_empty_state(self, client, db_session):
        from unittest.mock import patch

        with patch(self.PATCH_TARGET) as mock_fva:
            resp = client.get(f"{self.URL}?requirement_ids=")
        assert resp.status_code == 200
        mock_fva.assert_not_called()
        assert "No additional vendors" in resp.text

    def test_no_matches_renders_empty_state(self, client, db_session):
        from unittest.mock import patch

        items = self._requirements(db_session, ["AF-MPN-9"])
        with patch(self.PATCH_TARGET, return_value=[]):
            resp = self._get(client, items)
        assert resp.status_code == 200
        assert "No additional vendors" in resp.text

    def test_thread_calls_get_their_own_session(self, client, db_session):
        """SQLAlchemy sessions are not thread-safe: each to_thread call must receive a
        fresh short-lived SessionLocal, never the request session. Duplicate MPNs
        across requirements collapse to one call (no double L3 spend)."""
        from unittest.mock import patch

        from sqlalchemy.orm import Session as SASession

        items = self._requirements(db_session, ["AF-MPN-10", "AF-MPN-10", "AF-MPN-11"])
        seen_sessions = []

        def _capture(mpn, db):
            seen_sessions.append(db)
            return []

        with patch(self.PATCH_TARGET, side_effect=_capture) as mock_fva:
            resp = self._get(client, items)
        assert resp.status_code == 200
        assert mock_fva.call_count == 2  # AF-MPN-10 deduped
        for sess in seen_sessions:
            assert isinstance(sess, SASession)
            assert sess is not db_session

    def test_partial_affinity_failure_renders_results_with_notice(self, client, db_session):
        """F6: one MPN's affinity lookup failing must neither 500 the endpoint nor
        silently hide the surviving results — partial rows render, the failed MPN
        is logged, and a quiet 'suggestions incomplete' notice row appears."""
        from unittest.mock import patch

        items = self._requirements(db_session, ["AF-MPN-20", "AF-MPN-21"])

        def _maybe_fail(mpn, db):
            if mpn == "AF-MPN-20":
                raise RuntimeError("affinity backend down")
            return [self._match("Solo Vendor", 0.50)]

        with patch(self.PATCH_TARGET, side_effect=_maybe_fail):
            resp = self._get(client, items)
        assert resp.status_code == 200
        assert "Solo Vendor" in resp.text
        assert "Suggestions incomplete" in resp.text

    def test_full_affinity_success_has_no_incomplete_notice(self, client, db_session):
        from unittest.mock import patch

        items = self._requirements(db_session, ["AF-MPN-22"])
        with patch(self.PATCH_TARGET, return_value=[self._match("Ok Vendor", 0.50)]):
            resp = self._get(client, items)
        assert resp.status_code == 200
        assert "Suggestions incomplete" not in resp.text

    def test_modal_renders_affinity_section_with_button(self, client, db_session):
        """vendor_modal.html: 'Suggest more vendors' button lives inside the stable-id
        #rfq-affinity-section sub-container and targets IT (never the x-data wrapper —
        re-init would wipe selection state)."""
        items = self._requirements(db_session, ["AF-MPN-12"])
        ids = ",".join(str(r.id) for r in items)
        resp = client.get(f"/v2/partials/sightings/vendor-modal?requirement_ids={ids}")
        assert resp.status_code == 200
        assert 'id="rfq-affinity-section"' in resp.text
        assert "Suggest more vendors" in resp.text
        assert f'hx-get="/v2/partials/sightings/vendor-affinity?requirement_ids={ids}"' in resp.text
        assert 'hx-target="#rfq-affinity-section"' in resp.text


class TestComposerVendor:
    """Spec Part 2 §3/§4 (bulk RFQ composer): `POST /v2/partials/sightings/ composer-
    vendor` resolves-or-creates a vendor for the any-vendor picker and the inline "Add
    new vendor" mini-form.

    A confident duplicate (EXACT normalized-name match per the extracted
    check_vendor_duplicate service — fuzzy >= 80 are suggestions, not dupes) returns the
    EXISTING vendor as a selected row with a "matched existing vendor" notice and NO new
    DB row. Otherwise the minimal VendorCard (+ VendorContact when an email is given) is
    created, committed, and _background_enrich_vendor fires post-commit (mocked at its
    SOURCE module — the route imports it lazily). An unavailability-excluded vendor
    renders the rose chip with a DISABLED checkbox.
    """

    URL = "/v2/partials/sightings/composer-vendor"

    def _requirements(self, db_session, mpns):
        req = Requisition(name="Composer RFQ", status="active", customer_name="Comp Co")
        db_session.add(req)
        db_session.flush()
        items = []
        for mpn in mpns:
            r = Requirement(
                requisition_id=req.id,
                primary_mpn=mpn,
                target_qty=10,
                sourcing_status="open",
            )
            db_session.add(r)
            items.append(r)
        db_session.commit()
        return items

    def _card(self, db_session, display, normalized=None):
        card = VendorCard(
            normalized_name=normalized or display.lower(),
            display_name=display,
            is_blacklisted=False,
        )
        db_session.add(card)
        db_session.commit()
        return card

    def test_exact_duplicate_returns_existing_row_and_attaches_typed_email(self, client, db_session):
        """F4: a name that normalizes to an existing card is a confident duplicate —
        the EXISTING vendor comes back as a selected row (no new VendorCard), but
        a typed email must NOT be silently discarded: it is attached to the
        existing card as a VendorContact and the notice says so."""
        card = self._card(db_session, "Known Vendor")
        resp = client.post(self.URL, data={"vendor_name": "Known Vendor, Inc.", "email": "x@known.com"})
        assert resp.status_code == 200
        assert "Known Vendor" in resp.text
        assert "matched existing vendor — contact email added" in resp.text
        # Selected row: joins the modal's Alpine selection state checked
        assert 'selectVendor("known vendor")' in resp.text
        assert 'isSelected("known vendor")' in resp.text
        assert 'toggleVendor("known vendor")' in resp.text
        # No new CARD — but the typed email survives as a contact
        assert db_session.query(VendorCard).filter_by(normalized_name="known vendor").count() == 1
        contact = db_session.query(VendorContact).filter_by(vendor_card_id=card.id).one()
        assert contact.email == "x@known.com"
        assert contact.source == "rfq_manual"

    def test_exact_duplicate_without_email_writes_nothing(self, client, db_session):
        """A bare duplicate pick stays read-only: plain notice, no contact rows."""
        card = self._card(db_session, "Known Vendor")
        resp = client.post(self.URL, data={"vendor_name": "Known Vendor"})
        assert resp.status_code == 200
        assert "matched existing vendor" in resp.text
        assert "contact email added" not in resp.text
        assert db_session.query(VendorContact).filter_by(vendor_card_id=card.id).count() == 0

    def test_exact_duplicate_existing_email_not_duplicated(self, client, db_session):
        """The attach dedupes case-insensitively against the card's existing contacts —
        and the notice stays plain (nothing was added)."""
        card = self._card(db_session, "Known Vendor")
        db_session.add(VendorContact(vendor_card_id=card.id, email="X@Known.com", source="email"))
        db_session.commit()
        resp = client.post(self.URL, data={"vendor_name": "Known Vendor", "email": "x@known.com"})
        assert resp.status_code == 200
        assert "matched existing vendor" in resp.text
        assert "contact email added" not in resp.text
        assert db_session.query(VendorContact).filter_by(vendor_card_id=card.id).count() == 1

    def test_exact_duplicate_backfills_missing_domain_from_website(self, client, db_session):
        """F4: a typed website fills the existing card's missing domain."""
        card = self._card(db_session, "Known Vendor")
        assert card.domain is None
        resp = client.post(self.URL, data={"vendor_name": "Known Vendor", "website": "https://www.known.com/contact"})
        assert resp.status_code == 200
        db_session.refresh(card)
        assert card.domain == "known.com"

    def test_exact_duplicate_never_overwrites_existing_domain(self, client, db_session):
        card = self._card(db_session, "Known Vendor")
        card.domain = "known.com"
        db_session.commit()
        resp = client.post(self.URL, data={"vendor_name": "Known Vendor", "website": "https://other.example.org"})
        assert resp.status_code == 200
        db_session.refresh(card)
        assert card.domain == "known.com"

    def test_new_vendor_creates_card_contact_and_fires_enrichment(self, client, db_session):
        """No duplicate → minimal VendorCard (normalized_name, display_name, domain
        parsed from website) + VendorContact for the email, committed, then
        _background_enrich_vendor fires post-commit (source-module mock)."""
        from unittest.mock import AsyncMock, patch

        with (
            patch("app.services.credential_service.get_credential_cached", return_value="key"),
            patch("app.utils.vendor_helpers._background_enrich_vendor", new_callable=AsyncMock) as mock_enrich,
        ):
            resp = client.post(
                self.URL,
                data={
                    "vendor_name": "Fresh Vendor",
                    "website": "https://www.freshvendor.com/about",
                    "email": "sales@freshvendor.com",
                },
            )
        assert resp.status_code == 200
        card = db_session.query(VendorCard).filter_by(normalized_name="fresh vendor").one()
        assert card.display_name == "Fresh Vendor"
        assert card.domain == "freshvendor.com"
        contact = db_session.query(VendorContact).filter_by(vendor_card_id=card.id).one()
        assert contact.email == "sales@freshvendor.com"
        assert contact.source == "rfq_manual"
        assert mock_enrich.call_count == 1
        assert mock_enrich.call_args[0] == (card.id, "freshvendor.com", "Fresh Vendor")
        # New row comes back selected, wired to the modal's selection state
        assert "matched existing vendor" not in resp.text
        assert 'selectVendor("fresh vendor")' in resp.text
        assert 'toggleVendor("fresh vendor")' in resp.text

    def test_new_vendor_without_email_or_website_creates_bare_card(self, client, db_session):
        """Optional fields stay optional: a bare name creates the card only — no
        contact, no domain, no enrichment fired (nothing to enrich from)."""
        from unittest.mock import AsyncMock, patch

        with (
            patch("app.services.credential_service.get_credential_cached", return_value="key"),
            patch("app.utils.vendor_helpers._background_enrich_vendor", new_callable=AsyncMock) as mock_enrich,
        ):
            resp = client.post(self.URL, data={"vendor_name": "Bare Vendor"})
        assert resp.status_code == 200
        card = db_session.query(VendorCard).filter_by(normalized_name="bare vendor").one()
        assert card.domain is None
        assert db_session.query(VendorContact).filter_by(vendor_card_id=card.id).count() == 0
        mock_enrich.assert_not_called()

    def test_fuzzy_match_is_not_a_confident_duplicate(self, client, db_session):
        """Pins the threshold semantics: the duplicate-check service classifies only
        EXACT normalized-name matches as confident; a fuzzy >= 80 candidate (typo) is
        a suggestion, so the composer still creates the new card."""
        self._card(db_session, "Arrow Electronics Co")
        resp = client.post(self.URL, data={"vendor_name": "Arrow Electronisc Co"})
        assert resp.status_code == 200
        assert "matched existing vendor" not in resp.text
        assert db_session.query(VendorCard).filter_by(normalized_name="arrow electronisc co").count() == 1

    def test_empty_name_returns_400_json_error(self, client, db_session):
        """Empty/whitespace vendor_name → 400 in the repo JSON error format ({"error":

        ...}, not {"detail": ...}); nothing written.
        """
        before = db_session.query(VendorCard).count()
        resp = client.post(self.URL, data={"vendor_name": "   "})
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]
        assert body["status_code"] == 400
        assert db_session.query(VendorCard).count() == before

    def test_name_normalizing_to_nothing_returns_400_json_error(self, client, db_session):
        """A name that is all legal suffix ('Inc.') normalizes to '' — reject with the
        same 400 JSON error instead of writing a junk empty-norm card."""
        resp = client.post(self.URL, data={"vendor_name": "Inc."})
        assert resp.status_code == 400
        assert resp.json()["error"]
        assert db_session.query(VendorCard).filter_by(normalized_name="").count() == 0

    def test_website_without_scheme_parses_domain(self, client, db_session):
        """F12: urlsplit-based parsing handles scheme-less input."""
        resp = client.post(self.URL, data={"vendor_name": "Scheme Less", "website": "www.schemeless.io/shop"})
        assert resp.status_code == 200
        card = db_session.query(VendorCard).filter_by(normalized_name="scheme less").one()
        assert card.domain == "schemeless.io"

    def test_website_strips_only_leading_www(self, client, db_session):
        """F12: only a LEADING 'www.' is stripped — the old blanket
        str.replace("www.", "") mangled hosts containing the substring."""
        resp = client.post(self.URL, data={"vendor_name": "Www Embedded", "website": "https://shop.mywww.example.com"})
        assert resp.status_code == 200
        card = db_session.query(VendorCard).filter_by(normalized_name="www embedded").one()
        assert card.domain == "shop.mywww.example.com"

    def test_unusable_website_returns_400_json_error(self, client, db_session):
        """F12: unparseable / domain-less websites are rejected with the visible
        400 JSON error (surfaced by the modal via F8), never silently saved as a
        junk domain. Nothing is written."""
        before = db_session.query(VendorCard).count()
        for i, bad in enumerate(("https://", "no-dot", "ht!tp://%%%")):
            resp = client.post(self.URL, data={"vendor_name": f"Bad Site {i}", "website": bad})
            assert resp.status_code == 400, f"website {bad!r} should be rejected"
            assert "website" in resp.json()["error"].lower()
        assert db_session.query(VendorCard).count() == before

    def test_post_commit_enrichment_failure_still_returns_row(self, client, db_session):
        """F7: the card is committed before enrichment fires — a post-commit
        failure must be logged, not turned into a 500 (the modal would report a
        bogus failure for a vendor that EXISTS, and a retry would dupe-check)."""
        from unittest.mock import AsyncMock, patch

        with (
            patch("app.services.credential_service.get_credential_cached", return_value="key"),
            patch("app.utils.vendor_helpers._background_enrich_vendor", new_callable=AsyncMock),
            patch("app.utils.async_helpers.safe_background_task", side_effect=RuntimeError("event loop down")),
        ):
            resp = client.post(
                self.URL, data={"vendor_name": "Enrich Fail Co", "website": "https://enrichfail.example"}
            )
        assert resp.status_code == 200
        assert 'selectVendor("enrich fail co")' in resp.text
        assert db_session.query(VendorCard).filter_by(normalized_name="enrich fail co").count() == 1

    def test_excluded_vendor_renders_rose_chip_and_disabled_checkbox(self, client, db_session):
        """An existing vendor with an ACTIVE unavailability record on the selected parts
        renders the rose 'marked unavailable' chip and a DISABLED, unchecked checkbox —
        it never joins the selection (send-time re-validation stays the backstop)."""
        items = self._requirements(db_session, ["CP-MPN-1"])
        self._card(db_session, "Dead Vendor")
        _unav_record(db_session, vendor_norm="dead vendor", key="cpmpn1", requirement_id=items[0].id)
        resp = client.post(
            self.URL,
            data={"vendor_name": "Dead Vendor", "requirement_ids": str(items[0].id)},
        )
        assert resp.status_code == 200
        assert "marked unavailable" in resp.text
        assert "bg-rose-100 text-rose-700" in resp.text
        assert "disabled" in resp.text
        # Never selected, never toggleable
        assert "selectVendor(" not in resp.text
        assert "toggleVendor(" not in resp.text
        # F11: excluded rows still carry the normalized name so the client-side
        # dedupe can see them (no x-init to read it from).
        assert 'data-vendor-norm="dead vendor"' in resp.text

    def test_modal_renders_picker_added_container_and_inline_form(self, client, db_session):
        """vendor_modal.html: the vendor panel carries a stable id; the any-vendor
        autocomplete, the #rfq-added-vendors append target, and the 'Add new vendor'
        toggle all live INSIDE the rfqVendorModal x-data wrapper (appends target the
        sub-container, never the wrapper — re-init would wipe selection state)."""
        items = self._requirements(db_session, ["CP-MPN-2"])
        ids = ",".join(str(r.id) for r in items)
        resp = client.get(f"/v2/partials/sightings/vendor-modal?requirement_ids={ids}")
        assert resp.status_code == 200
        assert 'id="rfq-vendor-panel"' in resp.text
        assert 'id="rfq-added-vendors"' in resp.text
        assert "Find any vendor" in resp.text
        assert "Add new vendor" in resp.text
        # Inside the x-data wrapper: the wrapper opens before the sub-containers
        wrapper_at = resp.text.index("rfqVendorModal(")
        assert wrapper_at < resp.text.index('id="rfq-added-vendors"')
        assert wrapper_at < resp.text.index("Find any vendor")
        # Debounced autocomplete input per the established Alpine pattern
        assert "@input.debounce.300ms" in resp.text


class TestSendInquiryFailureContainment:
    """F3/F12: a mid-send crash must not commit partial tracking rows, and a selection
    whose every requirement id is stale 400s up front instead of reaching
    send_batch_rfq's NOT-NULL crash path with no requisition at all."""

    def test_send_failure_rolls_back_partial_state(self, client, db_session, monkeypatch):
        from app.models.offers import Contact

        _, r, _ = _seed_data(db_session)
        req_id = r.requisition_id

        async def fake_send(**kwargs):
            # Simulate send_batch_rfq dying MID-batch: one Contact row already
            # flushed on the shared session when the exception escapes.
            inner_db = kwargs["db"]
            inner_db.add(
                Contact(
                    requisition_id=req_id,
                    user_id=kwargs["user_id"],
                    contact_type="email",
                    vendor_name="Half Written",
                    created_at=datetime.now(timezone.utc),
                )
            )
            inner_db.flush()
            raise RuntimeError("Graph died mid-batch")

        monkeypatch.setattr("app.email_service.send_batch_rfq", fake_send)
        resp = client.post(
            "/v2/partials/sightings/send-inquiry",
            data={"requirement_ids": str(r.id), "vendor_names": ["Acme"], "email_body": "Quote."},
        )
        assert resp.status_code == 200
        assert resp.headers["X-RFQ-Sent"] == "0"
        # The partial row was rolled back, not committed by the route's commit.
        assert db_session.query(Contact).count() == 0

    def test_all_stale_requirement_ids_return_400(self, client, db_session):
        resp = client.post(
            "/v2/partials/sightings/send-inquiry",
            data={"requirement_ids": "99999", "vendor_names": ["Acme"], "email_body": "Quote."},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]


class TestSendInquiryUnavailableExclusion:
    """Send/preview re-validate submitted vendor_names against active-only
    excluded_vendor_norms at request time — excluded vendors are dropped and the skip is
    visibly reported (never silent)."""

    def _post_send(self, client, r):
        return client.post(
            "/v2/partials/sightings/send-inquiry",
            data={
                "requirement_ids": str(r.id),
                "vendor_names": ["Acme", "Globex"],
                "email_body": "Please quote.",
            },
        )

    def test_send_drops_excluded_vendor_and_reports(self, client, db_session, monkeypatch):
        _, r, _ = _seed_data(db_session)
        _unav_record(db_session, vendor_norm="globex", requirement_id=r.id)
        captured = {}

        async def fake_send(**kwargs):
            captured["groups"] = kwargs["vendor_groups"]
            return [{"vendor_name": g["vendor_name"], "status": "sent"} for g in kwargs["vendor_groups"]]

        monkeypatch.setattr("app.email_service.send_batch_rfq", fake_send)
        resp = self._post_send(client, r)
        assert resp.status_code == 200
        assert [g["vendor_name"] for g in captured["groups"]] == ["Acme"]
        assert "Globex" in resp.text  # the dropped vendor is named
        assert "unavailable" in resp.text.lower()  # and the reason is stated
        assert resp.headers["X-RFQ-Sent"] == "1"
        assert resp.headers["X-RFQ-Total"] == "2"
        assert resp.headers["X-RFQ-Unavailable"] == "1"

    def test_send_all_excluded_sends_nothing(self, client, db_session, monkeypatch):
        _, r, _ = _seed_data(db_session)
        _unav_record(db_session, vendor_norm="acme", requirement_id=r.id)
        _unav_record(db_session, vendor_norm="globex", requirement_id=r.id)
        called = {}

        async def fake_send(**kwargs):
            called["yes"] = True
            return []

        monkeypatch.setattr("app.email_service.send_batch_rfq", fake_send)
        resp = self._post_send(client, r)
        assert resp.status_code == 200
        assert "yes" not in called  # nothing was sent
        assert "Acme" in resp.text and "Globex" in resp.text
        assert "unavailable" in resp.text.lower()
        assert resp.headers["X-RFQ-Sent"] == "0"
        assert resp.headers["X-RFQ-Total"] == "2"
        assert resp.headers["X-RFQ-Unavailable"] == "2"

    def test_expired_record_does_not_block_send(self, client, db_session, monkeypatch):
        _, r, _ = _seed_data(db_session)
        _unav_record(db_session, vendor_norm="globex", age_days=45, requirement_id=r.id)

        async def fake_send(**kwargs):
            return [{"vendor_name": g["vendor_name"], "status": "sent"} for g in kwargs["vendor_groups"]]

        monkeypatch.setattr("app.email_service.send_batch_rfq", fake_send)
        resp = self._post_send(client, r)
        assert resp.status_code == 200
        assert resp.headers["X-RFQ-Sent"] == "2"

    def test_preview_drops_excluded_vendor_and_reports(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        _unav_record(db_session, vendor_norm="globex", requirement_id=r.id)
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={
                "requirement_ids": str(r.id),
                "vendor_names": ["Acme", "Globex"],
                "email_body": "Please quote.",
            },
        )
        assert resp.status_code == 200
        assert "1 vendor" in resp.text  # only Acme is previewed
        assert "Globex" in resp.text  # the skip is visibly reported
        assert "unavailable" in resp.text.lower()


def _seed_two_requisitions(db_session):
    """Two requisitions, one OPEN requirement each — a cross-requisition selection."""
    out = []
    for i, mpn in enumerate(["CROSS-MPN-A", "CROSS-MPN-B"]):
        req = Requisition(name=f"Cross RFQ {i}", status="active", customer_name="Acme Corp")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn=mpn,
            target_qty=100 * (i + 1),
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.flush()
        out.append((req, r))
    db_session.commit()
    return out


def _seed_vendor_with_email(db_session, normalized, display, email):
    """VendorCard + VendorContact so send-inquiry resolves a contact email."""
    vc = VendorCard(normalized_name=normalized, display_name=display)
    db_session.add(vc)
    db_session.flush()
    db_session.add(VendorContact(vendor_card_id=vc.id, email=email, source="email"))
    db_session.commit()
    return vc


class TestCrossRequisitionTracking:
    """Part 1 of the bulk RFQ composer spec: a send spanning requisitions tracks
    on EVERY involved requisition (per-requisition Contact rows, multi-token
    subjects, preview/send lockstep)."""

    def test_preview_renders_all_ref_tokens_sorted(self, client, db_session):
        (req_a, r_a), (req_b, r_b) = _seed_two_requisitions(db_session)
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={
                "requirement_ids": [str(r_a.id), str(r_b.id)],
                "vendor_names": ["Acme"],
                "email_body": "Please quote.",
            },
        )
        assert resp.status_code == 200
        lo, hi = sorted([req_a.id, req_b.id])
        # The exact subject the send will produce — both tokens, ascending req id.
        assert f"RFQ — 2 parts [ref:{lo}] [ref:{hi}]" in resp.text

    def test_send_passes_per_requisition_parts_map(self, client, db_session, monkeypatch):
        (req_a, r_a), (req_b, r_b) = _seed_two_requisitions(db_session)
        captured = {}

        async def fake_send(**kwargs):
            captured.update(kwargs)
            return [{"vendor_name": g["vendor_name"], "status": "sent"} for g in kwargs["vendor_groups"]]

        monkeypatch.setattr("app.email_service.send_batch_rfq", fake_send)
        resp = client.post(
            "/v2/partials/sightings/send-inquiry",
            data={
                "requirement_ids": [str(r_a.id), str(r_b.id)],
                "vendor_names": ["Acme"],
                "email_body": "Please quote.",
            },
        )
        assert resp.status_code == 200
        assert captured["requisition_parts_map"] == {
            req_a.id: [{"mpn": "CROSS-MPN-A", "qty": 100}],
            req_b.id: [{"mpn": "CROSS-MPN-B", "qty": 200}],
        }
        # No scalar collapse to a single arbitrary requisition anymore.
        assert captured.get("requisition_id") is None

    def test_send_single_requisition_call_shape_regression(self, client, db_session, monkeypatch):
        """Single-requisition sends keep working through the same path: the map
        carries exactly one entry with all selected parts."""
        _, r, _ = _seed_data(db_session)
        captured = {}

        async def fake_send(**kwargs):
            captured.update(kwargs)
            return [{"vendor_name": g["vendor_name"], "status": "sent"} for g in kwargs["vendor_groups"]]

        monkeypatch.setattr("app.email_service.send_batch_rfq", fake_send)
        resp = client.post(
            "/v2/partials/sightings/send-inquiry",
            data={
                "requirement_ids": str(r.id),
                "vendor_names": ["Acme"],
                "email_body": "Please quote.",
            },
        )
        assert resp.status_code == 200
        assert captured["requisition_parts_map"] == {r.requisition_id: [{"mpn": "TEST-MPN-001", "qty": 100}]}
        assert resp.headers["X-RFQ-Sent"] == "1"
        assert resp.headers["X-RFQ-Total"] == "1"

    def test_send_two_requisitions_two_vendors_full_tracking(self, client, db_session):
        """The core spec scenario: 2 requisitions x 2 vendors through the REAL
        send_batch_rfq (Graph mocked) → 4 Contacts, shared graph ids per vendor,
        per-requisition parts, both requirements progressed, activity per
        requirement, X-RFQ-* headers unchanged."""
        from unittest.mock import AsyncMock, patch

        from app.models.offers import Contact

        (req_a, r_a), (req_b, r_b) = _seed_two_requisitions(db_session)
        _seed_vendor_with_email(db_session, "acme", "Acme", "sales@acme.com")
        _seed_vendor_with_email(db_session, "globex", "Globex", "sales@globex.com")

        lo, hi = sorted([req_a.id, req_b.id])
        tagged = f"RFQ — 2 parts [ref:{lo}] [ref:{hi}]"

        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        # ONE shared sent-items list per lookup (identical subjects, both vendors'
        # messages present) — the realistic Graph shape; toRecipients is what
        # discriminates each vendor's own message (F1).
        mock_gc.get_json.return_value = {
            "value": [
                {
                    "id": "sent-globex",
                    "conversationId": "conv-globex",
                    "subject": tagged,
                    "toRecipients": [{"emailAddress": {"address": "sales@globex.com"}}],
                },
                {
                    "id": "sent-acme",
                    "conversationId": "conv-acme",
                    "subject": tagged,
                    "toRecipients": [{"emailAddress": {"address": "sales@acme.com"}}],
                },
            ]
        }

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": [str(r_a.id), str(r_b.id)],
                    "vendor_names": ["Acme", "Globex"],
                    "email_body": "Please quote.",
                },
            )

        assert resp.status_code == 200
        assert resp.headers["X-RFQ-Sent"] == "2"
        assert resp.headers["X-RFQ-Total"] == "2"
        assert resp.headers["X-RFQ-Skipped"] == "0"
        assert resp.headers["X-RFQ-Unavailable"] == "0"

        contacts = db_session.query(Contact).order_by(Contact.id).all()
        assert len(contacts) == 4  # one per (requisition, vendor)
        assert {(c.requisition_id, c.vendor_name) for c in contacts} == {
            (req_a.id, "Acme"),
            (req_b.id, "Acme"),
            (req_a.id, "Globex"),
            (req_b.id, "Globex"),
        }
        expected_parts = {
            req_a.id: [{"mpn": "CROSS-MPN-A", "qty": 100}],
            req_b.id: [{"mpn": "CROSS-MPN-B", "qty": 200}],
        }
        for c in contacts:
            assert c.subject == tagged  # identical to the preview subject
            assert c.parts_included == expected_parts[c.requisition_id]
        graph_ids = {}
        for c in contacts:
            graph_ids.setdefault(c.vendor_name, set()).add((c.graph_message_id, c.graph_conversation_id))
        assert graph_ids["Acme"] == {("sent-acme", "conv-acme")}
        assert graph_ids["Globex"] == {("sent-globex", "conv-globex")}

        # Both requisitions' requirements auto-progressed OPEN → SOURCING.
        db_session.refresh(r_a)
        db_session.refresh(r_b)
        assert r_a.sourcing_status == "sourcing"
        assert r_b.sourcing_status == "sourcing"

        # rfq_sent activity logged per requirement per vendor.
        logs = db_session.query(ActivityLog).filter(ActivityLog.activity_type == "rfq_sent").all()
        assert sorted((entry.requisition_id, entry.requirement_id) for entry in logs) == sorted(
            [(req_a.id, r_a.id), (req_a.id, r_a.id), (req_b.id, r_b.id), (req_b.id, r_b.id)]
        )


class TestSightingsVendorRowStatusTreatment:
    """Row-level visual treatment keyed off computed vendor status (spec
    2026-06-10-sightings-status-row-treatment-design.md)."""

    def test_unavailable_vendor_gets_red_row_treatment(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        db_session.add(
            Sighting(
                requirement_id=r.id,
                vendor_name="Good Vendor",
                mpn_matched="TEST-MPN-001",
                is_unavailable=True,
            )
        )
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "bg-rose-50/60" in resp.text  # row tint
        assert "bg-rose-100 text-rose-700" in resp.text  # badge

    def test_offer_in_vendor_gets_green_row_treatment(self, client, db_session):
        req, r, _ = _seed_data(db_session)
        db_session.add(
            Offer(
                requirement_id=r.id,
                requisition_id=req.id,
                vendor_name="Good Vendor",
                mpn="TEST-MPN-001",
                unit_price=1.50,
                qty_available=100,
            )
        )
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "bg-emerald-50/50" in resp.text  # row tint
        assert "bg-emerald-100 text-emerald-700" in resp.text  # badge

    def test_offer_dominates_unavailable_row_treatment(self, client, db_session):
        """Precedence pin: a vendor whose sightings are ALL unavailable but who has a
        live Offer renders green, never red — offer-in dominates unavailable in
        compute_vendor_statuses (app/services/sighting_status.py)."""
        req, r, _ = _seed_data(db_session)
        db_session.add(
            Sighting(
                requirement_id=r.id,
                vendor_name="Good Vendor",
                mpn_matched="TEST-MPN-001",
                is_unavailable=True,
            )
        )
        db_session.add(
            Offer(
                requirement_id=r.id,
                requisition_id=req.id,
                vendor_name="Good Vendor",
                mpn="TEST-MPN-001",
                unit_price=1.50,
                qty_available=100,
            )
        )
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "bg-emerald-50/50" in resp.text  # offer-in row tint wins
        assert "bg-rose-50/60" not in resp.text  # unavailable tint must NOT render

    def test_plain_sighting_row_has_no_status_tint(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "bg-rose-50/60" not in resp.text
        assert "bg-emerald-50/50" not in resp.text


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

    def test_xdata_not_truncated_with_vendors(self, client, db_session):
        """Regression for the broken "Send RFQ" modal.

        With at least one suggested vendor, the modal's root x-data must survive HTML
        attribute tokenization. The original template injected the vendor-name list via
        |tojson INSIDE a double-quoted x-data attribute; tojson emits literal double
        quotes (``["good vendor"]``) which close the attribute at the first vendor name,
        so Alpine fails to init and the whole modal goes inert (the reported bug). The
        fix routes the data through a single-quoted x-data invoking the rfqVendorModal()
        factory, so the attribute — and the vendor name inside it — parse intact.
        """
        _, r, s = _seed_data(db_session)
        # VendorCard FK-linked to the seeded summary (the coverage query joins on
        # vendor_card_id) so suggested_vendors is non-empty (the failing scenario).
        card = VendorCard(
            normalized_name="good vendor",
            display_name="Good Vendor",
            is_blacklisted=False,
            engagement_score=50.0,
        )
        db_session.add(card)
        db_session.flush()
        s.vendor_card_id = card.id
        db_session.commit()

        resp = client.get(f"/v2/partials/sightings/vendor-modal?requirement_ids={r.id}")
        assert resp.status_code == 200
        # Sanity: the join matched and the vendor is actually in the modal.
        assert "Good Vendor" in resp.text

        extractor = _XDataExtractor()
        extractor.feed(resp.text)
        modal_xdata = [v for v in extractor.xdata_values if "rfqVendorModal" in v]
        assert modal_xdata, (
            "modal root must invoke the rfqVendorModal() factory via a single-quoted "
            f"x-data; x-data values parsed: {extractor.xdata_values!r}"
        )
        # The vendor name must survive intact inside the parsed attribute — proof the
        # tojson double-quotes did NOT terminate the attribute early.
        assert any("good vendor" in v for v in modal_xdata), (
            "vendor name was truncated out of the x-data attribute — tojson double-quotes "
            "broke attribute tokenization (the original bug)."
        )


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


class TestSightingsSendInquiryResultHeaders:
    """The route returns HTTP 200 even on a partial/total send failure (failures are
    captured, not raised).

    The X-RFQ-Sent / X-RFQ-Total headers carry the true outcome so the browser modal
    (rfqVendorModal.confirmSend) never reports a false success.
    """

    def _post(self, client, r):
        return client.post(
            "/v2/partials/sightings/send-inquiry",
            data={
                "requirement_ids": str(r.id),
                "vendor_names": ["Acme", "Globex"],  # list → repeated form keys
                "email_body": "Please quote.",
            },
        )

    def test_full_success_headers(self, client, db_session, monkeypatch):
        _, r, _ = _seed_data(db_session)

        async def fake_send(**kwargs):
            # Real send_batch_rfq returns one record per attempted vendor tagged with a
            # status; all "sent" here.
            return [{"vendor_name": g["vendor_name"], "status": "sent"} for g in kwargs["vendor_groups"]]

        monkeypatch.setattr("app.email_service.send_batch_rfq", fake_send)
        resp = self._post(client, r)
        assert resp.status_code == 200
        assert resp.headers["X-RFQ-Sent"] == "2"
        assert resp.headers["X-RFQ-Total"] == "2"

    def test_partial_failure_still_200_with_headers(self, client, db_session, monkeypatch):
        _, r, _ = _seed_data(db_session)

        async def fake_send(**kwargs):
            # Mirror the real contract: one record per vendor, the second tagged "failed"
            # (NOT a shorter list — that never happens in production and would hide the
            # len(results) over-count bug).
            groups = kwargs["vendor_groups"]
            return [
                {"vendor_name": groups[0]["vendor_name"], "status": "sent"},
                {"vendor_name": groups[1]["vendor_name"], "status": "failed"},
            ]

        monkeypatch.setattr("app.email_service.send_batch_rfq", fake_send)
        resp = self._post(client, r)
        assert resp.status_code == 200
        assert resp.headers["X-RFQ-Sent"] == "1"  # only the "sent" record counts
        assert resp.headers["X-RFQ-Total"] == "2"

    def test_total_failure_still_200_with_headers(self, client, db_session, monkeypatch):
        _, r, _ = _seed_data(db_session)

        async def fake_send(**kwargs):
            raise RuntimeError("Graph API down")

        monkeypatch.setattr("app.email_service.send_batch_rfq", fake_send)
        resp = self._post(client, r)
        assert resp.status_code == 200
        assert resp.headers["X-RFQ-Sent"] == "0"
        assert resp.headers["X-RFQ-Total"] == "2"

    def test_skipped_no_email_reported_distinctly(self, client, db_session, monkeypatch):
        """A vendor with no contact email is 'skipped' — counted in X-RFQ-Skipped and
        named in the toast as 'No email on file', NOT folded into a delivery-failure
        count."""
        _, r, _ = _seed_data(db_session)

        async def fake_send(**kwargs):
            groups = kwargs["vendor_groups"]
            return [
                {"vendor_name": groups[0]["vendor_name"], "status": "sent"},
                {"vendor_name": groups[1]["vendor_name"], "status": "skipped", "error": "no contact email on file"},
            ]

        monkeypatch.setattr("app.email_service.send_batch_rfq", fake_send)
        resp = self._post(client, r)
        assert resp.status_code == 200
        assert resp.headers["X-RFQ-Sent"] == "1"
        assert resp.headers["X-RFQ-Total"] == "2"
        assert resp.headers["X-RFQ-Skipped"] == "1"
        assert "No email on file" in resp.text  # distinguished from "Failed:"
        assert "Globex" in resp.text  # the skipped vendor is named


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
        inactive_req = Requisition(name="Inactive RFQ", status="archived", customer_name="Closed Corp")
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
                ActivityLog.activity_type == ActivityType.STATUS_CHANGED,
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


class TestSightingsVendorRowActions:
    """Send RFQ / Mark Unavail actions live on the always-visible collapsed vendor row,
    not tucked inside the expandable detail (x-show="expanded")."""

    def _seed_vendor_row(self, db_session, status="open"):
        req = Requisition(name="Action RFQ", status="active", customer_name="ActCo")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="ACT-PRIMARY",
            manufacturer="Mfr",
            target_qty=50,
            sourcing_status=status,
        )
        db_session.add(r)
        db_session.flush()
        db_session.add(
            VendorSightingSummary(
                requirement_id=r.id,
                vendor_name="ActVendor",
                listing_count=1,
                score=60.0,
            )
        )
        db_session.commit()
        return r

    def test_actions_present_on_collapsed_row(self, client, db_session):
        """Both actions render and sit BEFORE the expandable detail block, so they are
        reachable on the collapsed row without expanding."""
        r = self._seed_vendor_row(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        body = resp.text
        assert "Send RFQ" in body
        assert "Mark Unavail" in body
        # The expandable detail is marked by x-show="expanded"; the actions must
        # appear earlier in the markup (i.e. on the always-visible row).
        expanded_marker = 'x-show="expanded"'
        assert expanded_marker in body
        assert body.index("Send RFQ") < body.index(expanded_marker)
        assert body.index("Mark Unavail") < body.index(expanded_marker)


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


class TestRequisitionStatusFilter:
    """Sightings list excludes requirements from archived/cancelled requisitions."""

    def test_archived_requisition_excluded(self, client, db_session):
        req = Requisition(name="Archived RFQ", status="archived", customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(requisition_id=req.id, primary_mpn="ARCHIVED-MPN", target_qty=10, sourcing_status="open")
        db_session.add(r)
        db_session.commit()
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "ARCHIVED-MPN" not in resp.text

    def test_cancelled_requisition_excluded(self, client, db_session):
        req = Requisition(name="Cancelled RFQ", status="cancelled", customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(requisition_id=req.id, primary_mpn="CANCELLED-MPN", target_qty=10, sourcing_status="open")
        db_session.add(r)
        db_session.commit()
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "CANCELLED-MPN" not in resp.text

    def test_non_active_requisition_included(self, client, db_session):
        """WON/SOURCING/QUOTED requisitions should appear in sightings."""
        for status in ("sourcing", "won", "quoted"):
            req = Requisition(name=f"{status} RFQ", status=status, customer_name="Acme")
            db_session.add(req)
            db_session.flush()
            r = Requirement(
                requisition_id=req.id, primary_mpn=f"MPN-{status.upper()}", target_qty=10, sourcing_status="open"
            )
            db_session.add(r)
        db_session.commit()
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        for status in ("sourcing", "won", "quoted"):
            assert f"MPN-{status.upper()}" in resp.text
