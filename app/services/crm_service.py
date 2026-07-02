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

from sqlalchemy import and_, func, or_, select
from sqlalchemy import case as sa_case
from sqlalchemy.orm import Session, joinedload

from ..constants import RequisitionStatus
from ..dependencies import is_manager_or_admin
from ..models import AccountCollaborator, Company, CustomerSite, Quote, Requisition, SiteContact
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


def company_visibility_predicate(user: User):
    """Predicate restricting Company rows to those *user* (a rep) can manage: account-
    owner OR owner of at least one site OR named collaborator.

    Single source of truth for rep-scoped account visibility — used by
    cdm_company_query's my_only branch, cdm_overdue_count, and the global customer-
    contacts list. Callers must themselves skip it for managers/admins
    (is_manager_or_admin), who see everything.
    """
    site_company_ids = select(CustomerSite.company_id).where(CustomerSite.owner_id == user.id)
    collab_company_ids = select(AccountCollaborator.company_id).where(AccountCollaborator.user_id == user.id)
    return or_(
        Company.account_owner_id == user.id,
        Company.id.in_(site_company_ids),
        Company.id.in_(collab_company_ids),
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
    disposition: str | None = None,
    has_open_reqs: bool = False,
):
    """Build the filtered + sorted CDM account list query.

    staleness: "" (all) | "overdue" | "due_soon" | "recent" | "new" — one band each —
    or "needs_call" (overdue OR never contacted), the "needs a call" chip's filter,
    or "bucket" (the explicit Bucket facet — the ONLY way to surface bucketed accounts).

    Bucketed accounts (disposition='bucket') are hidden from the base query so they
    drop out of the call-list everywhere, EXCEPT when the "bucket" facet is
    explicitly requested (so they stay findable + un-bucketable). The needs_call
    branch inherits its exclusion from the shared _needs_call_filter (count==list).

    disposition: None/"" = default suppression behaviour (bucket hidden unless staleness="bucket");
    "active" = only non-bucket accounts; "bucket" = only bucket accounts (overrides staleness
    bucket-suppression so the caller can combine disposition="bucket" with any staleness band).

    has_open_reqs: when True, restrict to companies that have at least one requisition whose
    status is NOT in RequisitionStatus.TERMINAL (mirrors the open_req_count PG trigger).
    """
    query = db.query(Company).filter(Company.is_active.is_(True)).options(joinedload(Company.account_owner))

    if search.strip():
        sb = SearchBuilder(search.strip())
        query = query.filter(sb.ilike_filter(Company.name))

    # Disposition filter: explicit param takes full precedence over staleness-driven
    # bucket suppression so "Active only" and "Bucket only" can compose with any
    # staleness band. When disposition is unset, the legacy staleness-driven rules apply.
    if disposition == "bucket":
        query = query.filter(Company.disposition == "bucket")
    elif disposition == "active":
        query = query.filter(_not_bucketed())
    else:
        # Legacy bucket suppression: "bucket" staleness facet reveals only bucketed;
        # every other view (including needs_call) hides them.
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
        # Also exclude companies where they HAVE active sites but ALL of them are
        # marked do_not_contact (nothing to call).  Companies with no sites at all
        # still appear — the cadence obligation lives at the company level.
        active_site_exists, non_dnc_site_exists = _dnc_site_subqueries()
        # Exclude only when: there IS at least one active site AND none are reachable.
        query = query.filter(
            _needs_call_filter(now),
            or_(~active_site_exists, non_dnc_site_exists),
        )
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
    if my_only and not is_manager_or_admin(user):
        # Reps see accounts they own directly, where they own at least one site,
        # OR where they are a named collaborator (Phase 3: helper role).
        # Managers/admins always see everything — my_only is ignored for them.
        query = query.filter(company_visibility_predicate(user))
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
    if has_open_reqs:
        # Mirrors the open_req_count PG trigger: open = NOT in TERMINAL statuses.
        query = query.filter(
            db.query(Requisition)
            .filter(
                Requisition.company_id == Company.id,
                Requisition.status.notin_(RequisitionStatus.TERMINAL),
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


def _dnc_site_subqueries():
    """Return (active_site_exists, non_dnc_site_exists) correlated EXISTS subqueries.

    Shared by cdm_company_query (staleness="needs_call") and cdm_overdue_count so the
    two apply identical DNC-site suppression and count == list is guaranteed. A company
    with no sites at all passes the filter (the cadence obligation lives at the company
    level, not the site level).
    """
    active_site_exists = (
        select(CustomerSite.id)
        .where(CustomerSite.company_id == Company.id, CustomerSite.is_active.is_(True))
        .correlate(Company)
        .exists()
    )
    non_dnc_site_exists = (
        select(CustomerSite.id)
        .where(
            CustomerSite.company_id == Company.id,
            CustomerSite.is_active.is_(True),
            CustomerSite.do_not_contact.is_(False),
        )
        .correlate(Company)
        .exists()
    )
    return active_site_exists, non_dnc_site_exists


def cdm_overdue_count(db: Session, user: User, now: datetime | None = None) -> int:
    """Count this user's accounts needing a call (overdue or never contacted).

    Sales/trader only — others get 0 (no chip rendered). Uses the same
    _needs_call_filter predicate AND the same DNC-site filter as the chip's
    click-through query (staleness="needs_call") so count == list at all times.

    Mirrors the my_only visibility rule in cdm_company_query: rep sees accounts
    they own (account_owner_id) OR where they own a site.
    """
    if user.role not in ("sales", "trader"):
        return 0
    now = now or datetime.now(timezone.utc)
    active_site_exists, non_dnc_site_exists = _dnc_site_subqueries()
    return (
        db.query(func.count(Company.id))
        .filter(
            Company.is_active.is_(True),
            company_visibility_predicate(user),
            _needs_call_filter(now),
            # Mirror cdm_company_query(staleness="needs_call"): exclude companies
            # where every active site is DNC (nothing to call).  Companies with no
            # active sites at all are kept — cadence obligation is at company level.
            or_(~active_site_exists, non_dnc_site_exists),
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
    disposition: str | None = None,
    has_open_reqs: bool = False,
    include_overdue: bool = False,
    include_users: bool = False,
) -> dict:
    """Shared context for the CDM workspace shell and its account-list partial.

    include_overdue: adds the filter-bar-only context — overdue_count (the
    "needs a call" chip, an extra COUNT query) AND account_types (the type
    dropdown options). The account-list refresh route re-renders neither, so
    it omits both and skips the COUNT query.
    include_users: adds "users" — the active-user list (name-sorted) that backs
    the bulk "Assign owner" <select>. Carried ONLY for managers/admins (the
    bulk assign-owner action is manager/admin-only server-side); reps omit it.
    segment: when non-zero, filter companies carrying that segment tag_id.
    disposition: None/"" = default; "active" = active only; "bucket" = bucket only.
    has_open_reqs: when True, restrict to companies with at least one open requisition.
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
        disposition=disposition,
        has_open_reqs=has_open_reqs,
    )
    total = query.count()
    companies = query.offset(offset).limit(limit).all()
    for c in companies:
        c.staleness = staleness_tier(c.last_activity_at)
        c.cadence_state = cadence_state(c.tier, c.last_outbound_at, now)

    # When a search term is provided, also surface archived (DNC) companies that match.
    archived_search_results: list[Company] = []
    if search.strip():
        archived_search_results = (
            db.query(Company)
            .filter(Company.is_active.is_(False))
            .filter(SearchBuilder(search.strip()).ilike_filter(Company.name))
            .order_by(Company.name)
            .limit(10)
            .all()
        )

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
        "disposition": disposition or "",
        "has_open_reqs": has_open_reqs,
        "total": total,
        "limit": limit,
        "offset": offset,
        "all_segment_tags": list_all_segment_tags(db),
        "archived_search_results": archived_search_results,
    }
    if include_overdue:
        ctx["overdue_count"] = cdm_overdue_count(db, user, now=now)
        ctx["account_types"] = CDM_ACCOUNT_TYPES
    if include_users:
        ctx["users"] = db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all()
    return ctx


# Same keys as the cadence-dot map in the customers _account_list / vendor list.
CONTACT_CADENCE_DOTS = ("new", "on_target", "due", "overdue")


def customer_contacts_query(
    db: Session,
    user: User,
    *,
    search: str = "",
    company_id: int = 0,
    contact_role: str = "",
):
    """Cross-company customer-contacts query, role-scoped + filtered.

    Joins SiteContact → CustomerSite → Company and restricts to active rows.
    SALES/TRADER reps see ONLY contacts in companies they can manage (the shared
    company_visibility_predicate — account-owner OR site-owner OR collaborator);
    MANAGER/ADMIN see all. This is the cross-tenant-PII gate for /v2/contacts.

    Filters: search (name OR email), company_id, contact_role. cadence_state is a
    derived value (not a column) so it is filtered in Python by the caller, not here.
    Ordered newest-activity-first to surface the most recently touched contacts.
    """
    query = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .join(Company, CustomerSite.company_id == Company.id)
        .filter(
            SiteContact.is_active.is_(True),
            CustomerSite.is_active.is_(True),
            Company.is_active.is_(True),
        )
        .options(joinedload(SiteContact.customer_site).joinedload(CustomerSite.company))
    )
    if not is_manager_or_admin(user):
        query = query.filter(company_visibility_predicate(user))
    if search.strip():
        sb = SearchBuilder(search.strip())
        query = query.filter(or_(sb.ilike_filter(SiteContact.full_name), sb.ilike_filter(SiteContact.email)))
    if company_id:
        query = query.filter(Company.id == company_id)
    if contact_role:
        query = query.filter(SiteContact.contact_role == contact_role)
    return query.order_by(SiteContact.last_activity_at.desc().nullslast(), SiteContact.id.desc())


def customer_contacts_list_ctx(
    db: Session,
    user: User,
    *,
    search: str = "",
    company_id: int = 0,
    contact_role: str = "",
    cadence_state: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Template context for the global customer-contacts list (/v2/contacts).

    Applies the role-scoped query, derives each contact's cadence_state, then
    (optionally) filters on it in Python — cadence_state is computed, not stored. The
    company dropdown is built from the same visibility scope so a rep can only filter
    within accounts they can see.
    """
    now = datetime.now(timezone.utc)
    base = customer_contacts_query(db, user, search=search, company_id=company_id, contact_role=contact_role)

    if cadence_state in CONTACT_CADENCE_DOTS:
        # cadence_state is derived from last_outbound_at, but its integer day-floor
        # thresholds collapse to exact timestamp cutoffs, so the filter (and therefore
        # count + paging) runs in SQL rather than loading the whole scoped set into
        # Python. contact_cadence_predicate mirrors cadence_state_of EXACTLY. (PERF-10)
        base = base.filter(contact_cadence_predicate(cadence_state, now))
    total = base.count()
    contacts = base.offset(offset).limit(limit).all()
    for c in contacts:
        c.cadence_state = cadence_state_of(c, now)

    # Company filter options: distinct companies within the viewer's scope.
    company_q = (
        db.query(Company.id, Company.name)
        .join(CustomerSite, CustomerSite.company_id == Company.id)
        .filter(Company.is_active.is_(True), CustomerSite.is_active.is_(True))
    )
    if not is_manager_or_admin(user):
        company_q = company_q.filter(company_visibility_predicate(user))
    companies = company_q.distinct().order_by(Company.name).all()

    return {
        "contacts": contacts,
        "companies": companies,
        "search": search,
        "company_id": company_id,
        "contact_role": contact_role,
        "cadence_state": cadence_state,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def cadence_state_of(contact: SiteContact, now: datetime) -> str:
    """Contact-level cadence dot — standard 30d outbound clock (tier=None)."""
    return cadence_state(None, contact.last_outbound_at, now)


def contact_cadence_predicate(state: str, now: datetime):
    """SQL predicate selecting SiteContacts whose contact-level cadence_state ==
    ``state``.

    Mirrors ``cadence_state_of()`` → ``cadence_state(tier=None, last_outbound_at, now)``
    EXACTLY so the filter (and thus count + LIMIT/OFFSET paging) can run in the database
    instead of loading the whole scoped contact set into Python (PERF-10). cadence_state
    derives from the OUTBOUND clock with integer day-floor thresholds:

        new        last_outbound_at IS NULL
        overdue    floor(days) > CADENCE_RED_DAYS
        due        target < floor(days) <= CADENCE_RED_DAYS   (empty when target == red)
        on_target  floor(days) <= target                       (and not NULL)

    where ``days == (now - last_outbound_at).days`` and ``target`` is the "standard" tier
    target (tier is always None for contacts). The day-FLOOR comparison collapses to an
    exact timestamp cutoff — ``floor(days) > k  ⇔  (now - ts) >= (k+1) days  ⇔
    ts <= now - (k+1) days`` — so no SQL date-diff is needed and the comparison is portable
    across SQLite (tests) and PostgreSQL (prod), matching the existing _needs_call_filter
    pattern. NULL rows are excluded from the timestamp bands explicitly (they are "new").
    """
    col = SiteContact.last_outbound_at
    if state == "new":
        return col.is_(None)
    target = TIER_TARGET_DAYS.get("standard", CADENCE_RED_DAYS)
    overdue_cutoff = now - timedelta(days=CADENCE_RED_DAYS + 1)  # ts <= this ⇒ overdue
    due_cutoff = now - timedelta(days=target + 1)  # ts <= this ⇒ due-or-worse
    if state == "overdue":
        return and_(col.isnot(None), col <= overdue_cutoff)
    if state == "due":
        # Collapses to an empty set when target == CADENCE_RED_DAYS (the current config),
        # exactly matching cadence_state's unreachable "due" band for tier=None contacts.
        return and_(col.isnot(None), col <= due_cutoff, col > overdue_cutoff)
    # on_target — the only remaining CONTACT_CADENCE_DOTS value.
    return and_(col.isnot(None), col > due_cutoff)


def company_contact_rows(
    db: Session,
    company_id: int,
    sites: list[CustomerSite] | None = None,
    viewer: User | None = None,
) -> list[dict]:
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

    viewer (optional): When set, applies the Phase 2b site-scope rule:
      - manager/admin → all sites
      - account owner (company.account_owner_id == viewer.id) → all sites
      - else → only sites where CustomerSite.owner_id == viewer.id
    viewer=None retains legacy behaviour (no scoping).
    """
    if sites is None:
        sites = (
            db.query(CustomerSite).filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True)).all()
        )

    if viewer is not None:
        # Resolve account_owner_id for this company without an extra round-trip when
        # sites is pre-loaded; fall back to a targeted scalar if not available.
        if sites and hasattr(sites[0], "company") and sites[0].company is not None:
            account_owner_id = sites[0].company.account_owner_id
        else:
            account_owner_id = db.scalar(select(Company.account_owner_id).where(Company.id == company_id))

        sees_all = is_manager_or_admin(viewer) or account_owner_id == viewer.id
        if not sees_all:
            # Sites with no owner (owner_id=None) are accessible to all viewers.
            # Sites with an explicit owner are restricted to that owner.
            sites = [s for s in sites if s.owner_id is None or s.owner_id == viewer.id]
    site_map = {s.id: s for s in sites}
    contacts: list[SiteContact] = []
    if site_map:
        contacts = (
            db.query(SiteContact)
            .filter(SiteContact.customer_site_id.in_(list(site_map)), SiteContact.is_active.isnot(False))
            .order_by(
                SiteContact.is_archived.asc(),
                SiteContact.is_priority.desc(),
                SiteContact.is_primary.desc(),
                SiteContact.full_name,
            )
            .all()
        )
    now_utc = datetime.now(timezone.utc)
    recent_notes = _latest_contact_notes(db, [c.id for c in contacts])
    rows = [
        {
            "contact": c,
            "site": site_map.get(c.customer_site_id),
            "legacy": False,
            "cadence": cadence_state(None, c.last_outbound_at, now_utc),
            "recent_note": recent_notes.get(c.id),
        }
        for c in contacts
    ]
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
            rows.append({"contact": None, "site": s, "legacy": True, "cadence": "new", "recent_note": None})
    return rows


def _latest_contact_notes(db: Session, contact_ids: list[int]) -> dict[int, str]:
    """Return {site_contact_id: latest manual-note text} for the given contacts.

    One batched query (no N+1): picks each contact's newest ActivityLog NOTE so the
    contact-row drawer can show a recent-note preview without a per-row round-trip. The
    full note feed lives behind the notes modal (get_site_contact_notes).
    """
    if not contact_ids:
        return {}
    from ..models.intelligence import ActivityLog

    rows = (
        db.query(ActivityLog.site_contact_id, ActivityLog.notes, ActivityLog.created_at)
        .filter(
            ActivityLog.site_contact_id.in_(contact_ids),
            ActivityLog.activity_type == "note",
            ActivityLog.notes.isnot(None),
        )
        .order_by(ActivityLog.site_contact_id, ActivityLog.created_at.desc())
        .all()
    )
    latest: dict[int, str] = {}
    for site_contact_id, notes, _created in rows:
        # Rows are ordered newest-first per contact; keep the first seen for each.
        if site_contact_id not in latest and notes:
            latest[site_contact_id] = notes
    return latest


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
