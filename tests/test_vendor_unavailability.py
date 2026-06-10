"""Tests for durable vendor+part unavailability knowledge (constants, model, service).

Verifies the UnavailabilityReason StrEnum (members + display labels), the
VendorPartUnavailability model (creation, defaults, FK to users), the
(vendor_name_normalized, normalized_mpn) unique constraint, and the
vendor_unavailability service (record/clear upsert semantics, key composition,
normalized vendor matching, fresh-sighting re-stamping, RFQ exclusion,
ActivityLog provenance entries).

Called by: pytest
Depends on: conftest.py (db_session, test_user fixtures), app/constants.py,
            app/models/vendor_part_unavailability.py,
            app/services/vendor_unavailability.py
"""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.constants import ActivityType, UnavailabilityReason
from app.models import User, VendorPartUnavailability
from app.models.intelligence import ActivityLog
from app.models.sourcing import Requirement, Requisition, Sighting
from app.services.vendor_unavailability import (
    _keys_for_vendor,
    apply_to_fresh_sightings,
    clear_unavailability,
    excluded_vendor_norms,
    record_unavailability,
    unavailability_for_requirement,
)
from app.utils.normalization import normalize_mpn_key
from app.vendor_utils import normalize_vendor_name


class TestUnavailabilityReason:
    def test_members(self):
        assert UnavailabilityReason.BOUGHT_BY_US == "bought_by_us"
        assert UnavailabilityReason.SOLD_ELSEWHERE == "sold_elsewhere"
        assert UnavailabilityReason.BROKEN == "broken"
        assert UnavailabilityReason.NOT_REALLY_THERE == "not_really_there"
        assert UnavailabilityReason.DIFFERENT_PART == "different_part"
        assert UnavailabilityReason.OTHER == "other"
        assert len(UnavailabilityReason) == 6

    def test_labels(self):
        assert UnavailabilityReason.BOUGHT_BY_US.label == "We bought them"
        assert UnavailabilityReason.SOLD_ELSEWHERE.label == "Vendor sold them"
        assert UnavailabilityReason.BROKEN.label == "Broken / bad condition"
        assert UnavailabilityReason.NOT_REALLY_THERE.label == "Not really in stock"
        assert UnavailabilityReason.DIFFERENT_PART.label == "Different part number"
        assert UnavailabilityReason.OTHER.label == "Other"

    def test_every_member_has_a_label(self):
        for reason in UnavailabilityReason:
            assert reason.label, f"{reason!r} has no display label"


