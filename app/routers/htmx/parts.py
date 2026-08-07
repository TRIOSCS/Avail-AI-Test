"""routers/htmx/parts.py — Parts triage partials (HTMX).

Server-rendered HTML partials for the read-only parts triage list (the Sales Hub
workspace body): the filterable/sortable parts list with offer count + best price
per row, and its CSV export. Each row is an "Open deal" door into the requisition
detail page, which is the ONE deal editor — the old split-panel detail tabs and
inline editors were removed (simplification spec §5.1). The workspace SHELL entry
(`/v2/partials/parts/workspace`) stays in htmx_views.py.

Called by: app/main.py (router mount).
Depends on: app.models, app.dependencies, app.database, ._shared
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import case
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload

from ...constants import (
    OfferStatus,
    RequisitionStatus,
    SourcingStatus,
)
from ...database import get_db
from ...dependencies import require_user
from ...models import (
    Offer,
    Requirement,
    Requisition,
    User,
)
from ...template_env import _part_description, template_response
from ...utils.csv_export import stream_csv
from ...utils.search_builder import SearchBuilder
from ._shared import _base_ctx

router = APIRouter(tags=["htmx-views"])


def _parse_parts_date(value: str) -> datetime | None:
    """Parse a ``YYYY-MM-DD`` filter string to a datetime, or None when
    blank/invalid."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def _filtered_parts_query(
    db: Session,
    *,
    q: str = "",
    requisition_name: str = "",
    customer: str = "",
    brand: str = "",
    status: str = "",
    owner: int = 0,
    date_from_dt: datetime | None = None,
    date_to_dt: datetime | None = None,
    include_archived: bool | None = None,
):
    """Build the filtered parts (requirements) query shared by the list partial and the
    CSV export.

    Applies the identical archive-visibility logic plus every parts filter (search,
    requisition, customer, brand, status, owner, created-date range). Adds NO ordering
    or pagination — each caller layers those on — so the CSV export can never diverge
    from what the list shows.
    """
    query = db.query(Requirement).join(Requisition, Requirement.requisition_id == Requisition.id)

    # Archive visibility logic:
    # - status=archived  → show only archived parts (any requisition status)
    # - include_archived → show everything (no filtering)
    # - default          → exclude archived parts AND archived requisitions
    if status == "archived":
        query = query.filter(Requirement.sourcing_status == SourcingStatus.ARCHIVED)
    elif not include_archived:
        query = query.filter(
            Requisition.status.in_(
                [
                    RequisitionStatus.OPEN,
                ]
            )
        )
        query = query.filter(Requirement.sourcing_status != SourcingStatus.ARCHIVED)

    if q:
        query = query.filter(
            SearchBuilder(q).ilike_filter(
                Requirement.primary_mpn,
                Requirement.customer_pn,
                Requirement.brand,
                Requirement.substitutes_text,
                Requisition.name,
                Requisition.customer_name,
            )
        )
    if requisition_name:
        query = query.filter(SearchBuilder(requisition_name).ilike_filter(Requisition.name))
    if customer:
        query = query.filter(SearchBuilder(customer).ilike_filter(Requisition.customer_name))
    if brand:
        query = query.filter(SearchBuilder(brand).ilike_filter(Requirement.brand))
    if status and status != "archived":
        query = query.filter(Requirement.sourcing_status == status)
    if owner:
        query = query.filter(Requisition.claimed_by_id == owner)
    if date_from_dt:
        query = query.filter(Requirement.created_at >= date_from_dt)
    if date_to_dt:
        query = query.filter(Requirement.created_at <= date_to_dt)
    return query


