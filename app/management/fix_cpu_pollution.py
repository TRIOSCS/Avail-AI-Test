"""One-shot bulk re-classifier for the polluted `cpu` catch-all bucket.

What: scans material_cards WHERE category='cpu', and for each MPN that
    classify_polluted_mpn definitively maps to a non-CPU commodity, re-categorizes it via
    set_category(source="cpu_pollution_fix") (tier 96 — beats the trio_source 'cpu' default).
    Dry-run by default; --apply commits. Reversible (the cpu_pollution_fix provenance is
    queryable). Scopes ONLY to category='cpu' — no other bucket is touched.
Called by: operators (python -m app.management.fix_cpu_pollution [--apply] [--limit N]).
Depends on: cpu_pollution.classifier, spec_tiers.set_category.
"""

from __future__ import annotations

import argparse

from loguru import logger
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import MaterialCard
from app.services.cpu_pollution.classifier import classify_polluted_mpn
from app.services.spec_tiers import set_category

_SOURCE = "cpu_pollution_fix"
_CONFIDENCE = 0.97


def reclassify_cpu_pollution(db: Session, *, apply: bool, limit: int | None = None) -> dict:
    """Re-classify definitively-non-CPU cards out of the `cpu` bucket.

    Returns summary.
    """
    q = db.query(MaterialCard).filter(MaterialCard.category == "cpu", MaterialCard.deleted_at.is_(None))
    if limit:
        q = q.limit(limit)
    scanned = reclassified = 0
    by_commodity: dict[str, int] = {}
    for card in q.yield_per(500):
        scanned += 1
        commodity = classify_polluted_mpn(card.display_mpn)
        if not commodity:
            continue
        if apply:
            try:
                with db.begin_nested():
                    set_category(card, commodity, _SOURCE, _CONFIDENCE)
            except Exception:
                logger.exception("cpu-pollution: failed on card_id={}", card.id)
                continue
        reclassified += 1
        by_commodity[commodity] = by_commodity.get(commodity, 0) + 1
    if apply:
        db.commit()
    logger.info(
        "cpu-pollution: scanned={} reclassified={} by_commodity={} (apply={})",
        scanned,
        reclassified,
        by_commodity,
        apply,
    )
    return {"scanned": scanned, "reclassified": reclassified, "by_commodity": by_commodity}


def main() -> None:
    ap = argparse.ArgumentParser(description="Re-classify pollution out of the cpu bucket.")
    ap.add_argument("--apply", action="store_true", help="commit changes (default: dry-run)")
    ap.add_argument("--limit", type=int, default=None, help="cap cards scanned")
    args = ap.parse_args()
    db = SessionLocal()
    try:
        reclassify_cpu_pollution(db, apply=args.apply, limit=args.limit)
    finally:
        db.close()


if __name__ == "__main__":
    main()
