"""Data Ops tab + CRM vendor/company merge, delete, bulk dedup, and admin api-health.

W4.8 split of the 1,032-line app/routers/htmx/settings.py — pure structural move: URLs
and behavior unchanged (same /v2/partials/settings/data-ops + /v2/partials/admin/* paths,
same htmx-views tag); routes attach to the shared router imported from .common.

Called by: app/main.py (via the package router mount).
Depends on: app.database, app.dependencies, app.models, app.services
    (merge services, connector_health), app.template_env, routers.htmx._shared, .common
"""

from fastapi import Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ....database import get_db
from ....dependencies import require_admin, require_user
from ....models import Company, User, VendorCard
from ....services.connector_health import get_health_dashboard
from ....template_env import template_response
from .._shared import _base_ctx
from .common import router, settings_toast


def _render_data_ops(request: Request, user: User, db: Session) -> Response:
    """Render the Data Ops tab partial — vendor/company dedup suggestions.

    Each scan is guarded independently. A scan that RAISES sets a per-scan
    ``*_scan_failed`` flag so the template can render a distinct error block instead
    of swallowing the failure into the reassuring "no duplicates found" empty state
    (a crashed scan must never look like a clean dataset). Reused by the merge
    endpoints so a successful merge re-renders the surrounding list and stale pairs
    drop without a manual refresh.
    """
    vendor_dupes: list = []
    company_dupes: list = []
    vendor_scan_failed = False
    company_scan_failed = False
    try:
        from ....vendor_utils import find_vendor_dedup_candidates

        vendor_dupes = find_vendor_dedup_candidates(db, threshold=85, limit=30)
    except Exception as e:
        vendor_scan_failed = True
        logger.warning(f"Vendor dedup scan failed: {e}")
    try:
        from ....company_utils import find_company_dedup_candidates

        company_dupes = find_company_dedup_candidates(db, threshold=85, limit=30)
    except Exception as e:
        company_scan_failed = True
        logger.warning(f"Company dedup scan failed: {e}")

    ctx = _base_ctx(request, user, "settings")
    ctx["vendor_dupes"] = vendor_dupes
    ctx["company_dupes"] = company_dupes
    ctx["vendor_scan_failed"] = vendor_scan_failed
    ctx["company_scan_failed"] = company_scan_failed
    return template_response("htmx/partials/settings/data_ops.html", ctx)


@router.get("/v2/partials/settings/data-ops", response_class=HTMLResponse)
async def settings_data_ops_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Admin data operations tab — vendor/company dedup suggestions."""
    from ....dependencies import is_admin

    if not is_admin(user):
        raise HTTPException(403, "Admin only")

    return _render_data_ops(request, user, db)


@router.post("/v2/partials/admin/vendor-merge", response_class=HTMLResponse)
async def admin_vendor_merge(
    request: Request,
    keep_id: int = Form(...),
    remove_id: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Merge two vendor cards via HTMX."""
    from ....dependencies import is_admin

    if not is_admin(user):
        raise HTTPException(403, "Admin only")

    from ....services.vendor_merge_service import merge_vendor_cards as _merge

    try:
        result = _merge(keep_id, remove_id, db)
        db.commit()
    except Exception as e:
        # Align with company-merge: the service raises ValueError on validation, but an
        # unexpected SQLAlchemy error here must surface as a toast, not a 500.
        db.rollback()
        message, kind = f"Vendor merge failed: {e}", "error"
    else:
        kept = db.get(VendorCard, result.get("kept", keep_id))
        kept_name = kept.display_name if kept and kept.display_name else "vendor"
        message = f"Merged into {kept_name}. {result.get('reassigned', 0)} records reassigned."
        kind = "success"

    resp = _render_data_ops(request, user, db)
    settings_toast(resp, message, kind=kind)
    return resp


