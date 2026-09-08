# eBay Search Worker — design

**Status:** approved 2026-09-05, built the same day.
**Scope:** one feature — turn eBay from a synchronous connector into the fourth
queue-driven search worker. Nothing else in the search pipeline changes.

---

## What this is

eBay stops running inside the user's search request and starts running as a
background poller.

Before: a search for `LM317T` built an `EbayConnector`, called the Browse API
inline, and blocked the fan-out on eBay's latency alongside seven other
connectors.

After: the requirement is queued to `ebay_search_queue` alongside the ICS /
NetComponents / The Broker Forum queues, and the `avail-ebay-worker` systemd
unit drains that queue on its own schedule, writing Sightings asynchronously.

It is the **fourth** search worker and the **first API poller** — the other
three drive a real Chrome through Patchright. That single difference is the
source of nearly every decision below.

---

## Why move it off the synchronous path

1. **eBay is a marketplace, not a distributor feed.** Its answer to "who has
   this part" is slower to earn than DigiKey's and worth waiting for
   out-of-band, not on the critical path of a buyer's keystroke.
2. **It needs more than one page to be useful.** The old connector took the
   first 30 results from ONE category. Board-level surplus is scattered across
   many eBay categories, and the good listing is regularly not in the first 30.
   Paging properly is a background job's work, not a request's.
3. **The results need filtering that costs nothing at write time and
   everything at read time.** eBay's `q=` is a full-text search; unfiltered it
   drops a lot of junk into the sightings table. The worker can afford to be
   strict.
4. **Rate limits belong on a budget, not on a request.** A daily call budget is
   only enforceable by something that remembers yesterday.

---

## Architecture

```
requirement created / re-searched
        |
        v
search_service._worker_enqueues()
   +--> enqueue_for_ics_search    (browser worker)
   +--> enqueue_for_nc_search     (browser worker)
   +--> enqueue_for_tbf_search    (browser worker)
   +--> enqueue_for_ebay_search   (API poller)      <-- new
        |
        v
   ebay_search_queue (queued -> searching -> completed / failed)
        |
        v
   avail-ebay-worker  (host systemd unit, python -m app.services.ebay_worker.worker)
        |
        +--> read budget (calls_today / budget_day on ebay_worker_status)
        +--> claim next queued item (FOR UPDATE SKIP LOCKED on PG)
        +--> search_client.search_mpn      -> Browse API, up to EBAY_MAX_PAGES
        +--> result_parser.parse_item_summaries -> strict filter -> EbaySighting[]
        +--> sighting_writer.save_ebay_sightings -> Sighting rows (source_type='ebay')
        +--> ebay_search_log row + mark_completed + heartbeat
        |
        v
   sleep EBAY_MIN_DELAY_SECONDS, repeat
```

Package layout mirrors `tbf_worker` minus every browser module:

```
ebay_worker/
├── worker.py          # main loop
├── config.py          # EBAY_* knobs
├── search_client.py   # Browse API paging + OAuth bearer reuse
├── result_parser.py   # payload -> EbaySighting, strict MPN filter
├── sighting_writer.py # EbaySighting -> Sighting, item-id dedup
├── queue_manager.py   # thin wrapper on search_worker_base.QueueManager
├── scheduler.py       # min delay + daily call budget
└── circuit_breaker.py # API-failure trip
```

There is deliberately **no** `session_manager.py`, `search_engine.py`,
`human_behavior.py` or `ai_gate.py`.

---

## Decisions and their reasons

### 1. API poller, not a browser worker

eBay has a real, documented, credentialed API. Driving a browser against a site
that offers an API would be strictly worse in every dimension — slower,
fragile against DOM churn, and a terms-of-service problem we do not need to
have. So no Patchright, no Xvfb, no Chrome, no persistent profile directory,
no human-behavior delay simulation.

### 2. No AI commodity gate

The browser workers gate each queued part through Claude Haiku first, because a
browser search is expensive: minutes of wall clock, a scarce logged-in session,
and detection risk. Classification is worth paying for when the thing being
rationed is that costly.