@router.get("/v2/partials/parts", response_class=HTMLResponse)
async def parts_list_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    q: str = "",
    requisition_name: str = "",
    customer: str = "",
    brand: str = "",
    status: str = "",
    owner: int = 0,
    date_from: str = "",
    date_to: str = "",
    include_archived: bool | None = None,
    sort: str = "created",
    dir: str = "desc",
    offset: int = 0,
    limit: int = 50,
):
    """Return parts (requirements) list as HTML partial with filters and sorting."""

    # Clamp pagination params
    offset = max(0, offset)
    limit = max(1, min(200, limit))

    # Validate + parse date filters into datetimes (bind real datetimes, not
    # raw strings, so UTCDateTime columns receive UTC-aware values)
    date_from_dt = None
    date_to_dt = None
    if date_from:
        try:
            date_from_dt = datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError:
            date_from = ""
    if date_to:
        try:
            date_to_dt = datetime.strptime(date_to, "%Y-%m-%d")
        except ValueError:
            date_to = ""

    query = _filtered_parts_query(
        db,
        q=q,
        requisition_name=requisition_name,
        customer=customer,
        brand=brand,
        status=status,
        owner=owner,
        date_from_dt=date_from_dt,
        date_to_dt=date_to_dt,
        include_archived=include_archived,
    )

    total = query.count()

    # Sorting
    sort_map = {
        "mpn": Requirement.primary_mpn,
        "brand": Requirement.brand,
        "qty": Requirement.target_qty,
        "target_price": Requirement.target_price,
        "status": Requirement.sourcing_status,
        "requisition": Requisition.name,
        "customer": Requisition.customer_name,
        "created": Requirement.created_at,
    }
    sort_col = sort_map.get(sort) or Requirement.created_at
    query = query.order_by(sort_col.desc() if dir == "desc" else sort_col.asc())
    query = query.offset(offset).limit(limit)

    requirements = query.options(joinedload(Requirement.requisition).joinedload(Requisition.claimed_by)).all()

    # Aggregate offer count + best price per requirement
    req_ids = [r.id for r in requirements]
    offer_stats = {}
    if req_ids:
        stats = (
            db.query(
                Offer.requirement_id,
                sqlfunc.count(Offer.id).label("cnt"),
                sqlfunc.min(case((Offer.status == OfferStatus.ACTIVE, Offer.unit_price), else_=None)).label("best"),
            )
            .filter(Offer.requirement_id.in_(req_ids))
            .group_by(Offer.requirement_id)
            .all()
        )
        for row in stats:
            offer_stats[row.requirement_id] = {"count": row.cnt, "best_price": row.best}

    # Build display-MPN → material card ID mapping for click-through links
    from ...models.intelligence import MaterialCard
    from ...utils.normalization import normalize_mpn, normalize_mpn_key

    norm_to_mpns: dict[str, list[str]] = {}
    for r in requirements:
        raw_mpns = []
        if r.primary_mpn:
            raw_mpns.append(r.primary_mpn)
        for sub in r.substitutes or []:
            raw = sub["mpn"] if isinstance(sub, dict) else sub
            if raw:
                raw_mpns.append(raw)
        for raw in raw_mpns:
            display = normalize_mpn(raw) or raw.upper()
            nk = normalize_mpn_key(display)
            if nk:
                norm_to_mpns.setdefault(nk, []).append(display)

    sub_card_map: dict[str, int] = {}
    if norm_to_mpns:
        cards = (
            db.query(MaterialCard.normalized_mpn, MaterialCard.id)
            .filter(MaterialCard.normalized_mpn.in_(norm_to_mpns.keys()))
            .filter(MaterialCard.deleted_at.is_(None))
            .all()
        )
        for card_norm, card_id in cards:
            for display_mpn in norm_to_mpns[card_norm]:
                sub_card_map[display_mpn] = card_id

    # Team users for owner filter
    users_list = db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all()

    # Spotlight markers: requirement rows carrying new confirmed offers the user hasn't seen.
    from ...services.alerts import markers_for_tab

    alert_markers = markers_for_tab(db, user, "requisitions")

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requirements": requirements,
            "offer_stats": offer_stats,
            "alert_markers": alert_markers,
            "q": q,
            "requisition_name": requisition_name,
            "customer": customer,
            "brand": brand,
            "status": status,
            "owner": owner,
            "date_from": date_from,
            "date_to": date_to,
            "include_archived": include_archived,
            "sort": sort,
            "dir": dir,
            "offset": offset,
            "limit": limit,
            "total": total,
            "users": users_list,
            "user_role": user.role,
            "sub_card_map": sub_card_map,
        }
    )
    return template_response("htmx/partials/parts/list.html", ctx)


