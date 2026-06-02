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

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.exc import IntegrityError
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


def _row_removed_response(request: Request, db: Session, toast_message: str) -> HTMLResponse:
    """Build the HTMX response after a pending row is approved/rejected.

    The row itself is removed by the caller's ``hx-swap="outerHTML"`` against an
    empty body. We attach a ``showToast`` HX-Trigger (matching the pattern in
    app/routers/requisitions2.py) and, when the removed row was the LAST one,
    an out-of-band swap that replaces the table region with the empty-state so
    the page doesn't show a headerless empty table.
    """
    remaining = db.query(OemSpecCodePending).count()
    if remaining == 0:
        body = template_response(
            "htmx/partials/admin/spec_codes_empty.html",
            {"request": request, "oob": True},
        ).body.decode()
    else:
        body = ""
    response = HTMLResponse(body, status_code=200)
    response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": toast_message}})
    return response


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
    try:
        db.commit()
    except IntegrityError:
        # Another admin already approved this same (oem, spec_code)
        # mapping in a concurrent request. The uq_oem_spec_code unique
        # constraint catches it cleanly; return a 409 so the UI can show
        # an idempotent "already handled" message instead of a 500.
        db.rollback()
        logger.info(
            "spec_codes: concurrent approve collision; oem={} spec_code={}",
            row.oem,
            row.spec_code,
        )
        raise HTTPException(
            status_code=409,
            detail="this mapping has already been approved by another admin",
        ) from None
    return _row_removed_response(request, db, f"Approved {approved.oem} {approved.spec_code}")


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
    oem = row.oem
    spec_code = row.spec_code
    db.add(
        OemSpecCodeBlacklist(
            oem=oem,
            spec_code=spec_code,
            rejected_mpns=rejected_mpns,
            rejected_by_user_id=user.id,
            reason=body.reason,
        )
    )
    db.delete(row)
    db.commit()
    return _row_removed_response(request, db, f"Rejected {oem} {spec_code}")


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
    """Re-invoke the resolver for an existing pending row, replacing it only on success.

    Returns empty body on a fresh resolution (row swapped); a warning fragment if the
    resolver returned unresolved or raised.

    The fresh resolution runs FIRST (``propose`` forces a new LLM attempt instead of
    reusing the existing pending row) and the original row is swapped out only once we
    have a better result. A miss or a transient failure therefore leaves the buyer audit
    trail (citations, confidence, MPNs) completely intact — overwrite-on-success, never
    delete-then-hope. ``propose`` also runs the ~60s LLM call without holding a DB
    transaction, so the connection isn't pinned for its duration.
    """
    from ...services.spec_code_resolver import SpecCodeResolver

    row = db.get(OemSpecCodePending, pending_id)
    if row is None:
        raise HTTPException(status_code=404, detail="pending mapping not found")

    spec_code = row.spec_code
    oem = row.oem

    resolver = SpecCodeResolver(db)
    try:
        # Force a fresh attempt (don't reuse the row we're trying to replace). No
        # writes happen here, so the original row is untouched whatever the outcome.
        result, persist_payload = await resolver.propose(spec_code, oem=oem, allow_pending_reuse=False)
    except Exception:
        db.rollback()
        logger.exception(
            "spec_codes: re-resolve failed for pending_id={}",
            pending_id,
        )
        return template_response(
            "htmx/partials/admin/spec_codes_reresolve_unresolved.html",
            {"request": request, "spec_code": spec_code, "error": True},
        )

    if result.status == "unresolved":
        # Fresh attempt found nothing — leave the original pending row intact. No
        # overwrite, no data loss; the admin can still reject to blacklist it.
        return template_response(
            "htmx/partials/admin/spec_codes_reresolve_unresolved.html",
            {"request": request, "spec_code": spec_code, "error": False},
        )

    # Fresh resolution succeeded — atomically swap the old row for the new one. The
    # LLM call is already done, so this transaction is short.
    try:
        with db.begin_nested():
            db.delete(row)
            db.flush()
            if persist_payload is not None:
                resolver.persist(persist_payload)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "spec_codes: re-resolve swap failed for pending_id={}",
            pending_id,
        )
        return template_response(
            "htmx/partials/admin/spec_codes_reresolve_unresolved.html",
            {"request": request, "spec_code": spec_code, "error": True},
        )

    return HTMLResponse("", status_code=200)
