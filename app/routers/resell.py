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
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, joinedload

from ..constants import (
    AccessKey,
    ExcessLineItemStatus,
    ExcessListStatus,
    ExcessOfferScope,
    ExcessOfferStatus,
    ExcessOutreachChannel,
    ExcessOutreachStatus,
)
from ..database import get_db
from ..dependencies import require_access, require_fresh_token
from ..file_utils import parse_tabular_file
from ..models import Company, User, VendorCard, VendorResponse
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
from ..utils.csv_export import stream_csv
from ..utils.normalization import normalize_mpn_key
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
# Offer statuses shown in the owner's Offers tab: the live bids (open/late), the winner
# (won), and the decided-competitor context (lost, rendered "Not selected"). A WITHDRAWN
# (or dead EXPIRED) offer is retracted and drops out of the tab entirely.
_VISIBLE_OFFER_STATUSES = (
    ExcessOfferStatus.OPEN,
    ExcessOfferStatus.LATE,
    ExcessOfferStatus.WON,
    ExcessOfferStatus.LOST,
)
# Offer statuses a withdraw may act on: an inbound bid still in play. A won offer must be
# unawarded first (withdrawing it would strand its awarded lines); a lost/withdrawn offer
# is already closed.
_WITHDRAWABLE_OFFER_STATUSES = (ExcessOfferStatus.OPEN, ExcessOfferStatus.LATE)


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
        close_at = close_at.replace(tzinfo=UTC)
    return (close_at - datetime.now(UTC)).total_seconds() / 3600.0


def _offer_coverage(items: list[ExcessLineItem]) -> tuple[int, int]:
    """(lines with ≥1 offer, total lines) — the list's offer-coverage meter."""
    total = len(items)
    covered = sum(1 for it in items if (it.offer_count or 0) > 0)
    return covered, total


def _display_title(el: ExcessList, *, can_see_customer: bool) -> str:
    """The list title as shown to *this* viewer.

    Customer-identity hiding is view discipline: the owner sees the real free-text
    title, but a non-owner (the anonymized "Open to Me" lens, the non-owner detail,
    the submit-offer modal) gets a neutral, id-derived label instead. Traders name
    lists after the customer ("Acme Corp — surplus FPGAs"), so the raw title is the
    one field the ``customer_name`` anonymization doesn't sanitize — gate it the
    same way (same predicate that nulls the seller name in ``_list_cards``).
    """
    if can_see_customer:
        return el.title
    return f"Excess listing #{el.id}"


def _list_cards(db: Session, lists: list[ExcessList], *, can_see_customer: bool) -> list[dict]:
    """Project many ExcessLists into left-list rows in a FIXED number of queries.

    Was one line-items ``.all()`` PLUS one filtered offer-count query PER list (~2N
    queries for N lists, re-run on every filter keystroke) — SQLite-masked, bit on live
    PG. Now: one grouped coverage query (total + offered-line count per list) and one
    grouped unactioned-offer-count query, keyed by ``excess_list_id``. ``can_see_customer``
    gates the seller name (False for the offerer-facing "Open to Me" lens — pure whitelist,
    never leak the customer); the same gate swaps the free-text ``title`` for a neutral
    label (``_display_title``). Company is eager-loaded by the caller's query.
    """
    ids = [el.id for el in lists]
    if not ids:
        return []

    coverage: dict[int, tuple[int, int]] = {
        lid: (int(covered or 0), int(total or 0))
        for lid, total, covered in (
            db.query(
                ExcessLineItem.excess_list_id,
                func.count(ExcessLineItem.id),
                func.sum(case((ExcessLineItem.offer_count > 0, 1), else_=0)),
            )
            .filter(ExcessLineItem.excess_list_id.in_(ids))
            .group_by(ExcessLineItem.excess_list_id)
            .all()
        )
    }
    offer_counts: dict[int, int] = {
        lid: int(n or 0)
        for lid, n in (
            db.query(ExcessOffer.excess_list_id, func.count(ExcessOffer.id))
            .filter(
                ExcessOffer.excess_list_id.in_(ids),
                ExcessOffer.status.in_([s.value for s in _UNACTIONED_OFFER_STATUSES]),
            )
            .group_by(ExcessOffer.excess_list_id)
            .all()
        )
    }

    cards = []
    for el in lists:
        covered, total = coverage.get(el.id, (0, 0))
        cards.append(
            {
                "list": el,
                "display_title": _display_title(el, can_see_customer=can_see_customer),
                "customer_name": (el.company.name if (can_see_customer and el.company) else None),
                # Offer coverage + count are OWNER-PRIVATE (D2): a non-owner (the "Open to
                # Me" lens) must not learn how many lines already have offers or how many
                # bids are in — same competitive leak the per-line offer badge hides. Null
                # them here so the data never reaches the template (defense-in-depth with
                # the ``can_see_customer`` gate around the meter/badge in _lists.html).
                "coverage_filled": covered if can_see_customer else None,
                "coverage_total": total if can_see_customer else None,
                "offer_count": offer_counts.get(el.id, 0) if can_see_customer else None,
                "hours_until": _hours_until(getattr(el, "close_at", None)),
            }
        )
    return cards


