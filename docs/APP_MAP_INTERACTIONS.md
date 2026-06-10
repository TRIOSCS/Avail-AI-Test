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
              +---> fru_matrix_service.get_fru_view / get_reverse_context(db, mpn)
              |        FRU-crosswalk context (capped/cheap reads, only for a
              |        concrete searched MPN — see "FRU crosswalk context" below).
              |        Own scoped try/except: a crosswalk failure logs and degrades
              |        to "no crosswalk card", never touching the loaded history.
              +---> renders history_panel.html (or empty state if no card)
```

`get_part_history` is the single source of truth for a part's history; the
materials detail router (`material_detail_partial`, `material_tab_partial`)
consumes the same `*_for_card` helpers, so the search panel and the full part
page can never drift. The endpoint is wrapped in try/except (logged via
Loguru) and degrades to an empty/error panel rather than failing the page.

**FRU crosswalk context.** When the searched MPN matches `fru_links` in either
direction, the panel appends a compact "FRU crosswalk" card (silent on no hit,
matching the materials-detail decision):

- **Forward hit** (the MPN is a FRU): one-line counts via `FruView.summary`
  ("N drive PNs · M models · K 11S numbers · J trays", kind-neutral — no
  qualification claim — falling back to "N linked parts"), plus up to 3
  manufacturer-model chips (`FruView.top_models`).
- **Reverse hit** (the MPN appears under FRUs): "Used in N FRUs" — N is the
  DISTINCT-FRU count (`ReverseContext.distinct_frus`, SQL aggregate; NOT the
  (FRU, role) usage count `ReverseView.total`) — plus up to 3 distinct FRUs in
  canonical (shortest, de-padded) spelling (`ReverseContext.top_frus`).
  `get_reverse_context` is a lightweight column-fetch read path; the search
  panel never hydrates full `FruLink` rows.
- A crosswalk-known part with no trading history renders "No trading history
  yet" instead of the "looks new to us" empty state.
- Both cases share a "View full FRU matrix →" deep link to the materials
  surface (`/v2/materials?q=<mpn>`, the same URL pattern the fru-lookup
  partial pushes). The faceted results (`materials_faceted_partial` →
  `list.html`) render the full `fru_section.html` above the card list whenever
  `q` hits `fru_links`, so the deep link delivers the matrix even for a
  crosswalk-only PN that matches no material card; the full matrix is never
  duplicated on the search page itself.

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
`update_worker_status()` writes are not silently dropped. Each worker
(ics, nc, and enrichment) refreshes `last_heartbeat` on **every** loop
tick via `_record_heartbeat()` at the top of the loop — so the heartbeat
reflects process liveness independent of work, and stays fresh on idle /
cap-sleep / breaker-open / off-hours paths (a liveness monitor reading
`last_heartbeat` won't false-alarm "DOWN" while a worker is merely paused).

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

Vendor rows (_vendor_row.html) also carry a row-level status treatment keyed off
the server-computed vendor status `vs` (precedence resolved in
app/services/sighting_status.py — offer-in dominates unavailable): unavailable
rows get a soft rose tint + dimmed text + rose badge; offer-in rows get an
emerald tint + emerald badge.

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

## 10. Click-to-Contact Outreach Logging (CDM Workspace)

```
User clicks a contact link (tel:/mailto:/Teams deep link/weixin://) in
tabs/contacts_tab.html inside the CDM account workspace
    |
    v
Delegated click listener in app/static/htmx_app.js
    |
    +---> Reads data-* attributes from the [data-outreach-log] element
    |       (data-channel phone|email|teams|wechat, data-value,
    |        data-company-id, data-site-id, data-contact-id, data-contact-name)
    |
    +---> Fire-and-forget fetch POST /api/activity/outreach-initiated
    |       (app/routers/activity.py, schema OutreachInitiatedRequest in
    |        app/schemas/activity.py)
    |
    +---> On success: $store.toast success flash + #cdm-list refresh
    |       (re-sorts the account list so the touch is visible immediately;
    |        the refresh preserves the current pagination offset/limit, read
    |        from data-* attrs on the _account_list.html header)
    +---> On success WITH dropped_links (server removed stale entity links):
    |       $store.toast WARNING flash instead — the touch is logged but
    |       invisible on this account; the list refresh is skipped
    +---> On failure (429/5xx/network): $store.toast ERROR flash — outreach
    |       logging failures are never silent
    |
    v
activity.py router -> log_outreach_initiated(db, user_id=..., channel=...,
    |                                          contact_value=..., ...)
    |   - rate limit: per-user "outreach" bucket (30/min), separate from the
    |     click-to-call bucket (10/min) so channels never starve each other
    |   - nonexistent OR mismatched company/site/contact ids (site not under
    |     the company, contact not under the site) are nulled out with a
    |     warning (stale DOM ids must not FK-crash the insert or bump an
    |     unrelated entity) and reported back as dropped_links in the 201 body
    |
    v
app/services/activity_service.log_outreach_initiated()
    |
    +---> Dedup: same user + channel + company/site/contact links + contacted
    |       value (channel snapshot column; subject for WeChat) within 120s
    |       (OUTREACH_DEDUP_SECONDS) returns the existing row — double-clicks
    |       do not create duplicate activities or double bumps, while distinct
    |       same-named contacts never collapse into one log
    |
    +---> Maps channel to ActivityType:
    |       phone   -> ActivityType.CALL_LOGGED   (direction=outbound)
    |       email   -> ActivityType.EMAIL_SENT
    |       teams   -> ActivityType.TEAMS_MESSAGE
    |       wechat  -> ActivityType.WECHAT_MESSAGE  (new, constants.py)
    |
    +---> DB: INSERT activity_log (is_meaningful=True, direction=outbound,
    |       linked to company_id + site_contact_id)
    |
    +---> DB: UPDATE companies.last_activity_at = now()
    +---> DB: UPDATE customer_sites.last_activity_at = now()
    |       (both bumps feed the CDM workspace staleness sort:
    |        oldest = longest since activity first)

Channel enum (app/constants.py):
    Channel.PHONE | Channel.EMAIL | Channel.TEAMS | Channel.WECHAT (new)
```

CDM business rules (staleness tiers, account-list query/sort, contact-row
assembly) live in `app/services/crm_service.py` (`staleness_tier`,
`cdm_company_query`, `cdm_list_ctx`, `company_contact_rows`); the
`htmx_views.py` routes are thin wrappers.
`company_detail_partial` builds `contact_rows` via the `company_contact_rows` helper
(active SiteContacts across the company's active sites + legacy site-level
contacts on active sites) and passes it to
`tabs/contacts_tab.html`, which is now the default (first-rendered) tab.

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

Worker second-pass ordering (run_one_batch, same shared post-await session, one
commit). As of SP2 the run ORDER is no longer load-bearing: record_spec arbitrates
every write through the F1 source-tier ladder (app/services/spec_tiers.py —
mpn_decode 85 > fru_matrix_decode 84 > desc_parse 83 > fru_desc_parse 82 >
spec_extraction 60; vendor APIs 90, trio_source 95, manual 100). A lower-tier writer can never overwrite a higher-tier prior regardless
of the confidence it claims or which pass ran first, so the old per-writer
"skip keys already held at higher confidence" pre-gates are REMOVED — the ladder
owns arbitration in one place:

    1. mpn_decoder/writer.py::decode_and_record_specs   — deterministic MPN→spec
       decode, source="mpn_decode" (tier 85), confidence 0.95
       (settings.mpn_decode_enabled). Category via spec_tiers.set_category.
    2. fru_crosswalk_enrich.py::crosswalk_and_record_specs — deterministic FRU
       crosswalk enrichment: ONE pass, TWO evidence channels over the same single
       fru_links query (rel_kind IN mfg_model + drive_pn), both gated by
       settings.fru_crosswalk_enrich_enabled. (a) DECODE channel: IBM/Lenovo FRU
       spare PNs inherit the STRICT-INTERSECTED decode of their rel_kind='mfg_model'
       models — only spec keys present in every decode with equal values write; a
       commodity disagreement skips the card (BOTH channels) — source=
       "fru_matrix_decode" (tier 84), confidence 0.93. (b) LINKED-DESCRIPTION channel
       (wave 3A): the qual-sheet prose stored on the FRU's mfg_model + drive_pn rows
       (e.g. drive_pn `18TB 3.5 HDD 7.2K 12 Gb/s SAS`, mfg_model `SSD; 2.5; 1.92 TB
       Samsung PM1733`) runs through desc_extractor.extract_desc(description,
       commodity_hint=card.category) — the SPEC_COMMODITIES eligibility gate
       guarantees the hint is always set. Commodity agreement is judged over ALL
       extractions (a spec-less result like bare "HDD, Hot Swap" prose is still
       commodity evidence): a desc-side commodity disagreement skips just the desc
       channel (counted in desc_commodity_conflict), and a UNANIMOUS commodity
       contradicting the card's category skips it too (desc_category_mismatch —
       the decode channel's existing-category-is-authoritative rule applied to
       desc evidence; reachable only as hdd<->ssd via extract_desc's same-family
       lead refinement). Spec-less extractions are then EXCLUDED from the per-key
       intersection (one barren row must not veto rich siblings under
       absence-is-not-agreement) and the survivors intersect under the SAME
       intersect_decodes contract (conflicting values dropped + counted per card
       in desc_dropped_conflict — the decode channel's dropped_conflict counts per
       FRU; a single extracting description passes all its specs) — source=
       "fru_desc_parse" (tier 82), confidence 0.88. The desc channel runs in its
       OWN per-card SAVEPOINT after the decode channel's savepoint has RELEASED,
       so a category the decode just filled still routes the extraction in the
       same batch; it NEVER writes a category (linked prose is not a regex-gated
       commodity proof — a still-category-less card gets nothing from it).
       Zero LLM/network; ONE fru_links query per batch. Scope is the FULL batch ids,
       NOT enriched_ids — FRU spares finish not_found, and the pass never touches
       enrichment_status. Fills a NULL category from the agreed DECODE commodity via
       spec_tiers.set_category (an existing DIFFERENT category skips the card before
       any write); never writes manufacturer; never writes the reverse direction (a
       card that IS a mfg_model already decodes first-party at tier 85 and
       desc-parses its own description at tier 83). The ladder (82 < 83 desc_parse <
       84 < 85, < vendor 90) guarantees neither channel overwrites
       mpn_decode/desc_parse/vendor values — no per-writer pre-gate. Isolation is
       three-level: decode/intersect failures are caught per FRU, decode-channel
       write failures per card (SAVEPOINT 1 — the card is lost, counted in
       `failed`, and the desc channel does not run on it), and desc-channel
       failures per channel (SAVEPOINT 2, sequential after SAVEPOINT 1's release —
       a desc failure rolls back ONLY the desc writes, the card keeps its decode
       writes + category fill, counted in `desc_failed`, never `failed`). The
       worker's stats line distinguishes a no-op batch from a crashed one (desc
       channel adds desc_parsed/desc_written/desc_failed/desc_dropped_conflict/
       desc_commodity_conflict/desc_category_mismatch counters). Schema-drift drops
       (no schema row / out-of-enum value) from BOTH channels emit the same
       aggregate WARNING as the mpn-decode writer.
    3. desc_extractor/writer.py::extract_and_record_specs — deterministic
       description→spec token grammar across EIGHT commodities (phase 1: hdd/ssd/
       dram; phase 2: power_supplies/displays/tape_drives/gpu/motherboards — TRIO
       part-master/inventory descriptions like `HD, 450GB, 15KRPM, 3.5", Fibre
       Channel`, `PSU, 1460W 240V/200V AC Hot Swap`, `Tape, JAG 7`), source=
       "desc_parse" (tier 83), confidence 0.90 (settings.desc_parse_enabled). Zero LLM/network;
       extraction is suppressed on foreign commodity labels ("Other,"/"Tray,"/
       "Card,"/"Library,"…) and conflicting tokens, while NEUTRAL leads (packaging
       words/brands/SPS- prefixes: "ASSY,"/"MSI,"/"SPS-PCA,"…) fall through to
       body-token + category-hint arbitration instead of dying foreign. Per-module
       structural guards: wattage exists only on the power_supplies route (CPU "135W"
       TDP unreachable — cpu stays hint-only) and gpu memory_gb requires a GPU-context
       token (NVIDIA/GDDR/HBM/family hit), so NIC "10GB"/"100GbE" rows emit nothing.
       Only cards already categorized to one of the eight commodities are written
       (NEVER categorizes — a description is not a regex-gated commodity proof).
       The F1 ladder (fru_desc_parse 82 < desc_parse 83 < fru_matrix_decode 84 <
       mpn_decode 85 < vendor 90) keeps decode/vendor values authoritative and the
       card's OWN description above its FRU-linked prose — no per-writer
       pre-gate. The five phase-2 commodities have no MPN decoders, so
       desc_parse is their top non-vendor deterministic source.
    4. spec_enrichment_service.py::enrich_card_specs    — AI spec reader,
       source="spec_extraction" (tier 60), facets gated at confidence >= 0.85
       (FACET_MIN_CONF — an AI output-quality floor, not cross-source
       arbitration). The ladder guarantees it never clobbers an
       mpn_decode/fru_matrix_decode/desc_parse/fru_desc_parse/vendor key, even
       when it self-reports 0.95+.

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
    |       +---> spec_tiers.tier_for(source) + resolve(existing, incoming)  — F1 LADDER
    |       |       (single uniform conflict rule for ALL sources; see contract below)
    |       +---> DB: UPDATE material_cards.specs_structured (JSONB — keyed parametric values
    |       |        incl. tier) — only when resolve() says the incoming write wins
    |       +---> DB: UPSERT material_spec_facets (incl. source/confidence/tier mirroring the
    |       |        winning JSONB entry — a losing write never mutates the facet)
    |       +---> DB: UPDATE material_cards.specs_summary (plain-text key-spec summary)
    |
    +---> DB: UPDATE material_cards.specs_enriched_at = now()
    |       (idempotent gate: NULL cards are processed; non-NULL cards are skipped
    |        unless force=True, e.g. from the Enrich button)
```

### spec_tiers — source→tier provenance ladder (SP2/F1+F2, `app/services/spec_tiers.py`)

The single authoritative "which source wins" rule, so source-execution ORDER is no longer
load-bearing (it replaced `record_spec`'s old vendor-only special-case + "latest write wins").

```
SOURCE_TIER  manual:100 · trio_source:95 · {digikey,mouser,nexar,element14,oemsecrets}_api:90
             · trio_source_ai:88 · mpn_decode:85 · fru_matrix_decode:84 · desc_parse:83
             · fru_desc_parse:82 (FRU-linked qual-sheet descriptions — below the card's
               OWN description, above the OEM scrapers)
             · {partsurfer,psref}→oem_scrape:80 · web_search:70 · brokerbin:65
             · spec_extraction:60 · legacy_backfill:50 (pre-ladder data; also the runtime
               floor for a valued category with NULL provenance) ·
               {ai_guess,claude_opus_inferred,claude_haiku}:40
             (unknown → 0 with a once-per-source WARNING — an unregistered writer loses
              every conflict; migration 096 carries a CASE snapshot of this map, pinned by
              a sync test)

tier_for(source) -> int                 # SOURCE_TIER.get(source, 0); warns once on unknown

resolve(existing, incoming) -> bool      # incoming wins iff its (tier, confidence, updated_at)
                                         # tuple is STRICTLY greater. existing=None → win.
                                         # higher tier always overrides; equal tier → higher
                                         # confidence; exact tie → newer updated_at; full tie → keep.
                                         # Pure function — no DB, no side effects.

set_category(card, value, source, confidence, write=True) -> bool   # the ONE DB-touching helper
    +---> normalize_category(value); None (off-vocab/empty) → return False, no write
    +---> build incoming{tier,confidence,now}; existing from card.category_* (None if category
    |     NULL; valued-but-NULL-provenance → legacy_backfill floor tier 50, same as the
    |     migration backfill, so an AI guess can't flip un-routed legacy data)
    +---> resolve(): on win set card.category + category_source/confidence/tier/updated_at,
    |     return True; else leave card untouched, return False  (a lower-tier source can't
    |     overwrite a higher-tier category; junk can't blank a real one). Tie-breaks compare
    |     category_updated_at (the category's OWN timestamp), never the card-wide updated_at.
    +---> on a win that CHANGES the category: purge the OLD commodity's MaterialSpecFacet
    |     rows + their specs_structured mirrors (logged at INFO) — a re-categorized card
    |     must not keep matching the old commodity's deep-filters
    +---> write=False = read-only twin (same verdict, zero mutation) — used by the
          SP-Ingest dry run so its report can't drift from --apply
```

Consumers: `record_spec` (tier persisted into `specs_structured`, conflict via `resolve`),
`mpn_decoder/writer.py` (decode category via `set_category`, tier 85),
`fru_crosswalk_enrich.py` (tier 84), the SP-Ingest pipeline (`source_ingest/ingest.py` —
TRIO part-master categories via `set_category` at trio_source:95 / trio_source_ai:88,
specs via `record_spec` + dry-run parity via `spec_would_write`), the manual edit
endpoint `routers/htmx_views.py::update_material_card` (manual:100 — a deliberate human
change always wins and purges the old commodity's facets; an UNCHANGED re-submitted value
is NOT re-stamped manual, and off-vocab/blank values are rejected with a `showToast`
warning instead of persisting), and ALL three remaining
category writers — `enrichment.py` (connector `{name}_api` tiers), `material_enrichment_
service.py` (claude_haiku:40), `authoritative_enrichment_service.py`
(claude_opus_inferred:40) — now route through `set_category` (no direct `card.category`
assignment remains; SP3 adds the `@validates` hardening). The deterministic decode is
protected by its tier (85), not by running before the fru-crosswalk (84) / desc-parse
(83) / AI spec (60) passes — the old per-writer confidence pre-gates are removed.

### SP-Ingest — TRIO source-data pipeline (`app/services/source_ingest/`, SP2)

```
python -m app.management.ingest_source_data [--files GLOB] [--ai-correct] [--apply] [--limit N]
    |
    v
parsers.py     — parse_inventory_sheet (.csv/.xlsx/.txt, header auto-detect) +
    |            parse_sfdc_material_master (streams the multi-hundred-MB
    |            LSC1__Material__c.csv row by row) + parse_sfdc_manufacturers
    |            (lookup-ID → name; raw Salesforce IDs are never emitted)
    v
clean.py       — clean_record: MPN suffix strip + dedup key (normalize_mpn_key),
    |            _x000D_/control scrub, condition → constants.MaterialCondition
    |            (None when the source carries none — NEVER a synthetic "Unknown"),
    |            normalize_trio_category (TRIO-scoped codes, e.g. bare "Memory"→dram),
    |            CPU-bucket pollution deny-list, "DO NOT USE"/short-MPN drops
    v
consolidate.py — group by normalized_mpn → ConsolidatedPart per MPN (longest desc,
    |            modal manufacturer/condition, highest-priority-kind category,
    |            qty sum, sfdc_master>inventory_sheet spec merge); un-cleaned
    |            records (empty dedup key) are counted + WARNed, never silent
    v
ai_correct.py  — OPTIONAL (--ai-correct): one Claude call per part under the
    |            no-fabrication guardrail; per-PART failure isolation, fail-fast on
    |            ClaudeUnavailable/Auth, consecutive-failure abort; returns
    |            {corrected, failed} for the report — an EMPTY structured result
    |            (claude_structured → None, no tool_use block) counts as failed and
    |            toward the abort streak; corrected counts only applied results
    v
ingest.py      — AUGMENT material_cards: category via set_category (trio_source:95 /
                 trio_source_ai:88), specs via record_spec, description/condition
                 fill-only-when-empty ("Unknown" == empty). Per-card SAVEPOINTs;
                 tallies merge only after a clean release; failed parts counted +
                 sampled in the report. apply=False (DEFAULT) = dry run through the
                 SAME gates (set_category(write=False) + spec_would_write) so the
                 go/no-go report matches --apply exactly.
```

```
Startup backfill:
    _backfill_material_cards() in startup.py
    +---> Runs at application boot
    +---> Ensures every MPN in requirements has a material card
    +---> Idempotent: skips MPNs that already have cards

Env: MATERIAL_ENRICHMENT_ENABLED=true
```

### Deterministic MPN decode (worker second pass, BEFORE the AI spec pass)

`enrichment_worker/worker.py::run_one_batch` → `mpn_decoder/writer.py::decode_and_record_specs`
(gated by `settings.mpn_decode_enabled`, default on) → `decode_mpn()` in
`app/services/mpn_decoder/` → `record_spec(source="mpn_decode", confidence=0.95)`.
Zero network/LLM; strict per-vendor regex gates; anything unrecognized returns None.
Coverage report: `scripts/decode_mpn_dryrun.py` (read-only; per-vendor/commodity counts;
`--apply` backfills). Category conflicts skip; NULL categories are set from the decode.

Vendor/scheme inventory (module → gate → decoded keys):

| Module | Vendor | Scheme gate (examples) | Decodes |
|---|---|---|---|
| storage.py | Seagate | `ST<GB><family>` (ST4000NM0035) | capacity, form_factor, usage_class |
| storage.py | Western Digital | `WD<TB×10><family>` (WD40EFRX) | capacity, usage_class+form for known 3.5" families |
| storage.py | Toshiba | `(MG\|MN\|MD\|MQ\|DT)\d{2}[A-Z]{3}` (MG08ACA16TE) | form_factor, usage_class, capacity from `<n>T` token |
| storage.py | HGST/Hitachi | `HUH\|HUS(?=\d)\|HUC\|HTS\|HDN\|HDS\|HMS` | form_factor, usage_class, capacity from `<n>T` token; HUSMM/HUSSL SAS SSDs excluded |
| ssd.py | Samsung | retail `MZ-<fam><cap>` (MZ-V8P2T0B/AM) + OEM `MZ<fam><cap>` (MZVL21T0HCLR, MZ7LH1T9HMLT, MZQL21T9HCJR, MZILT3T8HBLS) | capacity, form_factor (2.5"/M.2 2280/M.2 22110/U.2/mSATA), interface (SATA/SAS; NVMe gen only via pinned family tables), nand_type for retail EVO/QVO + V-table only |
| ssd.py | Micron | `MTFD<code><cap>` (MTFDKBA960TFR, MTFDDAK1T9TDS) | capacity, form_factor+interface from verified code table (DAK/DAV/KBA/KBG/KBK/KCB/KCC/HBA/HAL); unknown codes → None |
| ssd.py | Intel/Solidigm | `SSD(SC2\|SCK\|PE2\|PEK\|PF2)…<nnn>[GT]` (SSDPE2KX040T8) | capacity (G literal, T via decimal-TB table), form_factor, interface (E=PCIe 3.0, F=PCIe 4.0) |
| ssd.py | Kioxia | `KXG<gen>` (XG M.2), `KPM<gen>` (PM SAS 2.5"), `K(CM\|CD)<gen>` (CM/CD U.2/U.3) | capacity via verified tokens (1T02=1024, 3T84=3840…), form_factor, interface (gen 5=PCIe 3.0, 6=PCIe 4.0; later gens capacity-only) |
| ssd.py | WD | `WDS<nnn>[GT]<rev><suffix>` (WDS100T1X0E) | capacity, form_factor+interface from suffix table (B0A/R0A/G0A/B0B/G0B/B0C/X0C/R0C/X0E) |
| memory.py | Samsung | `M<code><gen>` (M393A2K43DB3-CWE) | ddr_type, form_factor, ecc, registered; DDR4: voltage 1.2, capacity from density digit, rank via verified org-token table (ambiguous 8G40 omitted); DDR3 voltage from -C/-H/-Y suffix |
| memory.py | SK Hynix | `HM(A\|T\|CG\|CT)` (HMA84GR7AFR4N-UH) | ddr_type, form_factor, ecc, registered; DDR4: voltage 1.2, capacity+rank from die×width math (R/U modules only — LRDIMM/3DS excluded) |
| memory.py | Micron | `MTA…G(64\|72)<mod>Z` + DDR3 `MT<n>(J\|K)SF…` (MTA18ASF2G72PZ-2G6E1, MT36KSF2G72PZ-1G6M1) | ddr_type, ecc, capacity (n×8), form_factor+registered from module letter, rank from device count (two-letter module codes omit rank), voltage (DDR4 1.2; DDR3 J=1.5/K=1.35) |
| memory.py | Kingston | `KVR/KSM<speed><L?><module>` (+KCP/KTH/KTD cap-only) | speed, ddr_type from speed code (13-18=DDR3, 21-32=DDR4, 48-64=DDR5 — NOT the D4 rank token), form_factor, ecc, registered, rank from S/D/Q×4/8 token, voltage (DDR4 1.2; DDR3 1.5, L-flag 1.35), capacity from `/<n>` (die-rev suffixes tolerated) |
| memory.py | Crucial | `CT<cap>G<gen><form>` (CT16G4RFD8266) | capacity, ddr_type, form_factor (incl. L=LRDIMM), ecc, registered, rank from `F[SDQ][48]` token, speed, voltage (DDR4 1.2) |

Never guessed: NVMe PCIe generation outside the pinned tables (seeded `interface` enum has
no bare "NVMe"), nand_type outside Samsung retail EVO/QVO/V-table, DDR5 voltage (1.1 V is
deliberately not emitted), Hynix DDR3 voltage, ranks on 3DS/ambiguous org codes, Kingston
KVR/KSM generation when the speed code is unmapped (DDR2-era parts — the D4 rank token must
never be misread as DDR4). `rank`/`registered`/`voltage` are seeded `dram` spec schemas in
`commodity_seeds.json` (the boot seeder inserts them idempotently — no migration needed);
`tests/test_mpn_decoder_seed_sync.py` pins decoder↔seed sync, and `writer.py` logs an
aggregate WARNING for BOTH of `record_spec`'s silent vocabulary drops — a decoded key with
no schema row AND an enum value outside the LIVE row's enum_values (the worker decodes
against live DB rows, which can lag a deploy's reseed) — so a drift can never silently
zero the feature (`record_spec` drops both cases at DEBUG only). Cards skipped because
their existing category conflicts with the decoded commodity are counted too
(`skipped_category_conflict` in the per-batch stats, plus a WARNING with the
`card_category->decoded_commodity` pairs — the number that says whether the
category-alias map needs another entry).

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

## FRU Crosswalk (IBM/Lenovo FRU ↔ 11S ↔ model ↔ tray)

```
Ingest (one-off, admin CLI):
python -m app.management.ingest_fru_matrix <FRU_PN_TRAY matrix .xlsx> [--apply] [--allow-missing-sheets]
    |
    +---> sheet-coverage guard: a mapped sheet missing from the workbook is FATAL
    |       (date-stamped names get renamed in new revisions) unless
    |       --allow-missing-sheets; sheets neither mapped (PARSERS) nor in
    |       KNOWN_SKIPPED_SHEETS are reported as unexpected and block --apply
    +---> per-sheet parsers (openpyxl read_only): Main, Qlot, Gabor, CZ, CDC,
    |       Lenovo-HDD, Lenovo FRU-PN, LVN VPD Mapping, Series, NSeries(NetApp)
    |       - hygiene: nbsp strip, sentinel→NULL (N/A, #N/A, PENDIENTE, ...),
    |         comma/slash multi-value split, carrier parentheticals→note,
    |         Lenovo SAP zero-padding/_<letter><digits> suffix de-pad (FRU and PPN
    |         cells both gated by the PN-plausibility regex), NSeries FRU
    |         forward-fill, prose cells rejected by PN-plausibility regex
    |       - normalization: fru_norm/related_norm via normalize_mpn_key
    |       - bounded context columns (manufacturer/series/machine/qual_status)
    |         truncated to model column lengths at parse time (PG-safe)
    |
    +---> DEFAULT dry run: "sheets parsed X/Y", per-sheet parsed/skipped counts,
    |       per-kind link counts, unparsed-cell counters (per kind/column, so a
    |       column-wide format change is visible), samples — no writes
    +---> --apply: chunked upsert into fru_links in ONE transaction (all-or-nothing;
            insert new edges; refresh context attrs on existing unique key;
            additive-only — absent edges are never deleted, None never nulls)

Lookup (read path):
GET /v2/partials/materials/{card_id}          (material detail surface)
GET /v2/partials/materials/fru-lookup?q=<pn>  (standalone HTMX partial; must stay
    |                                          registered BEFORE the {card_id} route)
GET /v2/partials/materials/faceted?q=<pn>     (faceted results — renders the
    |                                          fru_section above the card list on a
    |                                          crosswalk hit, so /v2/materials?q=
    |                                          deep links land on the matrix)
GET /v2/partials/search/history?mpn=<pn>      (search-page "What we know" panel —
    |                                          compact context card only, via the
    |                                          lightweight get_reverse_context;
    |                                          see §2a)
    v
fru_matrix_service.get_fru_view(db, mpn)      — forward: the part IS a FRU
fru_matrix_service.get_reverse_view(db, mpn)  — reverse: FRUs the PN appears under
fru_matrix_service.get_reverse_context(db, mpn) — reverse, compact: COUNT(DISTINCT
    |    fru_norm) + top-3 canonical FRU spellings, no row hydration (search panel)
    |   (raw input normalized internally; cross-sheet dedup prefers rows with
    |    qual_status/manufacturer and coalesces missing attributes)
    v
htmx/partials/materials/fru_section.html
    |   (on detail.html the FRU panels render ABOVE Crosses & Substitutes)
    +---> "FRU matrix" panel: sections (Approved drives & models / 11S part numbers /
    |       Options / Trays & hardware / Lenovo PNs / Sourcing & assembly), count
    |       badges, qual pills (amber=cdc_pending sentinel, emerald=ANY other
    |       non-empty qual_status — free workbook text, no closed vocabulary),
    |       series chips + first 3 machine chips (+N overflow chip, title lists the
    |       rest); each section shows 12 items, the rest hidden behind an inline
    |       "Show all (N)" / "Show less" Alpine expander; items link to materials
    |       search
    +---> "Used in FRUs" panel: FRU | role | qualification | context table — shows
            10 rows, the rest behind the same inline expander; server-capped at
            REVERSE_VIEW_LIMIT (200) with "showing first N of M" line (shared
            screws/tray PNs can sit under thousands of FRUs); each FRU links to its
            own fru-lookup (swaps #fru-crosswalk in place, pushes the materials URL)
```

---

## Enrichment Coverage Telemetry (Ops / Observability)

```
python -m app.management.enrichment_coverage_report [--json] [--log-file PATH]
    |   (read-only — single session, no writes; on PG all queries share one
    |    REPEATABLE READ snapshot so concurrent enrichment-worker writes can't
    |    skew cross-metric ratios; intended as a daily ops cron — host-side,
    |    not yet registered anywhere in this repo)
    v
collect_metrics(db) — a handful of aggregate queries over active cards
    |   (deleted_at IS NULL everywhere, incl. the facet joins)
    |
    +---> Cards: total, category coverage (non-blank %, 'other' count,
    |       top-15 lower(trim(category)) by count), description coverage
    +---> Facets: distinct cards with >=1 material_spec_facets row (+% of cards),
    |       facet rows total, per-commodity rows + distinct spec_keys (top-15;
    |       facet.category IS the commodity)
    +---> Sources: specs_structured entries grouped by each entry's recorded
    |       "source" (mpn_decode / desc_parse / fru_matrix_decode / fru_desc_parse /
    |       spec_extraction / vendor APIs / "(none)" for legacy non-dict entries
    |       or entries with a missing/null source).
    |       ONE query: PG iterates the JSONB in SQL (CROSS JOIN LATERAL
    |       jsonb_each), SQLite uses json_each (tests), other dialects fall back
    |       to one streamed Python pass — keep all three branches in sync; when
    |       changing the PG SQL, run the opt-in parity test (set PG_TEST_DSN to
    |       a Postgres DSN) or verify against live PG
    +---> enrichment_status distribution; fru_links totals (rows + distinct
            fru_norm) only if the table exists
    |
    v
Output: one compact human-readable block, or the structured metrics dict with
--json. With --log-file it appends one JSONL line {ts, metrics} per run and, when
a previous line exists, prints "Δ since last run" for the headline numbers
(cards / with-category / with-description / faceted-cards / facet-rows /
spec-entries / fru-rows). The history reader scans backwards past corrupt or
wrong-shape trailing lines (each logged as a warning), and appends heal a
missing trailing newline first, so a torn write from a crashed run never merges
with — or suppresses deltas beyond — its own entry.

Tests: tests/test_enrichment_coverage_report.py (seeded fixture set; metrics,
delta math, --json shape, log-file behavior).
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

Sidebar facets (workspace.html + materialsFilter Alpine component) — COMMODITY-FIRST
(Direction B). Order top→bottom:
    +---> Sticky summary band: "<N> active · Clear all · Copy link" (Clear all =
    |     clearAllFilters(), keeps commodity; Copy link copies the URL). Compact only —
    |     detailed removable chips stay in the results header.
    +---> Recents strip (recentCommodities, $persist, cap 5) + type-to-find "Jump to
    |     category…" (categorySearch client-filters the tree; group headers hide, matches
    |     show flat).
    +---> Commodity tree → /v2/partials/materials/filters/tree (13 groups; the entry
    |     point, moved to TOP). Memory ≠ Storage & Drives, Connectors ≠ Electromechanical.
    +---> Selected commodity's sub-filters → /v2/partials/materials/filters/sub:
    |       is_primary expanded; rest fold under "More filters (N)". Fixed-vocab enums
    |       show every canonical value with a count incl. (0); open-vocab → typeahead.
    |       Fold/typeahead state HOISTED to materialsFilter.ui.* so it survives the
    |       per-filters-changed HTMX reload. Counts via get_facet_counts() — which now
    |       SELF-EXCLUDES each actively-filtered facet (OR-within-facet; selecting one
    |       value no longer collapses its siblings to 0). With NO commodity selected the
    |       route renders the server-side placeholder "Select a category to unlock spec
    |       filters" (subfilters.html commodity_selected=False branch; no service calls)
    |       instead of an empty response.
    +---> Data confidence (FIRST filter fold, EXPANDED by default — $persist
    |     confidenceOpen defaults true under the ROTATED key mat_confidence_open2;
    |     the legacy mat_confidence_open key held a persisted `false` for every
    |     prior visitor — persist writes the current value on init — and is removed
    |     on load so the new default reaches returning users): 3 groups —
    |     Trusted / AI-inferred / No data;
    |     default all-on; `statuses[]` → `?statuses=` CSV → search_materials_faceted
    |     (IN-filters enrichment_status; precedence over the legacy verified_only).
    |     Collapse policy: navigation sections open, trust fold open, heavy folds
    |     below closed.
    +---> "Sourcing signals" (2nd fold, collapsed, $persist sourcingOpen; active-count badge) —
    |     Layer-3 operational filters, all top-level params on
    |     /v2/partials/materials/faceted → search_materials_faceted():
    |       has_stock   (EXISTS MaterialVendorHistory row)
    |       has_price   (EXISTS row with last_price IS NOT NULL)
    |       has_crosses (cross_references holds a non-empty list; portable text-cast
    |                    predicate — identical on PG JSONB and SQLite JSON-as-text)
    |       internal    (tri-state all|standard|internal on is_internal_part; default
    |                    `all` — deliberately not `standard` — so first load never
    |                    silently drops rows)
    |       searched_within (7d|30d|90d|any chips on last_searched_at)
    |       min_searches    (int ≥ 0 on search_count)
    |     Unknown/invalid values degrade to the no-op default with a WARNING log
    |     (hand-edited URLs never 500/422; the log surfaces frontend/backend
    |     vocabulary drift). This covers ALL the operational params: the enum-ish
    |     ones (internal / searched_within), non-numeric or negative min_searches,
    |     AND the boolean flags (has_stock / has_price / has_crosses /
    |     has_datasheet) — declared as lenient strings, truthy {true,1,yes,on} /
    |     falsy {false,0,'',no,off}, anything else WARNs and degrades to False
    |     (a bool Query would 422 on ?has_stock=bogus and htmx would silently
    |     refuse to swap, leaving stale results with only the generic error toast). Vocabularies are owned by
    |     faceted_search_service (INTERNAL_FILTER_VALUES / SEARCHED_WITHIN_VALUES,
    |     derived from the maps that drive the query branches); the JS twin is
    |     INTERNAL_MODES / SEARCH_BUCKETS on the materialsFilter component.
    |     Static section (no per-value counts → no HTMX reload).
    +---> "More attributes" (LAST fold, collapsed, $persist moreAttrsOpen; active-count
    |     badge): Manufacturer (search + top-N) + Global facets (lifecycle / rohs /
    |     condition / has_datasheet) via get_global_facet_counts(). Containers load
    |     while hidden.
    Live result count "<N> <Commodity> parts" renders at the top of the results pane
    (list.html) every filters-changed cycle, with an sr-only aria-live announcement.
    Mobile drawer: x-trap focus trap + Escape-to-close.

Coverage-aware empty states (get_commodity_spec_coverage(db, commodity) →
SpecCoverage(with_specs=N, total=M) NamedTuple; two cheap aggregates, no N+1):
    +---> Sub-filters panel header shows "N of M parts in <commodity> have filterable
    |     specs" so thin parametric results are explained before filtering.
    +---> Zero results + active parametric sub_filters + N < M → list.html renders the
    |     "not yet spec-enriched" nudge instead of the generic empty state.
Result-row upgrades (list.html, server-side in materials_faceted_partial):
    +---> Spec chips also render WITHOUT a commodity: each card's own category's
    |     is_primary schema keys (one batched CommoditySpecSchema query), else the first
    |     3 scalar specs_structured entries, formatted "label: value". Every chip carries
    |     title="label: value" so the value-only commodity rendering keeps its label
    |     on hover.
    +---> Datasheet icon-link (new tab, rel=noopener) when datasheet_url is set;
    |     "N alternates" chip when cross_references is non-empty (neutral gray — count
    |     metadata, not a status; indigo is reserved for OEM-SOURCED); condition badge
    |     styled like the lifecycle palette, with Refurbished/Used sharing violet
    |     (second-life family) so amber stays exclusively caution/reconfirm.

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
| Companies/CRM | 42 | CRUD, sites, contacts, enrichment, import; CDM workspace (`/v2/partials/customers`, `/v2/partials/customers/account-list`); outreach logging (`POST /api/activity/outreach-initiated`) |
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
    +---> Step 6b: Host worker venv refresh + restart (nc/ics)
    |       +---> pip install -r requirements.txt into /root/availai/.venv
    |       +---> systemctl restart avail-nc-worker avail-ics-worker
    |       +---> WARN (re-surfaced after logs) if venv/restart fails
    +---> Step 7: Tail logs for errors
```

**Host worker dependencies (pinned-lockfile venv).** The `avail-nc-worker`
/ `avail-ics-worker` systemd units run on the HOST (outside docker, from
`/root/availai`, `User=root`) and execute
`/root/availai/.venv/bin/python -m app.services.{nc,ics}_worker.worker`.
That venv is built from the SAME pinned `requirements.txt` as the docker
app/enrichment images (not ad-hoc `pip install patchright beautifulsoup4`),
so the host workers carry identical pinned deps — notably `patchright`,
which they use to drive **system Google Chrome** via `channel="chrome"`
(the bundled Chromium is unused). `deploy.sh` Step 6b refreshes the venv
from the lockfile and restarts both units on every deploy;
`scripts/setup_nc_worker.sh` bootstraps the venv on a fresh host. The
unit files live in `deploy/avail-{nc,ics}-worker.service`.

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
captured, not raised) and exposes the true outcome via `X-RFQ-Sent` / `X-RFQ-Total` /
`X-RFQ-Skipped` response headers. `send_batch_rfq` tags each result `sent` / `failed` /
`skipped` (no contact email — logged, not silently dropped); the route counts only
`sent`, names `failed` vs "No email on file" vendors distinctly in the toast, and logs
activity + auto-advances status only for actually-sent vendors. `confirmSend` reads the
headers and toasts via `$store.toast`: full success, partial (warning, distinguishing
"N failed" from "N had no email"), or total failure (error — modal stays open to retry);
it never infers success from the HTTP status alone.

### solicitModal  (htmx_app.js, Alpine.data)

Backs `excess/solicit_modal.html` — the "Solicit Bids" email composer opened from an
excess list. Only the email **subject** (derived from the list title) is dynamic, so the
factory takes that one argument: `x-data='solicitModal({{ (("Bid Request: " ~ list.title)
if list else "Bid Request")|tojson }})'`. It lives in JS (not inline) for the same reason
as `rfqVendorModal`: the title may contain `'`/`"`, and the old inline **double-quoted**
`x-data` interpolated it with `|e` — which HTML-escapes but emits an invalid JS string
literal, so an apostrophe/quote title broke `Alpine.init()` and left the whole modal inert
(spinner/"AI Clean Up" never un-cloaked, submit dead). The **single-quoted** `x-data` +
`|tojson` is immune. State: `recipientEmail`, `recipientName`, `subject`, `bundled`
(one bundled email vs one per item), `message`, `polishing`. Method: `polishEmail()` →
`POST /api/excess-lists/polish-email` ({text} in, {text} out) replaces the body with an
AI-cleaned version. The form itself posts via HTMX to
`/v2/partials/excess/{list_id}/solicit` and closes the modal on success.

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
