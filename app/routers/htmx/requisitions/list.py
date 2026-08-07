"""Requisitions list partial (filters/sorting/grouping) + streamed CSV export.

W4.8 split of the 1,473-line app/routers/htmx/requisitions.py — pure structural
move: URLs and behavior unchanged; every route attaches to the shared router
imported from .common (registration assembled in __init__).
"""

from datetime import datetime
from typing import Literal

from fastapi import Depends, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import case, exists, or_, select
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload, selectinload

from ....constants import (
    RESTRICTED_ROLES,
    AccessKey,
)
from ....database import get_db
from ....dependencies import require_access, require_user
from ....models import (
    Offer,
    Quote,
    QuoteRequisition,
    Requirement,
    Requisition,
    User,
)
from ....template_env import template_response
from ....utils.csv_export import stream_csv
from ....utils.search_builder import SearchBuilder
from .._shared import _base_ctx
from .common import router

# Quote-status significance for the list's aggregate Quotes column — lower wins
# (won > lost > sent > revised > everything else).
_QUOTE_STATUS_PRIORITY = {"won": 1, "lost": 2, "sent": 3, "revised": 4}


def _best_quote_status(quotes) -> str | None:
    """The most significant quote status across a requisition's quotes, or None if it
    has none — the value shown in the list's Quotes column."""
    if not quotes:
        return None
    status: str | None = min(quotes, key=lambda qt: _QUOTE_STATUS_PRIORITY.get(qt.status, 5)).status
    return status


