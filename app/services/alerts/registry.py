"""Alert-source registry — maps each nav tab key to its AlertSource list.

A tab's badge count is the SUM of its sources' counts (a tab may carry more than
one source, e.g. Sales Hub = confirmed offers + vendor inbound). Concrete sources
register themselves at import time via :func:`register`; the routers import the
sources package, which wires everything up.

Called by: badge/seen routers.
Depends on: services/alerts/base.py.
"""

from __future__ import annotations

from collections import defaultdict

from loguru import logger
from sqlalchemy.orm import Session

from app.models.auth import User

from .base import AlertSource

_BY_TAB: dict[str, list[AlertSource]] = defaultdict(list)
_BY_KIND: dict[str, AlertSource] = {}


def register(tab_key: str, source: AlertSource) -> None:
    """Register ``source`` under a nav ``tab_key`` (idempotent per kind)."""
    if source.kind in _BY_KIND:
        # Re-registration (e.g. a test re-import) — replace in place, don't duplicate.
        _BY_TAB[tab_key] = [s for s in _BY_TAB[tab_key] if s.kind != source.kind]
    _BY_TAB[tab_key].append(source)
    _BY_KIND[source.kind] = source


def sources_for_tab(tab_key: str) -> list[AlertSource]:
    return list(_BY_TAB.get(tab_key, []))


def source_for_kind(kind: str) -> AlertSource | None:
    return _BY_KIND.get(kind)


def count_for_tab(db: Session, user: User, tab_key: str) -> int:
    """Sum of this tab's sources' counts for ``user``.

    Fail-quiet per source: a badge must never break the nav, so a source that raises
    is logged and contributes 0.
    """
    total = 0
    for source in sources_for_tab(tab_key):
        try:
            total += source.count_for_user(db, user)
        except Exception:  # noqa: BLE001 — a badge must never break the nav
            logger.exception("alert source {} count failed", source.kind)
    return total
