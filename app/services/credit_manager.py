"""Credit Manager — tracks monthly API credit usage per enrichment provider.

Prevents overspend by checking budgets before each external API call.
Providers: lusha, hunter_search, hunter_verify, apollo.

Called by: customer_enrichment_service.py waterfall steps.
Depends on: app.models.enrichment.EnrichmentCreditUsage, app.config.settings.
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..models.enrichment import EnrichmentCreditUsage


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _default_limit(provider: str) -> int:
    limits = {
        "lusha": settings.lusha_monthly_credit_limit,
        "lusha_phone": settings.lusha_phone_credit_limit,
        "lusha_discovery": settings.lusha_discovery_credit_limit,
        "hunter_search": settings.hunter_monthly_search_limit,
        "hunter_verify": settings.hunter_monthly_verify_limit,
        "apollo": settings.apollo_monthly_credit_limit,
    }
    return limits.get(provider, 100)


def _get_or_create_row(db: Session, provider: str, month: str) -> EnrichmentCreditUsage:
    """Race-safe: savepoint + IntegrityError retry for concurrent inserts."""
    row = db.execute(
        select(EnrichmentCreditUsage).where(
            EnrichmentCreditUsage.provider == provider,
            EnrichmentCreditUsage.month == month,
        )
    ).scalar_one_or_none()
    if row:
        return row

    try:
        with db.begin_nested():
            row = EnrichmentCreditUsage(
                provider=provider,
                month=month,
                credits_used=0,
                credits_limit=_default_limit(provider),
            )
            db.add(row)
            db.flush()
        return row
    except IntegrityError:
        # Concurrent insert won — savepoint already rolled back by begin_nested()
        # Do NOT call db.rollback() — that would roll back the outer transaction
        return db.execute(
            select(EnrichmentCreditUsage).where(
                EnrichmentCreditUsage.provider == provider,
                EnrichmentCreditUsage.month == month,
            )
        ).scalar_one()


def get_monthly_usage(db: Session, provider: str) -> dict:
    month = _current_month()
    row = _get_or_create_row(db, provider, month)
    return {
        "provider": provider,
        "month": month,
        "used": row.credits_used,
        "limit": row.credits_limit,
        "remaining": max(0, row.credits_limit - row.credits_used),
    }


def can_use_credits(db: Session, provider: str, count: int = 1) -> bool:
    """NOTE: Prefer check_and_record_credits() for atomic check+spend."""
    month = _current_month()
    row = _get_or_create_row(db, provider, month)
    return (row.credits_used + count) <= row.credits_limit


def record_credit_usage(db: Session, provider: str, count: int = 1) -> None:
    month = _current_month()
    row = _get_or_create_row(db, provider, month)
    row.credits_used += count
    row.updated_at = datetime.now(timezone.utc)
    logger.debug("Credit usage: %s %s/%s (month=%s)", provider, row.credits_used, row.credits_limit, month)


def check_and_record_credits(db: Session, provider: str, count: int = 1) -> bool:
    """Atomic check-and-record with SELECT FOR UPDATE.

    Returns True if credits consumed, False if budget exceeded.
    """
    month = _current_month()
    _get_or_create_row(db, provider, month)  # ensure row exists

    locked_row = db.execute(
        select(EnrichmentCreditUsage)
        .where(EnrichmentCreditUsage.provider == provider, EnrichmentCreditUsage.month == month)
        .with_for_update()
    ).scalar_one()

    if (locked_row.credits_used + count) > locked_row.credits_limit:
        logger.warning(
            "Credit budget exceeded: %s %s/%s (requested %d, month=%s)",
            provider,
            locked_row.credits_used,
            locked_row.credits_limit,
            count,
            month,
        )
        return False

    locked_row.credits_used += count
    locked_row.updated_at = datetime.now(timezone.utc)
    logger.debug(
        "Credit usage: %s %s/%s (month=%s)", provider, locked_row.credits_used, locked_row.credits_limit, month
    )
    return True


def get_all_budgets(db: Session) -> list[dict]:
    providers = ["lusha_phone", "lusha_discovery", "hunter_search", "hunter_verify", "apollo"]
    budgets = [get_monthly_usage(db, p) for p in providers]
    phone = next(b for b in budgets if b["provider"] == "lusha_phone")
    discovery = next(b for b in budgets if b["provider"] == "lusha_discovery")
    budgets.append(
        {
            "provider": "lusha",
            "month": phone["month"],
            "used": phone["used"] + discovery["used"],
            "limit": phone["limit"] + discovery["limit"],
            "remaining": phone["remaining"] + discovery["remaining"],
        }
    )
    return budgets
