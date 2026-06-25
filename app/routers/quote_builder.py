"""routers/quote_builder.py — Quote Builder modal, save, and export endpoints.

Serves the full-screen two-panel quote builder modal, handles save (with
revision support), and streams Excel/PDF exports.

Called by: Parts tab "Build Quote" button (HTMX), Alpine.js fetch (save)
Depends on: app.services.quote_builder_service, app.schemas.quote_builder
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from loguru import logger
from sqlalchemy.orm import Session

from ..constants import AccessKey
from ..database import get_db
from ..dependencies import get_quote_for_user, require_access, require_user
from ..models import User
from ..schemas.quote_builder import QuoteBuilderSaveRequest

router = APIRouter(tags=["quote-builder"])


def _parse_req_ids(requisition_ids: str) -> list[int]:
    """Parse a comma-separated requisition-id string into ints.

    Raises HTTP 400 on a malformed value or an empty selection.
    """
    try:
        ids = [int(x.strip()) for x in requisition_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "Invalid requisition IDs")
    if not ids:
        raise HTTPException(400, "No requisitions selected")
    return ids


def _customer_name_for_site(db: Session, customer_site_id: int | None) -> str:
    """Resolve the customer company name for a requisition's customer site ("" if
    none)."""
    if not customer_site_id:
        return ""
    from ..models import CustomerSite

    site = db.get(CustomerSite, customer_site_id)
    if site and site.company:
        return site.company.name or ""
    return ""


@router.get("/v2/partials/quote-builder/{req_id}")
async def quote_builder_modal(
    req_id: int,
    request: Request,
    requirement_ids: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Open the quote builder modal shell (lightweight HTML, no line data).

    Line data is fetched separately via the /data endpoint by the Alpine component on
    init, keeping the initial HTML payload small even for requisitions with 200+
    requirements.
    """
    from ..dependencies import get_req_for_user

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")

    from ..template_env import template_response

    return template_response(
        "htmx/partials/quote_builder/modal.html",
        {
            "request": request,
            "req": req,
            "customer_name": _customer_name_for_site(db, req.customer_site_id),
            "has_customer_site": bool(req.customer_site_id),
            "requirement_ids": requirement_ids or "",
        },
    )


