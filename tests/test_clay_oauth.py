# tests/test_clay_oauth.py — unit tests for Clay OAuth token lifecycle service
import base64
import hashlib

import pytest

from app.services import clay_oauth as co


class Resp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload

    def json(self):
        return self._p


def _seed_store(monkeypatch):
    store = {}
    monkeypatch.setattr(
        co,
        "_store",
        lambda upd: (
            store.update({k: v for k, v in upd.items() if v is not None})
            or [store.pop(k, None) for k, v in upd.items() if v is None]
        ),
    )
    monkeypatch.setattr(co, "_load", lambda k: store.get(k))
    return store


def test_pkce_pair_is_s256():
    v, c = co.pkce_pair()
    expect = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert c == expect


def test_build_authorize_url_has_scope_and_challenge():
    url = co.build_authorize_url("cid", "st8", "chal")
    assert url.startswith(co.CLAY_AUTHORIZE_URL)
    assert "scope=mcp" in url and "code_challenge=chal" in url and "code_challenge_method=S256" in url
    assert "client_id=cid" in url and "state=st8" in url and "response_type=code" in url


@pytest.mark.asyncio
async def test_register_client_reuses_existing(monkeypatch):
    store = _seed_store(monkeypatch)
    store["CLAY_OAUTH_CLIENT_ID"] = "existing"
    cid = await co.register_client()
    assert cid == "existing"


@pytest.mark.asyncio
async def test_register_client_does_dcr(monkeypatch):
    store = _seed_store(monkeypatch)

    async def fake_post(url, **k):
        return Resp(201, {"client_id": "new-cid"})

    monkeypatch.setattr(co.http, "post", fake_post, raising=False)
    cid = await co.register_client()
    assert cid == "new-cid" and store["CLAY_OAUTH_CLIENT_ID"] == "new-cid"


@pytest.mark.asyncio
async def test_exchange_code_persists_tokens(monkeypatch):
    store = _seed_store(monkeypatch)

    async def fake_post(url, **k):
        return Resp(200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})

    monkeypatch.setattr(co.http, "post", fake_post, raising=False)
    ok = await co.exchange_code("code", "verifier", "cid")
    assert ok and store["CLAY_OAUTH_ACCESS_TOKEN"] == "AT" and store["CLAY_OAUTH_REFRESH_TOKEN"] == "RT"
    assert "CLAY_OAUTH_EXPIRES_AT" in store and co.is_connected()


@pytest.mark.asyncio
async def test_get_access_token_returns_fresh(monkeypatch):
    from datetime import datetime, timedelta, timezone

    store = _seed_store(monkeypatch)
    store["CLAY_OAUTH_ACCESS_TOKEN"] = "AT"
    store["CLAY_OAUTH_REFRESH_TOKEN"] = "RT"
    store["CLAY_OAUTH_EXPIRES_AT"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert await co.get_access_token() == "AT"


@pytest.mark.asyncio
async def test_get_access_token_refreshes_when_expired(monkeypatch):
    from datetime import datetime, timedelta, timezone

    store = _seed_store(monkeypatch)
    store["CLAY_OAUTH_ACCESS_TOKEN"] = "OLD"
    store["CLAY_OAUTH_REFRESH_TOKEN"] = "RT"
    store["CLAY_OAUTH_CLIENT_ID"] = "cid"
    store["CLAY_OAUTH_EXPIRES_AT"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()

    async def fake_post(url, **k):
        return Resp(200, {"access_token": "NEW", "refresh_token": "RT2", "expires_in": 3600})

    monkeypatch.setattr(co.http, "post", fake_post, raising=False)
    assert await co.get_access_token() == "NEW"
    assert store["CLAY_OAUTH_ACCESS_TOKEN"] == "NEW" and store["CLAY_OAUTH_REFRESH_TOKEN"] == "RT2"


@pytest.mark.asyncio
async def test_refresh_failure_marks_needs_reconnect(monkeypatch):
    store = _seed_store(monkeypatch)
    store["CLAY_OAUTH_REFRESH_TOKEN"] = "RT"
    store["CLAY_OAUTH_CLIENT_ID"] = "cid"

    async def fake_post(url, **k):
        return Resp(400, {"error": "invalid_grant"})

    monkeypatch.setattr(co.http, "post", fake_post, raising=False)
    assert await co.refresh() is None
    assert store.get("CLAY_OAUTH_NEEDS_RECONNECT") == "1" and co.needs_reconnect()
    assert store.get("CLAY_OAUTH_ACCESS_TOKEN") is None


@pytest.mark.asyncio
async def test_get_access_token_none_when_absent(monkeypatch):
    _seed_store(monkeypatch)
    assert await co.get_access_token() is None and not co.is_connected()


@pytest.mark.asyncio
async def test_get_access_token_refreshes_when_expiry_absent(monkeypatch):
    store = _seed_store(monkeypatch)
    store["CLAY_OAUTH_ACCESS_TOKEN"] = "OLD"
    store["CLAY_OAUTH_REFRESH_TOKEN"] = "RT"
    store["CLAY_OAUTH_CLIENT_ID"] = "cid"
    # No CLAY_OAUTH_EXPIRES_AT in store — must trigger refresh

    async def fake_post(url, **k):
        return Resp(200, {"access_token": "REFRESHED", "refresh_token": "RT2", "expires_in": 3600})

    monkeypatch.setattr(co.http, "post", fake_post, raising=False)
    result = await co.get_access_token()
    assert result == "REFRESHED", f"Expected refreshed token, got {result!r} (should not return stale 'OLD')"
    assert store["CLAY_OAUTH_ACCESS_TOKEN"] == "REFRESHED"


@pytest.mark.asyncio
async def test_refresh_failure_clears_access_token(monkeypatch):
    store = _seed_store(monkeypatch)
    store["CLAY_OAUTH_REFRESH_TOKEN"] = "RT"
    store["CLAY_OAUTH_CLIENT_ID"] = "cid"
    store["CLAY_OAUTH_ACCESS_TOKEN"] = "STALE"

    async def fake_post(url, **k):
        return Resp(400, {"error": "invalid_grant"})

    monkeypatch.setattr(co.http, "post", fake_post, raising=False)
    assert await co.refresh() is None
    assert store.get("CLAY_OAUTH_NEEDS_RECONNECT") == "1" and co.needs_reconnect()
    assert store.get("CLAY_OAUTH_ACCESS_TOKEN") is None


def test_disconnect_clears(monkeypatch):
    store = _seed_store(monkeypatch)
    store.update({"CLAY_OAUTH_ACCESS_TOKEN": "AT", "CLAY_OAUTH_REFRESH_TOKEN": "RT"})
    co.disconnect()
    assert "CLAY_OAUTH_ACCESS_TOKEN" not in store and "CLAY_OAUTH_REFRESH_TOKEN" not in store
