"""services/crm_service.py -- Shared CRM helpers (extracted from routers).

Holds quote numbering (used by proactive_service) and the CDM account-workspace
business rules: staleness tiers, account-list query building/sorting, and the
contact-row assembly for the company detail panel.

Called by: app/routers/htmx_views.py (CDM workspace routes), app/services/proactive_service.py
Depends on: app/models (Company, CustomerSite, SiteContact, Quote), app/utils/search_builder
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from ..models import Company, CustomerSite, Quote, SiteContact
from ..models.auth import User
from ..utils.search_builder import SearchBuilder

STALENESS_OVERDUE_DAYS = 30
STALENESS_DUE_SOON_DAYS = 14


def next_quote_number(db: Session) -> str:
    """Generate next sequential quote number: Q-YYYY-NNNN.

    Uses SELECT FOR UPDATE to prevent race conditions.
    """
    year = datetime.now(timezone.utc).year
    prefix = f"Q-{year}-"
    last = (
        db.query(Quote)
        .filter(Quote.quote_number.like(f"{prefix}%"))
        .order_by(Quote.id.desc())
        .with_for_update()
        .first()
    )
    if last:
        try:
            seq = int(last.quote_number.split("-")[-1]) + 1
        except ValueError:
            seq = 1
    else:
        seq = 1
    return f"{prefix}{seq:04d}"


# ═══════════════════════════════════════════════════════════════════════
#  CDM ACCOUNT WORKSPACE — staleness, list query, contact rows
# ═══════════════════════════════════════════════════════════════════════


def staleness_tier(last_activity_at: datetime | None) -> str:
    """Compute staleness tier (new/overdue/due_soon/recent) from a timestamp."""
    if last_activity_at is None:
        return "new"
    ts = last_activity_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - ts).days
    if days >= STALENESS_OVERDUE_DAYS:
        return "overdue"
    if days >= STALENESS_DUE_SOON_DAYS:
        return "due_soon"
    return "recent"


# Sort options for the CDM account workspace left panel. Default "oldest"
# puts the longest-neglected accounts at the top (call-list order).
CDM_SORTS = {
    "oldest": Company.last_activity_at.asc().nullsfirst(),
    "newest": Company.last_activity_at.desc().nullslast(),
    "name_asc": func.lower(Company.name).asc(),
    "name_desc": func.lower(Company.name).desc(),
}

CDM_ACCOUNT_TYPES = ("Customer", "Prospect", "Partner", "Competitor")


def cdm_company_query(
    db: Session,
    user: User,
    *,
    search: str,
    staleness: str,
    account_type: str,
    my_only: bool,
    sort: str,
    now: datetime | None = None,
):
    """Build the filtered + sorted CDM account list query."""
    query = db.query(Company).filter(Company.is_active.is_(True)).options(joinedload(Company.account_owner))

    if search.strip():
        sb = SearchBuilder(search.strip())
        query = query.filter(sb.ilike_filter(Company.name))

    now = now or datetime.now(timezone.utc)
    overdue_cutoff = now - timedelta(days=STALENESS_OVERDUE_DAYS)
    due_soon_cutoff = now - timedelta(days=STALENESS_DUE_SOON_DAYS)
    if staleness == "overdue":
        query = query.filter(Company.last_activity_at < overdue_cutoff)
    elif staleness == "due_soon":
        query = query.filter(
            Company.last_activity_at >= overdue_cutoff,
            Company.last_activity_at < due_soon_cutoff,
        )
    elif staleness == "recent":
        query = query.filter(Company.last_activity_at >= due_soon_cutoff)
    elif staleness == "new":
        query = query.filter(Company.last_activity_at.is_(None))

    if account_type in CDM_ACCOUNT_TYPES:
        query = query.filter(Company.account_type == account_type)
    if my_only:
        query = query.filter(Company.account_owner_id == user.id)

    return query.order_by(CDM_SORTS.get(sort, CDM_SORTS["oldest"]))


def cdm_overdue_count(db: Session, user: User, now: datetime | None = None) -> int:
    """Count this user's accounts needing a call (overdue or never contacted).

    Sales/trader only — others get 0 (no chip rendered).
    """
    if user.role not in ("sales", "trader"):
        return 0
    now = now or datetime.now(timezone.utc)
    call_threshold = now - timedelta(days=STALENESS_OVERDUE_DAYS)
    return (
        db.query(func.count(Company.id))
        .filter(
            Company.is_active.is_(True),
            Company.account_owner_id == user.id,
            or_(
                Company.last_activity_at < call_threshold,
                Company.last_activity_at.is_(None),
            ),
        )
        .scalar()
        or 0
    )


def cdm_list_ctx(
    db: Session,
    user: User,
    *,
    search: str,
    staleness: str,
    account_type: str,
    my_only: bool,
    sort: str,
    limit: int,
    offset: int,
    include_overdue: bool = False,
) -> dict:
    """Shared context for the CDM workspace shell and its account-list partial.

    include_overdue: the overdue chip lives in the filter bar (workspace shell
    only), so the account-list refresh route skips that COUNT query.
    """
    now = datetime.now(timezone.utc)
    query = cdm_company_query(
        db, user, search=search, staleness=staleness, account_type=account_type, my_only=my_only, sort=sort, now=now
    )
    total = query.count()
    companies = query.offset(offset).limit(limit).all()
    for c in companies:
        c.staleness = staleness_tier(c.last_activity_at)

    ctx = {
        "companies": companies,
        "search": search,
        "staleness": staleness,
        "account_type": account_type,
        "my_only": my_only,
        "sort": sort,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
    if include_overdue:
        ctx["overdue_count"] = cdm_overdue_count(db, user, now=now)
        ctx["account_types"] = CDM_ACCOUNT_TYPES
    return ctx


def company_contact_rows(db: Session, company_id: int, sites: list[CustomerSite] | None = None) -> list[dict]:
    """All contacts for a company across its sites, plus legacy site-level contacts.

    Returns [{"contact": SiteContact|None, "site": CustomerSite|None, "legacy": bool}].
    Pass pre-loaded sites (e.g. from joinedload) to skip the sites query.
    """
    if sites is None:
        sites = db.query(CustomerSite).filter(CustomerSite.company_id == company_id).all()
    site_map = {s.id: s for s in sites}
    contacts: list[SiteContact] = []
    if site_map:
        contacts = (
            db.query(SiteContact)
            .filter(SiteContact.customer_site_id.in_(list(site_map)), SiteContact.is_active.is_(True))
            .order_by(SiteContact.is_primary.desc(), SiteContact.full_name)
            .all()
        )
    rows = [{"contact": c, "site": site_map.get(c.customer_site_id), "legacy": False} for c in contacts]
    for s in sites:
        if s.contact_name or s.contact_email:
            rows.append({"contact": None, "site": s, "legacy": True})
    return rows
