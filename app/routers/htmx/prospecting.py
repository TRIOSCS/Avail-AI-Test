"""routers/htmx/prospecting.py — Prospecting / lead UI partials (HTMX + Alpine).

Server-rendered HTML partials for the prospecting surface: the prospect list/grid,
stats, add-domain, detail panel, claim/dismiss/release/enrich + enrich-status poller,
and the reclaim/reassign admin actions. Extracted verbatim from htmx_views.py (same
`/v2/partials/prospecting` + `/v2/partials/prospects` paths, same `htmx-views` tag).

Called by: app/main.py (router mount).
Depends on: app.models, app.dependencies, app.database, app.services, ._shared
"""

import html as html_mod
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload

from ...constants import (
    AccessKey,
    ProspectAccountStatus,
    UserRole,
)
from ...database import get_db
from ...dependencies import (
    is_manager_or_admin,
    require_access,
    require_buyer,
    require_user,
)
from ...models import (
    User,
)
from ...models.prospect_account import ProspectAccount
from ...services.prospect_priority import build_priority_snapshot, build_signal_tags, contacts_summary
from ...template_env import template_response
from ...utils.search_builder import SearchBuilder
from ._shared import _base_ctx

router = APIRouter(tags=["htmx-views"])


# ── Prospecting partials ──────────────────────────────────────────────


# Statuses shown in the default ("All") view — dismissed/converted are hidden
# unless explicitly selected via the filter pills.
_PROSPECT_DEFAULT_STATUSES = ("suggested", "claimed")

# A background enrichment 'running' longer than this is treated as failed (its worker
# died mid-job) so the enrich flow self-heals instead of wedging forever.
_ENRICH_STALE_SECONDS = 180


def _enrich_is_stale(started_iso) -> bool:
    """True when a 'running' enrich job started longer than _ENRICH_STALE_SECONDS
    ago."""
    if not started_iso:
        return False
    try:
        started = datetime.fromisoformat(started_iso)
    except (ValueError, TypeError):
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).total_seconds() > _ENRICH_STALE_SECONDS


def _enrich_in_progress(enrichment_data) -> bool:
    """True only when a background enrichment is *genuinely* still running.

    The single source of truth for "is this prospect currently enriching?" — shared by
    the detail context (button disable), the enrich trigger (double-trigger guard), and
    the status poller (stop-polling). Returns True when ``enrich_status == 'running'``
    AND the job started within ``_ENRICH_STALE_SECONDS``; a ``'running'`` flag left
    behind by a crashed/OOM-killed worker is stale, returns False, and so is treated as
    re-enrichable everywhere (button re-enabled, trigger restarts, poller surfaces the
    failure) instead of wedging forever. A fresh in-flight job returns True and stays
    protected from a duplicate spawn.
    """
    ed = enrichment_data or {}
    return ed.get("enrich_status") == "running" and not _enrich_is_stale(ed.get("enrich_started_at"))


def _prospect_toast(response, message: str, kind: str = "success") -> None:
    """Attach a showToast HX-Trigger so the Alpine $store.toast surfaces feedback."""
    response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": message, "type": kind}})


def _prospect_error_toast(message: str) -> HTMLResponse:
    """Honest error feedback for an HTMX action that has no card to re-render.

    HTMX suppresses non-2xx swaps and the JSON HTTPException handler carries no
    showToast, so raising a 4xx here would leave the modal open with ZERO feedback (a
    silent no-op). Instead return a 200 that swaps nothing (HX-Reswap: none) but fires
    an error showToast — mirroring the reassign handler's ValueError path, which also
    returns 200 + a toast.
    """
    resp = HTMLResponse("", headers={"HX-Reswap": "none"})
    _prospect_toast(resp, message, "error")
    return resp


def _wants_detail(request: Request) -> bool:
    """True when an action came from the detail view (targets #main-content) rather than
    from an in-grid card (targets #prospect-<id>) — so we return the right partial."""
    return request.headers.get("HX-Target") == "main-content"


