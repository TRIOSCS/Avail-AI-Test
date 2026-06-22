"""Tests for app/services/enrichment_router.py and app/connectors/sam_gov_company.py.

Verifies:
- Free providers (SAM.gov + Apollo) always run first.
- Metered providers (Clay, Explorium, Lusha) are skipped when free providers fill all
  _GAP_FIELDS (gap-gating).
- Metered providers run in cost order when gaps remain.
- circuit_open blocks a provider without calling it.
- ProviderQuotaError trips the circuit and is swallowed (never propagates out of gather_*).
- gather_contacts runs cheap providers concurrently; escalates to Lusha/Explorium when
  verified contacts < limit; escalation results actually land in the output list.
"""

import os

os.environ["TESTING"] = "1"

import pytest

from app.services import enrichment_router as er

# ── helpers ───────────────────────────────────────────────────────────────────


def _full_apollo_result():
    """Apollo payload that fills every _GAP_FIELDS entry."""
    return {
        "source": "apollo",
        "legal_name": "Arrow Inc",
        "industry": "Wholesale",
        "employee_size": "10001+",
        "hq_city": "X",
        "hq_state": "Y",
        "hq_country": "US",
        "website": "arrow.com",
        "linkedin_url": "li",
        "domain": "arrow.com",
    }


# ── gather_company: ordering + gap-gating ─────────────────────────────────────


@pytest.mark.asyncio
async def test_company_order_free_then_metered_and_gap_gates(monkeypatch):
    """SAM then Apollo run; all gaps filled → Clay/Explorium/Lusha/AI skipped."""
    calls = []

    async def sam(d, n):
        calls.append("sam")
        return {"source": "sam_gov", "legal_name": "Arrow Inc"}

    async def apollo(d, n):
        calls.append("apollo")
        return _full_apollo_result()

    async def clay(d):
        calls.append("clay")
        return None

    async def expl(d, n):
        calls.append("explorium")
        return None

    async def ai(d, n):
        calls.append("ai")
        return None

    monkeypatch.setattr(er, "_sam_company", sam)
    monkeypatch.setattr(er, "_apollo_company", apollo)
    monkeypatch.setattr(er, "_clay_company", clay)
    monkeypatch.setattr(er, "_explorium_company", expl)
    monkeypatch.setattr(er, "_lusha_company", ai)  # reuse no-op
    monkeypatch.setattr(er, "_ai_company", ai)
    monkeypatch.setattr(er.settings, "sam_gov_enrichment_enabled", True)
    monkeypatch.setattr(er.settings, "clay_enrichment_enabled", True)
    monkeypatch.setattr(er.settings, "explorium_enrichment_enabled", True)
    monkeypatch.setattr(er.settings, "lusha_enrichment_enabled", True)
    monkeypatch.setattr(er, "circuit_open", lambda p: False)

    results = await er.gather_company("arrow.com", "Arrow")

    assert calls[0] == "sam"
    assert calls[1] == "apollo"
    # metered providers gap-gated out
    assert "explorium" not in calls
    assert "clay" not in calls
    assert "ai" not in calls
    assert len(results) == 2


@pytest.mark.asyncio
async def test_company_gaps_trigger_metered_providers(monkeypatch):
    """When SAM+Apollo leave gaps, Clay should be called."""
    calls = []

    async def sam(d, n):
        calls.append("sam")
        # Only fills legal_name — many gaps remain
        return {"source": "sam_gov", "legal_name": "Arrow Inc"}

    async def apollo(d, n):
        calls.append("apollo")
        return None

    async def clay(d):
        calls.append("clay")
        return {"source": "clay", "industry": "Technology"}

    async def expl(d, n):
        calls.append("explorium")
        return None

    async def lusha(d, n):
        calls.append("lusha")
        return None

    async def ai(d, n):
        calls.append("ai")
        return None

    monkeypatch.setattr(er, "_sam_company", sam)
    monkeypatch.setattr(er, "_apollo_company", apollo)
    monkeypatch.setattr(er, "_clay_company", clay)
    monkeypatch.setattr(er, "_explorium_company", expl)
    monkeypatch.setattr(er, "_lusha_company", lusha)
    monkeypatch.setattr(er, "_ai_company", ai)

    # Enable all gates
    monkeypatch.setattr(er.settings, "sam_gov_enrichment_enabled", True)
    monkeypatch.setattr(er.settings, "clay_enrichment_enabled", True)
    monkeypatch.setattr(er.settings, "explorium_enrichment_enabled", True)
    monkeypatch.setattr(er.settings, "lusha_enrichment_enabled", True)
    monkeypatch.setattr(er, "circuit_open", lambda p: False)

    results = await er.gather_company("arrow.com", "Arrow")

    assert "sam" in calls
    assert "apollo" in calls
    assert "clay" in calls
    # Clay returned a result; still many gaps → explorium also runs
    assert "explorium" in calls


