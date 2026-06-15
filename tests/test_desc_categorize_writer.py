"""The categorize stage (writer.categorize_and_record): NULL-only fill + tier-83 ladder.

Fixtures create cards with category=None (NULL) and assert the categorizer SETS the right
category via the F1 ladder — NEVER hand-set an off-vocab category string (the ladder /
@validates guard reject off-vocab direct assignment).
"""

from sqlalchemy.orm import Session

from app.models import MaterialCard, MaterialSpecFacet
from app.services.commodity_registry import seed_commodity_schemas
from app.services.desc_extractor.writer import categorize_and_record
from app.services.spec_tiers import set_category


def _facets(db: Session, card_id: int) -> dict:
    rows = db.query(MaterialSpecFacet).filter_by(material_card_id=card_id).all()
    return {r.spec_key: (r.value_text if r.value_text is not None else r.value_numeric) for r in rows}


def _uncategorized_card(db: Session, mpn: str, description: str) -> MaterialCard:
    """A NULL-category card (the real target population).

    Never hand-set category.
    """
    card = MaterialCard(normalized_mpn=mpn.lower(), display_mpn=mpn, category=None, description=description)
    db.add(card)
    db.flush()
    return card


def test_categorizes_null_card_and_fills_facets(db_session: Session):
    seed_commodity_schemas(db_session)
    card = _uncategorized_card(db_session, "00AR327", 'HD, 450GB, 15KRPM, 3.5", Fibre Channel')

    categorized, written = categorize_and_record(db_session, card)
    db_session.commit()

    assert categorized is True
    assert card.category == "hdd"
    # Category written THROUGH the ladder at desc_parse / tier 83 — not assigned directly.
    assert card.category_source == "desc_parse"
    assert card.category_tier == 83
    assert card.category_confidence == 0.90
    # Facets follow in the SAME transaction, also at desc_parse.
    assert written >= 1
    f = _facets(db_session, card.id)
    assert f["capacity_gb"] == 450
    assert card.specs_structured["capacity_gb"]["source"] == "desc_parse"


def test_never_overwrites_existing_category(db_session: Session):
    # NULL-only guard: a card already categorized (even via the ladder) is left untouched —
    # categorization is fill-only, never a reclassifier.
    seed_commodity_schemas(db_session)
    card = _uncategorized_card(db_session, "X1", 'HD, 450GB, 15KRPM, 3.5", Fibre Channel')
    # Pre-categorize as ssd through the ladder (the sanctioned path), THEN try categorize.
    assert set_category(card, "ssd", source="mpn_decode", confidence=0.95)
    db_session.flush()

    categorized, written = categorize_and_record(db_session, card)
    db_session.commit()

    assert categorized is False
    assert written == 0
    assert card.category == "ssd"  # untouched
    assert card.category_source == "mpn_decode"


def test_no_grammar_match_leaves_card_uncategorized(db_session: Session):
    seed_commodity_schemas(db_session)
    # MPN-as-description / no commodity signal — the grammar declines.
    card = _uncategorized_card(db_session, "GRM155R71C104MA88D", "GRM155R71C104MA88D")

    categorized, written = categorize_and_record(db_session, card)
    db_session.commit()

    assert (categorized, written) == (False, 0)
    assert card.category is None
    assert _facets(db_session, card.id) == {}


def test_empty_description_is_noop(db_session: Session):
    seed_commodity_schemas(db_session)
    card = _uncategorized_card(db_session, "EMPTY1", "")
    assert categorize_and_record(db_session, card) == (False, 0)
    assert card.category is None


def test_fru_desc_channel_writes_at_tier_82(db_session: Session):
    # A card with no usable OWN description categorizes from a passed FRU description at
    # fru_desc_parse / tier 82 (the one-hop prose ranks below own-desc 83).
    seed_commodity_schemas(db_session)
    card = _uncategorized_card(db_session, "FRU100", "FRU100")  # own desc == MPN, unusable
    fru_desc = "HDD, 6Gbps 1.2TB 10K 2.5 Inch HDD, IBM"

    categorized, written = categorize_and_record(
        db_session, card, description=fru_desc, source="fru_desc_parse", confidence=0.90
    )
    db_session.commit()

    assert categorized is True
    assert card.category == "hdd"
    assert card.category_source == "fru_desc_parse"
    assert card.category_tier == 82
    assert written >= 1
    assert card.specs_structured["capacity_gb"]["source"] == "fru_desc_parse"


def test_categorizes_phase2_commodity_with_no_extractor(db_session: Session):
    # A cables card (no spec extractor exists) still CATEGORIZES — written=0 facets is
    # correct and expected, the category fill is the value.
    seed_commodity_schemas(db_session)
    card = _uncategorized_card(db_session, "CBL55", "CABLE, LVDS 40-pin display harness 500mm")

    categorized, written = categorize_and_record(db_session, card)
    db_session.commit()

    assert categorized is True
    assert card.category == "cables"
    assert card.category_tier == 83
    assert written == 0  # no cables extractor; facets follow only where one exists


def test_savepoint_rolls_back_card_on_facet_failure(db_session: Session, monkeypatch):
    # A DB-level failure while writing facets must roll back the WHOLE card (category +
    # facets are one atomic unit) via the per-card SAVEPOINT and re-raise — never strand a
    # category with no facets, and keep the OUTER transaction usable.
    seed_commodity_schemas(db_session)
    card = _uncategorized_card(db_session, "BAD1", 'HD, 450GB, 15KRPM, 3.5", Fibre Channel')
    db_session.commit()  # persist the card so the savepoint rollback can't expunge it

    import app.services.desc_extractor.writer as writer_mod

    def boom(*_a, **_k):
        raise RuntimeError("simulated facet failure")

    monkeypatch.setattr(writer_mod, "record_spec", boom)

    try:
        categorize_and_record(db_session, card)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the facet failure to propagate")

    # The SAVEPOINT rolled back the category set + the empty facet write; the outer
    # transaction is still usable (no full rollback needed).
    db_session.expire(card)
    assert card.category is None
    assert _facets(db_session, card.id) == {}
    # Outer txn still works: a fresh card categorizes normally.
    good = _uncategorized_card(db_session, "GOOD1", "CABLE, LVDS 40-pin harness 500mm")
    monkeypatch.undo()
    assert categorize_and_record(db_session, good) == (True, 0)
    db_session.commit()
    assert good.category == "cables"
