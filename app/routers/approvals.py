"""routers/approvals.py — Thin HTMX router for the Approval Engine.

Purpose: HTTP surface for approval workflows. Returns JSON; HTMX partials can
         swap these responses. Delegates all business logic to:
           - app.services.approvals.service (decide)
           - app.services.approvals.events (reassign, cancel)

         QP Phase C1: the engine OWNS the buy-plan gate, so list_requests and the
         /v2/approvals/queue view are engine-only — a buy-plan submission surfaces here
         as a native ApprovalRequest (gate_type=buy_plan, subject_type=buy_plan). The old
         read-only buy-plan bridge has been retired.

Called by: app.main (router registration).
Depends on: app.services.approvals.service, app.services.approvals.events,
            app.dependencies (require_user, require_approval_gatekeeper),
            app.database (get_db), app.models.approvals,
            app.constants, app.template_env.
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_approval_gatekeeper, require_user
from ..models.approvals import ApprovalRequest
from ..models.auth import User
from ..services.approvals.events import cancel as svc_cancel
from ..services.approvals.events import reassign as svc_reassign
from ..services.approvals.service import decide as svc_decide
from ..template_env import template_response

router = APIRouter(tags=["approvals"])


def _serialize_request(r: ApprovalRequest) -> dict:
    """Project an ApprovalRequest to its JSON shape (shared by list + detail).

    The 11-field engine-item projection: id, gate_type, status, subject_type, subject_id,
    amount-as-str, currency, requested_by_id, owner_id, resolved_at (iso), resolution_note,
    created_at (iso). subject_type/subject_id let a caller link a buy_plan request back to
    its plan detail partial.
    """
    return {
        "id": r.id,
        "gate_type": r.gate_type,
        "status": r.status,
        "subject_type": r.subject_type,
        "subject_id": r.subject_id,
        "amount": str(r.amount) if r.amount is not None else None,
        "currency": r.currency,
        "requested_by_id": r.requested_by_id,
        "owner_id": r.owner_id,
        "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
        "resolution_note": r.resolution_note,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.post("/v2/approvals/requests/{id}/decision")
def post_decision(
    id: int,
    action: str = Form(...),
    comment: str | None = Form(default=None),
    db: Session = Depends(get_db),
    acting_user: User = Depends(require_approval_gatekeeper),
):
    """POST a decision (approve/reject) on an ApprovalRequest.

    Gate: require_approval_gatekeeper — 403 unless acting_user is a PENDING recipient.
    Body (form): action ("approve"|"reject"), comment (required for reject).
    Returns: {"id": ..., "status": "approved"|"rejected"}.
    """
    try:
        request = svc_decide(db, id, acting_user, action, comment=comment or None)
        db.commit()
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    return {"id": request.id, "status": request.status}


@router.post("/v2/approvals/requests/{id}/reassign")
def post_reassign(
    id: int,
    to_user_id: int = Form(...),
    db: Session = Depends(get_db),
    acting_user: User = Depends(require_approval_gatekeeper),
):
    """Reassign the acting user's PENDING slot to another user.

    Gate: require_approval_gatekeeper — 403 unless acting_user is a PENDING recipient.
    Body (form): to_user_id (int).
    Returns: {"reassigned": true, "to_user_id": ...}.
    """
    to_user = db.get(User, to_user_id)
    if to_user is None:
        return JSONResponse(status_code=404, content={"error": f"User {to_user_id} not found"})

    try:
        svc_reassign(db, id, acting_user, to_user, actor=acting_user)
        db.commit()
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    return {"reassigned": True, "to_user_id": to_user_id}


@router.post("/v2/approvals/requests/{id}/cancel")
def post_cancel(
    id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    """Cancel an open ApprovalRequest.

    Gate: require_user (any authenticated user), then the service enforces
    ownership — only the requester, the owner, or a manager/admin may cancel
    (PermissionError → 403). The service raises ValueError if the request is
    already resolved (→ 400).
    Returns: {"cancelled": true}.
    """
    try:
        svc_cancel(db, id, actor=current_user)
        db.commit()
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    return {"cancelled": True}


@router.get("/v2/approvals/requests")
def list_requests(
    gate_type: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    """List engine ApprovalRequests (optionally filtered by gate_type / status).

    QP Phase C1: engine-only. A buy-plan submission is a native ApprovalRequest
    (gate_type=buy_plan, subject_type=buy_plan), so the old read-only buy-plan bridge is
    gone — filter on gate_type='buy_plan' to get exactly the buy-plan requests.

    Returns: {"items": [...], "total": N}.
    """
    q = select(ApprovalRequest)
    if gate_type:
        q = q.where(ApprovalRequest.gate_type == gate_type)
    if status:
        q = q.where(ApprovalRequest.status == status)

    rows = db.execute(q).scalars().all()
    items = [_serialize_request(r) for r in rows]
    return {"items": items, "total": len(items)}


@router.get("/v2/approvals/queue", response_class=HTMLResponse)
def get_queue(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    """Render the engine approvals queue as an HTMX partial.

    QP Phase C1: engine-only. Every pending approval — buy plans included — is an
    ApprovalRequest, so the queue renders one table of engine rows. A buy_plan-subject row
    links to its plan detail partial and offers inline approve/reject posting to the
    engine's decision endpoint.
    """
    engine_rows = db.execute(select(ApprovalRequest)).scalars().all()

    ctx = {
        "request": request,
        "current_user": current_user,
        "engine_requests": engine_rows,
    }
    return template_response("htmx/partials/approvals/_queue.html", ctx)


@router.get("/v2/approvals/requests/{id}")
def get_request(
    id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    """Get a single ApprovalRequest by id.

    Returns: the request object as a dict.
    Raises: 404 if not found.
    """
    request = db.get(ApprovalRequest, id)
    if request is None:
        return JSONResponse(status_code=404, content={"error": f"ApprovalRequest {id} not found"})

    return _serialize_request(request)
