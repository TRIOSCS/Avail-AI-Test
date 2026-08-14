"""routers/crm/clone.py — Clone-requisition endpoint (thin wrapper).

POST /api/requisitions/{req_id}/clone duplicates a requisition with its
requirements and active/selected offers (copied as "reference" offers). The
copy logic lives in services/requisition_service.clone_requisition — this
router only enforces ownership and shapes the JSON response.

Called by: app/main.py (router mount).
Depends on: app.dependencies (get_req_for_user), app.services.requisition_service
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import require_user
from ...models import User
from ...services.requisition_service import clone_requisition as clone_requisition_service

router = APIRouter()


# ── Clone Requisition ────────────────────────────────────────────────────


@router.post("/api/requisitions/{req_id}/clone")
async def clone_requisition(req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    from ...dependencies import get_req_for_user

    # get_req_for_user enforces role-scoped ownership (SALES/TRADER restricted to
    # requisitions they created) and raises 404 for non-owners.
    req = get_req_for_user(db, user, req_id)
    new_req = clone_requisition_service(db, req, user.id)
    return {"ok": True, "id": new_req.id, "name": new_req.name}
