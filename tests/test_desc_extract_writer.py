"""The worker desc-parse pass writes extracted specs via record_spec, with guards."""

from sqlalchemy.orm import Session

from app.models import MaterialCard, MaterialSpecFacet
from app.services.commodity_registry import seed_commodity_schemas
from app.services.desc_extractor.writer import extract_and_record, extract_and_record_specs
from app.services.spec_write_service import record_spec


def _facets(db: Session, card_id: int) -> dict:
    rows = db.query(MaterialSpecFacet).filter_by(material_card_id=card_id).all()
    return {r.spec_key: (r.value_text if r.value_text is not None else r.value_numeric) for r in rows}


def _card(db: Session, mpn: str, category: str | None, description: str) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        category=category,
        description=description,
    )
    db.add(card)
    db.flush()
    return card


def test_desc_writes_facets_for_hdd_description(db_session: Session):
    seed_commodity_schemas(db_session)
    card = _card(db_session, "44X2459", "hdd", 'HD, 450GB, 15KRPM, 3.5", Fibre Channel')

    stats = extract_and_record_specs(db_session, [card.id])
    db_session.commit()

    assert stats == {"parsed": 1, "written": 4, "failed": 0}
    f = _facets(db_session, card.id)
    assert f["capacity_gb"] == 450
    assert f["rpm"] == "15000"
    assert f["form_factor"] == '3.5"'
    assert f["interface"] == "FC"
    # Every JSONB entry carries the desc_parse provenance at 0.90.
    for key in ("capacity_gb", "rpm", "form_factor", "interface"):
        entry = card.specs_structured[key]
        assert entry["source"] == "desc_parse"
        assert entry["confidence"] == 0.90


def test_desc_writes_dram_including_seeded_rank(db_session: Session):
    seed_commodity_schemas(db_session)
    card = _card(db_session, "46W0769M", "dram", "Mem, 16GB DDR4 2Rx4 PC4-2400T RDIMM")

    written = extract_and_record(db_session, card)
    db_session.commit()

    # All 6 extracted keys persist — the seeded dram rank enum mirrors the extractor's
    # _RANK_VALID set (pinned by the drift guard in test_desc_extractor_routing.py).
    assert written == 6
    f = _facets(db_session, card.id)
    assert f["capacity_gb"] == 16
    assert f["ddr_type"] == "DDR4"
    assert f["speed_mhz"] == 2400
    assert f["form_factor"] == "RDIMM"
    assert f["ecc"] == "true"
    assert f["rank"] == "2Rx4"
    assert card.specs_structured["rank"]["source"] == "desc_parse"


def test_desc_writes_non_ecc_as_false_facet(db_session: Session):
    # The ecc=False path must persist as a searchable "false" facet (record_spec's
    # boolean path), not be dropped — Non-ECC is a real, filterable property.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "KCP316ND8/8", "dram", "Memory, 8GB DDR3 1600MHz Non-ECC UDIMM, Kingston")

    written = extract_and_record(db_session, card)
    db_session.commit()

    assert written == 5
    f = _facets(db_session, card.id)
    assert f["ecc"] == "false"
    assert card.specs_structured["ecc"]["source"] == "desc_parse"


def test_desc_rerun_is_idempotent_and_rewrites_corrected_descriptions(db_session: Session):
    # Re-enrichment runs the desc-parse pass again over the same card. Pin the intended
    # semantics: an unchanged description re-writes the SAME values (equal tier+confidence,
    # newer updated_at → the ladder lets the rerun overwrite in place, so written re-counts
    # — data stays identical), and a corrected description MUST replace stale values.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "46W0769M", "dram", "Mem, 16GB DDR4 2Rx4 PC4-2400T RDIMM")

    first = extract_and_record(db_session, card)
    db_session.commit()
    before = _facets(db_session, card.id)

    second = extract_and_record(db_session, card)
    db_session.commit()

    assert first == second == 6  # same-source equal-(tier, confidence) priors lose the
    # updated_at tie-break and are overwritten in place — re-runs re-count, data unchanged
    assert _facets(db_session, card.id) == before
    assert card.specs_structured["capacity_gb"]["source"] == "desc_parse"

    # Description corrected between runs (16GB was wrong) — the new value must win.
    card.description = "Mem, 32GB DDR4 2Rx4 PC4-2400T RDIMM"
    db_session.flush()
    assert extract_and_record(db_session, card) == 6
    db_session.commit()
    assert _facets(db_session, card.id)["capacity_gb"] == 32


