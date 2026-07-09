"""Stop-the-bleed trust hotfix — one-shot cleanup of documented-bad catalog data.

What: OPTIMIZATION_PLAN_2026_06_12 §2 item 1.1. Three idempotent passes, dry-run by
      default (``--apply`` writes):
        1. Delete the two documented-wrong facet rows, matched by CONTENT (never by
           row id — ids differ across environments): the fru_matrix_decode
           capacity_gb=373,455 row (card 413156 in the 2026-06-10 audit; true value
           36.4 GB) and the hdd capacity_gb=973,452 outlier. The matching
           specs_structured mirror entry is dropped when its provenance agrees.
        2. Normalize-or-null every non-canonical material_cards.category (the
           pre-#267 bypass-writer residue: 61 NULL-provenance junk strings like
           "IGBT Modules", plus ~139 provenanced cards on ~89 non-canonical
           strings). Resolvable values (CATEGORY_ALIASES / case-trim) are written
           through set_category at legacy_backfill when unprovenanced, or
           normalized IN PLACE preserving the existing source when provenanced;
           unresolvable values are nulled with provenance cleared. One
           MaterialCardAudit row per changed card (action=category_cleanup).
        3. Stamp ``manufacturer_source='legacy_backfill'`` (conf 0.5, tier 50) on
           every card with a manufacturer but NULL maker provenance — attribution
           of EXISTING data, deliberately NOT a ladder write (no new evidence);
           ``manufacturer_updated_at`` stays NULL (true write time unknown), which
           ranks identically to the runtime NULL-provenance floor.
Usage: python -m app.management.cleanup_known_bad [--apply]
Called by: admin manually at deploy time (after the PR carrying it ships).
Depends on: MaterialCard / MaterialSpecFacet / MaterialCardAudit,
      category_normalizer.normalize_category, commodity_registry
      CANONICAL_COMMODITY_KEYS, spec_tiers.set_category (+ the legacy_backfill
      constants), audit_service.log_audit.
"""

import argparse
from collections import Counter

from loguru import logger
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.models import MaterialCard, MaterialSpecFacet
from app.services.audit_service import log_audit
from app.services.category_normalizer import normalize_category
from app.services.commodity_registry import CANONICAL_COMMODITY_KEYS
from app.services.spec_tiers import (
    LEGACY_BACKFILL_CONFIDENCE,
    LEGACY_BACKFILL_SOURCE,
    LEGACY_BACKFILL_TIER,
    set_category,
)

CREATED_BY = "cleanup_known_bad"

# Documented-wrong facet rows (2026-06-10/12 audits), matched by content criteria.
# Each entry: (spec_key, value_numeric, source-or-None, category-or-None).
KNOWN_BAD_FACETS: tuple[tuple[str, float, str | None, str | None], ...] = (
    # Card 413156: FRU-matrix capacity misdecode — 373,455 GB stored, true 36.4 GB.
    ("capacity_gb", 373455.0, "fru_matrix_decode", None),
    # The hdd capacity outlier — 973,452 GB (no shipped drive exists at that point);
    # matched by value+key (+the hdd category cell), source left open by design.
    ("capacity_gb", 973452.0, None, "hdd"),
)


