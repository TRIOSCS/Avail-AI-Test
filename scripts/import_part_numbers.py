"""Import a bare part-number file and run verified enrichment.

Usage:
  python3 scripts/import_part_numbers.py --file "/path/report.xls" --dry-run
  python3 scripts/import_part_numbers.py --file "/path/report.xls" --commit
"""

import argparse
import asyncio
import csv
import os
import sys
from datetime import datetime, timezone

# Allow running from the scripts/ directory or from the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from loguru import logger  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.file_utils import extract_mpns, parse_tabular_file  # noqa: E402
from app.models import MaterialCard  # noqa: E402
from app.services.authoritative_enrichment_service import (  # noqa: E402
    _connectors_in_order,
    enrich_card,
)
from app.utils.normalization import normalize_mpn_key  # noqa: E402

_REPORT_COLS = [
    "input_mpn",
    "normalized_mpn",
    "status",
    "source",
    "manufacturer",
    "category",
    "lifecycle_status",
    "package_type",
    "pin_count",
    "rohs_status",
    "description",
    "datasheet_url",
    "notes",
]


async def _run(file_path: str, commit: bool, report_path: str, refresh: bool) -> None:
    db = SessionLocal()
    try:
        content = open(file_path, "rb").read()
        mpns = extract_mpns(parse_tabular_file(content, file_path))
        logger.info("Parsed {} part numbers from {}", len(mpns), file_path)

        conns = _connectors_in_order(db)
        rows = []
        counts = {"verified": 0, "ai_inferred": 0, "not_found": 0}

        for i, raw in enumerate(mpns):
            norm = normalize_mpn_key(raw)
            if not norm:
                rows.append(
                    {
                        "input_mpn": raw,
                        "normalized_mpn": "",
                        "status": "skipped",
                        "notes": "unparseable mpn",
                    }
                )
                continue
            # In dry-run, use a transient card not added to the session.
            card = db.query(MaterialCard).filter_by(normalized_mpn=norm).first()
            transient = card is None
            if transient:
                card = MaterialCard(
                    normalized_mpn=norm,
                    display_mpn=raw.strip(),
                    created_at=datetime.now(timezone.utc),
                )
            status = await enrich_card(card, db, connectors=conns, refresh=refresh)
            counts[status] = counts.get(status, 0) + 1
            prov = card.enrichment_provenance or {}
            rows.append(
                {
                    "input_mpn": raw,
                    "normalized_mpn": norm,
                    "status": status,
                    "source": (prov.get("description") or {}).get("source", ""),
                    "manufacturer": card.manufacturer or "",
                    "category": card.category or "",
                    "lifecycle_status": card.lifecycle_status or "",
                    "package_type": card.package_type or "",
                    "pin_count": card.pin_count or "",
                    "rohs_status": card.rohs_status or "",
                    "description": card.description or "",
                    "datasheet_url": card.datasheet_url or "",
                    "notes": "" if not transient else "new card",
                }
            )
            if commit:
                if transient:
                    db.add(card)
                if (i + 1) % 50 == 0:
                    db.commit()
                    logger.info("Committed {}/{}", i + 1, len(mpns))
            if (i + 1) % 25 == 0:
                logger.info("Processed {}/{} ({})", i + 1, len(mpns), counts)

        if commit:
            db.commit()
        else:
            db.rollback()

        with open(report_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_REPORT_COLS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        logger.info("Wrote report -> {}", report_path)
        logger.info("SUMMARY: {} (committed={})", counts, commit)
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--commit", action="store_true", help="Write to DB (default is dry-run)")
    ap.add_argument("--refresh", action="store_true", help="Re-enrich already-verified cards")
    ap.add_argument("--report", default=None)
    args = ap.parse_args()
    report = args.report or f"reports/part_import_report_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.csv"
    os.makedirs(os.path.dirname(report) or ".", exist_ok=True)
    if not args.commit:
        logger.info("DRY RUN — no DB writes. Use --commit to persist.")
    asyncio.run(_run(args.file, args.commit, report, args.refresh))
