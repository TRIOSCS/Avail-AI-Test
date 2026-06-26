"""routers/quality_plans.py — HTMX router for Quality Plan views.

Purpose: Exposes GET /v2/qp/{id} (QP detail partial),
         POST /v2/qp/{id}/submit (submit-for-review action), and the QP Phase C2a
         section gates POST /v2/qp/{id}/submit-sales + /submit-purchasing (open the
         SALES_ORDER / PURCHASE_ORDER approval gate). All return the refreshed partial.
         Thin router: all business logic lives in app.services.quality_plan_service.

Called by: app.main (router registration).
Depends on: app.services.quality_plan_service (validate_complete, submit, submit_section,
            IncompleteQPError, NoSectionApproverError),
            app.models.quality_plan (QualityPlan), app.models.buy_plan (BuyPlan, BuyPlanLine),
            app.models.approvals (ApprovalRequest),
            app.constants (ApprovalGateType),
            app.dependencies (require_user), app.database (get_db),
            app.template_env (template_response).
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..constants import ApprovalGateType, ApprovalSubjectType
from ..database import get_db
from ..dependencies import require_requisition_access, require_user
from ..models.approvals import ApprovalRequest
from ..models.buy_plan import BuyPlan, BuyPlanLine
from ..models.crm import CustomerSite
from ..models.quality_plan import QualityPlan
from ..models.quotes import Quote
from ..services.quality_plan_service import (
    IncompleteQPError,
    NoSectionApproverError,
    submit,
    submit_section,
    validate_complete,
)
from ..template_env import template_response

router = APIRouter(tags=["quality_plans"])


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
    *,
    section_error: str | None = None,
) -> HTMLResponse:
    """Build and render the QP detail partial.

    Loads the linked BuyPlan with its lines (eager), computes completeness errors for
    inline display, resolves the latest approval-request per section gate (Sales /
    Purchasing / Buy Plan / Prepayment) for the section chips, and renders
    qp/detail.html. section_error surfaces an inline "no approver configured" banner
    (C2a) without a 500.
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
        "section_error": section_error,
        "sales_gate": _get_gate(db, qp.id, ApprovalGateType.SALES_ORDER),
        "purchasing_gate": _get_gate(db, qp.id, ApprovalGateType.PURCHASE_ORDER),
        "buy_plan_gate": _get_gate(db, qp.id, ApprovalGateType.BUY_PLAN),
        "prepayment_gate": _get_gate(db, qp.id, ApprovalGateType.PREPAYMENT),
    }
    return template_response("htmx/partials/qp/detail.html", ctx)


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
            joinedload(QualityPlan.approved_by),
            joinedload(QualityPlan.buy_plan),
        ],
    )
    if qp is None:
        raise HTTPException(status_code=404, detail="Quality plan not found")
    _require_qp_access(db, user, qp)

    return _qp_detail_response(request, user, db, qp)


@router.post("/v2/qp/{qp_id}/submit", response_class=HTMLResponse)
def qp_submit(
    request: Request,
    qp_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
) -> HTMLResponse:
    """Submit the Quality Plan for review (DRAFT → IN_REVIEW).

    On success refreshes the detail partial. On IncompleteQPError returns the detail
    partial with errors displayed inline (no 500).
    """
    qp = db.get(QualityPlan, qp_id)
    if qp is None:
        raise HTTPException(status_code=404, detail="Quality plan not found")
    _require_qp_access(db, user, qp)

    try:
        qp = submit(db, qp_id, user)
        db.commit()
    except IncompleteQPError:
        # Refresh qp from DB so we render current state with errors
        db.rollback()
        qp = db.get(QualityPlan, qp_id)
    except ValueError as exc:
        # submit() raises ValueError if the QP was concurrently deleted — surface 404
        # rather than a 500.
        db.rollback()
        raise HTTPException(status_code=404, detail="Quality plan not found") from exc

    # Guard the re-fetch (and the success path): a concurrent delete can leave qp None,
    # and _qp_detail_response would dereference qp.buy_plan_id → AttributeError 500.
    if qp is None:
        raise HTTPException(status_code=404, detail="Quality plan not found")

    return _qp_detail_response(request, user, db, qp)


def _submit_section_response(request: Request, qp_id: int, gate_type: str, db: Session, user) -> HTMLResponse:
    """Open a section gate (SALES_ORDER / PURCHASE_ORDER) and refresh the QP detail.

    On NoSectionApproverError surfaces an inline "no approver configured" banner (NOT a
    500); on a concurrent delete (ValueError) returns 404. Shared by the sales and
    purchasing submit endpoints.
    """
    qp = db.get(QualityPlan, qp_id)
    if qp is None:
        raise HTTPException(status_code=404, detail="Quality plan not found")
    _require_qp_access(db, user, qp)

    section_error: str | None = None
    try:
        submit_section(db, qp_id, gate_type, user)
        db.commit()
    except NoSectionApproverError as exc:
        # No eligible approver holds the section toggle. create_request already removed
        # the half-built request, so commit the (empty) transaction and show the banner.
        db.commit()
        section_error = f"No approver configured for the {exc.section} section. An admin must grant the right first."
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail="Quality plan not found") from exc

    qp = db.get(QualityPlan, qp_id)
    if qp is None:
        raise HTTPException(status_code=404, detail="Quality plan not found")

    return _qp_detail_response(request, user, db, qp, section_error=section_error)


@router.post("/v2/qp/{qp_id}/submit-sales", response_class=HTMLResponse)
def qp_submit_sales(
    request: Request,
    qp_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
) -> HTMLResponse:
    """Submit the QP Sales section for approval (opens the SALES_ORDER gate).

    Refreshes the detail partial. No eligible approver → inline banner, never a 500.
    """
    return _submit_section_response(request, qp_id, ApprovalGateType.SALES_ORDER, db, user)


@router.post("/v2/qp/{qp_id}/submit-purchasing", response_class=HTMLResponse)
def qp_submit_purchasing(
    request: Request,
    qp_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
) -> HTMLResponse:
    """Submit the QP Purchasing section for approval (opens the PURCHASE_ORDER gate).

    Refreshes the detail partial. No eligible approver → inline banner, never a 500.
    """
    return _submit_section_response(request, qp_id, ApprovalGateType.PURCHASE_ORDER, db, user)