def _reclaim_ui_flags(user: User, prospect) -> dict:
    """Reclaim/reassign button visibility for one (user, prospect) pair.

    A prospect is "swept" when ``swept_from_owner_id`` is set — an auto-swept dormant
    account parked back in the pool. The sweep email tells the former owner to reclaim it
    "from the Prospecting tab", so these flags drive that UI:

    - can_reclaim: former owner, a manager/admin, or the configured sweep-manager email
      (mirrors ``reclaim_prospect_account``'s permission gate).
    - in_cooldown: the plain former owner is inside the 30-day post-sweep cooldown
      (supervisors bypass it) — the reclaim button is shown disabled with the unlock date.
    - can_reassign: a manager/admin (mirrors ``reassign_account``'s gate); requires a
      linked company since reassign acts on the company.
    """
    if prospect.swept_from_owner_id is None:
        return {
            "is_swept": False,
            "can_reclaim": False,
            "can_reassign": False,
            "in_cooldown": False,
            "cooldown_until": None,
        }

    from ...config import settings

    supervisor = is_manager_or_admin(user)
    manager_email = settings.account_sweep_manager_email
    is_email_manager = bool(manager_email and user.email == manager_email)
    is_former_owner = prospect.swept_from_owner_id == user.id
    is_supervisor = supervisor or is_email_manager

    in_cooldown = False
    cooldown_until = None
    blocked = prospect.reclaim_blocked_until
    if is_former_owner and not is_supervisor and blocked is not None:
        if blocked.tzinfo is None:
            blocked = blocked.replace(tzinfo=timezone.utc)
        if blocked > datetime.now(timezone.utc):
            in_cooldown = True
            cooldown_until = blocked.strftime("%b %d, %Y")

    return {
        "is_swept": True,
        "can_reclaim": is_former_owner or is_supervisor,
        "can_reassign": supervisor and prospect.company_id is not None,
        "in_cooldown": in_cooldown,
        "cooldown_until": cooldown_until,
    }


def _prospect_card_ctx(request: Request, user: User, prospect) -> dict:
    """Context for rendering a single prospect card (snapshot + contact summary maps,
    keyed by id so _card.html renders identically in the grid and in OOB swaps)."""
    ctx = _base_ctx(request, user, "prospecting")
    ctx["prospect"] = prospect
    ctx["snapshots"] = {prospect.id: build_priority_snapshot(prospect)}
    ctx["contact_stats_map"] = {prospect.id: contacts_summary(prospect.contacts_preview)}
    ctx["reclaim_ui_map"] = {prospect.id: _reclaim_ui_flags(user, prospect)}
    return ctx


def _prospect_detail_ctx(request: Request, user: User, prospect) -> dict:
    """Context for the detail partial — surfaces the buyer-ready snapshot, signal tags,
    contacts, and similar customers the scoring services compute."""
    ctx = _base_ctx(request, user, "prospecting")
    ctx["prospect"] = prospect
    ctx["enrichment"] = prospect.enrichment_data or {}
    ctx["warm_intro"] = (prospect.enrichment_data or {}).get("warm_intro", {})
    ctx["snapshot"] = build_priority_snapshot(prospect)
    ctx["signal_tags"] = build_signal_tags(prospect.readiness_signals)
    ctx["contacts"] = prospect.contacts_preview or []
    ctx["contact_stats"] = contacts_summary(prospect.contacts_preview)
    ctx["similar_customers"] = prospect.similar_customers or []
    ctx["reclaim_ui"] = _reclaim_ui_flags(user, prospect)
    # Resume the enrich poller only if a background enrichment is genuinely in flight; a
    # stale 'running' (crashed worker) leaves enrich_state None so the button re-enables.
    ctx["enrich_state"] = "running" if _enrich_in_progress(prospect.enrichment_data) else None
    return ctx


def _prospect_stats_ctx(db: Session) -> dict:
    """Canonical prospecting KPIs (single definition, shared by the stats route and the
    OOB refresh after grid actions).

    "Buyer ready" = is_buyer_ready over SUGGESTED.
    """
    from ...config import settings as _settings

    suggested = db.query(ProspectAccount).filter(ProspectAccount.status == ProspectAccountStatus.SUGGESTED).all()
    claimed = (
        db.query(sqlfunc.count(ProspectAccount.id))
        .filter(ProspectAccount.status == ProspectAccountStatus.CLAIMED)
        .scalar()
        or 0
    )
    screened_out_count = (
        sum(1 for p in suggested if (p.enrichment_data or {}).get("ai_screen", {}).get("verdict") == "screened_out")
        if _settings.ai_screen_enabled
        else 0
    )
    return {
        "total": len(suggested),
        "buyer_ready": sum(1 for p in suggested if build_priority_snapshot(p)["is_buyer_ready"]),
        "call_now": sum(1 for p in suggested if (p.readiness_score or 0) >= 70),
        "claimed": claimed,
        "screened_out": screened_out_count,
    }


