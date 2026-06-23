"""Tests for the Clay MCP backend connector (app/connectors/clay_mcp.py).

Monkeypatches _mcp_call / _poll_emails / clay_oauth.get_access_token, or fakes http.post
with the Clay MCP protocol (_fake_clay) — no network I/O.
Covers:
- MCP handshake: initialize → notifications/initialized → tools/call with the
  Mcp-Session-Id header; session reuse; re-init on 400; SSE response parsing
- enrich_company field mapping (legal_name, hq_state from locality, revenue_range,
  ticker from NYSE description)
- find_contacts domain filter (ex-employees at foreign domains dropped)
- disabled-without-token short-circuit (both functions)
- ProviderQuotaError propagates out of enrich_company (not swallowed)
- 401 response triggers one refresh + re-init + retry in _mcp_call
"""

import json

import pytest

from app.services.enrichment_credit_guard import ProviderQuotaError

# ── test isolation: the MCP session is cached at module level ────────────────


@pytest.fixture(autouse=True)
def _reset_clay_session():
    """Reset the module-level Clay MCP session cache so tests don't leak sessions."""
    from app.connectors import clay_mcp

    clay_mcp._session.update(id=None, token=None)
    yield
    clay_mcp._session.update(id=None, token=None)


# ── Clay MCP protocol fake (handshake + SSE) ─────────────────────────────────


class _FakeResp:
    """Minimal httpx.Response stand-in covering the Clay MCP wire shapes."""

    def __init__(self, status_code, *, headers=None, json_body=None, sse_body=None):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self._json = json_body
        if sse_body is not None:
            self.headers["content-type"] = "text/event-stream"
            self.text = f"event: message\ndata: {json.dumps(sse_body)}\n\n"
        else:
            self.headers.setdefault("content-type", "application/json")
            self.text = json.dumps(json_body) if json_body is not None else ""

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


def _fake_clay(
    state,
    *,
    tool_statuses=(200,),
    tool_result=None,
    sse=True,
    init_statuses=(200,),
    note_status=202,
    session_ids=("SID-1", "SID-2", "SID-3", "SID-4"),
):
    """An http.post replacement that simulates Clay's MCP handshake + tools/call.

    initialize → status from *init_statuses* (200 carries an incrementing Mcp-Session-Id);
    notifications/initialized → *note_status*; tools/call → status from *tool_statuses*
    (per successive call, last value repeats), body carrying ``result.structuredContent`` =
    *tool_result*. Records state for asserts.
    """
    state.setdefault("methods", [])
    state.setdefault("init_count", 0)
    state.setdefault("tool_calls", 0)
    state.setdefault("call_session_headers", [])

    async def fake_post(url, **kw):
        method = kw["json"]["method"]
        state["methods"].append(method)
        if method == "initialize":
            i = state["init_count"]
            state["init_count"] += 1
            status = init_statuses[min(i, len(init_statuses) - 1)]
            if status != 200:
                return _FakeResp(status, json_body={})
            sid = session_ids[min(i, len(session_ids) - 1)]
            return _FakeResp(
                200,
                headers={"mcp-session-id": sid},
                json_body={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18"}},
            )
        if method == "notifications/initialized":
            return _FakeResp(note_status, json_body={})
        if method == "tools/call":
            i = state["tool_calls"]
            state["tool_calls"] += 1
            state["call_session_headers"].append(kw["headers"].get("Mcp-Session-Id"))
            status = tool_statuses[min(i, len(tool_statuses) - 1)]
            if status != 200:
                return _FakeResp(status, json_body={"jsonrpc": "2.0", "id": 1, "error": {"code": status}})
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"structuredContent": tool_result if tool_result is not None else {"ok": True}},
            }
            return _FakeResp(status, sse_body=body) if sse else _FakeResp(status, json_body=body)
        return _FakeResp(200, json_body={})

    return fake_post


# ── fixture shapes mirroring the verified live response ──────────────────────

