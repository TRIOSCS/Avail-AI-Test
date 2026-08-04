"""Consolidated nav-badge counts — the ONE service call behind /v2/partials/nav/badges.

Spec §5.5 (badge consolidation): the app shell used to poll six badge endpoints every
60s. This module merges their logic into a single collect_badge_counts() call that the
alerts router renders as one HTMX OOB-swap response:

- requisitions / buy-plans / crm / my-day → AlertSource registry (count_for_tab)
- proactive → NEW ProactiveMatch count, gated by the existing
  proactive_matching_enabled flag + the PROACTIVE module key (Wave 2 parks the
  workspace by flipping that flag — the payload keeps the key either way)
- follow-ups → follow_up_count (stays in the follow-ups router, in lockstep with
  the queue/batch predicate there)

Every count is fail-quiet: a badge must never break the nav, so any source that
raises contributes 0.

Called by: app/routers/alerts.py (GET /v2/partials/nav/badges)
Depends on: services/alerts (registry), services/admin_service.get_effective_flag,
            dependencies.user_has_access, routers/htmx/offers/follow_ups.follow_up_count
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import AccessKey, ProactiveMatchStatus
from ..dependencies import user_has_access
from ..models.auth import User
from .admin_service import get_effective_flag
from .alerts import count_for_tab

# Nav badge keys, matching mobile_nav.html's {key}-nav-badge span ids. The first four
# are AlertSource tabs; proactive + follow-ups have their own counters below.
ALERT_TAB_KEYS: tuple[str, ...] = ("requisitions", "buy-plans", "crm", "my-day")
NAV_BADGE_KEYS: tuple[str, ...] = (*ALERT_TAB_KEYS, "proactive", "follow-ups")


def _proactive_count(db: Session, user: User) -> int:
    """NEW proactive matches for *user* — 0 when the flag or module access is off.

    The flag check is the park switch (spec §5.4/§8): Wave 2 turns
    proactive_matching_enabled off and this contributes 0 with no further code change.
    The module-key check preserves the old per-badge behavior, where the badge route
    lived under the PROACTIVE-gated path prefix.
    """
    if not get_effective_flag(db, "proactive_matching_enabled", settings.proactive_matching_enabled):
        return 0
    if not user_has_access(user, AccessKey.PROACTIVE, db):
        return 0
    from ..models import ProactiveMatch

    return (
        db.query(sqlfunc.count(ProactiveMatch.id))
        .filter(ProactiveMatch.salesperson_id == user.id, ProactiveMatch.status == ProactiveMatchStatus.NEW)
        .scalar()
        or 0
    )


def collect_badge_counts(db: Session, user: User) -> dict[str, int]:
    """Return {nav-badge-key: count} for every key in NAV_BADGE_KEYS.

    One call replaces the six per-badge endpoints. Fail-quiet per source — a failed
    counter logs and contributes 0, never raises.
    """
    counts: dict[str, int] = {}
    for tab in ALERT_TAB_KEYS:
        # count_for_tab is itself fail-quiet per source; belt-and-braces here anyway.
        try:
            counts[tab] = count_for_tab(db, user, tab)
        except Exception:  # noqa: BLE001 — a badge must never break the nav
            logger.exception("nav badge count failed for tab {}", tab)
            counts[tab] = 0

    try:
        counts["proactive"] = _proactive_count(db, user)
    except Exception:  # noqa: BLE001 — a badge must never break the nav
        logger.exception("nav badge proactive count failed")
        counts["proactive"] = 0

    try:
        # Function-level import: follow_up_count deliberately lives beside the
        # follow-ups queue/batch predicate it must stay in lockstep with (same
        # pattern as routers/sightings.py's quick-link count).
        from ..routers.htmx.offers.follow_ups import follow_up_count

        counts["follow-ups"] = follow_up_count(db, user)
    except Exception:  # noqa: BLE001 — a badge must never break the nav
        logger.exception("nav badge follow-up count failed")
        counts["follow-ups"] = 0

    return counts
