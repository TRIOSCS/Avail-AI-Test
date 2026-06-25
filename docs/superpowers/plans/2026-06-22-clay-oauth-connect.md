# Clay OAuth "Connect" Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the AvailAI backend authenticate to Clay's OAuth-gated MCP (`api.clay.com/v3/mcp`) via a one-time interactive "Connect Clay" flow, then call it headless with an auto-refreshed token.

**Architecture:** Mirror the app's existing Azure AD OAuth (`app/routers/auth.py`), adapted for Clay's public-client + PKCE + dynamic client registration (DCR), `scope=mcp`. A token-lifecycle service holds encrypted tokens in the existing `ApiSource('clay_enrichment').credentials` store and auto-refreshes; the connector swaps its `Bearer <CLAY_API_KEY>` for `Bearer <oauth_access_token>`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0, httpx (shared `app/http_client.py`), Redis-backed `intel_cache`, pytest (`-n auto`, in-memory SQLite, `TESTING=1`).

## Global Constraints

- Run pytest: `TESTING=1 PYTHONPATH=<worktree> /root/availai/.venv/bin/python -m pytest`.
- Clay OAuth endpoints (verbatim, from `api.clay.com/.well-known/oauth-authorization-server`):
  `CLAY_AUTHORIZE_URL = "https://app.clay.com/oauth/authorize"`,
  `CLAY_TOKEN_URL = "https://api.clay.com/oauth/token"`,
  `CLAY_REGISTER_URL = "https://api.clay.com/oauth/register"`, `scope = "mcp"`, grant types `authorization_code` + `refresh_token`, `token_endpoint_auth_method = "none"` (public client + PKCE S256).
- Redirect URI = `f"{settings.app_url}/auth/clay/callback"` (app_url = `https://app.availai.net`).
- MCP endpoint = `https://api.clay.com/v3/mcp` (JSON-RPC over HTTPS; `Authorization: Bearer <access_token>`).
- Tokens stored ENCRYPTED via `credential_service.encrypt_value` in `ApiSource('clay_enrichment').credentials` under keys `CLAY_OAUTH_CLIENT_ID`, `CLAY_OAUTH_ACCESS_TOKEN`, `CLAY_OAUTH_REFRESH_TOKEN`, `CLAY_OAUTH_EXPIRES_AT` (ISO8601), `CLAY_OAUTH_NEEDS_RECONNECT`. Never log tokens.
- Fail-soft: not-connected / refresh-failed → connector returns `None`/`[]` (Clay skipped; blend continues). Never raises non-quota errors to callers.
- New files get a header comment (what/called-by/depends-on). loguru, never print. Routes admin-gated.
- Build behind the existing `clay_enrichment_enabled` flag. No DB migration (reuses `ApiSource.credentials` JSONB).

---

## File Structure

| File | Responsibility |
|---|---|
| `app/services/clay_oauth.py` (new) | Token lifecycle: DCR, PKCE, authorize-URL, code exchange, get/refresh access token, is_connected, disconnect, encrypted store. |
| `app/routers/clay_oauth.py` (new) | `/auth/clay/connect`, `/auth/clay/callback`, `/auth/clay/disconnect` (admin-only). |
| `app/connectors/clay_mcp.py` (modify) | Swap key auth → OAuth token; 401→refresh→retry; MCP handshake. |
| `app/main.py` (modify) | Mount `clay_oauth.router`. |
| `app/templates/htmx/partials/settings/api_keys.html` (modify) | Replace Clay key card with Connect card. |
| `app/routers/htmx_views.py` (modify) | `settings_api_keys_tab` context: `clay_connected`/`clay_needs_reconnect`. |

---

### Task 1: Clay OAuth token-lifecycle service

**Files:**
- Create: `app/services/clay_oauth.py`
- Test: `tests/test_clay_oauth.py`

