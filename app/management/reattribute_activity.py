"""app/management/reattribute_activity.py — Backfill: re-attribute orphaned ActivityLog
rows (ISS-030).

Historical ActivityLog rows written before the email write-time filters (log_email_
activity's own-domain/junk pre-filter, scan_sent_folder's entity resolution) can carry
requisition_id but NO company_id/vendor_card_id. Those rows were (wrongly) visible to
the OLD company Activity tab's requisition_id OR-leak query, but the NEW
get_company_activities() scope is company_id-only — without this backfill, a real
customer email thread on one of those orphaned rows would silently vanish from the tab
instead of just stopping the leak.

This command re-runs match_email_to_entity() on each candidate row's stored
contact_email and, when it resolves, fills in company_id or vendor_card_id. Rows whose
counterparty is an own-domain or junk address (settings.own_domains / JUNK_DOMAINS /
JUNK_EMAIL_PREFIXES) are demoted instead via the write path's demote_internal_activity
(is_meaningful=False + quality_assessed_at/quality_classification="internal" stamps —
score_unscored_activities selects on quality_assessed_at IS NULL, so an unstamped
demotion would be AI re-promoted within days). This keeps them excluded from both the
company and vendor Activity tabs without deleting any data.

Usage: python -m app.management.reattribute_activity [--apply] [--limit N]

DRY-RUN by DEFAULT (--apply required to write) — no ActivityLog row is mutated unless
--apply is passed; the dry-run tally reports exactly what --apply would write.
Idempotent — re-running is safe: a row that already resolved falls out of the candidate
query (company_id/vendor_card_id no longer NULL); a row already flagged is_meaningful=
False is re-flagged to the same value. No deletions.

Called by: an operator (manually, post-deploy of the ISS-030 activity-tab fix).
Depends on: app.database.SessionLocal, app.models.ActivityLog,
    app.services.activity_service.match_email_to_entity / _is_internal_email /
    _is_junk_email / demote_internal_activity.
"""

from __future__ import annotations

import argparse
import sys

from loguru import logger
from sqlalchemy.orm import Session

from ..models import ActivityLog
from ..services.activity_service import (
    _is_internal_email,
    _is_junk_email,
    demote_internal_activity,
    match_email_to_entity,
)

_COMMIT_CHUNK = 500


def _candidate_rows(db: Session, limit: int | None) -> list[ActivityLog]:
    q = (
        db.query(ActivityLog)
        .filter(
            ActivityLog.requisition_id.isnot(None),
            ActivityLog.company_id.is_(None),
            ActivityLog.vendor_card_id.is_(None),
            ActivityLog.contact_email.isnot(None),
        )
        .order_by(ActivityLog.id)
    )
    if limit:
        q = q.limit(limit)
    return q.all()


def run_backfill(db: Session, *, apply: bool, limit: int | None = None) -> dict:
    """Re-attribute (or flag-as-noise) orphaned ActivityLog rows.

    Returns the tally dict. Nothing is written to the session unless apply=True — the
    dry-run pass only reads (match_email_to_entity queries other tables, never
    ActivityLog), so no rollback is needed.
    """
    mode = "APPLY" if apply else "DRY-RUN (no writes — pass --apply to write)"
    logger.info("reattribute_activity: starting in {} mode", mode)

    tally = {
        "scanned": 0,
        "company_attributed": 0,
        "vendor_attributed": 0,
        "flagged_noise": 0,
        "unresolved": 0,
    }
    rows = _candidate_rows(db, limit)
    for row in rows:
        tally["scanned"] += 1
        email = (row.contact_email or "").strip().lower()
        if not email:
            tally["unresolved"] += 1
            continue

        match = match_email_to_entity(email, db)
        company_id = match["id"] if match and match["type"] == "company" else None
        vendor_card_id = match["id"] if match and match["type"] == "vendor" else None
        is_noise = _is_internal_email(email) or _is_junk_email(email)

        if company_id is not None:
            tally["company_attributed"] += 1
        elif vendor_card_id is not None:
            tally["vendor_attributed"] += 1
        if is_noise:
            tally["flagged_noise"] += 1
        if company_id is None and vendor_card_id is None and not is_noise:
            tally["unresolved"] += 1

        if apply:
            if company_id is not None:
                row.company_id = company_id
            if vendor_card_id is not None:
                row.vendor_card_id = vendor_card_id
            if is_noise:
                demote_internal_activity(row)
            if tally["scanned"] % _COMMIT_CHUNK == 0:
                db.commit()

    if apply:
        db.commit()

    logger.info("reattribute_activity [{}]: {}", mode, tally)
    return tally


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Backfill company_id/vendor_card_id attribution on orphaned ActivityLog rows (dry-run by default)."
    )
    parser.add_argument("--apply", action="store_true", help="Write the backfill (default: dry-run, no writes)")
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of candidate rows scanned")
    args = parser.parse_args(argv)

    from ..database import SessionLocal

    db = SessionLocal()
    try:
        run_backfill(db, apply=args.apply, limit=args.limit)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
