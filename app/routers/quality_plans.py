"""routers/quality_plans.py — HTMX router for Quality Plan views.

Purpose: Exposes GET /v2/qp/{id} (QP detail partial) and
         POST /v2/qp/{id}/submit (submit action, returns refreshed partial).
         Thin router: all business logic lives in app.services.quality_plan_service.

Called by: app.main (router registration).
Depends on: app.services.quality_plan_service (validate_complete, submit, IncompleteQPError),
            app.models.quality_plan (QualityPlan), app.models.buy_plan (BuyPlan, BuyPlanLine),
            app.dependencies (require_user), app.database (get_db),
            app.template_env (template_response).
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session, joinedload

from ..database import get_db
from ..dependencies import require_user
from ..models.buy_plan import BuyPlan, BuyPlanLine
from ..models.crm import CustomerSite
from ..models.quality_plan import QualityPlan
from ..models.quotes import Quote
from ..services.quality_plan_service import IncompleteQPError, submit, validate_complete
from ..template_env import template_response

router = APIRouter(tags=["quality_plans"])


def _qp_detail_response(request: Request, user, db: Session, qp: QualityPlan) -> HTMLResponse:
    """Build and render the QP detail partial.

    Loads the linked BuyPlan with its lines (eager), computes completeness errors for
    inline display, and renders qp/detail.html.
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
    }
    return template_response("htmx/partials/qp/detail.html", ctx)


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
