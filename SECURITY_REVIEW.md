# AVAIL AI — Security Review

**Date:** 2026-03-14
**Scope:** Authentication, session management, CORS, CSRF, secrets, container security, headers, rate limiting
**Files Reviewed:** `app/dependencies.py`, `app/config.py`, `app/main.py`, `app/routers/auth.py`, `app/startup.py`, `docker-compose.yml`, `Dockerfile`, `Caddyfile`, `docker-entrypoint.sh`

---

## CRITICAL Findings

### SEC-01: Agent API Key — Timing-Safe Comparison Missing (dependencies.py:54)

```53:54:app/dependencies.py
        agent_key = request.headers.get("x-agent-key")
        if agent_key and settings.agent_api_key and agent_key == settings.agent_api_key:
```

The agent API key is compared using Python `==`, which is vulnerable to **timing attacks**. An attacker can measure response times to infer the key character-by-character.

**Fix:** Use `hmac.compare_digest(agent_key, settings.agent_api_key)` (already imported in `auth.py`).

**Severity:** CRITICAL — service-to-service auth bypass via side-channel.

---

### SEC-02: Agent User Bypass — No Rate Limiting or IP Restriction (dependencies.py:50-55)

```50:55:app/dependencies.py
        from .config import settings
        agent_key = request.headers.get("x-agent-key")
        if agent_key and settings.agent_api_key and agent_key == settings.agent_api_key:
            user = db.query(User).filter_by(email="agent@availai.local").first()
```

The `x-agent-key` header is accepted on **every endpoint** that uses `require_user`. There is:
- No rate limiting on agent-key authentication attempts
- No IP allowlist (any public client can try keys)
- No logging of failed agent key attempts
- No lockout after repeated failures

**Severity:** CRITICAL — brute-force attack surface on a global auth bypass.

---

### SEC-03: Encryption Salt Is Static / Hardcoded (utils/encrypted_type.py:25)

```24:25:app/utils/encrypted_type.py
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"availai-token-encryption-v1",
        iterations=100_000,
    )
```

The Fernet encryption key derivation uses a **hardcoded salt**. This means:
- Any attacker who obtains `secret_key` can immediately derive the Fernet key
- The salt provides zero additional entropy
- All deployments sharing the same `secret_key` share the same encryption key

**Fix:** Generate a random salt on first deploy, store it in an env var or file, and rotate it with a re-encryption migration when changed.

**Severity:** CRITICAL — reduces encryption at rest to a single secret.

---

### SEC-04: Decryption Fallback Returns Plaintext on Error (utils/encrypted_type.py:54-59)

```54:59:app/utils/encrypted_type.py
        except InvalidToken:
            # Value may be stored in plaintext (pre-migration data)
            return value
        except Exception:
            logger.warning("Unexpected decryption error, returning raw value")
            return value
```

When decryption fails (including `InvalidToken`), the raw ciphertext/plaintext is returned silently. If the encryption key rotates or is misconfigured, **every token in the DB becomes readable as garbled text** with no alarm. Worse, if an attacker corrupts the key, all error paths silently succeed.

**Fix:** After migration from plaintext is complete, the `InvalidToken` fallback should raise or at minimum log at ERROR level and return `None`.

**Severity:** HIGH — silent failure degrades encryption to no-op.

---

### SEC-05: Default Database Password in docker-compose.yml (docker-compose.yml:23-25)

```23:25:docker-compose.yml
      POSTGRES_USER: ${POSTGRES_USER:-availai}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-availai}
      POSTGRES_DB: ${POSTGRES_DB:-availai}
```

The default PostgreSQL password is `availai` — the same as the username. The `.env.example` also ships `POSTGRES_PASSWORD=availai` (line 55). If the `.env` file is not changed in production, the database is accessible with trivially guessable credentials.

**Fix:** Remove the default from `docker-compose.yml` so it fails if not set, or generate a random password in a deploy script.

**Severity:** HIGH — database compromise if defaults are used.

---

### SEC-06: Redis Has No Authentication (docker-compose.yml:53)

```53:docker-compose.yml
    command: redis-server --maxmemory 512mb --maxmemory-policy allkeys-lru
```

