#!/usr/bin/env python3
"""Report (and optionally backfill) MPN-decoded specs over material_cards.

Default is READ-ONLY: scan inventory, decode each MPN, and report coverage so you can
spot-check decode accuracy before trusting writes. With ``--apply`` it performs the backfill
by delegating to ``decode_and_record_specs`` (the same writer the enrichment worker uses), so
the write path — categorize-an-uncategorized-card-from-the-decode, conflict guard, record_spec
validation — is identical to production. Commits in chunks.

Usage:
    python scripts/decode_mpn_dryrun.py [--limit N] [--samples N]   # read-only report
    python scripts/decode_mpn_dryrun.py --apply [--limit N]         # backfill decoded specs
"""

import argparse
from collections import Counter

from app.database import SessionLocal
from app.models import MaterialCard
from app.services.mpn_decoder import decode_mpn
from app.services.mpn_decoder.writer import decode_and_record_specs

_CHUNK = 500  # cards per commit when applying


def _candidate_query(db):
    return db.query(MaterialCard.id, MaterialCard.display_mpn, MaterialCard.manufacturer, MaterialCard.category).filter(
        MaterialCard.deleted_at.is_(None), MaterialCard.display_mpn.isnot(None)
    )


def _report(rows, samples_n: int) -> None:
    total = len(rows)
    decoded = writable = conflict = 0
    by_vendor: Counter = Counter()
    by_commodity: Counter = Counter()
    spec_coverage: Counter = Counter()
    dropped_only: Counter = Counter()
    samples: list = []

    for _id, mpn, mfr, category in rows:
        result = decode_mpn(mpn, mfr)
        if result is None:
            continue
        if not result.specs:
            # Every decoded value failed a plausibility gate (grid/envelope) — the
            # writer would persist nothing for this card; surface it separately.
            for spec_key, value in result.dropped.items():
                dropped_only[f"{result.commodity}.{spec_key}={value}"] += 1
            continue
        decoded += 1
        # vendor/commodity pairs: WD HDD vs WD SSD, Samsung DRAM vs Samsung SSD, … stay distinct
        by_vendor[f"{result.vendor}/{result.commodity}"] += 1
        by_commodity[result.commodity] += 1
        for key in result.specs:
            spec_coverage[key] += 1
        cat = (category or "").lower().strip()
        # An empty category is writable (the decode categorizes it); a DIFFERENT category conflicts.
        is_conflict = bool(cat) and cat != result.commodity
        if is_conflict:
            conflict += 1
        else:
            writable += 1
        if len(samples) < samples_n:
            samples.append((mpn, result.vendor, result.commodity, cat, is_conflict, result.specs))

    pct = decoded * 100 // total if total else 0
    print(f"Scanned {total} cards · decoded {decoded} ({pct}%) · writable {writable} · conflict {conflict}")
    print(f"Conflict (decoded commodity ≠ an existing category → NOT written): {conflict}")
    print(f"By vendor:    {dict(by_vendor.most_common())}")
    print(f"By commodity: {dict(by_commodity.most_common())}")
    print(f"Spec coverage: {dict(spec_coverage.most_common())}")
    if dropped_only:
        print(f"Dropped-only (plausibility-gated, nothing writable): {dict(dropped_only.most_common())}")
    print("Samples:")
    for mpn, vendor, commodity, cat, is_conflict, specs in samples:
        if is_conflict:
            flag = f"  ⚠ card.category={cat!r} (conflict — skipped)"
        elif not cat:
            flag = "  → will categorize"
        else:
            flag = ""
        print(f"  {mpn:24} [{vendor}/{commodity}]{flag}  {specs}")


def _apply(db, ids: list[int]) -> None:
    totals: Counter = Counter()
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start : start + _CHUNK]
        # Per-card savepoints inside decode_and_record_specs keep the transaction usable, but
        # guard the chunk commit too: a failed commit must roll back and report THIS chunk, not
        # die mid-backfill after printing success for earlier chunks.
        try:
            res = decode_and_record_specs(db, chunk)
            db.commit()
        except Exception as exc:
            db.rollback()
            print(f"  ⚠ chunk {start}-{start + len(chunk)} FAILED — rolled back, continuing ({exc!r})")
            continue
        for k, v in res.items():
            totals[k] += v
        print(f"  …{min(start + _CHUNK, len(ids))}/{len(ids)} cards · {dict(totals)}")
    print(
        f"Applied: wrote {totals['written']} specs across {totals['decoded']} cards "
        f"({totals['categorized']} newly categorized)."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Report or backfill MPN-decoded specs over material_cards")
    parser.add_argument("--limit", type=int, default=None, help="cap rows scanned/applied")
    parser.add_argument("--samples", type=int, default=15, help="sample decoded rows to print (report mode)")
    parser.add_argument("--apply", action="store_true", help="WRITE decoded specs (default: read-only report)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        q = _candidate_query(db)
        if args.limit:
            q = q.limit(args.limit)
        rows = q.all()

        _report(rows, args.samples)
        if args.apply:
            print(f"\n--apply: backfilling decoded specs over {len(rows)} candidate cards…")
            _apply(db, [r[0] for r in rows])
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
