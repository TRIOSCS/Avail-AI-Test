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
    # Full write path for a known-rank RDIMM: the round-2 keys (rank/registered/voltage)
    # must SURVIVE record_spec — i.e. their dram schemas are seeded — not just decode.
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
    assert f["rank"] == "2Rx8"
    assert f["registered"] == "Registered"
    assert f["voltage"] == 1.2
    assert f["capacity_gb"] == 16


def test_decode_writes_ssd_and_categorizes_null_category(db_session: Session):
    # SSD commodity through the FULL write path on a NULL-category card: categorize from the
    # decode, then persist facets. Also implicitly pins decoder↔seed enum agreement for the
    # ssd vocabulary — an out-of-enum value would be dropped by record_spec and fail here.
    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="mzql21t9hcjr", display_mpn="MZQL21T9HCJR", category=None)
    db_session.add(card)
    db_session.flush()

    stats = decode_and_record_specs(db_session, [card.id])
    db_session.commit()

    assert stats == {
        "decoded": 1,
        "written": 3,
        "categorized": 1,
        "manufacturers_set": 1,  # dual-brand W4: decode vendor → manufacturer ladder
        "skipped_category_conflict": 0,
        "skipped_maker_conflict": 0,
    }
    assert card.category == "ssd"
    assert card.manufacturer == "Samsung"  # verbatim — manufacturers table unseeded here
    assert card.manufacturer_source == "mpn_decode"
    assert card.manufacturer_tier == 85
    assert card.manufacturer_confidence == 0.9
    f = _facets(db_session, card.id)
    assert f["form_factor"] == "U.2"
    assert f["interface"] == "NVMe PCIe 4.0"
    assert f["capacity_gb"] == 1920


def test_writer_warns_when_decoded_key_has_no_schema(db_session: Session):
    # If a decoder emits a key with no commodity_spec_schemas row, record_spec drops it at
    # DEBUG (invisible at the production INFO level) — the writer must surface the discard
    # as an aggregate WARNING so a decoder↔seed drift is never silent.
    from loguru import logger as loguru_logger

    from app.models import CommoditySpecSchema

    seed_commodity_schemas(db_session)
    db_session.query(CommoditySpecSchema).filter_by(commodity="dram", spec_key="rank").delete()
    db_session.flush()
    card = MaterialCard(normalized_mpn="m393a2k43db3-cwe", display_mpn="M393A2K43DB3-CWE", category="dram")
    db_session.add(card)
    db_session.flush()

    warnings: list[str] = []
    sink_id = loguru_logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    try:
        decode_and_record_specs(db_session, [card.id])
    finally:
        loguru_logger.remove(sink_id)
    db_session.commit()

    assert any("dram.rank" in w and "dropped" in w for w in warnings), warnings
    f = _facets(db_session, card.id)
    assert "rank" not in f  # dropped (no schema)
    assert f["registered"] == "Registered"  # sibling keys still written


def test_writer_warns_when_enum_value_outside_live_schema(db_session: Session):
    # record_spec's OTHER silent vocabulary drop: a schema row exists but the decoded value is
    # not in its LIVE enum_values (a stale DB row after a failed/lagging reseed — CI only pins
    # the decoder against the JSON seeds, the worker decodes against live rows). The writer
    # must surface this drop in the same aggregate WARNING as the no-schema case.
    from loguru import logger as loguru_logger

    from app.models import CommoditySpecSchema

    seed_commodity_schemas(db_session)
    schema = db_session.query(CommoditySpecSchema).filter_by(commodity="dram", spec_key="registered").one()
    schema.enum_values = ["Unbuffered"]  # simulate live-DB enum drift: "Registered" removed
    db_session.flush()
    card = MaterialCard(normalized_mpn="m393a2k43db3-cwe", display_mpn="M393A2K43DB3-CWE", category="dram")
    db_session.add(card)
    db_session.flush()

    warnings: list[str] = []
    sink_id = loguru_logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    try:
        decode_and_record_specs(db_session, [card.id])
    finally:
        loguru_logger.remove(sink_id)
    db_session.commit()

    assert any("dram.registered=Registered" in w and "dropped" in w for w in warnings), warnings
    f = _facets(db_session, card.id)
    assert "registered" not in f  # dropped (out-of-enum), exactly mirroring record_spec
    assert f["form_factor"] == "RDIMM"  # sibling keys still written