COMPANY_PAYLOAD = {
    "companies": {
        "arrow.com": {
            "name": "Arrow Electronics",
            "domain": "arrow.com",
            "website": "https://arrow.com",
            "industry": "Technology",
            "size": "10,001+ employees",
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
    assert out["ticker"] == "ARW"
    assert out["source"] == "clay"
    # annual_revenue is not a base field and the call requests no data points, so the
    # connector never emits revenue_range (Explorium supplies it in the blend instead).
    assert "revenue_range" not in out


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


# ── _mcp_call MCP handshake + session ────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_call_performs_handshake_and_sends_session_header(monkeypatch):
    """First call runs initialize → notifications/initialized → tools/call, and the
    tools/call carries the Mcp-Session-Id from initialize."""
    from app.connectors import clay_mcp

    async def tok():
        return "AT"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)
    state: dict = {}
    monkeypatch.setattr(clay_mcp.http, "post", _fake_clay(state, tool_result={"companies": {}}), raising=False)

    await clay_mcp._mcp_call("find-and-enrich-company", {"companyIdentifier": "x.com"})

    assert state["methods"][:3] == ["initialize", "notifications/initialized", "tools/call"]
    assert state["init_count"] == 1
    assert state["call_session_headers"] == ["SID-1"]


@pytest.mark.asyncio
async def test_mcp_call_parses_sse_response(monkeypatch):
    """Tools/call returns text/event-stream; the structuredContent must be parsed."""
    from app.connectors import clay_mcp

    async def tok():
        return "AT"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)
    state: dict = {}
    payload = {"companies": {"x.com": {"name": "X"}}}
    monkeypatch.setattr(clay_mcp.http, "post", _fake_clay(state, sse=True, tool_result=payload), raising=False)

    out = await clay_mcp._mcp_call("find-and-enrich-company", {"companyIdentifier": "x.com"})
    assert out == payload


@pytest.mark.asyncio
async def test_mcp_call_reuses_session_across_calls(monkeypatch):
    """A second call reuses the cached session — only one initialize handshake."""
    from app.connectors import clay_mcp

    async def tok():
        return "AT"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)
    state: dict = {}
    monkeypatch.setattr(clay_mcp.http, "post", _fake_clay(state, tool_result={"ok": 1}), raising=False)

    await clay_mcp._mcp_call("find-and-enrich-company", {"companyIdentifier": "a.com"})
    await clay_mcp._mcp_call("get-task-context", {"taskId": "t"})

    assert state["init_count"] == 1
    assert state["tool_calls"] == 2
    assert state["call_session_headers"] == ["SID-1", "SID-1"]


@pytest.mark.asyncio
async def test_mcp_call_reinitializes_on_400_missing_session(monkeypatch):
    """A 400 (expired/missing session) triggers one re-initialize + retry."""
    from app.connectors import clay_mcp

    async def tok():
        return "AT"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)
    state: dict = {}
    monkeypatch.setattr(
        clay_mcp.http, "post", _fake_clay(state, tool_statuses=(400, 200), tool_result={"ok": 2}), raising=False
    )

    out = await clay_mcp._mcp_call("find-and-enrich-company", {"companyIdentifier": "x.com"})
    assert out == {"ok": 2}
    assert state["init_count"] == 2  # initial + re-init after the 400
    assert state["tool_calls"] == 2


@pytest.mark.asyncio
async def test_mcp_call_returns_empty_on_jsonrpc_error(monkeypatch):
    """A 200 response carrying a JSON-RPC error (no result) degrades to {}."""
    from app.connectors import clay_mcp

    async def tok():
        return "AT"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)

    async def fake_post(url, **kw):
        method = kw["json"]["method"]
        if method == "initialize":
            return _FakeResp(200, headers={"mcp-session-id": "S"}, json_body={"result": {}})
        if method == "notifications/initialized":
            return _FakeResp(202, json_body={})
        return _FakeResp(200, sse_body={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}})

    monkeypatch.setattr(clay_mcp.http, "post", fake_post, raising=False)
    out = await clay_mcp._mcp_call("find-and-enrich-company", {"companyIdentifier": "x.com"})
    assert out == {}


