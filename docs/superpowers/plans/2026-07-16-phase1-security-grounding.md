# Phase 1 Security — Grounding (source for the TDD plan)

## Lock down /docs, /redoc, /openapi.json in BOTH layers: (1) Caddy edge — extend the existing @blocked matcher; (2) FastAPI app — gate docs_url/redoc_url/openapi_url on a NEW setting `expose_api_docs` (default False) so the routes aren't even registered (→ 404). Keep tests/test_contract.py green by feeding schemathesis the in-process `app.openapi()` dict instead of fetching /openapi.json over HTTP. Add a test asserting all three paths → 404 by default.

**files:**
- /root/availai/Caddyfile (lines 36-39, the @blocked block)
- /root/availai/app/config.py (insert new field near line 54, right after the Sentry block that ends at line 53 and before `# --- Rate limiting ---` at line 55; instantiation `settings = Settings()` is at line 437; `settings` is exported and imported by main.py at line 23)
- /root/availai/app/main.py (FastAPI() constructor at lines 238-244; `from .config import APP_VERSION, settings` already present at line 23)
- /root/availai/tests/test_contract.py (edit lines 24-33 and 45-73 — the two schemathesis tests that currently HTTP-fetch /openapi.json)
- /root/availai/tests/test_security_headers.py (add the new 404-gating test here; existing 404-status mirror is at lines 79-87; uses the `client` fixture)
- /root/availai/tests/conftest.py (the `client` fixture is defined at line 579; env is set at lines 32-37 BEFORE any app import)


**current_code:**
CADDYFILE — /root/availai/Caddyfile:36-39 (current):
    @blocked path /metrics
    handle @blocked {
        respond 403
    }

CONFIG — /root/availai/app/config.py:50-57 (current; note the Sentry block ends at 53, Rate-limiting starts at 55):
    # --- Sentry ---
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.1
    sentry_profiles_sample_rate: float = 0.1

    # --- Rate limiting ---
    rate_limit_default: str = "120/minute"
    rate_limit_enabled: bool = True
(and at the bottom, line 437):  settings = Settings()

MAIN — /root/availai/app/main.py:238-244 (current):
    app = FastAPI(
        title="AVAIL — Electronic Component Sourcing",
        description="Electronic component sourcing engine with vendor intelligence, RFQ automation, and CRM.",
        version=APP_VERSION,
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
    )

CONTRACT TEST — /root/availai/tests/test_contract.py:24-33 (current, test_openapi_schema_is_valid) — this HTTP-fetches /openapi.json and asserts 200, which BREAKS when openapi_url=None:
    def test_openapi_schema_is_valid():
        from starlette.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        ...

CONTRACT TEST — /root/availai/tests/test_contract.py:52-60 (current, test_contract_health) — from_asgi("/openapi.json", app) also fetches the route over ASGI, so it 404s when the route is unregistered:
        if hasattr(schemathesis, "from_asgi"):
            schema = schemathesis.from_asgi("/openapi.json", app)
        elif hasattr(schemathesis, "from_dict"):
            client = TestClient(app)
            raw = client.get("/openapi.json").json()
            schema = schemathesis.from_dict(raw)
        else:
            pytest.skip("schemathesis version lacks from_asgi/from_dict")


**edit_plan:**
EDIT 1 — Caddyfile:36 (edge block). Change the @blocked matcher to include the three doc paths (space-separated = OR):
    @blocked path /metrics /docs /redoc /openapi.json
    handle @blocked {
        respond 403
    }
Keep `respond 403` to match the existing /metrics convention. (Optional hardening the plan-writer may note: `respond 404` would hide existence rather than reveal a blocked resource; and FastAPI's Swagger UI also serves /docs/oauth2-redirect — add it only if you ever enable docs behind Caddy. Not required since the app-layer default already 404s.) Caddy `path` is exact-match, case-sensitive; FastAPI's defaults are exactly /docs, /redoc, /openapi.json, so exact paths are correct.

EDIT 2 — app/config.py: insert a new field between the Sentry block (ends line 53) and `# --- Rate limiting ---` (line 55). This lands the field at ~line 55 as requested and mirrors the existing `bool = False` feature-flag style used at lines 114/127/236:
    # --- API docs exposure ---
    # Swagger UI (/docs), ReDoc (/redoc), and the raw schema (/openapi.json) are
    # OFF by default. FastAPI only registers those routes when their *_url is
    # non-None (see app/main.py FastAPI() ctor), so False => the routes never
    # exist => 404. Flip True only in a trusted/dev environment. Env: EXPOSE_API_DOCS.
    expose_api_docs: bool = False
(pydantic-settings maps the field to env var EXPOSE_API_DOCS automatically.)

EDIT 3 — app/main.py:238-244. Add three gated kwargs to the FastAPI() constructor (settings is already imported at line 23):
    app = FastAPI(
        title="AVAIL — Electronic Component Sourcing",
        description="Electronic component sourcing engine with vendor intelligence, RFQ automation, and CRM.",
        version=APP_VERSION,
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
        docs_url="/docs" if settings.expose_api_docs else None,
        redoc_url="/redoc" if settings.expose_api_docs else None,
        openapi_url="/openapi.json" if settings.expose_api_docs else None,
    )
Verified empirically (fastapi 0.139.0): with all three *_url=None, GET /docs, /redoc, /openapi.json each return 404, and `app.openapi()` STILL returns the full schema dict (keys: openapi/info/paths, paths populated).

EDIT 4 — tests/test_contract.py: stop fetching /openapi.json over HTTP; use the in-process `app.openapi()` method (works regardless of openapi_url=None).
  4a) test_openapi_schema_is_valid (lines 24-42): replace the TestClient + client.get("/openapi.json") + status-code assert with:
        from app.main import app
        schema = app.openapi()
    then keep the existing dict assertions (schema["openapi"], schema["paths"], len>0, info.version, info.description, tags). Drop the `from starlette.testclient import TestClient` import and the `resp.status_code == 200` line.
  4b) test_contract_health (lines 52-60): prefer building the schema from the dict so it never depends on the HTTP route. Replace the branch with:
        raw_schema = app.openapi()  # in-process; valid even when openapi_url=None
        if hasattr(schemathesis, "from_dict"):
            schema = schemathesis.from_dict(raw_schema)
        elif hasattr(schemathesis, "from_asgi"):
            schema = schemathesis.from_asgi("/openapi.json", app)  # only if docs exposed
        else:
            pytest.skip("schemathesis version lacks from_dict/from_asgi")
    Leave the rest of the function (endpoint iteration, case.call_asgi(app) / client.get('/health'), validate_response) unchanged — /health is a real route and still 200s.

EDIT 5 — add the new gating test (see test_file field) asserting all three paths → 404 under the default (expose_api_docs=False).


**test_file:**
/root/availai/tests/test_security_headers.py — add a new test here (this file already exercises app-level security posture via the `client` fixture and asserts 404 status codes, so it is the natural home). Add:

    @pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
    def test_api_docs_disabled_by_default(client, path):
        \"\"\"Swagger UI, ReDoc, and the raw OpenAPI schema are not exposed by default
        (expose_api_docs=False) — FastAPI never registers the routes, so they 404.\"\"\"
        resp = client.get(path)
        assert resp.status_code == 404

Notes for the plan-writer: `pytest` is already imported at the top of this file (line 12). The `client` fixture (conftest.py:579) is authed, but these routes 404 regardless of auth, so `client` is fine (mirrors every other test in this file). Because the process-wide `app.main.app` is constructed with the default (conftest never sets EXPOSE_API_DOCS), the routes are genuinely unregistered for the whole session — this test asserts exactly that default. Do NOT try to also assert a "docs enabled → 200" case in the same process: the *_url values are frozen at construction time, so a runtime env flip won't take effect on the singleton (see gotchas).


**test_pattern_to_mirror:**
tests/test_security_headers.py:79-87 `test_error_response_format` — uses the `client` fixture and asserts `resp.status_code == 404` on an unauthenticated GET. Mirror its structure exactly (fixture + `client.get(path)` + `assert resp.status_code == 404`), just parametrized over the three doc paths. For the parametrize style, mirror `test_static_security_header` at lines 22-35 in the same file. For the contract-test edits, the existing in-repo pattern to lean on is that test_contract.py already has a `from_dict` branch (lines 55-58) — you are promoting that dict-based path (fed by `app.openapi()`) to primary.


