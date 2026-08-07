"""List detail pane — lines, list/line CRUD, tabular import, publish/close lifecycle.

W4.8 split of the 2,830-line app/routers/resell.py — pure structural move: URLs and
behavior unchanged; every route attaches to the shared router imported from .common
(registration assembled in __init__).
"""

import json

from fastapi import Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...constants import (
    AccessKey,
    ExcessListStatus,
)
from ...database import get_db
from ...dependencies import require_access
from ...file_utils import ParseError, parse_tabular_file
from ...models import Company, User
from ...models.excess import ExcessLineItem
from ...services import (
    excess_mirror,
    excess_service,
)
from ...template_env import template_response
from .common import (
    ALLOWED_EXTENSIONS,
    MAX_UPLOAD_BYTES,
    _detail_context,
    _file_extension,
    _get_list_for_user,
    _require_owner,
    _to_decimal,
    _toast,
    router,
)


@router.get("/v2/partials/resell/{list_id}", response_class=HTMLResponse)
async def resell_detail(
    request: Request,
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Right detail partial — slim header, breadcrumb, chips, lazy tabs."""
    el, _ = _get_list_for_user(db, list_id, user)
    return template_response("htmx/partials/resell/detail.html", _detail_context(request, db, el, user))


@router.get("/v2/partials/resell/{list_id}/lines", response_class=HTMLResponse)
async def resell_lines(
    request: Request,
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Lazy Lines tab body — adaptive: 1 line → card, ≥2 → compact table."""
    el, _ = _get_list_for_user(db, list_id, user)
    return template_response("htmx/partials/resell/_lines.html", _detail_context(request, db, el, user))


# ── Modal forms ──────────────────────────────────────────────────────


@router.get("/v2/partials/resell/{list_id}/add-line-form", response_class=HTMLResponse)
async def resell_add_line_form(
    request: Request,
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Render the add-line modal (draft lists only)."""
    el, _ = _get_list_for_user(db, list_id, user)
    _require_owner(el, user)
    if el.status != ExcessListStatus.DRAFT:
        raise HTTPException(409, "Posted lists are locked. Close this list and create a new one to make changes.")
    return template_response(
        "htmx/partials/resell/add_line_modal.html",
        {"request": request, "list_id": list_id},
    )


@router.post("/api/resell/{list_id}/lines", response_class=HTMLResponse)
async def resell_add_line(
    request: Request,
    list_id: int,
    part_number: str = Form(...),
    quantity: int = Form(...),
    manufacturer: str = Form(""),
    condition: str = Form("New"),
    date_code: str = Form(""),
    asking_price: str = Form(""),
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Add a single line via the service, then re-render the whole detail panel.

    HTTP-boundary guards stay here (404-mask, can_post); the line construction, card
    resolve, counter bump, blank-part-number 400, and the guarded commit live in
    :func:`excess_service.add_line` (findings #33/#42 — thin-router discipline).
    """
    # 404-mask a non-owner on a private draft (finding #48) BEFORE the service's 403.
    _get_list_for_user(db, list_id, user)
    if not excess_service.can_post(user):
        raise HTTPException(403, "You do not have permission to post excess lists")
    el = excess_service.add_line(
        db,
        list_id,
        user,
        part_number=part_number,
        quantity=quantity,
        manufacturer=manufacturer or None,
        condition=condition or "New",
        date_code=date_code or None,
        asking_price=_to_decimal(asking_price),
    )
    # Re-render the WHOLE detail (not just the Lines tab): adding the first line to a
    # draft is what makes the header Post button appear (line_count > 0), so a Lines-only
    # swap would leave the header stale and the user with no way to publish (RS-5).
    return template_response("htmx/partials/resell/detail.html", _detail_context(request, db, el, user))


# ── Draft editing (finding #14 / D4) — all DRAFT-only + owner-only, thin over the
#    service guards (404 → 403 → 409). A draft has no offers/mirror, so these are
#    side-effect-free except total_line_items. ──────────────────────────────────


@router.get("/v2/partials/resell/{list_id}/lines/{line_id}/edit-form", response_class=HTMLResponse)
async def resell_edit_line_form(
    request: Request,
    list_id: int,
    line_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Render the pre-filled edit-line modal (owner-only, draft-only)."""
    el, _ = _get_list_for_user(db, list_id, user)
    _require_owner(el, user)
    if el.status != ExcessListStatus.DRAFT:
        raise HTTPException(409, "Posted lists are locked. Close this list and create a new one to make changes.")
    line = db.get(ExcessLineItem, line_id)
    if line is None or line.excess_list_id != el.id:
        raise HTTPException(404, f"Line {line_id} not found on list {list_id}")
    return template_response(
        "htmx/partials/resell/edit_line_modal.html",
        {"request": request, "list_id": list_id, "line": line},
    )


@router.get("/v2/partials/resell/{list_id}/edit-form", response_class=HTMLResponse)
async def resell_edit_list_form(
    request: Request,
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Render the pre-filled edit-list-header modal (owner-only, draft-only)."""
    el, _ = _get_list_for_user(db, list_id, user)
    _require_owner(el, user)
    if el.status != ExcessListStatus.DRAFT:
        raise HTTPException(409, "Posted lists are locked. Close this list and create a new one to make changes.")
    # (id, name) tuples only — the dropdown never needs the full Company entity.
    companies = db.execute(select(Company.id, Company.name).order_by(Company.name)).all()
    return template_response(
        "htmx/partials/resell/edit_list_modal.html",
        {"request": request, "list": el, "companies": companies},
    )


@router.patch("/api/resell/{list_id}/lines/{line_id}", response_class=HTMLResponse)
async def resell_update_line(
    request: Request,
    list_id: int,
    line_id: int,
    part_number: str = Form(...),
    quantity: int = Form(...),
    manufacturer: str = Form(""),
    condition: str = Form("New"),
    date_code: str = Form(""),
    asking_price: str = Form(""),
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Edit one draft line, then re-render the whole detail panel."""
    # 404-mask a non-owner on a private draft (existence not revealed) BEFORE the service's
    # owner-check would 403 it — consistent with the GET edit-form path (finding #3).
    _get_list_for_user(db, list_id, user)
    el = excess_service.update_line(
        db,
        list_id,
        line_id,
        user,
        part_number=part_number,
        quantity=quantity,
        manufacturer=manufacturer or None,
        condition=condition or "New",
        date_code=date_code or None,
        asking_price=_to_decimal(asking_price),
    )
    return template_response("htmx/partials/resell/detail.html", _detail_context(request, db, el, user))


@router.delete("/api/resell/{list_id}/lines/{line_id}", response_class=HTMLResponse)
async def resell_delete_line(
    request: Request,
    list_id: int,
    line_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Delete one draft line, then re-render the whole detail panel."""
    # 404-mask a non-owner on a private draft (finding #3).
    _get_list_for_user(db, list_id, user)
    el = excess_service.delete_line(db, list_id, line_id, user)
    return template_response("htmx/partials/resell/detail.html", _detail_context(request, db, el, user))


@router.patch("/api/resell/{list_id}", response_class=HTMLResponse)
async def resell_update_list(
    request: Request,
    list_id: int,
    title: str = Form(...),
    company_id: int = Form(...),
    notes: str = Form(""),
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Edit a draft list's header (title/customer/notes), then re-render the detail
    panel."""
    # 404-mask a non-owner on a private draft (finding #3).
    _get_list_for_user(db, list_id, user)
    el = excess_service.update_excess_list(db, list_id, user, title=title, notes=notes or None, company_id=company_id)
    return template_response("htmx/partials/resell/detail.html", _detail_context(request, db, el, user))


@router.delete("/api/resell/{list_id}", response_class=HTMLResponse)
async def resell_delete_list(
    request: Request,
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Delete a whole draft list, then send the user back to the workspace root.

    The route is the single authority on what happens next (finding #15): it answers with
    an ``HX-Redirect`` to ``/v2/resell`` — correct from BOTH render contexts (the workspace
    split pane and a deep-linked ``/v2/resell/{id}`` full page, which has no
    ``#resell-list-body`` to refresh) — mirroring the companies-delete pattern
    (htmx/companies/core.py). The redirect also fixes the address bar so a reload never
    reopens the deleted id (finding #8).
    """
    # 404-mask a non-owner on a private draft (finding #3).
    _get_list_for_user(db, list_id, user)
    excess_service.delete_excess_list(db, list_id, user)
    resp = HTMLResponse("")
    resp.headers["HX-Redirect"] = "/v2/resell"
    return _toast(resp, "List deleted")


@router.post("/api/resell/{list_id}/import-preview", response_class=HTMLResponse)
async def resell_import_preview(
    request: Request,
    list_id: int,
    file: UploadFile,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Parse an uploaded file and render the shared import preview grid."""
    # 404-mask a non-owner on a private draft (finding #48) BEFORE the owner 403.
    el, _ = _get_list_for_user(db, list_id, user)
    _require_owner(el, user)
    # Draft-only guard (finding #34) — identical to the confirm counterpart, so a posted
    # list fails at upload time instead of after the user reviews the whole preview.
    if el.status != ExcessListStatus.DRAFT:
        raise HTTPException(409, "Posted lists are locked. Close this list and create a new one to make changes.")
    if not excess_service.can_post(user):
        raise HTTPException(403, "You do not have permission to post excess lists")
    filename = file.filename or ""
    if _file_extension(filename) not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type '{_file_extension(filename)}'")
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File too large")
    # Silent-failure e: a corrupt/unreadable file raises ParseError (distinct from a
    # genuinely-empty one) so we can tell the user which of the two actually happened,
    # instead of collapsing both to "No data rows found".
    try:
        rows = parse_tabular_file(content, filename)
    except ParseError as exc:
        raise HTTPException(400, "We couldn't read this file — it may be corrupt or not a valid spreadsheet") from exc
    if not rows:
        raise HTTPException(400, "No data rows found")
    result = excess_service.preview_import(rows)
    return template_response(
        "htmx/partials/resell/import_preview.html",
        {
            "request": request,
            "list_id": list_id,
            "filename": filename,
            **result,
            "all_valid_rows_json": json.dumps(result["all_valid_rows"]),
        },
    )


@router.post("/api/resell/{list_id}/import-confirm", response_class=HTMLResponse)
async def resell_import_confirm(
    request: Request,
    list_id: int,
    rows_json: str = Form(...),
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Confirm a previewed import, then re-render the Lines tab."""
    # 404-mask a non-owner on a private draft (finding #48) BEFORE the owner 403.
    el, _ = _get_list_for_user(db, list_id, user)
    _require_owner(el, user)
    if el.status != ExcessListStatus.DRAFT:
        raise HTTPException(409, "Posted lists are locked. Close this list and create a new one to make changes.")
    if not excess_service.can_post(user):
        raise HTTPException(403, "You do not have permission to post excess lists")
    try:
        rows = json.loads(rows_json)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, "Invalid import payload") from exc
    # Shape guard (finding #31, mirrors resell_assemble_bid): the payload must be a list
    # of dicts — '5' or '["x"]' would otherwise reach confirm_import's row parser and 500.
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise HTTPException(400, "Invalid import payload")
    result = excess_service.confirm_import(db, list_id, user, rows)
    # Re-render the WHOLE detail so the header Post button appears once the draft has
    # lines — a Lines-only swap leaves the header stale (RS-5).
    el = excess_service.get_excess_list(db, list_id)
    resp = template_response("htmx/partials/resell/detail.html", _detail_context(request, db, el, user))
    # Single server-side toast emitter (finding #16): the success message and the skipped-
    # rows warning share ONE HX-Trigger message — the old client-side handler fired a
    # green 'Imported N lines' unconditionally (even on 4xx) and clobbered this warning.
    imported = result.get("imported", 0)
    skipped = result.get("skipped", 0)
    message = f"Imported {imported} line{'s' if imported != 1 else ''}"
    if skipped > 0:
        message += f" — {skipped} row(s) skipped (invalid quantity or blank part number)"
    resp.headers["HX-Trigger"] = json.dumps(
        {"showToast": {"message": message, "type": "warning" if skipped else "success"}}
    )
    return resp


@router.post("/api/resell/{list_id}/publish", response_class=HTMLResponse)
async def resell_publish(
    request: Request,
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Publish a list: flip to open, then re-render detail (no Sighting mirror —
    the resell→Sighting dual-write is retired, SIMPLIFICATION_SPEC §5.3).

    A foreign private DRAFT 404-masks before any 403 (finding #48).
    """
    el, _ = _get_list_for_user(db, list_id, user)
    if not excess_service.can_post(user):
        raise HTTPException(403, "You do not have permission to post excess lists")
    if el.owner_id != user.id:
        raise HTTPException(403, "Only the list owner can publish it")
    excess_mirror.publish_list(db, list_id, user)
    el = excess_service.get_excess_list(db, list_id)
    return template_response("htmx/partials/resell/detail.html", _detail_context(request, db, el, user))


@router.post("/api/resell/{list_id}/close", response_class=HTMLResponse)
async def resell_close(
    request: Request,
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """End the posting window (owner-only): stamp close_at (status stays on bidding),
    re-render detail."""
    el = excess_service.close_list(db, list_id, user)
    return template_response("htmx/partials/resell/detail.html", _detail_context(request, db, el, user))


@router.post("/api/resell/{list_id}/close-without-bid", response_class=HTMLResponse)
async def resell_close_without_bid(
    request: Request,
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Close a posted list WITHOUT bidding (owner-only): flip to the terminal ``closed``
    state + stamp close_at + outcome ``no_bids``, then re-render detail (D5)."""
    el = excess_service.close_list_without_bid(db, list_id, user)
    return template_response("htmx/partials/resell/detail.html", _detail_context(request, db, el, user))


@router.post("/api/resell/{list_id}/close-awarded", response_class=HTMLResponse)
async def resell_close_awarded(
    request: Request,
    list_id: int,
    outcome: str = Form(...),
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Close an AWARDED list with its recorded outcome (owner-only): the ladder's last
    step (award → outcome, spec §5.3) — sold / scrapped / withdrawn — then re-render
    detail."""
    el = excess_service.close_awarded_list(db, list_id, user, outcome)
    return template_response("htmx/partials/resell/detail.html", _detail_context(request, db, el, user))