**Interfaces:**
- Produces: `async register_client() -> str`; `pkce_pair() -> tuple[str,str]`; `build_authorize_url(client_id:str, state:str, code_challenge:str) -> str`; `async exchange_code(code:str, code_verifier:str, client_id:str) -> bool`; `async get_access_token() -> str|None`; `async refresh() -> str|None`; `is_connected() -> bool`; `needs_reconnect() -> bool`; `disconnect() -> None`. Storage keys per Global Constraints.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_clay_oauth.py
import base64, hashlib
import pytest
from app.services import clay_oauth as co

class Resp:
    def __init__(self, status, payload): self.status_code, self._p = status, payload
    def json(self): return self._p

def _seed_store(monkeypatch):
    store = {}
    monkeypatch.setattr(co, "_store", lambda upd: store.update({k: v for k, v in upd.items() if v is not None}) or [store.pop(k, None) for k, v in upd.items() if v is None])
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
    store = _seed_store(monkeypatch); store["CLAY_OAUTH_CLIENT_ID"] = "existing"
    cid = await co.register_client()
    assert cid == "existing"

@pytest.mark.asyncio
async def test_register_client_does_dcr(monkeypatch):
    store = _seed_store(monkeypatch)
    async def fake_post(url, **k): return Resp(201, {"client_id": "new-cid"})
    monkeypatch.setattr(co.http, "post", fake_post, raising=False)
    cid = await co.register_client()
    assert cid == "new-cid" and store["CLAY_OAUTH_CLIENT_ID"] == "new-cid"

