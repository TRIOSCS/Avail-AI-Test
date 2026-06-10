"""Tests for durable vendor+part unavailability knowledge (constants, model, service).

Verifies the UnavailabilityReason StrEnum (members + display labels), the
VendorPartUnavailability model (creation, defaults, policy/provenance columns,
FK to users), the (vendor_name_normalized, normalized_mpn) unique constraint,
the vendor_unavailability service (record/clear upsert semantics, key
composition, normalized vendor matching, fresh-sighting re-stamping, RFQ
exclusion, ActivityLog provenance entries), the adopted "Two Windows, Real
Proof" temporal policy (per-class windows, is_active predicate, O1/O2/O3
suppression matrix, offer-hook release, per-key qty snapshots, re-arm
semantics, validated config knobs), and the silent-failure hardening
(zero-key/empty-norm raises, NULL-norm zombie clear, candidate-key set
matching, requirement_id provenance clear).

Called by: pytest
Depends on: conftest.py (db_session, test_user fixtures), app/constants.py,
            app/config.py, app/models/vendor_part_unavailability.py,
            app/services/vendor_unavailability.py
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from loguru import logger as loguru_logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.constants import ActivityType, UnavailabilityReason
from app.models import User, VendorPartUnavailability
from app.models.intelligence import ActivityLog
from app.models.sourcing import Requirement, Requisition, Sighting
from app.services.vendor_unavailability import (
    HUMAN_DIRECT_SOURCES,
    LIVE_SOURCES,
    LOT_REASONS,
    RELEASE_TRIGGER_OFFER_RECEIVED,
    RELEASE_TRIGGER_VENDOR_EMAIL,
    UnavailabilityIntel,
    _keys_for_vendor,
    apply_to_fresh_sightings,
    clear_unavailability,
    excluded_vendor_norms,
    is_active,
    record_unavailability,
    release_on_offer,
    unavailability_for_requirement,
)
from app.utils.normalization import normalize_mpn_key
from app.vendor_utils import normalize_vendor_name


@contextmanager
def _capture_warnings():
    """Collect loguru WARNING+ messages emitted inside the block."""
    messages: list[str] = []
    sink_id = loguru_logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        yield messages
    finally:
        loguru_logger.remove(sink_id)


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


def _make_requirement(db_session: Session, primary_mpn: str | None = "ST3300657SS") -> Requirement:
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
    qty_available: int | None = None,
    source_type: str | None = None,
    is_authorized: bool = False,
    vendor_name_normalized: str | None = "__derive__",
) -> Sighting:
    s = Sighting(
        requirement_id=requirement.id,
        vendor_name=vendor_name,
        vendor_name_normalized=(
            normalize_vendor_name(vendor_name) if vendor_name_normalized == "__derive__" else vendor_name_normalized
        ),
        mpn_matched=mpn_matched,
        qty_available=qty_available,
        source_type=source_type,
        is_authorized=is_authorized,
    )
    db_session.add(s)
    db_session.flush()
    return s


def _make_record(
    db_session: Session,
    vendor_norm: str = "acme components",
    key: str = "st3300657ss",
    reason: UnavailabilityReason = UnavailabilityReason.SOLD_ELSEWHERE,
    age_days: int = 0,
    qty_at_mark: int | None = None,
    released_at: datetime | None = None,
    release_trigger: str | None = None,
    requirement_id: int | None = None,
) -> VendorPartUnavailability:
    rec = VendorPartUnavailability(
        vendor_name_normalized=vendor_norm,
        normalized_mpn=key,
        reason=reason,
        created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
        qty_at_mark=qty_at_mark,
        released_at=released_at,
        release_trigger=release_trigger,
        requirement_id=requirement_id,
    )
    db_session.add(rec)
    db_session.flush()
    return rec


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

        # IMPORTANT-5 candidate-key SET: other_mpn's keys are {other999, st3300657ss}
        # — the primary-key record matches even though mpn_matched differs, so the
        # marked vendor's variant row is stamped too (supersedes the v1
        # matched-or-fallback single-key behavior).
        assert count == 3
        assert matched.is_unavailable is True
        assert fallback.is_unavailable is True  # empty mpn_matched falls back to primary
        assert other_mpn.is_unavailable is True  # matches via the primary-key candidate
        assert bool(other_vendor.is_unavailable) is False

    def test_candidate_key_set_row_matching_only_via_primary_still_stamped(self, db_session: Session, test_user: User):
        """A record on the primary key stamps a fresh row whose mpn_matched normalizes
        to a different key (IMPORTANT-5)."""
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        _make_record(db_session, key="st3300657ss", requirement_id=requirement.id)
        row = _make_sighting(db_session, requirement, "Acme Components", mpn_matched="VARIANT-77")

        count = apply_to_fresh_sightings(db_session, requirement, [row])

        assert count == 1
        assert row.is_unavailable is True


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
        assert intel["Acme Components, Inc."].record.id == newer.id

    def test_results_are_annotated_with_policy_state(self, db_session: Session):
        """The intel entries carry computed policy state (is_active, age_days,
        release_trigger) so templates never re-derive policy."""
        requirement = _make_requirement(db_session, primary_mpn="AAA-111")
        _make_record(
            db_session,
            vendor_norm="acme components",
            key=normalize_mpn_key("AAA-111"),
            reason=UnavailabilityReason.SOLD_ELSEWHERE,
            age_days=3,
        )
        _make_record(
            db_session,
            vendor_norm="globex parts",
            key=normalize_mpn_key("AAA-111"),
            reason=UnavailabilityReason.BOUGHT_BY_US,
            age_days=40,  # past the 30d LOT window
        )
        released_at = datetime.now(timezone.utc)
        _make_record(
            db_session,
            vendor_norm="initech supply",
            key=normalize_mpn_key("AAA-111"),
            reason=UnavailabilityReason.NOT_REALLY_THERE,
            age_days=1,
            released_at=released_at,
            release_trigger=RELEASE_TRIGGER_OFFER_RECEIVED,
        )
        db_session.commit()

        intel = unavailability_for_requirement(
            db_session, requirement, ["Acme Components", "Globex Parts", "Initech Supply"]
        )

        active = intel["Acme Components"]
        assert isinstance(active, UnavailabilityIntel)
        assert active.is_active is True
        assert active.age_days == 3
        assert active.release_trigger is None

        expired = intel["Globex Parts"]
        assert expired.is_active is False
        assert expired.age_days == 40

        released = intel["Initech Supply"]
        assert released.is_active is False
        assert released.release_trigger == RELEASE_TRIGGER_OFFER_RECEIVED

    def test_empty_vendor_names(self, db_session: Session):
        requirement = _make_requirement(db_session, primary_mpn="AAA-111")
        assert unavailability_for_requirement(db_session, requirement, []) == {}


# ── Temporal policy: model columns, predicate, source classes ─────────────────


class TestModelPolicyColumns:
    def test_policy_and_provenance_columns_default_null(self, db_session: Session):
        rec = VendorPartUnavailability(
            vendor_name_normalized="acme components",
            normalized_mpn="st3300657ss",
            reason=UnavailabilityReason.OTHER,
        )
        db_session.add(rec)
        db_session.commit()
        db_session.refresh(rec)

        assert rec.qty_at_mark is None
        assert rec.released_at is None
        assert rec.release_trigger is None
        assert rec.requirement_id is None

    def test_requirement_fk_round_trips(self, db_session: Session):
        requirement = _make_requirement(db_session)
        rec = _make_record(db_session, qty_at_mark=120, requirement_id=requirement.id)
        db_session.commit()
        db_session.refresh(rec)

        assert rec.qty_at_mark == 120
        assert rec.requirement_id == requirement.id


class TestPolicyConstants:
    def test_source_trust_classes(self):
        assert LIVE_SOURCES == frozenset({"digikey", "mouser", "element14"})
        assert HUMAN_DIRECT_SOURCES == frozenset({"email_attachment"})

    def test_lot_reasons(self):
        assert LOT_REASONS == frozenset(
            {
                UnavailabilityReason.BOUGHT_BY_US,
                UnavailabilityReason.SOLD_ELSEWHERE,
                UnavailabilityReason.BROKEN,
                UnavailabilityReason.OTHER,
            }
        )

    def test_release_trigger_values(self):
        assert RELEASE_TRIGGER_VENDOR_EMAIL == "vendor_email"
        assert RELEASE_TRIGGER_OFFER_RECEIVED == "offer_received"


class TestIsActive:
    def _record(self, reason: UnavailabilityReason, age_days: int, **kwargs) -> VendorPartUnavailability:
        return VendorPartUnavailability(
            vendor_name_normalized="acme components",
            normalized_mpn="st3300657ss",
            reason=reason,
            created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
            **kwargs,
        )

    def test_lot_reason_30_day_window(self):
        now = datetime.now(timezone.utc)
        for reason in LOT_REASONS:
            assert is_active(self._record(reason, age_days=29), now) is True
            assert is_active(self._record(reason, age_days=31), now) is False

    def test_listing_reason_180_day_window(self):
        now = datetime.now(timezone.utc)
        assert is_active(self._record(UnavailabilityReason.NOT_REALLY_THERE, age_days=179), now) is True
        assert is_active(self._record(UnavailabilityReason.NOT_REALLY_THERE, age_days=31), now) is True
        assert is_active(self._record(UnavailabilityReason.NOT_REALLY_THERE, age_days=181), now) is False

    def test_different_part_never_expires(self):
        now = datetime.now(timezone.utc)
        assert is_active(self._record(UnavailabilityReason.DIFFERENT_PART, age_days=400), now) is True

    def test_window_boundary_is_inclusive(self):
        """created_at >= now - window: a mark exactly window days old is still active."""
        now = datetime.now(timezone.utc)
        rec = self._record(UnavailabilityReason.BOUGHT_BY_US, age_days=0)
        rec.created_at = now - timedelta(days=30)
        assert is_active(rec, now) is True

    def test_released_record_is_never_active(self):
        now = datetime.now(timezone.utc)
        rec = self._record(
            UnavailabilityReason.DIFFERENT_PART,
            age_days=0,
            released_at=now,
            release_trigger=RELEASE_TRIGGER_OFFER_RECEIVED,
        )
        assert is_active(rec, now) is False

    def test_naive_created_at_is_treated_as_utc(self):
        """SQLite/legacy rows may surface naive datetimes — compared as UTC, no
        crash."""
        now = datetime.now(timezone.utc)
        rec = self._record(UnavailabilityReason.BOUGHT_BY_US, age_days=0)
        rec.created_at = (now - timedelta(days=1)).replace(tzinfo=None)
        assert is_active(rec, now) is True
        rec.created_at = (now - timedelta(days=31)).replace(tzinfo=None)
        assert is_active(rec, now) is False


class TestUnavailabilityKnobs:
    def test_defaults(self):
        from app.config import Settings

        s = Settings()
        assert s.unavailability_suppress_days == 30
        assert s.unavailability_listing_suppress_days == 180
        assert s.unavailability_qty_jump_factor == 2.0

    @pytest.mark.parametrize("value", [0, -1])
    def test_suppress_days_rejects_non_positive(self, value):
        from pydantic import ValidationError

        from app.config import Settings

        with pytest.raises(ValidationError):
            Settings(unavailability_suppress_days=value)

    @pytest.mark.parametrize("value", [0, -30])
    def test_listing_suppress_days_rejects_non_positive(self, value):
        from pydantic import ValidationError

        from app.config import Settings

        with pytest.raises(ValidationError):
            Settings(unavailability_listing_suppress_days=value)

    @pytest.mark.parametrize("value", [0.0, 0.99, -2.0])
    def test_qty_jump_factor_rejects_below_one(self, value):
        from pydantic import ValidationError

        from app.config import Settings

        with pytest.raises(ValidationError):
            Settings(unavailability_qty_jump_factor=value)


# ── Temporal policy: suppression matrix in apply_to_fresh_sightings ───────────


class TestSuppressionMatrixWindows:
    def test_expired_lot_record_never_stamps(self, db_session: Session):
        requirement = _make_requirement(db_session)
        rec = _make_record(db_session, reason=UnavailabilityReason.SOLD_ELSEWHERE, age_days=31)
        row = _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ST3300657SS")

        count = apply_to_fresh_sightings(db_session, requirement, [row])

        assert count == 0
        assert bool(row.is_unavailable) is False
        assert rec.released_at is None  # expiry never mutates the record

    def test_active_lot_record_stamps(self, db_session: Session):
        requirement = _make_requirement(db_session)
        _make_record(db_session, reason=UnavailabilityReason.SOLD_ELSEWHERE, age_days=29)
        row = _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ST3300657SS")

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 1
        assert row.is_unavailable is True

    def test_listing_reason_still_active_past_lot_window(self, db_session: Session):
        requirement = _make_requirement(db_session)
        _make_record(db_session, reason=UnavailabilityReason.NOT_REALLY_THERE, age_days=31)
        row = _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ST3300657SS")

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 1
        assert row.is_unavailable is True

    def test_listing_reason_expires_past_180_days(self, db_session: Session):
        requirement = _make_requirement(db_session)
        _make_record(db_session, reason=UnavailabilityReason.NOT_REALLY_THERE, age_days=181)
        row = _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ST3300657SS")

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 0
        assert bool(row.is_unavailable) is False

    def test_different_part_still_stamps_after_a_year(self, db_session: Session):
        requirement = _make_requirement(db_session)
        _make_record(db_session, reason=UnavailabilityReason.DIFFERENT_PART, age_days=400)
        row = _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ST3300657SS")

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 1
        assert row.is_unavailable is True

    def test_released_record_never_stamps(self, db_session: Session):
        requirement = _make_requirement(db_session)
        _make_record(
            db_session,
            reason=UnavailabilityReason.SOLD_ELSEWHERE,
            age_days=1,
            released_at=datetime.now(timezone.utc),
            release_trigger=RELEASE_TRIGGER_OFFER_RECEIVED,
        )
        row = _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ST3300657SS")

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 0
        assert bool(row.is_unavailable) is False


class TestOverrideO1LiveTruth:
    def test_identical_live_echo_stays_stamped(self, db_session: Session):
        """The equality-guard: a stale distributor-API echo showing the exact flagged
        qty is NOT live proof — the row stays stamped."""
        requirement = _make_requirement(db_session)
        _make_record(db_session, qty_at_mark=100)
        row = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            qty_available=100,
            source_type="digikey",
        )

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 1
        assert row.is_unavailable is True

    def test_changed_live_qty_surfaces_row(self, db_session: Session):
        requirement = _make_requirement(db_session)
        rec = _make_record(db_session, qty_at_mark=100)
        row = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            qty_available=120,
            source_type="digikey",
        )

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 0
        assert bool(row.is_unavailable) is False
        assert rec.released_at is None  # O1 is row-level only, no record mutation

    def test_null_snapshot_passes_the_equality_guard(self, db_session: Session):
        requirement = _make_requirement(db_session)
        _make_record(db_session, qty_at_mark=None)
        row = _make_sighting(
            db_session, requirement, "Acme Components", mpn_matched="ST3300657SS", qty_available=5, source_type="mouser"
        )

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 0
        assert bool(row.is_unavailable) is False

    def test_zero_qty_live_row_stays_stamped(self, db_session: Session):
        requirement = _make_requirement(db_session)
        _make_record(db_session, qty_at_mark=None)
        row = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            qty_available=0,
            source_type="digikey",
        )

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 1
        assert row.is_unavailable is True

    def test_is_authorized_flag_makes_a_row_live_class(self, db_session: Session):
        """Authorized octopart/oemsecrets/sourcengine/NC rows count as LIVE."""
        requirement = _make_requirement(db_session)
        _make_record(db_session, qty_at_mark=100)
        row = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            qty_available=150,
            source_type="octopart",
            is_authorized=True,
        )

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 0
        assert bool(row.is_unavailable) is False

    def test_o1_applies_to_different_part(self, db_session: Session):
        """An authorized catalog match is identity evidence — O1 covers ALL reasons."""
        requirement = _make_requirement(db_session)
        _make_record(db_session, reason=UnavailabilityReason.DIFFERENT_PART, qty_at_mark=100)
        row = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            qty_available=250,
            source_type="digikey",
        )

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 0
        assert bool(row.is_unavailable) is False


class TestOverrideO2Restock:
    def test_fires_at_exact_factor_boundary(self, db_session: Session):
        requirement = _make_requirement(db_session)
        _make_record(db_session, qty_at_mark=100)
        row = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            qty_available=200,
            source_type="brokerbin",
        )

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 0
        assert bool(row.is_unavailable) is False

    def test_below_factor_stays_stamped(self, db_session: Session):
        requirement = _make_requirement(db_session)
        _make_record(db_session, qty_at_mark=100)
        row = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            qty_available=199,
            source_type="brokerbin",
        )

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 1
        assert row.is_unavailable is True

    def test_strict_greater_required_even_at_factor_one(self, db_session: Session, monkeypatch):
        """An identical echo can never release regardless of knob misconfiguration."""
        from app.config import settings

        monkeypatch.setattr(settings, "unavailability_qty_jump_factor", 1.0)
        requirement = _make_requirement(db_session)
        _make_record(db_session, qty_at_mark=100)
        row = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            qty_available=100,
            source_type="brokerbin",
        )

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 1
        assert row.is_unavailable is True

    def test_null_snapshot_is_no_signal(self, db_session: Session):
        requirement = _make_requirement(db_session)
        _make_record(db_session, qty_at_mark=None)
        row = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            qty_available=100000,
            source_type="brokerbin",
        )

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 1
        assert row.is_unavailable is True

    def test_null_fresh_qty_is_no_signal(self, db_session: Session):
        requirement = _make_requirement(db_session)
        _make_record(db_session, qty_at_mark=100)
        row = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            qty_available=None,
            source_type="brokerbin",
        )

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 1
        assert row.is_unavailable is True

    def test_snapshot_zero_releases_on_any_positive_fresh_qty(self, db_session: Session):
        requirement = _make_requirement(db_session)
        _make_record(db_session, qty_at_mark=0)
        row = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            qty_available=1,
            source_type="brokerbin",
        )

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 0
        assert bool(row.is_unavailable) is False

    def test_disabled_for_different_part(self, db_session: Session):
        """More of the wrong part is still the wrong part."""
        requirement = _make_requirement(db_session)
        _make_record(db_session, reason=UnavailabilityReason.DIFFERENT_PART, qty_at_mark=100)
        row = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            qty_available=1000,
            source_type="brokerbin",
        )

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 1
        assert row.is_unavailable is True

    def test_no_record_mutation_on_o2(self, db_session: Session):
        requirement = _make_requirement(db_session)
        rec = _make_record(db_session, qty_at_mark=100)
        row = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            qty_available=500,
            source_type="brokerbin",
        )

        apply_to_fresh_sightings(db_session, requirement, [row])

        assert rec.released_at is None
        assert rec.release_trigger is None
        assert rec.qty_at_mark == 100  # stateless: snapshot untouched


class TestOverrideO3VendorDocument:
    def test_email_attachment_with_qty_releases_record(self, db_session: Session):
        requirement = _make_requirement(db_session)
        rec = _make_record(db_session, qty_at_mark=None)
        row = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            qty_available=500,
            source_type="email_attachment",
        )

        count = apply_to_fresh_sightings(db_session, requirement, [row])
        db_session.commit()

        assert count == 0
        assert bool(row.is_unavailable) is False
        assert rec.released_at is not None
        assert rec.release_trigger == RELEASE_TRIGGER_VENDOR_EMAIL
        entries = db_session.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.VENDOR_AVAILABLE).all()
        assert len(entries) == 1
        assert entries[0].requirement_id == requirement.id

    def test_email_auto_import_stamps_instead_of_releasing(self, db_session: Session):
        """Auto-mined documents are listing-class — the weekly stale stock-list re-
        upload must never release a mark."""
        requirement = _make_requirement(db_session)
        rec = _make_record(db_session, qty_at_mark=None)
        row = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            qty_available=500,
            source_type="email_auto_import",
        )

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 1
        assert row.is_unavailable is True
        assert rec.released_at is None

    def test_excess_list_stamps_instead_of_releasing(self, db_session: Session):
        requirement = _make_requirement(db_session)
        rec = _make_record(db_session, qty_at_mark=None)
        row = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            qty_available=500,
            source_type="excess_list",
        )

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 1
        assert row.is_unavailable is True
        assert rec.released_at is None

    def test_zero_qty_email_attachment_stamps(self, db_session: Session):
        requirement = _make_requirement(db_session)
        rec = _make_record(db_session, qty_at_mark=None)
        row = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            qty_available=0,
            source_type="email_attachment",
        )

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 1
        assert row.is_unavailable is True
        assert rec.released_at is None

    def test_disabled_for_different_part(self, db_session: Session):
        """A qty claim doesn't fix identity — only LIVE evidence or manual clear."""
        requirement = _make_requirement(db_session)
        rec = _make_record(db_session, reason=UnavailabilityReason.DIFFERENT_PART, qty_at_mark=None)
        row = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            qty_available=500,
            source_type="email_attachment",
        )

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 1
        assert row.is_unavailable is True
        assert rec.released_at is None


