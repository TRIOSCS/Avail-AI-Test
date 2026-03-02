"""Credit Manager — tracks monthly API credit usage per enrichment provider.

Prevents overspend by checking budgets before each external API call.
Providers: lusha, hunter_search, hunter_verify, apollo.

Called by: customer_enrichment_service.py waterfall steps.
Depends on: app.models.enrichment.EnrichmentCreditUsage, app.config.settings.
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..config import settings
from ..models.enrichment import EnrichmentCreditUsage


def _current_month() -> str:
    """Return current month as 'YYYY-MM' string."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _default_limit(provider: str) -> int:
    """Get the configured monthly credit limit for a provider."""
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
    """Get or create a credit usage row for provider/month."""
    row = db.query(EnrichmentCreditUsage).filter_by(provider=provider, month=month).first()
    if not row:
        row = EnrichmentCreditUsage(
            provider=provider,
            month=month,
            credits_used=0,
            credits_limit=_default_limit(provider),
        )
        db.add(row)
        db.flush()
    return row


def get_monthly_usage(db: Session, provider: str) -> dict:
    """Get current month's usage for a provider. Returns {used, limit, remaining}."""
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
    """Check if there are enough credits to make an API call."""
    month = _current_month()
    row = _get_or_create_row(db, provider, month)
    return (row.credits_used + count) <= row.credits_limit


def record_credit_usage(db: Session, provider: str, count: int = 1) -> None:
    """Record that credits were consumed. Call after a successful API call."""
    month = _current_month()
    row = _get_or_create_row(db, provider, month)
    row.credits_used += count
    row.updated_at = datetime.now(timezone.utc)
    logger.debug("Credit usage: %s %s/%s (month=%s)", provider, row.credits_used, row.credits_limit, month)


def get_all_budgets(db: Session) -> list[dict]:
    """Get credit usage for all providers this month, including split Lusha pools."""
    providers = ["lusha_phone", "lusha_discovery", "hunter_search", "hunter_verify", "apollo"]
    budgets = [get_monthly_usage(db, p) for p in providers]

    # Add aggregate "lusha" entry summing both split pools
    phone = next(b for b in budgets if b["provider"] == "lusha_phone")
    discovery = next(b for b in budgets if b["provider"] == "lusha_discovery")
    budgets.append({
        "provider": "lusha",
        "month": phone["month"],
        "used": phone["used"] + discovery["used"],
        "limit": phone["limit"] + discovery["limit"],
        "remaining": phone["remaining"] + discovery["remaining"],
    })
    return budgets