@pytest.mark.asyncio
async def test_exchange_code_persists_tokens(monkeypatch):
    store = _seed_store(monkeypatch)
    async def fake_post(url, **k): return Resp(200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
    monkeypatch.setattr(co.http, "post", fake_post, raising=False)
    ok = await co.exchange_code("code", "verifier", "cid")
    assert ok and store["CLAY_OAUTH_ACCESS_TOKEN"] == "AT" and store["CLAY_OAUTH_REFRESH_TOKEN"] == "RT"
    assert "CLAY_OAUTH_EXPIRES_AT" in store and co.is_connected()

@pytest.mark.asyncio
async def test_get_access_token_returns_fresh(monkeypatch):
    from datetime import datetime, timedelta, timezone
    store = _seed_store(monkeypatch)
    store["CLAY_OAUTH_ACCESS_TOKEN"] = "AT"; store["CLAY_OAUTH_REFRESH_TOKEN"] = "RT"
    store["CLAY_OAUTH_EXPIRES_AT"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert await co.get_access_token() == "AT"

@pytest.mark.asyncio
async def test_get_access_token_refreshes_when_expired(monkeypatch):
    from datetime import datetime, timedelta, timezone
    store = _seed_store(monkeypatch)
    store["CLAY_OAUTH_ACCESS_TOKEN"] = "OLD"; store["CLAY_OAUTH_REFRESH_TOKEN"] = "RT"
    store["CLAY_OAUTH_CLIENT_ID"] = "cid"
    store["CLAY_OAUTH_EXPIRES_AT"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    async def fake_post(url, **k): return Resp(200, {"access_token": "NEW", "refresh_token": "RT2", "expires_in": 3600})
    monkeypatch.setattr(co.http, "post", fake_post, raising=False)
    assert await co.get_access_token() == "NEW"
    assert store["CLAY_OAUTH_ACCESS_TOKEN"] == "NEW" and store["CLAY_OAUTH_REFRESH_TOKEN"] == "RT2"

@pytest.mark.asyncio
async def test_refresh_failure_marks_needs_reconnect(monkeypatch):
    store = _seed_store(monkeypatch)
    store["CLAY_OAUTH_REFRESH_TOKEN"] = "RT"; store["CLAY_OAUTH_CLIENT_ID"] = "cid"
    async def fake_post(url, **k): return Resp(400, {"error": "invalid_grant"})
    monkeypatch.setattr(co.http, "post", fake_post, raising=False)
    assert await co.refresh() is None
    assert store.get("CLAY_OAUTH_NEEDS_RECONNECT") == "1" and co.needs_reconnect()

@pytest.mark.asyncio
async def test_get_access_token_none_when_absent(monkeypatch):
    _seed_store(monkeypatch)
    assert await co.get_access_token() is None and not co.is_connected()

def test_disconnect_clears(monkeypatch):
    store = _seed_store(monkeypatch); store.update({"CLAY_OAUTH_ACCESS_TOKEN":"AT","CLAY_OAUTH_REFRESH_TOKEN":"RT"})
    co.disconnect()
    assert "CLAY_OAUTH_ACCESS_TOKEN" not in store and "CLAY_OAUTH_REFRESH_TOKEN" not in store
```

- [ ] **Step 2: Run → FAIL** (`pytest tests/test_clay_oauth.py -q` → module missing).

- [ ] **Step 3: Implement `app/services/clay_oauth.py`**

```python
"""Clay OAuth token lifecycle (authorization_code + PKCE + DCR) for the headless MCP.

Clay's MCP (api.clay.com/v3/mcp) is OAuth-gated (no client_credentials grant), so the
backend holds an access+refresh token obtained via a one-time interactive login and
auto-refreshes it. Tokens are stored encrypted in ApiSource('clay_enrichment').

Called by: app/routers/clay_oauth.py (connect/callback), app/connectors/clay_mcp.py
(get_access_token). Depends on: app/http_client.py, app/services/credential_service,
app/database, app/models.ApiSource.
"""

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from loguru import logger

from app.config import settings
from app.database import SessionLocal
from app.http_client import http
from app.models import ApiSource
from app.services import credential_service as cs

CLAY_AUTHORIZE_URL = "https://app.clay.com/oauth/authorize"
CLAY_TOKEN_URL = "https://api.clay.com/oauth/token"
CLAY_REGISTER_URL = "https://api.clay.com/oauth/register"
CLAY_SCOPE = "mcp"
_SOURCE = "clay_enrichment"
_REFRESH_BUFFER = timedelta(minutes=5)


def _redirect_uri() -> str:
    return f"{settings.app_url}/auth/clay/callback"


def _store(updates: dict[str, str | None]) -> None:
    """Encrypt+persist (or delete when value is None) CLAY_OAUTH_* keys; bust cred cache."""
    db = SessionLocal()
    try:
        s = db.query(ApiSource).filter_by(name=_SOURCE).first()
        if s is None:
            s = ApiSource(name=_SOURCE, credentials={})
            db.add(s)
        creds = dict(s.credentials or {})
        for k, v in updates.items():
            if v is None:
                creds.pop(k, None)
            else:
                creds[k] = cs.encrypt_value(v)
        s.credentials = creds
        db.commit()
    finally:
        db.close()
    cs._cred_cache.clear()


def _load(key: str) -> str | None:
    return cs.get_credential_cached(_SOURCE, key)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_authorize_url(client_id: str, state: str, code_challenge: str) -> str:
    return f"{CLAY_AUTHORIZE_URL}?" + urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": _redirect_uri(),
        "scope": CLAY_SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })


async def register_client() -> str:
    existing = _load("CLAY_OAUTH_CLIENT_ID")
    if existing:
        return existing
    resp = await http.post(CLAY_REGISTER_URL, json={
        "client_name": "AvailAI",
        "redirect_uris": [_redirect_uri()],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": CLAY_SCOPE,
    }, timeout=15)
    if resp.status_code not in (200, 201):
        logger.error("Clay DCR failed: {}", resp.status_code)
        raise RuntimeError(f"Clay client registration failed: {resp.status_code}")
    cid = resp.json().get("client_id")
    if not cid:
        raise RuntimeError("Clay DCR response missing client_id")
    _store({"CLAY_OAUTH_CLIENT_ID": cid})
    return cid


def _persist_tokens(tok: dict) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(tok.get("expires_in", 3600)))
    updates: dict[str, str | None] = {
        "CLAY_OAUTH_ACCESS_TOKEN": tok.get("access_token"),
        "CLAY_OAUTH_EXPIRES_AT": expires_at.isoformat(),
        "CLAY_OAUTH_NEEDS_RECONNECT": None,
    }
    if tok.get("refresh_token"):  # rotation-aware: keep old if not returned
        updates["CLAY_OAUTH_REFRESH_TOKEN"] = tok["refresh_token"]
    _store(updates)


