"""Tests for derived vendor status in the sightings tab.

Includes the Batch 4 reader-authority rule: a vendor is "unavailable" iff
(an active VendorPartUnavailability record matches AND the vendor has NO
unstamped sighting row) OR (no matching record at all AND all rows flagged —
true legacy). Rows win; expired/released records never pin the pill.

Called by: pytest
Depends on: conftest.py fixtures, app models, app/services/sighting_status.py,
            app/services/vendor_unavailability.py (is_active authority)
"""

from datetime import datetime, timedelta, timezone

from app.constants import UnavailabilityReason
from app.models.auth import User
from app.models.offers import Contact, Offer
from app.models.sourcing import Requirement, Requisition, Sighting
from app.models.vendor_part_unavailability import VendorPartUnavailability
from app.models.vendor_sighting_summary import VendorSightingSummary
from app.models.vendors import VendorCard
from app.utils.normalization import normalize_mpn_key
from app.vendor_utils import normalize_vendor_name


def _make_user(db_session) -> User:
    u = User(email="test@example.com", name="Test User", role="buyer")
    db_session.add(u)
    db_session.flush()
    return u


def _make_requisition(db_session) -> Requisition:
    req = Requisition(name="Test RFQ", status="active")
    db_session.add(req)
    db_session.flush()
    return req


def _make_requirement(db_session, req: Requisition) -> Requirement:
    r = Requirement(
        requisition_id=req.id,
        primary_mpn="TEST-MPN-001",
        manufacturer="TestMfr",
    )
    db_session.add(r)
    db_session.flush()
    return r


def _make_summary(db_session, req_id: int, vendor: str, qty: int = 100) -> VendorSightingSummary:
    s = VendorSightingSummary(
        requirement_id=req_id,
        vendor_name=vendor,
        estimated_qty=qty,
        listing_count=1,
        score=50.0,
        tier="Good",
    )
    db_session.add(s)
    db_session.flush()
    return s


class TestDeriveVendorStatus:
    """Test the compute_vendor_statuses helper function."""

    def test_default_status_is_sighting(self, db_session):
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        db_session.commit()
        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "sighting"

    def test_contacted_status(self, db_session):
        from app.services.sighting_status import compute_vendor_statuses

        user = _make_user(db_session)
        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        contact = Contact(
            requisition_id=req.id,
            user_id=user.id,
            contact_type="email",
            vendor_name="Acme Corp",
            parts_included=["TEST-MPN-001"],
            status="sent",
        )
        db_session.add(contact)
        db_session.commit()
        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "contacted"

    def test_offer_in_status(self, db_session):
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        offer = Offer(
            requisition_id=req.id,
            requirement_id=r.id,
            vendor_name="Acme Corp",
            mpn="TEST-MPN-001",
        )
        db_session.add(offer)
        db_session.commit()
        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "offer-in"

    def test_unavailable_status(self, db_session):
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        sighting = Sighting(
            requirement_id=r.id,
            vendor_name="Acme Corp",
            mpn_matched="TEST-MPN-001",
            is_unavailable=True,
        )
        db_session.add(sighting)
        db_session.commit()
        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "unavailable"

    def test_blacklisted_overrides_all(self, db_session):
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Bad Vendor")
        vc = VendorCard(normalized_name="bad vendor", display_name="Bad Vendor", is_blacklisted=True)
        db_session.add(vc)
        offer = Offer(
            requisition_id=req.id,
            requirement_id=r.id,
            vendor_name="Bad Vendor",
            mpn="TEST-MPN-001",
        )
        db_session.add(offer)
        db_session.commit()
        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Bad Vendor"] == "blacklisted"

    def test_offer_in_overrides_contacted(self, db_session):
        from app.services.sighting_status import compute_vendor_statuses

        user = _make_user(db_session)
        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        db_session.add(
            Contact(
                requisition_id=req.id,
                user_id=user.id,
                contact_type="email",
                vendor_name="Acme Corp",
                parts_included=["TEST-MPN-001"],
                status="sent",
            )
        )
        db_session.add(
            Offer(
                requisition_id=req.id,
                requirement_id=r.id,
                vendor_name="Acme Corp",
                mpn="TEST-MPN-001",
            )
        )
        db_session.commit()
        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "offer-in"

    def test_empty_vendor_names_returns_empty_dict(self, db_session):
        """Line 43: compute_vendor_statuses with no vendors returns empty dict."""
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        db_session.commit()
        # Pass empty vendor_names explicitly
        statuses = compute_vendor_statuses(r.id, req.id, db_session, vendor_names=[])
        assert statuses == {}

    def test_explicit_vendor_names_bypasses_db_query(self, db_session):
        """When vendor_names is passed explicitly, no DB query for summaries."""
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        db_session.commit()
        # Pass vendor_names directly (no summaries in DB)
        statuses = compute_vendor_statuses(r.id, req.id, db_session, vendor_names=["My Vendor"])
        assert statuses["My Vendor"] == "sighting"