def test_writer_warns_when_capacity_off_shipped_grid(db_session: Session):
    # Third drop channel (re-audit 2026-06-10): the DECODER itself refuses an off-grid
    # hdd capacity (shipped-capacity grid backstop) — the value lands in
    # DecodeResult.dropped, never in specs, so record_spec never sees it. The writer
    # must surface that silent, pure-function drop in the same aggregate WARNING as
    # record_spec's vocabulary drops. No 17 TB HDD has ever shipped, so the T-token
    # read on this Toshiba shape is implausible; the prefix-derived specs still land.
    from loguru import logger as loguru_logger

    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="mg09aca17te", display_mpn="MG09ACA17TE", category="hdd")
    db_session.add(card)
    db_session.flush()

    warnings: list[str] = []
    sink_id = loguru_logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    try:
        stats = decode_and_record_specs(db_session, [card.id])
    finally:
        loguru_logger.remove(sink_id)
    db_session.commit()

    assert any("hdd.capacity_gb=17000" in w and "shipped-capacity grid" in w for w in warnings), warnings
    assert stats["decoded"] == 1
    f = _facets(db_session, card.id)
    assert "capacity_gb" not in f  # the off-grid value was never offered to record_spec
    assert f["form_factor"] == '3.5"'  # trustworthy prefix-derived specs still written
    assert f["usage_class"] == "Enterprise / Datacenter"


def test_writer_warns_when_grid_empties_a_capacity_only_decode(db_session: Session):
    # The formerly-silent path: legacy WD decodes emit capacity ONLY, so an off-grid
    # capacity empties specs entirely (55.5 GB was never a shipped point). decode_mpn
    # now returns the specs-empty result carrying `dropped`, and the writer must count
    # it into the same aggregate WARNING — while writing nothing and leaving the card
    # untouched (a decode whose every value failed its plausibility gate contributes
    # no category, no maker, no specs, and does not count as decoded).
    from loguru import logger as loguru_logger

    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="wd555ab", display_mpn="WD555AB", category="hdd")
    db_session.add(card)
    db_session.flush()

    warnings: list[str] = []
    sink_id = loguru_logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    try:
        stats = decode_and_record_specs(db_session, [card.id])
    finally:
        loguru_logger.remove(sink_id)
    db_session.commit()

    assert any("hdd.capacity_gb=55.5" in w and "shipped-capacity grid" in w for w in warnings), warnings
    assert stats["decoded"] == 0
    assert stats["written"] == 0
    assert _facets(db_session, card.id) == {}


def test_writer_warns_on_envelope_rejection_with_its_own_counter(db_session: Session):
    # Seagate envelope rejections (truncated/malformed strings, unlisted families) ride
    # the same dropped channel but under a SEPARATE counter — an over-tight envelope
    # must be distinguishable from an incomplete shipped-capacity grid. The distrusted
    # decode must also never categorize the card (specs are empty).
    from loguru import logger as loguru_logger

    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="st120mm0198", display_mpn="ST120MM0198", category=None)
    db_session.add(card)
    db_session.flush()

    warnings: list[str] = []
    sink_id = loguru_logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    try:
        stats = decode_and_record_specs(db_session, [card.id])
    finally:
        loguru_logger.remove(sink_id)
    db_session.commit()

    assert any("hdd.capacity_gb=120" in w and "Seagate family envelope" in w for w in warnings), warnings
    assert stats["decoded"] == 0
    assert stats["categorized"] == 0
    assert card.category is None  # never categorized from a fully-distrusted decode
    assert _facets(db_session, card.id) == {}


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
    # category write, so set_category returns False, categorized stays 0, and the decode
    # contributes NOTHING: no drive specs land on the DRAM card AND the W4 maker write is
    # skipped (shared cross-commodity guard) — a decode whose commodity claim lost the
    # ladder is precisely the case where the regex match itself is suspect, so its maker
    # claim must not mutate the card either. The ladder loss is NOT silent: it is counted
    # in the returned stats and WARNed with the (card_category -> decoded_commodity) pair,
    # because a recurring pair is exactly the signal that the category alias map needs
    # another entry.
    from loguru import logger as loguru_logger

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

    warnings: list[str] = []
    sink_id = loguru_logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    try:
        stats = decode_and_record_specs(db_session, [card.id])
    finally:
        loguru_logger.remove(sink_id)
    assert stats == {
        "decoded": 1,
        "written": 0,
        "categorized": 0,
        "manufacturers_set": 0,  # maker write skipped — it rides the same suspect match
        "skipped_category_conflict": 1,
        "skipped_maker_conflict": 0,  # skipped by the guard, not lost in arbitration
    }
    assert any("dram->hdd" in w for w in warnings), warnings
    assert card.category == "dram"  # higher-tier category preserved
    assert card.manufacturer is None  # W4 maker write skipped on the conflicted decode
    # The card's category is still "dram", so a drive's capacity_gb has no dram schema match
    # and is rejected — nothing drive-specific lands on the DRAM card.
    assert "capacity_gb" not in _facets(db_session, card.id)


