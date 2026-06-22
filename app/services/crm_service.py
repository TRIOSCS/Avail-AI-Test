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

from sqlalchemy import and_, func, or_
from sqlalchemy import case as sa_case
from sqlalchemy.orm import Session, joinedload

from ..constants import RequisitionStatus
from ..models import Company, CustomerSite, Quote, Requisition, SiteContact
from ..models.auth import User
from ..models.tags import EntityTag
from ..models.vendors import VendorCard
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

_CLOCK_COLUMNS = {
    Company: {"outbound": Company.last_outbound_at, "reply": Company.last_reply_at},
    VendorCard: {"outbound": VendorCard.last_outbound_at, "reply": VendorCard.last_reply_at},
}
# Keep the old name as a view of the Company entry for backwards-compat internal refs.
_CLOCK_COLUMN = _CLOCK_COLUMNS[Company]


def order_by_clock(query, clock: str, *, model=Company, now=None):
    """Order rows stalest-first: NULL clocks (never contacted) first, then oldest.

    Works for both Company (customer cadence) and VendorCard (vendor cadence).
    Defaults to Company so all existing customer call sites are unchanged.

    NULLs-first is portable across SQLite (tests) and PostgreSQL (prod) by
    ordering on the IS-NULL flag before the timestamp.
    """
    col = _CLOCK_COLUMNS[model][clock]
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


def _not_bucketed():
    """NULL-safe predicate excluding 'bucket'-disposition accounts.

    NULL disposition ⇒ active (mirrors tier's NULL ⇒ standard), so the bare `disposition
    != 'bucket'` is INSUFFICIENT — on Postgres `NULL != 'bucket'` is NULL (excluded),
    which would silently drop every untouched account. SQLite masks this. The explicit
    is_(None) arm keeps NULL rows in.
    """
    return or_(Company.disposition != "bucket", Company.disposition.is_(None))


def _needs_call_filter(now: datetime):
    """Predicate for accounts needing a call: outbound clock overdue (30d+) OR never touched.

    Uses last_outbound_at (not last_activity_at) so the chip and its click-through
    filter are consistent with the cadence model — a reply does NOT reset the
    outbound obligation. Excludes bucketed accounts (disposition='bucket') here so
    the suppression is applied in exactly ONE place shared by the chip COUNT and
    its click-through list — count==list invariant.

    Single source of truth shared by the "needs a call" chip COUNT
    (cdm_overdue_count) and the chip's click-through filter (cdm_company_query
    staleness="needs_call") — the two must never disagree.
    """
    cutoff = now - timedelta(days=CADENCE_RED_DAYS)
    return and_(
        or_(Company.last_outbound_at < cutoff, Company.last_outbound_at.is_(None)),
        _not_bucketed(),
    )


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
    segment: int = 0,
):
    """Build the filtered + sorted CDM account list query.

    staleness: "" (all) | "overdue" | "due_soon" | "recent" | "new" — one band each —
    or "needs_call" (overdue OR never contacted), the "needs a call" chip's filter,
    or "bucket" (the explicit Bucket facet — the ONLY way to surface bucketed accounts).

    Bucketed accounts (disposition='bucket') are hidden from the base query so they
    drop out of the call-list everywhere, EXCEPT when the "bucket" facet is
    explicitly requested (so they stay findable + un-bucketable). The needs_call
    branch inherits its exclusion from the shared _needs_call_filter (count==list).
    """
    query = db.query(Company).filter(Company.is_active.is_(True)).options(joinedload(Company.account_owner))

    if search.strip():
        sb = SearchBuilder(search.strip())
        query = query.filter(sb.ilike_filter(Company.name))

    # Bucket suppression at the query layer (never in materialize_all_clocks).
    # "bucket" facet reveals ONLY bucketed accounts; every other view hides them.
    if staleness == "bucket":
        query = query.filter(Company.disposition == "bucket")
    elif staleness != "needs_call":
        # needs_call already carries _not_bucketed() via _needs_call_filter.
        query = query.filter(_not_bucketed())

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
    if segment:
        query = query.filter(
            db.query(EntityTag)
            .filter(
                EntityTag.entity_type == "company",
                EntityTag.entity_id == Company.id,
                EntityTag.tag_id == segment,
                EntityTag.is_visible.is_(True),
            )
            .correlate(Company)
            .exists()
        )

    # Cadence clock sorts return a fully-ordered query directly (order_by_clock
    # calls query.order_by(...) internally), so we return early to avoid
    # double-applying .order_by() via the CDM_SORTS path.
    if sort == "outbound_asc":
        return order_by_clock(query, "outbound", now=now)
    if sort == "reply_asc":
        return order_by_clock(query, "reply", now=now)
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
    segment: int = 0,
    include_overdue: bool = False,
) -> dict:
    """Shared context for the CDM workspace shell and its account-list partial.

    include_overdue: adds the filter-bar-only context — overdue_count (the
    "needs a call" chip, an extra COUNT query) AND account_types (the type
    dropdown options). The account-list refresh route re-renders neither, so
    it omits both and skips the COUNT query.
    segment: when non-zero, filter companies carrying that segment tag_id.
    """
    from .tagging import list_all_segment_tags

    now = datetime.now(timezone.utc)
    query = cdm_company_query(
        db,
        user,
        search=search,
        staleness=staleness,
        account_type=account_type,
        my_only=my_only,
        sort=sort,
        now=now,
        segment=segment,
    )
    total = query.count()
    companies = query.offset(offset).limit(limit).all()
    for c in companies:
        c.staleness = staleness_tier(c.last_activity_at)
        c.cadence_state = cadence_state(c.tier, c.last_outbound_at, now)

    # Spotlight markers: accounts with new, unseen inbound customer comms (the owner's).
    from app.services.alerts import markers_for_tab
    from app.services.reporting_service import coverage_report

    ctx = {
        "companies": companies,
        "alert_markers": markers_for_tab(db, user, "crm"),
        "coverage": coverage_report(db),
        "search": search,
        "staleness": staleness,
        "account_type": account_type,
        "my_only": my_only,
        "sort": sort,
        "segment": segment,
        "total": total,
        "limit": limit,
        "offset": offset,
        "all_segment_tags": list_all_segment_tags(db),
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
            .order_by(
                SiteContact.is_archived.asc(),
                SiteContact.is_priority.desc(),
                SiteContact.is_primary.desc(),
                SiteContact.full_name,
            )
            .all()
        )
    rows = [{"contact": c, "site": site_map.get(c.customer_site_id), "legacy": False} for c in contacts]
    # Build a set of lowercased emails already covered by real SiteContacts so that
    # legacy site.contact_* rows with the same email are suppressed (dedup a migrated
    # site that now has a real SiteContact for the same person).
    real_emails: set[str] = {c.email.lower() for c in contacts if c.email}
    for s in sites:
        if s.contact_name or s.contact_email:
            legacy_email = (s.contact_email or "").strip().lower()
            if legacy_email and legacy_email in real_emails:
                # A real SiteContact already covers this address — suppress the legacy row.
                continue
            rows.append({"contact": None, "site": s, "legacy": True})
    return rows


