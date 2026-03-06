# Security & Operational Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix 6 audit items — OAuth CSRF, encryption fail-closed, 401 redirect, proxy headers, dev CSP, and model config unification.

**Architecture:** Each task is independent (no shared code dependencies). Execute in 3 phases of 2 parallel subagents each. Each subagent modifies its target files + updates tests.

**Tech Stack:** FastAPI, Starlette sessions, Fernet encryption, SlowAPI, Uvicorn, Vite

---

## Phase 1 — Security

### Task 1: OAuth `state` parameter + URL encoding

**Files:**
- Modify: `app/routers/auth.py:66-72` (login), `app/routers/auth.py:78-163` (callback)
- Test: `tests/test_routers_auth.py`

**Step 1: Write the failing tests**

Add to `tests/test_routers_auth.py`:

```python
# At top of file, add import:
from urllib.parse import parse_qs, urlparse

# ── OAuth State Tests ────────────────────────────────────────────────

def test_login_includes_state_param(auth_client):
    """GET /auth/login includes a state parameter in the redirect URL."""
    resp = auth_client.get("/auth/login", follow_redirects=False)
    location = resp.headers["location"]
    parsed = urlparse(location)
    params = parse_qs(parsed.query)
    assert "state" in params, "Missing OAuth state parameter"
    assert len(params["state"][0]) >= 32, "State token too short"


def test_login_url_encodes_scope(auth_client):
    """GET /auth/login properly URL-encodes the scope parameter."""
    resp = auth_client.get("/auth/login", follow_redirects=False)
    location = resp.headers["location"]
    # Spaces in scope should be encoded as + or %20, not raw spaces
    assert " " not in location.split("?", 1)[1], "Query string contains unencoded spaces"


@patch("app.routers.auth.http")
def test_callback_validates_state(mock_http, auth_client):
    """Callback rejects request when state param doesn't match session."""
    resp = auth_client.get(
        "/auth/callback?code=test-code&state=wrong-state",
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307)
    # Should redirect without exchanging token (mock_http.post not called)
    mock_http.post.assert_not_called()


@patch("app.routers.auth.http")
def test_callback_missing_state_rejected(mock_http, auth_client):
    """Callback rejects request when state param is missing entirely."""
    resp = auth_client.get(
        "/auth/callback?code=test-code",
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307)
    mock_http.post.assert_not_called()
```

**Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_auth.py::test_login_includes_state_param tests/test_routers_auth.py::test_login_url_encodes_scope tests/test_routers_auth.py::test_callback_validates_state tests/test_routers_auth.py::test_callback_missing_state_rejected -v`
Expected: FAIL (no state param in URL, no validation in callback)

**Step 3: Implement OAuth state + URL encoding**

In `app/routers/auth.py`:

```python
# Add imports at top:
import secrets
from urllib.parse import urlencode

# Replace login() (lines 66-72):
@router.get("/auth/login")
async def login(request: Request):
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    params = urlencode({
        "client_id": settings.azure_client_id,
        "response_type": "code",
        "redirect_uri": f"{settings.app_url}/auth/callback",
        "scope": SCOPES,
        "response_mode": "query",
        "state": state,
    })
    return RedirectResponse(f"{AZURE_AUTH}/authorize?{params}")

# In callback(), add state validation after the `if not code:` check (line 81):
# Add state parameter:
@router.get("/auth/callback")
@limiter.limit("10/minute")
async def callback(request: Request, code: str = "", state: str = "", db: Session = Depends(get_db)):
    if not code:
        return RedirectResponse("/")
    # Validate OAuth state (CSRF protection)
    expected_state = request.session.pop("oauth_state", None)
    if not expected_state or state != expected_state:
        logger.warning("OAuth callback state mismatch (possible CSRF)")
        return RedirectResponse("/")
    # ... rest of callback unchanged
```

**Step 4: Fix existing tests that now need state**

The existing callback tests (e.g. `test_callback_success_new_user`) send requests without state, so they'll now fail. Update the `auth_client` fixture or test helper to go through the login flow first to get a valid state, OR patch the session.

Simpler approach: add a helper that sets up the session state:

```python
def _set_oauth_state(client, state="test-state"):
    """Inject oauth_state into the test client session."""
    # Use the login endpoint to set state, then extract it
    # Or directly: in test mode, we can set session via cookie
    # Simplest: patch the session in each callback test
    pass
```

Actually, the cleanest approach: in each callback test, make the test client first hit `/auth/login` to populate the session, then extract the state from the redirect URL and pass it to `/auth/callback`:

```python
def _get_oauth_state(client):
    """Hit /auth/login and extract the state param from redirect."""
    resp = client.get("/auth/login", follow_redirects=False)
    from urllib.parse import parse_qs, urlparse
    parsed = urlparse(resp.headers["location"])
    params = parse_qs(parsed.query)
    return params["state"][0]
