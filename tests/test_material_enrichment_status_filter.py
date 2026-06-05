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


def test_statuses_filter_single(db_session):
    _mk(db_session, "v1", "verified")
    _mk(db_session, "ws1", "web_sourced")
    _mk(db_session, "ai1", "ai_inferred")
    _mk(db_session, "nf1", "not_found")
    db_session.flush()

    results, total = search_materials_faceted(db_session, statuses=["web_sourced"])
    assert {c.normalized_mpn for c in results} == {"ws1"}
    assert total == 1


def test_statuses_filter_multiple(db_session):
    _mk(db_session, "v2", "verified")
    _mk(db_session, "ws2", "web_sourced")
    _mk(db_session, "ai2", "ai_inferred")
    db_session.flush()

    results, total = search_materials_faceted(db_session, statuses=["verified", "web_sourced"])
    mpns = {c.normalized_mpn for c in results}
    assert "v2" in mpns
    assert "ws2" in mpns
    assert "ai2" not in mpns
    assert total == 2


def test_statuses_filter_none_means_no_filter(db_session):
    _mk(db_session, "v3", "verified")
    _mk(db_session, "ws3", "web_sourced")
    db_session.flush()

    results, total = search_materials_faceted(db_session, statuses=None)
    assert total >= 2


def test_statuses_filter_empty_list_means_no_filter(db_session):
    _mk(db_session, "v4", "verified")
    _mk(db_session, "ai4", "ai_inferred")
    db_session.flush()

    # An empty list is falsy → treated as no filter (all results returned)
    results, total = search_materials_faceted(db_session, statuses=[])
    assert total >= 2