An eBay Browse call is none of those. What is scarce is the **call allowance**,
and a call budget rations that directly and predictably. Adding a classifier
would spend an LLM call to decide whether to spend a cheaper HTTP call, and
would silently skip parts a human explicitly asked about. So: **every queued
MPN is searched**, and the budget is the only limiter.

One consequence has to be stated in code, not just here: the shared
`QueueManager.enqueue_search` creates rows as `pending`, and the ONLY thing in
the codebase that promotes `pending -> queued` is the AI gate the browser
workers run. With no gate, a `pending` eBay row would never be claimable — the
queue would grow forever while the worker logged "queue empty". So
`QueueManager` takes an `initial_status` (default `PENDING`, so ICS/NC/TBF are
untouched) and the eBay queue manager passes `QUEUED`. An eBay row is
claimable the moment it is enqueued, which is also what the migration's
partial index `ix_ebay_queue_poll (WHERE status='queued')` assumes.

### 3. Pacing = min delay + daily call budget, resetting at midnight UTC

The shared `search_worker_base.scheduler.SearchScheduler` is not reused. Its
whole purpose is to make a browser look like a person: log-normal delay
distribution, random coffee breaks, a Sunday-evening-through-Friday-afternoon
business-hours window. None of that means anything to an API — eBay does not
care what hour it is, and pausing on Saturday would just make the queue longer.

What actually bounds an API poller is its rate-limit allowance, so
`ebay_worker/scheduler.py` implements exactly two rules:

1. Wait `EBAY_MIN_DELAY_SECONDS` between calls (default 3).
2. Stop once `EBAY_DAILY_CALL_BUDGET` calls have been spent (default 4000), and
   resume at midnight **UTC**.

Two details make the budget an actual cap rather than an estimate:

- **A search is not allowed to start unless its whole page budget fits.** One
  search spends up to `EBAY_MAX_PAGES` calls, so the gate is
  `scheduler.can_afford_search()` (remaining >= MAX_PAGES), not "any calls
  left" — otherwise the last search of the day always overshoots.
- **Failed searches are charged for what they actually spent.** `search_mpn`
  accumulates into a caller-owned `CallCounter`, so a timeout (coroutine
  cancelled) and a 500 on page 2 both book the calls already made. Booking a
  flat 1, or nothing, let real spend drift silently past the budget.

The singleton also has to exist for any of this to work: `update_worker_status`
and the budget writers are no-ops without the id=1 row, which would leave the
poller with an unlimited allowance and no log line. The worker seeds the row at
startup and re-seeds (with a warning) if it ever disappears.

UTC, not Eastern, because eBay's own call allowances are UTC-day based —
matching the boundary that actually resets is the point.

The counter lives on the `ebay_worker_status` singleton (`calls_today`,
`budget_day`), not in process memory, so a restart mid-day does not hand the
worker a fresh 4000 calls. The rollover is lazy: on the first tick of a new UTC
day the worker sees a stale `budget_day` and zeroes `calls_today`. No scheduled
reset job.

### 4. Credentials are DB-first, not a host-only `.env`

The browser workers keep their logins in host-only `.env.<worker>` files,
because those are reused human account passwords that should never sit in a
database — and the Connectors UI fully masks them for the same reason.

eBay is different: `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` are an OAuth
**application** credential that AVAIL already stores encrypted, that the
Settings → Connectors page already manages, and that the Test button already
exercises. Reusing that path means rotating the key in the UI reaches the
worker with no file edit on the host. So the worker reads them through
`credential_service.get_credential("ebay", ...)` — DB first, environment
fallback — and `.env.ebay-worker` carries only tuning knobs.

### 5. One shared OAuth bearer with the connector

`EbayConnector._get_token()` and the worker both mint through
`app/connectors/ebay.get_ebay_access_token()`, on the cache key
`("EbayConnector", client_id)` that `BaseConnector._token_cache_key()` already
produced. The helpers moved from the connector method to module level for
exactly one reason: the worker needs a token WITHOUT the connector's
category-restricted, 30-result search. No token logic was duplicated.

### 6. Strict part-number match

`q=0F8NV` on the Browse API is a full-text search, and it will happily return
"Dell PowerEdge R740 server, no drives". An item is kept only when the
**alphanumeric-normalized** MPN (uppercase, everything outside `[A-Z0-9]`
stripped) is a substring of the **alphanumeric-normalized** title. Both sides
are normalized the same way, so a seller's dashes, dots, slashes and casing can
never hide a real match.

