"""Unit 1 — facet counts must self-exclude (OR-within-facet, AND-across-facets).

Selecting one value in a facet must NOT collapse that facet's sibling values to 0; only
OTHER facets narrow it. A different facet IS narrowed by the active selection.

SQL is plain (distinct subquery + IN + grouped COUNT(DISTINCT)) — no SQLite-only
behavior, so it behaves identically on Postgres.
"""

from sqlalchemy.orm import Session

from app.models import MaterialCard, MaterialSpecFacet
from app.services.faceted_search_service import get_facet_counts


def _card(db: Session, mpn: str, interface: str, form_factor: str) -> None:
    card = MaterialCard(normalized_mpn=mpn, display_mpn=mpn.upper(), category="hdd")
    db.add(card)
    db.flush()
    db.add(MaterialSpecFacet(material_card_id=card.id, category="hdd", spec_key="interface", value_text=interface))
    db.add(MaterialSpecFacet(material_card_id=card.id, category="hdd", spec_key="form_factor", value_text=form_factor))
    db.flush()


def test_selected_facet_does_not_collapse_its_siblings(db_session: Session):
    _card(db_session, "d1", "SATA", '3.5"')
    _card(db_session, "d2", "SAS", '2.5"')
    db_session.commit()

    counts = get_facet_counts(db_session, "hdd", active_filters={"interface": ["SATA"]})

    # interface (the actively-filtered facet) shows BOTH values — sibling not collapsed.
    assert counts["interface"] == {"SATA": 1, "SAS": 1}
    # form_factor (a DIFFERENT facet) IS narrowed by interface=SATA → only the SATA card's value.
    assert counts["form_factor"] == {'3.5"': 1}


def test_unfiltered_counts_reflect_full_set(db_session: Session):
    _card(db_session, "d1", "SATA", '3.5"')
    _card(db_session, "d2", "SAS", '2.5"')
    db_session.commit()

    counts = get_facet_counts(db_session, "hdd", active_filters=None)
    assert counts["interface"] == {"SATA": 1, "SAS": 1}
    assert counts["form_factor"] == {'3.5"': 1, '2.5"': 1}


def test_multi_select_within_facet_keeps_all_values(db_session: Session):
    _card(db_session, "d1", "SATA", '3.5"')
    _card(db_session, "d2", "SAS", '2.5"')
    _card(db_session, "d3", "SCSI", '3.5"')
    db_session.commit()

    # Two values selected in the same facet — all three values still counted (OR-within).
    counts = get_facet_counts(db_session, "hdd", active_filters={"interface": ["SATA", "SAS"]})
    assert counts["interface"] == {"SATA": 1, "SAS": 1, "SCSI": 1}
    # form_factor narrowed to the SATA+SAS cards (3.5" from d1, 2.5" from d2).
    assert counts["form_factor"] == {'3.5"': 1, '2.5"': 1}