```

Update ALL existing callback tests to use `state=_get_oauth_state(auth_client)` in the query string. For example:

```python
@patch("app.routers.auth.http")
def test_callback_success_new_user(mock_http, auth_client, db_session):
    mock_http.post = AsyncMock(return_value=_mock_token_response())
    mock_http.get = AsyncMock(return_value=_mock_graph_me())
    state = _get_oauth_state(auth_client)
    resp = auth_client.get(f"/auth/callback?code=test-auth-code&state={state}", follow_redirects=False)
    assert resp.status_code in (302, 307)
    # ... rest unchanged
```

Apply this pattern to ALL callback tests in the file.

**Step 5: Run all auth tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_auth.py -v`
Expected: ALL PASS

**Step 6: Commit**

```bash
git add app/routers/auth.py tests/test_routers_auth.py
git commit -m "security: add OAuth state param + URL encoding for CSRF protection"
```

---

### Task 2: Encryption fail-closed

**Files:**
- Modify: `app/utils/encrypted_type.py:38-46`
- Test: `tests/test_encrypted_type.py`

**Step 1: Update the existing test for new behavior**

In `tests/test_encrypted_type.py`, change `test_process_bind_param_error_returns_raw`:

```python
    @patch("app.utils.encrypted_type._get_fernet")
    def test_process_bind_param_error_raises(self, mock_fernet):
        """Encryption failure raises ValueError (fail-closed, no plaintext stored)."""
        mock_fernet.side_effect = Exception("key error")

        et = EncryptedText()
        with pytest.raises(ValueError, match="Encryption failed"):
            et.process_bind_param("secret-value", MagicMock())
```

Add `import pytest` at the top if not already present.

**Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_encrypted_type.py::TestEncryptedText::test_process_bind_param_error_raises -v`
Expected: FAIL (currently returns raw value instead of raising)

**Step 3: Implement fail-closed encryption**

In `app/utils/encrypted_type.py`, replace `process_bind_param`:

```python
    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        try:
            f = _get_fernet()
            return f.encrypt(value.encode()).decode()
        except Exception as e:
            logger.error(f"Encryption failed — refusing to store plaintext: {e}")
            raise ValueError("Encryption failed — cannot store sensitive data as plaintext") from e
```

**Step 4: Run all encrypted_type tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_encrypted_type.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add app/utils/encrypted_type.py tests/test_encrypted_type.py
git commit -m "security: encryption fail-closed — raise on encrypt failure instead of storing plaintext"
```

---

## Phase 2 — Bug Fix + Operational

### Task 3: Frontend 401 redirect path

**Files:**
- Modify: `app/static/app.js` (line ~320)
- No test file (frontend JS — verified by grep)

**Step 1: Fix the redirect path**

In `app/static/app.js`, find:
```javascript
setTimeout(() => { window.location.href = '/login'; }, 1500);
```

Replace with:
```javascript
setTimeout(() => { window.location.href = '/auth/login'; }, 1500);
```

**Step 2: Verify no other occurrences of '/login'**

Run: `grep -n "'/login'" app/static/app.js app/static/crm.js app/static/tickets.js app/static/touch.js 2>/dev/null`
Expected: No other occurrences (or fix them all)

**Step 3: Commit**

```bash
git add app/static/app.js
git commit -m "fix: correct 401 redirect from /login to /auth/login"
```

---

### Task 4: Uvicorn proxy headers

**Files:**
- Modify: `Dockerfile` (CMD line, line 45)

**Step 1: Update Dockerfile CMD**

In `Dockerfile`, replace:
```dockerfile
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

With:
```dockerfile
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
```

Note: `--forwarded-allow-ips '*'` is safe here because port 8000 is only reachable within the Docker network (Caddy is the only ingress).

**Step 2: Update rate limiter key function**

In `app/rate_limit.py`, the `get_remote_address` function already reads `request.client.host`. With `--proxy-headers`, Uvicorn will set `request.client.host` from `X-Forwarded-For`. No code change needed in rate_limit.py.

**Step 3: Commit**

```bash
git add Dockerfile
git commit -m "ops: enable Uvicorn proxy headers for correct client IP behind Caddy"
```

---

## Phase 3 — Dev UX + Cleanup

### Task 5: CSP for Vite dev server

**Files:**
- Modify: `app/main.py:297-317` (csp_middleware)
- Test: `tests/test_routers_auth.py` or new test (CSP middleware)

**Step 1: Write a test for dev CSP**

Add to a test file (can be `tests/test_middleware.py` if it exists, or add to an existing test file):

```python
# In tests/test_routers_auth.py or tests/test_middleware.py:
import os
from unittest.mock import patch


