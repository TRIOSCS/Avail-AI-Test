"""Writer — crosswalk_and_record_specs: FRU cards inherit the strict-intersected decode
of their approved mfg_model links via record_spec (source="fru_matrix_decode",
confidence 0.93), with the category fill / mismatch-skip / savepoint / single-query
guarantees of the spec (D1–D5, D7, D9–D11)."""

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models import FruLink, MaterialCard, MaterialSpecFacet
from app.services.commodity_registry import seed_commodity_schemas
from app.services.fru_crosswalk_enrich import (
    FRU_DECODE_CONFIDENCE,
    FRU_DECODE_SOURCE,
    crosswalk_and_record_specs,
)
from app.utils.normalization import normalize_mpn_key

ZERO_STATS = {
    "matched": 0,
    "decoded": 0,
    "written": 0,
    "categorized": 0,
    "dropped_conflict": 0,
    "commodity_conflict": 0,
    "category_mismatch": 0,
}


def _facets(db: Session, card_id: int) -> dict:
    rows = db.query(MaterialSpecFacet).filter_by(material_card_id=card_id).all()
    return {r.spec_key: (r.value_text if r.value_text is not None else r.value_numeric) for r in rows}


def _card(db: Session, mpn: str, category: str | None = None, **kw) -> MaterialCard:
    card = MaterialCard(normalized_mpn=mpn.lower(), display_mpn=mpn, category=category, **kw)
    db.add(card)
    db.flush()
    return card


def _link(
    db: Session, fru: str, related: str, mfg: str | None = "Seagate", kind: str = "mfg_model", sheet: str = "Main"
) -> FruLink:
    link = FruLink(
        fru_raw=fru,
        fru_norm=normalize_mpn_key(fru),
        related_raw=related,
        related_norm=normalize_mpn_key(related),
        rel_kind=kind,
        manufacturer=mfg,
        source_sheet=sheet,
    )
    db.add(link)
    db.flush()
    return link


def test_writer_writes_intersected_specs_with_source_and_confidence(db_session: Session):
    # Two approved substitutes that agree on form/usage but differ on capacity: the
    # shared keys write at 0.93/"fru_matrix_decode", the conflicting key is dropped.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "00AJ141", category="hdd")
    _link(db_session, "00AJ141", "ST4000NM0035")
    _link(db_session, "00AJ141", "ST8000NM0055")

    stats = crosswalk_and_record_specs(db_session, [card.id])
    db_session.commit()

    assert stats == {
        "matched": 1,
        "decoded": 1,
        "written": 2,
        "categorized": 0,
        "dropped_conflict": 1,  # capacity_gb 4000 vs 8000
        "commodity_conflict": 0,
        "category_mismatch": 0,
    }
    f = _facets(db_session, card.id)
    assert f == {"form_factor": '3.5"', "usage_class": "Enterprise / Datacenter"}
    entry = card.specs_structured["form_factor"]
    assert entry["source"] == FRU_DECODE_SOURCE == "fru_matrix_decode"
    assert entry["confidence"] == FRU_DECODE_CONFIDENCE == 0.93


def test_single_model_writes_all_specs_and_categorizes_null_category(db_session: Session):
    # Intersection of one → all its specs write; a NULL-category FRU card is
    # categorized from the agreed (regex-gated) commodity before the record_spec loop.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "00AJ141", category=None)
    _link(db_session, "00AJ141", "ST4000NM0035")

    stats = crosswalk_and_record_specs(db_session, [card.id])
    db_session.commit()

    assert stats["matched"] == 1
    assert stats["decoded"] == 1
    assert stats["categorized"] == 1
    assert stats["written"] == 3
    assert card.category == "hdd"
    f = _facets(db_session, card.id)
    assert f["capacity_gb"] == 4000
    assert f["form_factor"] == '3.5"'


def test_category_mismatch_skips_card(db_session: Session):
    # An existing category is authoritative — a dram card whose FRU links decode hdd
    # gets NOTHING asserted (never overwritten, never written-around).
    seed_commodity_schemas(db_session)
    card = _card(db_session, "00AJ141", category="dram")
    _link(db_session, "00AJ141", "ST4000NM0035")

    stats = crosswalk_and_record_specs(db_session, [card.id])

    assert stats["category_mismatch"] == 1
    assert stats["written"] == 0
    assert stats["categorized"] == 0
    assert card.category == "dram"
    assert _facets(db_session, card.id) == {}