class TestSourceClassDispatch:
    """Overrides dispatch on mutually exclusive source classes (LIVE → O1, HUMAN_DIRECT
    → O3, listing → O2) — never priority order.

    Pins the S7 edge: a qty that also clears the O2 jump must NOT shadow the
    stronger evidence class.
    """

    def test_human_direct_qty_jump_releases_record_not_o2(self, db_session: Session):
        """email_attachment row with qty >= 2x snapshot fires O3 (record release), not
        O2 (row-level only) — the vendor sent a stock list, so RFQ resumes."""
        requirement = _make_requirement(db_session)
        rec = _make_record(db_session, qty_at_mark=100)
        row = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            qty_available=200,  # >= 2.0 x snapshot — would satisfy the O2 jump too
            source_type="email_attachment",
        )

        count = apply_to_fresh_sightings(db_session, requirement, [row])
        db_session.commit()

        assert count == 0
        assert bool(row.is_unavailable) is False
        assert rec.released_at is not None
        assert rec.release_trigger == RELEASE_TRIGGER_VENDOR_EMAIL
        entries = db_session.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.VENDOR_AVAILABLE).all()
        assert len(entries) == 1
        assert entries[0].requirement_id == requirement.id

    def test_live_qty_jump_takes_o1_path_no_record_mutation(self, db_session: Session):
        """LIVE row with qty >= 2x snapshot surfaces via O1 (row-level only) — never O2,
        never a release."""
        requirement = _make_requirement(db_session)
        rec = _make_record(db_session, qty_at_mark=100)
        row = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            qty_available=200,  # >= 2.0 x snapshot — O1 subsumes any O2-shaped signal
            source_type="digikey",
        )

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 0
        assert bool(row.is_unavailable) is False
        assert rec.released_at is None
        assert rec.release_trigger is None
        assert rec.qty_at_mark == 100


