# tests/test_offer_condition_validator.py
# What: Offer.condition @validates normalization — P2.5 leftover (enforce OfferCondition
#       so raw strings can't silently diverge; legacy spellings normalize, unknown values
#       pass through with a logged warning rather than raising).
# Called by: pytest. Depends on: app.models.offers.Offer, app.constants.OfferCondition,
#       app.services.offer_qualification.normalize_offer_condition.
import pytest
from loguru import logger

from app.constants import OfferCondition
from app.models.offers import Offer


def _make_offer(db_session, test_requisition, test_user, condition):
    o = Offer(
        requisition_id=test_requisition.id,
        vendor_name="Arrow",
        mpn="LM317T",
        condition=condition,
        entered_by_id=test_user.id,
    )
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    return o


class TestOfferConditionValidatorNormalization:
    """Canonical OfferCondition members and case variants normalize to the enum
    value."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("new", "new"),
            ("New", "new"),
            ("NEW", "new"),
            ("new_no_pkg", "new_no_pkg"),
            ("New No Pkg", "new_no_pkg"),
            ("pulls", "pulls"),
            ("Pulls", "pulls"),
            ("refurb", "refurb"),
            ("Refurb", "refurb"),
        ],
    )
    def test_canonical_and_case_variants(self, raw, expected, db_session, test_requisition, test_user):
        offer = _make_offer(db_session, test_requisition, test_user, raw)
        assert offer.condition == expected
        assert offer.condition == OfferCondition(expected)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("used", "pulls"),
            ("Used", "pulls"),
            ("pull", "pulls"),
            ("pulled", "pulls"),
            ("refurbished", "refurb"),
            ("Refurbished", "refurb"),
            ("recertified", "refurb"),
        ],
    )
    def test_documented_legacy_values_normalize(self, raw, expected, db_session, test_requisition, test_user):
        """Legacy write paths passing 'used'/'refurbished'/etc. keep working — they
        normalize onto the live OfferCondition vocabulary instead of diverging from
        it."""
        offer = _make_offer(db_session, test_requisition, test_user, raw)
        assert offer.condition == expected


class TestOfferConditionValidatorUnknownValues:
    def test_none_condition_passes_through(self, db_session, test_requisition, test_user):
        offer = _make_offer(db_session, test_requisition, test_user, None)
        assert offer.condition is None

    def test_empty_string_passes_through(self, db_session, test_requisition, test_user):
        offer = _make_offer(db_session, test_requisition, test_user, "")
        assert offer.condition == ""

    def test_unknown_value_passes_through_unchanged_with_warning(self, db_session, test_requisition, test_user):
        """Data-safety: an off-vocab condition never raises — it is stored as-is and a
        warning is logged so the drift is visible without breaking the write."""
        captured: list[str] = []
        sink_id = logger.add(captured.append, level="WARNING", format="{message}")
        try:
            offer = _make_offer(db_session, test_requisition, test_user, "garbage_vocab")
        finally:
            logger.remove(sink_id)

        assert offer.condition == "garbage_vocab"
        blob = "".join(captured)
        assert "garbage_vocab" in blob
        assert "Unexpected offer condition" in blob
