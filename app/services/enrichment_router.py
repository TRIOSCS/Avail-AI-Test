"""Cost-tiered, gap-gated enrichment orchestration.

Calls providers free→metered→AI, gap-gated by remaining empty firmographic fields and
guarded by the per-provider circuit breaker. Returns raw provider results; arbitration is
performed by firmo_tiers.blend_company / blend_contacts (Task 1).

Provider cost order for companies: SAM.gov (free) → Clay → Explorium → Lusha → AI.
Metered providers are only called when free providers leave gaps in _GAP_FIELDS AND the
provider's feature gate is enabled AND the circuit is closed.

Provider order for contacts: Hunter + Clay cheaply/concurrently first; then escalate to
Lusha → Explorium when verified-contact count is below *limit*.

Called by: app/enrichment_service.py (enrich_entity, find_suggested_contacts — Task 9).
Depends on: app/connectors/{sam_gov_company,clay_mcp,explorium,lusha,hunter},
            app/services/enrichment_credit_guard, app/config.settings.
"""

import asyncio
import sys

from loguru import logger

from app.config import settings
from app.connectors import clay_mcp, explorium, lusha, sam_gov_company
from app.services import enrichment_credit_guard as _cg
from app.services.credential_service import get_credential_cached

# Re-export for monkeypatching by callers and tests.
ProviderQuotaError = _cg.ProviderQuotaError


def circuit_open(provider: str) -> bool:
    """Delegating wrapper so tests can monkeypatch er.circuit_open."""
    return _cg.circuit_open(provider)


def trip_circuit(provider: str, cooldown: int) -> None:
    """Delegating wrapper so tests can monkeypatch er.trip_circuit."""
    _cg.trip_circuit(provider, cooldown)


# Fields that define a "complete" company firmographic; metered providers are only called
# when at least one of these is still missing from the accumulated results.
_GAP_FIELDS = (
    "legal_name",
    "industry",
    "employee_size",
    "hq_city",
    "hq_state",
    "hq_country",
    "website",
    "linkedin_url",
)


# ── gap detector ──────────────────────────────────────────────────────────────


def _gaps_remain(results: list[dict]) -> bool:
    """Return True if any _GAP_FIELDS field is not yet filled by any provider result."""
    filled = {k for r in results if r for k, v in r.items() if v}
    return any(f not in filled for f in _GAP_FIELDS)


# ── thin provider wrappers (named so tests can monkeypatch) ──────────────────


async def _sam_company(domain: str, name: str) -> dict | None:
    return await sam_gov_company.enrich_company(domain, name)


async def _clay_company(domain: str) -> dict | None:
    return await clay_mcp.enrich_company(domain)


async def _explorium_company(domain: str, name: str) -> dict | None:
    api_key = get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY") or ""
    return await explorium.enrich_company(domain, name, api_key)


async def _lusha_company(domain: str, name: str) -> dict | None:
    api_key = get_credential_cached("lusha_enrichment", "LUSHA_API_KEY") or ""
    return await lusha.enrich_company(domain, api_key)


async def _ai_company(domain: str, name: str) -> dict | None:
    from app.enrichment_service import _ai_find_company  # lazy — avoids import cycles

    return await _ai_find_company(domain, name)


# ── contact provider wrappers (named for monkeypatching) ─────────────────────


async def _lusha_contacts(domain: str, limit: int) -> list[dict]:
    """Fetch contacts from Lusha; resolves the API key internally."""
    api_key = get_credential_cached("lusha_enrichment", "LUSHA_API_KEY") or ""
    return await lusha.search_contacts(domain, api_key, limit)


async def _explorium_contacts(domain: str, name: str, title_filter: str, limit: int) -> list[dict]:
    """Fetch contacts from Explorium; resolves the API key internally."""
    api_key = get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY") or ""
    return await explorium.search_contacts(domain, name, api_key, title_filter, limit)


# ── cheap-contact gather (monkeypatchable for tests) ─────────────────────────


async def _gather_cheap_contacts(domain: str, title_filter: str, limit: int) -> list[dict]:
    """Gather contacts from free/cheap providers concurrently.

    Named so tests can replace it via monkeypatch.setattr(er, '_gather_cheap_contacts', ...).
    """
    _mod = sys.modules[__name__]

    # Build (provider_name, coroutine) pairs so we can attribute ProviderQuotaError
    # outcomes to the correct provider and call trip_circuit when appropriate.
    # provider_name is None for Hunter (free tier — no circuit/cooldown).
    named_tasks: list[tuple[str | None, object]] = []

    # Hunter.io — free, always-run when key exists; no credit guard → no circuit trip
    if settings.hunter_enrichment_enabled:

        async def _hunter() -> list[dict]:
            from app.enrichment_service import _hunter_find_contacts  # lazy

            return await _hunter_find_contacts(domain)

        named_tasks.append((None, _hunter()))

    # Clay — not verified, but cheap credit-wise
    if settings.clay_enrichment_enabled and not _mod.circuit_open("clay"):
        named_tasks.append(("clay", clay_mcp.find_contacts(domain, title_filter, limit, want_email=False)))

    if not named_tasks:
        return []

    provider_names = [name for name, _ in named_tasks]
    coros = [coro for _, coro in named_tasks]

    results: list[dict] = []
    for provider_name, outcome in zip(provider_names, await asyncio.gather(*coros, return_exceptions=True)):
        if isinstance(outcome, list):
            results.extend(outcome)
        elif isinstance(outcome, _cg.ProviderQuotaError):
            if provider_name is not None:
                logger.warning("{} cheap-contacts quota/rate-limit — tripping circuit", provider_name)
                _mod.trip_circuit(provider_name, settings.clay_cooldown_minutes)
            else:
                logger.warning("Cheap contacts provider quota error: {}", outcome)
        elif isinstance(outcome, BaseException):
            # Non-quota failure (timeout, transport, auth, or a bug). Don't let it vanish —
            # without this the caller just sees an empty list and reports a false
            # "no contacts found" with no trace to debug from.
            logger.warning("{} cheap-contacts failed (no circuit trip): {}", provider_name or "hunter", outcome)
    return results


