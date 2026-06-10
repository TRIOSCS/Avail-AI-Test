"""tests/test_backfill_dual_brand.py — dual-brand backfill (B1-B4).

Covers: app/management/backfill_dual_brand.py — B1 legacy-OEM reclassify (lossless copy
to brand), B2 maker from fru_links mfg_model rows (trio_source/0.9 displaces the legacy
OEM value), B3 trailing-description tokens (regex-gated to the literal lists), B4
verification gate (ST300MP0016 must end brand=IBM ∧ manufacturer=Seagate Technology),
dry-run-by-default parity (tallies == apply tallies, NOTHING written), duplicate
mfg_model idempotency, and the CLI exit code.

Called by: pytest
Depends on: conftest.py (db_session), MaterialCard + FruLink + Manufacturer models.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.constants import FruLinkKind
from app.management.backfill_dual_brand import main, run_backfill
from app.models import FruLink, Manufacturer, MaterialCard


def _seed_manufacturers(db: Session) -> None:
    db.add(Manufacturer(canonical_name="IBM", aliases=[]))
    db.add(Manufacturer(canonical_name="Seagate Technology", aliases=["Seagate"]))
    db.add(Manufacturer(canonical_name="Dell Technologies", aliases=["Dell"]))
    db.flush()


def _card(db: Session, mpn: str, **kw) -> MaterialCard:
    card = MaterialCard(normalized_mpn=mpn.lower(), display_mpn=mpn, **kw)
    db.add(card)
    db.flush()
    return card


def _mfg_link(db: Session, related_norm: str, manufacturer: str, sheet: str = "test_sheet") -> FruLink:
    # `sheet` varies for duplicate edges: uq_fru_links_edge spans
    # (fru_norm, related_norm, rel_kind, source_sheet) — real duplicates come from
    # different workbook sheets.
    link = FruLink(
        fru_raw="00TEST1",
        fru_norm="00test1",
        related_raw=related_norm.upper(),
        related_norm=related_norm,
        rel_kind=FruLinkKind.MFG_MODEL.value,
        manufacturer=manufacturer,
        source_sheet=sheet,
    )
    db.add(link)
    db.flush()
    return link


def _st300_fixture(db: Session) -> MaterialCard:
    """The headline dual-coverage card: OEM label in the description, maker in
    fru_links."""
    _seed_manufacturers(db)
    card = _card(db, "ST300MP0016", description='HDD, 300GB, 2.5" SED, 15K RPM, IBM')
    _mfg_link(db, "st300mp0016", "Seagate")
    return card


# --- The headline case ---------------------------------------------------------


def test_st300mp0016_ends_brand_ibm_manufacturer_seagate(db_session: Session):
    card = _st300_fixture(db_session)
    stats = run_backfill(db_session, apply=True)

    db_session.refresh(card)
    assert card.brand == "IBM"  # B3 desc_parse (B1 never ran — manufacturer was empty)
    assert card.brand_source == "desc_parse"
    assert card.brand_tier == 83
    assert card.manufacturer == "Seagate Technology"  # B2, normalized via the alias
    assert card.manufacturer_source == "trio_source"
    assert card.manufacturer_tier == 95
    assert card.manufacturer_confidence == 0.9
    assert stats["gate_passed"] is True
    assert stats["b2"]["manufacturers_set"] == 1
    assert stats["b3"]["brands_set"] == 1


def test_b2_trio_source_displaces_legacy_oem_in_manufacturer(db_session: Session):
    # A legacy card holding the OEM label in `manufacturer` (NULL provenance → floor 50):
    # B1 copies it to brand (lossless), then B2's tier-95 maker evidence displaces it.
    _seed_manufacturers(db_session)
    card = _card(db_session, "ST300MP0016", manufacturer="IBM", description='HDD, 300GB, 2.5" SED, 15K RPM, IBM')
    _mfg_link(db_session, "st300mp0016", "Seagate")

    stats = run_backfill(db_session, apply=True)
    db_session.refresh(card)

    assert card.brand == "IBM"
    assert card.manufacturer == "Seagate Technology"  # 95 beat the legacy-50 OEM value
    assert stats["b1"]["brands_set"] == 1
    assert stats["gate_passed"] is True
    # B3's desc_parse (83) brand write beat B1's legacy copy (50) — final provenance is
    # the strongest evidence, not the first writer.
    assert card.brand_source == "desc_parse"


# --- B1 ------------------------------------------------------------------------


def test_b1_copies_oem_label_to_brand_without_clearing_manufacturer(db_session: Session):
    _seed_manufacturers(db_session)
    card = _card(db_session, "00AR327", manufacturer="IBM")

    stats = run_backfill(db_session, apply=True)
    db_session.refresh(card)

    assert card.brand == "IBM"
    assert card.brand_source == "legacy_backfill"
    assert card.brand_tier == 50
    assert card.brand_confidence == 0.5
    assert card.manufacturer == "IBM"  # NOT cleared — lossless
    assert stats["b1"]["scanned"] == 1
    assert stats["b1"]["brands_set"] == 1


def test_b1_ignores_real_makers_in_manufacturer(db_session: Session):
    _seed_manufacturers(db_session)
    card = _card(db_session, "ST4000NM0035", manufacturer="Seagate Technology")

    stats = run_backfill(db_session, apply=True)
    db_session.refresh(card)

    assert card.brand is None  # Seagate is a maker, not an OEM_BRANDS member
    assert stats["b1"]["scanned"] == 0


def test_b1_normalizes_the_copied_label(db_session: Session):
    _seed_manufacturers(db_session)
    card = _card(db_session, "0X1234", manufacturer="Dell")

    run_backfill(db_session, apply=True)
    db_session.refresh(card)

    assert card.brand == "Dell Technologies"  # alias → canonical inside set_brand
    assert card.manufacturer == "Dell"  # untouched


def test_b1_trims_whitespace_before_the_oem_membership_test(db_session: Session):
    # lower(trim()) — a legacy value with stray whitespace ("IBM ") must not escape the
    # one-shot reclassification (mirrors the lower(trim(category)) idiom elsewhere).
    _seed_manufacturers(db_session)
    card = _card(db_session, "0Y5678", manufacturer="IBM ")

    stats = run_backfill(db_session, apply=True)
    db_session.refresh(card)

    assert stats["b1"]["scanned"] == 1
    assert card.brand == "IBM"  # set_brand strips before the alias lookup


def test_backfill_skips_soft_deleted_cards(db_session: Session):
    # Facet queries exclude deleted cards, so the backfill must neither write to them
    # nor count them in the operator's go/no-go tallies — across all three passes.
    from datetime import datetime, timezone

    _seed_manufacturers(db_session)
    card = _card(
        db_session,
        "DEL0001",
        manufacturer="IBM",
        description="HDD, 4TB 7.2K SAS, Seagate",
        deleted_at=datetime.now(timezone.utc),
    )
    _mfg_link(db_session, "del0001", "Seagate")

    stats = run_backfill(db_session, apply=True)
    db_session.refresh(card)

    assert stats["b1"]["scanned"] == 0
    assert stats["b2"]["links_scanned"] == 0
    assert stats["b3"]["matched"] == 0
    assert card.brand is None
    assert card.manufacturer == "IBM"  # untouched


def test_b2_reports_links_won_and_distinct_cards(db_session: Session):
    # 3 duplicate mfg_model rows for ONE card: links_won counts winning link rows,
    # manufacturers_set counts DISTINCT cards — the go/no-go report must not claim
    # 3 cards updated when 1 card was.
    _seed_manufacturers(db_session)
    card = _card(db_session, "DUP0001")
    _mfg_link(db_session, "dup0001", "Seagate", sheet="sheet_a")
    _mfg_link(db_session, "dup0001", "Seagate", sheet="sheet_b")
    _mfg_link(db_session, "dup0001", "Seagate", sheet="sheet_c")

    dry = run_backfill(db_session, apply=False)
    applied = run_backfill(db_session, apply=True)
    db_session.refresh(card)

    for stats in (dry, applied):
        assert stats["b2"]["links_scanned"] == 3
        assert stats["b2"]["links_won"] == 3  # ladder tie-break: each later dup wins
        assert stats["b2"]["manufacturers_set"] == 1  # ONE distinct card
    assert card.manufacturer == "Seagate Technology"


# --- B3 ------------------------------------------------------------------------


def test_b3_maker_trailing_token_routes_to_manufacturer(db_session: Session):
    _seed_manufacturers(db_session)
    card = _card(db_session, "XX9999", description="HDD, 4TB 7.2K SAS, Seagate")

    stats = run_backfill(db_session, apply=True)
    db_session.refresh(card)

    assert card.manufacturer == "Seagate Technology"
    assert card.manufacturer_source == "desc_parse"
    assert card.manufacturer_tier == 83
    assert card.brand is None
    assert stats["b3"]["manufacturers_set"] == 1


def test_b3_never_writes_outside_the_literal_lists(db_session: Session):
    _seed_manufacturers(db_session)
    card = _card(db_session, "YY8888", description="SSD, 100GB SFF SAS SSD, EMC")

    stats = run_backfill(db_session, apply=True)
    db_session.refresh(card)

    assert card.brand is None  # EMC is in NEITHER trailing list — never written
    assert card.manufacturer is None
    assert stats["b3"]["matched"] == 0


# --- Dry-run parity (the operator's go/no-go gate) -------------------------------


def _tally_keys(stats: dict) -> dict:
    return {p: stats[p] for p in ("b1", "b2", "b3")}


def test_dry_run_is_default_and_writes_nothing(db_session: Session):
    card = _st300_fixture(db_session)

    stats = run_backfill(db_session, apply=False)

    db_session.refresh(card)
    assert card.brand is None  # NOTHING written
    assert card.manufacturer is None
    assert card.brand_source is None
    assert card.manufacturer_source is None
    # ...but the simulated tallies show what --apply WOULD do, and the B4 gate
    # evaluates the simulated final state (so the dry run is a real go/no-go).
    assert stats["b2"]["manufacturers_set"] == 1
    assert stats["b3"]["brands_set"] == 1
    assert stats["gate_passed"] is True


def test_dry_run_tallies_equal_apply_tallies(db_session: Session):
    # Mixed fixture exercising all three passes + cross-pass ladder interaction
    # (B1's legacy-50 brand win is displaced by B3's desc_parse-83 on the same card).
    _seed_manufacturers(db_session)
    _card(db_session, "ST300MP0016", manufacturer="IBM", description='HDD, 300GB, 2.5" SED, 15K RPM, IBM')
    _mfg_link(db_session, "st300mp0016", "Seagate")
    _card(db_session, "XX9999", description="HDD, 4TB 7.2K SAS, Seagate")
    # A card whose manufacturer already carries HIGHER-tier provenance: B2 must lose
    # in BOTH modes (skipped, not set).
    _card(
        db_session,
        "ZZ7777",
        manufacturer="Kingston Technology",
        manufacturer_source="manual",
        manufacturer_confidence=1.0,
        manufacturer_tier=100,
    )
    _mfg_link(db_session, "zz7777", "Seagate")

    dry = run_backfill(db_session, apply=False)
    applied = run_backfill(db_session, apply=True)

    assert _tally_keys(dry) == _tally_keys(applied)
    assert dry["gate_passed"] is applied["gate_passed"] is True
    assert applied["b2"]["skipped"] == 1  # the manual-100 card resisted in both modes


def test_duplicate_mfg_model_rows_are_idempotent(db_session: Session):
    # 7-dup-row case from the spec: duplicates agree in practice; deterministic
    # fru_links.id order + the ladder tie-break keep the last — the final value is
    # stable and a SECOND full run converges (no value churn).
    _seed_manufacturers(db_session)
    card = _card(db_session, "SSDSC2BW180A3L", description="SSD, 180GB SATA, IBM")
    _mfg_link(db_session, "ssdsc2bw180a3l", "Seagate", sheet="sheet_a")
    _mfg_link(db_session, "ssdsc2bw180a3l", "Seagate", sheet="sheet_b")
    _mfg_link(db_session, "ssdsc2bw180a3l", "Seagate", sheet="sheet_c")

    run_backfill(db_session, apply=True)
    db_session.refresh(card)
    assert card.manufacturer == "Seagate Technology"
    first_state = (card.manufacturer, card.manufacturer_source, card.manufacturer_tier)

    run_backfill(db_session, apply=True)  # re-run: converged, same final state
    db_session.refresh(card)
    assert (card.manufacturer, card.manufacturer_source, card.manufacturer_tier) == first_state
    assert card.brand == "IBM"


# --- B4 gate + CLI exit code -----------------------------------------------------


def test_gate_fails_when_st300mp0016_absent(db_session: Session):
    _seed_manufacturers(db_session)
    stats = run_backfill(db_session, apply=True)
    assert stats["gate_passed"] is False


def test_gate_fails_when_maker_evidence_missing(db_session: Session):
    # Brand lands from the description, but with no fru_links maker row the card cannot
    # end manufacturer=Seagate Technology → the command must say NO.
    _seed_manufacturers(db_session)
    _card(db_session, "ST300MP0016", description='HDD, 300GB, 2.5" SED, 15K RPM, IBM')
    stats = run_backfill(db_session, apply=True)
    assert stats["gate_passed"] is False


def test_main_exit_codes(db_session: Session, monkeypatch):
    import app.database as dbmod

    class _SessionProxy:
        """Hand main() the test session but swallow its close() (conftest owns it)."""

        def __getattr__(self, name):
            if name == "close":
                return lambda: None
            return getattr(db_session, name)

    monkeypatch.setattr(dbmod, "SessionLocal", lambda: _SessionProxy())

    assert main([]) == 2  # empty DB → gate fails → non-zero

    _st300_fixture(db_session)
    assert main(["--apply"]) == 0  # gate passes after the apply
