"""The facet-accuracy reconcile command — dry-run parity, correction, and delete paths.

Seeds cards with the PRE-FIX wrong facet values the 2026-06-10 audit AND the same-day
re-audit (round 2) found (written through record_spec exactly like production, then
backdated so the re-run's newer timestamp wins the same-tier ladder), and asserts the
reconcile command corrects, deletes, or leaves each row per its failure class.
"""

from sqlalchemy.orm import Session

from app.management.reconcile_decoded_facets import reconcile
from app.models import MaterialCard, MaterialSpecFacet
from app.services.commodity_registry import seed_commodity_schemas
from app.services.spec_write_service import record_spec

_OLD_TS = "2026-01-01T00:00:00+00:00"


def _facets(db: Session, card_id: int) -> dict:
    rows = db.query(MaterialSpecFacet).filter_by(material_card_id=card_id).all()
    return {r.spec_key: (r.value_text if r.value_text is not None else r.value_numeric) for r in rows}


def _card(db: Session, mpn: str, category: str, description: str | None = None) -> MaterialCard:
    card = MaterialCard(normalized_mpn=mpn.lower(), display_mpn=mpn, category=category, description=description)
    db.add(card)
    db.flush()
    return card


def _seed_wrong(db: Session, card: MaterialCard, spec_key: str, value, source: str) -> None:
    """Write a pre-fix facet value through record_spec, then backdate its timestamp so
    the reconcile re-run (same source/tier/confidence, newer updated_at) wins the ladder
    deterministically."""
    confidence = 0.95 if source == "mpn_decode" else 0.90
    assert record_spec(db, card.id, spec_key, value, source=source, confidence=confidence)
    specs = dict(card.specs_structured)
    specs[spec_key] = {**specs[spec_key], "updated_at": _OLD_TS}
    card.specs_structured = specs
    db.flush()


def _seed_audit_cards(db: Session) -> dict[str, MaterialCard]:
    seed_commodity_schemas(db)
    cards = {
        # class 1 — legacy WD 1000× error (audit card 3648): corrected 80000 → 80
        "wd": _card(db, "WD800BB", "hdd"),
        # class 2/3 — STMicro transceiver behind the old Seagate gate (card 674852): deleted
        "stmicro": _card(db, "ST232BDR", "hdd"),
        # class 1 — legacy Seagate digit-swallow (card 195043): now None → deleted
        "seagate": _card(db, "ST373207LC", "hdd"),
        # class 4 — Gb bit-density coerced to GB (card 74143): now skipped → deleted
        "dram": _card(db, "K4B2G1646F", "dram", "Mem, DDR3, 2Gb, 128*16, Samsung"),
        # class 5 — RTX consumer fragmentation (card 583761): corrected RTX → GeForce
        "gpu": _card(db, "GPU3070OEM", "gpu", "NVIDIA, RTX, 3070"),
        # control — modern Seagate decode is already right: must stay untouched
        "modern": _card(db, "ST4000NM0035", "hdd"),
        # round 2 — WD modern revision digit (re-audit card 578746): corrected 10100 → 10000
        "wd_rev": _card(db, "WD101EFBX", "hdd"),
        # round 2 — digit-dropped truncation slips the Seagate shape gate (re-audit card
        # 120169): the envelope-gated decoder now yields None → deleted
        "seagate_trunc": _card(db, "ST120MM0198", "hdd"),
        # round 2 — off-grid capacity the decoder now drops (shipped-capacity grid): deleted
        "off_grid": _card(db, "MG09ACA17TE", "hdd"),
        # round 2 — bare-"G" NAND die density coerced to GB (re-audit card 74115): deleted
        "nand": _card(db, "MT29F512G08CBCAB", "dram", "MT29F512G08CBCAB, Micron, NAND, 512G, MLC"),
    }
    _seed_wrong(db, cards["wd"], "capacity_gb", 80000, "mpn_decode")
    _seed_wrong(db, cards["stmicro"], "capacity_gb", 232, "mpn_decode")
    _seed_wrong(db, cards["seagate"], "capacity_gb", 373207, "mpn_decode")
    _seed_wrong(db, cards["dram"], "capacity_gb", 2, "desc_parse")
    _seed_wrong(db, cards["gpu"], "gpu_family", "RTX", "desc_parse")
    _seed_wrong(db, cards["modern"], "capacity_gb", 4000, "mpn_decode")
    _seed_wrong(db, cards["wd_rev"], "capacity_gb", 10100, "mpn_decode")
    _seed_wrong(db, cards["seagate_trunc"], "capacity_gb", 120, "mpn_decode")
    _seed_wrong(db, cards["off_grid"], "capacity_gb", 17000, "mpn_decode")
    _seed_wrong(db, cards["nand"], "capacity_gb", 512, "desc_parse")
    # The dram card's CORRECT desc-parsed key — outside TARGET_SPEC_KEYS, must survive.
    assert record_spec(db, cards["dram"].id, "ddr_type", "DDR3", source="desc_parse", confidence=0.90)
    db.commit()
    return cards