class TestDurableUnavailabilityStatus:
    """Batch 4 ORs durable VendorPartUnavailability records into 'unavailable'."""

    def test_durable_record_alone_is_unavailable(self, db_session):
        """A record on the requirement's primary-MPN key marks the vendor unavailable
        even with no sighting rows flagged (or none at all)."""
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        db_session.add(
            VendorPartUnavailability(
                vendor_name_normalized=normalize_vendor_name("Acme Corp"),
                normalized_mpn=normalize_mpn_key(r.primary_mpn),
                reason=UnavailabilityReason.BOUGHT_BY_US,
            )
        )
        db_session.commit()
        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "unavailable"

    def test_durable_record_via_sighting_matched_mpn_key(self, db_session):
        """A record keyed on a sighting's matched MPN (not the primary) also marks the
        vendor — keys are matched-MPN keys ∪ primary key.

        The row is stamped here:
        under the reader-authority rule an unstamped row would flip the pill off
        (rows-win), so this pins purely the matched-key record matching.
        """
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        db_session.add(
            Sighting(
                requirement_id=r.id,
                vendor_name="Acme Corp",
                vendor_name_normalized=normalize_vendor_name("Acme Corp"),
                mpn_matched="ALT-123",
                is_unavailable=True,
            )
        )
        db_session.add(
            VendorPartUnavailability(
                vendor_name_normalized=normalize_vendor_name("Acme Corp"),
                normalized_mpn=normalize_mpn_key("ALT-123"),
                reason=UnavailabilityReason.DIFFERENT_PART,
            )
        )
        db_session.commit()
        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "unavailable"

    def test_offer_dominates_durable_record(self, db_session):
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        db_session.add(
            VendorPartUnavailability(
                vendor_name_normalized=normalize_vendor_name("Acme Corp"),
                normalized_mpn=normalize_mpn_key(r.primary_mpn),
                reason=UnavailabilityReason.BOUGHT_BY_US,
            )
        )
        db_session.add(
            Offer(
                requisition_id=req.id,
                requirement_id=r.id,
                vendor_name="Acme Corp",
                mpn="TEST-MPN-001",
            )
        )
        db_session.commit()
        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "offer-in"

    def test_legacy_row_flag_matches_despite_case_drift(self, db_session):
        """Summary says 'ACME CORP', sighting rows say 'Acme Corp' — the legacy all-
        rows-flagged branch is anchored on normalized names, so the drift no longer
        silently misses (architect finding 2)."""
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "ACME CORP")
        db_session.add(
            Sighting(
                requirement_id=r.id,
                vendor_name="Acme Corp",
                vendor_name_normalized=normalize_vendor_name("Acme Corp"),
                mpn_matched="TEST-MPN-001",
                is_unavailable=True,
            )
        )
        db_session.commit()
        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["ACME CORP"] == "unavailable"


def _add_sighting(db_session, req_id: int, vendor: str, stamped: bool) -> Sighting:
    s = Sighting(
        requirement_id=req_id,
        vendor_name=vendor,
        vendor_name_normalized=normalize_vendor_name(vendor),
        mpn_matched="TEST-MPN-001",
        is_unavailable=stamped,
    )
    db_session.add(s)
    db_session.flush()
    return s