# ── circuit-guarded single-result helper ─────────────────────────────────────


async def _guarded_lazy(provider: str, factory, cooldown: int, results: list) -> None:
    """Invoke *factory()* to create a coroutine; await it; append non-None result.

    The factory is only called after the circuit_open check, preventing unawaited-
    coroutine RuntimeWarnings when the circuit is open. Uses the module-level
    circuit_open/trip_circuit wrappers so tests can monkeypatch them.
    """
    _mod = sys.modules[__name__]
    if _mod.circuit_open(provider):
        return
    try:
        r = await factory()
        if r:
            results.append(r)
    except ProviderQuotaError:
        logger.warning("{} quota/rate-limit — tripping circuit", provider)
        _mod.trip_circuit(provider, cooldown)


# ── public interfaces ─────────────────────────────────────────────────────────


async def gather_company(domain: str, name: str = "") -> list[dict]:
    """Collect raw firmographic dicts from all appropriate providers.

    Free providers always run. Metered providers only run when free providers leave at
    least one _GAP_FIELDS field unfilled, the feature gate is enabled, and the circuit
    is closed. AI is last resort.

    Returns a list of raw provider dicts for firmo_tiers.blend_company to arbitrate.
    """
    _mod = sys.modules[__name__]
    results: list[dict] = []

    # FREE — always-run (guarded by feature flag only; no credit cost)
    # Use _guarded_lazy so the coroutine is only created after the circuit_open check,
    # preventing "coroutine was never awaited" RuntimeWarnings when the circuit is open.
    if settings.sam_gov_enrichment_enabled:
        await _guarded_lazy("sam_gov", lambda: _mod._sam_company(domain, name), 15, results)

    # METERED — gap-gated, ascending cost order
    # factory is called inside _guarded_lazy (after circuit_open check) to avoid creating
    # an unawaited coroutine when the circuit is open.
    metered = [
        ("clay", lambda: _mod._clay_company(domain), settings.clay_cooldown_minutes, settings.clay_enrichment_enabled),
        (
            "explorium",
            lambda: _mod._explorium_company(domain, name),
            settings.explorium_cooldown_minutes,
            settings.explorium_enrichment_enabled,
        ),
        (
            "lusha",
            lambda: _mod._lusha_company(domain, name),
            settings.lusha_cooldown_minutes,
            settings.lusha_enrichment_enabled,
        ),
    ]
    for provider, factory, cooldown, enabled in metered:
        if enabled and _gaps_remain(results):
            await _guarded_lazy(provider, factory, cooldown, results)

    # AI — last resort
    if _gaps_remain(results):
        await _guarded_lazy("ai", lambda: _mod._ai_company(domain, name), 15, results)

    return results


async def gather_contacts(
    domain: str,
    name: str,
    title_filter: str,
    limit: int,
) -> list[dict]:
    """Collect raw contact dicts; escalate to paid providers when verified count <
    limit.

    Phase 1 (cheap, concurrent): Hunter + Clay. Phase 2 (escalation, sequential): Lusha
    → Explorium when verified < limit.

    ProviderQuotaError in escalation trips the circuit and is swallowed — never
    propagates. Escalation results are extended directly into *results* (NOT via
    _ListSink).
    """
    _mod = sys.modules[__name__]

    results: list[dict] = list(await _mod._gather_cheap_contacts(domain, title_filter, limit))

    verified_n = sum(1 for c in results if c.get("verified"))
    if verified_n >= limit:
        return results

    # ESCALATION — paid / verified providers. Direct try/except so results.extend() works
    # (avoids the _ListSink hack from the brief where a new list would lose escalation data).

    # Lusha
    if settings.lusha_enrichment_enabled and not _mod.circuit_open("lusha"):
        try:
            contacts = await _mod._lusha_contacts(domain, limit)
            results.extend(contacts)
        except ProviderQuotaError:
            logger.warning("Lusha contacts quota/rate-limit — tripping circuit")
            _mod.trip_circuit("lusha", settings.lusha_cooldown_minutes)

    # Re-check verified count after Lusha before spending on Explorium
    verified_n = sum(1 for c in results if c.get("verified"))
    if verified_n >= limit:
        return results

    # Explorium
    if settings.explorium_enrichment_enabled and not _mod.circuit_open("explorium"):
        try:
            contacts = await _mod._explorium_contacts(domain, name, title_filter, limit)
            results.extend(contacts)
        except ProviderQuotaError:
            logger.warning("Explorium contacts quota/rate-limit — tripping circuit")
            _mod.trip_circuit("explorium", settings.explorium_cooldown_minutes)

    return results
