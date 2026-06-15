"""The facet-accuracy reconcile command — dry-run parity, correction, and delete paths.

Seeds cards with the PRE-FIX wrong facet values the 2026-06-10 audit AND the same-day
re-audit (round 2) found (written through record_spec exactly like production, then
backdated so the re-run's newer timestamp wins the same-tier ladder), and asserts the
reconcile command corrects, deletes, or leaves each row per its failure class.
"""

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.management.reconcile_decoded_facets import _classify, _facet_matches, reconcile
from app.models import MaterialCard, MaterialSpecFacet
from app.services.commodity_registry import seed_commodity_schemas
from app.services.spec_tiers import tier_for
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


def _seed_legacy_offenum_text(db: Session, card: MaterialCard, spec_key: str, value: str, source: str) -> None:
    """Seed a text facet whose value is no longer a valid enum member, bypassing
    record_spec.

    record_spec now rejects off-enum values (e.g. ``gpu_family='RTX'`` after "RTX" left the
    seeded enum in the trust hotfix), so a legacy bad row can no longer be written through it
    — yet such rows DO exist in the live DB from before the fix, and correcting them is exactly
    what the reconcile command is for. This mirrors what a pre-fix record_spec produced (JSONB
    source-of-truth entry + facet projection), backdated like _seed_wrong, with NO enum gate.
    """
    confidence = 0.95 if source == "mpn_decode" else 0.90
    tier = tier_for(source)
    specs = dict(card.specs_structured or {})
    specs[spec_key] = {
        "value": value,
        "source": source,
        "confidence": confidence,
        "tier": tier,
        "updated_at": _OLD_TS,
    }
    card.specs_structured = specs
    db.add(
        MaterialSpecFacet(
            material_card_id=card.id,
            category=card.category,
            spec_key=spec_key,
            value_text=value,
            source=source,
            confidence=confidence,
            tier=tier,
        )
    )
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
    # "RTX" left the seeded gpu_family enum (trust hotfix) so record_spec now rejects it;
    # seed the legacy bad row directly — correcting it is what reconcile is FOR.
    _seed_legacy_offenum_text(db, cards["gpu"], "gpu_family", "RTX", "desc_parse")
    _seed_wrong(db, cards["modern"], "capacity_gb", 4000, "mpn_decode")
    _seed_wrong(db, cards["wd_rev"], "capacity_gb", 10100, "mpn_decode")
    _seed_wrong(db, cards["seagate_trunc"], "capacity_gb", 120, "mpn_decode")
    _seed_wrong(db, cards["off_grid"], "capacity_gb", 17000, "mpn_decode")
    _seed_wrong(db, cards["nand"], "capacity_gb", 512, "desc_parse")
    # The dram card's CORRECT desc-parsed key — in scope under the generalized default
    # keys (every schema'd spec_key), but the re-run corroborates it: must survive.
    assert record_spec(db, cards["dram"].id, "ddr_type", "DDR3", source="desc_parse", confidence=0.90)
    db.commit()
    return cards


def test_dry_run_tallies_without_writing(db_session: Session):
    cards = _seed_audit_cards(db_session)

    summary = reconcile(db_session, apply=False)

    assert summary["mode"] == "dry-run"
    assert summary["corrected"] == 3  # WD legacy capacity + RTX family + WD revision digit
    assert summary["deleted"] == 6  # STMicro + legacy Seagate + Gb-bit dram + trunc + grid + NAND
    assert summary["unchanged"] == 2  # modern Seagate control + corroborated ddr_type
    assert summary["failed"] == 0
    assert summary["sources"] == ["mpn_decode", "desc_parse", "fru_matrix_decode", "fru_desc_parse"]
    assert "ddr_type" in summary["keys"]  # default keys = every schema'd spec_key
    assert summary["by_class"]["legacy_wd"] == {"corrected": 1}
    assert summary["by_class"]["stmicro_gate"] == {"deleted": 1}
    assert summary["by_class"]["legacy_seagate"] == {"deleted": 1}
    # The dram card's wrong capacity deletes; its corroborated ddr_type row (same
    # bit-token description) tallies unchanged in the same class.
    assert summary["by_class"]["gb_bit"] == {"deleted": 1, "unchanged": 1}
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
    # the dram card's corroborated desc_parse key survives (only capacity was wrong).
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
    assert second["unchanged"] == 5  # wd + gpu + wd_rev (now right) + modern control + ddr_type


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


