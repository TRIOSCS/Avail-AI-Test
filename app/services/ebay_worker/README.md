# eBay Search Worker

Queue-driven poller for eBay's **Browse API** (`GET
https://api.ebay.com/buy/browse/v1/item_summary/search`). It is the fourth
search worker and the first that is an **API poller** rather than a browser
automation: no Patchright, no Chrome, no Xvfb, no session manager, no
human-behavior simulation, and no AI commodity gate.

It reuses the same queue/save plumbing as the ICS / NC / TBF workers
(`app/services/search_worker_base`), so a requirement fans out to eBay exactly
the way it fans out to the browser marketplaces. One difference follows from
having no gate: `enqueue_for_ebay_search` creates rows as **queued**, not
`pending` — the AI gate is the only thing that promotes `pending -> queued`, so
a `pending` eBay row would never be claimable.

## Quick Start

```bash
# Credentials live in Settings -> Connectors -> eBay (encrypted in the DB).
# .env.ebay-worker is the fallback and the home of the EBAY_* tuning knobs.
PYTHONPATH=/root/availai /root/availai/.venv/bin/python -m app.services.ebay_worker.worker
```

## Architecture

```
ebay_worker/
├── worker.py          # Main loop: budget -> claim -> search -> parse -> save
├── config.py          # EBAY_* env vars with defaults
├── search_client.py   # Browse API paging (limit/offset), OAuth bearer reuse
├── result_parser.py   # JSON payload -> EbaySighting (strict MPN filter)
├── sighting_writer.py # EbaySighting -> AVAIL Sighting rows, item-id dedup
├── queue_manager.py   # Enqueue, dedup, claim, status updates
├── scheduler.py       # Flat min delay + daily call budget (midnight UTC reset)
└── circuit_breaker.py # Repeated-API-failure / auth-failure trip
```

## Credentials

`EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` — the same OAuth app credentials the
`ebay` connector already uses. Read **DB-first** through
`credential_service.get_credential("ebay", ...)` with an environment fallback,
so rotating the key in Settings reaches the worker without touching a file on
the host. That is the opposite of the browser workers, whose logins are
host-only `.env` secrets.

The bearer itself is minted by `app/connectors/ebay.get_ebay_access_token`, so
the worker and the connector share ONE process-wide cached token.

## Pacing

Two rules, no business hours and no random breaks — eBay does not care what
hour it is:

1. Wait `EBAY_MIN_DELAY_SECONDS` between calls.
2. Stop once `EBAY_DAILY_CALL_BUDGET` calls have been spent, and resume at
   midnight **UTC**.

The spend counter lives on the `ebay_worker_status` singleton
(`calls_today` / `budget_day`), so a restart mid-day does not hand the worker a
fresh allowance. A search does not start unless the whole `EBAY_MAX_PAGES`
allowance still fits, and failed or timed-out searches are charged for the calls
they actually spent — so `calls_today` is a real cap, not an estimate.

`EBAY_SEARCH_TIMEOUT_SECONDS` is the **per-request** timeout; the whole-search
deadline is that value x `EBAY_MAX_PAGES` plus slack, so a slow first page never
cancels a healthy multi-page search.

A persistent 429 (after the client has honored `Retry-After` and retried once)
re-queues the item and stands the worker down for 5 minutes — `failed` is
terminal for a `(requirement, MPN)` pair, so a throttle must never cost a
requirement its eBay coverage.

## Strict part-number match

eBay's `q=` is a full-text search: a query for `0F8NV` returns plenty of
listings that merely mention Dell. An item is kept only when the
alphanumeric-normalized MPN (uppercase, everything outside `[A-Z0-9]`
stripped) appears inside the alphanumeric-normalized title. Dell-style 5-char
part numbers are also accepted without their leading zero (`0F8NV` on the
label, `F8NV` in the listing). Items with `conditionId` 7000 ("For parts or
not working") are dropped, and auctions are dropped unless
`EBAY_INCLUDE_AUCTIONS` is set.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `EBAY_MARKETPLACE_ID` | `EBAY_US` | `X-EBAY-C-MARKETPLACE-ID` header |
| `EBAY_CATEGORY_IDS` | *(empty)* | No category filter = all of eBay |
| `EBAY_PAGE_LIMIT` | `50` | Results per page (API max 200) |
| `EBAY_MAX_PAGES` | `2` | Pages walked per MPN |
| `EBAY_DAILY_CALL_BUDGET` | `4000` | Browse API calls per UTC day |
| `EBAY_MIN_DELAY_SECONDS` | `3` | Minimum gap between calls |
| `EBAY_SEARCH_TIMEOUT_SECONDS` | `30` | Hard cap on one MPN's search |
| `EBAY_INCLUDE_AUCTIONS` | `false` | Include auction-only listings |
| `EBAY_POLL_IDLE_SECONDS` | `30` | Sleep when the queue is empty |
| `EBAY_DEDUP_WINDOW_DAYS` | `7` | Cross-requirement dedup window |
| `EBAY_BREAKER_COOLDOWN_MINUTES` | `30` | Circuit-breaker self-heal delay |

## Sighting shape

`source_type` is `ebay`. `vendor_name` is the seller username, `mpn_matched`
is the **queued** MPN (never the seller's spelling), `is_authorized` is always
False, and `confidence` runs 0.55–0.95 (base 0.55, +0.15 for a reported
quantity, +0.15 for a seller at ≥98% feedback, +0.10 when the MPN is a whole
title token). `raw_data` carries the deep link, item id, title, condition,
seller feedback, location, buying options, image and fetch timestamp.

Dedup within a requirement is keyed on **(vendor, eBay item id)** — one seller
routinely lists the same part several times at the same quantity, which the
shared (vendor, mpn, qty) key would collapse into one row.

## Deploy

`deploy/avail-ebay-worker.service` (no `avail-xvfb` dependency, no `DISPLAY`),
bootstrapped by `scripts/setup_ebay_worker.sh`. Health surfaces on the
Connectors tab (worker heartbeat, `WORKER_BACKED_SOURCES`) and in
`GET /api/admin/workers/status`; `app/jobs/worker_liveness_jobs.py` alerts on a
stale heartbeat.
