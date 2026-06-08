"""Unit 8 — Condition global facet (broker stock condition).

Filters by material_cards.condition; renders like Lifecycle/RoHS (values with data
only), so the section stays hidden until a source populates the column.
"""

from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.services.faceted_search_service import get_global_facet_counts, search_materials_faceted


def _mk(db: Session, mpn: str, cond: str | None) -> None:
    db.add(MaterialCard(normalized_mpn=mpn, display_mpn=mpn.upper(), category="dram", condition=cond))
    db.flush()


def test_condition_filter_narrows(db_session: Session):
    _mk(db_session, "n", "New")
    _mk(db_session, "u", "Used")
    db_session.commit()
    results, total = search_materials_faceted(db_session, condition=["New"])
    assert total == 1
    assert results[0].normalized_mpn == "n"


def test_global_counts_include_condition(db_session: Session):
    _mk(db_session, "r", "Refurbished")
    db_session.commit()
    counts = get_global_facet_counts(db_session)
    assert counts["condition"].get("Refurbished") == 1


def test_global_partial_renders_condition_when_data(client, db_session: Session):
    _mk(db_session, "c1", "Refurbished")
    db_session.commit()
    resp = client.get("/v2/partials/materials/filters/global")
    assert resp.status_code == 200
    assert "Condition" in resp.text
    assert "Refurbished" in resp.text
    assert "toggleGlobalFacet('condition'" in resp.text


def test_global_partial_hides_condition_when_empty(client, db_session: Session):
    _mk(db_session, "c2", None)  # no condition data
    db_session.commit()
    resp = client.get("/v2/partials/materials/filters/global")
    assert resp.status_code == 200
    # No permanent zero rows — the Condition section is absent until data exists.
    assert "toggleGlobalFacet('condition'" not in resp.text


def test_faceted_route_accepts_condition_param(client, db_session: Session):
    _mk(db_session, "c3", "New")
    db_session.commit()
    resp = client.get("/v2/partials/materials/faceted?condition=New")
    assert resp.status_code == 200
    assert "C3" in resp.text