@router.get("/v2/partials/quote-builder/multi")
async def quote_builder_modal_multi(
    request: Request,
    requisition_ids: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Open quote builder for multiple requisitions selected from the list page.

    Picks the first requisition as the primary (for customer site/quote record), loads
    requirements from all selected requisitions.
    """
    from ..dependencies import get_req_for_user

    req_id_list = _parse_req_ids(requisition_ids)

    # Use the first requisition as primary
    primary_req = get_req_for_user(db, user, req_id_list[0])
    if not primary_req:
        raise HTTPException(404, "Requisition not found")

    from ..template_env import template_response

    return template_response(
        "htmx/partials/quote_builder/modal.html",
        {
            "request": request,
            "req": primary_req,
            "customer_name": _customer_name_for_site(db, primary_req.customer_site_id),
            "has_customer_site": bool(primary_req.customer_site_id),
            "requirement_ids": "",
            "multi_req_ids": requisition_ids,
        },
    )


@router.get("/v2/partials/quote-builder/multi/data")
async def quote_builder_data_multi(
    requisition_ids: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return line data from multiple requisitions for the quote builder."""
    from ..dependencies import get_req_for_user
    from ..services.quote_builder_service import apply_smart_defaults, get_builder_data

    req_id_list = _parse_req_ids(requisition_ids)

    all_lines = []
    for rid in req_id_list:
        req = get_req_for_user(db, user, rid)
        if req:
            lines = get_builder_data(rid, db)
            all_lines.extend(lines)

    apply_smart_defaults(all_lines)
    return {"lines": all_lines}


@router.get("/v2/partials/quote-builder/{req_id}/data")
async def quote_builder_data(
    req_id: int,
    requirement_ids: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return line data as JSON for the quote builder Alpine component.

    Called by the Alpine component on init via fetch(). Keeps the modal HTML small and
    allows the browser to parse the JSON efficiently as a separate network request
    rather than inline in an HTML attribute.
    """
    from ..dependencies import get_req_for_user
    from ..services.quote_builder_service import apply_smart_defaults, get_builder_data

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")

    req_ids = None
    if requirement_ids:
        try:
            req_ids = [int(x.strip()) for x in requirement_ids.split(",") if x.strip()]
        except ValueError:
            req_ids = None

    lines = get_builder_data(req_id, db, requirement_ids=req_ids)
    apply_smart_defaults(lines)

    return {"lines": lines}


@router.post("/v2/partials/quote-builder/{req_id}/save")
async def quote_builder_save(
    req_id: int,
    payload: QuoteBuilderSaveRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save the quote from the builder modal."""
    from ..dependencies import get_req_for_user, require_requisition_access
    from ..services.quote_builder_service import save_quote_from_builder

    # Ownership guard: SALES/TRADER may only save quotes for requisitions they own
    # (no-op for buyer/manager/admin). 404 to avoid leaking existence.
    require_requisition_access(db, req_id, user)

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    if not req.customer_site_id:
        raise HTTPException(400, "Requisition must be linked to a customer site before quoting")

    try:
        result = save_quote_from_builder(db, req_id=req_id, payload=payload, user=user)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error("Quote builder save failed for req {}: {}", req_id, e)
        raise HTTPException(500, "Failed to save quote. Please try again.")

    return result


@router.get("/v2/partials/quote-builder/{req_id}/export/excel")
async def quote_builder_export_excel(
    req_id: int,
    quote_id: int,
    user: User = Depends(require_access(AccessKey.EXPORT_DATA)),
    db: Session = Depends(get_db),
):
    """Stream an Excel export of a saved quote."""
    quote = get_quote_for_user(db, user, quote_id)
    if quote.requisition_id != req_id:
        raise HTTPException(404, "Quote not found")

    from ..services.quote_builder_service import build_excel_export

    customer_name = ""
    if quote.customer_site and quote.customer_site.company:
        customer_name = quote.customer_site.company.name or ""

    try:
        xlsx_bytes = build_excel_export(
            line_items=quote.line_items or [],
            quote_number=quote.quote_number,
            customer_name=customer_name,
        )
    except Exception as e:
        logger.error("Excel export failed for quote {}: {}", quote_id, e)
        raise HTTPException(500, "Excel export failed. Please try again.")

    filename = f"{quote.quote_number}.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error("PDF generation failed for quote {}: {}", quote_id, e)
        raise HTTPException(500, "PDF generation failed")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{quote.quote_number}.pdf"'},
    )


# ── In-workspace Build-Quote tab ──────────────────────────────────────────────
# A single-stage inline reshape of the modal, mirroring the resell Build-Bid tab:
# lines + best-cost reference + seeded sell price, "Assemble" -> save_quote_from_builder
# -> clean inline summary. Owner/buyer-gated via require_requisition_access.


def _build_quote_tab_context(request: Request, db: Session, req, quote=None) -> dict:
    """Context for the Build-Quote tab body: seeded lines + latest-quote summary.

    Each line carries its best-cost reference + ACTIVE offers + a pre-filled sell
    seed (``build_quote_tab_data``). ``quote`` (when an assemble just happened, else the
    requisition's most recent quote) drives the clean inline summary via the
    ``quote_export_context`` whitelist — the same source the customer PDF renders from.
    """
    from ..models import Quote
    from ..services.quote_builder_service import (
        DEFAULT_MARKUP_PCT,
        DEFAULT_MIN_MARGIN_PCT,
        build_quote_tab_data,
        quote_export_context,
    )

    if quote is None:
        quote = (
            db.query(Quote)
            .filter(Quote.requisition_id == req.id)
            .order_by(Quote.revision.desc().nullslast(), Quote.id.desc())
            .first()
        )

    lines = build_quote_tab_data(db, req.id)
    # Compact reactive seed keyed by requirement id — passed to the Alpine component as a
    # single |tojson blob inside a SINGLE-quoted x-data (tojson emits double quotes, safe
    # only inside single quotes; mirrors quote_builder/modal.html + the resell tab).
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

    return {
        "request": request,
        "req": req,
        "lines": lines,
        "tab_data": tab_data,
        "markup_pct": DEFAULT_MARKUP_PCT,
        "min_margin_pct": DEFAULT_MIN_MARGIN_PCT,
        "has_customer_site": bool(req.customer_site_id),
        "quote": quote,
        "summary": quote_export_context(quote) if quote else None,
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
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error("Build-Quote assemble failed for req {}: {}", req_id, e)
        raise HTTPException(500, "Failed to assemble quote. Please try again.")

    from ..models import Quote

    quote = db.get(Quote, result["quote_id"])
    return template_response(
        "htmx/partials/requisitions/tabs/build_quote.html",
        _build_quote_tab_context(request, db, req, quote=quote),
    )
