"""services/crm_service.py -- Shared CRM helpers (extracted from routers).

Holds quote numbering (used by proactive_service) and the CDM account-workspace
business rules: staleness tiers, account-list query building/sorting, and the
contact-row assembly for the company detail panel.

Called by: app/routers/htmx_views.py (CDM workspace routes),
    app/routers/crm/quotes.py + app/routers/crm/__init__.py (next_quote_number),
    app/services/proactive_service.py, app/services/quote_builder_service.py
Depends on: app/models (Company, CustomerSite, SiteContact, Quote),
    app/models/auth (User), app/utils/search_builder
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


TIER_TARGET_DAYS = {"key": 7, "core": 14, "standard": 30, "prospect": 30}
CADENCE_RED_DAYS = 30  # universal ceiling — every tier goes overdue past this

_CLOCK_COLUMN = {"outbound": Company.last_outbound_at, "reply": Company.last_reply_at}


def order_by_clock(query, clock: str, now=None):
    """Order companies stalest-first: NULL clocks (never contacted) first, then oldest.

    NULLs-first is portable across SQLite (tests) and PostgreSQL (prod) by
    ordering on the IS-NULL flag before the timestamp.
    """
    col = _CLOCK_COLUMN[clock]
    return query.order_by(col.isnot(None), col.asc())


def cadence_state(tier: str | None, last_outbound_at: datetime | None, now: datetime | None = None) -> str:
    """Cadence state from the OUTBOUND clock against the account's tier target.

    Returns "new" (never touched), "on_target" (<= tier target), "due" (past target, <=
    30d), or "overdue" (> 30d, for every tier).
    """
    if last_outbound_at is None:
        return "new"
    now = now or datetime.now(timezone.utc)
    ts = last_outbound_at if last_outbound_at.tzinfo else last_outbound_at.replace(tzinfo=timezone.utc)
    days = (now - ts).days
    if days > CADENCE_RED_DAYS:
        return "overdue"
    target = TIER_TARGET_DAYS.get(tier or "standard", TIER_TARGET_DAYS["standard"])
    if days > target:
        return "due"
    return "on_target"


# Sort options for the CDM account workspace left panel. Default "oldest"
# puts the longest-neglected accounts at the top (call-list order).
CDM_SORTS = {
    "oldest": Company.last_activity_at.asc().nullsfirst(),
    "newest": Company.last_activity_at.desc().nullslast(),
    "name_asc": func.lower(Company.name).asc(),
    "name_desc": func.lower(Company.name).desc(),
}

CDM_ACCOUNT_TYPES = ("Customer", "Prospect", "Partner", "Competitor")


def _needs_call_filter(now: datetime):
    """Predicate for accounts needing a call: outbound clock overdue (30d+) OR never touched.

    Uses last_outbound_at (not last_activity_at) so the chip and its click-through
    filter are consistent with the cadence model — a reply does NOT reset the
    outbound obligation.

    Single source of truth shared by the "needs a call" chip COUNT
    (cdm_overdue_count) and the chip's click-through filter (cdm_company_query
    staleness="needs_call") — the two must never disagree.
    """
    cutoff = now - timedelta(days=CADENCE_RED_DAYS)
    return or_(Company.last_outbound_at < cutoff, Company.last_outbound_at.is_(None))


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
    """Build the filtered + sorted CDM account list query.

    staleness: "" (all) | "overdue" | "due_soon" | "recent" | "new" — one band each —
    or "needs_call" (overdue OR never contacted), the "needs a call" chip's filter.
    """
    query = db.query(Company).filter(Company.is_active.is_(True)).options(joinedload(Company.account_owner))

    if search.strip():
        sb = SearchBuilder(search.strip())
        query = query.filter(sb.ilike_filter(Company.name))

    now = now or datetime.now(timezone.utc)
    overdue_cutoff = now - timedelta(days=STALENESS_OVERDUE_DAYS)
    due_soon_cutoff = now - timedelta(days=STALENESS_DUE_SOON_DAYS)
    if staleness == "overdue":
        query = query.filter(Company.last_activity_at < overdue_cutoff)
    elif staleness == "needs_call":
        # The "N need a call" chip — must match cdm_overdue_count exactly.
        query = query.filter(_needs_call_filter(now))
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

    # Cadence clock sorts return a fully-ordered query directly (order_by_clock
    # calls query.order_by(...) internally), so we return early to avoid
    # double-applying .order_by() via the CDM_SORTS path.
    if sort == "outbound_asc":
        return order_by_clock(query, "outbound", now)
    if sort == "reply_asc":
        return order_by_clock(query, "reply", now)
    return query.order_by(CDM_SORTS.get(sort, CDM_SORTS["oldest"]))


def cdm_overdue_count(db: Session, user: User, now: datetime | None = None) -> int:
    """Count this user's accounts needing a call (overdue or never contacted).

    Sales/trader only — others get 0 (no chip rendered). Uses the same
    _needs_call_filter predicate as the chip's click-through query
    (staleness="needs_call") so count and list never diverge.
    """
    if user.role not in ("sales", "trader"):
        return 0
    now = now or datetime.now(timezone.utc)
    return (
        db.query(func.count(Company.id))
        .filter(
            Company.is_active.is_(True),
            Company.account_owner_id == user.id,
            _needs_call_filter(now),
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

    include_overdue: adds the filter-bar-only context — overdue_count (the
    "needs a call" chip, an extra COUNT query) AND account_types (the type
    dropdown options). The account-list refresh route re-renders neither, so
    it omits both and skips the COUNT query.
    """
    now = datetime.now(timezone.utc)
    query = cdm_company_query(
        db, user, search=search, staleness=staleness, account_type=account_type, my_only=my_only, sort=sort, now=now
    )
    total = query.count()
    companies = query.offset(offset).limit(limit).all()
    for c in companies:
        c.staleness = staleness_tier(c.last_activity_at)
        c.cadence_state = cadence_state(c.tier, c.last_outbound_at, now)

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
    """Active contacts for a company across its ACTIVE sites, plus legacy site-level
    contacts.

    Both is_active filters apply: deactivated SiteContacts are excluded, and contacts
    (real or legacy) on deactivated sites are excluded — outreach must never be logged
    against, or bump last_activity_at on, a deactivated entity. Pass pre-loaded ACTIVE
    sites (e.g. the filtered list from company_detail_partial) to skip the sites query.

    Returns [{"contact": SiteContact|None, "site": CustomerSite|None, "legacy": bool}].
    Invariant: legacy=True <=> contact is None <=> the contact fields live on
    site.contact_* (contacts_tab.html branches on row.legacy and relies on this).
    For legacy rows site is always set; for contact rows site is the contact's site
    (None only if the lookup misses).
    """
    if sites is None:
        sites = (
            db.query(CustomerSite).filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True)).all()
        )
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
