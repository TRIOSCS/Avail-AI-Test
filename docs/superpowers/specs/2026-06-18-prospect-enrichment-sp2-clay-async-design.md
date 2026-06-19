# Real Prospect Enrichment — SP2: Clay Async-Primary Layer

**Date:** 2026-06-18
**Status:** Architecture approved; detailed spec + plan to be finalized when SP1 lands and
Clay's HTTP API is configured.
**Owner:** prospecting
**Program:** Sub-project 2 of 2. Builds on **SP1**
(`2026-06-18-prospect-real-enrichment-design.md`) — SP1's synchronous credit-aware router
is SP2's **gap-fill engine**.

## Goal

Make **Clay the primary enrichment tool** the system tries first, because Clay is TRIO's
largest allocation *and* a meta-broker: it waterfalls across many data sources internally
and **bills a credit only when it returns data.** This maximizes broker coverage and
minimizes wasted credits in one provider. SP1's synchronous providers (Lusha → Apollo →
Explorium → AI) become the fallback that fills whatever Clay misses.

## Why async

Clay has no synchronous "enrich this domain" endpoint. It is a table/webhook tool:
the app POSTs a domain into a Clay table (inbound webhook), Clay runs its enrichment
columns, and a Clay action column POSTs the result to **our** callback endpoint
seconds-to-minutes later. So Clay-primary is a round-trip, not a blocking call.

## External dependency (on TRIO)

Clay-in-app requires, on the Clay side: (1) Clay's HTTP API / inbound webhook enabled;
(2) a Clay table with the desired enrichment columns (firmographics + contacts filtered to
procurement/supply-chain titles) and an HTTP-action column that POSTs results to our
callback URL with the correlation token echoed back. The connector's `normalize_callback`
is then configured to map *that table's* output columns → our canonical shapes. SP2 cannot
fire until this exists.

## Architecture

### 1. Clay connector (`app/connectors/clay.py` — new)

```python
async def enqueue(domain: str, correlation_token: str, callback_url: str,
                  api_key: str, webhook_url: str) -> bool
# POSTs {domain, correlation_token, callback_url} into the Clay inbound webhook/table.
# Returns True on accepted enqueue (HTTP 2xx), False otherwise.

def normalize_callback(payload: dict) -> dict
# Maps Clay's table output -> {"company": {...firmographics...},
#                              "contacts": [{name,title,seniority,email,verified}, ...]}
```

Uses the shared `app/http_client.py` `http` client. Surfaces 402/429 to trip Clay's
circuit in SP1's router registry (Clay is registered as a router provider too, so the
availability gate / circuit-breaker apply uniformly).

### 2. Clay callback endpoint (`app/routers/webhooks.py` — new route)

`POST /webhooks/clay/enrichment`

- **Auth (security-critical):** a shared-secret header (`clay_webhook_secret`) **and** a
  signed, single-use **correlation token** (itsdangerous, embeds `prospect_id` + nonce +
  issue time, max-age = `clay_timeout_minutes` + slack). Reject on bad secret, bad/expired
  token, or unknown prospect.
- **Idempotent:** if the prospect's `clay_status` is already `done`/`missed`, no-op (Clay
  may retry). Fill-only writes prevent clobbering.
- **Rate-limited** via the existing limiter.
- **Behavior:** validate → `normalize_callback` → ingest contacts + firmographics
  (fill-only) → run **SP1's synchronous router for any field/contact gap Clay left** →
  recompute `fit_score` + `readiness_score` → set `enrichment_data['clay_status']='done'`,
  `enrich_status='done'`. Never trusts payload shape blindly.

### 3. Async state machine (on `ProspectAccount.enrichment_data`, JSONB — no migration)

- `clay_status` ∈ `{pending, done, missed, error}`
- `clay_correlation` — the signed token (matched on callback)
- `clay_enqueued_at` — ISO timestamp (drives timeout)

### 4. Modified enrich flow (`run_enrichment_job`)

```
1. run_free_enrichment(...)                       # SAM.gov + news — instant, free
2. if clay_enabled and clay configured:
     enqueue Clay (domain + signed correlation + callback_url)
     set clay_status='pending', clay_enqueued_at=now, enrich_status='running'
     RETURN  (do not block; the callback or the timeout sweep finishes the job)
   else:
     run SP1 synchronous router now  (today's SP1 behavior)
3. recompute fit + readiness on whatever data we have so far
```

The buyer sees free data immediately; Clay's richer data lands on callback.

### 5. Timeout fallback — "info when needed" guarantee

If a prospect sits in `clay_status='pending'` longer than `clay_timeout_minutes`
(default **5**), a path runs SP1's synchronous router to fill from Lusha/Apollo/etc.,
recomputes scores, and sets `clay_status='missed'`, `enrich_status='done'`. Two triggers,
whichever first:
- the enrich-status **poll** handler notices the timeout and kicks the fallback inline; and
- a lightweight **scheduled sweep** (APScheduler) catches prospects whose buyer closed the
  tab (no poll) so the queue still converges.

This guarantees the team is never blocked waiting on Clay.

### 6. Router placement

Clay is added to SP1's `CAPABILITY_ORDER` as the **first** entry for both tasks, but
because Clay is async it is invoked via the enqueue/callback path, not the synchronous
`route()` loop. The synchronous `route()` (Lusha→Apollo→Explorium→AI) runs in the callback
and timeout paths as the gap-fill. Clay's circuit-breaker entry still applies (skip enqueue
if Clay recently 402/429'd → fall straight to synchronous router).

## Config (additions to SP1's)

```
clay_api_key: str = ""
clay_enrichment_enabled: bool = False
clay_inbound_webhook_url: str = ""     # Clay table's inbound URL we POST to
clay_callback_base_url: str = ""       # our public base for the callback URL we hand Clay
clay_webhook_secret: str = ""          # shared secret validating inbound callbacks
clay_timeout_minutes: int = 5
```

## Security review

The new public webhook is a `/security-review` item built in from the start: shared secret
+ signed single-use correlation token + idempotency + rate-limit + strict payload
validation + fill-only writes. No credential or PII leakage in logs (extend the Sentry
`before_send` scrubber for any new sensitive field).

## Testing strategy

- Connector: enqueue success/failure; `normalize_callback` mapping; 402/429 → trip circuit.
- Webhook: valid callback ingests + gap-fills + recomputes; bad secret/expired token/unknown
  prospect rejected; idempotent replay no-ops; malformed payload rejected.
- Timeout: pending past timeout → sync fallback runs + status `missed`; sweep catches
  no-poll prospects.
- Flow: Clay-enabled enqueues + returns fast (free data present, scores provisional); Clay
  disabled falls straight to SP1 synchronous behavior.

## Out of scope (SP2)

- Clay for CRM enrichment (CRM stays synchronous; a later add if wanted).
- Per-provider monthly caps (circuit-breaker only, per SP1).
