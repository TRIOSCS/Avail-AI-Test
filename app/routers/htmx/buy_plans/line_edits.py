"""Editable plan lines (epic I) — add / edit / remove / bulk save, with the optional-
int/float form parsers.

W4.8 split of the 1,543-line app/routers/htmx/buy_plans.py — pure structural
move: URLs and behavior unchanged; every route attaches to the shared router
imported from .common (registration assembled in __init__).
"""

import json
from datetime import datetime

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session, joinedload

from ....constants import (
    BuyPlanStatus,
)
from ....database import get_db
from ....dependencies import (
    get_buyplan_for_user,
    require_user,
)
from ....models import (
    BuyPlan,
    User,
)
from ....services.stale_guard import StaleEditError, ensure_not_stale, stale_conflict_response
from .common import _notify_if_completed, buy_plan_detail_partial, router


def _parse_optional_int(raw: str | None) -> int | None:
    """Parse an optional whole-number form field: blank → None; non-numeric → 400."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "Expected a whole number.") from e


def _parse_optional_float(raw: str | None) -> float | None:
    """Parse an optional decimal form field: blank → None; non-numeric → 400."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "Expected a number.") from e


@router.post("/v2/partials/buy-plans/{plan_id}/lines/add", response_class=HTMLResponse)
async def buy_plan_add_line_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a line (vendor offer + qty + sell) to an editable plan (epic I).

    Role×status gate is enforced in the service (PermissionError → 403). Bad input (non-
    numeric / missing offer / wrong requisition) → 400.
    """
    from ....services.buyplan_workflow import add_buy_plan_line

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation. Same
    # loader options as add_buy_plan_line's own db.get() so the ownership pre-check's
    # load isn't silently wasted — a bare Session.get() on a PK already in the identity
    # map does NOT retroactively apply new loader options, so without this the service's
    # joinedload(BuyPlan.lines)/joinedload(BuyPlan.requisition) would do nothing and
    # plan.lines/plan.requisition would lazy-load one row at a time instead.
    plan = get_buyplan_for_user(db, user, plan_id, options=[joinedload(BuyPlan.lines), joinedload(BuyPlan.requisition)])

    form = await request.form()
    # Stale-edit guard (2.1): a new line's narrowest EXISTING object is the plan.
    try:
        ensure_not_stale(plan, form.get("expected_updated_at"))
    except StaleEditError:
        return stale_conflict_response()
    try:
        requirement_id = int(form.get("requirement_id") or 0)
        offer_id = int(form.get("offer_id") or 0)
        quantity = int(form.get("quantity") or 0)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "Requirement, vendor offer and a whole-number quantity are required.") from e
    unit_sell = _parse_optional_float(form.get("unit_sell"))

    try:
        add_buy_plan_line(plan_id, requirement_id, offer_id, quantity, user, db, unit_sell=unit_sell)
        db.commit()
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/edit", response_class=HTMLResponse)
async def buy_plan_edit_line_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Edit a line's qty / sell price / vendor(offer) on an editable plan (epic I).

    Only the submitted fields change (blank = unchanged). Role×status gate in the
    service (PermissionError → 403); a cut-PO vendor/qty change or bad input → 400.
    """
    from ....services.buyplan_workflow import edit_buy_plan_line

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation. Matches
    # edit_buy_plan_line's own loader options (see buy_plan_add_line_partial for why).
    plan = get_buyplan_for_user(db, user, plan_id, options=[joinedload(BuyPlan.lines), joinedload(BuyPlan.requisition)])

    form = await request.form()
    # Stale-edit guard (2.1): the narrowest edited object is the LINE.
    target_line = next((ln for ln in (plan.lines or []) if ln.id == line_id), None)
    if target_line is not None:
        try:
            ensure_not_stale(target_line, form.get("expected_updated_at"))
        except StaleEditError:
            return stale_conflict_response()
    quantity = _parse_optional_int(form.get("quantity"))
    unit_sell = _parse_optional_float(form.get("unit_sell"))
    offer_id = _parse_optional_int(form.get("offer_id"))
    # Manager edit-anything-at-verify fields (2.3) — the service refuses them for
    # anyone but a manager/admin on a PENDING_VERIFY line. po_number keeps the
    # present-vs-absent distinction: the field ABSENT is a no-op (None → _UNSET in the
    # service), while present-but-EMPTY is an explicit clear of an erroneous number
    # (audited old→"" by the service; empty-on-empty stays a no-op).
    po_number_raw = form.get("po_number")
    po_number = str(po_number_raw).strip() if po_number_raw is not None else None
    unit_cost = _parse_optional_float(form.get("unit_cost"))
    ship_date_str = (form.get("estimated_ship_date") or "").strip()
    estimated_ship_date = None
    if ship_date_str:
        try:
            estimated_ship_date = datetime.fromisoformat(ship_date_str)
        except ValueError as e:
            raise HTTPException(400, "Expected an ISO date for the estimated ship date.") from e

    try:
        edit_buy_plan_line(
            plan_id,
            line_id,
            user,
            db,
            quantity=quantity,
            unit_sell=unit_sell,
            offer_id=offer_id,
            po_number=po_number,
            estimated_ship_date=estimated_ship_date,
            unit_cost=unit_cost,
        )
        db.commit()
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    if form.get("origin") == "approvals_workspace":
        from ..approvals_hub import render_po_pane

        resp = render_po_pane(request, user, db, line_id)
        resp.headers["HX-Trigger"] = "awListRefresh"
        return resp

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/remove", response_class=HTMLResponse)
async def buy_plan_remove_line_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Remove a line from an editable plan (epic I).

    Role×status gate in the service (PermissionError → 403); removing a cut-PO line →
    400. ``remove_buy_plan_line`` already auto-completes at service depth (removing the
    plan's last open line can leave every remaining line terminal); the returned plan's
    ``.status`` is read BEFORE commit to drive ``_notify_if_completed`` without re-
    deriving the fact via a second ``check_completion`` scan.
    """
    from ....services.buyplan_workflow import remove_buy_plan_line

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation. Matches
    # remove_buy_plan_line's own loader options (see buy_plan_add_line_partial).
    plan = get_buyplan_for_user(db, user, plan_id, options=[joinedload(BuyPlan.lines), joinedload(BuyPlan.requisition)])

    # Stale-edit guard (2.1): the narrowest edited object is the LINE being removed.
    form = await request.form()
    target_line = next((ln for ln in (plan.lines or []) if ln.id == line_id), None)
    if target_line is not None:
        try:
            ensure_not_stale(target_line, form.get("expected_updated_at"))
        except StaleEditError:
            return stale_conflict_response()

    try:
        updated = remove_buy_plan_line(plan_id, line_id, user, db)
        just_completed = updated.status == BuyPlanStatus.COMPLETED.value
        db.commit()
        await _notify_if_completed(plan_id, just_completed)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/bulk", response_class=HTMLResponse)
async def buy_plan_bulk_lines_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save the entire plan's lines (edited qty/sell/vendor, added lines, removed lines)
    in one POST (epic I "save all").

    Form field ``payload`` is a JSON object ``{"lines": [...], "known_line_ids": [...]}``
    (the Alpine editor posts it via an htmx ``hx-vals`` JSON blob). ``known_line_ids``
    (optional; a list of ints) is every line id the client's form actually rendered —
    it scopes removal-by-omission so a line added by someone else after the form loaded
    is left untouched instead of silently deleted; omitted entirely falls back to the
    legacy (unscoped) removal-by-omission behavior. Role×status gate and per-line rules
    are enforced in the service (PermissionError → 403); malformed JSON, a bad shape, or
    bad line data → 400. ``known_line_ids`` element-level validation (whole numbers,
    bools rejected) lives in the SERVICE now (``bulk_edit_buy_plan_lines`` owns the
    contract) — this route only checks the outer shape (a list, if present).
    ``bulk_edit_buy_plan_lines`` already auto-completes at service depth (removing the
    last open line can leave every remaining line terminal); the returned plan's
    ``.status`` is read BEFORE commit to drive ``_notify_if_completed`` without re-
    deriving the fact via a second ``check_completion`` scan.
    """
    from ....services.buyplan_workflow import bulk_edit_buy_plan_lines

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation. Matches
    # bulk_edit_buy_plan_lines's own loader options (see buy_plan_add_line_partial).
    plan = get_buyplan_for_user(db, user, plan_id, options=[joinedload(BuyPlan.lines), joinedload(BuyPlan.requisition)])

    form = await request.form()
    # Stale-edit guard (2.1): a whole-plan save's narrowest object is the PLAN.
    try:
        ensure_not_stale(plan, form.get("expected_updated_at"))
    except StaleEditError:
        return stale_conflict_response()
    raw_payload = form.get("payload")
    try:
        parsed = json.loads(str(raw_payload))
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "Malformed lines payload — expected JSON.") from e

    if not isinstance(parsed, dict) or not isinstance(parsed.get("lines"), list):
        raise HTTPException(400, 'Lines payload must be a JSON object shaped {"lines": [...]}.')

    known_line_ids = parsed.get("known_line_ids")
    if known_line_ids is not None and not isinstance(known_line_ids, list):
        raise HTTPException(400, "known_line_ids must be a list of whole-number line ids.")

    try:
        updated = bulk_edit_buy_plan_lines(plan_id, parsed["lines"], user, db, known_line_ids=known_line_ids)
        just_completed = updated.status == BuyPlanStatus.COMPLETED.value
        db.commit()
        await _notify_if_completed(plan_id, just_completed)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    return await buy_plan_detail_partial(request, plan_id, user, db)