def _stat_strip(db: Session, user: User) -> dict:
    """Team-glance counters for the stat-card strip (My Lists scope).

    Open · Offers-to-review · Take-all · Bids-out · Awarded$ — each a one-click filter
    into the list (the triage ask). Scoped to lists this user owns so the glance is the
    trader's own board, not the whole tenant.
    """
    owned = db.query(ExcessList.id).filter(ExcessList.owner_id == user.id).subquery()

    # One GROUP BY status for the four list-status counts (was four separate COUNTs).
    status_counts = {
        status: int(n or 0)
        for status, n in (
            db.query(ExcessList.status, func.count(ExcessList.id))
            .filter(ExcessList.owner_id == user.id)
            .group_by(ExcessList.status)
            .all()
        )
    }
    # One GROUP BY scope for the unactioned-offer counts (was two separate COUNTs):
    # offers-to-review is the sum, take-all its take_all slice.
    offers_by_scope = {
        scope: int(n or 0)
        for scope, n in (
            db.query(ExcessOffer.scope, func.count(ExcessOffer.id))
            .filter(
                ExcessOffer.excess_list_id.in_(owned.select()),
                ExcessOffer.status.in_([s.value for s in _UNACTIONED_OFFER_STATUSES]),
            )
            .group_by(ExcessOffer.scope)
            .all()
        )
    }
    return {
        "open": status_counts.get(ExcessListStatus.OPEN, 0) + status_counts.get(ExcessListStatus.COLLECTING, 0),
        "offers_to_review": sum(offers_by_scope.values()),
        "take_all": offers_by_scope.get(ExcessOfferScope.TAKE_ALL, 0),
        "bids_out": status_counts.get(ExcessListStatus.BID_OUT, 0),
        "awarded": status_counts.get(ExcessListStatus.AWARDED, 0),
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
        "display_title": _display_title(el, can_see_customer=can_see_customer),
        "line_items": items,
        "line_count": len(items),
        "awarded_line_count": sum(1 for it in items if it.status == ExcessLineItemStatus.AWARDED),
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
    needs: str = Query(""),
    q: str = Query(""),
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Split-panel Resell workspace shell: lens pills + stat strip + lists."""
    lens = lens if lens in ("mine", "open") else "mine"
    needs = needs if needs in ("offers", "take_all") else ""
    # The active triage token drives the single-highlight ring. The offer-based cards
    # (offers/take_all) live in the ``needs`` dimension; the status cards in ``stage`` —
    # so the token is ``needs`` when set, else the ``stage`` value (never both at once).
    active_filter = needs or stage
    return template_response(
        "htmx/partials/resell/workspace.html",
        {
            "request": request,
            "user": user,
            "lens": lens,
            "stage": stage,
            "needs": needs,
            "active_filter": active_filter,
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
    needs: str = Query(""),
    q: str = Query(""),
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Left list partial — opportunity rows, lens + stage/needs filters + search.

    ``lens=mine`` → lists this user owns (seller identity visible).
    ``lens=open`` → posted lists owned by OTHERS that this user may offer on
    (customer-anonymized — pure whitelist, never the seller).

    ``stage`` filters on list STATUS (open/collecting/…). ``needs`` is the offer-based
    triage dimension the status filter can't express: ``needs=offers`` → lists with ≥1
    live, unactioned offer; ``needs=take_all`` → lists with a live whole-list offer. These
    back the "Offers to review" / "Take-all" stat cards (their counts come from offers, not
    a list status), so they need their own filter rather than a status value.
    """
    lens = lens if lens in ("mine", "open") else "mine"
    needs = needs if needs in ("offers", "take_all") else ""
    # Eager-load company so the per-card seller-name render (mine lens) doesn't lazy-load
    # one company per list (M8: kill the N+1s in the left list).
    query = db.query(ExcessList).options(joinedload(ExcessList.company))

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

    # D2 (offer-EXISTENCE oracle): the offer-based ``needs`` triage — "lists carrying a
    # live bid" — is the OWNER's board only. Applied in the open (offerer) lens it becomes
    # an existence oracle: a non-owner could diff ``lens=open&needs=offers`` against plain
    # ``lens=open`` to learn which anonymized "Excess listing #N" postings have already drawn
    # a competing bid (``needs=take_all`` narrows it to whole-list bids) — the SAME
    # competitive signal the coverage meter / amber badge / offer-count chip are hidden from
    # non-owners to protect. Gate it on the one predicate (``can_see_customer``) everywhere:
    # for a non-owner the filter never runs and the passed-through state never reflects it.
    if not can_see_customer:
        needs = ""

    if stage:
        query = query.filter(ExcessList.status == stage)
    if needs:
        # Lists carrying a live, unactioned offer (take_all = its whole-list slice) — the
        # same offer population the triage stat cards count (_stat_strip).
        offer_lists = db.query(ExcessOffer.excess_list_id).filter(
            ExcessOffer.status.in_([s.value for s in _UNACTIONED_OFFER_STATUSES])
        )
        if needs == "take_all":
            offer_lists = offer_lists.filter(ExcessOffer.scope == ExcessOfferScope.TAKE_ALL)
        query = query.filter(ExcessList.id.in_(offer_lists))
    if q:
        if lens == "open":
            # #10: a non-owner must NOT be able to search the free-text title — traders name
            # lists after the customer ("Acme Corp — surplus"), so title search is a
            # de-anonymization oracle (a hit/miss confirms the hidden customer name). Match
            # on PART IDENTITY instead: normalized MPN (query normalized the same way the
            # column is) or manufacturer — both indexed (models/excess.py) — via a subquery
            # on excess_list_id. The title ILIKE stays for the owner's mine lens only.
            conds = [ExcessLineItem.manufacturer.ilike(f"%{escape_like(q)}%", escape="\\")]
            norm_q = normalize_mpn_key(q)
            if norm_q:
                conds.append(ExcessLineItem.normalized_part_number.ilike(f"%{escape_like(norm_q)}%", escape="\\"))
            part_match = select(ExcessLineItem.excess_list_id).where(or_(*conds))
            query = query.filter(ExcessList.id.in_(part_match))
        else:
            query = query.filter(ExcessList.title.ilike(f"%{escape_like(q)}%", escape="\\"))

    lists = query.order_by(ExcessList.updated_at.desc().nullslast(), ExcessList.id.desc()).all()
    cards = _list_cards(db, lists, can_see_customer=can_see_customer)

    return template_response(
        "htmx/partials/resell/_lists.html",
        {
            "request": request,
            "user": user,
            "lens": lens,
            "stage": stage,
            "needs": needs,
            "q": q,
            "cards": cards,
            "can_see_customer": can_see_customer,
            "can_post": excess_service.can_post(user),
        },
    )


# ── Right detail + lazy tab bodies ───────────────────────────────────


# NB: this static route MUST be registered before the dynamic "/{list_id}" route below —
# otherwise FastAPI matches "create-form" against {list_id} and 422s on int parsing.
@router.get("/v2/partials/resell/create-form", response_class=HTMLResponse)
async def resell_create_form(
    request: Request,
    user: User = Depends(require_access(AccessKey.RESELL)),
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
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Right detail partial — slim header, breadcrumb, chips, lazy tabs."""
    el, _ = _get_list_for_user(db, list_id, user)
    return template_response("htmx/partials/resell/detail.html", _detail_context(request, db, el, user))


@router.get("/v2/partials/resell/{list_id}/lines", response_class=HTMLResponse)
async def resell_lines(
    request: Request,
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Lazy Lines tab body — adaptive: 1 line → card, ≥2 → compact table."""
    el, _ = _get_list_for_user(db, list_id, user)
    return template_response("htmx/partials/resell/_lines.html", _detail_context(request, db, el, user))


def _offers_context(request: Request, db: Session, el: ExcessList, user: User) -> dict:
    """Build the Offers tab context — per-line offer stacks + pinned take-all banners.

    Offers are the owner's private view: a non-owner gets an empty stack (the template
    renders the "offers are private" state instead). ``take_all_blocked`` is True once any
    line is already awarded, which disables a whole-list take-all award (it would collide
    with the per-line winner). Caller must have already authorized access to *el*.
    """
    is_owner = el.owner_id == user.id
    items = db.query(ExcessLineItem).filter_by(excess_list_id=el.id).order_by(ExcessLineItem.id).all()
    take_all_blocked = any(it.status == ExcessLineItemStatus.AWARDED for it in items)
    base = {
        "request": request,
        "user": user,
        "list": el,
        "line_items": items,
        "shape": "single" if len(items) == 1 else "table",
        "take_all_blocked": take_all_blocked,
    }

    if not is_owner:
        # Non-owner: render an empty offers view — no offer data in the response.
        return {
            **base,
            "by_line": {it.id: [] for it in items},
            "unmatched": [],
            "take_all_offers": [],
            "can_see_customer": False,
            "is_owner": False,
        }

    take_all_offers = (
        db.query(ExcessOffer)
        .filter(
            ExcessOffer.excess_list_id == el.id,
            ExcessOffer.scope == ExcessOfferScope.TAKE_ALL,
            ExcessOffer.status.in_([s.value for s in _VISIBLE_OFFER_STATUSES]),
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
            ExcessOffer.status.in_([s.value for s in _VISIBLE_OFFER_STATUSES]),
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

    return {
        **base,
        "by_line": by_line,
        "unmatched": unmatched,
        "take_all_offers": take_all_offers,
        "can_see_customer": True,
        "is_owner": True,
    }


def _award_response_context(request: Request, db: Session, el: ExcessList, user: User) -> dict:
    """Combined context for the award/unaward OOB response.

    ``_award_response.html`` swaps the Offers tab (its primary target) and, out-of-band,
    the Lines tab (awarded/withdrawn pills) and the header chips (the awarded-count chip +
    list-status badge) — so awarding never resets the Alpine ``tab`` state. Merging the
    two contexts is safe: the shared keys (request/user/list/line_items/shape) are equal.
    """
    return {**_detail_context(request, db, el, user), **_offers_context(request, db, el, user)}


@router.get("/v2/partials/resell/{list_id}/offers", response_class=HTMLResponse)
async def resell_offers(
    request: Request,
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Lazy Offers tab body — per-line offer stacks + pinned take-all banner."""
    el, _ = _get_list_for_user(db, list_id, user)
    return template_response("htmx/partials/resell/_offers.html", _offers_context(request, db, el, user))


@router.get("/v2/partials/resell/{list_id}/offers/export")
async def resell_offers_export(
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Stream the list's collected inbound offers as a CSV download (owner-only).

    Mirrors the Offers tab: the SAME owner-only gate (offers are the owner's private view —
    a non-owner 403s, matching resell_line_offer_compare) and the SAME visible-offer set
    (open/late/won/lost). One row per per-line offer line, plus one row per take-all offer
    (which carries no lines). Broker identity is always shown here — the endpoint is
    owner-only, so the customer-anonymization that governs the "Open to Me" lens never applies.
    """
    el, is_owner = _get_list_for_user(db, list_id, user)
    if not is_owner:
        raise HTTPException(403, "Offers are only visible to the list owner")

    offers = (
        db.query(ExcessOffer)
        .filter(
            ExcessOffer.excess_list_id == el.id,
            ExcessOffer.status.in_([s.value for s in _VISIBLE_OFFER_STATUSES]),
        )
        .options(
            joinedload(ExcessOffer.offerer_company),
            joinedload(ExcessOffer.offerer_vendor_card),
            joinedload(ExcessOffer.lines).joinedload(ExcessOfferLine.excess_line_item),
        )
        .order_by(ExcessOffer.created_at.desc(), ExcessOffer.id.desc())
        .all()
    )

    header = [
        "Offer ID",
        "Broker",
        "Scope",
        "MPN",
        "Quantity",
        "Unit Price",
        "Condition",
        "Lead Time (Days)",
        "Terms",
        "Take-All Total",
        "Status",
        "Received",
    ]

    def _rows():
        for offer in offers:
            broker = _offer_broker_label(offer)
            received = _fmt_dt(offer.created_at)
            if offer.scope == ExcessOfferScope.TAKE_ALL or not offer.lines:
                # Take-all binds the whole list with no line rows — one summary row.
                yield [
                    offer.id,
                    broker,
                    offer.scope,
                    "",
                    "",
                    "",
                    "",
                    "",
                    offer.notes,
                    offer.take_all_total_price,
                    offer.status,
                    received,
                ]
                continue
            for line in offer.lines:
                item = line.excess_line_item
                yield [
                    offer.id,
                    broker,
                    offer.scope,
                    line.mpn_raw,
                    line.quantity,
                    line.unit_price,
                    item.condition if item else "",
                    line.lead_time_days,
                    line.terms_text,
                    "",
                    offer.status,
                    received,
                ]

    return stream_csv(f"resell_offers_list_{el.id}.csv", header, _rows())


@router.get("/v2/partials/resell/{list_id}/lines/{line_id}/offers", response_class=HTMLResponse)
async def resell_line_offer_compare(
    request: Request,
    list_id: int,
    line_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
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
    visible = {s.value for s in _VISIBLE_OFFER_STATUSES}
    for offer in el.offers:
        if offer.scope != ExcessOfferScope.PER_LINE or offer.status not in visible:
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
    # Resolve the seller's send contact so the Send button can show WHERE the bid goes
    # (or disable + warn when no email is on file — never silently email nobody).
    recipient_name, recipient_email = bid_back_service.resolve_seller_contact(db, el)
    return {
        "request": request,
        "user": user,
        "list": el,
        "line_items": items,
        "line_count": len(items),
        "bid": bid,
        "summary": summary,
        "recipient_name": recipient_name,
        "recipient_email": recipient_email,
    }


@router.get("/v2/partials/resell/{list_id}/build-bid", response_class=HTMLResponse)
async def resell_build_bid(
    request: Request,
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
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
    user: User = Depends(require_access(AccessKey.RESELL)),
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
    user: User = Depends(require_access(AccessKey.RESELL)),
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


@router.post("/api/resell/{list_id}/bid/{bid_id}/send", response_class=HTMLResponse)
async def resell_send_bid(
    request: Request,
    list_id: int,
    bid_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
    token: str = Depends(require_fresh_token),
):
    """Email the clean bid-back PDF to the customer (owner-only): flip ``draft→sent``.

    Delegates to :func:`bid_back_service.send_bid_back`, which resolves the seller
    contact, renders the whitelisted PDF, sends via the RFQ engine (no requisition), and
    stamps ``sent_at`` only on a confirmed send. Re-renders the Build-Bid tab with a
    toast. A missing contact email surfaces as a 422 the toast reports.
    """
    await bid_back_service.send_bid_back(db, list_id=list_id, bid_id=bid_id, owner=user, token=token)
    el = excess_service.get_excess_list(db, list_id)
    resp = template_response("htmx/partials/resell/_build_bid.html", _build_bid_context(request, db, el, user))
    return _toast(resp, "Bid sent to the customer")


@router.post("/api/resell/{list_id}/bid/{bid_id}/accept", response_class=HTMLResponse)
async def resell_accept_bid(
    request: Request,
    list_id: int,
    bid_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Record the customer's ACCEPTANCE of a sent bid (owner-only): ``sent→accepted``.

    Owner logs the seller's answer (the seller is not a User). Re-renders the Build-Bid
    tab with a toast. The service 409s if the bid is not ``sent`` (can't accept a draft).
    """
    bid_back_service.record_bid_response(db, list_id=list_id, bid_id=bid_id, owner=user, accepted=True)
    el = excess_service.get_excess_list(db, list_id)
    resp = template_response("htmx/partials/resell/_build_bid.html", _build_bid_context(request, db, el, user))
    return _toast(resp, "Bid marked accepted")


@router.post("/api/resell/{list_id}/bid/{bid_id}/reject", response_class=HTMLResponse)
async def resell_reject_bid(
    request: Request,
    list_id: int,
    bid_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Record the customer's REJECTION of a sent bid (owner-only): ``sent→rejected``.

    Owner logs the seller's answer. Re-renders the Build-Bid tab with a toast. The service
    409s if the bid is not ``sent``.
    """
    bid_back_service.record_bid_response(db, list_id=list_id, bid_id=bid_id, owner=user, accepted=False)
    el = excess_service.get_excess_list(db, list_id)
    resp = template_response("htmx/partials/resell/_build_bid.html", _build_bid_context(request, db, el, user))
    return _toast(resp, "Bid marked rejected")


# ── Modal forms ──────────────────────────────────────────────────────


@router.get("/v2/partials/resell/{list_id}/add-line-form", response_class=HTMLResponse)
async def resell_add_line_form(
    request: Request,
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
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
    user: User = Depends(require_access(AccessKey.RESELL)),
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
    # Only ever rendered to a non-owner (is_owner 403s above), so the header shows the
    # anonymized label — never the seller-named free-text title.
    return template_response(
        "htmx/partials/resell/offer_form.html",
        {"request": request, "list": el, "display_title": _display_title(el, can_see_customer=is_owner)},
    )


# ── Mutations (thin — delegate to the service) ───────────────────────


@router.post("/api/resell/lists", response_class=HTMLResponse)
async def resell_create_list(
    request: Request,
    title: str = Form(...),
    company_id: int = Form(...),
    notes: str = Form(""),
    user: User = Depends(require_access(AccessKey.RESELL)),
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
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Add a single line, resolve its MaterialCard, re-render the Lines tab."""
    el = excess_service.get_excess_list(db, list_id)
    _require_owner(el, user)
    if el.status != ExcessListStatus.DRAFT:
        raise HTTPException(409, "Posted lists are locked; revise as a new version")
    if not excess_service.can_post(user):
        raise HTTPException(403, "You do not have permission to post excess lists")
    # L2: a non-positive quantity would reach the ExcessLineItem @validates("quantity")
    # ValueError and surface as an unhandled 500 — validate the bound here and return a
    # clear 400 instead.
    if quantity <= 0:
        raise HTTPException(400, "Quantity must be a positive whole number")
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
    user: User = Depends(require_access(AccessKey.RESELL)),
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
    user: User = Depends(require_access(AccessKey.RESELL)),
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
    user: User = Depends(require_access(AccessKey.RESELL)),
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
    user: User = Depends(require_access(AccessKey.RESELL)),
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
    user: User = Depends(require_access(AccessKey.RESELL)),
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
        # L2: reject a non-positive quantity here (400) — otherwise it reaches the
        # ExcessOfferLine @validates("quantity") ValueError as an unhandled 500.
        if not mpn_raw.strip() or qty is None or qty <= 0:
            raise HTTPException(400, "Per-line offer needs a part number and a positive quantity")
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


def _toast(resp: Response, message: str) -> Response:
    """Attach the ``showToast`` HX-Trigger so an award/unaward confirms even though the
    triggering button was swapped out of the DOM (same pattern as
    sightings._with_toast)."""
    resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": message, "type": "success"}})
    return resp


@router.post("/api/resell/{list_id}/offers/{offer_id}/award", response_class=HTMLResponse)
async def resell_award_offer(
    request: Request,
    list_id: int,
    offer_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Award an inbound offer (owner-only): flip it to ``won``, mark its lines sold,
    recompute the winning buyer's scorecard, and re-render the Offers tab + OOB
    lines/chips.

    The service owns the transaction (buyer-score hook, mirror-retire, list-status
    derivation) and enforces owner-gating, 404 on a missing offer, and 409 when a line is
    already awarded to a different offer. The response is an OOB compose so awarding from a
    tab never resets the Alpine ``tab`` state; the toast fires via HX-Trigger.
    """
    offer = excess_service.award_offer(db, offer_id, user)
    el = excess_service.get_excess_list(db, offer.excess_list_id)
    resp = template_response(
        "htmx/partials/resell/_award_response.html", _award_response_context(request, db, el, user)
    )
    return _toast(resp, "Offer awarded")


@router.post("/api/resell/{list_id}/offers/{offer_id}/unaward", response_class=HTMLResponse)
async def resell_unaward_offer(
    request: Request,
    list_id: int,
    offer_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Reverse an award (owner-only): flip the offer back to ``open``, return its lines
    to the pool, and re-render the Offers tab + OOB lines/chips.

    The explicit inverse of award — never a silent auto-swap to a different winner. The
    service enforces owner-gating, 404 on a missing offer, and 409 when the offer is not
    awarded (nothing to reverse).
    """
    offer = excess_service.unaward_offer(db, offer_id, user)
    el = excess_service.get_excess_list(db, offer.excess_list_id)
    resp = template_response(
        "htmx/partials/resell/_award_response.html", _award_response_context(request, db, el, user)
    )
    return _toast(resp, "Award reversed")


@router.post("/api/resell/{list_id}/offers/{offer_id}/withdraw", response_class=HTMLResponse)
async def resell_withdraw_offer(
    request: Request,
    list_id: int,
    offer_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Withdraw an inbound offer, then re-render the Offers tab + OOB lines/chips.

    Authorized for the offer's SUBMITTER (a buyer retracting their own bid) OR the list
    OWNER (clearing a stale / erroneous offer). Only an open/late offer may be withdrawn —
    a won offer is 409 (unaward it first), and a lost/withdrawn one is already closed. The
    service (``withdraw_offer``) flips the status to ``withdrawn`` and recomputes every
    touched line's rollup; the withdrawn offer then drops out of the Offers tab.
    """
    offer = db.get(ExcessOffer, offer_id)
    if offer is None or offer.excess_list_id != list_id:
        raise HTTPException(404, f"Offer {offer_id} not found on list {list_id}")
    el = excess_service.get_excess_list(db, list_id)
    if user.id != offer.submitted_by and user.id != el.owner_id:
        raise HTTPException(403, "You can only withdraw your own offer")
    if offer.status not in {s.value for s in _WITHDRAWABLE_OFFER_STATUSES}:
        raise HTTPException(409, "Only an open offer can be withdrawn — unaward a won offer first")

    excess_service.withdraw_offer(db, offer_id)
    el = excess_service.get_excess_list(db, list_id)
    resp = template_response(
        "htmx/partials/resell/_award_response.html", _award_response_context(request, db, el, user)
    )
    return _toast(resp, "Offer withdrawn")


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
    # Batch the advisory overlap flag for every ranked buyer (M8: was one query per buyer).
    overlaps = buyer_affinity_service.overlap_warnings_for(
        db,
        excess_list_id=el.id,
        target_vendor_card_ids=[rb.vendor_card_id for rb in ranked],
        owner_id=owner.id,
    )
    return [{"buyer": rb, "overlap": overlaps.get(rb.vendor_card_id)} for rb in ranked]


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
    # Line count for the neutral outreach subject prefill (#11) — the campaign's scope:
    # the selected lines, or the whole list. NEVER the title (which names the customer).
    line_count = (
        len(scope_lines)
        if scope_lines
        else (db.scalar(select(func.count(ExcessLineItem.id)).where(ExcessLineItem.excess_list_id == el.id)) or 0)
    )
    return {
        "request": request,
        "user": owner,
        "list": el,
        "suggestions": suggestions,
        "no_contact_buyers": _no_contact_buyers(db, el, suggested_ids),
        "channels": [c.value for c in ExcessOutreachChannel],
        "line_ids": line_ids or [],
        "scope_lines": scope_lines,
        "line_count": line_count,
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
    # (buyer × line) row — a 3-line per-line campaign is one buyer offered, not three. Only
    # genuinely-sent rows count as "offered": a ``sending`` / ``failed`` / ``interrupted``
    # row never reached the buyer, so it must not inflate the offered tally.
    offered = {
        r.target_vendor_card_id
        for r in rows
        if r.target_vendor_card_id is not None and r.status not in buyer_affinity_service._NOT_SENT_STATUSES
    }
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
        # The shared not-sent set (as string values) so the template renders "—" for a
        # non-sent row's "When" instead of its meaningless created_at.
        "not_sent_statuses": [s.value for s in buyer_affinity_service._NOT_SENT_STATUSES],
        # Drives the tracker's self-poll: while any row is still ``sending`` (its
        # background send job has not finalized), the tab polls itself for the final state.
        "any_sending": any(r.status == ExcessOutreachStatus.SENDING for r in rows),
    }


def _replies_context(db: Session, el: ExcessList) -> dict[str, dict]:
    """Map each of the list's outreach conversations to its buyer replies.

    Joins the buyer's inbound emails (``VendorResponse``, one per received message, written
    by the inbox poll and carrying the reply body) back to the ``ExcessOutreach`` they
    answered — on the shared ``graph_conversation_id`` the send path stamped. Returns
    ``{conversation_id: {"outreach": ExcessOutreach, "replies": [VendorResponse, …]}}`` with
    replies newest-first, so the reply viewer can render one conversation's thread. Only
    outreach rows with a conversation id participate (an unstamped row has no thread to show).
    """
    outreach_rows = (
        db.query(ExcessOutreach)
        .filter(
            ExcessOutreach.excess_list_id == el.id,
            ExcessOutreach.graph_conversation_id.isnot(None),
        )
        .all()
    )
    owning: dict[str, ExcessOutreach] = {}
    for row in outreach_rows:
        # Per-line campaigns write several rows on one conversation; keep the first as the
        # display anchor (they share buyer + list, which is all the viewer reads).
        owning.setdefault(row.graph_conversation_id, row)

    replies: dict[str, list[VendorResponse]] = {}
    if owning:
        for vr in (
            db.query(VendorResponse)
            .filter(VendorResponse.graph_conversation_id.in_(list(owning.keys())))
            .order_by(VendorResponse.received_at.desc())
            .all()
        ):
            replies.setdefault(vr.graph_conversation_id, []).append(vr)

    return {conv: {"outreach": owning[conv], "replies": replies.get(conv, [])} for conv in owning}


@router.get("/v2/partials/resell/{list_id}/offer-buyers-form", response_class=HTMLResponse)
async def resell_offer_buyers_form(
    request: Request,
    list_id: int,
    line_ids: str = Query(""),
    preselect_vendor_card_id: str = Query(""),
    user: User = Depends(require_access(AccessKey.RESELL)),
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
    user: User = Depends(require_access(AccessKey.RESELL)),
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


@router.get("/v2/partials/resell/{list_id}/outreach/export")
async def resell_outreach_export(
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Stream the list's outreach tracker as a CSV download (owner-only).

    Reuses the tracker's row query + the SAME owner gate (_require_owner) as the tracker
    tab: one row per buyer x line touch — buyer · line · channel · by · status · sent ·
    last activity — newest first (identical order to the tab).
    """
    el = excess_service.get_excess_list(db, list_id)
    _require_owner(el, user)
    rows = (
        db.query(ExcessOutreach)
        .filter(ExcessOutreach.excess_list_id == el.id)
        .options(
            joinedload(ExcessOutreach.target_vendor_card),
            joinedload(ExcessOutreach.excess_line_item),
            joinedload(ExcessOutreach.submitted_by_user),
        )
        .order_by(ExcessOutreach.created_at.desc(), ExcessOutreach.id.desc())
        .all()
    )

    header = ["Buyer", "Line", "Channel", "Sent By", "Status", "Sent At", "Last Activity", "Note"]
    not_sent = buyer_affinity_service._NOT_SENT_STATUSES

    def _rows():
        for r in rows:
            # Mirror the tracker's "When": a non-sent row (sending / failed / interrupted)
            # never reached the buyer, so its created_at is NOT a real send time — leave the
            # "Sent At" cell blank instead of misreporting the row-creation time as a send.
            sent_at = "" if r.status in not_sent else _fmt_dt(r.sent_at or r.created_at)
            yield [
                r.target_vendor_card.display_name if r.target_vendor_card else "Unknown buyer",
                r.excess_line_item.part_number if r.excess_line_item else "Whole list",
                r.channel,
                r.submitted_by_user.name if r.submitted_by_user else "",
                r.status,
                sent_at,
                _fmt_dt(r.updated_at),
                # Surface the persisted send-failure / degraded-reply-matching reason so an
                # exported failed/interrupted (or delivered-but-degraded) row is not silent.
                r.send_error or "",
            ]

    return stream_csv(f"resell_outreach_list_{el.id}.csv", header, _rows())


@router.get("/v2/partials/resell/{list_id}/not-yet-strip", response_class=HTMLResponse)
async def resell_not_yet_strip(
    request: Request,
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
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
    due = datetime.now(UTC)
    for b in buyers:
        task_service.auto_create_resell_followup_task(
            db,
            excess_list_id=el.id,
            vendor_card_id=b.vendor_card_id,
            owner_id=el.owner_id,
            buyer_name=b.display_name,
            due_at=due,
        )
    return template_response(
        "htmx/partials/resell/_not_yet_strip.html",
        {"request": request, "user": user, "list": el, "buyers": buyers},
    )


@router.post("/api/resell/{list_id}/outreach", response_class=HTMLResponse)
async def resell_submit_outreach(
    request: Request,
    background_tasks: BackgroundTasks,
    list_id: int,
    vendor_card_ids: str = Form(""),
    company_ids: str = Form(""),
    scope: str = Form("whole_list"),
    channel: str = Form(ExcessOutreachChannel.EMAIL),
    line_ids: str = Form(""),
    notes: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
    token: str = Depends(require_fresh_token),
):
    """Submit an outreach campaign (owner-only), then re-render the tracker.

    Buyers arrive as ``vendor_card_ids`` (ranked picks) and/or ``company_ids`` (a
    manual-add company with no card yet — the service backfills a card). ``channel`` ==
    ``email`` writes the tracker rows in the transient ``sending`` state via
    :func:`resell_outreach_service.enqueue_outreach_email` and hands the actual send +
    per-buyer sent-message lookups to a background job
    (:func:`resell_outreach_service.run_outreach_email_send`) so a multi-buyer send never
    blocks the modal — the tracker re-renders at once showing ``sending`` and polls itself
    to the final status. Any other channel is a manual log via :func:`submit_outreach`.
    ``scope`` is ``per_line`` (scoped to ``line_ids``) or ``whole_list``. The service
    enforces the owner + can_post guards.
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
        # Phase 1 (fast, inline): write the rows as ``sending`` and return at once.
        _rows, plan = resell_outreach_service.enqueue_outreach_email(
            db,
            list_id=list_id,
            owner=user,
            buyers=buyers,
            scope=scope_value,
            subject=subject.strip(),
            body=body.strip(),
            line_item_ids=parsed_lines,
        )
        # Phase 2 (background): the multi-buyer send + per-buyer Graph sent-message
        # lookups run off the request path and advance each row to its final status.
        background_tasks.add_task(
            resell_outreach_service.run_outreach_email_send,
            list_id=list_id,
            owner_id=user.id,
            subject=subject.strip(),
            body=body.strip(),
            token=token,
            groups=plan,
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


# Retry prefers the EXACT subject/body the campaign was sent with (persisted on the row
# since Phase 2: ``send_subject`` / ``send_body``) so the Sent-folder reconcile matches and
# a customized send re-sends verbatim. These are only the FALLBACK for a row missing that
# persisted text (legacy / cleared-subject rows). The subject ships EXTERNALLY to the buyer,
# so the fallback must stay anonymized — a part-count subject, NEVER ``el.title`` (which
# traders write as the customer name, the #11/#12 leak class the modal prefill + internal
# ActivityLog subject already neutralized). Kept in sync with offer_buyers_modal.html.
_RETRY_BODY = "We have the following excess available — let us know if you'd like to bid."


def _neutral_outreach_subject(line_count: int) -> str:
    """Neutral, part-count outreach subject — mirrors the compose-modal prefill.

    Used as the retry resend's fallback subject when a row has no persisted ``send_subject``.
    NEVER embeds ``el.title`` (the customer name), so the anonymized listing stays anonymized
    on the external send. Matches ``offer_buyers_modal.html``'s ``Excess available: N line(s)``.
    """
    return f"Excess available: {line_count} line" + ("s" if line_count != 1 else "")


# Outreach statuses a failed send can be retried FROM (a genuine send failure or an
# interrupted/orphaned send — never a live sending/sent/engaged row).
_RETRYABLE_OUTREACH = (ExcessOutreachStatus.FAILED, ExcessOutreachStatus.INTERRUPTED)


@router.post("/api/resell/{list_id}/outreach/{outreach_id}/retry", response_class=HTMLResponse)
async def resell_retry_outreach(
    request: Request,
    background_tasks: BackgroundTasks,
    list_id: int,
    outreach_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
    token: str = Depends(require_fresh_token),
):
    """Retry a failed / interrupted EMAIL outreach (owner-only), then re-render the
    tracker.

    Optimistically flips the row back to ``sending`` (so the tracker re-render shows it +
    polls) and hands the reconcile-first resend to a background job
    (:func:`resell_outreach_service.retry_outreach_send`), which re-checks the Sent folder
    BEFORE resending so an already-delivered row is reconciled to ``sent`` instead of
    double-sent. 404 when the row is missing / on another list; 409 when it is not in a
    retryable state or is not an email outreach.
    """
    el = excess_service.get_excess_list(db, list_id)
    _require_owner(el, user)
    row = db.get(ExcessOutreach, outreach_id)
    if row is None or row.excess_list_id != el.id:
        raise HTTPException(404, "Outreach not found")
    if row.channel != ExcessOutreachChannel.EMAIL:
        raise HTTPException(409, "Only an email outreach can be retried")
    if row.status not in _RETRYABLE_OUTREACH:
        raise HTTPException(409, "Only a failed or interrupted outreach can be retried")

    line_count = db.scalar(select(func.count(ExcessLineItem.id)).where(ExcessLineItem.excess_list_id == el.id)) or 0
    subject = _neutral_outreach_subject(line_count)
    body = _RETRY_BODY
    # Optimistic flip so the tracker shows ``sending`` + self-polls to the final state.
    # Refresh ``created_at`` to now so the row is not "born stale": the nightly stale-sending
    # sweeper selects on ``created_at < now - 30min`` (the sending-started proxy), and the
    # row's created_at still holds the original, possibly hours-old enqueue time — without
    # this the sweeper could flip the in-flight retry to ``interrupted`` mid-resend.
    row.status = ExcessOutreachStatus.SENDING
    row.send_error = None
    row.sent_at = None
    row.created_at = datetime.now(UTC)
    db.commit()

    background_tasks.add_task(
        resell_outreach_service.retry_outreach_send,
        outreach_id=outreach_id,
        owner_id=user.id,
        subject=subject,
        body=body,
        token=token,
    )
    el = excess_service.get_excess_list(db, list_id)
    return template_response("htmx/partials/resell/_outreach.html", _outreach_tracker_context(request, db, el, user))


def _load_outreach_for_owner(
    db: Session, list_id: int, outreach_id: int, user: User
) -> tuple[ExcessList, ExcessOutreach]:
    """Owner-gated load of one outreach row on a list, requiring an email thread.

    404 (not 403) when the row is missing or belongs to another list — existence is not
    revealed. 404 when the row has no ``graph_conversation_id`` (a manual-log or degraded
    send has no thread to view or convert). Shared by the reply-viewer + convert-to-offer
    routes so both enforce the same guard.
    """
    el = excess_service.get_excess_list(db, list_id)
    _require_owner(el, user)
    outreach = db.get(ExcessOutreach, outreach_id)
    if outreach is None or outreach.excess_list_id != el.id:
        raise HTTPException(404, "Outreach not found")
    if not outreach.graph_conversation_id:
        raise HTTPException(404, "No email thread on this outreach")
    return el, outreach


@router.get("/v2/partials/resell/{list_id}/outreach/{outreach_id}/reply", response_class=HTMLResponse)
async def resell_outreach_reply(
    request: Request,
    list_id: int,
    outreach_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Reply viewer for one buyer×list outreach (owner-only): the reply thread + a
    convert-to-offer quick-add.

    Loads the buyer's inbound emails on this outreach's conversation and renders them
    with a "Convert to offer" form, so the trader can turn a free-text reply into a
    tracked inbound ExcessOffer. Owner's private view (403 non-owner); 404 when the
    outreach has no email thread.
    """
    el, outreach = _load_outreach_for_owner(db, list_id, outreach_id, user)
    ctx = _replies_context(db, el).get(outreach.graph_conversation_id, {})
    return template_response(
        "htmx/partials/resell/_reply_viewer.html",
        {
            "request": request,
            "user": user,
            "list": el,
            "outreach": outreach,
            "replies": ctx.get("replies", []),
        },
    )


@router.post("/api/resell/{list_id}/outreach/{outreach_id}/offer", response_class=HTMLResponse)
async def resell_outreach_convert_offer(
    request: Request,
    list_id: int,
    outreach_id: int,
    mpn_raw: str = Form(""),
    quantity: str = Form(""),
    unit_price: str = Form(""),
    lead_time_days: str = Form(""),
    terms_text: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Convert a buyer's reply into a tracked inbound offer (owner-only), then re-render
    the tracker.

    Human-reviewed offer extraction: the trader reads the reply and types the line. Reuses
    :func:`resell_outreach_service.record_response` (``has_offer=True``) so the offer is
    created via the SAME queued-never-dropped line matcher as an emailed bid and the
    outreach advances sent/responded → ``bid``. Owner-gated; 404 when there is no thread.
    """
    el, outreach = _load_outreach_for_owner(db, list_id, outreach_id, user)

    qty = _to_int(quantity)
    if not mpn_raw.strip() or qty is None:
        raise HTTPException(400, "A converted offer needs a part number and quantity")

    resell_outreach_service.record_response(
        db,
        conversation_id=outreach.graph_conversation_id,
        has_offer=True,
        offer_lines=[
            {
                "mpn_raw": mpn_raw.strip(),
                "quantity": qty,
                "unit_price": _to_decimal(unit_price),
                "lead_time_days": _to_int(lead_time_days),
                "terms_text": terms_text or None,
            }
        ],
        offer_notes=notes or None,
    )

    el = excess_service.get_excess_list(db, list_id)
    return template_response("htmx/partials/resell/_outreach.html", _outreach_tracker_context(request, db, el, user))


# ── CSV export helpers ───────────────────────────────────────────────


def _fmt_dt(dt: datetime | None) -> str:
    """Minute-precision timestamp for a CSV cell (empty string when missing)."""
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def _offer_broker_label(offer: ExcessOffer) -> str:
    """The competing broker's name for the owner-only offers export.

    Mirrors the ``_broker_label`` macro's owner branch (company → vendor card → id
    fallback). Only ever called from the owner-gated export, so it never anonymizes.
    """
    # Typed locals: both relationship chains are legacy untyped reads.
    label: str
    if offer.offerer_company:
        label = offer.offerer_company.name
        return label
    if offer.offerer_vendor_card:
        label = offer.offerer_vendor_card.display_name
        return label
    return f"Broker #{offer.id}"


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