@pytest.mark.asyncio
async def test_mcp_call_returns_empty_when_initialize_fails(monkeypatch):
    """If the initialize handshake fails, no tools/call is attempted and {} returned."""
    from app.connectors import clay_mcp

    async def tok():
        return "AT"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)
    calls = {"tool": 0}

    async def fake_post(url, **kw):
        if kw["json"]["method"] == "initialize":
            return _FakeResp(503, json_body={})
        calls["tool"] += 1
        return _FakeResp(200, sse_body={"result": {"structuredContent": {"ok": True}}})

    monkeypatch.setattr(clay_mcp.http, "post", fake_post, raising=False)
    out = await clay_mcp._mcp_call("find-and-enrich-company", {"companyIdentifier": "x.com"})
    assert out == {}
    assert calls["tool"] == 0


# ── _mcp_call 401 refresh-retry ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_call_refreshes_on_401(monkeypatch):
    """A 401 on tools/call refreshes the token, re-initializes, and retries once."""
    from app.connectors import clay_mcp

    refreshed = {"n": 0}

    async def tok():
        return "AT"

    async def refresh():
        refreshed["n"] += 1
        return "AT2"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)
    monkeypatch.setattr(clay_mcp.clay_oauth, "refresh", refresh)
    state: dict = {}
    monkeypatch.setattr(clay_mcp.http, "post", _fake_clay(state, tool_statuses=(401, 200)), raising=False)

    out = await clay_mcp._mcp_call("find-and-enrich-company", {"companyIdentifier": "arrow.com"})
    assert out == {"ok": True}
    assert refreshed["n"] == 1
    assert state["tool_calls"] == 2
    assert state["init_count"] == 2  # initial + re-init after refresh


@pytest.mark.asyncio
async def test_mcp_call_quota_after_refresh_propagates(monkeypatch):
    """After a 401→refresh, a 429 on the retry must raise ProviderQuotaError."""
    from app.connectors import clay_mcp

    refreshed = {"n": 0}

    async def tok():
        return "AT"

    async def refresh():
        refreshed["n"] += 1
        return "AT2"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)
    monkeypatch.setattr(clay_mcp.clay_oauth, "refresh", refresh)
    state: dict = {}
    monkeypatch.setattr(clay_mcp.http, "post", _fake_clay(state, tool_statuses=(401, 429)), raising=False)

    with pytest.raises(ProviderQuotaError):
        await clay_mcp._mcp_call("find-and-enrich-company", {"companyIdentifier": "arrow.com"})

    assert refreshed["n"] == 1
    assert state["tool_calls"] == 2


# ── _mcp_call edge cases ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_call_returns_empty_when_no_token(monkeypatch):
    from app.connectors import clay_mcp

    async def no_token():
        return None

    monkeypatch.setattr(clay_mcp, "_access_token", no_token)
    result = await clay_mcp._mcp_call("find-and-enrich-company", {})
    assert result == {}


@pytest.mark.asyncio
async def test_mcp_call_returns_empty_when_refresh_fails_after_401(monkeypatch):
    from app.connectors import clay_mcp

    async def tok():
        return "AT"

    async def refresh_fail():
        return None

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)
    monkeypatch.setattr(clay_mcp.clay_oauth, "refresh", refresh_fail)
    state: dict = {}
    monkeypatch.setattr(clay_mcp.http, "post", _fake_clay(state, tool_statuses=(401,)), raising=False)

    result = await clay_mcp._mcp_call("find-and-enrich-company", {})
    assert result == {}
    assert state["tool_calls"] == 1  # no retry once the refresh fails