def delete_known_bad_facets(db: Session, *, apply: bool = False) -> dict:
    """Pass 1 — delete the documented-wrong facet rows (and their JSONB mirrors)."""
    tally: Counter = Counter()
    for spec_key, value_numeric, source, category in KNOWN_BAD_FACETS:
        q = db.query(MaterialSpecFacet).filter(
            MaterialSpecFacet.spec_key == spec_key,
            MaterialSpecFacet.value_numeric == value_numeric,
        )
        if source is not None:
            q = q.filter(MaterialSpecFacet.source == source)
        if category is not None:
            q = q.filter(MaterialSpecFacet.category == category)
        for facet in q.all():
            card = db.get(MaterialCard, facet.material_card_id)
            logger.info(
                "cleanup-known-bad: documented-wrong facet card={} {}={} (source={}, category={}) — {}",
                facet.material_card_id,
                facet.spec_key,
                facet.value_numeric,
                facet.source,
                facet.category,
                "deleting" if apply else "would delete",
            )
            if apply:
                if card is not None:
                    specs = dict(card.specs_structured or {})
                    entry = specs.get(facet.spec_key)
                    # The mirror is popped only when it mirrors THIS facet's source —
                    # a mismatch means drift (some other source owns the JSONB entry).
                    if entry is not None and entry.get("source") == facet.source:
                        specs.pop(facet.spec_key, None)
                        card.specs_structured = specs  # type: ignore[assignment, unused-ignore]  # legacy Column-model ORM noise
                        tally["mirrors_dropped"] += 1
                    log_audit(
                        db,
                        material_card_id=card.id,  # type: ignore[arg-type, unused-ignore]  # legacy Column-model ORM noise
                        action="facet_cleanup",
                        normalized_mpn=card.normalized_mpn,  # type: ignore[arg-type, unused-ignore]  # legacy Column-model ORM noise
                        details={
                            "spec_key": facet.spec_key,
                            "value_numeric": facet.value_numeric,
                            "source": facet.source,
                            "reason": "documented_wrong_2026_06_12",
                        },
                        created_by=CREATED_BY,
                    )
                db.delete(facet)
            tally["facets_deleted"] += 1
    return dict(tally)


def cleanup_junk_categories(db: Session, *, apply: bool = False) -> dict:
    """Pass 2 — normalize-or-null every non-canonical category, audit per card."""
    tally: Counter = Counter()
    transitions: Counter = Counter()
    cards = (
        db.query(MaterialCard)
        .filter(
            MaterialCard.category.isnot(None),
            MaterialCard.category.notin_(CANONICAL_COMMODITY_KEYS),
            MaterialCard.deleted_at.is_(None),
        )
        .order_by(MaterialCard.id)
        .all()
    )

    def _null_category(card: MaterialCard) -> None:
        """Clear the category cell and all four provenance columns (junk that resolves
        nowhere — the value the vocabulary rejects, and any attestation of it)."""
        card.category = None
        card.category_source = None
        card.category_confidence = None
        card.category_tier = None
        card.category_updated_at = None

    for card in cards:
        raw = card.category
        source_before = card.category_source
        target = normalize_category(raw)  # type: ignore[arg-type, unused-ignore]  # legacy Column-model ORM noise
        if source_before is None:
            if target is not None:
                # Unprovenanced junk that resolves to a canonical key: through the
                # ladder at legacy_backfill (true origin unknown). The existing
                # NULL-provenance value ranks at the same (50, 0.5) floor with an
                # empty timestamp, so the re-write always wins the tie-break.
                wrote = set_category(card, target, LEGACY_BACKFILL_SOURCE, LEGACY_BACKFILL_CONFIDENCE, write=apply)
                mode = "normalized" if wrote else "skipped_ladder"
            else:
                mode = "nulled"
                if apply:
                    _null_category(card)
        else:
            if target is not None:
                # Provenanced but non-canonical (e.g. a pre-alias-map vendor string):
                # canonicalize the VALUE in place, preserving the original source/
                # confidence/tier/updated_at — the evidence didn't change, only its
                # spelling. Defensive mirror of set_category's stale-facet purge:
                # facet rows keyed to the old category cell can no longer match.
                mode = "normalized_in_place"
                if apply:
                    stale = (
                        db.query(MaterialSpecFacet)
                        .filter(
                            MaterialSpecFacet.material_card_id == card.id,
                            MaterialSpecFacet.category != target,
                        )
                        .all()
                    )
                    for facet in stale:
                        db.delete(facet)
                        tally["stale_facets_purged"] += 1
                    card.category = target  # type: ignore[assignment, unused-ignore]  # legacy Column-model ORM noise
            else:
                # Provenanced junk that resolves nowhere: the provenance attests to a
                # write of a value the vocabulary rejects — clear both.
                mode = "nulled"
                if apply:
                    _null_category(card)
        tally[mode] += 1
        transitions[f"{raw!r} -> {target if mode.startswith('normalized') else None}"] += 1
        if apply and mode != "skipped_ladder":
            log_audit(
                db,
                material_card_id=card.id,  # type: ignore[arg-type, unused-ignore]  # legacy Column-model ORM noise
                action="category_cleanup",
                normalized_mpn=card.normalized_mpn,  # type: ignore[arg-type, unused-ignore]  # legacy Column-model ORM noise
                details={
                    "from": raw,
                    "to": target if mode.startswith("normalized") else None,
                    "source_before": source_before,
                    "source_after": card.category_source,
                    "mode": mode,
                },
                created_by=CREATED_BY,
            )
    for transition, n in transitions.most_common():
        logger.info("cleanup-known-bad: category {:>5}x {}", n, transition)
    result = dict(tally)
    result["cards"] = len(cards)
    return result