@pytest.mark.asyncio
async def test_company_circuit_open_skips_provider(monkeypatch):
    """A provider with an open circuit is never called."""
    calls = []

    async def sam(d, n):
        calls.append("sam")
        return None

    async def apollo(d, n):
        calls.append("apollo")
        return None

    async def clay(d):
        calls.append("clay")
        return None

    async def noop(d, n):
        return None

    monkeypatch.setattr(er, "_sam_company", sam)
    monkeypatch.setattr(er, "_apollo_company", apollo)
    monkeypatch.setattr(er, "_clay_company", clay)
    monkeypatch.setattr(er, "_explorium_company", noop)
    monkeypatch.setattr(er, "_lusha_company", noop)
    monkeypatch.setattr(er, "_ai_company", noop)

    monkeypatch.setattr(er.settings, "sam_gov_enrichment_enabled", True)
    monkeypatch.setattr(er.settings, "clay_enrichment_enabled", True)
    monkeypatch.setattr(er.settings, "explorium_enrichment_enabled", False)
    monkeypatch.setattr(er.settings, "lusha_enrichment_enabled", False)
    # Clay circuit is open
    monkeypatch.setattr(er, "circuit_open", lambda p: p == "clay")

    await er.gather_company("example.com", "Example")

    assert "clay" not in calls


@pytest.mark.asyncio
async def test_company_quota_error_trips_circuit_and_is_swallowed(monkeypatch):
    """ProviderQuotaError from a provider trips the circuit and does not propagate."""
    tripped: list[str] = []

    async def sam(d, n):
        return None

    async def apollo_bad(d, n):
        raise er.ProviderQuotaError("Apollo quota hit")

    async def noop(d, n):
        return None

    async def noop_clay(d):
        return None

    monkeypatch.setattr(er, "_sam_company", sam)
    monkeypatch.setattr(er, "_apollo_company", apollo_bad)
    monkeypatch.setattr(er, "_clay_company", noop_clay)
    monkeypatch.setattr(er, "_explorium_company", noop)
    monkeypatch.setattr(er, "_lusha_company", noop)
    monkeypatch.setattr(er, "_ai_company", noop)
    monkeypatch.setattr(er, "trip_circuit", lambda p, m: tripped.append(p))
    monkeypatch.setattr(er, "circuit_open", lambda p: False)
    monkeypatch.setattr(er.settings, "sam_gov_enrichment_enabled", False)
    monkeypatch.setattr(er.settings, "clay_enrichment_enabled", False)
    monkeypatch.setattr(er.settings, "explorium_enrichment_enabled", False)
    monkeypatch.setattr(er.settings, "lusha_enrichment_enabled", False)

    # Must not raise
    results = await er.gather_company("example.com", "Example")

    assert "apollo" in tripped
    assert isinstance(results, list)


@pytest.mark.asyncio
async def test_company_no_sam_when_feature_disabled(monkeypatch):
    """SAM.gov is skipped when sam_gov_enrichment_enabled=False."""
    calls = []

    async def sam(d, n):
        calls.append("sam")
        return None

    async def apollo(d, n):
        calls.append("apollo")
        return None

    async def noop(d, n):
        return None

    async def noop_clay(d):
        return None

    monkeypatch.setattr(er, "_sam_company", sam)
    monkeypatch.setattr(er, "_apollo_company", apollo)
    monkeypatch.setattr(er, "_clay_company", noop_clay)
    monkeypatch.setattr(er, "_explorium_company", noop)
    monkeypatch.setattr(er, "_lusha_company", noop)
    monkeypatch.setattr(er, "_ai_company", noop)
    monkeypatch.setattr(er.settings, "sam_gov_enrichment_enabled", False)
    monkeypatch.setattr(er.settings, "clay_enrichment_enabled", False)
    monkeypatch.setattr(er.settings, "explorium_enrichment_enabled", False)
    monkeypatch.setattr(er.settings, "lusha_enrichment_enabled", False)
    monkeypatch.setattr(er, "circuit_open", lambda p: False)

    await er.gather_company("example.com", "Example")

    assert "sam" not in calls
    assert "apollo" in calls


# ── gather_contacts: escalation results land in output ───────────────────────