def test_commodity_conflict_skips_card(db_session: Session):
    # Substitutes that can't agree on what they ARE (hdd vs ssd) → the card is
    # skipped entirely; a NULL category is NOT filled from a conflicted decode.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "00AJ141", category=None)
    _link(db_session, "00AJ141", "ST4000NM0035")
    _link(db_session, "00AJ141", "MZQL21T9HCJR", mfg="Samsung")

    stats = crosswalk_and_record_specs(db_session, [card.id])

    assert stats["matched"] == 1
    assert stats["decoded"] == 1
    assert stats["commodity_conflict"] == 1
    assert stats["written"] == 0
    assert stats["categorized"] == 0
    assert card.category is None
    assert _facets(db_session, card.id) == {}


def test_card_manufacturer_never_written(db_session: Session):
    # D4: the FRU card keeps its IBM/Lenovo manufacturer context — the drive vendor on
    # the link (Seagate) is display-only and never copied onto the card.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "00AJ141", category="hdd", manufacturer="IBM")
    _link(db_session, "00AJ141", "ST4000NM0035", mfg="Seagate")

    stats = crosswalk_and_record_specs(db_session, [card.id])
    db_session.commit()

    assert stats["written"] == 3
    assert card.manufacturer == "IBM"


def test_confidence_guard_skips_higher_prior_overwrites_lower(db_session: Session):
    # D7: a prior key held at confidence > 0.93 (mpn_decode 0.95) is skipped; a prior
    # held lower (spec_extraction 0.85) is overwritten — record_spec alone is
    # latest-write-wins, so this pre-gate is what keeps the decode baseline authoritative.
    seed_commodity_schemas(db_session)
    card = _card(
        db_session,
        "00AJ141",
        category="hdd",
        specs_structured={
            "capacity_gb": {"value": 8000, "source": "mpn_decode", "confidence": 0.95, "updated_at": "x"},
            "form_factor": {"value": '2.5"', "source": "spec_extraction", "confidence": 0.85, "updated_at": "x"},
        },
    )
    _link(db_session, "00AJ141", "ST4000NM0035")

    stats = crosswalk_and_record_specs(db_session, [card.id])
    db_session.commit()

    assert stats["written"] == 2  # form_factor overwritten + usage_class fresh; capacity skipped
    specs = card.specs_structured
    assert specs["capacity_gb"] == {"value": 8000, "source": "mpn_decode", "confidence": 0.95, "updated_at": "x"}
    assert specs["form_factor"]["value"] == '3.5"'
    assert specs["form_factor"]["source"] == FRU_DECODE_SOURCE
    assert specs["usage_class"]["source"] == FRU_DECODE_SOURCE


def test_duplicate_links_across_sheets_decode_once(db_session: Session, monkeypatch):
    # Cross-sheet duplicates of the same related model collapse into one decode call
    # (the per-FRU link set), and the single-model intersection still writes.
    import app.services.fru_crosswalk_enrich as mod

    seed_commodity_schemas(db_session)
    card = _card(db_session, "00AJ141", category="hdd")
    _link(db_session, "00AJ141", "ST4000NM0035", sheet="Main")
    _link(db_session, "00AJ141", "ST4000NM0035", sheet="xSeries")

    calls: list[str] = []
    real_decode = mod.decode_mpn

    def counting(mpn, manufacturer=None):
        calls.append(mpn)
        return real_decode(mpn, manufacturer)

    monkeypatch.setattr(mod, "decode_mpn", counting)

    stats = crosswalk_and_record_specs(db_session, [card.id])

    assert calls == ["ST4000NM0035"]
    assert stats["written"] == 3
    assert stats["dropped_conflict"] == 0


def test_non_mfg_model_links_excluded(db_session: Session):
    # D1: drive_pn (and every other rel_kind) is out of scope this wave — a FRU with
    # only non-mfg_model links is not even "matched".
    seed_commodity_schemas(db_session)
    card = _card(db_session, "00AJ141", category="hdd")
    _link(db_session, "00AJ141", "ST4000NM0035", kind="drive_pn")
    _link(db_session, "00AJ141", "00AJ144", mfg=None, kind="option")

    stats = crosswalk_and_record_specs(db_session, [card.id])

    assert stats == ZERO_STATS
    assert _facets(db_session, card.id) == {}


