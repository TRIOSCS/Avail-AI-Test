"""Consolidated nav-badge counts — the ONE service call behind /v2/partials/nav/badges.

Spec §5.5 (badge consolidation): the app shell used to poll six badge endpoints every
60s. This module merges their logic into a single collect_badge_counts() call that the
alerts router renders as one HTMX OOB-swap response:

- requisitions / buy-plans / crm / my-day → AlertSource registry (count_for_tab)
- proactive → NEW ProactiveMatch count, gated by the existing
  proactive_matching_enabled flag (Wave 2 parks the workspace by flipping that
  flag — the payload keeps the key either way)
- follow-ups → follow_up_count (stays in the follow-ups router, in lockstep with
  the queue/batch predicate there)

A module hidden from the user (module_access_map — the same gate that hides its nav
item) contributes 0 without running its count queries. Every count is fail-quiet: a
badge must never break the nav, so any source that raises contributes 0.

Called by: app/routers/alerts.py (GET /v2/partials/nav/badges)
Depends on: services/alerts (registry), services/admin_service.get_effective_flag,
            routers/admin/users.module_access_map,
            routers/htmx/offers/follow_ups.follow_up_count
"""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import ProactiveMatchStatus
from ..models.auth import User
from .admin_service import get_effective_flag
from .alerts import count_for_tab

# Nav badge key → pill color, matching mobile_nav.html's {key}-nav-badge span ids and
# ordered as the spans render. The first four are AlertSource tabs (count_for_tab);
# proactive + follow-ups have their own counters below. Emerald everywhere except the
# amber follow-ups pill.
NAV_BADGE_KEYS: dict[str, str] = {
    "requisitions": "emerald",
    "buy-plans": "emerald",
    "crm": "emerald",
    "my-day": "emerald",
    "proactive": "emerald",
    "follow-ups": "amber",
}
ALERT_TAB_KEYS: tuple[str, ...] = ("requisitions", "buy-plans", "crm", "my-day")

# Badge key → the nav module whose access gates its count queries. Identity for every
# key except follow-ups, whose amber pill hangs on the Sales Hub (requisitions) item.
_BADGE_MODULE: dict[str, str] = {key: key for key in NAV_BADGE_KEYS} | {"follow-ups": "requisitions"}


def _proactive_count(db: Session, user: User) -> int:
    """NEW proactive matches for *user* — 0 when the park flag is off.

    The flag check is the park switch (spec §5.4/§8): Wave 2 turns
    proactive_matching_enabled off and this contributes 0 with no further code change.
    PROACTIVE module access is enforced upstream by collect_badge_counts' gate,
    preserving the old per-badge behavior (the badge route lived under the PROACTIVE-
    gated path prefix).
    """
    if not get_effective_flag(db, "proactive_matching_enabled", settings.proactive_matching_enabled):
        return 0
    from ..models import ProactiveMatch

    return (
        db.query(sqlfunc.count(ProactiveMatch.id))
        .filter(ProactiveMatch.salesperson_id == user.id, ProactiveMatch.status == ProactiveMatchStatus.NEW)
        .scalar()
        or 0
    )


def _follow_up_count(db: Session, user: User) -> int:
    """Stale-RFQ follow-up queue count for the amber Sales-Hub pill.

    Function-level import: follow_up_count deliberately lives beside the follow-ups
    queue/batch predicate it must stay in lockstep with (same pattern as
    routers/sightings.py's quick-link count).
    """
    from ..routers.htmx.offers.follow_ups import follow_up_count

    return follow_up_count(db, user)


def collect_badge_counts(db: Session, user: User) -> dict[str, int]:
    """Return {nav-badge-key: count} for every key in NAV_BADGE_KEYS.

    One call replaces the six per-badge endpoints. A module hidden from *user* (nav
    gate) contributes 0 without paying its count queries — its span is not rendered
    anyway. Fail-quiet per source: a failed counter logs and contributes 0, never raises
    (count_for_tab is itself fail-quiet per source; belt-and-braces here).
    """
    # Function-level import from the admin users router — same pattern as
    # routers/htmx/_shared._base_ctx, avoiding a service→router import cycle.
    from ..routers.admin.users import module_access_map

    access = module_access_map(user)
    sources: dict[str, Callable[[], int]] = {
        **{tab: (lambda t=tab: count_for_tab(db, user, t)) for tab in ALERT_TAB_KEYS},
        "proactive": lambda: _proactive_count(db, user),
        "follow-ups": lambda: _follow_up_count(db, user),
    }
    counts: dict[str, int] = {}
    for key, counter in sources.items():
        if not access.get(_BADGE_MODULE[key], True):
            counts[key] = 0
            continue
        try:
            counts[key] = counter()
        except Exception:  # noqa: BLE001 — a badge must never break the nav
            logger.exception("nav badge count failed for {}", key)
            counts[key] = 0
    return counts
