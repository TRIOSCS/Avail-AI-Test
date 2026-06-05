# AvailAI Application Map — Interaction & Data Flow

> **Auto-maintained reference.** Update this file whenever service interactions, data flows, or integration patterns change.

## Core Business Flow

```
Customer RFQ -> Requisition -> Requirements (parts list)
                                    |
                          Search 10+ sources in parallel
                                    |
                          Sightings (vendor quotes scored T1-T7)
                                    |
                          Sourcing Leads (AI-ranked, buyer reviews)
                                    |
                          Send RFQs via Graph API email
                                    |
                          Vendor Responses -> AI-parsed -> Offers
                                    |
                          Build Quote (selected offers + margin)
                                    |
                          Send Quote -> Customer accepts
                                    |
                          Buy Plan (PO tracking, fulfillment)
```

---

## 1. Requisition Creation

```
Browser POST /v2/partials/requisitions/create
    |
    v
htmx_views.py (router)
    |
    +---> requisition_service.py --> DB: INSERT requisitions
    |
    +---> ai_intake_parser.py (if freeform text)
    |       +---> claude_client.py --> Anthropic API
    |               +---> Returns structured parts list
    |
    +---> DB: INSERT requirements (one per part line)
    |
    +---> material_card_service.py
    |       +---> DB: UPSERT material_cards (dedup by normalized_mpn)
    |               +---> DB: UPDATE requirements.material_card_id = primary_card_id (link)
    |
    +---> activity_service.py --> DB: INSERT activity_log
```

System and RFQ activity events route through `activity_service.log_activity()`,
the canonical writer (`log_rfq_activity()` is kept as a thin delegating alias).
Email and call events are written by `log_email_activity()`/`log_call_activity()`,
which run their own contact-matching. The requisition Activity tab reads its
timeline back via `activity_service.get_requisition_activities()` rather than an
inlined query. Offer creation and offer status changes now also route through
`activity_service.log_activity()` (`ActivityType.OFFER_CREATED` /
`ActivityType.OFFER_STATUS_CHANGED`) so offer events appear on the requisition
Activity tab. Task completion, requisition assignment (claim/unclaim/batch),
archive/unarchive, and sales-note edits likewise route through
`activity_service.log_activity()` (`ActivityType.TASK_COMPLETED`,
`ASSIGNMENT_CHANGED`, `REQ_ARCHIVED`/`REQ_UNARCHIVED`, `SALES_NOTE`).

**AI curation:** each search batch logs one aggregated `sighting_added` row
("N sightings added from <sources>", with `details={count, sources}`).
`log_activity()` flags inherently-meaningful event types `is_meaningful=True`
at write time (cheap, deterministic); the high-volume / free-text types
(`sighting_added`, `email_received`) are left unscored and classified by the
`activity_quality_service` AI pass (`score_unscored_activities`, allow-list
keyed on `activity_type`). The requisition Activity tab defaults to meaningful
events — `get_requisition_activities(meaningful_only=True)` keeps `is_meaningful`
True-or-unscored and hides AI-rejected rows — with a `show_all` toggle.

The requisition Activity tab (`requisitions/tabs/activity.html`) renders these
rows as one date-grouped chronological timeline (newest-first, "Today" /
"Yesterday" / dated headers keyed on `occurred_at or created_at`). RFQ sends
appear inline as `rfq_sent` events — there is no separate "RFQ History"
section. Each row's leading glyph comes from the `activity_icon` macro
(`shared/_macros.html`), which maps the canonical `ActivityType` values to a
heroicon + accent color (unmapped types fall back to a neutral info glyph).
Vendor attribution on a row reads `vendor_card.display_name` (the canonical
attribute — `VendorCard` has no `name`). The paginated account/contact timeline
read helpers (`get_account_timeline` / `get_contact_timeline`) `selectinload`
`user`/`company`/`vendor_card` so serializing a page is O(1) queries, not O(N).