def test_desc_writes_facets_for_phase2_commodity(db_session: Session):
    # A power_supplies card (no MPN decoder exists for PSUs — desc_parse is its top
    # non-vendor source) writes both the numeric wattage and the enum psu_class
    # through record_spec, with desc_parse provenance at 0.90.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "01KL563", "power_supplies", "PSU, 1460W 240V/200V AC Hot Swap for EN 62368-1")

    stats = extract_and_record_specs(db_session, [card.id])
    db_session.commit()

    assert stats == {"parsed": 1, "written": 2, "failed": 0}
    f = _facets(db_session, card.id)
    assert f["wattage"] == 1460
    assert f["psu_class"] == "Server/Redundant"
    for key in ("wattage", "psu_class"):
        entry = card.specs_structured[key]
        assert entry["source"] == "desc_parse"
        assert entry["confidence"] == 0.90


def test_desc_writes_phase2_seed_extension_members(db_session: Session):
    # The phase-2 seed APPENDS must actually round-trip through record_spec's enum
    # validation: tape LTO-3 + USB and the motherboards board_type spec are all new.
    seed_commodity_schemas(db_session)
    tape = _card(db_session, "AA928A", "tape_drives", "Tape Drive, 400/800gb Ultrium Lto-3 HH SCSI LVD External")
    usb_tape = _card(db_session, "Q1581SB", "tape_drives", "DAT160 INTERNAL USB TAPE DRIVE 80/160GB MFG REF")
    board = _card(db_session, "5B20T04908", "motherboards", "5B20T04908:MB WHL I7 DIS 4G 8G WIN")

    stats = extract_and_record_specs(db_session, [tape.id, usb_tape.id, board.id])
    db_session.commit()

    assert stats == {"parsed": 3, "written": 6, "failed": 0}
    assert _facets(db_session, tape.id) == {
        "drive_type": "LTO-3",
        "interface": "SCSI",
        "form_factor": "Half-Height",
    }
    assert _facets(db_session, usb_tape.id) == {"drive_type": "DAT", "interface": "USB"}
    assert _facets(db_session, board.id) == {"board_type": "System Board"}


def test_desc_skips_higher_confidence_prior_on_phase2_commodity(db_session: Session):
    # A vendor-API gpu memory_gb at 0.95 must survive the desc-parse pass — the
    # writer's strictly-higher-confidence guard applies to the new commodities too.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "900-2G500-0000-000", "gpu", "SPS-PCA, NVIDIA Tesla V100 32GB Module")
    assert record_spec(db_session, card.id, "memory_gb", 16, source="vendor_api", confidence=0.95)

    written = extract_and_record(db_session, card)
    db_session.commit()

    assert written == 1  # gpu_family only; memory_gb skipped
    f = _facets(db_session, card.id)
    assert f["memory_gb"] == 16  # the 0.95 prior is untouched (desc said 32)
    assert card.specs_structured["memory_gb"]["source"] == "vendor_api"
    assert f["gpu_family"] == "Tesla"
    assert card.specs_structured["gpu_family"]["source"] == "desc_parse"


def test_desc_skips_uncategorized_card(db_session: Session):
    # Unlike the MPN decoder, a description is not a regex-gated commodity proof — the
    # writer never categorizes, and an uncategorized card cannot take facets anyway.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "00AR327", None, "HDD, 6Gbps 1.2TB 10K 2.5 Inch HDD, IBM")

    assert extract_and_record(db_session, card) == 0
    assert extract_and_record_specs(db_session, [card.id]) == {"parsed": 0, "written": 0, "failed": 0}
    assert _facets(db_session, card.id) == {}
    assert card.category is None  # never categorized from a description


def test_desc_skips_non_storage_memory_category(db_session: Session):
    # A capacitor card whose prose mentions drive-like tokens must write nothing.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "C0805C104K5RACTU", "capacitors", 'HD, 450GB, 15KRPM, 3.5", Fibre Channel')

    assert extract_and_record_specs(db_session, [card.id]) == {"parsed": 0, "written": 0, "failed": 0}
    assert _facets(db_session, card.id) == {}


