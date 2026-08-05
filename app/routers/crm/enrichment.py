import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ...config import settings
from ...database import get_db
from ...dependencies import can_manage_account, require_user
from ...models import Company, CustomerSite, User
from ...schemas.crm import EnrichDomainRequest
from ...services.credential_service import get_credential_cached
from ...template_env import template_response
from ...utils.claude_client import claude_configured

router = APIRouter()


def _normalize_domain(value: str) -> str:
    """Strip scheme, www, and any path from a domain/website string."""
    return value.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]


def _wants_html(request: Request) -> bool:
    """True for HTMX requests — the enrich button POSTs with the HX-Request header.

    HTMX callers get a rendered result panel; programmatic/API callers get JSON.
    """
    return request.headers.get("HX-Request") == "true"


def _paid_provider_configured() -> bool:
    """True when at least one keyed (paid) enrichment provider credential is set."""
    return bool(
        get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY")
        or claude_configured()
        or get_credential_cached("hunter_enrichment", "HUNTER_API_KEY")
        or get_credential_cached("lusha_enrichment", "LUSHA_API_KEY")
    )


def _require_enrichment_provider() -> None:
    """Raise 503 only when NO enrichment path can run at all.

    Keys-off honesty (spec §7): the SAM.gov path is free — it needs no credential (the
    connector falls back to the public DEMO_KEY tier), so a fully keyless instance still
    enriches from SAM.gov. The old guard 503'd whenever the paid keys were absent,
    blocking the free path. Only when the SAM.gov feature gate is off AND no paid key is
    configured is there genuinely nothing to run.
    """
    if settings.sam_gov_enrichment_enabled or _paid_provider_configured():
        return
    raise HTTPException(
        503,
        "Enrichment is off — enable SAM.gov (SAM_GOV_ENRICHMENT_ENABLED) or set a "
        "provider key (EXPLORIUM_API_KEY, ANTHROPIC_API_KEY, HUNTER_API_KEY, or LUSHA_API_KEY)",
    )


# ── Enrichment (shared for vendors + customers) ─────────────────────────


async def _run_company_enrichment(company_id: int, domain: str, name: str) -> None:
    """Background worker: run the account enrichment waterfall for one company.

    Scheduled by ``enrich_company`` (HTMX path) so the click never blocks on the ~20-40s of
    external-provider calls: firmographics (``enrich_entity``: SAM.gov + Clay/Explorium/Lusha
    + Anthropic) then contact discovery (``find_suggested_contacts_with_errors``: Hunter/Clay).
    Firmographics are committed here; the transient result (which fields changed, discovered
    contacts, errored providers) is recorded in ``company_enrich_runs`` so the enrich-status
    poller can render the same ``_enrich_result.html`` panel the synchronous path produced.

    A firmographics failure marks the outcome ``blocked`` (the poller shows a "couldn't
    complete" toast). A contact-discovery hiccup is NOT blocked — it degrades to the amber
    "couldn't reach" banner via ``errored_providers``, mirroring the old inline behavior.

    Opens its own session — FastAPI has already returned the response and closed the request
    session by the time this runs. Must NEVER raise: it is a fire-and-forget task.
    """
    from ...database import SessionLocal
    from ...services.company_enrich_runs import CompanyEnrichOutcome, company_enrich_runs

    db = SessionLocal()
    blocked = False
    updated: list[str] = []
    suggested: list[dict] = []
    errored: list[str] = []
    company_missing = False
    try:
        company = db.get(Company, company_id)
        if company is None:
            # Company vanished between click and run — nothing to enrich; drop the guard.
            company_missing = True
        else:
            # Firmographics — commit on success; a genuine outage marks the run blocked (toast).
            try:
                from ...enrichment_service import apply_enrichment_to_company, enrich_entity

                enrichment = await enrich_entity(domain, name)
                updated = apply_enrichment_to_company(company, enrichment)
                db.commit()
            except Exception as e:
                logger.opt(exception=e).warning("Account enrichment firmographics failed for {}", company_id)
                db.rollback()
                blocked = True

            # Contact discovery — degrade to the amber "couldn't reach" banner, never a toast.
            try:
                from ...enrichment_service import find_suggested_contacts_with_errors

                suggested, errored = await find_suggested_contacts_with_errors(domain, name)
            except Exception as e:
                logger.opt(exception=e).warning("Account enrichment contact discovery failed for {}", company_id)
                errored = ["all"]
    except Exception:
        logger.exception("Account enrichment task crashed for {}", company_id)
        blocked = True
    finally:
        db.close()
        if company_missing:
            company_enrich_runs.clear(company_id)
        else:
            company_enrich_runs.finish(
                company_id,
                CompanyEnrichOutcome(
                    blocked=blocked,
                    updated_fields=updated,
                    suggested=suggested,
                    errored_providers=errored,
                ),
            )