**Phone calls** (manual logs and the 8x8 CDR poll) log the canonical
`ActivityType.CALL_LOGGED` type; inbound/outbound is carried on the `direction`
column (not encoded in `activity_type`). Readers that distinguish direction
(e.g. AVAIL scoring's outbound-follow-up metric) filter on `direction`.

**Enabling 8x8 call logging** (operator/ops action — not code; the
`EIGHT_BY_EIGHT_ENABLED` default stays `False`):
1. In `.env`: set `EIGHT_BY_EIGHT_ENABLED=true` and supply
   `EIGHT_BY_EIGHT_API_KEY`, `EIGHT_BY_EIGHT_USERNAME`,
   `EIGHT_BY_EIGHT_PASSWORD`, `EIGHT_BY_EIGHT_PBX_ID`
   (`EIGHT_BY_EIGHT_TIMEZONE` / `EIGHT_BY_EIGHT_POLL_INTERVAL_MINUTES` have
   defaults).
2. Per user whose calls should be logged: set their `eight_by_eight_extension`
   and enable their per-user `eight_by_eight_enabled` toggle in user settings.
3. On restart, `register_eight_by_eight_jobs()` schedules the CDR poll. Calls
   reverse-matched to a CRM company with an open requisition appear on that
   requisition's Activity tab as `call_logged` events.

## 2. Search (User-Initiated Only)

Sourcing is strictly user-initiated. There is no background cron, no
auto-enqueue on requirement creation, and no row-click POST. Two entry
points trigger a search:

- Per-row search icon on `/v2/sightings`
- Detail-panel "Search" button (`m.search_button` macro)

Both POST `/v2/partials/sightings/{requirement_id}/refresh?source=user`.

A 48-hour per-MPN cooldown is enforced via `MaterialCard.last_searched_at`.
Every MPN whose card was searched within 48h is skipped; prior sightings
on those MPNs (across all requirements) are surfaced via the
`material_card_id` linkage on Sighting rows.

### 2a. Search-page part-history panel ("What we know")

The `/v2/search` results shell (`results_shell.html`) renders a two-column
grid: the left column streams live supplier offers over SSE (above), and the
right column shows the searched part's **internal history**, loaded in
parallel with — and independent of — the SSE stream:

```
results_shell.html (right column)
    |
    +---> hx-get /v2/partials/search/history?mpn=<searched mpn>   (hx-trigger=load)
              |
              v
          htmx_views.search_history_panel
              |
              +---> normalize_mpn_key(mpn)        # same key MaterialCard stores
              +---> part_history_service.get_part_history(db, key)   # READ-ONLY
              |        resolves MaterialCard (deleted_at IS NULL), then aggregates
              |        BY material_card_id: offers, distinct buyers, confirmed/won
              |        (won/sold offers + won requisitions + customer purchases),
              |        sightings, requirements, and a min/max/last price trend.
              +---> renders history_panel.html (or empty state if no card)
```

`get_part_history` is the single source of truth for a part's history; the
materials detail router (`material_detail_partial`, `material_tab_partial`)
consumes the same `*_for_card` helpers, so the search panel and the full part
page can never drift. The endpoint is wrapped in try/except (logged via
Loguru) and degrades to an empty/error panel rather than failing the page.

```
Browser POST /v2/partials/sightings/{requirement_id}/refresh?source=user
    |
    v
sightings.py (router) → search_requirement(req, db)
    |
    +---> _mpn_cooldown_partition(pns) → (to_search, cached_card_ids)
    |     Per-MPN 48h cooldown. Cards inside the window are partitioned
    |     out; their material_card_id is returned for the detail-panel
    |     query so prior sightings remain visible.
    |
    +---> _fetch_fresh(to_search) — every live HTTP connector in parallel
    |       (asyncio.wait, search_total_timeout_s budget)
    |
    +---> enqueue_for_ics_search(requirement_id, db)   # browser worker queue
    +---> enqueue_for_nc_search(requirement_id, db)    # browser worker queue
    |
    +---> _save_sightings + scoring + material card upsert
    |
    +---> Stamp MaterialCard.last_searched_at = now on every searched card
    |
    +---> Returns {sightings, source_stats, mpn_results: {mpn: "searched"|"cached"}}
    |
    v
search_service.py (orchestrator)
    |
    +---> ai_part_normalizer.py --> Claude API (normalize MPN)
    |
    +---> asyncio.wait(tasks, timeout=settings.search_total_timeout_s)
    |     -- ALL connectors fire in parallel, bounded by the search budget
    |     (default 12s, env SEARCH_TOTAL_TIMEOUT_S). Pending tasks when the
    |     deadline expires are cancelled and recorded in source_stats with
    |     error="search budget exceeded"; completed connectors' results are
    |     preserved so the response degrades gracefully rather than 504.
    |
    |       +---> nexar.py ----------> Octopart/Nexar API
    |       +---> brokerbin.py ------> BrokerBin API
    |       +---> digikey.py --------> DigiKey API
    |       +---> mouser.py ---------> Mouser API
    |       +---> element14.py ------> Element14 API
    |       +---> ebay.py -----------> eBay API
    |       +---> oemsecrets.py -----> OEMSecrets API
    |       +---> sourcengine.py ----> SourceEngine API
    |       +---> email_mining.py ---> DB: query email_intelligence
    |       +---> ai_live_web.py ----> Claude API (web fallback)
    |       +---> sources.py --------> DB: query source_stocks (local)
    |
    |   Each returns: [{mpn, vendor, qty, price, lead_time, source}]
    |
    +---> vendor_utils.py (fuzzy match, dedup vendor names)
    |       +---> DB: UPSERT vendor_cards
    |
    +---> scoring.py (6-factor: price, qty, freshness, auth, confidence, vendor)
    |       +---> evidence_tiers.py (assign T1-T7)
    |
    +---> DB: UPSERT sightings (dedup by requirement + vendor + mpn)
    |
    +---> DB: UPDATE requirements.material_card_id = primary_card_id (link req to card)
    |
    +---> material_card_service.py --> DB: UPSERT material_vendor_history
    |
    +---> sourcing_leads.py --> sourcing_score.py
    |       +---> DB: UPSERT sourcing_leads + lead_evidence
    |
    +---> sighting_aggregation.py --> DB: UPSERT vendor_sighting_summary
    |       +---> rebuild_vendor_summaries_from_sightings() is a TRIGGER, not
    |             a filter — when new sightings land, it always rebuilds ALL
    |             vendor summaries for the requirement (never a subset). The
    |             `sightings` arg only signals "do anything if at least one
    |             carries a vendor_name"; the function then aggregates from
    |             the live Sighting rows for that requirement_id. Passing a
    |             normalized vendor_names subset would mismatch the raw
    |             Sighting.vendor_name column and silently produce zero rows.
    |
    NOTE: Sightings page MPN chips link to material card detail pages
          when a MaterialCard exists. The sightings router
          (sightings_list, sightings_detail) builds a link_map dict
          (MPN string → MaterialCard.id) by querying MaterialCard with
          normalize_mpn_key(). This is passed to the template context
          and consumed by the shared _mpn_chips.html macro via its
          link_map parameter. All MPNs render as equal inline pills
          with an overflow toggle; clicking a chip opens the material
          card modal.
    |
    NOTE: Sightings filter shows all non-archived/cancelled requisitions
          (not just active status), so sightings from completed or other
          in-progress requisitions remain visible.
    |
    +---> sse_broker.py --> SSE push to browser ("search complete")
    |
    +---> connector_status.py --> DB: UPDATE api_sources
```

### 2b. Streaming Part-Search (`/v2/partials/search/run`)

```
Browser POST /v2/partials/search/run  (manual MPN entry)
    |
    v
htmx_views.py: search_run()
    |
    +---> Returns HTML shell + spinner immediately (200 OK)
    |
    +---> _safe_bg(stream_search_mpn(search_id, mpn))   # fire-and-forget asyncio.Task
              |
              v
    search_service.stream_search_mpn(search_id, mpn)
        |
        +---> db = SessionLocal()      # OWNS its own session — must NOT
        |                                receive a request-scoped session: web
        |                                framework finalizers close those as soon
        |                                as the response is sent, so a request
        |                                session would be dead before the worker's
        |                                first db.query(...). The fire-and-forget
        |                                wrapper swallows exceptions, so the
        |                                failure would surface only as a hung SSE
        |                                stream. Same pattern as _enrich_cards.
        |
        +---> connectors = _build_connectors(db)         # one-shot setup query
        |     vendor_score_map = db.query(VendorCard...) # one-shot setup query
        |
        +---> for each connector: asyncio.create_task(connector.search(mpn))
        |     loop with asyncio.wait(FIRST_COMPLETED):
        |       publish "source-status" / "results" / "card-update" per connector
        |     publish terminal "done" once all settle
        |
        +---> Always publishes a terminal "done" — including on uncaught
              exceptions (pool exhaustion, broker outage, render errors). The
              SSE client only knows the stream is complete via the done event,
              so worker death without done means a hung browser spinner.
```

Browser opens `GET /v2/partials/search/stream?search_id=...` (SSE long-poll) in
parallel to the POST so it receives events as the worker publishes them.

### Connector Failure Contract

External-API connectors (`app/connectors/*.py`) follow a single contract
for upstream failures: **auth, quota, and rate-limit conditions raise
typed `ConnectorError` subclasses; do not silently return `[]`**. The
exception propagates through `BaseConnector.search()` to the caller
(search orchestrator or `health_monitor.ping_source`).

```
connector._do_search(part_number)
    |
    +-- 200 OK         ----> parse + return list[dict]
    +-- 400 (bad input) --> log + return []   (input-domain error, not contract)
    +-- 401/403 (auth)  --> raise ConnectorAuthError
    +-- 429 (rate)      --> raise ConnectorRateLimitError
    +-- explicit quota  --> raise ConnectorQuotaError
    +-- 5xx             --> raise (httpx.HTTPStatusError via raise_for_status)
```

The `BaseConnector.search` wrapper:
- Re-raises `ConnectorError` immediately without retry (hard failures
  are not transient; retrying just burns more upstream calls).
- Raises `ConnectorError` on open circuit breaker (was: silently `[]`,
  which masked the contract — health_monitor saw success and flipped
  status back to live).
- Raises `ConnectorRateLimitError` on httpx 429 retries exhausted.

`health_monitor.ping_source` catches each subtype and writes a
type-specific `last_error` message:

| Exception | last_error prefix | Operator action |
|---|---|---|
| `ConnectorAuthError` | "Auth error — rotate credentials: ..." | Rotate API key in Admin > API Sources |
| `ConnectorRateLimitError` | "Rate limited — auto-recovers when window expires: ..." | Usually none |
| `ConnectorQuotaError` | "Quota exhausted — upgrade plan or wait for cycle: ..." | Upgrade plan or wait |

In all cases `api_sources.status` flips to `'error'`, and
`search_service._build_connectors` excludes the source from the next
user search with a `source_stats[i].status = 'error_skipped'` chip.
`stream_search_mpn` publishes a `source-status` SSE event for every
non-ok source at search start so the chip strip renders the right
state immediately.

**Auto-recovery.** The 15-min ping loop continues to ping all
`is_active=True` sources, including those at `status='error'`. On the
first ping that returns 200, status flips back to `'live'` and the
source rejoins user searches automatically. Persistent failures
(revoked key, exhausted quota) keep flipping back to `'error'` on each
ping, keeping the source excluded until the operator intervenes.

**No carve-outs.** All seven connectors (Mouser, BrokerBin, Nexar,
DigiKey, Element14, OEMSecrets, Sourcengine) follow this contract
uniformly. The Mouser HTTP-403/429 silent-empty path that existed prior
to round-2 was the silent-failure mode the contract is designed to
eliminate; it has been removed.

**Test enforcement** lives in `tests/test_connectors.py`,
`tests/test_connector_rate_limits.py`,
`tests/test_sourcengine_connector.py`, `tests/test_connector_errors.py`,
`tests/test_constants.py`, `tests/test_search_streaming.py`, and
`tests/test_health_monitor.py`.

### Browser-worker carve-out

`icsource` and `netcomponents` are queue-driven via `avail-ics-worker` /
`avail-nc-worker` rather than request/response connectors. They have no
entry in `_get_connector_for_source`, so the 15-min ping loop would flip
them to DISABLED on every run. `app.constants.BROWSER_WORKER_SOURCES`
holds this set, and `run_health_checks` excludes those names from the
ping loop. Their `api_sources` row is seeded to `LIVE` + `is_active=True`
once at startup by `seed_browser_worker_sources` (see `app/startup.py`)
and the seed survives because the ping loop never touches them. Their
actual health is tracked via `IcsWorkerStatus` / `NcWorkerStatus`
heartbeats; both singletons are seeded at startup so
`update_worker_status()` writes are not silently dropped.

### Removed (2026-05-14)

- Daily 3 AM `_job_refresh_stale_requisitions` cron — no background refresh
- Requirement-creation auto-enqueue (ICS + NC + background full-connector search)
- Legacy `POST /api/requirements/{id}/search` and
  `POST /api/requisitions/{id}/search-all` routes
- Row-click POST `/refresh` (row click is read-only `GET /detail` only)

### 2c. Sightings detail — Offers tab (part-centric)

The sightings detail pane (`GET /v2/partials/sightings/{requirement_id}/detail`)
has three tabs: **Vendors · Offers · Activity**. The Offers tab is
**part-centric** — it shows every Offer for the part number, not just the open
requirement.

```
sightings_detail (router)
  +-- part_offers_for(requirement, db)   [app/services/part_offers.py]
  |     +-- MPN set = primary_mpn + substitute MPNs (parse_substitute_mpns)
  |     +-- match Offer WHERE material_card_id IN {cards}
  |         OR normalized_mpn IN {both normalize_mpn_key + normalize_mpn forms}
  |     +-- returns offers across ALL requisitions, newest first
  +-- renders offers_panel.html into #sightings-offers-panel
        +-- _offer_row.html per offer (vendor, price/qty/lead, status pill,
            "↳ customer · Req #" source hint, kebab actions)

Pending-review offers render here (Approve/Reject) — moved out of the Vendors
panel so offers have a single home.

Offer actions (all on the prefix-less sightings router) call the canonical
crm.offers functions directly (no logic duplication) and re-render the panel:
  GET  .../offer-form            modal, blank (Enter) or prefilled (Convert)
  POST .../offers                -> create_offer(...)        [Convert / Enter]
  GET  .../offers/{id}/edit-form modal prefilled from the offer
  POST .../offers/{id}           -> update_offer(...)
  POST .../offers/{id}/review    -> approve_offer / reject_offer
  POST .../offers/{id}/reconfirm -> reconfirm_offer
  POST .../offers/{id}/mark-sold -> mark_offer_sold
  DELETE .../offers/{id}         -> delete_offer

"Convert to offer" sits on the collapsed vendor row (next to Send RFQ / Mark
Unavail) and opens the modal prefilled from the VendorSightingSummary. The modal
and the requisitions add-offer form share one field grid
(offers/_offer_form_fields.html). Offer creation logs OFFER_CREATED, so converted/
entered offers appear in the Activity tab automatically.

NOTE: the two creation paths historically wrote Offer.normalized_mpn differently
(create_offer = normalize_mpn_key, add_offer = normalize_mpn); add_offer was
fixed to use normalize_mpn_key, and the part query matches both forms for safety.

## 3. RFQ Email Sending

```
Browser POST (send RFQ)
    |
    v
requirements.py (router)
    |
    v
email_service.py
    |
    +---> graph_client.py --> Microsoft Graph API
    |       +---> POST /me/sendMail (subject tagged [AVAIL-{req_id}])
    |       +---> Returns graph_message_id
    |
    +---> DB: INSERT contacts (type=rfq, status=sent, graph_message_id)
    |
    +---> activity_service.py --> DB: INSERT activity_log (outbound email)
    |
    +---> vendor_card update: total_outreach++, last_contact_at
    |
    +---> teams_notifications.py --> Microsoft Teams webhook
```

## 4. Inbox Monitoring & Response Parsing (Background Job)

```
APScheduler (every 30 min)
    |
    v
email_jobs.py -> inbox_monitor()
    |
    v
email_service.py
    |
    +---> graph_client.py --> Graph API: GET inbox messages
    |       Filter: subject contains [AVAIL-*]
    |
    +---> Match reply to original contact via graph_conversation_id
    |
    +---> DB: INSERT vendor_responses (raw email)
    |       +---> activity_service.py: log_email_activity() --> DB: INSERT activity_log
    |             (event_type='email', direction='inbound', activity_type='email_received';
    |              dedups on external_id=message_id) so inbound vendor replies
    |              appear on the requisition Activity tab
    |
    v
ai_email_parser.py
    |
    +---> claude_client.py --> Anthropic API
    |       Extract: price, qty, lead_time, date_code, condition
    |       Returns: confidence 0.0-1.0
    |
    +---> signature_parser.py --> DB: UPSERT email_signature_extracts
    |
    +---> DB: UPDATE vendor_responses (parsed_data, classification)
    |
    v
ai_offer_service.py (if confidence >= 0.8)
    |
    +---> DB: INSERT offers (source='email_parsed')
    +---> material_card_service.py --> DB: UPSERT material_cards
    +---> vendor_card update: total_responses++, avg_response_hours
    +---> activity_service.py --> DB: INSERT activity_log (inbound email)
    +---> sse_broker.py --> SSE push ("new offer parsed")
```

## 5. Quote Building

```
Browser (select offers -> build quote)
    |
    v
crm/quotes.py + quote_builder_service.py
    |
    +---> DB: SELECT offers WHERE selected_for_quote=true
    +---> DB: INSERT quotes (auto-generate quote_number)
    +---> DB: INSERT quote_lines (from offers, with margin calc)
    +---> document_service.py --> Jinja2 render --> PDF
    +---> email_service.py (on send) --> Graph API /me/sendMail
    +---> activity_service.py --> DB: INSERT activity_log
```

## 6. Buy Plan Workflow

```
Quote accepted (status='won')
    |
    v
buyplan_builder.py
    |
    +---> DB: INSERT buy_plans_v3 (DRAFT, linked to quote + requisition)
    +---> DB: INSERT buy_plan_lines (buyer assigned via ownership_service)
    +---> buyplan_scoring.py (ai_score per line)
    |
    v
buyplan_workflow.py (state machine)
    |
    |  DRAFT --> SUBMITTED --> APPROVED --> PO_SENT --> COMPLETE
    |                |              |
    |                v              v
    |            REJECTED        HALTED
    |
    +---> buyplan_notifications.py
    |       +---> teams_notifications.py --> Teams webhook
    |       +---> DB: INSERT notifications
    |       +---> email (approval token link)
    |
    +---> activity_service.py --> DB: INSERT activity_log
```

## 7. Proactive Matching

```
APScheduler (daily) OR user trigger
    |
    v
proactive_matching.py
    |
    +---> DB: SELECT offers WHERE status='active' AND recent
    +---> DB: SELECT customer_part_history (who bought this MPN?)
    +---> DB: CHECK proactive_throttle (not offered recently?)
    +---> DB: CHECK proactive_do_not_offer (not blacklisted?)
    +---> Score: match_score = f(purchase_count, recency, margin)
    +---> DB: INSERT proactive_matches
    +---> sse_broker.py --> SSE push

User sends:
    +---> proactive_email.py --> Graph API /me/sendMail
    +---> DB: INSERT proactive_offers
    +---> DB: INSERT proactive_throttle
    +---> activity_service.py --> DB: INSERT activity_log
```

## 8. Activity Digest (AI Timeline Summary)

```
Browser (Activity tab loads) — lazy HTMX placeholder fires GET
    |
    +---> GET /v2/partials/requisitions/{req_id}/activity-digest?force=0
    |     GET /v2/partials/customers/{company_id}/activity-digest?force=0
    |
    v
htmx_views.py: requisition_activity_digest() / customer_activity_digest()
    |
    v
activity_digest_service.get_or_build_digest(entity_type, entity_id, db, force)
    |
    +---> Cooldown guard: if existing row and cooldown_until > now (and not force)
    |       -> return cached digest immediately
    |
    +---> Load up to 30 meaningful activities via
    |     get_requisition_activities(meaningful_only=True) or get_company_activities()
    |
    +---> Insufficient guard: < 2 activities -> return {state: "insufficient"}
    |
    +---> Basis freshness check: if existing and (basis_last_activity_at, basis_activity_count)
    |     unchanged and not force -> return cached digest
    |
    +---> Redis nx-lock (key: lock:digest:{type}:{id}, ex=30s):
    |       lock miss -> serve stale if exists, else return {state: "generating"}
    |
    +---> claude_structured(model_tier="smart"/Sonnet, DIGEST_SCHEMA, max_tokens=700)
    |       +---> headline, narrative, highlights[{label,value}], next_step, status_signal
    |
    +---> DB: UPSERT activity_digest (one row per entity, unique on entity_type+entity_id)
    +---> Set cooldown_until = now + digest_cooldown_seconds (default 120s)
    |
    v
Rendered via shared/activity_digest_card.html (states: ready/insufficient/generating/error)
```

Self-invalidating: the service regens automatically when `basis_last_activity_at` or
`basis_activity_count` changes on next view — no write-path hooks needed.
`?force=1` bypasses both the cooldown and the basis freshness check.

---

## 9. Inbox Observability

```
GET /v2/partials/requisitions (list page load)
GET /v2/partials/settings/profile (Settings → Profile tab)
    |
    v
activity_service.get_inbox_sync_status(user)
    |
    +---> Reads existing User fields:
    |       m365_connected, last_inbox_scan, access_token, token_expires_at
    |
    +---> Derives health:
    |       ERROR  — m365_connected=False OR token expired/missing
    |       WARNING — last_inbox_scan > 2× inbox_scan_interval_min ago
    |       OK     — connected, token valid, scan recent
    |
    +---> Returns: {connected, last_scan_at, is_stale, token_ok, error_reason, health}
    |
    v
Two surfaces:
    1. Requisitions list: shared/inbox_disconnected_banner.html
       (shown when health=error or is_stale=True; included at top of list.html)
    2. Settings → Profile: settings/_mailbox_sync_card.html
       (always shows sync status + "Scan now" button)

"Scan now" button:
    POST /v2/partials/settings/inbox/scan-now
        |
        v
    htmx_views.settings_scan_now()
        |
        +---> _run_inbox_scan_now(user, db)  [TESTING=1 skips, else 12s timeout]
        |       +---> email_jobs._scan_user_inbox(user, db)
        |               +---> Graph API inbox poll -> parse -> activity_log
        |                     (same path as scheduled _job_inbox_scan)
        |
        +---> db.refresh(user)
        +---> get_inbox_sync_status(user) -> re-render _mailbox_sync_card.html
```

`poll_inbox_htmx` (`POST /v2/partials/requisitions/{req_id}/poll-inbox`) also calls
`_run_inbox_scan_now` and returns the refreshed responses tab; the scan is user-scoped,
not requisition-scoped.

---

## Enrichment Pipeline

```
Trigger: user click OR background job OR bulk import
    |
    v
enrichment_service.py (orchestrator)
    |
    +---> Phase 1a: Free enrichment
    |       +---> prospect_free_enrichment.py (web search)
    |       +---> signature_parser.py (from email_signature_extracts)
    |
    +---> Phase 1b: API enrichment
    |       +---> apollo.py --> Apollo.io API
    |       +---> prospect_discovery_explorium.py --> Explorium API
    |
    +---> Phase 2: AI analysis
    |       +---> ai_service.py --> Claude API (company intel, ICP fit)
    |
    +---> DB: UPSERT companies (domain, size, location, enrichment_source)
    +---> DB: UPSERT vendor_cards (domain, industry)
    +---> DB: INSERT enrichment_queue (proposed changes for review)
    +---> DB: INSERT enrichment_jobs (batch tracking)
```

---

## Tagging & Classification Pipeline

```
Trigger: new material_card OR hourly job
    |
    v
tagging_ai.py (orchestrator)
    |
    +---> tagging_ai_triage.py (needs classification?)
    |
    +---> prefix_lookup.py (fast: MPN prefix -> known brand/commodity)
    |       +---> commodity_registry.py
    |
    +---> tagging_ai_classify.py (slow: Claude AI)
    |       +---> claude_client.py --> Anthropic API
    |
    +---> DB: INSERT material_tags (material_card_id, tag_id, confidence)
    +---> DB: UPSERT entity_tags (propagate to vendor_cards, companies)
    +---> DB: UPDATE vendor_cards.brand_tags, companies.commodity_tags
```

## Material Enrichment Pipeline

```
Trigger: every 2h job (_job_material_enrichment) OR Enrich button on material detail
    |
    v
tagging_jobs.py -> enrich_pending_cards() [scheduled, first pass]
  OR
materials router -> enrich_material_cards() [user-triggered, batch processing]
    |
    +---> Batch processing with zip length validation
    |       (ensures MPN list and card list stay in sync)
    |
    v
authoritative_enrichment_service.enrich_card()  — ENRICHMENT TIER SEQUENCE
    |
    +---> Tier 1: distributor connector fanout (fetch_authoritative → merge_authoritative)
    |       HIT  → status=verified; apply_authoritative() writes description/specs/lifecycle.
    |       MISS → fall through.
    |
    +---> Tier 2: distributor/manufacturer web search (extract_part_from_web, web_meter +1)
    |       HIT (web_sourced)  → apply_web_sourced(); done.
    |       MISS → fall through.
    |
    +---> OEM gate: classify_oem_vendor(display_mpn) — pure regex, no web call.
    |       Non-OEM parts skip Tiers 3-4 entirely.
    |
    +---> Tier 3 (OEM only): cross-reference MPN (cross_reference_mpn, web_meter +1)
    |       Grounded Claude web search; four Python gates:
    |         (1) ≥1 source URL on is_crossref_domain allowlist
    |         (2) both OEM code and resolved MPN appear verbatim in the sourced linkage_quote
    |         (3) resolved_mpn != original (no echo)
    |         (4) confidence ≥ 0.90
    |       RESOLVED → fetch_authoritative(resolved_mpn) double-verify against distributors
    |         CONFIRMED → apply_cross_ref_verified(): writes distributor data onto card,
    |                     records FRU→MPN linkage in cross_references JSONB +
    |                     cross_ref provenance block; status=verified.
    |         UNCONFIRMED → discard candidate, fall through.
    |       FAILED → fall through.
    |
    +---> Tier 4 (OEM only): OEM-official description (extract_oem_description, web_meter +1)
    |       Grounded Claude web search on OEM's own page; four Python gates:
    |         (1) ≥1 source URL on is_oem_domain allowlist (stricter than cross-ref)
    |         (2) exact_mpn_found matches normalized_mpn verbatim
    |         (3) confidence ≥ 0.90
    |         (4) description ≥ 10 chars + manufacturer present
    |       HIT → apply_oem_sourced(): writes description/category/datasheet + oem_sourced
    |             provenance; status=oem_sourced.
    |       MISS → fall through.
    |
    +---> Tier 5: AI inference fallback (infer_part via ai_inference_fallback,
    |       web_meter: claude_ok=True)
    |       ai_inferred → writes description/category with reconfirm_needed flag.
    |       not_found → terminal.
    |
    +---> Terminal:
    |       OEM pattern matched AND OEM tiers ran → status=not_catalogued
    |         (recognised OEM/FRU part; no public specs; retries on 30-day backoff)
    |       Otherwise → status=not_found (22h backoff)

web_meter contract ({"web_calls": int, "claude_ok": bool}, updated in place):
    - web_calls: incremented by 1 for each billable web-search call
      (Tier 2 + Tier 3 + Tier 4 each count as 1; connector fanout is free).
    - claude_ok: set True after ANY Claude call returns without raising
      (Tier 2, 3, 4 or infer_part); the worker reads this to reset its
      circuit breaker. Default None = no metering (call still works).
    The enrichment worker passes a fresh meter per card and adds
    card_meter["web_calls"] to the rolling web_calls_today counter for
    the daily budget gate (ENRICHMENT_WEB_DAILY_CAP, env-configurable).

Claude Haiku (Anthropic API)  — FIRST PASS (legacy path — superseded by
    authoritative_enrichment_service for new cards; kept for bulk/batch jobs)
    |
    +---> Classify card: description, category, lifecycle_status
    +---> DB: UPDATE material_cards (description, category, lifecycle_status)
    +---> search_vector trigger auto-updates TSVECTOR with new description/category
    +---> Stamps material_cards.enriched_at

After first pass (scheduled job only):
tagging_jobs.py -> enrich_pending_specs() [spec extraction, second pass]
  OR
enrichment_worker/worker.py::run_one_batch -> enrich_card_specs(<this batch's
    newly core-enriched card ids>)  [paced, once per batch, same session + commit;
    only verified/web_sourced/ai_inferred cards — never not_found]
  OR
POST /v2/partials/materials/{id}/enrich (Enrich button) -> enrich_card_specs([id], force=True)
  OR
python -m app.management.enrich_specs --limit N  (one-time/on-demand backfill)
    |
    v
spec_enrichment_service.py  — SECOND PASS
    |
    +---> Per-commodity structured-spec extraction via claude_structured (model_tier="smart")
    |       +---> COMMODITY_SPECS schema drives prompt (per category: key, label, type, values)
    |       +---> Records facets at confidence >= 0.85 (FACET_MIN_CONF; higher than the
    |             free-text summary bar because a wrong spec value silently mis-filters a part)
    |
    +---> spec_write_service.record_spec()
    |       +---> DB: UPDATE material_cards.specs_structured (JSONB — keyed parametric values)
    |       +---> DB: UPSERT material_spec_facets (one row per spec facet per card)
    |       +---> DB: UPDATE material_cards.specs_summary (plain-text key-spec summary)
    |
    +---> DB: UPDATE material_cards.specs_enriched_at = now()
    |       (idempotent gate: NULL cards are processed; non-NULL cards are skipped
    |        unless force=True, e.g. from the Enrich button)

Startup backfill:
    _backfill_material_cards() in startup.py
    +---> Runs at application boot
    +---> Ensures every MPN in requirements has a material card
    +---> Idempotent: skips MPNs that already have cards

Env: MATERIAL_ENRICHMENT_ENABLED=true
```

## Cross-Reference Caching

```
GET /find_crosses (material detail page)
    |
    v
find_crosses() endpoint
    |
    +---> DB: SELECT material_cards.cross_references
    |       |
    |       +--- non-empty AND refresh!=True -> return cached cross_references (skip AI)
    |       +--- empty OR refresh=True -> call AI for cross-references
    |               +---> Claude API -> discover alternative MPNs
    |               +---> DB: UPDATE material_cards.cross_references (cache result)
    |
    +---> Return cross-references to template
```

---

## Faceted Search (Full-Text Search)

```
Materials workspace search input
    |
    v
faceted_search_service.py
    |
    +---> Multi-word queries:
    |       +---> plainto_tsquery(query) on material_cards.search_vector (PostgreSQL FTS)
    |       +---> ts_rank() for relevance ordering
    |       +---> pg_trgm fuzzy match on display_mpn (typo-tolerant via ix_material_cards_trgm_mpn)
    |
    +---> Single-token queries (likely MPN prefixes):
    |       +---> ILIKE fallback for substring match on MPN fields
    |
    +---> Facet filters: category, manufacturer, lifecycle_status, commodity specs
    +---> Returns ranked, filtered material cards

NOTE: Uses tsvector + trgm for multi-word queries, ILIKE fallback for
      single tokens. GIN-indexed TSVECTOR with weighted fields
      (MPN=A, manufacturer=B, description/category=C).

Sidebar facets (workspace.html + materialsFilter Alpine component):
    +---> Data confidence (trust ladder): 5 ordered, color-coded enrichment
    |     tiers (verified > web_sourced > ai_inferred > not_found > unenriched).
    |     Multi-select; default selection = verified + web_sourced. Alpine state
    |     `statuses[]` → `?statuses=` CSV → search_materials_faceted(statuses=...)
    |     which IN-filters MaterialCard.enrichment_status. `statuses` takes
    |     precedence over the legacy `verified_only` boolean (never ANDed).
    +---> Global facets (MaterialCard columns, OR-within each, AND across):
    |       +---> lifecycle  → lifecycle_status IN (active|nrfnd|eol|obsolete|ltb)
    |       +---> rohs        → rohs_status IN (compliant|non-compliant|exempt)
    |       +---> has_datasheet (boolean) → datasheet_url IS NOT NULL
    |     Counts come from get_global_facet_counts(); rendered by
    |     /v2/partials/materials/filters/global (reloads on commodity-changed).
    +---> Manufacturer facet → /v2/partials/materials/filters/manufacturers
    +---> Commodity sub-filters → /v2/partials/materials/filters/sub (spec facets)

Search coverage:
    +---> global_search_service.py includes substitutes_text.ilike for
    |     cross-MPN matching in global search
    +---> requisition_list_service.py includes substitutes_text.ilike
          for parts list filtering
```

---

## Scoring System Hierarchy

```
unified_score_service.py (top-level, monthly)
    |
    +---> avail_score_service.py (behavior + outcomes)
    |       +---> engagement_scorer.py
    |       +---> activity_quality_service.py
    |       +---> response_analytics.py
    |
    +---> multiplier_score_service.py (points system)
    |       +---> buyer_leaderboard_snapshot
    |
    +---> vendor_scorecard.py (per-vendor)
            +---> vendor_score.py (composite)
            |       +---> response_rate, on_time_delivery
            |       +---> cancellation_rate, quote_conversion
            +---> vendor_metrics_snapshot (DB)

SIGHTING SCORING (per search result):
    scoring.py
        +---> price competitiveness
        +---> quantity match
        +---> freshness (recency)
        +---> authorized distributor bonus
        +---> source confidence
        +---> vendor reliability (from vendor_score)

LEAD SCORING (per sourcing lead):
    sourcing_score.py
        +---> freshness_score
        +---> source_reliability_score
        +---> contactability_score
        +---> historical_success_score
        +---> vendor_safety_score --> prospect_signals.py

PROSPECT SCORING:
    prospect_scoring.py
        +---> fit_score (ICP match)
        +---> readiness_score (buying signals)
        +---> prospect_priority.py (rank order)
```

---

## Cache Interaction Pattern

```
Route Handler
    |
    +---> @cached_endpoint(prefix="key", ttl_hours=24)
    |       |
    |       v
    |   cache/decorators.py
    |       +---> Redis GET (if REDIS_URL set)
    |       |       HIT -> return cached
    |       |       MISS v
    |       +---> (fallback) intel_cache.py -> DB: SELECT intel_cache
    |       |       HIT -> return cached
    |       |       MISS v
    |       v
    |   Execute handler -> Redis SET (or DB INSERT) with TTL
    |
    |   INVALIDATION (on mutations):
    |       +---> cache.invalidate(prefix="key") -> Redis DEL
```

---

## Background Job Interactions

```
APScheduler (scheduler.py)
    |
    +---> core_jobs.py (5 min)
    |       +---> Renew Graph webhooks, health checks, connector status
    |
    +---> email_jobs.py (30 min)
    |       +---> Graph API inbox poll -> Claude parse -> offers -> SSE
    |
    +---> offers_jobs.py (daily)
    |       +---> proactive_matching -> Teams + email notifications
    |
    +---> inventory_jobs.py (4 hours)
    |       +---> Refresh material_cards, price snapshots
    |
    +---> tagging_jobs.py (2 hours)
    |       +---> Claude batch classify -> material_tags, entity_tags
    |       +---> _job_material_enrichment -> enrich_pending_cards() (Claude Haiku, first pass)
    |       +---> _job_material_enrichment -> enrich_pending_specs() (Claude Sonnet, second pass)
    |
    +---> sourcing_refresh_jobs.py (4 hours)
    |       +---> Re-search stale requirements through all connectors
    |
    +---> maintenance_jobs.py (daily)
    |       +---> DB ANALYZE, cache cleanup, integrity checks
    |
    +---> quality_jobs.py (daily)
    |       +---> Vendor scorecards, engagement scoring, contact quality
    |
    +---> task_jobs.py (2 hours)
    |       +---> Overdue task notifications
    |
    +---> prospecting_jobs.py (daily)
    |       +---> Explorium discovery, web search for contacts
    |
    +---> eight_by_eight_jobs.py (30 min, gated by EIGHT_BY_EIGHT_ENABLED)
    |       +---> 8x8 CDR poll -> log_call_activity() -> activity_log
    |             (canonical activity_type='call_logged'; in/out on the
    |             direction column; reverse-matched calls link to an open req)
    |
    +---> health_jobs.py (5 min)
            +---> Ping each connector -> update api_sources
```

---

## Frontend <-> Backend Pattern

```
BROWSER (HTMX + Alpine.js)

  0. Bottom navigation (mobile_nav.html):
     Reqs | Sightings | Materials | Search | ...
     "Materials" tab links to /v2/materials, loads /v2/partials/materials/workspace

  1. Page load:
     base_page.html -> hx-get="/v2/partials/X" on load
         -> Server renders partial -> swaps #main-content

  2. Tab click:
     <button hx-get="/v2/partials/req/{id}/tab/offers">
         -> Server renders tab partial -> swaps #tab-content

  3. Form submit:
     <form hx-post="/v2/partials/req/create">
         -> Server processes -> returns updated list partial
         -> HX-Trigger: {"showToast": "Created!"}
         -> Alpine catches -> $store.toast.show = true

  4. Inline edit:
     Click cell -> hx-get="/.../edit/{field}" (swap form in)
     Submit -> hx-patch="/.../inline" (swap display back)

  5. Search:
     <input hx-get hx-trigger="keyup changed delay:500ms">
         -> Debounced typeahead -> swaps results

  6. Real-time:
     <div hx-ext="sse" sse-connect="/api/events/stream">
       <div sse-swap="offer_parsed">
         -> Server pushes event -> HTMX swaps element

  7. Modal:
     Button -> Alpine @click="$dispatch('open-modal', {url, wide})"
     Modal  -> base.html chrome htmx.ajax()'s url into #modal-content
     Close  -> base.html chrome renders ONE persistent X (dispatches
               close-modal); also Escape + backdrop click. Content templates
               loaded into #modal-content do NOT need their own close control,
               and must not add a top-right X (it would double up with chrome).
     MPN chip click -> open-modal with material card detail URL

  8. Loading:
     <button data-loading-disable hx-indicator="#spinner">
         -> HTMX adds .htmx-request class during flight

  9. Editable descriptions (parts header):
     AI-generated description displayed -> click to edit
     -> hx-patch inline update -> swap display back

  10. Shared components:
      _mpn_chips.html: equal inline pills for all MPNs (primary + subs)
          with overflow toggle and modal material card click
      status_badge macro (_macros.html): unified badge rendering
          used across all pages (requisitions, sightings, parts, etc.)
```

---

## HTMX Click-to-Refresh Pattern (Sightings)

The sightings page uses a structural HTMX pattern that avoids the
GET-then-POST race conditions and SSE re-trigger loops the codebase used
to suffer from. It's the canonical pattern for any panel where a
user-initiated swap and a background SSE-driven refresh can both target
the same DOM region.

**Authoritative do/don't rules live in `docs/htmx-conventions.md`** — this
section documents the data flow only, not the rules.

### Click-to-Refresh Data Flow (end to end)

```
USER clicks a sightings row
    |
    v
Alpine selectReq() in partials/sightings/list.html
    |
    +---> $store.sightingSelection.selectedReqId = <id>
    +---> $store.sightingSelection.clickPending += 1
    |     -- single in-flight slot for the GET below; SSE handler stays
    |        suppressed until it decrements back to 0.
    |
    +---> htmx.ajax('GET', '/v2/partials/sightings/<id>/detail', {
    |         target: '#sightings-detail',
    |         swap:   'innerHTML',
    |         indicator: '#sightings-detail-skeleton',
    |         headers: { 'X-Click-Req-Id': '<id>' }
    |     })
    |     -- Row click is READ-ONLY. No connector calls. Paints the
    |        cached panel from VendorSightingSummary in ~100ms.
    |        Fresh searches happen only when the user explicitly clicks
    |        the per-row refresh icon or the detail-panel "Search" button
    |        (both POST /refresh, gated by 48h per-MPN cooldown).
    |
    v
SERVER: sightings_detail(...) [app/routers/sightings.py]
    |
    +---> Reads cached VendorSightingSummary rows; renders detail partial.
    +---> resp.headers["X-Rendered-Req-Id"] = str(requirement_id).
    +---> Does NOT call search_requirement (pinned by tests).
    |
    v
USER clicks per-row refresh icon OR detail-panel "Search" button
    |
    v
SERVER: sightings_refresh(source="user", ...) [app/routers/sightings.py]
    |
    +---> search_requirement() runs connector fan-out for MPNs that pass
    |       the 48h per-MPN cooldown (skipped MPNs surface prior sightings
    |       via material_card_id linkage instead)
    |
    +---> renders sightings_detail() partial (HTMX response body)
    |       +---> resp.headers["X-Rendered-Req-Id"] = str(requirement_id)
    |               (set unconditionally in sightings_detail; sightings_refresh
    |                inherits via `await sightings_detail(...)`)
    |
    +---> (source="user" only) await _publish_if_user_source(...)
    |       +---> sse_broker.publish("sighting-updated", {requirement_id})
    |               on the user's channel
    |
    +---> (source="user" only) HX-Trigger per-MPN toast summarizing
    |     {searched, cached} counts. SSE-source POSTs suppress toasts.
    |
    v
CLIENT: htmx:beforeSwap listener in app/static/htmx_app.js
    |
    +---> Reads X-Rendered-Req-Id from response.
    +---> Compares to $store.sightingSelection.selectedReqId.
    +---> If they differ, evt.preventDefault() — drop the stale swap.
    |     (User clicked a different row mid-flight.)
    |
    v
CLIENT: htmx:afterRequest listener in app/static/htmx_app.js
    |
    +---> store.clickPending = Math.max(0, store.clickPending - 1)
    |     -- decrements on EVERY outcome (swap, error, abort, stale-reject)
    |     so the counter never gets stuck.
    |
    v
SSE channel for the user fires "sighting-updated" event
    |
    v
SSE listener in partials/sightings/list.html
    |
    +---> if ($store.sightingSelection.clickPending > 0) return;
    |     -- user has an in-flight click; skip background refresh to avoid
    |        target collisions.
    |
    +---> if (eventReqId !== $store.sightingSelection.selectedReqId) return;
    |     -- only refresh the currently-displayed requirement.
    |
    +---> htmx.ajax('POST', '/v2/partials/sightings/refresh?source=sse', {
    |         target: '#sightings-detail',
    |         swap:   'innerHTML',
    |         indicator: '#sightings-detail-skeleton',
    |         values: { requirement_id: <id> }
    |     })
    |
    v
SERVER: sightings_refresh(source="sse", ...)
    |
    +---> Runs the connector fan-out and re-renders detail.
    +---> Skips broker.publish (loop break — see do-not rule in
    |     docs/htmx-conventions.md).
    +---> Skips HX-Trigger toasts (background-triggered toasts are not
    |     surfaced to the user).
    +---> Still sets X-Rendered-Req-Id; client guard still runs.
```

### X-Rendered-Req-Id Correlation Header

**Why it exists.** Out-of-order swap protection. If the user clicks rows
A then B in quick succession, both POSTs are in flight simultaneously and
either response can arrive first. Without correlation, the A response can
clobber the B response that already swapped — leaving the wrong detail
panel for row B.

**Server side.** Set on every response from any endpoint that renders
into `#sightings-detail`. Endpoints today:
- `sightings_detail()` — the canonical setter; inherited by
  `sightings_refresh()` via `await sightings_detail(...)`.
- `sightings_log_activity()` — sets the header on its rendered detail
  response too.

**Client side.** `htmx:beforeSwap` listener in `app/static/htmx_app.js`
reads the header off the XHR, compares to
`Alpine.store('sightingSelection').selectedReqId`, and calls
`evt.preventDefault()` if they differ.

### `?source=user|sse` Query-Param Convention

**Why it exists.** Break the SSE → broker.publish → SSE → endpoint loop
that occurs when a refresh handler both consumes and re-emits the same
event. Also: gate user-facing toasts so background-triggered refreshes
stay silent.

**Type contract.** `source: Literal["user", "sse"] = Query(default="user")`.
The `Literal` type is load-bearing — a plain `str` would silently fall
back to the user-path branch on typos like `?source=SSE` and re-introduce
the loop. `Literal` makes FastAPI return HTTP 422 on unknown values.

**Endpoints that use the gate.** All mutation endpoints under
`app/routers/sightings.py` whose response can land in `#sightings-detail`
or whose state change should propagate via SSE:
- `sightings_refresh`
- `sightings_batch_refresh`
- `sightings_mark_unavailable`
- `sightings_assign_buyer`
- `sightings_advance_status`
- `sightings_log_activity`

**Shared helpers** (`app/routers/sightings.py`):
- `_publish_if_user_source(source, user_id, requirement_id)` — calls
  `broker.publish` only when `source != "sse"`. Used at the point where a
  handler would otherwise emit the looped SSE event.
- `_toast_suppressed_for_sse(source) -> bool` — guards `HX-Trigger`
  emission. Rate-guard toasts ("Already searched within X minutes") and
  refresh-failure toasts only fire when `source == "user"`.

### Static-Analysis Enforcement

The conventions are not just guidance — `tests/test_static_analysis.py`
walks the source tree and fails CI on regressions:
- `broker.publish` calls inside source-gated handlers must be guarded by
  `_publish_if_user_source` or an equivalent `if source != "sse"` check.
- `htmx.ajax()` call sites whose target is a content-sensitive panel
  must pass an `indicator:` option (HTMX does not read `hx-indicator`
  from the target element on imperative calls).
- Endpoints that render into `#sightings-detail` must set
  `X-Rendered-Req-Id`.

See `docs/htmx-conventions.md` for the do/don't rules and pointers into
the current implementation.

---

## Routes Summary (400+ endpoints)

| Domain | Routes | Key Operations |
|--------|--------|---------------|
| Auth | 7 | OAuth login/callback/logout, status |
| Requisitions | 45 | CRUD, search, bulk archive/assign, claim |
| Requirements | 23 | Add parts, CSV upload, search, leads, tasks |
| Vendors | 35 | CRUD, contacts, stock history, reviews, tags |
| Companies/CRM | 40 | CRUD, sites, contacts, enrichment, import |
| Offers | 30 | CRUD, line items, accept/reject, changelog |
| Quotes | 25 | CRUD, send, PDF, e-signature, pricing history |
| Buy Plans | 6 | CRUD, external approval via token |
| Materials | 20 | CRUD, substitutes, stock levels, price history |
| Sightings | 25 | CRUD, RFQ send, batch RFQ, inquiry |
| Excess | 30 | Lists, line items, bids, solicitations, import |
| AI | 18 | Parse email, normalize, find contacts, draft RFQ |
| Proactive | 12 | Matches, refresh, dismiss, send, scorecard |
| Prospects | 12 | Suggested prospects, claim, dismiss, enrich |
| Sources | 35 | Connector config, test, stocklist, webhooks |
| Tags | 4 | List, entity tags |
| Activity | 14 | Log calls, timeline, dashboards |
| Admin | 15 | Users, config, diagnostics, maintenance |
| Tickets | 12 | Error reports, trouble tickets, AI analysis |
| Documents | 2 | Requisition PDF, quote PDF |
| Quote Builder | 5 | Draft, save, send, signature |
| Events | 1 | SSE stream |
| HTMX Pages | 24 | Top-level page shells |
| HTMX Partials | ~100 | Tab content, forms, inline edits |

---

## Service Modules (118 total)

| Category | Count | Key Modules |
|----------|-------|-------------|
| AI & NLP | 9 | ai_service, ai_email_parser, ai_offer_service, tagging_ai |
| Search & Prospecting | 30+ | search_worker_base/, ics_worker/, nc_worker/, sourcing_leads |
| Email & Communication | 10 | email_threads, contact_intelligence, signature_parser |
| Scoring & Matching | 10+ | unified_score, avail_score, multiplier_score, proactive_matching |
| CRM & Data | 20+ | company_merge, vendor_merge, auto_dedup, enrichment |
| Vendor Mgmt | 8 | vendor_analysis, vendor_affinity, vendor_scorecard |
| Buy Plans | 6 | buyplan_builder, buyplan_workflow, buyplan_scoring |
| Materials | 7 | material_enrichment, spec_enrichment_service, materials_ai_search, faceted_search_service, excess_service |
| Admin & Health | 6 | health_monitor, integrity_service, audit_service |
| Misc | 14 | knowledge_service, document_service, sse_broker, webhook_service |

---

## Deploy Verification (deploy.sh)

```
deploy.sh
    |
    +---> Step 1: Commit & push
    +---> Step 2: Build with --no-cache (build tag = git commit SHA)
    +---> Step 3: Start with --force-recreate
    +---> Step 4: Health checks (wait for app to respond)
    +---> Step 5: Verify deployed build tag matches built commit
    +---> Step 6: Verify CSS coverage
    |       +---> Scan templates for Tailwind color classes
    |       +---> Grep CSS bundle for each class
    |       +---> Warn on any MISSING classes (safelist gap)
    +---> Step 7: Tail logs for errors
```

---

## Alpine.js Directives (htmx_app.js)

### x-truncate-tip  (htmx_app.js)

Hover tooltip that appears when the element overflows its box
(`scrollWidth > clientWidth`), OR when the element has a `_tipNodes`
DocumentFragment property attached at runtime. The `_tipNodes` path is
the contract with `x-chip-overflow` — hidden chips flow as cloned DOM,
never through HTML-string attributes. Re-entrant guard prevents
orphaned tooltips on rapid mouseenter events; Alpine `cleanup()`
callback removes any visible tip on element teardown.

### x-chip-overflow  (htmx_app.js)

Chip-row directive. ResizeObserver watches container inline-size; hides
chips that don't fit (left-to-right walk), exposes a trailing
`.opp-chip-more` button whose `_tipNodes` property holds a
DocumentFragment of cloned hidden chips for `x-truncate-tip` to reveal
on hover. Primaries-first DOM order (enforced by `_build_row_mpn_chips`
service helper) guarantees primary MPNs never hide while subs are
visible. Cleanup via Alpine's `cleanup()` disconnects the observer on
element teardown.

### rowActionRail  (htmx_app.js, Alpine.data)

Component bound to `/requisitions2` `<tr>`. CSS handles hover reveal via
`tr:hover .opp-action-rail`; this component exposes `show` state so
`@focusin`/`@focusout`/`@keydown.enter` toggle visibility for keyboard
users. `Escape` dismisses.

### rfqVendorModal  (htmx_app.js, Alpine.data)

Backs `sightings/vendor_modal.html` — the "Send RFQ" batch-inquiry modal opened
from a sighting's vendor row (`requirement_ids=<id>`) or the table action bar
(comma-joined ids). Invoked as
`x-data='rfqVendorModal({{ suggested_vendors|map(attribute="normalized_name")|list|tojson }}, {{ requirement_ids|tojson }})'`.
The factory lives in JS (not inline) because `|tojson` emits double quotes that
would close a double-quoted `x-data` attribute and break Alpine init; the data is
carried through a **single-quoted** attribute (tojson escapes `'`). State:
`step` (compose|preview), `selectedVendors` (a plain reactive object keyed by vendor
name — matches the `sightingSelection` store, not a Set; `selectedCount` getter +
`isSelected(name)` back the bindings), `emailBody`. Methods: `toggleVendor`;
`loadPreview()` → `POST /v2/partials/sightings/preview-inquiry` (htmx.ajax swap into
`x-ref="previewContent"`); `confirmSend()` → `fetch POST
/v2/partials/sightings/send-inquiry` with the `x-csrftoken` header, then on success
`_refreshSightings()` re-GETs the open `#sightings-detail` (status auto-advances
OPEN→SOURCING + new activity rows) and the `#sightings-table` list, and dispatches
`close-modal`. `_form()` builds a `FormData` with **repeated** `requirement_ids`/
`vendor_names` keys (not `Object.fromEntries`, which collapses duplicates).

The route returns **HTTP 200 even on partial/total send failure** (failures are
captured, not raised) and exposes the true outcome via `X-RFQ-Sent` / `X-RFQ-Total`
response headers. `confirmSend` reads them and toasts via `$store.toast`: full
success, partial-failure (warning), or total-failure (error — modal stays open to
retry); it never infers success from the HTTP status alone.

---

## 8x8 Integration — Operator Enablement

The 8x8 CDR (call-detail record) integration is **code-complete but
disabled by default**. The polling job runs only when
`eight_by_eight_enabled` is true in settings (driven by env vars).

**Required env vars** (set in deployment `.env`, then restart the api
container):

| Var | Purpose |
|---|---|
| `EIGHT_BY_EIGHT_ENABLED` | `true` to register the polling job |
| `EIGHT_BY_EIGHT_API_KEY` | 8x8 API token |
| `EIGHT_BY_EIGHT_USERNAME` | Service account username |
| `EIGHT_BY_EIGHT_PASSWORD` | Service account password |
| `EIGHT_BY_EIGHT_PBX_ID` | Tenant PBX identifier |

**Optional env vars** (defaults applied if unset):

| Var | Default | Purpose |
|---|---|---|
| `EIGHT_BY_EIGHT_TIMEZONE` | `America/Los_Angeles` | Tenant timezone for CDR timestamp parsing |
| `EIGHT_BY_EIGHT_POLL_INTERVAL_MINUTES` | `30` | How often the job pulls new CDRs |

**Per-user setup:** Each user that should have their calls ingested
needs their 8x8 extension stored on the `users` table (fields added
in alembic migration `052_add_8x8_user_fields.py`). Without this,
their calls land in `activity_log` but are not attributed to a user.

**Verifying enablement landed:** On api container start, `docker
compose logs api` will show exactly one of three lines from
`register_eight_by_eight_jobs`:

| Log line | Means |
|---|---|
| `8x8 CDR polling NOT registered (EIGHT_BY_EIGHT_ENABLED is false)` | Flag is off — set `EIGHT_BY_EIGHT_ENABLED=true`. |
| `8x8 CDR polling NOT registered — enabled flag is true but credentials missing: ...` | Flag is on but one or more secrets are unset. The line lists which ones. |
| `8x8 CDR polling registered (every 30min)` | Job is live; CDRs will pull on the next interval tick. |

If none of these appear in the logs, the api container did not finish
starting — check `docker compose ps` and earlier log lines.

**Data flow:** Job → `eight_by_eight_service` → CDR pull → matched to
users by extension → rows inserted into `activity_log` with
`source='8x8_call'`. Visible in the per-record activity timeline.
