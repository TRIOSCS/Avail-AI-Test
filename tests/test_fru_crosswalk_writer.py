"""Writer — crosswalk_and_record_specs: FRU cards inherit the strict-intersected decode
of their approved mfg_model links via record_spec (source="fru_matrix_decode",
confidence 0.93), with the category fill / mismatch-skip / per-FRU + per-card isolation
/ single-query guarantees stated inline on each test."""

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
    "failed": 0,
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
        "failed": 0,
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
    # The FRU card keeps its IBM/Lenovo manufacturer context — the drive vendor on
    # the link (Seagate) is display-only and never copied onto the card.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "00AJ141", category="hdd", manufacturer="IBM")
    _link(db_session, "00AJ141", "ST4000NM0035", mfg="Seagate")

    stats = crosswalk_and_record_specs(db_session, [card.id])
    db_session.commit()

    assert stats["written"] == 3
    assert card.manufacturer == "IBM"


def test_ladder_skips_higher_tier_prior_overwrites_lower(db_session: Session):
    # record_spec's F1 tier ladder arbitrates: a prior mpn_decode key (tier 85,
    # backfilled in-memory from its source) beats the incoming fru_matrix_decode (84)
    # and is skipped; a prior spec_extraction key (tier 60) loses and is overwritten.
    # The writer carries NO pre-gate of its own.
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
    # The mpn_decode prior survives untouched, except record_spec's in-memory legacy
    # backfill stamps the tier it derived from the source (85) onto the entry.
    assert specs["capacity_gb"] == {
        "value": 8000,
        "source": "mpn_decode",
        "confidence": 0.95,
        "updated_at": "x",
        "tier": 85,
    }
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
    # drive_pn (and every other rel_kind) is out of scope this wave — a FRU with
    # only non-mfg_model links is not even "matched".
    seed_commodity_schemas(db_session)
    card = _card(db_session, "00AJ141", category="hdd")
    _link(db_session, "00AJ141", "ST4000NM0035", kind="drive_pn")
    _link(db_session, "00AJ141", "00AJ144", mfg=None, kind="option")

    stats = crosswalk_and_record_specs(db_session, [card.id])

    assert stats == ZERO_STATS
    assert _facets(db_session, card.id) == {}


def test_reverse_only_card_gets_nothing(db_session: Session):
    # A card whose norm matches only related_norm (it IS a mfg_model) inherits
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
    # A record_spec failure on card 2 of 3 rolls back ONLY that card (including a
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
    assert stats["failed"] == 1  # the rolled-back card surfaces in the aggregate
    assert stats["categorized"] == 2
    assert stats["written"] == 6
    assert _facets(db_session, card1.id)["capacity_gb"] == 4000
    assert _facets(db_session, card3.id)["capacity_gb"] == 1920
    assert _facets(db_session, bad.id) == {}  # bad card fully rolled back
    assert bad.category is None  # categorize-from-null did NOT leak past the rollback


def test_links_resolved_via_exactly_one_select(db_session: Session):
    # The whole batch resolves its links through ONE fru_links SELECT — no N+1.
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


def test_single_survivor_passthrough_with_undecodable_sibling(db_session: Session):
    # FRU matrices routinely link models outside the storage/ssd/memory decoder
    # registry. Undecodable links contribute NO evidence — they are filtered, not
    # treated as conflicts — so when exactly one link decodes, ALL of its specs pass
    # through at 0.93: one-of-N agreement is the contract, not all-of-N.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "00AJ141", category="hdd")
    _link(db_session, "00AJ141", "ST4000NM0035")
    _link(db_session, "00AJ141", "00AJ144X", mfg="IBM")  # outside the decoder registry

    stats = crosswalk_and_record_specs(db_session, [card.id])
    db_session.commit()

    assert stats["matched"] == 1
    assert stats["decoded"] == 1
    assert stats["written"] == 3
    assert stats["commodity_conflict"] == 0
    assert stats["failed"] == 0
    f = _facets(db_session, card.id)
    assert f == {"capacity_gb": 4000, "form_factor": '3.5"', "usage_class": "Enterprise / Datacenter"}
    assert card.specs_structured["capacity_gb"]["source"] == FRU_DECODE_SOURCE


def test_all_links_undecodable_is_a_no_evidence_noop(db_session: Session):
    # The sibling branch: EVERY link is outside the decoder registry → the card is
    # matched but never decoded, nothing is asserted, and the no-evidence FRU is NOT
    # misreported as a commodity conflict.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "00AJ141", category=None)
    _link(db_session, "00AJ141", "00AJ144X", mfg="IBM")

    stats = crosswalk_and_record_specs(db_session, [card.id])

    assert stats["matched"] == 1
    assert stats["decoded"] == 0
    assert stats["written"] == 0
    assert stats["commodity_conflict"] == 0
    assert stats["failed"] == 0
    assert card.category is None
    assert _facets(db_session, card.id) == {}


