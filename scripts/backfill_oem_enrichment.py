"""One-time backfill: re-enrich not_found / not_catalogued MaterialCards through the OEM
cross-ref + description tiers.

Dry-run by default: runs ``enrich_card`` over the backlog, tallies projected outcomes,
writes a coverage CSV, and ROLLS BACK (writes nothing). ``--commit`` persists, with a
shared web-call budget cap so it cannot blow the API spend. The paced worker drains any
remainder afterward.

Usage:
  python3 scripts/backfill_oem_enrichment.py --dry-run
  python3 scripts/backfill_oem_enrichment.py --commit --max-web-calls 300 --limit 500

Called by: operators (manual, under explicit authorization). Depends on:
app.database.SessionLocal, app.services.authoritative_enrichment_service.
"""

import argparse
import asyncio
import csv
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from loguru import logger  # noqa: E402

from app.constants import MaterialEnrichmentStatus  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import MaterialCard  # noqa: E402
from app.services.authoritative_enrichment_service import (  # noqa: E402
    _connectors_in_order,
    enrich_card,
)
from app.services.enrichment_types import WebMeter  # noqa: E402
from app.services.enrichment_worker.oem_classifier import classify_oem_vendor  # noqa: E402
from app.utils.claude_errors import ClaudeError  # noqa: E402

_TARGET_STATUSES = (MaterialEnrichmentStatus.NOT_FOUND, MaterialEnrichmentStatus.NOT_CATALOGUED)


def _select(db, limit):
    q = (
        db.query(MaterialCard)
        .filter(
            MaterialCard.deleted_at.is_(None),
            MaterialCard.is_internal_part.is_(False),
            MaterialCard.enrichment_status.in_([s.value for s in _TARGET_STATUSES]),
        )
        .order_by(MaterialCard.search_count.desc(), MaterialCard.created_at.desc())
    )
    return q.limit(limit).all() if limit else q.all()


async def run(*, commit: bool, limit, max_web_calls: int, csv_path: str, db=None) -> dict:
    """Process the backlog.

    Returns a counts dict. Writes a coverage CSV. Rolls back unless commit.

    ``db`` may be injected (tests own/close it); when omitted a session is created and
    closed here. ``ClaudeError`` is caught per card and counted under ``"claude_error"``;
    5 consecutive ones abort the run (a backend outage — stop burning the web budget).
    """
    owns_session = db is None
    if db is None:
        db = SessionLocal()
    counts: dict[str, int] = {"processed": 0, "web_calls": 0}
    rows: list[dict] = []
    try:
        conns = _connectors_in_order(db)
        cards = _select(db, limit)
        logger.info("BACKFILL: {} candidate cards (commit={}, budget={})", len(cards), commit, max_web_calls)
        disabled: set[str] = set()
        cooldown: dict[str, float] = {}
        web_total = 0
        consecutive_claude_errors = 0
        aborted = False
        for i, card in enumerate(cards, 1):
            if web_total >= max_web_calls:
                logger.info("BACKFILL: web budget {} reached — stopping (remaining drains via worker)", max_web_calls)
                break
            meter = WebMeter()
            try:
                status = await enrich_card(
                    card, db, connectors=conns, disabled=disabled, cooldown=cooldown, web_meter=meter
                )
                consecutive_claude_errors = 0
            except ClaudeError:
                consecutive_claude_errors += 1
                status = "claude_error"
                logger.warning(
                    "BACKFILL: {} Claude error ({} consecutive)", card.display_mpn, consecutive_claude_errors
                )
                if consecutive_claude_errors >= 5:
                    aborted = True
            except Exception as e:
                consecutive_claude_errors = 0
                logger.warning("BACKFILL: {} failed: {}", card.display_mpn, type(e).__name__)
                status = "error"
            web_total += meter.web_calls
            counts["processed"] += 1
            counts[status] = counts.get(status, 0) + 1
            resolved = None
            if card.enrichment_provenance and isinstance(card.enrichment_provenance, dict):
                resolved = (card.enrichment_provenance.get("cross_ref") or {}).get("resolved_mpn")
            rows.append(
                {
                    "display_mpn": card.display_mpn,
                    "vendor": classify_oem_vendor(card.display_mpn) or "",
                    "projected_status": status,
                    "resolved_mpn": resolved or "",
                    "source": card.enrichment_source or "",
                }
            )
            if i % 25 == 0:
                logger.info("BACKFILL: {}/{} (web_calls={})", i, len(cards), web_total)
            if aborted:
                logger.error("BACKFILL: 5 consecutive Claude errors — aborting (backend outage)")
                break

        counts["web_calls"] = web_total
        if commit:
            db.commit()
            logger.info("BACKFILL: committed.")
        else:
            db.rollback()
            logger.info("BACKFILL: DRY RUN — rolled back, no DB writes.")

        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["display_mpn", "vendor", "projected_status", "resolved_mpn", "source"])
            w.writeheader()
            w.writerows(rows)
        logger.info("BACKFILL: coverage CSV → {}", csv_path)
        logger.info("BACKFILL SUMMARY: {}", counts)
        return counts
    except Exception:
        db.rollback()
        raise
    finally:
        if owns_session:
            db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="Preview only; roll back (default).")
    g.add_argument("--commit", action="store_true", help="Persist results.")
    ap.add_argument("--limit", type=int, default=None, help="Max cards to process.")
    ap.add_argument("--max-web-calls", type=int, default=300, help="Web-search call budget cap.")
    ap.add_argument("--csv", default="backfill_oem_coverage.csv", help="Coverage CSV output path.")
    args = ap.parse_args()
    if not args.commit:
        logger.info("DRY RUN — no DB writes. Use --commit to persist.")
    asyncio.run(run(commit=args.commit, limit=args.limit, max_web_calls=args.max_web_calls, csv_path=args.csv))
