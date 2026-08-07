"""Resell board — workspace shell, list rail (cards / rows / stat strip), create-list
flow.

W4.8 split of the 2,830-line app/routers/resell.py — pure structural move: URLs and
behavior unchanged; every route attaches to the shared router imported from .common
(registration assembled in __init__).
"""

from datetime import UTC, datetime, timedelta

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, joinedload

from ...constants import (
    AccessKey,
    ExcessListStatus,
    ExcessOfferScope,
)
from ...database import get_db
from ...dependencies import require_access
from ...models import Company, User
from ...models.excess import ExcessLineItem, ExcessList, ExcessOffer
from ...services import (
    excess_service,
)
from ...template_env import template_response
from ...utils.normalization import normalize_mpn_key
from ...utils.sql_helpers import escape_like
from .common import (
    _POSTED_STATUSES,
    _UNACTIONED_OFFER_STATUSES,
    _close_at_display,
    _display_title,
    _hours_until,
    _is_live,
    _to_int,
    router,
)


def _parse_close_at(raw: str, tz_offset_min: str = "") -> datetime | None:
    """Parse the D1 ``datetime-local`` form value into a tz-aware UTC instant (or None).

    An empty field means "no deadline". A ``datetime-local`` input carries a naive LOCAL
    wall-clock string (``2026-07-20T15:30``) with no zone, so the modal posts the browser's
    UTC offset alongside it (a hidden ``tz_offset_min`` filled from
    ``Date.getTimezoneOffset()``: minutes such that UTC = local + offset, e.g. 240 at
    UTC-4). The wall-clock is converted to the real UTC instant here (finding #20 — the old
    ``replace(tzinfo=UTC)`` stamped the local wall-clock as UTC, shifting the deadline by
    the user's offset and spuriously 400-ing locally-future deadlines). A missing/invalid
    offset falls back to treating the wall-clock as UTC (the legacy semantic). A malformed
    datetime is a 400 (the service enforces future+non-past).
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(400, "Invalid offer-close date/time") from exc
    if parsed.tzinfo is not None:
        return parsed
    offset = _to_int(tz_offset_min)
    if offset is not None and -16 * 60 <= offset <= 16 * 60:
        # UTC = local wall-clock + getTimezoneOffset() minutes.
        return parsed.replace(tzinfo=UTC) + timedelta(minutes=offset)
    return parsed.replace(tzinfo=UTC)


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
                # Chip gate (finding #8): countdown only while live; a resolved list shows a
                # muted "closed {date}" (never a red "Overdue").
                "is_live": _is_live(el),
                "close_at_display": _close_at_display(getattr(el, "close_at", None)),
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
        "live": status_counts.get(ExcessListStatus.POSTED, 0) + status_counts.get(ExcessListStatus.BIDDING, 0),
        "offers_to_review": sum(offers_by_scope.values()),
        "take_all": offers_by_scope.get(ExcessOfferScope.TAKE_ALL, 0),
        "awarded": status_counts.get(ExcessListStatus.AWARDED, 0),
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
    # PARKED (spec §5.3, W2.3 — solo-operator): the "Open to Me" offerer lens is
    # parked until a second trader user exists; every request renders the owner
    # ("mine") lens. The open-lens implementation in _list_rows_context stays.
    lens = "mine"
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


# Left-list page size: the workspace list is capped at the query level and revealed
# page-by-page via a "Load more" row (perf: the old unbounded ``.all()`` re-ran the whole
# table on every filter keystroke). Counts for the stat cards come from _stat_strip, so
# capping the visible rows loses nothing.
_LIST_PAGE_SIZE = 50


def _list_rows_context(
    request: Request,
    db: Session,
    user: User,
    *,
    lens: str,
    stage: str,
    needs: str,
    q: str,
    offset: int = 0,
) -> dict:
    """Build the left-list context shared by the full partial and the rows-only swap.

    ``lens=mine`` → lists this user owns (seller identity visible).
    ``lens=open`` → posted lists owned by OTHERS that this user may offer on
    (customer-anonymized — pure whitelist, never the seller).

    ``stage`` filters on list STATUS (posted/bidding/…). ``needs`` is the offer-based
    triage dimension the status filter can't express: ``needs=offers`` → lists with ≥1
    live, unactioned offer; ``needs=take_all`` → lists with a live whole-list offer. These
    back the "Offers to review" / "Take-all" stat cards (their counts come from offers, not
    a list status), so they need their own filter rather than a status value.

    Rows are paged at the query level (``_LIST_PAGE_SIZE`` per page, ``offset`` from the
    "Load more" reveal); ``has_more``/``next_offset`` drive the Load-more row.
    """
    # PARKED (spec §5.3, W2.3): the offerer-facing ``lens=open`` branch below is parked
    # until a second trader user exists — every request is coerced to the owner lens.
    lens = "mine"
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

    # Finding #13 (the surviving D2 oracle): in the open lens the posted/bidding SPLIT is
    # itself an offer-existence signal (submit_offer flips posted→bidding on the first
    # inbound bid), so a non-owner diffing ``stage=bidding`` against ``stage=posted``
    # learns exactly which anonymized postings have drawn a competing bid. Merge the three
    # live-window tokens onto the SAME _LIVE_STATUSES set for non-owners — every one
    # answers only "can I still bid on it?". The ``awarded`` token is a lifecycle
    # facts, not the protected offer-existence signal, and pass through unchanged. Only the
    # FILTER merges: the context keeps the REQUESTED token, so the clicked pill keeps its
    # active state and the search/Load-more URLs echo it (a pure reflection of the request
    # — no server state — so no oracle; the server re-merges on every round trip).
    stage_filter = stage
    if not can_see_customer and stage in ("posted", "bidding"):
        stage_filter = "live"

    if stage_filter == "live":
        # ``live`` = [posted, bidding] (finding #16): the "Live" triage card counts BOTH
        # (a list flips posted→bidding on its first offer but is still live), so its filter
        # must widen to match its count. The strict ``posted`` pill keeps meaning EXACTLY
        # status=posted — only this token widens (owner lens; non-owners are merged above).
        query = query.filter(ExcessList.status.in_([ExcessListStatus.POSTED, ExcessListStatus.BIDDING]))
    elif stage_filter:
        query = query.filter(ExcessList.status == stage_filter)
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

    offset = max(offset, 0)
    # Fetch one row beyond the page to learn whether a "Load more" reveal is needed
    # WITHOUT a second COUNT query.
    lists = (
        query.order_by(ExcessList.updated_at.desc().nullslast(), ExcessList.id.desc())
        .offset(offset)
        .limit(_LIST_PAGE_SIZE + 1)
        .all()
    )
    has_more = len(lists) > _LIST_PAGE_SIZE
    lists = lists[:_LIST_PAGE_SIZE]
    cards = _list_cards(db, lists, can_see_customer=can_see_customer)

    return {
        "request": request,
        "user": user,
        "lens": lens,
        "stage": stage,
        "needs": needs,
        "q": q,
        "cards": cards,
        "has_more": has_more,
        "next_offset": offset + _LIST_PAGE_SIZE,
        "offset": offset,
        "can_see_customer": can_see_customer,
        "can_post": excess_service.can_post(user),
    }


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
    """Left list partial — filter bar + first page of opportunity rows.

    See :func:`_list_rows_context` for the lens/stage/needs/q semantics. The search input
    inside the rendered bar swaps ONLY the ``#resell-list-rows`` container (via
    :func:`resell_list_rows`), so typing never replaces the input itself (finding #17).
    """
    return template_response(
        "htmx/partials/resell/_lists.html",
        _list_rows_context(request, db, user, lens=lens, stage=stage, needs=needs, q=q),
    )


@router.get("/v2/partials/resell/list-rows", response_class=HTMLResponse)
async def resell_list_rows(
    request: Request,
    lens: str = Query("mine"),
    stage: str = Query(""),
    needs: str = Query(""),
    q: str = Query(""),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Rows-only swap for the left list: debounced search + the "Load more" reveal.

    The filter bar (pills + search input) stays OUTSIDE this swap (finding #17 — the
    sightings/knowledge/parts pattern), so a debounced keystroke never replaces the input
    being typed in. ``offset`` pages further rows in (perf: the list is capped at
    ``_LIST_PAGE_SIZE`` per page at the query level).
    """
    return template_response(
        "htmx/partials/resell/_list_rows.html",
        _list_rows_context(request, db, user, lens=lens, stage=stage, needs=needs, q=q, offset=offset),
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
    # (id, name) tuples only — the dropdown never needs the full Company entity (perf:
    # the old full-table ORM load dragged every column of every company into the modal).
    companies = db.execute(select(Company.id, Company.name).order_by(Company.name)).all()
    return template_response(
        "htmx/partials/resell/create_modal.html",
        {"request": request, "companies": companies},
    )


# ── Mutations (thin — delegate to the service) ───────────────────────


@router.post("/api/resell/lists", response_class=HTMLResponse)
async def resell_create_list(
    request: Request,
    title: str = Form(...),
    company_id: int = Form(...),
    notes: str = Form(""),
    close_at: str = Form(""),
    tz_offset_min: str = Form(""),
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Create a new excess list (owner = current user); re-render the My-Lists list.

    ``close_at`` is the optional D1 "Offers close by" ``datetime-local`` value (a naive
    LOCAL wall-clock string); ``tz_offset_min`` is the browser's UTC offset (a hidden
    input from ``Date.getTimezoneOffset()``) so the wall-clock converts to the real UTC
    instant (finding #20). The service rejects a past deadline.
    """
    if not excess_service.can_post(user):
        raise HTTPException(403, "You do not have permission to post excess lists")
    excess_service.create_excess_list(
        db,
        title=title,
        company_id=company_id,
        owner_id=user.id,
        notes=notes or None,
        close_at=_parse_close_at(close_at, tz_offset_min),
    )
    return await resell_lists(request, lens="mine", stage="", q="", user=user, db=db)
