"""routers/trading.py — Thin HTMX router for the Trading workspace (Chunk F).

ADDITIVE vertical slice. Builds the viewable Trading workspace on top of the
already-green backend (Chunk A models, Chunk B excess_service offers, Chunk C
excess_mirror): a split-panel page, lens-switched lists (My Lists / Open to Me),
adaptive detail (lines / offers / build-bid / activity), and inbound-offer entry
(per_line / take_all) reusing the service's submit_offer + import parsers.

Strategy is additive-first: NEW templates under htmx/partials/trading/, NEW
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
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..constants import ExcessListStatus, ExcessOfferScope, ExcessOfferStatus
from ..database import get_db
from ..dependencies import require_user
from ..file_utils import parse_tabular_file
from ..models import Company, User
from ..models.excess import ExcessLineItem, ExcessList, ExcessOffer
from ..services import excess_mirror, excess_service
from ..template_env import template_response

router = APIRouter(tags=["trading"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls"}

# List statuses whose Sighting mirror is live (the deal is collecting offers).
_POSTED_STATUSES = (
    ExcessListStatus.OPEN,
    ExcessListStatus.COLLECTING,
    ExcessListStatus.BID_OUT,
)
# Offer statuses that count as a live, unactioned offer (triage glance).
_ACTIVE_OFFER_STATUSES = (ExcessOfferStatus.OPEN, ExcessOfferStatus.LATE)


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
            ExcessOffer.status.in_([s.value for s in _ACTIVE_OFFER_STATUSES]),
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
            ExcessOffer.status.in_([s.value for s in _ACTIVE_OFFER_STATUSES]),
        )
        .scalar()
        or 0
    )
    take_all = (
        db.query(func.count(ExcessOffer.id))
        .filter(
            ExcessOffer.excess_list_id.in_(owned.select()),
            ExcessOffer.scope == ExcessOfferScope.TAKE_ALL,
            ExcessOffer.status.in_([s.value for s in _ACTIVE_OFFER_STATUSES]),
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

    The owner always sees the real customer; everyone else gets the anonymized offerer-
    facing view (spec §"Customer identity hiding").
    """
    el = excess_service.get_excess_list(db, list_id)
    return el, (el.owner_id == user.id)


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
            ExcessOffer.status.in_([s.value for s in _ACTIVE_OFFER_STATUSES]),
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