def test_desc_never_overwrites_higher_confidence_decode_value(db_session: Session):
    # An mpn_decode capacity must survive a CONFLICTING desc-parsed capacity — the F1
    # tier ladder in record_spec (mpn_decode 85 > desc_parse 83) rejects the write; the
    # writer no longer carries its own confidence pre-gate.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "ST4000NM0035", "hdd", 'HD, 450GB, 15KRPM, 3.5", Fibre Channel')
    assert record_spec(db_session, card.id, "capacity_gb", 4000, source="mpn_decode", confidence=0.95)

    written = extract_and_record(db_session, card)
    db_session.commit()

    assert written == 3  # rpm + form_factor + interface; capacity skipped
    f = _facets(db_session, card.id)
    assert f["capacity_gb"] == 4000  # decode value untouched
    assert card.specs_structured["capacity_gb"]["source"] == "mpn_decode"
    assert f["rpm"] == "15000"


def test_desc_overwrites_lower_confidence_ai_value(db_session: Session):
    # A prior AI-mined value (spec_extraction, tier 60) yields to desc_parse (tier 83).
    seed_commodity_schemas(db_session)
    card = _card(db_session, "17P8581", "hdd", 'HDD, 300GB, 15,000 RPM, 3.5", FC w/Tray')
    assert record_spec(db_session, card.id, "rpm", "7200", source="spec_extraction", confidence=0.85)

    extract_and_record(db_session, card)
    db_session.commit()

    f = _facets(db_session, card.id)
    assert f["rpm"] == "15000"
    assert card.specs_structured["rpm"]["source"] == "desc_parse"


def test_savepoint_isolates_a_failing_card(db_session: Session, monkeypatch):
    # If a card's spec write raises mid-card, the per-card SAVEPOINT must roll back that
    # card's partial writes WITHOUT poisoning the shared transaction — sibling cards in
    # the same batch still commit and the counters stay honest.
    seed_commodity_schemas(db_session)
    bad = _card(db_session, "00AR144", "hdd", 'HDD, 4 TB 6GB 3.5" 7,200 RPM SAS, IBM')
    good = _card(db_session, "85Y6185", "hdd", 'HDD, 300GB, 15K RPM, 2.5", 6Gbps, SAS, IBM')

    import app.services.desc_extractor.writer as writer_mod

    real_record_spec = writer_mod.record_spec
    calls = {"bad": 0}

    def flaky(db, card_id, *args, **kwargs):
        if card_id == bad.id:
            calls["bad"] += 1
            if calls["bad"] == 2:  # first key persists, second key fails mid-card
                db.flush()
                raise RuntimeError("simulated flush failure")
        return real_record_spec(db, card_id, *args, **kwargs)

    monkeypatch.setattr(writer_mod, "record_spec", flaky)

    stats = extract_and_record_specs(db_session, [bad.id, good.id])
    db_session.commit()  # must NOT raise — the bad card's savepoint kept the txn clean

    assert stats["parsed"] == 1  # only the good card
    assert stats["written"] == 4
    assert stats["failed"] == 1  # the bad card surfaces in the aggregate, not just the log
    assert _facets(db_session, bad.id) == {}  # bad card fully rolled back, even key 1
    f = _facets(db_session, good.id)
    assert f["capacity_gb"] == 300
    assert f["rpm"] == "15000"
    assert f["form_factor"] == '2.5"'
    assert f["interface"] == "SAS"


def test_batch_skips_missing_and_unparseable_cards(db_session: Session):
    seed_commodity_schemas(db_session)
    parseable = _card(db_session, "00AR323", "hdd", "HDD, IBM 600G 15K Sas 12gbps, IBM")
    no_grammar = _card(db_session, "78P2425", "dram", "Memory, Memory module, IBM")

    stats = extract_and_record_specs(db_session, [parseable.id, no_grammar.id, 999_999])
    db_session.commit()

    assert stats == {"parsed": 1, "written": 3, "failed": 0}
    assert _facets(db_session, parseable.id) == {"capacity_gb": 600, "rpm": "15000", "interface": "SAS"}
    assert _facets(db_session, no_grammar.id) == {}