def test_cards_sharing_one_normalized_key_all_enriched(db_session: Session, monkeypatch):
    # key_to_card_ids is a dict of LISTS: dash/spacing MPN variants collapse to one
    # normalized key, and EVERY card on that key receives the FRU's agreed specs
    # while each unique model decodes exactly once. The intersection (and its
    # dropped-keys count) is computed once per FRU — the conflicting capacity key
    # reports dropped_conflict=1, not once per card sharing the key.
    import app.services.fru_crosswalk_enrich as mod

    seed_commodity_schemas(db_session)
    card_a = _card(db_session, "00AJ141", category="hdd")
    card_b = _card(db_session, "00-AJ-141", category="hdd")
    assert normalize_mpn_key(card_a.normalized_mpn) == normalize_mpn_key(card_b.normalized_mpn)
    _link(db_session, "00AJ141", "ST4000NM0035")
    _link(db_session, "00AJ141", "ST8000NM0055")

    calls: list[str] = []
    real_decode = mod.decode_mpn

    def counting(mpn, manufacturer=None):
        calls.append(mpn)
        return real_decode(mpn, manufacturer)

    monkeypatch.setattr(mod, "decode_mpn", counting)

    stats = crosswalk_and_record_specs(db_session, [card_a.id, card_b.id])
    db_session.commit()

    assert sorted(calls) == ["ST4000NM0035", "ST8000NM0055"]  # one decode per model, not per card
    assert stats["matched"] == 2
    assert stats["decoded"] == 2
    assert stats["written"] == 4  # 2 agreed keys x 2 cards
    assert stats["dropped_conflict"] == 1  # capacity conflict counted once per FRU, not per card
    for card in (card_a, card_b):
        assert _facets(db_session, card.id) == {"form_factor": '3.5"', "usage_class": "Enterprise / Datacenter"}


def test_decode_exception_on_one_fru_does_not_abort_the_rest(db_session: Session, monkeypatch):
    # decode/intersect runs per FRU OUTSIDE the per-card savepoint — a decoder
    # exception on one weird workbook string must fail ONLY that FRU's cards
    # (surfaced in `failed`), never the remaining FRUs or the stats dict.
    import app.services.fru_crosswalk_enrich as mod

    seed_commodity_schemas(db_session)
    card1 = _card(db_session, "00AJ141", category="hdd")
    card2 = _card(db_session, "00AJ142", category="hdd")
    card3 = _card(db_session, "00AJ143", category="ssd")
    _link(db_session, "00AJ141", "ST4000NM0035")
    _link(db_session, "00AJ142", "ST8000NM0055")
    _link(db_session, "00AJ143", "MZQL21T9HCJR", mfg="Samsung")

    real_decode = mod.decode_mpn

    def exploding(mpn, manufacturer=None):
        if mpn == "ST8000NM0055":
            raise RuntimeError("simulated decoder failure")
        return real_decode(mpn, manufacturer)

    monkeypatch.setattr(mod, "decode_mpn", exploding)

    stats = crosswalk_and_record_specs(db_session, [card1.id, card2.id, card3.id])
    db_session.commit()  # the failing FRU must not poison the shared transaction

    assert stats["matched"] == 3
    assert stats["failed"] == 1  # every card on the raising FRU counts as failed
    assert stats["decoded"] == 2
    assert stats["written"] == 6
    assert _facets(db_session, card1.id)["capacity_gb"] == 4000
    assert _facets(db_session, card3.id)["capacity_gb"] == 1920
    assert _facets(db_session, card2.id) == {}


def test_writer_warns_when_crosswalk_key_has_no_schema(db_session: Session):
    # record_spec drops a value with no commodity_spec_schemas row at DEBUG only
    # (invisible at the production INFO level) — the crosswalk writer must surface
    # the discard as an aggregate WARNING exactly like mpn_decoder's writer, or a
    # post-deploy schema lag silently zeroes the pass (decoded=N, written=0 with no
    # production-visible explanation).
    from loguru import logger as loguru_logger

    from app.models import CommoditySpecSchema

    seed_commodity_schemas(db_session)
    db_session.query(CommoditySpecSchema).filter_by(commodity="hdd", spec_key="usage_class").delete()
    db_session.flush()
    card = _card(db_session, "00AJ141", category="hdd")
    _link(db_session, "00AJ141", "ST4000NM0035")

    warnings: list[str] = []
    sink_id = loguru_logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    try:
        stats = crosswalk_and_record_specs(db_session, [card.id])
    finally:
        loguru_logger.remove(sink_id)
    db_session.commit()

    assert any("hdd.usage_class" in w and "dropped" in w for w in warnings), warnings
    f = _facets(db_session, card.id)
    assert "usage_class" not in f  # dropped (no schema)
    assert f["capacity_gb"] == 4000  # sibling keys still written
    assert stats["written"] == 2


def test_writer_warns_when_enum_value_outside_live_schema(db_session: Session):
    # record_spec's OTHER silent vocabulary drop: a schema row exists but the agreed
    # value is not in its LIVE enum_values (a stale DB row after a failed/lagging
    # reseed). The writer must surface this drop in the same aggregate WARNING as
    # the no-schema case, mirroring mpn_decoder's writer.
    from loguru import logger as loguru_logger

    from app.models import CommoditySpecSchema

    seed_commodity_schemas(db_session)
    schema = db_session.query(CommoditySpecSchema).filter_by(commodity="hdd", spec_key="form_factor").one()
    schema.enum_values = ['2.5"']  # simulate live-DB enum drift: 3.5" removed
    db_session.flush()
    card = _card(db_session, "00AJ141", category="hdd")
    _link(db_session, "00AJ141", "ST4000NM0035")

    warnings: list[str] = []
    sink_id = loguru_logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    try:
        stats = crosswalk_and_record_specs(db_session, [card.id])
    finally:
        loguru_logger.remove(sink_id)
    db_session.commit()

    assert any('hdd.form_factor=3.5"' in w and "dropped" in w for w in warnings), warnings
    f = _facets(db_session, card.id)
    assert "form_factor" not in f  # dropped (out-of-enum), exactly mirroring record_spec
    assert f["capacity_gb"] == 4000  # sibling keys still written
    assert stats["written"] == 2
