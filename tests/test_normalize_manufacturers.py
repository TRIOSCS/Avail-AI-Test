"""tests/test_normalize_manufacturers.py — one-shot brand/maker canonicalization CLI.

Covers: app/management/normalize_manufacturers.py — garbage values ("F)", "F") NULLed
with provenance cleared; alias variants (HP → HPE, DELL → Dell Technologies) rewritten
with provenance PRESERVED byte-identical (same-evidence canonicalization — the bypass
of set_manufacturer is the documented contract); verbatim misses untouched; dry-run
mutates nothing; soft-deleted cards included; per-value tallies.

Called by: pytest
Depends on: conftest.py (db_session), app.models.Manufacturer/MaterialCard
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.management.normalize_manufacturers import run
from app.models import Manufacturer, MaterialCard

_TS = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _seed_manufacturers(db: Session) -> None:
    db.add(Manufacturer(canonical_name="HPE", aliases=["Hewlett Packard Enterprise", "HP", "Hewlett Packard"]))
    db.add(Manufacturer(canonical_name="Dell Technologies", aliases=["Dell"]))
    db.add(Manufacturer(canonical_name="Texas Instruments", aliases=["TI", "Texas Instruments (TI)"]))
    db.flush()


def _card(db: Session, mpn: str, **kw) -> MaterialCard:
    card = MaterialCard(normalized_mpn=mpn.lower(), display_mpn=mpn, **kw)
    db.add(card)
    db.flush()
    return card


def test_dry_run_mutates_nothing(db_session: Session):
    _seed_manufacturers(db_session)
    garbage = _card(db_session, "TLP781A", manufacturer="F)")
    alias = _card(db_session, "P12345-001", manufacturer="HP", manufacturer_source="trio_source")

    plans = run(db_session, apply=False)

    db_session.expire_all()
    assert garbage.manufacturer == "F)"  # untouched
    assert alias.manufacturer == "HP"
    # ... but the report already carries the exact would-be changes.
    assert plans["manufacturer"].garbage == {"F)": 1}
    assert plans["manufacturer"].renames == {("HP", "HPE"): 1}


def test_apply_nulls_garbage_and_clears_provenance(db_session: Session):
    _seed_manufacturers(db_session)
    # Live garbage is unprovenanced, but a provenanced junk row must ALSO end clean.
    provenanced = _card(
        db_session,
        "TLP781B",
        manufacturer="F)",
        manufacturer_source="legacy_backfill",
        manufacturer_confidence=0.5,
        manufacturer_tier=50,
        manufacturer_updated_at=_TS,
    )
    bare = _card(db_session, "RN1302", manufacturer="F")

    plans = run(db_session, apply=True)

    db_session.expire_all()
    for card in (provenanced, bare):
        assert card.manufacturer is None
        assert card.manufacturer_source is None
        assert card.manufacturer_confidence is None
        assert card.manufacturer_tier is None
        assert card.manufacturer_updated_at is None
    assert plans["manufacturer"].garbage == {"F)": 1, "F": 1}


def test_apply_renames_alias_and_preserves_provenance(db_session: Session):
    _seed_manufacturers(db_session)
    card = _card(
        db_session,
        "P23456-001",
        manufacturer="HP",
        manufacturer_source="trio_source",
        manufacturer_confidence=0.9,
        manufacturer_tier=95,
        manufacturer_updated_at=_TS,
    )

    run(db_session, apply=True)

    db_session.expire_all()
    assert card.manufacturer == "HPE"
    # Same-evidence canonicalization: provenance byte-identical — NOT re-stamped.
    assert card.manufacturer_source == "trio_source"
    assert card.manufacturer_confidence == 0.9
    assert card.manufacturer_tier == 95
    assert card.manufacturer_updated_at == _TS


def test_apply_renames_brand_column_independently(db_session: Session):
    _seed_manufacturers(db_session)
    card = _card(
        db_session,
        "00AR327",
        brand="Hewlett Packard Enterprise",
        brand_source="trio_source",
        brand_confidence=0.9,
        brand_tier=95,
        brand_updated_at=_TS,
        manufacturer="DELL",  # maker column normalizes on its own track
    )

    plans = run(db_session, apply=True)

    db_session.expire_all()
    assert card.brand == "HPE"
    assert card.brand_source == "trio_source"
    assert card.brand_updated_at == _TS
    assert card.manufacturer == "Dell Technologies"
    assert plans["brand"].renames == {("Hewlett Packard Enterprise", "HPE"): 1}
    assert plans["manufacturer"].renames == {("DELL", "Dell Technologies"): 1}


def test_verbatim_miss_and_canonical_values_stay_untouched(db_session: Session):
    _seed_manufacturers(db_session)
    composite = _card(db_session, "HUS156045VLS600", manufacturer="HITACHI/IBM")
    canonical = _card(db_session, "P34567-001", manufacturer="HPE")

    plans = run(db_session, apply=True)

    db_session.expire_all()
    assert composite.manufacturer == "HITACHI/IBM"  # miss → verbatim, never invented
    assert canonical.manufacturer == "HPE"
    assert plans["manufacturer"].renames == {}
    assert plans["manufacturer"].unchanged_values == 2
    assert plans["manufacturer"].unchanged_cards == 2


def test_soft_deleted_cards_are_cleaned_too(db_session: Session):
    # Representation fix (migration 100 contract): restoring a card must surface a
    # canonical value, so deleted_at is NOT filtered.
    _seed_manufacturers(db_session)
    card = _card(db_session, "TLP781C", manufacturer="F)", deleted_at=datetime.now(UTC))

    run(db_session, apply=True)

    db_session.expire_all()
    assert card.manufacturer is None


def test_tallies_count_cards_per_value(db_session: Session):
    _seed_manufacturers(db_session)
    for i in range(3):
        _card(db_session, f"TLP78{i}D", manufacturer="F)")
    for i in range(2):
        _card(db_session, f"P4567{i}-001", manufacturer="HP")

    plans = run(db_session, apply=False)

    assert plans["manufacturer"].garbage == {"F)": 3}
    assert plans["manufacturer"].renames == {("HP", "HPE"): 2}
    assert plans["manufacturer"].garbage_cards == 3
    assert plans["manufacturer"].renamed_cards == 2