def test_dry_run_tallies_without_writing(db_session: Session):
    cards = _seed_audit_cards(db_session)

    summary = reconcile(db_session, apply=False)

    assert summary["mode"] == "dry-run"
    assert summary["corrected"] == 3  # WD legacy capacity + RTX family + WD revision digit
    assert summary["deleted"] == 6  # STMicro + legacy Seagate + Gb-bit dram + trunc + grid + NAND
    assert summary["unchanged"] == 1  # modern Seagate control
    assert summary["failed"] == 0
    assert summary["by_class"]["legacy_wd"] == {"corrected": 1}
    assert summary["by_class"]["stmicro_gate"] == {"deleted": 1}
    assert summary["by_class"]["legacy_seagate"] == {"deleted": 1}
    assert summary["by_class"]["gb_bit"] == {"deleted": 1}
    assert summary["by_class"]["rtx_family"] == {"corrected": 1}
    # Round-2 classes: the modern-shape control row moved from the round-1 legacy_seagate
    # bucket into the (more precise) envelope-gated branch bucket.
    assert summary["by_class"]["wd_revision_digit"] == {"corrected": 1}
    assert summary["by_class"]["seagate_envelope"] == {"deleted": 1, "unchanged": 1}
    assert summary["by_class"]["capacity_grid"] == {"deleted": 1}
    assert summary["by_class"]["nand_density"] == {"deleted": 1}

    # Dry-run wrote NOTHING: every wrong value and every facet row is still in place.
    db_session.expire_all()
    assert _facets(db_session, cards["wd"].id)["capacity_gb"] == 80000
    assert _facets(db_session, cards["stmicro"].id)["capacity_gb"] == 232
    assert _facets(db_session, cards["dram"].id)["capacity_gb"] == 2
    assert _facets(db_session, cards["gpu"].id)["gpu_family"] == "RTX"
    assert _facets(db_session, cards["wd_rev"].id)["capacity_gb"] == 10100
    assert _facets(db_session, cards["seagate_trunc"].id)["capacity_gb"] == 120
    assert _facets(db_session, cards["off_grid"].id)["capacity_gb"] == 17000
    assert _facets(db_session, cards["nand"].id)["capacity_gb"] == 512


