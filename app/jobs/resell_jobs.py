"""Resell background jobs — none scheduled since W1 simplification (2026-08-04).

Per docs/W1_JOB_DISPOSITION.md:
  - expire_resell_lists — DELETED (spec §3 kernel list + §6): the nightly M5
    list-expiry backstop and its service (``excess_service.expire_overdue_lists``)
    were removed; an owner resolves a lapsed posting via close / close-without-bid.
    Git history restores.
  - sweep_stale_sending_outreach — DELETED (spec §3/§8 zero-yield): the nightly
    table-wide sweep and its thin service wrapper were removed;
    ``resell_outreach_service.reclassify_stale_sending`` still reclassifies stale
    ``sending`` rows inline on tab load / retry. Git history restores.
  - recompute_buyer_scores — PARKED (spec §5.3 buyer-intelligence layer): the
    registration was removed; the job implementation below stays. Comeback
    trigger: a second trader user exists.

Called by: app/jobs/__init__.py via register_resell_jobs()
Depends on: app.database (SessionLocal), app.services.buyer_affinity_service
    (recompute_all_buyer_scores), app.scheduler (_traced_job)
"""

import sqlalchemy.exc
from loguru import logger

from ..scheduler import _traced_job


def register_resell_jobs(scheduler, settings):
    """Register the Resell lifecycle jobs — none since W1 (no-op).

    ``_job_recompute_buyer_scores`` below is PARKED, not deleted — re-add its
    ``scheduler.add_job`` call when a second trader user exists (spec §5.3).
    """


@_traced_job
async def _job_recompute_buyer_scores():
    """PARKED (not registered) — reconcile every buyer's BuyerScore against ground truth
    (finding #17 core).

    Delegates to ``buyer_affinity_service.recompute_all_buyer_scores`` (walks every
    VendorCard with an ExcessOffer or ExcessOutreach and upserts its scorecard). The
    backstop for a missed on-win / on-send hook — idempotent (the rollup reads full
    history), so a double-run is harmless.
    """
    from ..database import SessionLocal
    from ..services.buyer_affinity_service import recompute_all_buyer_scores

    db = SessionLocal()
    try:
        count = recompute_all_buyer_scores(db)
        if count:
            logger.info(f"Nightly buyer-score backstop recomputed {count} buyer(s)")
    except sqlalchemy.exc.SQLAlchemyError as e:
        logger.error(f"Resell buyer-score backstop DB error: {e}")
        db.rollback()
    except Exception as e:
        logger.exception(f"Resell buyer-score backstop error: {e}")
        db.rollback()
    finally:
        db.close()
