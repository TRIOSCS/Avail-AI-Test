"""routers/resell.py — Thin HTMX router for the Resell workspace (Chunk F).

ADDITIVE vertical slice. Builds the viewable Resell workspace on top of the
already-green backend (Chunk A models, Chunk B excess_service offers, Chunk C
excess_mirror): a split-panel page, lens-switched lists (My Lists / Open to Me),
adaptive detail (lines / offers / build-bid / activity), and inbound-offer entry
(per_line / take_all) reusing the service's submit_offer + import parsers.

Strategy is additive-first: NEW templates under htmx/partials/resell/, NEW
endpoints here, NEW nav item. The OLD excess router/templates stay mounted — a
later cutover chunk removes them. Logic stays in excess_service / excess_mirror;
this layer only resolves request → context → template (the fat-service / thin-
router split the rest of the app uses).

Customer hiding is VIEW DISCIPLINE (single-tenant, spec §"Customer identity
hiding"): the "Open to Me" lens and the non-owner detail render project ONLY
MPN / qty / condition and never the seller company — enforced by a `can_see_customer`
flag the templates whitelist on, plus an owner-only list query.

Called by: app/main.py (router mount).
Depends on: services.excess_service, services.excess_mirror, file_utils,
            dependencies, template_env, models.excess.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..constants import (
    AccessKey,
    ExcessListStatus,
    ExcessOfferScope,
    ExcessOfferStatus,
    ExcessOutreachChannel,
    ExcessOutreachStatus,
)
from ..database import get_db
from ..dependencies import require_access, require_fresh_token, require_user
from ..file_utils import parse_tabular_file
from ..models import Company, User, VendorCard
from ..models.excess import CustomerBid, ExcessLineItem, ExcessList, ExcessOffer, ExcessOfferLine, ExcessOutreach
from ..services import (
    bid_back_service,
    buyer_affinity_service,
    excess_mirror,
    excess_service,
    resell_outreach_service,
    task_service,
)
from ..template_env import template_response
from ..utils.sql_helpers import escape_like

router = APIRouter(tags=["resell"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls"}

# List statuses whose Sighting mirror is live OR completed (deal visible to non-owners).
# Drafts are excluded — only the owner may see a draft list; 404 for anyone else.
_POSTED_STATUSES = (
    ExcessListStatus.OPEN,
    ExcessListStatus.COLLECTING,
    ExcessListStatus.BID_OUT,
    ExcessListStatus.AWARDED,
)
# Offer statuses that count as a live, unactioned offer (triage glance).
_UNACTIONED_OFFER_STATUSES = (ExcessOfferStatus.OPEN, ExcessOfferStatus.LATE)


# ── Helpers ──────────────────────────────────────────────────────────


def _file_extension(filename: str) -> str:
    """Return the lowercase extension (with dot), or '' if none."""
    if "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[-1].lower()


def _hours_until(close_at: datetime | None) -> float | None:
    """Hours until *close_at* (negative = overdue), or None when no close date.

    Drives the shared ``time_text`` urgency macro. Tolerates naive datetimes by
    stamping UTC — the close date is a coarse urgency signal.
    """
    if close_at is None:
        return None
    if close_at.tzinfo is None:
        close_at = close_at.replace(tzinfo=timezone.utc)
    return (close_at - datetime.now(timezone.utc)).total_seconds() / 3600.0


def _offer_coverage(items: list[ExcessLineItem]) -> tuple[int, int]:
    """(lines with ≥1 offer, total lines) — the list's offer-coverage meter."""
    total = len(items)
    covered = sum(1 for it in items if (it.offer_count or 0) > 0)
    return covered, total


def _list_card(db: Session, el: ExcessList, *, can_see_customer: bool) -> dict:
    """Project one ExcessList into the left-list row context.

    ``can_see_customer`` gates the seller name (False for the offerer-facing
    "Open to Me" lens — pure whitelist, never leak the customer).
    """
    items = db.query(ExcessLineItem).filter_by(excess_list_id=el.id).all()
    covered, total = _offer_coverage(items)
    offer_count = (
        db.query(func.count(ExcessOffer.id))
        .filter(
            ExcessOffer.excess_list_id == el.id,
            ExcessOffer.status.in_([s.value for s in _UNACTIONED_OFFER_STATUSES]),
        )
        .scalar()
        or 0
    )
    return {
        "list": el,
        "customer_name": (el.company.name if (can_see_customer and el.company) else None),
        "coverage_filled": covered,
        "coverage_total": total,
        "offer_count": offer_count,
        "hours_until": _hours_until(getattr(el, "close_at", None)),
    }