Redis is started with **no `requirepass`** and no ACL. Any container on the Docker network (or any process on the host if ports were exposed) can read/write rate-limit state, potentially bypassing rate limits.

**Fix:** Add `--requirepass ${REDIS_PASSWORD}` and update `REDIS_URL` to include the password.

**Severity:** HIGH — rate limit bypass and cache poisoning within the Docker network.

---

## HIGH Findings

### SEC-07: CSRF Exemption on All /v2/* Routes (main.py:294)

```289:296:app/main.py
    app.add_middleware(
        CSRFMiddleware,
        secret=settings.secret_key,
        sensitive_cookies={"session"},
        exempt_urls=[
            re.compile(r"/auth/.*"),
            re.compile(r"/health"),
            re.compile(r"/metrics"),
            re.compile(r"/api/buy-plans/token/.*"),
            re.compile(r"/v2/.*"),
        ],
    )
```

The entire `/v2/` namespace is CSRF-exempt. The comment says "HTMX views use session auth, not CSRF tokens" — but this is backwards: HTMX views **do** use session cookies, so they **need** CSRF protection. Any state-mutating HTMX endpoint under `/v2/` is vulnerable to cross-site request forgery.

**Fix:** Remove the `/v2/` exemption. Use HTMX's `hx-headers` to send the CSRF token with every request.

**Severity:** HIGH — any POST/PUT/DELETE under `/v2/` is CSRF-vulnerable.

---

### SEC-08: Password Login Gate Can Be Enabled in Production (auth.py:191-199)

```191:199:app/routers/auth.py
def _password_login_enabled() -> bool:
    if os.getenv("TESTING") == "1":
        return True
    return os.getenv("ENABLE_PASSWORD_LOGIN", "false").lower() == "true"
```

Setting `ENABLE_PASSWORD_LOGIN=true` in `.env` enables password-based login in production. This circumvents Azure AD entirely. Combined with `_create_default_user_if_env_set()` in `startup.py` (line 63), an operator who accidentally leaves `DEFAULT_USER_EMAIL`/`DEFAULT_USER_PASSWORD` and `ENABLE_PASSWORD_LOGIN=true` in the production `.env` creates a **static admin backdoor**.

**Fix:** Add a startup warning when `ENABLE_PASSWORD_LOGIN=true` and `TESTING!=1`. Consider a dedicated allow-list of IPs for password login.

**Severity:** HIGH — accidental backdoor in production.

---

### SEC-09: Default User Created with Admin Role (startup.py:74)

```74:startup.py
    role = os.environ.get("DEFAULT_USER_ROLE", "admin")
```

When `DEFAULT_USER_ROLE` is not set, the default user gets **admin** privileges. This silently creates a superuser.

**Fix:** Default to `"buyer"` (least privilege) and require explicit `DEFAULT_USER_ROLE=admin`.

**Severity:** HIGH — privilege escalation by omission.

---

### SEC-10: Session Cookie max_age Is 24 Hours (main.py:273-274)

```268:274:app/main.py
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    https_only=settings.app_url.startswith("https"),
    same_site="lax",
    max_age=86400,
)
```

The session cookie lives for 24 hours regardless of activity. There is no server-side session invalidation store — sessions are purely cookie-based (signed by `secret_key`). This means:
- Logout only clears the client cookie; if the cookie was copied, the session is still valid
- There is no mechanism to force-logout a compromised user
- Sessions survive password changes

**Fix:** Use server-side sessions (e.g., Redis-backed) with explicit invalidation on logout.

**Severity:** HIGH — session theft persists for up to 24 hours with no revocation.

---

### SEC-11: Hardcoded Admin User Seeded on Every Boot (startup.py:103-128)

```103:128:app/startup.py
def _seed_vinod_user(db=None) -> None:
    ...
    existing = db.query(User).filter_by(email="vinod@trioscs.com").first()
    if existing:
        ...
        return
    user = User(email="vinod@trioscs.com", name="Vinod", role="admin")
```