async def exchange_code(code: str, code_verifier: str, client_id: str) -> bool:
    resp = await http.post(CLAY_TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _redirect_uri(),
        "client_id": client_id,
        "code_verifier": code_verifier,
    }, timeout=15)
    if resp.status_code != 200 or not resp.json().get("access_token"):
        logger.error("Clay code exchange failed: {}", resp.status_code)
        return False
    _persist_tokens(resp.json())
    return True


async def refresh() -> str | None:
    rt = _load("CLAY_OAUTH_REFRESH_TOKEN")
    cid = _load("CLAY_OAUTH_CLIENT_ID")
    if not rt or not cid:
        return None
    resp = await http.post(CLAY_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": cid,
    }, timeout=15)
    if resp.status_code != 200 or not resp.json().get("access_token"):
        logger.warning("Clay refresh failed ({}) — needs reconnect", resp.status_code)
        _store({"CLAY_OAUTH_ACCESS_TOKEN": None, "CLAY_OAUTH_NEEDS_RECONNECT": "1"})
        return None
    _persist_tokens(resp.json())
    return _load("CLAY_OAUTH_ACCESS_TOKEN")


async def get_access_token() -> str | None:
    at = _load("CLAY_OAUTH_ACCESS_TOKEN")
    exp = _load("CLAY_OAUTH_EXPIRES_AT")
    if not at:
        return await refresh() if _load("CLAY_OAUTH_REFRESH_TOKEN") else None
    try:
        if exp and datetime.fromisoformat(exp) - _REFRESH_BUFFER <= datetime.now(timezone.utc):
            return await refresh()
    except ValueError:
        return await refresh()
    return at


def is_connected() -> bool:
    return bool(_load("CLAY_OAUTH_REFRESH_TOKEN")) and not needs_reconnect()


def needs_reconnect() -> bool:
    return _load("CLAY_OAUTH_NEEDS_RECONNECT") == "1"


def disconnect() -> None:
    _store({k: None for k in (
        "CLAY_OAUTH_CLIENT_ID", "CLAY_OAUTH_ACCESS_TOKEN", "CLAY_OAUTH_REFRESH_TOKEN",
        "CLAY_OAUTH_EXPIRES_AT", "CLAY_OAUTH_NEEDS_RECONNECT",
    )})
```

- [ ] **Step 4: Run → PASS** (`pytest tests/test_clay_oauth.py -q`). Pristine output.
- [ ] **Step 5: Commit** — `git add app/services/clay_oauth.py tests/test_clay_oauth.py && git commit -m "feat(clay-oauth): token lifecycle service (DCR+PKCE+refresh)"`

---

### Task 2: Clay OAuth router (connect / callback / disconnect)

**Files:**
- Create: `app/routers/clay_oauth.py`
- Modify: `app/main.py` (mount router)
- Test: `tests/test_clay_oauth_router.py`

**Interfaces:**
- Consumes: `clay_oauth.{register_client,pkce_pair,build_authorize_url,exchange_code,disconnect}`; `intel_cache.{set_cached,get_cached}`.
- Produces: routes `GET /auth/clay/connect`, `GET /auth/clay/callback`, `POST /auth/clay/disconnect`.

- [ ] **Step 1: Write failing tests** (mirror the existing admin-client test pattern in `tests/test_settings_api_keys_cards.py` / `tests/test_credential_management.py` — override `require_user`/`require_admin`/`require_settings_access`; do NOT follow redirects):

```python
# tests/test_clay_oauth_router.py
import pytest
# Build an admin TestClient the same way tests/test_settings_api_keys_cards.py does
# (reuse its _make_admin_client helper pattern: override get_db + auth deps).

