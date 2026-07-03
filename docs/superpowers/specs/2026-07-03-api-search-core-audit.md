# API-Search Core Audit (2026-07-03)

Fable four-facet deep-dive (connectors / orchestration / infra / Settings API-management
tool), 27 findings, synthesized. The parallel supplier-API search is the product core.

## Headline

The **requisition** search path (`_fetch_fresh`, `search_service.py:1506-1582`) is a solid
reference — bounded deadline, straggler cancellation, typed-error retry with secret
redaction, full health telemetry. The problem: the **primary interactive path**
(`stream_search_mpn`, the SSE search users drive) never inherited that budget — with no
aggregate deadline (`asyncio.wait` at `:2735` has no timeout), one hung/rate-limited
connector holds the browser spinner and delays the terminal `done` event **60s to ~10min**,
and records **zero** ApiSource telemetry so those failures are invisible in admin health.
Compounding it, **"Test all" is broken** (>4 live connectors overrun the 15s htmx timeout →
htmx aborts the XHR, discards every result, server keeps burning paid quota), and **keyless
Test buttons are cosmetic no-ops that falsely report OK**. Biggest single risk: the unbounded
streaming deadline turns one flaky upstream into a multi-minute hang on the central function.

## FIX NOW (critical / high)

1. **[HIGH stability] Interactive SSE search has no aggregate timeout** — `search_service.py:2735`
   (`asyncio.wait(pending, return_when=FIRST_COMPLETED)` no timeout) vs `_fetch_fresh:1524`
   which passes `timeout=settings.search_total_timeout_s` + cancels stragglers. FIX: track
   `t_start`, pass `remaining` to `asyncio.wait`; on expiry cancel pending tasks, publish an
   error/timeout source-status chip each, then `done`. Share `search_total_timeout_s`. Land
   with the Retry-After cap (`sources.py:221`, 300s → `min(header, 30)`).
2. **[HIGH functionality] "Test all" silently produces no result** — `settings.py:641-651`
   loops SEQUENTIALLY awaiting live tests; >4 live connectors exceed
   `htmx.config.timeout=15000` (`htmx_app.js:368`) → XHR aborted client-side, no OOB cards/
   summary applied, server burns quota on discarded results. FIX: run probes concurrently
   (bounded budget) + raise the Test-all button's `hx-request` timeout (~120000) or
   fire-and-poll; add `request.is_disconnected()` to abort once the client gives up.
3. **[HIGH functionality] Keyless per-source Test falsely reports OK** — `sources.py:71-142`
   (`_get_connector_for_source`) has no branch for `ai_live_web`, `sam_gov_enrichment`,
   `stock_list_import` (email_mining IS now wired), so Test returns None → swallowed; the
   `has_env_vars` gate skips status persistence; Test-all resolves them "untested" not
   "error" → summary says "all OK". The core AI web-search Test is zero-feedback. FIX: derive
   testable from "a real test path exists", wire `AiLiveWebConnector` + keyless hooks, drop
   the `has_env_vars` persistence gate, hide Test where no test path exists.
4. **[MEDIUM but silent CORE failure] Price-break `min()` crashes on null quantity** —
   `digikey.py:139`, `mouser.py:124`, `sources.py:557` & `:412`. `.get(k, default)` returns
   the default only on a MISSING key; an explicit `{Quantity: null}` yields None → `min()`
   does `None < int` → TypeError → after retries the **entire PN** errors. FIX: coalesce —
   `key=lambda p: p.get('Quantity') or 999999` (+ BreakQuantity/breakQuantity/quantity) at all 4.
5. **[MEDIUM but silent enrichment failure] Hunter `r.json()` outside try/except** —
   `hunter.py:57` (also email_finder `:110`, verify `:141`). A 200 with non-JSON body raises
   ValueError into the enrichment caller; every sibling (Lusha/Explorium/SAM/Clay) guards json.
   FIX: move `r.json()` inside the try / add `except ValueError`.

## OPTIMIZATIONS (ranked)

1. **OAuth token cache is dead across searches** — DigiKey/eBay/Nexar cache the bearer in
   instance fields (`digikey.py:35-36`, `ebay.py:25-26`, `sources.py:295-296`), but
   `_build_connectors` (`:1303-1352`) builds fresh instances every search → each search pays
   3 serial token POSTs (~200-500ms) on the critical path; per-search minting can trip eBay
   auth under load. FIX: module-level token cache keyed by `(class, client_id)` + expiry (like
   `_breakers`/`_connector_semaphores`) + an `asyncio.Lock` (kills the intra-search herd).
2. **Sync Redis on the async search hot path** — `search_service.py:141-168` (`_get/_set_search_cache`
   sync `r.get/r.setex`) called inside async `_fetch_fresh` (`:1468`,`:1708`) + streaming
   (`:2836-2842`); same pattern in `@cached_endpoint`. Redis latency blocks the single event
   loop up to 2s, stalling every in-flight request. FIX: `asyncio.to_thread` or `redis.asyncio`.
3. **Cap 429 Retry-After budget-aware** — `sources.py:221` caps at 300s; `BaseConnector` sleeps
   inline (`:170`) up to max_retries (~600s) holding the semaphore + concurrency slot. FIX:
   `min(header, 30)` for the retry loop, or fast-fail once remaining budget < requested wait.
