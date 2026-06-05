from datetime import datetime, timezone

import pytest

from app.constants import MaterialEnrichmentStatus
from app.models import MaterialCard


def test_enum_values():
    assert MaterialEnrichmentStatus.WEB_SOURCED == "web_sourced"
    assert set(MaterialEnrichmentStatus) >= {
        MaterialEnrichmentStatus.UNENRICHED,
        MaterialEnrichmentStatus.VERIFIED,
        MaterialEnrichmentStatus.WEB_SOURCED,
        MaterialEnrichmentStatus.AI_INFERRED,
        MaterialEnrichmentStatus.NOT_FOUND,
    }


def test_validator_rejects_bad_status(db_session):
    card = MaterialCard(normalized_mpn="x1", display_mpn="X1", created_at=datetime.now(timezone.utc))
    with pytest.raises(ValueError):
        card.enrichment_status = "verifed"  # typo


def test_validator_accepts_enum_and_literal(db_session):
    card = MaterialCard(normalized_mpn="x2", display_mpn="X2", created_at=datetime.now(timezone.utc))
    card.enrichment_status = "web_sourced"
    assert card.enrichment_status == "web_sourced"
    card.enrichment_status = MaterialEnrichmentStatus.VERIFIED
    assert card.enrichment_status == "verified"


def test_validator_accepts_oem_tiers():
    from app.constants import MaterialEnrichmentStatus
    from app.models import MaterialCard

    c = MaterialCard(display_mpn="01HW917", normalized_mpn="01hw917")
    c.enrichment_status = MaterialEnrichmentStatus.OEM_SOURCED
    assert c.enrichment_status == "oem_sourced"
    c.enrichment_status = "not_catalogued"
    assert c.enrichment_status == "not_catalogued"


def test_validator_still_rejects_junk():
    import pytest

    from app.models import MaterialCard

    c = MaterialCard(display_mpn="X", normalized_mpn="x")
    with pytest.raises(ValueError):
        c.enrichment_status = "bogus_status"