One variant is accepted on top of that: a 5-character part number starting with
`0` also matches without its leading zero. That is the Dell convention — the
label reads `0F8NV`, and sellers routinely list it as `F8NV`. The reverse
direction needs no rule: a title containing `0F8NV` already contains `F8NV` as
a substring.

Also dropped: `conditionId` 7000 ("For parts or not working" — never a
sourceable offer), and auction-only listings unless `EBAY_INCLUDE_AUCTIONS` is
set (a live auction price is not a quotable number). Auctions are filtered
server-side too, via `filter=buyingOptions:{FIXED_PRICE|BEST_OFFER}`, so they
do not consume page slots.

### 7. Search all of eBay by default

`EBAY_CATEGORY_IDS` defaults to empty, meaning **no** `category_ids` parameter
and therefore all of eBay. The old connector pinned category `175673`
(Electronic Components & Semiconductors), which is wrong for this business:
board-level assemblies, FRUs, RAID controllers and memory sit under server,
networking and computer-parts categories. With the strict title filter doing
the precision work, a category filter only costs recall.

### 8. Item detail pages are never fetched

Everything a Sighting needs — seller, price, condition, availability, deep
link, image — is already in the search response. Per-item detail calls would
multiply the call budget by the page size for no new field.

### 9. Confidence on the 0.0–1.0 scale

The `Sighting` CHECK constraint (`ck_sightings_confidence_range`) demands
0.0–1.0. The old connector emitted `3` or `2`, which is a different scale
entirely. The worker scores:

| Component | Value |
|---|---|
| base (a strict-matched listing from an unvetted seller) | 0.55 |
| eBay reported an actual available quantity | +0.15 |
| seller feedback ≥ 98% | +0.15 |
| the MPN is a whole title token, not just a substring | +0.10 |
| **cap** | **0.95** |

Capped at 0.95 because a marketplace listing is never certainty.

### 10. Dedup keys on the eBay item id

The shared writer dedups on `(vendor, mpn, quantity)`. That is wrong for eBay:
one seller routinely posts the same part as several separate listings at the
same quantity and different prices, and the shared key would collapse them into
one row — losing the cheaper listing.

So `search_worker_base.sighting_writer.save_sightings` gained an **optional**
`dedup_key_fn` hook. The default is the existing triple, so ICS / NC / TBF are
byte-for-byte unchanged; eBay passes `(vendor, ebay_item_id)`. A stored row
carrying no item id (anything written before this change) falls back to the
default key, so nothing already in the table is silently merged. The existing-row
dedup query now also selects `raw_data` so the hook can read the id back out.

### 11. Circuit breaker reads HTTP, not a DOM

The browser breakers inspect the page for captcha / session markers. There is
no page here, so `ebay_worker/circuit_breaker.py` reads the HTTP outcome
instead: a 401/403 that survived the token re-mint trips **immediately** (the
credentials are wrong — retrying will not fix that), three consecutive
transport/5xx failures trip, and the base class's ten-empty-result streak still
guards against a silently broken query.

Three rules keep that breaker honest for an API poller:

- **The empty-result streak counts the RAW payload, not the filtered rows.**
  Dropping every hit is routine here — the strict MPN filter exists precisely
  to throw away eBay's fuzzy matches — so counting post-filter emptiness would
  trip the breaker (10 in a row) during perfectly healthy operation. A 200 with
  any `itemSummaries` counts as results.
- **Unexpected exceptions feed the breaker too.** A failure that is neither
  `httpx.HTTPError` nor `ValueError` (a token body with no `access_token`, a
  parser `AttributeError`) used to fail one queue item every 3 seconds forever
  with nothing ever tripping. `failed` is terminal for a
  `(requirement, normalized_mpn)` pair, so that quietly burned the queue.
