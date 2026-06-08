"""Unit 3 — the worker decode pass writes decoded specs via record_spec, with guards."""

from sqlalchemy.orm import Session

from app.models import MaterialCard, MaterialSpecFacet
from app.services.commodity_registry import seed_commodity_schemas
from app.services.mpn_decoder.writer import decode_and_record_specs


def _facets(db: Session, card_id: int) -> dict:
    rows = db.query(MaterialSpecFacet).filter_by(material_card_id=card_id).all()
    return {r.spec_key: (r.value_text if r.value_text is not None else r.value_numeric) for r in rows}


def test_decode_writes_facets_for_known_hdd(db_session: Session):
    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="st4000nm0035", display_mpn="ST4000NM0035", category="hdd")
    db_session.add(card)
    db_session.flush()

    stats = decode_and_record_specs(db_session, [card.id])
    db_session.commit()

    assert stats["decoded"] == 1
    assert stats["written"] >= 3
    f = _facets(db_session, card.id)
    assert f["form_factor"] == '3.5"'
    assert f["usage_class"] == "Enterprise / Datacenter"
    assert f["capacity_gb"] == 4000


def test_decode_writes_dram(db_session: Session):
    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="m393a2k43db3-cwe", display_mpn="M393A2K43DB3-CWE", category="dram")
    db_session.add(card)
    db_session.flush()

    decode_and_record_specs(db_session, [card.id])
    db_session.commit()
    f = _facets(db_session, card.id)
    assert f["ddr_type"] == "DDR4"
    assert f["form_factor"] == "RDIMM"
    assert f["ecc"] == "true"


def test_decode_skips_on_category_mismatch(db_session: Session):
    # A drive MPN on a card mis-categorized as dram must NOT write (commodity guard prevents
    # writing a drive's capacity onto a DRAM card via the shared capacity_gb key).
    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="st4000nm0035", display_mpn="ST4000NM0035", category="dram")
    db_session.add(card)
    db_session.flush()

    stats = decode_and_record_specs(db_session, [card.id])
    assert stats == {"decoded": 0, "written": 0}
    assert _facets(db_session, card.id) == {}


def test_decode_skips_unrecognized_mpn(db_session: Session):
    # OEM/FRU spare numbers don't match any vendor scheme → nothing written.
    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="593553-001", display_mpn="593553-001", category="hdd")
    db_session.add(card)
    db_session.flush()

    stats = decode_and_record_specs(db_session, [card.id])
    assert stats == {"decoded": 0, "written": 0}
