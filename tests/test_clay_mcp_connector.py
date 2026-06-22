"""Tests for the Clay MCP backend connector (app/connectors/clay_mcp.py).

Monkeypatches _mcp_call / _poll_emails / clay_oauth.get_access_token — no network I/O.
Covers:
- enrich_company field mapping (legal_name, hq_state from locality, revenue_range,
  ticker from NYSE description)
- find_contacts domain filter (ex-employees at foreign domains dropped)
- disabled-without-token short-circuit (both functions)
- ProviderQuotaError propagates out of enrich_company (not swallowed)
- 401 response triggers one refresh + retry in _mcp_call
"""

import pytest

from app.services.enrichment_credit_guard import ProviderQuotaError

# ── fixture shapes mirroring the live spike response ────────────────────────

COMPANY_PAYLOAD = {
    "companies": {
        "arrow.com": {
            "name": "Arrow Electronics",
            "domain": "arrow.com",
            "website": "https://arrow.com",
            "industry": "Technology",
            "size": "10,001+ employees",
            "annual_revenue": "10B-100B",
            "locality": "Centennial, Colorado",
            "country": "US",
            "url": "https://www.linkedin.com/company/arrow-electronics",
            "description": "Arrow Electronics (NYSE:ARW) is a global provider ...",
        }
    }
}

CONTACTS_PAYLOAD = {
    "taskId": "task-abc-123",
    "contacts": [
        {
            "name": "Jane Buyer",
            "latest_experience_title": "Global Commodity Buyer",
            "domain": "arrow.com",
            "url": "https://linkedin.com/in/jane-buyer",
            "location_name": "Denver, CO",
            "latest_experience_company": "Arrow Electronics",
            "entityId": "eid-001",
        },
        {
            # Ex-employee now at a different company/domain — must be filtered out
            "name": "Ex Employee",
            "latest_experience_title": "Director",
            "domain": "other.com",
            "url": "https://linkedin.com/in/ex-emp",
            "location_name": "Austin, TX",
            "latest_experience_company": "Other Corp",
            "entityId": "eid-002",
        },
    ],
}


# ── enrich_company ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enrich_company_maps_base_fields(monkeypatch):
    from app.connectors import clay_mcp

    async def fake_token():
        return "TESTTOKEN"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", fake_token)

    async def fake_call(tool, args):
        return COMPANY_PAYLOAD

    monkeypatch.setattr(clay_mcp, "_mcp_call", fake_call)

    out = await clay_mcp.enrich_company("arrow.com")

    assert out is not None
    assert out["legal_name"] == "Arrow Electronics"
    assert out["hq_state"] == "Colorado"
    assert out["hq_city"] == "Centennial"
    assert out["revenue_range"] == "10B-100B"
    assert out["ticker"] == "ARW"
    assert out["source"] == "clay"


@pytest.mark.asyncio
async def test_enrich_company_nasdaq_ticker(monkeypatch):
    from app.connectors import clay_mcp

    async def fake_token():
        return "TESTTOKEN"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", fake_token)

    async def fake_call(tool, args):
        return {
            "companies": {
                "intel.com": {
                    "name": "Intel",
                    "domain": "intel.com",
                    "description": "Intel (NASDAQ:INTC) makes chips.",
                    "locality": "Santa Clara, California",
                }
            }
        }

    monkeypatch.setattr(clay_mcp, "_mcp_call", fake_call)

    out = await clay_mcp.enrich_company("intel.com")
    assert out["ticker"] == "INTC"
    assert out["hq_state"] == "California"


@pytest.mark.asyncio
async def test_enrich_company_returns_none_for_empty_payload(monkeypatch):
    from app.connectors import clay_mcp

    async def fake_token():
        return "TESTTOKEN"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", fake_token)

    async def fake_call(tool, args):
        return {}

    monkeypatch.setattr(clay_mcp, "_mcp_call", fake_call)

    out = await clay_mcp.enrich_company("unknown.com")
    assert out is None


@pytest.mark.asyncio
async def test_enrich_company_propagates_quota_error(monkeypatch):
    """ProviderQuotaError must NOT be swallowed — it propagates to the caller."""
    from app.connectors import clay_mcp

    async def fake_token():
        return "TESTTOKEN"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", fake_token)

    async def fake_call(tool, args):
        raise ProviderQuotaError("Clay MCP quota/rate-limit: 429")

    monkeypatch.setattr(clay_mcp, "_mcp_call", fake_call)

    with pytest.raises(ProviderQuotaError):
        await clay_mcp.enrich_company("arrow.com")


@pytest.mark.asyncio
async def test_enrich_company_none_when_not_connected(monkeypatch):
    from app.connectors import clay_mcp

    async def no_token():
        return None

    monkeypatch.setattr(clay_mcp, "_access_token", no_token)
    assert await clay_mcp.enrich_company("arrow.com") is None


@pytest.mark.asyncio
async def test_disabled_without_key_enrich_company(monkeypatch):
    from app.connectors import clay_mcp

    async def no_token():
        return None

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", no_token)

    out = await clay_mcp.enrich_company("x.com")
    assert out is None