def _resolve_company_domain(company: Company, payload: EnrichDomainRequest) -> str:
    """Domain to enrich: explicit payload override, else the company's domain/website."""
    domain = company.domain or company.website or ""
    if domain:
        domain = _normalize_domain(domain)
    if payload.domain:
        domain = payload.domain
    return domain


@router.post("/api/enrich/company/{company_id}")
async def enrich_company(
    company_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    payload: EnrichDomainRequest = EnrichDomainRequest(),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Enrich a customer company with external data.

    Content-negotiates on the HX-Request header:

      * HTMX callers (the Enrich button) **enqueue** the provider waterfall on a background
        task and return an "Enriching…" poller immediately — the click never blocks on the
        ~20-40s of SAM.gov/Clay/Explorium/Lusha/Anthropic/Hunter calls. When the run lands,
        the poller swaps in the firmographics + discovered-contacts panel (or a "couldn't
        complete" toast if a data source was down).
      * Programmatic callers get the synchronous JSON result (firmographics awaited inline).
    """
    _require_enrichment_provider()

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "You do not have access to this company")
    domain = _resolve_company_domain(company, payload)
    if not domain:
        raise HTTPException(400, "No domain available — set company website or domain first")

    if _wants_html(request):
        # Async path: schedule the waterfall and return the polling "Enriching…" panel.
        from ...services.company_enrich_runs import company_enrich_runs

        if company_enrich_runs.begin(company_id):
            background_tasks.add_task(_run_company_enrichment, company_id, domain, company.name or "")
        return template_response(
            "htmx/partials/customers/enrich_status.html",
            {"request": request, "company": company},
        )

    # JSON/programmatic path: synchronous firmographics (unchanged contract).
    from ...enrichment_service import apply_enrichment_to_company, enrich_entity

    enrichment = await enrich_entity(domain, company.name)
    updated = apply_enrichment_to_company(company, enrichment)
    db.commit()
    return {"ok": True, "updated_fields": updated, "enrichment": enrichment}


@router.get("/api/enrich/company/{company_id}/status")
async def enrich_company_status(
    company_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Poll target for the account "Enriching…" panel — reflects the background run's
    state.

    While the run is in flight, re-renders the polling panel (keep polling). When it lands,
    returns the ``_enrich_result.html`` panel (firmographics + discovered contacts) and
    answers HTTP 286 to STOP polling; a blocked run (a data source was unavailable) also
    fires the "couldn't complete" toast. A deleted company or an already-consumed outcome
    stops polling with an empty body.
    """
    from ...services.company_enrich_runs import company_enrich_runs

    company = db.get(Company, company_id)
    if not company:
        # Polling sub-resource: htmx neither swaps nor cancels an `every 2s` poll on a 4xx,
        # so a 404 would leave the panel hammering this route. 286 stops it; empty clears it.
        return HTMLResponse("", status_code=286)
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "You do not have access to this company")

    if company_enrich_runs.is_running(company_id):
        return template_response(
            "htmx/partials/customers/enrich_status.html",
            {"request": request, "company": company},
        )

    outcome = company_enrich_runs.consume_outcome(company_id)
    if outcome is None:
        # No run in flight and no pending outcome (already consumed, or lost on restart) —
        # stop polling and clear the panel.
        return HTMLResponse("", status_code=286)

    active_sites = (
        db.query(CustomerSite)
        .filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True))
        .order_by(CustomerSite.site_name)
        .all()
    )
    response = template_response(
        "htmx/partials/shared/_enrich_result.html",
        {
            "request": request,
            "entity": company,
            "entity_type": "company",
            "updated_fields": outcome.updated_fields,
            "show_contacts": True,
            "suggested": outcome.suggested,
            "errored_providers": outcome.errored_providers,
            "active_sites": active_sites,
            "add_target": "closest li",
            "add_swap": "outerHTML",
            "paid_providers_off": not _paid_provider_configured(),
        },
    )
    response.status_code = 286  # htmx's stop-polling status — the result panel still swaps in.
    if outcome.blocked:
        # Bridged to the global $store.toast via the showToast HX-Trigger convention.
        response.headers["HX-Trigger"] = json.dumps(
            {
                "showToast": {
                    "message": "Enrichment couldn't complete — a data source was unavailable. Try again shortly.",
                    "type": "error",
                }
            }
        )
    return response