def build_requisition_list_query(
    db: Session,
    user: User,
    *,
    q: str = "",
    status: str = "",
    owner: int = 0,
    urgency: str = "",
    date_from: str = "",
    date_to: str = "",
):
    """Filtered (unordered, unpaginated) Requisition query shared by the list partial
    and the CSV export.

    Applies the EXACT filter predicates the requisitions list uses — the ``is_scratch``
    exclusion, search (name/customer + a part-number EXISTS subquery), status, owner,
    urgency, created-at date range — plus the restricted-role ownership boundary
    (sales/trader see only their own). It layers on NO ordering, pagination, or eager
    loads, so each caller adds those; this is the single source of truth for "what the
    list matches" so the export can never drift from the on-screen list.
    """
    query = db.query(Requisition).filter(Requisition.is_scratch.is_(False))

    search_term = q.strip()
    if search_term:
        sb = SearchBuilder(search_term)
        safe = f"%{sb.safe}%"
        mpn_match = exists(
            select(Requirement.id).where(
                Requirement.requisition_id == Requisition.id,
                or_(
                    Requirement.primary_mpn.ilike(safe, escape="\\"),
                    Requirement.customer_pn.ilike(safe, escape="\\"),
                    Requirement.substitutes_text.ilike(safe, escape="\\"),
                ),
            )
        )
        query = query.filter(
            or_(
                sb.ilike_filter(Requisition.name, Requisition.customer_name),
                mpn_match,
            )
        )
    if status:
        query = query.filter(Requisition.status == status)
    if owner:
        query = query.filter(Requisition.created_by == owner)
    if urgency:
        query = query.filter(Requisition.urgency == urgency)
    if date_from:
        try:
            query = query.filter(Requisition.created_at >= datetime.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            query = query.filter(Requisition.created_at <= datetime.fromisoformat(date_to))
        except ValueError:
            pass

    # Restricted roles (sales/trader) only see their own
    if user.role in RESTRICTED_ROLES:
        query = query.filter(Requisition.created_by == user.id)

    return query


# ── Requisition partials ────────────────────────────────────────────────


@router.get("/v2/partials/requisitions", response_class=HTMLResponse)
async def requisitions_list_partial(
    request: Request,
    q: str = "",
    status: str = "",
    owner: int = Query(0, ge=0),
    urgency: str = "",
    date_from: str = "",
    date_to: str = "",
    sort: str = "created_at",
    sort_dir: Literal["asc", "desc"] = Query("desc", alias="dir"),
    group_by: str = "",
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return requisitions list as HTML partial with filters and sorting.

    ``group_by='customer'`` renders a 2-level nested tree (Customer → Requisition →
    requirement lines) built server-side over the current page's rows; any other value
    is the default flat list.
    """
    query = build_requisition_list_query(
        db,
        user,
        q=q,
        status=status,
        owner=owner,
        urgency=urgency,
        date_from=date_from,
        date_to=date_to,
    ).options(
        joinedload(Requisition.creator),
        # selectinload (not joinedload) for the three collections: stacking
        # collection joinedloads multiplies the base query's rows per requisition
        # (requirements × offers × quotes cartesian before entity dedup).
        selectinload(Requisition.requirements),
        selectinload(Requisition.offers),
        selectinload(Requisition.quotes),
    )

    # Retained for the per-row match-reason/scope logic below.
    search_term = q.strip()

    total = query.count()

    # Sorting — whitelist of sortable columns, including subqueries for computed counts
    req_count_sub = (
        select(sqlfunc.count(Requirement.id))
        .where(Requirement.requisition_id == Requisition.id)
        .correlate(Requisition)
        .scalar_subquery()
        .label("req_count_sort")
    )
    offer_count_sub = (
        select(sqlfunc.count(Offer.id))
        .where(Offer.requisition_id == Requisition.id)
        .correlate(Requisition)
        .scalar_subquery()
        .label("offer_count_sort")
    )
    # ASAP sorts before all dates (most urgent); nullslast() handles NULLs
    deadline_sort = case(
        (Requisition.deadline == "ASAP", "0000-00-00"),
        else_=Requisition.deadline,
    )
    # Aggregate quote significance (asc = won first), mirroring _best_quote_status; a
    # requisition with no quotes yields NULL → nullslast puts it at the bottom either way.
    quote_status_sub = (
        select(
            sqlfunc.min(
                case(
                    *[(Quote.status == status, prio) for status, prio in _QUOTE_STATUS_PRIORITY.items()],
                    else_=5,
                )
            )
        )
        # Correlate through the join table (not Quote.requisition_id) so a combined quote
        # counts for every contributing requisition's Quotes-column sort, not just its anchor.
        .select_from(QuoteRequisition)
        .join(Quote, Quote.id == QuoteRequisition.quote_id)
        .where(QuoteRequisition.requisition_id == Requisition.id)
        .correlate(Requisition)
        .scalar_subquery()
        .label("quote_status_sort")
    )
    sort_col_map = {
        "name": Requisition.name,
        "customer_name": Requisition.customer_name,
        "status": Requisition.status,
        "urgency": Requisition.urgency,
        "created_at": Requisition.created_at,
        "deadline": deadline_sort,
        "updated_at": Requisition.updated_at,
        "req_count": req_count_sub,
        "offer_count": offer_count_sub,
        "quote_status": quote_status_sub,
    }
    sort_col = sort_col_map.get(sort)
    if sort_col is None:
        logger.warning("Unknown sort key '{}', falling back to created_at", sort)
        sort_col = Requisition.created_at
        sort = "created_at"
    # nullslast: NULLs always sort to the bottom regardless of direction
    order = sort_col.desc().nullslast() if sort_dir == "desc" else sort_col.asc().nullslast()
    reqs = query.order_by(order).offset(offset).limit(limit).all()

    # Quotes contributing to each requisition on THIS page, via the join table — ONE extra
    # query for the whole page (not per row) so a combined quote's status shows on every
    # contributing requisition, not just the one it anchors (which req.quotes would miss).
    page_req_ids = [r.id for r in reqs]
    quotes_by_req: dict[int, list] = {}
    if page_req_ids:
        for rid, qt in (
            db.query(QuoteRequisition.requisition_id, Quote)
            .join(Quote, Quote.id == QuoteRequisition.quote_id)
            .filter(QuoteRequisition.requisition_id.in_(page_req_ids))
            .all()
        ):
            quotes_by_req.setdefault(rid, []).append(qt)

    # Attach counts + match reason when searching
    for req in reqs:
        req.req_count = len(req.requirements) if req.requirements else 0
        req.offer_count = len(req.offers) if req.offers else 0
        # Aggregate quote status for the list's Quotes column — the most significant of the
        # req's contributing quotes (won > lost > sent > revised > other), per
        # _best_quote_status / _QUOTE_STATUS_PRIORITY above. None → the column shows a dash.
        req.quote_status = _best_quote_status(quotes_by_req.get(req.id, []))
        req.match_reason = None
        req.matched_mpn = None
        if search_term:
            term_lower = search_term.lower()
            if req.name and term_lower in req.name.lower():
                req.match_reason = "name"
            elif req.customer_name and term_lower in req.customer_name.lower():
                req.match_reason = "customer"
            else:
                matched_mpn = next(
                    (
                        r.primary_mpn
                        for r in (req.requirements or [])
                        if (r.primary_mpn and term_lower in r.primary_mpn.lower())
                        or (r.customer_pn and term_lower in r.customer_pn.lower())
                    ),
                    None,
                )
                if matched_mpn:
                    req.match_reason = "part"
                    req.matched_mpn = matched_mpn

    # Match stats for search scope indicators
    match_counts = None
    if search_term:
        match_counts = {"name": 0, "customer": 0, "part": 0}
        for req in reqs:
            reason = req.match_reason
            if reason and reason in match_counts:
                match_counts[reason] += 1

    # Fetch team users for owner dropdown (unrestricted roles only)
    users = []
    if user.role not in RESTRICTED_ROLES:
        users = db.query(User).order_by(User.name).all()

    # Nested Customer → Requisition → requirement-line tree for the "By Customer" view.
    # Grouping is scoped to the CURRENT PAGE's requisitions (same page-scoped behaviour as
    # the sightings board): pagination limits the rows, then we group what's on the page.
    # Ownership/authz is inherited — `reqs` is already filtered by the RESTRICTED_ROLES
    # clause above, so restricted users only ever group their own requisitions.
    customer_groups = None
    if group_by == "customer":
        grouped: dict[str, list[Requisition]] = {}
        for req in reqs:
            customer = (req.customer_name or "").strip() or "Unknown customer"
            grouped.setdefault(customer, []).append(req)
        customer_groups = [
            {
                "customer": customer,
                "requisitions": [{"req": r, "requirements": list(r.requirements or [])} for r in group_reqs],
            }
            for customer, group_reqs in grouped.items()
        ]

    from ....services.activity_service import get_inbox_sync_status
    from ....services.requisition_state import attach_display_status

    # W3.3: one batch query set per page — badges show the derived pipeline stage.
    attach_display_status(db, reqs)

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requisitions": reqs,
            "q": q,
            "match_counts": match_counts,
            "status": status,
            "owner": owner,
            "urgency": urgency,
            "date_from": date_from,
            "date_to": date_to,
            "sort": sort,
            "dir": sort_dir,
            "group_by": group_by,
            "customer_groups": customer_groups,
            "total": total,
            "limit": limit,
            "offset": offset,
            "users": users,
            "user": user,  # req_row kebab gates Claim/Unclaim on `user` — omitting it hid them
            "user_role": user.role,
            "inbox_status": get_inbox_sync_status(db, user),
        }
    )
    return template_response("htmx/partials/requisitions/list.html", ctx)


# CSV export column order for the requisitions list — the REAL Requisition fields. Header
# row written first, then one row per matching requisition (no pagination).
_REQ_EXPORT_COLUMNS = [
    "Name",
    "Customer",
    "Status",
    "Owner",
    "Value",
    "Deadline",
    "Created",
    "# Requirements",
]

# Plain-column sort whitelist for the export (mirrors the list's sortable columns; the
# list's computed subquery sorts — req_count/offer_count/quote_status — fall back to
# Created here since ordering is cosmetic and filter parity is what matters).
_REQ_EXPORT_SORT_COLUMNS = {
    "name": Requisition.name,
    "customer_name": Requisition.customer_name,
    "status": Requisition.status,
    "urgency": Requisition.urgency,
    "created_at": Requisition.created_at,
    "updated_at": Requisition.updated_at,
    "deadline": Requisition.deadline,
}


def _requisition_export_rows(
    db: Session,
    user: User,
    *,
    q: str,
    status: str,
    owner: int,
    urgency: str,
    date_from: str,
    date_to: str,
    sort: str,
    sort_dir: str,
):
    """Yield one CSV row per requisition matching the list's filters (no pagination).

    Reuses build_requisition_list_query so the exported set is exactly the filtered
    list. A correlated scalar subquery supplies the requirement count so the
    requirements collection never has to be loaded, keeping the streamed export memory-
    flat via yield_per; claimed_by (Owner) is eager-loaded as a many-to-one join.
    """
    req_count_sub = (
        select(sqlfunc.count(Requirement.id))
        .where(Requirement.requisition_id == Requisition.id)
        .correlate(Requisition)
        .scalar_subquery()
    )
    query = build_requisition_list_query(
        db,
        user,
        q=q,
        status=status,
        owner=owner,
        urgency=urgency,
        date_from=date_from,
        date_to=date_to,
    ).options(joinedload(Requisition.claimed_by))

    sort_col = _REQ_EXPORT_SORT_COLUMNS.get(sort, Requisition.created_at)
    order = sort_col.desc().nullslast() if sort_dir == "desc" else sort_col.asc().nullslast()

    from ....services.requisition_state import derived_status_map

    def _flush(chunk: list) -> object:
        # W3.3: derive the pipeline stage per chunk (matches yield_per) so the
        # Status column shows what the list shows without an N+1 per row.
        stage_map = derived_status_map(db, [r.id for r, _ in chunk if (r.status or "open") == "open"])
        for req, req_count in chunk:
            yield (
                req.name,
                req.customer_name or "",
                stage_map.get(req.id, req.status or ""),
                req.claimed_by.name if req.claimed_by else "",
                req.opportunity_value if req.opportunity_value is not None else "",
                req.deadline or "",
                req.created_at.strftime("%Y-%m-%d %H:%M") if req.created_at else "",
                req_count,
            )

    rows = query.add_columns(req_count_sub.label("req_count")).order_by(order).yield_per(500)
    chunk: list = []
    for req, req_count in rows:
        chunk.append((req, req_count))
        if len(chunk) >= 500:
            yield from _flush(chunk)
            chunk = []
    if chunk:
        yield from _flush(chunk)


@router.get("/v2/partials/requisitions/export")
async def requisitions_export(
    q: str = "",
    status: str = "",
    owner: int = Query(0, ge=0),
    urgency: str = "",
    date_from: str = "",
    date_to: str = "",
    sort: str = "created_at",
    sort_dir: Literal["asc", "desc"] = Query("desc", alias="dir"),
    user: User = Depends(require_access(AccessKey.EXPORT_BULK_DATA)),
    db: Session = Depends(get_db),
):
    """Stream every list-matching requisition as a CSV download (attachment, no
    pagination).

    Gated on EXPORT_BULK_DATA (ISS-028 bulk dataset export lockdown — admin-by-default,
    per-user override possible). Same filter params as the list route (GET
    /v2/partials/requisitions) — the export mirrors the list's active view
    (search/status/owner/urgency/date-range) including the restricted-role ownership
    boundary.
    """
    return stream_csv(
        "requisitions_export.csv",
        _REQ_EXPORT_COLUMNS,
        _requisition_export_rows(
            db,
            user,
            q=q,
            status=status,
            owner=owner,
            urgency=urgency,
            date_from=date_from,
            date_to=date_to,
            sort=sort,
            sort_dir=sort_dir,
        ),
    )
