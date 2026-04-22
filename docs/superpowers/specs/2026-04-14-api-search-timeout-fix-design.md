# API Search Timeout Fix — Design

**Date:** 2026-04-14
**Scope:** Bug fix across `app/search_service.py`, `app/connectors/*.py`, `app/routers/sightings.py`, `app/static/htmx_app.js`, `app/templates/htmx/partials/...`
**Context:** The API search feature on the RFQ and Sightings pages times out and returns no results to users. Investigation revealed five stacked bugs, not one.

---

## Symptom

Users click Search on the Sightings page (or the RFQ parts tab) and see one of:

- A spinner that never resolves into results
- An HTMX "request timed out" toast
- An "Already searched within the last 5 minutes" message on any retry, for the next 5 minutes

## Root cause (measured, not theorized)

A sub-agent investigation ran one real cold search (requirement 31, 3 MPNs, Redis cache flushed) and timed every connector. Results:

| Connector | Time (ms) | Results | Note |
|---|---|---|---|
| **Mouser** (broken key) | **8646** | 0 | Retries internally on "Invalid unique identifier" |
| OEMSecrets | 3049 | 132 | |
| DigiKey | 1414 | 15 | |
| Nexar (broken) | 871 | 0 | Fails fast, correct |
| Element14 (403) | 587 | 2 | |
| BrokerBin (broken) | 201 | 0 | Fails fast, correct |

Total `_fetch_fresh` wall: **9.57 s**. `asyncio.gather` waits for the slowest task, so Mouser's broken-key retries define the floor.

The HTMX client timeout is **15 000 ms** (`app/static/htmx_app.js:155`), measured globally. Single searches are marginal; batch searches are guaranteed to time out because `sightings_batch_refresh` runs search_requirement serially.

When a search fails, `search_requirement` unconditionally stamps `last_searched_at = now` inside its own write session. That session commits even when the outer request errored. The 5-minute rate guard (`_within_rate_limit`) then silently refuses every retry, producing the "no responses returned" experience.

Additional issues surfaced:

- `api_sources` DB rows for Nexar and BrokerBin report `status=live` despite every call failing — the status-write path counts returned-empty as success.
- The circuit breaker only trips on raw httpx exceptions, not on API-level errors that connectors swallow into `[]`. So broken connectors are called on every search forever.
- Credentials are loaded DB-first (`api_sources.credentials` JSONB), env-fallback. A stale encrypted key in the DB silently shadows a correct `.env` value.

## Phased fix

Each phase is a standalone PR with its own tests and APP_MAP update. Each phase unblocks the next but does not depend on future phases.

### Phase 1 — Make search fast and retryable (core bugs)

Three fixes, bundled. Without these, the product is broken.

**1.1 Mouser fails fast on auth errors.** In `app/connectors/mouser.py`, detect the "Invalid unique identifier" (and any other auth-shaped) response body, log once, and return `[]` without retrying. Match the pattern Nexar and BrokerBin already follow in `app/connectors/sources.py` (a single fast failure, no per-attempt backoff). Expected saving: ~8 s per search cycle.

**1.2 Rate guard only stamps `last_searched_at` on a successful run.** In `app/search_service.py`, hold off on stamping `last_searched_at` until after `_save_sightings` has committed real results, or stamp it inside an `else` branch after the try. The simplest shape: stamp only when the fresh-results list is non-empty OR when every connector explicitly succeeded with zero matches. Treat "all connectors errored" as a failure and do not stamp. This turns the rate guard into a real success gate.

**1.3 Parallelize `sightings_batch_refresh`.** In `app/routers/sightings.py`, replace the `for req in selected: await search_requirement(req, db)` serial loop with `asyncio.gather(*[search_requirement(r, db) for r in selected], return_exceptions=True)`. Each call uses its own write session already (`55093bf1` — "fix: use separate DB sessions for concurrent search task writes"), so this is safe. Wall time drops from N × slowest to 1 × slowest.