- **A rate limit is not a failure of the item.** `search_client` mirrors
  `EbayConnector` on 429: honor `Retry-After`, retry once, then raise the typed
  `ConnectorRateLimitError`. The worker re-queues the item (never `failed`) and
  stands down for `RATE_LIMIT_BACKOFF_SECONDS` (300) instead of coming back in
  3 seconds. A 404 is deliberately NOT turned into an empty result set: this
  endpoint answers 200-with-nothing when eBay has nothing, so a 404 means the
  endpoint/marketplace is wrong, and faking "0 results" would COMPLETE the row
  and suppress re-search of that MPN for the whole dedup window.

Timeouts are two separate budgets, not one. `EBAY_SEARCH_TIMEOUT_SECONDS` is
the **per-request** httpx timeout; the outer `asyncio.wait_for` guard uses
`scheduler.search_deadline()` = that value x `EBAY_MAX_PAGES` + slack. Using one
number for both cancelled healthy multi-page searches — and threw away the pages
already fetched — whenever page 1 ran slow.

Every search attempt writes an `ebay_search_log` row, failures included (with
`error` set), so "why did eBay stop producing sightings" is answerable from the
database rather than from journald retention.

### 12. eBay is worker-backed, but not a *browser* worker

Two different lists, and eBay is in exactly one of them:

- `connector_service.WORKER_BACKED_SOURCES` — **yes.** The Connectors card
  should show worker heartbeat health (`worker_active` / `worker_down`), not a
  credential ladder. Because eBay renders as a `key` card (it owns real
  credentials) rather than a `browser_login` one, the worker-health line had to
  move out of the `browser_login` branch of `_connector_macros.html` and become
  a shared block, and `last_error` now renders for any worker-backed card —
  otherwise an eBay auth failure from `health_monitor` had nowhere to appear.
  The worker also writes `circuit_breaker_reason` when it has no credentials, so
  an unconfigured poller reads red-with-a-reason instead of a green pill above
  a worker that is only idling.
- `constants.BROWSER_WORKER_SOURCES` — **no.** Members of that set are excluded
  from the health_monitor ping loop and pinned to LIVE, because there is no
  connector to probe them with. eBay HAS a connector and real credentials, so a
  genuine auth or quota failure SHOULD flip its `api_sources` status the way it
  does for any other API.

That distinction forced one small change in the Connectors tab: testability was
previously "worker-backed ⇒ no Test button". It is now derived from
`connector_registry.source_has_test_path()`, which returns False for the three
browser workers (nothing to build) and True for eBay (its credentials build an
`EbayConnector`). The rendered result for ICS / NC / TBF is identical to before.

### 13. `EbayConnector` is kept, not deleted

Three callers still need it: the Settings → Connectors Test button, the
health_monitor credential ping, and `enrichment.harvest_ebay_titles` (eBay
listing titles feed the description ladder at tier 83). Only the **search
fan-out** stops using it — `_build_connectors` no longer constructs it, and
`_CONNECTOR_SOURCE_MAP` / `_MARKET_SOURCE_DISPLAY` drop it so a healthy worker
is never reported as a down synchronous market source.

---

## Schema (migration 219)

Three tables mirroring the TBF triple, with the `ebay` prefix:

- **`ebay_search_queue`** — `SearchQueueMixin`; compound unique
  `uq_ebay_queue_requirement_mpn` plus the partial `ix_ebay_queue_poll` /
  `ix_ebay_queue_dedup` indexes.
- **`ebay_search_log`** — `SearchLogMixin` + queue FK. `page_html_hash` holds
  the SHA-256 of the Browse API **JSON payload**; there is no HTML, but a
  response-shape change is still worth detecting.
- **`ebay_worker_status`** — `WorkerStatusMixin` singleton (`id = 1` CHECK,
  seeded by the migration and reseeded idempotently by
  `startup.seed_ebay_worker_status_singleton`) **plus two eBay-only columns**:
  `calls_today` (Integer, default 0) and `budget_day` (Date, nullable). Those
  two are the durable budget counter from decision 3; no other worker needs
  them, so they live on the concrete model rather than the shared mixin.

Additive and fully reversible. Round-tripped upgrade → downgrade → upgrade on a
throwaway PostgreSQL 16; the fresh-DB schema-drift gate is green because the
mixin derives every constraint/index name from `__tablename__`.

