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


def test_fmt_band_zero_min():
    """_fmt_band({min:0, max:N}) must format as '0-N', not drop min==0."""
    from app.connectors.explorium import _fmt_band

    assert _fmt_band({"min": 0, "max": 100}) == "0-100"
    assert _fmt_band({"min": 0, "max": 0}) == "0-0"
    assert _fmt_band({"min": 1, "max": 10}) == "1-10"
    assert _fmt_band({"min": 5}) == "5"
    assert _fmt_band({"max": 50}) == "50"
    assert _fmt_band(None) is None


@pytest.mark.asyncio
async def test_search_contacts_happy_path(monkeypatch):
    """Happy path: match → prospects → contact_information → returns contact dict."""
    from app.connectors import explorium

    async def fake_post(url, **k):
        if url.endswith("/businesses/match"):
            return Resp(200, {"data": {"matched_businesses": [{"business_id": "biz1"}]}})
        if url.endswith("/prospects"):
            return Resp(
                200,
                {
                    "data": [
                        {
                            "prospect_id": "p1",
                            "full_name": "Jane Doe",
                            "job_title": "VP Engineering",
                            "linkedin": "https://linkedin.com/in/janedoe",
                            "company_name": "Acme Corp",
                        }
                    ]
                },
            )
        if url.endswith("/prospects/contacts_information/enrich"):
            return Resp(
                200,
                {
                    "data": {
                        "professional_email": "jane@acme.com",
                        "professional_email_status": "valid",
                        "mobile_phone": "+15551234567",
                    }
                },
            )
        return Resp(200, {})

    monkeypatch.setattr(explorium.http, "post", fake_post, raising=False)
    contacts = await explorium.search_contacts("acme.com", "Acme Corp", "K", "", 10)

    assert len(contacts) == 1
    c = contacts[0]
    assert c["full_name"] == "Jane Doe"
    assert c["email"] == "jane@acme.com"
    assert c["verified"] is True
    assert c["phone"] == "+15551234567"
    assert c["title"] == "VP Engineering"
    assert c["source"] == "explorium"


@pytest.mark.asyncio
async def test_search_contacts_quota_429(monkeypatch):
    """429 from /prospects must propagate as ProviderQuotaError."""
    from app.connectors import explorium

    async def fake_post(url, **k):
        if url.endswith("/businesses/match"):
            return Resp(200, {"data": {"matched_businesses": [{"business_id": "biz1"}]}})
        return Resp(429, {})

    monkeypatch.setattr(explorium.http, "post", fake_post, raising=False)
    with pytest.raises(ProviderQuotaError):
        await explorium.search_contacts("acme.com", "Acme Corp", "K", "", 10)


@pytest.mark.asyncio
async def test_search_contacts_no_match_returns_empty(monkeypatch):
    """Null business_id → returns [] and never calls /prospects."""
    from app.connectors import explorium

    calls = []

    async def fake_post(url, **k):
        calls.append(url)
        return Resp(200, {"data": {"matched_businesses": [{"business_id": None}]}})

    monkeypatch.setattr(explorium.http, "post", fake_post, raising=False)
    result = await explorium.search_contacts("nobody.com", "Nobody", "K", "", 5)

    assert result == []
    # Only the match call should have been made
    assert all("/businesses/match" in u for u in calls)
    assert not any("/prospects" in u for u in calls)


@pytest.mark.asyncio
async def test_search_contacts_title_filter_sent_in_body(monkeypatch):
    """title_filter is passed as job_title filter in the /prospects request body."""
    from app.connectors import explorium

    prospects_body = {}

    async def fake_post(url, **k):
        if url.endswith("/businesses/match"):
            return Resp(200, {"data": {"matched_businesses": [{"business_id": "biz1"}]}})
        if url.endswith("/prospects"):
            prospects_body.update(k.get("json", {}))
            return Resp(200, {"data": []})
        return Resp(200, {})

    monkeypatch.setattr(explorium.http, "post", fake_post, raising=False)
    await explorium.search_contacts("acme.com", "Acme Corp", "K", "CTO", 5)

    assert prospects_body["filters"]["job_title"] == {"values": ["CTO"]}


@pytest.mark.asyncio
async def test_search_contacts_drops_prospect_with_no_name(monkeypatch):
    """Prospects missing full_name are filtered out of the returned list."""
    from app.connectors import explorium

    async def fake_post(url, **k):
        if url.endswith("/businesses/match"):
            return Resp(200, {"data": {"matched_businesses": [{"business_id": "biz1"}]}})
        if url.endswith("/prospects"):
            return Resp(
                200,
                {
                    "data": [
                        {"prospect_id": "p1", "full_name": None, "job_title": "Dev"},
                        {"prospect_id": "p2", "full_name": "Bob Smith", "job_title": "CEO"},
                    ]
                },
            )
        if url.endswith("/prospects/contacts_information/enrich"):
            return Resp(
                200,
                {
                    "data": {
                        "professional_email": "bob@co.com",
                        "professional_email_status": "valid",
                    }
                },
            )
        return Resp(200, {})

    monkeypatch.setattr(explorium.http, "post", fake_post, raising=False)
    contacts = await explorium.search_contacts("co.com", "Co", "K", "", 10)

    assert len(contacts) == 1
    assert contacts[0]["full_name"] == "Bob Smith"