@pytest.mark.asyncio
async def test_mcp_call_returns_empty_on_non_200_non_quota(monkeypatch):
    from app.connectors import clay_mcp

    async def tok():
        return "AT"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)
    state: dict = {}
    monkeypatch.setattr(clay_mcp.http, "post", _fake_clay(state, tool_statuses=(503,)), raising=False)

    result = await clay_mcp._mcp_call("find-and-enrich-company", {})
    assert result == {}
    assert state["init_count"] == 1
    assert state["tool_calls"] == 1


# ── _parse_response (JSON vs SSE) ─────────────────────────────────────────────


def test_parse_response_sse():
    from app.connectors.clay_mcp import _parse_response

    resp = _FakeResp(200, sse_body={"jsonrpc": "2.0", "result": {"x": 1}})
    assert _parse_response(resp) == {"jsonrpc": "2.0", "result": {"x": 1}}


def test_parse_response_json():
    from app.connectors.clay_mcp import _parse_response

    resp = _FakeResp(200, json_body={"result": {"y": 2}})
    assert _parse_response(resp) == {"result": {"y": 2}}


def test_parse_response_unparseable_returns_empty():
    from app.connectors.clay_mcp import _parse_response

    class _Bad:
        headers = {"content-type": "application/json"}
        text = "not json"

        def json(self):
            raise ValueError("nope")

    assert _parse_response(_Bad()) == {}


class _RawSSE:
    """An httpx.Response stand-in with a hand-written SSE body."""

    status_code = 200
    headers = {"content-type": "text/event-stream"}

    def __init__(self, text):
        self.text = text

    def json(self):  # pragma: no cover - SSE branch never calls this
        raise ValueError("sse")


def test_parse_response_sse_multiple_data_lines_last_wins():
    from app.connectors.clay_mcp import _parse_response

    resp = _RawSSE('event: message\ndata: {"first": 1}\n\ndata: {"jsonrpc": "2.0", "win": true}\n\n')
    assert _parse_response(resp) == {"jsonrpc": "2.0", "win": True}


def test_parse_response_sse_skips_garbage_data_line():
    from app.connectors.clay_mcp import _parse_response

    resp = _RawSSE('data: not-json\ndata: {"ok": 1}\n')
    assert _parse_response(resp) == {"ok": 1}


def test_parse_response_sse_no_data_line_returns_empty():
    from app.connectors.clay_mcp import _parse_response

    assert _parse_response(_RawSSE("event: ping\n: keep-alive\n\n")) == {}


def test_parse_response_sse_non_dict_payload_returns_empty():
    from app.connectors.clay_mcp import _parse_response

    assert _parse_response(_RawSSE("data: [1, 2, 3]\n")) == {}


# ── _ensure_session: double-checked invalidation (concurrency safety) ──────────


@pytest.mark.asyncio
async def test_ensure_session_invalidate_only_clears_matching_id(monkeypatch):
    """A caller invalidating a stale id must NOT clobber a session a peer already re-
    established — only re-init when the cache still holds the failed id."""
    from app.connectors import clay_mcp

    inits = {"n": 0}

    async def fake_init(token):
        inits["n"] += 1
        return f"NEW-{inits['n']}"

    monkeypatch.setattr(clay_mcp, "_initialize", fake_init)

    # A peer already swapped in a live session → caller reuses it, no re-init.
    clay_mcp._session.update(id="LIVE", token="AT")
    assert await clay_mcp._ensure_session("AT", invalidate="DEAD") == "LIVE"
    assert inits["n"] == 0

    # The failed id is still cached → re-initialize.
    clay_mcp._session.update(id="DEAD", token="AT")
    assert await clay_mcp._ensure_session("AT", invalidate="DEAD") == "NEW-1"
    assert inits["n"] == 1


@pytest.mark.asyncio
async def test_ensure_session_reinitializes_on_token_change(monkeypatch):
    """A changed access token always invalidates the cached session."""
    from app.connectors import clay_mcp

    inits = {"n": 0}

    async def fake_init(token):
        inits["n"] += 1
        return "S2"

    monkeypatch.setattr(clay_mcp, "_initialize", fake_init)
    clay_mcp._session.update(id="S1", token="AT")
    assert await clay_mcp._ensure_session("AT2") == "S2"
    assert inits["n"] == 1


