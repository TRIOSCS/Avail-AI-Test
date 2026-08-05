"""routers/quote_builder.py — Build-Quote tab, combined multi-req builder, PDF export.

ONE quote builder (Wave 3): the in-workspace Build-Quote tab is the single flow.
The old two-panel modal (its /data + /save JSON endpoints and Excel export) is
deleted; the multi-req entry from the requisitions list renders the SAME tab body
(merged lines across the selection) inside the global modal and assembles through
``save_quote_from_builder_multi``.

Called by: requisition detail Build-Quote tab (HTMX), requisitions list bulk
           Build-Quote button (2+ selected → /multi)
Depends on: app.services.quote_builder_service, app.schemas.quote_builder
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from loguru import logger
from sqlalchemy.orm import Session

from ..constants import AccessKey
from ..database import get_db
from ..dependencies import get_quote_for_user, require_access, require_user
from ..models import Requisition, User
from ..schemas.quote_builder import QuoteBuilderSaveRequest

# Defined in the service layer so the dependency runs router → service (not the reverse);
# re-exported through this router for its two call sites.
from ..services.quote_requisitions import _customer_name_for_site

router = APIRouter(tags=["quote-builder"])


def _parse_req_ids(requisition_ids: str) -> list[int]:
    """Parse a comma-separated requisition-id string into ints.

    Raises HTTP 400 on a malformed value or an empty selection.
    """
    try:
        ids = [int(x.strip()) for x in requisition_ids.split(",") if x.strip()]
    except ValueError as e:
        raise HTTPException(400, "Invalid requisition IDs") from e
    if not ids:
        raise HTTPException(400, "No requisitions selected")
    return ids


# ── Combined cross-req quote (OQ-02) — MUST be declared before the /{req_id} routes ──
# FastAPI matches in declaration order; "multi" would otherwise be captured by the
# {req_id} path param (and fail int coercion → 422), so every /multi* route lives here,
# above the single-req routes.


@router.get("/v2/partials/quote-builder/multi")
async def quote_builder_modal_multi(
    request: Request,
    requisition_ids: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Open the COMBINED quote builder for 2+ requisitions selected on the list page.

    Every selected requisition is ownership-checked (SALES/TRADER may only combine reqs
    they own — 404 otherwise, so unowned existence isn't leaked), then the selection must
    share ONE customer (``validate_same_customer``). On a customer mismatch we render a
    plain static error fragment (HTTP 200) into #modal-content — NOT an exception — so the
    modal shows an honest per-requisition breakdown of the clash instead of a bare toast.
    ``requisition_ids[0]`` is the primary/anchor (its customer site + the quote record).

    Renders the SAME Build-Quote tab body as the single-req flow (merged lines across the
    selection) inside the global modal — the old two-panel modal is deleted (Wave 3).
    """
    from ..dependencies import get_req_for_user
    from ..services.quote_requisitions import CustomerMismatchError, validate_same_customer
    from ..template_env import template_response

    req_id_list = _parse_req_ids(requisition_ids)

    for rid in req_id_list:
        if not get_req_for_user(db, user, rid):
            raise HTTPException(404, "Requisition not found")

    primary_req = db.get(Requisition, req_id_list[0])
    if not primary_req:
        raise HTTPException(404, "Requisition not found")

    try:
        validate_same_customer(db, req_id_list)
    except CustomerMismatchError as exc:
        return template_response(
            "htmx/partials/quote_builder/multi_error.html",
            {"request": request, "message": exc.detail},
        )

    context = _build_quote_tab_context(request, db, primary_req, all_req_ids=req_id_list)
    context["customer_name"] = _customer_name_for_site(db, primary_req.customer_site_id)
    return template_response("htmx/partials/quote_builder/combined.html", context)


