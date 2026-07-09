"""tests/test_spec_tiers.py -- Tests for the source→tier provenance ladder (SP2/F1+F2).

Covers: app/services/spec_tiers.py (tier_for, resolve, set_category, SOURCE_TIER).
Depends on: conftest.py (db_session), MaterialCard with category provenance columns.

resolve() is a pure function (no DB); set_category mutates a MaterialCard's category +
category_source/confidence/tier through the ladder.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models import MaterialCard, MaterialCardAudit
from app.services.spec_tiers import SOURCE_TIER, recategorize, resolve, set_category, tier_for

# --- tier_for ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected_tier"),
    [
        ("manual", 100),
        ("trio_source", 95),  # TRIO ground truth — above vendor APIs
        ("digikey_api", 90),
        ("mouser_api", 90),
        ("nexar_api", 90),
        ("element14_api", 90),
        ("oemsecrets_api", 90),
        ("trio_source_ai", 88),  # AI-corrected TRIO — below vendor, above decode
        ("mpn_decode", 85),
        ("partsurfer", 80),
        ("psref", 80),
        ("web_search", 70),
        ("brokerbin", 65),
        ("spec_extraction", 60),
        ("ai_guess", 40),
        ("claude_opus_inferred", 40),
    ],
)
def test_tier_for_known_sources(source: str, expected_tier: int):
    assert tier_for(source) == expected_tier


@pytest.mark.parametrize("source", ["something_made_up", ""])
def test_tier_for_unknown_source_is_zero(source: str):
    assert tier_for(source) == 0


def test_source_tier_map_has_expected_keys():
    # The map must contain every source the spec mandates.
    assert SOURCE_TIER["manual"] == 100
    assert SOURCE_TIER["partsurfer"] == 80  # oem_scrape mapped to 80
    assert SOURCE_TIER["psref"] == 80


def test_trio_source_tiers_rank_correctly():
    # SP-Ingest: TRIO ground truth beats every vendor API; the AI-corrected variant beats
    # the deterministic decode but loses to vendor APIs.
    assert SOURCE_TIER["trio_source"] == 95
    assert SOURCE_TIER["trio_source_ai"] == 88
    assert SOURCE_TIER["trio_source"] > SOURCE_TIER["digikey_api"]  # 95 > 90
    assert SOURCE_TIER["trio_source_ai"] < SOURCE_TIER["digikey_api"]  # 88 < 90
    assert SOURCE_TIER["trio_source_ai"] > SOURCE_TIER["mpn_decode"]  # 88 > 85


def test_desc_parse_tier_sits_between_decode_and_ai_extraction():
    # The deterministic decoders replace the old run-order + writer pre-gate protection:
    # the ladder itself must pin mpn_decode > fru_matrix_decode > desc_parse > spec_extraction.
    assert SOURCE_TIER["fru_matrix_decode"] == 84
    assert SOURCE_TIER["desc_parse"] == 83
    assert SOURCE_TIER["mpn_decode"] > SOURCE_TIER["fru_matrix_decode"]  # 85 > 84
    assert SOURCE_TIER["fru_matrix_decode"] > SOURCE_TIER["desc_parse"]  # 84 > 83
    assert SOURCE_TIER["desc_parse"] > SOURCE_TIER["spec_extraction"]  # 83 > 60


# --- resolve ----------------------------------------------------------------


def _prov(tier: int, confidence: float, updated_at: str) -> dict:
    return {"tier": tier, "confidence": confidence, "updated_at": updated_at}


_T0 = "2026-06-01T00:00:00+00:00"
_T1 = "2026-06-02T00:00:00+00:00"


def test_resolve_none_existing_always_wins():
    assert resolve(None, _prov(0, 0.0, _T0)) is True


def test_resolve_higher_tier_always_wins_even_against_higher_confidence():
    # The headline regression: decode (tier 85) beats spec_extraction (tier 60) at 0.99.
    existing = _prov(60, 0.99, _T1)
    incoming = _prov(85, 0.50, _T0)
    assert resolve(existing, incoming) is True


def test_resolve_lower_tier_always_loses():
    existing = _prov(85, 0.95, _T0)
    incoming = _prov(60, 0.85, _T1)
    assert resolve(existing, incoming) is False


def test_resolve_equal_tier_higher_confidence_wins():
    assert resolve(_prov(60, 0.80, _T0), _prov(60, 0.90, _T0)) is True
    assert resolve(_prov(60, 0.90, _T0), _prov(60, 0.80, _T0)) is False


def test_resolve_exact_tier_conf_tie_newer_wins():
    assert resolve(_prov(60, 0.80, _T0), _prov(60, 0.80, _T1)) is True


def test_resolve_identical_timestamps_no_churn():
    # Exact tuple tie → incoming does NOT win (no needless churn).
    assert resolve(_prov(60, 0.80, _T0), _prov(60, 0.80, _T0)) is False


def test_resolve_explicit_none_values_coerce_instead_of_raising():
    # A hand-edited / legacy JSONB entry can carry an explicit null — the key IS present,
    # so .get(key, default) would hand None to the tuple comparison and raise TypeError.
    # resolve must coerce (None tier→0, None confidence→0.0, None updated_at→"").
    assert resolve({"tier": 60, "confidence": None, "updated_at": _T0}, _prov(60, 0.5, _T1)) is True
    assert resolve(_prov(60, 0.5, _T1), {"tier": 60, "confidence": None, "updated_at": _T0}) is False
    assert resolve({"tier": None, "confidence": None, "updated_at": None}, _prov(40, 0.1, _T0)) is True


def test_resolve_clamps_out_of_range_confidence():
    # A percent-style confidence (95 instead of 0.95) clamps to 1.0 — it can win a tie
    # against a lower real confidence but can never become an unbeatable >1.0 value.
    assert resolve(_prov(60, 1.0, _T1), {"tier": 60, "confidence": 95, "updated_at": _T0}) is False


def test_tier_for_unknown_source_warns_once():
    # An unregistered writer fails 100% of its writes — that must be production-visible
    # (WARNING), but only once per source (not once per row).
    from loguru import logger as loguru_logger

    import app.services.spec_tiers as st

    st._warned_unknown_sources.discard("totally_unregistered")
    warnings: list[str] = []
    sink_id = loguru_logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    try:
        assert tier_for("totally_unregistered") == 0
        assert tier_for("totally_unregistered") == 0
    finally:
        loguru_logger.remove(sink_id)
    assert sum("totally_unregistered" in w for w in warnings) == 1


def test_legacy_backfill_is_a_registered_ladder_key():
    # Migration 095 persists category_source='legacy_backfill' — every persisted source
    # must be a ladder key, or a future tier re-derivation would demote it to 0.
    from app.services.spec_tiers import (
        LEGACY_BACKFILL_CONFIDENCE,
        LEGACY_BACKFILL_SOURCE,
        LEGACY_BACKFILL_TIER,
    )

    assert SOURCE_TIER[LEGACY_BACKFILL_SOURCE] == LEGACY_BACKFILL_TIER == 50
    assert tier_for("legacy_backfill") == 50
    assert 0.0 < LEGACY_BACKFILL_CONFIDENCE < 1.0
    # Deliberate ranking: above every AI guess, below every real source.
    assert SOURCE_TIER["ai_guess"] < 50 < SOURCE_TIER["spec_extraction"]


# --- set_category -----------------------------------------------------------


def _card(db: Session, **kw) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=kw.pop("normalized_mpn", "SC-001"),
        display_mpn=kw.pop("display_mpn", "SC-001"),
        **kw,
    )
    db.add(card)
    db.flush()
    return card


def test_set_category_off_vocab_returns_false_no_write(db_session: Session):
    # "VPD Card" has no canonical key in any alias map (the 2026-06-09 taxonomy expansion
    # made "Integrated Circuits (ICs)" a real alias → ics_other, so it no longer works here).
    card = _card(db_session, normalized_mpn="off-vocab", category=None)
    wrote = set_category(card, "VPD Card", "claude_opus_inferred", 0.9)
    assert wrote is False
    assert card.category is None
    assert card.category_source is None
    assert card.category_tier is None


def test_set_category_writes_canonical_on_empty_card(db_session: Session):
    card = _card(db_session, normalized_mpn="empty-cat", category=None)
    # "Microprocessors - MPU" is an existing alias → "microprocessors" (case-insensitive).
    wrote = set_category(card, "Microprocessors - MPU", "mpn_decode", 0.95)
    assert wrote is True
    assert card.category == "microprocessors"  # alias-resolved + validated column
    assert card.category_source == "mpn_decode"
    assert card.category_confidence == 0.95
    assert card.category_tier == 85


def test_set_category_cannot_downgrade_higher_tier(db_session: Session):
    card = _card(
        db_session,
        normalized_mpn="vendor-cat",
        category="dram",
        category_source="digikey_api",
        category_confidence=1.0,
        category_tier=90,
    )
    wrote = set_category(card, "flash", "spec_extraction", 0.99)
    assert wrote is False
    assert card.category == "dram"
    assert card.category_source == "digikey_api"
    assert card.category_tier == 90


def test_set_category_higher_tier_corrects_lower(db_session: Session):
    card = _card(
        db_session,
        normalized_mpn="guess-cat",
        category="cpu",
        category_source="claude_opus_inferred",
        category_confidence=0.5,
        category_tier=40,
    )
    wrote = set_category(card, "dram", "mpn_decode", 0.95)
    assert wrote is True
    assert card.category == "dram"
    assert card.category_source == "mpn_decode"
    assert card.category_tier == 85


def test_set_category_junk_cannot_blank_real_category(db_session: Session):
    card = _card(
        db_session,
        normalized_mpn="real-cat",
        category="dram",
        category_source="mpn_decode",
        category_confidence=0.95,
        category_tier=85,
    )
    # Off-vocab incoming normalizes to None → never overwrites a real category.
    wrote = set_category(card, "Intel", "claude_opus_inferred", 0.9)
    assert wrote is False
    assert card.category == "dram"
    assert card.category_tier == 85


def test_set_category_equal_tier_higher_confidence_wins(db_session: Session):
    card = _card(
        db_session,
        normalized_mpn="eq-tier",
        category="dram",
        category_source="spec_extraction",
        category_confidence=0.70,
        category_tier=60,
    )
    wrote = set_category(card, "flash", "spec_extraction", 0.90)
    assert wrote is True
    assert card.category == "flash"
    assert card.category_confidence == 0.90


def test_set_category_equal_tier_lower_confidence_loses(db_session: Session):
    card = _card(
        db_session,
        normalized_mpn="eq-tier-lo",
        category="dram",
        category_source="spec_extraction",
        category_confidence=0.90,
        category_tier=60,
    )
    wrote = set_category(card, "flash", "spec_extraction", 0.70)
    assert wrote is False
    assert card.category == "dram"


def test_set_category_exact_tie_newer_updated_at_wins(db_session: Session):
    # Existing category written 2 days ago (category_updated_at — the category's OWN
    # timestamp, NOT the card-wide updated_at); equal tier + equal confidence → newer wins.
    old = datetime.now(UTC) - timedelta(days=2)
    card = _card(
        db_session,
        normalized_mpn="tie-newer",
        category="dram",
        category_source="spec_extraction",
        category_confidence=0.80,
        category_tier=60,
        category_updated_at=old,
    )
    wrote = set_category(card, "flash", "spec_extraction", 0.80)
    assert wrote is True
    assert card.category == "flash"
    assert card.category_updated_at is not None and card.category_updated_at > old


def test_set_category_stamps_category_updated_at_on_win(db_session: Session):
    card = _card(db_session, normalized_mpn="stamp-ts", category=None)
    assert card.category_updated_at is None
    assert set_category(card, "hdd", "trio_source", 1.0) is True
    assert card.category_updated_at is not None


def test_set_category_null_provenance_existing_ranks_at_legacy_floor(db_session: Session):
    # A valued category with NULL provenance (pre-ladder data, or a write that bypassed
    # set_category) ranks at the SAME mid-tier the migration backfill stamps (50): a
    # stray AI guess (40) can NOT flip it, but a decode (85) corrects it — identical
    # treatment whether the row existed at migration time or was written a minute later.
    card = _card(db_session, normalized_mpn="legacy-floor", category="dram")
    assert card.category_tier is None and card.category_source is None

    assert set_category(card, "flash", "ai_guess", 0.99) is False  # 40 < legacy floor 50
    assert card.category == "dram"
    assert card.category_tier is None  # losing write never stamps provenance

    assert set_category(card, "flash", "mpn_decode", 0.95) is True  # 85 > legacy floor 50
    assert card.category == "flash"
    assert card.category_source == "mpn_decode"
    assert card.category_tier == 85


def test_set_category_write_false_is_read_only_twin(db_session: Session):
    # write=False runs the full ladder and returns the same verdicts as write=True but
    # never mutates the card (dry-run parity contract).
    card = _card(
        db_session,
        normalized_mpn="dry-twin",
        category="dram",
        category_source="claude_opus_inferred",
        category_confidence=0.5,
        category_tier=40,
    )
    assert set_category(card, "hdd", "trio_source", 1.0, write=False) is True
    assert set_category(card, "hdd", "ai_guess", 0.4, write=False) is False  # 40/0.4 < 40/0.5
    assert card.category == "dram"  # untouched either way
    assert card.category_tier == 40


def test_set_category_flip_purges_stale_commodity_facets_and_specs(db_session: Session):
    # Re-categorization must not leave the old commodity's facet rows / JSONB entries
    # behind: a dram→hdd card whose old ddr_type facet survived would keep matching dram
    # deep-filters (silent cross-commodity filter corruption).
    from app.models import MaterialSpecFacet
    from app.services.commodity_registry import seed_commodity_schemas
    from app.services.spec_write_service import record_spec

    seed_commodity_schemas(db_session)
    card = _card(
        db_session,
        normalized_mpn="flip-purge",
        category="dram",
        category_source="claude_opus_inferred",
        category_confidence=0.5,
        category_tier=40,
    )
    assert record_spec(db_session, card.id, "ddr_type", "DDR4", source="mpn_decode", confidence=0.95) is True
    assert db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id).count() == 1

    assert set_category(card, "hdd", "trio_source", 1.0) is True
    db_session.flush()
    assert card.category == "hdd"
    # Old commodity's facet row AND its JSONB mirror are gone.
    assert db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id).count() == 0
    assert "ddr_type" not in (card.specs_structured or {})


# --- recategorize (P4.5: the single entry point for direct card.category writes) ---


def _audits(db: Session, action: str) -> list[MaterialCardAudit]:
    return db.query(MaterialCardAudit).filter_by(action=action).all()


def test_recategorize_normal_mode_delegates_to_ladder(db_session: Session):
    # force=False is a thin wrapper over set_category — a lower-tier source still loses.
    card = _card(
        db_session,
        normalized_mpn="recat-normal",
        category="dram",
        category_source="digikey_api",
        category_confidence=1.0,
        category_tier=90,
    )
    wrote = recategorize(db_session, card, "flash", source="spec_extraction", confidence=0.99)
    assert wrote is False
    assert card.category == "dram"
    assert card.category_source == "digikey_api"

    wrote = recategorize(db_session, card, "flash", source="trio_source", confidence=1.0)
    assert wrote is True
    assert card.category == "flash"
    assert card.category_source == "trio_source"
    assert card.category_tier == 95


def test_recategorize_normal_mode_writes_audit_row_only_on_win(db_session: Session):
    card = _card(db_session, normalized_mpn="recat-normal-audit", category=None)
    # Off-vocab -> set_category no-ops -> no audit row.
    assert recategorize(db_session, card, "Nonsense Value", source="claude_opus_inferred", confidence=0.5) is False
    assert _audits(db_session, "category_recategorize") == []

    assert recategorize(db_session, card, "hdd", source="trio_source", confidence=1.0) is True
    db_session.flush()
    rows = _audits(db_session, "category_recategorize")
    assert len(rows) == 1
    assert rows[0].details["to"] == "hdd"
    assert rows[0].details["force"] is False


def test_recategorize_force_mode_bypasses_ladder_preserves_provenance(db_session: Session):
    # force=True writes unconditionally — a tier-90 category can be "corrected" to a
    # differently-spelled value even by a nominally lower-tier caller, and the ORIGINAL
    # provenance columns are left completely untouched (only the string form changes).
    card = _card(
        db_session,
        normalized_mpn="recat-force",
        category="dram",
        category_source="digikey_api",
        category_confidence=0.9,
        category_tier=90,
    )
    wrote = recategorize(db_session, card, "hdd", source="legacy_backfill", confidence=0.5, force=True)
    assert wrote is True
    assert card.category == "hdd"
    # Provenance columns are untouched — force mode never restamps them.
    assert card.category_source == "digikey_api"
    assert card.category_confidence == 0.9
    assert card.category_tier == 90


def test_recategorize_force_mode_noop_when_category_unchanged(db_session: Session):
    card = _card(db_session, normalized_mpn="recat-force-noop", category="hdd", category_source="digikey_api")
    wrote = recategorize(db_session, card, "hdd", source="digikey_api", force=True)
    assert wrote is False
    assert _audits(db_session, "category_recategorize") == []


def test_recategorize_force_mode_purges_stale_facets_and_audits(db_session: Session):
    from app.models import MaterialSpecFacet
    from app.services.commodity_registry import seed_commodity_schemas
    from app.services.spec_write_service import record_spec

    seed_commodity_schemas(db_session)
    card = _card(
        db_session,
        normalized_mpn="recat-force-purge",
        category="dram",
        category_source="digikey_api",
        category_confidence=0.9,
        category_tier=90,
    )
    assert record_spec(db_session, card.id, "ddr_type", "DDR4", source="mpn_decode", confidence=0.95) is True
    assert db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id).count() == 1

    wrote = recategorize(db_session, card, "hdd", source="digikey_api", force=True, reason="re-spell in place")
    db_session.flush()

    assert wrote is True
    assert card.category == "hdd"
    assert card.category_source == "digikey_api"  # untouched — same evidence, just re-spelled
    # The old commodity's facet row + JSONB mirror are purged, same guarantee set_category
    # gives normal-mode callers on a real category flip.
    assert db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id).count() == 0
    assert "ddr_type" not in (card.specs_structured or {})

    rows = _audits(db_session, "category_recategorize")
    assert len(rows) == 1
    assert rows[0].details == {
        "from": "dram",
        "to": "hdd",
        "source": "digikey_api",
        "force": True,
        "reason": "re-spell in place",
    }


def test_fru_desc_parse_tier_sits_between_desc_parse_and_partsurfer():
    # Wave 3A: the desc grammar run over a FRU's LINKED qual-sheet descriptions is
    # one hop weaker than the card's OWN description (desc_parse 83) but stronger
    # than the OEM scrapers (partsurfer/psref 80).
    assert SOURCE_TIER["fru_desc_parse"] == 82
    assert SOURCE_TIER["desc_parse"] > SOURCE_TIER["fru_desc_parse"]  # 83 > 82
    assert SOURCE_TIER["fru_desc_parse"] > SOURCE_TIER["partsurfer"]  # 82 > 80
    assert SOURCE_TIER["fru_matrix_decode"] > SOURCE_TIER["fru_desc_parse"]  # 84 > 82


# --- set_brand / set_manufacturer (dual-brand, migration 097) -----------------


def test_set_brand_rejects_none_empty_whitespace(db_session: Session):
    card = _card(db_session, normalized_mpn="brand-empty")
    from app.services.spec_tiers import set_brand, set_manufacturer

    for empty in (None, "", "   "):
        assert set_brand(card, empty, "trio_source", 0.9) is False
        assert set_manufacturer(card, empty, "trio_source", 0.9) is False
    assert card.brand is None
    assert card.manufacturer is None
    assert card.brand_source is None
    assert card.manufacturer_source is None


def test_set_brand_and_manufacturer_reject_garbage_fragments(db_session: Session):
    # The ladder is the single arbitration point — packing-suffix fragments ("F)",
    # "LF(T") and single chars die here for EVERY writer, not just the ingest parser.
    card = _card(db_session, normalized_mpn="brand-garbage")
    from app.services.spec_tiers import set_brand, set_manufacturer

    for junk in ("F)", "F", "LF(T", "TSOP)"):
        assert set_brand(card, junk, "trio_source", 0.9) is False, junk
        assert set_manufacturer(card, junk, "trio_source", 0.9) is False, junk
    assert card.brand is None
    assert card.manufacturer is None
    assert card.manufacturer_source is None


def test_set_brand_writes_on_empty_card_with_provenance(db_session: Session):
    from app.services.spec_tiers import set_brand

    card = _card(db_session, normalized_mpn="brand-fresh")
    assert set_brand(card, "IBM", "desc_parse", 0.85) is True
    assert card.brand == "IBM"
    assert card.brand_source == "desc_parse"
    assert card.brand_confidence == 0.85
    assert card.brand_tier == 83
    assert card.brand_updated_at is not None


def test_set_manufacturer_higher_tier_corrects_lower(db_session: Session):
    from app.services.spec_tiers import set_manufacturer

    card = _card(
        db_session,
        normalized_mpn="maker-correct",
        manufacturer="IBM",
        manufacturer_source="desc_parse",
        manufacturer_confidence=0.85,
        manufacturer_tier=83,
    )
    assert set_manufacturer(card, "Seagate", "trio_source", 0.9) is True
    assert card.manufacturer == "Seagate"  # verbatim — manufacturers table unseeded here
    assert card.manufacturer_source == "trio_source"
    assert card.manufacturer_tier == 95


def test_set_manufacturer_lower_tier_cannot_downgrade(db_session: Session):
    from app.services.spec_tiers import set_manufacturer

    card = _card(
        db_session,
        normalized_mpn="maker-keep",
        manufacturer="Seagate Technology",
        manufacturer_source="trio_source",
        manufacturer_confidence=0.9,
        manufacturer_tier=95,
    )
    assert set_manufacturer(card, "Kingston", "desc_parse", 0.99) is False
    assert card.manufacturer == "Seagate Technology"
    assert card.manufacturer_source == "trio_source"


def test_set_brand_equal_tier_higher_confidence_wins(db_session: Session):
    from app.services.spec_tiers import set_brand

    card = _card(
        db_session,
        normalized_mpn="brand-eq-tier",
        brand="Dell",
        brand_source="desc_parse",
        brand_confidence=0.70,
        brand_tier=83,
    )
    assert set_brand(card, "IBM", "desc_parse", 0.90) is True
    assert card.brand == "IBM"
    assert set_brand(card, "Lenovo", "desc_parse", 0.50) is False  # lower conf loses
    assert card.brand == "IBM"


def test_set_manufacturer_null_provenance_existing_ranks_at_legacy_floor(db_session: Session):
    # ALL pre-097 manufacturer values are valued-but-unprovenanced: they must rank at the
    # legacy_backfill floor (50) — an AI guess (40) cannot flip them, but trio_source (95)
    # maker evidence displaces a legacy OEM name (the ST300MP0016 headline case).
    from app.services.spec_tiers import set_manufacturer

    card = _card(db_session, normalized_mpn="maker-legacy", manufacturer="IBM")
    assert card.manufacturer_tier is None and card.manufacturer_source is None

    assert set_manufacturer(card, "Seagate", "ai_guess", 0.99) is False  # 40 < floor 50
    assert card.manufacturer == "IBM"
    assert card.manufacturer_tier is None  # losing write never stamps provenance

    assert set_manufacturer(card, "Seagate", "trio_source", 0.9) is True  # 95 > floor 50
    assert card.manufacturer == "Seagate"
    assert card.manufacturer_source == "trio_source"
    assert card.manufacturer_tier == 95


def test_set_brand_null_provenance_existing_ranks_at_legacy_floor(db_session: Session):
    from app.services.spec_tiers import set_brand

    card = _card(db_session, normalized_mpn="brand-legacy", brand="IBM")
    assert set_brand(card, "Dell", "ai_guess", 0.99) is False  # 40 < floor 50
    assert card.brand == "IBM"
    assert set_brand(card, "Dell", "desc_parse", 0.85) is True  # 83 > floor 50
    assert card.brand == "Dell"


def test_set_brand_and_manufacturer_write_false_twins_do_not_mutate(db_session: Session):
    from app.services.spec_tiers import set_brand, set_manufacturer

    card = _card(
        db_session,
        normalized_mpn="dual-dry-twin",
        brand="IBM",
        brand_source="legacy_backfill",
        brand_confidence=0.5,
        brand_tier=50,
        manufacturer="IBM",
        manufacturer_source="legacy_backfill",
        manufacturer_confidence=0.5,
        manufacturer_tier=50,
    )
    assert set_brand(card, "Dell", "desc_parse", 0.85, write=False) is True
    assert set_brand(card, "Dell", "ai_guess", 0.9, write=False) is False
    assert set_manufacturer(card, "Seagate", "trio_source", 0.9, write=False) is True
    assert set_manufacturer(card, "Seagate", "ai_guess", 0.9, write=False) is False
    # Untouched either way (dry-run parity contract).
    assert card.brand == "IBM"
    assert card.manufacturer == "IBM"
    assert card.brand_tier == 50
    assert card.manufacturer_tier == 50


def test_set_brand_normalizes_via_manufacturers_table(db_session: Session):
    from app.models import Manufacturer
    from app.services.spec_tiers import set_brand, set_manufacturer

    db_session.add(Manufacturer(canonical_name="Hewlett Packard Enterprise", aliases=["HPE", "HP"]))
    db_session.add(Manufacturer(canonical_name="Seagate Technology", aliases=["Seagate"]))
    db_session.flush()

    card = _card(db_session, normalized_mpn="dual-normalize")
    assert set_brand(card, "HP", "desc_parse", 0.85) is True
    assert card.brand == "Hewlett Packard Enterprise"
    assert set_manufacturer(card, "SEAGATE", "trio_source", 0.9) is True
    assert card.manufacturer == "Seagate Technology"


def test_set_brand_same_value_same_tier_refreshes_via_newer_timestamp(db_session: Session):
    # Exact (tier, confidence) tie → newer updated_at wins (same F1 rule as category).
    from app.services.spec_tiers import set_brand

    old = datetime.now(UTC) - timedelta(days=2)
    card = _card(
        db_session,
        normalized_mpn="brand-tie-newer",
        brand="IBM",
        brand_source="desc_parse",
        brand_confidence=0.85,
        brand_tier=83,
        brand_updated_at=old,
    )
    assert set_brand(card, "Dell", "desc_parse", 0.85) is True
    assert card.brand == "Dell"
    assert card.brand_updated_at is not None and card.brand_updated_at > old


def test_set_category_unchanged_behavior_through_shared_helper(db_session: Session):
    # Regression pin for the _set_provenanced_column extraction: set_category still
    # normalizes, still refuses junk, still stamps the same provenance columns.
    card = _card(db_session, normalized_mpn="cat-delegate", category=None)
    assert set_category(card, "Microprocessors - MPU", "mpn_decode", 0.95) is True
    assert card.category == "microprocessors"
    assert card.category_source == "mpn_decode"
    assert card.category_tier == 85
    assert set_category(card, "VPD Card", "manual", 1.0) is False  # off-vocab still rejected
    assert card.category == "microprocessors"


def test_set_brand_on_detached_card_warns_and_writes_verbatim(db_session: Session, monkeypatch):
    # A detached/transient card has no session, so alias canonicalization is SKIPPED —
    # the verbatim strip is written with full provenance (documented behavior), and the
    # first occurrence per process fires a WARNING so a detached-card writer is never
    # silent (it would fragment the brand facet: "HP" vs "Hewlett Packard Enterprise").
    from loguru import logger as loguru_logger

    import app.services.spec_tiers as spec_tiers_mod
    from app.models import Manufacturer
    from app.services.spec_tiers import set_brand

    db_session.add(Manufacturer(canonical_name="Hewlett Packard Enterprise", aliases=["HP"]))
    db_session.flush()

    monkeypatch.setattr(spec_tiers_mod, "_warned_detached_normalize", False)
    detached = MaterialCard(normalized_mpn="detached-001", display_mpn="DETACHED-001")  # never added

    warnings: list[str] = []
    sink_id = loguru_logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    try:
        assert set_brand(detached, "HP", "trio_source", 0.9) is True
    finally:
        loguru_logger.remove(sink_id)

    assert detached.brand == "HP"  # verbatim — canonicalization skipped without a session
    assert detached.brand_source == "trio_source"
    assert any("not session-attached" in w for w in warnings), warnings

    # Once per process: a second detached write does not warn again.
    warnings2: list[str] = []
    sink_id = loguru_logger.add(lambda message: warnings2.append(str(message)), level="WARNING")
    try:
        assert set_brand(detached, "HPE", "manual", 1.0) is True
    finally:
        loguru_logger.remove(sink_id)
    assert not any("not session-attached" in w for w in warnings2), warnings2


def test_set_category_non_manual_rejection_logs_at_info(db_session: Session):
    # Visibility rule (mirrors record_spec): a NON-manual writer that loses arbitration
    # must be visible at production log levels (INFO) for EVERY provenanced column —
    # category AND brand/manufacturer. The W8 enrichment writers (apply_authoritative /
    # apply_cross_ref_verified / apply_oem_sourced / apply_web_sourced) carry no
    # aggregate maker-conflict counter, so a DEBUG-only maker loss (e.g. a tier-90
    # connector maker losing to trio_source/95, or web_search/70 losing to decode/85
    # — below the conflict band when the prior isn't manual) would be
    # production-invisible. Only manual submissions stay at DEBUG (the human gets
    # endpoint feedback — toast/422).
    from loguru import logger as loguru_logger

    card = _card(
        db_session,
        normalized_mpn="info-loss",
        category="dram",
        category_source="trio_source",
        category_confidence=1.0,
        category_tier=95,
        manufacturer="Seagate",
        manufacturer_source="manual",
        manufacturer_confidence=1.0,
        manufacturer_tier=100,
    )

    infos: list[str] = []
    sink_id = loguru_logger.add(lambda message: infos.append(str(message)), level="INFO")
    try:
        # Non-manual category loss → INFO.
        assert set_category(card, "hdd", "digikey_api", 1.0) is False
        assert any("set_category" in m and "kept existing" in m for m in infos), infos
        # Non-manual manufacturer loss (vs a manual prior) → INFO too.
        infos.clear()
        from app.services.spec_tiers import set_manufacturer

        assert set_manufacturer(card, "Samsung", "mpn_decode", 0.9) is False
        assert any("set_manufacturer" in m and "kept existing" in m for m in infos), infos
        # A non-manual maker loss against a NON-manual prior (tier-90 connector maker vs
        # trio/95) logs the INFO line AND — since the trust-architecture dissent channel
        # — records an evidence-dissent artifact (kept!=manual, loser tier>=80, values
        # differ), so the contradiction surfaces in the needs-review filter instead of
        # being resolved silently by the ladder. The INFO line names BOTH sides.
        card2 = _card(
            db_session,
            normalized_mpn="info-loss-2",
            manufacturer="Samsung",
            manufacturer_source="trio_source",
            manufacturer_confidence=1.0,
            manufacturer_tier=95,
        )
        infos.clear()
        assert set_manufacturer(card2, "Seagate Technology", "digikey_api", 1.0) is False
        assert any("set_manufacturer" in m and "kept existing" in m for m in infos), infos
        assert card2.has_validation_conflict  # dissent recorded (authoritative-vs-authoritative)
        (dissent,) = card2.validation_conflicts
        assert dissent["kind"] == "dissent"
        assert dissent["key"] == "manufacturer"
        assert dissent["manual"]["value"] == "Samsung"
        assert dissent["manual"]["source"] == "trio_source"
        assert dissent["evidence"]["source"] == "digikey_api"
        assert dissent["evidence"]["value"] == "Seagate Technology"
    finally:
        loguru_logger.remove(sink_id)


# --- @validates("category") guard (SP3 ladder hardening) --------------------
# The guard on MaterialCard.category (app/models/intelligence.py) rejects any off-vocab
# direct assignment, so a future un-routed writer can no longer persist junk past the F1
# ladder. set_category (the single routed writer) only ever assigns canonical keys, so it
# passes the guard untouched; these pin the guard's contract directly.


class TestCategoryValidatesGuard:
    def test_canonical_key_assignment_passes(self, db_session: Session):
        card = _card(db_session, normalized_mpn="guard-ok", category="dram")
        assert card.category == "dram"

    def test_none_assignment_passes(self, db_session: Session):
        card = _card(db_session, normalized_mpn="guard-none", category=None)
        card.category = None
        assert card.category is None

    def test_off_vocab_assignment_raises(self, db_session: Session):
        card = _card(db_session, normalized_mpn="guard-bad", category=None)
        with pytest.raises(ValueError, match="canonical commodity key or None"):
            card.category = "IGBT Modules"  # the pre-#267 bypass-writer junk class

    def test_off_vocab_at_construction_raises(self, db_session: Session):
        with pytest.raises(ValueError, match="canonical commodity key or None"):
            MaterialCard(normalized_mpn="guard-ctor", display_mpn="GUARD-CTOR", category="Voltage Regulator")

    def test_set_category_canonical_value_passes_the_guard(self, db_session: Session):
        # The routed writer normalizes "IC" → canonical "ics_other" before assigning, so it
        # never trips the guard (the guard hardens OTHER paths, never the ladder).
        card = _card(db_session, normalized_mpn="guard-routed", category=None)
        assert set_category(card, "IC", "digikey_api", 0.9) is True
        assert card.category == "ics_other"


# --- record_evidence_dissent (trust architecture §1.2b) ----------------------
# The companion of record_validation_conflict for the case it never covers: an
# authoritative-vs-authoritative contradiction (kept value is NOT manual). Fires when a
# LOSING write has tier >= 80 AND a value DIFFERENT from the kept value; writes into the
# same validation_conflicts JSONB + has_validation_conflict flag, tagged kind='dissent'.


class TestRecordEvidenceDissent:
    def test_tier84_contradiction_records_dissent(self, db_session: Session):
        """The spec's canonical case: a tier-84 (fru_matrix_decode) contradiction of a
        kept trio_source (95) value records a dissent row and raises the flag."""
        from app.services.spec_tiers import record_evidence_dissent

        card = _card(db_session, normalized_mpn="dissent-84", category="hdd")
        wrote = record_evidence_dissent(
            card,
            "capacity_gb",
            {"source": "trio_source", "value": 1000, "tier": 95},
            {"source": "fru_matrix_decode", "tier": 84, "confidence": 0.9},
            373455,
        )
        assert wrote is True
        assert card.has_validation_conflict
        (entry,) = card.validation_conflicts
        assert entry["kind"] == "dissent"
        assert entry["key"] == "capacity_gb"
        assert entry["manual"]["value"] == 1000
        assert entry["manual"]["source"] == "trio_source"
        assert entry["manual"]["tier"] == 95
        assert entry["evidence"]["source"] == "fru_matrix_decode"
        assert entry["evidence"]["tier"] == 84
        assert entry["evidence"]["value"] == 373455

    def test_manual_kept_value_is_left_to_validation_conflict(self, db_session: Session):
        """A manual kept value is record_validation_conflict's job — dissent no-ops so
        exactly one recorder fires per loss."""
        from app.services.spec_tiers import record_evidence_dissent

        card = _card(db_session, normalized_mpn="dissent-manual", category="hdd")
        wrote = record_evidence_dissent(
            card,
            "capacity_gb",
            {"source": "manual", "value": 1000, "tier": 100},
            {"source": "mpn_decode", "tier": 85},
            500,
        )
        assert wrote is False
        assert not card.has_validation_conflict

    def test_below_band_loser_no_dissent(self, db_session: Session):
        """A loser below the authoritative band (tier < 80) never dissents."""
        from app.services.spec_tiers import record_evidence_dissent

        card = _card(db_session, normalized_mpn="dissent-lowtier", category="hdd")
        wrote = record_evidence_dissent(
            card,
            "capacity_gb",
            {"source": "mpn_decode", "value": 1000, "tier": 85},
            {"source": "web_search", "tier": 70},
            500,
        )
        assert wrote is False
        assert not card.has_validation_conflict

    def test_corroboration_clears_a_same_source_stale_dissent(self, db_session: Session):
        """A deterministic source that re-fires and now AGREES drops its own stale
        dissent and recomputes the flag (a fixed decoder must not flag forever)."""
        from app.services.spec_tiers import record_evidence_dissent

        card = _card(db_session, normalized_mpn="dissent-heal", category="hdd")
        assert record_evidence_dissent(
            card,
            "capacity_gb",
            {"source": "trio_source", "value": 1000, "tier": 95},
            {"source": "mpn_decode", "tier": 85},
            500,
        )
        assert card.has_validation_conflict
        # mpn_decode is fixed and now reports the kept value — its stale dissent clears.
        assert (
            record_evidence_dissent(
                card,
                "capacity_gb",
                {"source": "trio_source", "value": 1000, "tier": 95},
                {"source": "mpn_decode", "tier": 85},
                1000,
            )
            is False
        )
        assert not card.has_validation_conflict
        assert not (card.validation_conflicts or [])


# --- count_ladder_rejection (trust architecture §1.2c) -----------------------


class TestCountLadderRejection:
    def test_contradiction_bumps_the_contradiction_field(self, monkeypatch):
        import app.cache.intel_cache as intel_cache
        from app.services import spec_tiers

        calls: list[tuple] = []
        monkeypatch.setattr(intel_cache, "incr_hash_count", lambda key, field, **kw: calls.append((key, field, kw)))

        spec_tiers.count_ladder_rejection("trio_source", "mpn_decode", contradiction=True)
        assert len(calls) == 1
        key, field, kw = calls[0]
        assert key.startswith("ladder:rejections:")
        assert field == "trio_source|mpn_decode|contradiction"
        assert kw.get("ttl_days") == spec_tiers.REJECTION_COUNTER_TTL_DAYS

    def test_corroboration_classifies_distinctly(self, monkeypatch):
        from app.services import spec_tiers

        calls: list[tuple] = []
        import app.cache.intel_cache as intel_cache

        monkeypatch.setattr(intel_cache, "incr_hash_count", lambda key, field, **kw: calls.append((key, field)))

        spec_tiers.count_ladder_rejection("digikey_api", "web_search", contradiction=False)
        assert calls[0][1] == "digikey_api|web_search|corroboration"

    def test_unknown_sides_default_and_never_raises(self, monkeypatch):
        import app.cache.intel_cache as intel_cache
        from app.services import spec_tiers

        def _boom(*a, **k):
            raise RuntimeError("redis down")

        monkeypatch.setattr(intel_cache, "incr_hash_count", _boom)
        # Telemetry must never break the write path — the failure is swallowed.
        spec_tiers.count_ladder_rejection("", "", contradiction=True)
