# Paced Web-Search Enrichment Worker — Design Spec

**Date:** 2026-06-05
**Status:** Approved design, pending implementation plan
**Builds on:** the verified-material-enrichment feature (`enrichment_status`/`enrichment_provenance`, `authoritative_enrichment_service`, `ai_inference_fallback`) merged in `ae8ae4f6`.

## 1. Problem & Goal

The one-shot bulk importer hammered the free-tier distributor APIs at high concurrency and **exhausted/throttled them** (OEMSecrets out of daily calls, DigiKey 401/429 storms, element14 QPS caps). Result: of 1,827 parts, only ~47 verified, ~853 `not_found`, ~895 never loaded. Bulk-API enrichment is the wrong shape.

**Goal:** a dedicated, **paced background worker** that fills `material_cards` descriptions/specs for `not_found`/`unenriched` parts, **web-search-first**, within quotas, self-healing over hours/days — never thrashing. New data is grounded in **authorized-distributor or manufacturer-official pages only**, to an extreme-confidence bar.

## 2. Approved decisions (from brainstorming)

| Decision | Choice |
|---|---|
| Gap-fill source | **Web search** (Claude `web_search_20250305`, the proven `ai_live_web` mechanism), NOT raw HTML scraping (no ToS/anti-bot/maintenance burden). |
| Trust of web data | A **distinct `web_sourced` tier**, below API-`verified`, always carrying the source URL(s); separately filterable. |
| Source restriction | `web_sourced` accepted **only** from an authorized-distributor or manufacturer-official domain, **validated in code** (never trusting the LLM's claim). |
| Confidence | **Extreme bar** + exact-MPN-verbatim-on-page proof (see §5 gates). |
| Runtime | **Dedicated background worker** (repurpose the disabled `enrichment-worker` container), **steady pace**, self-healing. |

**Trust ladder:** `verified` (structured API) › `web_sourced` (grounded, gated) › `ai_inferred` (Opus ≥0.95, reconfirm) › `not_found` › `unenriched`.

## 3. Prerequisite: harden the enrichment type-foundation (do FIRST)

Adding a fifth tier (`web_sourced`) to a stringly-typed, scattered vocabulary is a footgun (type-design review). Before the worker, make the tier vocabulary a single enforced source of truth:

- **`MaterialEnrichmentStatus(StrEnum)`** in `app/constants.py` (mirroring the existing `DigestEntityType`/`DigestStatusSignal` StrEnums): `UNENRICHED="unenriched"`, `VERIFIED="verified"`, `WEB_SOURCED="web_sourced"`, `AI_INFERRED="ai_inferred"`, `NOT_FOUND="not_found"`. StrEnum is `==`-compatible with the existing string literals → drop-in, zero breaking changes to existing comparisons/templates.
- **`@validates("enrichment_status")`** on `MaterialCard` (mirror the validator pattern already in `app/models/intelligence.py`) → `return MaterialEnrichmentStatus(value).value`; a typo/out-of-set value fails loudly at write time.
- **Widen the column** `String(20)→String(32)` via the migration in §6 (`"claude_opus_inferred"` is already exactly 20 chars — zero headroom).
- **`enrichment_provenance` TypedDicts** in a small `app/services/enrichment_types.py`: `FieldProvenance = TypedDict({"source": str, "confidence": float, "fetched_at": str, "matched_mpn": NotRequired[str]})` and an `EnrichmentProvenance` with optional top-level `reconfirm_needed: bool`, `web_sourced: bool`, `source_urls: list[str]`, `source_domains: list[str]`. The JSONB column stays `dict` at the ORM layer; the TypedDicts constrain the producer functions (`merge_authoritative`/`apply_authoritative`/`apply_web_sourced`/the AI path) under mypy.
- **Derive the worker/import counts dict keys from the enum** (not hardcoded), so a renamed tier can't silently drop a bucket.

The services/router/template keep using their existing comparisons (now enum-backed). New code (`web_sourced`) uses the enum.

## 4. Silent-failure fixes folded into this work

The error-handling review found real gaps (they caused the import thrash). Fix them as part of this build:

- **F1/F2 — rate-limit handling (HIGH).** Today `ConnectorRateLimitError` (incl. element14's QPS-403 reclass) gets **no backoff** (`_search_with_retry`'s `except ConnectorError` re-raises) and is **not disabled** by `fetch_authoritative` (only Quota/Auth are), so a throttled source is retried every MPN and trips its breaker. Fix:
  - In `fetch_authoritative`, handle `ConnectorRateLimitError` and breaker-open `ConnectorError` explicitly: **apply a per-source cooldown** rather than permanent disable. Track `{source: cooldown_until}` (passed alongside `disabled`); a source that rate-limits is skipped until `cooldown_until` (e.g. now + 5 min), then retried. This suits the **long-lived worker** (permanent disable would kill a source forever) while still stopping the per-MPN thrash. Genuine Quota/Auth still → disable-for-run.
  - Log at WARNING **with `str(e)` and the MPN** (not just `type(e).__name__`), so a source-wide failure is greppable, and increment a per-source failure counter surfaced in the worker heartbeat/summary.
  - Fix the **misleading element14 comment** (it does not "retry with backoff") and **tighten the QPS classification**: classify a 403 as a rate limit **only when** the body lower-cases to contain `"queries per second"` **AND does NOT** contain any auth marker (`"invalid"`, `"unauthorized"`, `"forbidden"`, `"api key"`, `"not accepted"`); otherwise it stays `ConnectorAuthError`.
- **F3 — `asyncio.gather(..., return_exceptions=True)`** in any concurrent enrich loop (import script + the worker if it batches concurrently); map an exception result to a `status="error"` report row (logged with MPN) so one poison MPN can't sink a whole batch/commit.
- **F4 — guard the per-chunk `db.commit()`** (try/except → log ERROR with chunk range, rollback that chunk, continue) and **write the report in a `finally`** so a mid-run failure still yields a partial report.
- **F5 — `not_found` provenance.** On `not_found`, set `enrichment_source = None` (or `"none"`) — NOT `"claude_opus_inferred"` — and clear/normalize `enrichment_provenance` so an unresolved part isn't labeled as if an inference source produced it.

## 5. Components

### 5.1 `app/services/enrichment_worker/trusted_domains.py`
The security gate (pure code, version-controlled, reviewed — not a DB/config table).
- `AUTHORIZED_DISTRIBUTORS: frozenset[str]` — exact hostnames: digikey.com (www), mouser.com, newark.com, element14.com, farnell.com, arrow.com, avnet.com, ttiinc.com, {uk,us,www}.rs-online.com, futureelectronics.com.
- `MANUFACTURER_DOMAINS: dict[str, str]` — suffix→canonical-name map (st.com, ti.com, analog.com, infineon.com, samsung.com, bourns.com, nxp.com, microchip.com, onsemi.com, vishay.com, murata.com, tdk.com, te.com, molex.com, amphenol.com, rohm.com, renesas.com, …). Extensible by adding entries + deploying.
- `is_trusted_domain(url) -> bool`: parse with `urllib.parse.urlparse`; require http(s) scheme + non-empty hostname (lowercased); exact match in distributors OR `hostname == key or hostname.endswith("." + key)` for a manufacturer key. The dot-prefix suffix match prevents `evil-st.com` matching `st.com`.

### 5.2 `app/services/enrichment_worker/web_extractor.py`
`WebExtractResult` dataclass (`status: "web_sourced"|"failed"`, description, manufacturer, category, datasheet_url, confidence, source_urls, source_domains) and:
```python
async def extract_part_from_web(display_mpn, normalized_mpn, *, timeout=90) -> WebExtractResult
```
Uses the `ai_live_web` mechanism: `claude_json(prompt, system=…, model_tier="smart", max_tokens=1200, tools=[{"type":"web_search_20250305","name":"web_search","max_uses":4}], timeout=…)` with a JSON-schema-hint prompt requesting description/manufacturer/category/datasheet_url/confidence/**exact_mpn_found**/source_urls. System prompt: extraction-only, authoritative pages only, "Return ONLY valid JSON. Never invent data."

**Four gates, enforced in Python AFTER the call (never trusting the LLM):**
1. **Domain allowlist** — every accepted source URL must pass `is_trusted_domain`; require ≥1 trusted URL, else `failed` (log rejected domains).
2. **Exact MPN verbatim** — `normalize_mpn_key(exact_mpn_found) == normalized_mpn`, else `failed`.
3. **Extreme confidence** — `confidence >= 0.92`, else `failed`.
4. **URL capture** — store only Gate-1-passing URLs in `source_urls`.
Plus a quality check: non-empty `description` (≥10 chars) and non-empty `manufacturer`, else `failed`. Any `claude_json` exception → log warning, return `failed` (and signal `web_search` for cooldown).

### 5.3 `enrich_card` chain (modify `authoritative_enrichment_service.py`)
Insert the web tier between the distributor merge and the Opus fallback:
```
authoritative distributors (verified)
  → [no hit] → web extractor (web_sourced)
    → [gate fail] → Opus ≥0.95 (ai_inferred)
      → [else] → not_found
```
New `apply_web_sourced(card, result)` sets the fields (non-empty only), `enrichment_status="web_sourced"`, `enrichment_source="web_search"`, `enriched_at=now`, and provenance `{web_sourced: True, confidence, source_urls, source_domains, fetched_at, <per-field>{source:"web_search",…}}`. The web step is gated by the `disabled`/cooldown set (skip when `"web_search"` is disabled or in cooldown or the daily web cap is hit). **Concurrency invariant preserved** — the web call is a pure `await` with no DB op before the attribute writes.

### 5.4 `app/models/enrichment_worker_status.py` + migration (singleton)
Singleton (`CheckConstraint("id = 1")`) parallel to `IcsWorkerStatus`: `is_running`, `last_heartbeat`, `last_enriched_at`, `enriched_today`, `web_sourced_today`, `ai_inferred_today`, `not_found_today`, `circuit_breaker_open`, `circuit_breaker_reason`, `daily_stats_json`, `updated_at`. Helper `update_enrichment_worker_status(db, **kw)`.

### 5.5 `app/services/enrichment_worker/` worker
- `config.py` — `EnrichmentWorkerConfig` from env: `ENRICHMENT_BATCH_SIZE`(5), `ENRICHMENT_DAILY_CAP`(200), `ENRICHMENT_WEB_DAILY_CAP`(80), `ENRICHMENT_LOOP_SLEEP_SECONDS`(30), `ENRICHMENT_IDLE_SLEEP_SECONDS`(300), `ENRICHMENT_NOT_FOUND_RETRY_HOURS`(22), `ENRICHMENT_CIRCUIT_BREAKER_ERRORS`(5).
- `circuit_breaker.py` — `EnrichmentCircuitBreaker(CircuitBreakerBase)` with `record_claude_error/success`; trips after N consecutive Claude errors; 1h cooldown.
- `worker.py` + `__main__.py` (mirror `ics_worker`): async loop with SIGTERM/SIGINT graceful shutdown.
  - **Anti-spin batch query:** `deleted_at IS NULL AND is_internal_part IS False AND (status=='unenriched' OR (status=='not_found' AND (enriched_at IS NULL OR enriched_at < now-RETRY_HOURS)))`, `ORDER BY search_count DESC, created_at ASC`, `LIMIT BATCH_SIZE`. So `unenriched` is always eligible; `not_found` retried at most ~once/day (self-heal as quotas reset).
  - **Web daily budget:** Redis/`intel_cache` counter `enrichment_worker:web_calls:{YYYY-MM-DD}`; skip the web tier (fall to Opus) when `>= ENRICHMENT_WEB_DAILY_CAP`; increment after each web call.
  - **Per-batch** `disabled`/cooldown set threaded into `enrich_card`; consult `ApiSource.status`/`error_count_24h` to skip a source the app already flagged unhealthy.
  - Commit per batch (guarded, F4); `enriched_at` always stamped; update heartbeat + per-tier daily counters; daily reset at UTC midnight (archive yesterday to `daily_stats_json`). Idle sleep when the queue is empty; `DAILY_CAP`/breaker → long sleep.

### 5.6 `docker-compose.yml` — re-enable
Replace the disabled block: `command: ["python","-m","app.services.enrichment_worker"]`, `restart: always`, `env_file: .env` + `ENRICHMENT_*` env, `depends_on: {db: healthy, redis: healthy, app: healthy}` (app-healthy ensures migrations ran), `healthcheck: disable`, mem limit 512M. Add an `enrichment_worker_enabled`-style gate if we want to toggle it.

## 6. Migration
`alembic/versions/088_enrichment_worker_status.py`, `down_revision = "a1f7c2d9e4b8"` (current head): create `enrichment_worker_status` + seed `INSERT … VALUES (1)`; **alter `material_cards.enrichment_status` to `VARCHAR(32)`**. Test `upgrade→downgrade→upgrade` (Postgres; SQLite via model schema).

## 7. UI
Add the `web_sourced` badge to `list.html` (blue "WEB-SOURCED", source URL as a link/tooltip from `enrichment_provenance.source_urls[0]`); add a `web_sourced` option to the materials status filter (the existing "Verified only" stays API-only). A small worker-status surface (last_heartbeat + today's tier counts) is optional/nice-to-have.

## 8. Data flow
`worker loop → batch query (anti-spin) → per card enrich_card[verified→web_sourced→ai_inferred→not_found] → guarded commit → heartbeat/counters → sleep`. Web tier gated by daily cap + cooldown; sources self-heal across cycles.

## 9. Error handling
Per-card failures isolated (card keeps status, retried later per backoff). Rate-limit → per-source cooldown (F1/F2). Quota/Auth → disable-for-run. Claude errors → circuit breaker (trip after N, sleep 1h). Commit failures guarded (F4). Concurrent batches (if used) → `return_exceptions=True` (F3). Graceful SIGTERM.

## 10. Testing (pytest, mocked Claude/web_search + connectors)
- `trusted_domains`: distributor exact-match; manufacturer suffix-match; `evil-st.com` rejected; non-http rejected.
- `web_extractor`: each gate rejects (untrusted domain, MPN mismatch, low confidence, empty desc) → `failed`; all-pass → `web_sourced` with source_urls; claude error → `failed`.
- `enrich_card`: chain transitions incl. the new `web_sourced` slot; concurrency invariant intact; `not_found` provenance/source correct (F5).
- rate-limit cooldown (F1/F2): a rate-limited source is skipped during cooldown, retried after; quota/auth still disables.
- batch query anti-spin: `unenriched` selected; `not_found` within RETRY_HOURS skipped, older retried; ordering by search_count.
- worker: one loop iteration (mock enrich), heartbeat update, daily cap halt, circuit-breaker trip, graceful shutdown; web daily-cap gating skips web tier.
- enum/validator: invalid `enrichment_status` rejected; counts dict derived from enum.

## 11. Rollout
1. Prereqs (§3) + silent-failure fixes (§4) + migration (§6); full suite green; `pre-commit run --all-files`.
2. **Load all 1,827 bare cards** (import endpoint, no enrichment) so the worker has the full set.
3. Deploy (`deploy.sh --no-cache`) with the worker service enabled; verify it heartbeats + enriches a few cards; watch logs for cooldown/breaker behavior.
4. Let it grind; monitor `enrichment_worker_status` + per-tier growth over a day. Update `APP_MAP_*` docs.

## 12. Out of scope (YAGNI)
Raw HTML scraping; parametric facet sliders; multi-worker scaling; auto-expanding the manufacturer-domain allowlist (curated + grown by PR).

## 13. Known follow-ups
- Manufacturer-domain allowlist is curated (top vendors); parts whose manufacturer isn't on it won't get `web_sourced` from the manufacturer site (still eligible via authorized distys). Grow the list as needed.
- DigiKey daily-quota exhaustion from the earlier bulk run may persist until reset; the worker's pacing + cooldown avoids re-triggering it.

## 14. Addendum (2026-06-05) — Auto-enrich new part numbers via the saved worker

**Request:** "save the worker and when a new part number is added it should be sent to search along with api and AI inquiries to get the best data possible."

**Finding:** every path that creates a `MaterialCard` already leaves it `enrichment_status='unenriched'` (column server_default + `@validates`): `resolve_material_card`, `POST /api/materials/import-part-numbers`, `POST /api/materials/import-stock`, and the background email-attachment job. The worker's `select_batch` already selects `unenriched` cards. So new parts already reach the worker — the worker IS the single enrichment authority. The only gap was *promptness/priority* for a just-added part.

**Decision:** keep the worker as the single authority; route new parts to it (no inline at-creation full-pipeline enrichment — that thrashed free-tier quotas and is explicitly rejected). Two behavior changes + a contract test:
1. **Fresh-part fast lane** — `select_batch` ordering tiebreaker `created_at ASC → DESC`. Demand stays primary (`search_count DESC`); among equal demand (the common case where new parts have `search_count=0`) the most-recently-added part is enriched first, so a just-added part heads the next batch.
2. **Idle responsiveness** — `idle_sleep_seconds` 300 → 60 (config default, `from_env()` fallback, AND `docker-compose.yml` Compose fallback — all three, or the change is inert in the container). A part added to an otherwise-empty queue is enriched within ~1 min.
3. **Contract regression test** — assert new cards (constructor + `resolve_material_card`) are `unenriched` and selected by `select_batch`, so a future creation path can't silently bypass enrichment.

**Left as-is (follow-up, not a band-aid conflict):** the inline `_schedule_background_enrichment` during search is a manufacturer-only connector hint that does NOT set a terminal `enrichment_status`, so the worker still performs the authoritative full pass. Unifying it into the worker is a separate, non-blocking follow-up.