class TestUnknownSourceClass:
    @pytest.mark.parametrize("source_type", [None, "", "mystery_feed", "stock_list", "historical", "vendor_affinity"])
    def test_unknown_or_listing_source_stamps_and_never_releases(self, db_session: Session, source_type):
        requirement = _make_requirement(db_session)
        rec = _make_record(db_session, qty_at_mark=None)
        row = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            qty_available=999,
            source_type=source_type,
        )

        assert apply_to_fresh_sightings(db_session, requirement, [row]) == 1
        assert row.is_unavailable is True
        assert rec.released_at is None


# ── Temporal policy: per-key snapshots, re-mark, offer hook ───────────────────


class TestQtyAtMarkSnapshot:
    def test_per_key_snapshot_isolation(self, db_session: Session, test_user: User):
        """Two keys, different qtys — each record snapshots ONLY its own key's max; rows
        with empty mpn_matched count toward the primary-key record."""
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ALT-123", qty_available=5)
        _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ALT-123", qty_available=50)
        _make_sighting(db_session, requirement, "Acme Components", mpn_matched=None, qty_available=10)

        record_unavailability(
            db_session, requirement, "Acme Components", UnavailabilityReason.SOLD_ELSEWHERE, None, test_user
        )
        db_session.commit()

        by_key = {r.normalized_mpn: r for r in _records(db_session, "acme components")}
        assert by_key["alt123"].qty_at_mark == 50  # max of 5/50, never cross-key
        assert by_key["st3300657ss"].qty_at_mark == 10  # empty-mpn row → primary key

    def test_snapshot_none_when_no_qty_visible(self, db_session: Session, test_user: User):
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ST3300657SS", qty_available=None)

        record_unavailability(
            db_session, requirement, "Acme Components", UnavailabilityReason.BOUGHT_BY_US, None, test_user
        )
        db_session.commit()

        (rec,) = _records(db_session, "acme components")
        assert rec.qty_at_mark is None

    def test_requirement_id_provenance_recorded(self, db_session: Session, test_user: User):
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ST3300657SS")

        record_unavailability(
            db_session, requirement, "Acme Components", UnavailabilityReason.BOUGHT_BY_US, None, test_user
        )
        db_session.commit()

        (rec,) = _records(db_session, "acme components")
        assert rec.requirement_id == requirement.id


