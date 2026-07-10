"""Cross-app alert framework — reusable AlertSource primitive + registry.

The alert layer behind the per-tab green nav badges and the in-tab fluid spotlight.
"""

# Importing the sources package registers every concrete AlertSource against its nav tab
# (a register() side effect). Done here so the registry is populated for ANY importer of
# this package — the badge/seen router AND the list partials that call markers_for_tab —
# rather than relying on the alerts router happening to be imported first.
from . import sources  # noqa: F401
from .base import AlertItem, AlertSource, Temperament, recency_floor, record_seen
from .registry import (
    count_for_tab,
    markers_for_tab,
    register,
    source_for_kind,
    sources_for_tab,
    tab_for_kind,
)

__all__ = [
    "AlertItem",
    "AlertSource",
    "Temperament",
    "recency_floor",
    "record_seen",
    "count_for_tab",
    "markers_for_tab",
    "register",
    "source_for_kind",
    "sources_for_tab",
    "tab_for_kind",
]
