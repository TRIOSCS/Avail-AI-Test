# Design: "Connect Clay" OAuth flow (headless Clay MCP via authorization-code + PKCE)

- **Date:** 2026-06-22
- **Status:** Draft — awaiting user review
- **Builds on:** the merged enrichment-blending feature (main 859a35ca) — specifically `app/connectors/clay_mcp.py`.

## 1. Background

The enrichment-blending feature shipped a backend Clay MCP connector that authenticates with a static `CLAY_API_KEY` as `Authorization: Bearer`. A live spike proved that assumption **wrong**: `https://api.clay.com/v3/mcp` is **OAuth-gated**, returning a 401 OAuth challenge to any Bearer-key request. Clay's authorization-server metadata
(`https://api.clay.com/.well-known/oauth-authorization-server`) confirms:

```
issuer                  = https://api.clay.com
authorization_endpoint  = https://app.clay.com/oauth/authorize
token_endpoint          = https://api.clay.com/oauth/token
registration_endpoint   = https://api.clay.com/oauth/register
grant_types_supported   = ['authorization_code', 'refresh_token']     # NO client_credentials
token_endpoint_auth_methods_supported = ['client_secret_post', 'client_secret_basic', 'none']
scopes_supported        = ['mcp']
```

There is **no machine-to-machine grant** — the only way to obtain a token is the interactive `authorization_code` flow (a human logs in at `app.clay.com/oauth/authorize`). This feature adds that flow so the AvailAI backend can hold a Clay token and call the MCP **headless** thereafter (auto-refreshing). Works on the user's current **Launch** plan (no upgrade).

The app already implements this exact shape for Azure AD in `app/routers/auth.py` (`/auth/login`→authorize redirect with `state`→`/auth/callback`→httpx token exchange→DB token store→proactive refresh in `dependencies.py`). We mirror it, adapted for Clay's **public client + PKCE + dynamic client registration (DCR)**.

## 2. Goals & non-goals

### Goals
- A one-time interactive "Connect Clay" OAuth flow (DCR → authorize → callback → encrypted token store).
- Headless Clay MCP calls afterward via the stored access token, **auto-refreshed** (rotation-aware).
- Rewire `clay_mcp.py` from `Bearer <CLAY_API_KEY>` to `Bearer <oauth_access_token>` with a 401→refresh→retry.
- Settings → API Keys "Connect Clay" card (Connected/Not-connected + Connect/Reconnect/Disconnect).
- Fail-soft: when not connected / refresh fails, Clay is skipped and the blend continues on the other providers.

### Non-goals
- Per-user Clay connections — this is a **single app-wide** connection (matches the data-source/API-keys model; correct for this single-user app).
- A confidential-client (client_secret) flow — we use a **public client + PKCE** (`token_endpoint_auth_method=none`), which Clay's metadata supports and is the standard for MCP.
- Changing Clay enrichment behavior (sync firmographics + bounded email poll stays as built).
- Reviving the deleted webhook path or any non-MCP Clay surface.

## 3. Architecture

Three units with clear boundaries, mirroring the Azure OAuth split:

```
Settings "Connect Clay" card ──▶ GET /auth/clay/connect ──▶ DCR(once) + PKCE + state
                                                              └─▶ redirect app.clay.com/oauth/authorize?scope=mcp
   (user logs into Clay) ◀───────────────────────────────────────────────┘
        │ redirect back
        ▼
GET /auth/clay/callback?code&state ──▶ exchange code+verifier @ token_endpoint ──▶ encrypted token store
                                                                                      (ApiSource clay_enrichment)
app/connectors/clay_mcp.py ──▶ clay_oauth.get_access_token() (auto-refresh) ──▶ Bearer ──▶ api.clay.com/v3/mcp
```

## 4. Components

### 4.1 `app/services/clay_oauth.py` (new) — token lifecycle
Constants from the metadata above (`CLAY_AUTHORIZE_URL`, `CLAY_TOKEN_URL`, `CLAY_REGISTER_URL`, `CLAY_MCP_SCOPE="mcp"`). Redirect URI: `f"{settings.app_url}/auth/clay/callback"`.