def test_decode_maker_ladder_loss_is_counted_and_warned(db_session: Session):
    # The decode's commodity AGREES with the card (so the W4 maker write runs), but the
    # card already holds a DIFFERENT maker at a higher tier (vendor, 90): the decode's
    # maker (85) loses arbitration. set_manufacturer's losing path logs at DEBUG only,
    # so — mirroring skipped_category_conflict — the loss must be counted in the stats
    # and WARNed with the (existing -> incoming) pair.
    from loguru import logger as loguru_logger

    seed_commodity_schemas(db_session)
    card = MaterialCard(
        normalized_mpn="st4000nm0035",
        display_mpn="ST4000NM0035",
        category="hdd",
        manufacturer="Western Digital",  # conflicting maker, vendor-API tier
        manufacturer_source="digikey_api",
        manufacturer_confidence=1.0,
        manufacturer_tier=90,
    )
    db_session.add(card)
    db_session.flush()

    warnings: list[str] = []
    sink_id = loguru_logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    try:
        stats = decode_and_record_specs(db_session, [card.id])
    finally:
        loguru_logger.remove(sink_id)
    db_session.commit()

    assert stats["manufacturers_set"] == 0
    assert stats["skipped_maker_conflict"] == 1
    assert any("Western Digital->Seagate" in w for w in warnings), warnings
    assert card.manufacturer == "Western Digital"  # higher-tier maker preserved
    assert stats["written"] >= 3  # the specs still land — only the maker claim lost


def test_decode_maker_agreement_is_not_a_conflict(db_session: Session):
    # A higher-tier existing maker that AGREES with the decode is not a conflict: the
    # decode's write returns False (tier 85 < 90) but no counter increments and no
    # WARNING fires — skipped_maker_conflict must stay a pure data-conflict signal.
    from loguru import logger as loguru_logger

    seed_commodity_schemas(db_session)
    card = MaterialCard(
        normalized_mpn="st4000nm0035",
        display_mpn="ST4000NM0035",
        category="hdd",
        manufacturer="Seagate",  # same maker the decode yields (verbatim, no alias seeds)
        manufacturer_source="digikey_api",
        manufacturer_confidence=1.0,
        manufacturer_tier=90,
    )
    db_session.add(card)
    db_session.flush()

    warnings: list[str] = []
    sink_id = loguru_logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    try:
        stats = decode_and_record_specs(db_session, [card.id])
    finally:
        loguru_logger.remove(sink_id)

    assert stats["manufacturers_set"] == 0
    assert stats["skipped_maker_conflict"] == 0
    assert not any("Seagate->Seagate" in w for w in warnings), warnings
    assert card.manufacturer_tier == 90  # untouched


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
    assert stats["skipped_category_conflict"] == 0
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
    assert stats == {
        "decoded": 0,
        "written": 0,
        "categorized": 0,
        "manufacturers_set": 0,
        "skipped_category_conflict": 0,
        "skipped_maker_conflict": 0,
    }


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