def test_reverse_only_card_gets_nothing(db_session: Session):
    # D5: a card whose norm matches only related_norm (it IS a mfg_model) inherits
    # nothing — its own MPN already decodes directly via the mpn_decode pass.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "ST4000NM0035", category="hdd")
    _link(db_session, "00AJ141", "ST4000NM0035")

    stats = crosswalk_and_record_specs(db_session, [card.id])

    assert stats == ZERO_STATS
    assert _facets(db_session, card.id) == {}


def test_no_link_batch_zero_stats_and_no_decode_calls(db_session: Session, monkeypatch):
    import app.services.fru_crosswalk_enrich as mod

    seed_commodity_schemas(db_session)
    card = _card(db_session, "593553-001", category="hdd")

    calls: list[str] = []
    monkeypatch.setattr(mod, "decode_mpn", lambda mpn, manufacturer=None: calls.append(mpn))

    stats = crosswalk_and_record_specs(db_session, [card.id])

    assert stats == ZERO_STATS
    assert calls == []


def test_savepoint_isolates_a_failing_card(db_session: Session, monkeypatch):
    # D9: a record_spec failure on card 2 of 3 rolls back ONLY that card (including a
    # categorize-from-null) without poisoning the shared transaction — siblings persist,
    # the counters stay honest, and the session still commits.
    import app.services.fru_crosswalk_enrich as mod

    seed_commodity_schemas(db_session)
    card1 = _card(db_session, "00AJ141", category=None)
    bad = _card(db_session, "00AJ142", category=None)
    card3 = _card(db_session, "00AJ143", category=None)
    _link(db_session, "00AJ141", "ST4000NM0035")
    _link(db_session, "00AJ142", "ST8000NM0055")
    _link(db_session, "00AJ143", "MZQL21T9HCJR", mfg="Samsung")

    real_record_spec = mod.record_spec

    def flaky(db, card_id, *args, **kwargs):
        if card_id == bad.id:
            db.flush()  # flush the pending categorize, then fail the way a DB error would
            raise RuntimeError("simulated flush failure")
        return real_record_spec(db, card_id, *args, **kwargs)

    monkeypatch.setattr(mod, "record_spec", flaky)

    stats = crosswalk_and_record_specs(db_session, [card1.id, bad.id, card3.id])
    db_session.commit()  # must NOT raise — the bad card's savepoint kept the transaction clean

    assert stats["matched"] == 3
    assert stats["decoded"] == 2  # only the clean cards
    assert stats["categorized"] == 2
    assert stats["written"] == 6
    assert _facets(db_session, card1.id)["capacity_gb"] == 4000
    assert _facets(db_session, card3.id)["capacity_gb"] == 1920
    assert _facets(db_session, bad.id) == {}  # bad card fully rolled back
    assert bad.category is None  # categorize-from-null did NOT leak past the rollback


def test_links_resolved_via_exactly_one_select(db_session: Session):
    # D10: the whole batch resolves its links through ONE fru_links SELECT — no N+1.
    engine = db_session.get_bind()

    seed_commodity_schemas(db_session)
    cards = []
    for i, (fru, model) in enumerate(
        [("00AJ141", "ST4000NM0035"), ("00AJ142", "ST8000NM0055"), ("00AJ143", "MZQL21T9HCJR")]
    ):
        cards.append(_card(db_session, fru, category=None))
        _link(db_session, fru, model, mfg="Samsung" if i == 2 else "Seagate")

    fru_link_selects: list[str] = []

    def counter(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "fru_links" in statement:
            fru_link_selects.append(statement)

    event.listen(engine, "before_cursor_execute", counter)
    try:
        stats = crosswalk_and_record_specs(db_session, [c.id for c in cards])
    finally:
        event.remove(engine, "before_cursor_execute", counter)

    assert len(fru_link_selects) == 1, fru_link_selects
    assert stats["matched"] == 3
    assert stats["written"] == 9
