"""Cross-app alert endpoints — per-tab badge + mark-seen.

GET  /v2/partials/alerts/{tab_key}/badge  → the emerald nav-badge inner HTML (or empty).
POST /v2/partials/alerts/{kind}/seen      → idempotently mark an item seen, return the
                                            owning tab's refreshed nav badge as an OOB swap.

Mirrors the Proactive badge pattern. Fail-quiet: a badge must never break the nav.

Called by: mobile_nav.html (badge poll), the shared tab-alerts frontend (seen POST).
Depends on: services/alerts (registry + record_seen) and services/alerts/sources
            (imported for its registration side effect).
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
from ..services.alerts import sources as _sources  # noqa: F401  (import = source registration)

router = APIRouter()


def _badge_html(count: int) -> str:
    """The inner emerald pill, matching the Proactive badge exactly (empty at 0)."""
    if count > 0:
        return (
            '<span class="ml-auto px-1.5 py-0.5 text-[10px] font-bold text-white '
            f'bg-emerald-500 rounded-full">{count}</span>'
        )
    return ""


@router.get("/v2/partials/alerts/{tab_key}/badge", response_class=HTMLResponse)
async def alert_badge(
    tab_key: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Emerald count badge for a nav tab (or empty string).

    Fail-quiet.
    """
    try:
        count = count_for_tab(db, user, tab_key)
    except Exception:  # noqa: BLE001 — the nav must never break
        logger.exception("alert badge failed for tab {}", tab_key)
        count = 0
    return HTMLResponse(_badge_html(count))


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
        except Exception:  # noqa: BLE001 — never 500 a cosmetic seen-ping
            logger.exception("alert seen failed for kind {} ref {}", kind, raw)

    tab_key = tab_for_kind(kind)
    if not tab_key:
        return HTMLResponse("")
    try:
        count = count_for_tab(db, user, tab_key)
    except Exception:  # noqa: BLE001 — never break the seen-ping on a badge recompute
        logger.exception("alert seen badge recompute failed for tab {}", tab_key)
        return HTMLResponse("")
    return HTMLResponse(f'<span id="{tab_key}-nav-badge" hx-swap-oob="innerHTML">{_badge_html(count)}</span>')