Token storage: encrypted in `ApiSource(name="clay_enrichment").credentials` via `credential_service.encrypt_value` / read via the same decrypt path. Keys:
`CLAY_OAUTH_CLIENT_ID`, `CLAY_OAUTH_ACCESS_TOKEN`, `CLAY_OAUTH_REFRESH_TOKEN`, `CLAY_OAUTH_EXPIRES_AT` (ISO8601 UTC).

Functions:
- `async register_client() -> str` — POST `registration_endpoint` with `{redirect_uris:[redirect], grant_types:["authorization_code","refresh_token"], response_types:["code"], token_endpoint_auth_method:"none", scope:"mcp", client_name:"AvailAI"}`. Persist + return `client_id`. Idempotent: reuse stored `CLAY_OAUTH_CLIENT_ID` if present.
- `build_authorize_url(client_id, state, code_challenge) -> str` — `{CLAY_AUTHORIZE_URL}?response_type=code&client_id=…&redirect_uri=…&scope=mcp&state=…&code_challenge=…&code_challenge_method=S256`.
- `async exchange_code(code, code_verifier, client_id) -> dict` — POST `token_endpoint` `grant_type=authorization_code` (+ code, redirect_uri, client_id, code_verifier). Returns `{access_token, refresh_token, expires_in}`. Persist (compute `expires_at = now + expires_in`).
- `async get_access_token() -> str | None` — return a valid access token; if missing → None; if expired/within 5-min buffer → `refresh()`; never raises.
- `async refresh() -> str | None` — POST `token_endpoint` `grant_type=refresh_token` (+ refresh_token, client_id). Persist the new access token, new `expires_at`, and the **rotated refresh_token if returned** (else keep the old). On failure (revoked/expired) → clear access token, leave a `CLAY_OAUTH_NEEDS_RECONNECT=1` marker, return None.
- `is_connected() -> bool` — true when a refresh token is stored and no needs-reconnect marker.
- `disconnect() -> None` — delete all `CLAY_OAUTH_*` from the credential store.

PKCE helpers (`secrets.token_urlsafe` verifier; `base64url(sha256(verifier))` challenge) — mirror `auth.py`'s `hashlib`/`base64` usage.

### 4.2 `app/routers/clay_oauth.py` (new, admin-only — `require_admin`/`require_settings_access`)
- `GET /auth/clay/connect` — `register_client()` (or reuse); generate `state` + PKCE; store `{state: code_verifier}` in the intel cache (Redis→PG) with a short TTL (10 min); `RedirectResponse(build_authorize_url(...))`.
- `GET /auth/clay/callback?code,state` — pop `code_verifier` by `state` (reject unknown/expired state); `exchange_code(...)`; on success `RedirectResponse("/v2/...settings api-keys?clay=connected")`; on error redirect with an error flag. Never logs tokens.
- `POST /auth/clay/disconnect` — `disconnect()`, return the refreshed Settings card / redirect.
Register the router in `app/main.py` (mirror auth router mount).

### 4.3 Rewire `app/connectors/clay_mcp.py`
Replace `_resolve_key()` (and the `CLAY_API_KEY` Bearer) with `clay_oauth.get_access_token()`:
- `enrich_company`/`find_contacts` short-circuit to `None`/`[]` when `get_access_token()` is None (not connected) — same shape as today's key-absent guard.
- `_mcp_call(tool, args)` sets `Authorization: Bearer <access_token>`; on a `401` response, call `clay_oauth.refresh()` once and retry; if still 401, log + raise/degrade (treated like not-connected, fail-soft). Keep the JSON-RPC `initialize` → `notifications/initialized` → `tools/call` handshake (the spike showed the endpoint speaks MCP-over-HTTP; the connector must complete the handshake, which the original Bearer-key version did not fully do — add it here).
- Quota (402/429) handling and the sync-company / polled-email logic stay as built.

### 4.4 Settings → API Keys card (`app/templates/htmx/partials/settings/api_keys.html` + `htmx_views.py`)
Replace the Clay **API-key input** card with a **Connect card**: shows `Connected` (emerald) when `clay_oauth.is_connected()` else `Not connected` (amber, or "Needs reconnect" if the marker is set); a **Connect / Reconnect** link-button to `GET /auth/clay/connect` (full-page navigation, since OAuth redirects the browser — NOT an htmx swap) and a **Disconnect** button (`POST /auth/clay/disconnect`). Add the connected-state to the `settings_api_keys_tab` context via `clay_oauth.is_connected()`.