# ── find_contacts ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_contacts_filters_to_target_domain(monkeypatch):
    from app.connectors import clay_mcp

    async def fake_token():
        return "TESTTOKEN"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", fake_token)

    async def fake_call(tool, args):
        return CONTACTS_PAYLOAD

    monkeypatch.setattr(clay_mcp, "_mcp_call", fake_call)

    async def fake_poll(task_id, n):
        return {}

    monkeypatch.setattr(clay_mcp, "_poll_emails", fake_poll)

    out = await clay_mcp.find_contacts("arrow.com", "", 10, want_email=False)

    assert [c["full_name"] for c in out] == ["Jane Buyer"]
    # No ex-employee from other.com
    assert all(c["full_name"] != "Ex Employee" for c in out)


@pytest.mark.asyncio
async def test_find_contacts_maps_fields(monkeypatch):
    from app.connectors import clay_mcp

    async def fake_token():
        return "TESTTOKEN"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", fake_token)

    async def fake_call(tool, args):
        return CONTACTS_PAYLOAD

    monkeypatch.setattr(clay_mcp, "_mcp_call", fake_call)

    async def fake_poll(task_id, n):
        return {}

    monkeypatch.setattr(clay_mcp, "_poll_emails", fake_poll)

    out = await clay_mcp.find_contacts("arrow.com", "", 10, want_email=False)
    assert len(out) == 1
    jane = out[0]
    assert jane["source"] == "clay"
    assert jane["title"] == "Global Commodity Buyer"
    assert jane["linkedin_url"] == "https://linkedin.com/in/jane-buyer"
    assert jane["location"] == "Denver, CO"
    assert jane["verified"] is False


@pytest.mark.asyncio
async def test_find_contacts_includes_polled_email(monkeypatch):
    from app.connectors import clay_mcp

    async def fake_token():
        return "TESTTOKEN"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", fake_token)

    async def fake_call(tool, args):
        return CONTACTS_PAYLOAD

    monkeypatch.setattr(clay_mcp, "_mcp_call", fake_call)

    async def fake_poll(task_id, n):
        assert task_id == "task-abc-123"
        return {"eid-001": "jane@arrow.com"}

    monkeypatch.setattr(clay_mcp, "_poll_emails", fake_poll)

    out = await clay_mcp.find_contacts("arrow.com", "", 10, want_email=True)
    assert out[0]["email"] == "jane@arrow.com"


@pytest.mark.asyncio
async def test_find_contacts_respects_limit(monkeypatch):
    from app.connectors import clay_mcp

    async def fake_token():
        return "TESTTOKEN"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", fake_token)

    many_contacts = {
        "contacts": [{"name": f"Person {i}", "domain": "arrow.com", "entityId": f"e{i}"} for i in range(20)]
    }

    async def fake_call(tool, args):
        return many_contacts

    monkeypatch.setattr(clay_mcp, "_mcp_call", fake_call)

    async def fake_poll(task_id, n):
        return {}

    monkeypatch.setattr(clay_mcp, "_poll_emails", fake_poll)

    out = await clay_mcp.find_contacts("arrow.com", "", 5, want_email=False)
    assert len(out) == 5


@pytest.mark.asyncio
async def test_find_contacts_propagates_quota_error(monkeypatch):
    """ProviderQuotaError must NOT be swallowed — it propagates to the caller."""
    from app.connectors import clay_mcp

    async def fake_token():
        return "TESTTOKEN"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", fake_token)

    async def fake_call(tool, args):
        raise ProviderQuotaError("Clay MCP quota/rate-limit: 429")

    monkeypatch.setattr(clay_mcp, "_mcp_call", fake_call)

    with pytest.raises(ProviderQuotaError):
        await clay_mcp.find_contacts("arrow.com", "", 10, want_email=False)


@pytest.mark.asyncio
async def test_disabled_without_key_find_contacts(monkeypatch):
    from app.connectors import clay_mcp

    async def no_token():
        return None

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", no_token)

    out = await clay_mcp.find_contacts("x.com", "", 10, want_email=False)
    assert out == []


# ── _mcp_call 401 refresh-retry ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_call_refreshes_on_401(monkeypatch):
    from app.connectors import clay_mcp

    calls = {"n": 0, "refreshed": 0}

    async def tok():
        return "AT"

    async def refresh():
        calls["refreshed"] += 1
        return "AT2"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)
    monkeypatch.setattr(clay_mcp.clay_oauth, "refresh", refresh)

    class R:
        def __init__(s, code):
            s.status_code = code

        def json(s):
            return {"result": {"structuredContent": {"ok": True}}}

    async def fake_post(url, **k):
        calls["n"] += 1
        return R(401) if calls["n"] == 1 else R(200)

    monkeypatch.setattr(clay_mcp.http, "post", fake_post, raising=False)
    out = await clay_mcp._mcp_call("find-and-enrich-company", {"companyIdentifier": "arrow.com"})
    assert calls["refreshed"] == 1 and calls["n"] == 2 and out == {"ok": True}
