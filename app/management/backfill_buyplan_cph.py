"""Backfill customer_part_history from already-COMPLETED buy plans.

Called by: ops, once after deploy — `docker compose exec app python -m app.management.backfill_buyplan_cph`.
Depends on: purchase_history_service.record_buyplan_purchase_history. Idempotent via
BuyPlan.purchase_history_recorded_at.
"""

from loguru import logger
from sqlalchemy.orm import Session

from app.constants import BuyPlanStatus
from app.database import SessionLocal
from app.models.buy_plan import BuyPlan
from app.services.purchase_history_service import record_buyplan_purchase_history


def backfill(db: Session) -> int:
    plans = (
        db.query(BuyPlan)
        .filter(BuyPlan.status == BuyPlanStatus.COMPLETED.value, BuyPlan.purchase_history_recorded_at.is_(None))
        .all()
    )
    for plan in plans:
        record_buyplan_purchase_history(db, plan, refresh=False)
        db.commit()
    logger.info("BUYPLAN_CPH backfill: recorded {} completed plans", len(plans))
    return len(plans)


if __name__ == "__main__":
    db = SessionLocal()
    try:
        backfill(db)
    finally:
        db.close()
