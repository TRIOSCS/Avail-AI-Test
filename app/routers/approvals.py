"""routers/approvals.py — Thin HTMX router for the Approval Engine.

Purpose: HTTP surface for approval workflows. Returns JSON; HTMX partials can
         swap these responses. Delegates all business logic to:
           - app.services.approvals.service (decide)
           - app.services.approvals.events (reassign, cancel)

Called by: app.main (router registration).
Depends on: app.services.approvals.service, app.services.approvals.events,
            app.dependencies (require_user, require_approval_gatekeeper),
            app.database (get_db), app.models.approvals, app.constants.
"""

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_approval_gatekeeper, require_user
from ..models.approvals import ApprovalRequest
from ..models.auth import User
from ..services.approvals.events import cancel as svc_cancel
from ..services.approvals.events import reassign as svc_reassign
from ..services.approvals.service import decide as svc_decide

router = APIRouter(tags=["approvals"])


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

    Gate: require_user (any authenticated user). The service raises ValueError
    if the request is already resolved.
    Returns: {"cancelled": true}.
    """
    try:
        svc_cancel(db, id, actor=current_user)
        db.commit()
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
    """List ApprovalRequests, optionally filtered by gate_type and/or status.

    Returns: {"items": [...], "total": N}.
    """
    q = select(ApprovalRequest)
    if gate_type:
        q = q.where(ApprovalRequest.gate_type == gate_type)
    if status:
        q = q.where(ApprovalRequest.status == status)

    rows = db.execute(q).scalars().all()

    items = [
        {
            "id": r.id,
            "gate_type": r.gate_type,
            "status": r.status,
            "amount": str(r.amount) if r.amount is not None else None,
            "currency": r.currency,
            "requested_by_id": r.requested_by_id,
            "owner_id": r.owner_id,
            "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
            "resolution_note": r.resolution_note,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    return {"items": items, "total": len(items)}


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

    return {
        "id": request.id,
        "gate_type": request.gate_type,
        "status": request.status,
        "amount": str(request.amount) if request.amount is not None else None,
        "currency": request.currency,
        "requested_by_id": request.requested_by_id,
        "owner_id": request.owner_id,
        "resolved_at": request.resolved_at.isoformat() if request.resolved_at else None,
        "resolution_note": request.resolution_note,
        "created_at": request.created_at.isoformat() if request.created_at else None,
    }