class TestReMark:
    def test_remark_keeps_old_snapshot_when_new_is_null(self, db_session: Session, test_user: User):
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        s = _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ST3300657SS", qty_available=50)
        record_unavailability(
            db_session, requirement, "Acme Components", UnavailabilityReason.SOLD_ELSEWHERE, None, test_user
        )
        db_session.commit()

        s.qty_available = None  # qty no longer visible on any row
        db_session.flush()
        record_unavailability(db_session, requirement, "Acme Components", UnavailabilityReason.BROKEN, None, test_user)
        db_session.commit()

        (rec,) = _records(db_session, "acme components")
        assert rec.qty_at_mark == 50  # keep-old-on-NULL: no cross-requirement clobber
        assert rec.reason == UnavailabilityReason.BROKEN

    def test_remark_resnapshots_when_qty_visible(self, db_session: Session, test_user: User):
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        s = _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ST3300657SS", qty_available=50)
        record_unavailability(
            db_session, requirement, "Acme Components", UnavailabilityReason.SOLD_ELSEWHERE, None, test_user
        )
        db_session.commit()

        s.qty_available = 120  # the just-seen echo becomes the new baseline
        db_session.flush()
        record_unavailability(
            db_session, requirement, "Acme Components", UnavailabilityReason.SOLD_ELSEWHERE, None, test_user
        )
        db_session.commit()

        (rec,) = _records(db_session, "acme components")
        assert rec.qty_at_mark == 120

    def test_remark_resets_release_state_and_refreshes_window(self, db_session: Session, test_user: User):
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ST3300657SS")
        record_unavailability(
            db_session, requirement, "Acme Components", UnavailabilityReason.SOLD_ELSEWHERE, None, test_user
        )
        db_session.commit()

        (rec,) = _records(db_session, "acme components")
        rec.released_at = datetime.now(timezone.utc) - timedelta(days=1)
        rec.release_trigger = RELEASE_TRIGGER_OFFER_RECEIVED
        rec.created_at = datetime.now(timezone.utc) - timedelta(days=40)
        db_session.flush()
        assert is_active(rec) is False

        record_unavailability(
            db_session, requirement, "Acme Components", UnavailabilityReason.SOLD_ELSEWHERE, None, test_user
        )
        db_session.commit()

        (rec,) = _records(db_session, "acme components")
        assert rec.released_at is None
        assert rec.release_trigger is None
        assert is_active(rec) is True  # created_at refreshed: one click buys a full window

    def test_remark_refreshes_requirement_provenance(self, db_session: Session, test_user: User):
        req_a = _make_requirement(db_session, primary_mpn="ST3300657SS")
        req_b = _make_requirement(db_session, primary_mpn="ST3300657SS")
        _make_sighting(db_session, req_a, "Acme Components", mpn_matched="ST3300657SS")
        record_unavailability(db_session, req_a, "Acme Components", UnavailabilityReason.BOUGHT_BY_US, None, test_user)
        db_session.commit()

        record_unavailability(db_session, req_b, "Acme Components", UnavailabilityReason.BOUGHT_BY_US, None, test_user)
        db_session.commit()

        (rec,) = _records(db_session, "acme components")
        assert rec.requirement_id == req_b.id


