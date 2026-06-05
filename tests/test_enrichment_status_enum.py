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
