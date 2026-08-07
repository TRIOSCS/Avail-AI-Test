"""routers/quality_plans.py — HTMX router for Quality Plan views.

Purpose: Exposes GET /v2/qp/for-buy-plan/{bp_id} (the front door — get-or-create the QP
         for a buy plan and render its native detail), GET /v2/qp/{id} (QP detail
         partial), serial CRUD (POST/DELETE /v2/qp/{id}/serial[/{entry_id}]), and FRU
         pin/unpin (POST/DELETE /v2/qp/{id}/fru[/{lookup_id}]). All return the refreshed
         partial. The Sales/Purchasing sections render READ-ONLY here — they are edited
         in the Approvals workspace panes (W4.3 absorbed the standalone section editors;
         the old PATCH /v2/qp/{id}/sales + /purchasing routes are gone). This page stays
         as the app's serial/FRU surface (spec §5.2 Decision E — absorbing those into
         the workspace pane is post-launch).
         Thin router: business logic lives in app.services.quality_plan_service.

Called by: app.main (router registration).
Depends on: app.services.quality_plan_service (validate_complete, create_qp),
            app.models.quality_plan (QualityPlan, QpSerialEntry, QpFruLookup),
            app.models.buy_plan (BuyPlan, BuyPlanLine),
            app.models.approvals (ApprovalRequest), app.models.fru_link (FruLink),
            app.constants (ApprovalGateType),
            app.utils.normalization (normalize_mpn_key),
            app.dependencies (require_user), app.database (get_db),
            app.template_env (template_response).
"""

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..constants import ApprovalGateType, ApprovalSubjectType
from ..database import get_db
from ..dependencies import get_buyplan_for_user, require_requisition_access, require_user
from ..models.approvals import ApprovalRequest
from ..models.buy_plan import BuyPlan, BuyPlanLine
from ..models.crm import CustomerSite
from ..models.fru_link import FruLink
from ..models.quality_plan import QpFruLookup, QpSerialEntry, QualityPlan
from ..models.quotes import Quote
from ..services.quality_plan_service import (
    create_qp,
    validate_complete,
)
from ..template_env import template_response
from ..utils.normalization import normalize_mpn_key
from .htmx._shared import full_page_shell

router = APIRouter(tags=["quality_plans"])

# Form-value coercion for the serial CRUD inputs. "bool" coerces a checkbox /
# "true"/"false" string to a tri-state (None when absent), "int" to int|None, "str" to a
# stripped string|None.
_BOOL_TRUE = {"true", "on", "1", "yes", "y"}
_BOOL_FALSE = {"false", "off", "0", "no", "n"}