@pytest.mark.asyncio
async def test_contacts_escalation_results_in_output(monkeypatch):
    """Lusha contacts returned by escalation actually appear in gather_contacts
    output."""
    lusha_contacts = [
        {"source": "lusha", "full_name": "Jane Buyer", "email": "jane@example.com", "verified": True},
        {"source": "lusha", "full_name": "Bob Buyer", "email": "bob@example.com", "verified": True},
    ]

    # Cheap providers return nothing
    async def cheap(domain, title_filter, limit):
        return []

    async def fake_lusha(d, lim):
        return lusha_contacts

    async def fake_explorium(d, n, tf, lim):
        return []

    monkeypatch.setattr(er, "_gather_cheap_contacts", cheap)
    monkeypatch.setattr(er, "_lusha_contacts", fake_lusha)
    monkeypatch.setattr(er, "_explorium_contacts", fake_explorium)
    monkeypatch.setattr(er.settings, "lusha_enrichment_enabled", True)
    monkeypatch.setattr(er.settings, "explorium_enrichment_enabled", False)
    monkeypatch.setattr(er, "circuit_open", lambda p: False)
    monkeypatch.setattr(er, "trip_circuit", lambda p, m: None)

    results = await er.gather_contacts("example.com", "Example", "", 2)

    full_names = [r["full_name"] for r in results]
    assert "Jane Buyer" in full_names
    assert "Bob Buyer" in full_names


@pytest.mark.asyncio
async def test_contacts_explorium_escalation_results_in_output(monkeypatch):
    """Explorium contacts from escalation appear in results."""
    explorium_contacts = [
        {"source": "explorium", "full_name": "Alice Mgr", "email": "alice@x.com", "verified": True},
    ]

    async def cheap(domain, title_filter, limit):
        return []

    async def fake_lusha(d, lim):
        return []

    async def fake_explorium(d, n, tf, lim):
        return explorium_contacts

    monkeypatch.setattr(er, "_gather_cheap_contacts", cheap)
    monkeypatch.setattr(er, "_lusha_contacts", fake_lusha)
    monkeypatch.setattr(er, "_explorium_contacts", fake_explorium)
    monkeypatch.setattr(er.settings, "lusha_enrichment_enabled", True)
    monkeypatch.setattr(er.settings, "explorium_enrichment_enabled", True)
    monkeypatch.setattr(er, "circuit_open", lambda p: False)
    monkeypatch.setattr(er, "trip_circuit", lambda p, m: None)

    results = await er.gather_contacts("example.com", "Example", "manager", 1)

    assert any(r["full_name"] == "Alice Mgr" for r in results)


@pytest.mark.asyncio
async def test_contacts_no_escalation_when_verified_sufficient(monkeypatch):
    """Escalation is skipped when cheap providers already return >= limit verified
    contacts."""
    cheap_contacts = [
        {"source": "apollo", "full_name": "Person A", "email": "a@ex.com", "verified": True},
        {"source": "apollo", "full_name": "Person B", "email": "b@ex.com", "verified": True},
    ]
    lusha_called = []

    async def cheap(domain, title_filter, limit):
        return cheap_contacts

    async def fake_lusha(d, lim):
        lusha_called.append(True)
        return []

    async def fake_explorium(d, n, tf, lim):
        return []

    monkeypatch.setattr(er, "_gather_cheap_contacts", cheap)
    monkeypatch.setattr(er, "_lusha_contacts", fake_lusha)
    monkeypatch.setattr(er, "_explorium_contacts", fake_explorium)
    monkeypatch.setattr(er.settings, "lusha_enrichment_enabled", True)
    monkeypatch.setattr(er.settings, "explorium_enrichment_enabled", True)
    monkeypatch.setattr(er, "circuit_open", lambda p: False)

    results = await er.gather_contacts("example.com", "Example", "", 2)

    assert not lusha_called
    assert len(results) == 2


@pytest.mark.asyncio
async def test_contacts_quota_error_trips_circuit_and_is_swallowed(monkeypatch):
    """ProviderQuotaError during contacts escalation trips circuit and doesn't
    propagate."""
    tripped: list[str] = []

    async def cheap(domain, title_filter, limit):
        return []

    async def bad_lusha(d, lim):
        raise er.ProviderQuotaError("Lusha quota")

    async def fake_explorium(d, n, tf, lim):
        return []

    monkeypatch.setattr(er, "_gather_cheap_contacts", cheap)
    monkeypatch.setattr(er, "_lusha_contacts", bad_lusha)
    monkeypatch.setattr(er, "_explorium_contacts", fake_explorium)
    monkeypatch.setattr(er.settings, "lusha_enrichment_enabled", True)
    monkeypatch.setattr(er.settings, "explorium_enrichment_enabled", False)
    monkeypatch.setattr(er, "circuit_open", lambda p: False)
    monkeypatch.setattr(er, "trip_circuit", lambda p, m: tripped.append(p))

    results = await er.gather_contacts("example.com", "Example", "", 5)

    assert "lusha" in tripped
    assert isinstance(results, list)