def test_csp_includes_vite_dev_origin_when_enabled(client):
    """CSP includes localhost:5173 when VITE_DEV is set."""
    with patch.dict(os.environ, {"VITE_DEV": "1"}):
        resp = client.get("/")
        csp = resp.headers.get("content-security-policy", "")
        assert "http://localhost:5173" in csp


def test_csp_excludes_vite_dev_origin_in_production(client):
    """CSP does NOT include localhost:5173 when VITE_DEV is not set."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("VITE_DEV", None)
        resp = client.get("/")
        csp = resp.headers.get("content-security-policy", "")
        assert "localhost:5173" not in csp
```

**Step 2: Run tests to verify they fail (first test should fail)**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_auth.py::test_csp_includes_vite_dev_origin_when_enabled -v`
Expected: FAIL

**Step 3: Implement conditional CSP**

In `app/main.py`, update `csp_middleware`:

```python
@app.middleware("http")
async def csp_middleware(request: Request, call_next):
    """Add Content-Security-Policy header.

    Uses 'unsafe-inline' for script-src because the app relies on inline
    onclick handlers throughout the SPA template.  A nonce cannot be used
    alongside 'unsafe-inline' — browsers that support nonces silently
    ignore 'unsafe-inline', which breaks all inline event handlers.
    """
    response = await call_next(request)

    vite_dev = os.environ.get("VITE_DEV")
    vite_origins = " http://localhost:5173" if vite_dev else ""
    vite_ws = " ws://localhost:5173" if vite_dev else ""

    csp = (
        "default-src 'self'; "
        f"script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com{vite_origins}; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        f"connect-src 'self'{vite_origins}{vite_ws}"
    )
    response.headers["Content-Security-Policy"] = csp
    return response
```

Add `import os` at the top of `app/main.py` if not already present (it is — line 7).

**Step 4: Run CSP tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_auth.py::test_csp_includes_vite_dev_origin_when_enabled tests/test_routers_auth.py::test_csp_excludes_vite_dev_origin_in_production -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add app/main.py tests/test_routers_auth.py
git commit -m "dev: allow Vite dev server in CSP when VITE_DEV is set"
```

---

### Task 6: Model config unification

**Files:**
- Modify: `app/utils/claude_client.py:32-35`
- Modify: `app/services/gradient_service.py:37-40`
- Modify: `app/config.py:72` (remove dead setting)
- Test: existing test or new test

**Step 1: Write failing tests**

```python
# In tests/test_claude_client.py (or create it):
from unittest.mock import patch, MagicMock


def test_claude_client_reads_model_from_settings():
    """claude_client MODELS['smart'] should read from settings.anthropic_model."""
    with patch("app.config.settings", MagicMock(anthropic_model="claude-test-model")):
        # Re-import to pick up patched settings
        import importlib
        import app.utils.claude_client as cc
        importlib.reload(cc)
        assert cc.MODELS["smart"] == "claude-test-model"


def test_gradient_strong_model_uses_dash():
    """Gradient strong model should use dash not dot: anthropic-claude-opus-4-6."""
    from app.services.gradient_service import MODELS
    assert "." not in MODELS["strong"], f"Model ID should use dashes: {MODELS['strong']}"
```

**Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_claude_client.py -v` (or wherever tests are placed)
Expected: FAIL

**Step 3: Implement model config unification**

In `app/utils/claude_client.py`, replace hardcoded MODELS:

```python
from app.config import settings

# Model tiers — read smart model from settings, fast stays fixed
MODELS = {
    "fast": "claude-haiku-4-5-20251001",
    "smart": settings.anthropic_model,
}
```

In `app/services/gradient_service.py`, fix the strong model ID (line 39):

```python
MODELS = {
    "default": _configured or "anthropic-claude-sonnet-4-6",
    "strong": "anthropic-claude-opus-4-6",  # dash, not dot
}
```

In `app/config.py`, keep `anthropic_model` (it's now actually used by claude_client.py). No deletion needed.

**Step 4: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_claude_client.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add app/utils/claude_client.py app/services/gradient_service.py tests/test_claude_client.py
git commit -m "fix: unify model config — claude_client reads from settings, fix Gradient opus model ID"
```

---

## Final Verification

After all 6 tasks complete:

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
```

Expected: All tests pass, coverage >= 97% (no regression).

Then commit and deploy:

```bash
docker compose up -d --build
docker compose logs -f app  # verify clean startup
```