def test_connect_redirects_to_clay(admin_client, monkeypatch):
    import app.routers.clay_oauth as r
    async def fake_register(): return "cid"
    monkeypatch.setattr(r.clay_oauth, "register_client", fake_register)
    resp = admin_client.get("/auth/clay/connect", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"].startswith("https://app.clay.com/oauth/authorize")
    assert "scope=mcp" in resp.headers["location"]

def test_callback_rejects_unknown_state(admin_client):
    resp = admin_client.get("/auth/clay/callback?code=x&state=nope", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "error" in resp.headers["location"]

def test_callback_happy_path_stores(admin_client, monkeypatch):
    import app.routers.clay_oauth as r
    from app.cache.intel_cache import set_cached
    captured = {}
    async def fake_register(): return "cid"
    async def fake_exchange(code, verifier, cid): captured.update(code=code, verifier=verifier, cid=cid); return True
    monkeypatch.setattr(r.clay_oauth, "register_client", fake_register)
    monkeypatch.setattr(r.clay_oauth, "exchange_code", fake_exchange)
    # seed state→verifier as /connect would
    set_cached("clay:oauth:state:STATE1", {"verifier": "VER", "client_id": "cid"}, ttl_days=0.01)
    resp = admin_client.get("/auth/clay/callback?code=CODE&state=STATE1", follow_redirects=False)
    assert resp.status_code in (302, 307) and "clay=connected" in resp.headers["location"]
    assert captured == {"code": "CODE", "verifier": "VER", "cid": "cid"}

def test_routes_admin_gated(client):  # plain non-admin client
    assert client.get("/auth/clay/connect", follow_redirects=False).status_code in (401, 403, 302)
```

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement `app/routers/clay_oauth.py`** (admin dep mirrors how other Settings routes gate — use the same dependency `htmx_views.settings_api_keys_tab` uses; confirm its name, e.g. `require_settings_access` or `require_admin`):

```python
"""Clay OAuth connect/callback/disconnect routes (admin-only).

One-time interactive flow to authorize the AvailAI backend against Clay's MCP.
Called by: the Settings → API Keys "Connect Clay" card. Depends on:
app/services/clay_oauth, app/cache/intel_cache (state store), app/dependencies (admin gate).
"""

import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from loguru import logger

from app.cache.intel_cache import get_cached, set_cached
from app.dependencies import require_admin  # match the dep used by settings_api_keys_tab
from app.services import clay_oauth

router = APIRouter()
_SETTINGS_URL = "/v2/partials/settings/api-keys"
_STATE_PREFIX = "clay:oauth:state:"
_STATE_TTL_DAYS = 10 / 1440  # 10 minutes


@router.get("/auth/clay/connect")
async def connect(request: Request, _=Depends(require_admin)):
    try:
        client_id = await clay_oauth.register_client()
    except Exception as e:
        logger.error("Clay connect (DCR) failed: {}", e)
        return RedirectResponse(f"{_SETTINGS_URL}?clay=error", status_code=302)
    verifier, challenge = clay_oauth.pkce_pair()
    state = secrets.token_urlsafe(32)
    set_cached(f"{_STATE_PREFIX}{state}", {"verifier": verifier, "client_id": client_id}, ttl_days=_STATE_TTL_DAYS)
    return RedirectResponse(clay_oauth.build_authorize_url(client_id, state, challenge), status_code=302)


@router.get("/auth/clay/callback")
async def callback(request: Request, code: str = "", state: str = "", _=Depends(require_admin)):
    err = request.query_params.get("error")
    if err or not code or not state:
        logger.warning("Clay callback error/missing params: {}", err or "no code/state")
        return RedirectResponse(f"{_SETTINGS_URL}?clay=error", status_code=302)
    stash = get_cached(f"{_STATE_PREFIX}{state}")
    if not stash:
        logger.warning("Clay callback unknown/expired state")
        return RedirectResponse(f"{_SETTINGS_URL}?clay=error", status_code=302)
    set_cached(f"{_STATE_PREFIX}{state}", None, ttl_days=_STATE_TTL_DAYS)  # one-time use
    ok = await clay_oauth.exchange_code(code, stash["verifier"], stash["client_id"])
    return RedirectResponse(f"{_SETTINGS_URL}?clay={'connected' if ok else 'error'}", status_code=302)


@router.post("/auth/clay/disconnect")
async def disconnect(request: Request, _=Depends(require_admin)):
    clay_oauth.disconnect()
    return RedirectResponse(f"{_SETTINGS_URL}?clay=disconnected", status_code=302)
```

In `app/main.py`, mirror the existing router mounts: `from app.routers import clay_oauth as clay_oauth_router` and `app.include_router(clay_oauth_router.router)`. (Check exact import style used for `auth`/other routers and match it.)
> Note: if `intel_cache.set_cached` cannot store `None` to delete, use a 1-second TTL re-set instead; verify against the real signature.

- [ ] **Step 4: Run → PASS.** Confirm admin gating works with the test fixtures.
- [ ] **Step 5: Commit** — `git add app/routers/clay_oauth.py app/main.py tests/test_clay_oauth_router.py && git commit -m "feat(clay-oauth): connect/callback/disconnect routes"`

---

### Task 3: Rewire `clay_mcp.py` to OAuth token + handshake

**Files:**
- Modify: `app/connectors/clay_mcp.py`
- Test: `tests/test_clay_mcp_connector.py` (update auth seam)

**Interfaces:**
- Consumes: `clay_oauth.get_access_token()`, `clay_oauth.refresh()`.
- Produces: unchanged public API `enrich_company(domain)`, `find_contacts(domain,title_filter,limit,want_email)`.

- [ ] **Step 1: Update tests** — the existing tests monkeypatch `clay_mcp._resolve_key`. Replace those with monkeypatching `clay_mcp._access_token` (the new async seam) and `clay_mcp._mcp_call`. Add:

```python
@pytest.mark.asyncio
async def test_enrich_company_none_when_not_connected(monkeypatch):
    from app.connectors import clay_mcp
    async def no_token(): return None
    monkeypatch.setattr(clay_mcp, "_access_token", no_token)
    assert await clay_mcp.enrich_company("arrow.com") is None

@pytest.mark.asyncio
async def test_mcp_call_refreshes_on_401(monkeypatch):
    from app.connectors import clay_mcp
    calls = {"n": 0, "refreshed": 0}
    async def tok(): return "AT"
    async def refresh(): calls["refreshed"] += 1; return "AT2"
    monkeypatch.setattr(clay_mcp.clay_oauth, "get_access_token", tok)
    monkeypatch.setattr(clay_mcp.clay_oauth, "refresh", refresh)
    class R:
        def __init__(s, code): s.status_code = code
        def json(s): return {"result": {"structuredContent": {"ok": True}}}
    async def fake_post(url, **k):
        calls["n"] += 1
        return R(401) if calls["n"] == 1 else R(200)
    monkeypatch.setattr(clay_mcp.http, "post", fake_post, raising=False)
    out = await clay_mcp._mcp_call("find-and-enrich-company", {"companyIdentifier": "arrow.com"})
    assert calls["refreshed"] == 1 and calls["n"] == 2 and out == {"ok": True}
```
Keep the existing company-mapping, domain-filter, quota, and poll tests (they already pass; only the auth seam changes — point them at `_access_token`/`_mcp_call` monkeypatches instead of `_resolve_key`).

- [ ] **Step 2: Run → FAIL** (no `_access_token`; old `_resolve_key` references break).
- [ ] **Step 3: Implement.** In `clay_mcp.py`:
  - Replace `from app.services.credential_service import get_credential_cached` with `from app.services import clay_oauth`.
  - Delete `_resolve_key`; add:
    ```python
    async def _access_token() -> str | None:
        """Valid Clay OAuth access token (auto-refreshed), or None if not connected."""
        return await clay_oauth.get_access_token()
    ```
  - Rewrite `_mcp_call` to use the token + 401-refresh-retry:
    ```python
    async def _mcp_call(tool: str, args: dict) -> dict:
        token = await _access_token()
        if not token:
            return {}

        async def _post(tok: str):
            return await http.post(MCP_URL, headers={
                "Authorization": f"Bearer {tok}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": tool, "arguments": args}}, timeout=40)

        resp = await _post(token)
        if resp.status_code == 401:  # token rejected → one refresh + retry
            new = await clay_oauth.refresh()
            if not new:
                logger.warning("Clay MCP 401 and refresh failed — not connected")
                return {}
            resp = await _post(new)
        if resp.status_code in _QUOTA_STATUSES:
            raise ProviderQuotaError(f"Clay MCP quota/rate-limit: {resp.status_code}")
        if resp.status_code != 200:
            logger.warning("Clay MCP {} failed: {}", tool, resp.status_code)
            return {}
        payload = resp.json()
        result = payload.get("result", payload)
        content = result.get("structuredContent") or result
        return content if isinstance(content, dict) else {}
    ```
  - In `enrich_company` and `find_contacts`, replace the `if not _resolve_key():` guard with `if not await _access_token():`.
  - Update the module docstring (Clay is OAuth now, not key).

- [ ] **Step 4: Run → PASS** (`pytest tests/test_clay_mcp_connector.py -q`).
- [ ] **Step 5: Commit** — `git add app/connectors/clay_mcp.py tests/test_clay_mcp_connector.py && git commit -m "feat(clay-oauth): clay_mcp uses OAuth token + 401-refresh-retry"`

---

### Task 4: Settings "Connect Clay" card

**Files:**
- Modify: `app/templates/htmx/partials/settings/api_keys.html` (the Clay card)
- Modify: `app/routers/htmx_views.py` (`settings_api_keys_tab` context)
- Test: `tests/test_settings_api_keys_cards.py` (extend)

- [ ] **Step 1: Write failing test** (extend the existing file):

```python
def test_clay_card_shows_connect_when_disconnected(admin_client, monkeypatch):
    import app.routers.htmx_views as v
    monkeypatch.setattr(v.clay_oauth, "is_connected", lambda: False)
    monkeypatch.setattr(v.clay_oauth, "needs_reconnect", lambda: False)
    html = admin_client.get("/v2/partials/settings/api-keys").text
    assert "/auth/clay/connect" in html and "Connect Clay" in html
    assert "CLAY_API_KEY" not in html  # the old key input is gone

def test_clay_card_shows_connected(admin_client, monkeypatch):
    import app.routers.htmx_views as v
    monkeypatch.setattr(v.clay_oauth, "is_connected", lambda: True)
    monkeypatch.setattr(v.clay_oauth, "needs_reconnect", lambda: False)
    html = admin_client.get("/v2/partials/settings/api-keys").text
    assert "Connected" in html and "/auth/clay/disconnect" in html
```

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3a: Context** — in `htmx_views.py`, add `from app.services import clay_oauth` and in `settings_api_keys_tab` context: `"clay_connected": clay_oauth.is_connected(), "clay_needs_reconnect": clay_oauth.needs_reconnect(),`. Remove the now-unused `"clay_api_key": _field("clay_enrichment","CLAY_API_KEY")` line.
- [ ] **Step 3b: Card** — replace the Clay card body in `api_keys.html` (keep the outer `bg-white border ... rounded-xl p-5` shell + the `<h3>Clay</h3>` + description). Replace the password input + Save with:

```html
    <div class="flex items-center gap-3">
      {% if clay_connected %}
        <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium bg-emerald-50 text-emerald-700">
          <span class="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>Connected
        </span>
        <a href="/auth/clay/connect" class="btn-md bg-brand-600 text-white font-medium rounded-lg hover:bg-brand-700">Reconnect</a>
        <button type="button" hx-post="/auth/clay/disconnect" hx-swap="none"
                class="btn-md border border-brand-200 text-brand-700 font-medium rounded-lg hover:bg-brand-50">Disconnect</button>
      {% else %}
        <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium {{ 'bg-amber-50 text-amber-700' if clay_needs_reconnect else 'bg-brand-50 text-brand-600' }}">
          <span class="w-1.5 h-1.5 rounded-full {{ 'bg-amber-400' if clay_needs_reconnect else 'bg-brand-300' }}"></span>{{ 'Needs reconnect' if clay_needs_reconnect else 'Not connected' }}
        </span>
        <a href="/auth/clay/connect" class="btn-md bg-brand-600 text-white font-medium rounded-lg hover:bg-brand-700">Connect Clay</a>
      {% endif %}
    </div>
```
Description text: "OAuth connection to Clay's MCP — enrichment waterfall + contact discovery." Use the `<a href>` for connect/reconnect (full-page OAuth redirect, NOT htmx). Use `.btn-md` (the static-guard ratchet forbids inline `px-/py-` buttons).

- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git add app/templates/htmx/partials/settings/api_keys.html app/routers/htmx_views.py tests/test_settings_api_keys_cards.py && git commit -m "feat(clay-oauth): Settings Connect Clay card"`

---

### Task 5: Docs + full suite + review

**Files:** Modify `docs/APP_MAP_INTERACTIONS.md` (Clay is OAuth-connected, not key-based).

- [ ] **Step 1:** Update the Clay section in `docs/APP_MAP_INTERACTIONS.md` (OAuth Connect flow; tokens in credential store; auto-refresh; MCP endpoint).
- [ ] **Step 2:** `pre-commit run --all-files` then full suite `TESTING=1 PYTHONPATH=$PWD /root/availai/.venv/bin/python -m pytest tests/ -q` → green (re-run twice if docformatter mutates).
- [ ] **Step 3:** `/qa` + PR-review fleet; fix ALL Critical/Important findings.
- [ ] **Step 4:** Commit docs — `git add docs/APP_MAP_INTERACTIONS.md && git commit -m "docs(clay-oauth): map OAuth Connect flow"`
- [ ] **Step 5 (deploy, not code):** merge to main → `./deploy.sh` → **one-time interactive Connect** (user clicks Connect Clay, logs in) → live-verify a real Clay enrichment + token auto-refresh.

---

## Self-Review

**Spec coverage:** §4.1 service → Task 1; §4.2 router → Task 2; §4.3 clay_mcp rewire → Task 3; §4.4 Settings card → Task 4; §4.5 config (keep flag, drop key input) → Task 4; §7 testing → every task; §8 rollout/docs → Task 5. All covered.

**Placeholder scan:** No TBDs; two implementer notes (admin-dep name confirmation; intel_cache None-delete fallback; main.py mount style) are explicit "verify against real code" instructions, not gaps.

**Type consistency:** `get_access_token`/`refresh`/`is_connected`/`needs_reconnect`/`disconnect`/`exchange_code`/`register_client`/`build_authorize_url`/`pkce_pair` used consistently across Tasks 1–4; `_access_token`/`_mcp_call` seams consistent in Task 3; storage keys (`CLAY_OAUTH_*`) consistent service↔tests.