### 4.5 Config
Keep `clay_enrichment_enabled` (gates whether Clay runs at all). No `CLAY_API_KEY` needed anymore (remove its Settings input; the env/config field may remain unused/deprecated — leave the config attribute to avoid churn, but the UI no longer collects it).

## 5. Data flow
1. Admin clicks **Connect Clay** → `/auth/clay/connect` → DCR (once) → browser redirected to Clay, logs in, approves `mcp` scope.
2. Clay redirects to `/auth/clay/callback?code&state` → app validates `state`, exchanges `code`+`code_verifier` → stores encrypted access+refresh tokens + expiry + client_id.
3. Enrichment: `clay_mcp` → `get_access_token()` (auto-refresh) → `Bearer` → MCP `initialize`+`tools/call`.

## 6. Error handling (fail-soft)
- Not connected / `get_access_token()` None → Clay skipped; blend continues (Explorium/Apollo/AI). No error to the user-facing enrichment.
- Access token expired → transparent refresh.
- Refresh fails (revoked/expired) → set `CLAY_OAUTH_NEEDS_RECONNECT`, Settings shows "Reconnect Clay"; Clay stays skipped until reconnected.
- Callback with bad/expired `state` → reject (CSRF protection), redirect with error.
- Tokens never logged; stored encrypted.

## 7. Testing (TDD)
- `clay_oauth`: DCR request shape + client_id persist/reuse; `build_authorize_url` params (scope=mcp, S256); `exchange_code` persists tokens+expiry; `get_access_token` returns stored when fresh, refreshes when expired (mock clock via injected `now`/passing expires_at), returns None when absent; `refresh` persists rotated refresh token, sets needs-reconnect on failure; `disconnect` clears keys. Mock httpx.
- `clay_oauth` PKCE: challenge == base64url(sha256(verifier)).
- `clay_mcp`: uses access token; 401 → single refresh+retry; not-connected → None/[]; handshake order (initialize→initialized→tools/call). Mock `_mcp_call`/`get_access_token`.
- router: `/auth/clay/connect` redirects to Clay with state stored; `/auth/clay/callback` rejects unknown state, happy-path stores tokens + redirects; admin-gated (non-admin → 403); `/auth/clay/disconnect` clears. Use the existing admin test client pattern.
- Settings card: renders Connected vs Not-connected vs Needs-reconnect states.
- Full suite green; `/qa`; PR-review fleet.

## 8. Rollout
Build behind the existing `clay_enrichment_enabled` flag. Merge to main, deploy (no migration — token storage reuses the existing `ApiSource.credentials` JSONB). Then the **one-time interactive step (user)**: click **Connect Clay** in Settings, log into Clay, approve. Then I live-verify a real Clay enrichment (firmographics on a real domain) through the OAuth'd MCP, and confirm token auto-refresh.

## 9. Open risks
1. **Refresh-token longevity unknown** — if Clay rotates/expires refresh tokens aggressively, periodic reconnect is needed (surfaced in Settings; fail-soft meanwhile). Acceptable for single-user staging.
2. **DCR acceptance** — Clay must accept our dynamic registration + redirect_uri; if DCR is restricted, fall back to a manually pre-registered client_id (config). Validate during build with a live DCR call.
3. **MCP handshake nuances** — Streamable-HTTP MCP may require the `mcp-session-id` header from `initialize` on subsequent calls and SSE-framed responses; the connector must handle both (the spike captured the 401 path; the connected path is validated post-Connect).
4. **Redirect during htmx** — the Connect button must be a full-page navigation, not an htmx swap (OAuth redirects the top-level browser).

## 10. File list
- **New:** `app/services/clay_oauth.py`, `app/routers/clay_oauth.py`, `tests/test_clay_oauth.py`, `tests/test_clay_oauth_router.py`.
- **Modified:** `app/connectors/clay_mcp.py` (OAuth token auth + 401-refresh-retry + handshake), `app/main.py` (mount router), `app/templates/htmx/partials/settings/api_keys.html` + `app/routers/htmx_views.py` (Connect card + context), `tests/test_clay_mcp_connector.py` (auth seam update).
- **Docs:** update `docs/APP_MAP_INTERACTIONS.md` (Clay now OAuth, not key).