@pytest.mark.asyncio
async def test_mcp_call_reinitializes_when_token_rotates(monkeypatch):
    """A silently-rotated token between two healthy calls forces a fresh handshake."""
    from app.connectors import clay_mcp

    tokens = iter(["AT1", "AT2"])

    async def rotating_token():
        return next(tokens)

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", rotating_token)
    state: dict = {}
    monkeypatch.setattr(clay_mcp.http, "post", _fake_clay(state), raising=False)

    await clay_mcp._mcp_call("find-and-enrich-company", {"companyIdentifier": "a.com"})
    await clay_mcp._mcp_call("find-and-enrich-company", {"companyIdentifier": "b.com"})

    assert state["init_count"] == 2  # token changed → new handshake
    assert state["call_session_headers"] == ["SID-1", "SID-2"]


# ── _mcp_call: tool-level error, content fallback, handshake robustness ────────


@pytest.mark.asyncio
async def test_mcp_call_tool_isError_returns_empty(monkeypatch):
    """A 200 response with result.isError (tool-level failure) degrades to {} + a log,
    not silent 'no data'."""
    from app.connectors import clay_mcp

    async def tok():
        return "AT"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)

    async def fake_post(url, **kw):
        method = kw["json"]["method"]
        if method == "initialize":
            return _FakeResp(200, headers={"mcp-session-id": "S"}, json_body={"result": {}})
        if method == "notifications/initialized":
            return _FakeResp(202, json_body={})
        return _FakeResp(
            200,
            sse_body={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"isError": True, "content": [{"type": "text", "text": "bad identifier"}]},
            },
        )

    monkeypatch.setattr(clay_mcp.http, "post", fake_post, raising=False)
    assert await clay_mcp._mcp_call("find-and-enrich-company", {"companyIdentifier": "x"}) == {}


@pytest.mark.asyncio
async def test_mcp_call_falls_back_to_content_text(monkeypatch):
    """When structuredContent is absent, the JSON in content[0].text is parsed."""
    from app.connectors import clay_mcp

    async def tok():
        return "AT"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)
    inner = {"companies": {"x.com": {"name": "X Corp"}}}

    async def fake_post(url, **kw):
        method = kw["json"]["method"]
        if method == "initialize":
            return _FakeResp(200, headers={"mcp-session-id": "S"}, json_body={"result": {}})
        if method == "notifications/initialized":
            return _FakeResp(202, json_body={})
        return _FakeResp(
            200,
            sse_body={"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps(inner)}]}},
        )

    monkeypatch.setattr(clay_mcp.http, "post", fake_post, raising=False)
    assert await clay_mcp._mcp_call("find-and-enrich-company", {"companyIdentifier": "x.com"}) == inner


@pytest.mark.asyncio
async def test_mcp_call_returns_empty_when_notifications_initialized_fails(monkeypatch):
    """A failed notifications/initialized leaves the session half-open — do not cache
    it."""
    from app.connectors import clay_mcp

    async def tok():
        return "AT"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)
    state: dict = {}
    monkeypatch.setattr(clay_mcp.http, "post", _fake_clay(state, note_status=400), raising=False)

    assert await clay_mcp._mcp_call("find-and-enrich-company", {"companyIdentifier": "x.com"}) == {}
    assert state["tool_calls"] == 0  # never reached tools/call without a session
    assert clay_mcp._session["id"] is None  # half-open session not cached


@pytest.mark.asyncio
async def test_mcp_call_chained_401_then_400_retries_once(monkeypatch):
    """401 then 400 must NOT chain a second retry (elif): at most one retry per call."""
    from app.connectors import clay_mcp

    async def tok():
        return "AT"

    async def refresh():
        return "AT2"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)
    monkeypatch.setattr(clay_mcp.clay_oauth, "refresh", refresh)
    state: dict = {}
    monkeypatch.setattr(clay_mcp.http, "post", _fake_clay(state, tool_statuses=(401, 400)), raising=False)

    assert await clay_mcp._mcp_call("find-and-enrich-company", {"companyIdentifier": "x"}) == {}
    assert state["tool_calls"] == 2  # initial + single 401 retry; the 400 is not re-retried
    assert state["init_count"] == 2


