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


def test_decode_writes_ecc_false(db_session: Session):
    # Regression: a non-ECC module must persist ecc="false" (the string→bool corruption bug).
    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="m378b5273dh0-ck0", display_mpn="M378B5273DH0-CK0", category="dram")
    db_session.add(card)
    db_session.flush()

    decode_and_record_specs(db_session, [card.id])
    db_session.commit()
    assert _facets(db_session, card.id)["ecc"] == "false"


def test_decode_does_not_overwrite_higher_tier_category(db_session: Session):
    # A drive MPN on a card whose "dram" category came from a HIGHER tier (vendor, tier 90)
    # must NOT be re-categorized by the decode (tier 85). The ladder rejects the decode's
    # category write, so set_category returns False, categorized stays 0, and no drive specs
    # are written onto the DRAM card (record_spec rejects the cross-commodity capacity_gb).
    seed_commodity_schemas(db_session)
    card = MaterialCard(
        normalized_mpn="st4000nm0035",
        display_mpn="ST4000NM0035",
        category="dram",
        category_source="digikey_api",
        category_confidence=1.0,
        category_tier=90,
    )
    db_session.add(card)
    db_session.flush()

    stats = decode_and_record_specs(db_session, [card.id])
    assert stats["categorized"] == 0
    assert card.category == "dram"  # higher-tier category preserved
    # The card's category is still "dram", so a drive's capacity_gb has no dram schema match
    # and is rejected — nothing drive-specific lands on the DRAM card.
    assert "capacity_gb" not in _facets(db_session, card.id)


def test_decode_recategorizes_low_tier_category(db_session: Session):
    # A drive MPN on a card mis-categorized as "dram" from a LOW tier (ai_guess, tier 40)
    # IS corrected by the decode (tier 85): the ladder now governs, so the card becomes "hdd"
    # and the drive specs land. This is the override half of the new ladder semantics.
    seed_commodity_schemas(db_session)
    card = MaterialCard(
        normalized_mpn="st4000nm0035",
        display_mpn="ST4000NM0035",
        category="dram",
        category_source="claude_opus_inferred",
        category_confidence=0.4,
        category_tier=40,
    )
    db_session.add(card)
    db_session.flush()

    stats = decode_and_record_specs(db_session, [card.id])
    db_session.commit()

    assert stats["categorized"] == 1
    assert card.category == "hdd"  # corrected by the higher-tier decode
    assert card.category_source == "mpn_decode"
    assert card.category_tier == 85
    assert _facets(db_session, card.id)["capacity_gb"] == 4000


def test_decode_skips_unrecognized_mpn(db_session: Session):
    # OEM/FRU spare numbers don't match any vendor scheme → nothing written.
    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="593553-001", display_mpn="593553-001", category="hdd")
    db_session.add(card)
    db_session.flush()

    stats = decode_and_record_specs(db_session, [card.id])
    assert stats == {"decoded": 0, "written": 0, "categorized": 0}


def test_decode_categorizes_uncategorized_card(db_session: Session):
    # A card with NO category but a deterministically-decodable MPN gets categorized FROM the
    # decode (regex-gated commodity), then its specs are written. This is what unblocks the
    # existing inventory, where most decodable cards have a NULL category.
    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="st4000nm0035", display_mpn="ST4000NM0035", category=None)
    db_session.add(card)
    db_session.flush()

    stats = decode_and_record_specs(db_session, [card.id])
    db_session.commit()

    assert stats["categorized"] == 1
    assert stats["decoded"] == 1
    assert stats["written"] >= 3
    assert card.category == "hdd"  # set from the decode
    assert card.category_source == "mpn_decode"  # provenance recorded via set_category
    assert card.category_tier == 85
    f = _facets(db_session, card.id)
    assert f["capacity_gb"] == 4000
    assert f["form_factor"] == '3.5"'


def test_savepoint_isolates_a_failing_card(db_session: Session, monkeypatch):
    # If a card's spec write raises mid-card, the per-card SAVEPOINT must roll back that card's
    # partial state (including a categorize-from-null) WITHOUT poisoning the shared transaction —
    # so sibling cards in the same batch still commit and the counters stay honest.
    seed_commodity_schemas(db_session)
    bad = MaterialCard(normalized_mpn="st4000nm0035", display_mpn="ST4000NM0035", category=None)
    # The good card already carries a higher-tier (vendor) "hdd" category, so the decode does
    # NOT re-categorize it — isolating this test to the savepoint/rollback behavior on `bad`.
    good = MaterialCard(
        normalized_mpn="st8000nm0055",
        display_mpn="ST8000NM0055",
        category="hdd",
        category_source="digikey_api",
        category_confidence=1.0,
        category_tier=90,
    )
    db_session.add_all([bad, good])
    db_session.flush()

    import app.services.mpn_decoder.writer as writer_mod

    real_record_spec = writer_mod.record_spec

    def flaky(db, card_id, *args, **kwargs):
        if card_id == bad.id:
            db.flush()  # flush the pending categorize, then fail the way a DB error would
            raise RuntimeError("simulated flush failure")
        return real_record_spec(db, card_id, *args, **kwargs)

    monkeypatch.setattr(writer_mod, "record_spec", flaky)

    stats = decode_and_record_specs(db_session, [bad.id, good.id])
    db_session.commit()  # must NOT raise — the bad card's savepoint kept the transaction clean

    assert stats["decoded"] == 1  # only the good card
    assert stats["categorized"] == 0
    assert stats["written"] >= 3
    assert _facets(db_session, good.id)["capacity_gb"] == 8000
    assert _facets(db_session, bad.id) == {}  # bad card fully rolled back
    assert bad.category is None  # categorize-from-null did NOT leak past the rollback
