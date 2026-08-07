"""Vendor list partial, CSV export, and the global vendor-contacts list.

W4.8 split of the 1,475-line app/routers/htmx/vendors.py — pure structural move: URLs
and behavior unchanged; every route attaches to the shared router imported from .common
(registration assembled in __init__).
"""

from datetime import UTC, datetime

from fastapi import Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func as sqlfunc
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from ....constants import AccessKey
from ....database import get_db
from ....dependencies import require_access, require_user
from ....models import User, VendorCard
from ....models.vendors import VendorContact
from ....services.crm_service import cadence_state as _cadence_state
from ....services.crm_service import order_by_clock as _order_by_clock
from ....template_env import template_response
from ....utils.csv_export import stream_csv
from ....utils.search_builder import SearchBuilder
from ....utils.sql_helpers import escape_like
from .._shared import _base_ctx, _sanitize_hx_params
from .common import router

# ── Vendor partials ─────────────────────────────────────────────────────


def build_vendor_list_query(
    db: Session,
    user: User,
    *,
    q: str = "",
    hide_blacklisted: bool = True,
    include_archived: bool = False,
    my_only: bool = False,
):
    """Filtered (unordered, unpaginated) VendorCard query shared by the list partial and
    the CSV export.

    Applies the EXACT filter predicates the vendor list uses — the "My Vendors"
    (StrategicVendor) scoping, the hide-blacklisted default, the soft-archive
    (is_active) filter, and the name/domain/brand-tag/commodity-tag search. It layers on
    NO ordering, pagination, or eager loads, so each caller adds those; this is the
    single source of truth for "what the list matches" so the export can never drift
    from the on-screen list.
    """
    from ....models.strategic import StrategicVendor

    query = db.query(VendorCard)

    # Filter to user's strategic vendors if "My Vendors" tab is active
    if my_only:
        my_vendor_ids = (
            db.query(StrategicVendor.vendor_card_id)
            .filter(StrategicVendor.user_id == user.id, StrategicVendor.released_at.is_(None))
            .subquery()
        )
        query = query.filter(VendorCard.id.in_(my_vendor_ids))

    if hide_blacklisted:
        query = query.filter(VendorCard.is_blacklisted.is_(False))

    # Soft-archive: archived vendors are hidden from the default list (mirrors the
    # customer/company is_active archive). "Show archived" lifts the filter.
    if not include_archived:
        query = query.filter(VendorCard.is_active.is_(True))

    if q.strip():
        from sqlalchemy import Text, cast

        sb = SearchBuilder(q.strip())
        term = f"%{escape_like(q.strip())}%"
        query = query.filter(
            or_(
                sb.ilike_filter(VendorCard.display_name, VendorCard.domain),
                cast(VendorCard.brand_tags, Text).ilike(term, escape="\\"),
                cast(VendorCard.commodity_tags, Text).ilike(term, escape="\\"),
            )
        )

    return query