4. **Element14 fires 2 calls per PN on exact-match miss** — `element14.py:74-80` keyword
   fallback; element14 returns 403 for both auth AND QPS cap (`:110-115`), so doubling volume
   accelerates the 403s that ERROR-exclude it, and the fallback is catalog noise the relevance
   guard discards anyway. FIX: gate the fallback to partial/alt-format parts, or drop it.
5. **~70 redundant SELECTs per Connectors render** — N+1: `_build_connector_field` calls
   `credential_is_set` + `get_credential` (each a fresh `db.query(ApiSource)`) per env var,
   though `_build_connector_groups` already loaded every ApiSource. FIX: pass the loaded row;
   add `is_set_for(src, ev)`/`decrypt_from(src, ev)`.
6. **Per-call httpx/Anthropic clients bypass the shared pool** — `eight_by_eight_service.py:79,132`
   (fresh `httpx.AsyncClient`/call), `sighting_aggregation.py:64` & `vendor_affinity_service.py:212`
   (fresh `anthropic.Anthropic()`/call, no `.close()`). FIX: shared http client for 8x8; a
   cached module-level Anthropic client.

## SETTINGS-TOOL UX / MANAGEMENT

1. Per-source Test gives no pass/fail feedback or last-tested time (`sources.py:549-562` JSON
   discarded by `hx-swap=none`; macro never renders `last_success`). FIX: `HX-Trigger showToast`
   (status + count + elapsed) + render "Last checked …".
2. `email_mining` misclassified as a "key" connector (`env_vars=['EMAIL_MINING_ENABLED']` is a
   flag; `control_type()` falls through to "key" → masked password field). Typing encrypts a
   bogus credential. FIX: classify keyless/flag → on/off toggle.
3. `teams_notifications` reads "No key required" but needs `TEAMS_WEBHOOK_URL` with no field
   to enter/rotate it. FIX: reclassify key/multi_field, or fix the copy.
4. **Masked placeholder exposes the last 4 chars of every secret** (`mask_value` renders
   `dots + plaintext[-4:]`), including `browser_login` account passwords (TBF/ICS) in the DOM.
   FIX: fully mask (dots only) for `browser_login`/password types.
5. Startup "Connectors enabled/disabled" log reflects env-var presence only, diverging from the
   DB-first credential resolution + `api_sources.status` health. Misleads triage. FIX: base it
   on the DB-first resolution + health, or relabel "env-var presence".
6. Test / Test-all spend real paid quota with no cost guard; Test-all bypasses the 5/min limit.
   FIX: cost note + debounce + cheapest verify call per provider.

## SOLID BASELINE — do NOT disturb

1. `_fetch_fresh` (requisition path) — the reference; bounded deadline, straggler-as-errored,
   CancelledError not swallowed, full telemetry in one guarded pass. The shape streaming copies.
2. `BaseConnector` retry contract (`sources.py:160-209`) — typed errors, 401/403/422 fail-fast,
   URL-secret redaction, breaker.record_failure, backoff+jitter. Only the inline Retry-After
   sleep length needs bounding.
3. Module-level circuit breakers + per-connector semaphores — correct process-wide pooling
   (OAuth tokens are the lone outlier that should follow this).
4. The relevance guard (`fuzzy_mpn_match`, `:2753-2760`) correctly discards off-target catalog
   noise; incremental dedup + streaming SSE card rendering work.
5. Enrichment json guards (Lusha/Explorium/SAM/Clay), htmx x-csrftoken injection, and
   `htmx.config.selfRequestsOnly=true` (SSRF guard) — correct; do not touch.

## RECOMMENDED SEQUENCE

- **Phase 0 (immediate, one batch — restore trust in the two surfaces users touch):**
  (a) streaming bounded deadline + cancel-and-chip stragglers + Retry-After `min(header,30)`;
  (b) fix Test-all (concurrent bounded probes + raised timeout / fire-and-poll +
  `is_disconnected`); (c) fix keyless Test (real-test-path gate + wire AiLiveWeb + drop the
  persistence gate + hide where no path).
- **Phase 1 (core hardening + top perf, small/high-confidence, land together):** OAuth
  token-cache hoist + Lock; sync-Redis → `to_thread`; price-break None-coalesce (4 sites);
  Hunter `json` guard; Element14 fallback gate.
- **Phase 2 (infra resilience + observability):** Redis lazy-client re-probe + downgrade
  metric; stop a search-induced breaker trip from flipping health to a 15-min ERROR exclusion;
  add ApiSource telemetry to the interactive path; strip attributes from the HTML structure
  hash (kills false "layout changed" Sentry spam + unbounded hash-set growth).
- **Phase 3 (Settings-tool UX):** per-test toast + timestamp; reclassify email_mining (flag) +
  teams (webhook); fully mask browser_login; relabel connector_status; Test-all cost note + debounce.
- **Phase 4 (schedule):** Nexar REST empty-result short-circuit → GraphQL; validate/disable
  Sourcengine parser; eBay explicit 429; `@cached_endpoint` async-target guard; xdist rate-limiter freeze.

**Verify Phase 0/1 on real PostgreSQL + a headless SSE drive** (curl ≠ htmx — observe the
terminal `done` event + OOB swaps), not SQLite.
