# Security & Operational Hardening — Design Doc

**Date:** 2026-03-03
**Scope:** 6-item codebase audit fix (security, bug, operational, dev UX)

## Items

### 1. OAuth `state` parameter + URL encoding (Security)
- **File:** `app/routers/auth.py`
- **Problem:** `/auth/login` builds authorize URL without CSRF `state` param and without URL-encoding `scope` (contains spaces)
- **Fix:** Generate random `state`, store in `request.session`, include in authorize URL via `urllib.parse.urlencode()`. Validate in `/auth/callback`, pop after use.

### 2. Frontend 401 redirect path (Bug)
- **File:** `app/static/app.js` (line ~320)
- **Problem:** `apiFetch()` redirects to `/login` on 401, but login route is `/auth/login`
- **Fix:** Change `'/login'` → `'/auth/login'`

### 3. Uvicorn proxy headers (Operational)
- **File:** `Dockerfile` (CMD line)
- **Problem:** Rate limiter sees Caddy container IP, not real client IP
- **Fix:** Add `--proxy-headers --forwarded-allow-ips '*'` to uvicorn CMD (port 8000 not publicly exposed)

### 4. CSP for Vite dev server (Dev UX)
- **File:** `app/main.py` (csp_middleware, line ~298)
- **Problem:** CSP blocks Vite dev server scripts/HMR websocket
- **Fix:** When `VITE_DEV` env var is set, add `http://localhost:5173` to `script-src` and `http://localhost:5173 ws://localhost:5173` to `connect-src`

### 5. Model config unification (Maintainability)
- **Files:** `app/config.py`, `app/utils/claude_client.py`, `app/services/gradient_service.py`
- **Problem:** `claude_client.py` ignores `settings.anthropic_model`; Gradient `strong` model ID has dot vs dash inconsistency
- **Fix:** `claude_client.py` reads from settings; Gradient `strong` fixed to `"anthropic-claude-opus-4-6"` (dash)

### 6. Encryption fail-closed (Security)
- **File:** `app/utils/encrypted_type.py`
- **Problem:** Encryption failure silently stores plaintext
- **Fix:** Raise `ValueError` on encryption failure (rolls back transaction, no plaintext stored)

## Execution Plan

### Phase 1 — Security (parallel agents A + B)
- Agent A: OAuth state + URL encoding (item 1)
- Agent B: Encryption fail-closed (item 6)

### Phase 2 — Bug + operational (parallel agents C + D)
- Agent C: Frontend 401 redirect (item 2)
- Agent D: Uvicorn proxy headers (item 3)

### Phase 3 — Dev UX + cleanup (parallel agents E + F)
- Agent E: CSP Vite dev (item 4)
- Agent F: Model config unification (item 5)

## Decision Record
- Encryption fail-closed: **raise exception** (user-approved) — no NULL fallback
- Proxy headers: `--forwarded-allow-ips '*'` safe because port 8000 is internal to Docker network only
- Each agent writes tests alongside the fix
