"""Tests for app/management/cleanup_known_bad.py — the stop-the-bleed trust hotfix.

Covers the three idempotent passes (documented-wrong facet deletes, non-canonical
category normalize-or-null, legacy manufacturer-provenance stamp) in BOTH dry-run
(default, writes nothing) and --apply mode, plus the per-card audit trail.

Off-vocab/legacy categories that pre-date the @validates("category") guard are seeded
through conftest.force_card_category (a Core UPDATE that bypasses the ORM guard), exactly
as a pre-guard writer would have left them in the live DB — the residue this command exists
to clean up.

Called by: pytest
Depends on: app/management/cleanup_known_bad.py, conftest (db_session, force_card_category),
            MaterialCard / MaterialSpecFacet / MaterialCardAudit.
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.management.cleanup_known_bad import (
    cleanup_junk_categories,
    delete_known_bad_facets,
    run,
    stamp_legacy_manufacturer_provenance,
)
from app.models import MaterialCard, MaterialCardAudit, MaterialSpecFacet
from app.services.spec_tiers import LEGACY_BACKFILL_SOURCE, LEGACY_BACKFILL_TIER
from tests.conftest import engine, force_card_category  # noqa: F401


def _card(db: Session, mpn: str, **kw) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn.upper(),
        created_at=datetime.now(timezone.utc),
        **kw,
    )
    db.add(card)
    db.flush()
    return card


def _facet(
    db: Session,
    card_id: int,
    *,
    value: float,
    source: str,
    category: str = "hdd",
    spec_key: str = "capacity_gb",
) -> MaterialSpecFacet:
    facet = MaterialSpecFacet(
        material_card_id=card_id,
        category=category,
        spec_key=spec_key,
        value_numeric=value,
        source=source,
    )
    db.add(facet)
    return facet


def _audits(db: Session, action: str) -> list[MaterialCardAudit]:
    return db.query(MaterialCardAudit).filter_by(action=action).all()


# ── Pass 1: documented-wrong facet deletes ──────────────────────────────


class TestDeleteKnownBadFacets:
    def _seed_bad_facets(self, db: Session) -> tuple[MaterialCard, MaterialCard]:
        # Card with the FRU-matrix capacity misdecode (373,455 GB) matched by key+value+source.
        fru = _card(db, "FRU-BAD", category="hdd")
        fru.specs_structured = {"capacity_gb": {"value": 373455.0, "source": "fru_matrix_decode"}}
        _facet(db, fru.id, value=373455.0, source="fru_matrix_decode")
        # The hdd capacity outlier (973,452 GB) matched by key+value+category.
        hdd = _card(db, "HDD-BAD", category="hdd")
        _facet(db, hdd.id, value=973452.0, source="mpn_decode")
        db.flush()
        return fru, hdd

    def test_dry_run_counts_but_writes_nothing(self, db_session: Session):
        self._seed_bad_facets(db_session)
        result = delete_known_bad_facets(db_session, apply=False)

        assert result["facets_deleted"] == 2
        # Nothing actually removed.
        assert db_session.query(MaterialSpecFacet).count() == 2
        assert _audits(db_session, "facet_cleanup") == []

    def test_apply_deletes_facets_drops_mirror_and_audits(self, db_session: Session):
        fru, _hdd = self._seed_bad_facets(db_session)
        result = delete_known_bad_facets(db_session, apply=True)
        db_session.flush()

        assert result["facets_deleted"] == 2
        assert result["mirrors_dropped"] == 1  # only the FRU card had a matching JSONB mirror
        assert db_session.query(MaterialSpecFacet).count() == 0
        # The FRU card's specs_structured mirror was popped (source matched the facet).
        db_session.refresh(fru)
        assert "capacity_gb" not in (fru.specs_structured or {})
        # One audit row per deleted facet that still had a card.
        assert len(_audits(db_session, "facet_cleanup")) == 2

    def test_mirror_kept_when_provenance_drifted(self, db_session: Session):
        # A JSONB mirror owned by a DIFFERENT source must not be dropped with the facet.
        fru = _card(db_session, "FRU-DRIFT", category="hdd")
        fru.specs_structured = {"capacity_gb": {"value": 373455.0, "source": "manual"}}
        _facet(db_session, fru.id, value=373455.0, source="fru_matrix_decode")
        db_session.flush()

        result = delete_known_bad_facets(db_session, apply=True)
        assert result["facets_deleted"] == 1
        assert result.get("mirrors_dropped", 0) == 0
        db_session.refresh(fru)
        assert "capacity_gb" in fru.specs_structured  # drift-owned mirror survives

    def test_correct_capacity_untouched(self, db_session: Session):
        ok = _card(db_session, "OK-CARD", category="hdd")
        _facet(db_session, ok.id, value=4000.0, source="mpn_decode")
        db_session.flush()

        result = delete_known_bad_facets(db_session, apply=True)
        assert result.get("facets_deleted", 0) == 0
        assert db_session.query(MaterialSpecFacet).count() == 1


# ── Pass 2: non-canonical category normalize-or-null ────────────────────


class TestCleanupJunkCategories:
    def test_unprovenanced_alias_normalized_through_ladder(self, db_session: Session):
        # "Hard Drives" → canonical "hdd" via the alias map; unprovenanced, so it routes
        # through set_category at legacy_backfill.
        card = _card(db_session, "ALIAS-1")
        force_card_category(db_session, card, "Hard Drives")
        db_session.flush()

        result = cleanup_junk_categories(db_session, apply=True)
        db_session.flush()
        db_session.refresh(card)
        assert card.category == "hdd"
        assert card.category_source == LEGACY_BACKFILL_SOURCE
        assert card.category_tier == LEGACY_BACKFILL_TIER
        assert result["normalized"] == 1
        assert len(_audits(db_session, "category_cleanup")) == 1

    def test_unprovenanced_unresolvable_nulled(self, db_session: Session):
        card = _card(db_session, "JUNK-1")
        force_card_category(db_session, card, "IGBT Modules")  # no canonical mapping
        db_session.flush()

        result = cleanup_junk_categories(db_session, apply=True)
        db_session.flush()
        db_session.refresh(card)
        assert card.category is None
        assert card.category_source is None
        assert result["nulled"] == 1
        assert len(_audits(db_session, "category_cleanup")) == 1

    def test_provenanced_noncanonical_normalized_in_place(self, db_session: Session):
        # A provenanced but mixed-case value: canonicalize in place, KEEP the source.
        card = _card(db_session, "INPLACE-1")
        card.category_source = "digikey_api"
        card.category_confidence = 0.9
        card.category_tier = 90
        db_session.flush()
        force_card_category(db_session, card, "Internal Hard Drives")  # alias → hdd
        # Stale facet keyed to the old category cell must be purged.
        _facet(db_session, card.id, category="Internal Hard Drives", value=4000.0, source="digikey_api")
        db_session.flush()

        result = cleanup_junk_categories(db_session, apply=True)
        db_session.flush()
        db_session.refresh(card)
        assert card.category == "hdd"
        assert card.category_source == "digikey_api"  # source preserved — value only changed
        assert result["normalized_in_place"] == 1
        assert result["stale_facets_purged"] == 1
        # the stale facet (category != "hdd") is gone
        assert db_session.query(MaterialSpecFacet).filter(MaterialSpecFacet.category != "hdd").count() == 0

    def test_canonical_categories_skipped(self, db_session: Session):
        _card(db_session, "CANON-1", category="dram")
        _card(db_session, "CANON-2", category="ssd")
        result = cleanup_junk_categories(db_session, apply=True)
        assert result["cards"] == 0  # nothing non-canonical selected

    def test_dry_run_writes_nothing(self, db_session: Session):
        card = _card(db_session, "DRY-1")
        force_card_category(db_session, card, "Hard Drives")
        db_session.flush()

        result = cleanup_junk_categories(db_session, apply=False)
        db_session.expire(card, ["category"])
        assert card.category == "Hard Drives"  # untouched
        assert result["normalized"] == 1  # but tallied as if applied
        assert _audits(db_session, "category_cleanup") == []


# ── Pass 3: legacy manufacturer-provenance stamp ────────────────────────


class TestStampLegacyManufacturerProvenance:
    def test_apply_stamps_only_unprovenanced(self, db_session: Session):
        unprov = _card(db_session, "MFR-1", manufacturer="Samsung")  # NULL provenance
        prov = _card(db_session, "MFR-2", manufacturer="Micron")
        prov.manufacturer_source = "manual"
        prov.manufacturer_tier = 100
        _card(db_session, "MFR-3")  # no manufacturer at all
        db_session.flush()

        result = stamp_legacy_manufacturer_provenance(db_session, apply=True)
        assert result["manufacturers_stamped"] == 1
        db_session.refresh(unprov)
        db_session.refresh(prov)
        assert unprov.manufacturer_source == LEGACY_BACKFILL_SOURCE
        assert unprov.manufacturer_tier == LEGACY_BACKFILL_TIER
        assert unprov.manufacturer_updated_at is None  # true write time unknown
        assert prov.manufacturer_source == "manual"  # already provenanced — untouched

    def test_dry_run_writes_nothing(self, db_session: Session):
        unprov = _card(db_session, "MFR-DRY", manufacturer="Samsung")
        db_session.flush()
        result = stamp_legacy_manufacturer_provenance(db_session, apply=False)
        assert result["manufacturers_stamped"] == 1
        db_session.refresh(unprov)
        assert unprov.manufacturer_source is None  # untouched


# ── run() orchestration + idempotence ───────────────────────────────────


class TestRun:
    def _seed_all(self, db: Session) -> None:
        bad = _card(db, "RUN-FACET", category="hdd")
        _facet(db, bad.id, value=973452.0, source="mpn_decode")
        alias = _card(db, "RUN-ALIAS")
        force_card_category(db, alias, "Hard Drives")
        _card(db, "RUN-MFR", manufacturer="Samsung")
        db.flush()

    def test_run_dry_run_mode_label(self, db_session: Session):
        self._seed_all(db_session)
        summary = run(db_session, apply=False)
        assert summary["mode"] == "dry-run"
        assert summary["facets"]["facets_deleted"] == 1
        assert summary["categories"]["normalized"] == 1
        assert summary["manufacturers"]["manufacturers_stamped"] == 1

    def test_run_apply_is_idempotent(self, db_session: Session):
        self._seed_all(db_session)
        first = run(db_session, apply=True)
        db_session.flush()
        assert first["mode"] == "apply"

        second = run(db_session, apply=True)
        assert second["facets"].get("facets_deleted", 0) == 0
        assert second["categories"]["cards"] == 0
        assert second["manufacturers"]["manufacturers_stamped"] == 0


def test_main_dry_run_rolls_back(db_session: Session, monkeypatch):
    """Main() without --apply must roll back (belt-and-braces: dry-run leaves no
    writes)."""
    import sys

    import app.database
    from app.management import cleanup_known_bad

    card = _card(db_session, "MAIN-DRY")
    force_card_category(db_session, card, "Hard Drives")
    db_session.flush()

    committed = {"value": False}
    rolled_back = {"value": False}
    monkeypatch.setattr(db_session, "commit", lambda: committed.__setitem__("value", True))
    monkeypatch.setattr(db_session, "rollback", lambda: rolled_back.__setitem__("value", True))
    monkeypatch.setattr(db_session, "close", lambda: None)
    # main() does `from app.database import SessionLocal` — patch at the source module.
    monkeypatch.setattr(app.database, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(sys, "argv", ["cleanup_known_bad"])

    cleanup_known_bad.main()
    assert rolled_back["value"] is True
    assert committed["value"] is False