def stamp_legacy_manufacturer_provenance(db: Session, *, apply: bool = False) -> dict:
    """Pass 3 — stamp legacy_backfill provenance on unprovenanced manufacturers.

    In-place attribution of existing data (NOT a ladder write — there is no new
    evidence). A single bulk UPDATE; ``manufacturer_updated_at`` stays NULL so the
    stamped rows keep ranking exactly like the runtime NULL-provenance floor
    ((50, 0.5, "") in spec_tiers._set_provenanced_column).
    """
    criteria = (
        MaterialCard.manufacturer.isnot(None),
        func.trim(MaterialCard.manufacturer) != "",
        MaterialCard.manufacturer_source.is_(None),
    )
    count = db.query(func.count(MaterialCard.id)).filter(*criteria).scalar() or 0
    logger.info(
        "cleanup-known-bad: {} cards carry a manufacturer with NULL provenance — {} {}/conf {}/tier {}",
        count,
        "stamping" if apply else "would stamp",
        LEGACY_BACKFILL_SOURCE,
        LEGACY_BACKFILL_CONFIDENCE,
        LEGACY_BACKFILL_TIER,
    )
    if apply and count:
        db.execute(
            update(MaterialCard)
            .where(*criteria)
            .values(
                manufacturer_source=LEGACY_BACKFILL_SOURCE,
                manufacturer_confidence=LEGACY_BACKFILL_CONFIDENCE,
                manufacturer_tier=LEGACY_BACKFILL_TIER,
            )
            .execution_options(synchronize_session=False)
        )
    return {"manufacturers_stamped": count}


def run(db: Session, *, apply: bool = False) -> dict:
    """Run all three passes; returns the combined tally (never commits)."""
    summary = {
        "mode": "apply" if apply else "dry-run",
        "facets": delete_known_bad_facets(db, apply=apply),
        "categories": cleanup_junk_categories(db, apply=apply),
        "manufacturers": stamp_legacy_manufacturer_provenance(db, apply=apply),
    }
    logger.info("cleanup-known-bad [{}]: {}", summary["mode"], summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Stop-the-bleed trust hotfix (plan 1.1) — dry-run by default")
    parser.add_argument("--apply", action="store_true", help="Write the deletes/normalizations/stamps")
    args = parser.parse_args()

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        if args.apply:
            logger.warning(
                "cleanup-known-bad: --apply will DELETE documented-wrong facet rows, rewrite/null "
                "non-canonical categories, and stamp manufacturer provenance — ensure a recent "
                "db-backup exists (pg_dump runs every 6h; scripts/restore.sh to roll back)"
            )
        run(db, apply=args.apply)
        if args.apply:
            db.commit()
        else:
            db.rollback()  # belt-and-braces: dry-run leaves no writes behind
    finally:
        db.close()


if __name__ == "__main__":
    main()
