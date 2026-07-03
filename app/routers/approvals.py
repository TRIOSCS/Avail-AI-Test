"""routers/approvals.py — Thin HTMX router for the Approval Engine.

Purpose: HTTP surface for approval workflows. Returns JSON; HTMX partials can
         swap these responses. Delegates all business logic to:
           - app.services.approvals.service (decide)
           - app.services.approvals.events (reassign, cancel)

         QP Phase C1: the engine OWNS the buy-plan gate, so list_requests is engine-only —
         a buy-plan submission surfaces as a native ApprovalRequest (gate_type=buy_plan,
         subject_type=buy_plan). The old read-only buy-plan bridge has been retired.

         The human-facing decide queue is the Approvals hub (3 tabs: Buy Plan / PO Approval
         / Vendor Prepayment) at /v2/approvals — rendered by routers/htmx/approvals_hub.py
         via services/approvals/{queue,po_queue}. This module keeps the engine's JSON
         decide/reassign/cancel/list endpoints only.

Called by: app.main (router registration).
Depends on: app.services.approvals.service, app.services.approvals.events,
            app.dependencies (require_user, require_approval_gatekeeper),
            app.database (get_db), app.models.approvals,
            app.constants, app.template_env.
"""

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..constants import RESTRICTED_ROLES, ApprovalRecipientStatus
from ..database import get_db
from ..dependencies import require_approval_gatekeeper, require_user
from ..models.approvals import ApprovalRequest, ApprovalStep, ApprovalStepRecipient
from ..models.auth import User
from ..services.approvals.events import cancel as svc_cancel
from ..services.approvals.events import reassign as svc_reassign
from ..services.approvals.service import decide as svc_decide

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


def _pending_recipient_exists(user: User):
    """Correlated EXISTS: *user* holds a PENDING recipient row on the outer request.

    Shared by the list scope and the detail guard so both mirror the exact set
    require_approval_gatekeeper acts on.
    """
    return (
        select(ApprovalStepRecipient.id)
        .join(ApprovalStep, ApprovalStepRecipient.step_id == ApprovalStep.id)
        .where(
            ApprovalStep.request_id == ApprovalRequest.id,
            ApprovalStepRecipient.user_id == user.id,
            ApprovalStepRecipient.status == ApprovalRecipientStatus.PENDING,
        )
        .exists()
    )


def _restricted_visibility_clause(user: User):
    """WHERE clause limiting ApprovalRequest rows a RESTRICTED_ROLES user may see.

    Mirrors how the app scopes requisition-derived data (require_requisition_access /
    RESTRICTED_ROLES): SALES/TRADER see only requests they submitted (requested_by_id),
    own (owner_id), or must personally decide (a PENDING recipient row). Unrestricted
    roles (buyer/manager/admin) are never passed here and keep full visibility.
    """
    return or_(
        ApprovalRequest.requested_by_id == user.id,
        ApprovalRequest.owner_id == user.id,
        _pending_recipient_exists(user),
    )


def _can_view_request(db: Session, request: ApprovalRequest, user: User) -> bool:
    """True if *user* may view *request* — full for unrestricted roles, scoped for
    RESTRICTED_ROLES to their own/owned/pending-recipient requests (mirrors
    _restricted_visibility_clause)."""
    if getattr(user, "role", None) not in RESTRICTED_ROLES:
        return True
    if user.id in (request.requested_by_id, request.owner_id):
        return True
    recipient = db.execute(
        select(ApprovalStepRecipient.id)
        .join(ApprovalStep, ApprovalStepRecipient.step_id == ApprovalStep.id)
        .where(
            ApprovalStep.request_id == request.id,
            ApprovalStepRecipient.user_id == user.id,
            ApprovalStepRecipient.status == ApprovalRecipientStatus.PENDING,
        )
    ).first()
    return recipient is not None


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

    Ownership scope: RESTRICTED_ROLES (SALES/TRADER) see only requests they submitted,
    own, or must personally decide (see _restricted_visibility_clause); unrestricted
    roles (buyer/manager/admin) see all — mirrors requisition-derived data scoping.

    Returns: {"items": [...], "total": N}.
    """
    q = select(ApprovalRequest)
    if gate_type:
        q = q.where(ApprovalRequest.gate_type == gate_type)
    if status:
        q = q.where(ApprovalRequest.status == status)
    if getattr(current_user, "role", None) in RESTRICTED_ROLES:
        q = q.where(_restricted_visibility_clause(current_user))

    rows = db.execute(q).scalars().all()
    items = [_serialize_request(r) for r in rows]
    return {"items": items, "total": len(items)}


@router.get("/v2/approvals/requests/{id}")
def get_request(
    id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    """Get a single ApprovalRequest by id.

    Ownership scope: RESTRICTED_ROLES (SALES/TRADER) may only read a request they
    submitted, own, or must personally decide (see _can_view_request); otherwise 404
    (existence not leaked, mirroring require_requisition_access). Unrestricted roles
    (buyer/manager/admin) may read any request.

    Returns: the request object as a dict.
    Raises: 404 if not found or not visible to the current user.
    """
    request = db.get(ApprovalRequest, id)
    if request is None or not _can_view_request(db, request, current_user):
        return JSONResponse(status_code=404, content={"error": f"ApprovalRequest {id} not found"})

    return _serialize_request(request)
