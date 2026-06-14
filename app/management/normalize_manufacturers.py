"""One-shot brand/manufacturer canonicalization backfill.

Usage: python -m app.management.normalize_manufacturers [--apply]

DRY-RUN by DEFAULT: prints the exact per-value tallies --apply would write (both modes
classify from the same distinct-value scan, so the dry-run report cannot drift from
--apply). Two classes, applied to EVERY non-null ``manufacturer`` and ``brand`` value
on material_cards (soft-deleted cards INCLUDED — restoring a card must surface a
canonical value, same contract as migration 100's category normalization):

  1. GARBAGE → NULL + provenance cleared. Values failing
     ``is_garbage_brand_value`` (manufacturer_normalizer.py: <2 chars stripped, or
     unbalanced parentheses) are comma-split fragments of parenthesized MPN packing
     suffixes — the "(TP,F)" ingest leak ("F)", "F", "LF(T", "TSOP)", …) plus the
     empty-string residue. They are evidence of NOTHING, so the value AND its four
     provenance columns (``<attr>_source/_confidence/_tier/_updated_at``) are nulled —
     a later real write starts from a clean slate instead of fighting a junk value at
     the legacy floor.
  2. ALIAS → canonical, provenance PRESERVED. Every other value is re-run through
     ``normalize_brand_name`` (manufacturers-table aliases, e.g. HP → HPE,
     DELL → Dell Technologies); when the canonical differs, ONLY the value cell is
     rewritten.

WHY THIS BYPASSES set_manufacturer/set_brand (and why that is correct HERE ONLY):
this command corrects the REPRESENTATION of evidence that already won the F1 ladder —
it does not introduce new evidence. "HP" written by trio_source at tier 95 is still the
same tier-95 trio_source observation once spelled "HPE"; re-stamping it through
set_manufacturer would forge a new source/confidence/timestamp for an observation that
never re-occurred (and the ladder would block most rewrites anyway — same-tier ties
keep the incumbent). So each card's existing ``<attr>_source/_confidence/_tier/
_updated_at`` are deliberately left byte-identical on canonicalization. This is the
same contract as migrations 093/100 ("the value's SOURCE did not change, only its
spelling was canonicalized"). Any writer introducing NEW brand/maker evidence MUST
still go through set_brand/set_manufacturer — never copy this pattern for that.

Called by: an operator (manually, post-deploy of migration 104 — NOT at startup).
Depends on: app.database.SessionLocal; app.services.manufacturer_normalizer
      (normalize_brand_name + is_garbage_brand_value); the material_cards table.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.manufacturer_normalizer import is_garbage_brand_value, normalize_brand_name

_COLUMNS = ("manufacturer", "brand")


@dataclass
class ColumnPlan:
    """Classification of one column's distinct values: what --apply will rewrite."""

    column: str
    garbage: dict[str, int] = field(default_factory=dict)  # value -> card count (NULL + clear provenance)
    renames: dict[tuple[str, str], int] = field(default_factory=dict)  # (old, new) -> card count
    unchanged_values: int = 0
    unchanged_cards: int = 0

    @property
    def garbage_cards(self) -> int:
        return sum(self.garbage.values())

    @property
    def renamed_cards(self) -> int:
        return sum(self.renames.values())


def _classify(db: Session, column: str) -> ColumnPlan:
    """Scan a column's distinct values and classify each: garbage / rename / unchanged.

    Both modes run THIS — apply executes the resulting per-value UPDATEs, dry-run only
    prints them — so the dry-run tallies cannot drift from --apply.
    """
    plan = ColumnPlan(column=column)
    rows = db.execute(
        text(f"SELECT {column} AS val, COUNT(*) AS n FROM material_cards WHERE {column} IS NOT NULL GROUP BY {column}")
    ).all()
    for raw, count in rows:
        if is_garbage_brand_value(raw):
            plan.garbage[raw] = count
            continue
        canonical = normalize_brand_name(db, raw)
        if canonical != raw:
            plan.renames[(raw, canonical)] = count
        else:
            plan.unchanged_values += 1
            plan.unchanged_cards += count
    return plan


def _apply(db: Session, plan: ColumnPlan) -> None:
    """Execute the per-value UPDATEs for one column (value-keyed, exact string match)."""
    col = plan.column
    for value in plan.garbage:
        db.execute(
            text(
                f"UPDATE material_cards SET {col} = NULL, {col}_source = NULL, "
                f"{col}_confidence = NULL, {col}_tier = NULL, {col}_updated_at = NULL "
                f"WHERE {col} = :val"
            ),
            {"val": value},
        )
    for old, new in plan.renames:
        # Value cell ONLY — provenance columns stay byte-identical (see module header).
        db.execute(text(f"UPDATE material_cards SET {col} = :new WHERE {col} = :old"), {"new": new, "old": old})


def _report(plan: ColumnPlan) -> None:
    logger.info(
        "{}: {} garbage value(s) / {} card(s) -> NULL; {} rename(s) / {} card(s); {} value(s) / {} card(s) already canonical",
        plan.column,
        len(plan.garbage),
        plan.garbage_cards,
        len(plan.renames),
        plan.renamed_cards,
        plan.unchanged_values,
        plan.unchanged_cards,
    )
    for value, count in sorted(plan.garbage.items(), key=lambda kv: -kv[1]):
        logger.info("  {} GARBAGE {!r} -> NULL (provenance cleared): {} card(s)", plan.column, value, count)
    for (old, new), count in sorted(plan.renames.items(), key=lambda kv: -kv[1]):
        logger.info("  {} RENAME {!r} -> {!r} (provenance preserved): {} card(s)", plan.column, old, new, count)


def run(db: Session, apply: bool) -> dict[str, ColumnPlan]:
    """Classify both columns, report, and (with apply=True) execute + commit."""
    plans = {column: _classify(db, column) for column in _COLUMNS}
    for plan in plans.values():
        _report(plan)
    total_garbage = sum(p.garbage_cards for p in plans.values())
    total_renamed = sum(p.renamed_cards for p in plans.values())
    if not apply:
        logger.info(
            "DRY RUN — nothing written ({} card value(s) would be NULLed, {} renamed). Re-run with --apply.",
            total_garbage,
            total_renamed,
        )
        return plans
    for plan in plans.values():
        _apply(db, plan)
    db.commit()
    logger.info("APPLIED: {} card value(s) NULLed, {} renamed (provenance preserved)", total_garbage, total_renamed)
    return plans


def main() -> int:
    parser = argparse.ArgumentParser(description="Canonicalize material_cards.manufacturer/brand (dry-run default)")
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry-run report only)")
    args = parser.parse_args()

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        run(db, apply=args.apply)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
