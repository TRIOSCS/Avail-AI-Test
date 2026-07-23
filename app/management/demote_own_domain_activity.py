"""app/management/demote_own_domain_activity.py — Backfill: demote own-domain attributed
ActivityLog rows (ISS-030 follow-up).

ISS-030 (PR #782) added a WRITE-TIME own-domain filter so internal (own-org) emails are
no longer marked is_meaningful on the company Activity tab — but historical rows written
before it still surface (e.g. the org's own company record carries thousands of
own-domain rows still is_meaningful=True). The reattribute_activity backfill structurally
cannot fix them: its candidate query requires company_id IS NULL, and these rows already
have company_id set.

This command scans attributed rows (company_id NOT NULL, contact_email NOT NULL,
is_meaningful TRUE OR NULL — the Activity tab's exact visibility predicate,
_is_meaningful_or_unscored, so unscored rows that surface on the tab are in scope too)
and demotes the ones whose contact_email is on one of the org's own domains — reusing
the exact write-path helpers (_is_internal_email for detection, demote_internal_activity
for the write: is_meaningful=False + quality_assessed_at/quality_classification stamps)
so read-time backfill and write-time filter can never disagree. The stamps matter:
score_unscored_activities selects on quality_assessed_at IS NULL, so an unstamped
demoted row <7 days old with an AI-scored activity_type would be AI re-promoted.
Candidates stream in id-keyset batches (no full-table materialization). External-domain
rows are counted but never touched. No deletions, no attribution changes.

Usage: python -m app.management.demote_own_domain_activity [--apply] [--limit N]

DRY-RUN by DEFAULT (--apply required to write) — no ActivityLog row is mutated unless
--apply is passed; the dry-run tally (plus sample rows) reports exactly what --apply
would write. Idempotent — re-running is safe: a demoted row falls out of the candidate
query (is_meaningful=False fails both arms of the TRUE-OR-NULL predicate).

Called by: an operator (manually, post-deploy of the ISS-030 write-time filter).
Depends on: app.database.SessionLocal, app.models.ActivityLog,
    app.services.activity_service._is_internal_email / _is_meaningful_or_unscored /
    demote_internal_activity.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator

from loguru import logger
from sqlalchemy.orm import Session

from ..models import ActivityLog
from ..services.activity_service import (
    _is_internal_email,
    _is_meaningful_or_unscored,
    demote_internal_activity,
)

_COMMIT_CHUNK = 500
_SCAN_CHUNK = 500
_SAMPLE_LIMIT = 5


def _iter_candidate_rows(db: Session, limit: int | None) -> Iterator[ActivityLog]:
    """Stream candidate rows in id-keyset batches of _SCAN_CHUNK.

    Keyset pagination (id > last seen, ordered by id) instead of .all() so a large
    activity table is never materialized in memory, and instead of yield_per so
    run_backfill's mid-scan chunked commits can't invalidate a server-side cursor. ORM-
    portable (SQLite tests / PG prod). The keyset cursor is captured before each batch
    is yielded, so a commit that expires the ORM objects never stalls the scan.
    """
    last_id = 0
    remaining = limit
    while remaining is None or remaining > 0:
        batch_size = _SCAN_CHUNK if remaining is None else min(_SCAN_CHUNK, remaining)
        rows = (
            db.query(ActivityLog)
            .filter(
                ActivityLog.company_id.isnot(None),
                ActivityLog.contact_email.isnot(None),
                # The Activity tab's exact visibility predicate (TRUE OR NULL) —
                # an unscored own-domain row surfaces on the tab just like a True one.
                _is_meaningful_or_unscored(),
                ActivityLog.id > last_id,
            )
            .order_by(ActivityLog.id)
            .limit(batch_size)
            .all()
        )
        if not rows:
            return
        last_id = rows[-1].id  # type: ignore[assignment]  # legacy Column-model ORM noise
        if remaining is not None:
            remaining -= len(rows)
        yield from rows


def run_backfill(db: Session, *, apply: bool, limit: int | None = None) -> dict:
    """Demote own-domain attributed ActivityLog rows (is_meaningful=False + the
    quality_assessed_at/quality_classification="internal" stamps).

    Returns the tally dict. Nothing is written to the session unless apply=True — the
    dry-run pass only reads, so no rollback is needed. Detection is the write path's
    _is_internal_email (settings.own_domains) and the write is the write path's
    demote_internal_activity — never a re-implementation of either.
    """
    mode = "APPLY" if apply else "DRY-RUN (no writes — pass --apply to write)"
    logger.info("demote_own_domain_activity: starting in {} mode", mode)

    tally = {
        "scanned": 0,
        "own_domain_flagged": 0,
        "external_skipped": 0,
    }
    samples: list[str] = []
    for row in _iter_candidate_rows(db, limit):
        tally["scanned"] += 1
        email = (row.contact_email or "").strip().lower()

        if not _is_internal_email(email):
            tally["external_skipped"] += 1
            continue

        tally["own_domain_flagged"] += 1
        if len(samples) < _SAMPLE_LIMIT:
            samples.append(f"id={row.id} contact_email={row.contact_email!r} subject={row.subject!r}")

        if apply:
            demote_internal_activity(row)
            if tally["own_domain_flagged"] % _COMMIT_CHUNK == 0:
                db.commit()

    if apply:
        db.commit()

    for sample in samples:
        logger.info("demote_own_domain_activity [{}] sample: {}", mode, sample)
    logger.info("demote_own_domain_activity [{}]: {}", mode, tally)
    return tally


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Demote own-domain attributed ActivityLog rows to is_meaningful=False (dry-run by default)."
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
