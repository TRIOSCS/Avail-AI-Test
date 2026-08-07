"""app/routers/htmx/materials/actions.py — Material card actions — background
enrichment/crosses runners, enrich, find-crosses, crosses-status poller, insights.

W4.8 split of the 1,483-line app/routers/htmx/materials.py — pure structural
move: URLs and behavior unchanged; every route attaches to the shared router
imported from .common (registration assembled in __init__).
"""

from fastapi import BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ....constants import (
    AccessKey,
)
from ....database import get_db
from ....dependencies import (
    require_access,
)
from ....models import (
    Offer,
    User,
)
from ....template_env import template_response
from .cards import material_detail_partial
from .common import router


async def _run_card_enrichment(material_id: int) -> None:
    """Background worker: run the authoritative ladder + structured-spec pass for one card.

    Scheduled by ``enrich_material`` so the click never blocks on the ~30s of web
    extraction. Runs the authoritative ladder (verified -> web -> OEM -> flagged
    inference) with refresh=True so even a terminal card re-enters the ladder, then a
    status-gated structured-spec pass. The Haiku card-enrichment path was removed in SP1
    (2026-06-09). Records the run's outcome in ``enrich_runs`` so the enrich-status poller
    can surface the "couldn't complete" toast on a blocked/no-op run (which leaves the
    card ``unenriched``, indistinguishable from success by the status column alone).

    Opens its own session — FastAPI has already returned the response and closed the
    request session by the time this runs. Must NEVER raise: it is a fire-and-forget task.
    """
    from ....constants import MaterialEnrichmentStatus
    from ....database import SessionLocal
    from ....services.material_enrich_runs import enrich_runs

    db = SessionLocal()
    blocked = False
    try:
        # enrich_cards self-handles ClaudeError / disabled-source outages internally and
        # returns a counts dict (it does NOT raise on a backend outage). Capture it so the
        # poller can tell the user when nothing actually happened, not report false success.
        counts: dict = {}
        try:
            from ....services.authoritative_enrichment_service import enrich_cards

            counts = await enrich_cards([material_id], db, refresh=True)
        except Exception as e:
            logger.exception("Enrichment failed for material {}: {}", material_id, e)
            blocked = True

        # A single card produces exactly one status tally on success. If no real status
        # landed, or a Claude outage / disabled source blocked the run, the card is unchanged.
        status_tallies = sum(int(counts.get(s, 0)) for s in MaterialEnrichmentStatus)
        if counts.get("claude_error") or counts.get("disabled_sources") or status_tallies == 0:
            blocked = True
            logger.warning("Enrichment no-op for material {} (counts={})", material_id, counts)

        try:
            from ....services.spec_enrichment_service import enrich_card_specs

            await enrich_card_specs([material_id], db, force=True)
        except Exception as e:
            logger.warning("Spec enrichment failed for material {}: {}", material_id, e)
    except Exception:
        logger.exception("Card enrichment task crashed for material {}", material_id)
        blocked = True
    finally:
        db.close()
        enrich_runs.finish(material_id, blocked=blocked)