# CSV export column order — the real parts-worklist fields the buyer sees on the list.
_PARTS_EXPORT_COLUMNS = [
    "MPN",
    "Description",
    "Brand",
    "Status",
    "Qty",
    "Target $",
    "Offers",
    "Best $",
    "Requisition",
    "Customer",
    "Owner",
    "Created",
]


def _parts_export_rows(db: Session, query):
    """Yield one CSV row per filter-matching requirement (header handled by stream_csv).

    Pages through the shared filtered query in chunks so memory stays flat regardless of
    match count, batch-loading each chunk's offer stats (count + best ACTIVE unit price)
    exactly the way ``parts_list_partial`` does — no N+1, and NO pagination cap on the
    exported set.
    """
    chunk = 500
    offset = 0
    while True:
        batch = (
            query.order_by(Requirement.created_at.desc())
            .options(joinedload(Requirement.requisition).joinedload(Requisition.claimed_by))
            .offset(offset)
            .limit(chunk)
            .all()
        )
        if not batch:
            break
        req_ids = [r.id for r in batch]
        offer_stats: dict[int, tuple[int, object]] = {}
        for rid, cnt, best in (
            db.query(
                Offer.requirement_id,
                sqlfunc.count(Offer.id),
                sqlfunc.min(case((Offer.status == OfferStatus.ACTIVE, Offer.unit_price), else_=None)),
            )
            .filter(Offer.requirement_id.in_(req_ids))
            .group_by(Offer.requirement_id)
            .all()
        ):
            offer_stats[rid] = (cnt, best)
        for req in batch:
            cnt, best = offer_stats.get(req.id, (0, None))
            requisition = req.requisition
            owner = requisition.claimed_by.name if requisition and requisition.claimed_by else ""
            yield (
                req.primary_mpn or "",
                _part_description(req),
                req.brand or "",
                req.sourcing_status or "open",
                req.target_qty if req.target_qty is not None else "",
                req.target_price if req.target_price is not None else "",
                cnt,
                best if best is not None else "",
                requisition.name if requisition else "",
                requisition.customer_name if requisition else "",
                owner,
                req.created_at.strftime("%Y-%m-%d %H:%M") if req.created_at else "",
            )
        if len(batch) < chunk:
            break
        offset += chunk


@router.get("/v2/partials/parts/export")
async def parts_export(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    q: str = "",
    requisition_name: str = "",
    customer: str = "",
    brand: str = "",
    status: str = "",
    owner: int = 0,
    date_from: str = "",
    date_to: str = "",
    include_archived: bool | None = None,
):
    """Stream every filter-matching part (requirement) as a CSV download (attachment, no
    pagination).

    Same auth (``require_user``) and the same filter params as the parts list route — the
    export mirrors the list's active view (search / requisition / customer / brand /
    status / owner / created-date range / archive visibility) via the shared
    ``_filtered_parts_query``, so the download can never drift from what the list shows.
    """
    query = _filtered_parts_query(
        db,
        q=q,
        requisition_name=requisition_name,
        customer=customer,
        brand=brand,
        status=status,
        owner=owner,
        date_from_dt=_parse_parts_date(date_from),
        date_to_dt=_parse_parts_date(date_to),
        include_archived=include_archived,
    )
    return stream_csv("parts_export.csv", _PARTS_EXPORT_COLUMNS, _parts_export_rows(db, query))
