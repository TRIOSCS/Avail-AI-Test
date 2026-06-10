"""Tests for durable vendor+part unavailability knowledge (Task 1: constants + model).

Verifies the UnavailabilityReason StrEnum (members + display labels), the
VendorPartUnavailability model (creation, defaults, FK to users), and the
(vendor_name_normalized, normalized_mpn) unique constraint.

Called by: pytest
Depends on: conftest.py (db_session, test_user fixtures), app/constants.py,
            app/models/vendor_part_unavailability.py
"""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.constants import UnavailabilityReason
from app.models import User, VendorPartUnavailability


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
