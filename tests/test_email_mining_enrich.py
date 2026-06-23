"""Tests for the email-mining enrichment hybrid (app/services/prospect_discovery_email).

Covers the Explorium→prospect adapter, the self-gating eager enrich_fn, and the hybrid
enrich_email_domains behavior (signal-always + capped eager enrichment).
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock

import pytest

from app.services.enrichment_credit_guard import ProviderQuotaError

# ── _map_explorium_to_prospect ───────────────────────────────────────────────


def test_map_explorium_to_prospect_full():
    from app.services.prospect_discovery_email import _map_explorium_to_prospect

    crm = {
        "legal_name": "Arrow Electronics",
        "industry": "Technology",
        "naics": "423690",
        "employee_size": "10,001+",
        "revenue_range": "10B+",
        "website": "https://arrow.com",
        "hq_city": "Centennial",
        "hq_state": "Colorado",
        "hq_country": "United States",
    }
    out = _map_explorium_to_prospect(crm)
    assert out["name"] == "Arrow Electronics"
    assert out["naics_code"] == "423690"
    assert out["employee_count_range"] == "10,001+"
    assert out["revenue_range"] == "10B+"
    assert out["hq_location"] == "Centennial, Colorado, United States"
    assert out["region"] == "US"
    assert out["discovery_source"] == "explorium"


def test_map_explorium_to_prospect_missing_fields():
    from app.services.prospect_discovery_email import _map_explorium_to_prospect

    out = _map_explorium_to_prospect({"legal_name": "X"})
    assert out["name"] == "X"
    assert out["industry"] is None
    assert out["naics_code"] is None
    assert out["hq_location"] is None
    assert out["region"] is None


def test_map_explorium_region_from_country_name():
    """Explorium returns a country NAME ("Germany"), not an ISO code — region must
    map."""
    from app.services.prospect_discovery_email import _map_explorium_to_prospect

    assert _map_explorium_to_prospect({"legal_name": "G", "hq_country": "Germany"})["region"] == "EU"
    assert _map_explorium_to_prospect({"legal_name": "J", "hq_country": "Japan"})["region"] == "Asia"
    assert _map_explorium_to_prospect({"legal_name": "U", "hq_country": "United States"})["region"] == "US"


# ── _explorium_domain_enrich (self-gating) ───────────────────────────────────


@pytest.mark.asyncio
async def test_domain_enrich_returns_none_when_disabled(monkeypatch):
    import app.services.prospect_discovery_email as pde
    from app.connectors import explorium

    monkeypatch.setattr(pde.settings, "explorium_enrichment_enabled", False)
    called = {"n": 0}

    async def _spy(*a, **k):
        called["n"] += 1
        return {}

    monkeypatch.setattr(explorium, "enrich_company", _spy)
    assert await pde._explorium_domain_enrich("x.com") is None
    assert called["n"] == 0  # short-circuits before any API call


@pytest.mark.asyncio
async def test_domain_enrich_returns_none_when_circuit_open(monkeypatch):
    import app.services.prospect_discovery_email as pde
    from app.services import enrichment_credit_guard as cg

    monkeypatch.setattr(pde.settings, "explorium_enrichment_enabled", True)
    monkeypatch.setattr(cg, "circuit_open", lambda p: True)
    assert await pde._explorium_domain_enrich("x.com") is None


@pytest.mark.asyncio
async def test_domain_enrich_success_maps_shape(monkeypatch):
    import app.services.prospect_discovery_email as pde
    from app.connectors import explorium
    from app.services import credential_service
    from app.services import enrichment_credit_guard as cg

    monkeypatch.setattr(pde.settings, "explorium_enrichment_enabled", True)
    monkeypatch.setattr(cg, "circuit_open", lambda p: False)
    monkeypatch.setattr(credential_service, "get_credential_cached", lambda *a: "KEY")
    monkeypatch.setattr(
        explorium,
        "enrich_company",
        AsyncMock(return_value={"legal_name": "Acme", "industry": "Mfg", "hq_country": "United States"}),
    )
    out = await pde._explorium_domain_enrich("acme.com")
    assert out is not None
    assert out["name"] == "Acme"
    assert out["region"] == "US"
    assert out["discovery_source"] == "explorium"


@pytest.mark.asyncio
async def test_domain_enrich_quota_trips_circuit_returns_none(monkeypatch):
    import app.services.prospect_discovery_email as pde
    from app.connectors import explorium
    from app.services import credential_service
    from app.services import enrichment_credit_guard as cg

    monkeypatch.setattr(pde.settings, "explorium_enrichment_enabled", True)
    monkeypatch.setattr(cg, "circuit_open", lambda p: False)
    monkeypatch.setattr(credential_service, "get_credential_cached", lambda *a: "KEY")
    tripped = {"n": 0}
    monkeypatch.setattr(cg, "trip_circuit", lambda *a, **k: tripped.__setitem__("n", tripped["n"] + 1))

    async def _quota(*a, **k):
        raise ProviderQuotaError("429")

    monkeypatch.setattr(explorium, "enrich_company", _quota)
    assert await pde._explorium_domain_enrich("x.com") is None
    assert tripped["n"] == 1


@pytest.mark.asyncio
async def test_domain_enrich_none_when_no_key(monkeypatch):
    import app.services.prospect_discovery_email as pde
    from app.services import credential_service
    from app.services import enrichment_credit_guard as cg

    monkeypatch.setattr(pde.settings, "explorium_enrichment_enabled", True)
    monkeypatch.setattr(cg, "circuit_open", lambda p: False)
    monkeypatch.setattr(credential_service, "get_credential_cached", lambda *a: None)
    assert await pde._explorium_domain_enrich("x.com") is None


# ── enrich_email_domains: hybrid (signal-always + cap) ───────────────────────


@pytest.mark.asyncio
async def test_bare_prospect_when_no_enrich_fn():
    from app.services.prospect_discovery_email import enrich_email_domains

    domains = [{"domain": "a.com", "email_count": 3, "sample_senders": [{"name": "X", "email": "x@a.com"}]}]
    results = await enrich_email_domains(domains, enrich_fn=None)
    assert len(results) == 1
    assert results[0].name == "a.com"
    assert results[0].enrichment_data["email_mining"]["email_count"] == 3
    assert results[0].discovery_source == "email_history"


@pytest.mark.asyncio
async def test_enrich_cap_limits_calls_but_creates_all_prospects():
    from app.services.prospect_discovery_email import enrich_email_domains

    domains = [{"domain": f"d{i}.com", "email_count": 10 - i, "sample_senders": []} for i in range(5)]
    calls = []

    async def fake_enrich(domain):
        calls.append(domain)
        return {"name": f"Co {domain}", "industry": "Tech"}

    results = await enrich_email_domains(domains, enrich_fn=fake_enrich, enrich_cap=2)

    # All 5 domains become prospects; only the first 2 (highest volume) were enriched.
    assert len(results) == 5
    assert calls == ["d0.com", "d1.com"]
    assert results[0].industry == "Tech"  # enriched
    assert results[0].name == "Co d0.com"
    assert results[2].industry is None  # past the cap → bare
    assert results[2].name == "d2.com"


@pytest.mark.asyncio
async def test_enrich_miss_yields_bare_prospect_within_cap():
    from app.services.prospect_discovery_email import enrich_email_domains

    domains = [{"domain": "miss.com", "email_count": 4, "sample_senders": []}]
    results = await enrich_email_domains(domains, enrich_fn=AsyncMock(return_value=None), enrich_cap=25)
    assert len(results) == 1
    assert results[0].domain == "miss.com"
    assert results[0].industry is None


@pytest.mark.asyncio
async def test_run_batch_uses_settings_cap(monkeypatch):
    import app.services.prospect_discovery_email as pde

    monkeypatch.setattr(pde.settings, "email_mining_enrich_cap", 1)
    monkeypatch.setattr(
        pde,
        "mine_unknown_domains",
        AsyncMock(
            return_value=[
                {"domain": "a.com", "email_count": 9, "sample_senders": []},
                {"domain": "b.com", "email_count": 8, "sample_senders": []},
            ]
        ),
    )
    seen_cap = {}

    async def spy_enrich_domains(domains, enrich_fn=None, enrich_cap=25):
        seen_cap["cap"] = enrich_cap
        return []

    monkeypatch.setattr(pde, "enrich_email_domains", spy_enrich_domains)
    await pde.run_email_mining_batch("b1", AsyncMock(), object(), enrich_fn=AsyncMock())
    assert seen_cap["cap"] == 1
