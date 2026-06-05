"""Load a bare part-number file into MaterialCards (NO enrichment).

Creates bare cards (``enrichment_status='unenriched'``) via the same
``resolve_material_card`` upsert the POST /api/materials/import-part-numbers
endpoint uses. The paced enrichment worker fills descriptions/specs afterward,
within its daily web/AI budget and per-source cooldowns.

Inline enrichment was removed from this script deliberately: running connector /
web-search / Opus calls for thousands of parts at once thrashed free-tier API
quotas — that is the entire reason the paced worker exists and is the single
enrichment authority. This loader only inserts cards; the worker does the rest.

Usage:
  python3 scripts/import_part_numbers.py --file "/path/report.xls" --dry-run
  python3 scripts/import_part_numbers.py --file "/path/report.xls" --commit

Called by: operators (manual one-off load). Depends on: app.file_utils,
app.search_service.resolve_material_card, app.database.SessionLocal.
"""

import argparse
import os
import sys

# Allow running from the scripts/ directory or from the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from loguru import logger  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.file_utils import extract_mpns, parse_tabular_file  # noqa: E402
from app.search_service import resolve_material_card  # noqa: E402


def _run(file_path: str, commit: bool) -> dict[str, int]:
    """Parse the file and upsert a bare MaterialCard per MPN.

    Returns a counts dict {created, existing, skipped, total}. With ``commit=False``
    (dry run) the transaction is rolled back, but the counts still reflect what WOULD
    be created (cards are flushed during the loop, then rolled back).
    """
    db = SessionLocal()
    created = existing = skipped = 0
    try:
        content = open(file_path, "rb").read()
        mpns = extract_mpns(parse_tabular_file(content, file_path))
        logger.info("Parsed {} part numbers from {}", len(mpns), file_path)

        for i, raw in enumerate(mpns, 1):
            card = resolve_material_card(raw, db)
            if card is None:
                skipped += 1
                continue
            # resolve_material_card logs created vs resolved; a brand-new bare card is
            # detectable by the default state (unenriched, never enriched, never searched).
            if card.enrichment_status == "unenriched" and card.enriched_at is None and card.search_count == 0:
                created += 1
            else:
                existing += 1
            if i % 250 == 0:
                logger.info("...{}/{} (created={}, existing={}, skipped={})", i, len(mpns), created, existing, skipped)

        if commit:
            db.commit()
        else:
            db.rollback()

        logger.info(
            "SUMMARY created={} existing={} skipped={} total={} committed={}",
            created,
            existing,
            skipped,
            len(mpns),
            commit,
        )
        return {"created": created, "existing": existing, "skipped": skipped, "total": len(mpns)}
    finally:
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
    args = ap.parse_args()
    if not args.commit:
        logger.info("DRY RUN — no DB writes. Use --commit to persist. (Worker enriches loaded cards.)")
    _run(args.file, args.commit)
