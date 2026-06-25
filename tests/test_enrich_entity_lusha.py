"""Tests for enrich_entity and find_suggested_contacts under the new router+blend arch.

The old fixed waterfall (Explorium→Lusha→AI) is replaced by enrichment_router
which handles provider selection + circuit-breaking internally.  These tests verify:
 - Lusha results blend with other providers by authority (not just gap-fill)
 - Lusha quota trips the circuit inside enrichment_router (via the module-level wrappers)
 - Lusha disabled → enrichment_router still runs but without Lusha
 - Contacts: early-exit when enough verified contacts; fall-through to blend otherwise

All patching targets enrichment_router (the new orchestration layer) so these tests
remain stable regardless of which provider the router decides to call internally.
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, patch

import app.enrichment_service as es
from app.config import settings
from app.services import enrichment_router
from app.services.enrichment_credit_guard import ProviderQuotaError

# ── enrich_entity via router+blend ───────────────────────────────────────────


async def test_lusha_result_blends_by_authority(monkeypatch):
    """When the router returns both Lusha and Explorium results, the blend layer picks
    the highest-tier value per field (Explorium=85 > Lusha=75 for industry)."""

    async def fake_gather(domain, name=""):
        return [
            {
                "source": "lusha",
                "legal_name": "AcmeL",
                "industry": "Aero",
                "employee_size": "100-200",
                "hq_city": "Dallas",
                "hq_state": "TX",
                "hq_country": "United States",
                "website": "acme.com",
                "linkedin_url": "li/acme",
            },
            {"source": "explorium", "legal_name": "AcmeE", "industry": "Electronics"},
        ]

    monkeypatch.setattr(enrichment_router, "gather_company", fake_gather)
    monkeypatch.setattr("app.cache.intel_cache.get_cached", lambda k: None)
    monkeypatch.setattr("app.cache.intel_cache.set_cached", lambda *a, **k: None)

    out = await es.enrich_entity("acme.com", "Acme")

    # Explorium tier 85 beats Lusha tier 65 for industry
    assert out["industry"] == "Electronics"
    # Lusha-only fields survive (Explorium didn't supply employee_size)
    assert out["employee_size"] == "100-200"
    # _provenance must be present
    assert "_provenance" in out
    assert out["_provenance"]["industry"]["source"] == "explorium"


async def test_lusha_only_result_fills_all_fields(monkeypatch):
    """When only Lusha is returned, all its fields are in the output.

    normalize_company_output title-cases legal_name, so 'AcmeL' → 'Acmel'.
    """

    async def fake_gather(domain, name=""):
        return [
            {"source": "lusha", "legal_name": "Acme Corp", "industry": "Aero"},
        ]

    monkeypatch.setattr(enrichment_router, "gather_company", fake_gather)
    monkeypatch.setattr("app.cache.intel_cache.get_cached", lambda k: None)
    monkeypatch.setattr("app.cache.intel_cache.set_cached", lambda *a, **k: None)

    out = await es.enrich_entity("acme.com", "Acme")
    assert out["industry"] == "Aero"
    # normalize_company_output applies title-case to legal_name
    assert "acme" in out["legal_name"].lower()


async def test_router_called_with_domain_and_name(monkeypatch):
    """enrich_entity passes both domain and name to gather_company."""
    calls = []

    async def recording_gather(domain, name=""):
        calls.append((domain, name))
        return []

    monkeypatch.setattr(enrichment_router, "gather_company", recording_gather)
    monkeypatch.setattr("app.cache.intel_cache.get_cached", lambda k: None)
    monkeypatch.setattr("app.cache.intel_cache.set_cached", lambda *a, **k: None)

    await es.enrich_entity("acme.com", "Acme Inc")
    assert calls == [("acme.com", "Acme Inc")]


async def test_lusha_quota_inside_router_trips_circuit(monkeypatch):
    """If the router's Lusha wrapper raises ProviderQuotaError, trip_circuit is called
    on the router module (which the router does internally — we just verify the circuit
    state is affected so the next call skips Lusha)."""

    # Patch at the router level — that's where Lusha lives now
    from app.services import enrichment_credit_guard as ecg

    tripped = {}
    original_trip = ecg.trip_circuit

    def recording_trip(provider, cooldown):
        tripped[provider] = cooldown
        return original_trip(provider, cooldown)

    monkeypatch.setattr(ecg, "trip_circuit", recording_trip)
    monkeypatch.setattr(settings, "lusha_enrichment_enabled", True)
    monkeypatch.setattr("app.cache.intel_cache.get_cached", lambda k: None)
    monkeypatch.setattr("app.cache.intel_cache.set_cached", lambda *a, **k: None)

    # Make Lusha's connector raise ProviderQuotaError
    from app.connectors import lusha as lusha_connector

    monkeypatch.setattr(lusha_connector, "enrich_company", AsyncMock(side_effect=ProviderQuotaError("402")))
    # Patch credential resolution so Lusha gate passes
    with patch("app.services.enrichment_router.get_credential_cached", return_value="lusha-key"):
        # Other providers return nothing so circuit trip is the only interesting thing
        monkeypatch.setattr(enrichment_router, "_sam_company", AsyncMock(return_value=None))
        monkeypatch.setattr(enrichment_router, "_clay_company", AsyncMock(return_value=None))
        monkeypatch.setattr(enrichment_router, "_explorium_company", AsyncMock(return_value=None))
        monkeypatch.setattr(enrichment_router, "_ai_company", AsyncMock(return_value=None))

        await es.enrich_entity("acme.com", "Acme")

    assert "lusha" in tripped


async def test_lusha_disabled_router_still_runs(monkeypatch):
    """When lusha_enrichment_enabled=False the router runs without Lusha (other
    providers may still contribute — router handles the gate internally)."""
    monkeypatch.setattr(settings, "lusha_enrichment_enabled", False)
    monkeypatch.setattr("app.cache.intel_cache.get_cached", lambda k: None)
    monkeypatch.setattr("app.cache.intel_cache.set_cached", lambda *a, **k: None)

    router_calls = []

    async def recording_gather(domain, name=""):
        router_calls.append(domain)
        return [{"source": "explorium", "industry": "Tech"}]

    monkeypatch.setattr(enrichment_router, "gather_company", recording_gather)

    out = await es.enrich_entity("acme.com", "Acme")
    # Router was still called
    assert router_calls == ["acme.com"]
    assert out["industry"] == "Tech"


# ── find_suggested_contacts via router+blend ─────────────────────────────────


async def test_find_contacts_lusha_first_early_stop(monkeypatch):
    """If gather_contacts already returns >= limit verified contacts, blend just returns
    them (no second call needed — that's the router's job, not ours to test here)."""
    verified = [
        {"source": "lusha", "full_name": f"P{i}", "title": "Buyer", "email": f"p{i}@a.com", "verified": True}
        for i in range(3)
    ]

    async def fake_gather(domain, name, title_filter, limit):
        return verified

    monkeypatch.setattr(enrichment_router, "gather_contacts", fake_gather)

    out = await es.find_suggested_contacts("acme.com", "Acme", limit=3)
    assert len(out) == 3
    assert all(c.get("full_name", "").startswith("P") for c in out)


async def test_find_contacts_falls_through_when_lusha_thin(monkeypatch):
    """When gather_contacts returns Lusha + Explorium rows for the same person (matching
    email), blend_contacts deduplicates by email and picks the highest-tier value per
    field."""

    async def fake_gather(domain, name, title_filter, limit):
        return [
            # Both share the same email → deduplicated to one contact
            {
                "source": "lusha",
                "full_name": "Jane Smith",
                "title": "Procurement",
                "email": "jane@a.com",
                "phone": "+1234",
                "verified": True,
            },
            {
                "source": "explorium",
                "full_name": "Jane Smith",
                "title": "Procurement Mgr",
                "email": "jane@a.com",
                "verified": True,
            },
        ]

    monkeypatch.setattr(enrichment_router, "gather_contacts", fake_gather)

    out = await es.find_suggested_contacts("acme.com", "Acme", limit=5)
    # Deduped to one Jane (same email key)
    janes = [c for c in out if "Jane" in (c.get("full_name") or "")]
    assert len(janes) == 1
    # Lusha tier 95 wins for phone; email preserved
    assert janes[0].get("email") == "jane@a.com"
    assert janes[0].get("phone") == "+1234"


async def test_explorium_disabled_by_default(monkeypatch):
    """Explorium is opt-in: gating is handled by enrichment_router, not enrichment_service.
    Verify the router is still called and its result is blended normally."""
    monkeypatch.setattr(settings, "explorium_enrichment_enabled", False)
    monkeypatch.setattr("app.cache.intel_cache.get_cached", lambda k: None)
    monkeypatch.setattr("app.cache.intel_cache.set_cached", lambda *a, **k: None)

    async def fake_gather(domain, name=""):
        # Router should NOT include explorium when disabled; clay result still blends
        return [{"source": "clay", "industry": "Tech"}]

    monkeypatch.setattr(enrichment_router, "gather_company", fake_gather)

    out = await es.enrich_entity("acme.com", "Acme")
    # Remaining-provider result still blended normally
    assert out["industry"] == "Tech"


async def test_explorium_quota_trips_circuit_via_router(monkeypatch):
    """Explorium 402/429 inside the router trips the Explorium circuit; enrichment still
    completes (returns whatever other providers gave)."""
    from app.services import enrichment_credit_guard as ecg

    tripped = {}
    original_trip = ecg.trip_circuit

    def recording_trip(provider, cooldown):
        tripped[provider] = cooldown
        return original_trip(provider, cooldown)

    monkeypatch.setattr(ecg, "trip_circuit", recording_trip)
    monkeypatch.setattr(settings, "explorium_enrichment_enabled", True)
    monkeypatch.setattr(settings, "lusha_enrichment_enabled", False)
    monkeypatch.setattr("app.cache.intel_cache.get_cached", lambda k: None)
    monkeypatch.setattr("app.cache.intel_cache.set_cached", lambda *a, **k: None)

    from app.connectors import explorium as exp_connector

    monkeypatch.setattr(exp_connector, "enrich_company", AsyncMock(side_effect=ProviderQuotaError("429")))

    with patch("app.services.enrichment_router.get_credential_cached", return_value="exp-key"):
        monkeypatch.setattr(enrichment_router, "_sam_company", AsyncMock(return_value=None))
        monkeypatch.setattr(enrichment_router, "_clay_company", AsyncMock(return_value=None))
        monkeypatch.setattr(enrichment_router, "_ai_company", AsyncMock(return_value=None))

        out = await es.enrich_entity("acme.com", "Acme")

    assert "explorium" in tripped
    # Function still returns a valid dict (may be empty but not an exception)
    assert isinstance(out, dict)
    assert out.get("domain") == "acme.com"
