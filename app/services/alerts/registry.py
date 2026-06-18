"""Alert-source registry — maps each nav tab key to its AlertSource list.

A tab's badge count is the SUM of its sources' counts (a tab may carry more than one
source, e.g. Sales Hub = confirmed offers + vendor inbound). Concrete sources are
registered centrally in services/alerts/sources/__init__.py.

Called by: the badge/seen routers, the list partials (markers_for_tab).
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
_TAB_BY_KIND: dict[str, str] = {}


def register(tab_key: str, source: AlertSource) -> None:
    """Register ``source`` under a nav ``tab_key`` (idempotent per kind)."""
    old_tab = _TAB_BY_KIND.get(source.kind)
    if old_tab is not None:
        # Re-registration (e.g. a test re-import) — drop the prior copy, don't duplicate.
        _BY_TAB[old_tab] = [s for s in _BY_TAB[old_tab] if s.kind != source.kind]
    _BY_TAB[tab_key].append(source)
    _BY_KIND[source.kind] = source
    _TAB_BY_KIND[source.kind] = tab_key


def sources_for_tab(tab_key: str) -> list[AlertSource]:
    return list(_BY_TAB.get(tab_key, []))


def source_for_kind(kind: str) -> AlertSource | None:
    return _BY_KIND.get(kind)


def tab_for_kind(kind: str) -> str | None:
    """The nav tab a given alert kind belongs to (used to refresh its badge)."""
    return _TAB_BY_KIND.get(kind)


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


def markers_for_tab(db: Session, user: User, tab_key: str) -> dict[str, dict]:
    """Spotlight markers for a tab's list, grouped by row anchor.

    Returns ``{anchor: {"kind": str, "temperament": str, "refs": [int, ...]}}`` — the
    list partial stamps each matching row with data-alert-* attributes so the shared
    frontend can glide to / rail / observe it. Fail-quiet per source.
    """
    markers: dict[str, dict] = {}
    for source in sources_for_tab(tab_key):
        try:
            items = source.new_items_for_user(db, user)
        except Exception:  # noqa: BLE001 — a spotlight must never break the page
            logger.exception("alert source {} new_items failed", source.kind)
            continue
        for item in items:
            if not item.anchor:
                continue
            entry = markers.setdefault(
                item.anchor,
                {"kind": str(source.kind), "temperament": str(source.temperament), "refs": []},
            )
            entry["refs"].append(item.ref_id)
    return markers
