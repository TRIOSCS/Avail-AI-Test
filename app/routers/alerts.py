"""Cross-app alert endpoints — consolidated nav badges + mark-seen.

GET  /v2/partials/nav/badges          → ALL six nav badge pills in one response, each
                                        as an hx-swap-oob="innerHTML" span (spec §5.5 —
                                        replaces the six per-badge pollers).
POST /v2/partials/alerts/{kind}/seen  → idempotently mark an item seen, return the
                                        owning tab's refreshed nav badge as an OOB swap.

Fail-quiet: a badge must never break the nav.

Called by: mobile_nav.html (single badge poller), the shared tab-alerts frontend
           (seen POST).
Depends on: services/nav_badges (consolidated counts), services/alerts
            (record_seen + registry) and services/alerts/sources (imported for its
            registration side effect).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..models.auth import User
from ..services.alerts import count_for_tab, record_seen, tab_for_kind
from ..services.alerts import sources as _sources  # noqa: F401
from ..services.nav_badges import NAV_BADGE_KEYS, collect_badge_counts

router = APIRouter()


def _badge_html(count: int, color: str = "emerald") -> str:
    """The inner count pill (empty at 0).

    Emerald everywhere except the amber follow-ups.
    """
    if count > 0:
        return (
            '<span class="ml-auto px-1.5 py-0.5 text-[10px] font-bold text-white '
            f'bg-{color}-500 rounded-full">{count}</span>'
        )
    return ""


@router.get("/v2/partials/nav/badges", response_class=HTMLResponse)
async def nav_badges(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Every nav badge pill in ONE response (spec §5.5).

    Each span is an hx-swap-oob="innerHTML" fragment targeting its {key}-nav-badge
    element; the nav's single hidden poller requests this with hx-swap="none" so only
    the OOB spans apply. Spans whose target is not rendered (module hidden for this
    user) are simply dropped by htmx. Zero counts render an EMPTY span so a stale pill
    clears when its count drops. Fail-quiet.
    """
    try:
        counts = collect_badge_counts(db, user)
    except Exception:
        logger.exception("nav badge collection failed")
        counts = {}
    parts = []
    for key in NAV_BADGE_KEYS:
        color = "amber" if key == "follow-ups" else "emerald"
        parts.append(
            f'<span id="{key}-nav-badge" hx-swap-oob="innerHTML">{_badge_html(counts.get(key, 0), color)}</span>'
        )
    return HTMLResponse("".join(parts))


@router.post("/v2/partials/alerts/{kind}/seen", response_class=HTMLResponse)
async def alert_seen(
    kind: str,
    ref_ids: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Mark one or more ``(kind, ref_id)`` seen for the current user (own rows only).

    ``ref_ids`` is comma-separated so one row's whole batch of refs is a single request.
    Returns the owning tab's refreshed nav badge as an OOB swap so it updates instantly;
    the in-tab pill is decremented client-side. Idempotent + fully fail-quiet (a cosmetic
    seen-ping must never 500).
    """
    for raw in ref_ids.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            record_seen(db, user, kind, int(raw))
        except Exception:
            logger.exception("alert seen failed for kind {} ref {}", kind, raw)

    tab_key = tab_for_kind(kind)
    if not tab_key:
        return HTMLResponse("")
    try:
        count = count_for_tab(db, user, tab_key)
    except Exception:
        logger.exception("alert seen badge recompute failed for tab {}", tab_key)
        return HTMLResponse("")
    return HTMLResponse(f'<span id="{tab_key}-nav-badge" hx-swap-oob="innerHTML">{_badge_html(count)}</span>')