@router.post("/v2/partials/quote-builder/multi/assemble", response_class=Response)
async def quote_builder_assemble_multi(
    request: Request,
    requisition_ids: str = "",
    selections_json: str = Form(...),
    quote_id: int | None = Form(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Assemble ONE combined quote spanning every selected requisition's checked lines.

    Ownership-checks each requisition (looped ``require_requisition_access`` +
    ``get_req_for_user`` — an unowned req can't be smuggled into the combine), then
    delegates to ``save_quote_from_builder_multi`` (same revision lifecycle, requisition
    state transition, and knowledge-ledger capture as the single-req assemble). A customer
    mismatch surfaces as an honest 400, a missing/unknown req as 404 — never a silent
    partial write. Re-renders the merged Build-Quote body into the modal.
    """
    import json

    from ..dependencies import get_req_for_user, require_requisition_access
    from ..services.quote_builder_service import save_quote_from_builder_multi
    from ..services.quote_requisitions import CustomerMismatchError
    from ..template_env import template_response

    req_ids = _parse_req_ids(requisition_ids)

    for rid in req_ids:
        require_requisition_access(db, rid, user)
        if not get_req_for_user(db, user, rid):
            raise HTTPException(404, "Requisition not found")

    primary_req = db.get(Requisition, req_ids[0])
    if not primary_req:
        raise HTTPException(404, "Requisition not found")

    try:
        raw = json.loads(selections_json)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, "Invalid quote payload") from exc
    if not isinstance(raw, list) or not raw:
        raise HTTPException(400, "Select at least one line to assemble a quote")

    try:
        payload = QuoteBuilderSaveRequest(lines=raw, quote_id=quote_id)
    except Exception as exc:
        raise HTTPException(422, "Invalid quote line data") from exc

    try:
        result = save_quote_from_builder_multi(db, req_ids=req_ids, payload=payload, user=user)
    except CustomerMismatchError as e:
        # Subclass of ValueError — MUST be caught before the ValueError arm below.
        raise HTTPException(400, e.detail) from e
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        logger.error("Combined quote assemble failed for reqs {}: {}", req_ids, e)
        raise HTTPException(500, "Failed to assemble quote. Please try again.") from e

    from ..models import Quote

    quote = db.get(Quote, result["quote_id"])
    return template_response(
        "htmx/partials/requisitions/tabs/build_quote.html",
        _build_quote_tab_context(request, db, primary_req, quote=quote, all_req_ids=req_ids),
    )


@router.get("/v2/partials/quote-builder/{req_id}/export/pdf")
async def quote_builder_export_pdf(
    req_id: int,
    quote_id: int,
    user: User = Depends(require_access(AccessKey.EXPORT_DATA)),
    db: Session = Depends(get_db),
):
    """Stream a PDF export of a saved quote (reuses existing PDF generator)."""
    import asyncio

    quote = get_quote_for_user(db, user, quote_id)
    if quote.requisition_id != req_id:
        raise HTTPException(404, "Quote not found")

    from ..services.document_service import generate_quote_report_pdf

    try:
        loop = asyncio.get_running_loop()
        pdf_bytes = await loop.run_in_executor(None, generate_quote_report_pdf, quote.id, db)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        logger.error("PDF generation failed for quote {}: {}", quote_id, e)
        raise HTTPException(500, "PDF generation failed") from e

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{quote.quote_number}.pdf"'},
    )


# ── In-workspace Build-Quote tab ──────────────────────────────────────────────
# A single-stage inline reshape of the modal, mirroring the resell Build-Bid tab:
# lines + best-cost reference + seeded sell price, "Assemble" -> save_quote_from_builder
# -> clean inline summary. Owner/buyer-gated via require_requisition_access.


def _build_quote_tab_context(
    request: Request,
    db: Session,
    req,
    quote=None,
    *,
    all_req_ids: list[int] | None = None,
) -> dict:
    """Context for the Build-Quote tab body: seeded lines + latest-quote summary.

    Each line carries its best-cost reference + ACTIVE offers + a pre-filled sell
    seed (``build_quote_tab_data``). ``quote`` (when an assemble just happened, else the
    requisition's most recent quote) drives the clean inline summary via the
    ``quote_export_context`` whitelist — the same source the customer PDF renders from.

    ``all_req_ids`` (2+ ids, anchor first) switches the SAME body into combined mode:
    lines merge across every selected requisition and the assemble form posts to the
    /multi/assemble endpoint, re-rendering inside the modal (#combined-quote-body)
    instead of the detail-page tab (#tab-content).
    """
    from ..models import Quote
    from ..services.quote_builder_service import (
        DEFAULT_MARKUP_PCT,
        DEFAULT_MIN_MARGIN_PCT,
        build_quote_tab_data,
        quote_export_context,
    )
    from ..services.quote_preflight import quote_preflight
    from ..services.quote_requisitions import quotes_for_requisition

    req_ids = all_req_ids or [req.id]
    is_multi = len(req_ids) > 1

    if quote is None:
        # quotes_for_requisition (join-based) so a SECONDARY requisition of a combined
        # quote surfaces that quote here too — not just the ones it anchors.
        quote = quotes_for_requisition(db, req.id).order_by(Quote.revision.desc().nullslast(), Quote.id.desc()).first()

    lines = []
    for rid in req_ids:
        lines.extend(build_quote_tab_data(db, rid))
    # Compact reactive seed keyed by requirement id — passed to the Alpine component as a
    # single |tojson blob inside a SINGLE-quoted x-data (tojson emits double quotes, safe
    # only inside single quotes; mirrors the resell build-bid tab).
    tab_data = {
        li["requirement_id"]: {
            "sel": False,
            "price": "",
            "qty": li["qty"],
            "cost": li["best_cost"],
            "offerId": li["best_offer_id"],
            # Per-line offer choices (internal only — vendor identity never crosses into the
            # customer doc; quote_export_context strips it). Lets the salesperson pick WHICH
            # offer is used; the chosen offerId + its cost drive the persisted QuoteLine and
            # the live margin. id->cost keeps the @change handler a pure client-side lookup.
            "offers": [{"id": o["id"], "cost": o["unit_price"]} for o in li["offers"]],
            "seed": li["sell_seed"],
            "mpn": li["mpn"],
            "mfr": li["manufacturer"],
            "cond": li["condition"],
        }
        for li in lines
    }

    if is_multi:
        ids_str = ",".join(str(rid) for rid in req_ids)
        assemble_url = f"/v2/partials/quote-builder/multi/assemble?requisition_ids={ids_str}"
        assemble_target = "#combined-quote-body"
    else:
        assemble_url = f"/v2/partials/requisitions/{req.id}/build-quote/assemble"
        assemble_target = "#tab-content"

    return {
        "request": request,
        "req": req,
        "req_count": len(req_ids),
        "is_multi": is_multi,
        "assemble_url": assemble_url,
        "assemble_target": assemble_target,
        "lines": lines,
        "tab_data": tab_data,
        "markup_pct": DEFAULT_MARKUP_PCT,
        "min_margin_pct": DEFAULT_MIN_MARGIN_PCT,
        "has_customer_site": bool(req.customer_site_id),
        "quote": quote,
        "summary": quote_export_context(quote) if quote else None,
        # Advisory pre-send checks (DNC / non-US COO / MPN drift). Surfaced as a banner
        # above Send; never blocks the send (see services/quote_preflight.py).
        "preflight_warnings": [w.to_dict() for w in quote_preflight(db, quote)] if quote else [],
    }


@router.get("/v2/partials/requisitions/{req_id}/build-quote", response_class=Response)
async def build_quote_tab(
    req_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Lazy Build-Quote tab body — single-stage inline assembly for one requisition.

    Owner/buyer-gated (``require_requisition_access`` no-ops for buyer/manager/admin;
    SALES/TRADER must own the requisition or get 404). Renders each line's best-cost
    reference + seeded sell price, an "Assemble" action, and (once a quote exists) the
    clean inline summary + Download-PDF / Send.
    """
    from ..dependencies import get_req_for_user, require_requisition_access
    from ..template_env import template_response

    require_requisition_access(db, req_id, user)
    req = get_req_for_user(db, user, req_id)
    return template_response(
        "htmx/partials/requisitions/tabs/build_quote.html",
        _build_quote_tab_context(request, db, req),
    )


@router.post("/v2/partials/requisitions/{req_id}/build-quote/assemble", response_class=Response)
async def build_quote_assemble(
    req_id: int,
    request: Request,
    selections_json: str = Form(...),
    quote_id: int | None = Form(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Assemble a quote from the checked lines (owner/buyer-gated), then re-render the
    tab.

    ``selections_json`` is a JSON array of builder lines (the ``QuoteBuilderLine`` shape).
    Delegates to ``save_quote_from_builder`` so the revision lifecycle, requisition state
    transition, and knowledge-ledger capture are preserved exactly as the modal path.
    """
    import json

    from ..dependencies import get_req_for_user, require_requisition_access
    from ..services.quote_builder_service import save_quote_from_builder
    from ..template_env import template_response

    require_requisition_access(db, req_id, user)
    req = get_req_for_user(db, user, req_id)
    if not req.customer_site_id:
        raise HTTPException(400, "Link a customer to this requisition before quoting")

    try:
        raw = json.loads(selections_json)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, "Invalid quote payload") from exc
    if not isinstance(raw, list) or not raw:
        raise HTTPException(400, "Select at least one line to assemble a quote")

    try:
        payload = QuoteBuilderSaveRequest(lines=raw, quote_id=quote_id)
    except Exception as exc:
        raise HTTPException(422, "Invalid quote line data") from exc

    try:
        result = save_quote_from_builder(db, req_id=req_id, payload=payload, user=user)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        logger.error("Build-Quote assemble failed for req {}: {}", req_id, e)
        raise HTTPException(500, "Failed to assemble quote. Please try again.") from e

    from ..models import Quote

    quote = db.get(Quote, result["quote_id"])
    return template_response(
        "htmx/partials/requisitions/tabs/build_quote.html",
        _build_quote_tab_context(request, db, req, quote=quote),
    )