@router.post("/v2/partials/materials/{material_id}/enrich", response_class=HTMLResponse)
async def enrich_material(
    request: Request,
    material_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Queue authoritative enrichment for a material card and return immediately.

    The heavy work (authoritative ladder + structured-spec pass, ~30s of web extraction)
    runs in a FastAPI background task so the click never blocks. The card is flipped to
    the ``unenriched`` ("Queued for enrichment") marker and the detail partial is returned
    right away with the enrich-status badge polling; that poller lands the refreshed detail
    on success — or the "couldn't complete" toast on a blocked run — when the task finishes.
    """
    from ....constants import MaterialEnrichmentStatus
    from ....models.intelligence import MaterialCard
    from ....services.material_enrich_runs import enrich_runs

    mc = db.get(MaterialCard, material_id)
    if not mc:
        raise HTTPException(404, "Material not found")

    # Guard double-enqueue: a run already in flight for this card must not stack another.
    if enrich_runs.begin(material_id):
        # Flip to the queued/in-progress marker so the badge polls while the worker runs
        # (also resets an already-terminal card so its poller re-activates on re-enrich).
        mc.enrichment_status = MaterialEnrichmentStatus.UNENRICHED
        db.commit()
        background_tasks.add_task(_run_card_enrichment, material_id)

    return await material_detail_partial(request, material_id, user, db)


async def _run_card_crosses(material_id: int) -> None:
    """Background worker: run the AI crosses/substitutes lookup for one material card.

    Scheduled by ``find_crosses`` so the click never blocks on the ~30s Claude call. Opens
    its own session (FastAPI has already returned the response and closed the request
    session by the time this runs), persists the deduplicated crosses onto the card, and
    records the run's outcome in ``crosses_runs`` so the crosses-status poller can swap in
    the results (``done``) or show the retry/error state (``blocked``). Because a
    legitimate no-results run leaves ``cross_references`` empty — indistinguishable from
    "never ran" by the column alone — the registry outcome is what the poller trusts.

    Must NEVER raise: it is a fire-and-forget task.
    """
    from ....database import SessionLocal
    from ....models.intelligence import MaterialCard
    from ....services.material_enrich_runs import crosses_runs
    from ....utils.claude_client import claude_json as ai_json
    from ....utils.normalization import normalize_mpn_key

    db = SessionLocal()
    blocked = False
    try:
        mc = db.get(MaterialCard, material_id)
        if not mc:
            blocked = True
            return

        mpn = mc.display_mpn or mc.normalized_mpn
        mfg = mc.manufacturer or "unknown"
        category = mc.category or "electronic component"

        import asyncio as _asyncio

        result = await _asyncio.wait_for(
            ai_json(
                f"List all known CROSSES and SUBSTITUTES for this electronic component:\n"
                f"  MPN: {mpn}\n"
                f"  Manufacturer: {mfg}\n"
                f"  Category: {category}\n\n"
                f"Include:\n"
                f"1. Cross-manufacturer equivalents\n"
                f"2. Pin-compatible alternatives / clones\n"
                f"3. Same-family variants (different speed grades, temp ranges, packages)\n"
                f"4. Second-source parts\n\n"
                f"Only include REAL part numbers you are confident exist. Up to 10 results.\n\n"
                f'Respond with JSON: {{"crosses": [{{"mpn": "...", "manufacturer": "..."}}]}}',
                system=(
                    "You are an expert electronic component sourcing engineer. "
                    "List real, verified part numbers only — no guessing."
                ),
                model_tier="smart",
                max_tokens=2048,
            ),
            timeout=30.0,
        )

        crosses = result.get("crosses", []) if isinstance(result, dict) else []
        # Deduplicate: exclude the card's own MPN (both display and normalized forms)
        own_mpns = {normalize_mpn_key(mc.normalized_mpn or ""), normalize_mpn_key(mc.display_mpn or "")} - {""}
        crosses = [
            c for c in crosses if isinstance(c, dict) and c.get("mpn") and normalize_mpn_key(c["mpn"]) not in own_mpns
        ]

        mc.cross_references = crosses
        db.commit()
    except Exception as exc:
        logger.warning("Cross-reference search failed for material {}: {}", material_id, exc)
        db.rollback()
        blocked = True
    finally:
        db.close()
        crosses_runs.finish(material_id, blocked=blocked)


@router.post("/v2/partials/materials/{material_id}/find-crosses", response_class=HTMLResponse)
async def find_crosses(
    request: Request,
    material_id: int,
    background_tasks: BackgroundTasks,
    refresh: bool = Form(False),
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Queue the on-demand AI crosses/substitutes lookup and return immediately.

    The Claude call (``asyncio.wait_for`` up to 30s) used to run INLINE before responding,
    so the Crosses section spun and the page felt frozen. It now runs in a FastAPI
    background task and this handler returns the "Finding crosses…" polling partial right
    away; the crosses-status poller swaps in the results (or the retry state) when the task
    finishes. A cache hit (already-populated ``cross_references`` and no explicit refresh)
    still returns the loaded section synchronously — no background work needed.

    Called by: HTMX button on the material detail Crosses section.
    Depends on: _run_card_crosses (background worker), crosses_runs (double-enqueue guard).
    """
    from ....models.intelligence import MaterialCard
    from ....services.material_enrich_runs import crosses_runs

    mc = db.get(MaterialCard, material_id)
    if not mc:
        raise HTTPException(404, "Material not found")

    # Cache hit: return the loaded section immediately (skip on explicit refresh).
    if mc.cross_references and not refresh:
        return template_response(
            "htmx/partials/materials/crosses_section.html",
            {"request": request, "card": mc},
        )

    # Guard double-enqueue: a lookup already in flight for this card must not stack another.
    if crosses_runs.begin(material_id):
        background_tasks.add_task(_run_card_crosses, material_id)

    # Return the polling in-progress state immediately (no inline 30s block).
    return template_response(
        "htmx/partials/materials/crosses_status.html",
        {"request": request, "card": mc},
    )


@router.get("/v2/partials/materials/{card_id}/crosses-status", response_class=HTMLResponse)
async def material_crosses_status_partial(
    request: Request,
    card_id: int,
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Poll the in-flight AI crosses lookup and swap in the result when it lands.

    While the background lookup is running this returns the "Finding crosses…" polling
    partial (keep polling). On the terminal outcome it returns the refreshed
    ``crosses_section.html`` (loaded results, empty "none found", or — on a blocked run —
    the retry/error state) and answers HTTP 286 so htmx swaps the section and STOPS
    polling. If no run is tracked (e.g. the process restarted mid-run) it stops polling and
    renders the card's current section rather than spinning forever.
    """
    from ....models.intelligence import MaterialCard
    from ....services import material_enrich_runs
    from ....services.material_enrich_runs import crosses_runs

    card = db.get(MaterialCard, card_id)
    if not card or card.deleted_at is not None:
        # Polling sub-resource: htmx neither swaps nor cancels an `every Ns` poll on a 4xx,
        # so a 404 would hammer this route forever. 286 stops the poll; empty body clears it.
        return HTMLResponse("", status_code=286)

    outcome = crosses_runs.consume_outcome(card_id)
    if outcome is None and crosses_runs.is_running(card_id):
        # Still running → keep polling.
        return template_response(
            "htmx/partials/materials/crosses_status.html",
            {"request": request, "card": card},
        )

    # Terminal (done / blocked) or no tracked run → swap in the section and stop polling.
    ctx = {"request": request, "card": card}
    if outcome == material_enrich_runs.BLOCKED:
        ctx["error"] = "Cross-reference search failed. Please try again."
    response = template_response("htmx/partials/materials/crosses_section.html", ctx)
    response.status_code = 286  # htmx's stop-polling status — the section still swaps in.
    return response


@router.get("/v2/partials/materials/{material_id}/insights", response_class=HTMLResponse)
async def material_insights(
    request: Request,
    material_id: int,
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Return MPN insights panel for a material card."""
    from ....models.intelligence import MaterialCard

    mc = db.query(MaterialCard).filter(MaterialCard.id == material_id).first()
    if not mc:
        raise HTTPException(404, "Material not found")

    # Get related offers for pricing data
    offers = (
        (
            db.query(Offer)
            .filter(Offer.normalized_mpn == mc.normalized_mpn, Offer.unit_price.isnot(None))
            .order_by(Offer.created_at.desc())
            .limit(20)
            .all()
        )
        if mc.normalized_mpn
        else []
    )

    return template_response(
        "htmx/partials/materials/insights.html",
        {"request": request, "material": mc, "offers": offers},
    )