class TestReleaseOnOffer:
    def test_releases_active_records_except_different_part(self, db_session: Session, test_user: User):
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ALT-123")
        lot_rec = _make_record(
            db_session, key="st3300657ss", reason=UnavailabilityReason.SOLD_ELSEWHERE, requirement_id=requirement.id
        )
        identity_rec = _make_record(
            db_session, key="alt123", reason=UnavailabilityReason.DIFFERENT_PART, requirement_id=requirement.id
        )
        db_session.commit()

        released = release_on_offer(db_session, requirement, "Acme Components, Inc.", test_user)
        db_session.commit()

        assert released == 1
        assert lot_rec.released_at is not None
        assert lot_rec.release_trigger == RELEASE_TRIGGER_OFFER_RECEIVED
        assert identity_rec.released_at is None  # identity knowledge survives offers
        entries = db_session.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.VENDOR_AVAILABLE).all()
        assert len(entries) == 1

    def test_expired_record_is_not_touched(self, db_session: Session, test_user: User):
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        rec = _make_record(db_session, reason=UnavailabilityReason.SOLD_ELSEWHERE, age_days=31)
        db_session.commit()

        assert release_on_offer(db_session, requirement, "Acme Components", test_user) == 0
        assert rec.released_at is None

    def test_already_released_record_is_not_re_released(self, db_session: Session, test_user: User):
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        first_release = datetime.now(timezone.utc) - timedelta(days=2)
        rec = _make_record(
            db_session,
            reason=UnavailabilityReason.SOLD_ELSEWHERE,
            released_at=first_release,
            release_trigger=RELEASE_TRIGGER_VENDOR_EMAIL,
        )
        db_session.commit()

        assert release_on_offer(db_session, requirement, "Acme Components", test_user) == 0
        assert rec.release_trigger == RELEASE_TRIGGER_VENDOR_EMAIL

    def test_no_match_returns_zero(self, db_session: Session, test_user: User):
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        assert release_on_offer(db_session, requirement, "Globex Parts", test_user) == 0
        assert release_on_offer(db_session, requirement, "  ", test_user) == 0


# ── Temporal policy: RFQ exclusion is active-only ─────────────────────────────


class TestExcludedVendorNormsActiveOnly:
    def test_expired_record_is_not_excluded(self, db_session: Session):
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        _make_record(db_session, reason=UnavailabilityReason.SOLD_ELSEWHERE, age_days=31)
        db_session.commit()

        assert excluded_vendor_norms(db_session, [requirement]) == set()

    def test_released_record_is_not_excluded(self, db_session: Session):
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        _make_record(
            db_session,
            reason=UnavailabilityReason.SOLD_ELSEWHERE,
            released_at=datetime.now(timezone.utc),
            release_trigger=RELEASE_TRIGGER_OFFER_RECEIVED,
        )
        db_session.commit()

        assert excluded_vendor_norms(db_session, [requirement]) == set()

    def test_old_different_part_record_stays_excluded(self, db_session: Session):
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        _make_record(db_session, reason=UnavailabilityReason.DIFFERENT_PART, age_days=400)
        db_session.commit()

        assert excluded_vendor_norms(db_session, [requirement]) == {"acme components"}

    def test_unkeyable_requirement_logs_warning(self, db_session: Session):
        """IMPORTANT-6: a requirement whose primary_mpn derives no key must not
        silently widen RFQ suggestions."""
        keyless = _make_requirement(db_session, primary_mpn=None)
        db_session.commit()

        with _capture_warnings() as messages:
            assert excluded_vendor_norms(db_session, [keyless]) == set()

        assert any(str(keyless.id) in m for m in messages)


# ── Silent-failure hardening ──────────────────────────────────────────────────