class TestVendorPartUnavailabilityModel:
    def test_import(self):
        assert VendorPartUnavailability.__tablename__ == "vendor_part_unavailability"

    def test_create(self, db_session: Session, test_user: User):
        record = VendorPartUnavailability(
            vendor_name_normalized="acme components",
            normalized_mpn="ST3300657SS",
            reason=UnavailabilityReason.BOUGHT_BY_US,
            note="PO 1234 cleared their stock",
            created_by_id=test_user.id,
        )
        db_session.add(record)
        db_session.commit()
        db_session.refresh(record)

        assert record.id is not None
        assert record.vendor_name_normalized == "acme components"
        assert record.normalized_mpn == "ST3300657SS"
        assert record.reason == UnavailabilityReason.BOUGHT_BY_US
        assert record.note == "PO 1234 cleared their stock"
        assert record.created_by_id == test_user.id
        assert record.created_at is not None

    def test_note_and_created_by_are_optional(self, db_session: Session):
        record = VendorPartUnavailability(
            vendor_name_normalized="acme components",
            normalized_mpn="ST3300657SS",
            reason=UnavailabilityReason.OTHER,
        )
        db_session.add(record)
        db_session.commit()
        db_session.refresh(record)

        assert record.id is not None
        assert record.note is None
        assert record.created_by_id is None

    def test_duplicate_vendor_mpn_raises_integrity_error(self, db_session: Session):
        db_session.add(
            VendorPartUnavailability(
                vendor_name_normalized="acme components",
                normalized_mpn="ST3300657SS",
                reason=UnavailabilityReason.BROKEN,
            )
        )
        db_session.commit()

        db_session.add(
            VendorPartUnavailability(
                vendor_name_normalized="acme components",
                normalized_mpn="ST3300657SS",
                reason=UnavailabilityReason.SOLD_ELSEWHERE,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_same_mpn_different_vendor_is_allowed(self, db_session: Session):
        db_session.add(
            VendorPartUnavailability(
                vendor_name_normalized="acme components",
                normalized_mpn="ST3300657SS",
                reason=UnavailabilityReason.BROKEN,
            )
        )
        db_session.add(
            VendorPartUnavailability(
                vendor_name_normalized="globex parts",
                normalized_mpn="ST3300657SS",
                reason=UnavailabilityReason.NOT_REALLY_THERE,
            )
        )
        db_session.commit()

        rows = db_session.query(VendorPartUnavailability).filter_by(normalized_mpn="ST3300657SS").all()
        assert len(rows) == 2


# ── Service-layer helpers ─────────────────────────────────────────────────────


def _make_requirement(db_session: Session, primary_mpn: str = "ST3300657SS") -> Requirement:
    requisition = Requisition(name="Test RFQ", status="active")
    db_session.add(requisition)
    db_session.flush()
    requirement = Requirement(
        requisition_id=requisition.id,
        primary_mpn=primary_mpn,
        manufacturer="TestMfr",
    )
    db_session.add(requirement)
    db_session.flush()
    return requirement


def _make_sighting(
    db_session: Session,
    requirement: Requirement,
    vendor_name: str,
    mpn_matched: str | None = None,
) -> Sighting:
    s = Sighting(
        requirement_id=requirement.id,
        vendor_name=vendor_name,
        vendor_name_normalized=normalize_vendor_name(vendor_name),
        mpn_matched=mpn_matched,
    )
    db_session.add(s)
    db_session.flush()
    return s


def _records(db_session: Session, vendor_norm: str) -> list[VendorPartUnavailability]:
    return (
        db_session.query(VendorPartUnavailability)
        .filter(VendorPartUnavailability.vendor_name_normalized == vendor_norm)
        .order_by(VendorPartUnavailability.normalized_mpn)
        .all()
    )


class TestKeysForVendor:
    def test_matched_mpn_keys_union_primary(self, db_session: Session):
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        s1 = _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ALT-123")
        s2 = _make_sighting(db_session, requirement, "Acme Components", mpn_matched=None)

        keys = _keys_for_vendor(requirement, [s1, s2])
        assert keys == {"st3300657ss", "alt123"}

    def test_no_sightings_still_includes_primary(self, db_session: Session):
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        assert _keys_for_vendor(requirement, []) == {"st3300657ss"}


class TestRecordUnavailability:
    def test_writes_one_record_per_key_and_flags_sightings(self, db_session: Session, test_user: User):
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        s1 = _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ALT-123")
        s2 = _make_sighting(db_session, requirement, "Acme Components", mpn_matched=None)

        written = record_unavailability(
            db_session,
            requirement,
            "Acme Components",
            UnavailabilityReason.BOUGHT_BY_US,
            "PO 1234 cleared their stock",
            test_user,
        )
        db_session.commit()

        assert written == 2
        records = _records(db_session, "acme components")
        assert {r.normalized_mpn for r in records} == {"st3300657ss", "alt123"}
        for r in records:
            assert r.reason == UnavailabilityReason.BOUGHT_BY_US
            assert r.note == "PO 1234 cleared their stock"
            assert r.created_by_id == test_user.id
            assert r.created_at is not None
        assert s1.is_unavailable is True
        assert s2.is_unavailable is True

    def test_second_mark_updates_not_duplicates(self, db_session: Session, test_user: User):
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ALT-123")

        record_unavailability(
            db_session,
            requirement,
            "Acme Components",
            UnavailabilityReason.BOUGHT_BY_US,
            "first note",
            test_user,
        )
        db_session.commit()

        written = record_unavailability(
            db_session,
            requirement,
            "Acme Components",
            UnavailabilityReason.SOLD_ELSEWHERE,
            "second note",
            test_user,
        )
        db_session.commit()

        assert written == 2
        records = _records(db_session, "acme components")
        assert len(records) == 2  # updated in place, no duplicates
        for r in records:
            assert r.reason == UnavailabilityReason.SOLD_ELSEWHERE
            assert r.note == "second note"
            assert r.created_by_id == test_user.id

    def test_suffixed_vendor_name_flags_sightings(self, db_session: Session, test_user: User):
        """Suffixed display name "Acme Components, Inc." normalizes to the same key the
        sighting rows carry — the old lower(trim(...)) comparison missed legal suffixes
        (architect finding 1)."""
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        sighting = _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ST3300657SS")

        written = record_unavailability(
            db_session,
            requirement,
            "Acme Components, Inc.",
            UnavailabilityReason.BROKEN,
            None,
            test_user,
        )
        db_session.commit()

        assert written == 1
        assert sighting.is_unavailable is True
        assert _records(db_session, "acme components")

    def test_activity_log_written(self, db_session: Session, test_user: User):
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ST3300657SS")

        record_unavailability(
            db_session,
            requirement,
            "Acme Components",
            UnavailabilityReason.BOUGHT_BY_US,
            "PO 1234",
            test_user,
        )
        db_session.commit()

        entries = (
            db_session.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.VENDOR_UNAVAILABLE).all()
        )
        assert len(entries) == 1
        entry = entries[0]
        assert entry.requirement_id == requirement.id
        assert entry.requisition_id == requirement.requisition_id
        assert entry.user_id == test_user.id
        assert entry.contact_name == "Acme Components"
        assert "Acme Components" in entry.notes
        assert UnavailabilityReason.BOUGHT_BY_US.label in entry.notes
        assert "PO 1234" in entry.notes
        assert requirement.primary_mpn in entry.notes


class TestClearUnavailability:
    def test_clear_deletes_records_unflags_sightings_logs_activity(self, db_session: Session, test_user: User):
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        s1 = _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ALT-123")
        record_unavailability(
            db_session,
            requirement,
            "Acme Components",
            UnavailabilityReason.NOT_REALLY_THERE,
            None,
            test_user,
        )
        db_session.commit()
        assert len(_records(db_session, "acme components")) == 2

        cleared = clear_unavailability(db_session, requirement, "Acme Components", test_user)
        db_session.commit()

        assert cleared == 2
        assert _records(db_session, "acme components") == []
        assert s1.is_unavailable is False
        entries = db_session.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.VENDOR_AVAILABLE).all()
        assert len(entries) == 1
        assert entries[0].contact_name == "Acme Components"
        assert "available again" in entries[0].notes


