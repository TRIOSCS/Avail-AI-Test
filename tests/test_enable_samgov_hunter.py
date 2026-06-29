"""Tests for enabling the SAM.gov + Hunter.io enrichment feature flags by default.

Verifies (per chore/enable-samgov-hunter):
- Both flags default to True (provider on out-of-the-box).
- Flag-on + key absent → provider is skipped/degraded GRACEFULLY (no error, no raise):
    * Hunter: _hunter_find_contacts returns [] without an outbound call.
    * SAM.gov: connector falls back to public DEMO_KEY and degrades to None on error.
- Flag-on + key present → provider actually fires and appears in the enrichment order.

Depends on: app/config.Settings, app/services/enrichment_router, app/enrichment_service,
            app/connectors/{hunter,sam_gov_company}.
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import patch

import httpx
import pytest

from app.config import Settings
from app.services import enrichment_router as er

# ── 1. Defaults: both flags ship enabled ──────────────────────────────────────


def test_samgov_and_hunter_default_enabled():
    """Field defaults (env-independent) are True for both safe providers."""
    assert Settings.model_fields["sam_gov_enrichment_enabled"].default is True
    assert Settings.model_fields["hunter_enrichment_enabled"].default is True


# ── 2. Hunter: flag-on + NO key → graceful skip (no outbound call, no raise) ───


@pytest.mark.asyncio
async def test_hunter_no_key_returns_empty_without_calling_connector():
    """With HUNTER_API_KEY absent, _hunter_find_contacts returns [] and never builds a
    HunterConnector (so no outbound call, no exception)."""
    from app import enrichment_service

    with (
        patch.object(enrichment_service, "get_credential_cached", return_value=None),
        patch("app.connectors.hunter.HunterConnector.domain_search") as domain_search,
    ):
        result = await enrichment_service._hunter_find_contacts("example.com")

    assert result == []
    domain_search.assert_not_called()


@pytest.mark.asyncio
async def test_cheap_contacts_hunter_enabled_no_key_is_graceful(monkeypatch):
    """Router cheap-contact gather with Hunter enabled but no key returns [] cleanly."""
    from app import enrichment_service

    monkeypatch.setattr(er.settings, "hunter_enrichment_enabled", True)
    monkeypatch.setattr(er.settings, "clay_enrichment_enabled", False)
    monkeypatch.setattr(enrichment_service, "get_credential_cached", lambda *a: None)

    results = await er._gather_cheap_contacts("example.com", "", 5)

    assert results == []


# ── 3. Hunter: flag-on + key present → provider fires, appears in order ────────


@pytest.mark.asyncio
async def test_cheap_contacts_hunter_enabled_with_key_appears(monkeypatch):
    """With Hunter enabled and a key present, Hunter contacts appear in the gather."""
    from app import enrichment_service

    monkeypatch.setattr(er.settings, "hunter_enrichment_enabled", True)
    monkeypatch.setattr(er.settings, "clay_enrichment_enabled", False)
    monkeypatch.setattr(enrichment_service, "get_credential_cached", lambda *a: "HUNTER-KEY")

    async def fake_domain_search(self, domain, limit=10):
        return [{"email": "buyer@example.com", "first_name": "Jane", "last_name": "Buyer", "position": "Buyer"}]

    monkeypatch.setattr("app.connectors.hunter.HunterConnector.domain_search", fake_domain_search)

    results = await er._gather_cheap_contacts("example.com", "", 5)

    assert any(c.get("source") == "hunter" and c.get("email") == "buyer@example.com" for c in results)


# ── 4. SAM.gov: flag-on + NO key → DEMO_KEY fallback, degrades to None, no raise ─


@pytest.mark.asyncio
async def test_sam_no_key_uses_demo_key_and_degrades_to_none(monkeypatch):
    """With SAM_GOV_API_KEY absent, the connector uses DEMO_KEY and returns None on a
    transport error — it must not raise."""
    from app.connectors import sam_gov_company

    monkeypatch.setattr(sam_gov_company, "get_credential_cached", lambda *a: None)

    captured = {}

    async def fake_get(url, **kwargs):
        captured["api_key"] = kwargs.get("params", {}).get("api_key")
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(sam_gov_company.http, "get", fake_get)

    result = await sam_gov_company.enrich_company("example.com", "Some Co")

    assert result is None  # graceful degradation, no exception propagated
    assert captured["api_key"] == "DEMO_KEY"  # keyless → public free tier


@pytest.mark.asyncio
async def test_sam_no_key_demo_tier_still_maps_results(monkeypatch):
    """The DEMO_KEY (keyless) path is functional: a 200 response still maps fields."""
    from app.connectors import sam_gov_company

    monkeypatch.setattr(sam_gov_company, "get_credential_cached", lambda *a: None)

    fake_resp = httpx.Response(
        200,
        json={
            "entityData": [
                {
                    "entityRegistration": {"legalBusinessName": "Demo Corp"},
                    "coreData": {"physicalAddress": {"city": "Reston", "stateOrProvinceCode": "VA"}},
                }
            ]
        },
        request=httpx.Request("GET", sam_gov_company._URL),
    )

    async def fake_get(url, **kwargs):
        return fake_resp

    monkeypatch.setattr(sam_gov_company.http, "get", fake_get)

    result = await sam_gov_company.enrich_company("example.com", "Demo Corp")

    assert result is not None
    assert result["legal_name"] == "Demo Corp"
    assert result["hq_city"] == "Reston"


# ── 5. SAM.gov: flag-on + key present → provider appears in company order ──────


@pytest.mark.asyncio
async def test_sam_enabled_with_key_appears_first_in_company_order(monkeypatch):
    """With the SAM flag on, the free SAM provider runs first in gather_company."""
    calls = []

    async def sam(d, n):
        calls.append("sam")
        return {"source": "sam_gov", "legal_name": "Arrow Inc"}

    async def noop_clay(d):
        return None

    async def noop(d, n):
        return None

    monkeypatch.setattr(er, "_sam_company", sam)
    monkeypatch.setattr(er, "_clay_company", noop_clay)
    monkeypatch.setattr(er, "_explorium_company", noop)
    monkeypatch.setattr(er, "_lusha_company", noop)
    monkeypatch.setattr(er, "_ai_company", noop)
    monkeypatch.setattr(er.settings, "sam_gov_enrichment_enabled", True)
    monkeypatch.setattr(er.settings, "clay_enrichment_enabled", False)
    monkeypatch.setattr(er.settings, "explorium_enrichment_enabled", False)
    monkeypatch.setattr(er.settings, "lusha_enrichment_enabled", False)
    monkeypatch.setattr(er, "circuit_open", lambda p: False)

    results = await er.gather_company("arrow.com", "Arrow")

    assert calls and calls[0] == "sam"
    assert any(r.get("source") == "sam_gov" for r in results)