A hardcoded admin user is seeded on every application boot. Even if this user is deleted or deactivated by an admin, it will be recreated on the next restart (only the email-exists check prevents recreation; deactivation is not checked).

**Fix:** Move admin bootstrapping to a one-time CLI command or migration, not startup.

**Severity:** HIGH — cannot remove this user without code changes.

---

### SEC-12: Auth Status Endpoint Leaks All Connected Users' Info (auth.py:271-311)

```271:311:app/routers/auth.py
@router.get("/auth/status")
async def auth_status(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user:
        return JSONResponse({"connected": False, "users": []})
    all_users = db.query(User).filter(User.refresh_token.isnot(None)).all()
    ...
```

Any authenticated user (regardless of role) can see all other users' names, emails, roles, M365 connection status, error reasons, and last scan timestamps. This is an **information disclosure** issue — non-admin users should not see other users' data.

**Fix:** Restrict the `users` list to admin-only; non-admin users should only see their own status.

**Severity:** HIGH — information disclosure.

---

## MEDIUM Findings

### SEC-13: CSP Allows 'unsafe-inline' for Scripts (main.py:325)

```323:331:app/main.py
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://unpkg.com https://cdn.jsdelivr.net https://cdn.tailwindcss.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.tailwindcss.com; "
        ...
    )
```

`'unsafe-inline'` in `script-src` defeats XSS protection that CSP is designed to provide. The comment on lines 318-321 acknowledges this is due to inline `onclick` handlers.

**Fix:** Migrate inline event handlers to `addEventListener()` and switch to nonce-based CSP.

**Severity:** MEDIUM — CSP provides no XSS mitigation in its current state.

---

### SEC-14: CSP Allows Multiple CDN Origins (main.py:325)

The CSP allows scripts from `cdnjs.cloudflare.com`, `unpkg.com`, `cdn.jsdelivr.net`, and `cdn.tailwindcss.com`. Each CDN is a potential attack vector (CDN compromise, package confusion). Additionally, some CDNs host "CDN bypass gadgets" that can be used to circumvent CSP entirely.

**Fix:** Self-host critical JS/CSS dependencies or use Subresource Integrity (SRI) hashes.

**Severity:** MEDIUM — supply chain risk through CDN compromise.

---

### SEC-15: OAuth State Mismatch Fails Open (auth.py:98-101)

```98:101:app/routers/auth.py
    expected_state = request.session.pop("oauth_state", None)
    if not expected_state or state != expected_state:
        logger.warning("OAuth callback state mismatch (possible CSRF)")
        return RedirectResponse("/")
```

On state mismatch, the user is silently redirected home with no error message. This is correct behavior (fail safe), but the state comparison on line 99 uses `!=` instead of `hmac.compare_digest()`, introducing a minor timing side-channel on the OAuth state parameter.

**Fix:** Use `hmac.compare_digest(state, expected_state)`.

**Severity:** LOW (the state is single-use and random, but defense-in-depth).

---

### SEC-16: Prometheus /metrics Blocked Only at Caddy (Caddyfile:27-29)

```27:29:Caddyfile
	@blocked path /metrics
	handle @blocked {
		respond 403
	}
```

The `/metrics` endpoint is blocked by Caddy, but the FastAPI app still exposes it on port 8000. If Caddy is bypassed (e.g., direct access to the app container, or during local development), Prometheus metrics with request counts, latencies, and error rates are fully exposed.

**Fix:** Add authentication to the `/metrics` endpoint in FastAPI (e.g., require a bearer token or limit to localhost).

**Severity:** MEDIUM — information disclosure if Caddy is bypassed.

---

### SEC-17: X-Frame-Options Inconsistency Between Caddy and FastAPI

Caddy sets `X-Frame-Options: SAMEORIGIN` (Caddyfile:20), while FastAPI sets `X-Frame-Options: DENY` (main.py:362). These conflict — Caddy's response will include both headers, and browser behavior on duplicate headers is undefined (most use the last one).

**Fix:** Remove one source. Since `DENY` is stricter and more appropriate, remove the Caddy header or ensure they match.

**Severity:** LOW — defense-in-depth inconsistency.

---