@router.get("/v2/partials/vendors", response_class=HTMLResponse)
async def vendors_list_partial(
    request: Request,
    q: str = "",
    hide_blacklisted: bool = True,
    include_archived: bool = False,
    sort: str = "sighting_count",
    dir: str = "desc",
    my_only: bool = False,
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    hx_target: str = Query("#main-content", alias="hx_target"),
    push_url_base: str = Query("/v2/vendors", alias="push_url_base"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return vendor list as HTML partial with blacklisted toggle and sorting."""
    hx_target, push_url_base = _sanitize_hx_params(hx_target, push_url_base, "/v2/vendors")

    query = build_vendor_list_query(
        db,
        user,
        q=q,
        hide_blacklisted=hide_blacklisted,
        include_archived=include_archived,
        my_only=my_only,
    )

    total = query.count()

    # Sorting — outbound_asc uses the generalized order_by_clock (VendorCard clocks)
    now_utc = datetime.now(UTC)
    if sort == "outbound_asc":
        vendors = _order_by_clock(query, "outbound", model=VendorCard).offset(offset).limit(limit).all()
    else:
        sort_col_map = {
            "display_name": VendorCard.display_name,
            "sighting_count": VendorCard.sighting_count,
            "overall_win_rate": VendorCard.overall_win_rate,
            "hq_country": VendorCard.hq_country,
            "industry": VendorCard.industry,
        }
        sort_col = sort_col_map.get(sort, VendorCard.sighting_count)
        order = sort_col.desc().nullslast() if dir == "desc" else sort_col.asc().nullslast()
        vendors = query.order_by(order).offset(offset).limit(limit).all()

    # Attach cadence_state to each vendor (tier=None → standard/30d target)
    for v in vendors:
        v.cadence_state = _cadence_state(None, v.last_outbound_at, now_utc)

    ctx = _base_ctx(request, user, "vendors")
    ctx.update(
        {
            "vendors": vendors,
            "user": user,  # kept in ctx; the toolbar Export CSV button it once gated is gone (ISS-028 — bulk export lives on the capability-gated Settings ▸ Data export tab)
            "q": q,
            "hide_blacklisted": hide_blacklisted,
            "include_archived": include_archived,
            "sort": sort,
            "dir": dir,
            "total": total,
            "limit": limit,
            "offset": offset,
            "my_only": my_only,
            "hx_target": hx_target,
            "push_url_base": push_url_base,
            "now_utc": now_utc,
        }
    )
    return template_response("htmx/partials/vendors/list.html", ctx)


# CSV export column order for the vendor list — the REAL VendorCard fields. There is no
# broker/vendor "type" column on VendorCard, so "Source" carries the real provenance field
# (v.source). Header row written first, then one row per matching vendor (no pagination).
_VENDOR_EXPORT_COLUMNS = [
    "Vendor",
    "Domain",
    "Website",
    "Source",
    "Blacklisted",
    "Active",
    "Commodity Tags",
    "Contacts",
    "Created",
]

# Plain-column sort whitelist for the export (mirrors the list's sortable columns; the
# list's "outbound_asc" clock sort falls back to Sightings here since ordering is cosmetic
# and filter parity is what matters).
_VENDOR_EXPORT_SORT_COLUMNS = {
    "display_name": VendorCard.display_name,
    "sighting_count": VendorCard.sighting_count,
    "overall_win_rate": VendorCard.overall_win_rate,
    "hq_country": VendorCard.hq_country,
    "industry": VendorCard.industry,
}


def _vendor_export_rows(
    db: Session,
    user: User,
    *,
    q: str,
    hide_blacklisted: bool,
    include_archived: bool,
    my_only: bool,
    sort: str,
    direction: str,
):
    """Yield one CSV row per vendor matching the list's filters (no pagination).

    Reuses build_vendor_list_query so the exported set is exactly the filtered list. A
    correlated scalar subquery supplies the contact count so the vendor_contacts
    collection never has to be loaded, keeping the streamed export memory-flat via
    yield_per.
    """
    contact_count_sub = (
        select(sqlfunc.count(VendorContact.id))
        .where(VendorContact.vendor_card_id == VendorCard.id)
        .correlate(VendorCard)
        .scalar_subquery()
    )
    query = build_vendor_list_query(
        db,
        user,
        q=q,
        hide_blacklisted=hide_blacklisted,
        include_archived=include_archived,
        my_only=my_only,
    )

    sort_col = _VENDOR_EXPORT_SORT_COLUMNS.get(sort, VendorCard.sighting_count)
    order = sort_col.desc().nullslast() if direction == "desc" else sort_col.asc().nullslast()

    rows = query.add_columns(contact_count_sub.label("contact_count")).order_by(order).yield_per(500)
    for v, contact_count in rows:
        yield (
            v.display_name,
            v.domain or "",
            v.website or "",
            v.source or "",
            "Yes" if v.is_blacklisted else "No",
            "Yes" if v.is_active else "No",
            "; ".join(str(t) for t in (v.commodity_tags or [])),
            contact_count,
            v.created_at.strftime("%Y-%m-%d %H:%M") if v.created_at else "",
        )


@router.get("/v2/partials/vendors/export")
async def vendors_export(
    q: str = "",
    hide_blacklisted: bool = True,
    include_archived: bool = False,
    my_only: bool = False,
    sort: str = "sighting_count",
    dir: str = "desc",
    user: User = Depends(require_access(AccessKey.EXPORT_BULK_DATA)),
    db: Session = Depends(get_db),
):
    """Stream every list-matching vendor as a CSV download (attachment, no pagination).

    Gated on EXPORT_BULK_DATA (ISS-028 bulk dataset export lockdown — admin-by-default,
    per-user override possible). Same filter params as the list route
    (GET /v2/partials/vendors) — the export mirrors the list's active view
    (search/hide-blacklisted/show-archived/my-vendors).
    """
    return stream_csv(
        "vendors_export.csv",
        _VENDOR_EXPORT_COLUMNS,
        _vendor_export_rows(
            db,
            user,
            q=q,
            hide_blacklisted=hide_blacklisted,
            include_archived=include_archived,
            my_only=my_only,
            sort=sort,
            direction=dir,
        ),
    )


# ── Global vendor-contacts list ────────────────────────────────────────────
# View-open (require_user) — vendor data is not tenant-scoped, mirroring the
# /api/vendor-contacts/bulk endpoint this surfaces. Search/sort/paginate over all
# structured VendorContacts (blacklisted vendors excluded, as in the bulk route).


@router.get("/v2/partials/vendor-contacts", response_class=HTMLResponse)
async def vendor_contacts_partial(
    request: Request,
    search: str = "",
    sort: str = "name",
    dir: str = "asc",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the global vendor-contacts list as an HTML partial."""
    from ....models import VendorCard

    query = (
        db.query(VendorContact)
        .join(VendorCard, VendorContact.vendor_card_id == VendorCard.id)
        .filter(VendorCard.is_blacklisted.is_(False), VendorCard.is_active.is_(True))
        .options(joinedload(VendorContact.vendor_card))
    )
    if search.strip():
        sb = SearchBuilder(search.strip())
        query = query.filter(
            or_(
                sb.ilike_filter(VendorContact.full_name, VendorContact.email),
                sb.ilike_filter(VendorCard.display_name),
            )
        )

    # "score" (relationship_score) sort removed in the Wave 2 sweep — the
    # contact-intelligence layer that computed it is deleted (spec §5.4).
    sort_col_map = {
        "name": VendorContact.full_name,
        "email": VendorContact.email,
        "vendor": VendorCard.display_name,
    }
    sort_col = sort_col_map.get(sort, VendorContact.full_name)
    order = sort_col.desc().nullslast() if dir == "desc" else sort_col.asc().nullslast()

    total = query.count()
    contacts = query.order_by(order, VendorContact.id).offset(offset).limit(limit).all()

    ctx = _base_ctx(request, user, "crm")
    ctx.update(
        {
            "contacts": contacts,
            "search": search,
            "sort": sort,
            "dir": dir,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )
    return template_response("htmx/partials/vendors/contacts_list.html", ctx)