@pytest.mark.asyncio
async def test_contacts_escalation_circuit_open_skips_provider(monkeypatch):
    """A provider with circuit open is skipped during contacts escalation."""
    lusha_called = []

    async def cheap(domain, title_filter, limit):
        return []

    async def fake_lusha(d, lim):
        lusha_called.append(True)
        return []

    async def fake_explorium(d, n, tf, lim):
        return []

    monkeypatch.setattr(er, "_gather_cheap_contacts", cheap)
    monkeypatch.setattr(er, "_lusha_contacts", fake_lusha)
    monkeypatch.setattr(er, "_explorium_contacts", fake_explorium)
    monkeypatch.setattr(er.settings, "lusha_enrichment_enabled", True)
    monkeypatch.setattr(er.settings, "explorium_enrichment_enabled", False)
    # Lusha circuit is open
    monkeypatch.setattr(er, "circuit_open", lambda p: p == "lusha")

    await er.gather_contacts("example.com", "Example", "", 5)

    assert not lusha_called


# ── sam_gov_company adapter ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sam_company_returns_none_without_name():
    """SAM.gov adapter returns None immediately if name is empty."""
    from app.connectors import sam_gov_company

    result = await sam_gov_company.enrich_company("example.com", "")
    assert result is None


@pytest.mark.asyncio
async def test_sam_company_maps_fields(monkeypatch):
    """SAM.gov adapter maps entity fields to the shared firmographic shape."""
    import httpx

    from app.connectors import sam_gov_company

    fake_resp = httpx.Response(
        200,
        json={
            "entityData": [
                {
                    "entityRegistration": {"legalBusinessName": "Arrow Electronics Inc"},
                    "coreData": {
                        "physicalAddress": {
                            "city": "Centennial",
                            "stateOrProvinceCode": "CO",
                            "countryCode": "USA",
                        },
                        "assertions": {"goodsAndServices": {"primaryNaics": "5065"}},
                    },
                }
            ]
        },
        request=httpx.Request("GET", "https://api.sam.gov/entity-information/v3/entities"),
    )

    monkeypatch.setattr(sam_gov_company, "get_credential_cached", lambda s, e: "TEST_KEY")

    async def fake_get(url, **kwargs):
        return fake_resp

    monkeypatch.setattr(sam_gov_company.http, "get", fake_get)

    result = await sam_gov_company.enrich_company("arrow.com", "Arrow Electronics")

    assert result is not None
    assert result["source"] == "sam_gov"
    assert result["legal_name"] == "Arrow Electronics Inc"
    assert result["hq_city"] == "Centennial"
    assert result["hq_state"] == "CO"
    assert result["hq_country"] == "USA"
    assert result["naics"] == "5065"


@pytest.mark.asyncio
async def test_sam_company_returns_none_on_error(monkeypatch):
    """SAM.gov adapter degrades to None on HTTP errors."""
    import httpx

    from app.connectors import sam_gov_company

    async def bad_get(url, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(sam_gov_company, "get_credential_cached", lambda s, e: "TEST_KEY")
    monkeypatch.setattr(sam_gov_company.http, "get", bad_get)

    result = await sam_gov_company.enrich_company("example.com", "Broken Co")
    assert result is None


@pytest.mark.asyncio
async def test_sam_company_returns_none_on_non_200(monkeypatch):
    """SAM.gov adapter degrades to None on non-200 HTTP response."""
    import httpx

    from app.connectors import sam_gov_company

    fake_resp = httpx.Response(
        403,
        json={"error": "Forbidden"},
        request=httpx.Request("GET", "https://api.sam.gov/entity-information/v3/entities"),
    )

    async def fake_get(url, **kwargs):
        return fake_resp

    monkeypatch.setattr(sam_gov_company, "get_credential_cached", lambda s, e: "DEMO_KEY")
    monkeypatch.setattr(sam_gov_company.http, "get", fake_get)

    result = await sam_gov_company.enrich_company("example.com", "Some Co")
    assert result is None


@pytest.mark.asyncio
async def test_sam_company_returns_none_when_no_entities(monkeypatch):
    """SAM.gov adapter returns None when API returns empty entityData."""
    import httpx

    from app.connectors import sam_gov_company

    fake_resp = httpx.Response(
        200,
        json={"entityData": []},
        request=httpx.Request("GET", "https://api.sam.gov/entity-information/v3/entities"),
    )

    async def fake_get(url, **kwargs):
        return fake_resp

    monkeypatch.setattr(sam_gov_company, "get_credential_cached", lambda s, e: "TEST_KEY")
    monkeypatch.setattr(sam_gov_company.http, "get", fake_get)

    result = await sam_gov_company.enrich_company("example.com", "Nonexistent Corp")
    assert result is None
