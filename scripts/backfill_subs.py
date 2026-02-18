#!/usr/bin/env python3
"""Backfill substitute parts from Salesforce CSV exports into the requirements table.

Reads:
  /root/sf_import/LSC1__Material__c.csv         — material ID → MPN
  /root/sf_import/LSC1__Substitution__c.csv      — links req items to alternate materials

Matches on requirements.sf_req_item_id and writes the substitutes JSON array.

Usage:
  DRY RUN:   python scripts/backfill_subs.py
  APPLY:     python scripts/backfill_subs.py --apply
"""

import csv
import json
import sys
import os

# Add project root so we can import app modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SF_DIR = "/root/sf_import"
MATERIAL_CSV = os.path.join(SF_DIR, "LSC1__Material__c.csv")
SUBSTITUTION_CSV = os.path.join(SF_DIR, "LSC1__Substitution__c.csv")

DRY_RUN = "--apply" not in sys.argv


def main():
    # 1. Build material ID → MPN lookup
    print("Loading materials...")
    mat_map = {}
    with open(MATERIAL_CSV, encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            name = row.get("Name", "").strip()
            if name:
                mat_map[row["Id"]] = name
    print(f"  {len(mat_map)} materials loaded")

    # 2. Build sf_req_item_id → [substitute MPNs]
    print("Loading substitutions...")
    subs_by_req = {}
    matched = 0
    for row_iter in [csv.DictReader(
        open(SUBSTITUTION_CSV, encoding="utf-8", errors="replace")
    )]:
        for row in row_iter:
            req_item_id = row.get("LSC1__Requisition_Item__c", "").strip()
            mat_id = row.get("LSC1__Alternate_Material__c", "").strip()
            if not req_item_id or not mat_id:
                continue
            mpn = mat_map.get(mat_id)
            if not mpn:
                continue
            matched += 1
            subs_by_req.setdefault(req_item_id, []).append(mpn)
    print(f"  {matched} substitution links across {len(subs_by_req)} req items")

    # 3. Connect to DB and update
    from sqlalchemy import create_engine, text

    db_url = os.environ.get(
        "DATABASE_URL", "postgresql://availai:availai@localhost:5432/availai"
    )
    engine = create_engine(db_url)

    with engine.connect() as conn:
        # Get all sf_req_item_ids that exist in DB
        result = conn.execute(
            text("SELECT id, sf_req_item_id, substitutes::text FROM requirements WHERE sf_req_item_id IS NOT NULL")
        )
        rows = result.fetchall()
        print(f"  {len(rows)} requirements with sf_req_item_id in DB")

        updated = 0
        skipped_no_match = 0
        skipped_already = 0

        for row_id, sf_id, existing_subs in rows:
            new_subs = subs_by_req.get(sf_id)
            if not new_subs:
                skipped_no_match += 1
                continue

            # Deduplicate, preserve order
            seen = set()
            deduped = []
            for s in new_subs:
                key = s.strip().upper()
                if key not in seen:
                    seen.add(key)
                    deduped.append(s.strip())

            # Check if already populated
            if existing_subs and existing_subs not in ("[]", "null", "", "None"):
                try:
                    existing = json.loads(existing_subs)
                    if existing:
                        skipped_already += 1
                        continue
                except (json.JSONDecodeError, TypeError):
                    pass

            if DRY_RUN:
                updated += 1
                if updated <= 5:
                    print(f"    [DRY RUN] req {row_id} (sf={sf_id}): {deduped[:5]}{'...' if len(deduped) > 5 else ''}")
            else:
                conn.execute(
                    text("UPDATE requirements SET substitutes = :subs WHERE id = :id"),
                    {"subs": json.dumps(deduped), "id": row_id},
                )
                updated += 1

        if not DRY_RUN:
            conn.commit()

        print(f"\nResults:")
        print(f"  Updated:          {updated}")
        print(f"  No SF match:      {skipped_no_match}")
        print(f"  Already had subs: {skipped_already}")
        if DRY_RUN:
            print(f"\n  *** DRY RUN — no changes made. Run with --apply to commit. ***")
        else:
            print(f"\n  Done! {updated} requirements backfilled with substitute parts.")


if __name__ == "__main__":
    main()
