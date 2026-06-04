"""Tests for the verified-material-enrichment feature.

Covers: enrichment_status / enrichment_provenance model columns (Task 1),
and (future tasks) authoritative-enrichment service logic.

Called by: pytest
Depends on: app/models/intelligence.py, tests/conftest.py (db_session fixture)
"""

from datetime import datetime, timezone

from app.models import MaterialCard


def test_new_card_defaults_to_unenriched(db_session):
    card = MaterialCard(
        normalized_mpn="teststatusdefault",
        display_mpn="TEST-STATUS-DEFAULT",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.flush()
    db_session.refresh(card)
    assert card.enrichment_status == "unenriched"
    assert card.enrichment_provenance is None