class TestSilentFailureHardening:
    def test_zero_key_record_raises_and_writes_nothing(self, db_session: Session, test_user: User):
        """CRITICAL-1: no primary-MPN key and no matched-sighting keys → ValueError,
        no records, no flags, no ActivityLog."""
        requirement = _make_requirement(db_session, primary_mpn=None)
        row = _make_sighting(db_session, requirement, "Acme Components", mpn_matched=None)

        with pytest.raises(ValueError):
            record_unavailability(
                db_session, requirement, "Acme Components", UnavailabilityReason.BOUGHT_BY_US, None, test_user
            )
        db_session.commit()

        assert db_session.query(VendorPartUnavailability).count() == 0
        assert db_session.query(ActivityLog).count() == 0
        assert bool(row.is_unavailable) is False

    def test_empty_vendor_norm_record_raises(self, db_session: Session, test_user: User):
        """IMPORTANT-4: a vendor name that normalizes to nothing must not create a
        wildcard record."""
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")

        with pytest.raises(ValueError):
            record_unavailability(db_session, requirement, "  ", UnavailabilityReason.BOUGHT_BY_US, None, test_user)
        db_session.commit()

        assert db_session.query(VendorPartUnavailability).count() == 0
        assert db_session.query(ActivityLog).count() == 0

    def test_empty_vendor_norm_clear_raises(self, db_session: Session, test_user: User):
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")

        with pytest.raises(ValueError):
            clear_unavailability(db_session, requirement, "  ", test_user)
        db_session.commit()

        assert db_session.query(ActivityLog).count() == 0

    def test_null_norm_legacy_sighting_flagged_on_record(self, db_session: Session, test_user: User):
        """CRITICAL-2: a legacy row with NULL vendor_name_normalized matches via the
        shared helper's display-name fallback."""
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        legacy = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            vendor_name_normalized=None,
        )

        record_unavailability(
            db_session, requirement, "Acme Components, Inc.", UnavailabilityReason.BOUGHT_BY_US, None, test_user
        )
        db_session.commit()

        assert legacy.is_unavailable is True

    def test_null_norm_zombie_clear(self, db_session: Session, test_user: User):
        """CRITICAL-2: clearing unflags legacy NULL-norm rows — no zombie flags."""
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        legacy = _make_sighting(
            db_session,
            requirement,
            "Acme Components",
            mpn_matched="ST3300657SS",
            vendor_name_normalized=None,
        )
        legacy.is_unavailable = True
        _make_record(db_session, key="st3300657ss", requirement_id=requirement.id)
        db_session.commit()

        cleared = clear_unavailability(db_session, requirement, "Acme Components, Inc.", test_user)
        db_session.commit()

        assert cleared == 1
        assert legacy.is_unavailable is False

    def test_provenance_clear_deletes_key_drifted_record(self, db_session: Session, test_user: User):
        """IMPORTANT-3: a record whose key no longer matches the requirement's current
        keys is still deleted via requirement_id — no unclearable zombie."""
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        _make_record(db_session, key="zzz999", requirement_id=requirement.id)
        db_session.commit()

        cleared = clear_unavailability(db_session, requirement, "Acme Components", test_user)
        db_session.commit()

        assert cleared == 1
        assert _records(db_session, "acme components") == []

    def test_provenance_clear_leaves_other_requirements_records(self, db_session: Session, test_user: User):
        req_a = _make_requirement(db_session, primary_mpn="ST3300657SS")
        req_b = _make_requirement(db_session, primary_mpn="ZZZ-999")
        survivor = _make_record(db_session, key="zzz999", requirement_id=req_b.id)
        db_session.commit()

        cleared = clear_unavailability(db_session, req_a, "Acme Components", test_user)
        db_session.commit()

        assert cleared == 0
        assert db_session.get(VendorPartUnavailability, survivor.id) is not None


# ── Re-application at every sighting-persistence path (Task 3) ───────────────


class TestSearchPathReapplication:
    """app/search_service.py _save_sightings — the synchronous resurrection hole:

    the connector-aware delete + recreate must re-stamp fresh rows while the record is
    active, and leave them unstamped once it has expired.
    """

    def test_search_resurrection_stamps_fresh_listing_rows(self, db_session: Session, test_user: User):
        from app.search_service import _save_sightings

        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        _make_sighting(db_session, requirement, "Acme Components", mpn_matched="ST3300657SS", source_type="brokerbin")
        record_unavailability(
            db_session,
            requirement,
            "Acme Components",
            UnavailabilityReason.SOLD_ELSEWHERE,
            None,
            test_user,
        )
        db_session.commit()

        fresh = [
            {
                "vendor_name": "Acme Components",
                "mpn_matched": "ST3300657SS",
                "qty_available": 40,
                "unit_price": 1.25,
                "source_type": "brokerbin",
            },
            {
                "vendor_name": "Globex Parts",
                "mpn_matched": "ST3300657SS",
                "qty_available": 40,
                "unit_price": 1.10,
                "source_type": "brokerbin",
            },
        ]
        saved = _save_sightings(fresh, requirement, db_session, succeeded_sources={"brokerbin"})

        by_vendor = {s.vendor_name: s for s in saved}
        assert by_vendor["Acme Components"].is_unavailable is True
        assert bool(by_vendor["Globex Parts"].is_unavailable) is False

    def test_search_expired_record_rows_not_stamped(self, db_session: Session, test_user: User):
        from app.search_service import _save_sightings

        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        _make_record(
            db_session,
            key="st3300657ss",
            reason=UnavailabilityReason.SOLD_ELSEWHERE,  # LOT — 30d window
            age_days=31,
            requirement_id=requirement.id,
        )
        db_session.commit()

        fresh = [
            {
                "vendor_name": "Acme Components",
                "mpn_matched": "ST3300657SS",
                "qty_available": 40,
                "unit_price": 1.25,
                "source_type": "brokerbin",
            }
        ]
        saved = _save_sightings(fresh, requirement, db_session, succeeded_sources={"brokerbin"})

        assert len(saved) == 1
        assert bool(saved[0].is_unavailable) is False


