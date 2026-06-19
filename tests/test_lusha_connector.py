"""SP1 Lusha connector: field mapping, empty handling, quota → ProviderQuotaError.

http is patched at the connector module (app.connectors.lusha.http) with an AsyncMock
returning a fake httpx.Response-like object.
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock

import httpx
import pytest

from app.connectors import lusha
from app.services.enrichment_credit_guard import ProviderQuotaError


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


async def test_enrich_company_maps_fields(monkeypatch):
    payload = {
        "data": {
            "name": "Acme Aerospace Inc",
            "industry": "Aerospace & Defense",
            "employees": "501-1000",
            "location": {"city": "Dallas", "state": "TX", "country": "United States"},
            "social": {"linkedin": "linkedin.com/company/acme"},
        }
    }
    monkeypatch.setattr(lusha, "http", AsyncMock())
    lusha.http.get.return_value = _Resp(200, payload)
    out = await lusha.enrich_company("acme.com", "key")
    assert out["source"] == "lusha"
    assert out["legal_name"] == "Acme Aerospace Inc"
    assert out["domain"] == "acme.com"
    assert out["industry"] == "Aerospace & Defense"
    assert out["employee_size"] == "501-1000"
    assert out["hq_city"] == "Dallas"
    assert out["hq_state"] == "TX"
    assert out["hq_country"] == "United States"
    assert out["linkedin_url"] == "linkedin.com/company/acme"


async def test_enrich_company_empty_returns_none(monkeypatch):
    monkeypatch.setattr(lusha, "http", AsyncMock())
    lusha.http.get.return_value = _Resp(200, {"data": {}})
    assert await lusha.enrich_company("acme.com", "key") is None


@pytest.mark.parametrize("code", [402, 429])
async def test_enrich_company_quota_raises(monkeypatch, code):
    monkeypatch.setattr(lusha, "http", AsyncMock())
    lusha.http.get.return_value = _Resp(code, {})
    with pytest.raises(ProviderQuotaError):
        await lusha.enrich_company("acme.com", "key")


async def test_enrich_company_http_error_returns_none(monkeypatch):
    monkeypatch.setattr(lusha, "http", AsyncMock())
    lusha.http.get.side_effect = httpx.HTTPError("boom")
    assert await lusha.enrich_company("acme.com", "key") is None


async def test_search_contacts_maps_and_filters(monkeypatch):
    payload = {
        "contacts": [
            {
                "fullName": "Jane Buyer",
                "emailAddresses": [{"email": "jane@acme.com"}],
                "phoneNumbers": [{"number": "+15551234567"}],
                "jobTitle": "Director of Procurement",
                "isEmailVerified": True,
            },
            {"fullName": None},  # dropped (no name)
        ]
    }
    monkeypatch.setattr(lusha, "http", AsyncMock())
    lusha.http.post.return_value = _Resp(200, payload)
    out = await lusha.search_contacts("acme.com", "key", limit=5)
    assert len(out) == 1
    c = out[0]
    assert c["source"] == "lusha"
    assert c["full_name"] == "Jane Buyer"
    assert c["email"] == "jane@acme.com"
    assert c["phone"] == "+15551234567"
    assert c["title"] == "Director of Procurement"
    assert c["verified"] is True


@pytest.mark.parametrize("code", [402, 429])
async def test_search_contacts_quota_raises(monkeypatch, code):
    monkeypatch.setattr(lusha, "http", AsyncMock())
    lusha.http.post.return_value = _Resp(code, {})
    with pytest.raises(ProviderQuotaError):
        await lusha.search_contacts("acme.com", "key", limit=5)


async def test_search_contacts_http_error_returns_empty(monkeypatch):
    monkeypatch.setattr(lusha, "http", AsyncMock())
    lusha.http.post.side_effect = httpx.HTTPError("boom")
    assert await lusha.search_contacts("acme.com", "key", limit=5) == []