def test_grid_emptied_capacity_only_rows_classify_as_capacity_grid(db_session: Session):
    # Misattribution fix: when the grid kills a capacity-ONLY decode (all legacy WD;
    # family-unmapped Seagate like ST800MM0006, which PASSES the MM envelope), the
    # decode used to return None and the row fell through to the shape-regex buckets
    # (legacy_wd / seagate_envelope). The dropped channel now carries the grid refusal,
    # so these tally under capacity_grid — the bucket of the gate that actually fired.
    seed_commodity_schemas(db_session)
    wd = _card(db_session, "WD555AB", "hdd")  # legacy WD shape, 55.5 GB off-grid
    st = _card(db_session, "ST800MM0006", "hdd")  # within MM envelope, 800 GB off-grid
    _seed_wrong(db_session, wd, "capacity_gb", 55.5, "mpn_decode")
    _seed_wrong(db_session, st, "capacity_gb", 800, "mpn_decode")
    db_session.commit()

    summary = reconcile(db_session, apply=True)

    assert summary["by_class"]["capacity_grid"] == {"deleted": 2}
    assert "legacy_wd" not in summary["by_class"]
    assert "seagate_envelope" not in summary["by_class"]
    db_session.expire_all()
    for card in (wd, st):
        assert "capacity_gb" not in _facets(db_session, card.id)


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


# ── _classify unit tests ──────────────────────────────────────────────────────


def test_classify_other_mpn_decode_non_wd_st(db_session: Session):
    """Line 85: DECODE_SOURCE + non-WD/ST/STMicro MPN returns 'other_mpn_decode'."""
    seed_commodity_schemas(db_session)
    card = _card(db_session, "K4B2G1646F", "dram", "DDR3 DRAM Samsung")
    _seed_wrong(db_session, card, "capacity_gb", 2, "mpn_decode")
    db_session.commit()

    facet = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id, spec_key="capacity_gb").one()
    result = _classify(facet, card)
    assert result == "other_mpn_decode"


def test_classify_other_desc_parse(db_session: Session):
    """Line 93: DESC_SOURCE + non-gpu_family, non-nand, no bit-token → 'other_desc_parse'."""
    seed_commodity_schemas(db_session)
    # Use dram category with a plain description — no bit token, no NAND context.
    card = _card(db_session, "MT16KTF1G64AZ", "dram", "DDR4 Module 16GB")
    _seed_wrong(db_session, card, "capacity_gb", 16, "desc_parse")
    db_session.commit()

    facet = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id, spec_key="capacity_gb").one()
    result = _classify(facet, card)
    assert result == "other_desc_parse"


# ── _facet_matches unit tests ─────────────────────────────────────────────────


def _make_schema(data_type: str):
    """Return a minimal mock schema object with the requested data_type."""
    schema = MagicMock()
    schema.data_type = data_type
    return schema


def _make_facet(*, value_numeric=None, value_text=None):
    """Build a minimal mock MaterialSpecFacet projection."""
    facet = MagicMock(spec=MaterialSpecFacet)
    facet.value_numeric = value_numeric
    facet.value_text = value_text
    return facet


def test_facet_matches_boolean_true():
    """Line 130: boolean schema — stored 'true' matches a truthy new_value."""
    facet = _make_facet(value_text="true")
    schema = _make_schema("boolean")
    assert _facet_matches(facet, schema, True) is True


def test_facet_matches_boolean_false():
    """Line 130: boolean schema — stored 'false' matches a falsy new_value."""
    facet = _make_facet(value_text="false")
    schema = _make_schema("boolean")
    assert _facet_matches(facet, schema, False) is True


def test_facet_matches_boolean_mismatch():
    """Line 130: boolean schema — stored 'true' does NOT match False."""
    facet = _make_facet(value_text="true")
    schema = _make_schema("boolean")
    assert _facet_matches(facet, schema, False) is False


def test_facet_matches_text_equal():
    """Line 131: text schema (or no schema) — value_text matches str(new_value)."""
    facet = _make_facet(value_text="GeForce")
    schema = _make_schema("text")
    assert _facet_matches(facet, schema, "GeForce") is True


def test_facet_matches_text_mismatch():
    """Line 131: text schema — stored value differs."""
    facet = _make_facet(value_text="RTX")
    schema = _make_schema("text")
    assert _facet_matches(facet, schema, "GeForce") is False


def test_facet_matches_no_schema_uses_text_fallback():
    """When schema is None the function falls through to the text comparison."""
    facet = _make_facet(value_text="4000")
    assert _facet_matches(facet, None, 4000) is True  # str(4000) == "4000"


# ── limit parameter (line 156) ───────────────────────────────────────────────


