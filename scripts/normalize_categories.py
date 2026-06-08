#!/usr/bin/env python3
"""Backfill material_cards.category to canonical commodity keys.

Maps free-text variant strings (e.g. "connectors, interconnects" -> "connectors") via
app.services.category_normalizer so the faceted sidebar buckets every card. Idempotent;
unmapped/ambiguous strings are left untouched.

Usage:
    python scripts/normalize_categories.py --dry-run   # report from->to counts, no writes
    python scripts/normalize_categories.py --apply      # apply the updates
"""

import argparse
import sys
from collections import Counter

from app.database import SessionLocal
from app.models import MaterialCard
from app.services.category_normalizer import normalize_category


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize material_cards.category to canonical commodity keys")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="report changes without writing")
    mode.add_argument("--apply", action="store_true", help="apply the category updates")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        rows = (
            db.query(MaterialCard.id, MaterialCard.category)
            .filter(MaterialCard.category.isnot(None), MaterialCard.deleted_at.is_(None))
            .all()
        )
        changes: dict[int, str] = {}
        transitions: Counter = Counter()
        for card_id, category in rows:
            target = normalize_category(category)
            current = (category or "").strip().lower()
            if target is None or target == current:
                continue
            changes[card_id] = target
            transitions[f"{current!r} -> {target}"] += 1

        print(f"Scanned {len(rows)} categorized cards; {len(changes)} would change:")
        for transition, n in transitions.most_common():
            print(f"  {n:>5}  {transition}")

        if args.dry_run or not changes:
            print("Dry run — no changes written." if args.dry_run else "Nothing to change.")
            return 0

        for card_id, target in changes.items():
            db.query(MaterialCard).filter(MaterialCard.id == card_id).update({"category": target})
        db.commit()
        print(f"Applied {len(changes)} category updates.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
