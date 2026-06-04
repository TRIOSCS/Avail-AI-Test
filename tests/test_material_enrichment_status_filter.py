from datetime import datetime, timezone

from app.models import MaterialCard
from app.services.faceted_search_service import search_materials_faceted


def _mk(db, mpn, status):
    c = MaterialCard(
        normalized_mpn=mpn,
        display_mpn=mpn.upper(),
        enrichment_status=status,
        created_at=datetime.now(timezone.utc),
    )
    db.add(c)
    return c


def test_verified_only_filter(db_session):
    _mk(db_session, "verifiedone", "verified")
    _mk(db_session, "guessedone", "ai_inferred")
    _mk(db_session, "missingone", "not_found")
    db_session.flush()

    all_cards, total_all = search_materials_faceted(db_session)
    assert total_all >= 3

    verified, total_v = search_materials_faceted(db_session, verified_only=True)
    assert {c.normalized_mpn for c in verified} == {"verifiedone"}
    assert total_v == 1