**Deliverables:** unit tests for Mouser fast-fail, an integration test that shows a failed search does not stamp `last_searched_at`, and a test for parallel batch refresh.

**APP_MAP:** update `docs/APP_MAP_INTERACTIONS.md` (search flow) and `docs/APP_MAP_ARCHITECTURE.md` (rate guard semantics).

### Phase 2 — Clean up stale credentials

Credentials live in two places. Phase 2 makes the DB copy authoritative where present, and gives operators a single place to rotate keys.

**2.1 Audit DB credentials.** Write a one-shot script (`scripts/audit_api_credentials.py`) that reads every row in `api_sources`, decrypts `credentials`, compares against the corresponding env var, and prints which ones differ. Do not modify anything. Run it once.

**2.2 Rotate broken keys.** Based on the audit, either clear the stale DB row (`UPDATE api_sources SET credentials=NULL WHERE name IN (...)`) so env fallback wins, or write fresh credentials into the DB. Record the chosen approach in the runbook.

**2.3 Add an operator-visible warning.** In `app/services/credential_service.py`, log a single warning at startup if the DB copy and env copy both exist and differ. This prevents future silent shadowing.

**Deliverables:** audit script, runbook entry in `docs/OPERATIONS.md` (or create it), startup warning.

**APP_MAP:** no code-structure change; document the credential resolution order in `APP_MAP_ARCHITECTURE.md`.

### Phase 3 — Per-endpoint HTMX timeout and real circuit breaker

Protects against future slow connectors without raising the global timeout (which would hurt other pages).

**3.1 Per-button `hx-timeout` on search actions.** In the search buttons on the sightings page and the RFQ parts tab, add `hx-timeout="45000"`. Do not raise `htmx.config.timeout` globally. This gives search 45 s while leaving every other HTMX call on the 15 s default.

**3.2 Circuit breaker trips on API-level failures.** In `app/connectors/sources.py`, change `_breaker.record_failure()` to also fire when a connector's `_do_search` path returns empty due to auth/quota/rate errors (the same failure paths the log warnings cover). Keep the happy path `record_success()` only when the response was actually 2xx with parseable data. Raise `reset_timeout` from 60 s to 300 s so a genuinely broken connector stays skipped for a useful window.

**3.3 Source status write fidelity.** In whatever code writes `api_sources.last_success` / `error_count_24h`, distinguish "empty result from a healthy API" (no change) from "empty result because the connector errored" (count as error). This is why Nexar and BrokerBin looked `live` in the DB.

**Deliverables:** templates updated, breaker tests, status-write tests, APP_MAP update covering the new breaker semantics.

---

## Out of scope

- The unrelated `RuntimeError: No response returned` on `GET /v2/partials/parts` seen in the logs. Different bug, different PR.
- Replacing the search architecture with streaming (already covered by an older spec, `2026-03-18-search-experience-redesign.md`).
- Re-subscribing to paid Nexar / BrokerBin plans.

## Testing strategy

Each phase adds its own tests. Before merging Phase 1, we run the live timing script again (`search_requirement()` against a real req with Redis flushed) and confirm the cold wall time drops from ~9.6 s to under 4 s. That is the primary success criterion.

## Risk and rollback

- **Phase 1.2** (conditional stamping) changes rate-guard semantics. A buggy implementation could leave `last_searched_at` never stamped, letting users spam the API. Mitigation: integration test that a successful search always stamps, a failed search never does.
- **Phase 1.3** (parallel batch refresh) increases connector load. Existing global concurrency semaphore (`search_concurrency_limit=10`) still applies, so connector APIs will not see more parallel traffic than they already tolerate for a single-requirement search.
- **Phase 3.2** (aggressive breaker) could skip a recovering connector. The 300 s reset and existing `reset_timeout` path handle recovery. Worst case: operator clears the breaker by restarting the app (breaker state is process-local).

Each phase is a single commit on a feature branch, merged as its own PR. Rollback = revert the PR.
