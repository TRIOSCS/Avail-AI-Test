"""One-shot categorize-from-description channel (OPTIMIZATION_PLAN §2.4).

What: Categorizes UNCATEGORIZED material cards from their human descriptions using the
      shared lead-token grammar (desc_extractor.categorizer.categorize_from_desc), then
      immediately fills the freshly-categorized card's desc_parse facets — each becomes
      food for the existing extractor by definition (real descriptions). Two channels:
        * OWN-DESC: cards with a REAL description (alphanumeric-normalized description !=
          display_mpn, length >= 15) — categorized + faceted at desc_parse / tier 83.
        * FRU-DESC: cards still uncategorized AND with no usable own description, but whose
          linked fru_links row carries a usable description — categorized + faceted at
          fru_desc_parse / tier 82 (a one-hop FRU prose, ranks just below the own-desc
          channel, per spec_tiers.SOURCE_TIER).
      Every category write goes through the F1 ladder (spec_tiers.set_category) and ONLY
      when card.category IS NULL (the ladder + the IS NULL pre-filter both enforce
      fill-only — never reclassify). One MaterialCardAudit row per categorized card.
Usage: python -m app.management.categorize_from_desc [--apply] [--limit N]
      Dry-run by default: prints a yield report (cards that WOULD categorize, broken down
      by resulting category, per channel) and writes NOTHING. --apply commits.
Called by: admin manually (the orchestrator runs --apply during the deploy phase).
Depends on: desc_extractor.writer.categorize_and_record, desc_extractor.categorizer
      (dry-run yield), spec_tiers (tier constants), audit_service.log_audit,
      MaterialCard + FruLink, app.database.SessionLocal.
"""

import argparse
from collections import Counter

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.models.fru_link import FruLink
from app.services.audit_service import log_audit
from app.services.desc_extractor._common import DESC_CONFIDENCE, DESC_SOURCE
from app.services.desc_extractor.categorizer import categorize_from_desc
from app.services.desc_extractor.writer import categorize_and_record
from app.services.spec_tiers import SOURCE_TIER
from app.utils.normalization import normalize_mpn_key

# FRU-linked descriptions are a ONE-HOP prose (the FRU's row, not the card's own
# description), so they write at fru_desc_parse / tier 82 — strictly below the own-desc
# channel (desc_parse / 83), exactly the ranking spec_tiers.SOURCE_TIER assigns.
FRU_DESC_SOURCE = "fru_desc_parse"
FRU_DESC_CONFIDENCE = DESC_CONFIDENCE

# A "REAL" description (the spec's gate): its alphanumeric-normalized form differs from
# the alphanumeric-normalized display_mpn AND is at least this long. The ~39k cards whose
# description IS just the MPN carry zero extractable signal and are excluded.
MIN_REAL_DESC_LEN = 15

# Lower-cased, alphanumeric-only form — the shared dedup-key normalizer (so "00AR327" ==
# "00AR327 " == "00-AR-327" and a description that is just the MPN with punctuation/
# spacing noise is recognized as NOT a real description).
_alnum_norm = normalize_mpn_key


def _has_real_own_desc(card: MaterialCard) -> bool:
    """True iff *card*'s OWN description is a real description (not the MPN, len >=
    15)."""
    norm = _alnum_norm(card.description)
    return len(norm) >= MIN_REAL_DESC_LEN and norm != _alnum_norm(card.display_mpn)


def _fru_description_for(db: Session, card: MaterialCard) -> str | None:
    """A usable linked-FRU description for *card*, or None.

    The card's ``normalized_mpn`` matches a FRU edge as either the FRU or the related part;
    return the first non-empty FRU description that is itself "real" (len >= 15, != the
    card's MPN — a FRU row echoing the bare PN is no signal). Deterministic order (by id)
    so the run is reproducible.
    """
    mpn_norm = card.normalized_mpn
    rows = (
        db.execute(
            select(FruLink.description)
            .where(
                or_(FruLink.fru_norm == mpn_norm, FruLink.related_norm == mpn_norm),
                FruLink.description.isnot(None),
            )
            .order_by(FruLink.id)
            .limit(50)
        )
        .scalars()
        .all()
    )
    mpn_alnum = _alnum_norm(card.display_mpn)
    for desc in rows:
        norm = _alnum_norm(desc)
        if len(norm) >= MIN_REAL_DESC_LEN and norm != mpn_alnum:
            return desc
    return None


def _select_uncategorized(db: Session, limit: int) -> list[MaterialCard]:
    """Active, uncategorized cards — ordered by id for a reproducible run.

    The own-desc / fru-desc split is decided per card below (the own-desc gate needs the
    alphanumeric comparison, and the fru-desc lookup is a join), so this returns ALL
    uncategorized cards and the channel routing happens in ``run``.
    """
    query = (
        db.query(MaterialCard)
        .filter(MaterialCard.category.is_(None), MaterialCard.deleted_at.is_(None))
        .order_by(MaterialCard.id)
    )
    if limit:
        query = query.limit(limit)
    return query.all()


