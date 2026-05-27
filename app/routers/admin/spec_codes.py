"""Admin router — pending OEM spec-code mapping approval queue.

Routes (all require require_settings_access):
- GET  /admin/spec-codes/pending                       — list page (HTMX partial)
- POST /admin/spec-codes/pending/{id}/approve          — promote to OemSpecCode
                                                         (edited_avl=null uses
                                                         proposed_avl as-is;
                                                         non-null replaces it)
- POST /admin/spec-codes/pending/{id}/reject           — move MPNs to blacklist
- POST /admin/spec-codes/pending/{id}/re-resolve       — re-run LLM with current
                                                         blacklist

Called by: app/routers/admin/__init__.py (included via router)
Depends on: app/models/sourcing.py, app/services/spec_code_resolver.py,
            app/schemas/spec_codes.py, app/template_env.py
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import require_settings_access
from ...models import User
from ...models.sourcing import (
    OemSpecCode,
    OemSpecCodeBlacklist,
    OemSpecCodePending,
)
from ...schemas.spec_codes import ApproveActionBody, RejectActionBody
from ...template_env import template_response

router = APIRouter(tags=["admin"])


@router.get("/admin/spec-codes/pending", response_class=HTMLResponse)
async def list_pending(
    request: Request,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Render the pending spec-code approval queue (HTMX partial)."""
    rows = db.query(OemSpecCodePending).order_by(OemSpecCodePending.discovered_at.desc()).all()
    return template_response(
        "htmx/partials/admin/spec_codes_pending.html",
        {"request": request, "rows": rows, "user": user},
    )


@router.post("/admin/spec-codes/pending/{pending_id}/approve", response_class=HTMLResponse)
async def approve(
    pending_id: int,
    body: ApproveActionBody,
    request: Request,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Promote a pending mapping to the authoritative OemSpecCode table.

    If body.edited_avl is None, promote with the LLM's proposed_avl as-is. If
    body.edited_avl is non-null, promote with the buyer-corrected list.
    """
    row = db.get(OemSpecCodePending, pending_id)
    if row is None:
        raise HTTPException(status_code=404, detail="pending mapping not found")

    avl_to_save = [entry.model_dump() for entry in body.edited_avl] if body.edited_avl is not None else row.proposed_avl
    approved = OemSpecCode(
        oem=row.oem,
        spec_code=row.spec_code,
        avl=avl_to_save,
        source="llm_approved",
        approved_by_user_id=user.id,
        approved_at=datetime.now(timezone.utc),
    )
    db.add(approved)
    db.delete(row)
    db.commit()
    return HTMLResponse("", status_code=200)


@router.post("/admin/spec-codes/pending/{pending_id}/reject", response_class=HTMLResponse)
async def reject(
    pending_id: int,
    body: RejectActionBody,
    request: Request,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Reject a pending mapping — blacklist the rejected MPNs and delete the row.

    If body.rejected_mpns is empty, all MPNs in proposed_avl are rejected.
    """
    row = db.get(OemSpecCodePending, pending_id)
    if row is None:
        raise HTTPException(status_code=404, detail="pending mapping not found")

    rejected_mpns = body.rejected_mpns or [entry["mpn"] for entry in (row.proposed_avl or []) if entry.get("mpn")]
    db.add(
        OemSpecCodeBlacklist(
            oem=row.oem,
            spec_code=row.spec_code,
            rejected_mpns=rejected_mpns,
            rejected_by_user_id=user.id,
            reason=body.reason,
        )
    )
    db.delete(row)
    db.commit()
    return HTMLResponse("", status_code=200)


@router.post(
    "/admin/spec-codes/pending/{pending_id}/re-resolve",
    response_class=HTMLResponse,
)
async def re_resolve(
    pending_id: int,
    request: Request,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Delete the existing pending row and re-invoke the resolver.

    Returns empty body on a fresh resolution (row replaced); a warning fragment if the
    resolver returned unresolved.
    """
    from ...services.spec_code_resolver import SpecCodeResolver

    row = db.get(OemSpecCodePending, pending_id)
    if row is None:
        raise HTTPException(status_code=404, detail="pending mapping not found")

    spec_code = row.spec_code
    oem = row.oem
    db.delete(row)
    db.commit()

    resolver = SpecCodeResolver(db)
    result = await resolver.resolve(spec_code, oem=oem)
    if result.status == "unresolved":
        return HTMLResponse(
            '<tr><td colspan="7" class="px-4 py-3 text-sm text-amber-700 bg-amber-50">'
            f"Re-resolution of <code>{spec_code}</code> returned no result.</td></tr>",
            status_code=200,
        )
    return HTMLResponse("", status_code=200)