def _stat_strip(db: Session, user: User) -> dict:
    """Team-glance counters for the stat-card strip (My Lists scope).

    Open · Offers-to-review · Take-all · Bids-out · Awarded$ — each a one-click filter
    into the list (the triage ask). Scoped to lists this user owns so the glance is the
    trader's own board, not the whole tenant.
    """
    owned = db.query(ExcessList.id).filter(ExcessList.owner_id == user.id).subquery()

    open_count = (
        db.query(func.count(ExcessList.id))
        .filter(ExcessList.owner_id == user.id, ExcessList.status == ExcessListStatus.OPEN)
        .scalar()
        or 0
    )
    collecting_count = (
        db.query(func.count(ExcessList.id))
        .filter(ExcessList.owner_id == user.id, ExcessList.status == ExcessListStatus.COLLECTING)
        .scalar()
        or 0
    )
    bid_out_count = (
        db.query(func.count(ExcessList.id))
        .filter(ExcessList.owner_id == user.id, ExcessList.status == ExcessListStatus.BID_OUT)
        .scalar()
        or 0
    )
    awarded_count = (
        db.query(func.count(ExcessList.id))
        .filter(ExcessList.owner_id == user.id, ExcessList.status == ExcessListStatus.AWARDED)
        .scalar()
        or 0
    )
    offers_to_review = (
        db.query(func.count(ExcessOffer.id))
        .filter(
            ExcessOffer.excess_list_id.in_(owned.select()),
            ExcessOffer.status.in_([s.value for s in _UNACTIONED_OFFER_STATUSES]),
        )
        .scalar()
        or 0
    )
    take_all = (
        db.query(func.count(ExcessOffer.id))
        .filter(
            ExcessOffer.excess_list_id.in_(owned.select()),
            ExcessOffer.scope == ExcessOfferScope.TAKE_ALL,
            ExcessOffer.status.in_([s.value for s in _UNACTIONED_OFFER_STATUSES]),
        )
        .scalar()
        or 0
    )
    return {
        "open": open_count + collecting_count,
        "offers_to_review": offers_to_review,
        "take_all": take_all,
        "bids_out": bid_out_count,
        "awarded": awarded_count,
    }


def _get_list_for_user(db: Session, list_id: int, user: User) -> tuple[ExcessList, bool]:
    """Fetch a list and decide whether *user* may see the seller's identity.

    The owner always sees the real customer; non-owners may only see the list when it is
    in a posted status (open/collecting/bid_out/awarded) — drafts are private to the
    owner (404, not 403, to avoid revealing the list's existence).
    """
    el = excess_service.get_excess_list(db, list_id)
    is_owner = el.owner_id == user.id
    if not is_owner and el.status not in {s.value for s in _POSTED_STATUSES}:
        raise HTTPException(404, "List not found")
    return el, is_owner


def _require_owner(el: ExcessList, user: User) -> None:
    """Raise 403 if *user* is not the list owner.

    Used by mutation endpoints that only the owner may call (add-line, import-preview,
    import-confirm). Mirrors the guard in resell_publish.
    """
    if el.owner_id != user.id:
        raise HTTPException(403, "Only the list owner can edit it")


def _detail_context(request: Request, db: Session, el: ExcessList, user: User) -> dict:
    """Build the shared detail context: chips + adaptive-shape flags.

    The adaptive rule (spec §"Flexible detail"): ``shape`` is ``single`` for a
    one-line deal (one card, no table chrome), ``table`` otherwise; ``take_all``
    offers render as a pinned banner above the lines regardless of shape.
    """
    items = db.query(ExcessLineItem).filter_by(excess_list_id=el.id).order_by(ExcessLineItem.id).all()
    can_see_customer = el.owner_id == user.id
    offer_count = db.query(func.count(ExcessOffer.id)).filter(ExcessOffer.excess_list_id == el.id).scalar() or 0
    take_all_count = (
        db.query(func.count(ExcessOffer.id))
        .filter(
            ExcessOffer.excess_list_id == el.id,
            ExcessOffer.scope == ExcessOfferScope.TAKE_ALL,
            ExcessOffer.status.in_([s.value for s in _UNACTIONED_OFFER_STATUSES]),
        )
        .scalar()
        or 0
    )
    return {
        "request": request,
        "user": user,
        "list": el,
        "line_items": items,
        "line_count": len(items),
        "offer_count": offer_count,
        "take_all_count": take_all_count,
        "can_see_customer": can_see_customer,
        "can_post": excess_service.can_post(user),
        "can_offer": excess_service.can_offer(user) and el.owner_id != user.id,
        "shape": "single" if len(items) == 1 else "table",
        "hours_until": _hours_until(getattr(el, "close_at", None)),
        "is_posted": el.status in {s.value for s in _POSTED_STATUSES},
    }


# ── Full workspace page ──────────────────────────────────────────────


