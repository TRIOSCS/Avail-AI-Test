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
from typing import NamedTuple

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


class _WorkItem(NamedTuple):
    raw: str
    norm: str
    card: MaterialCard
    transient: bool


def _report_row(raw: str, norm: str, status: str, card: MaterialCard, transient: bool) -> dict:
    prov = card.enrichment_provenance or {}
    return {
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
        "notes": "new card" if transient else "",
    }


async def _run(file_path: str, commit: bool, report_path: str, refresh: bool, concurrency: int = 8) -> None:
    db = SessionLocal()
    disabled: set[str] = set()
    counts: dict[str, int] = {"verified": 0, "ai_inferred": 0, "not_found": 0}
    rows: list[dict] = []
    try:
        content = open(file_path, "rb").read()
        mpns = extract_mpns(parse_tabular_file(content, file_path))
        logger.info("Parsed {} part numbers from {}", len(mpns), file_path)

        conns = _connectors_in_order(db)
        logger.info("Active connectors: {}", [c.source_name for c in conns])

        # Phase 1 (serial): resolve existing or build transient cards.
        work: list[_WorkItem] = []
        for raw in mpns:
            norm = normalize_mpn_key(raw)
            if not norm:
                rows.append({"input_mpn": raw, "normalized_mpn": "", "status": "skipped", "notes": "unparseable mpn"})
                continue
            card = db.query(MaterialCard).filter_by(normalized_mpn=norm).first()
            transient = card is None
            if transient:
                card = MaterialCard(normalized_mpn=norm, display_mpn=raw.strip(), created_at=datetime.now(timezone.utc))
            work.append(_WorkItem(raw, norm, card, transient))

        # Phase 2: enrich concurrently (only the network calls overlap; sync DB ops
        # run atomically between awaits, so the shared Session stays consistent).
        # Commit per chunk for partial-progress durability; report rows are built
        # BEFORE each commit to avoid expire_on_commit reloads.
        sem = asyncio.Semaphore(concurrency)

        async def _enrich(card: MaterialCard) -> str:
            async with sem:
                return await enrich_card(card, db, connectors=conns, refresh=refresh, disabled=disabled)

        chunk_size = 100
        done = 0
        for start in range(0, len(work), chunk_size):
            chunk = work[start : start + chunk_size]
            statuses = await asyncio.gather(*(_enrich(item.card) for item in chunk), return_exceptions=True)
            for item, status in zip(chunk, statuses):
                if isinstance(status, Exception):
                    logger.error("enrich failed for {}: {}", item.raw, status)
                    rows.append(
                        {
                            "input_mpn": item.raw,
                            "normalized_mpn": item.norm,
                            "status": "error",
                            "notes": f"{type(status).__name__}: {status}",
                        }
                    )
                    continue
                counts[status] = counts.get(status, 0) + 1
                rows.append(_report_row(item.raw, item.norm, status, item.card, item.transient))
                if commit and item.transient:
                    db.add(item.card)
            if commit:
                try:
                    db.commit()
                except Exception as e:
                    logger.error("commit failed for chunk {}-{}: {}", start, start + len(chunk), e)
                    db.rollback()
                    continue
            done += len(chunk)
            logger.info("Processed {}/{} ({}) committed={}", done, len(work), counts, commit)

        if not commit:
            db.rollback()
    finally:
        try:
            with open(report_path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=_REPORT_COLS, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
            logger.info("Wrote report -> {}", report_path)
        except Exception as e:
            logger.error("Failed to write report to {}: {}", report_path, e)
        if disabled:
            logger.error("Sources DISABLED this run (quota/auth): {}", sorted(disabled))
        logger.info("SUMMARY: {} disabled_sources={} (committed={})", counts, sorted(disabled), commit)
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only, no DB writes (this is the default; flag accepted explicitly)",
    )
    ap.add_argument("--commit", action="store_true", help="Write to DB (default is dry-run)")
    ap.add_argument("--refresh", action="store_true", help="Re-enrich already-verified cards")
    ap.add_argument("--concurrency", type=int, default=8, help="Concurrent enrichments (network overlap)")
    ap.add_argument("--report", default=None)
    args = ap.parse_args()
    report = args.report or f"reports/part_import_report_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.csv"
    os.makedirs(os.path.dirname(report) or ".", exist_ok=True)
    if not args.commit:
        logger.info("DRY RUN — no DB writes. Use --commit to persist.")
    asyncio.run(_run(args.file, args.commit, report, args.refresh, args.concurrency))
