"""Prospect pool service — queries unowned companies for the Suggested tab."""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Company
from app.schemas.prospect_pool import (
    PoolAccountList,
    PoolAccountRead,
    PoolFilters,
    PoolStats,
)
from app.utils.sql_helpers import escape_like


def _pool_base_filter(db: Session):
    """Base query: unowned + not dismissed."""
    return db.query(Company).filter(
        Company.account_owner_id == None,  # noqa: E711
        Company.is_active == True,  # noqa: E712
        (Company.import_priority != "dismissed") | (Company.import_priority == None),  # noqa: E711
    )


def get_pool_stats(db: Session) -> PoolStats:
    """Aggregate counts for the stats bar."""
    base = _pool_base_filter(db)
    total = base.count()
    priority = base.filter(Company.import_priority == "priority").count()
    standard = base.filter(Company.import_priority == "standard").count()

    # Claimed this month = companies whose account_owner_id was set this calendar month
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    claimed = (
        db.query(func.count(Company.id))
        .filter(
            Company.account_owner_id != None,  # noqa: E711
            Company.updated_at >= month_start,
        )
        .scalar()
        or 0
    )

    return PoolStats(
        total_available=total,
        priority_count=priority,
        standard_count=standard,
        claimed_this_month=claimed,
    )


def get_pool_accounts(filters: PoolFilters, db: Session) -> PoolAccountList:
    """Paginated pool listing with search and filters."""
    query = _pool_base_filter(db)

    if filters.import_priority:
        query = query.filter(Company.import_priority == filters.import_priority)

    if filters.industry:
        safe = escape_like(filters.industry.strip())
        query = query.filter(Company.industry.ilike(f"%{safe}%"))

    if filters.search:
        safe = escape_like(filters.search.strip())
        query = query.filter(Company.name.ilike(f"%{safe}%") | Company.domain.ilike(f"%{safe}%"))

    total = query.count()

    # Sort: priority first (nulls last), then name
    if filters.sort_by == "name":
        query = query.order_by(Company.name)
    else:
        query = query.order_by(
            # 'priority' sorts before 'standard' alphabetically
            Company.import_priority.asc().nullslast(),
            Company.name,
        )

    offset = (filters.page - 1) * filters.per_page
    rows = query.offset(offset).limit(filters.per_page).all()

    items = [
        PoolAccountRead(
            id=c.id,
            name=c.name,
            domain=c.domain,
            website=c.website,
            industry=c.industry,
            phone=c.phone,
            hq_city=c.hq_city,
            hq_state=c.hq_state,
            hq_country=c.hq_country,
            import_priority=c.import_priority,
            sf_account_id=c.sf_account_id,
        )
        for c in rows
    ]

    stats = get_pool_stats(db)

    return PoolAccountList(
        items=items,
        total=total,
        page=filters.page,
        per_page=filters.per_page,
        pool_stats=stats,
    )


def claim_pool_account(company_id: int, user_id: int, user_name: str, db: Session) -> dict:
    """Claim a pool account — sets account_owner_id."""
    company = db.get(Company, company_id)
    if not company:
        return {"error": "Company not found", "status": 404}

    if company.account_owner_id is not None:
        return {"error": "Already claimed", "status": 409}

    company.account_owner_id = user_id
    company.import_priority = None  # no longer in pool
    db.commit()

    logger.info(
        "User {} claimed account {} (id={}) from prospect pool",
        user_name,
        company.name,
        company.id,
    )

    return {
        "company_id": company.id,
        "company_name": company.name,
        "status": "claimed",
    }


def dismiss_pool_account(company_id: int, user_id: int, user_name: str, reason: str, db: Session) -> dict:
    """Dismiss a pool account — marks import_priority='dismissed'."""
    company = db.get(Company, company_id)
    if not company:
        return {"error": "Company not found", "status": 404}

    if company.account_owner_id is not None:
        return {"error": "Cannot dismiss an owned account", "status": 409}

    company.import_priority = "dismissed"
    company.notes = f"{company.notes or ''}\n[Dismissed by {user_name}: {reason}]".strip()
    db.commit()

    logger.info(
        "User {} dismissed account {} (id={}) reason={}",
        user_name,
        company.name,
        company.id,
        reason,
    )

    return {
        "company_id": company.id,
        "company_name": company.name,
        "status": "dismissed",
    }
