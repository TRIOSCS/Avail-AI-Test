#!/usr/bin/env python3
"""One-time dedup of MaterialVendorHistory records.

Merges rows where the same material_card has multiple vendor history entries
that normalize to the same vendor name (e.g. "ARROW" vs "Arrow" vs "arrow").

For each group of duplicates:
- Keep the record with the highest times_seen
- Merge counts, keep earliest first_seen, latest last_seen
- Delete the duplicates

Run: PYTHONPATH=/root/availai python scripts/dedup_vendor_history.py [--dry-run]
"""

import os
import sys
from collections import defaultdict

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models import MaterialVendorHistory
from app.vendor_utils import normalize_vendor_name


def dedup_vendor_history(dry_run: bool = True):
    db = SessionLocal()
    try:
        # Group all MVH records by (material_card_id, normalized_vendor_name)
        all_mvh = db.query(MaterialVendorHistory).all()
        print(f"Total vendor history records: {len(all_mvh)}")

        groups = defaultdict(list)
        for mvh in all_mvh:
            key = (mvh.material_card_id, normalize_vendor_name(mvh.vendor_name))
            groups[key].append(mvh)

        # Find groups with duplicates
        dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
        print(f"Duplicate groups found: {len(dup_groups)}")

        if not dup_groups:
            print("No duplicates — nothing to do.")
            return

        merged = 0
        deleted = 0

        for (card_id, norm_name), records in dup_groups.items():
            # Sort: highest times_seen first (this is the "keeper")
            records.sort(key=lambda r: r.times_seen or 0, reverse=True)
            keeper = records[0]
            dupes = records[1:]

            print(
                f"  Card {card_id} vendor '{norm_name}': keeping id={keeper.id} "
                f"({keeper.vendor_name}, seen={keeper.times_seen}), "
                f"merging {len(dupes)} duplicate(s)"
            )

            for dupe in dupes:
                # Merge counts
                keeper.times_seen = (keeper.times_seen or 1) + (dupe.times_seen or 1)

                # Keep earliest first_seen
                if dupe.first_seen and (not keeper.first_seen or dupe.first_seen < keeper.first_seen):
                    keeper.first_seen = dupe.first_seen

                # Keep latest last_seen + associated "last_*" fields
                if dupe.last_seen and (not keeper.last_seen or dupe.last_seen > keeper.last_seen):
                    keeper.last_seen = dupe.last_seen
                    if dupe.last_qty is not None:
                        keeper.last_qty = dupe.last_qty
                    if dupe.last_price is not None:
                        keeper.last_price = dupe.last_price
                    if dupe.last_currency:
                        keeper.last_currency = dupe.last_currency
                    if dupe.last_manufacturer:
                        keeper.last_manufacturer = dupe.last_manufacturer
                    if dupe.vendor_sku:
                        keeper.vendor_sku = dupe.vendor_sku

                # Promote authorization
                if dupe.is_authorized:
                    keeper.is_authorized = True

                if not dry_run:
                    db.delete(dupe)
                deleted += 1

            # Normalize the keeper's vendor_name to the canonical form
            if keeper.vendor_name != norm_name and norm_name:
                keeper.vendor_name = norm_name

            merged += 1

        if dry_run:
            print(f"\nDRY RUN: would merge {merged} groups, delete {deleted} records")
            print("Re-run with --live to apply changes.")
        else:
            db.commit()
            print(f"\nDONE: merged {merged} groups, deleted {deleted} records")

    finally:
        db.close()


if __name__ == "__main__":
    live = "--live" in sys.argv
    if live:
        print("=== LIVE MODE — changes will be committed ===\n")
    else:
        print("=== DRY RUN MODE — no changes will be made ===\n")
    dedup_vendor_history(dry_run=not live)
