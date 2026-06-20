"""SP1: Lusha in enrich_entity (gap-fill + gap-gated Apollo) and find_suggested_contacts.

All providers are patched at the enrichment_service module. Lusha gated off by default →
CRM regression behavior is unchanged (covered by existing tests); these tests force it on.
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock

import app.enrichment_service as es
from app.config import settings
from app.services.enrichment_credit_guard import ProviderQuotaError


def _enable_lusha(monkeypatch):
    monkeypatch.setattr(settings, "lusha_enrichment_enabled", True)
    monkeypatch.setattr(es, "get_credential_cached", lambda src, var: "lusha-key")
    monkeypatch.setattr(es, "circuit_open", lambda provider: False)


async def test_lusha_fills_gaps_and_gap_gates_apollo(monkeypatch):
    _enable_lusha(monkeypatch)
    monkeypatch.setattr(settings, "explorium_enrichment_enabled", True)  # Explorium is opt-in now
    monkeypatch.setattr("app.cache.intel_cache.get_cached", lambda key: None)
    monkeypatch.setattr("app.cache.intel_cache.set_cached", lambda *a, **k: None)
    monkeypatch.setattr(
        es, "_explorium_find_company", AsyncMock(return_value={"source": "explorium", "legal_name": "Acme"})
    )
    full = {
        "source": "lusha",
        "legal_name": "AcmeL",
        "industry": "Aero",
        "employee_size": "100-200",
        "hq_city": "Dallas",
        "hq_state": "TX",
        "hq_country": "United States",
        "website": "acme.com",
        "linkedin_url": "li/acme",
    }
    monkeypatch.setattr(es.lusha, "enrich_company", AsyncMock(return_value=full))
    apollo_mock = AsyncMock(return_value={"source": "apollo", "legal_name": "AcmeA"})
    monkeypatch.setattr("app.connectors.apollo.search_company", apollo_mock)
    monkeypatch.setattr(settings, "apollo_api_key", "apollo-key")
    monkeypatch.setattr(es, "_ai_find_company", AsyncMock(return_value=None))

    out = await es.enrich_entity("acme.com", "Acme")
    assert out["industry"] == "Aero"  # filled by Lusha (not clobbered)
    assert out["legal_name"] == "Acme"  # Explorium won (fill-only)
    apollo_mock.assert_not_called()  # no gaps remain → Apollo skipped (gap-gate)


async def test_lusha_quota_trips_circuit(monkeypatch):
    _enable_lusha(monkeypatch)
    monkeypatch.setattr("app.cache.intel_cache.get_cached", lambda key: None)
    monkeypatch.setattr("app.cache.intel_cache.set_cached", lambda *a, **k: None)
    monkeypatch.setattr(es, "_explorium_find_company", AsyncMock(return_value=None))
    monkeypatch.setattr(es.lusha, "enrich_company", AsyncMock(side_effect=ProviderQuotaError("402")))
    tripped = {}
    monkeypatch.setattr(es, "trip_circuit", lambda p, m: tripped.update(provider=p, minutes=m))
    monkeypatch.setattr(settings, "apollo_api_key", "")
    monkeypatch.setattr(es, "_ai_find_company", AsyncMock(return_value=None))

    await es.enrich_entity("acme.com", "Acme")
    assert tripped["provider"] == "lusha"
    assert tripped["minutes"] == settings.lusha_cooldown_minutes


async def test_lusha_disabled_chain_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "lusha_enrichment_enabled", False)
    monkeypatch.setattr("app.cache.intel_cache.get_cached", lambda key: None)
    monkeypatch.setattr("app.cache.intel_cache.set_cached", lambda *a, **k: None)
    monkeypatch.setattr(
        es, "_explorium_find_company", AsyncMock(return_value={"source": "explorium", "legal_name": "Acme"})
    )
    lusha_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(es.lusha, "enrich_company", lusha_mock)
    monkeypatch.setattr(settings, "apollo_api_key", "")
    monkeypatch.setattr(es, "_ai_find_company", AsyncMock(return_value=None))

    await es.enrich_entity("acme.com", "Acme")
    lusha_mock.assert_not_called()  # disabled → never called


async def test_find_contacts_lusha_first_early_stop(monkeypatch):
    _enable_lusha(monkeypatch)
    verified = [
        {"source": "lusha", "full_name": f"P{i}", "title": "Buyer", "email": f"p{i}@a.com", "verified": True}
        for i in range(3)
    ]
    monkeypatch.setattr(es.lusha, "search_contacts", AsyncMock(return_value=verified))
    expl = AsyncMock(return_value=[])
    ai = AsyncMock(return_value=[])
    monkeypatch.setattr(es, "_explorium_find_contacts", expl)
    monkeypatch.setattr(es, "_ai_find_contacts", ai)

    out = await es.find_suggested_contacts("acme.com", "Acme", limit=3)
    assert len(out) == 3
    expl.assert_not_called()  # ≥limit verified → existing gather (providers) skipped
    ai.assert_not_called()


async def test_find_contacts_falls_through_when_lusha_thin(monkeypatch):
    _enable_lusha(monkeypatch)
    monkeypatch.setattr(settings, "explorium_enrichment_enabled", True)  # Explorium is opt-in now
    monkeypatch.setattr(es.lusha, "search_contacts", AsyncMock(return_value=[]))  # nothing from Lusha
    monkeypatch.setattr(
        es,
        "_explorium_find_contacts",
        AsyncMock(
            return_value=[{"source": "explorium", "full_name": "Jane", "title": "Procurement", "email": "jane@a.com"}]
        ),
    )
    monkeypatch.setattr(es, "_ai_find_contacts", AsyncMock(return_value=[]))

    out = await es.find_suggested_contacts("acme.com", "Acme", limit=5)
    assert any(c["full_name"] == "Jane" for c in out)  # fallback merged + relevance-filtered


async def test_explorium_disabled_by_default(monkeypatch):
    """Explorium is opt-in: off by default → never called even with a key."""
    monkeypatch.setattr(settings, "explorium_enrichment_enabled", False)
    monkeypatch.setattr(settings, "lusha_enrichment_enabled", False)
    monkeypatch.setattr(es, "get_credential_cached", lambda src, var: "some-key")
    monkeypatch.setattr("app.cache.intel_cache.get_cached", lambda key: None)
    monkeypatch.setattr("app.cache.intel_cache.set_cached", lambda *a, **k: None)
    expl = AsyncMock(return_value={"source": "explorium", "legal_name": "Acme"})
    monkeypatch.setattr(es, "_explorium_find_company", expl)
    monkeypatch.setattr(settings, "apollo_api_key", "")
    monkeypatch.setattr(es, "_ai_find_company", AsyncMock(return_value=None))

    await es.enrich_entity("acme.com", "Acme")
    expl.assert_not_called()


async def test_explorium_quota_trips_circuit(monkeypatch):
    """When enabled and Explorium returns 402/429, its circuit trips."""
    monkeypatch.setattr(settings, "explorium_enrichment_enabled", True)
    monkeypatch.setattr(settings, "lusha_enrichment_enabled", False)
    monkeypatch.setattr(es, "get_credential_cached", lambda src, var: "exp-key")
    monkeypatch.setattr(es, "circuit_open", lambda provider: False)
    monkeypatch.setattr("app.cache.intel_cache.get_cached", lambda key: None)
    monkeypatch.setattr("app.cache.intel_cache.set_cached", lambda *a, **k: None)
    monkeypatch.setattr(es, "_explorium_find_company", AsyncMock(side_effect=ProviderQuotaError("429")))
    tripped = {}
    monkeypatch.setattr(es, "trip_circuit", lambda p, m: tripped.update(provider=p, minutes=m))
    monkeypatch.setattr(settings, "apollo_api_key", "")
    monkeypatch.setattr(es, "_ai_find_company", AsyncMock(return_value=None))

    await es.enrich_entity("acme.com", "Acme")
    assert tripped["provider"] == "explorium"
    assert tripped["minutes"] == settings.explorium_cooldown_minutes