@router.get("/v2/partials/trading/workspace", response_class=HTMLResponse)
async def trading_workspace(
    request: Request,
    lens: str = Query("mine"),
    stage: str = Query(""),
    q: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Split-panel Trading workspace shell: lens pills + stat strip + lists."""
    lens = lens if lens in ("mine", "open") else "mine"
    return template_response(
        "htmx/partials/trading/workspace.html",
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


@router.get("/v2/partials/trading/lists", response_class=HTMLResponse)
async def trading_lists(
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
        query = query.filter(ExcessList.title.ilike(f"%{q}%"))

    lists = query.order_by(ExcessList.updated_at.desc().nullslast(), ExcessList.id.desc()).all()
    cards = [_list_card(db, el, can_see_customer=can_see_customer) for el in lists]

    return template_response(
        "htmx/partials/trading/_lists.html",
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


@router.get("/v2/partials/trading/{list_id}", response_class=HTMLResponse)
async def trading_detail(
    request: Request,
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Right detail partial — slim header, breadcrumb, chips, lazy tabs."""
    el, _ = _get_list_for_user(db, list_id, user)
    return template_response("htmx/partials/trading/detail.html", _detail_context(request, db, el, user))


@router.get("/v2/partials/trading/{list_id}/lines", response_class=HTMLResponse)
async def trading_lines(
    request: Request,
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Lazy Lines tab body — adaptive: 1 line → card, ≥2 → compact table."""
    el, _ = _get_list_for_user(db, list_id, user)
    return template_response("htmx/partials/trading/_lines.html", _detail_context(request, db, el, user))


@router.get("/v2/partials/trading/{list_id}/offers", response_class=HTMLResponse)
async def trading_offers(
    request: Request,
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Lazy Offers tab body — per-line offer stacks + pinned take-all banner."""
    el, can_see_customer = _get_list_for_user(db, list_id, user)
    items = db.query(ExcessLineItem).filter_by(excess_list_id=el.id).order_by(ExcessLineItem.id).all()

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
        "htmx/partials/trading/_offers.html",
        {
            "request": request,
            "user": user,
            "list": el,
            "line_items": items,
            "by_line": by_line,
            "unmatched": unmatched,
            "take_all_offers": take_all_offers,
            "can_see_customer": can_see_customer,
            "is_owner": el.owner_id == user.id,
            "shape": "single" if len(items) == 1 else "table",
        },
    )


@router.get("/v2/partials/trading/{list_id}/lines/{line_id}/offers", response_class=HTMLResponse)
async def trading_line_offer_compare(
    request: Request,
    list_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Per-line offer comparison table (best highlighted + price-spread bar).

    Cloned from the quote-builder modal. NO auto-select — the trader eyeballs terms /
    lead before picking (spec §Offer-collection).
    """
    el, _ = _get_list_for_user(db, list_id, user)
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
        "htmx/partials/trading/offer_compare.html",
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


# ── Modal forms ──────────────────────────────────────────────────────


@router.get("/v2/partials/trading/create-form", response_class=HTMLResponse)
async def trading_create_form(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the new-list modal (only for users who can post)."""
    if not excess_service.can_post(user):
        raise HTTPException(403, "You do not have permission to post excess lists")
    companies = db.query(Company).order_by(Company.name).all()
    return template_response(
        "htmx/partials/trading/create_modal.html",
        {"request": request, "companies": companies},
    )


@router.get("/v2/partials/trading/{list_id}/add-line-form", response_class=HTMLResponse)
async def trading_add_line_form(
    request: Request,
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the add-line modal."""
    excess_service.get_excess_list(db, list_id)
    return template_response(
        "htmx/partials/trading/add_line_modal.html",
        {"request": request, "list_id": list_id},
    )


@router.get("/v2/partials/trading/{list_id}/offer-form", response_class=HTMLResponse)
async def trading_offer_form(
    request: Request,
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the submit-offer modal (per-line / take-all scope toggle)."""
    el = excess_service.get_excess_list(db, list_id)
    if not excess_service.can_offer(user):
        raise HTTPException(403, "You do not have permission to submit offers")
    if el.owner_id == user.id:
        raise HTTPException(403, "You cannot offer on your own excess list")
    return template_response(
        "htmx/partials/trading/offer_form.html",
        {"request": request, "list": el},
    )


# ── Mutations (thin — delegate to the service) ───────────────────────


@router.post("/api/trading/lists", response_class=HTMLResponse)
async def trading_create_list(
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
    return await trading_lists(request, lens="mine", stage="", q="", user=user, db=db)


@router.post("/api/trading/{list_id}/lines", response_class=HTMLResponse)
async def trading_add_line(
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
    return await trading_lines(request, list_id=list_id, user=user, db=db)


@router.post("/api/trading/{list_id}/import-preview", response_class=HTMLResponse)
async def trading_import_preview(
    request: Request,
    list_id: int,
    file: UploadFile,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Parse an uploaded file and render the shared import preview grid."""
    excess_service.get_excess_list(db, list_id)
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
        "htmx/partials/trading/import_preview.html",
        {
            "request": request,
            "list_id": list_id,
            "filename": filename,
            **result,
            "all_valid_rows_json": json.dumps(result["all_valid_rows"]),
        },
    )


@router.post("/api/trading/{list_id}/import-confirm", response_class=HTMLResponse)
async def trading_import_confirm(
    request: Request,
    list_id: int,
    rows_json: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Confirm a previewed import, then re-render the Lines tab."""
    try:
        rows = json.loads(rows_json)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, "Invalid import payload") from exc
    excess_service.confirm_import(db, list_id, rows)
    return await trading_lines(request, list_id=list_id, user=user, db=db)


@router.post("/api/trading/{list_id}/publish", response_class=HTMLResponse)
async def trading_publish(
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
    return template_response("htmx/partials/trading/detail.html", _detail_context(request, db, el, user))


@router.post("/api/trading/{list_id}/offers", response_class=HTMLResponse)
async def trading_submit_offer(
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
    return await trading_offers(request, list_id=el.id, user=user, db=db)


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