### SEC-18: Uvicorn `--forwarded-allow-ips *` Trusts All Proxy Headers (Dockerfile:53)

```53:Dockerfile
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
```

`--forwarded-allow-ips "*"` means uvicorn trusts `X-Forwarded-For` headers from **any IP**. An attacker sending requests directly to port 8000 (bypassing Caddy) can spoof their IP address, potentially bypassing rate limiting (which uses `get_remote_address`).

**Fix:** Restrict to the Docker network gateway IP or `172.0.0.0/8`: `--forwarded-allow-ips "172.0.0.0/8"`.

**Severity:** MEDIUM — rate limit bypass via IP spoofing.

---

### SEC-19: No Account Lockout After Failed Password Attempts (auth.py:215-244)

```215:244:app/routers/auth.py
@router.post("/auth/login")
async def password_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
```

The password login endpoint has no rate limiting decorator and no account lockout mechanism. While the global rate limit applies (120/min), this is far too permissive for authentication — it allows ~120 password guesses per minute per IP.

**Fix:** Add `@limiter.limit("5/minute")` on the password login endpoint and implement account lockout after N failures.

**Severity:** MEDIUM — brute-force risk on password login.

---

### SEC-20: entrypoint.sh — WARNING on Missing Vars Instead of EXIT (docker-entrypoint.sh:10-12)

```10:12:docker-entrypoint.sh
if [ -n "$MISSING" ]; then
    echo "WARNING: Missing required env vars:$MISSING"
fi
```

When `DATABASE_URL`, `AZURE_CLIENT_ID`, or `AZURE_TENANT_ID` are missing, the entrypoint only logs a WARNING and **continues booting**. The app may start in a degraded/insecure state. Only `SESSION_SECRET` triggers a hard exit (line 13-17).

**Fix:** Exit non-zero when critical env vars like `DATABASE_URL` are missing.

**Severity:** MEDIUM — app boots in insecure/broken state.

---

### SEC-21: Auto-Promotion to Admin via admin_emails (auth.py:156-158)

```156:158:app/routers/auth.py
    if user.email.lower() in settings.admin_emails and user.role != "admin":
        user.role = "admin"
        logger.info(f"Auto-promoted {user.email} to admin via admin_emails bootstrap")
```

Any user whose email appears in `ADMIN_EMAILS` is **automatically promoted to admin on every login**. If an admin explicitly demotes a user (e.g., for cause), the user simply logs in again and is re-promoted. The env var overrides manual role changes.

**Fix:** Only auto-promote on first login (when `user.role` is the default), not on every login.

**Severity:** MEDIUM — admin demotion is impossible without code/config change.

---

### SEC-22: Docker Container Runs Initial Commands as Root (docker-entrypoint.sh:19-27)

The entrypoint copies static files and runs Alembic migrations as root before dropping privileges on line 38:

```19:38:docker-entrypoint.sh
if [ -d /srv/static ]; then
    rm -rf /srv/static/assets /srv/static/.vite
    ...
fi
echo "Running alembic upgrade head..."
if ! runuser -u appuser -- alembic upgrade head; then
    ...
fi
exec runuser -u appuser -- "$@"
```

While the app itself runs as `appuser`, the static file copy runs as root. If an attacker can influence the contents of `app/static/dist/`, they could write to `/srv/static` as root. This is a minor concern since the source is from the build stage.

**Fix:** Copy static files as `appuser` or verify the shared volume permissions.

**Severity:** LOW — limited blast radius due to Docker build isolation.

---

### SEC-23: Logout Does Not Invalidate Server-Side Session (auth.py:185-188)

```185:188:app/routers/auth.py
@router.post("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return JSONResponse({"ok": True})
```

`request.session.clear()` only clears the session data from the cookie. Since Starlette's `SessionMiddleware` uses signed cookies (not server-side storage), a previously captured session cookie remains valid until expiry (24 hours). The user's M365 tokens in the DB are also not cleared on logout.

**Fix:** Clear `user.access_token` and `user.refresh_token` on logout. Implement server-side session store for proper invalidation.

**Severity:** MEDIUM — token persistence after logout.

---