**verification:**
Local (app layer): TESTING=1 PYTHONPATH=/root/availai /root/availai/.venv/bin/pytest tests/test_security_headers.py -k api_docs_disabled -v  → 3 passed (/docs, /redoc, /openapi.json all 404). Also confirm nothing else regressed: TESTING=1 PYTHONPATH=/root/availai /root/availai/.venv/bin/pytest tests/test_security_headers.py tests/test_contract.py -v (contract tests SKIP locally since schemathesis is absent — that's expected; they must PASS in CI where schemathesis==4.22.4 is installed). Sanity one-liner (no deps): /root/availai/.venv/bin/python -c \"import os; os.environ['TESTING']='1'; os.environ['DATABASE_URL']='sqlite://'; from app.main import app; from starlette.testclient import TestClient; c=TestClient(app); print({p:c.get(p).status_code for p in ['/docs','/redoc','/openapi.json']}); print('openapi() paths:', len(app.openapi()['paths']))\"  → expect all three 404 and a non-zero path count. Staging/edge layer: after deploy, `curl -s -o /dev/null -w '%{http_code}\\n' https://app.availai.net/docs` (and /redoc, /openapi.json) → 403 (Caddy) or 404 (app), never 200. Flip test only: EXPOSE_API_DOCS=true in a throwaway process → /docs returns 200 (confirms the gate is a real toggle).


**gotchas:**
- CRITICAL — settings read at import/construction time: `settings = Settings()` runs once (config.py:437), and the FastAPI() ctor evaluates `settings.expose_api_docs` once when app/main.py is imported. The *_url kwargs are then frozen on the singleton `app.main.app`. Setting os.environ['EXPOSE_API_DOCS'] AFTER app.main is first imported has NO effect. Do not try to toggle it per-test on the shared app.
- CRITICAL — why test_contract.py must switch to app.openapi(): with openapi_url=None the /openapi.json route is unregistered, so BOTH the direct `client.get('/openapi.json')` (line 31) and `schemathesis.from_asgi('/openapi.json', app)` (line 54) get a 404 and fail. `app.openapi()` is the in-process schema generator and is INDEPENDENT of route registration — verified: it returns full openapi/info/paths even when openapi_url=None. Feed schemathesis via `from_dict(app.openapi())`, never the HTTP route.
- Do NOT globally set EXPOSE_API_DOCS=true in conftest.py (lines 32-37) to 'fix' the contract test — that would expose docs for the whole session and directly break the new 404-by-default test, since both tests share the one process-wide app. The app.openapi() approach avoids the conflict entirely.
- schemathesis is NOT installed in /root/availai/.venv (it's only in requirements-dev.txt==4.22.4, present in CI). Both contract tests are guarded by `@pytest.mark.skipif(not HAS_SCHEMATHESIS)`, so locally they SKIP — you cannot fully exercise EDIT 4b here. Verify `schemathesis.from_dict` exists in 4.22.4 (schemathesis 4.x reorganized loaders); the code keeps the `from_asgi` fallback and a final `pytest.skip`, so a missing `from_dict` degrades gracefully rather than erroring. The new 404 test in test_security_headers.py has NO such dependency and runs locally.
- Caddy `path` matcher is exact and case-sensitive (no implicit trailing wildcard). `/docs /redoc /openapi.json` match FastAPI's exact default routes. If you ever enable docs behind Caddy, Swagger UI additionally fetches /docs/oauth2-redirect — but with the app-layer default (404) this is moot. Keep `respond 403` to match the existing /metrics block (defense-in-depth: edge blocks even if the app flag is ever flipped).
- Two independent layers by design: Caddy 403 (edge) and FastAPI 404 (app). The app-layer 404 is what the new test asserts (TestClient hits the ASGI app directly, bypassing Caddy). No app test can observe the Caddy 403 — that layer is config-only and not unit-testable here; call it out in the plan as verified-in-deploy, not in pytest.
- pre-commit runs mypy: the three ternaries `"/docs" if settings.expose_api_docs else None` are `str | None`, which is exactly FastAPI's accepted type for docs_url/redoc_url/openapi_url — no annotation or ignore needed. Adding `expose_api_docs: bool = False` needs no validator (mirrors the many existing plain-bool flags).
- Update docs after the change per CLAUDE.md: reflect the new EXPOSE_API_DOCS flag + the /docs,/redoc,/openapi.json lockdown in the relevant docs/APP_MAP_*.md (architecture/interactions) and, if you list env vars anywhere (.env.example / Configuration), add EXPOSE_API_DOCS.


---

## Register the global slowapi rate limiter by adding SlowAPIMiddleware, raise the too-low default limit for htmx-heavy admin sessions, and exempt streaming/infra endpoints. NOTE: the task's premise for gotcha (a) is already solved at the uvicorn layer (see gotchas) — do NOT add a custom X-Forwarded-For key_func.

**files:**
- app/main.py:246-254 (rate-limit setup block — PRIMARY edit: add import + app.add_middleware(SlowAPIMiddleware))
- app/config.py:56 (rate_limit_default default value — raise from 120/minute)
- app/main.py:558-561 (metrics_endpoint — add @limiter.exempt; infra hygiene, optional)
- app/main.py:689-694 (health — add @limiter.exempt; infra hygiene, optional)
- app/main.py:763-764 (health_ready — add @limiter.exempt; infra hygiene, optional)
- app/routers/events.py:20-35 (event_stream SSE — add `from ..rate_limit import limiter` + @limiter.exempt; REQUIRED)
- app/routers/htmx/search_views.py:227-231 (search_stream SSE — add `from ...rate_limit import limiter` + @limiter.exempt; REQUIRED)
- tests/test_main.py:405-420 (add new TestRateLimitMiddleware class next to existing TestRateLimitHandler)


**current_code:**
app/main.py:246-254 (the entire block to edit):
```
# Rate limiting (slowapi)
from .rate_limit import limiter

app.state.limiter = limiter
if settings.rate_limit_enabled:
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type, unused-ignore]  # slowapi handler is narrower than Starlette's protocol; slowapi absent from the hook env, so the ignore is unused there
```

app/config.py:56:
```
    rate_limit_default: str = "120/minute"
    rate_limit_enabled: bool = True
```

app/rate_limit.py:42-47 (the limiter — key_func stays get_remote_address, DO NOT change):
```
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[settings.rate_limit_default],
    enabled=settings.rate_limit_enabled,
    storage_uri=_resolve_storage(),
)
```

app/routers/events.py:20-35 (SSE endpoint to exempt; note: this file does NOT currently import limiter):
```
router = APIRouter(tags=["events"])


@router.get("/api/events/stream")
async def event_stream(request: Request, user=Depends(require_user)):
```

app/routers/htmx/search_views.py:227-231 (2nd SSE endpoint to exempt):
```
@router.get("/v2/partials/search/stream")
async def search_stream(
    request: Request,
    search_id: str = Query(...),
    user: User = Depends(require_user),
):
```

app/main.py:558 and :689 and :763 (infra endpoints to exempt):
```
@app.get("/metrics", include_in_schema=False, dependencies=[Depends(_metrics_auth)])
async def metrics_endpoint() -> Response:
...
@app.get("/health")
async def health(
...
@app.get("/health/ready")
async def health_ready() -> JSONResponse:
```


**edit_plan:**
1) PRIMARY (app/main.py:250-254): inside the existing `if settings.rate_limit_enabled:` block, add the import and register the middleware. Final block:
```
if settings.rate_limit_enabled:
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.middleware import SlowAPIMiddleware

    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type, unused-ignore]  # slowapi handler is narrower than Starlette's protocol; slowapi absent from the hook env, so the ignore is unused there
    app.add_middleware(SlowAPIMiddleware)
```
Import path MUST be `slowapi.middleware` (not re-exported from top-level `slowapi` — verified `hasattr(slowapi,'SlowAPIMiddleware')==False`). No `# type: ignore` on the add_middleware line (see gotcha 8). This is the whole fix for "global limiter never enforced" — the decorators already work; only the `default_limits` needed the middleware.

2) DEFAULT LIMIT (app/config.py:56): change `rate_limit_default: str = "120/minute"` -> `rate_limit_default: str = "600/minute"`. Rationale in gotcha 5. (Alt if you want burst protection: `"1200/minute;40/second"`.) This is the recommended safe default for htmx-heavy admin.

3) EXEMPT the two SSE endpoints (REQUIRED — functional):
   - app/routers/events.py: add `from ..rate_limit import limiter` to the import group, then add `@limiter.exempt` as the innermost decorator directly above `async def event_stream` (between `@router.get(...)` and `async def`).
   - app/routers/htmx/search_views.py: add `from ...rate_limit import limiter` (THREE dots — file is app/routers/htmx/) to imports, then `@limiter.exempt` directly above `async def search_stream`.

4) EXEMPT infra endpoints (recommended hygiene): add `@limiter.exempt` directly above `async def metrics_endpoint` (main.py:559), `async def health` (main.py:690), and `async def health_ready` (main.py:764). `limiter` is already imported at main.py:247 so it's in scope at all three sites. Order: `@app.get(...)` then `@limiter.exempt` then `async def`.

Do NOT touch app/rate_limit.py — key_func=get_remote_address is already correct (gotcha 1). Do NOT decorate the every-2s/3s status pollers — the raised 600/min default absorbs them (gotcha 5 math).


**test_file:**
tests/test_main.py (add a new class `TestRateLimitMiddleware` immediately after the existing `TestRateLimitHandler` at line 405). Do NOT create a new test file — main-app/middleware tests live in test_main.py.