---

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `EBAY_MARKETPLACE_ID` | `EBAY_US` | `X-EBAY-C-MARKETPLACE-ID` header |
| `EBAY_CATEGORY_IDS` | *(empty)* | No category filter = all of eBay |
| `EBAY_PAGE_LIMIT` | `50` | Results per page (clamped to the API max, 200) |
| `EBAY_MAX_PAGES` | `2` | Pages walked per MPN |
| `EBAY_DAILY_CALL_BUDGET` | `4000` | Browse API calls per UTC day |
| `EBAY_MIN_DELAY_SECONDS` | `3` | Minimum gap between calls |
| `EBAY_SEARCH_TIMEOUT_SECONDS` | `30` | Hard cap on one MPN's search |
| `EBAY_INCLUDE_AUCTIONS` | `false` | Include auction-only listings |
| `EBAY_POLL_IDLE_SECONDS` | `30` | Sleep when the queue is empty |
| `EBAY_DEDUP_WINDOW_DAYS` | `7` | Cross-requirement dedup window |
| `EBAY_BREAKER_COOLDOWN_MINUTES` | `30` | Circuit-breaker self-heal delay |

`EBAY_MIN_DELAY_SECONDS` and `EBAY_DEDUP_WINDOW_DAYS` come from the shared
`search_worker_base.config.build_worker_config` factory. Its other common
fields are browser-only (`BROWSER_PROFILE_DIR`, max/typical delay) and are
deliberately not copied onto `EbayConfig` — an API poller must not carry dead
browser knobs.

---

## Sighting shape

| Field | Source |
|---|---|
| `vendor_name` | `seller.username` |
| `mpn_matched` | the **queued** MPN — never the seller's spelling |
| `qty_available` | `estimatedAvailabilities[0].estimatedAvailableQuantity`, else 1 |
| `unit_price` / `currency` | `price.value` / `price.currency` |
| `condition` | explicit eBay label map first (New / New other / Open box → new; the five Refurbished labels → refurb; Used → used), then the shared `normalize_condition` |
| `is_authorized` | always False — every eBay seller is an open-marketplace seller |
| `source_type` | `ebay` |
| `confidence` | decision 9 |
| `raw_data` | `click_url`, `ebay_item_id`, `ebay_title`, `ebay_condition`, `ebay_condition_id`, `seller_feedback_pct`, `seller_feedback_score`, `item_location_country`, `buying_options`, `image_url`, `fetched_at` |

The explicit label map exists because the shared `_CONDITION_MAP` does not know
eBay's vocabulary: "Open box" contains no keyword it recognizes at all, and the
`X - Refurbished` family must land on `refurb` rather than falling through to
`None`.

---

## Deploy

- `deploy/avail-ebay-worker.service` — modelled on the TBF unit but with **no**
  `Requires=avail-xvfb.service` and **no** `DISPLAY`. `MemoryMax=1G`,
  `CPUQuota=25%` (half the browser workers' budget — there is no Chrome here).
- `scripts/setup_ebay_worker.sh` — venv + unit install only; no Chrome, no
  Xvfb, no Patchright registration.
- `.env.ebay-worker.example` — the `EBAY_*` knobs, with the credentials
  commented out and a pointer to Settings → Connectors.
- `deploy.sh` Step 6b restarts `avail-ebay-worker` with the other three units.

Health surfaces in three places: the Connectors tab (heartbeat via
`WORKER_BACKED_SOURCES`), `GET /api/admin/workers/status` (heartbeat + queue
depth), and `app/jobs/worker_liveness_jobs.py` (debounced stale-heartbeat and
open-breaker alerts to Teams + Sentry).

---

## Deliberately NOT in this change

- **`source_trust.py` re-tuning.** `ebay` remains in `MARKETPLACE_SOURCES` with
  its existing weight. Now that eBay rows pass a strict part-number filter they
  are arguably more trustworthy than before, but changing trust scoring moves
  every ranked list in the app and deserves its own change with its own
  before/after evidence.
- **Backfilling old `ebay` Sightings.** Rows written by the old synchronous
  connector keep their old confidence scale and their old `raw_data` shape. The
  eBay dedup key falls back to the shared triple for any row without an
  `ebay_item_id`, so they coexist safely.
- **Any change to the other three workers.** The `dedup_key_fn` hook defaults to
  their existing behavior precisely so this change cannot touch them.
