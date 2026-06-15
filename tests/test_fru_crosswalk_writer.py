"""Writer — crosswalk_and_record_specs: FRU cards inherit the strict-intersected decode
of their approved mfg_model links via record_spec (source="fru_matrix_decode",
confidence 0.93) AND the strict-intersected desc_extractor parse of their linked qual-
sheet descriptions (source="fru_desc_parse", tier 82, confidence 0.88), with the
category fill / mismatch-skip / per-FRU + per-card isolation / single-query guarantees
stated inline on each test.

Description corpus strings are REAL fru_links rows from the live FRU matrix ingest
(Qlot/Gabor/CZ sheets).
"""

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models import FruLink, MaterialCard, MaterialSpecFacet
from app.services.commodity_registry import seed_commodity_schemas
from app.services.fru_crosswalk_enrich import (
    FRU_DECODE_CONFIDENCE,
    FRU_DECODE_SOURCE,
    FRU_DESC_CONFIDENCE,
    FRU_DESC_SOURCE,
    crosswalk_and_record_specs,
)
from app.utils.normalization import normalize_mpn_key

ZERO_STATS = {
    "matched": 0,
    "decoded": 0,
    "written": 0,
    "categorized": 0,
    "manufacturers_set": 0,
    "desc_parsed": 0,
    "desc_written": 0,
    "failed": 0,
    "desc_failed": 0,
    "dropped_conflict": 0,
    "desc_dropped_conflict": 0,
    "commodity_conflict": 0,
    "desc_commodity_conflict": 0,
    "category_mismatch": 0,
    "desc_category_mismatch": 0,
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
    db: Session,
    fru: str,
    related: str,
    mfg: str | None = "Seagate",
    kind: str = "mfg_model",
    sheet: str = "Main",
    description: str | None = None,
) -> FruLink:
    link = FruLink(
        fru_raw=fru,
        fru_norm=normalize_mpn_key(fru),
        related_raw=related,
        related_norm=normalize_mpn_key(related),
        rel_kind=kind,
        manufacturer=mfg,
        description=description,
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

    assert stats == dict(
        ZERO_STATS,
        matched=1,
        decoded=1,
        written=2,
        manufacturers_set=1,  # both substitutes decode to Seagate — unanimous maker (D4)
        dropped_conflict=1,  # capacity_gb 4000 vs 8000
    )
    f = _facets(db_session, card.id)
    assert f == {"form_factor": '3.5"', "usage_class": "Enterprise / Datacenter"}
    entry = card.specs_structured["form_factor"]
    assert entry["source"] == FRU_DECODE_SOURCE == "fru_matrix_decode"
    assert entry["confidence"] == FRU_DECODE_CONFIDENCE == 0.93
    # (d) D4: the unanimous decoded vendor is the deterministic maker, written at tier 84.
    assert card.manufacturer == "Seagate"
    assert card.manufacturer_source == FRU_DECODE_SOURCE


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
    # NOT counted as decoded: contradicting substitutes never produced a decoded
    # verdict — `decoded` counts truly-decoded cards only.
    assert stats["decoded"] == 0
    assert stats["commodity_conflict"] == 1
    assert stats["written"] == 0
    assert stats["categorized"] == 0
    assert card.category is None
    assert _facets(db_session, card.id) == {}


def test_deterministic_maker_upgrades_legacy_oem_label(db_session: Session):
    # (d) D4 maker propagation: a legacy IBM/Lenovo OEM label (unprovenanced → ranks at
    # the legacy_backfill tier 50) is UPGRADED to the DETERMINISTIC maker the decoder
    # identifies (Seagate, tier 84) — never inferred from prose, always from the
    # regex-gated decode of the linked canonical model.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "00AJ141", category="hdd", manufacturer="IBM")
    _link(db_session, "00AJ141", "ST4000NM0035", mfg="Seagate")

    stats = crosswalk_and_record_specs(db_session, [card.id])
    db_session.commit()

    assert stats["written"] == 3
    assert stats["manufacturers_set"] == 1
    assert card.manufacturer == "Seagate"
    assert card.manufacturer_source == FRU_DECODE_SOURCE
    assert card.manufacturer_tier == 84


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
    # The mpn_decode prior survives COMPLETELY untouched: record_spec backfills the legacy
    # tier on a COPY for the comparison and never mutates a losing entry in place.
    assert specs["capacity_gb"] == {
        "value": 8000,
        "source": "mpn_decode",
        "confidence": 0.95,
        "updated_at": "x",
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


def test_out_of_scope_rel_kinds_excluded_even_with_descriptions(db_session: Session):
    # Only mfg_model + drive_pn rows are in scope — an option/tray row's description
    # describes an accessory context, never the FRU's own specs, so a FRU with only
    # out-of-scope links is not even "matched" (they are filtered in the ONE query).
    seed_commodity_schemas(db_session)
    card = _card(db_session, "00AJ141", category="hdd")
    _link(db_session, "00AJ141", "00AJ144", mfg=None, kind="option", description="8TB 3.5 HDD 7.2K 12Gb/s SAS")
    _link(db_session, "00AJ141", "00AJ145", mfg=None, kind="tray", description="1.2TB 2.5 HDD 10K 12 Gb/s SAS")

    stats = crosswalk_and_record_specs(db_session, [card.id])

    assert stats == ZERO_STATS
    assert _facets(db_session, card.id) == {}


def test_drive_pn_without_description_matches_but_asserts_nothing(db_session: Session):
    # A drive_pn related PN is an IBM spare number, NOT a decodable manufacturer
    # model — it joins the scope ONLY for its description. Description-less drive_pn
    # rows are matched (the kind is queried now) but contribute zero evidence.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "00AJ141", category="hdd")
    _link(db_session, "00AJ141", "00D5317", mfg=None, kind="drive_pn")

    stats = crosswalk_and_record_specs(db_session, [card.id])

    assert stats == dict(ZERO_STATS, matched=1)
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


# ---------------------------------------------------------------------------
# Linked-description channel (fru_desc_parse, tier 82) — wave 3A
# ---------------------------------------------------------------------------

# REAL fru_links descriptions from the live FRU matrix ingest (Qlot/Gabor/CZ sheets).
_DESC_HDD_8TB = "8TB 3.5 HDD 7.2K 12Gb/s SAS"  # fru 01LJ065 / 00VN423
_DESC_HDD_18TB = "18TB 3.5 HDD 7.2K 12 Gb/s SAS"  # qual-sheet drive_pn prose
_DESC_HDD_1_2TB = "1.2TB 2.5 HDD 10K 12 Gb/s SAS"  # fru 01LJ787 / 00FJ069
_DESC_HDD_450GB = 'HDD, 450GB 15000RPM 16MB 3.5" SAS'  # fru 46Y0295 / ST3450857SS
_DESC_SSD_PM1733 = "SSD; 2.5; 1.92 TB Samsung PM1733"  # fru 01YM586 / MZWLJ1T9HBJR-000M3
_DESC_SSD_PHOENIX = 'SSD, Toshiba, Phoenix M3, Non-SED, 2.5", 800GB, SAS, 12Gb/s, 10 DWPD'  # fru 00AR331


def test_drive_pn_descriptions_intersect_and_write_fru_desc_parse(db_session: Session):
    # Two qual-sheet drive_pn rows agreeing on rpm + interface but conflicting on
    # capacity: the agreed keys write at tier 82 / "fru_desc_parse" / 0.88, the
    # conflicting key is dropped AND counted in the DESC-side counter (per card —
    # never blended into the per-FRU decode counter). drive_pn related PNs are IBM
    # spares (never decoded), so the decode channel contributes nothing (decoded=0).
    seed_commodity_schemas(db_session)
    card = _card(db_session, "01LJ065", category="hdd")
    _link(db_session, "01LJ065", "00VN423", mfg=None, kind="drive_pn", description=_DESC_HDD_8TB)
    _link(db_session, "01LJ065", "00VN424", mfg=None, kind="drive_pn", description=_DESC_HDD_18TB)

    stats = crosswalk_and_record_specs(db_session, [card.id])
    db_session.commit()

    assert stats == dict(
        ZERO_STATS,
        matched=1,
        desc_parsed=1,
        desc_written=2,
        desc_dropped_conflict=1,  # capacity_gb 8000 vs 18000
    )
    f = _facets(db_session, card.id)
    assert f == {"rpm": "7200", "interface": "SAS"}
    entry = card.specs_structured["rpm"]
    assert entry["source"] == FRU_DESC_SOURCE == "fru_desc_parse"
    assert entry["confidence"] == FRU_DESC_CONFIDENCE == 0.88
    assert entry["tier"] == 82


def test_mfg_model_description_feeds_desc_channel_single_source_passes(db_session: Session):
    # mfg_model rows carry bare-drive prose too; an UNDECODABLE model (bare "PM863"
    # passes no vendor regex gate) still contributes its description. One extracting
    # description passes ALL its specs — one-of-N agreement, same as the decode channel.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "00AR331", category="ssd")
    _link(db_session, "00AR331", "PM863", mfg="Toshiba", description=_DESC_SSD_PHOENIX)

    stats = crosswalk_and_record_specs(db_session, [card.id])
    db_session.commit()

    assert stats == dict(ZERO_STATS, matched=1, desc_parsed=1, desc_written=3)
    f = _facets(db_session, card.id)
    assert f == {"capacity_gb": 800, "form_factor": '2.5"', "interface": "SAS"}
    assert card.specs_structured["capacity_gb"]["source"] == FRU_DESC_SOURCE


def test_desc_commodity_conflict_skips_desc_channel(db_session: Session):
    # An HDD-prose row next to an SSD-prose row (same storage family, so neither is
    # suppressed by the card hint): the descriptions can't agree on what the part IS
    # — the desc channel asserts nothing, the skip surfaces in the
    # desc_commodity_conflict counter (not buried at DEBUG), and the disagreement is
    # not a counted key-value conflict.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "00AJ141", category="hdd")
    _link(db_session, "00AJ141", "00D5317", mfg=None, kind="drive_pn", description=_DESC_HDD_450GB)
    _link(db_session, "00AJ141", "00D5318", mfg=None, kind="drive_pn", description=_DESC_SSD_PHOENIX)

    stats = crosswalk_and_record_specs(db_session, [card.id])

    assert stats == dict(ZERO_STATS, matched=1, desc_commodity_conflict=1)
    assert _facets(db_session, card.id) == {}


def test_fru_desc_parse_ladder_loses_to_higher_tiers_beats_lower(db_session: Session):
    # record_spec's F1 ladder arbitrates the desc channel exactly like every writer:
    # a desc_parse prior (83 — the card's OWN description) and an mpn_decode prior
    # (85) both survive an incoming fru_desc_parse (82); a spec_extraction prior
    # (60) loses and is overwritten.
    seed_commodity_schemas(db_session)
    card = _card(
        db_session,
        "01LJ065",
        category="hdd",
        specs_structured={
            "rpm": {"value": "15000", "source": "desc_parse", "confidence": 0.90, "updated_at": "x"},
            "capacity_gb": {"value": 4000, "source": "mpn_decode", "confidence": 0.95, "updated_at": "x"},
            "interface": {"value": "SATA", "source": "spec_extraction", "confidence": 0.85, "updated_at": "x"},
        },
    )
    _link(db_session, "01LJ065", "00VN423", mfg=None, kind="drive_pn", description=_DESC_HDD_8TB)

    stats = crosswalk_and_record_specs(db_session, [card.id])
    db_session.commit()

    assert stats == dict(ZERO_STATS, matched=1, desc_parsed=1, desc_written=1)  # only interface lands
    specs = card.specs_structured
    # Higher-tier priors survive COMPLETELY untouched (record_spec never mutates a loser).
    assert specs["rpm"] == {"value": "15000", "source": "desc_parse", "confidence": 0.90, "updated_at": "x"}
    assert specs["capacity_gb"] == {"value": 4000, "source": "mpn_decode", "confidence": 0.95, "updated_at": "x"}
    assert specs["interface"]["value"] == "SAS"
    assert specs["interface"]["source"] == FRU_DESC_SOURCE
    assert specs["interface"]["tier"] == 82


def test_decode_filled_category_routes_desc_channel_same_pass(db_session: Session):
    # ONE pass, chained channels: the decode channel fills the NULL category from the
    # agreed commodity inside its own savepoint, and the desc channel (running after
    # that savepoint RELEASED — released changes stay visible in the enclosing
    # transaction) uses that fresh category as its extraction hint. The decode's
    # tier-84 capacity beats the desc row's conflicting tier-82 capacity, so the desc
    # channel lands only its novel keys.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "01LJ787", category=None)
    _link(db_session, "01LJ787", "ST4000NM0035")  # decodes hdd: capacity 4000 / 3.5" / Enterprise
    _link(db_session, "01LJ787", "00FJ069", mfg=None, kind="drive_pn", description=_DESC_HDD_1_2TB)

    stats = crosswalk_and_record_specs(db_session, [card.id])
    db_session.commit()

    assert stats == dict(
        ZERO_STATS,
        matched=1,
        decoded=1,
        written=3,
        categorized=1,
        manufacturers_set=1,  # the lone decoding model (ST…) is Seagate — unanimous maker
        desc_parsed=1,
        desc_written=2,  # rpm + interface; capacity_gb lost 82 < 84
    )
    assert card.category == "hdd"
    assert card.category_source == FRU_DECODE_SOURCE  # the desc channel NEVER categorizes
    assert card.manufacturer == "Seagate"  # the drive_pn IBM FRU (00FJ069) does not decode
    f = _facets(db_session, card.id)
    assert f["capacity_gb"] == 4000  # decode (84) kept over desc prose (82)
    assert f["rpm"] == "10000"
    assert f["interface"] == "SAS"
    assert card.specs_structured["capacity_gb"]["source"] == FRU_DECODE_SOURCE
    assert card.specs_structured["rpm"]["source"] == FRU_DESC_SOURCE


def test_descriptions_never_categorize_a_category_less_card(db_session: Session):
    # No decodable models → no regex-gated commodity proof → the NULL category stays
    # NULL and the desc channel is skipped entirely (record_spec requires a category;
    # linked prose must never fill one).
    seed_commodity_schemas(db_session)
    card = _card(db_session, "01LJ065", category=None)
    _link(db_session, "01LJ065", "00VN423", mfg=None, kind="drive_pn", description=_DESC_HDD_8TB)

    stats = crosswalk_and_record_specs(db_session, [card.id])

    assert stats == dict(ZERO_STATS, matched=1)
    assert card.category is None
    assert _facets(db_session, card.id) == {}


def test_desc_channel_skipped_for_non_spec_commodity_category(db_session: Session):
    # The desc channel mirrors the first-party desc writer's eligibility gate: a card
    # categorized outside SPEC_COMMODITIES (e.g. "cables") takes nothing from linked
    # prose, even when that prose extracts cleanly under no hint.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "01LJ065", category="cables")
    _link(db_session, "01LJ065", "00VN423", mfg=None, kind="drive_pn", description=_DESC_HDD_8TB)

    stats = crosswalk_and_record_specs(db_session, [card.id])

    assert stats == dict(ZERO_STATS, matched=1)
    assert _facets(db_session, card.id) == {}


def test_category_mismatch_skips_desc_channel_too(db_session: Session):
    # The decode commodity contradicts the existing category → the card is skipped
    # ENTIRELY: the linked descriptions describe those same mismatching substitutes,
    # so the desc channel must not write around the skip.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "00AJ141", category="dram")
    _link(db_session, "00AJ141", "ST4000NM0035")
    _link(db_session, "00AJ141", "00D5317", mfg=None, kind="drive_pn", description=_DESC_HDD_8TB)

    stats = crosswalk_and_record_specs(db_session, [card.id])

    assert stats == dict(ZERO_STATS, matched=1, decoded=1, category_mismatch=1)
    assert card.category == "dram"
    assert _facets(db_session, card.id) == {}


def test_decode_commodity_conflict_skips_desc_channel_too(db_session: Session):
    # Substitute models that disagree on what the part IS (hdd vs ssd decode) poison
    # BOTH channels — nothing may be asserted from descriptions of contradicting
    # substitutes either.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "00AJ141", category=None)
    _link(db_session, "00AJ141", "ST4000NM0035")
    _link(db_session, "00AJ141", "MZQL21T9HCJR", mfg="Samsung")
    _link(db_session, "00AJ141", "00D5317", mfg=None, kind="drive_pn", description=_DESC_HDD_8TB)

    stats = crosswalk_and_record_specs(db_session, [card.id])

    # decoded stays 0 — a commodity conflict is not a decoded verdict.
    assert stats == dict(ZERO_STATS, matched=1, commodity_conflict=1)
    assert card.category is None
    assert _facets(db_session, card.id) == {}


def test_duplicate_descriptions_across_sheets_extract_once(db_session: Session, monkeypatch):
    # The same qual-sheet prose repeated across sheets collapses into ONE extraction
    # (the per-FRU description set), mirroring the decode channel's dedup.
    import app.services.fru_crosswalk_enrich as mod

    seed_commodity_schemas(db_session)
    card = _card(db_session, "01LJ065", category="hdd")
    _link(db_session, "01LJ065", "00VN423", mfg=None, kind="drive_pn", sheet="Qlot", description=_DESC_HDD_8TB)
    _link(db_session, "01LJ065", "00VN423", mfg=None, kind="drive_pn", sheet="Gabor", description=_DESC_HDD_8TB)

    calls: list[str] = []
    real_extract = mod.extract_desc

    def counting(description, commodity_hint=None):
        calls.append(description)
        return real_extract(description, commodity_hint=commodity_hint)

    monkeypatch.setattr(mod, "extract_desc", counting)

    stats = crosswalk_and_record_specs(db_session, [card.id])

    assert calls == [_DESC_HDD_8TB]
    assert stats == dict(ZERO_STATS, matched=1, desc_parsed=1, desc_written=3)


def test_desc_channel_links_resolved_in_the_same_single_select(db_session: Session):
    # The desc channel rides the decode channel's ONE fru_links SELECT — extending
    # the rel_kind filter must not add a second query.
    engine = db_session.get_bind()

    seed_commodity_schemas(db_session)
    card = _card(db_session, "01LJ787", category=None)
    _link(db_session, "01LJ787", "ST4000NM0035")
    _link(db_session, "01LJ787", "00FJ069", mfg=None, kind="drive_pn", description=_DESC_HDD_1_2TB)

    fru_link_selects: list[str] = []

    def counter(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "fru_links" in statement:
            fru_link_selects.append(statement)

    event.listen(engine, "before_cursor_execute", counter)
    try:
        stats = crosswalk_and_record_specs(db_session, [card.id])
    finally:
        event.remove(engine, "before_cursor_execute", counter)

    assert len(fru_link_selects) == 1, fru_link_selects
    assert stats["written"] == 3
    assert stats["desc_written"] == 2


def test_desc_failure_keeps_decode_writes_and_counts_desc_failed(db_session: Session, monkeypatch):
    # The channels run in SEQUENTIAL savepoints: a failure during the desc channel
    # rolls back ONLY the desc savepoint — the card's already-RELEASED decode writes
    # and category fill survive (the stronger evidence is never nuked by the more
    # failure-prone prose channel), the loss surfaces in desc_failed (NOT failed —
    # the card is not lost), siblings persist, and the shared transaction stays clean.
    import app.services.fru_crosswalk_enrich as mod

    seed_commodity_schemas(db_session)
    good = _card(db_session, "01LJ065", category="hdd")
    bad = _card(db_session, "01LJ787", category=None)
    _link(db_session, "01LJ065", "00VN423", mfg=None, kind="drive_pn", description=_DESC_HDD_8TB)
    _link(db_session, "01LJ787", "ST4000NM0035")
    _link(db_session, "01LJ787", "00FJ069", mfg=None, kind="drive_pn", description=_DESC_HDD_1_2TB)

    real_record_spec = mod.record_spec

    def flaky(db, card_id, *args, **kwargs):
        if card_id == bad.id and kwargs.get("source") == FRU_DESC_SOURCE:
            db.flush()
            raise RuntimeError("simulated flush failure in the desc channel")
        return real_record_spec(db, card_id, *args, **kwargs)

    monkeypatch.setattr(mod, "record_spec", flaky)

    stats = crosswalk_and_record_specs(db_session, [good.id, bad.id])
    db_session.commit()  # must NOT raise — the desc savepoint kept the transaction clean

    assert stats["matched"] == 2
    assert stats["failed"] == 0  # no card was LOST
    assert stats["desc_failed"] == 1  # the desc-channel loss surfaces in its own counter
    assert stats["desc_parsed"] == 1  # only the good card
    assert stats["desc_written"] == 3
    assert stats["decoded"] == 1
    assert stats["written"] == 3  # the bad card's decode writes SURVIVE the desc failure
    assert stats["categorized"] == 1
    assert bad.category == "hdd"  # the decode-channel category fill survives too
    f_bad = _facets(db_session, bad.id)
    assert f_bad == {"capacity_gb": 4000, "form_factor": '3.5"', "usage_class": "Enterprise / Datacenter"}
    assert bad.specs_structured["capacity_gb"]["source"] == FRU_DECODE_SOURCE
    assert "rpm" not in f_bad  # the desc savepoint itself fully rolled back
    assert _facets(db_session, good.id) == {"capacity_gb": 8000, "rpm": "7200", "interface": "SAS"}


def test_uniform_sibling_commodity_prose_skips_desc_channel(db_session: Session):
    # UNANIMOUS SSD-lead prose on an hdd-categorized card: extract_desc's same-family
    # lead refinement returns commodity='ssd' for EVERY row, so there is no
    # intra-description conflict — but the agreed commodity contradicts the card's
    # category. The desc channel must mirror the decode channel's category_mismatch
    # rule (an existing category is authoritative, never written-around): NOTHING is
    # written (no SSD capacity asserted on an hdd card at tier 82, no ssd-only keys
    # polluting the dropped_no_schema seed-drift WARNING), and the skip surfaces in
    # desc_category_mismatch.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "01YM586", category="hdd")
    _link(db_session, "01YM586", "00D5317", mfg=None, kind="drive_pn", description=_DESC_SSD_PM1733)
    _link(db_session, "01YM586", "00D5318", mfg=None, kind="drive_pn", description=_DESC_SSD_PHOENIX)

    stats = crosswalk_and_record_specs(db_session, [card.id])

    assert stats == dict(ZERO_STATS, matched=1, desc_category_mismatch=1)
    assert card.category == "hdd"
    assert _facets(db_session, card.id) == {}


def test_spec_less_description_does_not_veto_rich_sibling_specs(db_session: Session):
    # "HDD, Hot Swap" extracts commodity-only (empty specs) — common tray/hot-swap
    # qual-sheet rows sitting next to full drive prose under the same FRU. Under the
    # strict absence-is-not-agreement intersection one barren row would silently veto
    # EVERY key of the rich sibling (agreed={}, dropped=0 — invisible); instead it is
    # excluded from the per-key intersection (while still counting as commodity
    # evidence) and the rich row's specs land.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "01LJ065", category="hdd")
    _link(db_session, "01LJ065", "00VN423", mfg=None, kind="drive_pn", description=_DESC_HDD_8TB)
    _link(db_session, "01LJ065", "00VN424", mfg=None, kind="drive_pn", description="HDD, Hot Swap")

    stats = crosswalk_and_record_specs(db_session, [card.id])
    db_session.commit()

    assert stats == dict(ZERO_STATS, matched=1, desc_parsed=1, desc_written=3)
    assert _facets(db_session, card.id) == {"capacity_gb": 8000, "rpm": "7200", "interface": "SAS"}
    assert card.specs_structured["capacity_gb"]["source"] == FRU_DESC_SOURCE


def test_spec_less_description_commodity_still_vetoes_the_channel(db_session: Session):
    # The flip side of the empty-spec filter: a spec-less extraction stays IN the
    # commodity-agreement check. A bare SSD-lead row ("SSD, Hot Swap" → commodity=
    # 'ssd', specs={}) next to HDD prose is a real commodity conflict and must skip
    # the channel even though it would contribute no keys to the intersection.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "01LJ065", category="hdd")
    _link(db_session, "01LJ065", "00VN423", mfg=None, kind="drive_pn", description=_DESC_HDD_8TB)
    _link(db_session, "01LJ065", "00VN424", mfg=None, kind="drive_pn", description="SSD, Hot Swap")

    stats = crosswalk_and_record_specs(db_session, [card.id])

    assert stats == dict(ZERO_STATS, matched=1, desc_commodity_conflict=1)
    assert _facets(db_session, card.id) == {}


def test_desc_drop_counts_per_card_on_a_multi_card_fru(db_session: Session):
    # The desc intersection runs per CARD (its extraction hint is the card's
    # category), so the SAME conflicting description pair counts once for EACH card
    # sharing the fru_norm — desc_dropped_conflict is per-card where dropped_conflict
    # is per-FRU, which is exactly why they are separate counters (one unit each).
    seed_commodity_schemas(db_session)
    card_a = _card(db_session, "01LJ065", category="hdd")
    card_b = _card(db_session, "01-LJ-065", category="hdd")
    assert normalize_mpn_key(card_a.normalized_mpn) == normalize_mpn_key(card_b.normalized_mpn)
    _link(db_session, "01LJ065", "00VN423", mfg=None, kind="drive_pn", description=_DESC_HDD_8TB)
    _link(db_session, "01LJ065", "00VN424", mfg=None, kind="drive_pn", description=_DESC_HDD_18TB)

    stats = crosswalk_and_record_specs(db_session, [card_a.id, card_b.id])
    db_session.commit()

    assert stats == dict(
        ZERO_STATS,
        matched=2,
        desc_parsed=2,
        desc_written=4,
        desc_dropped_conflict=2,  # capacity conflict counted once PER CARD on the shared FRU
    )
    for card in (card_a, card_b):
        assert _facets(db_session, card.id) == {"rpm": "7200", "interface": "SAS"}


def test_desc_channel_surfaces_schema_drift_in_the_aggregate_warning(db_session: Session):
    # A desc-extracted key with no live schema row must surface in the same aggregate
    # WARNING the decode channel uses — record_spec alone drops it at DEBUG only.
    from loguru import logger as loguru_logger

    from app.models import CommoditySpecSchema

    seed_commodity_schemas(db_session)
    db_session.query(CommoditySpecSchema).filter_by(commodity="hdd", spec_key="rpm").delete()
    db_session.flush()
    card = _card(db_session, "01LJ065", category="hdd")
    _link(db_session, "01LJ065", "00VN423", mfg=None, kind="drive_pn", description=_DESC_HDD_8TB)

    warnings: list[str] = []
    sink_id = loguru_logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    try:
        stats = crosswalk_and_record_specs(db_session, [card.id])
    finally:
        loguru_logger.remove(sink_id)
    db_session.commit()

    assert any("hdd.rpm" in w and "dropped" in w for w in warnings), warnings
    f = _facets(db_session, card.id)
    assert "rpm" not in f  # dropped (no schema)
    assert f["capacity_gb"] == 8000  # sibling keys still written
    assert stats["desc_written"] == 2