def _status_visible_under_filter(new_status: str, flt_status: str) -> bool:
    """Whether a card with `new_status` should remain visible under the active filter.

    Default (empty filter = "All") shows suggested + claimed; an explicit filter shows
    only that status.
    """
    if flt_status:
        return new_status == flt_status
    return new_status in _PROSPECT_DEFAULT_STATUSES


def _prospect_action_response(
    request: Request,
    user: User,
    db: Session,
    prospect,
    *,
    message: str,
    kind: str,
    flt_status: str = "",
) -> HTMLResponse:
    """Build the response for a claim/dismiss/release action.

    Detail-view actions (HX-Target=main-content) return the full refreshed detail. Grid
    actions return `_action_oob.html`: the updated card (omitted → removed when it leaves
    the active filter) plus an OOB refresh of the #prospect-stats panel.
    """
    if _wants_detail(request):
        resp = template_response("htmx/partials/prospecting/detail.html", _prospect_detail_ctx(request, user, prospect))
    else:
        ctx = _prospect_card_ctx(request, user, prospect)
        ctx["status"] = flt_status  # so the re-rendered card's buttons carry the filter forward
        ctx["include_card"] = _status_visible_under_filter(prospect.status, flt_status)
        ctx.update(_prospect_stats_ctx(db))
        resp = template_response("htmx/partials/prospecting/_action_oob.html", ctx)
    _prospect_toast(resp, message, kind)
    return resp


