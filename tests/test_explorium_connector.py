"""Tests for the Explorium connector (task 4 — real v1 API pipeline).

Covers:
- match → firmographics 2-call pipeline with full field mapping
- no-match (business_id=None) returns None
- 429 quota raises ProviderQuotaError (NOT swallowed by try/except)
- api_key header used (NOT Authorization: Bearer)
"""

import pytest

from app.services.enrichment_credit_guard import ProviderQuotaError


class Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload

    def json(self):
        return self._p


@pytest.mark.asyncio
async def test_enrich_company_match_then_firmographics(monkeypatch):
    from app.connectors import explorium

    calls = []

    async def fake_post(url, **k):
        calls.append((url, k.get("headers", {}), k.get("json")))
        if url.endswith("/businesses/match"):
            return Resp(200, {"data": {"matched_businesses": [{"business_id": "abc"}]}})
        return Resp(
            200,
            {
                "data": {
                    "name": "Arrow",
                    "website": "https://arrow.com",
                    "linkedin_industry_category": "Electronics",
                    "naics": "423690",
                    "ticker": "ARW",
                    "yearly_revenue_range": {"min": 10_000_000_000},
                    "number_of_employees_range": {"min": 10001},
                    "city_name": "Centennial",
                    "region_name": "Colorado",
                    "country_name": "US",
                    "linkedin_profile": "https://linkedin.com/company/arrow",
                }
            },
        )

    monkeypatch.setattr(explorium.http, "post", fake_post, raising=False)
    out = await explorium.enrich_company("arrow.com", "Arrow", "K")

    # Must use api_key header, NOT Authorization: Bearer
    assert calls[0][1]["api_key"] == "K"
    assert "Authorization" not in calls[0][1]

    # Field mapping
    assert out["legal_name"] == "Arrow"
    assert out["naics"] == "423690"
    assert out["ticker"] == "ARW"
    assert out["hq_state"] == "Colorado"
    assert out["industry"] == "Electronics"
    assert out["source"] == "explorium"
    # Two calls were made (match + firmographics)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_enrich_company_no_match_returns_none(monkeypatch):
    from app.connectors import explorium

    async def fake_post(url, **k):
        return Resp(200, {"data": {"matched_businesses": [{"business_id": None}]}})

    monkeypatch.setattr(explorium.http, "post", fake_post, raising=False)
    assert await explorium.enrich_company("nope.com", "", "K") is None


@pytest.mark.asyncio
async def test_quota_raises(monkeypatch):
    from app.connectors import explorium

    async def fake_post(url, **k):
        return Resp(429, {})

    monkeypatch.setattr(explorium.http, "post", fake_post, raising=False)
    with pytest.raises(ProviderQuotaError):
        await explorium.enrich_company("x.com", "", "K")


@pytest.mark.asyncio
async def test_quota_402_raises(monkeypatch):
    from app.connectors import explorium

    async def fake_post(url, **k):
        return Resp(402, {})

    monkeypatch.setattr(explorium.http, "post", fake_post, raising=False)
    with pytest.raises(ProviderQuotaError):
        await explorium.enrich_company("x.com", "", "K")


@pytest.mark.asyncio
async def test_quota_403_raises(monkeypatch):
    from app.connectors import explorium

    async def fake_post(url, **k):
        return Resp(403, {})

    monkeypatch.setattr(explorium.http, "post", fake_post, raising=False)
    with pytest.raises(ProviderQuotaError):
        await explorium.enrich_company("x.com", "", "K")


@pytest.mark.asyncio
async def test_enrich_company_non_quota_error_returns_none(monkeypatch):
    """Non-quota HTTP errors (e.g. 500) degrade to None, not a raise."""
    from app.connectors import explorium

    calls = []

    async def fake_post(url, **k):
        calls.append(url)
        if url.endswith("/businesses/match"):
            return Resp(200, {"data": {"matched_businesses": [{"business_id": "abc"}]}})
        return Resp(500, {})

    monkeypatch.setattr(explorium.http, "post", fake_post, raising=False)
    result = await explorium.enrich_company("err.com", "Err Co", "K")
    assert result is None


@pytest.mark.asyncio
async def test_revenue_range_band_formatting(monkeypatch):
    """Band with both min+max formats as 'min-max'."""
    from app.connectors import explorium

    async def fake_post(url, **k):
        if url.endswith("/businesses/match"):
            return Resp(200, {"data": {"matched_businesses": [{"business_id": "bbb"}]}})
        return Resp(
            200,
            {
                "data": {
                    "name": "Acme",
                    "yearly_revenue_range": {"min": 1_000_000, "max": 10_000_000},
                    "number_of_employees_range": {"min": 100, "max": 500},
                }
            },
        )

    monkeypatch.setattr(explorium.http, "post", fake_post, raising=False)
    out = await explorium.enrich_company("acme.com", "Acme", "K")
    assert out["revenue_range"] == "1000000-10000000"
    assert out["employee_size"] == "100-500"


@pytest.mark.asyncio
async def test_domain_stripped_from_website(monkeypatch):
    """Domain in output is stripped of https:// prefix."""
    from app.connectors import explorium

    async def fake_post(url, **k):
        if url.endswith("/businesses/match"):
            return Resp(200, {"data": {"matched_businesses": [{"business_id": "ccc"}]}})
        return Resp(200, {"data": {"name": "Corp", "website": "https://corp.com/en/home"}})

    monkeypatch.setattr(explorium.http, "post", fake_post, raising=False)
    out = await explorium.enrich_company("corp.com", "Corp", "K")
    assert out["domain"] == "corp.com"