@pytest.mark.asyncio
async def test_mcp_call_returns_empty_when_reinit_fails_mid_retry(monkeypatch):
    """If the forced re-init during a 401 retry fails, no sessionless tools/call
    fires."""
    from app.connectors import clay_mcp

    async def tok():
        return "AT"

    async def refresh():
        return "AT2"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)
    monkeypatch.setattr(clay_mcp.clay_oauth, "refresh", refresh)
    state: dict = {}
    monkeypatch.setattr(
        clay_mcp.http, "post", _fake_clay(state, tool_statuses=(401,), init_statuses=(200, 503)), raising=False
    )

    assert await clay_mcp._mcp_call("find-and-enrich-company", {"companyIdentifier": "x"}) == {}
    assert state["tool_calls"] == 1  # the first call 401'd; re-init failed → no second post


@pytest.mark.asyncio
async def test_find_contacts_email_poll_integration(monkeypatch):
    """End-to-end (real handshake fake): find_contacts → _poll_emails → get-task-context
    over ONE shared session, surfacing the polled email."""
    from app.connectors import clay_mcp

    async def tok():
        return "AT"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)
    monkeypatch.setattr(clay_mcp, "_POLL_DELAY", 0.0)
    state = {"init": 0}

    async def fake_post(url, **kw):
        method = kw["json"]["method"]
        if method == "initialize":
            state["init"] += 1
            return _FakeResp(200, headers={"mcp-session-id": "S"}, json_body={"result": {}})
        if method == "notifications/initialized":
            return _FakeResp(202, json_body={})
        name = kw["json"]["params"]["name"]
        if name == "find-and-enrich-contacts-at-company":
            sc = {
                "taskId": "T1",
                "contacts": [
                    {
                        "name": "Jane",
                        "domain": "arrow.com",
                        "entityId": "E1",
                        "latest_experience_title": "Buyer",
                        "url": "u",
                        "location_name": "Denver",
                        "latest_experience_company": "Arrow",
                    }
                ],
            }
            return _FakeResp(200, sse_body={"result": {"structuredContent": sc}})
        # get-task-context
        sc = {
            "contacts": [
                {"entityId": "E1", "enrichments": [{"name": "Email", "state": "completed", "value": "jane@arrow.com"}]}
            ]
        }
        return _FakeResp(200, sse_body={"result": {"structuredContent": sc}})

    monkeypatch.setattr(clay_mcp.http, "post", fake_post, raising=False)
    out = await clay_mcp.find_contacts("arrow.com", "", 10, want_email=True)

    assert len(out) == 1
    assert out[0]["email"] == "jane@arrow.com"
    assert state["init"] == 1  # both tools shared one session


# ── _parse_locality edge cases ────────────────────────────────────────────────


def test_parse_locality_empty():
    from app.connectors.clay_mcp import _parse_locality

    city, state = _parse_locality("")
    assert city is None and state is None


def test_parse_locality_no_state():
    from app.connectors.clay_mcp import _parse_locality

    city, state = _parse_locality("Denver")
    assert city == "Denver" and state is None


# ── _map_company: all-falsy informative fields ────────────────────────────────


@pytest.mark.asyncio
async def test_map_company_returns_none_when_all_fields_empty(monkeypatch):
    from app.connectors import clay_mcp

    async def tok():
        return "AT"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)

    async def fake_call(tool, args):
        return {"companies": {"empty.com": {"name": None, "domain": "empty.com"}}}

    monkeypatch.setattr(clay_mcp, "_mcp_call", fake_call)
    result = await clay_mcp.enrich_company("empty.com")
    assert result is None