def _coerce(kind: str, raw: str | None) -> str | int | bool | None:
    """Coerce a raw form value to the column's Python type (None when blank/unset)."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if kind == "bool":
        low = text.lower()
        if low in _BOOL_TRUE:
            return True
        if low in _BOOL_FALSE:
            return False
        return None
    if kind == "int":
        try:
            return int(text)
        except ValueError:
            return None
    return text


def _parse_date(raw: str | None) -> date | None:
    """Parse an HTML date input (YYYY-MM-DD) to a date, or None when blank/invalid."""
    if not raw or not raw.strip():
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _get_gate(db: Session, qp_id: int, gate_type: str) -> ApprovalRequest | None:
    """Return the latest ApprovalRequest for this QP + gate, or None.

    The QP is the polymorphic subject (subject_type=QUALITY_PLAN); gate_type
    discriminates the section. Ordered by id descending so the most recent request wins
    (a resubmit supersedes a prior rejected one in the section chip).
    """
    return db.execute(
        select(ApprovalRequest)
        .where(
            ApprovalRequest.subject_type == ApprovalSubjectType.QUALITY_PLAN,
            ApprovalRequest.subject_id == qp_id,
            ApprovalRequest.gate_type == gate_type,
        )
        .order_by(ApprovalRequest.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _qp_detail_response(
    request: Request,
    user,
    db: Session,
    qp: QualityPlan,
) -> HTMLResponse:
    """Build and render the QP detail partial.

    Loads the linked BuyPlan with its lines (eager), computes completeness errors for
    inline display, and renders qp/detail.html. The Sales/Purchasing sections render
    read-only (edited in the Approvals workspace — W4.3); the Buy-Plan and Prepayment
    approval-request context is still resolved for their read-only status chips.
    """
    bp = (
        db.get(
            BuyPlan,
            qp.buy_plan_id,
            options=[
                joinedload(BuyPlan.lines).joinedload(BuyPlanLine.requirement),
                joinedload(BuyPlan.lines).joinedload(BuyPlanLine.offer),
                joinedload(BuyPlan.submitted_by),
                joinedload(BuyPlan.quote).joinedload(Quote.customer_site).joinedload(CustomerSite.company),
                joinedload(BuyPlan.requisition),
            ],
        )
        if qp.buy_plan_id
        else None
    )

    errors = validate_complete(qp)

    ctx = {
        "request": request,
        "user": user,
        "qp": qp,
        "bp": bp,
        "bp_lines": (bp.lines or []) if bp else [],
        "errors": errors,
        "fru_rows": _fru_rows(db, qp),
        "buy_plan_gate": _get_gate(db, qp.id, ApprovalGateType.BUY_PLAN),
        "prepayment_gate": _get_gate(db, qp.id, ApprovalGateType.PREPAYMENT),
    }
    return template_response("htmx/partials/qp/detail.html", ctx)


def _fru_rows(db: Session, qp: QualityPlan) -> list[dict]:
    """Live-join each pinned QpFruLookup to the shared FruLink crosswalk by fru_norm.

    Returns one dict per pinned FRU: the lookup row plus the related crosswalk edges
    (model / carrier / series context). A FRU with no crosswalk match still appears
    (empty links) so the user sees the pin and can unpin it.
    """
    rows: list[dict] = []
    for pin in qp.fru_lookups:
        links = (
            db.execute(select(FruLink).where(FruLink.fru_norm == pin.fru_norm).order_by(FruLink.id).limit(50))
            .scalars()
            .all()
        )
        rows.append({"pin": pin, "links": links})
    return rows


def _require_qp_access(db: Session, user, qp: QualityPlan) -> None:
    """Enforce requisition-ownership scope on a Quality Plan action.

    A QP belongs to its BuyPlan's parent requisition; restricted roles may only act on
    QPs under requisitions they own (or that they created). 404 (not 403) so a QP's
    existence isn't leaked. No-op for buyer/manager/admin.
    """
    bp = db.get(BuyPlan, qp.buy_plan_id) if qp.buy_plan_id else None
    require_requisition_access(
        db,
        bp.requisition_id if bp else None,
        user,
        owner_id=qp.created_by_id,
        label="Quality plan",
    )


@router.get("/v2/qp/for-buy-plan/{bp_id}", response_class=HTMLResponse)
def qp_for_buy_plan(
    request: Request,
    bp_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
) -> Response:
    """Get-or-create the Quality Plan for a buy plan and render its native detail.

    The front door to the QP view: the buy-plan owner clicks "Quality Plan" and lands
    here. Idempotent — the first open creates the (DRAFT) QP for this buy plan, every
    later open returns the same one (a buy plan has at most one QP). Ownership is scoped
    through the buy plan's parent requisition via get_buyplan_for_user, so a restricted-
    role non-owner gets a 404 (existence not leaked) before any QP is created.

    This url is pushed into history (the buy-plan "Quality Plan" button hx-push-urls it),
    so a full-page reload / bookmark / share arrives WITHOUT the HX-Request header: serve
    the app shell (which then HTMX-loads this same url and get-or-creates the QP) instead
    of a shell-less fragment. get-or-create runs only on the HTMX pass, keeping the
    full-page load side-effect-free.
    """
    if request.headers.get("HX-Request") != "true":
        return full_page_shell(request, user, request.url.path, "buy-plans")

    # Ownership-scoped load (404 for missing buy plan or restricted non-owner).
    bp = get_buyplan_for_user(db, user, bp_id)

    qp = db.execute(select(QualityPlan).where(QualityPlan.buy_plan_id == bp.id)).scalar_one_or_none()
    if qp is None:
        qp = create_qp(db, owner_id=user.id, buy_plan_id=bp.id)
        db.commit()

    # Re-load with the same eager options qp_detail uses so the detail partial renders
    # the owner / serial / FRU sections without lazy-load surprises.
    qp = db.get(
        QualityPlan,
        qp.id,
        options=[
            joinedload(QualityPlan.created_by),
            joinedload(QualityPlan.buy_plan),
            joinedload(QualityPlan.serial_entries).joinedload(QpSerialEntry.buyer),
            joinedload(QualityPlan.serial_entries).joinedload(QpSerialEntry.submitted_by),
            joinedload(QualityPlan.fru_lookups),
        ],
    )
    return _qp_detail_response(request, user, db, qp)


@router.get("/v2/qp/{qp_id}", response_class=HTMLResponse)
def qp_detail(
    request: Request,
    qp_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
) -> HTMLResponse:
    """Return the Quality Plan detail as an HTMX partial.

    Loads the QP and its linked BuyPlan (with lines). Completeness errors are shown
    inline — the route never 500s on an incomplete plan.
    """
    qp = db.get(
        QualityPlan,
        qp_id,
        options=[
            joinedload(QualityPlan.created_by),
            joinedload(QualityPlan.buy_plan),
            joinedload(QualityPlan.serial_entries).joinedload(QpSerialEntry.buyer),
            joinedload(QualityPlan.serial_entries).joinedload(QpSerialEntry.submitted_by),
            joinedload(QualityPlan.fru_lookups),
        ],
    )
    if qp is None:
        raise HTTPException(status_code=404, detail="Quality plan not found")
    _require_qp_access(db, user, qp)

    return _qp_detail_response(request, user, db, qp)


# ── Serial + FRU CRUD ────────────────────────────────────────────────────────────
# The Sales/Purchasing section editors that used to live here (PATCH /v2/qp/{id}/sales
# + /purchasing, C2b) were absorbed into the Approvals workspace in W4.3 — those
# sections now render read-only on this page. Serial/FRU stays native here (spec §5.2
# Decision E).


def _load_qp_for_edit(db: Session, qp_id: int, user) -> QualityPlan:
    """Fetch a QP (with section children) and enforce ownership, or raise 404/HTTP.

    Shared by the serial/FRU mutation endpoints so each handler stays thin. Eager-loads
    the serial entries + FRU pins the refreshed section partials render.
    """
    qp = db.get(
        QualityPlan,
        qp_id,
        options=[
            joinedload(QualityPlan.buy_plan),
            joinedload(QualityPlan.serial_entries).joinedload(QpSerialEntry.buyer),
            joinedload(QualityPlan.serial_entries).joinedload(QpSerialEntry.submitted_by),
            joinedload(QualityPlan.fru_lookups),
        ],
    )
    if qp is None:
        raise HTTPException(status_code=404, detail="Quality plan not found")
    _require_qp_access(db, user, qp)
    return qp


def _render_serial_section(request: Request, qp: QualityPlan, user) -> HTMLResponse:
    """Render the refreshed Serial section partial."""
    return template_response(
        "htmx/partials/qp/_section_serial.html",
        {"request": request, "user": user, "qp": qp},
    )


def _render_fru_section(request: Request, db: Session, qp: QualityPlan, user) -> HTMLResponse:
    """Render the refreshed FRU section partial (with the live-joined crosswalk
    rows)."""
    return template_response(
        "htmx/partials/qp/_section_fru.html",
        {"request": request, "user": user, "qp": qp, "fru_rows": _fru_rows(db, qp)},
    )


@router.post("/v2/qp/{qp_id}/serial", response_class=HTMLResponse)
def qp_add_serial(
    request: Request,
    qp_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
    purchase_order: str | None = Form(None),
    part_number: str | None = Form(None),
    serial_number: str | None = Form(None),
    seagate_sn: str | None = Form(None),
    tso: str | None = Form(None),
    customer_po: str | None = Form(None),
    buyer_date: str | None = Form(None),
    has_sn_prev_received: str | None = Form(None),
    submitted_to_customer_date: str | None = Form(None),
    customer_approved: str | None = Form(None),
    customer_approved_date: str | None = Form(None),
    ops_received: str | None = Form(None),
) -> HTMLResponse:
    """Add one Serial-preapproval entry → refreshed Serial section partial.

    submitted_by defaults to the acting user. Date inputs are YYYY-MM-DD; Y/N inputs are
    tri-state (unanswered stays None).
    """
    qp = _load_qp_for_edit(db, qp_id, user)
    entry = QpSerialEntry(
        qp_id=qp.id,
        submitted_by_id=user.id if user else None,
        buyer_date=_parse_date(buyer_date),
        has_sn_prev_received=_coerce("bool", has_sn_prev_received),
        purchase_order=_coerce("str", purchase_order),
        part_number=_coerce("str", part_number),
        serial_number=_coerce("str", serial_number),
        seagate_sn=_coerce("str", seagate_sn),
        tso=_coerce("str", tso),
        customer_po=_coerce("str", customer_po),
        submitted_to_customer_date=_parse_date(submitted_to_customer_date),
        customer_approved=_coerce("bool", customer_approved),
        customer_approved_date=_parse_date(customer_approved_date),
        ops_received=_coerce("bool", ops_received),
    )
    db.add(entry)
    db.commit()
    qp = _load_qp_for_edit(db, qp_id, user)
    return _render_serial_section(request, qp, user)


@router.delete("/v2/qp/{qp_id}/serial/{entry_id}", response_class=HTMLResponse)
def qp_delete_serial(
    request: Request,
    qp_id: int,
    entry_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
) -> HTMLResponse:
    """Delete one Serial entry → refreshed Serial section partial.

    The entry must belong to this QP (404 otherwise so a foreign entry isn't leaked).
    """
    qp = _load_qp_for_edit(db, qp_id, user)
    entry = db.get(QpSerialEntry, entry_id)
    if entry is None or entry.qp_id != qp.id:
        raise HTTPException(status_code=404, detail="Serial entry not found")
    db.delete(entry)
    db.commit()
    qp = _load_qp_for_edit(db, qp_id, user)
    return _render_serial_section(request, qp, user)


@router.post("/v2/qp/{qp_id}/fru", response_class=HTMLResponse)
def qp_pin_fru(
    request: Request,
    qp_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
    fru: str = Form(...),
) -> HTMLResponse:
    """Pin a FRU part number to the QP → refreshed FRU section partial.

    Resolves fru_norm via normalize_mpn_key; a blank/unnormalizable value is ignored.
    Re-pinning an already-pinned FRU is a no-op (the (qp_id, fru_norm) unique
    constraint), so we look it up first rather than relying on an IntegrityError round-
    trip.
    """
    qp = _load_qp_for_edit(db, qp_id, user)
    fru_norm = normalize_mpn_key(fru)
    if fru_norm:
        exists = db.execute(
            select(QpFruLookup).where(QpFruLookup.qp_id == qp.id, QpFruLookup.fru_norm == fru_norm)
        ).scalar_one_or_none()
        if exists is None:
            db.add(QpFruLookup(qp_id=qp.id, fru_norm=fru_norm))
            db.commit()
    qp = _load_qp_for_edit(db, qp_id, user)
    return _render_fru_section(request, db, qp, user)


@router.delete("/v2/qp/{qp_id}/fru/{lookup_id}", response_class=HTMLResponse)
def qp_unpin_fru(
    request: Request,
    qp_id: int,
    lookup_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
) -> HTMLResponse:
    """Unpin a FRU lookup → refreshed FRU section partial.

    The lookup must belong to this QP (404 otherwise).
    """
    qp = _load_qp_for_edit(db, qp_id, user)
    pin = db.get(QpFruLookup, lookup_id)
    if pin is None or pin.qp_id != qp.id:
        raise HTTPException(status_code=404, detail="FRU lookup not found")
    db.delete(pin)
    db.commit()
    qp = _load_qp_for_edit(db, qp_id, user)
    return _render_fru_section(request, db, qp, user)