def test_apply_corrects_deletes_and_matches_dry_run(db_session: Session):
    cards = _seed_audit_cards(db_session)

    dry = reconcile(db_session, apply=False)
    applied = reconcile(db_session, apply=True)

    # Dry-run parity: the read-only pass must predict apply mode exactly.
    for key in ("cards", "facets", "corrected", "deleted", "unchanged", "skipped", "by_class"):
        assert dry[key] == applied[key], f"dry-run/apply diverged on {key}"

    db_session.expire_all()
    # class 1 corrected: same source, newer ladder timestamp, right value.
    assert _facets(db_session, cards["wd"].id)["capacity_gb"] == 80
    wd_entry = cards["wd"].specs_structured["capacity_gb"]
    assert wd_entry["value"] == 80
    assert wd_entry["source"] == "mpn_decode"
    assert wd_entry["updated_at"] > _OLD_TS
    # round 2 corrected: the revision-digit read (10.1 TB ghost) becomes 10 TB.
    assert _facets(db_session, cards["wd_rev"].id)["capacity_gb"] == 10000
    wd_rev_entry = cards["wd_rev"].specs_structured["capacity_gb"]
    assert wd_rev_entry["source"] == "mpn_decode"
    assert wd_rev_entry["updated_at"] > _OLD_TS
    # deleted classes (rounds 1+2): facet row AND specs_structured entry are gone.
    for name in ("stmicro", "seagate", "dram", "seagate_trunc", "off_grid", "nand"):
        assert "capacity_gb" not in _facets(db_session, cards[name].id), name
        assert "capacity_gb" not in (cards[name].specs_structured or {}), name
    # the dram card's non-targeted desc_parse key survives (only capacity was wrong).
    assert _facets(db_session, cards["dram"].id)["ddr_type"] == "DDR3"
    # class 5 corrected: one family, the catalog-canonical one.
    assert _facets(db_session, cards["gpu"].id)["gpu_family"] == "GeForce"
    assert cards["gpu"].specs_structured["gpu_family"]["source"] == "desc_parse"
    # control untouched, timestamp included (no churn on already-correct rows).
    assert _facets(db_session, cards["modern"].id)["capacity_gb"] == 4000
    assert cards["modern"].specs_structured["capacity_gb"]["updated_at"] == _OLD_TS


def test_apply_is_idempotent(db_session: Session):
    _seed_audit_cards(db_session)
    first = reconcile(db_session, apply=True)
    second = reconcile(db_session, apply=True)

    assert first["corrected"] == 3 and first["deleted"] == 6
    # Second pass finds the corrected rows already right and the deleted rows gone.
    assert second["corrected"] == 0
    assert second["deleted"] == 0
    assert second["unchanged"] == 4  # wd + gpu + wd_rev (now right) + modern control


def test_delete_skipped_when_jsonb_provenance_drifted(db_session: Session):
    seed_commodity_schemas(db_session)
    card = _card(db_session, "ST232BDR", "hdd")
    _seed_wrong(db_session, card, "capacity_gb", 232, "mpn_decode")
    # Simulate drift: the JSONB winner no longer belongs to the facet's source —
    # the reconcile must never delete another source's value.
    specs = dict(card.specs_structured)
    specs["capacity_gb"] = {**specs["capacity_gb"], "source": "manual", "tier": 100}
    card.specs_structured = specs
    db_session.commit()

    summary = reconcile(db_session, apply=True)

    assert summary["deleted"] == 0
    assert summary["skipped"] == 1
    assert summary["by_class"]["stmicro_gate"] == {"skipped_provenance_mismatch": 1}
    db_session.expire_all()
    assert _facets(db_session, card.id)["capacity_gb"] == 232  # untouched
    assert card.specs_structured["capacity_gb"]["source"] == "manual"


def test_untargeted_sources_and_keys_are_never_selected(db_session: Session):
    seed_commodity_schemas(db_session)
    card = _card(db_session, "WD800BB", "hdd")
    # A manual capacity (higher tier) is outside the source filter entirely.
    assert record_spec(db_session, card.id, "capacity_gb", 80, source="manual", confidence=1.0)
    db_session.commit()

    summary = reconcile(db_session, apply=True)

    assert summary["facets"] == 0
    db_session.expire_all()
    assert _facets(db_session, card.id)["capacity_gb"] == 80
    assert card.specs_structured["capacity_gb"]["source"] == "manual"
