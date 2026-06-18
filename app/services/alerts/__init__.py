"""Cross-app alert framework — reusable AlertSource primitive + registry.

The alert layer behind the per-tab green nav badges and the in-tab fluid spotlight.
See docs/superpowers/specs/2026-06-18-comm-ledger-alerts-design.md.
"""

from .base import AlertItem, AlertSource, Temperament, recency_floor, record_seen
from .registry import count_for_tab, register, source_for_kind, sources_for_tab

__all__ = [
    "AlertItem",
    "AlertSource",
    "Temperament",
    "recency_floor",
    "record_seen",
    "count_for_tab",
    "register",
    "source_for_kind",
    "sources_for_tab",
]
