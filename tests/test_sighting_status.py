"""Tests for compute_vendor_statuses in app/services/sighting_status.py.

Focused on the condition-aware vendor-pill gate (Task 6 / brief 6.1):
a record's condition must be NULL (all-conditions catch-all) or match a
condition the vendor actually has sightings for.

Called by: pytest
Depends on: conftest.py fixtures (db_session), app models
"""

from datetime import datetime, timezone

from app.models.sourcing import Requirement, Requisition, Sighting
from app.models.vendor_part_unavailability import VendorPartUnavailability
from app.models.vendor_sighting_summary import VendorSightingSummary
from app.services.sighting_status import compute_vendor_statuses

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_req(db_session, mpn="TEST-MPN-001"):
    """Minimal Requisition + Requirement; returns (req, requirement)."""
    req = Requisition(name="Test RFQ", status="open", customer_name="Acme Corp")
    db_session.add(req)
    db_session.flush()
    r = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        manufacturer="TestMfr",
        target_qty=10,
        sourcing_status="open",
    )
    db_session.add(r)
    db_session.flush()
    return req, r


def _make_summary(db_session, requirement_id, vendor_name="Good Vendor"):
    """VendorSightingSummary so compute_vendor_statuses discovers the vendor."""
    s = VendorSightingSummary(
        requirement_id=requirement_id,
        vendor_name=vendor_name,
        estimated_qty=10,
        listing_count=1,
        score=50.0,
        tier="Good",
    )
    db_session.add(s)


def _make_sighting(db_session, requirement_id, condition, stamped, vendor_name="Good Vendor", mpn="TEST-MPN-001"):
    """Sighting with explicit condition and render-cache stamp."""
    s = Sighting(
        requirement_id=requirement_id,
        vendor_name=vendor_name,
        mpn_matched=mpn,
        condition=condition,
        is_unavailable=stamped,
    )
    db_session.add(s)
    return s


def _make_record(db_session, condition, vendor_norm="good vendor", mpn_norm="testmpn001"):
    """Active VendorPartUnavailability record (just created, not released)."""
    rec = VendorPartUnavailability(
        vendor_name_normalized=vendor_norm,
        normalized_mpn=mpn_norm,
        condition=condition,
        reason="sold_elsewhere",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(rec)
    return rec


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestComputeVendorStatusesConditionGate:
    """Condition-aware vendor-pill gate (brief 6.1 — Task 6).

    An active VendorPartUnavailability record with condition='new' must NOT gate a
    vendor whose sightings are all 'refurb' — a stray other-condition record must not
    pin the pill.  A NULL-condition record is a catch-all and always gates (it matches
    every sighting condition).
    """

    def test_active_new_record_refurb_sightings_stamped_not_unavailable(self, db_session):
        """(a) The condition-gate: active 'new' record + REFURB sightings (all stamped)
        → pill must NOT be 'unavailable'.

        This is the key regression guard: without the condition gate the old
        code would return 'unavailable' because `any(is_active(...))` passes
        and `all(flags)` passes (stamps are True).  After the gate only records
        whose condition appears in the vendor's sighting conditions are
        considered, so the 'new' record is filtered out.
        """
        req, r = _make_req(db_session)
        _make_summary(db_session, r.id)
        # Sightings are REFURB and all stamped
        _make_sighting(db_session, r.id, condition="Refurbished", stamped=True)
        _make_sighting(db_session, r.id, condition="refurb", stamped=True)
        # Active unavailability record scoped to 'new' — must NOT match
        _make_record(db_session, condition="new")
        db_session.commit()

        result = compute_vendor_statuses(r.id, req.id, db_session)
        assert result.get("Good Vendor") == "sighting", (
            "A 'new'-condition record must not pin the pill for a vendor with only refurb sightings"
        )

    def test_active_new_record_new_sightings_stamped_is_unavailable(self, db_session):
        """(b) Active 'new' record + all-NEW sightings (all stamped) → 'unavailable'.

        Confirms the condition gate does NOT break the normal happy path.
        """
        req, r = _make_req(db_session)
        _make_summary(db_session, r.id)
        _make_sighting(db_session, r.id, condition="new", stamped=True)
        _make_sighting(db_session, r.id, condition="New", stamped=True)
        _make_record(db_session, condition="new")
        db_session.commit()

        result = compute_vendor_statuses(r.id, req.id, db_session)
        assert result.get("Good Vendor") == "unavailable", (
            "Active 'new' record + all NEW sightings stamped must yield 'unavailable'"
        )

    def test_null_condition_record_any_sightings_stamped_is_unavailable(self, db_session):
        """(c) NULL-condition record (catch-all) + all sightings stamped →
        'unavailable'.

        NULL condition means the record applies to all conditions, so the gate must pass
        regardless of what condition the sightings carry.
        """
        req, r = _make_req(db_session)
        _make_summary(db_session, r.id)
        # Sightings have mixed conditions but all stamped
        _make_sighting(db_session, r.id, condition="new", stamped=True)
        _make_sighting(db_session, r.id, condition="Refurbished", stamped=True)
        # NULL-condition catch-all record
        _make_record(db_session, condition=None)
        db_session.commit()

        result = compute_vendor_statuses(r.id, req.id, db_session)
        assert result.get("Good Vendor") == "unavailable", (
            "A NULL-condition (all-conditions) record must still gate the pill"
        )

    def test_active_new_record_refurb_sightings_none_stamped_not_unavailable(self, db_session):
        """(a-baseline) active 'new' record + REFURB sightings, none stamped → NOT
        'unavailable'.

        Double-checks the full rule: even before the condition gate fires, the
        `all(flags)` sub-gate already prevents the pill when no sightings are
        stamped.  The condition gate adds defence-in-depth.
        """
        req, r = _make_req(db_session)
        _make_summary(db_session, r.id)
        _make_sighting(db_session, r.id, condition="Refurbished", stamped=False)
        _make_record(db_session, condition="new")
        db_session.commit()

        result = compute_vendor_statuses(r.id, req.id, db_session)
        assert result.get("Good Vendor") == "sighting"
