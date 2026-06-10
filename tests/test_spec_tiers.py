"""tests/test_spec_tiers.py -- Tests for the source→tier provenance ladder (SP2/F1+F2).

Covers: app/services/spec_tiers.py (tier_for, resolve, set_category, SOURCE_TIER).
Depends on: conftest.py (db_session), MaterialCard with category provenance columns.

resolve() is a pure function (no DB); set_category mutates a MaterialCard's category +
category_source/confidence/tier through the ladder.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.services.spec_tiers import SOURCE_TIER, resolve, set_category, tier_for

# --- tier_for ---------------------------------------------------------------


def test_tier_for_known_sources():
    assert tier_for("manual") == 100
    assert tier_for("trio_source") == 95  # TRIO ground truth — above vendor APIs
    assert tier_for("digikey_api") == 90
    assert tier_for("mouser_api") == 90
    assert tier_for("nexar_api") == 90
    assert tier_for("element14_api") == 90
    assert tier_for("oemsecrets_api") == 90
    assert tier_for("trio_source_ai") == 88  # AI-corrected TRIO — below vendor, above decode
    assert tier_for("mpn_decode") == 85
    assert tier_for("partsurfer") == 80
    assert tier_for("psref") == 80
    assert tier_for("web_search") == 70
    assert tier_for("brokerbin") == 65
    assert tier_for("spec_extraction") == 60
    assert tier_for("ai_guess") == 40
    assert tier_for("claude_opus_inferred") == 40


def test_tier_for_unknown_source_is_zero():
    assert tier_for("something_made_up") == 0
    assert tier_for("") == 0


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
    # The deterministic description grammar replaces the old run-order + writer pre-gate
    # protection: the ladder itself must pin mpn_decode > desc_parse > spec_extraction.
    assert SOURCE_TIER["desc_parse"] == 83
    assert SOURCE_TIER["mpn_decode"] > SOURCE_TIER["desc_parse"]  # 85 > 83
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
    # Existing card with an old updated_at; equal tier + equal confidence → newer wins.
    old = datetime.now(timezone.utc) - timedelta(days=2)
    card = _card(
        db_session,
        normalized_mpn="tie-newer",
        category="dram",
        category_source="spec_extraction",
        category_confidence=0.80,
        category_tier=60,
    )
    card.updated_at = old
    db_session.flush()
    wrote = set_category(card, "flash", "spec_extraction", 0.80)
    assert wrote is True
    assert card.category == "flash"