class TestAsyncWriterReapplication:
    """ICS/NC browser-worker sighting writers — without re-application their results re-
    open the hole minutes after a search."""

    def test_nc_writer_stamps_fresh_rows(self, db_session: Session, test_user: User):
        from types import SimpleNamespace

        from app.services.nc_worker.result_parser import NcSighting
        from app.services.nc_worker.sighting_writer import save_nc_sightings

        requirement = _make_requirement(db_session, primary_mpn="LM317T")
        _make_record(db_session, vendor_norm="arrow electronics", key="lm317t", requirement_id=requirement.id)
        db_session.commit()

        queue_item = SimpleNamespace(requirement_id=requirement.id)
        created = save_nc_sightings(
            db_session,
            queue_item,
            [NcSighting(part_number="LM317T", quantity=5000, vendor_name="Arrow Electronics")],
        )

        assert created == 1
        row = db_session.query(Sighting).filter_by(requirement_id=requirement.id, source_type="netcomponents").one()
        assert row.is_unavailable is True

    def test_ics_writer_stamps_fresh_rows(self, db_session: Session, test_user: User):
        from types import SimpleNamespace

        from app.services.ics_worker.result_parser import IcsSighting
        from app.services.ics_worker.sighting_writer import save_ics_sightings

        requirement = _make_requirement(db_session, primary_mpn="LM317T")
        _make_record(db_session, vendor_norm="arrow electronics", key="lm317t", requirement_id=requirement.id)
        db_session.commit()

        queue_item = SimpleNamespace(requirement_id=requirement.id)
        created = save_ics_sightings(
            db_session,
            queue_item,
            [IcsSighting(part_number="LM317T", quantity=100, vendor_name="Arrow Electronics", in_stock=True)],
        )

        assert created == 1
        row = db_session.query(Sighting).filter_by(requirement_id=requirement.id, source_type="icsource").one()
        assert row.is_unavailable is True


class TestEmailAttachmentReapplication:
    """app/routers/sources.py email-attachment import — the HUMAN_DIRECT/O3 path:

    a buyer-routed attachment row with qty>0 RELEASES the record instead of stamping the
    row.
    """

    def test_routed_attachment_qty_releases_record_row_unstamped(self, db_session: Session, test_user: User):
        from app.models import VendorResponse
        from app.routers.sources import _create_sightings_from_attachment

        requirement = _make_requirement(db_session, primary_mpn="LM358N")
        rec = _make_record(
            db_session,
            vendor_norm="acme components",
            key="lm358n",
            reason=UnavailabilityReason.SOLD_ELSEWHERE,
            qty_at_mark=50,
            requirement_id=requirement.id,
        )
        vr = VendorResponse(
            requisition_id=requirement.requisition_id,
            vendor_name="Acme Components",
            vendor_email="sales@acme.example",
        )
        db_session.add(vr)
        db_session.commit()

        created = _create_sightings_from_attachment(
            db_session,
            vr,
            [{"mpn": "LM358N", "qty": 100, "unit_price": 0.50}],
        )
        db_session.commit()

        assert created == 1
        row = db_session.query(Sighting).filter_by(requirement_id=requirement.id, source_type="email_attachment").one()
        assert bool(row.is_unavailable) is False  # O3 surfaces, never stamps
        db_session.refresh(rec)
        assert rec.released_at is not None
        assert rec.release_trigger == RELEASE_TRIGGER_VENDOR_EMAIL
        assert not is_active(rec)


class TestPickerReapplication:
    """app/routers/htmx_views.py add-to-requisition picker — a manually added sighting
    for a known-dead vendor+part is surfaced flagged (user can Mark available to
    override)."""

    def test_manually_added_sighting_stamped(self, client, db_session: Session):
        requirement = _make_requirement(db_session, primary_mpn="ST3300657SS")
        _make_record(db_session, key="st3300657ss", requirement_id=requirement.id)
        db_session.commit()

        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            headers={"HX-Request": "true", "Content-Type": "application/json"},
            json={
                "requisition_id": requirement.requisition_id,
                "mpn": "ST3300657SS",
                "items": [
                    {
                        "vendor_name": "Acme Components",
                        "mpn_matched": "ST3300657SS",
                        "qty_available": 10,
                        "source_type": "nexar",
                        "score": 50,
                    }
                ],
            },
        )
        assert resp.status_code == 200

        row = db_session.query(Sighting).filter_by(requirement_id=requirement.id).one()
        assert row.is_unavailable is True


class TestInventoryJobReapplication:
    """app/jobs/inventory_jobs.py stock-list import — created sightings are grouped per
    requirement and re-stamped before the commit."""

    def test_stock_import_rows_stamped(self, db_session: Session, test_user: User):
        import asyncio
        import base64
        from unittest.mock import AsyncMock, MagicMock, patch

        requirement = _make_requirement(db_session, primary_mpn="LM317T")
        _make_record(db_session, vendor_norm="arrow", key="lm317t", requirement_id=requirement.id)
        test_user.access_token = "tok"
        db_session.commit()

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"data").decode()})
        rows = [{"mpn": "LM317T", "qty": 100, "unit_price": 0.50}]

        with (
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
            patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
            patch("app.services.activity_service.match_email_to_entity", return_value=None),
        ):
            from app.jobs.inventory_jobs import _download_and_import_stock_list

            asyncio.run(
                _download_and_import_stock_list(
                    test_user,
                    db_session,
                    message_id="m1",
                    attachment_id="a1",
                    filename="stock.csv",
                    vendor_name="Arrow",
                    vendor_email="sales@arrow.example",
                )
            )

        row = db_session.query(Sighting).filter_by(requirement_id=requirement.id, source_type="email_auto_import").one()
        assert row.is_unavailable is True


# ── Offer-hook wiring: user-initiated proof releases at five sites; auto-mined
#    and clone paths never release (maybe_release_on_offer is the single gate) ──