@router.post("/v2/partials/admin/company-merge", response_class=HTMLResponse)
async def admin_company_merge(
    request: Request,
    keep_id: int = Form(...),
    remove_id: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Merge two companies via HTMX."""
    from ....dependencies import is_admin

    if not is_admin(user):
        raise HTTPException(403, "Admin only")

    from ....services.company_merge_service import merge_companies

    try:
        result = merge_companies(keep_id, remove_id, db)
        db.commit()
    except Exception as e:
        db.rollback()
        message, kind = f"Company merge failed: {e}", "error"
    else:
        kept = db.get(Company, result.get("kept", keep_id))
        kept_name = kept.name if kept and kept.name else "company"
        message, kind = f"Merged into {kept_name}.", "success"

    resp = _render_data_ops(request, user, db)
    settings_toast(resp, message, kind=kind)
    return resp


@router.post("/v2/partials/admin/vendor-delete-both", response_class=HTMLResponse)
async def admin_vendor_delete_both(
    request: Request,
    id_a: int = Form(...),
    id_b: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete BOTH vendor cards in a dedup pair (neither is worth keeping)."""
    from ....dependencies import is_admin

    if not is_admin(user):
        raise HTTPException(403, "Admin only")

    from ....services.vendor_merge_service import delete_vendor_cards

    try:
        result = delete_vendor_cards(id_a, id_b, db)
        db.commit()
    except Exception as e:
        db.rollback()
        message, kind = f"Vendor delete failed: {e}", "error"
    else:
        message = f"Deleted both vendors. {result.get('detached', 0)} records detached."
        kind = "success"

    resp = _render_data_ops(request, user, db)
    settings_toast(resp, message, kind=kind)
    return resp


@router.post("/v2/partials/admin/company-delete-both", response_class=HTMLResponse)
async def admin_company_delete_both(
    request: Request,
    id_a: int = Form(...),
    id_b: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete BOTH companies in a dedup pair (neither is worth keeping)."""
    from ....dependencies import is_admin

    if not is_admin(user):
        raise HTTPException(403, "Admin only")

    from ....services.company_merge_service import delete_companies

    try:
        result = delete_companies(id_a, id_b, db)
        db.commit()
    except Exception as e:
        db.rollback()
        message, kind = f"Company delete failed: {e}", "error"
    else:
        message = f"Deleted both companies. {result.get('detached', 0)} records detached."
        kind = "success"

    resp = _render_data_ops(request, user, db)
    settings_toast(resp, message, kind=kind)
    return resp


# Mass dedup actions accept a comma-joined "pairs" token list where each token is
# "<id_a>-<id_b>" (the two ids of one candidate pair). Mirrors the requisitions2 /
# customers bulk convention (one hidden field, server-side parse + per-item gate),
# but the dedup unit is a PAIR, not a single row, so the token carries both ids.
_MAX_DEDUP_PAIRS = 200


def _parse_dedup_pairs(raw: str) -> list[tuple[int, int]]:
    """Parse a "a-b,c-d" pair-token string into [(a, b), ...]; skip malformed tokens."""
    pairs: list[tuple[int, int]] = []
    for tok in (raw or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        a, _, b = tok.partition("-")
        if a.lstrip("-").isdigit() and b.lstrip("-").isdigit():
            pairs.append((int(a), int(b)))
    return pairs


async def _dedup_bulk(request, user, db, entity: str) -> HTMLResponse:
    """Shared body for vendor/company bulk dedup actions (merge | delete | dismiss).

    ``merge`` keeps the FIRST id of each pair (the template emits keeper-first tokens);
    ``delete`` removes both; ``dismiss`` is a view-only clear (no durable state yet — the
    rows just drop from this render and reappear on the next scan). Per-pair failures don't
    abort the batch, but each is logged at error level and the failing pair tokens are
    surfaced in the toast — any failure makes the toast an ``error`` (never green success).
    """
    from ....dependencies import is_admin

    if not is_admin(user):
        raise HTTPException(403, "Admin only")

    form = await request.form()
    action = (form.get("action") or "").strip()
    if action not in {"merge", "delete", "dismiss"}:
        raise HTTPException(400, f"Invalid action {action!r}")

    pairs = _parse_dedup_pairs(form.get("pairs", ""))
    if len(pairs) > _MAX_DEDUP_PAIRS:
        raise HTTPException(400, f"Maximum {_MAX_DEDUP_PAIRS} pairs per bulk action")

    if not pairs or action == "dismiss":
        # Dismiss is purely client-side (the row was already hidden); just re-render.
        resp = _render_data_ops(request, user, db)
        if pairs:
            settings_toast(resp, f"Dismissed {len(pairs)} pair(s) for now.", kind="success")
        return resp

    if entity == "vendor":
        from ....services.vendor_merge_service import delete_vendor_cards, merge_vendor_cards

        merge_fn, delete_fn, noun = merge_vendor_cards, delete_vendor_cards, "vendor"
    else:
        from ....services.company_merge_service import delete_companies, merge_companies

        merge_fn, delete_fn, noun = merge_companies, delete_companies, "company"

    done = 0
    failed_tokens: list[str] = []
    for a, b in pairs:
        try:
            if action == "merge":
                merge_fn(a, b, db)
            else:
                delete_fn(a, b, db)
            db.commit()
            done += 1
        except Exception as e:
            db.rollback()
            failed_tokens.append(f"{a}-{b}")
            logger.error("Bulk {} {}: pair {}-{} failed: {}", noun, action, a, b, e)

    verb = "Merged" if action == "merge" else "Deleted"
    failed = len(failed_tokens)
    message = f"{verb} {done} {noun} pair(s)."
    if failed:
        message += f" {failed} failed: {', '.join(failed_tokens)}."
    resp = _render_data_ops(request, user, db)
    # Any failure surfaces as an error toast — a partial failure must not look green.
    settings_toast(resp, message, kind="error" if failed else "success")
    return resp


@router.post("/v2/partials/admin/vendor-bulk", response_class=HTMLResponse)
async def admin_vendor_bulk(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Bulk merge/delete/dismiss selected vendor dedup pairs."""
    return await _dedup_bulk(request, user, db, "vendor")


@router.post("/v2/partials/admin/company-bulk", response_class=HTMLResponse)
async def admin_company_bulk(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Bulk merge/delete/dismiss selected company dedup pairs."""
    return await _dedup_bulk(request, user, db, "company")


@router.get("/v2/partials/admin/api-health", response_class=HTMLResponse)
async def admin_api_health(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Return the connector health dashboard assembled from api_sources telemetry."""
    health = get_health_dashboard(db)
    return template_response(
        "htmx/partials/admin/api_health.html",
        {"request": request, "health": health},
    )


# admin_data_ops (GET /v2/partials/admin/data-ops) + its admin/data_ops.html template were
# removed as superseded dupes of the settings Data Ops tab (GET /v2/partials/settings/data-ops,
# settings_data_ops_tab above). No template/JS/nav referenced the admin/data-ops route.