# ═══════════════════════════════════════════════════════════════════════
#  COMMERCIAL STATS — company_commercial_stats, next_best_touch
# ═══════════════════════════════════════════════════════════════════════


def company_commercial_stats(db: Session, company_ids: list[int]) -> dict[int, dict]:
    """Compute win_rate, revenue_90d, last_req_date for a list of company IDs.

    Returns {company_id: {"win_rate": int|None, "revenue_90d": float, "last_req_date":
    str|None}}. win_rate is None when no decided (WON+LOST) reqs exist. revenue_90d is
    sum of Quote.subtotal on WON reqs in last 90 days. last_req_date is ISO string of
    the max Requisition.created_at across all sites.
    """
    if not company_ids:
        return {}

    # Initialise defaults so every requested id is present in the result
    result: dict[int, dict] = {
        cid: {"win_rate": None, "revenue_90d": 0.0, "last_req_date": None} for cid in company_ids
    }

    # Win-rate + last_req_date — single pass via CustomerSite → Requisition join
    stats_rows = (
        db.query(
            CustomerSite.company_id,
            func.count(sa_case((Requisition.status == RequisitionStatus.WON, 1))).label("won_count"),
            func.count(sa_case((Requisition.status.in_([RequisitionStatus.WON, RequisitionStatus.LOST]), 1))).label(
                "decided_count"
            ),
            func.max(Requisition.created_at).label("last_req_date"),
        )
        .join(Requisition, Requisition.customer_site_id == CustomerSite.id)
        .filter(CustomerSite.company_id.in_(company_ids))
        .group_by(CustomerSite.company_id)
        .all()
    )
    for row in stats_rows:
        wr = round(row.won_count / row.decided_count * 100) if row.decided_count > 0 else None
        result[row.company_id]["win_rate"] = wr
        result[row.company_id]["last_req_date"] = row.last_req_date.isoformat() if row.last_req_date else None

    # 90-day won revenue — CustomerSite → Requisition → Quote join
    rev_cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    rev_rows = (
        db.query(
            CustomerSite.company_id,
            func.coalesce(func.sum(Quote.subtotal), 0).label("revenue"),
        )
        .join(Requisition, Requisition.customer_site_id == CustomerSite.id)
        .join(Quote, Quote.requisition_id == Requisition.id)
        .filter(
            CustomerSite.company_id.in_(company_ids),
            Requisition.status == RequisitionStatus.WON,
            Quote.created_at >= rev_cutoff,
        )
        .group_by(CustomerSite.company_id)
        .all()
    )
    for row in rev_rows:
        result[row.company_id]["revenue_90d"] = float(row.revenue)

    return result


def next_best_touch(
    tier: str | None,
    last_outbound_at: datetime | None,
    now: datetime | None = None,
) -> str:
    """Human-readable next-best-touch guidance derived from cadence_state.

    "Never contacted — reach out"  (state=new) "Overdue — reach out now" (state=overdue)
    "Due for outreach"             (state=due) "On track" (state=on_target)
    """
    state = cadence_state(tier, last_outbound_at, now)
    return {
        "new": "Never contacted — reach out",
        "overdue": "Overdue — reach out now",
        "due": "Due for outreach",
        "on_target": "On track",
    }.get(state, "On track")