def _add_record(db_session, vendor: str, key: str, age_days: int = 0, reason=UnavailabilityReason.SOLD_ELSEWHERE):
    rec = VendorPartUnavailability(
        vendor_name_normalized=normalize_vendor_name(vendor),
        normalized_mpn=key,
        reason=reason,
        created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
    )
    db_session.add(rec)
    db_session.flush()
    return rec


class TestReaderAuthorityRule:
    """Batch 4 rewrite: the record predicate is the authority; rows win."""

    def test_rows_win_unstamped_row_flips_pill_off(self, db_session):
        """One unstamped (e.g. override-surfaced) row + an active record → NOT
        unavailable."""
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        _add_sighting(db_session, r.id, "Acme Corp", stamped=True)
        _add_sighting(db_session, r.id, "Acme Corp", stamped=False)
        _add_record(db_session, "Acme Corp", normalize_mpn_key(r.primary_mpn), age_days=1)
        db_session.commit()

        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "sighting"

    def test_active_record_all_rows_stamped_is_unavailable(self, db_session):
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        _add_sighting(db_session, r.id, "Acme Corp", stamped=True)
        _add_sighting(db_session, r.id, "Acme Corp", stamped=True)
        _add_record(db_session, "Acme Corp", normalize_mpn_key(r.primary_mpn), age_days=1)
        db_session.commit()

        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "unavailable"

    def test_expired_record_stale_stamped_rows_do_not_pin_pill(self, db_session):
        """All rows still carry the stale stamp, but the record's 30d LOT window has
        lapsed → NOT unavailable (RFQ and pill agree again)."""
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        _add_sighting(db_session, r.id, "Acme Corp", stamped=True)
        _add_record(db_session, "Acme Corp", normalize_mpn_key(r.primary_mpn), age_days=31)
        db_session.commit()

        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "sighting"

    def test_released_record_stale_stamped_rows_do_not_pin_pill(self, db_session):
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        _add_sighting(db_session, r.id, "Acme Corp", stamped=True)
        rec = _add_record(db_session, "Acme Corp", normalize_mpn_key(r.primary_mpn), age_days=1)
        rec.released_at = datetime.now(timezone.utc)
        rec.release_trigger = "offer_received"
        db_session.commit()

        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "sighting"

    def test_mixed_variant_legacy_pin_not_unavailable(self, db_session):
        """MINOR-9 pin: vendor with a record + a MIX of stamped and unstamped rows is
        NOT unavailable — the legacy all-rows-flagged branch is restricted to vendors
        with NO record (deliberate strictening of the v1 OR-semantics)."""
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        _add_sighting(db_session, r.id, "Acme Corp", stamped=True)
        _add_sighting(db_session, r.id, "Acme Corp", stamped=False)
        # Expired record — even a non-active record disables the legacy branch.
        _add_record(db_session, "Acme Corp", normalize_mpn_key(r.primary_mpn), age_days=31)
        db_session.commit()

        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "sighting"

    def test_no_record_all_rows_flagged_legacy_unavailable(self, db_session):
        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        _make_summary(db_session, r.id, "Acme Corp")
        _add_sighting(db_session, r.id, "Acme Corp", stamped=True)
        _add_sighting(db_session, r.id, "Acme Corp", stamped=True)
        db_session.commit()

        statuses = compute_vendor_statuses(r.id, req.id, db_session)
        assert statuses["Acme Corp"] == "unavailable"

    def test_missing_requirement_logs_warning(self, db_session):
        """MINOR-8: a status computation against a missing requirement row warns
        instead of silently treating it as key-less."""
        from loguru import logger as loguru_logger

        from app.services.sighting_status import compute_vendor_statuses

        req = _make_requisition(db_session)
        db_session.commit()

        messages: list[str] = []
        sink_id = loguru_logger.add(lambda m: messages.append(str(m)), level="WARNING")
        try:
            statuses = compute_vendor_statuses(99999999, req.id, db_session, vendor_names=["Acme Corp"])
        finally:
            loguru_logger.remove(sink_id)

        assert statuses["Acme Corp"] == "sighting"
        assert any("99999999" in m for m in messages)
