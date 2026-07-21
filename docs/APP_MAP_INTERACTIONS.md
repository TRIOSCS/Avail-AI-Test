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
status transitions (including the required-reason close to Won/Lost), and
sales-note edits likewise route through `activity_service.log_activity()`
(`ActivityType.TASK_COMPLETED`, `ASSIGNMENT_CHANGED`, `STATUS_CHANGED`,
`SALES_NOTE`). There is no requisition archive action — a requisition ends in
Won or Lost, each recording an `outcome_reason`.

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

### 1a. New-Requisition "Hot List" toggle (monitor-only create)

The unified create/import modal (`requisitions/unified_modal.html`) carries a
**Hot List** monitor-only checkbox. `POST /v2/partials/requisitions/import-save`
(`requisition_import_save`, `routers/htmx/requisitions.py`) takes it as
`hotlist: bool = Form(False)` and, when set, creates the requisition in
`RequisitionStatus.HOTLIST` **instead of** `OPEN`. A Hot List requisition is
**not sourced** (there is no create-time search kickoff, and the search queue is
`OPEN_PIPELINE`-scoped so `HOTLIST` is excluded): its parts are stored and market
data is built, but nothing is queried out to vendors. Instead the **Proactive
matcher** (§ 7) surfaces offers when matching stock appears — its query filters
`status == HOTLIST` and joins `Company` on `Requisition.company_id`, so the
create path populates `company_id` from the chosen site for **every** create
(Hot List or not), guarded for the no-site case. A "Hot List" filter pill on the
requisitions list (`requisitions/list.html`) surfaces created monitor deals.

### 1b. Sales-Hub per-part Won/Lost (replaced the bulk Archive)

The Sales-Hub parts workspace selection bar offers **Mark Won** / **Mark Lost**
in place of the removed bulk Archive. `POST /v2/partials/parts/bulk-outcome`
(`bulk_outcome`, `routers/htmx/parts.py`) takes
`{requirement_ids, outcome: "won"|"lost", reason}` and resolves each selected
line at the **part level**: every `Requirement` is transitioned to
`SourcingStatus.WON`/`LOST` via the sourcing state machine
(`transition_requirement`) and stamped with the one shared `outcome_reason`
(migration 185; see APP_MAP_DATABASE § requirements). A **blank reason 400s** the
whole request; ids missing or not in a legal source state are logged and skipped
(never 500 the batch); the handler commits once and re-renders the parts list
with a "N part(s) marked Won/Lost" toast. Ownership is guarded per line via
`require_requisition_access` (mirrors the retired `bulk_archive`). This is the
per-line analogue of the requisition-level required-reason close to Won/Lost
(§ 1) — there is no part-line archive.

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
          htmx.search_views.search_history_panel
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
    +---> _expand_fru_aliases(db, req) → fru_matrix_service.get_search_aliases
    |     FRU-crosswalk alias injection (item 2.7). The primary MPN is looked
    |     up in fru_links BOTH directions; its mfg_model/drive_pn/option/ibm_11s
    |     equivalents (the canonical numbers brokers actually list) are deduped
    |     against the primary + existing substitutes, capped at 8 in that kind
    |     priority order, and appended to the search MPN set so they fan out to
    |     every connector. Each alias is durably persisted as a system-derived
    |     substitute {"mpn", "manufacturer", "source": "fru_crosswalk"} via a
    |     dedicated write session (_persist_fru_aliases) — so it survives through
    |     the existing primary+substitutes contract and future searches, AND on
    |     the all-cached short-circuit path that never opens the main write
    |     session. Best-effort: a lookup/persist failure logs and the search
    |     proceeds with the explicit MPNs. Display flags crosswalk-derived subs
    |     with a "via FRU crosswalk" tooltip via the |fru_alias_mpns filter in
    |     _mpn_chips.html (no new UI elements).
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
    +---> enqueue_for_tbf_search(requirement_id, db)   # browser worker queue (The Broker Forum)
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
    NOTE: MPN dedup across connector results goes through
          `strip_packaging_suffixes()` (app/services/search_worker_base/
          mpn_normalizer.py). It now also strips `-TRPBF`/`/TRPBF` (combined
          tape-and-reel + lead-free), `-E3`/`-E4` (Vishay/ON Semi Pb-free grade
          suffix), and a `[-/]TR<n>` reel-quantity pattern. It deliberately does
          NOT strip `-13`/`-Q1`/`-EP`/bare `-T` — these mark genuinely distinct
          SKUs (package variant, AEC-Q100 automotive grade, "Enhanced Product"
          grade) whose stripping would wrongly merge different parts.
          `normalize_mpn_key()` deliberately does NOT strip date codes — a
          guardrail-tested decision, not an oversight.
    |
    +---> scoring.py (score_sighting_v2 — 5-factor weighted: trust 0.30, price 0.25,
    |       qty 0.20, freshness 0.15, completeness 0.10; SIGHTING_V2_WEIGHTS)
    |       +---> evidence_tiers.py (assign T1-T7, via app/source_trust.py)
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

### 2a-bis. Part Dossier ("The Bench") — `/v2/search?mpn=<PN>`

The Search tab is a single one-PN **Part Dossier**: a scrolling document that paints
identity/specs/history instantly from the DB, with the live market streaming in below.

```
GET /v2/search?mpn=<PN>  (v2_page → base_page.html fires hx-get partial_url)
    |  v2_page search branch: partial_url = /v2/partials/search?mpn=<quote(PN)>
    v
htmx/search_views.py: search_form_partial(mpn)
    |  mpn present  → dossier_shell.html      (the Bench)
    |  no mpn       → form.html landing + lazy /v2/partials/search/recent
    v
dossier_shell.html lazy-loads (each div has an explicit hx-target="this"):
    +-- GET /v2/partials/search/dossier/hero?mpn=    part_dossier.dossier_hero
    |     instant DB read (MaterialCard + PartHistory). Light-footprint write:
    |     bumps search_count/last_searched_at on an EXISTING card only (never
    |     creates one — unknown PN stays "New to us" / "Known via FRU crosswalk").
    +-- GET /v2/partials/search/dossier/market?mpn=  part_dossier.dossier_market
    |     A degraded-source banner (dossier_market_banner.html) renders above both
    |     branches when live-market sources are down (auth/quota) — see market_health
    |     below. cache HIT (Redis search:{key}:latest → search:{id}:results) → cached
    |     vendor rows in the light market card + "↻ Refresh market"; cache MISS
    |     (or ?refresh=1) → inner div auto-fires the EXISTING POST /v2/partials/
    |     search/run SSE flow (results_shell.html). The banner sits OUTSIDE that
    |     hx-post div so it survives the cache-miss SSE swap.
    |     On cache HIT a read-only market-baseline strip renders above the rows
    |     (compute_market_baseline helper): franchise-median price, authorized stock,
    |     and authorized source count — computed from cached rows, no new DB columns,
    |     no persistence. Graceful empty state when no authorized rows exist.
    +-- GET /v2/partials/search/history?mpn=         (EXISTING search_history_panel)
    +-- GET /v2/partials/search/dossier/specs?mpn=   part_dossier.dossier_specs
```

New router `app/routers/part_dossier.py` (GET-only; reuses data/services, no route
moves). `stream_search_mpn` now also writes the pointer key `search:{normalize_mpn_key
(mpn)}:latest = search_id` (TTL 900s) so the dossier market cache-hit path can find the
freshest run. The search-flow templates (`dossier_shell` "Live market" section,
`dossier_market`, `results_shell`, `vendor_card`, `shortlist_bar`,
`requisition_picker_modal`) use the **light brand-card skin** matching the rest of the
site — the earlier dark "terminal" look was the visual outlier and has been removed.
Page-level + per-row RFQ/offer actions (the quick-source endpoints) are wired.

**Market-baseline strip (price-sanity signal)** — `compute_market_baseline(rows)` in
`app/search_service.py` filters the already-fetched cached rows to
`is_authorized=True` rows and computes: franchise-median price (same upper-median
algorithm as `search_service._median`), authorized stock (sum of `qty_available`),
and authorized source count. `part_dossier.dossier_market` passes it as
`market_baseline` to `dossier_market.html`, which renders a read-only strip above the
vendor rows on cache HIT — the buyer-facing reference for spotting over/under-priced
offers. No DB column, no persistence, no SSE change, no Alpine state — pure server-side
summary. Graceful empty state ("No franchise/authorized pricing for this part.") when
no authorized row exists. `market_baseline=None` on cache MISS (strip omitted entirely).

**Degraded-source banner** — `search_service.get_market_source_health(db)` reuses
`_build_connectors` to partition the live-market connectors into available / `down`
(health_monitor flagged ERROR — auth/quota, operator must rotate credentials in Settings
→ Connectors) / `unconfigured` (no API key). `dossier_market` passes the result as
`market_health`; the banner names each down source with its specific error as a hover
tooltip and deep-links `/v2/settings` (→ Settings → Connectors tab). Best-effort: a
health lookup failure leaves `market_health=None` and never breaks the market section.

**Relevance guard** — `stream_search_mpn` keeps only hits whose `mpn_matched`
`fuzzy_mpn_match`es the searched MPN (handles dash/case + ≤2-char revision suffixes,
symmetrically). Keyword-match noise from catalog distributors (a different MPN — e.g. a
component returned for a storage FRU) is excluded before scoring/dedup/cache; the dropped
count rides the `done` SSE event as `off_target` and surfaces as a footnote in
`#search-stats`. Cross-references (alternate/FRU part numbers) belong in "What we know",
not the live-market offer list.

**Auto-datasheet capture** — a fire-and-forget `capture_datasheet(mpn, user_id)` job is
enqueued via `safe_background_task(..., suppress_in_testing=True)` on two triggers:
(1) `dossier_hero` — every Part Dossier page-load; (2) `quick_source_rfq` /
`quick_source_offer` + `add_requirements` (Requirements router) — whenever a part is
added to an RFQ or the requirements list. The job opens its own DB session (request
session is already closed) and follows this pipeline:

```
capture_datasheet(mpn, user_id)
    |
    +-- gate: card already has a datasheet row?          → skip (already stored)
    +-- gate: datasheet_searched_at < 30 days?           → skip (negative cache)
    |
    +-- find_datasheet_url(card, mpn):
    |       connector card.datasheet_url if present      → source="connector" (trusted)
    |       else: Claude web_search (up to 6 uses)       → source="web" (untrusted)
    |       TESTING env: web-search branch skipped
    |
    +-- download_pdf(url):
    |       SSRF guard: scheme + per-hop IP check (blocks private/loopback/
    |       link-local/multicast/reserved); follows ≤5 redirects, re-validates
    |       each hop; 25 MB size cap; must begin with %PDF- magic bytes.
    |       (DNS resolution runs in asyncio.to_thread to avoid blocking the loop.)
    |
    +-- source=="web": pdf_contains_mpn(pdf, mpn)?
    |       pypdf extracts text from first 20 pages; MPN normalised to alnum-lower
    |       key (≥4 chars); mismatch → stamp searched, discard (wrong file)
    |
    +-- cardless MPN + verified hit: resolve_material_card(mpn, db)
    |       creates a card only on a *verified* hit (miss never creates a card)
    |
    +-- upload_datasheet_to_library(file_name, pdf, "application/pdf",
    |       manufacturer=card.manufacturer)                      [datasheet_library.py]
    |       |
    |       +-- get_app_graph_token()                           [graph_app_auth.py]
    |       |   client-credentials (AZURE_CLIENT_ID/SECRET/TENANT_ID);
    |       |   requires Sites.Selected application permission; cached 55 min.
    |       |   Returns None → skip storage gracefully.
    |       |
    |       +-- PUT /drives/{DATASHEET_LIBRARY_DRIVE_ID}/root:/
    |               Datasheets/{manufacturer}/{MPN}-datasheet.pdf:/content
    |       → {library_item_id, library_web_url, size_bytes, library_drive_id}
    |       DATASHEET_LIBRARY_DRIVE_ID unset or token unavailable → returns None
    |       (capture stamps datasheet_searched_at and returns; upload is optional)
    |
    +-- INSERT MaterialCardDatasheet row (material_card_datasheets, migration 111)
         UPDATE MaterialCard.datasheet_captured_at / datasheet_searched_at
```

`material_card_datasheets` stores one row per captured file: `library_item_id`,
`library_web_url`, `library_drive_id` (Graph drive id of the company library),
`source` ("connector"/"web"), `original_url`, `verified`, `uploaded_by_id`
(optional; user who triggered the capture), `captured_at`.
`MaterialCard` carries two stamps: `datasheet_captured_at` (hit) and
`datasheet_searched_at` (any attempt — gates the 30-day negative cache).

**Storage: company SharePoint library (app-only).** Storage goes to a shared
SharePoint document library, not a per-user OneDrive. The app uses an app-only
Graph token (`graph_app_auth.get_app_graph_token`, client-credentials flow) so no
user must be present. One-time Azure-admin setup required: create the SharePoint
library, grant the Azure app `Sites.Selected` (application permission, admin-consented)
scoped to that site, and set `DATASHEET_LIBRARY_DRIVE_ID` to the library's Graph drive
id. Until that env var is set the upload step is silently skipped.

**Dossier UI** (`dossier_datasheet_block.html`, included in `dossier_specs`) has three
states: (a) `card.datasheets[0]` present → "Datasheet (saved MMM DD, YYYY)" link that
hits the in-app streaming endpoint `GET /v2/partials/search/dossier/datasheet/{id}/download`
(fetches from the company library via app-only token, streams as `application/pdf`);
(b) `datasheet_searched_at` set but no captured copy → "No datasheet found (will retry)";
(c) neither stamp yet → "Fetching Datasheet…" spinner that polls
`GET /v2/partials/search/dossier/datasheet-status?mpn=` every 15 s
(`hx-trigger="every 15s"`). The status endpoint returns the same `dossier_datasheet_block`
fragment and responds HTTP 286 (stops HTMX polling) once either stamp is set.

### 2b. Streaming Part-Search (`/v2/partials/search/run`)

```
Browser POST /v2/partials/search/run  (manual MPN entry)
    |
    v
htmx/search_views.py: search_run()
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
        |     loop via _await_next_within_budget(pending, remaining):
        |       publish "source-status" / "results" / "card-update" per connector
        |     publish terminal "done" once all settle
        |
        +---> Bounded aggregate deadline (shares _fetch_fresh's
        |     settings.search_total_timeout_s). Each round passes the REMAINING
        |     budget to asyncio.wait; when it is spent with tasks still pending,
        |     _await_next_within_budget cancels + drains the stragglers and the
        |     loop publishes an error chip ("search budget exceeded") for each,
        |     then breaks to "done". One hung/rate-limited connector can no longer
        |     hold the browser spinner for minutes.
        |
        +---> Per-source telemetry (searches/results/latency + errors) is
        |     accumulated and flushed to ApiSource in one guarded pass after the
        |     loop (mirrors _fetch_fresh) — the interactive path recorded ZERO
        |     telemetry before, so streaming failures were invisible in admin health.
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
| `ConnectorAuthError` | "Auth error — rotate credentials: ..." | Rotate API key in Settings → Connectors |
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

**Health probes bypass the open circuit breaker (2026-07-04, Phase 2).** The
health monitor (`ping_source` / `deep_test_source`) and the Settings "Test"
button (`routers.sources._probe_source`) probe via
`connectors.sources.run_health_probe`, which for `BaseConnector` connectors calls
`health_probe` — identical to `search` (semaphore + retry + breaker bookkeeping)
except it does NOT short-circuit when the breaker is open. Rationale: a breaker
that tripped during a user search is a *transient* in-process protection (it
resets after `reset_timeout`). If a health ping honored that open state it would
fail with a bare "circuit breaker open" and flip `api_sources.status` to a
15-min ERROR exclusion — turning one flaky search into a 15-minute outage of a
perfectly healthy source. The probe therefore measures GENUINE upstream health: a
real 200 clears the trip (`record_success` → source stays `'live'`), while a real
auth/quota/5xx failure still records against the breaker and flips status to
`'error'` (a truly-down connector stays excluded, and the operator gets the real
typed error, not "circuit breaker open"). Keyless test connectors (AI web,
email-mining, Teams, Clay, …) have no breaker and fall back to plain `search`.
Tests: `tests/test_circuit_breaker.py`, `tests/test_health_monitor.py`.

**No carve-outs.** All seven connectors (Mouser, BrokerBin, Nexar,
DigiKey, Element14, OEMSecrets, Sourcengine) follow this contract
uniformly. The Mouser HTTP-403/429 silent-empty path that existed prior
to round-2 was the silent-failure mode the contract is designed to
eliminate; it has been removed.

**Core-search hardening (2026-07-03, Phase 1).** Four call-volume /
robustness invariants layered on top of the contract above, none of which
change the retry/breaker/health semantics:

- **Process-wide OAuth token cache.** DigiKey / eBay / Nexar authenticate
  with a client_credentials bearer. `_build_connectors` rebuilds connector
  instances every search, so the old per-instance token cache died each
  search (~3 serial token POSTs on the critical path + an intra-search mint
  herd). The cache is now module-level in `connectors/sources.py`
  (`_token_cache`, keyed by `(connector class, client_id)` with expiry) and
  minting is serialized by a per-key `asyncio.Lock` (`_get_cached_token` /
  `_invalidate_token`), mirroring the `_breakers` / `_connector_semaphores`
  pooling. A 401 invalidates the shared entry before re-minting. Tests clear
  `_token_cache` + `_token_locks` per-test (a Lock is loop-bound).
- **element14 keyword fallback dropped.** `_do_search` no longer fires a
  second keyword-search call on a 0-result exact-MPN miss. element14 returns
  HTTP 403 for BOTH auth rejection and its per-second QPS cap, so doubling
  call volume accelerated the 403s that ERROR-exclude the source — and the
  keyword rows were catalog noise the relevance guard discards anyway. The
  exact `manuPartNum:` lookup is the authoritative path.
- **Price-break null-quantity coalesce.** DigiKey / Mouser / Nexar (GraphQL
  + REST v4) select the lowest-quantity price break via `min(..., key=...)`.
  A present-but-null quantity key made the key return `None` → `None < int`
  TypeError → the whole PN errored. The key now coalesces
  Quantity/BreakQuantity/breakQuantity/quantity to a large sentinel.
- **Search-cache Redis off the event loop.** `_fetch_fresh` wraps the sync
  `_get_search_cache` / `_set_search_cache` (sync `redis.get`/`setex`) in
  `asyncio.to_thread` so a slow/unreachable Redis can't block the single
  loop and stall every in-flight request. The helpers stay best-effort.

**Infra resilience (2026-07-04, Phase 2).**

- **Redis lazy-client re-probe + downgrade metric.** Both Redis-backed cache
  clients — `search_service._get_search_redis` (search-result cache) and
  `cache.intel_cache._get_redis` (intel cache + `@cached_endpoint` +
  `rate_limit.check_rate_limit`) — used to init lazily and *stick*: the first
  failed connect disabled Redis for the whole process lifetime, silently, with
  no recovery. Both now delegate to `cache.redis_probe.RedisProbe`, which
  re-probes the real Redis at most once per `REPROBE_INTERVAL_S` (30s) while
  degraded and recovers transparently when it returns. The degraded state is
  observable: Prometheus `redis_degraded{subsystem}` (1/0) +
  `redis_downgrade_total{subsystem}` (healthy→degraded transitions), plus a
  WARNING on downgrade / INFO on recovery. `get()` is best-effort and never
  raises, so it is safe on the request hot path. Tests: `tests/test_redis_probe.py`.

**Connector polish (2026-07-04, Phase 4 — schedule tier).** Lower-priority
robustness items that do not change the retry/breaker/health semantics above:

- **eBay explicit 429.** `ebay.py::_do_search` special-cased 401 and 404 but let a
  429 fall through to the generic `raise_for_status` path. It now handles 429
  explicitly like DigiKey (its OAuth-client-credentials sibling): honor Retry-After
  (capped by `_parse_retry_after`, see below) with one inline retry,
  then raise the typed `ConnectorRateLimitError` on a persistent 429. eBay now
  conforms to the 429→`ConnectorRateLimitError` row of the contract above.
- **`_parse_retry_after` cap lowered to 8s (was 30s).** `_fetch_fresh`'s aggregate
  fan-out budget is `settings.search_total_timeout_s` (12s default) — a 30s sleep
  would always outlive that deadline and get cancelled anyway, so honoring an
  upstream's longer Retry-After was pointless in the search context. Paired with
  this, `_search_with_retry` (`app/connectors/sources.py`) now acquires the
  per-connector semaphore ONLY around the `_do_search` HTTP call, not around the
  retry sleep — a 429 backoff no longer pins the connector's concurrency slot (and
  transitively the caller's search-wide `asyncio.Semaphore(10)`) while it sleeps,
  so a slow retrying connector no longer starves its peers' throughput.
- **Nexar empty-REST → GraphQL fall-through.** `NexarConnector._do_search` tried the
  Octopart REST v4 path first and returned its result whenever it was `not None` —
  so a 200 REST response with ZERO seller rows (`[]`) short-circuited the richer
  GraphQL seller path. Only a NON-EMPTY REST result now wins outright; an empty (or
  None) REST result falls through to GraphQL, which may surface rows the REST key's
  plan/coverage misses. FLAG: a live Nexar search is required to confirm the GraphQL
  path returns rows when REST is empty.
- **Sourcengine response-shape drift guard.** `SourcengineConnector._parse` is a
  best-guess adapter (offers/results/data envelope) never validated against the live
  shape. It now logs a WARNING (never a silent `[]`) when a 200 body is not a JSON
  object or carries none of the recognized offer keys — surfacing drift instead of
  masquerading as "no matches". FLAG: the connector's `SEARCH_URL` (`/v1/search`) no
  longer matches the documented endpoint (`/app/api/search/parts/searchpart`); a live
  call must confirm the real endpoint + shape before this source is trusted.
- **`@cached_endpoint` async-target guard.** `cache/decorators.py` selects an async
  wrapper via `asyncio.iscoroutinefunction` so async endpoints are awaited and cached
  correctly (the old sync-only wrapper returned an unawaited coroutine on a miss and a
  bare value on a hit, and never cached). Streaming targets (`StreamingResponse` /
  async generators) are awaited then passed through uncached by the dict/list gate.
  All current callers are sync and keep identical behavior.
- **Rate-limiter xdist reset.** A central autouse conftest fixture
  (`_reset_rate_limiter_state`) resets both `rate_limit._fallback_counts` (the
  per-window outreach fallback counter) and the slowapi `limiter` storage before AND
  after each test, killing an intermittent cross-test state leak that flaked the
  parallel (xdist) suite. Mirrors `_clear_connector_token_cache` / `_reset_ai_gate_state`.

**Test enforcement** lives in `tests/test_connectors.py`,
`tests/test_connector_rate_limits.py`,
`tests/test_sourcengine_connector.py`, `tests/test_connector_errors.py`,
`tests/test_constants.py`, `tests/test_search_streaming.py`, and
`tests/test_health_monitor.py`.

**Search fan-out performance &amp; correctness.**

- **Real cache-age scoring.** Search-result Redis cache entries now carry a
  `cached_at` timestamp; results served from cache are tagged
  `_source_age_hours` with the real elapsed age (`_cache_age_hours`) instead of
  a hardcoded `0.0`, so `score_sighting_v2`'s freshness factor reflects reality
  for cache-served rows. A live fetch still tags `0.0`.
- **Streamed results are durable.** `Sighting.requirement_id` is nullable
  (migration 196); after a live (non-cache-hit) `stream_search_mpn` run emits
  its terminal `done` SSE event, `_persist_interactive_sightings` runs via
  `asyncio.to_thread` and persists the deduped results as requirement-less
  Sightings through `_save_sightings(fresh, req=None, ...)`. Requirement-less
  saves keep vendor-card creation, scoring/evidence tiers, material-card
  upsert + `last_searched_at` stamping, and tag propagation, but skip lead
  sync, vendor-summary rebuild, and unavailability suppression (all
  requirement-scoped, NOT NULL FKs). Stale requirement-less rows are deduped
  by `(vendor_name_normalized, normalized_mpn)` — the key is set at Sighting
  construction, not just by the material-card backfill. Cache-hit streams
  never re-persist. Migration 196's downgrade fails loudly if NULL rows
  exist (documented DELETE required) rather than silently dropping data.
- **Vendor feedback reaches live sighting scores.**
  `get_vendor_feedback_adjustment` (sourcing_leads) is batched per distinct
  vendor card in `_save_sightings` — one grouped query per vendor, applied via
  `_effective_trust_score` to the vendor_score input of `score_sighting` /
  `score_sighting_v2` (clamped 0-100); a `do_not_contact` vendor's trust input
  is floored at ≤15 rather than the sighting being dropped.
- **Currency-aware price scoring.** `app/utils/currency.py` (`to_usd`, static
  approximate FX table, SCORING-ONLY — never invoicing/PO/customer-facing
  price) converts `unit_price` to USD before computing the median-price
  baseline and the per-offer price-competitiveness comparison, so listings in
  EUR/GBP/JPY/etc. no longer get compared as raw numbers against USD listings.
- **Shared single-MPN search cache.** `stream_search_mpn` now reads/writes the
  same shared `search:`-prefixed Redis cache key used by the batch search path
  (via the shared helpers `_flatten_dedupe_filter_junk` / `_aggregate_source_stats`),
  instead of maintaining its own cache.
- **Bounded AI web-search gather.** The "smart AI trigger" web-search gather in
  `_fetch_fresh` is now bounded by `settings.ai_search_timeout_s` (default 20.0,
  separate from `search_total_timeout_s` — the AI gather starts its own clock
  after conventional connectors finish). Previously bounded only by the
  connector's own 60s httpx timeout, so one slow Claude web-search call could
  hold `_fetch_fresh` open for a minute past every other connector. Pending
  tasks past the budget are cancelled and recorded in `stats_updates` with an
  "AI search budget exceeded" error.
- **Post-search persistence off the event loop.** The post-search DB write
  (`_persist_search_write`) now runs via `asyncio.to_thread` with its own
  dedicated write session (SQLAlchemy sessions are not thread-safe, so it
  cannot reuse the request session) and batched material-card existence
  checks, keeping the event loop free during the write.
- **`_build_connectors` config cache.** `_load_connector_config` now caches the
  disabled/errored source sets + batched credentials `_build_connectors`
  otherwise re-queried on every search, behind a 60s in-process TTL
  (`_CONNECTOR_CONFIG_TTL_S`) — a no-op under `TESTING=1`. Connector INSTANCES
  are still built fresh per call; `_reset_connector_config_cache()` forces
  immediate freshness after a Settings → Sources credential/status mutation.

### Browser-worker carve-out

`icsource`, `netcomponents`, and `thebrokersite` are queue-driven via
`avail-ics-worker` / `avail-nc-worker` / `avail-tbf-worker` rather than
request/response connectors. They have no
entry in `_get_connector_for_source`, so the 15-min ping loop would flip
them to DISABLED on every run. `app.constants.BROWSER_WORKER_SOURCES`
holds this set, and `run_health_checks` excludes those names from the
ping loop. Their `api_sources` row is seeded to `LIVE` + `is_active=True`
once at startup by `seed_browser_worker_sources` (see `app/startup.py`)
and the seed survives because the ping loop never touches them. Their
actual health is tracked via `IcsWorkerStatus` / `NcWorkerStatus` /
`TbfWorkerStatus` heartbeats; all three singletons are seeded at startup so
`update_worker_status()` writes are not silently dropped. Each worker
(ics, nc, tbf, and enrichment) refreshes `last_heartbeat` on **every** loop
tick via `_record_heartbeat()` at the top of the loop — so the heartbeat
reflects process liveness independent of work, and stays fresh on idle /
cap-sleep / breaker-open / off-hours paths (a liveness monitor reading
`last_heartbeat` won't false-alarm "DOWN" while a worker is merely paused).
The enrichment worker's LONG (~1h) daily-cap and circuit-breaker sleeps would
otherwise let the heartbeat lapse mid-sleep and false-alarm the watchdog, so
those two sleeps run through `_sleep_with_heartbeat()` — it splits the hour into
chunks (a third of `settings.worker_heartbeat_stale_minutes`, floored at 60s)
and re-touches the heartbeat after each chunk. Real-hang detection is preserved:
the refresh only follows a chunk the worker actually finished sleeping through,
so a wedged loop still goes silent and is caught (the chunk loop also honors the
shutdown flag so SIGTERM exits within one chunk).

**Proactive liveness watchdog.** Beyond the on-demand Connectors-page read, a
scheduler job (`app/jobs/worker_liveness_jobs.py`, registered by
`register_worker_liveness_jobs`, runs every `settings.worker_liveness_check_minutes`)
actively consumes `last_heartbeat` for all four singletons (ics, nc, tbf,
enrichment). When a worker that claims `is_running` has a heartbeat that is
NULL/never-seen or older than `settings.worker_heartbeat_stale_minutes`, or its
circuit breaker is open, it emits a debounced alert (Loguru + Sentry via
`search_worker_base.monitoring.capture_sentry_message` + Teams). Debounce state is
held per worker in the Redis-backed `intel_cache` keyed `worker_alert:<label>` with
TTL = `settings.worker_alert_debounce_minutes` (no schema/column). The
staleness + debounce decision is factored into pure functions
(`heartbeat_is_stale` / `should_alert_stale_heartbeat`) so it is unit-testable
without the scheduler or DB. The job is disabled under `TESTING` with the rest of
the scheduler. Tests: `tests/test_worker_liveness.py`.

**Worker-aware Connectors-page status.** The Settings → Connectors page must
NOT render a worker-backed source as "broken"/"no API"/"needs setup" just
because it has no direct API key. `connector_service.is_worker_backed()` (the
explicit `WORKER_BACKED_SOURCES` map: `thebrokersite`→tbf, `netcomponents`→nc,
`icsource`→ics) routes these sources through `connector_service.worker_health()`
instead of the key/credential ladder. `worker_health(row)` reads the heartbeat
singleton and returns a verdict (`healthy`, `heartbeat_age_secs`,
`last_search_at`, `problem`); a worker is **unhealthy** when the row is missing,
the heartbeat is absent or older than `settings.worker_heartbeat_stale_minutes`
(15 min, same threshold as `/api/admin/workers/status`), `is_running` is false,
or the circuit breaker is open. `connector_state()` then yields two
worker-specific states for active worker sources — `worker_active` (badge
"Worker active" + heartbeat age + last search) and `worker_down` (badge "Worker
down" + the specific problem) — or `off` when the operator has switched the
source off. Worker-backed sources are never offered the synchronous API "Test"
button (their health is the heartbeat, not a request/response probe). The
header/group live-vs-need-setup counters treat `worker_active` as live and
`worker_down` as needing attention. Logic in `app/services/connector_service.py`;
heartbeat read + enrich in `htmx_views._enrich_source` /
`_worker_status_row`; rendering in `settings/_connector_macros.html`
(`worker_detail` macro). Tests: `tests/test_connector_service.py` (pure
verdict/state) and `tests/test_connectors_settings.py` (rendered badges).

**Connectors-page classification + credential safety (2026-07-04, Phase 3).** Five
Settings-tool hardening rules on top of the worker-aware status above.
(1) **Flag connectors.** `connector_service._FLAG` (`email_mining`) classifies as
`keyless` even though it declares an env var — that var (`EMAIL_MINING_ENABLED`) is a
boolean feature flag, not a secret, so the card renders an on/off toggle + a note pointing
to the System-tab setting, never a masked key field (typing there would have encrypted a
bogus credential). Classification is name-based, so it holds on existing DBs regardless of
the seeded `env_vars`.
(2) **Webhook connectors.** `teams_notifications` is NOT in `_SCOPES` — it needs a
`TEAMS_WEBHOOK_URL` field, so it classifies as `key` and its card exposes an
enter/rotate field (was: "No key required", no field). `azure_oauth` remains the lone
`_SCOPES` (consented-Graph) connector.
(3) **Full masking.** `htmx_views._build_connector_field(..., mask_fully=True)` renders
dots-only (no `plaintext[-4:]` tail) for `browser_login` control types — the TBF/ICS
worker account passwords are reused human passwords, so even a 4-char tail in the DOM is a
leak. Keyed API sources keep the last-4 identifier (`credential_service.mask_value` is
unchanged).
(4) **Last-checked + Test feedback.** The per-source Test persists `last_success` and
emits an `HX-Trigger showToast` (status + count + elapsed, phase-0); the card now also
renders "Last checked `<timeago>`" from `last_success` on non-worker cards.
(5) **Paid-quota guard.** Test spends real provider quota; Test-all previously bypassed
the per-source `5/min` cap entirely, so it now runs under a per-user `3/min`
(`_TEST_ALL_MAX_PER_MIN` via `rate_limit.check_rate_limit`, friendly error toast on
exceed) with a cost note beside the button + `title` tooltips on both Test buttons;
`data-loading-disable` debounces in-flight.
Separately, the startup **`connector_status.log_connector_status`** now reports readiness
from DB-first credential resolution + `api_sources.status` health (was: raw `settings.*`
env-var presence, which diverged from what searches actually resolve). Logic in
`app/services/connector_service.py` + `app/routers/htmx/settings.py` +
`app/connector_status.py`; rendering in `settings/_connector_macros.html` /
`connectors.html`. Tests: `tests/test_connector_service.py`, `tests/test_connector_status.py`,
`tests/test_connectors_settings.py`.

**HTML structure-hash drift alert (2026-07-04, Phase 2).** Each browser worker
fingerprints the results HTML it parses via
`search_worker_base.monitoring.check_html_structure_hash` and warns (Loguru + Sentry)
when a page's structure is one it has not seen — an early signal that a supplier
changed its markup and the parser may need updating. The fingerprint hashes only the
sequence of tag NAMES (open/close), never attributes: attribute values (`class`, `id`,
`data-*`, inline `style`) vary per row and per part, so folding them in made almost
every page read as a "layout change" — spamming Sentry and growing the stored
per-component hash set without bound. Genuine layout changes (a table becoming a list,
a wrapper appearing) still alter the tag sequence and are still caught. The stored set
is additionally capped at `_MAX_STRUCTURE_HASHES` (evict-arbitrary-at-cap) as a hard
bound. Tests: `tests/test_search_worker_monitoring.py`.

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

The Vendors tab is a **fitted-column table — Vendor | Qty | Best Price | Score | ⋯**
where each vendor is its own `<tbody>` (a summary row carrying exactly one `<td>`
per `<th>`, plus an expandable intel drawer in a sibling `<tr><td colspan="5">`).
Status pill, phone (tel link) and the OOO / overlap / "via SUB-MPN" badges live
inside the Vendor cell; **every action lives in the row's ⋯ kebab** (Build RFQ /
Mark Unavail / Convert to offer for available vendors; Mark available / Verify in
the unavailability states). A test guards the header↔cell count
(`tests/test_panel_column_alignment.py`). "Convert to offer" opens the modal
prefilled from the VendorSightingSummary. The modal and the requisitions add-offer
form share one field grid (offers/_offer_form_fields.html). Offer creation logs
OFFER_CREATED, so converted/entered offers appear in the Activity tab automatically.

Vendor rows (_vendor_row.html) also carry a row-level status treatment keyed off
the server-computed vendor status `vs` (precedence resolved in
app/services/sighting_status.py — offer-in dominates unavailable): offer-in rows
get an emerald tint + emerald badge. Unavailability rendering is NOT keyed off the
row's `is_unavailable` boolean alone — the durable `vendor_part_unavailability`
record is the authority, and the row renders one of three states (suppressed rose
/ expired advisory / possible-restock chip). See § 2d.

NOTE: the two creation paths historically wrote Offer.normalized_mpn differently
(create_offer = normalize_mpn_key, add_offer = normalize_mpn); add_offer was
fixed to use normalize_mpn_key, and the part query matches both forms for safety.

### 2c-bis. Offer Qualification Capture (`app/services/offer_qualification.py`)

Standardized condition-driven qualification for buyer-entered offers. Every offer
has a **condition spine** (`new` / `new_no_pkg` / `pulls` / `refurb`) that drives
per-condition validation, a system-composed standardized note, a status/meter, and
optional vendor requests.

**Service API (all pure, no I/O except `apply_qualification` and `prefill_from_vendor`):**

| Function | Purpose |
|----------|---------|
| `validate_essentials(condition, data)` | Returns a list of error strings for missing per-condition essentials. Empty = OK. |
| `compose_note(condition, data)` | Builds the standardized human-readable note string from condition + data dict. |
| `meter(condition, data, has_images)` | Returns `(filled, total)` int tuple of qualification item counts. |
| `compute_status(condition, data, has_images)` | Returns the `QualificationStatus` string (`unset`/`incomplete`/`essentials`/`complete`). |
| `apply_qualification(offer)` | Composes `qualification_note` + `qualification_status` onto an Offer ORM object. **Never raises** — the gate lives in the buyer handlers. |
| `normalize_offer_condition(raw)` | Normalizes raw condition strings (incl. legacy `used`→`pulls`) to the `OfferCondition` vocabulary. |
| `prefill_from_vendor(db, vendor_name_normalized)` | Vendor-memory (#8): pulls stable answers (`country_of_origin`, `refurbished_by`, `terms`) from the vendor's most-recent offer. |
| `request_template(kind, mpn)` | Returns the RFQ-back request text for a given `kind` (`images`/`fpq`/`cert`/`pkg_qty`). |
| `essentials_data(...)` | Builds the canonical `data` dict accepted by `validate_essentials`/`meter` from named keyword args. |

**Gate placement (important).** The hard-block lives **only** in the buyer-facing handlers:

- `app/routers/sightings.py` (sightings create/update): calls `validate_essentials`; on
  errors, re-renders the modal with error messages at HTTP 200 (HTMX swap).
- `app/routers/htmx_views.py` (add/edit offer): calls `validate_essentials`; on errors,
  returns HTTP 400 with the form re-rendered.

The **canonical builders** (`crm.offers.create_offer` / `update_offer`) only call
`apply_qualification()` to compose the note+status — they **never block**. This means
API/AI offer ingestion (inbox monitor, email-parsed, proactive) is unaffected: those
paths may produce `qualification_status='incomplete'` but are never rejected.

**Vendor memory prefill (#8).** When the sightings offer modal opens (GET `.../offer-form`),
the router calls `prefill_from_vendor(db, vendor_name_normalized)` and passes the returned
dict to the template as Alpine initial-state `x-init` values. The buyer sees pre-populated
`refurbished_by` / `terms` / `country_of_origin` from the vendor's most-recent offer — no
extra form step required.

**One-tap vendor requests (#7).** `POST .../offers/{offer_id}/request` (scoped to
the path's `requirement_id` to prevent IDOR) accepts `kind` ∈ `REQUEST_KINDS`
(`images`/`fpq`/`cert`/`pkg_qty`). Logs the pending request into
`offer.qualification["requests"]` (status `"pending"`) and drafts the request text via
`request_template`. This route is `require_buyer`-only (NO Graph token) so logging
never 401s on an expired M365 token. The `offer_id` is validated against the
`requirement_id` path parameter so a buyer can only request on their own requirement's
offers.

**Sending a logged request (#7 send).** `POST .../offers/{offer_id}/request/{index}/send`
(`sightings_offer_request_send`) sends a single PENDING entry as a real RFQ-back email.
This is a SEPARATE, token-bearing route: it adds `require_fresh_token` on top of
`require_buyer` so the actual send fails loudly on an expired token while LOGGING never
does. `{index}` addresses the append-only `qualification["requests"]` list (stable
index). It resolves the vendor's best contact email via `_best_contacts_by_card`
(mirroring the batch send-inquiry path), drafts the body with `request_template`, and
hands ONE vendor group to `send_batch_rfq` with the SCALAR `requisition_id`
(single-requisition mode — passing the scalar AND a parts-map raises `ValueError`).
Because `send_batch_rfq` commits internally and can expire the session, the entry-status
update is applied AFTER it returns against a freshly re-fetched offer (and the mutated
entry is re-slotted as a fresh nested dict so the JSON column flush is detectable — a
shallow copy would mutate the committed baseline and persist nothing). Outcomes per
entry: `sent` (records `contact_id`/`sent_at` and logs an `rfq_sent` activity, but does
NOT auto-progress the sourcing status — one clarification is not a full RFQ round),
`skipped` (no contact email, OR `offer.requisition_id is None` since
`Contact.requisition_id` is NOT NULL — guarded BEFORE any send), `failed` (records the
error). Idempotent: an already-`sent` entry is a no-op. The template shows a per-PENDING
**Send** button next to each pending request pill (`status_colors` adds `skipped`/`failed`
states).

**Live badge/meter vs. stored snapshot.** `Offer.qualification_summary` (property)
recomputes status+meter live from the current column values — the display badge always
reflects reality. `qualification_status` is a refresh-on-save snapshot used for
filtering/reporting. An image attachment added after the last save bumps the live
meter but not the column until the next save.

### 2d. Vendor+part unavailability (durable knowledge + temporal policy)

"Unavailable" is learned vendor intelligence about a **(vendor, part)** pair, not
a row attribute: a durable `vendor_part_unavailability` record (reason + note +
provenance — schema in APP_MAP_DATABASE.md) that outlives the scraped Sighting
rows it was marked from. `Sighting.is_unavailable` is demoted to a **render
cache**; the predicate `is_active(record, now)` in
`app/services/vendor_unavailability.py` is the single authority every read
surface uses. All business logic lives in that service; the sightings router
stays thin.

**Mark (and re-arm).** Row "Mark Unavail" → `$dispatch('open-modal')` →
`GET /v2/partials/sightings/{requirement_id}/unavailable-form` (reason radios
from `UnavailabilityReason`, optional note, condition selector — blank = NULL
all-conditions catch-all; the selector is shown only for condition-specific
reasons: bought_by_us/sold_elsewhere/broken) → `POST .../mark-unavailable`:

```
sightings_mark_unavailable (router; reason required + enum-validated, else 400)
    |
    v
record_unavailability(db, requirement, vendor_name, reason, note, user,
                      condition=None)
    |
    +---> ValueError on empty vendor norm or zero derivable MPN keys
    |       --> router maps to 400 JSON error; NOTHING written (no ActivityLog)
    |
    +---> reason→condition coercion: condition-specific reasons
    |     (bought_by_us/sold_elsewhere/broken) store normalize_condition(passed);
    |     agnostic reasons (not_really_there/different_part/other) always store NULL
    |
    +---> UPSERT one record per (derivable key, condition) pair (matched-sighting
    |     MPN keys + primary-MPN key): reason/note/created_by/created_at refreshed;
    |     per-(key,condition) qty_at_mark snapshot (keep-old-on-NULL);
    |     released_at/release_trigger NULLed; requirement_id provenance refreshed
    |
    +---> stamps is_unavailable=True on the vendor's sightings via the ONE
    |     shared matching helper (NULL-norm legacy fallback — never a bare
    |     column-equality filter)
    |
    +---> ONE ActivityLog entry (vendor, reason label, note, MPN — never a
          None MPN: falls back to a matched MPN or "requirement #<id>")
```

Re-POSTing for an already-marked vendor is the **re-arm** path (upsert refresh —
one click buys a fresh quiet window; the just-seen qty becomes the new O2
baseline). There is NO separate verify endpoint: the advisory/restock "verify"
affordance maps onto re-arm (the mark-unavailable modal) and clear
(mark-available).

**Clear.** `POST .../mark-available` → `clear_unavailability`: DELETEs ALL
condition variants (NULL and specific) for the matching (vendor, key) pair — the
single affordance means "vendor is generally available again." Predicate: vendor
norm AND (key in the requirement's current keys OR `requirement_id ==
requirement.id` — the provenance arm catches zombie records whose key no longer
matches). Unflags the vendor's sightings via the same shared helper, writes an
ActivityLog entry. DELETE is deliberate (explicit human "forget it"); history
survives in the activity timeline. Auto-expiry and overrides O1/O2 never delete.

**Feedback.** Both routes re-render the detail panel with an appended OOB
toast fragment (success: "Marked {vendor} unavailable — {reason label}" /
"{vendor} marked available again"). On the 400 paths, htmx callers
(`HX-Request`) get the re-rendered detail plus the ACTIONABLE message as an
error toast (the global `htmx:responseError` handler only shows a generic
line); non-htmx/API callers keep the 400 JSON contract.

**Temporal policy — "Two Windows, Real Proof"**
(`docs/superpowers/specs/2026-06-10-unavailability-temporal-policy.md` is
authoritative). Suppression is read-time bounded per reason class: `is_active` =
`released_at IS NULL AND (reason == different_part OR created_at >= now −
window(reason))` — pure Python cutoffs, no cron, no lazy writes. Windows: **30d**
for lot reasons (bought_by_us/sold_elsewhere/broken/other; knob
`unavailability_suppress_days`), **180d** for the phantom-listing reason
(not_really_there; knob `unavailability_listing_suppress_days`), **never** for
different_part (identity knowledge — hard-coded, not a knob).

**Condition-aware suppression (migration 171).** A sighting/offer of canonical
condition X (X = `normalize_condition(stored)`, may be None) is suppressed iff
an `is_active` record exists where `record.condition == X OR record.condition IS
NULL`. A None-condition sighting (unknown/unlabeled) matches ONLY NULL catch-all
records — never a specific-condition record — so a "bought by us (new)" mark
does not suppress unlabeled listings (anti-over-suppression). Suppression check
is read-time: `suppressed_by(record, sighting_condition)`.

While a record is active, fresh rows are evaluated by overrides **dispatched on
the row's source class** (mutually exclusive — never priority order): LIVE
(digikey/mouser/element14 or `is_authorized`) → **O1**: qty > 0 and ≠
`qty_at_mark` leaves the row unstamped (advisory; applies to ALL reasons);
HUMAN_DIRECT (`email_attachment` only) → **O3**: qty > 0 releases the record
(`released_at=now`, `release_trigger='vendor_email'`, one ActivityLog line);
everything else is listing-class → **O2**: fresh qty > snapshot AND ≥ snapshot ×
`unavailability_qty_jump_factor` (2.0) leaves the row unstamped with no record
mutation (stateless, self-healing). O2/O3 — and the offer hook — are disabled
for different_part. **Offer-release hook (condition-aware):** an offer/email
proof of condition X releases the matching X record AND the NULL catch-all (the
all-conditions record is also resolved); an unknown-condition proof releases all
active non-different_part records for the (vendor, key). The hook is invoked at
the offer entry/save/approval sites (canonical `create_offer` incl. the sightings
route that delegates to it, manual add-offer, the save-parsed-offers route,
`save_freeform_offers`, pending-review approve, plus its three approval twins:
the htmx review-queue promote, the T4→T5 API promote, and the requisition
offers-tab review approve) after the offer persists (same transaction) —
`release_trigger='offer_received'`; auto-created offers (inbox monitor, excess
matching) and clone paths never release. Expired/released records render as
labeled advisory states, never silent suppression.

**Re-stamping at every sighting-persistence path.** Each of the eight code paths
that persist fresh Sighting rows calls `apply_to_fresh_sightings(db,
requirement, rows)` — which embeds the O1/O2/O3 matrix, so every path gets
policy behavior for free — in its OWN session, right where the rows are created:

1. `app/search_service.py` — after the fresh-Sighting construction loop that
   follows the connector-aware delete (inside search's separate write session).
2. `app/services/ics_worker/sighting_writer.py` — async ICS browser-worker save loop.
3. `app/services/nc_worker/sighting_writer.py` — same, NetComponents worker.
   `app/services/tbf_worker/sighting_writer.py` — same, The Broker Forum worker (ACTIVE: logs in with member creds and captures the real seller `vendor_name` + `vendor_phone` from the authenticated listing — logged-out, TBF anonymizes the seller to "TBS Member"). The session/circuit-breaker key on a POSITIVE, fail-safe logged-in marker (the "Sign out" control present, `session_manager.LOGGED_IN_MARKER`); never on "TBS Member" text, which is the logged-OUT anonymized company label.
4. `app/routers/sources.py` — email-attachment import (ALSO the HUMAN_DIRECT/O3
   release path: a buyer-routed attachment with qty > 0 releases instead of
   stamping). A RE-SENT attachment that hits the dedup key refreshes the
   existing row's qty/price from the new parse and joins the apply batch, so
   the O3 release still fires — never a silent skip.
5. `app/routers/htmx/search_views.py` — add-to-requisition picker (deliberately stamped;
   the user can Mark available to override).
6. `app/jobs/inventory_jobs.py` — excess-list sighting creation (rows grouped
   per requirement before calling).
7. `app/routers/requisitions/requirements.py` — `import_stock_list` manual
   vendor stock-list import (rows grouped per requirement before the commit).
8. `app/services/search_worker_base/queue_manager.py` — the ICS/NC
   cross-requirement dedup, which clones prior sightings onto a NEW
   requirement (applied before its commit).

**Reader-authority rule.** The record predicate is the only authority; the row
flag is a render cache that every reader reinterprets:

1. **Row render** (`_vendor_row.html`, via the annotated `unavailable_intel`
   context from `unavailability_for_requirement`): active record + stamped row →
   suppressed (rose tint, reason/note/age, only action = Mark available);
   non-active record → expired advisory (normal row, gray italic history hint,
   amber "Verify availability" link, full action trio — Mark Unavail doubles as
   re-arm); active record + row left unstamped by O1/O2 → possible restock
   (bordered emerald chip, qty delta, emerald "Verify restock" link; RFQ stays
   gated server-side).
2. **Status pill** (`compute_vendor_statuses` Batch 4): vendor is `unavailable`
   iff (an active record matches AND the vendor has NO unstamped row) OR (no
   record at all AND all rows flagged — true legacy). Rows-win: one
   override-surfaced row flips the pill; an expired record's stale stamped rows
   no longer pin it. The legacy all-rows-flagged branch applies ONLY to vendors
   with no record. Precedence: blacklisted > offer-in > unavailable > contacted
   > sighting — contacted is a step; unavailable is its answer: a mark made
   after contacting must be visible.
3. **RFQ:** active records only (next paragraph).

Races across the eight writers leave at most a stale flag that the next render
reinterprets correctly — no reconciliation pass, no read-path writes.

**RFQ exclusion (active NULL-condition records only, with visible skip).** The
RFQ vendor modal (`sightings_vendor_modal`) excludes vendors in
`excluded_vendor_norms(db, requirements)` — vendors with an ACTIVE **NULL-condition**
(all-conditions catch-all) record whose key matches a selected requirement's
primary-MPN key. Condition-specific records (e.g. "bought by us — new") do NOT
exclude the vendor from RFQ, so unlabeled/different-condition offers can still be
solicited (anti-over-suppression by design). Excluded if unavailable for ANY
selected part (deliberately conservative). Expired/released/cleared → the vendor
returns to suggestions. `sightings_send_inquiry` and `sightings_preview_inquiry`
re-validate the submitted vendor names against the same active NULL-condition set
at request time (closing the TOCTOU the modal filter alone leaves open): excluded
vendors are dropped from the send AND visibly reported ("Skipped (marked
unavailable): …" in the result toast + the `X-RFQ-Unavailable` count header) —
never a silent drop. Override-surfaced (possible-restock) rows do NOT re-enable
RFQ while the record is active; the exits are window expiry, offer/email release,
or manual clear.

## 3. RFQ Email Sending

```
Browser POST (send RFQ)
    |
    v
Two callers, ONE canonical send path:
  - htmx_views.rfq_send            (requisition page; legacy scalar requisition_id)
  - sightings.sightings_send_inquiry (bulk composer; requisition_parts_map)
    |
    v
email_service.send_batch_rfq
    |
    +---> graph_client.py --> Microsoft Graph API
    |       +---> POST /me/sendMail — ONE email per vendor; subject carries one
    |       |     [ref:{req_id}] token per involved requisition, ascending id
    |       |     ([AVAIL-{id}] is the legacy spelling, still matched on replies)
    |       +---> Sent-folder lookup -> graph_message_id + graph_conversation_id
    |
    +---> DB: INSERT contacts (type=email, status=sent|failed) — ONE row per
    |     (requisition, vendor) pair; each parts_included scoped to its OWN
    |     requisition; all of a vendor's rows share that one email's
    |     graph_message_id / graph_conversation_id
    |
    +---> tag propagation: material_card ids collected from ALL involved
    |     requisitions' requirements (one pass per unique vendor)
    |
    v
back in the sightings router:
    +---> activity_service.log_rfq_activity per requirement (rfq_sent),
    |     only for vendors actually sent
    +---> status auto-progress OPEN -> SOURCING per involved requisition
    +---> X-RFQ-Sent / X-RFQ-Total / X-RFQ-Skipped / X-RFQ-Unavailable headers
```

**Per-requisition Contact fan-out (cross-requisition bulk composer).** The
sightings vendor modal's selection can span requisitions. There is no collapse
to one arbitrary requisition anywhere in the path (the old
`next(iter(requisition_ids))` collapse in both preview and send is gone): the
router builds a `requisition_parts_map` ({requisition_id: parts}) and
`send_batch_rfq` takes it as an **additive** parameter. One email per vendor
still covers ALL selected parts (the point of a bulk composer); the
multiplicity moves to `Contact` rows — one per (requisition, vendor) pair, each
`parts_included` holding only that requisition's parts, all sharing the email's
graph ids. No schema change (Contact keeps its singular `requisition_id`).
A **legacy shim** keeps the scalar-`requisition_id` call shape byte-identical
(one Contact per vendor, parts from the vendor group) — `htmx_views.rfq_send`
was not touched.

**Preview/send lockstep.** `sightings_preview_inquiry` renders the exact
multi-token subject the send will produce — one `[ref:{id}]` per involved
requisition, ascending requisition id — so preview and send can never diverge.
The **compose step** also displays this tagged subject read-only
(`compose_subject` in the modal context, built with the same token logic) so the
buyer sees exactly what sends before previewing, even after a modal refresh. The
preview **parts body** groups by requisition when the basket is cross-req:
`requisition_parts_grouped` ({req_id, parts}, ascending) + `is_cross_req`
(`len(requisition_ids) > 1`) drive a `REQ-{id}` subhead per requisition; single-req
keeps the flat inline list (`parts_list` retained for that path).

**Subject-token single source of truth.** `shared_constants.RFQ_SUBJECT_TAG_RE`
(`[ref:{id}]` current, `[AVAIL-{id}]` legacy) is the ONE pattern; the duplicate
`AVAIL_TOKEN_RE` in `email_mining.py` was removed (the miner's sent-scan check
is presence-only and points at the shared pattern). Extractors use
`re.findall`, never `.search().group(1)`, so a multi-requisition subject
attributes to ALL token requisitions: the sent-folder scan
(`email_jobs.scan_sent_folder`) writes one `email_sent` ActivityLog per token
requisition (rows share the message's `external_id`, so the dedup check still
skips re-scans), and inbox Tier-2 matching resolves every token (next section).
Reply attribution to Contacts is Tier-1 fan-out — see section 4.

**Vendor panel — four selection sources.** The modal's vendor checklist
(`vendor_modal.html`, all rows joining the same Alpine selection state — see
the `rfqVendorModal` section below) is fed from four sources:

1. **Coverage-ranked suggestions (default) — coverage-DISCOVERY, includes cardless
   vendors.** `sightings_vendor_modal` runs the shared
   `_coverage_ranked_vendor_rows` query over `VendorSightingSummary` (VSS) filtered
   to the selected requirements, **OUTER-joined** to `VendorCard` via
   `_vss_vendor_card_join()` — an `or_()` coalesce whose PRIMARY branch is the
   `vendor_card_id` FK (indexed `ix_vss_vendor_card`), with a
   `lower(trim(vendor_name)) == normalized_name` FALLBACK for NULL-FK rows (the
   NULL-FK guard on the fallback prevents double-matching FK rows by name). The
   outer join means a VSS row that matches **no card at all** yields `card=None`
   and is **surfaced as a CARDLESS vendor**, not dropped — the composer is a
   coverage-discovery surface ("who has my parts?"), so every vendor with sightings
   on the selected parts appears (on live data ~112 of 120 distributors were
   previously invisible behind the old inner join). The result is **grouped in
   Python** (not SQL `GROUP BY` — sidesteps the GROUP-BY-entity SQLite/PG
   portability seam; VSS is a few hundred rows) by **`card.id` when carded, else
   `normalize_vendor_name(vendor_name)` when cardless** (the canonical normalizer,
   so two name variants of one cardless vendor merge into one group and the key
   matches the exclusion set). Per group it accumulates distinct-requirement
   `covered_count`, the mean of non-null scores (`avg_score`), a representative
   card (None if all cardless), and a deterministic display name
   (`card.display_name` if carded, else the lexicographically-min raw
   `vendor_name`). **Suffix-mismatch de-dup:** after grouping, any cardless group whose
   key equals a CARDED group's `normalize_vendor_name(display_name)` is folded into that
   carded group (covered requirements union, scores merged, carded card/`has_contact`
   kept) — the fallback SQL join matches NULL-FK rows by raw `lower(trim(vendor_name))`
   while grouping keys on `normalize_vendor_name`, so a NULL-FK `"Acme Inc"` would
   otherwise split off a duplicate cardless `"acme"` row beside card `"acme"`. Blacklist
   drops apply **only when carded**; `excluded_vendor_norms`
   unavailability filtering is by normalized name (cardless group key; carded
   belt-and-braces re-check). Each row exposes **`has_contact`**, which **mirrors
   the send-path skip EXACTLY**: True iff `card is not None` AND some `VendorContact`
   for that card has a non-empty `email` (resolved in ONE batched query via
   `_cards_with_resolvable_email`, no N+1; filters `email != ''`) — i.e. `has_contact`
   true ⇔ `sightings_send_inquiry` / `sightings_preview_inquiry` would NOT skip the
   vendor. The send path's `_best_contacts_by_card` orders empty-OR-NULL-email rows FIRST
   (lose last-wins) via `or_(email.is_(None), email == "").desc()`, so a high-confidence
   `''`-email contact can't win and resolve `vendor_email=''` → skip while the badge said
   contactable (the empty-email asymmetry). `card.emails` is deliberately NOT consulted
   (the send path resolves the address only from `VendorContact` rows), so the "no contact
   on file" badge never lies.
   Order: `covered_count` desc, then `has_contact` desc (contactable above
   equal-coverage non-contactable), then engagement desc nullslast, then a stable,
   deterministic tiebreak — `(0, card.id)` for carded (numeric id order, matching main;
   NOT lexicographic `str(key)`, which put "10" before "2"), `(1, group_key_str)` for
   cardless (carded ties first, cardless after); cap 20. Each row shows an `N/M parts` chip with the covered
   MPNs in `title` (computed server-side, plain-column aggregates — SQLite+PG safe),
   keyed by the group key (card id carded, normalized name cardless).
   - **Non-contactable rows** (cardless OR carded-but-no-resolvable-email) render a
     neutral `bg-gray-100 text-gray-500` **"no contact on file"** badge, a **disabled
     checkbox** (reusing the excluded-vendor disabled pattern — you can't select what
     the send path would skip), and an **"Add contact"** link. "Add contact"
     (`addContactFor` in `rfqVendorModal`) pre-fills + reveals the existing inline
     "Add new vendor" form (source 4 below) with the vendor's display name and focuses
     the email input — the buyer types the known email and the existing `composer-vendor`
     POST creates the card + `VendorContact`. **No new endpoint, no schema change, no
     bulk CRM writes.** Contactable rows carry an enabled checkbox + engagement/
     response badges.
   - **Lead time on EVERY row.** `SuggestedVendor.lead_time_days` (min of
     `VendorSightingSummary.best_lead_time_days` across the group, computed in
     `_coverage_ranked_vendor_rows`) now renders a `{N}d lead` span on **all three**
     row variants — contactable non-DNC, DNC, and no-contact — not just the
     contactable one (`{% if v.lead_time_days is not none %}`). Template-only on the
     DNC/no-contact branches (the data already flowed through).
   - **Score tooltip.** The contactable-row `Score:` span carries a `title=`
     explaining vendors are **ranked by responsiveness** (engagement score = reply
     rate × recency when present, else the overall vendor quality score) — native
     HTML attribute, no new Tailwind class.
   - **Commodity-segmented engagement chip.** When all selected requirements share
     ONE `material_card.category` (`current_commodity`), `sightings_vendor_modal`
     runs **one bounded query** `ActivityLog → Requirement → MaterialCard` grouped by
     `(vendor_card_id, direction)` filtered to that commodity, producing
     `commodity_signals` ({card_id: {outbound, inbound}}). The contactable row then
     shows an `{inbound}/{outbound} {commodity}` chip (rendered only when
     `outbound > 0`) with a "For {commodity}: N reply / M sent" tooltip. Read-only —
     **no schema change, no ranking change**; the query is skipped entirely when no
     vendor is carded or the basket spans >1 commodity.
   - **Deferred follow-up (tracked, not built):** `vendor_email` is 100% NULL on
     sightings — contact emails live only on cards. Capturing vendor contact emails at
     **scrape/enrich time** is the real long-tail lever that would make most cardless
     vendors instantly RFQ-able; it is the flagged pre-rollout follow-up, deliberately
     out of scope here.
2. **Affinity on demand.** A "Suggest more vendors" button hx-gets
   `GET /v2/partials/sightings/vendor-affinity?requirement_ids=…`, which runs
   `find_vendor_affinity` once per UNIQUE primary MPN. The service is SYNC with
   a blocking Anthropic L3 call inside, so each call runs via
   `asyncio.to_thread` (with its own short-lived `SessionLocal` — sessions
   never cross the thread boundary) gathered under an `asyncio.Semaphore(3)`;
   it is never called bare from the async route. Results are merged/deduped by
   vendor keeping the highest confidence, dropping already-suggested (same
   coverage query) and unavailability-excluded vendors, capped at 10; rows
   render a bordered indigo "affinity" chip + confidence % + reasoning in
   `title`. The button lives INSIDE its own swap target
   (`#rfq-affinity-section`), so the response replaces it — a second click
   cannot duplicate rows. `score_affinity_matches` (`vendor_affinity_service.py`) now
   takes an optional `db` and multiplies the AI base confidence by a behavioral
   multiplier from `VendorCard`: `response_rate` (+/-0.20), `ghost_rate` (-0.25),
   `cancellation_rate` (-0.15); multiplier clamped `[0.5, 1.5]`, final confidence
   re-clamped to the existing `[0.30, 0.75]` band. `db=None` (no card lookup) leaves
   the multiplier at 1.0.
3. **Any-vendor autocomplete.** A debounced input against the existing
   `GET /api/autocomplete/names` (vendors filtered client-side from the mixed
   response; the endpoint is not forked). Picking a result POSTs
   `composer-vendor` (below) and appends the returned checked row.
4. **Inline vendor creation.** An "Add new vendor" mini-form (name required;
   website + email optional) POSTs `POST /v2/partials/sightings/composer-vendor`,
   which calls `check_vendor_duplicate` from `app/services/vendor_duplicates.py`
   (direct service call — never loopback HTTP; the same function backs
   `GET /api/vendors/check-duplicate`). Duplicate semantics are
   **exact-match-only**: an exact normalized-name match (score 100) is the one
   confident duplicate → the EXISTING vendor row is returned with a "matched
   existing vendor" notice and no new DB row; fuzzy hits (pg_trgm with a
   rapidfuzz fallback, score >= 80) are suggestions, never auto-dedup.
   Otherwise it creates a minimal `VendorCard` (+ `VendorContact` when an email
   was given) and fires `_background_enrich_vendor` post-commit (same pattern
   as materials/vendor_contacts). Empty name → 400 JSON error. If the resolved
   vendor is unavailability-excluded for the selected parts, the row renders
   the rose "marked unavailable" chip with a DISABLED checkbox (send-time
   re-validation stays the backstop).

Every HTMX swap for these sections targets a stable-id sub-container INSIDE the
`x-data='rfqVendorModal(...)'` wrapper (`#rfq-affinity-section`,
`#rfq-added-vendors`) — never the wrapper itself, which would re-init the
Alpine component and wipe runtime selection state.

The affinity and composer rows carry Alpine directives (`:checked='isSelected(...)'`,
`@change='toggleVendor(...)'`, `x-init='selectVendor(...)'`) that bind to the
surrounding `rfqVendorModal` scope, and **htmx innerHTML swaps do not reliably
auto-run Alpine on new nodes**. So both swap targets are explicitly
`Alpine.initTree`-d: `#rfq-affinity-section` is in the `htmx:afterSwap` allowlist
(`htmx_app.js`, alongside `lead-drawer-content` / `rq2-table`), and the composer row
is `initTree`-d by `_addComposerVendor` after its `insertAdjacentHTML`. Without this
the checkboxes are inert and ticked vendors never enter `selectedVendors` / never get
sent.

## 4. Inbox Monitoring & Response Parsing (Background Job)

```
APScheduler (every 30 min)
    |
    v
email_jobs.py -> inbox_monitor()
    |
    v
email_service.poll_inbox()
    |
    +---> graph_client.py --> Graph API: GET inbox messages
    |       (delta query incremental sync when available; top-50 fallback —
    |        ALL messages fetched, matching happens locally below)
    |       delta_query contract: fetched items are NEVER dropped — when the
    |       per-run max_items budget is hit mid-round it stops paging and
    |       returns the current @odata.nextLink as the resumable state token
    |       (persisted into SyncState.delta_token exactly like a deltaLink;
    |       both are opaque Graph URLs, so backlogs > cap drain across runs
    |       instead of stalling). Because the nextLink is resumable, an
    |       UNBOUNDED initial round would drain the whole mailbox history —
    |       so message-delta call sites pass initial_lookback_days, which adds
    |       "$filter=receivedDateTime ge {ts}" (the only $filter Graph supports
    |       on message deltas; baked by Graph into every subsequent link, max
    |       5,000 msgs per filtered round) to the first request: poll_inbox and
    |       scan_sent_folder use settings.inbox_backfill_days; EmailMiner
    |       scan_inbox/scan_sent_items use their own lookback_days (same window
    |       as their search fallbacks). /me/contacts/delta stays unbounded —
    |       the filter is unsupported there and an address book is finite.
    |       Page size via "Prefer: odata.maxpagesize" (max_page_size param,
    |       keeps IdType="ImmutableId"). An {"error": ...} page raises typed
    |       GraphAPIError (also from get_all_pages) instead of reading as an
    |       empty page — callers log + fail the run and the token is NOT
    |       advanced. poll_inbox failure ladder: 410/GraphSyncStateExpired is
    |       the ONLY case that clears the token (then full-scan fallback);
    |       GraphAPIError keeps the token and falls back (401/403 re-raise);
    |       any other exception (transient/network) keeps the token and
    |       re-raises so the poll is recorded as FAILED — and the top-50
    |       fallback itself raises GraphAPIError on an {"error": ...} body
    |       instead of reporting a successful empty poll.
    |
    +---> reply matching (first tier that hits wins; exact before fuzzy):
    |       Tier 1: graph_conversation_id (global, exact) — matches ALL
    |               Contacts sharing the thread: a cross-requisition RFQ
    |               writes one Contact per (requisition, vendor) on ONE
    |               conversation, so conv_id_map is dict[str, list[Contact]]
    |               and the reply is attributed to every one of them
    |       Tier 2: subject [ref:{id}]/[AVAIL-{id}] tokens — re.findall over
    |               RFQ_SUBJECT_TAG_RE; every token's (req_id, sender email)
    |               pair is resolved via req_email_map (unique keys under the
    |               per-requisition fan-out); tokens with no email match still
    |               assign the first token's requisition
    |       Tier 2.5 (RS-4): RESELL outreach — a buyer replying to an offered-OUT
    |               excess list (the trader->buyer inverse of the RFQ). Runs ONLY
    |               when no Contact matched (Tiers 1-2), and BEFORE the fuzzy
    |               email/domain tiers (which now also yield to it), reusing the
    |               already-built resell_outreach_service._match_outreach
    |               (graph_conversation_id then graph_message_id, both stamped on
    |               ExcessOutreach at send time via migration 133)
    |       Tier 3: sender email -> most-recent contact (USER-SCOPED,
    |               single-contact fallback BY DESIGN — untokenized replies)
    |       Tier 4: sender domain (USER-SCOPED, same single-contact design)
    |
    +---> DB: INSERT vendor_responses (raw email) — exactly ONE per message
    |     (it is per-message, not per-requisition); contact_id anchors to the
    |     first matched contact; _progress_contact_status then advances EVERY
    |     matched contact, so all involved requisitions' rows progress.
    |     status='matched' when EITHER a Contact OR a resell row matched.
    |       +---> activity_service.py: log_email_activity() --> DB: INSERT activity_log
    |       |     (event_type='email', direction='inbound', activity_type='email_received';
    |       |      dedups on external_id=message_id) so inbound vendor replies
    |       |      appear on the requisition Activity tab
    |       +---> [RS-4 resell hit] resell_outreach_service.record_response(commit=False)
    |             INSIDE the per-message savepoint: advances the ExcessOutreach row(s)
    |             sent->responded (offer extraction stays MANUAL — see the resell
    |             "Convert to offer" quick-add, so has_offer=False here, never ->bid),
    |             then _log_inbound_reply_activity writes ONE excess_list_id-scoped
    |             inbound activity_log (dedup on external_id+excess_list_id, so a
    |             per-line campaign sharing one conversation logs the reply once and
    |             never collides with the requisition-side log_email_activity row).
    |             A PURELY-resell reply (no Contact) skips AI parsing (never billed).
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

**Email-mining daily budget cap (Wave 6).** The inbox-parse path turns every pending
vendor reply into one fast-tier Claude request — `_submit_parse_batch` submits them to the
Anthropic Batch API with `cost_bucket="email_mining"` (or, if submit raises,
`_parse_sequential_fallback` calls `parse_response_ai` per reply). Spend was *metered*
(`claude_usage:email_mining:{tier}:{metric}:{date}` via `_meter_usage` on results poll —
readout `python -m app.management.enrichment_spend --bucket email_mining`) but nothing
*gated* it, so a large first-time inbox backfill could dispatch thousands of requests
unbounded. `poll_inbox` now trims the day's batch via `_enforce_email_mining_cap(pending)`
**before** either path spends credits (so the sequential fallback can't bypass the cap):
`settings.email_mining_batch_daily_cap` (env `EMAIL_MINING_BATCH_DAILY_CAP`, default 1000
calls/UTC-day; `<= 0` disables → pre-cap behavior) minus today's spend
`_email_mining_calls_today()` = `max(`metered `claude_usage:email_mining:*:calls:{date}`,
the submit-time `email_mining:batch:submitted:{date}` counter `_record_email_mining_calls`
bumps after each dispatch`)`. At/over cap → the batch stops enqueuing and logs (Loguru);
trimmed-off replies stay raw (`status='new'/'matched'`, re-parsable via the ai.py reparse
endpoint after the UTC rollover — no data lost, only Claude spend bounded). Mirrors the
enrichment-worker `daily_cap` / `ai_screen_daily_cap` count-cap pattern; reuses the
`intel_cache` Redis/PG counter substrate (no migration).

### 4a. Graph webhook endpoint (push) + validation-echo hardening

Real-time complement to the polling job above. `webhook_service.create_mail_subscription`
(and `create_teams_subscription`) register a Graph subscription whose `notificationUrl`
is `POST /api/webhooks/graph` (`/api/webhooks/teams` for Teams), storing a random
per-subscription `clientState` (`secrets.token_hex(16)`) on `graph_subscriptions`.

`graph_webhook` (`app/routers/v13_features/activity.py`) handles two request shapes:

- **Subscription-validation handshake** — Graph creates the subscription by calling the
  endpoint with a `?validationToken=` query param and REQUIRES the raw token echoed back
  with HTTP 200 as `text/plain`. The echo is unauthenticated (Graph has no token yet), so
  it is hardened by `_validation_echo_response` → `webhook_service.is_safe_validation_token`:
  length-bounded (`MAX_VALIDATION_TOKEN_LEN = 2048`) + printable-ASCII-only + no angle
  brackets (reject → 400), and the 200 response pins `text/plain; charset=utf-8` with
  `X-Content-Type-Options: nosniff` so the endpoint can't be coerced into reflecting
  oversized or HTML/script payloads.
- **Change notifications** — validated by `validate_notifications`: unknown
  `subscriptionId` rejected, `clientState` checked against the **random
  per-subscription secret** stored on `graph_subscriptions` with a timing-safe
  `hmac.compare_digest` (wrong/missing/empty → rejected), plus a 5-min replay window
  keyed on `subscriptionId:resource`. An all-invalid batch → 403. Usually that is
  spoofed/probe traffic, but genuine Graph batches can hit it too: Graph redelivers
  on any non-2xx, so a batch that 500s *after* `validate_notifications` recorded its
  replay keys is replay-dropped (→ all-invalid → 403) on every retry within the
  5-min window, and notifications in flight when `renew_subscription` deletes a
  Graph-404/410 row reject as unknown-subscription. When triaging 403s, check for a
  preceding 5xx or subscription-row deletion before classifying as an attack probe —
  those 403s are lost live notifications; valid `created` notifications
  fetch the message and feed the same activity-log + inbox-poll path as section 4.
  **Fail-open safety valve:** a subscription row with *no* stored `clientState`
  (legacy / mis-provisioned) is accepted rather than hard-rejected — so a
  mis-provisioned subscription can't silently drop *every* notification and break
  live inbox/RFQ monitoring — but the bypass is logged at `logger.error` (captured
  as a Sentry event) so it gets noticed and re-provisioned. The per-subscription
  random secret is deliberately used instead of a single shared `clientState` config
  secret: a leak compromises one subscription, not all of them. The Teams endpoint
  shares this contract but is gated off in `MVP_MODE` (returns 404); the mail/graph
  endpoint runs in MVP mode (mail subscriptions are created regardless of `MVP_MODE`).

**ACS (Azure Communication Services) webhook — sibling endpoint, different auth
shape.** `POST /api/webhooks/acs` (`app/routers/v13_features/activity.py`) logs
`CallCompleted`/`CallDisconnected` events and handles the Event Grid
`SubscriptionValidationEvent` handshake. Event Grid has no per-subscription
`clientState` body field like Graph, so the shared secret instead travels as a
`?secret=` query param baked into the webhook URL at Event Grid subscription time
(and into the default call-callback URL built by `POST /api/calls/initiate`),
compared with `hmac.compare_digest` against `settings.acs_webhook_secret`
(`app/config.py`). Fails closed: an unset secret rejects (403) every event —
including the validation handshake — even when `ACS_CONNECTION_STRING` is
configured; the app lifespan (`app/main.py`) logs a startup warning for that
misconfiguration.

## 5. Quote Building

**Where quotes are surfaced.** The standalone Quotes nav tab was retired
(PR quotes-relocation). Bare `/v2/quotes` 307-redirects to
`/v2/requisitions`. Quotes are now accessed in two places:

- **Reqs workspace Quotes tab** — `GET /v2/partials/parts/{requirement_id}/tab/quotes`
  (reuses `requisitions/tabs/quotes.html`). Reached from the requirement
  detail panel inside the Reqs workspace.
- **CRM account Quotes tab** — `GET /v2/partials/customers/{id}/tab/quotes`
  (renders `customers/tabs/quotes_tab.html` with an Alpine status filter).
  The account quote set is the **union** of site-linked quotes (via the
  company's active sites) and requisition-linked quotes (via requirements on
  requisitions whose `site_id` matches no active site, or whose site is NULL)
  — computed by the shared `_company_quotes_query(db, company)` helper so
  neither surface can drift from the other.

The quote detail page `/v2/quotes/{id}` is unchanged.

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

**Build-Quote service layer (in-workspace tab — Chunk A).** Three additive,
compute-on-read helpers in `quote_builder_service.py` back the reshaped
Build-Quote tab (no schema change):

- `best_cost_for(db, requirement_id)` / `best_costs_for(db, requirement_ids)`
  — the MIN `unit_price` across a requirement's ACTIVE offers plus the offer
  that provided it (`{"unit_cost", "offer_id"}`). Buyer-side mirror of the
  resell `ExcessLineItem.best_offer_unit_price` rollup, computed at read time.
- `margin_guardrail(cost, sell, *, min_margin_pct=10.0)` — pure helper
  returning a short warning when a line sells below cost or under the margin
  floor (matches `proactive_min_margin_pct` / `buyplan_min_margin_pct`).
- `quote_export_context(quote)` — the CLEAN customer-facing whitelist
  (`lines` of part_number/manufacturer/quantity/condition/cost/sell/margin/
  extended + header). Mirrors `bid_back_service.bid_back_export_context`:
  vendor / offer / source identity is stripped at ASSEMBLY, never by template
  omission. `document_service.generate_quote_report_pdf` now renders
  `quote_report.html` from this context, so the customer PDF cannot leak a
  vendor name.

**Build-Quote tab (in-workspace single-stage assembly — Chunk B).** The sales
quote-builder modal is reshaped into a **Build Quote** tab on the requisition
detail (`requisitions/detail.html` tab strip, sibling to the Quotes list tab),
mirroring the resell **Build Bid** tab. The tab is lazy (`hx-trigger="click"`,
explicit `hx-target="#tab-content"`) and owner/buyer-gated.

```
Browser (Build Quote tab) ──click──> GET /v2/partials/requisitions/{id}/build-quote
    |                                      (require_requisition_access; quote_builder.py)
    v
build_quote_tab_data(db, id) ──> per line: best_costs_for ref + ACTIVE offers
    |                            + sell seed (last-quoted -> else best-cost x 20% markup)
    v
requisitions/tabs/build_quote.html  (quoteBuilderTab Alpine: live margin + guardrail,
    |                                 blended total, markup-% reseed; single-quoted x-data
    |                                 + |tojson seed blob)
    +-- check line -> sell-price field seeds -> live margin chip + guardrail
    +-- "Assemble" --POST--> /v2/partials/requisitions/{id}/build-quote/assemble
              |                  (parses QuoteBuilderLine[]; delegates to
              v                   save_quote_from_builder -> revision lifecycle preserved)
        re-render tab with inline clean summary (quote_export_context) +
        advisory pre-send banner (quote_preflight) +
        Download PDF (existing /export/pdf) / Send (existing /quotes/{id}/send)
```

**Combined cross-req quote (OQ-02/REQ-04).** Selecting **2+ requisitions** in the
requisitions list and clicking **Build Quote** opens ONE combined quote spanning every
selected requisition's lines (not one quote per req). The list bulk-bar's 2+ branch
`htmx.ajax('GET', '/v2/partials/quote-builder/multi?requisition_ids=...', {target:'#modal-content'})`
then `$dispatch('open-modal', {wide:true})` — into the always-present global modal. The
`/multi*` routes are declared BEFORE `/{req_id}` in `routers/quote_builder.py` (FastAPI
matches in order, so "multi" must win before the int path param captures it).

```
List (2+ selected) ──GET──> /v2/partials/quote-builder/multi?requisition_ids=a,b
    |   (loop get_req_for_user per id = ownership; validate_same_customer)
    |     mismatch/no-site -> quote_builder/multi_error.html (HTTP 200 honest breakdown)
    v     ok -> quote_builder/modal.html (quoteBuilder Alpine, multiReqIds set)
Alpine loadData() ──GET──> /multi/data (merges get_builder_data across all reqs)
    |
    saveQuote() ──POST──> /multi/save?requisition_ids=a,b
        |   (looped require_requisition_access + get_req_for_user; then service core)
        v
save_quote_from_builder_multi -> _save_quote_from_builder_core(db, req_ids, payload, user):
    validate_same_customer (400 on mismatch) · primary = req_ids[0] (Quote.requisition_id)
    · one Quote + QuoteLines from ALL reqs · transition EVERY req -> QUOTED
    · link_quote_to_requisitions(quote.id, req_ids)  (join rows; primary self-row already
      created by the Quote after_insert listener)
```

**Quote ⇄ requisition membership is one arbitration point** — `services/quote_requisitions.py`:
`validate_same_customer` (all reqs share one non-null `customer_site_id`, honest 400 naming
each offender), `link_quote_to_requisitions` (idempotent), `requisition_ids_for_quote` /
`requisitions_for_quote` (primary-first), and `quotes_for_requisition(db, req_id)` — the
join-based `Query[Quote]` that REPLACES the old `Quote.requisition_id == req_id` read filter
so a SECONDARY requisition also surfaces the combined quote on its Quotes tab, Build-Quote
tab, offers-tab draft lookup, and the list Quotes column (batched one-query-per-page). Quote
**send** loops the transition + one ActivityLog over every contributing req (response still
reflects the primary). Building a **buy plan** from a combined quote is HARD-BLOCKED
(`buyplan_builder.build_buy_plan` raises `ValueError` "spans N requisitions" → 400) rather than
silently dropping the non-primary reqs' lines.

**Quote pre-flight (advisory, never blocks send).** `services/quote_preflight.py`
`quote_preflight(db, quote)` runs three deterministic read-only checks and returns a list of
`PreflightWarning`s: **dnc** (recipient `CustomerSite.do_not_contact`, or a `SiteContact` at
that site whose email matches the recipient and is `do_not_contact`), **country_of_origin**
(a quoted line's sourced `Offer.country_of_origin` is non-US), and **mpn_drift** (a quoted MPN,
via `normalize_mpn`, is not among the requisition's requirement MPNs). The Build-Quote tab
(`quote_builder._build_quote_tab_context`) surfaces them as an amber banner above Send; it never
disables Send. Also exposed at `GET /api/quotes/{id}/preflight` (`{warnings, count}`).

**Quote send (canonical service).** `services/quote_send.py::send_quote_email(db, quote,
user, *, token, override_email=None, override_name=None, testing=False)` is the SINGLE place
that emails a quote. Both routes call it: the htmx Send button
(`htmx_views.send_quote_htmx`, `POST /v2/partials/quotes/{id}/send`) and the JSON route
(`crm/quotes.send_quote`, `POST /api/quotes/{id}/send`). It resolves the recipient (override
else `CustomerSite.contact_email`), **hard-blocks DNC** (site-level or a matching DNC
`SiteContact`) raising `QuoteSendDNCBlocked`, builds the branded HTML via
`_build_quote_email_html` (whose single home is now this module — re-exported from
`crm/_helpers.py` for the preview route), POSTs `/me/sendMail`, then captures the sent
message's Graph ids via `email_service._find_sent_message` into `quote.graph_message_id` /
`graph_conversation_id` (NULL-safe). It then transitions the quote→SENT, advances the
requisition→QUOTED (unless WON/LOST/ARCHIVED), writes an OUTBOUND email `ActivityLog` via
`activity_service.log_email_activity`, and commits, returning a frozen `SendQuoteResult`.
Under `TESTING=1` the Graph POST + Sent-Items lookup are skipped but the quote is still
marked sent. `QuoteSendError` (no/invalid recipient → 400; Graph error → 502) and
`QuoteSendDNCBlocked` (→ 409 JSON / rose partial on htmx) are the typed failures. This fixes
the prior S1 bug where the htmx Send button set status=SENT WITHOUT emailing.

`quoteBuilderTab` (in `htmx_app.js`) is the single-stage simplification of the
modal's `quoteBuilder` (same `(sell-cost)/sell` margin math + blended rollup, no
two-panel decision flow). The list-toolbar "Build Quote" action re-points a SINGLE
selected requisition to this tab via `?tab=build_quote` (the detail partial deep-links
+ auto-opens it); 2+ selections keep the cross-req bulk modal (`/quote-builder/multi`).

**Per-line offer selection (Chunk B2).** Each line with 2+ ACTIVE offers shows a
compact per-line `<select>` (progressive disclosure; default = best/cheapest) so the
salesperson picks WHICH offer is used, not just the auto-best. The `selectOffer`
Alpine action sets the line's `offerId` and re-points its `cost` so the live margin
reflects the chosen offer. The choice rides the existing assemble payload
(`offer_id`) through `save_quote_from_builder` onto `QuoteLine.offer_id`. Vendor
identity stays internal — the customer doc/export strips it (`quote_export_context`).

**Revision source attribution (Wave 6).** `Quote.source` (e.g. `'proactive'`; migration
113) is revenue-attribution provenance. Both quote-revision chokepoints inherit it from the
parent so attribution survives a revision: the htmx "Revise" button
(`htmx/quotes.py::revise_quote_htmx`, `source=quote.source`) and the builder revise branch
(`save_quote_from_builder`, `source=old_quote.source if old_quote else None`). A fresh
(non-revision) build has no parent so `source` stays NULL — it is set only at origin
(e.g. `proactive_service.py` sets `'proactive'`).

**Terms editor + Preview (OQ-08).** The quote detail action bar (`quotes/detail.html`)
carries two always-visible buttons (no status gate, beside Revise / Apply Markup): **Edit
Terms** opens a modal (`$dispatch('open-modal', {url: '/v2/partials/quotes/{id}/edit-form'})`
→ `htmx/quotes.py::edit_terms_form` renders `quotes/edit_form.html` into `#modal-content`,
lazy-loading recent payment/shipping `<datalist>`s from `GET .../recent-terms`), and
**Preview** (`POST /v2/partials/quotes/{id}/preview` → `quotes/preview.html` swapped into
`#main-content`). The modal POSTs `.../{id}/edit` (`edit_quote_metadata`), which updates
`payment_terms`/`shipping_terms` (each ≤100 chars → else `HTTPException(400)`), `notes`
(uncapped), and **"Valid Until"**. The Quote model has **no `valid_until` column** —
`validity_days` is the single source of truth (also read by `quote_send.py`,
`crm/_helpers.py`, `quote_report.html`). The editor shows a **date picker**; the route
converts it to `validity_days` via `_validity_days_from_valid_until(quote, target)` anchored
to `quote.sent_at.date()` if sent else today — the **same anchor** `quote_send.py` uses for
the emailed expiry — so the editor default, the preview cell, and the real outbound email all
agree. `< 1` day → 400 (must be in the future); unparseable → 400 (invalid date). Blank/omitted
fields are left unchanged (edit-in-place). Ownership is scoped through `get_quote_for_user`
(SALES sees only own reqs → 404 otherwise). Errors surface via the global `htmx:responseError`
toast with no swap, so the modal stays open to fix and retry.

## 6. Buy Plan Workflow

```
Quote accepted (status='won')
    |
    v
buyplan_builder.py
    |
    +---> DB: INSERT buy_plans_v3 (DRAFT, linked to quote + requisition)
    +---> _quote_chosen_offers: requirement_id -> QuoteLine.offer_id (one QuoteLine->Offer
    |       join). Each line DEFAULTS to the offer the salesperson quoted (Chunk B2); when
    |       that offer is stale/inactive or can't cover qty, falls back to the re-score /
    |       auto-split path (mirrors resell CustomerBidLine.selected_offer_id provenance).
    +---> DB: INSERT buy_plan_lines (buyer assigned via ownership_service)
    +---> buyplan_scoring.py (ai_score per line)
    |
    v
buyplan_workflow/ (state machine — package: buyplan_approval.py owns submit/approve/reject/
                    halt/resume/reset/cancel + completion; buyplan_lines.py owns the per-line
                    claim/flag/resolve/resource ops; buyplan_po.py owns PO confirm/verify)
    |
    |  draft --submit--> pending --approve--> active --(all lines verified)--> completed
    |                       |                    |                  \--> cancelled (cancel_buy_plan: cascades open lines)
    |                       v                    v
    |                 draft (reject)         halted (SO halt)
    |
    |  approve/reject gate: the per-user can_approve_buy_plans right (NOT role) — the POST
    |   route depends on require_buyplan_approver (403 otherwise), approve_buy_plan re-checks
    |   the predicate, and the detail/supervise templates hide the controls via the
    |   can_approve_buy_plans Jinja global. Reject requires a reason (service-enforced, 400 on
    |   blank) and stamps approved_by/approved_at + sends the plan back to draft; the workspace
    |   pane's approval block + decision-tagged notes carry the sent-back story (the retired
    |   hub blocker strings are gone). submit_buy_plan clears the prior decision on resubmit.
    |   approve AND reject each write a BUYPLAN_APPROVED/BUYPLAN_REJECTED ActivityLog (plan-scoped).
    |
    |  Per-line (active):  awaiting_po --confirm_po--> pending_verify --verify_po(PO approver)--> verified
    |  Re-source (§6e):    pending_verify|verified --resource_line--> resourcing --claim_line--> awaiting_po
    |  Flag/Resolve issue: flag_line_issue: awaiting_po|pending_verify --> issue (buyer). resolve_line_issue:
    |                      issue --> awaiting_po (clears issue + PO-confirm fields), POST .../lines/{id}/resolve-issue,
    |                      _can_halt-gated (supervisor/ops) — the buyer who raised it can't self-resolve, so
    |                      flagged ISSUE lines surface on the workspace PO tab for supervisors. Without resolve, ISSUE
    |                      was a dead-end (badge only; resource_line rejects it).
    |  SO fold (Phase D):  the single manager approval IS the SO sign-off — _run_approve_side_effects
    |                      stamps so_status=approved + so_verified_by/at at approval time; there is no
    |                      separate verify-SO step (route/modal/queue-kind retired). sales_order_number
    |                      + so_status columns kept.
    |  Halt (Phase D):     halt_plan(plan_id, user, reason) is the single off-ramp (POST .../halt,
    |                      surfaced on detail). Auth = supervisor/ops (_can_halt: manager/admin role OR
    |                      active VerificationGroupMember). Haltable from pending/active; sets
    |                      status=halted + so_status=rejected + halted_by/at; resubmittable via reset.
    |  Completion gate:    all lines terminal AND so_status=approved (the fold guarantees the SO gate).
    |                      Phase 3 adds _has_open_po_gate(plan) (any line pending_verify) as an explicit
    |                      guard inside _complete_plan itself, so BOTH check_completion and the stock-sale
    |                      job's direct _complete_plan call refuse to complete over an un-verified PO.
    |  PO sign-off (Ph3):  the per-line verify_po/resource_line trio IS the canonical per-PO decision — the
    |                      deal-level PURCHASE_ORDER ApprovalRequest gate (opened on approval, $5k-thresholded,
    |                      approve-only) and its INBOUND/receive_buy_plan scaffolding are RETIRED (migration 176
    |                      cancels stale gate rows + reverts inbound->active; po_auto_approve_threshold removed).
    |  verify-PO gate:     verify_po is gated on the per-user can_approve_purchase_orders right AND (Phase 3)
    |                      enforces the approver's purchase_order_approval_limit against THIS line's amount
    |                      (_line_amount = unit_cost*qty) — over-limit raises PermissionError (route -> 400),
    |                      and detail/PO-Approval-tab hide the buttons via the new can_verify_po_line(user,line)
    |                      Jinja global. Approve AND reject each write a PO_LINE_VERIFIED/PO_LINE_REJECTED
    |                      ActivityLog (line-scoped, reject logged before the po_number reset).
    |  No-approver stall:  Phase 3 makes this per-PENDING_VERIFY-line: plan_needs_approver_reason flags a
    |                      plan if any cut PO line has no amount-eligible approver
    |                      (has_eligible_approver(PURCHASE_ORDER, _line_amount)), replacing the old
    |                      plan-total>=threshold heuristic. Amber banner on detail + the workspace
    |                      BP-tab row/pane stall warnings.
    |  Approvals console:  Phase 3 split the old blended "Approvals" page into the Buy Plans hub (since
    |                      RETIRED post-parity — /v2/buy-plans 308s to the workspace) and the console
    |                      at /v2/approvals (routers/htmx/approvals_hub.py, now the Workspace): Buy Plan
    |                      (approvals + lifecycle-status tracking via buy_plan_tracking_rows), PO Approval
    |                      (services/approvals/po_queue.build_po_queue_view — pending_verify lines + PO_LINE_*
    |                      /POCancellation history, NOT ApprovalRequest-backed), Prepayment (PREPAYMENT gate via
    |                      queue.pending/resolved_rows_for_gate). Every tab has a See-all vs See-mine scope
    |                      toggle (mine = submitted_by/requested_by == user; default all). queue.build_queue_view
    |                      /TAB_GATE and the stale approvals.get_queue redirect are removed.
    |  Prepay cash ctx:     The Prepayment RowVM (queue._row_vm) is enriched as the cash-authorising surface:
    |                      beneficiary (queue._beneficiary chain legal_name→vendor_name→display_name→—), the
    |                      authoritative amount/currency from the Prepayment itself (2-decimal, honours currency),
    |                      PO#/SO#/plan_id, po_line_total (_line_amount) for the PO→requested delta, buyer_remarks,
    |                      test_report_sent, + decided_by (queue._decider_names, resolved rows). _load_subjects
    |                      eager-loads vendor_card + buy_plan→requisition + buy_plan_line→offer (no N+1). The tab
    |                      renders a LOUD amber warning above Approve when test_report_sent is False, wires the
    |                      beneficiary/Review→ to subject_href, and a self-documenting resolved row (approved-by +
    |                      amount + PO#). The workspace prepayment pane/rows carry the same cash-first fields
    |                      (value = the AUTHORISED total_incl_fees, not plan cost) with the same warning.
    |  Prepay on PO:        prepayment_service.prepayment_state_for_lines(db, line_ids) → {line_id:
    |                      'requested'|'approved'} in ONE query (ApprovalRequest⨝Prepayment, status in
    |                      REQUESTED/APPROVED, buy_plan_line_id in ids; approved wins). Fed into the plan-detail ctx
    |                      (buy_plan_detail_partial) AND the PO-Approval tab ctx (render_tab_body po-approval) as
    |                      prepay_state. Drives (a) an amber "Prepayment pending"/emerald "Prepaid" badge on the
    |                      _detail_lines status cell + the PO-Approval row (#11), and (b) a non-interactive
    |                      "Prepay requested"/"Prepaid" pill that REPLACES the live request button when a line is
    |                      already in prepay_state — gated on can_request_prepayment(user,line) AND line.id not in
    |                      prepay_state (#10, kills the duplicate-400 dead-end). PO-Approval rows also link the
    |                      identity cell to the plan (hx-get /v2/partials/buy-plans/{id} + push /v2/buy-plans/{id})
    |                      and show · SO {plan.sales_order_number} (Task 8). The request_prepayment_button macro +
    |                      request_modal thread origin/hub_scope (like resource_form): modal shows PO#/MPN/plan#·SO#
    |                      read-only (#3) + an Alpine :data-deviates / hx-on::confirm warn-only client confirm when
    |                      the entered amount swings >5% from the PO line total (#2); prepayment_request_create reads
    |                      origin and re-renders the RIGHT surface (approvals_hub → po-approval tab body into
    |                      #ap-hub-body @hub_scope; else → plan detail into #main-content) instead of a blank toast (#12).
    |  Backorder (Ph3):    resource_line already reopens a COMPLETED plan (RESOURCEABLE_LINE_STATUSES includes
    |                      verified); when a cancel fires on a plan that WAS completed, resource_line returns
    |                      was_completed=True (computed pre-reopen) which is threaded to
    |                      notify_resource_requested(was_completed=True): email + Teams DM go to ALL recipients
    |                      IGNORING notify_resource_alert_enabled, subject "URGENT - BACKORDER". Normal in-flight
    |                      cancels keep the opt-in preference gate. Startup sweep _complete_reverted_active_plans
    |                      finishes any plan migration 176 reverts to active whose lines are already all-terminal.
    |  QP fold (Ph3):      Quality-Plan sales/purchasing sections are no longer an approval gate: submit_section/
    |                      QP_SALES/QP_PURCHASING dispatch retired; quality_plan_service.toggle_section_reviewed
    |                      (mark|unmark) stamps *_section_reviewed_at/_by_id (migration 177 renamed *_approved_at,
    |                      dropped dead approved_by_id/at), gated on can_review_qp_sales/purchasing_section,
    |                      writes QP_SECTION_REVIEWED activity. Routes POST /v2/qp/{id}/{sales,purchasing}/review.
    |  Ops group:          VerificationGroupMember (Settings > Ops Group; seeded from ADMIN_EMAILS) now
    |                      authorizes Halt + was the grandfather basis for can_approve_purchase_orders
    |                      (migration 173). admin/buy_plan_ops.toggle_ops_member guards the toggle: it
    |                      refuses to deactivate the LAST active member or to let the acting admin
    |                      deactivate themselves (both -> 400 JSON, auto-toasted); success fires a showToast.
    |
    +---> buyplan_notifications.py (submit/approve/reject/SO/PO/completed/cancelled + buyer/ops nudges)
    |       +---> teams_notifications.py --> Teams webhook / DM
    |       +---> DB: INSERT activity_log (linked via buy_plan_id FK)
    |
    +---> inventory_jobs.py: buyplan_nudge (30 min) re-pings buyer (PO unconfirmed >4h) and
            ops (PO unverified >2h) until lines advance; idempotent via buy_plan_lines.last_nudge_at
```

**Line editing (epic I — add/edit/remove, `buyplan_lines.py`).** Role×status gate
(`can_edit_buy_plan_lines`, enforced server-side by `_ensure_can_edit_lines` in every
mutating call, NOT just the template): draft/pending → plan owner (via `Requisition
.created_by`) or a manager; active/inbound/halted → manager-only (sales locked out
post-approval); completed/cancelled → locked for everyone. `add_buy_plan_line` (POST
`.../lines/add`) requires the requirement to belong to the plan's requisition, the offer
to belong to the SAME requisition (`_ensure_offer_on_requisition` — mirrors the detail-
render's vendor-picker filter, `Offer.requisition_id == bp.requisition_id`, so nothing
outside the picker's own universe can be posted), and derives `unit_cost`/buyer via
`assign_buyer`. `edit_buy_plan_line` (POST `.../lines/{id}/edit`) and
`remove_buy_plan_line` (POST `.../lines/{id}/remove`) both refuse a vendor/qty change or
removal once `_has_cut_po(line)` is true (PO confirmed or status has left
`awaiting_po`) — the sell price stays editable regardless (it never touches the PO). Each
mutator recomputes the header rollups via `_recalculate_financials` and returns the
refreshed `buy_plan_detail_partial` into `#main-content`; `remove_buy_plan_line`'s route
also runs `check_completion` right after commit (removing the plan's last open line can
leave every remaining line terminal — same auto-complete call the verify-po route makes).

`bulk_edit_buy_plan_lines` (POST `.../lines/bulk`) is the "save all" counterpart driving
the whole-table Alpine editor (`buyPlanLinesEditor`, `htmx_app.js`; "Edit plan" toggle →
per-row inline edit/add/remove → "Save all"). Form field `payload` is JSON
`{"lines": [...], "known_line_ids": [...]}`:
  - an entry with `line_id` edits that line — offer/qty changes are refused once
    `_has_cut_po(line)` is true UNLESS the submitted value equals the line's CURRENT
    value (a no-op resend never trips the guard, so a PO cut on an untouched row between
    form-load and save can't 400 the whole save); an actual offer change re-derives
    `unit_cost`/buyer and must pass `_ensure_offer_on_requisition`; `unit_sell` uses
    key-presence semantics (`"unit_sell"` absent → unchanged; present + JSON `null` →
    cleared; present + a number → set);
  - an entry without `line_id` adds a new line — same validation as `add_buy_plan_line`,
    plus qty must be a whole number (a fractional value like `3.5` is rejected, not
    truncated);
  - any existing, non-PO-cut line whose id is NOT in the payload is removed (removal-by-
    omission — the same guard as `remove_buy_plan_line`, applied implicitly). When
    `known_line_ids` (every line id the client's form actually rendered) is given,
    removal-by-omission is further scoped to ids IN that set, so a line another user
    added after the form loaded — present on the plan but never in `known_line_ids` — is
    left untouched instead of silently deleted; `known_line_ids` omitted falls back to
    the unscoped legacy behavior (a route backward-compat contract; the UI always sends
    it). A PO-cut line omitted from the payload is always left untouched regardless.

Same role×status gate, same recompute-and-re-render tail, same post-commit
`check_completion` call as the single-line remove route. Malformed JSON, a wrong shape
(`{"lines": [...]}` required), or a non-list-of-ints `known_line_ids` → 400 before the
service is even called.

### 6e. Re-source (fall-down → open claim pool → urgent buyer backfill)

`buyplan_workflow.resource_line` (`app/services/buyplan_workflow/buyplan_lines.py`) is the
**single** fall-down → re-source engine, fed by
**two triggers** (never a parallel queue):

- **Vendor-cancel (SP-3):** a buyer records a PO on a line in Acctivate; when a **vendor
  falls down** (sells elsewhere / can't deliver) the buyer cancels that PO and clicks
  **Re-source** on the line (`POST /v2/partials/buy-plans/{plan}/lines/{line}/resource`).
  Shown on `pending_verify`/`verified` lines of an `active`/`completed` plan (the button
  survives even when the plan auto-completed — the late-fall-down case).
- **Receiving-reject (SP-4):** the vendor delivered, but the parts were rejected at
  receiving (**defective / wrong / short**). On a delivered — i.e. auto-**COMPLETED** —
  plan the buyer rejects the line
  (`POST /v2/partials/buy-plans/{plan}/lines/{line}/reject-received`) instead of letting
  the deal stand. The reject reasons (`defective`/`wrong_part`/`short_ship`) are
  receiving-specific `LineResourceReason`/`POCancellationReason` values (free strings on
  the `String` reason columns — **no migration**); each maps to a durable
  vendor-unavailability reason (`broken`/`different_part`/`not_really_there`). The plan
  reopens **completed → active**. (Phase 3 retired the INBOUND state along with the
  deal-level PO gate.)

Both triggers are gated by `get_buyplan_for_user` ownership + `_require_po_cutter` role and
both funnel through the router helper `_resource_lines_and_alert` (pool + commit + alert
fan-out). `buyplan_workflow.resource_line` (`buyplan_lines.py`; default one line; `scope=plan` escalates to the
plan's other cut/received lines) for each target, in one transaction:

1. `po_cancellation_service.record_po_cancellation` — append the immutable `po_cancellations`
   row (vendor-performance fact; `days_to_cancel` from `po_confirmed_at`).
2. `mark_offer_sold` — the offer → `OfferStatus.SOLD` (Offers tab) + ChangeLog/ActivityLog.
3. `mark_vendor_unavailable` — `record_unavailability(SOLD_ELSEWHERE-mapped)` so the vendor
   shows **unavailable** for that part on the Sightings tab (§2d).
4. Reset the line into the pool: clear buyer/offer/PO fields, `status = resourcing`; reopen
   the plan to `active` if it had auto-completed (`completed`), so `claim_line` →
   `confirm_po` (needs an active plan) works again.
5. `refresh_vendor_cancellation_metrics` — recompute the vendor's `cancellation_rate` /
   `avg_days_to_cancel` / `slow_cancel_count` (a slow cancel, >7d, dampens `vendor_score`
   harder — `vendor_score._cancel_dampener`).

The route then fires `run_notify_bg(notify_resource_requested)` — an **URGENT broadcast** to
all other active buyers + the deal's salesperson across three channels
(`buyplan_notifications.notify_resource_requested`): an in-app `ActivityLog`
(`RESOURCE_REQUESTED`, always written) + a Teams **channel Adaptive Card** (always posted,
webhook-gated) + **email and a Teams DM** (honor `users.notify_resource_alert_enabled`; the
in-app row + channel card are the always-on delivery floor). Each channel isolates its own
failures; the POST returns immediately (fire-and-forget own-session).

The pooled line surfaces on the **Approvals Workspace PO tab** (the claimable
re-sourcing pool in `_po_rows`, shown org-wide; the SO-pane kanban's Re-sourcing lane
carries the claim button too) + adds to the buy-plans badge via `BuyplanResourcingSource`
(ACTION, pool-wide). Any PO-cutter **Claims** it (`claim_line` — atomic guarded UPDATE,
first-to-claim wins, loser → 409) which returns it to `awaiting_po` under the new owner for
the normal PO flow. See APP_MAP_DATABASE `po_cancellations`.

**Buy Plans hub — RETIRED post-parity (spec §11.1).** The two-lens hub at
`/v2/buy-plans` (My Queue + Pipeline, Approvals rework Phases A–F) retired once the
Approvals Workspace reached capability parity — the mapping and the accepted gaps are
pinned in `docs/APPROVALS_PARITY_CHECKLIST.md`. What replaced what:

- **Full page** `/v2/buy-plans[?lens=]` → **308** to `/v2/approvals?tab=buy-plans`;
  `/v2/buy-plans/{id}` → **308** to `/v2/approvals?tab=buy-plans&select={id}`
  (`htmx_views.buy_plans_hub_retired_redirect`). `?select=` threads full page →
  shell → tab body → list; the SO/BP list dispatches that plan's pane as the default
  selection (access-checked via `get_buyplan_for_user`; unknown/inaccessible ids fall
  back silently to the oldest needs-approval default).
- **Partials**: `GET /v2/partials/buy-plans` → 308 `/v2/partials/approvals?tab=buy-plans`
  (`?new=1` → 308 the origination picker); `/{my-queue,pipeline}` → 308
  `/v2/partials/approvals/buy-plans[?scope=]`; `/pipeline-archive` → 308
  `/v2/partials/approvals/buy-plans/list?show_closed=true`. Unknown lens values 404.
- **My Queue rows** → every workspace list's **"Needs your approval"** group (oldest
  default-selected); buyer cut-PO / claim / flagged / re-sourcing rows → the PO tab;
  drafts + halted → the SO/BP lists (Mine scope); `no_approver` → the BP-tab
  row/pane stall warnings (`plan_needs_approver_reason`).
- **Pipeline board + Done archive** → the SO/BP work lists (status badges, search,
  Mine/All, the Closed filter) + the SO-pane kanban (`_pane_kanban.html`) for per-line
  progress. The metric strip / `open_avg_margin` aggregate was **consciously dropped**
  (per-deal margin still shows on the SO pane header — see the checklist rationale).
- **Origination** → the workspace lists' "New sales order" button loads the SELF-HOSTED
  picker (`_sales_order_new.html`, `#so-origination` outerHTML swaps, Cancel → the
  workspace shell) straight into `#main-content`.
- **Deleted read models** (`services/buyplan_hub.py` slimmed): `my_queue`/`QueueRow`,
  `deals_board`, `completed_archive`, `open_avg_margin`, `supervise_overview`,
  `buyer_line_queue`/`team_line_queue`/`resourcing_pool_queue`,
  `_query_stuck_no_approver_plans` + private helpers. Survivors: `_customer_name`,
  `_age_hours`, `_line_mpn`, `_query_po_pending_verify` (+ `_LINE_PLAN_LOADS`) —
  imported by `services/approvals/po_queue.build_po_queue_view`.
- **Deleted templates**: `buy_plans/hub.html`, `approvals/_surface_my_queue.html`,
  `approvals/_surface_pipeline.html`, `approvals/_pipeline_macros.html`,
  `approvals/_pipeline_archive_rows.html`.
- **origin=my_queue** action-return branches deleted (stale posts fall through to the
  detail partial); `prepay_request_decide` without an origin renders the workspace
  Prepayments tab body.

**Flagged-issue honesty (surviving).** The detail page's "AI Insights" indicator shows the
worst flag's verbatim reason via `buyplan_naming.summarize_top_flag(bp.ai_flags)`
(critical → warning → info) — it states WHAT is wrong, not just a count.
`buyplan_naming.build_card_title` remains the shared one-string title helper for any
caller that wants `{SalesOrder#} - {Customer} - {Owner} - {Type}`.


**Resell workspace — resell/excess split-panel (Chunk F, ADDITIVE).** `/v2/resell` is
its own primary-nav tab (9th item in `mobile_nav.html`) served by the `v2_page` shell →
`GET /v2/partials/resell/workspace` (router `app/routers/resell.py`, mounted alongside the
OLD `excess` router which a later cutover chunk removes). The workspace is a `splitPanel('resell')`
shell: lens pills (My Lists / Open to Me, buy-plans-hub pattern) + a `stat_card` triage strip
(Open · Offers to review · Take-all · Bids out · Awarded — each card a one-click stage filter) +
a lazy left list and a right detail. Logic stays in `excess_service` (offers/import) +
`excess_mirror` (publish); the router is thin (request → context → partial).

```
GET /v2/partials/resell/workspace?lens=mine|open   (shell: pills + stats + splitPanel)
    |
    +-- GET /v2/partials/resell/lists?lens=&stage=&q=   (left list; rows → detail)
    |        lens=mine  → lists OWNED by user (seller name VISIBLE)
    |        lens=open  → posted lists owned by OTHERS, customer-ANONYMIZED (pure whitelist)
    +-- GET /v2/partials/resell/{id}                    (right detail: breadcrumb + chips +
    |        lazy tabs Lines · Offers · Build Bid · Outreach(owner) · Activity; customer chip owner-only)
    |        +-- GET .../{id}/lines    (adaptive: 1 line → .card, ≥2 → compact-table)
    |        +-- GET .../{id}/offers   (OWNER: pinned take-all banner + per-line offer tables +
    |        |     unmatched queue [each row an "Assign to" select — finding #15]. NON-OWNER
    |        |     (broker): their OWN offers ONLY + a Withdraw per open/late bid — NO competitor
    |        |     data (Phase-3 anonymization); a submitter reaches it even after the window
    |        |     closes — finding #13)
    |        +-- GET .../{id}/lines/{line_id}/offers  (per-line comparison: best emerald +
    |        |     price-spread bar, cloned from quote_builder/modal.html, NO auto-select)
    |        +-- GET .../{id}/offer-buyers-form  (owner-only buyer panel: ranked suggestions
    |        |     [buyer_affinity_service.rank_buyers_for] + advisory overlap flag
    |        |     [overlap_warning] + no-contact history rows + scope + channel;
    |        |     ?preselect_vendor_card_id= seeds the checked set so a not-yet chip lands
    |        |     with its buyer already selected — RS-8)
    |        +-- GET .../{id}/outreach           (owner-only Outreach tab: tracker rows +
    |        |     'offered N · M responded · K bid' summary; lazy, explicit hx-target)
    |        +-- GET .../{id}/not-yet-strip      (owner-only nudge: not_yet_offered_strip;
    |              also persists each surfaced buyer as an owner-assigned My-Day follow-up via
    |              task_service.auto_create_resell_followup_task — idempotent per list+buyer+owner)
    +-- POST /api/resell/lists                          (create → excess_service.create_excess_list)
    +-- POST /api/resell/{id}/lines                     (add line; resolves MaterialCard;
    |     re-renders the WHOLE detail via [data-resell-detail-root], not just Lines, so the
    |     header Post button appears once a fresh draft has lines — RS-5)
    +-- DRAFT-EDIT set (finding #14 / D4 — all DRAFT-only + owner-only, guarded 404→403→409 in
    |     the service; a draft has no offers/mirror so side-effect-free except total_line_items):
    |        +-- PATCH  /api/resell/{id}/lines/{line_id}  (excess_service.update_line; re-validates
    |        |     quantity>0 → 400 [the model @validates 500s otherwise]; re-resolves the
    |        |     MaterialCard when MPN/manufacturer changes; re-renders detail)
    |        +-- DELETE /api/resell/{id}/lines/{line_id}  (excess_service.delete_line; decrements
    |        |     total_line_items; re-renders detail)
    |        +-- PATCH  /api/resell/{id}                   (excess_service.update_excess_list;
    |        |     title/notes/company_id[re-validates exists]/customer_site_id; re-renders detail)
    |        +-- DELETE /api/resell/{id}                   (excess_service.delete_excess_list; cascade
    |        |     cleans children → refreshes My-Lists [#resell-list-body] + OOB detail-pane reset
    |        |     [#split-right-resell] + toast + HX-Push-Url /v2/resell so a reload no longer
    |        |     reopens the deleted list id [finding #8])
    |        +-- GET .../{id}/edit-form, .../{id}/lines/{line_id}/edit-form (pre-filled modals)
    |     [All four mutating routes call _get_list_for_user FIRST so a NON-owner probing a private
    |      draft gets 404 [existence masked], not the service's 403 — matches the GET edit-form
    |      path [finding #3]. Honest 409 copy (×3): "Posted lists are locked. Close this list and
    |      create a new one to make changes." replaces the false "revise as a new version".]
    +-- POST /api/resell/{id}/import-preview|import-confirm  (reuse excess parsers + preview grid;
    |     preview ALWAYS renders a re-upload/back affordance even for an all-errors file — RS-6;
    |     confirm re-renders the whole detail like add-line — RS-5)
    +-- POST /api/resell/{id}/publish                   (excess_mirror.publish_list → Sighting mirror;
    |     GUARDED to draft [409 otherwise — no re-open of a resolved posting]; PRESERVES a future
    |     close_at, clears only a stale one [Phase 5])
    +-- POST /api/resell/{id}/offers                    (excess_service.submit_offer; scope
    |     per_line|take_all; service enforces can_offer + the self-offer guard)
    +-- POST /api/resell/{id}/offers/{offer_id}/award   (owner-only; excess_service.award_offer:
    |     the single offer→won chokepoint; take_all awards ALL non-withdrawn lines, per_line
    |     awards its matched lines; idempotent for an already-won offer; 409 on a TERMINAL list
    |     [closed/expired — awarding would reopen the dead list, finding #4]; 409 unless the offer
    |     is open/late [a lost/withdrawn offer is not awardable — guard runs BEFORE line scope];
    |     409 if a line is already awarded to another offer; recomputes rollups + buyer-score win-hook;
    |     retires the sold lines from the Sighting mirror (sync_list_mirror); derives the
    |     list→awarded status once every line is decided. RESPONSE is an OOB compose
    |     (_award_response.html): PRIMARY = Offers tab (hx-target #tab-offers-<id>), OOB =
    |     #tab-lines-<id> (Awarded/Withdrawn pills) + #resell-chips-<id> (N/M-awarded chip +
    |     status badge), so awarding never resets the Alpine tab state; HX-Trigger showToast)
    +-- POST /api/resell/{id}/offers/{offer_id}/unaward (owner-only; excess_service.unaward_offer:
    |     the EXPLICIT inverse — never a silent auto-swap to a new winner. 409 if the offer
    |     is not won; reverts offer→open + lines→available, recomputes rollups + buyer score
    |     (full-history recompute self-heals wins), steps the list back off awarded → bid_out
    |     (posting window CLOSED — close_at in the PAST, via _posting_window_closed; NOT bare
    |     close_at truthiness, since Phase 5 preserves a FUTURE deadline through publish) else
    |     collecting FIRST, THEN re-mirrors (so a reverted-to-bid_out closed posting stays retired,
    |     while a still-open future-deadline list re-advertises — M5, findings #1/#3). Same _award_response OOB)
    +-- POST /api/resell/{id}/offer-lines/{offer_line_id}/assign (owner-only; finding #15;
    |     excess_service.assign_offer_line: manual resolution of the unmatched queue — point a
    |     parked ExcessOfferLine at a posted line [404 target/offer-line off this list], flips
    |     match_status→matched + recomputes the target [+ old line on a re-assign] rollup so the
    |     salvaged bid is awardable. GUARDED: 409 on a resolved/terminal list [awarded/closed/
    |     expired] and 409 unless the parent offer is open/late [finding #2 + the finding #4
    |     "second vector"]. Same _award_response OOB compose as award)
    +-- GET  /v2/partials/resell/{id}/build-bid          (owner-only Build-Bid tab: each line's
    |     best-offer planning price + editable "our offer"; once assembled, the clean
    |     bid_back_export_context summary + Download-PDF + the lifecycle action bar. Context
    |     carries the resolved seller contact so Send shows/gates on the recipient email)
    +-- POST /api/resell/{id}/bid                        (owner-only; bid_back_service.build_bid_back:
    |     assemble / RE-ASSEMBLE — a NON-terminal latest bid [draft/sent] bumps CustomerBid.revision
    |     on the SAME row [audit chain, no orphan draft] + replaces its lines + resets to a fresh
    |     draft; a TERMINAL latest [accepted/rejected] is frozen history so a re-assemble INSERTs a
    |     NEW customer_bids row [revision+1, draft] and leaves the answered row untouched — D3;
    |     re-renders the tab — M4)
    +-- GET  /api/resell/{id}/bid/{bid_id}/pdf           (owner-only clean bid PDF, whitelist only)
    +-- POST /api/resell/{id}/bid/{bid_id}/send          (owner-only; bid_back_service.send_bid_back:
    |     resolve_seller_contact → email the clean PDF via send_batch_rfq [no requisition,
    |     PDF as the sole attachment] → draft→sent + stamp sent_at ONLY on a confirmed send;
    |     409 non-draft/no-lines, 422 no contact email, 502 failed send. require_fresh_token — M4)
    +-- POST /api/resell/{id}/bid/{bid_id}/accept|reject (owner-only; bid_back_service.record_bid_response:
    |     the trader logs the seller's answer sent→accepted/rejected + responded_at/by; 409 unless sent — M4)
    +-- POST /api/resell/{id}/close                      (owner-only; excess_service.close_list:
    |     GUARDED to open/collecting [409 otherwise] → bid_out + close_at + RETIRES the Sighting
    |     mirror [sync_list_mirror on a now-closed posting] — M5)
    +-- POST /api/resell/{id}/close-without-bid          (owner-only; D5; excess_service.
    |     close_list_without_bid → the TERMINAL closed state [distinct from bid_out]: same
    |     open/collecting guard + close_at + mirror retire, but CLOSED is never swept by the
    |     nightly expiry [only open/collecting are] and never reopens. close_list and
    |     close_list_without_bid share _end_posting_window(target_status))
    +-- POST /api/resell/{id}/outreach                  (owner-only; channel=email →
    |     resell_outreach_service.submit_outreach_email [RFQ send engine], else
    |     submit_outreach [manual log]; re-renders the Outreach tracker)
    +-- GET  /v2/partials/resell/{id}/outreach/{oid}/reply   (RS-4, owner-only; the tracker's
    |     "View reply" button opens _reply_viewer.html in #modal-content — the buyer's reply
    |     thread (_replies_context joins VendorResponse↔ExcessOutreach on graph_conversation_id,
    |     newest-first) + a "Convert to offer" quick-add. 404 when the outreach has no thread)
    +-- POST /api/resell/{id}/outreach/{oid}/offer      (RS-4, owner-only; human-reviewed
    |     offer extraction — record_response(has_offer=True) creates the inbound ExcessOffer
    |     via the SAME queued-never-dropped line matcher as an emailed bid + advances the
    |     outreach →bid; re-renders the tracker into #tab-outreach-<id>)
    +-- MANUAL-CHANNEL log (finding #12, owner-only; a phone/teams/marketplace row is 'sent'
          with NO email thread, so the conversation-keyed reply matcher can't advance it):
          +-- POST .../{oid}/log-response  (resell_outreach_service.record_manual_response →
          |     responded; never regresses a terminal bid/declined; 409 for an email row)
          +-- GET  .../{oid}/log-bid-form  (reuses _reply_viewer.html — manual flag + convert_url
          |     — as the Log-bid modal; honest 'Bid logged' toast, not 'Offer created from reply')
          +-- POST .../{oid}/log-bid       (record_manual_response(has_offer=True) → bid + an
                ExcessOffer via the SAME _link_inbound_offer path an emailed bid uses; 400 unless
                the qty is positive [finding #1]; _link_inbound_offer is GATED on the same terminal
                check as the status advance, so a replayed Log-bid on an already-bid/declined row is
                an idempotent no-op — no duplicate offer [finding #5/#9/#10])
```

**Triage filters (finding #16).** The left-list `stage` filter takes the usual status values
(`open`/`collecting`/`bid_out`/`awarded`/`closed`/`expired`, each an exact `status=`) PLUS a
synthetic `stage=live` token that widens to `[open, collecting]`. The workspace "Open" glance
card counts open+collecting (a list flips open→collecting on its first offer but is still
live), so it links to `stage=live` to match its count; the strict `open` pill in `_lists.html`
keeps meaning EXACTLY `status=open`.

**RS-4 reply tracking (inbound half).** The send path already stamps
`ExcessOutreach.graph_conversation_id`/`graph_message_id` (migration 133); RS-4 wires the
INBOUND half with NO new migration. `email_service.poll_inbox` gained Tier 2.5 (see §4):
a buyer's reply matches the outreach via `resell_outreach_service._match_outreach` and
advances it `sent→responded` through `record_response(commit=False)` inside the existing
per-message savepoint, logging one excess-scoped inbound `activity_log`. The tracker's
"View reply" button (shown once a reply lands — `graph_conversation_id` + an engaged status)
opens the reply viewer, which reuses the `VendorResponse` rows the poll already writes (no
new reply-content table) and offers a manual "Convert to offer" — offer auto-detection from
the AI parse is a deliberately deferred Phase-2 decision.

**Bid-back lifecycle (M4 + D3).** The `CustomerBid` runs `draft → sent → accepted/rejected`.
`build_bid_back` re-assemble BUMPS `revision` in place on the SAME row while the latest bid
is non-terminal (`draft`/`sent`) — audit chain preserved, resetting a sent bid to a fresh
draft instead of orphaning a new one. Once the latest bid is TERMINAL (`accepted`/`rejected`)
it is frozen history: a re-assemble INSERTs a NEW `CustomerBid` row (`revision`+1, `draft`)
and leaves the answered row — status, `sent_at`/`responded_at`/`responded_by_id` and its
lines — untouched (D3), so a list can hold multiple immutable answered revisions plus the
working draft (the id-desc select surfaces the newest).
`send_bid_back` resolves the seller's send contact (`resolve_seller_contact`: the list's
`customer_site` → an active company site → a primary `SiteContact`), renders the clean
whitelisted PDF, and emails it via `email_service.send_batch_rfq` in no-requisition mode
(the PDF is the sole attachment; DNC-at-send / save-to-sent for free) — flipping `draft→sent`
+ stamping `sent_at` ONLY on a confirmed send (422 when no contact email, 502 on a failed
send, so the bid never falsely reads "sent"). `record_bid_response` lets the trader log the
seller's answer (`sent→accepted/rejected`, stamping `responded_at`/`responded_by_id` — who/when;
the seller is not a User). Migration **183** adds `customer_bids.sent_at` / `responded_at` /
`responded_by_id` (the `status`+`revision` columns pre-existed).

**List close/expire lifecycle + posting-closed mirror gate (M5).** `close_list` is guarded
to `open`/`collecting` (409 for a draft or an already-resolved list). The Sighting live-mirror
is now POSTING-aware: `sync_list_mirror` retires a line when it is individually inactive OR
when the LIST's status is posting-closed (`bid_out`/`awarded`/`closed`/`expired`) — so closing
(or expiring, or awarding a late offer on) a list retires its WHOLE mirror; publishing +
collecting are unchanged (draft/open/collecting fall through to the per-line active check).
The nightly `app/jobs/resell_jobs.py::_job_expire_resell_lists` (02:15) →
`excess_service.expire_overdue_lists` flips past-`close_at` unresolved (open/collecting) lists
to `expired` and retires their mirror; the left-list stage filter now offers the `closed` /
`expired` stages (the status badges already rendered them).

**Phase 5 (posting window + scoring + mirror identity).** Four related fixes:
- *Posting-deadline entry point + chip fix (finding #8, D1).* `create_excess_list` /
  `update_excess_list` accept an optional future+tz-aware `close_at` (400 on naive/past via
  `_validate_draft_close_at`); the create modal exposes it as an optional "Offers close by"
  `datetime-local`. `publish_list` now PRESERVES a future `close_at` (was: nulled it), so the
  nightly expiry backstop finally has real windows. The resell chip context exposes `is_live`
  (open/collecting only) + `close_at_display`; `_header_chips.html`/`_lists.html` render the
  countdown ONLY while live and a muted "closed {date}" once resolved — never a red "Overdue"
  on a resolved list (the shared `time_text` macro is gated at the resell-template level, never
  edited). `_close_at_display` returns the label ONLY for a PAST `close_at`: a non-live list
  holding a FUTURE create-set deadline (a draft with an "Offers close by" next week, or an
  awarded list whose deadline survived publish) shows NO "closed {future date}" chip (finding #2).
- *Mirror line-identity (finding #18, migration 199).* `sightings.excess_line_item_id`
  (FK, indexed) is the mirror upsert/retire key — two duplicate-part lines on one list keep
  distinct Sightings and one award/withdraw no longer wipes the twin.
- *Mirror teardown on delete (P2).* `excess_mirror.teardown_list_mirror` deletes a list's whole
  mirror + its virtual scratch requisition; wired into `delete_companies`,
  `seed_resell_demo._reset`, and `delete_excess_list` so deletion never strands mirror rows.
  NOT called on close/expire (a closed list can reopen via unaward and re-mirror).
- *Nightly BuyerScore backstop + UI-submit attribution (finding #17).*
  `_job_recompute_buyer_scores` (02:35) → `buyer_affinity_service.recompute_all_buyer_scores`
  reconciles every buyer's scorecard so a missed on-win/on-send hook can't leave a row stale
  (the three nightly resell jobs run at 02:15/02:25/02:35 to avoid collision). The `response_rate`
  denominator KEEPS counting a manual-log SENT touch (`sent_at=None`); only SENDING/FAILED/
  INTERRUPTED are excluded. The submit-offer form gains an optional buyer `<select>` →
  `submit_offer(buyer_company_id=...)` resolves `offerer_vendor_card_id` via `counterparty_card`,
  so the award win-hook (`recompute_buyer_score_on_win`) now fires for UI-submitted manual offers;
  unattributed offers stay None and still award (no regression).

Adaptive-detail rule (spec "density scales to line count, placement follows offer scope"):
`shape='single'` (1 line → one `.card`, no table chrome) vs `'table'` (≥2 → `compact-table`);
any take-all offer pins as a violet banner above the lines. Status pills reuse existing
`status_badge` keys — no new colors (open→sky, collecting→sourcing/amber, bid_out→quoted/violet,
awarded→won/emerald, draft→muted). Customer hiding is view discipline (single-tenant): the
offerer-facing list + non-owner detail project ONLY MPN/qty/condition, never the seller company.
Demo seed: `python -m app.management.seed_resell_demo` (idempotent; `--reset` to clear) creates
three deal shapes (40-line collecting w/ per-line + unmatched + take-all offers, a single-line
one-off w/ 2 offers, an awarded list).

**Anonymization policy (Phase 3, decision D2 — one predicate everywhere).** Customer-identity
hiding is enforced through the SINGLE ownership predicate `can_see_customer` (== `is_owner` ==
`el.owner_id == user.id`), threaded from `resell.py` into every template. To a NON-owner (the
"Open to Me" offerer lens + the non-owner detail) the app projects only MPN / qty / condition —
never the seller company, and never any of these owner-private aggregates:
- the seller **company name** and the **owner name** chip (`_header_chips.html`);
- the free-text **title** — traders name lists after the customer, so a non-owner gets the
  neutral `Excess listing #N` label (`_display_title`);
- **offer count** (header "N offers" chip + Offers-tab count badge), **offer coverage** meter,
  the amber offer-count badge, and the **"N/M awarded"** progress chip — all competitive signal,
  gated identically to the already-private per-line offer badge / best-offer price (RS-1);
  `_list_cards` also NULLs coverage/offer_count for non-owners as defense-in-depth.
Three de-anonymization ORACLES are closed the same way: (1) the left-list `q` search filters on
**part identity** (normalized MPN / manufacturer, both indexed) in the open lens — never the
title, which would let a non-owner confirm a hidden customer name by hit/miss; the title ILIKE
stays for the owner's mine lens only. (2) The outreach email **subject** prefill is neutral
(`Excess available: N lines`) — never the customer-named title, which ships externally to the
buyer; and the internal per-touch **ActivityLog subject** references the list by id
(`excess offer (list #N)`), since that log lands on the SHARED buyer vendor-card timeline.
Reply-matching is unaffected — it keys on the PERSISTED `send_subject`, not the prefill default.
(3) The left-list **`needs`** offer-triage filter (`needs=offers` / `needs=take_all` → lists
carrying a live/whole-list bid) is the OWNER's board only — gated on `can_see_customer`
(`resell.py resell_lists`), so a non-owner cannot craft `lens=open&needs=offers` and diff it
against the plain open lens to learn which anonymized `Excess listing #N` postings have already
drawn a bid (the offer-EXISTENCE sibling of the offer-count chip it hides).

Same gate on every OTHER cross-trader writer that names the list, since each lands on a surface
keyed only on `vendor_card_id` (the shared buyer timeline / Tasks tab): the retry resend's
**fallback** subject is the neutral part-count default, never `el.title` (used only when a legacy
/ cleared row has no persisted `send_subject`); the inbound-offer **owner notification**
(`notify_owner_of_offer` → `New offer from <buyer> on list #N`); and the "not yet offered"
**follow-up task** title (`auto_create_resell_followup_task` → `… on Excess listing #N …`).

**Notification tiers (`buyplan_notifications.py`).** Two tiers gate which channels fire:
- **Urgent → email + Teams DM + in-app**: SO kickback (`notify_so_rejected`), PO kickback
  (`notify_po_rejected`, fired from the verify-po reject path), new assignment / approval
  (`notify_approved`, per assigned buyer).
- **Routine → in-app only**: plan completion (`notify_completed`) — no email, no Teams.
All tiers write an `activity_log` row linked via `buy_plan_id` (+ `requisition_id`).

**Per-user email opt-out (Task 9).** `_send_email(user, ...)` early-returns (no token
fetch, no Graph client) when the recipient's `notify_buyplan_email_enabled` is False —
the Profile-tab toggle. Suppression is at the firing site only: the calling `notify_*`
function still writes the recipient's in-app `activity_log` row, so nothing is lost
in-app — only the email channel is silenced for that user.

**Reporting fold.** The retired `/v2/reporting` page's analytics now live where the work
happens: the **Supervise** lens strip (open value / avg margin / approvals / halted /
overdue / flagged counts), the **Sales Hub** pipeline chip (`forecast_service.pipeline_summary`
in `parts_workspace_partial`), and the **CRM** coverage chip (`reporting_service.coverage_report`
in `crm_service.cdm_list_ctx`). `coverage_report` is global (population-wide, filter-independent),
so it is short-TTL cached (`@cached_endpoint`) to stay off the aggregation queries on every
CRM list refresh while the chip still re-renders.

**Buy-plan completion → CPH feed (proactive backbone).** When `check_completion`
(app/services/buyplan_workflow/buyplan_approval.py) transitions a plan to COMPLETE, it calls
`record_buyplan_purchase_history(db, plan)` (app/services/purchase_history_service.py)
inside a best-effort try/except so a CPH failure never rolls back the completion.

```
check_completion (buyplan_workflow/buyplan_approval.py)
    |
    v  [plan transitions to COMPLETE]
record_buyplan_purchase_history (purchase_history_service.py)
    |
    +---> Idempotency guard: plan.purchase_history_recorded_at IS NOT NULL → skip
    |
    +---> For each VERIFIED buy_plan_line:
    |       +---> resolve material_card_id (from requirement, then from offer)
    |       +---> upsert_purchase(db, company_id, material_card_id,
    |                             source="buy_plan", unit_sell, quantity,
    |                             purchased_at=plan.completed_at,
    |                             source_ref=sales_order_number)
    |             --> DB: UPSERT customer_part_history
    |
    +---> plan.purchase_history_recorded_at = now(); db.flush()  [idempotency stamp]
    |
    +---> refresh_matches_for_cards(db, affected_card_ids)  [immediate re-match]
              |
              +---> For each affected card: DB: SELECT newest offers (capped at 5)
              +---> find_matches_for_offer(db, offer) → proactive_matching.py
              +---> DB: INSERT proactive_matches (engine dedup prevents duplicates)
```

The buy plan is the **single source of truth** for CPH. The prior offer-won and
quote-won hooks that previously wrote CPH rows have been retired; all confirmed
customer purchases now flow through buy-plan completion. Historical rows written
by the old hooks are preserved (their `source` values remain valid).

Historical completed plans that predate this feature are backfilled one-time via
`python -m app.management.backfill_buyplan_cph`. The command records CPH for every
COMPLETED buy plan whose `purchase_history_recorded_at` is NULL, committing per plan.
It is idempotent and safe to re-run — plans that already have the stamp are skipped.

## 7. Proactive Matching

`customer_part_history` (CPH) is the backbone for proactive matching. As of the
buy-plan CPH feed (§ 6 above), CPH rows are fed by buy-plan completion rather than
offer/quote won hooks (those hooks have been retired). The matching engine itself
is unchanged.

```
APScheduler (daily) OR user trigger
    |
    v
proactive_matching.py
    |
    +---> DB: SELECT offers WHERE status='active' AND recent
    +---> DB: SELECT customer_part_history (who bought this MPN?)
    |         (source="buy_plan" for all new rows; legacy values also present)
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

**Immediate re-match on completion.** `refresh_matches_for_cards` (called from
`record_buyplan_purchase_history`) runs `find_matches_for_offer` against the
newest active offers for every card touched by the completed buy plan, so new
proactive matches surface on the Proactive tab without waiting for the daily cron.
Bounded to 5 offers per card (per_card_limit); the engine's own dedup prevents
duplicate matches.

**Immediate re-match on offer approval.**
`proactive_matching.trigger_rematch_on_offer_approval(db, offer)` closes a separate
watermark gap: a batch scan only ever sees an offer once, at `Offer.created_at`; an
offer created `pending_review` is excluded from the scan's live-status filter, so if
it's approved after the watermark has advanced past its `created_at`, it stays
invisible to every future batch scan too. The hook runs a targeted single-offer
`find_matches_for_offer(offer.id, db)` in its own commit/rollback (a re-match failure
never blocks the caller's approval transaction) and is a no-op for offers without a
`material_card_id`. Wired into all three offer-approval paths: the htmx offers CRUD
`approve`/`promote` actions (`app/routers/htmx/offers/crud.py`) and
`approve_offer` (`app/routers/crm/offers.py`).

**Hotlist → Proactive (monitor without purchase history).** The CPH path returns
no matches when a customer has never bought the part (`_find_matches` needs CPH
rows). A **HOTLIST** requisition (`RequisitionStatus.HOTLIST`) is an explicit
salesperson request to watch a part/customer, so `find_matches_for_offer` runs a
SECOND seeding pass — `_find_hotlist_matches` (`proactive_matching.py`) — that needs
NO history:

```
find_matches_for_offer(offer, db)
    +---> _find_matches(...)            # CPH-gated (purchase history)
    +---> _find_hotlist_matches(...)    # NEW: seeds from active HOTLIST reqs
            |   JOIN Requisition(status='hotlist')
            |        -> Requirement(material_card_id == offer.material_card_id)
            |        -> CustomerSite(is_active) -> Company(account_owner_id NOT NULL)
            +---> reuse suppression (do_not_offer) + dedup (company_id)
            +---> baseline match_score=60 (explicit monitor, no history to weight)
            +---> DB: INSERT proactive_matches (status=NEW), ActivityLog
    return cph_matches + hot_matches    # deduped on company_id across both passes
```

The seeded `ProactiveMatch` carries the hotlist `requisition_id` and the company's
`account_owner_id` as salesperson, and surfaces on the **existing Proactive list**
with the same one-click-send pipeline (`proactive_email.py` → Graph sendMail) — no
new surface. So a HOTLIST req turns "an offer arrived for the part you're watching"
into a one-click outbound, even for a customer with zero purchase history. (Auto-send
on a hotlist hit is intentionally out of scope — the salesperson confirms the send.)

### Unified AI Email Drafting (RFQ rephrase · vendor reply · follow-up)

```
app/services/email_drafting.py :: draft_email(kind, context)
    kind="rfq_rephrase" --> ai_service.rephrase_rfq (Haiku) ; fallback = original body
    kind="follow_up"    --> claude_text (Haiku)            ; fallback = template string
    kind="vendor_reply" --> claude_json (Haiku)            ; fallback = None (blank box)

Surfaces (all human-edit-before-send; nothing auto-sends):
  RFQ compose  "AI Rephrase" button
    POST /v2/partials/requisitions/{req_id}/ai-rephrase-email
      --> draft_email("rfq_rephrase") --> <script> sets #rfq-body-textarea
  Vendor response card  "AI Draft Reply" button
    POST /v2/partials/requisitions/{req_id}/responses/{response_id}/ai-draft-reply
      --> draft_email("vendor_reply") --> reply_compose.html into #reply-area-{id}
    POST /v2/partials/requisitions/{req_id}/responses/{response_id}/send-reply
      --> Graph /me/sendMail (as user; TESTING bypass) ; marks response reviewed
  Follow-up compose  "AI Draft" button
    POST /v2/partials/follow-ups/{contact_id}/ai-draft
      --> draft_email("follow_up") --> <script> sets #follow-up-body-{id}
```

Gives `rephrase_rfq` and `VendorResponse.classification` their first live consumers.
All paths degrade gracefully when Claude is unavailable (cost_bucket="email_drafting").

### Qualify-with-AI (offer pre-fill + ask-the-vendor for what's missing)

```
Offer row kebab "Qualify with AI" (shown only when offer.vendor_response_id set)
  GET /v2/partials/sightings/{requirement_id}/offers/{offer_id}/qualify-ai
    --> parse_vendor_response(linked email) --> extract_draft_offers
        --> pre-fill the offer form (AI fills EMPTY fields only; saved values win)
    --> offer_qualification.compute_qual_gaps(prefill, condition)
        --> condition-aware checklist (genuine gaps pre-checked)
    --> qual_request_modal.html : pre-filled offer form (same save path as edit) +
        _qual_checklist.html (gap checkboxes + user-addable custom items via "+ Add item")
  POST .../offers/{offer_id}/qualify-ai/draft-request
    checked_items[] (AI gaps kept) + custom_items[] (user's own, not AI-suggested)
    --> draft_email("qual_request") --> reply_compose.html in #qual-compose-{offer_id}
  Send → existing send-reply path (Graph as user; TESTING bypass)
    --> NEW: DNC hard-block in send_reply_htmx (SiteContact.do_not_contact) — never emails DNC vendors
```

**DNC hard-block parity across vendor-send paths.** The canonical idiom — query
`SiteContact` by `func.lower(email) == recipient.lower()` with `do_not_contact.is_(True)`;
if matched, do NOT call Graph `/me/sendMail` — is enforced on every vendor-send path:
`send_reply_htmx` (recipient `vr.vendor_email`), `send_follow_up_htmx` (recipient
`contact.vendor_contact`), and `send_email_reply` (recipient form `to`) each return the
rose "do-not-contact" partial *before* the TESTING gate / token fetch; the service path
`email_service.send_batch_rfq` skips the recipient (`status="skipped"`, `error="do-not-contact"`)
and continues. The resell `submit_outreach_email` path delegates to `send_batch_rfq`, so a
DNC buyer is recorded `ExcessOutreachStatus.FAILED` with `send_error="do-not-contact"` (Phase 2
send-truthfulness — NOT `NO_RESPONSE`, which is genuine buyer silence only) and never emailed.

**Resell outreach send truthfulness (Phase 2).** `_finalize_outreach_send` (the shared
send+stamp step behind the background `run_outreach_email_send` and the inline
`submit_outreach_email`) no longer collapses failures to `NO_RESPONSE`. A skipped/DNC
recipient or a per-buyer send error → `FAILED` + the result error persisted in `send_error`;
a total `send_batch_rfq` outage → all pending rows `FAILED` + the exception text (never
stranded `sending`); a delivered row whose Graph-id lookup came back empty stays `SENT` with a
"reply-matching degraded" `send_error` note. The send OUTCOME (status + graph ids) commits
BEFORE any bookkeeping, so a later activity/cadence write can't roll back a delivered `SENT`;
if that outcome commit itself fails it is snapshotted + re-applied in a fresh transaction (a
delivered send is never reverted to `sending`). The "Emailed" ActivityLog + cadence bump run
only for `SENT` buyers (gated at the call site). Every send persists its exact subject/body
(`send_subject`/`send_body`, migration 195) so the Retry guard can match a customized-subject
campaign. A Retry action (`retry_outreach_send`, background) and a nightly stale-`sending` sweeper
(`sweep_stale_sending_outreach`, flips aged `sending`→`interrupted`) close the durability gaps —
retry re-runs `_find_sent_message` (matched on the PERSISTED subject) BEFORE resending so an
already-delivered row is reconciled to `SENT`, never double-sent; a lookup that RAISES is the
UNKNOWN case → the row is left `interrupted` and nothing is resent (never assume not-sent); the
resend refreshes `created_at` so the stale sweeper can't flip an in-flight retry. An inbound
reply carrying a bid flips an `OPEN` list to `COLLECTING`
(mirroring `submit_offer`), and `expire_overdue_lists` isolates each list's flip+mirror+commit so
one bad list can't abort the nightly sweep.

Ownership-guarded via require_requisition_access (offer.requisition_id, owner_id=entered_by_id).
Read-only pre-fill; never auto-saves or auto-sends. Loop-closes: a vendor's reply becomes a new
linked VendorResponse, so re-opening Qualify-with-AI fills the remaining fields.

**Resell hot-path performance (Phase 6a — pure refactors, no behavior change).**
`buyer_affinity_service.rank_buyers_for` (#19) now narrows the candidate universe in SQL to
(won-offer history ∪ commodity-tag overlap ∪ reachable-engagement tiebreak) instead of hydrating
every non-blacklisted VendorCard, and only the 4 read columns are projected. The tag-overlap
branch is dialect-split and CASE-INSENSITIVE (`commodity_tags` are stored verbatim by the LLM
material-analysis writers — Title/mixed case, only `str().strip()` — so an exact-case match
would silently drop a real commodity buyer on Postgres): PostgreSQL uses a correlated `EXISTS`
over `jsonb_array_elements_text(commodity_tags)` that lowercases each stored element and matches
it against the lowercased target set (the JSONB `?|` any-key operator is exact/case-sensitive and
the GIN index `ix_vendor_cards_commodity_tags_gin` can't serve a `lower()` match, so this narrows
via a scan — still only overlapping cards return); SQLite keeps the Python set-intersection
fallback (`_tag_overlap_candidate_ids`, which already lowercases both sides). The cold engagement
tiebreak is filled by `_top_reachable_engagement_ids`, which pages cards in `engagement_score`
order THROUGH the reachability gate until it has up to `limit` reachable ids — a fixed `limit*3`
cap taken BEFORE that gate would starve the panel when the top band is mostly unreachable/DNC. `resell_outreach_service` (#20) precomputes the offered-lines snapshot ONCE per
campaign (`_campaign_parts_snapshot` → `whole_list_snapshot` + `by_line`) instead of a
per-(buyer×line) `ExcessLineItem` query, with `_parts_snapshot` now an in-memory lookup and a
SINGLE row flush after the per-buyer loop (the same-buyer dedup is kept in-memory since
autoflush is off); the persisted `parts_included` keys are byte-identical. `routers/resell.py`
adds the CSV-export twin joinedloads to the owner Offers render (`_offers_context`:
offerer_company / offerer_vendor_card / lines→excess_line_item) and the Outreach tracker render
(`_outreach_tracker_context`: target_vendor_card / excess_line_item / submitted_by_user, inside
the 3s poll); `_award_response_context` loads the line items once and threads them into both
`_detail_context` + `_offers_context` (one line-items SELECT for the combined render); and the
reply viewer replaced the whole-list `_replies_context` map with a narrow
`_conversation_replies(db, conversation_id)` single-conversation query (served by
`ix_vr_conversation`, migration 200).

## 8. Activity Digest (AI Timeline Summary)

```
Browser (Activity tab loads) — lazy HTMX placeholder fires GET
    |
    +---> GET /v2/partials/requisitions/{req_id}/activity-digest?force=0
    |     GET /v2/partials/customers/{company_id}/activity-digest?force=0
    |
    v
htmx/insights_views.py: requisition_activity_digest() / customer_activity_digest()
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

### 8a. AI Insights refresh — interactive Claude budget (P2.8)

The four "Refresh AI insights" HTMX endpoints in `app/routers/htmx/insights_views.py`
(requisition/vendor/customer/pipeline panels) call `knowledge_service.generate_insights` /
`generate_vendor_insights` / `generate_company_insights` / `generate_pipeline_insights` with
`interactive=True`. That flag tightens the shared `_regenerate_insights` Claude call to a
**~25s timeout, single attempt (no retries)** (`_INTERACTIVE_TIMEOUT_SECONDS = 25`,
`_INTERACTIVE_MAX_ATTEMPTS = 1` in `knowledge_service.py`) so a slow/overloaded API can't hold
the HTTP request open for the default `claude_structured` worst case (30s timeout × 3 attempts
≈ 96s). On timeout/failure the call returns `[]` and the route falls back to serving the
existing cached insights (`entries or get_cached_*_insights(...)`) rather than erroring.
Non-interactive callers (`interactive=False`, the default) keep the original uncapped
budget, but none exist today — the dormant KB-insight refresh job
(`knowledge_jobs._job_refresh_insights`) was deleted 2026-07-06 (no UI consumer, burned
Anthropic API cost when enabled; recoverable from git history).

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
    +---> Reverse-maps m365_error_reason -> error_action via
    |     m365_status.action_for_reason():
    |       REASON_AUTH         -> "reconnect" (sign-in dead; link to /auth/login)
    |       REASON_TRANSIENT    -> "wait" (self-heals on next cycle)
    |       REASON_SUBSCRIPTION -> None (informational; webhook channel)
    |       None / legacy raw   -> None / "wait" (never wrongly "reconnect")
    |
    +---> Returns: {connected, last_scan_at, is_stale, token_ok,
    |               error_reason, error_action, health}
    |
    v
Two surfaces:
    1. Requisitions list: shared/inbox_disconnected_banner.html
       (shown when health=error or is_stale=True; included at top of list.html)
    2. Settings → Profile: settings/_mailbox_sync_card.html
       (connected: friendly sync status + "Scan now" button + last-checked
        timeago; on error, branches on error_action — "reconnect" shows the
        accurate reason + a Reconnect Microsoft 365 link, otherwise an amber
        self-healing note. not connected: a "Mailbox not connected" empty state
        with a Connect Microsoft 365 button. m365_error_reason is ALWAYS a
        friendly, actionable sentence — never a raw str(exception).)

m365_error_reason vocabulary (app/services/m365_status.py is the single source
of truth — all four writers route through it):
    - token_manager.get_valid_token + core_jobs token-refresh failure
      -> reason_for(): auth-signal errors -> REASON_AUTH, else REASON_TRANSIENT
    - core_jobs inbox-scan timeout -> REASON_TRANSIENT; scan exception
      -> reason_for() (classified, never raw str(e))
    - webhook_service subscription renewal >= 3 fails -> REASON_SUBSCRIPTION
A successful inbox poll (email_jobs._scan_user_inbox) clears a self-healed
token/scan reason (NOT the subscription one — separate webhook lifecycle) so
the card stops showing a resolved error.

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

### Profile mutation endpoints (any logged-in user)

Settings → Profile lets a user edit their own display name, 8x8 extension, and
notification preferences. All three handlers live in `htmx_views.py`, take
`require_user` (the current user, NOT admin-only), commit, and emit a `showToast`
HX-Trigger via the shared `settings_toast()` helper.

```
POST /api/user/profile            (form: name, extension)
    htmx_views.update_user_profile()
    +---> name.strip(): empty OR >255 chars -> 400 JSON {error, status_code, request_id}
    +---> extension.strip(): >20 chars       -> 400 JSON {error, ...}
    +---> sets user.name + user.eight_by_eight_extension (empty extension clears it)
    +---> db.commit(); settings_toast("Profile updated.") -> 200 (empty body + HX-Trigger)

POST /api/user/toggle-buyplan-email
    htmx_views.toggle_buyplan_email()
    +---> flips user.notify_buyplan_email_enabled; toast "...enabled/disabled."
          When False, buyplan_notifications._send_email skips the Graph send (in-app
          activity_log row still written — see §8 notification tiers).

POST /api/user/toggle-new-offer-alert
    htmx_views.toggle_new_offer_alert()
    +---> flips user.notify_new_offer_alert_enabled; toast "New-offer alerts enabled/disabled."
          When False, alerts.sources.offers.OfferConfirmedSource.count_for_user /
          new_items_for_user return 0/empty, suppressing the FYI nav badge for that user.
```

These clone the existing `toggle_8x8` (`POST /api/user/toggle-8x8`) pattern but route
their toast through `settings_toast()`.

---

## 9a. Settings → Connectors Tab (admins + MANAGE_CONNECTORS holders)

Unified credential + health management surface. Replaces the old **Sources** tab and
the orphaned **API Keys** tab: both legacy routes (`/v2/partials/settings/sources` and
`/v2/partials/settings/api-keys`) 302 → `/v2/partials/settings/connectors`.

Gated on the `MANAGE_CONNECTORS` capability (admins always qualify via `user_has_access`),
not bare `is_admin` — the SET-06 fix that made the capability actually gate something.
`MANAGE_CONNECTORS` is deliberately NOT in the interactive role defaults (unlike
send_rfq / approve_offers / export_data): connector credentials + `is_active` are
workspace-global shared state, so it is a per-user grant an admin sets explicitly, never a
blanket buyer-tier default. The
tab button (`settings/index.html`, `can_manage_connectors` flag), the `settings_partial`
default-tab redirect, the tab + card-refresh + test-all endpoints, and the per-source
mutations (`sources.toggle_api_source` / `toggle_source_active` / `update_source_credentials`)
all honor the same gate, so a holder gets a fully functional tab with no dead 403 controls.
Clay OAuth connect/disconnect stays admin-only (a backend-wide authorization); non-admin
holders see Clay status read-only.

```
GET /v2/partials/settings/connectors
    |
    v
htmx_views.settings_connectors_tab  (require_access(MANAGE_CONNECTORS); 403 without it)
    |
    +---> _build_connector_groups(db, request)
    |       |
    |       +---> db.query(ApiSource).order_by(display_name).all()
    |       |     (dead rows excluded in-process via the shared module-level
    |       |      htmx_views._DEAD_CONNECTORS frozenset — single source of truth
    |       |      referenced by both _build_connector_groups and connectors_test_all:
    |       |      rocketreach_enrichment / clearbit_enrichment)
    |       |
    |       +---> per source: _enrich_source(source, db)
    |       |       |
    |       |       +---> connector_service.control_type(source)
    |       |       |       → "key" | "oauth_clay" | "multi_field" | "browser_login"
    |       |       |         | "scopes" | "keyless" | "planned"
    |       |       |
    |       |       +---> credential_service.credential_is_set / get_credential
    |       |       |       (masked display only)
    |       |       |
    |       |       +---> clay_oauth.is_connected() / needs_reconnect()
    |       |       |       (clay_enrichment only)
    |       |       |
    |       |       +---> connector_service.connector_state(source, ...)
    |       |               → "live" | "error" | "off" | "needs_setup" | "untested"
    |       |                 | "needs_reconnect" | "planned"
    |       |
    |       +---> connector_service.connector_group(source) → group key
    |       |     Buckets emitted in GROUP_ORDER:
    |       |       Part Sourcing / Enrichment / AI / Communications /
    |       |       Browser Workers / Manual
    |       |     7 planned connectors (findchips/future/heilind/lcsc/rochester/
    |       |       thebrokersite/verical) render as read-only "Planned" cards
    |       |       (no credential form, no toggle, no Test button)
    |       |
    |       v
    |     connector_groups: [{key, label, sources: [enriched_dict]}]
    |
    v
settings/connectors.html  (grouped card grid)
```

**Per-card controls (by `control_type`):**

| `control_type` | UI |
|---|---|
| `key` | API key input (masked), Save via `hx-ext="json-enc"` |
| `oauth_clay` | Connect / Reconnect / Disconnect buttons (Clay OAuth) |
| `multi_field` | 4-field form (8×8: API key + username + password + PBX id) |
| `keyless` | Enable toggle only (no credential; e.g. `ai_live_web`) |
| `browser_login` | Status-only (ICS/NC workers — managed by browser-worker containers) |
| `scopes` | Status-only (Azure AD / Teams — managed by Azure AD admin) |
| `planned` | Read-only label, no controls |

Every non-planned card has an **enable toggle** (`POST /api/sources/{id}/activate`) and a
**Test** button (hidden when no real test path exists). Both return JSON; the swap unit is
a refreshed card partial:

```
PUT  /api/sources/{id}/activate          → JSON {ok, is_active} + showToast HX-Trigger
POST /api/sources/{id}/test              → JSON {ok, error} + showToast HX-Trigger
     (status + results_count + elapsed — the button uses hx-swap=none, so the toast
      is the only feedback; single source of truth for a source's ok/error status)
PUT  /api/sources/{name}/credentials     → JSON {saved} + showToast HX-Trigger
GET  /v2/partials/settings/connector-card/{id}  → single card HTML (swap target)
POST /v2/partials/settings/connectors/test-all  → OOB bundle of refreshed cards
     + an OOB summary line ("Tested N · M failed") into #test-all-summary
     (skips inactive / untestable; per-source failures tolerated, never abort)
```

**Testability & Test-all concurrency.** A source is "testable" iff a real test path
exists — `services.connector_registry.source_has_test_path` (= `get_connector_for_source`
can build a probe: credential present, or a keyless test hook such as
`AIWebSearchConnector` for `ai_live_web`; P4.1 moved connector lookup out of
`routers/sources.py` into `app/services/connector_registry.py` so `health_monitor.py`
stopped reaching into the router for it — `routers/sources.py` still imports it back
under its original private name for its own Test-button call site). Keyless sources
with no hook (`sam_gov_enrichment`, `stock_list_import`)
are NOT testable and hide their Test button (previously they falsely reported OK). A
keyless probe's ok/error result IS persisted (the old `has_env_vars` gate was dropped).
`run_source_test` = `_probe_source` (network only, never raises) + `_persist_test_result`
(sequential DB write). `connectors_test_all` fans the probes out CONCURRENTLY with a
per-probe timeout + an overall budget (< the button's raised `hx-request timeout:120000`)
and polls `request.is_disconnected()` to cancel an abandoned sweep — the old sequential
loop blew the 15s htmx client timeout with >4 live connectors and discarded every result.

Credential save uses `hx-ext="json-enc"` to PUT a nested JSON body
(`{credentials:{ENV:val}}`) to `PUT /api/sources/{name}/credentials` (HTMX json-enc
extension — not a standard form POST). On success the card re-fetches via
`GET /v2/partials/settings/connector-card/{id}`.

**Feedback & safety polish (Task 5):**
- **Success toasts** — `activate` and `credentials` handlers attach a `showToast`
  HX-Trigger via `htmx_views.settings_toast` (lazy import to avoid a circular import):
  "Key saved." / "Credentials saved." / "<name> enabled/disabled." Clay `disconnect`
  (router `clay_oauth.py`) sets the same HX-Trigger on its 302 ("Clay disconnected.").
- **Destructive confirms** (`hx-confirm`, client-side) — Clay **Disconnect** ("Disconnect
  Clay enrichment for everyone? …"); the enable toggle gains a confirm **only when the
  card state is `live`** (Jinja-conditional, single-quoted, name `|e`-escaped) since
  disabling a live source reduces search/enrichment app-wide.
- **Empty state** — when `connector_groups` is empty (no `ApiSource` rows) the tab renders
  a house-style ghost-icon empty block ("No connectors yet.") instead of a bare header.
- **Unified status vocabulary** — header counter and group counts both read "N need setup"
  (was "need attention" / inconsistent singular); the per-card pill labels remain canonical.

The degraded-source banner on the Part Dossier (`§ 2a-bis`) deep-links
`/v2/settings` when live-market connectors are down — that link now routes to the
Connectors tab (default tab is `connectors`).

---

## 9b. User Management (allowlist login, per-user access, audit)

Admin-administered identity + access, surfaced at **Settings → Users** (admin only).
Three intertwined flows: the login allowlist gate (with invite adoption), the per-user
access model and its enforcement, and the audit trail.

### Allowlist login gate + invite adoption

The OAuth callback (`app/routers/auth.py callback`) is the single choke point. Gated by
`ENABLE_USER_ALLOWLIST` (`app/config.py`, default True):

```
Azure AD OAuth callback (email normalized to lowercase)
    |
    +-- no users row for this email?
    |       allowed = (email in ADMIN_EMAILS) or (not ENABLE_USER_ALLOWLIST)
    |       |
    |       +-- allowed     → auto-provision a User row (legacy posture / admin)
    |       +-- NOT allowed → 302 /auth/access-denied  ("Access not provisioned")
    |
    +-- users row exists, but is_active = False
    |       → 302 /auth/access-denied?reason=disabled  ("Account disabled")
    |
    +-- users row exists, azure_id is NULL (an INVITED, pre-provisioned row)
    |       → INVITE ADOPTION: bind profile.id onto user.azure_id on this first login.
    |         The invited role is left untouched (an invited trader stays a trader).
    |
    +-- (always) stamp user.last_login_at = now
```

`/auth/access-denied` is a self-contained branded page (no app shell, renders with no
session); `?reason=disabled` switches the copy. An invited row is created by an admin via
the Users tab (azure_id NULL, last_login_at NULL) and shows status **Invited** until its
first OAuth login adopts the azure_id.

### Per-user access model + enforcement

Access is a layered resolution computed by `dependencies.user_has_access(user, key, db)`
over `constants.AccessKey` (10 module keys + 6 capability keys):

```
admin role?                       → ALWAYS granted (every key)
key == ops_verification?          → VerificationGroupMember.is_active  (single source of truth)
explicit override present?        → User.access_overrides[key]         (override wins)
otherwise                         → key in ROLE_ACCESS_DEFAULTS[role]   (role default)
```

`access_overrides` stores *only* explicit grants/revokes — an absent key falls through to
the role default (defaults preserve prior behavior, so the model is a no-op until an admin
sets an override). `ops_verification` is special: it is never stored in `access_overrides`,
and the access editor writes `VerificationGroupMember` instead (default acts as off —
membership is curated, never "follow role"). Enforced at four points:

| Where | Mechanism |
|---|---|
| **Bottom nav** (hide revoked sections) | `_base_ctx` calls `admin.users.module_access_map(user)` → `{nav-id: bool}` `access` map; `shared/mobile_nav.html` skips any section where `access[id]` is False (default-show if absent). |
| **Module full-page route** | `v2_page` maps the resolved `current_view` to its module `AccessKey` (`_VIEW_ACCESS`; CRM sub-views all gate on CRM); a denied view 302-redirects to the user's FIRST allowed module (`_MODULE_ENTRY_URLS` order) — target is always allowed, so no loop. No-access-at-all → 403 with logout link. Un-gated views: settings/quotes/follow-ups/tickets. |
| **Module partial entry route** | `require_access(<module>)` dependency on each of the 10 nav-module partial routes (parts/sightings/materials/search/buy-plans/resell/crm/proactive/prospecting/my-day workspaces). |
| **Module SUB-partial chokepoint** | `ModuleAccessMiddleware` (`app/main.py`, inner of `SessionMiddleware`) closes the gap where a revoked user could still READ a module's *sub*-partials by direct URL (those carry only `require_user`). It resolves the request path through the pure `app.access_paths.module_key_for_path` and, if a guarded prefix matches and the session user lacks the key, returns a plain 403. **Only EMPIRICALLY module-exclusive prefixes are guarded: `crm`, `resell`, `proactive`, `prospecting`, `my-day`.** The other five entry-prefixes (`parts`, `sightings`, `materials`, `search`, `buy-plans`) are SHARED cross-module (embedded by other modules' templates) and DELIBERATELY un-gated, as are all CRM *data* partials (customers/contacts/vendors/vendor-contacts) and capability/global/global-search partials — gating them would over-block. Admins and logged-out requests pass through; a DB session opens only when a guarded prefix matches. |
| **Capability action** | `require_access(<capability>)` on: RFQ-send (`htmx_views.rfq_send`, `sightings.sightings_send_inquiry`); offer approve/reject/reconfirm (`crm/offers.py` + the Sightings `review_offer`/`reconfirm_offer` wrappers); quote-builder Excel/PDF exports (`quote_builder.py`, `EXPORT_DATA` — open to sales, single-deal customer documents); the whole Connectors surface — `MANAGE_CONNECTORS` on `sources.test_api_source` / `toggle_api_source` / `toggle_source_active` / `update_source_credentials` and the `settings.py` connectors tab + `connector_card_partial` + `connectors_test_all` (SET-06). |
| **Bulk dataset export (ISS-022)** | `require_access(AccessKey.EXPORT_BULK_DATA)` — manager/admin only — on the five bulk CSV export routes: companies + contacts (`crm/export.py`), vendors (`htmx/vendors.py:vendors_export`), requisitions (`htmx/requisitions.py:requisitions_export`), sightings (`sightings.py:sightings_export`). `ROLE_ACCESS_DEFAULTS[MANAGER]` is the only interactive role holding this key by default (`_INTERACTIVE_DEFAULTS | {EXPORT_BULK_DATA}`); buyer/sales/trader do not, even though they hold `EXPORT_DATA`. The four list toolbars (vendors/requisitions/sightings/customers) hide their "Export CSV" controls via the `can_export_bulk_data(user)` Jinja global (`template_env.py`, mirrors `has_buyer_role`/`can_approve_buy_plans`) — single source of truth with the route gate. |

`require_access(key)` is a factory returning a dependency that depends on `require_user`
and raises 403 unless `user_has_access` passes (admins always pass). The
`ModuleAccessMiddleware` SUB-partial chokepoint enforces the SAME `user_has_access`
decision one layer earlier (at the ASGI level) for module-exclusive fragment URLs that
have no per-route `require_access`, so module revocation is airtight without a gate on
every sub-partial. CRM data partials (customers/contacts/vendors) remain reachable by
design — they are shared cross-module, so revocation hides the CRM section/nav and blocks
crm-prefixed partials only.

### Admin Users tab (CRUD + access editor)

`app/routers/admin/users.py` (admin-only via `require_admin`; the agent service account is
404/uneditable). Every mutation appends a `UserAdminAudit` row
(`services.user_admin.record_user_audit`) and re-renders the relevant partial; validation
failures re-render with an inline error banner at HTTP 400.

```
GET  /v2/partials/settings/users            → users.html table (htmx_views.settings_users_tab; 403 non-admin)
POST /api/admin/users/invite                → create interactive user (status Invited)
POST /api/admin/users/{id}/role             → change role
POST /api/admin/users/{id}/active           → activate / deactivate
POST /api/admin/users/{id}/buyplan-approver → grant/revoke the per-user buy-plan approval right (User.can_approve_buy_plans); APPROVAL_GRANT/APPROVAL_REVOKE audit
POST /api/admin/users/{id}/prepayment-approver  → grant/revoke prepayment approval + optional dollar limit (User.can_approve_prepayments / prepayment_approval_limit)
POST /api/admin/users/{id}/sales-order-approver → grant/revoke QP Sales-section approval right (User.can_approve_qp_sales — column renamed by SP-2 migration 164; route/handler name kept for back-compat, spec §13); APPROVAL_GRANT/APPROVAL_REVOKE audit (QP Phase C2a)
POST /api/admin/users/{id}/po-approver          → grant/revoke QP Purchasing-section approval right (User.can_approve_qp_purchasing — column renamed by SP-3 migration 166; route/handler name kept for back-compat); APPROVAL_GRANT/APPROVAL_REVOKE audit
POST /api/admin/users/{id}/purchase-order-approver → grant/revoke deal-level PO approval right + optional dollar limit (User.can_approve_purchase_orders / purchase_order_approval_limit); APPROVAL_GRANT/APPROVAL_REVOKE audit (SP-3, mirrors prepayment-approver)
GET  /api/admin/users/{id}/access-panel     → user_access_panel.html (per-user access editor modal)
POST /api/admin/users/{id}/access           → grant/revoke/reset ONE key (value ∈ on|off|default)
GET  /api/admin/users/audit                 → users_audit.html (audit-log viewer modal)
```

Each user row shows a derived status (**Invited** | **Active** | **Disabled**). The access
editor renders module + capability rows, each with state `on`/`off` (explicit override) or
`default` (follow role) plus the resolved effective value. **Self-protection invariants**
(never lock everyone out): an admin can't demote or deactivate themselves, and the last
active admin can't be demoted or deactivated by anyone — the last-admin guard counts active
admins `FOR UPDATE` (row-locked) so concurrent demote/deactivate requests can't race past it.

### Audit trail

Every Users-tab mutation writes one append-only `user_admin_audit` row recording
`actor_id` (the admin), `target_user_id`, `action` (`constants.UserAuditAction`:
invite/role_change/activate/deactivate/access_grant/access_revoke), a JSON `detail`
(e.g. `{"from","to"}` for a role change, `{"key","value"}` for an access change), and
`created_at`. The viewer (`GET /api/admin/users/audit`) loads the latest 200 rows
newest-first, batch-resolving actor + target users in one query (no N+1); a SET-NULL'd
actor renders as "system". `actor_id` is SET NULL (trail survives the admin's deletion);
`target_user_id` CASCADEs with the user.

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
    |     click-to-call bucket (10/min) so channels never starve each other.
    |     Enforced by app.rate_limit.check_rate_limit — an atomic fixed-window
    |     INCR counter on the shared Redis substrate (app.cache.intel_cache), so
    |     the limit holds across worker processes/restarts; degrades to a
    |     per-process in-memory counter when Redis is down (same fallback posture
    |     as the slowapi limiter). call-outcome stamps use a third bucket.
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
`company_contact_rows` orders `is_archived ASC, is_priority DESC, is_primary DESC,
full_name` — priority contacts surface to the top, archived sink to the bottom
(still shown). Legacy rows (`contact is None`) are appended after, never sorted.

**Per-entity authz hardening (Phase 1, multi-user go-live).** Every state-changing
entity-by-id route is gated on per-entity ownership BEFORE mutating (the gate precedes
404/400/validation branches and any AI-spend/side-effect). Company/site/contact mutations
use `can_manage_account(user, company, db)` (owner OR site-owner OR collaborator OR
manager/admin); company_merge/merge-preview/merge-form gate BOTH the keeper and the
duplicate. Requisitions/quotes/buy-plans/offers scope to the parent **Requisition.created_by**
via `get_req_for_user` / `get_quote_for_user` / `get_buyplan_for_user` (BuyPlan→Requisition
join; 404-not-403 so existence isn't leaked) and `require_requisition_access`; owner
*reassignment* is `is_manager_or_admin`-only. Requisition list/detail visibility uses
`RESTRICTED_ROLES` (SALES+TRADER see only own), not SALES alone. The `attachments_extra`
company-access helper performs a real `can_manage_account` check (was a no-op). Restricted
roles get 403 (company routes) or 404 (requisition-derived) on entities they don't own.

**Per-entity authz hardening (Phase 1b — `fix/authz-hardening`, code-only).** A second pass
closed 13 remaining object-level / privilege-escalation gaps, deduped into 9 fix groups, each
REUSING the helpers above (no new ad-hoc checks):
- `edit_company` (`POST /v2/partials/customers/{id}/edit`) — primary-owner *reassignment* and
  parent-company (hierarchy) edits now require `can_manage_account_team` (collaborators / site-
  owners can no longer seize ownership), gated only when the value actually changes.
- `create_company` (`POST /v2/partials/customers/create`) — assigning `owner_id != self` requires
  `is_manager_or_admin` and validates the target is an active `User` (else 400), matching the bulk
  assign-owner path.
- `ai.py` site-linked prospect records — `save_prospect_contact`, `delete_prospect_contact`,
  `promote_prospect_contact`, and `apply_freeform_rfq` resolve
  `customer_site_id → CustomerSite.company_id → Company` and require `can_manage_account`;
  vendor-linked prospects stay global. The site-prospect guard lives once as
  `dependencies.require_prospect_site_access(db, user, pc)` (shared helper) — imported by both
  `ai.py` and `htmx_views.py`, never duplicated.
- `htmx_views` vendor-prospect twins — `vendor_prospect_save` / `vendor_prospect_promote` /
  `vendor_prospect_delete` (`POST|DELETE /v2/partials/vendors/{vendor_id}/ai/prospect/{prospect_id}`
  `[/save|/promote]`) are the HTMX siblings of the `ai.py` routes above and reach the same
  `ProspectContact` mutate/delete; they call `require_prospect_site_access` after the 404 check,
  before mutation, so a cross-account actor can no longer hijack a site-linked prospect by id.
- `htmx_views.proactive_do_not_offer` (`POST /v2/partials/proactive/do-not-offer`) — resolves the
  form `company_id`/`customer_site_id` to a `Company` and requires `can_manage_account` before
  inserting `ProactiveDoNotOffer`, so an arbitrary form-supplied company can no longer be
  suppressed; the auto-dismiss UPDATE is scoped to `ProactiveMatch.salesperson_id == user.id`
  (mirrors `/dismiss`) so it never wipes another owner's open matches. (The legacy JSON twin
  `POST /api/proactive/do-not-offer` was removed — the HTMX route is the sole surface.)
- `sources.parse_response_attachments` (`POST /api/email-mining/parse-response-attachments/{id}`)
  — `require_requisition_access(db, vr.requisition_id, user)` before any Sighting create/overwrite.
- `prepayment_service.create_prepayment` — `get_buyplan_for_user(db, created_by, buy_plan_id)` (the
  ownership check lives in the service so the router stays thin), so a Prepayment + routed
  ApprovalRequest cannot be attached to a buy plan the actor can't access.
- `htmx_views`: `sourcing_search_trigger` (connector spend) + `ai_rephrase_email` gated on
  `require_requisition_access`; `send_batch_follow_up` and `follow_up_badge` scope the stale-
  `RfqContact` query for `RESTRICTED_ROLES` (join `Requisition`, filter `created_by == user.id`)
  so the badge matches what the batch acts on. `send_batch_follow_up` re-renders the shared
  `follow_ups/list.html` (via `_build_follow_ups_ctx`, the same builder the list partial uses)
  so the "Send All" swap into `#main-content` leaves the refreshed queue intact instead of a
  bare success div, and surfaces the count through an `HX-Trigger: showToast`.
- `crm.quotes.create_quote` (`POST /api/requisitions/{req_id}/quote`) — `offer_ids` filtered to
  `Offer.requisition_id == req_id` (400 on any mismatch) and the `on_quote_built` requirement-
  advance query filtered the same way, so foreign offers can't enter the quote or advance another
  owner's requirement.
- `quality_plans` `qp_detail` / `qp_submit` — `_require_qp_access` loads the parent BuyPlan and
  calls `require_requisition_access(..., owner_id=qp.created_by_id, label="Quality plan")` (404 so
  a QP's existence isn't leaked).
Regression coverage: `tests/test_authz_hardening.py` (cross-account 403/404 + legitimate owner/
manager/admin allowed + per-owner data-isolation asserts for proactive / follow-ups / quote).

**Read-IDOR closure — offers/ GET partials.** Five requisition-scoped GET partial handlers in
`app/routers/htmx/offers/` (`parse_email_form`, `paste_offer_form`, `add_offer_form` in
`crud.py`; `rfq_compose`, `rfq_prepare_panel` in `rfq.py` — the parse-email/paste-offer/
add-offer forms and the rfq-compose/rfq-prepare panels) resolved the requisition via
`get_requisition_or_404` but skipped
`require_requisition_access`, so a `RESTRICTED_ROLES` (SALES/TRADER) non-owner could read another
rep's requisition name/customer/MPNs/vendor contacts by crafting a direct GET. Each now calls
`require_requisition_access(db, req_id, user)` right after the 404 check (404-not-403 so existence
isn't leaked), matching their mutating siblings in the same submodule. Regression coverage:
`tests/test_authz_offers_partials_idor.py`.

**Read-IDOR closure — CRM read endpoints (Wave 1, `fix/crm-authz-wave1`).** A module-wide sweep
found 23 CRM GET/read endpoints returning scoped data (contact/company/site PII, financials) with
only `require_user` while their mutating peers gated. Each now gates: the company/site/contact
read partials (`company_edit_form`, `company_field_edit_form`/`_display`, `company_dup_suggestion`,
`company_name_suggestion` in core.py; `site_contacts_list`, `site_edit_form` in sites.py;
`contacts_tab_add_form`, `contact_field_edit_form`/`_display`, `contact_notes_modal`,
`contact_history_modal`, `contact_files_modal`, and the `suggested-contacts` trigger/status in
contacts.py; `company_segment_tags_partial` in tags.py) load the owning `Company` and
`can_manage_account(user, company, db)` → **404** (matching `company_detail_partial`; the
suggested-contacts *poller* returns an empty `286` to stop polling instead). The JSON API
`GET /api/companies/{id}` → 404 via `can_manage_account`; `GET /api/companies` list filters on
`company_visibility_predicate` for non-managers (and threads `user` into the cached `_fetch` so
`@cached_endpoint` folds `user.id` into the key — no cross-rep cache bleed); `crm/offers.py`
`get_changelog`/`list_offer_attachments` require `require_requisition_access`, `list_review_queue`
is `is_manager_or_admin`-only; `crm/enrichment.py` `enrich_vendor_card`/`get_suggested_contacts`
require `require_buyer`. Managers/admins bypass throughout. Regression coverage: seven new
`tests/test_*_idor.py` / `test_enrichment_authz.py` modules (stranger 404/403/286 + owner 200 with
content asserts).

**Disposition (Increment 1, migration 118).** Salespeople dispose of accounts +
contacts via setter routes in `htmx_views.py` (all owner-or-admin where they touch
ownership/disposition; `is_admin = user.role == UserRole.ADMIN`, mirroring
`release_prospect`):
- `POST .../{company_id}/disposition` (`set_company_disposition`) — `_VALID_DISPOSITIONS`
  allowlist (`active`/`bucket`, invalid → 400), writes `disposition`/`disposition_reason`/
  `disposition_set_by`/`disposition_set_at`, `invalidate_prefix('company_list')`,
  re-renders `_disposition_control.html`. Reversible.
- `POST .../{company_id}/send-to-prospecting` (`send_company_to_prospecting_htmx`) →
  `prospect_claim.send_company_to_prospecting` (FOR-UPDATE lock, clears
  `account_owner_id` + sets `ownership_cleared_at`, find-or-create
  `ProspectAccount(status=SUGGESTED)` by `Company.domain`; no-domain ⇒ ownership-clear
  only, no pool row; commit/rollback). Returns the company detail partial + `HX-Trigger`
  showToast.
- `POST .../{company_id}/contacts/{contact_id}/priority` + `.../archive` —
  IDOR-scoped via `SiteContact JOIN CustomerSite WHERE company_id == company_id`
  (cross-company → 404); toggle the boolean, re-render `_priority_toggle.html` /
  `_archive_toggle.html`.
Bucket suppression is QUERY-LAYER only (never in `cadence_service.materialize_all_clocks`);
the NULL-safe exclusion lives in the shared `_needs_call_filter` (count==list invariant)
+ `cdm_company_query`'s base, with `staleness='bucket'` the lone escape hatch.

**Unified account workspace + Contacts canonical surface (IA redesign, no
migration).** The single-vs-multi-site fork is RETIRED — every account row in the
CDM left list (`_account_list.html`), regardless of `site_count`, hx-gets the SAME
`GET .../{company_id}` (`company_detail_partial`) into `#cdm-detail`, setting
`selectedId`. The left list is now a flat account picker (multi-site rows keep an
"N sites" hint badge, no accordion). Sites are reached INSIDE that unified detail
(Sites tab + the Contacts site sections), never via a left-panel drill-down.
- **Retired** (deleted): `company_header_partial`/`customers/header.html`,
  `company_sites_accordion_partial`/`customers/_sites_accordion.html`,
  `company_site_detail_partial`/`customers/site_detail.html`, and the
  `#cdm-workspace` `selectedSiteId` state. The `GET .../{company_id}/sites/{id}`
  path now resolves only to the surviving POST/DELETE site-contact CRUD routes (a
  GET → 405).
- **Breadcrumb** `Customers › {Account}` (`<nav aria-label="Breadcrumb">`) sits at
  the top of `detail.html` (reused from the retired `site_detail.html`).
- **Create / edit account (F7 — modal buttons).** The create/edit-account forms are
  surfaced as modals (they previously had NO UI entry — accounts arrived only via CSV
  import). The `+ New account` primary button (`list.html`, above the split workspace)
  `$dispatch('open-modal')` + hx-gets `GET .../create-form` into `#modal-content`;
  `create_form.html` posts to `POST .../create` targeting `#cdm-detail` (the new account's
  detail fills the right panel). The `Edit account` kebab item (`detail.html`) hx-gets
  `GET .../{id}/edit-form`; `edit_form.html` posts to `POST .../{id}/edit` targeting
  `#company-detail-{id}` with `outerHTML` (the `data-detail-root` div now carries that
  stable id, so the modal — which lives outside the root and can't use `closest` — re-swaps
  the detail in place in BOTH the workspace and a deep-linked full page). Both handlers set
  `HX-Trigger=cdmListRefresh`; a hidden listener in `list.html` reloads `#cdm-list` (honoring
  live `#cdm-filters`) so a new/renamed account shows immediately (no-ops on deep-link, where
  no listener exists). Both forms close the modal on success via `hx-on::after-request`; 4xx
  (missing name 400, duplicate 409, cross-owner 403) surfaces through the global
  `htmx:responseError` toast with the modal kept open. The shared `submit_cancel` macro
  (which navigated `#main-content`) was removed; `owner_select` remains.
- **Contacts is the default + primary right-panel surface.** `contacts_tab.html`
  (the ONLY contact-management surface, full feature set) wraps the
  `contactsView` Alpine.data component (`htmx_app.js`): a people-search + a site
  filter (`active_sites|length > 1` only) that filter the rendered rows
  CLIENT-SIDE (toggle `hidden` by `data-contact-search`/`data-site-id`, re-applied
  on `htmx:afterSettle` so a CRUD swap of the inner `#contacts-tab-list` keeps the
  filter). `_contacts_grouped_list.html` renders light per-site section headers
  (name · city · per-site cadence dot via the `site_cadence_dot` macro · `+ add
  here` → add-form with `?site_id=`); single-site → one section, no filter chrome.
  `company_detail_partial` passes `active_sites` (name-sorted) + `roles` so the
  inlined default tab matches the standalone `/tab/contacts` render.
- **Compact column layout (replaced the card grid).** Each per-site section renders
  a `<table class="compact-table">` of `contact_row` rows (`_contact_macros.html`):
  columns **Name · Title · Phone · Email · Last Contact** (primary contact ★).
  Phone is a `tel:` link and Email an Outlook-web compose deeplink — both carry the
  `data-outreach-log` attrs so `htmx_app.js` logs the touch (same path as the old
  `outreach_btn`). Each row has an Alpine `x-data='{open:false}'` **expand drawer**
  (chevron) laid out as a **dense horizontal flow** (`flex flex-wrap items-center
  gap-x-4 gap-y-2`, wraps on mobile) rather than a vertical stack: click-to-contact
  Phone (`tel:`) + Email (Outlook compose) lead, followed by Teams deeplink, WeChat
  copy-id, secondary email/phone, LinkedIn, reports-to, the inline role editor and
  cadence clocks, with a full-width **recent-note** footer (latest `ActivityLog` NOTE
  preview + a "See all notes" / "+ Add note" button that `$dispatch('open-modal')`s the
  contact-notes modal). Row-level management (edit, set-primary, priority, archive,
  DNC, merge, move, delete) lives in a per-row kebab so the row stays scannable. Cell
  padding uses the locked `.compact-table`/`.td-label` utilities (no inline `px-`/`py-`
  on `<td>`). The old per-card `contact_card` macro was **removed as dead code** — the
  `contact_row` macro is the only contact surface.
- **Buying-role vocabulary — single source `ContactRole` (`app/constants.py`).** The
  `StrEnum` `buyer/manager/engineer/planner/other` drives BOTH `CANONICAL_ROLES`
  (`htmx_views.py`, `tuple(ContactRole)`) and the `roles` Jinja2 global fallback
  (`template_env.py`); `_VALID_ROLES = frozenset(CANONICAL_ROLES)` gates the setter.
  The role editor (`POST .../contacts/{id}/role` → `set_contact_role`, validated by
  `_validate_role`: blank → NULL, non-canonical → 400) renders the five options + a
  "— clear —". Legacy DB values (`buyer_po/specifier/ap_payer/logistics/exec/technical/
  decision_maker/operations`) remain in the display-label + color maps so existing rows
  render a clean read-only chip, but they are NOT selectable and 400 if re-submitted.
  Chip colors are safelisted shades (buyer=blue, manager=violet, engineer=sky,
  planner=amber, other=gray).
- **Contact-notes modal (reuses the activity-log notes infra).** `recent_note` (latest
  `ActivityLog` NOTE per contact) is batched in `crm_service.company_contact_rows`
  (`_latest_contact_notes`, one grouped query — no N+1) and threaded through to
  `contact_row`. The drawer button opens
  `GET /v2/partials/customers/{company_id}/contacts/{contact_id}/notes-modal`
  (`contact_notes_modal` → `_contact_notes_modal.html`: feed via
  `activity_service.get_site_contact_notes`, newest-first with date + author, plus an
  add form; 404 if the contact isn't under that company).
  `POST /v2/partials/customers/{company_id}/contacts/{contact_id}/notes`
  (`add_contact_note`) is `can_manage_account`-gated (403 else), blank → inline error,
  else `activity_service.log_site_contact_note` + commit, re-rendering the modal body
  (`hx-swap='outerHTML'` into `#contact-notes-modal-body`).
- The contact edit modal (`_contact_form.html`) always renders **every** known
  contact field as a labeled input (blank → empty field), so a user sees all the
  fields they could fill in.
The in-panel **Sites tab** (`tabs/sites_tab.html` / `site_card.html`) is left intact
pending its CRUD-only rework (separate stage).

**AI organization (Increment 3, migration 120).** Durable company-dedup foundation +
review/banner surfaces — the merge engine (`company_merge_service.merge_companies`) and
the locked nightly tiering (`auto_dedup_service`: ≥98 auto / 92-97 Claude-confirm, never
merges different-`account_owner_id` accounts) are reused AS-IS.
- **Durable foundation:** `companies.normalized_name` (kept in lockstep with `name` via
  `Company._sync_normalized_name` `@validates`) + `companies.alternate_names`.
  `merge_companies` now also appends the loser's `name` + its alternates into
  `keep.alternate_names` (deduped) and backfills `keep.normalized_name` if empty, so a
  re-import of the old name fuzzy-matches the survivor.
- **Scanner** `company_utils.find_company_dedup_candidates(db, threshold, limit)` returns
  NESTED pairs `{company_a:{id,name,site_count,has_owner}, company_b:{…}, score,
  auto_keep_id}`. Dialect-split, same shape: **PostgreSQL** = pg_trgm self-join on
  `normalized_name` via `func.similarity()` (drops the 500-row O(n²) cap, uses the GIN
  index); **SQLite/fallback** = the original rapidfuzz `token_sort_ratio` scan (500-row
  cap) so the test DB stays green.
- **Review queue (home):** `settings/data_ops.html` Company-Duplicates loop rewritten
  against the nested shape (was reading FLAT `pair.name_a/id_a/sightings_a` → rendered
  blank + emitted empty merge ids; dead). Default keep/remove direction follows
  `pair.auto_keep_id`; both buttons POST `/v2/partials/admin/company-merge`. Reached via
  `GET /v2/partials/settings/data-ops` (`require_user` + explicit `is_admin` gate).
- **OEM Spec-Code Approvals "Open queue" (SET-07):** the Data Ops card links to the pending
  spec-code approval queue (`GET /admin/spec-codes/pending`, `admin/spec_codes.list_pending`,
  `require_settings_access`) with `hx-get` + `hx-push-url="true"`. Because that url is pushed
  into history, a raw browser reload / bookmark arrives WITHOUT the `HX-Request` header — the
  route content-negotiates (`request.headers.get("HX-Request") != "true"`) and serves the app
  shell via `full_page_shell(request, user, request.url.path, "settings")` (`routers/htmx/
  _shared.py`), whose `base_page.html` loader re-fires `hx-get` at this same url WITH the header
  to paint the queue; HTMX callers still get the bare `spec_codes_pending.html` partial. Coverage:
  `tests/routers/admin/test_spec_codes_pending.py` (HTMX pass renders the queue, full-page reload
  serves the shell).
- **Vendor-Duplicates loop (same template) had the identical flat-field bug** — it read
  `pair.name_a/id_a/sightings_a` while `vendor_utils.find_vendor_dedup_candidates` returns
  NESTED `{vendor_a:{id,name,sightings}, vendor_b:{…}, score}` → vendor rows rendered blank
  with empty `hx-vals` ids. Now rewritten against the nested shape, with a "suggested keep"
  hint (keeper = higher-sighting side, ties→`vendor_a`), matching the Company loop.
  `find_vendor_dedup_candidates` is now dialect-dispatched like the Company scanner:
  **PostgreSQL** = pg_trgm self-join on `normalized_name` via `func.similarity()` over
  the `ix_vendor_cards_name_trgm` GIN index — full-table coverage, no 500-row cap;
  **SQLite/fallback** = buckets candidates by a cheap first-4-chars blocking key on the
  normalized name before the pairwise scan.
- **Honest scan-error state:** the data-ops route runs each dedup scan inside its own
  `try/except` via the shared `_render_data_ops(request, user, db)` helper, which sets a
  per-scan `vendor_scan_failed`/`company_scan_failed` flag. A scan that RAISES renders a
  distinct rose error block ("Couldn't check for duplicate vendors/companies right now —
  try Refresh.") instead of swallowing the failure into the reassuring "No duplicate …
  found" clean empty state. The empty state copy now says "companies" (matched to the
  card title), not "customers".
- **Merge-button styling + post-merge refresh:** the `merge_button` macro takes an
  `is_keeper` flag — the suggested-keep direction is a filled brand chip, the drop
  direction a neutral outline (name truncation widened to 40 chars so similar-prefixed
  names never render as twin buttons). Both merge endpoints now re-render the whole
  Data Ops partial via `_render_data_ops` into `#settings-content` (so pairs referencing
  the just-merged entity drop without a manual refresh) and surface the kept-name /
  failure message via a `settings_toast` `HX-Trigger` rather than swapping a `<p>` into
  the row.
- **Click-merge bug fix (root cause):** the merge buttons used to be wrapped in a dead
  `x-data="{ merged: false }"` + `<template x-if="!merged" x-cloak>` guard. `merged` was
  never set true, so the wrapper added nothing — but because the Data Ops body is always
  delivered by an HTMX swap into `#settings-content` (which was NOT in the
  `htmx:afterSwap` `Alpine.initTree` allow-list), the `x-if` template was not reliably
  initialized: `x-cloak` (`display:none !important`) stayed applied and the buttons were
  hidden/inert, so a click hit a half-bound Alpine expression and threw instead of
  merging. Fix: the dead wrapper is removed (the buttons are pure HTMX, no Alpine needed)
  **and** `#settings-content` is added to the `htmx:afterSwap` initTree allow-list so the
  whole Settings cluster's Alpine directives re-init after every settings swap. The
  vendor-merge route now also catches `Exception` (was `ValueError`-only), matching
  company-merge, so an unexpected DB error surfaces as a toast not a 500.
- **Delete-both:** each pair row also has a rose-outline **Delete both** button
  (`delete_both_button` macro) → `POST /v2/partials/admin/vendor-delete-both` /
  `company-delete-both` (`id_a`/`id_b` form fields, admin-gated). Backed by
  `vendor_merge_service.delete_vendor_cards` / `company_merge_service.delete_companies`.
  Both split dependents into **detach vs cascade**: soft references that merely point at
  the parent (vendor: offers/stock-list-hashes/activity-log/prospect-contacts/enrichment-queue;
  company: activity-log/sightings/knowledge/prospect-accounts) are **detached** (FK NULLed,
  survive unlinked, mirroring how merge reassigns), while children declared NOT-NULL
  `ondelete=CASCADE` are **never NULLed** (that raises NotNullViolation on Postgres) and
  cascade-delete with the parent via `db.delete()` (vendor: `vendor_contacts`/`vendor_reviews`/
  `vendor_metrics_snapshot`/`buyer_vendor_stats` — the first two also via ORM
  `all, delete-orphan`; company: sites/attachments/collaborators via ORM cascade, plus
  `customer_part_history`/`excess_lists` purged explicitly since they're not ORM-cascaded).
  Both services are **fail-closed**: any detach/purge error logs at error level and
  re-raises so the route rolls back rather than deleting the parents and orphaning rows;
  the single-pair routes catch it and surface an error toast (never a 500). Native
  `hx-confirm` spells out both names ("cannot be undone").
- **Multi-select mass actions:** each dedup section is an `x-data="dedupSelect()"` Alpine
  component (registered in `htmx_app.js`, Set-based, mirroring `rq2Page`). Selection unit
  is a keeper-first **pair token** `"<keeperId>-<loserId>"`; per-row checkboxes + a
  section select-all feed a sticky `mass_action_bar` (visible when `count > 0`) with
  **Merge selected / Delete selected / Dismiss for now / Clear**. Each action POSTs a
  single comma-joined `pairs` field + an `action` field to
  `POST /v2/partials/admin/vendor-bulk` / `company-bulk` (`_dedup_bulk` parses the tokens
  via `_parse_dedup_pairs`, gates admin, caps at `_MAX_DEDUP_PAIRS=200`, processes
  per-pair with per-pair commit + rollback). A per-pair failure doesn't abort the batch,
  but each is logged at error level with its pair id + action, the failing pair tokens are
  named in the toast message, and **any** failure makes the toast an `error` (a partial
  failure never shows green success). The select-all `@change` is a
  **single-quoted** attribute because `tojson` emits double-quoted tokens (a double-quoted
  attr would close early and kill Alpine init). `Dismiss for now` is **view-only** (it
  hides the rows client-side and re-renders; pairs reappear on the next scan — there is no
  durable dismissal table yet).
- **Per-account banner:** `GET .../{company_id}/dup-suggestion` (`company_dup_suggestion`,
  declared ABOVE the catch-all) → lazy `hx-trigger=load` panel in `detail.html` →
  `_dup_suggestion.html`. Shows the top dedup match INVOLVING this company + a "Review &
  merge" button reusing the existing `merge-form → merge-preview → POST .../merge` flow.
  Empty 200 when no near-dup.
- **Name-suggestion chip (suggest-only):** `GET .../{company_id}/name-suggestion`
  (`company_name_suggestion`) → `_name_suggestion.html`, lazy in the header. Surfaces
  `company_utils.suggest_clean_company_name` (display-cased suffix-strip) as "Suggested
  name: X" with an Apply button → `POST .../{company_id}/apply-name`
  (`company_apply_name`; sets `Company.name`, `@validates` resyncs `normalized_name`,
  `invalidate_prefix('company_list')`). Empty 200 when already
  clean. **create_company no longer silently stores the AI-typo-corrected name** — it
  keeps the rep's typed name (the AI fix still strengthens the duplicate check), making
  naming suggest-only end-to-end.

### 10a. Global contact lists + vendor stock-list upload UI

Two cross-entity contact workspaces sit alongside the CDM account workspace, plus a
UI for uploading a vendor's stock list. All three are thin routes in
`htmx_views.py`; the scoping logic lives in `crm_service.py`.

- **`GET /v2/contacts`** (`customer_contacts_partial` → `customers/contacts_list.html`)
  — cross-company customer-contacts workspace. **Cross-tenant PII**: the role-scope is
  the SAME predicate as the CDM `my_only` branch, factored into the shared
  `crm_service.company_visibility_predicate(user)` (account-owner OR site-owner OR
  named collaborator). SALES/TRADER reps see ONLY contacts in accounts they can manage;
  MANAGER/ADMIN see all (the predicate is skipped for `is_manager_or_admin`).
  `customer_contacts_query` joins `SiteContact → CustomerSite → Company` (all
  `is_active`), filters search (name OR email) / company / role; `cadence_state` is a
  DERIVED dot (not a column) but its day-floor thresholds collapse to exact timestamp
  cutoffs, so `customer_contacts_list_ctx` filters it in SQL via
  `contact_cadence_predicate` (which mirrors `cadence_state_of` EXACTLY) and pages with
  count()/offset()/limit() — no full-set load before paging (PERF-10). The company-filter
  dropdown is built from the
  same visibility scope. `require_user`. Reached via the "All contacts" link in
  `customers/list.html`.
  Rows surface priority (star) / DNC / Archived flags, the contact's site under the
  company name, and mailto:/tel: links. A per-row Edit (pencil) button opens the shared
  `customers/tabs/_contact_form.html` edit modal with `origin=contacts` + the live
  `filter_*` values (namespaced to dodge the form's own `contact_role` field);
  `edit_site_contact` detects `origin=contacts` and re-renders THIS list via
  `_contacts_list_response(..., prefix="filter_")` (filters + paging intact, toast via
  `HX-Trigger`) instead of the company Contacts-tab grouped list. The edit-form GET
  gates on `can_manage_account` (404, matching `company_detail_partial`) like its
  save-path peer. The form prefills NULL columns as `''` (never the literal `None`)
  and, for legacy contacts whose name lives only in `full_name`, splits it on the
  first space into the first/last inputs so an untouched save round-trips exactly.
  Row links/buttons carry `@keyup.stop` in addition to `@click.stop` so Enter on a
  focused child never double-fires the row's account navigation.

  **Global modal host** (`base.html` + `resizableModal()` in `htmx_app.js`): on
  `open-modal {url}` it clears the previous modal's DOM, hand-toggles the
  `#modal-loading` spinner around the `htmx.ajax` fetch (htmx.ajax has no
  `indicator` option), holds a 180px min-height while `#modal-content` is empty
  (no grip+close "sliver" flash), and focuses the first field of the loaded form
  on desktop. Geometry memory (`avail_modal_geom`) is keyed **per modal** —
  `lg|wide` + the opened URL with numeric ids normalized to `:id` — so a size
  saved for one modal never cramps another; degenerate saved sizes (<320×240)
  are ignored. Modal close-after-save uses plain JS
  `this.dispatchEvent(new CustomEvent("close-modal", {bubbles: true}))` inside
  `hx-on::after-request` — Alpine's `$dispatch` is undefined in hx-on context
  (htmx evals it as plain JS; see CLAUDE.md anti-patterns).
- **`GET /v2/vendor-contacts`** (`vendor_contacts_partial` → `vendors/contacts_list.html`)
  — global vendor-contacts list, the HTML twin of `GET /api/vendor-contacts/bulk`
  (`vendor_contacts.py`). View-open (`require_user`; vendor data is not tenant-scoped);
  blacklisted vendors excluded, mirroring the bulk route. Search (contact name/email or
  vendor name) + sort (name/vendor/email/relationship score) + pagination. Reached via
  the "Contacts" link in `vendors/list.html`.
- **Vendor stock-list upload UI** — `vendors/list.html` carries an "Upload stock list"
  button + Alpine modal that POSTs `multipart/form-data` (CSV/TSV/XLSX file for a named
  vendor) to `POST /v2/partials/vendors/import-stock` (`import_vendor_stock_list`,
  `require_buyer`). The handler is a thin wrapper over the shared
  `app/services/stock_list_ingest.py::ingest_stock_list` service (the same ingest the JSON
  `POST /api/materials/import-stock` endpoint uses), which ingests rows
  (MPN, qty, price, manufacturer…) as `MaterialCard` + `MaterialVendorHistory` + price
  snapshots. The result HTML banner (`htmx/partials/vendors/stock_import_result.html`)
  swaps into `#vendor-stock-result`.

Both `/v2/contacts` and `/v2/vendor-contacts` are full-page entry points wired into
`v2_page` (segments precede `customers`/`vendors` — `/contacts` is a substring of
`/vendor-contacts`) and borrow the CRM nav highlight via `_NAV_ID_ALIAS` +
`mobile_nav.html`'s `urlToNav` map.

---

## 10a. CRM Audit Trail (created_by / modified_by)

Migration 147 adds `created_by_id` + `modified_by_id` (FK → `users.id`, `ondelete=SET NULL`,
nullable) to `companies`, `customer_sites`, and `site_contacts`.

Mechanism — three-layer:

1. **`app/request_context.py`** — `current_user_id_var: ContextVar[int | None]` (default
   `None`). Single module, no circular deps. Background jobs and imports never set it.

2. **`app/main.py` `audit_user_middleware`** (L3 HTTP middleware) — on every request,
   reads `user_id` from the Starlette session scope and calls
   `current_user_id_var.set(uid)`.  Resets the token in a `finally` block so cross-request
   leak is impossible even under async cancellation.

3. **`app/audit_listeners.py` `register_audit_listeners()`** — registers
   `before_insert` / `before_update` SQLAlchemy event listeners on `Company`,
   `CustomerSite`, `SiteContact`.  On insert: sets `created_by_id` and `modified_by_id`
   if the contextvar is non-None and the column is not already explicitly set.  On update:
   sets only `modified_by_id`.  Called once at module load from `app/main.py`.

Invariants:
- Authenticated request writes → both columns populated.
- Background job / APScheduler / import writes → both columns NULL.
- No cross-request contamination — `ContextVar.reset()` in `finally`.
- Company detail template (`htmx/partials/customers/detail.html`) renders
  "Created by {name} · Updated by {name}" inside the collapsible account-settings panel.

### Per-user display timezone (UTC storage → viewer-zone render)

Timestamps store UTC (`UTCDateTime`, unchanged); each viewer sees them in their own zone.
Foundation mechanism (migration 181 adds `users.display_timezone`, an IANA name):

1. **Auto-detect** — the base layout renders the stored zone onto `<body data-user-tz>`.
   `syncDisplayTimezone()` (`app/static/htmx_app.js`, on `DOMContentLoaded`) reads
   `Intl.DateTimeFormat().resolvedOptions().timeZone` and, only when it differs from the
   data attribute, fire-and-forget `fetch`es `POST /v2/profile/timezone` (form-encoded,
   x-csrftoken). The endpoint (`app/routers/htmx/settings.py`, `require_user`) validates the
   IANA name (`app.utils.timezones.is_valid_timezone` — rejects Windows/junk), stores it
   **only when unset or changed** (repeat visits are a no-op), and toasts via HX-Trigger
   (shown by the profile `<select>`; the `fetch` ignores it). The Profile-tab `<select>`
   (grouped `<optgroup>` from `grouped_timezones()`) posts the SAME endpoint for a manual override.

2. **Per-request contextvar** — `app/request_context.py` adds
   `current_user_display_tz_var: ContextVar[str | None]`. `AuditUserMiddleware` establishes a
   baseline (`None`) + `finally` reset (no keep-alive cross-request leak); `require_user`
   overrides it with the loaded user's `display_timezone`. Mirrors the `current_user_id_var`
   pattern above.

3. **Display layer** — `app/utils/timezones.py` (`DEFAULT_DISPLAY_TZ = "America/New_York"`,
   the fallback for unknown/NULL/invalid): `format_localtime` / `format_localdate` /
   `to_display_tz` convert a UTC datetime into the current viewer's zone (or an explicit zone
   arg for server-side use like emails). `template_env.py` exposes them as the `|localtime`
   and `|localdate` Jinja filters, and `_task_due_state` now judges "today"/"overdue" by
   `current_display_zoneinfo()` (was a hardcoded `America/New_York`) — a buyer in Asia/Tokyo
   near UTC midnight sees a task due on THEIR calendar day as "today". Distinct from
   `users.timezone` (the Graph mailbox Windows-format zone for RFQ scheduling).

   `template_env.py` also exposes `|localday` — the DATE-object companion to `|localdate`
   (convert to the viewer's zone, then `.date()`). The activity/timeline day-group headers
   ("Today"/"Yesterday"/date bucket) in the customer, vendor, and requisition Activity tabs
   (`partials/{customers,vendors}/tabs/activity_tab.html`,
   `partials/requisitions/tabs/activity.html`) bucket on `now()|localday` / `ts|localday`
   instead of the raw UTC `.date()`, so a non-Eastern viewer's rows land under the header
   that matches their own calendar day (and their already-localized rendered timestamp) at
   the midnight boundary — the requisition header also switched from raw `.strftime` to
   `|localdate` so its label matches the local bucket.

   NOTE: this is the mechanism + a couple of proof wirings (`|localdate` on the profile
   "Member since", `|localtime` on the offers review-queue `created_at`, `|localday` on the
   Activity-tab day grouping). The app-wide sweep of every `.strftime(...)` timestamp render
   is a deliberate follow-up.

### CRM P5 trust — field-history, completeness, phone-normalize, industry pick-list

These extend the audit trail above (migration 169 + reuse of existing primitives):

1. **Field-history (old→new)** — `crm_field_history` table + `app/services/crm_field_history.py`.
   The inline single-field POST handlers (`company_field_post` in
   `app/routers/htmx/companies/core.py` / `contact_field_post` in
   `app/routers/htmx/companies/contacts.py`) capture the attribute value *before* and *after*
   `apply_company_field` / `apply_contact_field`, then call `record_field_change` (which
   writes a row only when the canonical value actually changed; None↔"" is a no-op) before
   the same `db.commit()` so the history row and the edit land atomically. `changed_by_id`
   comes from the route's `user` (not the audit contextvar) so it is always set on
   user-driven edits. Surfaced via `field_history_for` on the account **History** tab
   (`GET …/tab/history` → `tabs/history_tab.html`) and the contact **History** modal
   (`GET …/contacts/{id}/history-modal` → `_contact_history_modal.html`), both rendering the
   shared `_field_history_list.html` (raw `field_name` → label via `FIELD_LABELS`).

2. **Completeness score** — `app/services/crm_completeness.py` scores a Company/SiteContact
   over a fixed key-field set → `{pct, filled, total, missing}`. The account header renders
   a colored % badge (context `account_completeness`); the contact row renders a small pill
   when incomplete via the `crm_completeness` Jinja2 global (registered in
   `app/template_env.py`, dispatches on entity type). The missing-field list is the badge
   tooltip; the **existing** `enrich_button.html` (`POST /api/enrich/company/{id}`) adjacent
   in the header is the reused "enrich to fill" affordance — no new enrichment trigger.
   (Contact rows have no inline enrich button; the seam is the same `enrich_button.html`.)

3. **Phone normalize on save** — already enforced for `Company.phone` (the model's
   `_sync_normalized_phone` `@validates` keeps `normalized_phone`, and `apply_company_field`
   stores E.164 via `normalize_phone_e164`). P5 extends the **same** util to the contact
   inline path: `apply_contact_field` now normalizes `phone` + `secondary_phone` to E.164
   (`normalize_phone_e164(v) or v` — keeps raw if unparseable, never silent data loss).

4. **Industry pick-list** — `CRM_INDUSTRIES` (`app/constants.py`) is the canonical list,
   exposed as the `crm_industries` Jinja2 global. `EDITABLE_ACCOUNT_FIELDS["industry"]` is a
   `select`; the create/edit forms and the inline editor render it from `crm_industries`.
   `apply_company_field` accepts a canonical value, a blank (clear), OR the **unchanged**
   current value — the last clause preserves legacy free-text industries on no-op saves while
   constraining every new value (mirrors the ContactRole legacy policy). The select widgets
   inject any out-of-list current value as a "(current)" option so it stays selectable.

---

## 11. Cross-App Alerts (Nav Badges + In-Tab Spotlight)

A reusable alert layer (`app/services/alerts/`) drives an emerald count badge on
three bottom-nav tabs and a one-time in-tab "spotlight" that glides to and rails
the new rows. Each tab registers one or more `AlertSource`s; a tab's badge count
is the SUM of its sources' counts.

```
Badge poll (every 60s, same pattern as Proactive):
    GET /v2/partials/alerts/{tab_key}/badge   (tab_key ∈ requisitions|buy-plans|crm)
        |
        v
    routers/alerts.py -> registry.count_for_tab(db, user, tab_key)
        |  (sum of the tab's AlertSource.count_for_user; FAIL-QUIET per source —
        |   a badge must never break the nav)
        v
    emerald pill HTML (empty at 0) swapped into #{tab_key}-nav-badge

Tab list render (parts list / buy_plans list / CDM account list):
    registry.markers_for_tab(db, user, tab_key)
        |  -> {anchor: {kind, temperament, refs:[ref_id,...]}}
        v
    _alert_macros.alert_row_attrs(markers, anchor) stamps data-alert-* on each row
        |  (anchor = "req-<id>" Sales Hub | "bp-<plan_id>" Buy Plans | "company-<id>" CRM)
        v
    htmx_app.js spotlight: emerald accent rail + one-time pulse, glide-to-first,
    IntersectionObserver marks each row seen as it scrolls into view, floating jump-pill

Mark-seen (per row, background, no spinner):
    POST /v2/partials/alerts/{kind}/seen  (ref_id form field)
        |
        +---> alerts.record_seen() — idempotent INSERT alert_seen (unique upsert)
        +---> returns the owning tab's refreshed badge as an OOB swap (tab_for_kind)
```

**Two temperaments** (`AlertSource.temperament`):

- **FYI** — clears on *see*. The count EXCLUDES `alert_seen` rows, so viewing the
  row drains the badge and fades its rail. Sources: `OfferConfirmedSource`
  (Sales Hub — new APPROVED+qualified offers on the buyer's requirements) and
  `InboundCustomerSource` (CRM — new inbound comms on a Customer account the user owns).
- **ACTION** — clears on *act*. The count derives PURELY from work-state
  (`BuyplanActionSource`: the user's open buy-plan steps — buyer PO line / manager
  approval / ops SO-verify; `TasksActionSource`: open tasks assigned to me, registered
  under the `my-day` tab — wires `task_service.get_my_tasks_summary`). `alert_seen` does
  NOT change the count — it only gates the cosmetic one-time pulse; the row keeps its rail
  until the underlying work is done.

**Tasks page + core-basic gaps (Phase 3).** `GET /v2/partials/my-day` (gated
`require_access(AccessKey.MY_DAY)`) is the **Tasks** page (nav label "Tasks"; formerly
"My Day", which also carried a follow-up-accounts call-down section — that account cadence
now lives in CRM, so this page is tasks-only). It is a filterable queue of tasks assigned
to me (`status`/`priority`/`due`; reuses `get_my_tasks`, filters priority/due in-route),
grouped by urgency (Overdue → Due soon → Later → No due date). Templates: `tasks/list.html`
(full page incl. filter bar) and `tasks/_results.html` (results-only fragment, returned
when `HX-Target == "tasks-results"`). The filter bar carries an EXPLICIT
`hx-target="#tasks-results"` (never inherits `#main-content`). The standalone
`GET /v2/partials/tasks` queue endpoint was folded into this route. The My-Day/Tasks nav
badge is the `TasksActionSource` above.

**Requisition Task board (detail "Tasks" tab).** The requisition detail "Tasks" tab
(`requisitions/tabs/tasks.html`, rendered by `requisition_tab` in
`routers/htmx/requisitions.py`) is the richest task-create surface — the only one with
type + priority + assignee. Its mutations are three HTML-returning routes in the same
router (previously the tab POSTed to non-existent `/api/requisitions/{id}/tasks*` and every
button 404'd silently): `POST /api/requisitions/{req_id}/tasks` (create — form carries
title/task_type/priority/assigned_to_id/due_at, returns the re-rendered `_task_list.html`
body swapped `innerHTML` into `#task-list`, clearing the empty state), `POST
/api/requisitions/{req_id}/tasks/{task_id}/complete` (returns the re-rendered `_task_row.html`
via `outerHTML`), and `DELETE /api/requisitions/{req_id}/tasks/{task_id}` (empty 200;
`hx-swap=delete` removes the row). A board task is a `RequisitionTask` with `requisition_id`
set and `requirement_id` NULL, created via `task_service.create_requisition_task` (no 24h
due-date floor, mirroring the CRM create helpers). The board is **shared per requisition**:
all three routes gate on `require_requisition_access` (not assignee-only, unlike the
part-comms complete) and IDOR-check `task.requisition_id == req_id`. Due dates arrive as
`<input type=date>` strings and are parsed to aware UTC-midnight datetimes before binding
(never a raw string — `UTCDateTime` passes strings through unnormalized). Row markup is
factored into `_task_row.html` (one row) and `_task_list.html` (list body) so the endpoints
can return single-row / list-body fragments.
Bulk "Assign owner" on the account list now uses a name+role `<select>` (`cdm_list_ctx(include_users=…)`,
manager/admin only) instead of a raw User-ID box. All clickable `<tr hx-get>` list rows
are keyboard-accessible (`role=button` + `tabindex=0` + `keyup[Enter]` + `focus:ring-2`).

`recency_floor()` keeps FYI badges from lighting up for the pre-launch backlog:
an item only counts if newer than `max(now - alert_recency_days, ALERTS_EPOCH)`.

**Inbound rides the existing ledger — no new capture path.** The CRM inbound
alert reads `ActivityLog` rows already written by the inbox poll (§4): `poll_inbox`
→ `log_email_activity` → `match_email_to_entity` resolves the sender by
site-contact/vendor-contact email then company/vendor domain and stamps
`company_id` / `vendor_card_id`. `InboundCustomerSource` simply filters those
inbound rows to Customer accounts the user owns. (Known gap, deferred: non-RFQ
OUTBOUND email is not captured — that would need a Sent-folder scan.)

---

## Enrichment Pipeline

### CRM / Vendor Firmographic + Contact Enrichment

`enrich_entity` and `find_suggested_contacts` in `app/enrichment_service.py` delegate
to two new modules:

- **`app/services/enrichment_router.py`** — cost-tiered, gap-gated provider
  orchestration. For company firmographics, providers run in ascending cost order; each
  metered provider is only called when at least one of the eight `_GAP_FIELDS`
  (`legal_name`, `industry`, `employee_size`, `hq_city`, `hq_state`, `hq_country`,
  `website`, `linkedin_url`) is still missing, the provider's feature gate is enabled,
  and its circuit breaker is closed. Provider order:

  | Tier | Provider | Cost | Notes |
  |------|----------|------|-------|
  | Free | SAM.gov | zero | `sam_gov_enrichment_enabled`; legal name + NAICS + HQ |
  | Metered | Clay | per-credit | MCP-only; gap-gated; `clay_enrichment_enabled` |
  | Metered | Explorium | per-call | gap-gated; `explorium_enrichment_enabled` |
  | Metered | Lusha | per-call | gap-gated; `lusha_enrichment_enabled` |
  | Last resort | AI | Claude API | only when gaps remain after all above |

  For contacts: Phase 1 runs Hunter + Clay concurrently (cheap/free); Phase 2
  escalates sequentially to Lusha → Explorium only when the verified-contact count is
  below the requested limit.

- **`app/services/firmo_tiers.py`** — per-field source-authority ladder (ported from
  the materials F1 `spec_tiers` pattern). `blend_company` and `blend_contacts` iterate
  the raw provider results and keep, for each field, the value from the highest-tier
  source (tie broken by confidence). Unknown source → tier 0 (loses every conflict).
  The blended result carries a `_provenance` dict `{field: {source, tier, confidence}}`
  used by the apply functions.

**Firmographic authority ladder (key fields):**

| Field | Highest tier | Runner-up |
|-------|-------------|-----------|
| `legal_name` | SAM.gov (95) | Explorium (85) |
| `naics` | SAM.gov (95) | Explorium (85) |
| `ticker` | Explorium (90) | Clay (75) |
| `revenue_range` | Explorium (90) | Clay (75) |
| `employee_size` | Explorium (85) | Lusha (70) |
| `linkedin_url` | Explorium (85) | Lusha (80) |
| `hq_city/state/country` | Explorium (85) | SAM.gov (80) |

**Contact authority ladder (key fields):**

| Field | Highest tier | Runner-up |
|-------|-------------|-----------|
| `email` | Lusha (95) | Hunter (85) |
| `phone` | Lusha (95) | Explorium (65) |
| `title` | Explorium (80) | Lusha (70) |

**Provenance-aware apply.** `apply_enrichment_to_company` / `apply_enrichment_to_vendor`
in `enrichment_service.py` call the shared `_apply_enrichment` function, which writes
with three rules:

1. Empty field → always write.
2. Existing value with no stored provenance (manual / legacy) → protect; never clobber.
3. Existing value with stored provenance → overwrite only when incoming (tier, confidence)
   strictly beats the stored pair.

Provenance is persisted in the new `enrichment_provenance` JSONB column on both
`companies` and `vendor_cards` (migration 125).

**Enrich button → result panel (content negotiation).** `POST /api/enrich/company/{id}`
and `/api/enrich/vendor/{id}` (`app/routers/crm/enrichment.py`) content-negotiate on the
`HX-Request` header: HTMX callers (the shared `partials/shared/enrich_button.html`) get a
rendered `partials/shared/_enrich_result.html` panel — firmographics found, each field badged
**Updated** (in `updated_fields`) vs **Current**, source badge + "enriched X ago", and (for
companies) contacts discovered via `find_suggested_contacts_with_errors` with per-row Add
buttons; programmatic callers still get JSON `{ok, updated_fields, enrichment}`. The contact
rows reuse the shared `partials/shared/_contact_row.html` macro (also used by the Contacts-tab
`_suggested_contacts.html`); their Add posts to the existing
`POST /v2/partials/customers/{id}/suggested-contacts/add` with a hidden `from_enrich=1` flag,
which makes that endpoint return a self-contained "✓ Added" `<li>` (`hx-swap=outerHTML`,
`hx-target="closest li"`) instead of re-rendering the Contacts-tab list. The dead
`enrich_customer_account` stub (returns `no_providers`) is no longer called from the enrich
endpoint — it stays wired to the CSV bulk-import path
(`app/routers/htmx/companies/core.py::import_companies_confirm` +
`app/services/company_import_service.py`) + the `customer_enrichment_batch` scheduler. Contact discovery degrades gracefully: a provider failure renders an amber
"couldn't reach" banner, never a 500.

**Connectors:**

- `app/connectors/explorium.py` — 2-call pipeline: `/businesses/match` → business_id,
  then `/businesses/firmographics/enrich`; contacts via `/prospects` +
  `/prospects/contacts_information/enrich`. ICP discovery via `discover_businesses()` →
  `/businesses` Fetch endpoint (`{"filters": {...}, "size": N}` body, `data` array
  response — same shape as the `/prospects` search). Auth: `api_key:` header (NOT
  `Authorization: Bearer`). 402/403/429 → `ProviderQuotaError`.
  - `app/services/prospect_discovery_explorium.py` (prospecting discovery) routes through
    `explorium.discover_businesses()` and normalizes each business into a scoring-ready
    dict. It previously hit an unverified `/v1/businesses/search` bulk route — now retired.
    `run_explorium_discovery_batch(batch_id, existing_domains, segment_keys, region_keys)`
    scans only the monthly rotation slice's segment×region cells (not all 12), so discovery
    spends ~1/6 the credits (H6).
  - `app/services/prospect_signals.py::enrich_missing_signals` is now a **verified
    firmographic backfill** — it calls `explorium.enrich_company` (real
    `match → firmographics/enrich` pipeline, `api_key` header, `ProviderQuotaError`/circuit
    guard) and fills only the prospect's empty firmographic fields, then recomputes fit. The
    old dead `/v1/businesses/search` + `Bearer` call is removed (H7). Explorium exposes no
    intent/hiring/events-by-domain route, so this backfills firmographics, not readiness
    signals.
- `app/connectors/clay_mcp.py` — backend MCP client (JSON-RPC 2.0 over HTTPS to
  `https://api.clay.com/v3/mcp`). Clay speaks **MCP Streamable HTTP**: every
  `tools/call` requires a session, so the connector first runs the handshake
  (`initialize` → read the `Mcp-Session-Id` response header → `notifications/initialized`)
  and **caches that session per access token** (reused across calls; a bare sessionless
  call returns `400 "Missing Mcp-Session-Id header"`). `tools/call` responses are
  **server-sent events** (`content-type: text/event-stream`), parsed from the `data:`
  line — not plain JSON. Authenticates with an OAuth access token
  (`Authorization: Bearer <token>`); on 401 it refreshes the token + re-initializes the
  session then retries once; on 400/404 (expired session) it re-initializes + retries
  once. Not connected → returns `None`/`[]` (fail-soft; blend continues without Clay).
  Company: `find-and-enrich-company` (sync; `result.structuredContent.companies[domain]`).
  Contacts: `find-and-enrich-contacts-at-company` (`.contacts[]`); emails polled via
  `get-task-context` (bounded: 5 polls × 3 s). 402/429 → `ProviderQuotaError`. Protocol
  verified live 2026-06-23. Health: `_ClayTestConnector` (in `routers/sources.py`,
  registered for `clay_enrichment`) probes `get-credits-available` so
  `health_monitor.ping_source` reports Clay's true state (live/error) instead of leaving
  it `disabled` — without it, `startup.py`'s `is_active=false WHERE status='disabled'`
  reconciliation keeps flipping the connector card off. **The old Clay WEBHOOK path
  (`clay_service.py` + `POST /api/webhooks/clay`) has been removed; Clay is now MCP-only.**

  **Clay OAuth Connect flow.** `api.clay.com/v3/mcp` is OAuth-gated
  (authorization_code + PKCE S256, scope=`mcp`; no client_credentials grant).

  - `app/services/clay_oauth.py` — token lifecycle: dynamic client registration
    (DCR, reuses an existing `CLIENT_ID` when already registered), PKCE S256 code
    challenge, code exchange, `get_access_token()` (auto-refresh with 5-min buffer,
    rotation-aware), `refresh()` (sets `NEEDS_RECONNECT` marker on failure),
    `is_connected()`, `disconnect()`. Tokens stored **encrypted** in
    `ApiSource('clay_enrichment').credentials` JSONB:
    `CLAY_OAUTH_ACCESS_TOKEN` / `REFRESH_TOKEN` / `EXPIRES_AT` / `CLIENT_ID` /
    `NEEDS_RECONNECT`. No DB migration — reuses the existing `ApiSource.credentials`
    column.

  - `app/routers/clay_oauth.py` — admin-only routes, mounted in `app/main.py`:
    - `GET /auth/clay/connect` — DCR-or-reuse → generate PKCE verifier + `state`
      (stored in `intel_cache`) → redirect to `app.clay.com/oauth/authorize`.
    - `GET /auth/clay/callback` — validate single-use `state` → exchange code →
      store encrypted tokens → redirect to Settings.
    - `POST /auth/clay/disconnect` — clears stored credentials.

  - **Settings → Connectors** — the Clay card renders as `control_type=oauth_clay`:
    a **Connect / Reconnect / Disconnect** card (no API-key input field). The
    `NEEDS_RECONNECT` marker surfaces a "Reconnect" prompt when the refresh token has
    expired; `connector_state` returns `needs_reconnect` in that case. Clay OAuth
    callbacks redirect to `/v2/partials/settings/connectors`.

  Mirrors the Azure AD OAuth pattern in `app/routers/auth.py`.
- `app/connectors/sam_gov_company.py` — name→firmographics adapter wrapping the public
  SAM.gov entity-information API (`api.sam.gov/entity-information/v3/entities`).

**Config flags** (all boolean, set in `.env`):
`hunter_enrichment_enabled` (default `True`), `sam_gov_enrichment_enabled` (default `True`),
`clay_enrichment_enabled` (default `False`), `explorium_enrichment_enabled` (default `False`),
`lusha_enrichment_enabled` (default `False`).
The feature flag is the **only** gate the enrichment chain consults — the `api_sources`
row (`seed_api_sources`) drives the Connectors UI / health monitor, not provider dispatch.
The two keyless/free providers default on and degrade cleanly when their key is absent:
**Hunter** returns `[]` (no outbound call) when `HUNTER_API_KEY` is unset, and **SAM.gov**
falls back to the public `DEMO_KEY` tier and degrades to `None` on error — neither raises.
Each metered provider also has a `*_cooldown_minutes` knob used by the circuit breaker.
Hunter, Clay, Explorium, and Lusha raise `ProviderQuotaError` on 402/429 (circuit-guarded).

```
Trigger: user click ("Enrich" on CRM account / vendor) OR background prospect scan
    |
    v
enrichment_service.enrich_entity(domain, name)
    |
    +---> enrichment_router.gather_company(domain, name)
    |       +---> sam_gov_company.enrich_company()       [free, always]
    |       +---> clay_mcp.enrich_company()              [metered, gap-gated]
    |       +---> explorium.enrich_company()             [metered, gap-gated]
    |       +---> lusha.enrich_company()                 [metered, gap-gated]
    |       +---> ai fallback                            [last resort]
    |
    +---> firmo_tiers.blend_company(results)   → blended dict + _provenance
    |
    +---> normalize_company_output()
    |
    +---> intel_cache table (14-day TTL, keyed by domain; raw-SQL, no ORM model)
    |
    v
apply_enrichment_to_company(company, blended)  OR
apply_enrichment_to_vendor(card, blended)
    |
    +---> _apply_enrichment: tier-arbitrated field writes
    +---> companies/vendor_cards.enrichment_provenance = updated provenance store
    +---> companies/vendor_cards.enrichment_source = blended source string
```

```
Trigger: "Find Contacts" on CRM account
    |
    v
enrichment_service.find_suggested_contacts(domain, name, title_filter, limit)
    |
    +---> enrichment_router.gather_contacts()
    |       Phase 1 (concurrent): Hunter + clay_mcp.find_contacts()
    |       Phase 2 (escalation): Lusha → Explorium (only if verified < limit)
    |
    +---> firmo_tiers.blend_contacts(raw)   → deduped by email→linkedin→name,
    |                                          per-field highest contact_tier wins
    +---> relevance filter (_RELEVANT_KEYWORDS title check)
    +---> returns list[dict], capped at limit
```

### Prospect Enrichment (legacy path)

```
Trigger: prospect scan OR background job
    |
    v
enrichment_service.py (orchestrator)
    |
    +---> Phase 1a: Free enrichment
    |       +---> prospect_free_enrichment.py (web search)
    |       +---> signature_parser.py (from email_signature_extracts)
    |
    +---> Phase 2: AI analysis
    |       +---> ai_service.py --> Claude API (company intel, ICP fit)
    |
    +---> DB: UPSERT companies (domain, size, location, enrichment_source)
    +---> DB: UPSERT vendor_cards (domain, industry)
    +---> DB: INSERT enrichment_queue (proposed changes for review)
    +---> DB: INSERT enrichment_jobs (batch tracking)
```

### Prospect Discovery — monthly email mining (`prospect_scheduler.job_discover_prospects`)

The 1st-of-month discovery job's email-mining branch needs a real mailbox identity
(it scans `/me`). It selects the first M365-connected user
(`User.refresh_token.isnot(None)` then `u.access_token and u.m365_connected`) — the same
selection the calendar/inbox jobs use — resolves a token via
`token_manager.get_valid_token(user, db)`, builds `GraphClient(token)`, and calls
`prospect_discovery_email.run_email_mining_batch(...)`. `mine_unknown_domains` lists the
inbox via `GraphClient.get_all_pages("/me/mailFolders/inbox/messages", params={$filter,
$select, $orderby, $top})`, which auto-paginates and returns a flat list of message dicts
(an `{"error": ...}` page raises typed `GraphAPIError` instead of reading as empty).
No mailbox or no token ⇒ the branch logs a warning and skips (email_count stays 0); the
surrounding `except` keeps the scheduler resilient. (There is no `get_graph_client()`
factory and `GraphClient` has no `list_messages` — both were dead references, now fixed.)

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
    LANE SPLIT (full_pipeline arg; settings.enrichment_lane_split_enabled, default on —
    CALL ROUTING ONLY, every write still arbitrates through the F1 ladder, no write
    pre-gate is ever added):
      • PRIORITY lane (full_pipeline=True): cards a user single-added (worker passes
        full_pipeline=True for every card whose enrich_requested_at was stamped) run the
        full sequence below.
      • BULK lane (full_pipeline=False): cards with enrich_requested_at IS NULL run only
        Tier 1 (the FREE connectors); Tiers 2-5 (web / OEM cross-ref / OEM description /
        Opus infer) are ALL skipped and a Tier-1 miss goes straight to terminal not_found
        (the OEM tiers never ran, so not_catalogued can't be concluded). Measured
        ~$6-10/day of paid calls for ~0 ladder-accepted bulk-lane writes.
    |
    +---> Tier 1: distributor connector fanout (fetch_authoritative → merge_authoritative)
    |       HIT  → status=verified; apply_authoritative() writes description/specs/lifecycle;
    |              category + manufacturer route through the F1 ladder at {connector}_api/90
    |              (ladder-rejected writes are dropped from enrichment_provenance).
    |       MISS → fall through (BULK lane stops here → not_found).
    |
    +---> OEM gate: classify_oem_vendor(display_mpn) — pure regex, no web call. Computed
    |       BEFORE Tier 2 now: an OEM/FRU vendor hit + settings.enrichment_skip_web_for_oem_mpns
    |       (default on) skips Tier 2 (web) on EVERY lane — OEM/FRU PNs surface only on
    |       reseller pages (the measured ~95% no-trusted-source reject class). The OEM
    |       tiers (3-4) + Opus (5) still run on the priority lane. Non-OEM parts skip
    |       Tiers 3-4 entirely.
    |
    +---> Tier 2 (priority lane; skipped for OEM-shaped MPNs per the gate above):
    |       distributor/manufacturer web search (extract_part_from_web, web_meter +1)
    |       HIT (web_sourced)  → apply_web_sourced(); category + manufacturer through the
    |              F1 ladder at web_search/70; done.
    |       MISS → fall through.
    |
    +---> Tier 3 (OEM only, priority lane only): cross-reference MPN (cross_reference_mpn, web_meter +1)
    |       Grounded Claude web search; four Python gates:
    |         (1) ≥1 source URL on is_crossref_domain allowlist
    |         (2) both OEM code and resolved MPN appear verbatim in the sourced linkage_quote
    |         (3) resolved_mpn != original (no echo)
    |         (4) confidence ≥ 0.90
    |       RESOLVED → fetch_authoritative(resolved_mpn) double-verify against distributors
    |         CONFIRMED → apply_cross_ref_verified(): writes distributor data onto card
    |                     (category + manufacturer through the F1 ladder at
    |                     {connector}_api/90, same as Tier 1), records FRU→MPN linkage in
    |                     cross_references JSONB + cross_ref provenance block;
    |                     status=verified.
    |         UNCONFIRMED → discard candidate, fall through.
    |       FAILED → fall through.
    |
    +---> Tier 4 (OEM only, priority lane only): OEM-official description (extract_oem_description, web_meter +1)
    |       Grounded Claude web search on OEM's own page; four Python gates:
    |         (1) ≥1 source URL on is_oem_domain allowlist (stricter than cross-ref)
    |         (2) exact_mpn_found matches normalized_mpn verbatim
    |         (3) confidence ≥ 0.90
    |         (4) description ≥ 10 chars + manufacturer present
    |       HIT → apply_oem_sourced(): writes description/category/datasheet + oem_sourced
    |             provenance (category + manufacturer through the F1 ladder at
    |             oem_official/80); status=oem_sourced.
    |       MISS → fall through.
    |
    +---> Tier 5 (priority lane only): AI inference fallback (infer_part via ai_inference_fallback,
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

Claude usage metering (MEASURED $/call — opt-in via cost_bucket):
    claude_client.claude_structured/claude_text/claude_json take an optional
    cost_bucket arg; default None = no metering (app/search/RFQ/email traffic
    is unaffected). The four enrichment Claude paths (oem_crosswalk_resolver,
    web_extractor, spec_enrichment_service, ai_inference_fallback) pass
    cost_bucket="enrichment", so on each 200 response _meter_usage aggregates
    response.usage (input/output/cache_read/cache_write tokens AND
    server_tool_use.web_search_requests) into Redis date-counters keyed
    claude_usage:{bucket}:{model_tier}:{metric}:{UTC-date} (atomic
    intel_cache.incr_count, 35-day TTL — same counter substrate as web_calls).
    Metering is best-effort and NEVER raises. Readout:
    `python -m app.management.enrichment_spend [--date YYYY-MM-DD] [--days N]`
    prices each tier (haiku $1/$5, sonnet $3/$15, opus $5/$25 per MTok; web
    search $10/1000; cache_read 0.1x / cache_write 1.25x input) → real
    $/call, $/day, ~$/mo. This replaces the prior estimate-only cost model;
    the worker's daily_cap counts every processed card (drain-speed), while
    web_daily_cap/oem_resolve_daily_cap are the only spend levers.

Claude Haiku (Anthropic API)  — FIRST PASS (legacy path — superseded by
    authoritative_enrichment_service for new cards; kept for bulk/batch jobs)
    |
    +---> Classify card: description, category, lifecycle_status
    +---> DB: UPDATE material_cards (description, category, lifecycle_status)
    +---> search_vector trigger auto-updates TSVECTOR with new description/category
    +---> Stamps material_cards.enriched_at

OEM web-resolution crosswalk (run_one_batch, BEFORE the per-card core loop — the
crosswalk-pass pattern; both passes gated by settings.oem_crosswalk_enrich_enabled,
migration 100, spec: SPEC_OEM_WEB_RESOLUTION):

    Pass A — paced resolution (network). Batch cards whose display_mpn classifies
    to a vendor in SOURCE_BY_VENDOR — "hpe" (HP/HPE via PartSurfer at
    https://partsurfer.hp.com; the classifier gained the `\d{6}-B\d{2}` option-kit
    and `L\d{5}-\d{3}` L-series shapes) or "lenovo" (Wave 6 — Lenovo via PSREF at
    https://psref.lenovo.com; the classic/modern FRU shapes already in the
    classifier) — and have NO fresh oem_crosswalk row are resolved via
    enrichment_worker/oem_crosswalk_resolver.resolve_oem_spare. Both vendors run the
    SAME grounded resolver, negative cache and crosswalk writer; only the per-spare
    vendor label differs (it steers the lookup hint PartSurfer-vs-PSREF and tags the
    row). pending_resolution is selected per vendor (it is vendor-scoped) and merged —
    Claude web_search
    grounded extraction (claude_json + five Python trust gates, HARDER than the
    ephemeral oem_extractor contract because the outcome is permanent with no
    distributor re-verification: (1) the SINGLE source_url the quote was taken from
    is allowlisted — provenance is gated, not just some visited URL; (2) BOTH PNs
    appear as whole TOKENS of the verbatim quote (token-boundary membership, never
    substring of the collapsed quote — rejects title fragments / truncations /
    cross-token spans) and the canonical normalizes to >= 6 chars, <= 64 raw;
    (3) no-echo by containment either way; (4) confidence >= 0.90; (5) graceful
    no_match on a parsed dict's null/malformed fields. An UNPARSEABLE/empty response
    raises ClaudeError — transient, NEVER a 90-day no_match. NO direct HTTP to
    PartSurfer/PSREF, ever). Outcomes upsert the PERMANENT oem_crosswalk cache
    (resolved rows forever; no_match rows are a 90-day negative cache, updated in
    place on retry — DB-deduped via the source_domain='' sentinel) through the
    shared apply_resolution writer, inside a SAVEPOINT (an IntegrityError/DataError
    race with the drain CLI rolls back ONLY that row, never the batch session; the
    run_one_batch wrapper also rollback-recovers a poisoned session) — each unique
    spare PN costs exactly ONE web call ever. Pacing: at most oem_resolve_per_batch
    (default 2) per batch and oem_resolve_daily_cap (default 40) per day, the
    sub-cap counted INSIDE web_daily_cap; every call bills
    enrichment_worker:web_calls:{date} AND enrichment_worker:oem_resolves:{date}
    BEFORE the await via intel_cache.incr_count (atomic Redis INCRBY — two
    concurrent billers never lose updates; max-of-cache-and-in-process defense
    retained for cache loss). ClaudeError → breaker.record_claude_error, NO row
    (free retry next batch). NOT a BaseConnector — no ApiSource row, no health ping
    (our code never opens an HTTP connection to the OEM; the only failure surface is
    the Claude API). The paced drain CLI `python -m
    app.management.backfill_oem_crosswalk --vendor hpe [--limit N] [--dry-run]`
    resolves+upserts rows only (demand-first: cpu+searched, cpu, rest; newest spare
    norms first within each bucket — Compaq-era numbers are near-universal no_match
    and must not front-load the daily budget), billing the
    SAME two counters the same atomic way, re-checking pending_resolution per item
    (a norm the worker cached mid-run is skipped, not re-billed), tolerating
    IntegrityError at its per-row commit (rollback + continue), >=2s between calls,
    aborting after 5 consecutive ClaudeErrors.

    Pass B — deterministic writer (zero network), oem_crosswalk_enrich.py::
    oem_crosswalk_and_record_specs over the FULL batch ids: cards whose
    normalize_mpn_key(display_mpn) has resolved rows inherit, at source
    "partsurfer" (vendor hpe) / "psref" (lenovo) — BOTH pre-registered at ladder
    tier 80: (1) agreement gate — resolved rows disagreeing on the canonical norm
    skip the card (canonical_conflict); (2) decode channel — decode_mpn(canonical)
    specs at confidence 0.90, category fill from the decode commodity ONLY when the
    card has none (a DIFFERENT existing category skips the card, category_mismatch);
    (3) title channel — extract_desc(f"{title} {canonical}", hint=card.category if
    in SPEC_COMMODITIES) specs at 0.85 (intra-tier-80, decode's 0.90 wins via the
    ladder; this is the CPU path: resolved Xeon/Core titles hit desc_extractor/
    cpu.py + cpu_model_specs.json → all six cpu facets; never writes category);
    (4) cross_references append {mpn, manufacturer, source}, deduped on normalized
    mpn+source; (5) status — (category OR >=1 spec written) AND not VERIFIED →
    enrichment_status=oem_sourced + enrichment_source + enriched_at +
    enrichment_provenance["oem_crosswalk"] audit entry; EXCEPT an UNENRICHED
    `\d{6}-B\d{2}` option kit (OPTION_KIT_RE — distributors DO catalogue that
    cohort), which takes the spec/xref writes but defers the uplift (counted
    option_kit_deferred) so enrich_card still runs the FREE tier-90 authoritative
    connector pass; it upgrades once any connector attempt has happened (not_found/
    not_catalogued/web/ai statuses). Running BEFORE the core loop means the upgrade
    short-circuits enrich_card's VERIFIED/OEM_SOURCED early-return and saves up to
    3 web calls/card for the service-spare cohort distributors miss by construction.
    Per-card SAVEPOINT; no commit (batch-final commit owns durability); the F1
    ladder (80 < fru_desc_parse 82 < ... < vendor 90; > web_search 70 >
    spec_extraction 60) arbitrates — no per-writer pre-gates.

Worker second-pass ordering (run_one_batch, same shared post-await session; passes 1-3
share the batch session and normally persist with pass 4's FIRST per-chunk commit —
pass 4, enrich_card_specs, commits PER CHUNK on that shared session because its chunks
are separated by long awaited Claude calls and three of its callers have no commit of
their own — see the load-bearing commit comment in spec_enrichment_service.py; the
batch-final commit is the safety net when pass 4 is skipped (no enriched_ids), raises
early, or processes zero chunks — but not for a failed first-chunk COMMIT, whose
rollback discards the batch's pending writes and lets the cards re-select next batch).
As of SP2 the run ORDER is
no longer load-bearing: record_spec arbitrates
every write through the F1 source-tier ladder (app/services/spec_tiers.py —
mpn_decode 85 > fru_matrix_decode 84 > desc_parse 83 > fru_desc_parse 82 >
spec_extraction 60; vendor APIs 90, trio_source 95, manual 100). A lower-tier writer can never overwrite a higher-tier prior regardless
of the confidence it claims or which pass ran first, so the old per-writer
"skip keys already held at higher confidence" pre-gates are REMOVED — the ladder
owns arbitration in one place:

    1. mpn_decoder/writer.py::decode_and_record_specs   — deterministic MPN→spec
       decode, source="mpn_decode" (tier 85), confidence 0.95
       (settings.mpn_decode_enabled). Category via spec_tiers.set_category; the
       decode's vendor (the actual MAKER — the regex gate is manufacturer-scheme-
       specific) via spec_tiers.set_manufacturer at mpn_decode/0.9 (dual-brand W4).
       The maker write shares the specs' cross-commodity guard: when the decoded
       commodity LOSES the category ladder, the regex match itself is suspect, so
       the decode contributes NOTHING (no specs, no maker). A maker write that loses
       arbitration against a DIFFERENT existing value is counted
       (skipped_maker_conflict) and WARNed after the batch, mirroring
       skipped_category_conflict. Per-card exceptions are counted in `failed` and
       surfaced in the batch summary log (same ops vocabulary as the desc/crosswalk
       writers — a crashed card never hides inside a healthy-looking line).
    2. fru_crosswalk_enrich.py::crosswalk_and_record_specs — deterministic FRU
       crosswalk enrichment: ONE pass, TWO evidence channels over the same single
       fru_links query (rel_kind IN mfg_model + drive_pn), both gated by
       settings.fru_crosswalk_enrich_enabled. (a) DECODE channel: IBM/Lenovo FRU
       spare PNs inherit the STRICT-INTERSECTED decode of their rel_kind='mfg_model'
       models (PLUS rel_kind='drive_pn' when settings.fru_crosswalk_drive_pn_decode_enabled
       — the §2.6(c) GATED widening; measured 0% OEM-firmware-suffix misread so default ON,
       since drive_pn related parts are IBM/Lenovo FRU numbers the regex gates reject, but
       the desc channel reads drive_pn descriptions regardless of the flag) — only spec keys
       present in every decode with equal values write; a commodity disagreement skips the
       card (BOTH channels) — source="fru_matrix_decode" (tier 84), confidence 0.93. The
       card's MANUFACTURER is filled via spec_tiers.set_manufacturer (tier 84, conf 0.9)
       ONLY when EVERY decoded substitute identifies the SAME maker (DecodeResult.vendor;
       the decoder's regex gate is manufacturer-scheme-specific, so a unanimous vendor is a
       DETERMINISTIC maker — §2.6(d)/D4: never a prose inference) and the decode commodity
       agrees with the card's category — counted in manufacturers_set. (b) LINKED-DESCRIPTION channel
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
       any write); writes manufacturer ONLY via the deterministic-maker propagation
       above (the desc channel never writes a maker); never writes the reverse direction (a
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
       description→spec token grammar across NINE commodities (phase 1: hdd/ssd/
       dram; phase 2: power_supplies/displays/tape_drives/gpu/motherboards; wave
       3B: cpu — TRIO part-master/inventory descriptions like `HD, 450GB, 15KRPM,
       3.5", Fibre Channel`, `PSU, 1460W 240V/200V AC Hot Swap`, `SPS-CPU BDW
       E5-2650L V4 14C 1_7GHZ 65W`), source=
       "desc_parse" (tier 83), confidence 0.90 (settings.desc_parse_enabled). Zero LLM/network;
       extraction is suppressed on foreign commodity labels ("Other,"/"Tray,"/
       "Card,"/"Library,"…) and conflicting tokens, while NEUTRAL leads (packaging
       words/brands/SPS- prefixes: "ASSY,"/"MSI,"/"SPS-PCA,"…) fall through to
       body-token + category-hint arbitration instead of dying foreign. Per-module
       structural guards: wattage exists only on the power_supplies route while the
       cpu route emits tdp_watts (CPU "135W" TDP can never land in wattage), gpu
       memory_gb requires a GPU-context token (NVIDIA/GDDR/HBM/family hit) so NIC
       "10GB"/"100GbE" rows emit nothing, gpu_family maps consumer RTX models
       (x050–x090 adjacent to the RTX token, incl. comma-tokenized "RTX, 3070")
       to GeForce and emits NO family for the professional Quadro-successor line
       (RTX A2000, RTX 4000 Ada) or bare context-less RTX — "RTX" was REMOVED
       from the seeded gpu_family enum (trust hotfix 2026-06-12: it re-fragmented
       one physical family across two facet values, audit cards 583761 vs 560385),
       and a wrong family is worse than a missing one, bit-unit tokens
       ("2Gb, 128*16" component densities — uppercase letter + lowercase b) are
       neutralized BEFORE the upper-casing so bits are never recorded as GB
       capacity (skipped, never ÷8-converted), NAND-die context (_common.
       nand_die_context: the NAND word or an MT29-series die MPN — DIE-SPECIFIC
       signals only; cell-type tokens (SLC/MLC/TLC/QLC) and spaced x8/x16 tokens
       are deliberately NOT triggers because they appear on ordinary SSD/module
       listings whose bare "<n>G" IS a capacity ("SSD, 480G, TLC, SATA",
       "16G, 2R X8, DDR4") — re-audit 2026-06-10 + round-2 re-review) makes a
       BARE "<n>G" token a gigaBIT die density on the dram and hdd/ssd routes
       ("Nand, 512G, MLC" = 512 Gbit, deliberate no-write — explicit GB/TB
       tokens unaffected; the cat=dram miscategorization of NAND dies is a
       separate, still-open defect), and
       cpu bare cores/TDP tokens AND
       codename-only architecture require a CPU-context signal (MPN-echo descs
       and chassis rows emit nothing). Hinted extraction adds a body-token
       contradiction guard (a cpu-hinted motherboard FRU returns None; dram
       tokens under a cpu hint are exempt subordinate vocabulary). The cpu route
       adds a step-0 pollution deny-list (is_cpu_pollution — Murata/EPCOS
       B-clusters/AVX/TE/StorageTek shapes from docs/CPU_DECODE_FEASIBILITY.md)
       and a curated model→spec table (app/data/cpu_model_specs.json) merged
       UNDER desc tokens (skipped when the desc names two models, incl. dangling
       slash-alternates like "GOLD 6230R/6240R").
       In the worker SPEC stage only cards ALREADY categorized to one of the nine
       commodities are written (this stage NEVER categorizes). A separate,
       opt-in CATEGORIZE stage (writer.categorize_and_record, NOT run by the
       worker — only the one-shot CLI + ingest call it) closes that gap for
       UNCATEGORIZED cards: a strict lead/body grammar
       (desc_extractor/categorizer.py — the nine SPEC commodities via the reused
       extract_desc router with a stricter CPU-identity gate, plus anchored
       cables/batteries/fans_cooling leads with pollution suppression) infers the
       commodity KEY and, ONLY when card.category IS NULL, writes it via
       set_category at desc_parse/83 (own description) or fru_desc_parse/82
       (a linked fru_links description), then runs the SAME spec extraction for the
       fresh category in the same SAVEPOINT. Reuses the desc_parse identity — no new
       tier-83 source. Driven by app/management/categorize_from_desc.py (one-shot,
       dry-run default, --apply; own-desc + FRU-desc channels, real-desc gate
       alphanumeric-norm(desc) != alphanumeric-norm(display_mpn) and len >= 15,
       MaterialCardAudit action="categorized" per card) and at ingest time by
       source_ingest/clean.py (same grammar, fallback when the source carries no
       mappable Commodity_Code__c). The F1 ladder (fru_desc_parse 82 < desc_parse
       83 < partsurfer_desc 84 = connector_desc 84 < fru_matrix_decode 84 < mpn_decode 85 < vendor 90)
       keeps decode/vendor
       values authoritative and the card's OWN description above its FRU-linked
       prose — no per-writer pre-gate. The phase-2/3 commodities have no MPN
       decoders, so desc_parse is their top non-vendor deterministic source.
    3.5. enrichment_worker/worker.py::_partsurfer_desc_pass    — PartSurfer
       description enrichment (HTTP, paced), source="partsurfer_desc" (tier 84),
       confidence 0.90 (settings.partsurfer_desc_enabled, default ON). Runs AFTER
       the deterministic categorize/decode passes so a billable fetch is only spent
       on cards STILL uncategorized. For batch cards classified "hpe"
       (classify_oem_vendor) that are UNCATEGORIZED, it does ONE polite GET against
       https://partsurfer.hpe.com/Search.aspx (robots-allowed; UA
       "AvailAI-PartLookup/1.0 …"; 1 req / 2s paced with asyncio.sleep; capped at
       settings.partsurfer_fetch_per_batch, deduped by display_mpn) via
       partsurfer_resolver.fetch_partsurfer_description, extracts the OEM's own
       verbatim description (the ctl00_BodyContentPlaceHolder_lblDescription span),
       and feeds it into the SAME desc grammar through
       writer.categorize_and_record(source="partsurfer_desc"). PartSurfer's Product
       Number just echoes the spare (so the canonical-MPN crosswalk is useless for
       HP); the rich DESCRIPTION is the win — it categorizes the ~70k uncategorized
       HP cards. Resilient: fetch_partsurfer_description returns None on a GENUINE
       no-result (404/3xx, missing/empty span, invalid input) but RAISES
       PartSurferTransient on a throttle/outage (429, 5xx, or any httpx
       transport/timeout error) — the pass then BREAKS for the rest of this batch
       (stops hammering a struggling host; descriptions already fetched are kept).
       Each card's categorize_and_record is wrapped per-card (mirrors
       extract_and_record_specs) so one bad card (IntegrityError/DataError on the
       shared session) can't abort the pass — failures are tallied into the summary's
       "failed" key. categorize_and_record is fill-only (a card already categorized
       this batch is skipped via the NULL-category gate). partsurfer_desc (84) outranks the card's own desc_parse
       (83) — OEM catalog text beats the card's own desc — but loses to the
       deterministic decoders (mpn_decode 85); it ties fru_matrix_decode (84, a
       different vendor — tie not load-bearing).
       NEGATIVE CACHE (durable, partsurfer_desc_negative table — migration 125):
       BEFORE fetching, the pass drops candidate spares with a FRESH negative row via
       partsurfer_negative_cache.blocked_spare_norms, so a dead/ungrammatical HP spare
       is NOT re-queried every batch (the throughput win on the 145k not_found cards).
       A None no-result is recorded as reason="no_result" (90-day window, the same
       policy as oem_crosswalk no_match); a description that the grammar DECLINES for
       every card (an opaque/truncated reply — NOT evidence the OEM lacks the part) is
       recorded as reason="ungrammatical" with a SHORT 14-day window so the improving
       grammar re-checks it sooner; both via record_negative inside a per-spare SAVEPOINT.
       A throttle (PartSurferTransient) and a per-card DB failure are NEVER cached (a
       transient outage / write error is not a verdict on the spare). This is a DISTINCT
       sub-resource from the oem_crosswalk no_match cache (Pass A / canonical-MPN web
       resolution) — a spare can miss one and hit the other, so they use separate keys.
    3.6. enrichment.py::_apply_enrichment_to_card → _harvest_connector_enrichment    —
       connector-description harvest (no new network — harvests data the connector
       pipeline ALREADY fetches per call but _try_connector_config previously discarded),
       gated by settings.connector_desc_harvest_enabled (default ON). Runs after the
       existing manufacturer/category apply. Three writes: (a) the connector DESCRIPTION
       → writer.categorize_and_record(source="connector_desc", tier 84, conf 0.90) —
       categorizes an uncategorized card + fills facets via the SAME desc grammar (serves
       the dominant server-commodity cohort); (b) STRUCTURED fields package_type→package,
       pin_count→pin_count, rohs_status→rohs → record_spec at the connector's vendor-API
       tier (digikey_api/mouser_api/… 90, conf 0.95), schema-gated so they only stick on
       component-commodity cards whose schema defines the key (no-op elsewhere); (c)
       datasheet_url → the card.datasheet_url column (feeds the future datasheet sub-project).
       connector_desc (84) outranks the card's own desc_parse (83) — a distributor's
       authoritative description beats the card's own prose — and loses to the
       deterministic decoders (85); structured facets at vendor tier 90 are authoritative.
       v1 = enrichment path only; the pricing-search path (search_service) is a follow-up.
    3.7. vendor_spec_enrich.py + backfill_vendor_specs.py (the DEMAND-ORDERED, QUOTA-PACED
       backfill CLI — `python -m app.management.backfill_vendor_specs --source mouser|
       element14 [--apply]`)    — vendor-API parametric enrichment. Selects uncategorized
       cards demand-first (sourced_qty_90d DESC), searches the source within a date-keyed
       per-day call cap (`vendor_api:{source}:calls:{date}`, billed BEFORE each call), and
       enriches via the per-source writer. TWO source strategies (measured — see spec
       Revision 1): (a) MOUSER — Mouser carries a rich DESCRIPTION but NO structured
       parametrics, so `enrich_card_from_mouser` runs the description through the desc
       grammar and writes at connector_desc/84 (same identity as 3.6). (b) ELEMENT14 —
       Element14's `attributes` ARE structured parametrics; `element14.py:_parse` maps them
       to seeded spec keys (`app/connectors/_vendor_spec_map.VENDOR_SPEC_MAP`, a
       per-commodity vendor-attribute→seeded-key alias table; unmapped attrs land in the
       result's observable `dropped`) and `enrich_card_from_element14` records each via
       record_spec at element14_api/90 (the seed schema's enum/numeric+unit gate is the
       final arbiter — off-enum values are dropped, never coerced). Element14 rate-limits
       hard → a much lower default daily cap (100 vs Mouser 800), so it is a bounded
       top-demand SUPPLEMENT to the Mouser-description backbone. Both writers categorize
       fill-only (distributor category string → desc-grammar fallback) and are commit-free
       (the backfill owns per-chunk commits + per-card SAVEPOINT isolation). Both writers
       return a typed `EnrichSummary(categorized, specs_written)` (frozen dataclass — the
       backfill aggregates it by attribute, not by dict key). Commodities mapped so far:
       capacitors, resistors (the top-demand passives).
    3.8. enrichment.py::harvest_ebay_titles    — eBay-TITLE mining,
       source="ebay_title" (tier 83), conf 0.90 (settings.ebay_title_mining_enabled,
       default ON). Called by enrich_batch._process_one for EVERY card (not only the
       manufacturer-matched ones): eBay's connector returns NO structured manufacturer,
       so unlike the distributor connectors eBay has no place in the manufacturer-finding
       _CONNECTOR_CONFIGS loop — its sole value is the listing TITLE, a free-text part
       description. Each Browse listing's `ebay_title` is run through the SAME desc grammar
       distributor descriptions use → writer.categorize_and_record(source="ebay_title",
       tier 83, conf 0.90): categorizes an UNCATEGORIZED card + fills facets via the F1
       ladder. ebay_title (83) ties the card's own desc_parse (external marketplace
       free-text, noisier than a curated distributor description connector_desc 84) and
       loses to the deterministic decoders (85), so an eBay title can never displace a
       higher-tier value. DORMANT no-op (returns 0, no network) when the flag is off OR
       EBAY_CLIENT_ID/EBAY_CLIENT_SECRET are absent (creds gated via get_credential_cached,
       like every other connector). Best-effort: swallows + logs its own errors so a
       failure never aborts the unguarded batch loop; commit-free (caller owns the txn).
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
    newly core-enriched card ids>)  [paced, once per batch, same session; commits per
    chunk (load-bearing — see above); only verified/web_sourced/ai_inferred cards —
    never not_found]
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
SOURCE_TIER  manual:100
             · cpu_pollution_fix:96 (deterministic re-classification of the polluted `cpu`
               catch-all — TRIO's SFDC dump dropped ~67% non-CPUs into category='cpu' at
               trio_source/95; this beats that DEFAULT, loses to manual/100. Written ONLY by
               the bulk CLI `app/management/fix_cpu_pollution.py` (dry-run default; --apply
               commits) on category='cpu' cards, using the precision-first prefix classifier
               in `app/services/cpu_pollution/` — classify_polluted_mpn maps a definitively
               non-CPU manufacturer prefix (TE/Samtec connectors, Nichicon/AVX/EPCOS caps,
               Murata beads, Vishay resistors, TI/74-series/Broadcom logic) to its commodity
               while a CPU_GUARD blocks any real Intel/AMD identifier from ever being re-homed)
             · trio_source:95 · {digikey,mouser,nexar,element14,oemsecrets}_api:90
             · trio_source_ai:88 · mpn_decode:85 · fru_matrix_decode:84
             · partsurfer_desc:84 (HP PartSurfer description channel — the OEM's OWN
               verbatim description fetched live via partsurfer_resolver and fed to the
               desc grammar; outranks the card's own desc_parse 83, loses to mpn_decode 85,
               ties fru_matrix_decode 84 — different vendors, tie not load-bearing)
             · desc_parse:83
             · ebay_title:83 (eBay-title mining — an eBay Browse listing TITLE is an
               external marketplace free-text part description, fed to the same desc
               grammar; written by enrichment.harvest_ebay_titles. Same evidence class as
               the card's own desc_parse — external free-text, noisier than a curated
               distributor description, so below connector_desc 84 and ties desc_parse 83)
             · fru_desc_parse:82 (FRU-linked qual-sheet descriptions — below the card's
               OWN description, above the OEM scrapers)
             · {partsurfer,psref,oem_official}:80 (partsurfer/psref are written by the
               oem_crosswalk_enrich writer — decode channel 0.90, title channel 0.85 —
               and the broader oem_official umbrella is
               authoritative_enrichment_service's OEM-domain extractor; all the same
               evidence class) · web_search:70 · brokerbin:65
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

recategorize(db, card, new_category, *, source, confidence=1.0, force=False,
             reason=None) -> bool   # (P4.5) the ONE sanctioned way to change
    |   # card.category outside set_category itself — no caller may ever assign
    |   # card.category directly (the @validates("category") guard only blocks
    |   # off-vocab strings; it doesn't enforce the ladder or purge stale facets).
    +---> force=False (default): thin wrapper over set_category — full ladder
    |     arbitration, same semantics/return as above.
    +---> force=True: writes new_category UNCONDITIONALLY (no tier comparison) and
    |     — unlike every other path — leaves category_source/confidence/tier/
    |     updated_at UNTOUCHED. For re-spelling an ALREADY-provenanced value (the
    |     evidence hasn't changed, only its canonical string form); a fresh
    |     provenance stamp here would be dishonest (nothing new observed) and would
    |     wrongly reset the updated_at tie-break. Sole legitimate caller today:
    |     management/cleanup_known_bad.py's cleanup_junk_categories
    |     "normalized_in_place" branch (Pass 2).
    +---> either mode, on a real category CHANGE: purges the old commodity's stale
    |     MaterialSpecFacet rows + specs_structured mirror + orphaned
    |     validation_conflicts entries via _purge_stale_commodity_data (same
    |     helper set_category's on_change hook uses).
    +---> every actual write (either mode) logs a MaterialCardAudit row
          (action=category_recategorize, details: from/to/source/force/reason) —
          in ADDITION to any audit row the caller writes for its own broader op.

set_brand(card, value, source, confidence, write=True) -> bool        # dual-brand, mig 097
set_manufacturer(card, value, source, confidence, write=True) -> bool # dual-brand, mig 097
    +---> brand = the OEM LABEL (IBM, Dell Technologies, Lenovo);
    |     manufacturer = the ACTUAL MAKER (Seagate Technology, Hitachi/IBM verbatim)
    +---> None/empty/whitespace → no-op False (a write can never blank a value)
    +---> is_garbage_brand_value(value) → no-op False + WARNING (brand canon, mig 106):
    |     fragment shapes that can never be a maker name (len<2 after strip, or unbalanced
    |     parens — the comma-split residue of parenthesized MPN packing suffixes like the
    |     "F)"/"LF(T" carved out of Toshiba ordering codes "TLP781(D4-GR-TP6,F)"). The
    |     ingest parser (clean.extract_trailing_oem) rejects these at extraction, but the
    |     ladder is the SINGLE arbitration point for ALL writers, so junk dies here too
    |     (mirrors set_category's off-vocab WARNING).
    +---> normalize_brand_name(db, value) (manufacturer_normalizer.py — manufacturers-
    |     table canonical_name+aliases, per-process cache, miss → verbatim strip;
    |     writers NEVER normalize themselves). The manufacturers seed (startup.
    |     _seed_manufacturers) + migration 106 fold the HPE family 4 ways
    |     (Hewlett Packard Enterprise / HP / Hewlett Packard / Hewlett-Packard → HPE),
    |     case-fold Dell (DELL/Dell → Dell Technologies), and alias Texas Instruments (TI)
    +---> identical F1 ladder via the shared _set_provenanced_column (generic over the
          column prefix — set_category delegates to it too, behavior unchanged incl.
          the stale-commodity purge); valued-but-NULL-provenance existing ranks at the
          legacy floor 50; write=False dry-run twins. No new SOURCE_TIER entries —
          all dual-brand writers use already-registered sources.

record_validation_conflict(card, key, existing_prov, incoming_prov, incoming_value) -> bool
    # The validation-contract choke point (on-add enrichment, migration 099). Called from
    # the LOSE branches of record_spec (spec_write_service) and _set_provenanced_column
    # (write=True only) — i.e. AFTER the ladder already kept the existing value. The hook
    # lives in _set_provenanced_column because that is where ladder losses for ALL
    # provenanced columns are decided, so it covers category AND brand/manufacturer
    # (manual maker edits exist — update_material/add_material route them through
    # set_manufacturer at manual/100 — so they carry the same flagging contract).
    # Gates (all here, so call sites stay dumb): existing source == "manual"; incoming
    # tier >= 80 (the authoritative band — web 70 / brokerbin 65 / ai 40 never challenge
    # a human); normalized values differ (numerics as float, strings casefolded —
    # corroboration is never stored, and a same-source observation that now AGREES with
    # the manual value REMOVES that source's stale entry: deterministic sources re-fire
    # every pass, so a fixed decoder/corrected description unflags the card). Appends to
    # card.validation_conflicts (de-dupe per (key, evidence.source), newest replaces) +
    # sets has_validation_conflict. Arbitration is UNCHANGED — the manual value always
    # survives; this only persists the contradiction.

record_evidence_dissent(card, key, kept_prov, incoming_prov, incoming_value) -> bool
    # Trust architecture §1.2b: the companion of record_validation_conflict for the case
    # it structurally NEVER covers — the kept value is NOT manual. Before this, an
    # authoritative-vs-authoritative contradiction (trio_source category vs mpn_decode, a
    # FRU 373TB capacity vs the description's 36.4 GB) was resolved silently by tier with
    # no review artifact. Called from the SAME LOSE branches (record_spec's _incoming_loses
    # path and _set_provenanced_column write=True). Gates (all here): kept source != manual
    # (the manual case stays with record_validation_conflict); LOSING source tier >= 80
    # (CONFLICT_EVIDENCE_MIN_TIER); values differ under values_contradict (the SINGLE
    # comparison both recorders + the counter share, so corroboration/contradiction can
    # never be classified differently — and a same-source observation that now AGREES drops
    # its own stale dissent, like record_validation_conflict). Exactly ONE of the two
    # recorders fires per loss (manual-or-not is mutually exclusive). Persists into the
    # SAME validation_conflicts JSONB + has_validation_conflict flag (de-dupe per
    # (key, evidence.source)), tagged kind:"dissent" — the kept side is stored under the
    # "manual" sub-key (for the existing rendered conflict rows + accept endpoint) but
    # carries honest source/tier. Zero UI work: dissents surface in the already-wired
    # needs-review filter. Arbitration is UNCHANGED.

count_ladder_rejection(winner_source, loser_source, *, contradiction) -> None
    # Trust architecture §1.2c: persistent per-day ladder-rejection telemetry. NEVER raises
    # (telemetry must not break the write path — every failure is swallowed, logged DEBUG).
    # Called once per real (write-mode) rejection from both LOSE branches. Bumps Redis hash
    # intel:ladder:rejections:{date} (via app.cache.intel_cache.incr_hash_count, 35-day TTL)
    # field "{winner}|{loser}|{corroboration|contradiction}" — kind via values_contradict
    # so corroborations and contradictions are now distinguishable (before, rejections were
    # log-only and died with container rotation, and the two were indistinguishable). The
    # set_* + record_spec rejection log lines now name BOTH sides (winner source+tier AND
    # loser) so a rejection is diagnosable from the log alone.

clear_validation_conflicts(card, key) -> bool
    # Drops every entry for *key* and recomputes has_validation_conflict. Called by the
    # PUT updates when the field is re-asserted (routers/materials.py::update_material —
    # category + manufacturer, htmx_views::update_material_card — category), by POST
    # /api/materials/add when an existing card's category or manufacturer is manually
    # re-asserted through the modal, and by the conflict-accept route (only after a
    # SUCCESSFUL write — see below). A commodity flip's _purge_stale_commodity_data also
    # drops entries keyed by the purged spec keys (orphans — their manual values were
    # just removed) and recomputes the flag.
```

Dual-brand writers (W1-W7 — every write regex-gated or source-backed, never guessed):
| # | Evidence | Field | Source/tier | Conf | Where |
|---|---|---|---|---|---|
| W1 | `fru_links` `rel_kind='mfg_model'` rows with a manufacturer, joined on `normalized_mpn = related_norm` | manufacturer | `trio_source`/95 | 0.9 | backfill B2 + ingest |
| W2 | Description trailing token ∈ `OEM_TRAILING_RE` (IBM\|Dell\|HP\|HPE\|Lenovo) | brand | `desc_parse`/83 | 0.85 | backfill B3 + clean.py routing |
| W3 | Description trailing token ∈ `MAKER_TRAILING_RE` (Seagate\|Kingston\|Samsung) | manufacturer | `desc_parse`/83 | 0.85 | backfill B3 |
| W4 | Deterministic MPN decode vendor (skipped when the decode's commodity LOSES the category ladder — shared cross-commodity guard) | manufacturer | `mpn_decode`/85 | 0.9 | `mpn_decoder/writer.py` going-forward |
| W5 | Legacy `manufacturer` value ∈ `OEM_BRANDS` | copy → brand (manufacturer NOT cleared) | `legacy_backfill`/50 | 0.5 | backfill B1 |
| W6 | TRIO ingest sheet columns | both | `trio_source`/95 | 0.9 | `source_ingest/ingest.py` |
| W7 | Manual edit (PUT `/api/materials/{id}`, PUT `/v2/partials/materials/{id}`, Add-part modal) | manufacturer | `manual`/100 | 1.0 | `routers/materials.py::update_material` + `add_material`, `routers/htmx_views.py::update_material_card` (wins also clear the key's validation conflicts — a manual re-assert resolves the flag; the htmx PUT clears on any non-empty re-assertion, mirrors its category path, and never re-stamps an unchanged value) |
| W8 | Authoritative enrichment — exact-MPN distributor match (incl. cross-ref re-verification), OEM-official page, web extraction | manufacturer (+ category, same writers) | `{connector}_api`/90 · `oem_official`/80 · `web_search`/70 | 1.0 / result confidence | `authoritative_enrichment_service.py` apply_authoritative / apply_cross_ref_verified / apply_oem_sourced / apply_web_sourced — ladder-rejected writes are dropped from `enrichment_provenance` (it never claims a write that didn't land) |

Ladder losses are NOT silent: every NON-manual rejection logs at INFO inside
`_set_provenanced_column` (category, brand and manufacturer alike — W8's enrichment
writers have no aggregate counter, so this per-loss INFO is their only production
trace). On top of that, W4 surfaces `skipped_maker_conflict` (writer stats + batch
WARNING) and W6 surfaces `brand_conflicts`/`manufacturer_conflicts` (ingest stats +
batch WARNING) whenever a non-empty incoming value loses to a DIFFERENT existing value
(same-value losses are agreement, not conflict). Card MERGE
(`material_card_service.merge_material_cards`)
carries the source card's brand + manufacturer through `set_brand`/`set_manufacturer`
with the source card's STORED provenance (legacy floor when unprovenanced) — the ladder
arbitrates target-vs-source and the outcome is logged at INFO (the losing value is
destroyed with the merged-away card).

Backfill command: `python -m app.management.backfill_dual_brand [--apply]` — dry-run by
DEFAULT (write=False twins + an overlay mirroring apply's sequential writes, so dry
tallies == apply tallies); four ordered passes B1→B2→B3→B4 with SAVEPOINT-per-card;
soft-deleted cards are excluded from every pass; B2 reports winning link rows
(`links_won`) AND distinct cards (`manufacturers_set`); B3's `matched` ==
brands_set+manufacturers_set+skipped+missing_cards+failed; B4 prints the 9 known
dual-coverage cards and exits non-zero unless ST300MP0016 ends brand=IBM ∧
manufacturer=Seagate Technology. Run post-merge-deploy, never at startup.

Facet flow (combined "Brand" facet — heading-only rename of the manufacturers partial):
`get_manufacturer_options()` = UNION ALL over brand+manufacturer, COUNT(DISTINCT id)
(a card with brand == manufacturer counts once; commodity scope applies inside BOTH
branches); `search_materials_faceted(manufacturers=[...])` ORs
`manufacturer.in_() | brand.in_()` — OR-within-facet, AND-across-facets. Wire format
unchanged (`sub_filters={"manufacturers":[...]}`; the router pop and Alpine
`subFilters.manufacturers` are untouched — old bookmarks are a strict superset match).
Result rows render `brand · manufacturer` ("IBM · Seagate Technology") when both set
and DIFFERENT COMPANIES — the view (htmx_views materials list) compares NORMALIZED
forms (`normalize_brand_name`) and annotates `_show_maker_suffix`, so a B1 alias pair
("Hewlett Packard Enterprise" in brand, raw "HP" in manufacturer) renders once, never
as a tautological dual display (materials/list.html).

Consumers: `record_spec` (tier persisted into `specs_structured`, conflict via `resolve`),
`mpn_decoder/writer.py` (decode category via `set_category`, tier 85),
`fru_crosswalk_enrich.py` (tier 84), the SP-Ingest pipeline (`source_ingest/ingest.py` —
TRIO part-master categories via `set_category` at trio_source:95 / trio_source_ai:88,
specs via `record_spec` + dry-run parity via `spec_would_write`), the manual edit
endpoint `routers/htmx_views.py::update_material_card` (manual:100 for category AND
manufacturer — a deliberate human change always wins, a category flip purges the old
commodity's facets, a non-empty maker re-assertion clears its validation conflicts; an
UNCHANGED re-submitted value is NOT re-stamped manual — the maker guard compares
canonical to CANONICAL via `normalize_brand_name` on BOTH sides, since legacy cards
store non-canonical aliases ("TI") that the edit form round-trips verbatim —
off-vocab/blank categories AND a blank maker are rejected with a `showToast` warning
instead of persisting/blanked silently), and ALL the enrichment category writers — `enrichment.py` (connector
`{name}_api` tiers), `material_enrichment_service.py` (claude_haiku:40), and
`authoritative_enrichment_service.py` (apply_authoritative + apply_cross_ref_verified
at `{connector}_api`:90, apply_oem_sourced at oem_official:80, apply_web_sourced at
web_search:70 — for BOTH category and manufacturer, with ladder-rejected writes dropped
from `enrichment_provenance` — plus the claude_opus_inferred:40 AI fallback). **The
ladder monopoly is complete: every category/manufacturer writer routes through
`set_category` / `set_manufacturer` — no direct overwrite of `card.category` or
`card.manufacturer` remains** (the last fill-when-NULL maker write — `_apply_enrichment_to_card`
in `enrichment.py` — now routes through `set_manufacturer` too, so a connector maker
displaces a legacy NULL-provenance value (50 < 90) instead of only filling an empty one).
SP3 hardening is LIVE: `MaterialCard` carries an `@validates("category")` guard
(`app/models/intelligence.py`) that REJECTS any off-vocab direct assignment (raises
`ValueError`) — a future un-routed writer can no longer persist junk past the ladder; the
guard's canonical vocabulary is the single frozen `commodity_registry.CANONICAL_COMMODITY_KEYS`,
shared with `category_normalizer` so the two can never drift. Visibility: a NON-manual rejection logs at INFO for EVERY
provenanced column — category, brand AND manufacturer (mirrors `record_spec` — a
systematically losing writer must be visible at production log levels; the W8
enrichment writers carry no aggregate maker-conflict counter, so DEBUG-only maker
losses there would be production-invisible). Only manual submissions stay DEBUG (the
human gets endpoint feedback). The deterministic decode is protected by its tier (85), not by
running before the fru-crosswalk (84) / desc-parse (83) / AI spec (60) passes — the old
per-writer confidence pre-gates are removed.

### On-add auto-enrichment (single-add modal, inline passes, priority lane, validation conflicts)

Every card created by a user action gets (a) immediate deterministic enrichment,
(b) prioritized worker enrichment, (c) ladder-safe validation of user input with
persistent, surfaced conflicts. Manual values are NEVER overwritten by the system.

```
"Add part" button (materials workspace header; rendered ONLY for buyer-tier roles via
    |   dependencies.has_buyer_role — POST /api/materials/add is require_buyer, so the
    |   require_user workspace must not show an action whose submit would 403)
    |   --hx-get--> GET /v2/partials/materials/add-form (require_buyer, same reason)
    |   renders htmx/partials/materials/add_modal.html into #modal-content
    v
POST /api/materials/add  (routers/materials.py — exactly 5 fields: mpn required;
    |                     manufacturer / description / category / condition optional)
    +---> V3 intake validation, BLOCKING + never silent: normalize_mpn (>=3 chars),
    |     category via category_normalizer → canonical commodity, condition in
    |     MaterialCondition. Failure → 422 re-rendering the modal with per-field
    |     messages (htmx_app.js allows 422 swaps targeted at #modal-content only).
    |     The dedup-key gate (punctuation-only MPN → empty normalize_mpn_key) re-renders
    |     the modal too — every 422 from this endpoint is a modal re-render, never JSON.
    +---> resolve_material_card() create-or-resolve; manual values enter the F1 ladder
    |     at manual/100 (category via set_category — a winning manual category also
    |     clears any recorded category conflict, same re-assertion semantics as the PUT
    |     paths; manufacturer/description/condition columns + manual/100/conf-1.0
    |     entries in enrichment_provenance). Blank = blank — omitted fields stay NULL
    |     for enrichment to fill, never defaulted or guessed.
    +---> db.flush() → search_service.run_deterministic_passes(db, [card.id]) — the three
    |     inline zero-network passes (decode 85 → fru-crosswalk 84 → desc-parse 83; same
    |     feature flags as the worker; ladder arbitrates, order not load-bearing) so
    |     deterministic facets/category are queryable in the create response. Each pass
    |     runs inside its own SAVEPOINT (db.begin_nested) — a DB error escaping a
    |     writer's internal per-card savepoints rolls back to the pass boundary instead
    |     of poisoning the shared transaction (on PG that would fail the caller's commit
    |     and lose the just-created card/import/sightings; SQLite tests can't reproduce
    |     this — verify against live PG).
    +---> stamps card.enrich_requested_at (PRIORITY LANE — single-add ONLY; bulk import,
    |     stock import, email auto-import, source ingest and the search flow never stamp:
    |     they ride the bulk lane, ordered by demand telemetry — sourced_qty_90d /
    |     last_sourced_at, migration 105). Stamped
    |     ONLY when enrichment_status is selectable by the worker (unenriched/not_found/
    |     not_catalogued) — an already-enriched re-add must not hold a stamp nothing
    |     clears (run_one_batch is the sole clearing mechanism).
    +---> success → HX-Redirect: /v2/materials/{id} (the modal redirects to card detail).

Bulk surfaces gain the same server-side pipeline (no UI changes):
  POST /api/materials/import-part-numbers + /api/materials/import-stock → V3-invalid rows
  are skipped + surfaced as response `warnings: [{row, field, reason}]` where `row` is
  the 1-based SOURCE-file row (header = row 1; file_utils.extract_mpns_with_rows carries
  it) so the user can open the exact spreadsheet line; all touched card ids run
  run_deterministic_passes in the same session/commit. Search-driven creation
  (search_requirement's write session) runs the passes over ALL searched card ids — a
  deliberate deviation from the original spec ("newly created ids only"): the passes are
  idempotent through the ladder and re-searching an old card backfills its decode.
  The JSON `POST /api/materials/import-stock` route (`import_stock_list_standalone`) is now
  a thin wrapper over the shared `app/services/stock_list_ingest.py::ingest_stock_list`
  service (JSON contract unchanged); the Vendors-page HTMX route
  `POST /v2/partials/vendors/import-stock` reuses the same `ingest_stock_list`.

Worker priority lane + demand ordering (enrichment_worker/worker.py, migration 105):
  select_batch ORDER BY is `enrich_requested_at ASC NULLS LAST, (status=unenriched) DESC,
  sourced_qty_90d DESC NULLS LAST, last_sourced_at DESC NULLS LAST, id`. ASC NULLS LAST
  alone gives stamped-first FIFO (the old redundant leading `IS NOT NULL DESC` term is
  dropped so the ORDER BY matches the PG index ix_mc_demand_queue). After the priority
  lane and the unenriched-before-recheck term, the demand tiebreak is TRIO's OWN SFDC
  sourcing telemetry — sourced_qty_90d (90-day volume) then last_sourced_at recency,
  NULLS LAST so every demanded card drains before unmatched ones; id makes the order
  total. This REPLACED the old search_count/created_at demand keys. run_one_batch sets
  enrich_requested_at = None on EVERY batch card immediately after select_batch returns
  (attribute writes pre-await — the worker's no-query-after-await discipline; persisted by
  the batch-final commit), so a terminal not_found card cannot pin the lane. The same
  ORDER BY is mirrored in spec_enrichment_service.enrich_pending_specs (the scheduled
  spec pass) so every spec-pass dollar lands on the parts TRIO actually trades. SLA
  (worker healthy, caps not exhausted): deterministic facets immediate; connector/web/AI
  tiers P50 <= 90s, P95 <= 5min.

  Lane split (run_one_batch, settings.enrichment_lane_split_enabled default on): lane
  membership is captured (priority_ids = cards with a stamp) BEFORE the stamps are
  cleared; priority-lane cards call enrich_card(full_pipeline=True), bulk-lane cards
  call enrich_card(full_pipeline=False) — see the enrich_card tier sequence above for
  what each lane runs. Call routing only; the F1 ladder still arbitrates every write.

  Demand-telemetry backfill (app/management/import_demand_telemetry.py): ONE-SHOT
  operator command (dry-run by default; --apply to write) that streams TRIO's SFDC
  Weekly Export (LSC1__Material__c), aggregates Sourced_Qty_Last_90_Days__c +
  Most_Recent_Source_TS__c per normalize_mpn_key (column-wise MAX on dup keys), and
  bulk-updates sourced_qty_90d / last_sourced_at on matched, non-soft-deleted cards.
  Run at deploy AFTER migration 105. No recurring refresh — re-run only when a NEW
  export lands. These columns are a prioritization signal ONLY, never a displayed fact,
  so they bypass the F1 ladder (not provenanced category/spec columns).

Status badge (card detail header): htmx/partials/materials/enrich_status.html — while
  enrichment_status == unenriched it polls GET /v2/partials/materials/{id}/enrich-status
  every 15s ("Queued for enrichment"); the route answers HTTP 286 once the status leaves
  unenriched (htmx swaps the final tier badge + enriched_at and STOPS polling). A
  missing/soft-deleted card answers 286 with an empty body too (NOT 404 — htmx neither
  swaps nor cancels a poll on 4xx, so a detail view left open after deletion would poll
  forever).

Validation conflicts (storage: material_cards.validation_conflicts JSONB +
  has_validation_conflict + partial index, migration 099; write hooks:
  spec_tiers.record_validation_conflict + record_evidence_dissent — see the spec_tiers
  contract above):
  V1 decode-vs-manual spec keys (the decoder writes, the ladder rejects, the hook
  records); V2 manual category/brand/manufacturer vs an authoritative writer
  (_set_provenanced_column hook — covers every provenanced column, so manual maker
  edits carry the same contract; the decoder's cross-commodity guard is unchanged);
  V3 = the intake rejections above;
  V4 authoritative-vs-authoritative dissent (trust architecture §1.2b,
  record_evidence_dissent) — a kept NON-manual value contradicted by a losing tier>=80
  source, tagged kind:"dissent", stored in the SAME JSONB/flag (so it surfaces in the
  same needs-review queue and renders through the same conflict rows / accept route with
  zero new UI). Exactly one of V1/V2 (manual-kept) or V4 (non-manual-kept) fires per loss.
  Every real rejection also bumps the persistent Redis ladder-rejection counter
  (spec_tiers.count_ladder_rejection, §1.2c) classified corroboration vs contradiction.
  Surfacing: amber "Needs review — N conflict(s)" hero badge + per-key warning rows in
  the detail Specifications panel with tooltip ("Manual value kept. <source> reported
  <value> (conf <c>) on <date>") and an "Use this value" button →
  POST /v2/partials/materials/{id}/conflicts/{key}/accept (writes the evidence value at
  manual/100 — a human decision — via set_category / set_brand / set_manufacturer /
  record_spec per key, and clears the key's entries ONLY when the write succeeded; a
  no-op write — off-vocab category, schema gone after a commodity flip,
  enum/numeric rejection — keeps the entry and surfaces a showToast warning instead of
  silently destroying the only record of the contradiction). Clearing: any PUT
  re-assertion of a conflicted field clears that key (the JSON PUT rejects off-vocab /
  blank categories with 422 — never a silent drop; a manufacturer carried by the PUT
  or the Add-part modal clears its key on a ladder win the same way); a re-add through
  the modal clears the category key the same way; a commodity flip purges entries
  keyed by the purged spec keys; empty list → flag false.
  Review queue: "Needs review" checkbox in filters/global.html →
  `has_validation_conflict=true` validated in the faceted route → query branch in
  faceted_search_service (backed by the partial index).
```

### SP3: AI Account Screening (`app/services/prospect_screening.py`)

Called by: `run_enrichment_job` in `prospect_free_enrichment.py` (final step, fire-and-forget).
Calls: `claude_structured` (smart tier, structured schema, 512 tokens, `cost_bucket="ai_screen"`).

**Cost control:** daily cap via `intel_cache.get_count("ai_screen:daily:{date}")` /
`incr_count(...)` (`ttl_days=1`). Default cap: 200/day (`ai_screen_daily_cap`). The cap is
approximate (get/incr is non-atomic) — acceptable under the single-worker drain. Re-screens
only when `enrichment_data['ai_screen']` is absent, verdict is `insufficient_data`, or the
grounding has materially changed: each `pass`/`screened_out` verdict stores a
`grounding_fingerprint` (SHA-256 of the assembled context), and a cache hit requires the
current fingerprint to match — so a buyer re-triggering enrichment with new
contacts/firmographics/news forces a fresh screen rather than reusing a stale score.

**Verdict persistence:** `trio_match_score` + `opportunity_score` → indexed Integer columns
on `prospect_accounts` (SQL-sortable for `ai_match_desc` sort); full verdict →
`enrichment_data['ai_screen']` (JSONB). Scores only written for `pass`/`screened_out`;
`insufficient_data` sets `needs_more_enrichment=True` without writing scores.

**Buyer-ready ranking cache:** `prospect_priority.build_priority_snapshot()` is the source of
truth for the composite `buyer_ready_score`; a `ProspectAccount` before_insert/before_update
mapper listener (`app/models/prospect_account.py`) writes it through to the persisted, indexed
`buyer_ready_score` column on every flush, so the `buyer_ready_desc` list sort ranks +
paginates in SQL instead of loading + snapshotting every row O(N) per request. Migration 170
backfills existing rows. The single-prospect card/detail still recomputes the full snapshot
(reasons/proof-points are not cached). **Warm-intro lookup** (`prospect_warm_intros.detect_warm_intros`)
scans `sightings.vendor_email` and `site_contacts.email` with a leading-wildcard ILIKE
(`%@<domain>`); both are pg_trgm GIN-indexed (`ix_sightings_vendor_email_trgm` added in
migration 170; `ix_site_contacts_email_trgm` pre-existing from a513288799de).

**Gate:** `ai_screen_enabled=False` (default) → no LLM call; returns `{"verdict": "disabled"}`.

**Screened-out bucket:** `verdict=screened_out` hides account from main queue grid when
`sort=ai_match_desc`; recoverable via buyer "Claim anyway" override; threshold controlled
by `ai_screen_min_match=40`. The score threshold override runs after the LLM: even if the
LLM returns `pass`, the service sets `screened_out` when `trio_match_score < ai_screen_min_match`.

**List route integration** (`htmx_views.py`):
- Sort option `ai_match_desc`: ranks by `trio_match_score DESC → opportunity_score DESC → readiness_score DESC`.
- `_prospect_stats_ctx` gains `"screened_out": <count>` (only when `ai_screen_enabled=True`).
- `screened_out_prospects` context var passed to the list template for the collapsed bucket.

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
    |            CPU-bucket pollution deny-list, "DO NOT USE"/short-MPN drops.
    |            Dual-brand routing: a trailing description token matching
    |            OEM_TRAILING_RE → record.brand (OEM label, never a maker); any other
    |            plausible trailing token fills manufacturer when absent (legacy
    |            behavior). Brand is never inferred beyond that literal regex.
    v
consolidate.py — group by normalized_mpn → ConsolidatedPart per MPN (longest desc,
    |            modal manufacturer/brand/condition, highest-priority-kind category,
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
                 trio_source_ai:88), manufacturer + brand via set_manufacturer/
                 set_brand (trio_source:95, conf 0.9 — dual-brand W6; the new-card
                 constructor no longer writes manufacturer directly), specs via
                 record_spec, description/condition fill-only-when-empty ("Unknown"
                 == empty). Per-card SAVEPOINTs; tallies merge only after a clean
                 release; failed parts counted + sampled in the report. apply=False
                 (DEFAULT) = dry run through the SAME gates (set_category/set_brand/
                 set_manufacturer write=False + spec_would_write) so the go/no-go
                 report matches --apply exactly.
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
| storage.py | Seagate | `ST<GB (3-5 digits)><family><0-led tail>$` (ST4000NM0035, ST300MM0006) — the structured 0-led tail is the era gate; the capacity must also sit inside the family's shipped envelope (`_SEAGATE_ENVELOPE` — re-audit 2026-06-10: a digit-dropped truncation like ST120MM0198 passes the SHAPE gate, so out-of-envelope OR an unlisted family ⇒ NO specs, never a best-effort capacity — the refused value rides `DecodeResult.dropped` (reason `out_of_envelope`) so the rejection stays observable; the closed table also keeps Nytro/Pulsar SAS SSD families FM/FP from taking an hdd decode); legacy `ST<ff><digits><iface>` shapes (ST39103FC, ST373207LC) and STMicroelectronics order codes (ST232BDR, STM32… — explicit `_STMICRO_DENY`) return None | capacity, form_factor+usage_class for mapped families |
| storage.py | Western Digital | era split by SUFFIX SHAPE: legacy `WD<digits><exactly 2 letters>` = decimal-GB (WD800BB = 80 GB, WD64AA = 6.4 GB, capacity only); modern `WD<2-3 digits><4+ letters>` = revision-digit scheme (re-audit 2026-06-10): the FINAL digit of the numeric group is a revision/generation marker, capacity = leading digits as TB (WD40PURZ = WD42PURZ = 4 TB, WD101EFBX = 10 TB rev 1 — never 4.2/10.1 TB), sole exception the shipped Caviar-Green fractional points WD15/WD25 = 1.5/2.5 TB; 3-digit forms only with a recognized family token (unrecognized 3-digit+4-letter and ALL 4-digit+4-letter shapes are era-ambiguous → None) | capacity, usage_class+form for known 3.5" families |
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
never be misread as DDR4), legacy Seagate `ST<ff><digits><iface>` capacities (the digit
string mixes a form-factor digit with MB digits and the pre-~1996 era encodes UNFORMATTED
MB — no pattern-only era split exists, so those shapes return None; facet-accuracy audit
2026-06-10, `tests/test_mpn_decoder_storage.py` pins the audit cards), era-ambiguous
WD shapes (3-digit+4-letter without a known modern family token, all 4-digit+4-letter),
fractional-TB reads of WD's modern revision digit (re-audit 2026-06-10: WD42PURZ is 4 TB
rev 2, never 4.2 TB), out-of-envelope/unlisted-family modern Seagate capacities
(truncated/malformed strings get NO specs), and ANY hdd capacity off the discrete
shipped-capacity grid (`storage.HDD_SHIPPED_CAPACITY_GB` — applied in `decode_storage`
to every hdd decoder; an off-grid value moves to `DecodeResult.dropped`, never `specs`,
deliberately hdd-only: SSD capacities are near-continuous and ssd.py reads explicit
size fields, so the digit-string failure class doesn't exist there). The grid is built
with an INCLUDE-WHEN-UNCERTAIN bias: a false-accept of a possibly-real capacity costs
nothing, while a false-delete destroys correct decodes (round-2 re-review restored the
attested 15.3/27.3/90/140 GB WD Caviar points, 1.6 TB enterprise SAS, and 36 TB Exos M).
Both plausibility gates keep the refusal observable even when it empties the decode:
`decode_mpn` returns a specs-EMPTY result carrying `dropped` + a per-key
`drop_reasons` tag (`off_grid` / `out_of_envelope`), so capacity-only decodes (all
legacy WD, family-unmapped Seagate) never vanish as a bare None — write paths must
gate on `result.specs`, never on result-is-None. `rank`/`registered`/`voltage` are seeded `dram` spec schemas in
`commodity_seeds.json` (the boot seeder inserts them idempotently — no migration needed);
`tests/test_mpn_decoder_seed_sync.py` pins decoder↔seed sync, and `writer.py` logs an
aggregate WARNING for all FOUR silent drop channels — a decoded key with
no schema row, an enum value outside the LIVE row's enum_values (the worker decodes
against live DB rows, which can lag a deploy's reseed), the decoder's off-grid
capacity refusals, and its Seagate-envelope refusals (separate counters, split by
`drop_reasons`, so an over-tight envelope is distinguishable from an incomplete
grid) — so a drift or plausibility rejection can
never silently zero the feature (`record_spec` drops its two cases at DEBUG only; the
decoder drop is a pure function with no logging of its own). Cards skipped because
their existing category conflicts with the decoded commodity are counted too
(`skipped_category_conflict` in the per-batch stats, plus a WARNING with the
`card_category->decoded_commodity` pairs — the number that says whether the
category-alias map needs another entry).

Reconciliation after a decoder/extractor fix: `python -m app.management.
reconcile_decoded_facets [--apply] [--limit N] [--sources csv] [--keys csv]`.
**Trust architecture §1.2a generalized the scope:** `--sources` defaults to ALL four
deterministic facet sources (mpn_decode, desc_parse, fru_matrix_decode, fru_desc_parse)
and `--keys` defaults to EVERY spec_key in commodity_spec_schemas (was 2 sources × 3
audit-affected keys). mpn_decode/desc_parse rows are RECOMPUTED against the fixed
extractors; the fru sources have NO card-local recompute channel (the crosswalk depends
on fru_links workbook state) so they ride a capacity PLAUSIBILITY-GRID gate instead — an
hdd capacity_gb off `HDD_SHIPPED_CAPACITY_GB` is a misread → DELETE; on-grid → unchanged;
every other key/category is tally-only (`fru_ungated`/`skipped_ungated`, so coverage gaps
stay visible). A DIFFERENT re-run value is re-recorded through `record_spec` under the SAME
source (the F1 newest-timestamp tie-break lets the re-run win its own stale entry); a key
the fixed extractor no longer yields is DELETED from both material_spec_facets and
specs_structured (wrong is worse than missing — provenance stays honest). Dry-run by
default with per-failure-class tallies (round 1:
legacy_wd/legacy_seagate/stmicro_gate/gb_bit/rtx_family; round 2:
wd_revision_digit/capacity_grid/seagate_envelope/nand_density — the decoder's
`dropped`/`drop_reasons` channel attributes grid-emptied capacity-only decodes to
capacity_grid and envelope refusals to seagate_envelope, never to the shape-regex
fallback buckets; fru: fru_capacity_grid/fru_ungated); SAVEPOINT per card with BUFFERED
tallies merged only after a clean release. **Every run (dry-run AND apply) persists its
summary to the `reconcile_runs` table** via `record_reconcile_run` (both prior rounds'
apply tallies were log-only and are unrecoverable) — a dry-run commits the report row
AFTER its facet-write rollback, so the row is the only write a dry-run leaves.

Targeted FRU-graph drain (§2.6): `python -m app.management.run_fru_crosswalk
[drain|create|all] [--apply] [--limit N] [--measure-drive-pn]` — dry-run by
default, two phases. PHASE A (`drain`) runs `crosswalk_and_record_specs` over the
EXISTING cards that have a mfg_model/drive_pn FRU link but are still UNFACETED (no
material_spec_facets row) or UNCATEGORIZED (category NULL/blank) — the worker only
crosswalks whatever lands in its current batch, so this is the targeted runner;
dry-run wraps the writer in a SAVEPOINT and rolls it back, so the returned stats are
a REAL yield report with nothing persisted. PHASE B (`create`) creates MaterialCards
(category=None, unenriched) for two dangling populations so the worker's tier-84
crosswalk / tier-85 mpn_decode passes fire on the next loop: (b1) dangling enrichable
FRUs — a fru_norm with NO card whose linked models decode or whose link descriptions
extract; (b2) dangling canonical models — a related_norm (mfg_model/drive_pn, NEVER
lenovo_ppn) with NO card whose related_raw decodes to a recognized vendor. The ~31k
lenovo_ppn danglers are EXPLICITLY out of scope (display-only; §5 kill-list).
`--measure-drive-pn` reports the §2.6(c) gate: the OEM-firmware-suffix MISREAD rate of
decoding drive_pn related parts (a decode whose commodity/specs contradict the linked
qual-sheet description) — drive_pn decode widening defaults ON iff that rate ≤2%
(measured 0/3328 decode → 0%). All writes go through the F1 ladder (set_category /
set_manufacturer / record_spec); the orchestrator runs `--apply` post-deploy.

Categorize-from-description backfill (OPTIMIZATION_PLAN §2.4): `python -m
app.management.categorize_from_desc [--apply] [--limit N]` categorizes UNCATEGORIZED
cards from their descriptions via the shared lead-token grammar
(`desc_extractor/categorizer.py::categorize_from_desc`), then fills each freshly
categorized card's desc_parse facets in the same SAVEPOINT (the new category is
immediately food for the existing extractor). Two channels: OWN-DESC (a REAL
description — alphanumeric-norm(desc) != alphanumeric-norm(display_mpn) and len >= 15
— at `desc_parse`/83) and FRU-DESC (the card has no usable own description but a linked
`fru_links` row carries one — at `fru_desc_parse`/82). Category writes go through
`set_category` and ONLY when `card.category IS NULL` (fill-only — never reclassifies);
the grammar is conservative (foreign/ambiguous/conflicting/pollution → no write).
Dry-run by default (prints a yield report broken down by resulting category + channel,
writes nothing); `--apply` commits and logs a `MaterialCardAudit` (action
`categorized`, `created_by="categorize_from_desc"`) per card. The SAME grammar runs at
ingest time in `source_ingest/clean.py` (fallback when the source carries no mappable
`Commodity_Code__c`) so future imports categorize real-desc rows — single source of
truth, no duplicated grammar.

Stop-the-bleed trust hotfix (one-shot, post-deploy): `python -m
app.management.cleanup_known_bad [--apply]` — dry-run by default. Three idempotent
passes that remediate documented-bad catalog data the new guards now block at the
source: (1) DELETE the two documented-wrong facet rows (fru_matrix_decode
capacity_gb=373,455 and the hdd capacity_gb=973,452 outlier), matched by CONTENT not
row id, dropping the specs_structured JSONB mirror only when its source agrees; (2)
normalize-or-null every non-canonical `material_cards.category` (the pre-#267
bypass-writer residue) — resolvable values route through `set_category` at
legacy_backfill when unprovenanced or, when provenanced, through
`spec_tiers.recategorize(force=True)` (P4.5 — the ladder is deliberately NOT consulted,
since the evidence hasn't changed, only its spelling; source/confidence/tier/updated_at
are left byte-identical, and the stale facet purge still runs), unresolvable values are
nulled with provenance cleared; (3) stamp `manufacturer_source='legacy_backfill'` (conf
0.5, tier 50) on every card with a maker but NULL provenance (attribution of existing
data, NOT a ladder write; `manufacturer_updated_at` stays NULL so it ranks at the
runtime NULL-provenance floor). One `MaterialCardAudit` row per changed card (action
`facet_cleanup` / `category_cleanup`), plus `recategorize`'s own audit row (action
`category_recategorize`) for the normalized-in-place branch; dry-run rolls back, never
commits.

Brand/manufacturer canonicalization backfill (OPTIMIZATION_PLAN §1.5B, one-shot
post-deploy of migration 106): `python -m app.management.normalize_manufacturers
[--apply]` — dry-run by default. Scans EVERY non-null `manufacturer` and `brand` value
on material_cards (soft-deleted INCLUDED — restoring a card must surface a canonical
value, same contract as migration 100). Two classes, both classified from the same
distinct-value scan so the dry-run report cannot drift from `--apply`: (1) GARBAGE
(`is_garbage_brand_value` — the "(TP,F)" ingest-leak fragments "F)"/"F"/"LF(T" plus
empty residue) → value NULLed AND its four provenance columns (`<attr>_source/
_confidence/_tier/_updated_at`) cleared, so a later real write starts clean; (2) ALIAS
→ canonical via `normalize_brand_name` (HP → HPE, DELL → Dell Technologies), value cell
ONLY — provenance left byte-identical. This deliberately BYPASSES set_brand/
set_manufacturer (the documented exception, same as migrations 093/100): it corrects the
SPELLING of evidence that already won the ladder, not new evidence — re-stamping through
the ladder would forge a fresh source/confidence/timestamp for an observation that never
re-occurred. Any writer introducing NEW brand/maker evidence MUST still route through
the ladder. The orchestrator runs `--apply` post-deploy of migration 106.

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
    |    skew cross-metric ratios; daily ops cron via
    |    scripts/enrichment_coverage_cron.sh — host crontab runs it inside the
    |    app container, JSONL history persisted in the applogs volume)
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
    +---> Category provenance: categorized cards grouped by category_source
    |       ("(none)" = NULL provenance — a writer bypassing set_category or
    |       unbackfilled pre-096 data); spec-entry counts alone are WINS-only and
    |       spec-only, so a category-only ingest is visible ONLY here
    +---> Facet provenance: facet rows grouped by source ("(none)" = NULL rows
    |       the guarded 096 backfill could not match to a JSONB entry)
    +---> Unregistered-source callout: any observed source string (spec, category,
    |       or facet) missing from spec_tiers.SOURCE_TIER — such a writer ranks at
    |       tier 0 and silently loses every conflict; the report makes it trend
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
    |       Fixed-vocab enums with >12 values ALSO get a search-within box (P3, bound to
    |       ui.facetSearch[spec_key]); observed values outside the canonical list append in
    |       natural-numeric order via _natural_sort_key (P5, type-ranked so a mixed
    |       digit/alpha overflow never raises).
    |       Numeric specs (range widget) also expose common-value CHIPS — the top
    |       NUMERIC_CHIP_N (8) discrete value_numeric values by distinct-card count
    |       (get_subfilter_options option["chips"], displayed value-ascending) as a
    |       multi-select row above the min/max inputs; selecting chips filters via the
    |       "{spec_key}__vals" key → value_numeric.in_() in _apply_facet_filters
    |       (OR-within-facet, AND-across). Chip live counts come from get_facet_counts's
    |       numeric path (string-keyed by str(value), same pass-1/pass-2 self-exclusion as
    |       enums). The "__vals" branch precedes the generic list branch so it isn't
    |       mis-read as a value_text enum.
    |       Fold/typeahead state HOISTED to materialsFilter.ui.* so it survives the
    |       per-filters-changed HTMX reload. Counts via get_facet_counts() — which now
    |       SELF-EXCLUDES each actively-filtered facet (OR-within-facet; selecting one
    |       value no longer collapses its siblings to 0) AND receives the FULL card-level
    |       filter set (card_filters=: q / brand / confidence / global / sourcing) so a
    |       facet count never overstates versus the visible results — the count-honesty
    |       invariant (see "Count-consistency" note below). With NO commodity selected the
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
    |       has_crosses ("Has alternates" — has_crosses_predicate(), the single
    |                    shared predicate for every list/count path: EXISTS fru_links
    |                    on normalized_mpn in EITHER direction (fru_norm OR
    |                    related_norm; two separate ORed EXISTS so PG plans hashed
    |                    SubPlans over ix_fru_links_fru_norm/_related_norm — both
    |                    sides are normalize_mpn_key form, direct equality is the
    |                    canonical join) OR cross_references holds a non-empty list
    |                    (portable text-cast predicate — identical on PG JSONB and
    |                    SQLite JSON-as-text))
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
    |     condition / has_datasheet / needs_review) via get_global_facet_counts(filters=)
    |     — also fed the FULL active filter set and self-excluding each facet's OWN key, so
    |     these counts match the visible results too (count-honesty invariant). Both count
    |     containers now reload on `filters-changed from:body` (not just commodity-changed),
    |     carrying the same wire params (hx-vals object literal) as #materials-results.
    |     Containers load while hidden.
    Live result count "<N> results [in <Commodity>] [· matching "<q>"]" renders at the top
    of the results pane (list.html) every filters-changed cycle (match-framed so the number
    reads as "how many matched", not a bare part count; singular "result" when N==1), with a
    parallel sr-only aria-live announcement.
    Mobile drawer: x-trap focus trap + Escape-to-close.

Count-consistency invariant (count-honesty, OPTIMIZATION_PLAN §3.3 backend):
    The faceted sidebar counts MUST equal what the user sees after applying a filter.
    Enforced structurally by a single source of predicate + parse truth:
    +---> _apply_card_filters(query, db, **card_kwargs) in faceted_search_service.py is
    |     the ONE card-level predicate builder (incl. the universal deleted_at IS NULL
    |     guard + has_crosses_predicate()). The results list (search_materials_faceted),
    |     get_facet_counts and get_global_facet_counts ALL run through it, so a count can
    |     never apply a different predicate than the list. It returns (query, ts_query):
    |     a non-None ts_query is the PG multi-word FTS branch — the list orders by ts_rank
    |     with it; counts ignore it (ORDER BY in a grouped count is meaningless / PG rejects).
    +---> _parse_card_filter_params() + _pop_manufacturers() in htmx_views.py are the ONE
    |     wire-param parser, shared by the results route AND both count routes, so the
    |     list and the counts can never read the same query string differently.
    +---> Self-exclusion: each facet drops its OWN selection before counting (spec facets
    |     in get_facet_counts pass 2; card columns via the own_key drop in
    |     get_global_facet_counts) so checking one value never zeroes its siblings.
    Vocabulary honesty: panel facet enums are real, curated values — displays.resolution
    holds monitor/laptop panel resolutions (the unreachable character-LCD formats like
    16x2/128x64 were dropped from commodity_seeds.json; _RES_SEEDED in
    desc_extractor/display.py mirrors the seed list exactly). A changed seed enum is
    reconciled into commodity_spec_schemas by reseed_changed_schemas() (run at startup;
    covered by tests/test_count_consistency.py::test_reseed_reconciles_displays_resolution_row).

Coverage-aware empty states (get_commodity_spec_coverage(db, commodity) →
SpecCoverage(with_specs=N, total=M) NamedTuple; two cheap aggregates, no N+1):
    +---> Sub-filters panel header shows "N of M parts in <commodity> have filterable
    |     specs" so thin parametric results are explained before filtering.
    +---> Zero results + active parametric sub_filters + N < M → list.html renders the
    |     "not yet spec-enriched" nudge instead of the generic empty state.
Result-row upgrades (list.html, server-side in materials_faceted_partial):
    +---> Condensed 7-column layout (was 9): MPN · Description · Manufacturer · Status ·
    |     Vendors · Best Price · Last Seen. Category is folded as a muted sub-line under the
    |     manufacturer (no standalone column); Lifecycle + Condition are merged into the
    |     Status cell alongside the enrichment-trust badge (one wrapping badge group). Best
    |     Price renders 2 decimals at/above $1, 4 below (passive precision). Table carries
    |     the scoped .compact-table--dense modifier (4px row padding; shared .compact-table
    |     untouched). No data dropped — only regrouped.
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

### Universal Top-Search (global search bar)

The header search input (templates/htmx/partials/shared/topbar.html, name="q") debounces
into `GET /v2/partials/search/global` → `htmx.search_views.global_search` →
`global_search_service.fast_search(q, db, user)` → renders the grouped dropdown
`partials/shared/search_results.html`. Pressing Enter posts `/v2/partials/search/ai` →
`ai_search(q, db, user)` (Claude Haiku intent parse, falls back to fast_search). "View all"
renders the full page `partials/search/full_results.html` via `/v2/partials/search/results`.

```
fast_search(query, db, user)  — one universal entity search, grouped results:
    +---> 9 groups: requisitions, companies, vendors, vendor_contacts, site_contacts,
    |     parts, offers, material_cards (material hub), sightings.
    +---> Part number: matches Requirement / Offer / MaterialCard / Sighting by ILIKE
    |     AND by exact normalized_mpn == normalize_mpn_key(query) (separator-insensitive,
    |     index-backed), so a PN returns every req/offer/card/sighting it appears on.
    +---> Vendor: VendorCard surfaced by its own name/email/phone OR via a matching
    |     VendorContact (vendor_card_id subquery) OR a matching Offer.vendor_name — so a
    |     contact name / stocked MPN leads back to the card. Its contacts, offers, and
    |     sightings surface in their own groups.
    +---> Sightings carry their parent requisition_id (Sighting→Requirement join) for nav;
    |     material cards link to the Part Dossier (/v2/search?mpn=...).
    +---> Read-gating: for RESTRICTED_ROLES (SALES/TRADER) the requisition-scoped groups
    |     (requisitions, parts, offers, sightings) are limited to requisitions the user
    |     owns (created_by); requisition-less offers are hidden from them. Companies /
    |     vendors / contacts / material cards follow the app-wide all-visible read policy.
    |     user=None (legacy/test callers) ⇒ unrestricted. _run_intent_query (AI path)
    |     applies the same gate; ai_search caches only for unrestricted users.
    +---> Reuses: SearchBuilder (escape_like ILIKE + pg_trgm similarity order),
          normalize_mpn_key, MaterialCard.deleted_at soft-delete filter.
```

---

## Scoring System Hierarchy

```
unified_score_service.py (top-level, monthly)
    |
    +---> avail_score_service.py (behavior + outcomes)
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

SIGHTING SCORING (per search result, score_sighting_v2, app/scoring.py):
    scoring.py — 5-factor weighted (SIGHTING_V2_WEIGHTS):
        +---> trust        0.30  (authorized=95, else vendor_score, else 35 new-vendor baseline)
        +---> price         0.25  (median/unit ratio, capped 0-100)
        +---> qty           0.20  (coverage of target_qty, or flat 60 if qty known but no target)
        +---> freshness     0.15  (100 - age_hours/24*5; missing = 25)
        +---> completeness  0.10  (price/qty/lead_time/condition fields present)

LEAD SCORING (per sourcing lead, app/services/sourcing_leads.py):
    SourcingLead.confidence_score = sighting*0.5 + source_reliability*0.2 +
        freshness*0.15 + contactability*0.1 + historical*0.05, plus an ADDITIVE
        vendor-feedback adjustment (see "Vendor Feedback Loop" below).
    sourcing_score.py
        +---> freshness_score
        +---> source_reliability_score  (app/source_trust.py base + evidence-tier bonus)
        +---> contactability_score
        +---> historical_success_score
        +---> vendor_safety_score --> prospect_signals.py (+ vendor-feedback do_not_contact override)

PROSPECT SCORING:
    prospect_scoring.py
        +---> fit_score (ICP match)
        +---> readiness_score (buying signals)
        +---> prospect_priority.py (rank order)

ACTIVITY SCORECARD (per-user leaderboard, on-demand read):
    activity_scorecard.py  (CRM -> Activity tab; ALL users)
        +---> calls            = activity_log WHERE channel=PHONE (both directions)
        +---> emails           = channel=EMAIL AND direction=OUTBOUND
        +---> accounts         = COUNT(companies.created_by_id) per user
        +---> contacts         = COUNT(site_contacts.created_by_id) per user
        +---> total = calls + emails + accounts + contacts (raw sum) -> rank desc
        +---> 4 GROUP BY queries total (no per-user N+1); range:
              this_week / this_month (default) / this_quarter / all_time
```

### Source Trust Authority (`app/source_trust.py`)

Single authority for source-type reliability + evidence-tier trust, replacing scattered
per-caller constants. Pure data/lookup module, no I/O.

- `SOURCE_RELIABILITY_BASE` — source_type -> base reliability (0-100): authorized/API
  aggregators (digikey/mouser/farnell/element14/nexar/octopart) = 90; `avail_history`/
  `salesforce` = 85; **`brokerbin`/`sourcengine` = 80** (new API-marketplace bucket — they
  are direct API connectors, previously mis-bucketed with scraped marketplaces at 72);
  scraped marketplaces (netcomponents/icsource/thebrokersite) = 72; `ai`/`web` = 40;
  default 60.
- `EVIDENCE_TIER_BONUS` — bonus/penalty on top of the base, ordering
  **T1 (+8) > T2 (+5) > T6 (+3) > T3 (+2) > T4 (0) > T5 (−5) > T7 (−15)**. Deliberate
  correction: T6 (manual buyer entry) moved from −10 to +3 — a human-verified manual
  entry now outranks T3 (anonymous marketplace scrape).
- Source-type category sets (`AUTHORIZED_SOURCES`/`API_SOURCES`/`MARKETPLACE_SOURCES`/
  `EMAIL_SOURCES`/`MANUAL_SOURCES`/`HISTORY_SOURCES`) so `evidence_tiers.py` (tier
  assignment) and `services/sourcing_leads.py` (`_source_reliability` = base + tier
  bonus) share one membership list instead of drifting copies.
- `VENDOR_RELIABILITY_UNKNOWN` (25.0) / `VENDOR_RELIABILITY_KNOWN_NO_SCORE` (50.0) —
  reliability fallbacks consumed by `services/buyplan_scoring.py`'s `score_offer`
  vendor-reliability component when a vendor has no computed `vendor_score` yet.

### Vendor Feedback Loop (sourcing leads)

`services/sourcing_leads.get_vendor_feedback_adjustment(db, vendor_card_id)` rolls up a
vendor's buyer `LeadFeedbackEvent` history (joined through `SourcingLead`) into a
time-decayed adjustment — one grouped query, no N+1:

```
get_vendor_feedback_adjustment(db, vendor_card_id)
    +---> SELECT LeadFeedbackEvent ⋈ SourcingLead WHERE vendor_card_id = ...
    |       AND created_at >= now - 270d   (FEEDBACK_LOOKBACK_DAYS, ~3 half-lives)
    +---> per event: decay = 0.5 ** (age_days / 90)   (FEEDBACK_HALF_LIFE_DAYS)
    |       confidence_penalty += weight[status] * decay
    |       safety_penalty     += weight[status] * decay
    +---> do_not_contact = True if ANY event has status == "do_not_contact"
    |       (NOT decayed — a standing buyer instruction, never fades)
    return VendorFeedbackAdjustment(confidence_penalty, safety_penalty, do_not_contact)
```

Applied ADDITIVELY (not weighted further) so a `do_not_contact`/repeated `bad_lead`
history from THIS vendor meaningfully drags the score rather than being diluted:
- `_compute_confidence` adds `feedback.confidence_penalty` after the weighted sum
  (`sighting*0.5 + source_reliability*0.2 + freshness*0.15 + contactability*0.1 +
  historical*0.05`).
- `_compute_vendor_safety` adds `feedback.safety_penalty`; a `do_not_contact` event on
  ANY of this vendor's leads forces `safety_score <= 15` and appends the
  `buyer_marked_do_not_contact` flag — this override never decays, unlike the rest of
  the adjustment.

### CRM -> Activity Scorecard Tab (`/v2/partials/crm/scorecard`, ALL users)

`crm.views.crm_scorecard` depends only on `require_user` (no supervisor gate) — the
per-user leaderboard was relocated from Settings to a CRM tab at the owner's request so
every logged-in user sees it. It calls `activity_scorecard.compute_scorecard(db,
time_range)` and renders `crm/scorecard.html` (full tab) or `crm/_scorecard_table.html`
(fragment) when the time-range `<select>` fires an `HX-Request` (`HX-Trigger-Name:
time_range`). The "Activity" tab button sits beside Customers/Vendors in `crm/shell.html`
(loads its content into `#crm-tab-content` on click, like the other CRM tabs).

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

**Feature-flag resolution at job registration.** `register_all_jobs()` opens one
short-lived `SessionLocal()` (closed in `finally`) and passes it to the three
flag-reading registrars — `register_core_jobs`, `register_email_jobs`,
`register_offers_jobs` — which resolve `inbox_scan_interval_min`,
`activity_tracking_enabled`, and `proactive_matching_enabled` via
`admin_service.get_effective_flag/get_effective_int(db, key, settings.<flag>)`. So the
System-tab toggles are read at scheduler-config time (after
`run_startup_migrations()` has seeded + reconciled the rows); a deliberate admin flip
takes effect at the next scheduler reconfigure/restart. The interval/flag is also
re-resolved per run inside `_job_inbox_scan` (uses its own session). The resolver
degrades to the env default when the session is absent (`db=None`) or the DB read
fails, so registration never crashes.

**System settings tab (curated typed UI).** `settings_system_tab`
(`/v2/partials/settings/system`, admin-only) renders curated typed controls instead of
a raw key/value table. The catalog `SYSTEM_SETTINGS_META` (owned in
`app/routers/admin/system.py`) maps each of the four user-facing keys to
`{type: bool|int, label, help, restart, min}`: three toggle switches
(`email_mining_enabled`, `proactive_matching_enabled`, `activity_tracking_enabled`) and
one number input (`inbox_scan_interval_min`, min 5). Each control's displayed value is
the effective resolver value (`get_effective_flag/get_effective_int` with the env
default). Controls `PUT /api/admin/config/{key}` (hx-swap="none"); `api_set_config`
validates curated keys via `_validate_typed_value` — booleans accept only true/false,
the interval rejects `< 5` with a 400 `{"error": "Inbox scan interval must be at least 5
minutes."}` (the global handler toasts it) — and on success returns an HX-Trigger
`showToast`. The three scheduler-read settings carry an inline "Applies after the next
restart." note (`email_mining_enabled` resolves per-request, so it has none). Internal
watermark keys (`teams_calls_last_poll`, `8x8_last_poll`, `proactive_last_scan`,
`SYSTEM_JOB_STATE_KEYS`) are never editable — surfaced read-only in a collapsed
"Job state (read-only)" disclosure. The tab also lazy-loads the read-only Connector
Health panel (`hx-get /v2/partials/admin/api-health`, `hx-trigger="load"`, explicit
`hx-target="this"`, `hx-push-url="false"`) — per-connector telemetry rows plus an
overall roll-up badge assembled by `app/services/connector_health.py`; the partial
shares the tab's admin-only gate (`require_admin`).

---

## Frontend <-> Backend Pattern

```
BROWSER (HTMX + Alpine.js)

  0. Bottom navigation (mobile_nav.html):
     Reqs | Sightings | Materials | Search | Buy Plans | Resell | CRM | ...
     "Materials" tab links to /v2/materials, loads /v2/partials/materials/workspace
     "Resell" tab links to /v2/resell, loads /v2/partials/resell/workspace
     (resell/excess split-panel — see § Resell workspace).
     Quotes has NO top-level nav tab — surfaced via the Reqs and CRM account
     tab strips (see § 5 Quote Building).

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
          used across all pages (requisitions, parts, etc.)
      Sales-Hub look (opportunity-table tokens, originally built for the
          retired /requisitions2 and now canonical in shared/_macros.html): the
          Sightings list table + detail panel reuse opp_status_cell
          (status dot+label), coverage_meter (6-seg meter), .opp-col-header
          (th), .h4 / .figure-accent / .input / .btn btn-sm. Sightings-only
          treatments are preserved: the red/green _vendor_row row_tint
          (bg-rose-50/60 unavailable, bg-emerald-50/50 offer-in), the inline
          status <select> hx-patch advance-status, the per-vendor expandable
          drawer, the SSE refresh, and the multi-select action bar.
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
- `sightings_mark_available`
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

## Shared CSV Export (list downloads)

List/table "Export CSV" downloads share one formula-injection-safe streaming
helper, `app/utils/csv_export.py`:

- **`safe_cell(value)`** — stringifies a cell (`None` → `""`) and neutralises CSV
  formula injection: a value whose first character is one of `= + - @ \t \r`
  (the tab/CR whitespace triggers are included — omitting them is a known
  sanitizer bypass) is prefixed with a single quote so a spreadsheet treats it as
  text, not a formula.
- **`stream_csv(filename, header, rows)`** — returns a
  `StreamingResponse` (`text/csv`, `Content-Disposition: attachment`). `rows` is
  consumed lazily through a reused `StringIO` buffer (pass a generator /
  `yield_per` query so a large export never fully materialises in memory); every
  header + body cell passes through `safe_cell`.

Each `/…/export` endpoint builds a lazy row generator off **the list's own
filtered query** so the download mirrors exactly what the user is looking at (same
filters, no pagination), then hands it to `stream_csv`. Endpoints reusing the
shared helper:

| Surface | Endpoint | Router |
|---------|----------|--------|
| Parts (Sales Hub) | `GET /v2/partials/parts/export` | `routers/htmx/parts.py` |
| Materials | `GET /v2/partials/materials/export` | `routers/htmx/materials.py` |
| Requisitions | `GET /v2/partials/requisitions/export` | `routers/htmx/requisitions.py` |
| Vendors | `GET /v2/partials/vendors/export` | `routers/htmx/vendors.py` |
| Resell | `GET /v2/partials/resell/{list_id}/offers/export` + `.../outreach/export` | `routers/resell.py` |
| Approvals | `GET /v2/partials/approvals/{tab}/export` | `routers/htmx/approvals_hub.py` |

The **Sightings board** export (`GET /v2/sightings/export`, `routers/sightings.py`)
and the **CRM** exports (`GET /v2/customers/export.csv` +
`.../contacts/export.csv`, `routers/crm/export.py`) implement the identical
formula-injection-safe streaming pattern but with their own local `_safe_cell` /
`StreamingResponse` — they are the two origins the shared helper was extracted
from (see the `csv_export.py` module docstring), and remain candidates to fold
onto `stream_csv`. Frontend: each export button is a plain
`<a hx-boost="false">` that forwards the current filter query-string, so it
downloads without an HTMX navigation.

## Async Background-Run + Self-Poller Pattern (heavy on-demand jobs)

Several on-demand actions run a heavy Claude / web-extraction call (~30s) that
would otherwise block the click for the whole request. The pattern: the endpoint
schedules the work on FastAPI `BackgroundTasks` and returns **immediately** with a
partial that **polls itself** until the run finishes. Because these subjects have
**no DB status column to flip** — a blocked or no-op run leaves the persisted
state indistinguishable from "never ran" (e.g. `material_cards.enrichment_status`
stays `unenriched`; `cross_references` stays empty on a legitimate no-results
run) — the transient per-subject run state lives in **process-wide in-memory
registries** guarded by a `threading.Lock` (the app runs a single uvicorn worker
and the background task executes in that same process, so a module-level dict
suffices; it resets cleanly on restart, losing only a stale in-flight guard the
next click clears).

**Worked example — material enrichment** (`app/services/material_enrich_runs.py`,
`routers/htmx/materials.py`):

1. `POST …/enrich` (`enrich_material`) calls `enrich_runs.begin(card_id)` — a
   `threading.Lock`-guarded claim that returns `False` if a run is already in
   flight (the **double-enqueue guard**). On a fresh claim it flips the card to
   the `unenriched` "Queued for enrichment" marker, `background_tasks.add_task`s
   the worker (`_run_card_enrichment`, which opens its **own** session since the
   request session is already closed), and returns the detail partial right away
   with an enrich-status badge polling every 15s.
2. The worker records its terminal outcome via `enrich_runs.finish(card_id,
   blocked=…)` — `blocked` for a no-op / unavailable-source run, else `done`.
3. The poller (`GET …/enrich-status`, `material_enrich_status_partial`) reads the
   registry, not just the column: on a terminal status it **OOB-swaps the whole
   refreshed detail** (`HX-Retarget: #main-content` / `HX-Reswap: innerHTML`) so
   the user sees the new category/specs, and answers **HTTP 286** — htmx's
   stop-polling status. On a blocked run it surfaces the "couldn't complete" toast
   **once** via `consume_outcome` (pop-once) and keeps polling. A 404 (deleted
   card) also returns 286 so a dangling poll can't hammer the route forever.

The registry API is uniform: `begin` (claim / double-enqueue guard), `finish`
(record terminal outcome), `is_running`, `consume_outcome` (pop-once), `clear`.
The registries in use:

| Registry module | Singleton(s) | Action / trigger |
|-----------------|--------------|------------------|
| `material_enrich_runs.py` | `enrich_runs` **+** `crosses_runs` (two `_RunRegistry` singletons) | Material-card "Enrich" and "Find Crosses"/"Refresh" (`materials.py`) |
| `company_enrich_runs.py` | `company_enrich_runs` | Account (Company) "Enrich" (`routers/crm/enrichment.py`) |
| `contact_discovery_runs.py` | `contact_discovery_runs` | Account "Find Contacts" contact discovery (`routers/htmx/companies/contacts.py`) — deliberately separate from `company_enrich_runs` |
| `vendor_contact_runs.py` | `vendor_contact_runs` | Vendor "Find Contacts" (`routers/htmx/vendors.py`) |

(The Prospects tab enrich flow — § Prospect Enrichment — uses the same
schedule-then-poll-to-286 shape but tracks its run via a DB status, so it needs no
registry.)

## Routes Summary (400+ endpoints)

| Domain | Routes | Key Operations |
|--------|--------|---------------|
| Auth | 7 | OAuth login/callback/logout, status |
| Requisitions | 47 | CRUD, search, bulk archive/assign, claim. The canonical surface is `/v2/requisitions` (**Sales Hub** = the split-panel parts workspace, `partials/parts/workspace.html`); the legacy `/requisitions2` split-panel was retired in #622 — `app/routers/requisitions2.py` now 302-redirects every `/requisitions2/*` URL to `/v2/requisitions` (no templates, no offers/activity sub-routes). **View toggle (finding REQ-12)**: a segmented switch (`partials/requisitions/_view_toggle.html`, in the workspace eyebrow + the list header) flips between the Sales Hub workspace and the flat **"Requisitions list"** (`partials/requisitions/list.html`). Clean full-page push URLs: `/v2/requisitions` → workspace (default); `/v2/requisitions?view=list` → list (`v2_page` honours `?view=list`). Every link that loads the flat list partial (detail back-link, dashboard "Open Requisitions" card, proactive convert-success) pushes `?view=list` so a reload/bookmark reproduces the list. **Create/import flow (unified_modal)**: `POST /v2/partials/requisitions/import-save` parses the modal's `reqs[i].substitutes_json` (per-sub mpn+manufacturer) via `parse_substitute_mpns()` into the canonical `[{mpn, manufacturer}]` list (falls back to the legacy comma-joined `reqs[i].substitutes` MPN string) — never stores raw strings. On success it fires `HX-Trigger: reqListRefresh` (no longer hard-targets the workspace-only `#parts-list`); **both** launch surfaces listen for `reqListRefresh from:body` — the parts workspace `#parts-list` and a hidden hook in `requisitions/list.html` (reloads the list into `#main-content`) — so the create modal refreshes whichever surface opened it. The parts-tab edit row (`tabs/req_row.html`) coerces legacy string subs → `{mpn, manufacturer}` dicts before Alpine binds `sub.mpn`. **By-Customer grouping (Workspace grouping)**: `requisitions_list_partial` takes a `group_by` param; `group_by=customer` builds a server-side nested tree (Customer → Requisition → requirement lines) over the CURRENT PAGE's rows (page-scoped, mirrors sightings; ownership inherited from the already-filtered query) and renders `partials/requisitions/grouped.html`. Both levels collapse (keys `cust:<name>` / `req:<id>`) against a per-user `$persist({}).as('saleshub-group-collapse')` map on the list root x-data; groups start expanded. The `group_by` `<select>` lives inside `#req-filters` so grouping rides `hx-include` and stays sticky across filter/sort/page changes. A **Clean & reset** button does a full reset: GET `/v2/partials/requisitions` with no params (clears search + all filters + grouping) + expands all groups + clears the `selectedIds` bulk basket |
| Requirements | 23 | Add parts, CSV upload, search, leads, tasks |
| Vendors | 57 | CRUD, contacts, stock history, reviews, tags; new create: `POST /api/vendors` (201, 409 dup), `GET /v2/partials/vendors/create-form`, `POST /v2/partials/vendors/create`; delete UI: `DELETE /v2/partials/vendors/{id}` (admin, 400 if active offers) — both returning vendor detail/list HTML; stock-list upload UI: `POST /v2/partials/vendors/import-stock` (`import_vendor_stock_list`, require_buyer — thin wrapper over `stock_list_ingest.ingest_stock_list`, result banner into `#vendor-stock-result`); CRM parity: activity tab, add-note, tasks tab + CRUD, attachments; **migration 145 (P1)**: HTMX vendor contact CRUD (`POST /v2/partials/vendors/{id}/contacts` require_user, `PUT .../contacts/{cid}` require_user, `DELETE .../contacts/{cid}` require_admin, `POST .../contacts/{cid}/set-primary` require_user — clears all others atomically); ownership badge (`GET/POST .../claim` require_user, `POST .../release` require_user — wraps `strategic_vendor_service.claim_vendor`/`drop_vendor`); custom fields (`POST/DELETE /v2/partials/vendors/{id}/custom-fields[/{label}]` require_user, mirrors company custom-fields); is_primary column on vendor_contacts; custom_fields JSONB on vendor_cards |
| Companies/CRM | 47 | CRUD, sites, contacts, enrichment, import; CDM workspace (`/v2/partials/customers`, `/v2/partials/customers/account-list`); outreach logging (`POST /api/activity/outreach-initiated`); CRM task CRUD: `DELETE /v2/partials/tasks/{id}` (delete), `GET /v2/partials/tasks/{id}/edit-form` + `POST /v2/partials/tasks/{id}/edit` (edit; the edit-form fragment AND both validation-error responses re-carry the swapped container id `#account-tasks-{cid}`/`#contact-tasks-{ctid}`/`#vendor-tasks-{vid}` at their root — the flow outerHTML-swaps that container, so an id-less fragment would destroy its own Save/Cancel hx-target); account add-note: `GET /v2/partials/customers/{id}/activity/add-note-form` + `POST /v2/partials/customers/{id}/activity/add-note` (cadence-neutral, direction=None → no last_outbound_at bump); all three gates reuse `_is_crm_task_authorized` (task) or `can_manage_account` (note); contact merge (dedup): `GET /v2/partials/customers/{cid}/contacts/{ctid}/merge-form` + preview + `POST .../merge` (can_manage_account on source company, merge_contacts service); contact move: `GET .../move-form` + `POST .../move` (can_manage_account on BOTH source+target companies, target site must be active); **migration 144**: contact secondary fields (secondary_email, secondary_phone in EDITABLE_CONTACT_FIELDS), reports_to_id self-FK in create+edit; contact tag routes: `POST /v2/partials/customers/{cid}/contacts/{ctid}/tags` (assign segment tag by tag_id or tag_name), `DELETE /v2/partials/customers/{cid}/contacts/{ctid}/tags/{tag_id}` (unassign), `GET /v2/partials/customers/{cid}/contacts/for-select` (JSON list for reports_to picker, exclude_id param); EntityTag entity_type='site_contact' now valid; **bulk actions**: `POST /v2/partials/customers/bulk/{action}` (deactivate, send-to-prospecting, assign-owner) — auth-scoped: deactivate+send-to-prospecting gate per-company via `can_manage_account` (skips non-manageable; summary), assign-owner is MANAGER/ADMIN ONLY (403 for reps); **CSV import**: `POST /v2/partials/customers/import/preview` (parse+flag dupes/invalid, no writes) + `POST /v2/partials/customers/import/confirm` (create Companies, dedup by normalized_name, sets importer as account_owner_id); **contact CSV import**: `POST /v2/partials/customers/import/contacts/preview` (parse+flag duplicate emails) |
| Offers | 30 | CRUD, line items, accept/reject, changelog |
| Quotes | 26 | CRUD, send, PDF, e-signature, pricing history; terms editor modal (`GET .../{id}/edit-form` + `POST .../{id}/edit`) + Preview (`POST .../{id}/preview`) — OQ-08; "Valid Until" date picker persists as `validity_days` (no `valid_until` column); bare `/v2/quotes` 307→`/v2/requisitions`; list partial removed; detail `/v2/quotes/{id}` unchanged; surfaced via Reqs workspace + CRM account Quotes tabs |
| Buy Plans | 10 | submit/approve, SO+PO verify, confirm-PO, flag-issue, cancel (service + line cascade), reset; ops-group admin tab |
| Materials | 20 | CRUD, substitutes, stock levels, price history |
| Sightings | 27 | CRUD, RFQ send, batch RFQ, inquiry (cross-requisition composer: vendor-affinity GET + composer-vendor POST), vendor+part unavailability (mark/clear/reason modal). **Group collapse (Workspace grouping)**: when `group_by` is `brand`/`manufacturer`, each `partials/sightings/table.html` group is a collapsible section (chevron header + count; group data rows `x-show` on the map). State is a per-user `$persist({}).as('sightings-group-collapse')` map (group key = `<group_by>:<name>`, read off a `data-gkey` attribute so any name is quote-safe) owned by the stable `#sightings-table` container in `list.html` (survives HTMX swaps + reloads). Groups start expanded. A toolbar **Clean & reset** button does a full reset: GET `/v2/partials/sightings` with no params (clears status/q/group_by/manufacturer) + re-expands all groups + `$store.sightingSelection.clear()`. |
| Resell | 20 | Resell-brokerage workspace (`routers/resell.py`): lists, line items, import, inbound offers (per_line/take_all + unmatched queue), best-price rollup, build/close bid-back + PDF. Replaced the removed old-excess router (bids/solicitations gone) |
| AI | 18 | Parse email, normalize, find contacts, draft RFQ |
| Proactive | 12 | Matches, refresh, dismiss, send, scorecard |
| Prospects | 9 | HTMX tab only (JSON `/api/prospects/*` removed, consolidated): list, stats, add-domain, detail, claim, dismiss, release, enrich (background — spawns run_enrichment_job; pulls real contacts + firmographics via Lusha chain: enrich_entity + find_suggested_contacts, fill-only onto prospect columns; recomputes both fit_score and readiness_score; 24h gate prevents repeat paid pulls), enrich-status (poll; HTTP 286 stops) |
| Sources | 35 | Connector config, test, stocklist, webhooks; Settings Connectors tab (`GET /v2/partials/settings/connectors`, card partial, test-all); legacy `/sources` + `/api-keys` 302 → connectors |
| Tags | 4 | List, entity tags |
| Activity | 14 | Log calls, timeline, dashboards |
| Admin | 15 | Users, config, diagnostics, maintenance |
| Tickets | 12 | Error reports, trouble tickets, AI analysis; **admin-gated triage vs. open submission**: the maintainer triage console (`GET /v2/partials/trouble-tickets/workspace`, `.../list`, `.../{id}` and screenshot `GET /api/trouble-tickets/{id}/screenshot`) is `require_admin` (also hidden behind `{% if is_admin %}` Tickets tab in settings/index.html — non-admins can't see or reach it, closing the cross-user report leak), while the floating "Report a Problem" submission flow stays open to any login: `GET /api/trouble-tickets/form`, `POST /api/trouble-tickets/submit`, `POST /api/trouble-tickets` + `POST /api/error-reports` remain `require_user`. **In-shell triage wiring**: the Tickets console lives inside the Settings shell — `_build_ticket_list_context(db, status)` (in htmx_views) is the single list query+grouping helper shared by `GET .../list` and `POST /api/trouble-tickets/analyze` (analyze renders+returns the freshly-grouped `list.html` so the `innerHTML` swap into `#ticket-list` shows results — no more empty body + dead `HX-Trigger: ticketsUpdated`). The logical `status="open"` filter expands to `(submitted, in_progress)` so in-progress tickets stay under the workspace's "Open" pill. Drill-in rows/detail target `#settings-content` (no `#main-content`, no `hx-push-url`); the legacy full-page `GET /v2/trouble-tickets` route redirects 303 → `/v2/settings?tab=tickets` (registered before the generic `v2_page` catch-all; `v2_page` threads `?tab=` through for settings and `settings/index.html` maps the active tab to its first-paint content URL so `tab=tickets` lazy-loads the workspace, not a non-existent `/settings/tickets`). The detail status `<select>` gates its "Status updated" toast on `r.ok` (+`.catch`) so a failed PATCH shows an error toast |
| Documents | 2 | Requisition PDF, quote PDF |
| Quote Builder | 5 | Draft, save, send, signature |
| Events | 1 | SSE stream |
| HTMX Pages | 24 | Top-level page shells |
| HTMX Partials | ~100 | Tab content, forms, inline edits |

---

## Service Modules (123 top-level)

| Category | Count | Key Modules |
|----------|-------|-------------|
| AI & NLP | 9 | ai_service, ai_email_parser, ai_offer_service, tagging_ai |
| Search & Prospecting | 30+ | search_worker_base/, ics_worker/, nc_worker/, tbf_worker/, sourcing_leads |
| Email & Communication | 10 | email_threads, contact_intelligence, signature_parser |
| Scoring & Matching | 10+ | unified_score, avail_score, multiplier_score, proactive_matching |
| CRM & Data | 20+ | company_merge, vendor_merge, auto_dedup, enrichment |
| Vendor Mgmt | 9 | vendor_analysis, vendor_affinity, vendor_scorecard, vendor_duplicates |
| Buy Plans | 6 | buyplan_builder, buyplan_workflow, buyplan_scoring, buyplan_notifications, status_machine |
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
    +---> Step 4: Health checks — GET /health (LIVENESS only; gates the deploy)
    +---> Step 4b: GET /health/ready (READINESS, informational only — never gates)
    +---> Step 5: Verify deployed build tag matches built commit
    +---> Step 6: Verify CSS coverage
    |       +---> Scan templates for Tailwind color classes
    |       +---> Grep CSS bundle for each class
    |       +---> Warn on any MISSING classes (safelist gap)
    +---> Step 6b: Host worker venv refresh + restart (nc/ics/tbf)
    |       +---> pip install -r requirements.txt into /root/availai/.venv
    |       +---> systemctl restart avail-nc-worker avail-ics-worker avail-tbf-worker
    |       +---> WARN (re-surfaced after logs) if venv/restart fails
    +---> Step 7: Tail logs for errors
```

**Liveness/readiness split (P2.7).** `app/startup.py`'s ~20 boot-time ops split into
FAST (order-critical seeds/DDL-triggers/single-row checks, run synchronously in
`run_startup_migrations()` before `main.py`'s lifespan yields) and SLOW (full-table
backfills + `ANALYZE`, moved to `run_deferred_startup_backfills()`, launched as a
post-yield background task via `asyncio.to_thread` + `app.utils.async_helpers.
safe_background_task`). `GET /health` is liveness-only and answers as soon as the
FAST phase completes — it no longer blocks behind a prod-sized full-table scan, so
`docker-compose.yml`'s healthcheck and `deploy.sh`'s Step 4 wait loop (which poll
`/health`) can no longer false-fail a deploy on a large DB. `GET /health/ready`
reports a tri-state (module variable `app.startup.deferred_backfills_state`:
`running` / `completed` / `failed`, `app.constants.DeferredBackfillState`) as both
`{"ready": bool, "state": str}` — `ready` is only `true` when the phase reports
`completed`, so a deferred phase that crashes (`failed`) is never misreported as
ready. `deploy.sh` Step 4b curls it once, purely to log the state — it never gates
the deploy on it. `_maybe_analyze_hot_tables`
additionally gates the `ANALYZE` call behind a `system_config` marker keyed to
`BUILD_COMMIT`, so a same-image container restart skips it and only a genuine new
deploy re-runs it. Migration `187_startup_backfill_partial_idx` adds 8 PostgreSQL
partial indexes on the exact `IS NULL` predicates the deferred backfills scan, so
repeat-boot scans stay O(remaining rows) instead of O(table).

**Host worker dependencies (pinned-lockfile venv).** The `avail-nc-worker`
/ `avail-ics-worker` / `avail-tbf-worker` systemd units run on the HOST (outside docker, from
`/root/availai`, `User=root`) and execute
`/root/availai/.venv/bin/python -m app.services.{nc,ics,tbf}_worker.worker`.
That venv is built from the SAME pinned `requirements.txt` as the docker
app/enrichment images (not ad-hoc `pip install patchright beautifulsoup4`),
so the host workers carry identical pinned deps — notably `patchright`,
which they use to drive **system Google Chrome** via `channel="chrome"`
(the bundled Chromium is unused). `deploy.sh` Step 6b refreshes the venv
from the lockfile and restarts all three units on every deploy;
`scripts/setup_nc_worker.sh` / `scripts/setup_tbf_worker.sh` bootstrap the venv on a fresh host. The
unit files live in `deploy/avail-{nc,ics,tbf}-worker.service`.

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

### rfqVendorModal  (htmx_app.js, Alpine.data)

Backs `sightings/vendor_modal.html` — the "Send RFQ" batch-inquiry modal opened
from a sighting's vendor row (`requirement_ids=<id>`) or the table action bar
(comma-joined ids). Invoked as
`x-data='rfqVendorModal({{ suggested_vendors|selectattr("has_contact")|map(attribute="normalized_name")|list|tojson }}, {{ requirement_ids|tojson }})'`.
The seed list is filtered to **`has_contact` rows ONLY** — non-contactable rows render a
DISABLED checkbox with no `@change`, so seeding their names would force-count and
force-post a vendor the send path then silently skips (they enter the selection only via
the explicit Add-contact → `createVendor` path). The factory lives in JS (not inline)
because `|tojson` emits double quotes that would close a double-quoted `x-data` attribute
and break Alpine init; the data is carried through a **single-quoted** attribute (tojson
escapes `'`). State:
`step` (compose|preview), `selectedVendors` (a plain reactive object keyed by vendor
name — matches the `sightingSelection` store, not a Set; `selectedCount` getter +
`isSelected(name)` back the bindings), `emailBody`, plus the any-vendor picker /
inline-create state (`vendorQuery`, `vendorResults`, `searchOpen`, `addingVendor`,
`addingVendorBusy`, `newVendorName/Website/Email`). Methods: `toggleVendor`;
`selectVendor(name)` (server-returned composer rows `x-init` through it so they
arrive CHECKED); `searchVendors()` → `GET /api/autocomplete/names` filtered to
`type === "vendor"` client-side; `pickVendor`/`createVendor` → `_addComposerVendor()`,
a **raw `fetch` POST** to `/v2/partials/sightings/composer-vendor` (raw fetch, not
`htmx.ajax`, so a server 4xx is detected and the inline create form keeps its typed
values) that appends the returned row into the stable-id `#rfq-added-vendors`
sub-container via `insertAdjacentHTML('beforeend')` + `htmx.process` +
`Alpine.initTree` on the new node (so the row's `x-init='selectVendor(...)'` /
`:checked` / `@change` directives bind to this `x-data` scope and the row arrives
CHECKED — never the `x-data` wrapper, whose re-init would wipe selection state);
`loadPreview()` → `POST /v2/partials/sightings/preview-inquiry` (htmx.ajax swap into
`x-ref="previewContent"`); `confirmSend()` → `fetch POST
/v2/partials/sightings/send-inquiry` with the `x-csrftoken` header, then on success
`_refreshSightings()` re-GETs the open `#sightings-detail` (status auto-advances
OPEN→SOURCING + new activity rows) and the `#sightings-table` list, and dispatches
`close-modal`. `_form()` builds a `FormData` with **repeated** `requirement_ids`/
`vendor_names` keys (not `Object.fromEntries`, which collapses duplicates) from
`Object.keys(selectedVendors)` — so rows added at runtime (affinity rows,
autocomplete picks, inline creates) flow into `vendor_names` with no extra wiring.
`addContactFor(name)` backs the **"Add contact"** link on a non-contactable
(cardless / emailless) coverage-suggested row: it sets `newVendorName = name` **only when
the field is empty** (a half-typed manual entry survives the click), `addingVendor =
true`, then `$nextTick`-focuses the email input — reusing the inline-create form (source
4) so the buyer's typed email flows through the existing `composer-vendor` POST; no new
endpoint or invented state.
The vendor panel's four selection sources (coverage-ranked — now including cardless
discovery rows, affinity on demand, autocomplete, inline create) are documented
flow-level in section 3.

The route returns **HTTP 200 even on partial/total send failure** (failures are
captured, not raised) and exposes the true outcome via `X-RFQ-Sent` / `X-RFQ-Total` /
`X-RFQ-Skipped` response headers. `send_batch_rfq` tags each result `sent` / `failed` /
`skipped` (no contact email — logged, not silently dropped); the route counts only
`sent`, names `failed` vs "No email on file" vendors distinctly in the toast, and logs
activity + auto-advances status only for actually-sent vendors. `confirmSend` reads the
headers and toasts via `$store.toast`: full success, partial (warning, distinguishing
"N failed" from "N had no email"), or total failure (error — modal stays open to retry);
it never infers success from the HTTP status alone.

### attachmentsPanel  (htmx_app.js, Alpine.data)

Backs the one shared file-attachments component
(`templates/htmx/partials/shared/_attachments.html`, macro
`attachments_panel(kind, entity_id)`) used identically on **eight** surfaces:
Company "Files" tab, MaterialCard "Files" tab, the contact-card kebab "Files" modal,
the requisition Parts-tab per-requirement Files drawer, the offer card, the vendor card
"Files" tab, and vendor contact. The macro maps
`kind ∈ {requisition,requirement,offer,company,contact,material,vendor_card,vendor_contact}`
→ the per-kind list/upload/delete URL family internally; download/open is always
`GET /api/attachments/{kind}/{att_id}/content`.

Flow (HTMX-native, no JSON-then-client-render):
- The list container lazy-loads via `hx-get` the per-kind list URL on `load` and
  re-fetches on the internal `attachments:refresh` trigger (explicit `hx-target="this"`).
- The **eight list endpoints** branch on `HX-Request`: present → render
  `shared/_attachment_list.html` (HTML rows); absent → the legacy JSON array
  (back-compat — existing tests assert the array). The branch is centralized in
  `attachment_service.attachment_list_response(request, kind, entity_id, rows)`, which
  also owns the kind→delete-base map.
- Upload form (`hx-post` the list URL, `hx-encoding="multipart/form-data"`, `name="file"`,
  `hx-swap="none"`) and each delete button (`hx-delete`) dispatch a bubbling
  `attachments:changed` DOM event on success; the panel root catches it and fires
  `attachments:refresh` on the list (distinct names avoid a re-fetch→re-fire loop).
- The Alpine factory owns only interaction state: `dragging` (dropzone hover), `busy`
  (upload spinner, toggled off the form's `htmx:beforeRequest`/`htmx:afterRequest`), and
  `onDrop()` (assigns dropped files to the picker input → `requestSubmit()`).
- Upload failures surface the server `{"error": …}` via the global `htmx:responseError`
  toast handler — no per-panel error wiring.

The Parts-tab drawer is a **sibling `<tr>`** (tables can't nest rows) synced to the row's
paperclip toggle via a per-requirement `files-toggle-{id}` window event, so the drawer
spans the full table width without breaking the column grid. The offer card and contact
modal wrap the panel in `<template x-if>` so it only mounts (and lazy-loads) when expanded.

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

---

## SP4 Account Reclamation

Three inflows that feed idle CRM accounts into the prospecting pool.

### Daily 90-day hardline sweep (1AM UTC)
- Trigger: APScheduler CronTrigger(hour=1, minute=0) registered by `register_sweep_jobs()`
- Job: `app/jobs/prospecting_jobs.py::_job_account_sweep()`
- Delegates to: `app/services/prospect_reclamation.py::job_account_sweep_with_db(db)`
- Query: Company WHERE account_owner_id IS NOT NULL AND last ActivityLog.created_at > 90 days ago (or no activity)
- Idempotency: skips if ProspectAccount.swept_at IS NOT NULL already
- On match: calls `send_company_to_prospecting()` (clears ownership), sets `swept_from_owner_id`/`swept_at`/`reclaim_blocked_until = swept_at + 30d`/`discovery_source="auto_sweep"` on ProspectAccount
- Notification: `_send_sweep_notification()` → Graph `/me/sendMail`, ONE message per recipient (each in its own try/except so one bad address can't break the sweep). Recipients = rep + every ACTIVE user with role MANAGER/ADMIN + `settings.account_sweep_manager_email`, deduped case-insensitively by `_sweep_notification_recipients()`
- Token: `get_valid_token(owner, db)` from `app.utils.token_manager`; skips email on missing token (no failure)

### Daily unassigned past-customer surface (2AM UTC)
- Trigger: APScheduler CronTrigger(hour=2, minute=0) registered by `register_sweep_jobs()`
- Job: `app/jobs/prospecting_jobs.py::_job_auto_surface_reactivation()`
- Delegates to: `app/services/prospect_reclamation.py::job_auto_surface_with_db(db)`
- Query: Company WHERE account_owner_id IS NULL AND (EXISTS requisition OR EXISTS quote via CustomerSite)
- Skip: company already has non-dismissed ProspectAccount; company has no domain
- Creates: ProspectAccount(discovery_source="reactivation", status="suggested")

### Manual park: park_company_in_prospecting() [Task 8 — APPROVAL GATE]
- Service: `app/services/prospect_reclamation.py::park_company_in_prospecting(company_id, user_id, db)`
- Calls `send_company_to_prospecting()` then overlays `discovery_source="sales_park"`, `parked_by_id=user_id`
- HTMX endpoint: `POST /v2/partials/customers/{company_id}/park-in-prospecting` [NOT YET BUILT]

### Reclaim: reclaim_prospect_account()
- Service: `app/services/prospect_reclamation.py::reclaim_prospect_account(prospect_id, user_id, db, *, is_admin)`
- Permission: swept_from_owner_id == user_id OR is_admin OR `is_manager_or_admin(user)` OR user.email == account_sweep_manager_email
- Phase 4 cooldown: a former owner is DENIED with "This account is in a 30-day cooldown; ask a manager to reassign it." while `reclaim_blocked_until` is in the future; managers/admins (the supervisor set above) bypass it
- Actions: dismisses ProspectAccount, re-assigns Company.account_owner_id, logs ActivityLog(activity_type="reclaim")
- HTMX endpoint: `POST /v2/partials/prospects/{prospect_id}/reclaim` (require_user)
- Returns: {prospect_id, company_id, company_name, status: "reclaimed"}

### Manager reassign (Phase 4): reassign_account() — cooldown override
- Service: `app/services/prospect_reclamation.py::reassign_account(company_id, to_user_id, by_user, db)`
- Gate: `is_manager_or_admin(by_user)` else `HTTPException(403)`
- Actions: sets `Company.account_owner_id = to_user_id` (clears ownership_cleared_at); if a swept (non-dismissed) ProspectAccount exists for the company, dismisses it (`dismiss_reason="reassigned"`) and clears `reclaim_blocked_until`; logs ActivityLog(activity_type="reassign")
- HTMX endpoint: `POST /v2/partials/prospects/{prospect_id}/reassign` with `to_user_id` form param (require_user + in-route `is_manager_or_admin` gate)
- Returns: {company_id, company_name, to_user_id, prospect_id|None, status: "reassigned"}

### Prospecting tab UI wiring (DC-02)
The sweep notification tells the rep to reclaim "from the Prospecting tab"; these buttons make that reachable. `_reclaim_ui_flags(user, prospect)` (in `app/routers/htmx/prospecting.py`) computes per-(user, prospect) visibility — added to the card context (`reclaim_ui_map` keyed by id, so it renders identically in the grid loop and OOB card swaps) and the detail context (`reclaim_ui`).
- A prospect is "swept" when `swept_from_owner_id` is set. On a swept SUGGESTED card/detail the generic **Claim** button is replaced by **Reclaim** (posts the existing `/prospects/{id}/reclaim`) for anyone who can reclaim (former owner / manager / admin / `account_sweep_manager_email`); everyone else still sees plain **Claim**.
- A former owner inside the 30-day cooldown (supervisors bypass) sees Reclaim rendered *disabled* with a `title` of the unlock date — honest state, not a dead click; the POST wiring is omitted.
- Managers/admins additionally get a **Reassign** button that opens `reassign_modal.html` (loaded into `#modal-content` via `$dispatch('open-modal', {url: .../reassign-form?ctx=grid|detail&flt_status=...})`). The modal is a `to_user_id` picker of active users that posts `/prospects/{id}/reassign`; `ctx` sets the form's `hx-target` (`#prospect-{id}` outerHTML for grid, `#main-content` innerHTML for detail) so the swap matches the surface the action came from.
- Route `GET /v2/partials/prospects/{prospect_id}/reassign-form` (`reassign_prospect_form`, require_user + `is_manager_or_admin` gate → 403) returns that modal body.

---

## CRM Rubric Batch A — 2026-06-24

### Account Archive (DNC) / reactivate (migration 148)
- `POST /v2/partials/customers/{id}/deactivate` — Archive (DNC): sets `is_active=False`, clears `account_owner_id=None`, stamps `ownership_cleared_at`, stores optional `disposition_reason` from form; gate: `can_manage_account_team`; re-renders company detail partial with rose "Archived — Do Not Call" banner
- `POST /v2/partials/customers/{id}/reactivate` — sets `is_active=True`; gate: `is_manager_or_admin` (STRICTER than deactivate — owner cannot reactivate own account); banner disappears
- `GET /v2/partials/customers/archived` — lists all archived (`is_active=False`) companies; gate: `require_user`; shows DNC badge + reason; Reactivate button gated on `is_manager_or_admin` (template: `archived_list.html`)
- `POST /v2/partials/customers/{company_id}/sites/{site_id}/mark-dnc` — toggle `CustomerSite.do_not_contact`; gate: `can_manage_account`; returns updated `site_card.html` partial; DNC sites excluded from `staleness=needs_call` call-list (when company has active sites and ALL are DNC)
- Template `detail.html`: rose "Archived — Do Not Call" banner (replaces amber); Reactivate button gated on `user.role in ('manager','admin')` (not `can_manage_team`); kebab "Archive (Do Not Call)" with updated confirm text
- Template `site_card.html`: "Mark DNC" / "Clear DNC" toggle replaces deleted "Delete Site" action; DNC site shows `opacity-75 border-rose-200` + strikethrough name + DNC badge; `do_not_contact` field on `CustomerSite` (migration 148)
- Name search: `cdm_list_ctx` also queries `is_active=False` companies when `search` is non-empty; `archived_search_results` in template context shows them with "Archived" badge in account list

### CRM CSV export
- `GET /v2/customers/export.csv` — StreamingResponse, companies visible to the requesting user; managers/admins see all, reps see owned only (mirrors `cdm_company_query`)
- `GET /v2/customers/contacts/export.csv` — StreamingResponse, contacts under those companies
- Both set `Content-Disposition: attachment` and `text/csv`
- Registered in `app/routers/crm/export.py` → included in `app/routers/crm/__init__.py`
- Export links added to `list.html` below the filter form

## CRM P4 power-UX — bulk on contacts, Saved Views, filter-aware export (migration 167)

Extends the existing accounts power-UX (bulk select on `_account_list.html`, CSV import,
tags, inline-edit) to the **contacts** list and adds per-user filter presets. New UI is
minimal and mirrors the accounts pattern; no rearrangement of existing controls.

### Filter-aware CSV export (extends `app/routers/crm/export.py`)
- `GET /v2/customers/export.csv` now accepts the same filter query params as the accounts
  list (`search/staleness/account_type/segment/disposition/has_open_reqs/my_only/sort`) and
  streams the CURRENT filtered view via `cdm_company_query`. Reps stay scoped to their own
  visible set (`effective_my_only = my_only OR not is_manager_or_admin`); managers can add
  the My-accounts filter. Unfiltered call = all visible (unchanged default).
- `GET /v2/customers/contacts/export.csv` now accepts `search/company_id/contact_role/
  cadence_state` and streams via the role-scoped `customer_contacts_query`; the derived
  `cadence_state` facet is applied in SQL via `contact_cadence_predicate` (mirroring
  `customer_contacts_list_ctx`) so the whole set streams through `yield_per(200)` instead
  of being materialized (PERF-10). Headers unchanged.
- Both export links are progressive-enhancement anchors: `@click.prevent` rebuilds the
  download URL from the live filter form (`#cdm-filters` / `#contacts-filters`) via
  `URLSearchParams(new FormData(...))`; the bare `href` is the no-JS fallback (full export).

### Contacts bulk actions (`POST /v2/partials/contacts/bulk/{action}`, `companies/contacts.py`)
- Actions: `archive` (`SiteContact.is_archived=True`), `dnc` (`do_not_contact=True`) — both
  wire the existing single-row toggles (`set_contact_archive` / `set_contact_dnc`) to the
  selected set. Mirrors the accounts `customers_bulk_action` contract.
- Auth: per-contact via `can_manage_account` on the owning company (manager/admin act on all);
  non-manageable selections are silently skipped; the summary reports applied vs skipped.
- `ids` comma-separated form field (`_BULK_MAX_IDS=200`); re-renders `contacts_list.html`
  scoped to the hx-included `#contacts-filters`; `HX-Trigger: {showToast, clearSelection}`.
- UI: `contacts_list.html` gains an Alpine select scope (checkbox column + select-all + bulk
  action bar shown only when ≥1 row checked), mirroring `_account_list.html`.

### Saved Views — per-user filter presets (`app/services/saved_views_service.py`)
- New `saved_views` table (**migration 167**): `id / user_id (FK users CASCADE) / list_key
  ('customers'|'contacts') / name / filters (JSON) / created_at`; unique on
  `(user_id, list_key, name)` → re-saving a name upserts. (No existing per-user prefs/JSON
  store existed, so a dedicated minimal table was added rather than overloading `User`.)
- Service whitelists which filter keys persist per `list_key` (`ALLOWED_FILTER_KEYS`) and
  drops "all" sentinels (`""`, `"0"`); cap 50 views per (user, list_key).
- Routes (static, precede the `/{company_id}` catch-all):
  `GET /v2/partials/customers/saved-views?list_key=` (render control),
  `POST /v2/partials/customers/saved-views` (name + hx-included filter fields → upsert),
  `DELETE /v2/partials/customers/saved-views/{id}?list_key=` (user-scoped).
- UI: `_saved_views.html` (chip per view: click = apply, × = delete; `+ Save view` popover),
  embedded in both filter bars inside a `#saved-views-<list_key>` wrapper. Apply writes the
  stored filters back onto the filter form fields and dispatches `change`, reusing the normal
  filter-submit pathway (no new fetch path). `v.filters|tojson` lives in a SINGLE-quoted
  `data-filters` attr (never a double-quoted Alpine attr) per the quoting rule.

### Notes excluded from dormancy
- `app/services/activity_service.get_last_activity_at()` now excludes `NOTE`, `SALES_NOTE`, `CONTACT_NOTE` from the `func.max(created_at)` query
- `_NOTE_TYPES` frozenset defined at module level for clarity
- Effect: a company with only note activities is treated as dormant; real activity (email, call, quote, etc.) still resets the 90-day clock

---

## Vendor CRM Parity — Activity, Tasks, Attachments

Customer-parity feature giving vendor cards the same activity timeline, tasks, and file
attachments that CRM accounts have. All routes live in `app/routers/vendors/` and are
registered under `/v2/partials/vendors` (HTMX partials) and `/api/vendors` (JSON/upload).

### Vendor Activity Tab

```
GET /v2/partials/vendors/{id}/tabs?tab=activity
    |
    v
vendor_tabs() in app/routers/vendors/tabs.py
    |
    +---> activity_service.get_vendor_activities(db, vendor_card_id)
    |       Returns ActivityLog rows scoped to vendor_card_id, newest-first.
    |       Same timeline renderer as CRM account activity tab.
    |
    +---> renders vendors/tabs/activity.html (date-grouped timeline)
```

```
POST /v2/partials/vendors/{id}/activity/add-note   (require_user)
    |
    v
vendor_add_note() in app/routers/vendors/activity.py
    |
    +---> log_vendor_note(db, vendor_card_id, user_id, note_text, bump_last_activity=False)
    |       Writes ActivityLog(activity_type=NOTE, vendor_card_id=...) — cadence-neutral.
    |       Does NOT update last_activity_at on VendorCard (this is intentional —
    |       log_vendor_note(bump_last_activity=False) preserves the cadence clock for
    |       real outreach events only; notes are internal team annotations).
    |
    +---> re-renders the activity tab partial (OOB swap)
```

**Contract note:** `log_vendor_note(bump_last_activity=False)` from the UI add-note route
is cadence-neutral — it does NOT update `vendor_card.last_activity_at`. Real outreach
events (email, call) remain the sole drivers of the cadence clock.

### Vendor Tasks Tab

```
GET /v2/partials/vendors/{id}/tabs?tab=tasks
    |
    +---> get_open_tasks_for_vendor_card(db, vendor_card_id)
    |       Query: RequisitionTask WHERE vendor_card_id=? AND status!=DONE
    |       NOTE: Tasks scoped to vendor_contact_id only are NOT surfaced here.
    |
    +---> renders vendors/tabs/tasks.html (task list + add-form toggle)

POST /v2/partials/vendors/{id}/tasks   (require_user)
    |
    +---> create_vendor_task(db, vendor_card_id, title, due_at, user_id)
    |       Writes RequisitionTask(vendor_card_id=..., scope=vendor_card)
    |
    +---> re-renders tasks tab partial

POST /v2/partials/tasks/{id}/complete  (require_user)
    — already existed for CRM tasks; now also handles vendor tasks
    — _is_crm_task_authorized() gate covers vendor_card_id and vendor_contact_id scope

DELETE /v2/partials/tasks/{id}         (require_admin for vendor tasks)
    — already existed for CRM tasks; same handler covers vendor tasks

POST /v2/partials/tasks/{id}/snooze    (require_user; _is_crm_task_authorized gate)
    — pushes due_at forward 1 week; if no due_at sets to tomorrow (midnight UTC)
    — same authz as edit/complete: assignee, creator, account owner, or admin
    — re-renders the parent task list (account, contact, or vendor card)
    — returns 403 if not authorized, 404 if task not found

PATCH /v2/partials/requisitions/{id}/win-probability  (require_user; require_requisition_access)
    — sets win_probability (0-100) on a requisition; 400 on out-of-range
    — returns _win_probability.html inline span for HTMX swap
    — migration 146 adds the column to requisitions
```

### Vendor Attachments

```
GET  /api/vendors/{id}/attachments      (require_user)
    |
    +---> attachment_service.list_vendor_card_attachments(db, vendor_card_id)
    |       Returns VendorCardAttachment rows for the vendor.
    |       HX-Request present → shared/_attachment_list.html HTML rows
    |       HX-Request absent  → legacy JSON array (back-compat)

POST /api/vendors/{id}/attachments      (require_user, multipart/form-data)
    |
    +---> attachment_service.upload_vendor_card_attachment(db, vendor_card_id, file, user_id)
    |       Uploads to SharePoint/OneDrive library, inserts VendorCardAttachment row.

DELETE /api/vendor-attachments/{id}     (require_admin)
    |
    +---> attachment_service.delete_vendor_card_attachment(db, att_id, user_id)
            Deletes VendorCardAttachment row + removes from library if library_item_id set.
```

All three endpoints are consumed by the shared `attachmentsPanel` Alpine component via
`kind="vendor_card"`. Vendor contact attachments use `kind="vendor_contact"` with
analogous routes under `/api/vendor-contacts/{id}/attachments` and
`/api/vendor-contact-attachments/{id}`.

## Trouble Tickets — Unified bug + feature capture + AI Create-Prompt (2026-06-24, kind added 2026-07-03)

One ticket system, two kinds (`trouble_tickets.ticket_type` = `TicketType.BUG` |
`TicketType.FEATURE`): a user-facing report/request (any authenticated user) and an
admin-only management console with AI diagnosis + a notes-aware "Create Prompt" flow.
Migration `180_ticket_kind_discriminator` adds `ticket_type` (server_default `'bug'`,
so existing rows read as bugs). Both kinds share capture, inbox, and review.

```
Report capture (frontend, app/static/htmx_app.js):
  More menu "Report a problem"  → window.openTroubleReport('bug')
  More menu "Request a feature" → window.openTroubleReport('feature')
    |  sets window._ttKind; double-rAF (menu paints out) → captureTroubleScreenshot()
    |     lazy import('modern-screenshot') → domToPng(document.body)
    |     viewport-clamped PNG, 2MB downscale ladder (scale 1→0.75→0.5), null on any failure
    |  collectTroubleContext() → {nav_history, current_view (URL-derived), app_build, ...}
    +→ $dispatch('open-modal', /api/trouble-tickets/form[?type=feature])
  One shared form partial (shared/trouble_report_form.html) — kind-aware copy + accent
  (rose bug / violet feature), tightened layout (compact collapsible screenshot chip,
  no large empty box). Capture stores: errorLog (window.onerror + console.error/warn tee),
  networkLog (htmx:afterRequest).

POST /api/trouble-tickets/submit   (require_user)
    +→ _create_ticket(ticket_type=_coerce_ticket_type(body.ticket_type)): default BUG,
       'feature' → FEATURE; persists description, screenshot (disk), console_errors,
       network_errors, browser_info, auto_captured_context (JSON), current_view
    +→ BackgroundTask _generate_ai_summary (claude_text, fast tier)
    Screenshot storage durability (TT-0002): PNGs land in error_reports.UPLOAD_DIR
    (/app/uploads/tickets) on the `uploads` named volume, which the non-root app
    process (appuser) must own. Three layers keep it durable: (1) Dockerfile chowns
    it at build (fresh volumes); (2) docker-entrypoint.sh re-chowns /app/uploads to
    appuser on every start as root before runuser (existing/upgraded volumes);
    (3) startup.ensure_screenshot_storage() (called from main.py lifespan, NOT gated
    by run_startup_migrations' TESTING short-circuit) mkdir+os.access(W_OK) and raises
    RuntimeError at boot if still not writable. _save_screenshot() re-raises
    PermissionError/OSError (returns None only for bad/undecodable base64); the submit
    route catches them → JSON 500 {"error": "Screenshot storage is not writable — ..."}.

Profile avatars (parallel to TT-0002 storage): `app/routers/avatars.py` adds a second
`uploads`-volume subdir, `AVATARS_DIR` = `/app/uploads/avatars`, with the SAME three
durability layers (Dockerfile mkdir+chown of `/app/uploads/{tickets,avatars}`;
docker-entrypoint.sh `mkdir -p /app/uploads/avatars` + the existing recursive
`chown -R appuser:appuser /app/uploads`; `startup.ensure_avatar_storage()` called from
the main.py lifespan right after `ensure_screenshot_storage()`). Routes (all
`require_user`, own-profile by construction — no user path param): `POST /api/user/avatar`
(multipart; validates the REAL image type by magic bytes via `filetype.guess` ∈
{png,jpeg,webp,gif} + ≤2 MB — both the accepted type AND the on-disk `{ext}` derive from
the verified bytes, never the attacker-controlled `Content-Type` header, so a polyglot
labelled `image/png` can't be stored as `.png` and served back inline; writes
`user_{id}_{uuid8}.{ext}`, deletes the prior file, sets `User.avatar_path`, returns
empty 200 + `HX-Trigger` {avatarUpdated:{filename}, showToast}); `DELETE /api/user/avatar`
(clears path + file); `GET /api/user/avatar/{filename}` (FileResponse, path-traversal
guarded against `realpath` leaving `AVATARS_DIR`, like the screenshot serve route). The
uploader lives in `settings/profile.html`; the shared `user_avatar(user, size)` macro
(`shared/_macros.html`) renders the photo or an accent-tinted initials circle and is
applied in the `activity_row` macro (comm-ledger actor `a.user`) and
`buy_plans/_detail_lines.html` (the line-items partial included by detail.html; line
assignee `line.buyer`).

Pan/zoom face-centering (client-side, route unchanged): picking a file in the profile-photo
card no longer auto-uploads — it opens a circular crop viewport driven by the vanilla
`Alpine.data('avatarCropper')` component (`htmx_app.js`; no third-party cropper dep). The
image is painted to a `<canvas>` sized to the round viewport; the user PANS by dragging
(mouse/touch) and ZOOMS via a slider, the mouse wheel, or a two-finger pinch, all clamped so
the image always fully covers the circle (cover-scale `minScale`, focal-point zoom). On Save
the visible circular region is re-rendered into a 512×512 export canvas and `toBlob()`'d
(JPEG q0.9; PNG when the source carries alpha; the 512² downscale keeps it well under the
2 MB cap), then POSTed as multipart to the SAME `/api/user/avatar` route — canvas output is a
real JPEG/PNG so it clears the magic-byte gate unchanged. Because the upload is a `fetch`
(not an HTMX swap), the component bridges the route's `HX-Trigger`: it fires the global
`showToast` on `document.body` and a kebab-case `avatar-updated` on `window`; the card's
outer Alpine state listens via `@avatar-updated.window` (Alpine lowercases attributes, so the
route's camelCase `avatarUpdated` would not match an `@`-binding) and cache-busts the preview
`src`. The Remove button (HTMX `hx-delete`) re-dispatches the same `avatar-updated` on
success. The `user_avatar` macro is unchanged — it already renders `object-cover` in a
circular frame, so the pre-centered 512² result lands correctly everywhere. The circular
viewport + dimmed-ring mask are the `.avatar-crop-stage` / `.avatar-crop-mask` primitives in
`styles.css`. ESLint's browser globals (`Image`/`File`/`FileReader`/`Blob`/`performance`/
`Element`/`devicePixelRatio`) were added to `eslint.config.mjs` for the canvas APIs.

Admin console (require_admin):  Settings → Tickets  (tab admin-gated)
  GET  /v2/partials/trouble-tickets/{workspace,list,{id}}   (require_admin)
       list?status=&type=  → _build_ticket_list_context(db, status, ticket_type):
       one inbox, both kinds; the status + kind filter pills live INSIDE list.html
       (each pill carries BOTH current_status and current_type in its hx-get URL, so
       the two filters compose without client state — mirrors sightings/table.html).
       _row.html carries a bug/feature badge.
  GET  /api/trouble-tickets/{id}/screenshot                 (require_admin — closes IDOR)
  POST /api/trouble-tickets/analyze          → claude_structured groups into RootCauseGroup
  POST /api/trouble-tickets/{id}/diagnose    → ticket_diagnosis_service.diagnose_ticket
       (bugs only in the UI); returns _diagnosis.html + OOB _generated_prompt.html
  POST /api/trouble-tickets/{id}/generate-prompt → ticket_prompt_service.generate_ticket_prompt
       (persists hx-included admin_notes first, then writes generated_prompt)
  PATCH /api/trouble-tickets/{id}            → status / resolution_notes / admin_notes
  POST /api/trouble-tickets/diagnose-bulk    → diagnose_tickets_bulk (Semaphore(4), one commit)
  POST /api/trouble-tickets/bulk-status      → bulk resolve/wont_fix/in_progress
       (all set HX-Trigger "ticketsUpdated"; #ticket-list reloads on it)

ticket_diagnosis_service.diagnose_ticket (bug root-cause aid):
  _build_diagnosis_prompt (text-only — claude_client has no vision; console/network truncated)
  → claude_structured_with_usage(schema=DIAGNOSIS_SCHEMA, model_tier="smart")
  → persists diagnosis (JSON), generated_prompt (paste-ready Claude Code prompt),
    diagnosed_at, cost_tokens, cost_usd

ticket_prompt_service.generate_ticket_prompt (the key review flow, both kinds):
  kind-aware system + user prompt built from captured context + admin_notes
    - BUG     → fix task (page/route, console/network errors, page_state, screenshot ref, repro)
    - FEATURE → build task (page/surface, description + why), nudging brainstorm→plan→build
  → claude_text(model_tier="smart" → settings.anthropic_model; never hardcoded)
  → persists generated_prompt. Detail.html renders it once in the shared #ticket-prompt
    copy box (tickets/_generated_prompt.html), fed by BOTH Create Prompt (direct swap)
    and Diagnose (OOB swap).
```

Bulk selection state lives on the workspace Alpine component (`selected` array); row
checkboxes bind to it; `window.ticketBulkAction(kind, ids, status)` POSTs and fires
`ticketsUpdated`. The "Copy fix prompt" button reads the `<pre x-ref="fixprompt">` text.

### Approvals Engine — request lifecycle & notification flow (migration 159 cleanup)

`approvals.service.create_request(db, *, gate_type, amount, subject, requested_by, owner,
currency="USD")` is the single entry for spawning a routed approval:
- Persists an `ApprovalRequest`, setting the **polymorphic** `(subject_type, subject_id)`
  pair from the subject (`Prepayment` → `prepayment`, `QualityPlan` → `quality_plan`,
  `BuyPlan` → `buy_plan`) — no cross-table FK (mirrors `MaterialCardAudit`). `currency`
  (defaults USD) lands on `request.currency`; `prepayment_service.create_prepayment` passes
  `currency=prepayment.currency`, completing the currency contract.
- Flushes, calls `routing.route_request`, then records the **genesis `submitted`**
  `ApprovalEvent` + `ActivityLog` (`APPROVAL_REQUESTED`) — so every request's append-only
  trail starts with a creation row (its `event_type` is `submitted`). If routing raises
  `NoEligibleApproverError` the half-built request is **deleted** before re-raising, so a
  caller that catches the error and still commits leaves no orphan engine state.

`approvals.service.decide(db, request_id, user, action, comment=None)` resolves a request
(first-responder-wins, row-locked). On resolution it records one `approved`/`rejected`
`ApprovalEvent` and enqueues **two** `ApprovalOutbox` rows for the notify user
(`owner_id` → `requested_by_id` → decider) — one `channel='in_app'` and one
`channel='email'` (Mike's locked dual-channel notice). Both carry the same payload
`{"event_type": "decided", "decision": <approved|rejected>, "comment": <comment>}`.

**On-resolve subject dispatch (QP Phase C1):** after the flush, a `subject_type=='buy_plan'`
request drives the EXISTING buy-plan side effects in the SAME session — approve →
`buyplan_workflow._run_approve_side_effects` (`buyplan_approval.py`; plan `ACTIVE` + `_generate_buyer_tasks` +
approver stamp + audit `ActivityLog`); reject → `_run_reject_side_effects` (plan `DRAFT`).
The dispatch runs inline with **no swallowing try/except**, so a side-effect failure
propagates and the router's transaction rolls the whole decision back atomically — a request
can never land `APPROVED` while its plan stays `PENDING` (RISK 1). `_run_approve_side_effects`
/`_run_reject_side_effects` are the single arbitration point shared by BOTH the engine
dispatch and the legacy `approve_buy_plan`, so the two paths can never drift.

`jobs.approval_outbox.dispatch_pending` drains the outbox (60s job):
- `in_app` → `notifications.write_in_app`; the `Notification.body` now carries the decision
  `comment` (`payload.get("comment")`; `None` when no comment was given).
- `email` → `notifications.send_email` via Graph (`_build_email_html`) — **now a live path**
  (previously dead: `decide()` only ever enqueued `in_app`). Send failure increments
  `fail_count`/`last_error`; the dead-letter cap (`MAX_OUTBOX_FAIL_COUNT`) retires a broken row.
- Channel server_default is `in_app` (migration 159) so an outbox row without an explicit
  channel is never silently treated as email.
- **Transaction model: PER-ROW COMMIT, not SAVEPOINT.** Each row commits in its own
  transaction (`row.sent_at = now; db.commit()` on success; `db.rollback()` +
  `_mark_failed` (which itself commits) on failure). This per-row isolation — not a
  `db.begin_nested()` SAVEPOINT — keeps a failing row from poisoning the batch. A SAVEPOINT
  is deliberately NOT used because the email path (`send_email` →
  `token_manager.get_valid_token`) commits the session MID-ROW when it refreshes an expired
  Graph token, which ends any enclosing savepoint; the old SAVEPOINT design therefore raised
  `ResourceClosedError` at `savepoint.commit()`, aborted the whole batch, and stranded the
  email row (sent_at NULL, fail_count 0) while the sibling in_app row had already delivered.
  A dispatch path that commits internally is fundamentally incompatible with an enclosing
  savepoint, so each row owns its own commit.

**Buy-plan submission → engine gate (QP Phase C1):** `buyplan_workflow.submit_buy_plan`
(`buyplan_approval.py`; and `resubmit_buy_plan`, same module), on the non-auto-approve path, call
`_open_engine_request_for_plan(plan, user, db)` — which FIRST cancels every existing open
(`REQUESTED`) `ApprovalRequest` for the plan via `events.cancel` (so a resubmit never leaves
two live requests — RISK 2), THEN `create_request(gate_type=BUY_PLAN, subject=plan,
requested_by=owner=user)` routes a request to every `can_approve_buy_plans` holder. No
approver configured → `NoEligibleApproverError` is caught, logged WARNING, plan stays
`PENDING` with no engine state. The live approve/reject POST
(`/v2/partials/buy-plans/{id}/approve`, `buy_plan_approve_partial`) looks up the open
`BUY_PLAN` request and resolves it via `decide` (side effects run inside `decide`); if NO
open request exists — a plan that went `PENDING` before C1 deployed — it falls back to the
legacy `approve_buy_plan` and logs a WARNING (RISK 3, transition window, removed in a
follow-up).

**QP front door — open-or-create from a buy plan (`GET /v2/qp/for-buy-plan/{bp_id}`):**
the only entry point to the native QP view. The buy-plan detail (`buy_plans/detail.html`)
renders a secondary "Quality Plan" button (`hx-get="/v2/qp/for-buy-plan/{bp.id}"`,
`hx-target="#main-content"`, `hx-push-url="true"`) in the header actions row, always shown
(independent of the role-aware action banner). The route (`quality_plans.qp_for_buy_plan`)
ownership-scopes via `get_buyplan_for_user(db, user, bp_id)` (404 for a missing plan OR a
`RESTRICTED_ROLES` non-owner — existence not leaked, enforced BEFORE any create), then
get-or-creates: `db.query(QualityPlan).filter_by(buy_plan_id=bp.id).first()`, else
`create_qp(db, owner_id=user.id, buy_plan_id=bp.id)` + commit. It is idempotent — a buy plan
has at most one QP, so a second open returns the same row (no duplicate). It re-loads the QP
with the same eager options `qp_detail` uses and renders via the shared `_qp_detail_response`,
so the user lands on the native QP detail. The QP detail's header sub-line carries a back-link
to the buy plan (`hx-get="/v2/partials/buy-plans/{bp.id}"`, `hx-target="#main-content"`) so the
view is not a dead-end. **Full-page reload / bookmark (SET-05):** the button `hx-push-url`s this
url into history, so a raw browser reload arrives WITHOUT the `HX-Request` header — the route
content-negotiates (`request.headers.get("HX-Request") != "true"`) and serves the app shell via
`full_page_shell(request, user, request.url.path, "buy-plans")` (`routers/htmx/_shared.py`), whose
`base_page.html` loader re-fires `hx-get` at this same url WITH the header to paint the QP; the
non-HTMX pass is side-effect-free (get-or-create runs only on the HTMX pass). Coverage:
`tests/test_qp_entry.py` (create-on-first-open, idempotent second open, restricted-non-owner 404
with no QP created, button renders the for-buy-plan hx-get, full-page reload serves the shell).

**QP section gates — Sales / Purchasing (QP Phase C2a):** the QualityPlan is the engine
subject (`subject_type='quality_plan'`); the `gate_type` discriminates the section.
`routing.route_request` gains `QP_SALES` → every active `can_approve_qp_sales` holder
and `QP_PURCHASING` → every active `can_approve_qp_purchasing` holder (no amount check, like
`BUY_PLAN`; `QP_PURCHASING` de-collided from the deal-level `PURCHASE_ORDER` gate in SP-3).
`quality_plan_service.submit_section(db, qp_id, gate_type, user)` calls
`create_request(gate_type, amount=None, subject=qp, requested_by=owner=user)`; a missing
approver raises `NoEligibleApproverError`, which `submit_section` re-raises as
`NoSectionApproverError` — the router (`POST /v2/qp/{id}/submit-sales`,
`/submit-purchasing`) catches it and re-renders the QP detail with an inline "no approver
configured" banner (NEVER a 500), with no orphan request (`create_request` already deleted
the half-built row). On resolution, `decide()`'s on-resolve dispatch — after the BUY_PLAN
block — runs for `subject_type=='quality_plan'` AND `gate_type ∈ {qp_sales,
qp_purchasing}`: a LAZY import (circular-safe) of
`quality_plan_service._on_section_approved(db, qp_id, gate_type, approved)`, which logs
an `APPROVAL_APPROVED`/`APPROVAL_REJECTED` `ActivityLog`. The QP detail renders a per-section
gate-status chip from `_get_gate(db, qp_id, gate_type)` (latest `ApprovalRequest` for the QP
+ gate), with an inline Approve/Reject affordance on BOTH the Sales (`sales_gate_can_act`) and
Purchasing (`purchasing_gate_can_act`, added in SP-3) section headers for eligible PENDING
recipients, and the "Submit … for Approval" button is hidden once a non-rejected request exists.

**Per-PO sign-off (Phase 3 — replaces the SP-3 deal-level PO gate):** distinct from the QP
Purchasing **section** gate above, a "PO" is one `BuyPlanLine` (its own `po_number` via
`confirm_po`), and its approval is the **per-line trio** — `verify_po(action="approve")`
(line → VERIFIED), `verify_po(action="reject")` (send-back: line → AWAITING_PO, plan
untouched), and `resource_line` (cancel → fall-down/re-source). The old deal-level
`PURCHASE_ORDER` `ApprovalRequest` (opened on buy-plan approval when the plan total cleared
`po_auto_approve_threshold`, approve → ACTIVE→INBOUND, completed via
`receive_buy_plan`/"Mark Received") is **retired**: `_maybe_open_po_gate`,
`_run_po_approve_side_effects`, `receive_buy_plan`, the `/receive` route, the INBOUND
lifecycle step, and the `po_auto_approve_threshold` setting are gone (the
`BuyPlanStatus.INBOUND` enum member remains for historical rows). `verify_po` now enforces
the approver's `purchase_order_approval_limit` against **this line's** amount
(`_line_amount = unit_cost × quantity`; NULL = unlimited) and writes a durable
`PO_LINE_VERIFIED`/`PO_LINE_REJECTED` `ActivityLog` row via `_log_po_line_activity`.
Templates hide Verify/Reject per line with the Jinja global `can_verify_po_line(user, line)`
(`app/dependencies.py` — the same right + limit check the POST enforces). `_complete_plan`
refuses (warn + no-op) while `_has_open_po_gate(plan)` — any line PENDING_VERIFY — so
neither `check_completion` nor the stock-sale auto-complete job
(`inventory_jobs._job_stock_autocomplete`, which skips-and-continues on a refused plan) can
complete past an undecided PO. The stall detectors are per-line too:
`plan_needs_approver_reason` returns `"purchase_order"` when any PENDING_VERIFY line's
amount has no eligible approver (`has_eligible_approver(PURCHASE_ORDER, _line_amount)`) —
the predicate behind the workspace BP-tab stall warnings.
`routing._eligible_approvers`'s `PURCHASE_ORDER` branch stays (it powers those per-line
checks); the workspace Purchase Orders tab's history feed is decision-log-backed (no new
gate rows are ever created).

**QP native sections (QP Phase C2b):** the QP detail (`qp/detail.html`) `{% include %}`s four
section partials — `qp/_section_sales.html`, `_section_purchasing.html`, `_section_serial.html`,
`_section_fru.html` — replacing the Phase-2 placeholders.
- *Sales / Purchasing fields:* each is one `<form>` that `hx-patch`es `/v2/qp/{id}/sales` (or
  `/purchasing`) `hx-trigger="change"` and swaps the section partial into itself. The router
  writes only the whitelisted `_SALES_FIELDS`/`_PURCHASING_FIELDS` (so a stray form key can't set
  an arbitrary column), coercing Y/N→tri-state Boolean, qty→int, else stripped string|None. A PATCH
  is a no-op once the section is approved (read-only). The grid is read-only while a request is
  `requested` or the section is approved.
- *Completeness gate:* `validate_section(qp, gate_type)` → `_validate_sales_section`/
  `_validate_purchasing_section` (required: the SO#/PO# + condition + product commodity + testing-
  required, plus quantity for Sales). `submit_section` now calls it FIRST and raises
  `IncompleteQPError` (no gate opened) when incomplete; the router re-renders with server-driven
  `section_errors` that disable the submit button. `_on_section_approved` now stamps
  `sales_section_approved_at`/`purchasing_section_approved_at` on approve (cleared on reject).
- *Serial CRUD:* `POST /v2/qp/{id}/serial` adds a `QpSerialEntry` (submitted_by = acting user),
  `DELETE …/serial/{entry_id}` removes it (404 if it belongs to another QP). Cascade with the QP.
- *FRU pin/unpin:* `POST /v2/qp/{id}/fru` resolves `fru_norm` via `normalize_mpn_key`, checks the
  `(qp_id, fru_norm)` unique constraint (re-pin = no-op), and the section live-joins `FruLink` by
  `fru_norm` (`_fru_rows`) to show the related model/carrier/series edges; `DELETE …/fru/{lookup_id}`
  unpins. All C2b mutation endpoints keep the `_require_qp_access` ownership scope (404 not 403).

**Leaving PENDING outside `decide()` cancels the open engine request (no orphan, no
resurrection):** a PENDING plan carries a live `REQUESTED` `BUY_PLAN` `ApprovalRequest`, so
any transition that takes the plan out of PENDING *without going through `decide()`* must
close that request or it would orphan a row in the approvals queue/badge — and, worse, let
an approver pull the stale request and resurrect the plan. `cancel_buy_plan` and the
`verify_so` **HALT** branch therefore call
`_cancel_open_engine_requests_for_plan(plan, user, db)` (the stale-cancel loop factored out
of `_open_engine_request_for_plan`) at/before the transition. `cancel_buy_plan` calls it
**unconditionally** (the helper matches by `subject_type=buy_plan` + `REQUESTED` and is a
no-op when none are open — it would also sweep any historical pre-Phase-3 deal-level
`PURCHASE_ORDER` row); the
`verify_so` HALT branch still guards on PENDING. That helper cancels each open request on behalf of the request's OWN
`requested_by`/`owner` (the original submitter), so the `events.cancel` authz
(requester/owner OR manager/admin) is satisfied for EVERY caller — including a `verify_so`
HALT driven by an ops-group member who is neither the submitter nor a manager/admin. As a
second line of defense, `_run_approve_side_effects`/`_run_reject_side_effects` re-check
`plan.status == PENDING` at entry: deciding a stale request whose plan already left PENDING
raises `ValueError` → the router returns a clean 400 (via `get_db`'s rollback) instead of
silently reactivating a cancelled/halted plan.

**Prepayment teardown sweep (money-safety, finding #2):** a plan that dies must not leave a
pending wire an approver could still authorise. `_cancel_open_prepayment_requests_for_plan(plan_id,
db, reason)` joins `ApprovalRequest → Prepayment` on `subject_id` and voids every
`REQUESTED` `PREPAYMENT` request whose `Prepayment.buy_plan_id == plan_id` (sets
`status=CANCELLED` + `resolved_at` + `resolution_note=reason`, flushes, returns the count).
It fires from `cancel_buy_plan`, `halt_plan`, `_complete_plan` (so BOTH completion paths —
`check_completion` and the stock-sale job — sweep) and `resource_line`. **Plan-scoped**, so a
multi-line re-source (`line_id` + `also_line_ids`) voids every affected line's pending
prepayment in one sweep — a re-sourced line means its PO/vendor changed, so the plan's pending
wire is stale regardless. **Idempotent** (`REQUESTED`-only): an already-`APPROVED`
prepayment (about to be wired) is deliberately left alone — clawing back an approved wire needs
the follow-up VOID lifecycle state.

The buy-plans ACTION badge (`alerts/sources/buyplan.py`, branch 2 — manager approval) counts
ONLY pending plans with **no open `BUY_PLAN` `ApprovalRequest`**, so a post-C1 plan surfaces
on the **Approvals** badge alone (no double-count) while a pre-C1 transition-window plan (no
engine request) still surfaces on the buy-plans badge and never goes invisible.

The **read-only buy-plan bridge is RETIRED** (C1). `list_requests` (`routers/approvals.py`)
is engine-only: a buy plan surfaces as a native `ApprovalRequest` (`gate_type=buy_plan`,
`subject_type=buy_plan`). `_serialize_request(r)` is the single 11-field engine-item
projection shared by `list_requests` + `get_request` (carrying `subject_type`/`subject_id`).

**Per-gate approvals — engine queue only (pinned stage-tab sections RETIRED in F-2).** The
per-gate "Pending approvals (N)" sections that used to render inside each lifecycle stage tab
were removed with those tabs in Phase F-2 (`approvals/_pending_section.html` +
`approvals/_macros.html` + the `_TAB_APPROVE_ATTR` map are deleted; the standalone combined
four-sub-tab `approvals/_queue.html` lens was already retired before that).
`services/approvals/queue.build_queue_view(db, user, tab=<gate>)` remains defined (TAB_GATE /
per-gate projection) but is no longer wired to a human surface; the only human-facing approvals
queue is now the engine API (`GET /v2/approvals/requests` + the standalone Approvals page).
Subjects resolve by `subject_type` (buy_plan→plan detail, quality_plan→`/v2/qp/{id}`,
prepayment→vendor + payment method). **Visibility is ownership-scoped** (parity with
requisition-derived data): unrestricted roles (buyer/manager/admin) see every request, but
`RESTRICTED_ROLES` (SALES/TRADER) see only requests they submitted (`requested_by_id`), own
(`owner_id`), or must personally decide (a PENDING `ApprovalStepRecipient` row) — enforced on
BOTH `list_requests` (`_restricted_visibility_clause`) and `get_request`
(`_can_view_request`; 404-not-403 so existence isn't leaked, mirroring
`require_requisition_access`). approve/reject still act only for an eligible PENDING recipient
(`decide()`). The legacy
`GET /v2/approvals/queue` redirect is long gone; any stale `/v2/buy-plans*` URL now
**308s** to `/v2/approvals?tab=buy-plans` (the hub retired post-parity).
`ApprovalRequestActionSource` (`AlertKind.APPROVAL_ACTION`) is registered under the
**`buy-plans`** tab, so its "awaiting me" count merges onto the Approvals nav badge.

**Sales Order origination from RFQ offers (SP-2).** A buy plan no longer requires a
customer quote: `buy_plans_v3.quote_id` is **nullable** (migration 163). The workspace
SO/BP lists carry the **"New sales order"** button
→ `GET /v2/partials/buy-plans/sales-orders/new` (`sales_order_new`,
`partials/approvals/_sales_order_new.html` — SELF-HOSTED in `#so-origination`, loaded into
`#main-content`), a dual-mode surface: first a **requisition
picker** scoped to the viewer's accessible requisitions that have ≥1 ACTIVE offer, then a
per-requirement **offer + sell-price** form (seeded via `get_builder_data` +
`apply_smart_defaults`). Submit `POST /v2/partials/approvals/sales-orders/create` runs
`require_requisition_access` then `buyplan_builder.create_sales_order_from_offers(req_id,
selections, sell_prices, db, user)` — a sibling of `build_buy_plan` (which stays
quote-required) sharing the `_assemble_buy_plan` core — producing a **DRAFT, quote-less**
buy plan and swapping its detail view in with `HX-Push-Url`. A requisition-keyed dup guard
(`find_open_sales_order` → raises `DuplicateSalesOrderError`, a `ValueError` carrying
`existing_plan_id`) stops a second open SO for the same requisition; the route catches it
specifically (curated 400 for other `ValueError`s). Per-status filtering now lives in the
workspace lists' status badges + Live/Closed filter (the Pipeline lens and `deals_board`
retired with the hub). The BUY_PLAN approval gate (`TAB_GATE["sales_orders"] =
ApprovalGateType.BUY_PLAN`, `can_approve_buy_plans`) still exists in the engine — there is
no separate "sales order" gate. The QP Sales-section gate (the QualityPlan, renamed
`SALES_ORDER`→`QP_SALES`, column `can_approve_qp_sales`, migration 164) is a distinct,
QP-scoped approval and **leaves** the lifecycle tabs; the canonical SO# is
`buy_plans_v3.sales_order_number` (the QP's editable `sales_so_number` input was removed and
the column dropped).

---

## Approvals Workspace — flows (Phases 0–1)

**One page, four lenses.** `/v2/approvals` → shell (4 pills, per-viewer badges) →
lazy `#ap-hub-body` ← `render_tab_body(tab)` → `_workspace_split.html` (split view) →
left `#aw-list` ← `GET /v2/partials/approvals/{tab}/list?q&scope&show_closed`
(re-fetches on `awListRefresh from:body`) → row click dispatches `aw-select` → right
`#aw-pane` ← the tab's pane route. The list's oldest Needs-your-approval row
dispatches `aw-default` once (applied only when nothing is selected) so opening a tab
lands the approver on a decision. Legacy 3-tab keys alias throughout.

**Decide loops (engine untouched).** Every pane action posts an EXISTING route with
`origin=approvals_workspace`; the handler re-renders the pane + `HX-Trigger:
awListRefresh`:
- SO/BP approve/reject → `POST /v2/partials/buy-plans/{id}/approve` (engine
  `decide()` inside) → `render_plan_pane`. Reject requires the note-to-fixer
  (engine-enforced comment).
- Confirm PO → `POST .../lines/{line}/confirm-po` → `confirm_po(payment_method=...)`
  + `apply_qp_purchasing` (QP-purchasing incl. AS9120B onto the (plan, vendor) QP
  row) + `log_field_edits` for the QP diff → `render_po_pane`.
- Verify / send back / re-source / claim → the existing verify-po / resource / claim
  routes → `render_po_pane`. `GET /po/{line}/sent-check` surfaces `verify_po_sent`
  detection **display-only** (never auto-verifies).
- Prepay decide → `POST /v2/partials/approvals/prepay-requests/{id}/decide` →
  `render_prepayment_pane`. Approve button reads **"OK to pay — {method}"**; the
  method dropdown on the approval card posts
  `POST /v2/partials/approvals/prepayments/{id}/method` (approver-only,
  REQUESTED-only, `ensure_not_stale` → non-destructive 409, audited via
  `log_field_edits(prepayment_id=...)`).

**Field-audit choke point (Phase 0.2, wired from Phase 1 onward).** Edit paths compute
`diff_fields(obj, updates)` and write ONE `FIELD_EDIT` ActivityLog row per save via
`log_field_edits` (`details={"edits": [...]}`, keyed by `buy_plan_id` +
`buy_plan_line_id`/`prepayment_id`); `edits_since` backs the Phase-2 approve-time
change summary; `manager_edited_line_ids` backs the Phase-3 kanban marker.

**Stale-edit guard (Phase 0.3).** Forms embed `stale_token(obj)` (Jinja global) as
`expected_updated_at`; handlers call `ensure_not_stale` and turn `StaleEditError`
into `stale_conflict_response()` (409, `HX-Reswap: none`, "This changed — refresh."
toast). Empty token skips (legacy forms never false-positive).

**Order type + lite path (Phase 1.3).** The SO picker
(`/v2/partials/buy-plans/sales-orders/new`) carries an order-type select: sourcing
types (New/Revision) require offers and build via
`create_sales_order_from_offers(order_type=...)`; non-sourcing types (Stock Sale /
Testing Service / Comps) list ANY open requisition and create via
`create_lite_sales_order` — a zero-line DRAFT plan that submits/approves/tracks
normally but generates **zero buyer tasks** and **never auto-completes**
(`check_completion`'s empty-lines early return). `_is_stock_sale` now lets an
explicit STOCK_SALE order type win over the vendor-name inference so submit can't
clobber the lite flag. The SO pane hides lines/kanban for non-sourcing types.

**COD guard (Phase 1.5).** `routers/prepayments.py` blocks a prepayment request on a
COD line (and any non-`PREPAYMENT_METHODS` method) with a friendly 400 BEFORE
`create_prepayment` — the service and engine stay untouched; the request modal's
method list derives from `PREPAYMENT_METHODS` (wire/PayPal/CC/ACH — COD never
renders).

**Editing layer (Phase 2).** Every edit route carries the stale guard
(narrowest-object `expected_updated_at`; mismatch → non-destructive 409) and lands
ONE `FIELD_EDIT` row per save: single-line edits log at service depth in
`edit_buy_plan_line`/`add`/`remove`/`set_sales_order_number`; the bulk save batches
all touched lines into one row (per-edit `line_id` in the details JSON); confirm-po
merges line PO fields + QP-purchasing into one row. QP-sales answers save via
`POST /v2/partials/approvals/plan/{id}/qp-sales` → `apply_qp_sales` (draft →
owner/manager; pending → MANAGER only). **Approve is two-part** (spec §7):
`handoff=proceed` → approve + the submitter's in-app change summary
(`edits_since(plan, submitted_at)` → "was X → now Y", skipped when empty);
`handoff=send_back` → the existing reject→draft with the summary attached (blank
note auto-fills; manager edits persist). Every reject/send-back note-to-the-fixer
ALSO lands as a decision-tagged NOTE row on the item's thread + a `write_in_app`
notification to the fixer. **Manager edit-anything at verify**: a manager/admin may
edit qty / unit cost / PO# / est ship on a PENDING_VERIFY line via
`/lines/{id}/edit` (vendor stays offer-swap-only; bulk stays strict); the pane
shows the Acctivate warning + "Edited by manager" marker. **Notes & attachments on
every item** (never status-locked): `POST /v2/partials/approvals/notes` /
`.../attachments` (shared `store_and_attach` on `BuyPlanAttachment`,
`validate_subject`, ATTACH_ADDED/REMOVED activity; delete = uploader or manager);
`_notes_thread.html` renders threads + files with decision-tagged rows in all three
panes. **Lifecycle controls**: manager-only halt/resume/cancel/reset on the SO pane
via the existing POSTs (`origin=approvals_workspace`); `plan_needs_approver_reason`
stall warnings on BP-tab rows and the pane.

**PO kanban (Phase 3, spec §6).** `render_plan_pane` builds `kanban` via
`services/kanban_lanes.build_kanban(db, plan)` on ACTIVE/INBOUND **sourcing** orders
only (lite plans and draft/pending/closed plans get no board); `_pane_kanban.html`
renders it inside `_pane_sales_order.html`. Lanes are **display-only, never
persisted**, computed per line by `kanban_lane(line_status, prepay_status,
payment_method, received)` with this exact precedence:

1. `cancelled` → hidden (no column);
2. `resourcing` → **Re-sourcing** (the claim pool — lane renders only when populated);
3. received (`received_at` stamped) → **Received** — paid-and-received is NOT a risk;
4. prepayment **PAID** and `payment_method != cod` → **Paid · awaiting delivery** (the
   RISK lane — money out before goods on any advance rail, **outranks verified**; COD
   never enters);
5. `verified` → **Approved**; 6. `pending_verify` → **Pending approval**;
7. else (`awaiting_po` + `issue`) → **Awaiting PO** (issue keeps a badge, not a column).

Card data is batch-resolved (no N+1): prepay badge state via
`prepayment_state_for_lines` (read-only), amount/payee/`paid_at` off the
most-progressed live Prepayment row, `manager_edited_line_ids` for the Edited marker,
`note_counts` + `BuyPlanAttachment` group-counts, plan-level QP `partial_ship`. Risk
cards show **amount + payee** on their face and age green → amber (3d) → red (7d)
keyed on `paid_at` (shared `age_chip` thresholds). **No drag** — cards move only by
the real actions; tapping a card `hx-get`s that line's PO pane into `#aw-pane`
(explicit `hx-target`).

**Mark received (Phase 3).** TRIO's "OPS Received (Y/N)" — no automated receiving
event exists, so `mark_line_received(plan_id, line_id, user, db)`
(`buyplan_workflow/buyplan_po.py`, ADDITIVE) backs the Received column: actor gate =
the line's buyer or a manager/admin (service-side); state gate = VERIFIED **or** the
paid-risk state (a PAID prepayment — goods can land before the verify sign-off);
idempotent (an already-received line no-ops); stamps `received_at`/`received_by_id` +
ONE `LINE_RECEIVED` ActivityLog row keyed to the line; **never** touches line.status
or the plan's completion machinery (completion still runs only through `verify_po`).
Route: `POST /v2/partials/buy-plans/{plan}/lines/{line}/receive` (`require_user`;
PermissionError→403, ValueError→400); `origin=approvals_workspace` re-renders the
SO/BP pane when a `lens` rides along (the kanban card's button — the board repaints
in place) else the PO-line pane, both with `awListRefresh`.