def test_reconcile_limit_restricts_cards_processed(db_session: Session):
    """Limit=1 causes only the first card to be processed."""
    seed_commodity_schemas(db_session)
    c1 = _card(db_session, "WD800BB", "hdd")
    c2 = _card(db_session, "WD1200BB", "hdd")
    _seed_wrong(db_session, c1, "capacity_gb", 80000, "mpn_decode")
    _seed_wrong(db_session, c2, "capacity_gb", 120000, "mpn_decode")
    db_session.commit()

    summary = reconcile(db_session, apply=False, limit=1)

    # With limit=1 only one card_id is in the working set.
    assert summary["cards"] == 1
    # Facets count reflects only the limited card set, not the full 2.
    assert summary["facets"] == 1


# ── schema_caches miss path (lines 167-168) ──────────────────────────────────


def test_reconcile_two_cards_different_categories_loads_schema_cache(db_session: Session):
    """Each new category triggers a schema_cache load — exercises the cache-miss
    branch."""
    seed_commodity_schemas(db_session)
    hdd_card = _card(db_session, "WD800BB", "hdd")
    dram_card = _card(db_session, "K4B2G1646F", "dram", "Mem, DDR3, 2Gb, 128*16, Samsung")
    _seed_wrong(db_session, hdd_card, "capacity_gb", 80000, "mpn_decode")
    _seed_wrong(db_session, dram_card, "capacity_gb", 2, "desc_parse")
    db_session.commit()

    # Dry-run should classify both cards without error, hitting the cache-miss path
    # for each distinct category.
    summary = reconcile(db_session, apply=False)
    assert summary["cards"] == 2


# ── SAVEPOINT rollback on record_spec exception (lines 231-234) ──────────────


