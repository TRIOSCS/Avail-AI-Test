# Production Readiness Checklist

**Target:** 50–100 concurrent users, thousands of requests. All items must be completed before production.

---

## 1. Database: Singleton / Shared Connection Pool

**Current state:**
- **Engine** is a single shared instance in `app/database.py` with `pool_size=15`, `max_overflow=30`, `pool_pre_ping=True`, `pool_recycle=3600`.
- **Request-scoped sessions:** `get_db()` yields one session per request and closes it in `finally` — correct.
- **Background jobs:** Each job creates its own `SessionLocal()` and must call `db.close()` in a `finally` block.

**Required before production:**

- [ ] **Increase pool for 50–100 users:** In `app/database.py`, consider `pool_size=25`, `max_overflow=50` so peak concurrent requests (e.g. 75) don’t exhaust the pool. Ensure PostgreSQL `max_connections` is higher than `pool_size + max_overflow` (e.g. 100+).
- [ ] **Audit all `SessionLocal()` usage:** Every place that does `db = SessionLocal()` must use `try/finally` and call `db.close()`. Known files: `app/jobs/*.py`, `app/services/buyplan_v3_notifications.py`, `app/services/trouble_ticket_service.py`, `app/services/prospect_contacts.py`, `app/services/customer_analysis_service.py`, `app/services/credential_service.py`, `app/services/prospect_scheduler.py`, `app/services/health_monitor.py`, `app/utils/vendor_helpers.py`, `app/search_service.py`, `app/cache/decorators.py`, `app/routers/crm/companies.py`. Confirm no session leak (no long-lived session held across many operations).
- [ ] **Statement timeouts:** Already set in `database.py`: `statement_timeout=30000`, `lock_timeout=5000`. Keep these for production to avoid runaway queries.

---

## 2. Redis: Caching for Duplicate / Heavy Queries

**Current state:**
- **Intel cache** (`app/cache/intel_cache.py`): Redis primary, PostgreSQL fallback; used by `cached_endpoint` decorator.
- **Endpoints already using `@cached_endpoint`:**
  - `vendor_analytics.get_vendor_parts_summary` (2h TTL),
  - `performance.list_vendor_scorecards` (4h),
  - `performance.get_vendor_detail` (4h),
  - `performance.list_buyer_leaderboard` (4h),
  - `vendor_contacts.vendor_email_metrics` (2h).
- **Search cache:** `app/search_service.py` caches search results in Redis (15 min TTL) by query key.

**Required before production:**

- [ ] **Ensure Redis is used in production:** Set `CACHE_BACKEND=redis` and `REDIS_URL` (e.g. `redis://redis:6379/0`). Do not use `CACHE_BACKEND=postgres` in production for response caching.
- [ ] **Add Redis caching to high-traffic list endpoints** that are read-heavy and parameterized (so cache keys are safe):
  - [ ] **Prospect suggested list** (`/api/prospects/suggested`): Consider short TTL (e.g. 5–10 min) with key_params `page`, `per_page`, `sort`, and filter params. Invalidate or use short TTL so new prospects appear.
  - [ ] **Requisitions list** (if applicable): If list-by-user is expensive, add `@cached_endpoint` with short TTL and key_params including `user.id`, filters, `limit`, `offset`.
  - [ ] **Dashboard / briefs:** `dashboard/briefs.py` — if morning brief or KPIs are heavy, cache with 15–30 min TTL.
- [ ] **Redis connection:** `intel_cache._get_redis()` is lazy singleton; one client shared across app. Redis client uses an internal connection pool — no change needed. Ensure production Redis has `maxclients` and memory appropriate for cache size.
- [ ] **Log cache misses/hits in production (optional):** `cache/decorators.py` logs at DEBUG. For production, consider INFO for cache hit ratio on critical endpoints or use metrics.

---

## 3. Logging

**Current state:**
- **Loguru** in `app/logging_config.py`: human-readable in dev, JSON in production when `EXTRA_LOGS=1`.
- **Request ID:** `main.py` has `request_id_middleware` that sets `request.state.request_id` and `logger.contextualize(request_id=...)`.
- **Global exception handler** in `main.py` logs with `request_id`, status, path, and error.
- **Structured fields:** Many logs use keyword args (e.g. `logger.info("...", key=value)`).

**Required before production:**

- [ ] **Production:** Set `EXTRA_LOGS=1` and `LOG_LEVEL=INFO` (or WARNING for less noise). Ensure `APP_URL` contains production host (e.g. `availai.net`) so file logging and JSON format are enabled.
- [ ] **Log file path:** `/var/log/avail/avail.log` must be writable by the app process; create directory and set permissions if needed.
- [ ] **Sensitive data:** Confirm no passwords, tokens, or PII in log messages (already scrubbed in Sentry `before_send`; ensure same for Loguru).
- [ ] **Critical paths:** Add or keep structured logs for: auth failures, payment/quote actions, and any admin data operations (e.g. bulk merge) with request_id and user id.

---

## 4. Migrations (Alembic) Aligned

**Current state:**
- Single linear chain from `001_initial_schema` through `050_increase_notification_title_to_500`.
- **Fix applied:** Stray `#` in `045_fix_material_card_normalized_mpn.py` (Step 3 comment) removed so the migration file is valid.

**Required before production:**

