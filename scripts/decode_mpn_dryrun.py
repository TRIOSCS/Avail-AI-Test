#!/usr/bin/env python3
"""Dry-run the MPN decoders over material_cards — report coverage, write NOTHING.

Use this to spot-check decode accuracy/coverage against real inventory before trusting the
worker's writes (or after enabling them). Read-only.

Usage:
    python scripts/decode_mpn_dryrun.py [--limit N] [--samples N]
"""

import argparse
from collections import Counter

from app.database import SessionLocal
from app.models import MaterialCard
from app.services.mpn_decoder import decode_mpn


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run MPN decoders over material_cards (no writes)")
    parser.add_argument("--limit", type=int, default=None, help="cap rows scanned")
    parser.add_argument("--samples", type=int, default=15, help="sample decoded rows to print")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        q = db.query(MaterialCard.display_mpn, MaterialCard.manufacturer, MaterialCard.category).filter(
            MaterialCard.deleted_at.is_(None), MaterialCard.display_mpn.isnot(None)
        )
        if args.limit:
            q = q.limit(args.limit)
        rows = q.all()

        total = len(rows)
        decoded = 0
        mismatch = 0
        by_vendor: Counter = Counter()
        by_commodity: Counter = Counter()
        spec_coverage: Counter = Counter()
        samples: list = []

        for mpn, mfr, category in rows:
            result = decode_mpn(mpn, mfr)
            if result is None:
                continue
            decoded += 1
            by_vendor[result.vendor] += 1
            by_commodity[result.commodity] += 1
            for key in result.specs:
                spec_coverage[key] += 1
            cat = (category or "").lower().strip()
            writable = result.commodity == cat
            if not writable:
                mismatch += 1
            if len(samples) < args.samples:
                samples.append((mpn, result.vendor, result.commodity, cat, writable, result.specs))

        pct = decoded * 100 // total if total else 0
        print(f"Scanned {total} cards · decoded {decoded} ({pct}%) · would-write {decoded - mismatch}")
        print(f"Category mismatch (decoded but card.category differs → NOT written): {mismatch}")
        print(f"By vendor:    {dict(by_vendor.most_common())}")
        print(f"By commodity: {dict(by_commodity.most_common())}")
        print(f"Spec coverage: {dict(spec_coverage.most_common())}")
        print("Samples:")
        for mpn, vendor, commodity, cat, writable, specs in samples:
            flag = "" if writable else f"  ⚠ card.category={cat!r} (skipped)"
            print(f"  {mpn:24} [{vendor}/{commodity}]{flag}  {specs}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