def test_reconcile_savepoint_rolls_back_on_record_spec_failure(db_session: Session, monkeypatch):
    """When record_spec raises inside apply mode the savepoint is rolled back and the
    card is counted as failed (lines 231-234, 240-242)."""
    seed_commodity_schemas(db_session)
    card = _card(db_session, "WD800BB", "hdd")
    _seed_wrong(db_session, card, "capacity_gb", 80000, "mpn_decode")
    db_session.commit()

    call_count = 0

    def _exploding_record_spec(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal call_count
        call_count += 1
        raise RuntimeError("simulated record_spec failure")

    with patch("app.management.reconcile_decoded_facets.record_spec", side_effect=_exploding_record_spec):
        summary = reconcile(db_session, apply=True)

    assert summary["failed"] == 1
    assert summary["corrected"] == 0


# ── outer exception handler (lines 240-242) ──────────────────────────────────


def test_reconcile_outer_exception_counted_as_failed(db_session: Session, monkeypatch):
    """When db.get raises for a card_id the outer handler increments
    totals['failed']."""
    seed_commodity_schemas(db_session)
    card = _card(db_session, "WD800BB", "hdd")
    _seed_wrong(db_session, card, "capacity_gb", 80000, "mpn_decode")
    db_session.commit()

    original_get = db_session.get

    def _failing_get(model, pk, *args, **kwargs):  # noqa: ANN002, ANN003
        if model is MaterialCard:
            raise RuntimeError("simulated db.get failure")
        return original_get(model, pk, *args, **kwargs)

    monkeypatch.setattr(db_session, "get", _failing_get)

    summary = reconcile(db_session, apply=False)
    assert summary["failed"] == 1


# ── main() function (lines 263-283) ──────────────────────────────────────────


def test_reconcile_main_dry_run(db_session: Session, monkeypatch):
    """Main() with no --apply calls reconcile(apply=False) and then rollback."""

    from app.management import reconcile_decoded_facets as mod

    reconcile_calls: list[dict] = []

    def fake_reconcile(db, *, apply, limit, sources=None, keys=None):  # noqa: ANN001
        reconcile_calls.append({"apply": apply, "limit": limit, "sources": sources, "keys": keys})
        return {
            "mode": "dry-run",
            "sources": list(sources or []),
            "keys": [],
            "corrected": 0,
            "deleted": 0,
            "unchanged": 0,
            "failed": 0,
            "skipped": 0,
            "cards": 0,
            "facets": 0,
            "by_class": {},
        }

    rollback_called: list[bool] = []
    commit_called: list[bool] = []
    recorded: list[dict] = []

    class _FakeSession:
        def rollback(self):
            rollback_called.append(True)

        def commit(self):
            commit_called.append(True)

        def close(self):
            pass

    monkeypatch.setattr(mod, "reconcile", fake_reconcile)
    monkeypatch.setattr(mod, "record_reconcile_run", lambda db, summary: recorded.append(summary))
    import app.database as db_mod

    monkeypatch.setattr(db_mod, "SessionLocal", lambda: _FakeSession())

    mod.main.__globals__["__name__"]  # touch to ensure import is live
    # Call main() directly by reproducing its argv parsing (no sys.argv mutation needed).
    import sys

    old_argv = sys.argv
    try:
        sys.argv = ["reconcile_decoded_facets"]
        mod.main()
    finally:
        sys.argv = old_argv

    assert len(reconcile_calls) == 1
    assert reconcile_calls[0]["apply"] is False
    assert reconcile_calls[0]["sources"] == tuple(mod.DEFAULT_SOURCES)
    assert reconcile_calls[0]["keys"] is None  # default: every schema'd spec_key
    assert rollback_called  # dry-run always calls rollback
    # The durable run report persists AFTER the rollback (the row is the only write).
    assert len(recorded) == 1 and recorded[0]["mode"] == "dry-run"
    assert commit_called


def test_reconcile_main_apply(db_session: Session, monkeypatch):
    """Main() with --apply calls reconcile(apply=True) and skips rollback."""
    from app.management import reconcile_decoded_facets as mod

    reconcile_calls: list[dict] = []

    def fake_reconcile(db, *, apply, limit, sources=None, keys=None):  # noqa: ANN001
        reconcile_calls.append({"apply": apply, "limit": limit, "sources": sources, "keys": keys})
        return {
            "mode": "apply",
            "sources": list(sources or []),
            "keys": list(keys or []),
            "corrected": 0,
            "deleted": 0,
            "unchanged": 0,
            "failed": 0,
            "skipped": 0,
            "cards": 0,
            "facets": 0,
            "by_class": {},
        }

    rollback_called: list[bool] = []
    recorded: list[dict] = []

    class _FakeSession:
        def rollback(self):
            rollback_called.append(True)

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(mod, "reconcile", fake_reconcile)
    monkeypatch.setattr(mod, "record_reconcile_run", lambda db, summary: recorded.append(summary))
    import app.database as db_mod

    monkeypatch.setattr(db_mod, "SessionLocal", lambda: _FakeSession())

    import sys

    old_argv = sys.argv
    try:
        sys.argv = ["reconcile_decoded_facets", "--apply", "--sources", "fru_matrix_decode", "--keys", "capacity_gb"]
        mod.main()
    finally:
        sys.argv = old_argv

    assert len(reconcile_calls) == 1
    assert reconcile_calls[0]["apply"] is True
    assert reconcile_calls[0]["sources"] == ("fru_matrix_decode",)
    assert reconcile_calls[0]["keys"] == ("capacity_gb",)
    assert not rollback_called  # apply mode skips rollback
    assert len(recorded) == 1 and recorded[0]["mode"] == "apply"


# ── _facet_matches: numeric schema sub-branches (lines 124, 127-128) ─────────


def test_facet_matches_numeric_value_numeric_none():
    """Line 124: numeric schema but value_numeric is None → False."""
    facet = _make_facet(value_numeric=None, value_text=None)
    schema = _make_schema("numeric")
    assert _facet_matches(facet, schema, 4000) is False


def test_facet_matches_numeric_match():
    """Lines 126-127: numeric schema — stored value equals new_value within epsilon."""
    facet = _make_facet(value_numeric=4000.0)
    schema = _make_schema("numeric")
    assert _facet_matches(facet, schema, 4000) is True


def test_facet_matches_numeric_mismatch():
    """Lines 126-127: numeric schema — stored value differs from new_value."""
    facet = _make_facet(value_numeric=80.0)
    schema = _make_schema("numeric")
    assert _facet_matches(facet, schema, 4000) is False


def test_facet_matches_numeric_conversion_error():
    """Lines 127-128: numeric schema — TypeError/ValueError on bad value → False."""
    facet = _make_facet(value_numeric="not-a-number")
    schema = _make_schema("numeric")
    # float("not-a-number") raises ValueError → returns False
    assert _facet_matches(facet, schema, 4000) is False


# ── reconcile: orphaned facet (card deleted, lines 167-168) ──────────────────


def test_reconcile_orphaned_facet_row_is_skipped(db_session: Session, monkeypatch):
    """Lines 167-168: when db.get returns None for a card_id the facets are skipped."""
    seed_commodity_schemas(db_session)
    card = _card(db_session, "WD800BB", "hdd")
    _seed_wrong(db_session, card, "capacity_gb", 80000, "mpn_decode")
    db_session.commit()

    # Patch db.get to return None for MaterialCard lookups, simulating orphaned facets.
    original_get = db_session.get

    def _none_for_card(model, pk, *args, **kwargs):
        if model is MaterialCard:
            return None
        return original_get(model, pk, *args, **kwargs)

    monkeypatch.setattr(db_session, "get", _none_for_card)

    summary = reconcile(db_session, apply=False)

    # The orphaned facet's card was not found — 1 facet counted as skipped, not failed.
    assert summary["skipped"] >= 1
    assert summary["failed"] == 0