@router.get("/v2/partials/prospecting", response_class=HTMLResponse)
async def prospecting_list_partial(
    request: Request,
    q: str = "",
    status: str = "",
    sort: str = "ai_match_desc",
    scope: str = "all",
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    user: User = Depends(require_access(AccessKey.PROSPECTING)),
    db: Session = Depends(get_db),
):
    """Return the prospecting card grid as an HTML partial.

    Sorts: ai_match_desc (default) ranks by trio_match_score DESC then opportunity_score
    DESC then readiness_score DESC; buyer_ready_desc ranks by the composite buyer-ready
    score from build_priority_snapshot; fit_desc and recent_desc sort in SQL.
    Dismissed prospects are hidden unless filtered for.

    ``scope`` ("all" | "mine") scopes the grid AND the pill counts to the current user's
    own claimed prospects (``claimed_by == user.id``), mirroring the Approvals See-All /
    See-Mine toggle. Defaults to "all".
    """
    scope = "mine" if scope == "mine" else "all"

    base = db.query(ProspectAccount)
    if status:
        base = base.filter(ProspectAccount.status == status)
    else:
        base = base.filter(ProspectAccount.status.in_(_PROSPECT_DEFAULT_STATUSES))
    if q.strip():
        sb = SearchBuilder(q.strip())
        base = base.filter(sb.ilike_filter(ProspectAccount.name, ProspectAccount.domain))
    if scope == "mine":
        base = base.filter(ProspectAccount.claimed_by == user.id)

    total = base.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    offset = (page - 1) * per_page

    if sort == "ai_match_desc":
        from ...config import settings as _settings

        if _settings.ai_screen_enabled:
            # AI-screen on: the screened-out split is a JSONB verdict predicate we keep in
            # Python (portable across PG/SQLite). The pool is already filtered to the
            # active status/search; the bucket is capped before render (see list.html).
            rows = base.all()
            screened_out_rows = [
                p for p in rows if (p.enrichment_data or {}).get("ai_screen", {}).get("verdict") == "screened_out"
            ]
            rows = [p for p in rows if (p.enrichment_data or {}).get("ai_screen", {}).get("verdict") != "screened_out"]
            rows.sort(
                key=lambda p: (
                    -(p.trio_match_score or 0),
                    -(p.opportunity_score or 0),
                    -(p.readiness_score or 0),
                    (p.name or "").lower(),
                )
            )
            total = len(rows)
            total_pages = max(1, (total + per_page - 1) // per_page)
            prospects = rows[offset : offset + per_page]
        else:
            # AI-screen off (default): trio_match/opportunity/readiness are all persisted
            # indexed columns, so rank + paginate in SQL instead of hydrating the whole
            # (only-grows) pool into memory each request. coalesce(.,0) keeps NULLs sorted
            # deterministically and the ordering dialect-portable. total/total_pages stay
            # the SQL base.count() computed above (no screened-out split to subtract).
            screened_out_rows = []
            base = base.order_by(
                sqlfunc.coalesce(ProspectAccount.trio_match_score, 0).desc(),
                sqlfunc.coalesce(ProspectAccount.opportunity_score, 0).desc(),
                sqlfunc.coalesce(ProspectAccount.readiness_score, 0).desc(),
                sqlfunc.lower(ProspectAccount.name),
            )
            prospects = base.offset(offset).limit(per_page).all()
    elif sort == "buyer_ready_desc":
        screened_out_rows = []
        # Rank by the persisted buyer_ready_score cache (kept in lockstep with
        # build_priority_snapshot by the ProspectAccount before_insert/before_update
        # listener), so we page in SQL instead of loading + snapshotting every row.
        # coalesce(.,0) keeps ordering deterministic and dialect-portable even if a row
        # somehow predates the backfill.
        base = base.order_by(
            sqlfunc.coalesce(ProspectAccount.buyer_ready_score, 0).desc(),
            ProspectAccount.fit_score.desc(),
            ProspectAccount.readiness_score.desc(),
            sqlfunc.lower(ProspectAccount.name),
        )
        prospects = base.offset(offset).limit(per_page).all()
    else:
        screened_out_rows = []
        if sort == "fit_desc":
            base = base.order_by(ProspectAccount.fit_score.desc(), ProspectAccount.readiness_score.desc())
        elif sort == "recent_desc":
            base = base.order_by(ProspectAccount.created_at.desc())
        else:
            base = base.order_by(ProspectAccount.readiness_score.desc(), ProspectAccount.fit_score.desc())
        prospects = base.offset(offset).limit(per_page).all()

    snapshots = {p.id: build_priority_snapshot(p) for p in prospects}
    contact_stats_map = {p.id: contacts_summary(p.contacts_preview) for p in prospects}
    reclaim_ui_map = {p.id: _reclaim_ui_flags(user, p) for p in prospects}

    # Per-status counts for the filter pills (respect the active search, not the active
    # status filter, so each pill shows its own stable total).
    count_q = db.query(ProspectAccount.status, sqlfunc.count(ProspectAccount.id))
    if q.strip():
        sb = SearchBuilder(q.strip())
        count_q = count_q.filter(sb.ilike_filter(ProspectAccount.name, ProspectAccount.domain))
    if scope == "mine":
        count_q = count_q.filter(ProspectAccount.claimed_by == user.id)
    status_counts = dict(count_q.group_by(ProspectAccount.status).all())
    all_total = sum(status_counts.get(s, 0) for s in _PROSPECT_DEFAULT_STATUSES)

    from ...config import settings as _list_settings

    ctx = _base_ctx(request, user, "prospecting")
    ctx.update(
        {
            "prospects": prospects,
            "snapshots": snapshots,
            "contact_stats_map": contact_stats_map,
            "reclaim_ui_map": reclaim_ui_map,
            "q": q,
            "status": status,
            "sort": sort,
            "scope": scope,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "status_counts": status_counts,
            "all_total": all_total,
            "screened_out_prospects": screened_out_rows if sort == "ai_match_desc" else [],
            "ai_screen_enabled": _list_settings.ai_screen_enabled,
        }
    )
    return template_response("htmx/partials/prospecting/list.html", ctx)


# Sprint 8 prospecting static routes — must precede {prospect_id} catch-all
@router.get("/v2/partials/prospecting/stats", response_class=HTMLResponse)
async def prospecting_stats(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the prospecting stats summary panel.

    "Buyer ready" uses the canonical is_buyer_ready from build_priority_snapshot — the
    same definition the list ranking uses — so the KPI never contradicts the grid.
    """
    return template_response(
        "htmx/partials/prospecting/stats.html",
        {"request": request, **_prospect_stats_ctx(db)},
    )


@router.post("/v2/partials/prospecting/add-domain", response_class=HTMLResponse)
async def add_prospect_domain(
    request: Request,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Manually submit a domain to the prospect pool.

    Returns an inline result chip.
    """
    from ...services.prospect_claim import add_prospect_manually

    form = await request.form()
    domain = (form.get("domain") or "").strip()
    if not domain:
        resp = HTMLResponse(
            '<div class="bg-rose-50 border border-rose-200 rounded p-2 text-sm text-rose-700">'
            "Enter a domain (e.g. acme.com).</div>"
        )
        _prospect_toast(resp, "Enter a domain first", "error")
        return resp

    try:
        result = add_prospect_manually(domain, user.id, db)
    except (ValueError, RuntimeError) as exc:
        logger.warning("Manual prospect add failed for {!r}: {}", domain, exc)
        resp = HTMLResponse(
            '<div class="bg-rose-50 border border-rose-200 rounded p-2 text-sm text-rose-700">'
            f"Could not add {html_mod.escape(domain)}.</div>"
        )
        _prospect_toast(resp, "Could not add prospect", "error")
        return resp

    # Service returns a dict ({prospect_id, name, domain, status, is_new}), not an ORM row.
    pid = result["prospect_id"]
    name = html_mod.escape(result.get("name") or domain)
    verb = "Added" if result.get("is_new") else "Already in pool"
    resp = template_response(
        "htmx/partials/prospecting/add_result.html",
        {"request": request, "pid": pid, "name": name, "verb": verb, "is_new": result.get("is_new")},
    )
    _prospect_toast(resp, f"{verb}: {result.get('name') or domain}", "success" if result.get("is_new") else "info")
    return resp


@router.get("/v2/partials/prospecting/{prospect_id}", response_class=HTMLResponse)
async def prospecting_detail_partial(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return prospect detail as HTML partial."""
    prospect = (
        db.query(ProspectAccount)
        .options(
            joinedload(ProspectAccount.claimed_by_user),
        )
        .filter(ProspectAccount.id == prospect_id)
        .first()
    )
    if not prospect:
        raise HTTPException(404, "Prospect not found")
    return template_response("htmx/partials/prospecting/detail.html", _prospect_detail_ctx(request, user, prospect))


@router.post("/v2/partials/prospecting/{prospect_id}/claim", response_class=HTMLResponse)
async def claim_prospect_htmx(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Claim a prospect.

    Enforces the site cap + ownership (in the service) and triggers background deep
    enrichment. Returns the refreshed detail or card per the call site.
    """
    from ...services.prospect_claim import claim_prospect, trigger_deep_enrichment_bg
    from ...utils.async_helpers import safe_background_task

    error = None
    result = None
    try:
        result = claim_prospect(prospect_id, user.id, db)
    except LookupError:
        raise HTTPException(404, "Prospect not found")
    except ValueError as e:
        error = str(e)

    if not error:
        await safe_background_task(trigger_deep_enrichment_bg(prospect_id), task_name="deep_enrichment_prospect")

    prospect = (
        db.query(ProspectAccount)
        .options(joinedload(ProspectAccount.claimed_by_user))
        .filter(ProspectAccount.id == prospect_id)
        .first()
    )
    if not prospect:
        raise HTTPException(404, "Prospect not found")

    # Surface the domain-collision warning (audit M1): on a domain collision claim_prospect
    # links the prospect to a DIFFERENT existing company and returns a `warning`. Without
    # this the user only saw a flat "Claimed X" and never learned their claim merged into
    # another account.
    warning = result.get("warning") if result else None
    if warning:
        message, kind = f"Claimed {prospect.name} — {warning}", "warning"
    elif error:
        message, kind = error, "error"
    else:
        message, kind = f"Claimed {prospect.name}", "success"

    form = await request.form()
    return _prospect_action_response(
        request,
        user,
        db,
        prospect,
        message=message,
        kind=kind,
        flt_status=form.get("flt_status", ""),
    )


@router.post("/v2/partials/prospecting/{prospect_id}/dismiss", response_class=HTMLResponse)
async def dismiss_prospect_htmx(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Dismiss a SUGGESTED prospect (claimed prospects use the Release action instead).

    Returns the refreshed detail or card per the call site.
    """
    from ...services.prospect_claim import dismiss_prospect

    form = await request.form()
    flt_status = form.get("flt_status", "")
    error = None
    try:
        dismiss_prospect(prospect_id, user.id, db, reason=form.get("reason") or "other")
    except LookupError:
        raise HTTPException(404, "Prospect not found")
    except ValueError as e:
        error = str(e)

    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        raise HTTPException(404, "Prospect not found")

    return _prospect_action_response(
        request,
        user,
        db,
        prospect,
        message=error or f"Dismissed {prospect.name}",
        kind="error" if error else "success",
        flt_status=flt_status,
    )


@router.post("/v2/partials/prospecting/{prospect_id}/release", response_class=HTMLResponse)
async def release_prospect_htmx(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Release a claimed prospect back to the pool: status -> SUGGESTED, clear the claim,
    and relinquish Company ownership. Only the claimer or an admin may release."""
    from ...services.prospect_claim import release_prospect

    error = None
    try:
        release_prospect(prospect_id, user.id, db, is_admin=(user.role == UserRole.ADMIN))
    except LookupError:
        raise HTTPException(404, "Prospect not found")
    except ValueError as e:
        error = str(e)

    prospect = (
        db.query(ProspectAccount)
        .options(joinedload(ProspectAccount.claimed_by_user))
        .filter(ProspectAccount.id == prospect_id)
        .first()
    )
    if not prospect:
        raise HTTPException(404, "Prospect not found")

    form = await request.form()
    return _prospect_action_response(
        request,
        user,
        db,
        prospect,
        message=error or f"Released {prospect.name} back to the pool",
        kind="error" if error else "success",
        flt_status=form.get("flt_status", ""),
    )


@router.post("/v2/partials/prospecting/{prospect_id}/enrich", response_class=HTMLResponse)
async def enrich_prospect_htmx(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Kick off enrichment in the BACKGROUND and return a status poller.

    The SAM.gov/news/warm-intro work runs off the request path (run_enrichment_job via
    safe_background_task); the detail page polls /enrich-status until it lands.
    """
    # Lock the row for the read-check-write on enrich_status (audit M7): without it two
    # near-simultaneous Enrich clicks both see "not running" and both spawn a background
    # job. FOR UPDATE serializes them so only the first flips to running + spawns.
    prospect = db.query(ProspectAccount).filter(ProspectAccount.id == prospect_id).with_for_update().first()
    if not prospect:
        raise HTTPException(404, "Prospect not found")

    ed = dict(prospect.enrichment_data or {})
    # Start a fresh job unless one is genuinely in flight — a stale 'running' left by a
    # crashed worker is restartable (otherwise Enrich/Retry would loop forever).
    if not _enrich_in_progress(ed):
        from ...services.prospect_free_enrichment import run_enrichment_job
        from ...utils.async_helpers import safe_background_task

        ed["enrich_status"] = "running"
        ed["enrich_started_at"] = datetime.now(timezone.utc).isoformat()
        prospect.enrichment_data = ed
        db.commit()
        await safe_background_task(run_enrichment_job(prospect_id), task_name="prospect_enrichment")

    return template_response(
        "htmx/partials/prospecting/enrich_status.html",
        {"request": request, "prospect": prospect, "enrich_state": "running"},
    )


@router.get("/v2/partials/prospecting/{prospect_id}/enrich-status", response_class=HTMLResponse)
async def enrich_status_partial(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Poll endpoint for background enrichment.

    HTTP 200 while running (htmx keeps polling); HTTP 286 when done/error (htmx swaps
    the final fragment and STOPS).
    """
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        # Stop the poll rather than 404 — htmx won't cancel an `every 2s` poll on a 4xx.
        return HTMLResponse("", status_code=286)

    ed = prospect.enrichment_data or {}
    state = ed.get("enrich_status") or "done"
    if state == "running" and not _enrich_in_progress(ed):
        state = "error"  # worker died mid-job (stale) — stop the poll
    resp = template_response(
        "htmx/partials/prospecting/enrich_status.html",
        {"request": request, "prospect": prospect, "enrich_state": state},
    )
    if state != "running":
        resp.status_code = 286  # htmx stop-polling status — the final fragment still swaps
        if state == "error":
            _prospect_toast(resp, "Enrichment failed — try again", "warning")
        else:
            _prospect_toast(resp, f"Enriched {prospect.name}", "success")
    return resp


# ── Reclaim endpoint ─────────────────────────────────────────────────


@router.post("/v2/partials/prospects/{prospect_id}/reclaim", response_class=HTMLResponse)
async def reclaim_prospect_htmx(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Reclaim a swept prospect: re-assign Company owner, dismiss from pool.

    Former owner, admin, or sweep manager only.
    Returns refreshed prospect card or detail with showToast trigger.
    """
    from ...services.prospect_reclamation import reclaim_prospect_account

    error = None
    result = None
    try:
        result = reclaim_prospect_account(
            prospect_id,
            user.id,
            db,
            is_admin=(user.role == UserRole.ADMIN),
        )
    except LookupError:
        raise HTTPException(404, "Prospect not found")
    except RuntimeError:
        raise HTTPException(500, "Session user record not found")
    except ValueError as e:
        error = str(e)

    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        raise HTTPException(404, "Prospect not found")

    form = await request.form()
    flt_status = form.get("flt_status", "")
    msg = error or f"Reclaimed {result['company_name']} — account re-assigned to you"
    return _prospect_action_response(
        request,
        user,
        db,
        prospect,
        message=msg,
        kind="error" if error else "success",
        flt_status=flt_status,
    )


@router.get("/v2/partials/prospects/{prospect_id}/reassign-form", response_class=HTMLResponse)
async def reassign_prospect_form(
    request: Request,
    prospect_id: int,
    ctx: str = "grid",
    flt_status: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Modal body for reassigning a swept prospect's company to another owner.

    Manager/admin only. Loaded into #modal-content by the Reassign button. ``ctx`` is
    "grid" (posts back to the in-grid card) or "detail" (posts back to #main-content) so
    the reassign response swaps the surface the action came from.
    """
    if not is_manager_or_admin(user):
        raise HTTPException(403, "Only a manager or admin can reassign an account")

    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        raise HTTPException(404, "Prospect not found")

    users = db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all()
    return template_response(
        "htmx/partials/prospecting/reassign_modal.html",
        {
            "request": request,
            "prospect": prospect,
            "users": users,
            "ctx": "detail" if ctx == "detail" else "grid",
            "flt_status": flt_status,
        },
    )


@router.post("/v2/partials/prospects/{prospect_id}/reassign", response_class=HTMLResponse)
async def reassign_prospect_htmx(
    request: Request,
    prospect_id: int,
    to_user_id: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manager/admin reassigns a swept prospect's company to another owner.

    Overrides the Phase 4 reclaim cooldown: dismisses the swept prospect, sets the new
    owner, and clears the cooldown. Manager/admin only. Returns the refreshed prospect
    card/detail with a showToast trigger.
    """
    if not is_manager_or_admin(user):
        return _prospect_error_toast("Only a manager or admin can reassign an account")

    from ...services.prospect_reclamation import reassign_account

    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        return _prospect_error_toast("Prospect not found")

    error = None
    result = None
    if not prospect.company_id:
        error = "Prospect is not linked to a company; nothing to reassign"
    else:
        try:
            result = reassign_account(prospect.company_id, to_user_id, user, db)
        except PermissionError as e:
            error = str(e)
        except LookupError:
            error = "Company not found"
        except ValueError as e:
            error = str(e)

    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        return _prospect_error_toast("Prospect not found")

    form = await request.form()
    flt_status = form.get("flt_status", "")
    msg = error or f"Reassigned {result['company_name']}"
    return _prospect_action_response(
        request,
        user,
        db,
        prospect,
        message=msg,
        kind="error" if error else "success",
        flt_status=flt_status,
    )