- [ ] **Run on target DB:** `alembic upgrade head` on the production database (or staging mirror). Resolve any missing revision / multiple heads before go-live.
- [ ] **Never run schema changes from app code:** No `Base.metadata.create_all` in startup (already removed). All schema changes only via new Alembic revisions.
- [ ] **After each deploy:** As part of deploy script, run `alembic upgrade head` before starting the new app process so the app never starts with an older schema.

---

## 5. Pagination

**Current state:**
- Many list endpoints use `limit`/`offset` or `page`/`per_page` with caps (e.g. `limit` ≤ 500, `per_page` ≤ 100).
- Some endpoints return fixed-size lists (e.g. `.limit(20)`) without pagination params — acceptable for “top N” widgets.

**Required before production:**

- [ ] **Audit all list endpoints** that could return large result sets and ensure they accept `limit`/`offset` or `page`/`per_page` with a **maximum cap**:
  - **Prospect suggested:** Already has `page`, `per_page` (capped 100). OK.
  - **Vendor analytics (offer-history, parts-summary):** Has `limit`/`offset` with caps. OK.
  - **Enrichment queues/jobs:** Has `limit`/`offset`. OK.
  - **Tags / entity_tags:** Has `limit`/`offset`. OK.
  - **Requisitions list:** Confirm pagination and cap (e.g. max 100 per page).
  - **CRM companies/sites, quotes, contacts:** Confirm list endpoints have pagination and caps.
- [ ] **Response shape:** Where applicable, return `total` or `total_count` so the frontend can show “Page X of Y” or “Showing N of M”. Many endpoints already do; verify for any new or changed list APIs.
- [ ] **Default page size:** Use a reasonable default (e.g. 20–50) so a single request never pulls thousands of rows without the client explicitly asking.

---

## 6. Other Improvements

**Security & env:**
- [ ] **Secrets:** Ensure production uses strong `SESSION_SECRET` or `SECRET_KEY` (startup already fails if default is used and not TESTING).
- [ ] **Password login:** If `ENABLE_PASSWORD_LOGIN` is used, ensure it is only for dev/staging; never enable in production unless intended and locked down.

**Rate limiting:**
- [ ] **Rate limits:** `RATE_LIMIT_ENABLED=true` in production. Review `RATE_LIMIT_DEFAULT` and per-endpoint limits (e.g. search, auth callback) so 50–100 users doing thousands of requests don’t hit 429 too often; adjust if needed.

**Health & ops:**
- [ ] **Health endpoint:** Expose a simple `/health` or `/api/health` that checks DB (and optionally Redis) and returns 200. Use it for load balancer and orchestrator health checks.
- [ ] **Sentry:** Set `SENTRY_DSN` in production for error tracking and optional performance traces.

**Background jobs:**
- [ ] **Scheduler:** Ensure only one instance of the scheduler runs in production (e.g. one app replica that runs jobs, or use a dedicated worker). Multiple instances could double-run jobs.
- [ ] **Job timeouts:** Long-running jobs (enrichment, tagging, discovery) should have timeouts or batch limits so they don’t hold DB connections indefinitely.

**Performance:**
- [ ] **N+1 queries:** For list endpoints that load related entities (e.g. companies with sites, quotes with lines), use `joinedload`/`selectinload` where appropriate to avoid N+1.
- [ ] **Heavy startup:** `startup.py` runs triggers, seeds, and backfills every boot. For large DBs, consider moving heavy backfills to one-time migrations or offline scripts so deploys stay fast.

**Cleanup:**
- [ ] **Unused code:** Remove or gate deprecated routes/views (e.g. old prospecting UI) once replaced and verified in production.
- [ ] **Deprecated env vars:** Document and remove any obsolete env vars from `.env.example` and deploy docs.

---

## 7. Pre-Launch Verification

- [ ] Run full test suite: `pytest tests/ -v --tb=short`.
- [ ] Run `alembic upgrade head` on a copy of production schema (or staging); then run app and smoke-test critical flows (login, search, create requisition, view dashboard).
- [ ] Load test: Simulate 50–100 concurrent users with thousands of requests (e.g. search, list requisitions, list prospects). Confirm no connection pool exhaustion, no 5xx from DB/Redis, and response times acceptable.
- [ ] Confirm Redis and PostgreSQL connection limits and memory are sufficient for production traffic and cache size.

---

## Summary Table

| Area              | Action |
|-------------------|--------|
| DB connections    | Verify pool_size/max_overflow and all SessionLocal() close in finally; consider 25/50 for 100 users. |
| Redis caching     | Use Redis in prod; add cached_endpoint to high-traffic list endpoints (prospects, dashboard) where appropriate. |
| Logging           | EXTRA_LOGS=1, LOG_LEVEL=INFO, writable /var/log/avail; ensure request_id and no PII in logs. |
| Migrations        | 045 typo fixed; run alembic upgrade head on prod; schema only via Alembic. |
| Pagination        | All list APIs have limit/offset or page/per_page with caps and total where needed. |
| Security / env    | Strong SESSION_SECRET; no password login in prod unless intended. |
| Rate limiting     | Enabled; tune limits for 50–100 users. |
| Health check      | /health or /api/health for LB. |
| Scheduler         | Single instance or dedicated worker; job timeouts/batch limits. |
| N+1 / startup     | Review N+1 on list endpoints; move heavy backfills off startup if needed. |