def _hook_record(
    db_session: Session,
    vendor: str,
    mpn: str = "LM317T",
    reason: UnavailabilityReason = UnavailabilityReason.SOLD_ELSEWHERE,
) -> VendorPartUnavailability:
    """An ACTIVE record keyed to the conftest test_requisition's primary MPN."""
    rec = VendorPartUnavailability(
        vendor_name_normalized=normalize_vendor_name(vendor),
        normalized_mpn=normalize_mpn_key(mpn),
        reason=reason,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(rec)
    db_session.commit()
    return rec


def _hook_offer_input(**kw):
    from types import SimpleNamespace

    defaults = dict(
        mpn="LM317T",
        vendor_name="Arrow Electronics",
        manufacturer="Texas Instruments",
        qty_available=1000,
        unit_price=0.50,
        currency="USD",
        lead_time="2 weeks",
        date_code="2025+",
        condition="new",
        packaging="tape_reel",
        moq=100,
        notes="Test offer",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _assert_released(db_session: Session, rec: VendorPartUnavailability) -> None:
    db_session.expire_all()
    rec = db_session.get(VendorPartUnavailability, rec.id)
    assert rec.released_at is not None
    assert rec.release_trigger == RELEASE_TRIGGER_OFFER_RECEIVED


def _assert_not_released(db_session: Session, rec: VendorPartUnavailability) -> None:
    db_session.expire_all()
    rec = db_session.get(VendorPartUnavailability, rec.id)
    assert rec.released_at is None
    assert rec.release_trigger is None


class TestOfferHookReleasingSites:
    """The five user-initiated creation/approval sites release via the shared
    maybe_release_on_offer gate."""

    def test_crm_create_offer_api_releases(self, client, db_session: Session, test_requisition):
        rec = _hook_record(db_session, "Arrow Electronics")
        requirement = test_requisition.requirements[0]
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/offers",
            json={
                "requirement_id": requirement.id,
                "vendor_name": "Arrow Electronics",
                "mpn": "LM317T",
                "qty_available": 500,
                "unit_price": 0.45,
            },
        )
        assert resp.status_code == 200
        _assert_released(db_session, rec)

    def test_crm_create_offer_api_never_releases_different_part(self, client, db_session: Session, test_requisition):
        rec = _hook_record(db_session, "Arrow Electronics", reason=UnavailabilityReason.DIFFERENT_PART)
        requirement = test_requisition.requirements[0]
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/offers",
            json={
                "requirement_id": requirement.id,
                "vendor_name": "Arrow Electronics",
                "mpn": "LM317T",
                "unit_price": 0.45,
            },
        )
        assert resp.status_code == 200
        _assert_not_released(db_session, rec)

    def test_manual_add_offer_releases(self, client, db_session: Session, test_requisition):
        rec = _hook_record(db_session, "Arrow Electronics")
        requirement = test_requisition.requirements[0]
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offer",
            data={
                "vendor_name": "Arrow Electronics",
                "mpn": "LM317T",
                "qty_available": "500",
                "requirement_id": str(requirement.id),
            },
        )
        assert resp.status_code == 200
        _assert_released(db_session, rec)

    def test_save_parsed_offers_route_releases(self, client, db_session: Session, test_requisition):
        """The user-edited parse form persists offers ACTIVE — user-initiated proof."""
        rec = _hook_record(db_session, "Arrow Electronics")
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/save-parsed-offers",
            data={
                "vendor_name": "Arrow Electronics",
                "offers[0].mpn": "LM317T",
                "offers[0].qty_available": "100",
                "offers[0].unit_price": "0.45",
            },
        )
        assert resp.status_code == 200
        _assert_released(db_session, rec)

    def test_ai_save_parsed_pending_review_does_not_release_until_approved(
        self, client, db_session: Session, test_requisition, test_user: User
    ):
        """save_parsed_offers persists PENDING_REVIEW (not proof yet); the user
        approving the pending offer is the proof that releases."""
        from unittest.mock import patch

        from app.services.ai_offer_service import save_parsed_offers as ai_save_parsed

        rec = _hook_record(db_session, "Arrow Electronics")
        with patch("app.search_service.resolve_material_card", return_value=None):
            result = ai_save_parsed(db_session, test_requisition.id, None, [_hook_offer_input()], test_user.id)
        db_session.commit()
        assert result["created"] == 1
        _assert_not_released(db_session, rec)

        resp = client.put(f"/api/offers/{result['offer_ids'][0]}/approve")
        assert resp.status_code == 200
        _assert_released(db_session, rec)

    def test_save_freeform_offers_releases(self, db_session: Session, test_requisition, test_user: User):
        from unittest.mock import patch

        from app.services.ai_offer_service import save_freeform_offers

        rec = _hook_record(db_session, "Arrow Electronics")
        with patch("app.search_service.resolve_material_card", return_value=None):
            result = save_freeform_offers(db_session, test_requisition.id, [_hook_offer_input()], test_user.id)
        db_session.commit()
        assert result["created"] == 1
        _assert_released(db_session, rec)


class TestOfferHookExcludedPaths:
    """Auto-created offers (inbox monitor, excess matching) and clones are auto-mined
    evidence / copies — never proof, never release."""

    def test_email_auto_create_does_not_release(self, db_session: Session, test_user: User, test_requisition):
        from unittest.mock import patch

        from app.email_service import _auto_create_offers_from_parse
        from app.models import Offer, VendorResponse

        rec = _hook_record(db_session, "TestVendor Inc")
        vr = VendorResponse(
            requisition_id=test_requisition.id,
            vendor_name="TestVendor Inc",
            vendor_email="sales@testvendor.com",
            confidence=0.9,
            scanned_by_user_id=test_user.id,
            status="new",
            received_at=datetime.now(timezone.utc),
            message_id="msg-unav-hook-1",
        )
        db_session.add(vr)
        db_session.commit()

        draft = {"mpn": "LM317T", "vendor_name": "TestVendor Inc", "unit_price": 1.5}
        with patch("app.services.response_parser.extract_draft_offers", return_value=[draft]):
            _auto_create_offers_from_parse(vr, {"confidence": 0.9}, db_session)
        db_session.commit()

        offer = db_session.query(Offer).filter_by(vendor_response_id=vr.id).one()
        assert offer.status == "active"  # would have released if this path were hooked
        assert offer.requirement_id == test_requisition.requirements[0].id
        _assert_not_released(db_session, rec)

    def test_excess_match_does_not_release(self, db_session: Session, test_user: User, test_requisition):
        from app.models import Company, Offer
        from app.services.excess_service import confirm_import, create_excess_list, match_excess_demand

        requirement = test_requisition.requirements[0]
        requirement.normalized_mpn = normalize_mpn_key("LM317T")
        company = Company(name="Arrow Electronics")
        db_session.add(company)
        db_session.commit()

        rec = _hook_record(db_session, "Arrow Electronics")
        el = create_excess_list(db_session, title="Excess", company_id=company.id, owner_id=test_user.id)
        confirm_import(db_session, el.id, [{"part_number": "LM317T", "quantity": 500, "asking_price": 0.45}])
        result = match_excess_demand(db_session, el.id, user_id=test_user.id)

        assert result["matches_created"] >= 1
        offer = db_session.query(Offer).filter_by(source="excess", requisition_id=test_requisition.id).one()
        assert offer.requirement_id == requirement.id
        _assert_not_released(db_session, rec)

    def test_clone_does_not_release(self, client, db_session: Session, test_requisition, test_offer):
        from app.models import Offer

        test_offer.requirement_id = test_requisition.requirements[0].id
        db_session.commit()
        rec = _hook_record(db_session, "Arrow Electronics")

        resp = client.post(f"/api/requisitions/{test_requisition.id}/clone")
        assert resp.status_code == 200
        new_id = resp.json()["id"]
        cloned = db_session.query(Offer).filter_by(requisition_id=new_id).all()
        assert len(cloned) == 1 and cloned[0].requirement_id is not None
        _assert_not_released(db_session, rec)