### SEC-24: OAuth Callback Rate Limit May Be Insufficient (auth.py:93)

```93:auth.py
@limiter.limit("10/minute")
async def callback(request: Request, code: str = "", state: str = "", ...):
```

10 attempts per minute is reasonable for legitimate OAuth callbacks, but the login endpoint (`GET /auth/login`) has **no rate limit**, allowing an attacker to generate unlimited OAuth state values and potentially abuse the authorization server.

**Fix:** Add `@limiter.limit("10/minute")` to the `/auth/login` endpoint as well.

**Severity:** LOW — abuse of Azure AD authorization endpoint.

---

## LOW / Informational Findings

### SEC-25: Default secret_key in Config (config.py:43)

```43:app/config.py
    secret_key: str = "change-me-in-production"
```

The default value `"change-me-in-production"` is checked and blocked at startup (main.py:39-40), but it exists in the code. If the startup check is ever bypassed (e.g., `TESTING=1`), all sessions and encryption are trivially breakable.

**Severity:** LOW — mitigated by runtime check, but defense-in-depth suggests no default.

---

### SEC-26: Database URL Contains Credentials in Default (config.py:44)

```44:app/config.py
    database_url: str = "postgresql://availai:availai@db:5432/availai"
```

Default DATABASE_URL contains hardcoded credentials. While this is overridden by `.env`, the default is visible in source code.

**Severity:** LOW — informational.

---

### SEC-27: HSTS max-age Inconsistency

FastAPI sets `max-age=31536000` (1 year, main.py:366) while Caddy sets `max-age=63072000` (2 years, Caddyfile:19). Both values are sent. Browsers typically use the last header they encounter.

**Severity:** LOW — both are safe values; just inconsistent.

---

### SEC-28: Caddy Admin API Exposed Within Docker Network (Caddyfile implicit)

Caddy's admin API listens on `:2019` by default (used by the healthcheck on docker-compose.yml:124). This API allows runtime configuration changes. While not exposed externally, any container on the Docker network can reach it.

**Fix:** Disable the admin API with `admin off` in the Caddyfile, and use a file-based healthcheck instead.

**Severity:** LOW — internal network only.

---

### SEC-29: No CORS Configuration (main.py)

There is no CORS middleware configured in `main.py`. This is actually **correct** for a same-origin app — the browser's default same-origin policy blocks cross-origin requests. However, if the API is ever consumed by a different frontend, CORS will need to be added carefully.

**Severity:** INFORMATIONAL — correct for current architecture; document intent.

---

### SEC-30: Request ID Is Truncated UUID (main.py:342)

```342:app/main.py
    req_id = str(uuid.uuid4())[:8]
```

8-character request IDs have only 32 bits of entropy (~4 billion values). In high-volume scenarios, collisions are likely. This doesn't directly cause security issues but can make incident investigation harder.

**Severity:** INFORMATIONAL.

---

## Summary by Severity

| Severity | Count | IDs |
|----------|-------|-----|
| CRITICAL | 4 | SEC-01, SEC-02, SEC-03, SEC-04 |
| HIGH | 6 | SEC-05, SEC-06, SEC-07, SEC-08, SEC-09, SEC-10, SEC-11, SEC-12 |
| MEDIUM | 7 | SEC-13, SEC-14, SEC-15, SEC-16, SEC-17, SEC-18, SEC-19, SEC-20, SEC-21, SEC-22, SEC-23, SEC-24 |
| LOW/INFO | 6 | SEC-25, SEC-26, SEC-27, SEC-28, SEC-29, SEC-30 |

## Top 5 Recommended Fixes (Priority Order)

1. **SEC-01 + SEC-02:** Use `hmac.compare_digest` for agent key, add rate limiting and IP restriction, log failures
2. **SEC-07:** Remove `/v2/` from CSRF exemptions; add CSRF token to HTMX requests
3. **SEC-05 + SEC-06:** Require strong DB password, add Redis auth
4. **SEC-10 + SEC-23:** Implement server-side session store with explicit invalidation; clear tokens on logout
5. **SEC-03 + SEC-04:** Use a random encryption salt; remove plaintext fallback after migration