# ── enrich_company: httpx error is swallowed ─────────────────────────────────


@pytest.mark.asyncio
async def test_enrich_company_swallows_http_error(monkeypatch):
    import httpx

    from app.connectors import clay_mcp

    async def tok():
        return "AT"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)

    async def raise_http(tool, args):
        raise httpx.HTTPError("connection failed")

    monkeypatch.setattr(clay_mcp, "_mcp_call", raise_http)
    result = await clay_mcp.enrich_company("arrow.com")
    assert result is None


# ── _poll_emails direct test ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_emails_returns_completed_emails(monkeypatch):
    from app.connectors import clay_mcp

    ctx = {
        "contacts": [
            {
                "entityId": "eid-1",
                "enrichments": [{"name": "Email", "state": "completed", "value": "jane@co.com"}],
            },
            {
                "entityId": "eid-2",
                "enrichments": [{"name": "Email", "state": "failed", "value": None}],
            },
        ]
    }

    async def fake_call(tool, args):
        return ctx

    monkeypatch.setattr(clay_mcp, "_mcp_call", fake_call)
    monkeypatch.setattr(clay_mcp, "_POLL_DELAY", 0.0)

    emails = await clay_mcp._poll_emails("task-1", 1)
    assert emails.get("eid-1") == "jane@co.com"
    assert "eid-2" not in emails


@pytest.mark.asyncio
async def test_poll_emails_stops_early_when_no_in_progress(monkeypatch):
    from app.connectors import clay_mcp

    calls = []

    async def fake_call(tool, args):
        calls.append(1)
        return {
            "contacts": [
                {"entityId": "e1", "enrichments": [{"name": "Email", "state": "completed", "value": "a@b.com"}]}
            ]
        }

    monkeypatch.setattr(clay_mcp, "_mcp_call", fake_call)
    monkeypatch.setattr(clay_mcp, "_POLL_DELAY", 0.0)

    await clay_mcp._poll_emails("task-1", 5)
    # Stops after first poll because no in-progress enrichments
    assert len(calls) == 1


# ── find_contacts: title_filter and missing name ──────────────────────────────


@pytest.mark.asyncio
async def test_find_contacts_with_title_filter(monkeypatch):
    from app.connectors import clay_mcp

    async def tok():
        return "AT"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)

    captured_args = {}

    async def fake_call(tool, args):
        captured_args.update(args)
        return {"contacts": []}

    monkeypatch.setattr(clay_mcp, "_mcp_call", fake_call)

    async def fake_poll(task_id, n):
        return {}

    monkeypatch.setattr(clay_mcp, "_poll_emails", fake_poll)

    await clay_mcp.find_contacts("co.com", "procurement", 10, want_email=False)
    assert "contactFilters" in captured_args


@pytest.mark.asyncio
async def test_find_contacts_skips_contacts_without_name(monkeypatch):
    from app.connectors import clay_mcp

    async def tok():
        return "AT"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)

    async def fake_call(tool, args):
        return {
            "contacts": [
                {"name": None, "domain": "co.com", "entityId": "e1"},
                {"name": "Jane", "domain": "co.com", "entityId": "e2"},
            ]
        }

    monkeypatch.setattr(clay_mcp, "_mcp_call", fake_call)

    async def fake_poll(task_id, n):
        return {}

    monkeypatch.setattr(clay_mcp, "_poll_emails", fake_poll)

    out = await clay_mcp.find_contacts("co.com", "", 10, want_email=False)
    assert len(out) == 1
    assert out[0]["full_name"] == "Jane"


@pytest.mark.asyncio
async def test_find_contacts_swallows_value_error(monkeypatch):
    from app.connectors import clay_mcp

    async def tok():
        return "AT"

    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)

    async def raise_value(tool, args):
        raise ValueError("unexpected payload")

    monkeypatch.setattr(clay_mcp, "_mcp_call", raise_value)

    out = await clay_mcp.find_contacts("co.com", "", 10, want_email=False)
    assert out == []
