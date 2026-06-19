"""Enrichment credit guard — quota error + per-provider cooldown ("circuit").

A paid provider (Lusha today) that returns 402/429 raises ``ProviderQuotaError``; the
caller trips a short cooldown so the same quota/rate-limit isn't re-hit on every Enrich
click. The cooldown marker lives in the shared intel cache (Redis → PG fallback), so it's
process-wide — graceful fall-through alone does NOT stop repeat spend across clicks.

Called by: app/enrichment_service.py (enrich_entity, find_suggested_contacts).
Depends on: app/cache/intel_cache.py (get_cached, set_cached; TTL in days).
"""

from app.cache.intel_cache import get_cached, set_cached


class ProviderQuotaError(Exception):
    """A paid provider returned a quota/rate-limit status (402/429)."""


def _circuit_key(provider: str) -> str:
    return f"enrich:circuit:{provider}"


def circuit_open(provider: str) -> bool:
    """True while *provider* is in cooldown (a trip marker exists and hasn't
    expired)."""
    return get_cached(_circuit_key(provider)) is not None


def trip_circuit(provider: str, minutes: int) -> None:
    """Open *provider*'s cooldown for *minutes* (intel-cache TTL is in days)."""
    set_cached(_circuit_key(provider), {"tripped": 1}, ttl_days=minutes / 1440)
