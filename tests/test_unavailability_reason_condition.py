"""Tests for UnavailabilityReason.condition_specific property and
CONDITION_SPECIFIC_REASONS."""

from app.constants import CONDITION_SPECIFIC_REASONS, UnavailabilityReason


def test_condition_specific_property():
    """Verify condition_specific property on UnavailabilityReason enum members."""
    # Condition-specific reasons: the mark is scoped to a specific lot/unit
    assert UnavailabilityReason.BOUGHT_BY_US.condition_specific is True
    assert UnavailabilityReason.SOLD_ELSEWHERE.condition_specific is True
    assert UnavailabilityReason.BROKEN.condition_specific is True

    # Condition-agnostic reasons: the part isn't there in ANY condition
    assert UnavailabilityReason.NOT_REALLY_THERE.condition_specific is False
    assert UnavailabilityReason.DIFFERENT_PART.condition_specific is False
    assert UnavailabilityReason.OTHER.condition_specific is False


def test_condition_specific_reasons_constant():
    """Verify CONDITION_SPECIFIC_REASONS constant is a frozenset with correct
    members."""
    assert isinstance(CONDITION_SPECIFIC_REASONS, frozenset)
    assert CONDITION_SPECIFIC_REASONS == {
        UnavailabilityReason.BOUGHT_BY_US,
        UnavailabilityReason.SOLD_ELSEWHERE,
        UnavailabilityReason.BROKEN,
    }