def run(db: Session, *, apply: bool = False, limit: int = 0) -> dict:
    """Categorize uncategorized cards from their (own or linked-FRU) descriptions.

    Dry-run (default) computes the same channel routing + grammar verdict apply-mode uses
    and writes NOTHING — the yield is what WOULD categorize. --apply runs
    categorize_and_record (own-desc at desc_parse/83, fru-desc at fru_desc_parse/82) inside
    its per-card SAVEPOINT, logs a MaterialCardAudit row per categorized card, and commits.

    Returns a tally dict: per-channel counts, per-resulting-category breakdown, specs
    written, and skip/fail counts.
    """
    cards = _select_uncategorized(db, limit)

    by_category: Counter = Counter()
    by_channel: Counter = Counter()
    totals: Counter = Counter()
    specs_written = 0

    for card in cards:
        try:
            if _has_real_own_desc(card):
                channel, source, confidence, description = "own_desc", DESC_SOURCE, DESC_CONFIDENCE, card.description
            else:
                fru_desc = _fru_description_for(db, card)
                if fru_desc is None:
                    totals["skipped_no_desc"] += 1
                    continue
                channel, source, confidence, description = "fru_desc", FRU_DESC_SOURCE, FRU_DESC_CONFIDENCE, fru_desc

            if apply:
                categorized, written = categorize_and_record(
                    db, card, description=description, source=source, confidence=confidence
                )
                if categorized:
                    log_audit(
                        db,
                        material_card_id=card.id,
                        action="categorized",
                        normalized_mpn=card.normalized_mpn,
                        details={
                            "category": card.category,
                            "source": source,
                            "tier": SOURCE_TIER[source],
                            "channel": channel,
                            "specs_written": written,
                        },
                        created_by="categorize_from_desc",
                    )
            else:
                # Read-only twin: the grammar verdict is the same set_category WOULD attempt
                # (existing category IS NULL, so the ladder always lets a canonical key win).
                commodity = categorize_from_desc(description)
                categorized, written = (commodity is not None), 0
                if categorized:
                    card.category = commodity  # transient only — db.rollback() in main()

            if categorized:
                by_category[card.category] += 1
                by_channel[channel] += 1
                totals["categorized"] += 1
                specs_written += written
            else:
                totals["no_grammar_match"] += 1
        except Exception:
            totals["failed"] += 1
            logger.exception("categorize-from-desc: failed on card_id={}", card.id)

    if apply:
        db.commit()

    summary = {
        "mode": "apply" if apply else "dry-run",
        "cards_examined": len(cards),
        "categorized": totals["categorized"],
        "specs_written": specs_written,
        "no_grammar_match": totals["no_grammar_match"],
        "skipped_no_desc": totals["skipped_no_desc"],
        "failed": totals["failed"],
        "by_channel": dict(by_channel),
        "by_category": dict(by_category.most_common()),
    }
    logger.info("categorize-from-desc [{}]: {}", summary["mode"], summary)
    return summary


def _print_report(summary: dict) -> None:
    """Human-readable yield report (dry-run) / outcome report (apply)."""
    logger.info("=" * 60)
    logger.info("categorize-from-desc [{}]", summary["mode"])
    logger.info("  cards examined .... {}", summary["cards_examined"])
    logger.info("  categorized ....... {}", summary["categorized"])
    logger.info("  specs written ..... {}", summary["specs_written"])
    logger.info("  no grammar match .. {}", summary["no_grammar_match"])
    logger.info("  skipped (no desc).. {}", summary["skipped_no_desc"])
    logger.info("  failed ............ {}", summary["failed"])
    logger.info("  by channel: {}", summary["by_channel"])
    logger.info("  by resulting category:")
    for category, count in summary["by_category"].items():
        logger.info("    {:<18} {}", category, count)
    logger.info("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Categorize uncategorized material cards from their descriptions (desc_parse/83 + fru_desc_parse/82)"
    )
    parser.add_argument("--apply", action="store_true", help="Write the categories + facets (default: dry-run)")
    parser.add_argument("--limit", type=int, default=0, help="Max uncategorized cards to examine (0 = all)")
    args = parser.parse_args()

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        if args.apply:
            logger.warning(
                "categorize-from-desc: --apply writes categories + facets for uncategorized cards "
                "(desc_parse/83 + fru_desc_parse/82) — ensure a recent db-backup exists "
                "(pg_dump runs every 6h; scripts/restore.sh to roll back)"
            )
        summary = run(db, apply=args.apply, limit=args.limit)
        if not args.apply:
            db.rollback()  # belt-and-braces: dry-run leaves no writes behind
        _print_report(summary)
    finally:
        db.close()


if __name__ == "__main__":
    main()