class TestApplyToFreshSightings:
    def test_fresh_sightings_get_flagged(self, db_session: Session, test_user: User):
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        old = _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ST3300657SS")
        record_unavailability(
            db_session,
            requirement,
            "Acme Components",
            UnavailabilityReason.BOUGHT_BY_US,
            None,
            test_user,
        )
        db_session.commit()

        # Simulate a re-search: delete + recreate the vendor's sightings.
        db_session.delete(old)
        db_session.flush()
        matched = _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ST3300657SS")
        fallback = _make_sighting(db_session, requirement, "Acme Components", mpn_matched=None)
        other_vendor = _make_sighting(db_session, requirement, "Globex Parts", mpn_matched="ST3300657SS")
        other_mpn = _make_sighting(db_session, requirement, "Acme Components", mpn_matched="OTHER-999")

        count = apply_to_fresh_sightings(db_session, requirement, [matched, fallback, other_vendor, other_mpn])
        db_session.commit()

        assert count == 2
        assert matched.is_unavailable is True
        assert fallback.is_unavailable is True  # empty mpn_matched falls back to primary
        assert bool(other_vendor.is_unavailable) is False
        assert bool(other_mpn.is_unavailable) is False


class TestExcludedVendorNorms:
    def test_matches_on_primary_key_only_for_related_requirements(self, db_session: Session, test_user: User):
        req_a = _make_requirement(db_session, primary_mpn="ST3300657SS")
        req_b = _make_requirement(db_session, primary_mpn="ZZZ-999")
        _make_sighting(db_session, req_a, "Acme Components", mpn_matched="ST3300657SS")
        record_unavailability(
            db_session,
            req_a,
            "Acme Components",
            UnavailabilityReason.SOLD_ELSEWHERE,
            None,
            test_user,
        )
        db_session.commit()

        assert excluded_vendor_norms(db_session, [req_a]) == {"acme components"}
        assert excluded_vendor_norms(db_session, [req_b]) == set()
        assert excluded_vendor_norms(db_session, [req_a, req_b]) == {"acme components"}
        assert excluded_vendor_norms(db_session, []) == set()


class TestUnavailabilityForRequirement:
    def test_maps_display_names_to_most_recent_record(self, db_session: Session, test_user: User):
        requirement = _make_requirement(db_session, primary_mpn="AAA-111")
        _make_sighting(db_session, requirement, "Acme Components", mpn_matched="BBB-222")
        _make_sighting(db_session, requirement, "Globex Parts", mpn_matched="AAA-111")

        from datetime import datetime, timedelta, timezone

        older = VendorPartUnavailability(
            vendor_name_normalized="acme components",
            normalized_mpn=normalize_mpn_key("BBB-222"),
            reason=UnavailabilityReason.BROKEN,
            created_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        newer = VendorPartUnavailability(
            vendor_name_normalized="acme components",
            normalized_mpn=normalize_mpn_key("AAA-111"),
            reason=UnavailabilityReason.BOUGHT_BY_US,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([older, newer])
        db_session.commit()

        intel = unavailability_for_requirement(db_session, requirement, ["Acme Components, Inc.", "Globex Parts"])

        assert set(intel) == {"Acme Components, Inc."}
        assert intel["Acme Components, Inc."].id == newer.id

    def test_empty_vendor_names(self, db_session: Session):
        requirement = _make_requirement(db_session, primary_mpn="AAA-111")
        assert unavailability_for_requirement(db_session, requirement, []) == {}