**test_pattern_to_mirror:**
Mirror the self-contained mini-app pattern at tests/test_main.py:480-510 (`test_csrf_middleware_full_flow` / the `csrf_app`): it builds a FRESH app, adds exactly one middleware, wraps it in `TestClient`, and asserts status codes per request. Build a fresh `FastAPI()` + a NEW `Limiter(key_func=get_remote_address, default_limits=["3/minute"])` (in-memory storage, isolated per instance), set `app.state.limiter`, `app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)`, `app.add_middleware(SlowAPIMiddleware)`, then define two routes: a plain `@app.get("/limited")` (undecorated → default limit applies) and `@app.get("/exempt")` + `@limiter.exempt`. Drive with `TestClient` (its request.client.host is constant "testclient" so all calls share one key and accumulate). Assert: first 3 GET /limited == 200, the 4th and 5th == 429; and 8x GET /exempt all == 200. Also assert the 429 body uses the repo error key: `resp.json()["error"].startswith("Rate limit exceeded")` (verified live — slowapi's default handler returns `{"error": "Rate limit exceeded: 3 per 1 minute"}`, matching the repo convention of `["error"]` not `["detail"]`). Optionally add a second test asserting the REAL app wired the middleware: `from app.main import app; assert "SlowAPIMiddleware" in [m.cls.__name__ for m in app.user_middleware]` (mirrors the `user_middleware`/`middleware_classes` inspection in `test_csrf_middleware_skipped_in_testing` at test_main.py:469-474) — guard it under `if settings.rate_limit_enabled`. VERIFIED end-to-end: an isolated app with default_limits=["3/minute"] returned [200,200,200,429,429] on /limited and all-200 on the exempt route.


**verification:**
Live-verify on staging (single-user, per-IP): (1) temporarily set `RATE_LIMIT_DEFAULT=5/minute` in staging .env and redeploy via ./deploy.sh so the low limit takes effect; (2) hammer an UNdecorated GET route from one client IP, e.g. `for i in $(seq 1 8); do curl -s -o /dev/null -w '%{http_code}\n' https://app.availai.net/v2/partials/proactive/badge -H 'Cookie: session=<valid>'; done` — expect the first ~5 to return 200 then 429 (429 body `{\"error\":\"Rate limit exceeded: 5 per 1 minute\"}`); (3) confirm exemption: `for i in $(seq 1 20); do curl -s -o /dev/null -w '%{http_code}\n' https://app.availai.net/health; done` — expect ALL 200 (never 429), and open the SSE stream `curl -N https://app.availai.net/api/events/stream -H 'Cookie: session=<valid>'` repeatedly to confirm no 429; (4) restore `RATE_LIMIT_DEFAULT=600/minute` and redeploy. Also run the unit test: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_main.py -k RateLimit -v`. Note slowapi's Limiter here has headers_enabled=False, so do NOT look for X-RateLimit-* response headers as evidence — verify via the 200-then-429 status transition instead.


**gotchas:**
- GOTCHA (a) PREMISE IS ALREADY SOLVED — do NOT add a custom key_func. The deployed stack runs uvicorn with `--proxy-headers --forwarded-allow-ips 172.28.0.0/24` (docker-compose.yml:92) and the compose default network is pinned to that subnet (docker-compose.yml:255-259) where Caddy lives. uvicorn 0.51.0's ProxyHeadersMiddleware runs at the ASGI-server layer and rewrites scope['client'] to the real client IP by walking X-Forwarded-For from the right, skipping trusted hosts. So `get_remote_address(request)` (reads request.client.host) ALREADY returns the real per-user client IP, NOT Caddy's. Caddy sets X-Forwarded-For automatically (default reverse_proxy) — the Caddyfile has NO `trusted_proxies` and does NOT set X-Real-IP. A hand-rolled key_func that reads the LEFTMOST XFF entry would be strictly WORSE: spoofable (a client can inject a fake leftmost value), whereas uvicorn's rightmost-untrusted walk is spoof-resistant. KEEP key_func=get_remote_address unchanged. Caveat to note in the task: the bare Dockerfile CMD (Dockerfile:115) omits --forwarded-allow-ips (uvicorn then defaults trusted_hosts=127.0.0.1 and get_remote_address collapses to Caddy's container IP → one global bucket); correctness depends on deploying via docker-compose, which the repo does.
- Existing @limiter.limit-decorated routes are AUTO-EXEMPT from the new default limit — no double-counting. slowapi's `_should_exempt` returns True whenever the route name is in `limiter._route_limits`. All the decorated routes (app/routers/auth.py, ai.py, sources.py, admin/system.py, documents.py, prepayment_confirm.py) keep their own per-route limits; the middleware's `default_limits` only bites UNdecorated routes.
- Static mounts are AUTO-EXEMPT — no work needed. `_find_route_handler` only returns handlers for routes that have an `.endpoint`; the `/static` and `/static/assets` Mounts (main.py:522-528) expose `.app`, not `.endpoint`, so handler is None → `_should_exempt` returns True. /metrics, /health, and the SSE routes ARE real routes (have endpoints) so they are NOT auto-exempt and must be decorated with @limiter.exempt.
- SSE endpoints MUST be exempted (functional): `/api/events/stream` (app/routers/events.py:23) and `/v2/partials/search/stream` (app/routers/htmx/search_views.py:227) return long-lived EventSourceResponse. Unexempted, the default limit counts every (re)connect and the middleware sets should_inject_headers=True → header injection on a streaming response. SlowAPIMiddleware is a BaseHTTPMiddleware; reassuringly the app ALREADY wraps every response (including these SSE routes, which work in prod) in 3 BaseHTTPMiddleware layers via the `@app.middleware('http')` csp/request_id/api_version handlers (main.py:568,590,660), so adding one more BaseHTTPMiddleware is consistent and won't newly break streaming — but exempting the two SSE routes cleanly avoids both the count and the header injection.
- Default 120/minute is too low for an htmx-heavy admin session and users would 429 their OWN UI. Poll cadences (grepped from templates): every 2s = 30 req/min EACH (`/api/enrich/company/{id}/status`, `/v2/partials/prospecting/{id}/enrich-status`, `/v2/partials/customers/{id}/suggested-contacts/status`, `/v2/partials/materials/{id}/enrich-status` + crosses-status, `/v2/partials/vendors/{id}/ai/find-contacts-status`); every 3s = 20/min (`/v2/partials/resell/{id}/outreach`, dossier datasheet-status); every 15s and `load, every 60s` badges (`/v2/partials/proactive/badge`, `/v2/partials/follow-ups/badge`, `/v2/partials/alerts/{id}/badge`). Two or three concurrent every-2s pollers alone = 60-90/min, plus per-navigation partial fan-out. Recommend raising rate_limit_default (config.py:56) to `600/minute` (10/s sustained) — comfortably absorbs the pollers so they need NOT be individually exempted. Alt for burst protection: compound `1200/minute;40/second`.
- Middleware position: adding it in the `if settings.rate_limit_enabled:` block (~main.py:254) makes it the FIRST add_middleware call → INNERMOST Starlette middleware (add_middleware inserts at index 0; the stack is applied in reverse). This is fine/correct: uvicorn ProxyHeadersMiddleware already fixed scope['client'] before ANY Starlette middleware runs, and SlowAPIMiddleware reads app.state.limiter + app.routes (position-independent). If you'd rather reject floods before Session/CSRF/GZip decode, move the `app.add_middleware(SlowAPIMiddleware)` line next to `app.add_middleware(PrometheusMiddleware)` at main.py:555 — marginal, not required.
- Import path: `from slowapi.middleware import SlowAPIMiddleware`. It is NOT re-exported from the top-level `slowapi` package (verified: hasattr(slowapi,'SlowAPIMiddleware') is False). The existing block already imports `_rate_limit_exceeded_handler` from `slowapi` and `RateLimitExceeded` from `slowapi.errors` — add the middleware import alongside them.
- mypy / hook-env: NO `# type: ignore` is needed on `app.add_middleware(SlowAPIMiddleware)` or the import. pyproject.toml:20 sets `ignore_missing_imports = true` and the pre-commit mypy hook passes `--ignore-missing-imports` with slowapi NOT in its additional_dependencies (the existing exception-handler comment literally says 'slowapi absent from the hook env'). So SlowAPIMiddleware resolves to Any in the hook env → add_middleware(Any) type-checks clean. Do NOT copy the neighboring line's `# type: ignore[arg-type, unused-ignore]` onto the new line — it would be an unused ignore.
- 429 response shape: slowapi's default `_rate_limit_exceeded_handler` returns JSON `{"error": "Rate limit exceeded: <limit>"}` (verified live). This already matches the repo convention (tests assert `['error']`, never `['detail']`), so no custom handler is needed — the one registered at main.py:254 is correct.
- Test isolation: the 429 test MUST build a fresh FastAPI + a fresh Limiter with a low `default_limits=["3/minute"]` (in-memory storage is per-Limiter-instance, so isolated and deterministic). Do NOT import the global `app`/`limiter` for the 429 assertion — the global limiter carries the 120/600-per-minute setting and shared storage, and the full middleware stack (Session/CSRF-in-prod/auth deps) would make counts nondeterministic and require auth. TestClient sets request.client.host='testclient' for every call, so all requests share one key and the counter accumulates as intended.
- The middleware is only added inside `if settings.rate_limit_enabled:` (default True in config.py:57). The isolated mini-app test is env-independent, but any assertion against the real `app.user_middleware` should be guarded by `if settings.rate_limit_enabled:` so it doesn't fail if someone disables the flag.


---

## Stop demoted ADMIN_EMAILS users from being silently re-promoted to admin on their next login. Add a persistent per-user boolean `users.admin_bootstrap_opted_out` that latches True when an admin explicitly demotes a bootstrap admin via the Users tab, and have the login bootstrap in auth.py skip re-promotion when it is set (cleared again on any re-promote to admin).

**files:**
- app/models/auth.py (User model, add column near the 'User-management foundation' block lines 35-41; imports Boolean+text already present at line 5)
- app/routers/auth.py (login bootstrap gate, lines 154-157)
- app/routers/admin/users.py (change_user_role, lines 253-288; edit around old_role/assign at lines 274-278)
- alembic/versions/189_admin_bootstrap_optout.py (NEW migration, mirror 184_user_reports_to.py)
- MIGRATION_NUMBERS_IN_FLIGHT.txt (append claim line for 189)
- tests/test_routers_auth.py (NEW re-login regression test, mirror test_callback_auto_admin_promotion at line 210)
- tests/test_migration_189_admin_bootstrap_optout.py (NEW round-trip test, mirror tests/test_migration_184_user_reports_to.py)
- tests/test_user_management.py (NEW endpoint test in TestChangeRole, mirror test_admin_can_change_role at line 169)


**current_code:**
app/routers/auth.py:154-157 (the gate to change):
    # Bootstrap admin: auto-promote users in admin_emails env var
    if user.email.lower() in settings.admin_emails and user.role != UserRole.ADMIN:
        user.role = UserRole.ADMIN
        logger.info(f"Auto-promoted {user.email} to admin via admin_emails bootstrap")

app/routers/admin/users.py:274-288 (the demote action; _validate_assignable_role returns a plain str, old_role is a str):
    old_role = str(target.role)
    if old_role == valid_role:
        return _render(db, request)  # no-op, nothing to audit

    target.role = valid_role
    record_user_audit(
        db,
        actor_id=admin.id,
        target_user_id=target.id,
        action=UserAuditAction.ROLE_CHANGE,
        detail={"from": old_role, "to": valid_role},
    )
    db.commit()

app/models/auth.py:35-41 (anchor block; Boolean and text are imported at line 5; note existing boolean pattern at line 77 `notify_resource_alert_enabled = Column(Boolean, nullable=False, default=True, server_default=text(\"true\"))`):
    # User-management foundation (Phase 1)
    last_login_at = Column(UTCDateTime, nullable=True)
    # Explicit per-user access overrides ONLY: {access_key_str: bool}. ...
    access_overrides = Column(JSON, default=dict)
    invited_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

app/config.py:222 (setting the gate reads): admin_emails: str | list[str] = ""  (parsed to list[str] by parse_csv_fields validator, config.py:415-430)


**edit_plan:**
MODEL (app/models/auth.py) — add the column inside the "User-management foundation" block, immediately after `invited_by_id` (line 41):
    # Latches True when an ADMIN_EMAILS bootstrap admin is explicitly demoted via the
    # admin Users tab (change_user_role). The login bootstrap in auth.py skips re-promotion
    # while this is set, so a demoted admin stays demoted across logins. Cleared when an
    # admin re-promotes them to admin.
    admin_bootstrap_opted_out = Column(Boolean, nullable=False, default=False, server_default=text("false"))
(Boolean + text already imported at line 5; mirrors the exact nullable=False/default/server_default pattern of notify_resource_alert_enabled at line 77 and the can_approve_* columns at 87-105.)

AUTH GATE (app/routers/auth.py:155) — add the opt-out to the condition:
    if (
        user.email.lower() in settings.admin_emails
        and user.role != UserRole.ADMIN
        and not user.admin_bootstrap_opted_out
    ):
        user.role = UserRole.ADMIN
        logger.info(f"Auto-promoted {user.email} to admin via admin_emails bootstrap")

DEMOTE ACTION (app/routers/admin/users.py) — after `target.role = valid_role` (line 278), before record_user_audit, latch/clear the flag (no settings import needed; UserRole comparison already used at lines 268/271):
    target.role = valid_role
    # Persist a demotion's intent so the ADMIN_EMAILS login bootstrap (auth.py) does not
    # silently re-promote this user on their next login; re-promoting to admin clears it.
    if valid_role == UserRole.ADMIN:
        target.admin_bootstrap_opted_out = False
    elif old_role == UserRole.ADMIN:
        target.admin_bootstrap_opted_out = True

MIGRATION (alembic/versions/189_admin_bootstrap_optout.py) — mirror 184 exactly (module vars revision/down_revision/branch_labels/depends_on + upgrade/downgrade):
    revision = "189_admin_bootstrap_optout"           # 26 chars <= 32 (PG VARCHAR(32))
    down_revision = "188_canonical_offers_excess_fk"   # current single head — VERIFY at write time
    def upgrade():
        op.add_column("users", sa.Column("admin_bootstrap_opted_out", sa.Boolean(),
                      nullable=False, server_default=sa.text("false")))
    def downgrade():
        op.drop_column("users", "admin_bootstrap_opted_out")
Include the standard file-header docstring (what/why/Revises/Called by alembic). server_default=sa.text("false") is REQUIRED (not just default=) so the NOT NULL add backfills existing rows on PG, and it must string-match the model's server_default so the drift gate sees no diff.

CLAIM LINE (MIGRATION_NUMBERS_IN_FLIGHT.txt) — append (append-only, same PR):
189  <branch-name>  users.admin_bootstrap_opted_out (Boolean NOT NULL server_default false) — latch a demoted ADMIN_EMAILS bootstrap admin's opt-out so the auth.py login bootstrap does not re-promote them on next login; additive/reversible (downgrade drops the column); round-tripped upgrade->downgrade->upgrade on a THROWAWAY PG 16 (staging untouched); chains onto 188_canonical_offers_excess_fk; single head verified via `alembic heads`


**needs_migration:**
True


**migration_notes:**
Table/column: users.admin_bootstrap_opted_out — Boolean, NOT NULL, server_default text('false'). New file alembic/versions/189_admin_bootstrap_optout.py, revision='189_admin_bootstrap_optout' (26 chars, within PG alembic_version VARCHAR(32)), down_revision='188_canonical_offers_excess_fk' (current single head — RE-VERIFY with `alembic heads` at write time). UP: `op.add_column('users', sa.Column('admin_bootstrap_opted_out', sa.Boolean(), nullable=False, server_default=sa.text('false')))` — PG-safe because the server_default backfills existing rows during the NOT NULL add (no table rewrite lock concern at this table size; single ALTER). DOWN: `op.drop_column('users', 'admin_bootstrap_opted_out')` — fully reversible. No FK, no index (the column is only read at login by primary-key-loaded user, never filtered on), so no create_foreign_key/create_index. Round-trip locally: upgrade -> downgrade -> upgrade on a THROWAWAY PG 16 (NOT staging db — an ahead-of-staging revision crash-loops the app). DRIFT-GATE REGISTRATION: none in scripts/check_schema_matches_models.py (clean additive column = zero drift); the required registrations are (1) the append-only claim line in MIGRATION_NUMBERS_IN_FLIGHT.txt for number 189, and (2) keeping model + migration server_default both text('false') so CI's compare_metadata step reports no diff. After creating, run `alembic heads` and confirm a SINGLE head; if a newer migration landed on main first, re-chain down_revision onto the new head (number stays 189).


**test_file:**
tests/test_routers_auth.py (primary re-login regression test). PLUS two supporting tests: tests/test_migration_189_admin_bootstrap_optout.py (new migration round-trip) and tests/test_user_management.py TestChangeRole (endpoint sets/clears the flag).


**test_pattern_to_mirror:**
RE-LOGIN REGRESSION — mirror tests/test_routers_auth.py:210-230 `test_callback_auto_admin_promotion` (uses `@patch("app.routers.auth.http")`, `_get_oauth_state(auth_client)`, monkeypatch `settings.admin_emails`, `mock_http.post = AsyncMock(return_value=_mock_token_response())`, `mock_http.get = AsyncMock(return_value=_mock_graph_me(email=..., name=...))`, GET /auth/callback, then `db_session.query(User).filter_by(email=...)` + assert role). New test seeds the POST-DEMOTE state directly (create a User with email in admin_emails, role='trader', azure_id set, admin_bootstrap_opted_out=True, commit via db_session — the auth_client fixture at line 68 overrides get_db with the same db_session), then drives the callback and asserts `user.role != 'admin'` (still 'trader') and `user.admin_bootstrap_opted_out is True`. Contrast: an identical seed WITHOUT the flag (or a fresh admin_emails user) still promotes — that positive path is already test_callback_auto_admin_promotion. Note the `_legacy_open_provisioning` autouse fixture (line 55) sets enable_user_allowlist=False for this module, which is fine.

ENDPOINT (flag set/clear) — mirror tests/test_user_management.py:169 `test_admin_can_change_role` (admin_client fixture line 50, `_make_user` helper line 33, `db_session.refresh`, `_audit_rows`). Seed a bootstrap admin via `_make_user(db_session, email='boot@trioscs.com', role='admin')` PLUS a second active admin so the last-admin guard (users.py:271, tested at line 199) does not block; POST /api/admin/users/{id}/role data={'role':'trader'}; refresh; assert role==TRADER and admin_bootstrap_opted_out is True. Then POST role='admin' back; assert admin_bootstrap_opted_out is False.

MIGRATION ROUND-TRIP — mirror tests/test_migration_184_user_reports_to.py in full (importlib.util load of the migration file; TestRevisionMetadata asserts revision id, len<=32, down_revision chains onto head; TestExecution._engine() builds an in-memory SQLite users table via MetaData/Table, _has_col checks inspect(engine).get_columns; run_ops from tests.migration_harness drives upgrade->downgrade->upgrade). SIMPLER than 184: this column has NO foreign key, so DROP the `patch.object(Operations, 'create_foreign_key'/'drop_constraint', ...)` block entirely — call run_ops directly. Assert only column presence/absence (not the default value) to stay SQLite-value-agnostic.


**verification:**
After deploy to staging (hosted CLI, no browser): (1) confirm the migration applied — `docker compose exec app alembic current` shows head = the new 189 revision, and `docker compose exec db psql -U $POSTGRES_USER -d $POSTGRES_DB -c '\\d users'` lists `admin_bootstrap_opted_out | boolean | not null | false`. (2) Live-drive the demote latch on real PG: as the seed admin (docker-net IP + session), POST the demote with CSRF — `curl -sS -X POST https://<staging>/api/admin/users/<id>/role -H 'x-csrftoken: <tok>' --cookie <session> -d 'role=trader'` (403 without x-csrftoken is expected, not an authz bug), then `psql ... -c \"select email, role, admin_bootstrap_opted_out from users where id=<id>;\"` must show role=trader AND admin_bootstrap_opted_out=t. (3) Re-promote: POST role=admin, then psql must show admin_bootstrap_opted_out=f. (The OAuth re-login leg cannot be driven headlessly on staging; it is covered by the test_routers_auth.py regression test.) (4) `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_auth.py tests/test_user_management.py tests/test_migration_189_admin_bootstrap_optout.py -v` green; then full suite `grep '^FAILED'` clean.


**gotchas:**
- placeholder


---

## HIGH-SEC-4 — Graph-webhook edge IP allowlist. Add a Caddy named-matcher block that restricts the 3 Microsoft webhook POST routes (/api/webhooks/graph, /api/webhooks/teams, /api/webhooks/acs) to a hardcoded snapshot of Microsoft ServiceTag IP ranges at the edge (returns 403 for any other source IP), leaving the already-shipped app-layer controls (Graph clientState/replay validation; ACS shared-secret fail-closed) untouched behind it. Edge-config only — no unit test; live-verify. CONFIRMED exact paths: app/routers/v13_features/activity.py declares @router.post("/api/webhooks/graph") (L65), "/api/webhooks/teams" (L101), "/api/webhooks/acs" (L142). All three are already CSRF-exempt (app/main.py:474-476) and today fall through Caddy's @api handler (path /api/*) straight to the backend with NO IP restriction.

**files:**
- /root/availai/Caddyfile (edit site: after the @safe_api handle block that closes at L74, before `@api path /api/* /auth/*` at L76; the whole site block is L15-88)
- /root/availai/Caddyfile.example (mirror the same block; its @api handle is L45-49 — insert immediately before L45; note this file still carries the pre-middleware CSP header and reverse_proxy app:8000 instead of `import backend`, so keep its style)
- /root/availai/app/routers/v13_features/activity.py (READ-ONLY context: routes L65/L101/L142; ACS fail-closed shared-secret L157-169; validationToken hardening already shipped L42-62 — do NOT edit)
- /root/availai/app/main.py (READ-ONLY context: CSRF_EXEMPT_URLS L463-480 already lists the 3 webhook paths L474-476)
- /root/availai/docker-compose.yml (READ-ONLY context: caddy service L208-238; Caddyfile mounted :ro at L217; ports 80/443 published directly L211-213)


**current_code:**
Caddyfile L65-88 (the tail of the site block — the webhook paths currently match @api and get proxied unrestricted):
```
	# Safe read-only GET endpoints — 30s browser cache reduces redundant
	# API calls when switching tabs rapidly
	@safe_api {
		method GET
		path /api/companies /api/users/list /api/vendors
	}
	handle @safe_api {
		header Cache-Control "private, max-age=30"
		import backend
	}

	@api path /api/* /auth/*
	handle @api {
		header Cache-Control "no-cache, no-store, must-revalidate"
		import backend
	}

	# Everything else (HTML pages, etc.) — never cache
	handle {
		header Cache-Control "no-store, must-revalidate"
		header Pragma "no-cache"
		import backend
	}
}
```
activity.py L157-169 (ACS already fails closed on the shared secret — this is why acs is scoped separately below):
```
	if not settings.acs_connection_string:
		raise HTTPException(503, "ACS not configured")
	if not settings.acs_webhook_secret:
		logger.warning("ACS webhook secret not configured; rejecting event")
		raise HTTPException(403, "Webhook not authorized")
	provided_secret = request.query_params.get("secret", "")
	if not hmac.compare_digest(provided_secret, settings.acs_webhook_secret):
		logger.warning("ACS webhook secret mismatch; rejecting event")
		raise HTTPException(403, "Webhook not authorized")
```


**edit_plan:**
In /root/availai/Caddyfile, INSERT the following block between the `@safe_api` handle block (closing `\t}` at L74) and `\t@api path /api/* /auth/*` (L76). It MUST come before the `@api` handle — sibling `handle` blocks are mutually exclusive and evaluated top-to-bottom in source order, so if `@api` (path /api/*) is reached first it will proxy the webhook POSTs and defeat the block. Recommended (two-matcher) design — graph/teams gated by the Graph/Azure sender snapshot, acs gated separately by the AzureEventGrid snapshot (acs keeps its shared-secret app check as the primary control, IP block is defense-in-depth):

```
	# ── HIGH-SEC-4: restrict Microsoft webhook routes to Microsoft's published
	# ServiceTag IP ranges (defense-in-depth on top of the app-layer clientState /
	# shared-secret checks). remote_ip = the DIRECT TCP peer; verified to be the real
	# external client IP on this host (caddy access log shows external remote_ip, and
	# client_ip == remote_ip since no trusted_proxies is configured). If this ever moves
	# behind a load balancer/CDN, ALL traffic would appear from the LB IP and this block
	# would break — re-verify after any infra change.
	#
	# SNAPSHOT SOURCE: Azure "Service Tags – Public Cloud" JSON
	# (https://www.microsoft.com/download/details.aspx?id=56519, file
	# ServiceTags_Public_<date>.json). For each chosen tag take values[].properties
	# .addressPrefixes (IPv4 + IPv6 CIDRs). Captured <YYYY-MM-DD>. Regeneration job is a
	# tracked follow-up (roadmap Phase 2) — for now this is a hardcoded snapshot.
	#
	# Graph/Teams change notifications: gate on AzureCloud region prefixes for the
	# tenant's home region(s) (MS publishes no compact Graph-only tag). Fill the CIDRs.
	@ms_graph_webhook_forbidden {
		path /api/webhooks/graph /api/webhooks/teams
		not remote_ip <AzureCloud.<region> CIDRs — IPv4 and IPv6, space-separated>
	}
	handle @ms_graph_webhook_forbidden {
		respond 403
	}

	# ACS/Event Grid delivery originates from the AzureEventGrid tag (a DIFFERENT, compact
	# range than Graph). acs also enforces a ?secret= shared secret in-app (activity.py
	# L157-169) so this is belt-and-suspenders.
	@ms_acs_webhook_forbidden {
		path /api/webhooks/acs
		not remote_ip <AzureEventGrid CIDRs — IPv4 and IPv6, space-separated>
	}
	handle @ms_acs_webhook_forbidden {
		respond 403
	}
```

Note the negation is load-bearing: `not remote_ip <allowed>` matches everything EXCEPT the allowed ranges, so an allowed Microsoft IP does NOT match, the `handle` is skipped, and the request falls through to the existing `@api` handle → backend. A disallowed IP matches → `respond 403` at the edge. Do NOT invert this to a positive `remote_ip` matcher + `respond 403` (that would 403 exactly the Microsoft senders). Then mirror the same two blocks in /root/availai/Caddyfile.example immediately before its `@api` block (L45), using placeholder CIDRs and its reverse_proxy style. Acceptable simpler fallback if the plan-writer prefers one matcher: a single `@ms_webhook_forbidden { path /api/webhooks/graph /api/webhooks/teams /api/webhooks/acs; not remote_ip <union of AzureCloud + AzureEventGrid> }` — accepts a larger list and treats acs's secret as sufficient; the two-matcher form is recommended because the sender ranges genuinely differ and a wrong range would silently break real acs delivery.


**test_file:**
No pytest is added — Caddy edge config is not exercised by the test suite (conftest uses in-memory SQLite via fastapi TestClient, which bypasses Caddy entirely). Verification is LIVE-ONLY (see verification field). The existing app-layer guard tests in /root/availai/tests/test_webhook_security_integration.py must stay green (they prove the clientState/replay/rate-limit → 403 controls independent of the edge and do NOT cover the IP block). OPTIONAL static guard (if the plan-writer wants a regression tripwire): a tiny test that reads /root/availai/Caddyfile as text and asserts it contains `not remote_ip` scoped to each of the 3 webhook paths, added to /root/availai/tests/test_main.py.


**test_pattern_to_mirror:**
For the OPTIONAL static Caddyfile-content guard, mirror the config-as-constant assertion style in /root/availai/tests/test_main.py:487-500 (imports app.main.CSRF_EXEMPT_URLS and asserts on the security-config value directly, no HTTP). A Caddyfile guard would instead `Path("Caddyfile").read_text()` and assert the `@ms_graph_webhook_forbidden`/`@ms_acs_webhook_forbidden` matchers and the three webhook paths are present. For the app-layer controls that must remain unbroken, the closest existing HTTP-level pattern is tests/test_webhook_security_integration.py (admin_client fixture L26-51, e.g. test_empty_payload_returns_403 L147, test_client_state_mismatch_returns_403 L157) — full-app TestClient asserting webhook 403s; keep these green.


**verification:**
See verification field content above (edge-config live-verify: caddy validate → blocked-IP 403 curls on all 3 paths → scoping proof on /api/companies → allowed-IP delivery via temp-allowlist or controlled Graph renewal → remote_ip already confirmed real in caddy logs).


**gotchas:**
- remote_ip vs client_ip / Docker source-IP: VERIFIED OK on this host — caddy v2.11.2 access log shows real external remote_ip (98.186.235.19) and client_ip==remote_ip (no trusted_proxies configured), so Docker's published-port DNAT preserves the client source IP. Use `remote_ip` (direct peer). Do NOT switch to `client_ip` — without a trusting upstream it just mirrors remote_ip. If a load balancer/CDN is ever introduced, every request would appear from the LB IP and this allowlist would 403 everyone — re-verify after any infra change.
- handle ordering: sibling `handle` blocks are mutually exclusive and evaluated top-to-bottom in source order. The new webhook blocks MUST precede the `@api` handle (Caddyfile L76 `path /api/* /auth/*`), or @api proxies /api/webhooks/* first and the block is dead. Inserting after the @safe_api handle (closes L74) is correct — webhook POSTs never match @safe_api (method GET only, specific paths).
- Negation is load-bearing: `not remote_ip <allowed>` = 'block everything except the allowed ranges' → allowed IPs skip the handle and fall through to @api. A positive `remote_ip` matcher + respond 403 would 403 exactly the Microsoft senders. Easy to get backwards.
- ACS scope: /api/webhooks/acs already fails closed on the ?secret= shared secret (activity.py:157-169) and its sender is Azure Event Grid (AzureEventGrid tag) — a DIFFERENT, compact range than Graph. Gating acs with the Graph/AzureCloud range would break real acs delivery. Use a separate AzureEventGrid matcher for acs (recommended) or leave acs out of the IP block entirely (secret suffices).
- teams + MVP_MODE: /api/webhooks/teams returns an app-level 404 when MVP_MODE=true (activity.py:111-112). During the 'allowed IP' live-verify, teams may return 404 from the app rather than 200 — that still proves the edge did NOT 403 it. Don't read the app-404 as a block failure.
- IPv6: the caddy container listens on [::]:80 and [::]:443 (docker ps shows [::]:443->443). Include the IPv6 addressPrefixes from the ServiceTags JSON in the same remote_ip list or IPv6 Microsoft senders get 403'd.
- Read-only mount: Caddyfile is mounted `:ro` (docker-compose.yml L217). Edit the HOST file, then reload Caddy (`caddy reload` or `docker compose restart caddy`). Confirm whether deploy.sh reloads Caddy — it primarily recreates app/enrichment-worker, so a Caddy-only change may need a manual reload.
- Snapshot staleness: Microsoft publishes NO compact stable IP range for Graph change notifications (their guidance is clientState/token validation — already done at the app layer, PR #563). The edge allowlist is inherently a broad snapshot; MS rotates ranges (~weekly). This task ships a HARDCODED snapshot with a source/date header comment; the auto-regeneration job is a tracked follow-up (roadmap Phase 2), out of scope here.
- No pytest harness for Caddy: the suite bypasses Caddy (SQLite + TestClient). Nothing in tests/ validates the edge block; the existing app-layer webhook 403 tests (tests/test_webhook_security_integration.py) must remain green but cannot cover this. `mypy app/`/ruff/pre-commit are irrelevant to a Caddyfile edit — the only pre-deploy gate is `caddy validate`.
- Caddyfile.example drift: the changed-files formatting/no-unrelated-drift gate (docs/BRANCH_AND_CI_WORKFLOW.md) — mirror the block into Caddyfile.example in the SAME PR, but keep its existing style (it uses `reverse_proxy app:8000` not `import backend`, and still carries the old inline CSP header). Don't bundle unrelated example-file cleanup.


---

## Password-login fail-boot guard (launch blocker #1): make a real (non-TESTING) boot raise RuntimeError when ENABLE_PASSWORD_LOGIN=true unless the operator has acknowledged the auth-bypass risk via ALLOW_PASSWORD_LOGIN_RISK=true; add a deploy.sh preflight assertion; keep staging (which sets the ack env) booting. Today the code only logs CRITICAL and continues.

**files:**
- /root/availai/app/startup.py (edit site: lines 119-126, inside run_startup_migrations, BEFORE the TESTING short-circuit at line 128 and the DB block at line 132)
- /root/availai/app/routers/auth.py (add helper after line 244, immediately below password_login_env_enabled which ends at line 244; _password_login_enabled begins at line 247)
- /root/availai/app/config.py (add field after line 57 rate_limit_enabled; Settings class starts line 37, singleton settings = Settings() at line 437)
- /root/availai/.env.example (add after line 20 ENABLE_PASSWORD_LOGIN=false, inside the 'Local Password Login' block at lines 13-23)
- /root/availai/deploy.sh (add preflight after the NO_COMMIT block closes at line 73 `fi`, before Step 2 comment at line 75; existing exit codes 1-4 are taken, use exit 5)
- /root/availai/tests/test_auth_password_guard.py (add new test class; file currently ends at line 99)


**current_code:**
app/startup.py:119-126 (the guard to REPLACE):
```
    # Warn if password login backdoor is active outside test mode
    from .routers.auth import password_login_env_enabled

    if password_login_env_enabled() and not os.getenv("TESTING"):
        logger.critical(
            "ENABLE_PASSWORD_LOGIN is active in non-test mode. "
            "This creates an authentication bypass. Disable before production use."
        )
```

app/startup.py:128-146 (unchanged, runs AFTER the guard — note _create_default_user_if_env_set at 141-142 stays gated by password_login_env_enabled()):
```
    if os.environ.get("TESTING"):
        logger.info("TESTING mode — skipping startup migrations")
        return
    ...
    _verify_encryption_canary()
    if password_login_env_enabled():
        _create_default_user_if_env_set()
    _seed_admin_user_if_env_set()
```

app/routers/auth.py:235-244 (existing helper to MIRROR — note it deliberately reads os.getenv at call time, not settings):
```
def password_login_env_enabled() -> bool:
    """True iff ENABLE_PASSWORD_LOGIN is set truthy in the environment.
    ... Read at call time (not via config.py's import-time Settings) so the flag
    can be toggled per-process — the behavior the tests and operator rely on.
    """
    return os.getenv("ENABLE_PASSWORD_LOGIN", "false").lower() == "true"
```

app/config.py:55-57 (insertion anchor):
```
    # --- Rate limiting ---
    rate_limit_default: str = "120/minute"
    rate_limit_enabled: bool = True
```

.env.example:20 (anchor, ends the password-login block):
```
ENABLE_PASSWORD_LOGIN=false
```

deploy.sh:73-75 (insertion anchor — line 73 `fi` closes the `if [ "$NO_COMMIT" = false ]` block so a preflight placed here runs on BOTH full and --no-commit/staging deploys):
```
fi

# Step 2: Rebuild app with a unique BUILD_COMMIT each deploy.
```


**edit_plan:**
1) app/routers/auth.py — add a runtime-read helper immediately after password_login_env_enabled (after line 244), mirroring it exactly:
```
def password_login_risk_acknowledged() -> bool:
    """True iff ALLOW_PASSWORD_LOGIN_RISK is set truthy in the environment.

    Explicit operator acknowledgement that the ENABLE_PASSWORD_LOGIN auth bypass
    is intended on this (non-production) environment. Read at call time — exactly
    like ``password_login_env_enabled`` — NOT via config.py's import-time
    Settings, so the boot guard and its tests can toggle it per-process.
    """
    return os.getenv("ALLOW_PASSWORD_LOGIN_RISK", "false").lower() == "true"
```

2) app/startup.py — replace the log-only block at lines 119-126 with a fail-boot guard. Keep the function-local import (runtime read; avoids import-time capture and auth-router circular imports):
```
    # Fail-boot guard: password login is an auth bypass. On a real (non-TESTING)
    # boot it may run ONLY when the operator has explicitly acknowledged the risk
    # via ALLOW_PASSWORD_LOGIN_RISK=true (staging sets this). Otherwise refuse to
    # start so the bypass can never reach an unacknowledged environment.
    from .routers.auth import password_login_env_enabled, password_login_risk_acknowledged

    if password_login_env_enabled() and not os.getenv("TESTING"):
        if not password_login_risk_acknowledged():
            raise RuntimeError(
                "ENABLE_PASSWORD_LOGIN=true creates an authentication bypass and is "
                "refused at boot. Disable it, or set ALLOW_PASSWORD_LOGIN_RISK=true to "
                "acknowledge the risk (non-production environments only, e.g. staging)."
            )
        logger.critical(
            "ENABLE_PASSWORD_LOGIN is active in non-test mode with "
            "ALLOW_PASSWORD_LOGIN_RISK=true — authentication bypass acknowledged. "
            "Acceptable only on non-production environments."
        )
```
This sits before the TESTING short-circuit (line 128) so it governs real boots, and before any DB access (line 132) so the RuntimeError raises cleanly with no DB dependency. No other lines in run_startup_migrations change.

3) app/config.py — add after line 57 (documents/loads the env var; the guard itself reads os.getenv at runtime — see gotcha):
```
    # Explicit acknowledgement that ENABLE_PASSWORD_LOGIN (an auth bypass) is
    # intentional on a non-prod env. Boot refuses password login without this.
    # The boot guard reads os.getenv at runtime (auth.password_login_risk_
    # acknowledged); this field documents the var and lets Settings load it.
    allow_password_login_risk: bool = False
```

4) .env.example — add after line 20:
```
# Set true ONLY on a non-production env to acknowledge the ENABLE_PASSWORD_LOGIN
# auth-bypass risk. Boot RAISES a RuntimeError if ENABLE_PASSWORD_LOGIN=true
# without this. Staging sets this to keep booting; leave false everywhere else.
ALLOW_PASSWORD_LOGIN_RISK=false
```

5) deploy.sh — insert after line 73 `fi`, before the Step 2 comment (line 75). Runs on every deploy incl. --no-commit/staging; turns a confusing health-check timeout into a clear early error:
```
# Step 1.5: Preflight — refuse to deploy a password-login backdoor without an
# explicit risk acknowledgement in .env. The app also fail-boots on this
# (app/startup.py), but asserting here fails fast with a clear message instead
# of a health-check timeout. Staging sets ALLOW_PASSWORD_LOGIN_RISK=true → passes.
if grep -qiE '^[[:space:]]*ENABLE_PASSWORD_LOGIN[[:space:]]*=[[:space:]]*true' .env 2>/dev/null \
   && ! grep -qiE '^[[:space:]]*ALLOW_PASSWORD_LOGIN_RISK[[:space:]]*=[[:space:]]*true' .env 2>/dev/null; then
    echo "ERROR: ENABLE_PASSWORD_LOGIN=true but ALLOW_PASSWORD_LOGIN_RISK is not true in .env." >&2
    echo "Password login is an auth bypass. Set ALLOW_PASSWORD_LOGIN_RISK=true to acknowledge" >&2
    echo "(non-production environments only), or disable ENABLE_PASSWORD_LOGIN." >&2
    exit 5
fi

```

6) Docs: per CLAUDE.md 'After any code change, update the relevant APP_MAP doc(s) in the same PR' — add a one-line note in docs/APP_MAP_ARCHITECTURE.md (Auth/config section) that ENABLE_PASSWORD_LOGIN now hard-fails boot unless ALLOW_PASSWORD_LOGIN_RISK=true.


**migration_notes:**
No DB migration. This is a boot-time config/guard change only — no schema, no models/ change, no alembic revision, and no MIGRATION_NUMBERS_IN_FLIGHT.txt claim needed.


**test_file:**
/root/availai/tests/test_auth_password_guard.py — add a new class TestStartupPasswordFailBoot after the existing TestStartupPasswordWarning (file currently ends at line 99). This file already owns the startup password-guard behavior and has the env-toggling harness. Also add `import pytest` at the top (the file currently imports only os, unittest.mock.patch, loguru.logger).


**test_pattern_to_mirror:**
Two patterns to mirror:
(a) tests/test_auth_password_guard.py:17-63 (TestStartupPasswordWarning + _critical_msgs_from_startup) — shows the exact env-toggling idiom: `with patch.dict(os.environ, env, clear=False):` then `os.environ.pop("TESTING", None)`, call run_startup_migrations(), and restore in finally. The 'raises without ack' test copies this but wraps the call in `pytest.raises(RuntimeError, match="ALLOW_PASSWORD_LOGIN_RISK")` (the guard raises BEFORE any DB access, so no DB patching is needed).
(b) tests/test_startup.py:731-776 (TestRunStartupMigrationsNonTesting.test_non_testing_mode_runs_fast_migrations_only) — shows how to run run_startup_migrations() with TESTING popped by patching app.startup.engine + every FAST seed helper so no real DB is touched, and restoring TESTING='1' in finally. The 'succeeds with ack' test copies this, adding env {ENABLE_PASSWORD_LOGIN:'true', ALLOW_PASSWORD_LOGIN_RISK:'true'} and asserting run_startup_migrations() returns without raising.

Concrete new test to add:
```
class TestStartupPasswordFailBoot:
    """Real (non-TESTING) boot must RAISE when ENABLE_PASSWORD_LOGIN=true unless
    ALLOW_PASSWORD_LOGIN_RISK=true acknowledges the auth-bypass risk."""

    def test_boot_raises_without_ack(self):
        from app.startup import run_startup_migrations

        with patch.dict(os.environ, {"ENABLE_PASSWORD_LOGIN": "true"}, clear=False):
            os.environ.pop("TESTING", None)
            os.environ.pop("ALLOW_PASSWORD_LOGIN_RISK", None)
            try:
                with pytest.raises(RuntimeError, match="ALLOW_PASSWORD_LOGIN_RISK"):
                    run_startup_migrations()
            finally:
                os.environ["TESTING"] = "1"

    def test_boot_succeeds_with_ack(self):
        from app.startup import run_startup_migrations
        from tests.test_startup import _make_sqlite_engine

        env = {"ENABLE_PASSWORD_LOGIN": "true", "ALLOW_PASSWORD_LOGIN_RISK": "true"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("TESTING", None)
            try:
                with (
                    patch("app.startup.engine", _make_sqlite_engine()),
                    patch("app.startup._create_fts_triggers"),
                    patch("app.startup._seed_system_config"),
                    patch("app.startup._reconcile_system_config"),
                    patch("app.startup._seed_manufacturers"),
                    patch("app.startup._create_count_triggers"),
                    patch("app.startup._reconcile_connector_active"),
                    patch("app.startup._verify_encryption_canary"),
                    patch("app.startup._create_default_user_if_env_set"),
                    patch("app.startup._seed_admin_user_if_env_set"),
                    patch("app.startup._seed_agent_user"),
                    patch("app.startup._seed_verification_group_from_admin_emails"),
                    patch("app.startup._seed_commodity_schemas"),
                ):
                    run_startup_migrations()  # must NOT raise
            finally:
                os.environ["TESTING"] = "1"
```
Run: `TESTING=1 PYTHONPATH=/root/availai .venv/bin/pytest tests/test_auth_password_guard.py -v --override-ini="addopts="` (single-file, no xdist, to avoid env-mutation interleaving during dev).


**verification:**
Unit: `TESTING=1 PYTHONPATH=/root/availai /root/availai/.venv/bin/pytest tests/test_auth_password_guard.py tests/test_startup.py -v --override-ini=\"addopts=\"` — new class green, existing TestStartupPasswordWarning still green.
Local boot simulation (proves fail-boot without ack): `cd /root/availai && env -u TESTING -u ALLOW_PASSWORD_LOGIN_RISK ENABLE_PASSWORD_LOGIN=true PYTHONPATH=/root/availai /root/availai/.venv/bin/python -c \"from app.startup import run_startup_migrations; run_startup_migrations()\"` → expect a RuntimeError mentioning ALLOW_PASSWORD_LOGIN_RISK (it raises before any DB call).
Ack path boots (no RuntimeError from the guard; a DB connect error afterward is fine/expected without PG): `cd /root/availai && env -u TESTING ENABLE_PASSWORD_LOGIN=true ALLOW_PASSWORD_LOGIN_RISK=true PYTHONPATH=/root/availai /root/availai/.venv/bin/python -c \"from app.routers.auth import password_login_risk_acknowledged; assert password_login_risk_acknowledged() is True; print('ack ok')\"`.
deploy.sh preflight: with a temp `.env` containing `ENABLE_PASSWORD_LOGIN=true` and no ack line, `bash -n deploy.sh` (syntax) then run the Step 1.5 grep block in isolation → exits 5 with the clear message; add `ALLOW_PASSWORD_LOGIN_RISK=true` → passes.
Post-deploy on staging: staging sets both envs, so `./deploy.sh --no-commit` reaches a healthy container (Step 4) and `docker compose logs app` shows the CRITICAL 'authentication bypass acknowledged' line rather than a boot crash. On any env WITHOUT the ack, the app container would crash-loop and Step 4 would fail — that is the intended launch-blocker enforcement.


**gotchas:**
- IMPORT-TIME vs RUNTIME (load-bearing): config.py instantiates `settings = Settings()` at import (line 437), freezing env values. The guard MUST read os.getenv at runtime (via auth.password_login_risk_acknowledged / password_login_env_enabled), NOT `settings.allow_password_login_risk`. The mirror test patch.dict's os.environ AFTER import; a settings.-based read would still be False → test_boot_succeeds_with_ack would wrongly raise. The existing password_login_env_enabled docstring (auth.py:238-243) documents this exact requirement. The new config field is documentation/loader only — do NOT wire the guard to it.
- Keep the auth import function-local inside run_startup_migrations (as at line 120) — hoisting it to module top would capture at import time and risks an auth-router circular import; the runtime import is what lets the test's env patch take effect.
- Guard ORDER: it must stay before the TESTING short-circuit (line 128). Under TESTING the guard's `and not os.getenv('TESTING')` is False so it never fires — that's why the whole test suite (which runs with TESTING=1) is unaffected. It must also stay before the DB block (line 132) so the RuntimeError needs no DB (PG unavailable in tests).
- os.environ mutation in tests: the mirror pattern pops TESTING and MUST restore os.environ['TESTING']='1' in a finally (see test_startup.py:772-776). Missing the restore leaks into sibling tests in the same xdist worker. Run the new file single-file with --override-ini="addopts=" during dev to avoid `-n auto` interleaving.
- test_auth_password_guard.py does not currently import pytest — add `import pytest` or pytest.raises is undefined.
- deploy.sh exit codes 1,2,3,4 are already used (branch check=1, diverge=2, push=3, sensitive-file=4); the new preflight uses exit 5. Place it AFTER line 73 `fi` (outside the NO_COMMIT block) so it also guards staging's `./deploy.sh --no-commit` path, which is where ENABLE_PASSWORD_LOGIN=true actually lives.
- deploy.sh preflight greps /root/availai/.env (the file docker-compose loads). If staging instead injects env via docker-compose.yml `environment:` or a systemd EnvironmentFile rather than .env, the grep won't see it — confirm where ENABLE_PASSWORD_LOGIN is actually set on staging before relying on the .env grep, or the preflight silently passes (the app-level RuntimeError still enforces correctness; the preflight is defense-in-depth only).
- PG-vs-SQLite: not applicable to the guard itself (it raises before any SQL), but the test_boot_succeeds_with_ack path patches app.startup.engine with a sqlite engine and stubs every conn-taking FAST helper so no PG-only DDL runs — do not remove any of those patches or the `with engine.connect()` block (line 132) will execute real seed SQL.
- pre-commit/mypy: new bool-returning helper and bool config field are trivially typed. docformatter may rewrap the new multi-line docstrings — per CLAUDE.md run `pre-commit run --all-files` twice (first mutates, second verifies). deploy.sh is shell (not mypy-checked) but keep `set -euo pipefail` compatible: the `grep -q ... && ! grep -q ...` compound is fine because it's a full if-condition (not a bare command whose non-zero status would trip -e).


---

## Three ops secrets — ENCRYPTION_SALT, REDIS_PASSWORD, BACKUP_GPG_PASSPHRASE. All are .env/runtime values (NOT code). This grounding gives the plan-writer the exact consuming code, the exact ops steps (generate/set/rotate/recreate), and read-only verifications for each. CONFIRMED: no code change is strictly required to *set* these — but two live code gaps make the current defaults unsafe if a secret is turned on naively (see gotchas #1 and #2), which the plan-writer must decide whether to fix in the same task.

=== ENCRYPTION_SALT ===
Consumers: app/config.py:47 (Settings field, default ""); app/utils/encrypted_type.py build_fernet()/_get_fernet()/verify_encryption_canary()/EncryptedText; app/services/credential_service.py _get_fernet() (separate legacy fallback salt); boot canary called from app/startup.py:140. Rotation command: python -m app.management.rotate_encryption_salt (re-encrypts users.refresh_token/access_token/password_hash only). Rotation risk = re-encrypts existing at-rest data; coordinated restart of app + enrichment-worker required (module-level Fernet cache + .env reload). Blast radius: supplier keys in api_sources.credentials share the salt but degrade gracefully (env-var fallback); users token/password columns do NOT.

=== REDIS_PASSWORD ===
NOT a Settings field. Consumed ONLY by docker-compose.yml interpolation: redis `command` --requirepass (line 60-62), redis healthcheck (line 70), and the composed REDIS_URL env on app (line 99-100) + enrichment-worker (line 158). App code reads settings.redis_url (config.py:64) in rate_limit.py:31, search_service.py:117, cache/intel_cache.py:52. Set REDIS_PASSWORD in .env then RECREATE (up -d, not restart) redis + app + enrichment-worker.

=== BACKUP_GPG_PASSPHRASE ===
NOT a Settings field. Consumed ONLY by shell: scripts/backup.sh:26,127-146 (gpg symmetric AES256, opt-in), scripts/restore.sh:26-31 (decrypt), root backup.sh (host-cron variant). Reaches db-backup container via env_file: .env (docker-compose.yml:183). Set in .env then RECREATE db-backup. Rotating the passphrase does NOT re-encrypt old .gpg backups — keep the old passphrase to restore them.

**files:**
- app/config.py:47 (encryption_salt: str = ""), :64 (redis_url default)
- app/utils/encrypted_type.py:11 (_fernet_instance cache), :17-34 (build_fernet), :37-51 (_get_fernet), :54-100 (verify_encryption_canary + _CANARY_KEY/_CANARY_SENTINEL), :103-133 (EncryptedText)
- app/services/credential_service.py:28 (_LEGACY_CREDENTIAL_SALT), :31-53 (_get_fernet reads settings.encryption_salt)
- app/startup.py:140 (calls _verify_encryption_canary), :705-720 (_verify_encryption_canary)
- app/management/rotate_encryption_salt.py:57-59 (TABLE='users', COLUMNS refresh_token/access_token/password_hash), :123-188 (rotate_salt), :220-262 (main/CLI)
- docker-compose.yml:49-83 (redis service: requirepass line 60-62, healthcheck line 70), :99-100 (app REDIS_URL compose), :158 (worker REDIS_URL compose), :178-205 (db-backup service, env_file .env line 183)
- scripts/backup.sh:26 (GPG_PASSPHRASE=${BACKUP_GPG_PASSPHRASE:-}), :123-146 (gpg encrypt block)
- scripts/restore.sh:23-36 (decompress/decrypt), :196-206 (safety-backup encrypt)
- scripts/backup-cron.sh:19 (UNGUARDED initial /scripts/backup.sh under set -e)
- scripts/verify-backup.sh:43-53 (reads /backups/LATEST, restore.sh --verify)
- .env.example:66-76 (ENCRYPTION_SALT), :99-104 (REDIS_PASSWORD/REDIS_URL), :106-114 (BACKUP_GPG_PASSPHRASE, commented out)
- docs/PRE_ROLLOUT_CHECKLIST.md:153-190 (Gate 4 / Gate 4c rotation procedure)
- deploy.sh:82-95,145-146 (recreates ONLY app + enrichment-worker — not redis/db-backup)


**current_code:**
ENCRYPTION_SALT consumer — app/config.py:47:
    encryption_salt: str = ""

app/utils/encrypted_type.py:37-51 (module-cached Fernet — a running process holds the OLD key until recreated):
    _fernet_instance = None
    def _get_fernet():
        global _fernet_instance
        if _fernet_instance is not None:
            return _fernet_instance
        from ..config import settings
        if not settings.encryption_salt:
            logger.warning("ENCRYPTION_SALT not set — using legacy static salt ...")
        _fernet_instance = build_fernet(settings.secret_key, settings.encryption_salt)
        return _fernet_instance

app/utils/encrypted_type.py:82-97 (boot canary — RAISES if live salt can't decrypt the stored canary row):
    if row is None:
        token = f.encrypt(_CANARY_SENTINEL.encode()).decode()
        db.add(SystemConfig(key=_CANARY_KEY, value=token, ...)); db.commit(); return
    try:
        decrypted = f.decrypt(row.value.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("ENCRYPTION MISCONFIG: the encryption canary failed to decrypt ...") from e

app/management/rotate_encryption_salt.py:58-59 (rotates users ONLY — does NOT touch the system_config canary row):
    TABLE = "users"
    COLUMNS: tuple[str, ...] = ("refresh_token", "access_token", "password_hash")

REDIS_PASSWORD — docker-compose.yml:60-62 (redis) and :100 (app) — compose-time interpolation, NOT a container env var:
    command: >
      redis-server --maxmemory 512mb --maxmemory-policy allkeys-lru
      ${REDIS_PASSWORD:+--requirepass ${REDIS_PASSWORD}}
    ...
    environment:
      - REDIS_URL=redis://${REDIS_PASSWORD:+:${REDIS_PASSWORD}@}redis:6379/0

BACKUP_GPG_PASSPHRASE — scripts/backup.sh:26 + 127-139 (gpg required only when set; calls die on failure):
    GPG_PASSPHRASE="${BACKUP_GPG_PASSPHRASE:-}"
    ...
    if [ -n "$GPG_PASSPHRASE" ]; then
        printf '%s' "$GPG_PASSPHRASE" | gpg --batch --yes --quiet \
            --pinentry-mode loopback --passphrase-fd 0 --cipher-algo AES256 --symmetric \
            --output "${BACKUP_FILE_GZ}.gpg" "$BACKUP_FILE_GZ" || die "gpg encryption failed"

scripts/backup-cron.sh:19 (UNGUARDED under set -e — a failed backup.sh crash-loops the db-backup container):
    /scripts/backup.sh


**edit_plan:**
No app-code edit is required to SET any of the three (they are .env values). The task is: (A) add the three keys to the deployed .env with strong generated values, (B) recreate the correct containers, (C) run the read-only verifications. Precise ops steps:

ENCRYPTION_SALT (rotation — highest risk):
1. Snapshot DB + .env first (a rotation rewrites encrypted cells). 2. Generate: openssl rand -base64 32. 3. DRY-RUN with .env still on the OLD salt: `docker compose exec -T app python -m app.management.rotate_encryption_salt --new-salt "<NEW>" --dry-run` and CONFIRM undecryptable=0 on every column (STOP if not). 4. Rotate for real (same NEW salt, drop --dry-run). 5. DELETE the stale boot canary so it re-bootstraps under the new key (see gotcha #1): `docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "DELETE FROM system_config WHERE key='encryption_canary';"`. 6. Set ENCRYPTION_SALT=<NEW> in .env. 7. Recreate app + enrichment-worker: `docker compose up -d app enrichment-worker` (module-level Fernet cache means a plain restart is NOT enough — must reload .env; both containers share the code path). 8. Re-enter any DB-stored supplier keys (admin → Connectors) — they share the salt and read empty until re-entered.

REDIS_PASSWORD:
1. Generate: openssl rand -base64 32 (avoid @ / : collisions in the URL — base64 is safe here since it is URL-embedded but compose does not URL-encode; a base64 value may contain +/= which are fine in the userinfo section, but if paranoid use `openssl rand -hex 32`). 2. Set REDIS_PASSWORD in .env. 3. Recreate (NOT restart — see gotcha #3): `docker compose up -d redis app enrichment-worker`. deploy.sh will NOT do this (it recreates only app + worker, never redis — deploy.sh:93-146).

BACKUP_GPG_PASSPHRASE:
1. Generate a long passphrase: openssl rand -base64 48. 2. Uncomment/set BACKUP_GPG_PASSPHRASE in .env. 3. RESOLVE gotcha #2 FIRST (gpg is absent from the db-backup image — setting the passphrase without fixing this crash-loops the container). 4. Recreate: `docker compose up -d db-backup` (env_file is read at create, not restart). 5. Store the passphrase off-box — it is required to restore.

CODE DEFAULTS THAT SHOULD CHANGE (note to plan-writer — decide whether in-scope):
- rotate_encryption_salt.py should also re-encrypt (or the runbook must delete) the system_config 'encryption_canary' row, else the documented Gate 4c step 5 bricks boot (gotcha #1). Minimal safe fix = document the DELETE in PRE_ROLLOUT_CHECKLIST.md Gate 4c step 5; robust fix = extend the rotation command to rotate that row too.
- scripts/backup.sh must `apk add --no-cache gnupg` if gpg is missing (mirror the aws-cli bootstrap in backup-to-spaces.sh:42-45), OR db-backup must move to a custom image with gnupg. Today BACKUP_GPG_PASSPHRASE is a latent trap (gotcha #2).


**migration_notes:**
No Alembic migration. These are .env/runtime secrets, not schema. One-time DATA touch during an ENCRYPTION_SALT rotation only: the rotation command re-encrypts existing users cells (idempotent/resumable, never discards) and the operator must DELETE the system_config 'encryption_canary' row (gotcha #1) — both are runtime operations, not migrations. Note that startup.py is runtime-ops-only by the repo's ABSOLUTE RULES, and the canary bootstrap already lives there (verify_encryption_canary), so re-bootstrapping the canary post-rotation is consistent with existing runtime-op patterns — no DDL involved.


**test_file:**
tests/test_backup.py (compose-wiring + gpg-availability regression guards live here — see TestDockerComposeBackup and TestPassphraseNotOnCommandLine). For ENCRYPTION_SALT rotation semantics: tests/test_rotate_encryption_salt.py. A new tests/test_ops_secrets.py mirroring TestDockerComposeBackup is the right home if the plan-writer wants a standalone REDIS_PASSWORD compose-wiring assertion (none exists today — grep of tests/ for REDIS_PASSWORD/requirepass returned nothing).


**test_pattern_to_mirror:**
tests/test_backup.py TestDockerComposeBackup (lines 320-353): reads docker-compose.yml as raw text and asserts substrings ("db-backup:", "./scripts:/scripts:ro"). Mirror this to assert the REDIS_PASSWORD wiring: e.g. `assert "${REDIS_PASSWORD:+--requirepass ${REDIS_PASSWORD}}" in content` and `assert "redis://${REDIS_PASSWORD:+:${REDIS_PASSWORD}@}redis:6379/0" in content`. For the gpg gap, mirror tests/test_backup.py test_backup_supports_at_rest_encryption (lines 112-116) but add a guard that backup.sh installs/verifies gnupg (e.g. assert an `apk add ... gnupg` bootstrap exists, analogous to backup-to-spaces.sh). For rotation: tests/test_rotate_encryption_salt.py seeds a User with EncryptedText columns via build_fernet(secret_key, old_salt), runs rotate_salt(db, old_salt=..., new_salt=..., secret_key=...), then asserts the row decrypts under the new Fernet and status counters (rotated/already/undecryptable) — mirror that to add a canary-row-survives-rotation regression test.


**verification:**
All read-only unless noted.
ENCRYPTION_SALT: (a) Boot health is the canary — after recreate, `docker compose logs app | grep -i canary` should show 'Encryption canary verified.' (or 'bootstrapped') and NO 'ENCRYPTION MISCONFIG' RuntimeError. (b) Confirm value is live: `docker compose exec -T app python -c \"from app.config import settings; print(bool(settings.encryption_salt))\"` → True. (c) Functional: a user can still log in / password-login (proves the three columns decrypt). (d) Dry-run report (writes nothing): `docker compose exec -T app python -m app.management.rotate_encryption_salt --new-salt xx --dry-run` prints per-column rotated/already/undecryptable.
REDIS_PASSWORD: (a) auth ON: `docker compose exec redis redis-cli -a \"$REDIS_PASSWORD\" --no-auth-warning CONFIG GET requirepass` → shows the value; `... ping` → PONG. (b) unauth REJECTED: `docker compose exec redis redis-cli ping` → '(error) NOAUTH Authentication required.'. (c) app actually using it: `docker compose logs app | grep -i 'Rate limiter using Redis\\|Redis cache connected'` (a wrong/absent password logs 'Redis unavailable — ... in-memory').
BACKUP_GPG_PASSPHRASE: (a) precondition: `docker compose exec db-backup which gpg` MUST print a path (today it does NOT — gotcha #2). (b) trigger a manual backup (writes a backup file, otherwise read-only to the DB): `docker compose exec -T db-backup /scripts/backup.sh`, then `docker compose exec -T db-backup cat /backups/LATEST` should end in '.gpg'. (c) round-trip verify (read-only, does not touch live DB): `docker compose exec -T db-backup /scripts/restore.sh --verify \"$(docker compose exec -T db-backup cat /backups/LATEST)\"` → 'Backup verification: PASSED' (proves the passphrase decrypts). (d) confirm plaintext is gone: `docker compose exec db-backup ls /backups` shows only *.dump.gz.gpg for the new timestamp, no bare *.dump.gz.


**gotchas:**
- CONFIRMED gotcha #1 (canary strands boot on rotation): rotate_encryption_salt.py rotates ONLY the users table (COLUMNS at :58-59); it does NOT re-encrypt the system_config 'encryption_canary' row. verify_encryption_canary (encrypted_type.py:82-97) RAISES RuntimeError at boot when the live salt can't decrypt that row. So following PRE_ROLLOUT_CHECKLIST Gate 4c step 5 verbatim (set new salt → recreate) will refuse to boot. Mitigation: DELETE FROM system_config WHERE key='encryption_canary' before recreating (it re-bootstraps under the new key on next boot). Verified: grep of the rotation command for encryption_canary/SystemConfig returned nothing.
- CONFIRMED gotcha #2 (gpg missing from db-backup image): `docker compose exec db-backup which gpg` returns only pg_dump (no gpg), and `docker run --rm postgres:16-alpine which gpg` prints NO_GPG. db-backup uses image: postgres:16-alpine with NO build step and backup.sh does NOT apk-add gnupg. Setting BACKUP_GPG_PASSPHRASE makes backup.sh:139 `|| die "gpg encryption failed"` fire; the initial backup in backup-cron.sh:19 is UNGUARDED under set -e, so the db-backup container exits and (restart: always) crash-loops, producing ZERO backups. Fix before enabling the passphrase.
- gotcha #3 (recreate, not restart — REDIS_PASSWORD & BACKUP_GPG_PASSPHRASE): REDIS_PASSWORD is a compose-file INTERPOLATION (${REDIS_PASSWORD:+...}), resolved only at `docker compose up`/create time — `docker compose restart` does NOT re-interpolate, so the new value silently won't apply. env_file: .env (db-backup) is likewise read at container create, not restart. Always `docker compose up -d <svc>`.
- gotcha #4 (deploy.sh scope): ./deploy.sh recreates ONLY app + enrichment-worker (deploy.sh:93-146). It NEVER recreates redis, db, db-backup, or caddy. So a REDIS_PASSWORD change needs an explicit `docker compose up -d redis`, and a BACKUP_GPG_PASSPHRASE change needs `docker compose up -d db-backup` — a plain deploy will not pick them up.
- gotcha #5 (module-level Fernet cache): encrypted_type.py caches _fernet_instance (:11,:43-44) for the process lifetime — you cannot change ENCRYPTION_SALT on a live process; the container MUST be recreated. credential_service._get_fernet() is NOT cached but reads settings loaded from .env at import, so it too needs a process restart to see the new value.
- gotcha #6 (asymmetric blast radius): supplier credentials in api_sources.credentials share the salt but degrade gracefully to env-var fallback (credential_service.decrypt_from :112-136) — an unrotated supplier key just reads empty and logs. The three users columns do NOT degrade: an orphaned refresh_token/access_token forces re-login, an orphaned password_hash breaks password login. That asymmetry is exactly why the rotation command targets users only.
- gotcha #7 (rotation is DB-write, coordinate the window): rotate_salt runs in a single transaction and COMMITs (rotate_encryption_salt.py:184-188). Run it during a quiet window; new logins/token refreshes between the rotate and the container recreate write ciphertext under the OLD live key and would then be undecryptable under the NEW salt. Sequence tightly: rotate → delete canary → set .env → up -d app+worker back-to-back.
- gotcha #8 (PG vs SQLite / TESTING): the boot canary and rotation exercise real Postgres (system_config + users). Tests run TESTING=1 on in-memory SQLite (per pytest.ini) and intel_cache._connect_intel_redis returns None under TESTING — so redis/canary/backup paths are inert in the suite. The two CONFIRMED code gaps (#1, #2) are only reproducible on the live PG/Docker stack, which is why they were verified with docker commands here, not pytest.


---