@router.get("/v2/partials/resell/workspace", response_class=HTMLResponse)
async def resell_workspace(
    request: Request,
    lens: str = Query("mine"),
    stage: str = Query(""),
    q: str = Query(""),
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Split-panel Resell workspace shell: lens pills + stat strip + lists."""
    lens = lens if lens in ("mine", "open") else "mine"
    return template_response(
        "htmx/partials/resell/workspace.html",
        {
            "request": request,
            "user": user,
            "lens": lens,
            "stage": stage,
            "q": q,
            "stats": _stat_strip(db, user),
            "can_post": excess_service.can_post(user),
        },
    )


# ── Left list partial ────────────────────────────────────────────────


@router.get("/v2/partials/resell/lists", response_class=HTMLResponse)
async def resell_lists(
    request: Request,
    lens: str = Query("mine"),
    stage: str = Query(""),
    q: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Left list partial — opportunity rows, lens + stage filters + search.

    ``lens=mine`` → lists this user owns (seller identity visible).
    ``lens=open`` → posted lists owned by OTHERS that this user may offer on
    (customer-anonymized — pure whitelist, never the seller).
    """
    lens = lens if lens in ("mine", "open") else "mine"
    query = db.query(ExcessList)

    if lens == "open":
        # Offerer-facing: posted lists owned by someone else (anonymized).
        query = query.filter(
            ExcessList.owner_id != user.id,
            ExcessList.status.in_([s.value for s in _POSTED_STATUSES]),
        )
        can_see_customer = False
    else:
        query = query.filter(ExcessList.owner_id == user.id)
        can_see_customer = True

    if stage:
        query = query.filter(ExcessList.status == stage)
    if q:
        query = query.filter(ExcessList.title.ilike(f"%{escape_like(q)}%", escape="\\"))

    lists = query.order_by(ExcessList.updated_at.desc().nullslast(), ExcessList.id.desc()).all()
    cards = [_list_card(db, el, can_see_customer=can_see_customer) for el in lists]

    return template_response(
        "htmx/partials/resell/_lists.html",
        {
            "request": request,
            "user": user,
            "lens": lens,
            "stage": stage,
            "q": q,
            "cards": cards,
            "can_post": excess_service.can_post(user),
        },
    )


# ── Right detail + lazy tab bodies ───────────────────────────────────


# NB: this static route MUST be registered before the dynamic "/{list_id}" route below —
# otherwise FastAPI matches "create-form" against {list_id} and 422s on int parsing.
@router.get("/v2/partials/resell/create-form", response_class=HTMLResponse)
async def resell_create_form(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the new-list modal (only for users who can post)."""
    if not excess_service.can_post(user):
        raise HTTPException(403, "You do not have permission to post excess lists")
    companies = db.query(Company).order_by(Company.name).all()
    return template_response(
        "htmx/partials/resell/create_modal.html",
        {"request": request, "companies": companies},
    )


@router.get("/v2/partials/resell/{list_id}", response_class=HTMLResponse)
async def resell_detail(
    request: Request,
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Right detail partial — slim header, breadcrumb, chips, lazy tabs."""
    el, _ = _get_list_for_user(db, list_id, user)
    return template_response("htmx/partials/resell/detail.html", _detail_context(request, db, el, user))


@router.get("/v2/partials/resell/{list_id}/lines", response_class=HTMLResponse)
async def resell_lines(
    request: Request,
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Lazy Lines tab body — adaptive: 1 line → card, ≥2 → compact table."""
    el, _ = _get_list_for_user(db, list_id, user)
    return template_response("htmx/partials/resell/_lines.html", _detail_context(request, db, el, user))


@router.get("/v2/partials/resell/{list_id}/offers", response_class=HTMLResponse)
async def resell_offers(
    request: Request,
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Lazy Offers tab body — per-line offer stacks + pinned take-all banner.

    Offers are the owner's private view — non-owners must not see other brokers' quotes.
    If the requester is not the owner, render the "offers are private" state without
    querying or passing any offer data.
    """
    el, can_see_customer = _get_list_for_user(db, list_id, user)
    is_owner = el.owner_id == user.id
    items = db.query(ExcessLineItem).filter_by(excess_list_id=el.id).order_by(ExcessLineItem.id).all()

    if not is_owner:
        # Non-owner: render an empty offers view — no offer data in the response.
        return template_response(
            "htmx/partials/resell/_offers.html",
            {
                "request": request,
                "user": user,
                "list": el,
                "line_items": items,
                "by_line": {it.id: [] for it in items},
                "unmatched": [],
                "take_all_offers": [],
                "can_see_customer": False,
                "is_owner": False,
                "shape": "single" if len(items) == 1 else "table",
            },
        )

    take_all_offers = (
        db.query(ExcessOffer)
        .filter(
            ExcessOffer.excess_list_id == el.id,
            ExcessOffer.scope == ExcessOfferScope.TAKE_ALL,
        )
        .order_by(ExcessOffer.created_at.desc())
        .all()
    )

    # Group per-line offer lines under their matched line item, plus an unmatched
    # queue for rows that didn't cleanly resolve (never dropped — spec §Offer-collection).
    by_line: dict[int, list] = {it.id: [] for it in items}
    unmatched: list = []
    per_line_offers = (
        db.query(ExcessOffer)
        .filter(
            ExcessOffer.excess_list_id == el.id,
            ExcessOffer.scope == ExcessOfferScope.PER_LINE,
        )
        .all()
    )
    for offer in per_line_offers:
        for line in offer.lines:
            entry = {"offer": offer, "line": line}
            if line.excess_line_item_id and line.excess_line_item_id in by_line:
                by_line[line.excess_line_item_id].append(entry)
            else:
                unmatched.append(entry)

    return template_response(
        "htmx/partials/resell/_offers.html",
        {
            "request": request,
            "user": user,
            "list": el,
            "line_items": items,
            "by_line": by_line,
            "unmatched": unmatched,
            "take_all_offers": take_all_offers,
            "can_see_customer": can_see_customer,
            "is_owner": True,
            "shape": "single" if len(items) == 1 else "table",
        },
    )


@router.get("/v2/partials/resell/{list_id}/lines/{line_id}/offers", response_class=HTMLResponse)
async def resell_line_offer_compare(
    request: Request,
    list_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Per-line offer comparison table (best highlighted + price-spread bar).

    Owner-only: the comparison reveals all competing brokers' prices, so non-owners
    receive 403 (not 404) to make the permission boundary explicit.

    Cloned from the quote-builder modal. NO auto-select — the trader eyeballs terms /
    lead before picking (spec §Offer-collection).
    """
    el, is_owner = _get_list_for_user(db, list_id, user)
    if not is_owner:
        raise HTTPException(403, "Offer comparison is only visible to the list owner")
    item = db.get(ExcessLineItem, line_id)
    if not item or item.excess_list_id != el.id:
        raise HTTPException(404, f"Line item {line_id} not found in list {list_id}")

    rows = []
    for offer in el.offers:
        if offer.scope != ExcessOfferScope.PER_LINE:
            continue
        for line in offer.lines:
            if line.excess_line_item_id == line_id:
                rows.append({"offer": offer, "line": line})

    priced = [r["line"].unit_price for r in rows if r["line"].unit_price is not None]
    return template_response(
        "htmx/partials/resell/offer_compare.html",
        {
            "request": request,
            "list": el,
            "item": item,
            "rows": rows,
            "min_price": float(min(priced)) if priced else None,
            "max_price": float(max(priced)) if priced else None,
            "is_owner": el.owner_id == user.id,
        },
    )


# ── Build Bid tab (owner-only bid-back assembly) ─────────────────────


def _latest_bid(db: Session, list_id: int) -> CustomerBid | None:
    """The most recent CustomerBid for a list (the one the Build-Bid tab shows)."""
    return db.query(CustomerBid).filter(CustomerBid.excess_list_id == list_id).order_by(CustomerBid.id.desc()).first()


def _build_bid_context(request: Request, db: Session, el: ExcessList, user: User) -> dict:
    """Context for the Build-Bid tab: each line + its best-offer planning reference,
    plus the most recent assembled bid (its clean export summary) if one exists.

    Owner-only — the caller must gate access (the tab reveals planning prices). Each line
    surfaces ``best_offer_unit_price`` as the pre-fill reference for the editable "our
    offer" input; the summary renders the clean ``bid_back_export_context`` so the owner
    sees exactly what the customer doc will carry (no broker names).
    """
    items = db.query(ExcessLineItem).filter_by(excess_list_id=el.id).order_by(ExcessLineItem.id).all()
    bid = _latest_bid(db, el.id)
    summary = bid_back_service.bid_back_export_context(bid) if bid else None
    return {
        "request": request,
        "user": user,
        "list": el,
        "line_items": items,
        "line_count": len(items),
        "bid": bid,
        "summary": summary,
    }


@router.get("/v2/partials/resell/{list_id}/build-bid", response_class=HTMLResponse)
async def resell_build_bid(
    request: Request,
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Lazy Build-Bid tab body — owner-only bid-back builder.

    Reveals each line's best-offer planning price + an editable "our offer" input, an
    "Assemble bid" action, and (once assembled) the clean bid summary + a Download-PDF
    link. Non-owners get 403 (the planning prices are the owner's private view).
    """
    el = excess_service.get_excess_list(db, list_id)
    _require_owner(el, user)
    return template_response("htmx/partials/resell/_build_bid.html", _build_bid_context(request, db, el, user))


@router.post("/api/resell/{list_id}/bid", response_class=HTMLResponse)
async def resell_assemble_bid(
    request: Request,
    list_id: int,
    selections_json: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Assemble a bid-back from the selected lines (owner-only), then re-render the tab.

    ``selections_json`` is a JSON array of ``{excess_line_item_id, customer_unit_price?}``
    (price blank → seeded from best_offer_unit_price). The service enforces owner-only +
    foreign-line rejection; this layer only parses the form and delegates.
    """
    el = excess_service.get_excess_list(db, list_id)
    _require_owner(el, user)
    try:
        raw = json.loads(selections_json)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, "Invalid bid payload") from exc
    if not isinstance(raw, list) or not raw:
        raise HTTPException(400, "Select at least one line to assemble a bid")

    selections = [
        {
            "excess_line_item_id": _to_int(str(s.get("excess_line_item_id"))),
            "customer_unit_price": _to_decimal(str(s.get("customer_unit_price")))
            if s.get("customer_unit_price") not in (None, "")
            else None,
        }
        for s in raw
    ]
    bid_back_service.build_bid_back(db, list_id=list_id, owner=user, selections=selections)
    el = excess_service.get_excess_list(db, list_id)
    return template_response("htmx/partials/resell/_build_bid.html", _build_bid_context(request, db, el, user))


@router.get("/api/resell/{list_id}/bid/{bid_id}/pdf")
async def resell_bid_pdf(
    list_id: int,
    bid_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Download the clean bid-back PDF (owner-only).

    The bid must belong to *list_id* and the requester must own the list. The PDF
    renders only the whitelisted bid_back_export_context — no broker / trader / seller
    identity.
    """
    el = excess_service.get_excess_list(db, list_id)
    _require_owner(el, user)
    bid = db.get(CustomerBid, bid_id)
    if not bid or bid.excess_list_id != list_id:
        raise HTTPException(404, f"Bid {bid_id} not found on list {list_id}")

    import asyncio

    from ..services.document_service import generate_bid_report_pdf

    loop = asyncio.get_running_loop()
    try:
        pdf_bytes = await loop.run_in_executor(None, generate_bid_report_pdf, bid.id, db)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=bid-{bid_id}.pdf"},
    )


# ── Modal forms ──────────────────────────────────────────────────────


@router.get("/v2/partials/resell/{list_id}/add-line-form", response_class=HTMLResponse)
async def resell_add_line_form(
    request: Request,
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the add-line modal (draft lists only)."""
    el, _ = _get_list_for_user(db, list_id, user)
    _require_owner(el, user)
    if el.status != ExcessListStatus.DRAFT:
        raise HTTPException(409, "Posted lists are locked; revise as a new version")
    return template_response(
        "htmx/partials/resell/add_line_modal.html",
        {"request": request, "list_id": list_id},
    )


@router.get("/v2/partials/resell/{list_id}/offer-form", response_class=HTMLResponse)
async def resell_offer_form(
    request: Request,
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the submit-offer modal (per-line / take-all scope toggle)."""
    # Load-and-authorize: non-owners 404 on a draft (existence not revealed).
    el, is_owner = _get_list_for_user(db, list_id, user)
    if not excess_service.can_offer(user):
        raise HTTPException(403, "You do not have permission to submit offers")
    if is_owner:
        raise HTTPException(403, "You cannot offer on your own excess list")
    if el.status not in {s.value for s in _POSTED_STATUSES}:
        raise HTTPException(404, "List not found")
    return template_response(
        "htmx/partials/resell/offer_form.html",
        {"request": request, "list": el},
    )


# ── Mutations (thin — delegate to the service) ───────────────────────


@router.post("/api/resell/lists", response_class=HTMLResponse)
async def resell_create_list(
    request: Request,
    title: str = Form(...),
    company_id: int = Form(...),
    notes: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new excess list (owner = current user); re-render the My-Lists list."""
    if not excess_service.can_post(user):
        raise HTTPException(403, "You do not have permission to post excess lists")
    excess_service.create_excess_list(
        db,
        title=title,
        company_id=company_id,
        owner_id=user.id,
        notes=notes or None,
    )
    return await resell_lists(request, lens="mine", stage="", q="", user=user, db=db)


@router.post("/api/resell/{list_id}/lines", response_class=HTMLResponse)
async def resell_add_line(
    request: Request,
    list_id: int,
    part_number: str = Form(...),
    quantity: int = Form(...),
    manufacturer: str = Form(""),
    condition: str = Form("New"),
    date_code: str = Form(""),
    asking_price: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a single line, resolve its MaterialCard, re-render the Lines tab."""
    el = excess_service.get_excess_list(db, list_id)
    _require_owner(el, user)
    if el.status != ExcessListStatus.DRAFT:
        raise HTTPException(409, "Posted lists are locked; revise as a new version")
    if not excess_service.can_post(user):
        raise HTTPException(403, "You do not have permission to post excess lists")
    from ..utils.normalization import normalize_mpn_key

    item = ExcessLineItem(
        excess_list_id=list_id,
        part_number=part_number,
        normalized_part_number=normalize_mpn_key(part_number) or None,
        manufacturer=manufacturer or None,
        quantity=quantity,
        condition=condition or "New",
        date_code=date_code or None,
        asking_price=_to_decimal(asking_price),
    )
    db.add(item)
    excess_service._resolve_line_material_card(db, item)
    el.total_line_items = (el.total_line_items or 0) + 1
    db.commit()
    # Re-render the WHOLE detail (not just the Lines tab): adding the first line to a
    # draft is what makes the header Post button appear (line_count > 0), so a Lines-only
    # swap would leave the header stale and the user with no way to publish (RS-5).
    return template_response("htmx/partials/resell/detail.html", _detail_context(request, db, el, user))


@router.post("/api/resell/{list_id}/import-preview", response_class=HTMLResponse)
async def resell_import_preview(
    request: Request,
    list_id: int,
    file: UploadFile,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Parse an uploaded file and render the shared import preview grid."""
    el = excess_service.get_excess_list(db, list_id)
    _require_owner(el, user)
    if not excess_service.can_post(user):
        raise HTTPException(403, "You do not have permission to post excess lists")
    filename = file.filename or ""
    if _file_extension(filename) not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type '{_file_extension(filename)}'")
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File too large")
    rows = parse_tabular_file(content, filename)
    if not rows:
        raise HTTPException(400, "No data rows found")
    result = excess_service.preview_import(rows)
    return template_response(
        "htmx/partials/resell/import_preview.html",
        {
            "request": request,
            "list_id": list_id,
            "filename": filename,
            **result,
            "all_valid_rows_json": json.dumps(result["all_valid_rows"]),
        },
    )


@router.post("/api/resell/{list_id}/import-confirm", response_class=HTMLResponse)
async def resell_import_confirm(
    request: Request,
    list_id: int,
    rows_json: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Confirm a previewed import, then re-render the Lines tab."""
    el = excess_service.get_excess_list(db, list_id)
    _require_owner(el, user)
    if el.status != ExcessListStatus.DRAFT:
        raise HTTPException(409, "Posted lists are locked; revise as a new version")
    if not excess_service.can_post(user):
        raise HTTPException(403, "You do not have permission to post excess lists")
    try:
        rows = json.loads(rows_json)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, "Invalid import payload") from exc
    excess_service.confirm_import(db, list_id, rows)
    # Re-render the WHOLE detail so the header Post button appears once the draft has
    # lines — a Lines-only swap leaves the header stale (RS-5).
    el = excess_service.get_excess_list(db, list_id)
    return template_response("htmx/partials/resell/detail.html", _detail_context(request, db, el, user))


@router.post("/api/resell/{list_id}/publish", response_class=HTMLResponse)
async def resell_publish(
    request: Request,
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Publish a list: flip to open + live-mirror every line, then re-render detail."""
    el = excess_service.get_excess_list(db, list_id)
    if not excess_service.can_post(user):
        raise HTTPException(403, "You do not have permission to post excess lists")
    if el.owner_id != user.id:
        raise HTTPException(403, "Only the list owner can publish it")
    excess_mirror.publish_list(db, list_id, user)
    el = excess_service.get_excess_list(db, list_id)
    return template_response("htmx/partials/resell/detail.html", _detail_context(request, db, el, user))


@router.post("/api/resell/{list_id}/close", response_class=HTMLResponse)
async def resell_close(
    request: Request,
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Close a posted list (owner-only): flip to bid_out + stamp close_at, re-render
    detail."""
    el = excess_service.close_list(db, list_id, user)
    return template_response("htmx/partials/resell/detail.html", _detail_context(request, db, el, user))


@router.post("/api/resell/{list_id}/offers", response_class=HTMLResponse)
async def resell_submit_offer(
    request: Request,
    list_id: int,
    scope: str = Form(...),
    notes: str = Form(""),
    mpn_raw: str = Form(""),
    quantity: str = Form(""),
    unit_price: str = Form(""),
    lead_time_days: str = Form(""),
    terms_text: str = Form(""),
    take_all_total_price: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Submit an inbound offer (per_line single-entry or take_all) via the service.

    This slice handles the single-line quick-add path; the paste/upload funnel reuses
    the same preview grid (import_preview) and lands here per-row. The service enforces
    can_offer + the self-offer guard.
    """
    # Load-and-authorize: non-owners 404 on a draft (existence not revealed), and offers
    # are only accepted on a posted/published list — never on an unpublished draft.
    el, _ = _get_list_for_user(db, list_id, user)
    if el.status not in {s.value for s in _POSTED_STATUSES}:
        raise HTTPException(404, "List not found")

    scope = ExcessOfferScope(scope).value if scope in (s.value for s in ExcessOfferScope) else ExcessOfferScope.PER_LINE

    lines = None
    if scope == ExcessOfferScope.PER_LINE:
        qty = _to_int(quantity)
        if not mpn_raw.strip() or qty is None:
            raise HTTPException(400, "Per-line offer needs a part number and quantity")
        lines = [
            {
                "mpn_raw": mpn_raw.strip(),
                "quantity": qty,
                "unit_price": _to_decimal(unit_price),
                "lead_time_days": _to_int(lead_time_days),
                "terms_text": terms_text or None,
            }
        ]

    excess_service.submit_offer(
        db,
        list_id=list_id,
        user=user,
        scope=scope,
        notes=notes or None,
        lines=lines,
        take_all_total_price=_to_decimal(take_all_total_price) if scope == ExcessOfferScope.TAKE_ALL else None,
    )

    el = excess_service.get_excess_list(db, list_id)
    return await resell_offers(request, list_id=el.id, user=user, db=db)


@router.post("/api/resell/{list_id}/offers/{offer_id}/award", response_class=HTMLResponse)
async def resell_award_offer(
    request: Request,
    list_id: int,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Award an inbound offer (owner-only): flip it to ``won`` and recompute the winning
    buyer's scorecard, then re-render the detail panel.

    The award is the single path that marks an ExcessOffer ``won``; the service owns the
    transaction and fires the buyer-score recompute hook before committing. Owner-gated +
    404 on a missing offer are enforced in ``excess_service.award_offer``.
    """
    offer = excess_service.award_offer(db, offer_id, user)
    el = excess_service.get_excess_list(db, offer.excess_list_id)
    return template_response("htmx/partials/resell/detail.html", _detail_context(request, db, el, user))


# ── Outreach: offer-to-buyers panel + tracker + don't-forget strip ───
#
# The trader→buyer half of Resell (the inverse of sourcing's RFQ). Offering excess OUT
# is the list OWNER's action, so every endpoint here is owner-gated via _require_owner;
# the buyer panel + tracker reveal the buyer "who" and the team's outreach board, both
# the owner's private view. Logic lives in resell_outreach_service (send/log/reply) and
# buyer_affinity_service (rank/overlap/nudge); this layer only resolves request →
# context → template.

# Outreach statuses that count as a buyer ENGAGED at all (the tracker "responded" tally).
_RESPONDED_OUTREACH = (
    ExcessOutreachStatus.OPENED,
    ExcessOutreachStatus.RESPONDED,
    ExcessOutreachStatus.BID,
    ExcessOutreachStatus.DECLINED,
)


def _suggestion_rows(db: Session, el: ExcessList, owner: User, line_ids: list[int] | None) -> list[dict]:
    """Ranked, offerable buyer suggestions for the panel, each with its advisory overlap
    flag.

    Wraps ``buyer_affinity_service.rank_buyers_for`` (already bounded + reachable-only)
    and decorates each row with ``overlap_warning`` (advisory — never blocks). Scoped to
    the selected lines when given, else the whole list.
    """
    ranked = buyer_affinity_service.rank_buyers_for(
        db,
        excess_list_id=el.id if not line_ids else None,
        line_item_ids=line_ids or None,
    )
    rows: list[dict] = []
    for rb in ranked:
        overlap = buyer_affinity_service.overlap_warning(
            db,
            excess_list_id=el.id,
            target_vendor_card_id=rb.vendor_card_id,
            owner_id=owner.id,
        )
        rows.append({"buyer": rb, "overlap": overlap})
    return rows


def _no_contact_buyers(db: Session, el: ExcessList, suggested_ids: set[int]) -> list[dict]:
    """Buyers with offer history on this list's lines but NO resolvable contact email.

    rank_buyers_for filters unreachable buyers out (it only returns offerable ones), but
    a buyer the owner has bought-from before may have lost their contact email — the
    panel still lists them (manual-log only) with a clear "no contact on file" badge so
    they're never silently dropped (mirrors the RFQ modal's no-email treatment). Returns
    [{card, last_bid}] for the history buyers absent from the reachable suggestions.
    """
    line_ids = [li.id for li in db.query(ExcessLineItem.id).filter_by(excess_list_id=el.id).all()]
    # History buyers: won an offer on one of this list's lines (the strongest "we've
    # dealt with them" signal — the ones worth surfacing even when unreachable).
    history_ids = {
        cid
        for (cid,) in db.query(ExcessOffer.offerer_vendor_card_id)
        .join(ExcessOfferLine, ExcessOfferLine.offer_id == ExcessOffer.id)
        .filter(
            ExcessOffer.status == ExcessOfferStatus.WON,
            ExcessOffer.offerer_vendor_card_id.isnot(None),
            ExcessOfferLine.excess_line_item_id.in_(line_ids) if line_ids else False,
        )
        .distinct()
        .all()
    }
    missing = history_ids - suggested_ids
    if not missing:
        return []
    cards = db.query(VendorCard).filter(VendorCard.id.in_(list(missing))).all()
    return [{"card": c} for c in cards]


def _buyer_panel_context(
    request: Request,
    db: Session,
    el: ExcessList,
    owner: User,
    line_ids: list[int] | None,
    preselect_ids: list[int] | None = None,
) -> dict:
    """Context for the offer-to-buyers panel: ranked suggestions + no-contact buyers +
    scope.

    ``preselect_ids`` (buyer ``vendor_card_id``s) seed the panel's checked set so a "not
    yet offered" nudge chip lands with its buyer already selected (RS-8) — one click from
    action instead of re-finding the buyer in the ranked list.
    """
    suggestions = _suggestion_rows(db, el, owner, line_ids)
    suggested_ids = {row["buyer"].vendor_card_id for row in suggestions}
    scope_lines = db.query(ExcessLineItem).filter(ExcessLineItem.id.in_(line_ids)).all() if line_ids else None
    return {
        "request": request,
        "user": owner,
        "list": el,
        "suggestions": suggestions,
        "no_contact_buyers": _no_contact_buyers(db, el, suggested_ids),
        "channels": [c.value for c in ExcessOutreachChannel],
        "line_ids": line_ids or [],
        "scope_lines": scope_lines,
        "preselect_ids": preselect_ids or [],
    }


def _outreach_tracker_context(request: Request, db: Session, el: ExcessList, user: User) -> dict:
    """Context for the unified Outreach tracker: rows (newest first) + the glance
    summary."""
    rows = (
        db.query(ExcessOutreach)
        .filter(ExcessOutreach.excess_list_id == el.id)
        .order_by(ExcessOutreach.created_at.desc(), ExcessOutreach.id.desc())
        .all()
    )
    # Distinct-buyer counts so "offered N · M responded · K bid" reads per buyer, not per
    # (buyer × line) row — a 3-line per-line campaign is one buyer offered, not three.
    offered = {r.target_vendor_card_id for r in rows if r.target_vendor_card_id is not None}
    responded = {
        r.target_vendor_card_id for r in rows if r.target_vendor_card_id is not None and r.status in _RESPONDED_OUTREACH
    }
    bid = {
        r.target_vendor_card_id
        for r in rows
        if r.target_vendor_card_id is not None and r.status == ExcessOutreachStatus.BID
    }
    return {
        "request": request,
        "user": user,
        "list": el,
        "rows": rows,
        "summary": {"offered": len(offered), "responded": len(responded), "bid": len(bid)},
    }


@router.get("/v2/partials/resell/{list_id}/offer-buyers-form", response_class=HTMLResponse)
async def resell_offer_buyers_form(
    request: Request,
    list_id: int,
    line_ids: str = Query(""),
    preselect_vendor_card_id: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the offer-to-buyers panel (owner-only): ranked suggestions + manual add +
    scope + channel.

    ``line_ids`` (comma-separated) scopes the campaign to specific lines; omitted = the
    whole list. ``preselect_vendor_card_id`` seeds the checked set so a "not yet offered"
    nudge chip opens the panel with that buyer already selected (RS-8). The panel reveals
    the buyer "who" + scorecard facts, so it is the owner's private view (403 for a
    non-owner).
    """
    el = excess_service.get_excess_list(db, list_id)
    _require_owner(el, user)
    parsed = [lid for lid in (_to_int(x) for x in line_ids.split(",")) if lid is not None] if line_ids else None
    preselect = _to_int(preselect_vendor_card_id)
    return template_response(
        "htmx/partials/resell/offer_buyers_modal.html",
        _buyer_panel_context(request, db, el, user, parsed, [preselect] if preselect is not None else None),
    )


@router.get("/v2/partials/resell/{list_id}/outreach", response_class=HTMLResponse)
async def resell_outreach_tracker(
    request: Request,
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Lazy Outreach tab body — the unified tracker (owner-only).

    One row per buyer×line touch reading buyer · when · by-whom · channel · status,
    above the "offered N · M responded · K bid" glance. Owner's private board (403
    otherwise).
    """
    el = excess_service.get_excess_list(db, list_id)
    _require_owner(el, user)
    return template_response("htmx/partials/resell/_outreach.html", _outreach_tracker_context(request, db, el, user))


@router.get("/v2/partials/resell/{list_id}/not-yet-strip", response_class=HTMLResponse)
async def resell_not_yet_strip(
    request: Request,
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """The "usually offered, not yet this round" nudge strip (owner-only).

    Wired into the EXISTING detail nudge surface AND, per CRM Phase 2, also persists
    each surfaced buyer as a durable My-Day follow-up task for the list owner so the
    nudge survives a page close. The in-page strip and the task share the same buyer
    set; task creation is idempotent per (list, buyer, owner) via the Task service, so
    reloading the strip never duplicates a buyer's task.
    """
    el = excess_service.get_excess_list(db, list_id)
    _require_owner(el, user)
    buyers = buyer_affinity_service.not_yet_offered_strip(db, excess_list_id=el.id)
    # Persist the nudge as owner-assigned My-Day follow-up tasks (idempotent). Due today
    # so it lands under the Tasks page "Due soon" bucket.
    due = datetime.now(timezone.utc)
    for b in buyers:
        task_service.auto_create_resell_followup_task(
            db,
            excess_list_id=el.id,
            vendor_card_id=b.vendor_card_id,
            owner_id=el.owner_id,
            buyer_name=b.display_name,
            list_title=el.title,
            due_at=due,
        )
    return template_response(
        "htmx/partials/resell/_not_yet_strip.html",
        {"request": request, "user": user, "list": el, "buyers": buyers},
    )


@router.post("/api/resell/{list_id}/outreach", response_class=HTMLResponse)
async def resell_submit_outreach(
    request: Request,
    list_id: int,
    vendor_card_ids: str = Form(""),
    company_ids: str = Form(""),
    scope: str = Form("whole_list"),
    channel: str = Form(ExcessOutreachChannel.EMAIL),
    line_ids: str = Form(""),
    notes: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    token: str = Depends(require_fresh_token),
):
    """Submit an outreach campaign (owner-only), then re-render the tracker.

    Buyers arrive as ``vendor_card_ids`` (ranked picks) and/or ``company_ids`` (a
    manual-add company with no card yet — the service backfills a card). ``channel`` ==
    ``email`` routes through :func:`resell_outreach_service.submit_outreach_email` (the
    RFQ send engine, DNC-at-send + save-to-sent for free); any other channel is a
    manual log via :func:`submit_outreach`. ``scope`` is ``per_line`` (scoped to
    ``line_ids``) or ``whole_list``. The service enforces the owner + can_post guards.
    """
    el = excess_service.get_excess_list(db, list_id)
    _require_owner(el, user)
    if el.status == ExcessListStatus.DRAFT:
        raise HTTPException(409, "List is not posted")
    if channel not in {c.value for c in ExcessOutreachChannel}:
        raise HTTPException(422, "Unknown channel")

    buyers: list[dict] = [{"vendor_card_id": cid} for cid in (_to_int(x) for x in vendor_card_ids.split(",")) if cid]
    buyers += [{"company_id": cid} for cid in (_to_int(x) for x in company_ids.split(",")) if cid]
    if not buyers:
        raise HTTPException(400, "Select at least one buyer to offer")

    parsed_lines = [lid for lid in (_to_int(x) for x in line_ids.split(",")) if lid is not None] if line_ids else None
    scope_value = scope if scope in ("per_line", "whole_list") else "whole_list"

    if channel == ExcessOutreachChannel.EMAIL:
        if not subject.strip() or not body.strip():
            raise HTTPException(400, "An email outreach needs a subject and a message")
        await resell_outreach_service.submit_outreach_email(
            db,
            list_id=list_id,
            owner=user,
            buyers=buyers,
            scope=scope_value,
            token=token,
            subject=subject.strip(),
            body=body.strip(),
            line_item_ids=parsed_lines,
        )
    else:
        resell_outreach_service.submit_outreach(
            db,
            list_id=list_id,
            owner=user,
            buyers=buyers,
            scope=scope_value,
            channel=channel,
            line_item_ids=parsed_lines,
            notes=notes or None,
        )

    el = excess_service.get_excess_list(db, list_id)
    return template_response("htmx/partials/resell/_outreach.html", _outreach_tracker_context(request, db, el, user))


# ── tiny parse helpers (forms send strings) ──────────────────────────


def _to_decimal(value: str | None) -> Decimal | None:
    """Parse an optional money string → Decimal, or None when blank/invalid."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value).strip().lstrip("$").replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _to_int(value: str | None) -> int | None:
    """Parse an optional integer string → int, or None when blank/invalid."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(float(str(value).strip().replace(",", "")))
    except (ValueError, TypeError):
        return None
