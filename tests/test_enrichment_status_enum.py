from datetime import datetime, timezone

import pytest

from app.constants import MaterialEnrichmentStatus
from app.models import MaterialCard


def _card(mpn: str) -> MaterialCard:
    return MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn.upper(),
        created_at=datetime.now(timezone.utc),
    )


def test_enum_values():
    assert MaterialEnrichmentStatus.WEB_SOURCED == "web_sourced"
    assert set(MaterialEnrichmentStatus) >= {
        MaterialEnrichmentStatus.UNENRICHED,
        MaterialEnrichmentStatus.VERIFIED,
        MaterialEnrichmentStatus.WEB_SOURCED,
        MaterialEnrichmentStatus.AI_INFERRED,
        MaterialEnrichmentStatus.NOT_FOUND,
    }


@pytest.mark.parametrize("bad_status", ["verifed", "bogus_status"])
def test_validator_rejects_bad_status(bad_status):
    card = _card("x1")
    with pytest.raises(ValueError):
        card.enrichment_status = bad_status


@pytest.mark.parametrize(
    "value,expected",
    [
        ("web_sourced", "web_sourced"),
        (MaterialEnrichmentStatus.VERIFIED, "verified"),
        (MaterialEnrichmentStatus.OEM_SOURCED, "oem_sourced"),
        ("not_catalogued", "not_catalogued"),
    ],
)
def test_validator_accepts_enum_and_literal(value, expected):
    card = _card("x2")
    card.enrichment_status = value
    assert card.enrichment_status == expected
