# Phase 4: World Class Sourcing Engine + Strengthen Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add vendor affinity matching and enhanced AI research to the sourcing engine, implement on-demand Claude-orchestrated enrichment, and strengthen 8x8 VoIP and M365 integrations.

**Architecture:** Four search layers fire simultaneously (Live Stock, Historical Sightings, Vendor Affinity, AI Research). Unified confidence scoring across all result types. On-demand enrichment replaces background waterfall.

**Tech Stack:** Python/FastAPI, Claude API (Haiku for classification, Sonnet for orchestration), asyncio, Redis

**Spec:** `docs/superpowers/specs/2026-03-13-mvp-strip-down-design.md` (Phase 4 + World Class Sourcing Engine sections)

---

## Current State

### Sourcing Engine (`app/search_service.py`)
- `search_requirement()` calls `_fetch_fresh()` which fans out to all connectors (Nexar, BrokerBin, eBay, DigiKey, Mouser, OEMSecrets, Sourcengine, Element14, AI Live Web) via `asyncio.gather()`
- Results are deduplicated, scored, and saved as `Sighting` records
- Historical vendor data is merged from `MaterialVendorHistory` records
- Material cards are upserted with cross-reference tracking
- Redis search cache with 15-min TTL

### Scoring (`app/scoring.py`)
- `score_sighting_v2()` — 5-factor weighted scoring:
  - Trust 30% (vendor score or baseline 35 for unknowns)
  - Price 25% (ratio to median)
  - Quantity 20% (coverage of target qty)
  - Freshness 15% (decays 5%/day from age_hours)
  - Completeness 10% (fields present: price, qty, lead_time, condition)
- `classify_lead()` — "strong" / "moderate" / "weak" classification
- `explain_lead()` — one-line plain-English buyer explanation
- `is_weak_lead()` — noise filter (authorized/T1/T2 exempt)

### AI Live Web (`app/connectors/ai_live_web.py`)
- `AIWebSearchConnector` uses Claude `web_search` tool (max 6 uses)
- Quality gates: must have qty > 0, vendor_url, evidence_note, stock signal words, age <= 30 days
- Always fires as part of connector fan-out (no smart trigger)
- Returns results with `source_type="ai_live_web"`, confidence 2-3

### Enrichment (`app/enrichment_service.py`)
- Company enrichment: Apollo > Explorium > Clearbit > Gradient > AI (concurrent via `asyncio.gather`)
- Contact enrichment: 6 sources concurrent (Apollo, Hunter, Lusha, Explorium, RocketReach, AI)
- Serial waterfall merge — first non-empty value wins per field
- Background batch jobs in `app/jobs/enrichment_jobs.py`

### 8x8 VoIP (`app/services/eight_by_eight_service.py`)
- Fetches CDRs from 8x8 Analytics API
- Auth via API key + username/password
- External calls only, dedup by callId
- No reverse lookup (phone → Company/VendorCard)
- No CRM context linking

### M365/Outlook (`app/utils/graph_client.py`)
- `GraphClient` with retry + immutable IDs + delta query
- Used for inbox scanning, email mining, RFQ sending
- No outbound email tracking
- No email threading by In-Reply-To/References
- No attachment detection pipeline

---

## Task 1: Vendor Affinity Service

**File:** `app/services/vendor_affinity_service.py` (new)
**Tests:** `tests/test_vendor_affinity.py` (new)

### TDD Steps

- [ ] **1.1** Write test for Level 1: query `Sighting` and `MaterialVendorHistory` for vendors who have supplied parts from the same manufacturer as the target MPN's `MaterialCard.manufacturer`
  - Mock DB with 3 vendors who share manufacturer "Texas Instruments", query for MPN "LM358N"
  - Assert returns ranked list of vendor suggestions with `level=1`, `reason` text

- [ ] **1.2** Implement `find_affinity_vendors_l1(mpn: str, db: Session) -> list[dict]`
  - Query `MaterialCard` for the target MPN to get manufacturer
  - Query `Sighting` + `MaterialVendorHistory` for other MPNs from same manufacturer
  - Group by vendor, count distinct MPNs they've supplied
  - Return `[{vendor_name, vendor_id, mpn_count, manufacturer, level: 1, confidence: 0.0}]`
  - Sort by mpn_count descending, limit to top 20

- [ ] **1.3** Write test for Level 2: query for vendors with same product family using `EntityTag` (commodity tags from tagging system)
  - Mock DB with vendors tagged in same commodity ("Capacitors > MLCC")
  - Assert returns suggestions with `level=2`

- [ ] **1.4** Implement `find_affinity_vendors_l2(mpn: str, db: Session) -> list[dict]`
  - Query `MaterialCard` → `EntityTag` → `Tag` for the target MPN's commodity tag
  - Find other `MaterialCard` records with same commodity tag
  - Query `Sighting` for vendors who supplied those MPNs
  - Return with `level=2`, deduplicated against L1 results

- [ ] **1.5** Write test for Level 3: query for vendors in same platform/system category via Claude classification
  - Mock Claude response classifying MPN into category "Power Management ICs"
  - Assert returns suggestions with `level=3`

- [ ] **1.6** Implement `find_affinity_vendors_l3(mpn: str, manufacturer: str | None, db: Session) -> list[dict]`
  - Call Claude Haiku to classify MPN into a broad sourcing category
  - Query `Sighting` for vendors who have supplied MPNs in that category
  - Return with `level=3`

- [ ] **1.7** Write test for Claude confidence assignment — `score_affinity_matches()`
  - Mock Claude response that reviews match list and assigns confidence 30-75%
  - Assert confidence values are within range, reasoning is populated

- [ ] **1.8** Implement `score_affinity_matches(mpn: str, matches: list[dict]) -> list[dict]`
  - Send match list to Claude Haiku with MPN context
  - Claude assigns confidence 0.30-0.75 per match with reasoning
  - Prompt: "Given MPN {mpn}, rate each vendor match. L1 (same manufacturer) = 50-75%, L2 (same commodity) = 40-60%, L3 (same category) = 30-50%. Adjust based on recency and volume."
  - Parse response, clamp confidence to [0.30, 0.75]

- [ ] **1.9** Write test for top-level `find_vendor_affinity(mpn, db)` that combines all 3 levels
  - Assert deduplication across levels (L1 wins over L2/L3 for same vendor)
  - Assert final list sorted by confidence descending

- [ ] **1.10** Implement `find_vendor_affinity(mpn: str, db: Session) -> list[dict]`
  - Run L1, L2 in parallel via `asyncio.gather`
  - If combined results < 5, run L3
  - Score all matches via `score_affinity_matches()`
  - Deduplicate (L1 wins), sort by confidence
  - Return top 10: `[{vendor_name, vendor_id, confidence, level, reasoning, mpn_count, manufacturer}]`

---

## Task 2: Integrate Vendor Affinity into Search

**Files:** `app/search_service.py` (modify)
**Tests:** `tests/test_search_service.py` (add cases)

### TDD Steps

- [ ] **2.1** Write test: search results include vendor affinity suggestions with `source_type="vendor_affinity"`
  - Mock `find_vendor_affinity()` to return 3 suggestions
  - Assert they appear in `search_requirement()` results
  - Assert they have `is_affinity=True`, `confidence_pct`, `reasoning` fields

- [ ] **2.2** Add vendor affinity as a parallel search layer in `_fetch_fresh()`
  - Import `find_vendor_affinity` from `app.services.vendor_affinity_service`
  - Create async wrapper that calls `find_vendor_affinity()` for the primary MPN
  - Add to `asyncio.gather()` alongside connector tasks
  - Tag results with `source_type="vendor_affinity"`

- [ ] **2.3** Write test: affinity results are included in the combined result list with correct fields
  - Assert `source_badge`, `confidence_pct`, `reasoning` present

- [ ] **2.4** Merge affinity results into the main results list in `search_requirement()`
  - Convert affinity dicts to sighting-like dicts with `is_historical=False`, `is_affinity=True`
  - Include `source_badge="Vendor Match"`, `confidence_pct`, `reasoning`
  - Do not create `Sighting` DB records for affinity results (they are suggestions, not stock)

- [ ] **2.5** Write test: affinity results do not duplicate existing sightings
  - If a vendor already appears in live results, skip the affinity suggestion for that vendor

---

## Task 3: Smart Trigger for AI Research

**Files:** `app/connectors/ai_live_web.py` (modify), `app/search_service.py` (modify)
**Tests:** `tests/test_ai_live_web.py` (add cases), `tests/test_search_service.py` (add cases)

### TDD Steps

- [ ] **3.1** Write test for `should_trigger_ai_search()` — fires when <5 API results
  - Input: 3 results from connectors → returns True
  - Input: 10 results from connectors → returns False

- [ ] **3.2** Write test: fires when no results under target price
  - Input: 5 results all above target → returns True
  - Input: 5 results with 2 below target → returns False

- [ ] **3.3** Write test: fires when part flagged obsolete
  - Input: MaterialCard with `lifecycle_status="obsolete"` → returns True

- [ ] **3.4** Write test: fires when zero sightings in 6 months
  - Input: last sighting > 180 days ago → returns True

- [ ] **3.5** Write test: manual trigger always works regardless of conditions

- [ ] **3.6** Implement `should_trigger_ai_search()` in `app/search_service.py`
  ```python
  def should_trigger_ai_search(
      api_result_count: int,
      has_price_below_target: bool,
      is_obsolete: bool,
      months_since_last_sighting: float | None,
      manual_trigger: bool = False,
  ) -> bool:
  ```
  - Returns True if: manual_trigger, or api_result_count < 5, or not has_price_below_target, or is_obsolete, or months_since_last_sighting >= 6

- [ ] **3.7** Modify `_fetch_fresh()` to separate AI connector from main fan-out
  - Run API connectors first via `asyncio.gather()`
  - Evaluate `should_trigger_ai_search()` on results
  - If triggered, fire AI connector as a second pass
  - Log trigger reason for debugging

- [ ] **3.8** Write test: AI search skipped when rich results exist (saves API cost)
  - 15 results with prices below target → AI not called

---

## Task 4: Unified Confidence Scoring

**Files:** `app/scoring.py` (modify)
**Tests:** `tests/test_scoring.py` (add cases)

### TDD Steps

- [ ] **4.1** Write test for `score_unified()` — Live API results score 70-95%
  - Authorized distributor with price + qty → 95%
  - Unknown vendor, no price → 70%

- [ ] **4.2** Write test: Historical sighting scores 50-80%, decays over time
  - 1 month old → ~75%
  - 6 months old → ~50%
  - Repeated sightings boost score

- [ ] **4.3** Write test: Vendor affinity scores 30-75% (passthrough from Claude)

- [ ] **4.4** Write test: AI research scores 20-60% based on evidence quality

- [ ] **4.5** Write test: source badges assigned correctly
  - Live API → "Live Stock"
  - Historical → "Historical"
  - Vendor affinity → "Vendor Match"
  - AI web → "AI Found"

- [ ] **4.6** Write test: color coding based on confidence
  - `>= 75` → "green"
  - `50-74` → "amber"
  - `< 50` → "red"

- [ ] **4.7** Implement `score_unified()` in `app/scoring.py`
  ```python
  def score_unified(
      source_type: str,
      vendor_score: float | None = None,
      is_authorized: bool = False,
      unit_price: float | None = None,
      median_price: float | None = None,
      qty_available: int | None = None,
      target_qty: int | None = None,
      age_hours: float | None = None,
      has_price: bool = False,
      has_qty: bool = False,
      has_lead_time: bool = False,
      has_condition: bool = False,
      repeat_sighting_count: int = 0,
      claude_confidence: float | None = None,
  ) -> dict:
  ```
  - Returns: `{score: float, source_badge: str, confidence_pct: int, confidence_color: str, components: dict}`
  - Live API: delegates to `score_sighting_v2()`, maps to 70-95 range, badge "Live Stock"
  - Historical: base 80, decays 5% per month from `age_hours`, boosted +2% per repeat (max +10%), badge "Historical"
  - Vendor affinity: uses `claude_confidence * 100`, badge "Vendor Match"
  - AI research: uses `claude_confidence * 100` (capped at 60), badge "AI Found"

- [ ] **4.8** Implement `confidence_color(pct: int) -> str` helper
  - `>= 75` → "green", `>= 50` → "amber", else → "red"

- [ ] **4.9** Wire `score_unified()` into `search_requirement()` to replace per-type scoring
  - All result types go through unified scoring
  - Existing `score_sighting_v2()` still used internally for the Live API path

---

## Task 5: Search Results Presentation

**Files:** `app/search_service.py` (modify), `app/schemas/responses.py` (modify)
**Tests:** `tests/test_search_service.py` (add cases)

### TDD Steps

- [ ] **5.1** Write test: search response includes `source_badge`, `confidence_pct`, `confidence_color` on every result
  - Assert all results have these three fields

- [ ] **5.2** Write test: affinity and AI results include `reasoning` field
  - Assert reasoning is a non-empty string for vendor_affinity and ai_live_web results

- [ ] **5.3** Update `sighting_to_dict()` in `app/search_service.py` to include unified score fields
  - Add `source_badge`, `confidence_pct`, `confidence_color` from `score_unified()` output
  - Add `reasoning` (None for live/historical, populated for affinity/AI)

- [ ] **5.4** Update response schema in `app/schemas/responses.py` (if applicable)
  - Add optional fields: `source_badge: str`, `confidence_pct: int`, `confidence_color: str`, `reasoning: str | None`

- [ ] **5.5** Write test: results are sorted by `confidence_pct` descending as primary sort
  - Live stock at 90% appears before historical at 70%

- [ ] **5.6** Update sort logic in `search_requirement()` to use `confidence_pct` as primary sort key

---

## Task 6: On-Demand Enrichment Orchestrator

**File:** `app/services/enrichment_orchestrator.py` (new)
**Tests:** `tests/test_enrichment_orchestrator.py` (new)

### TDD Steps

- [ ] **6.1** Write test for `fire_all_sources()` — all sources called in parallel
  - Mock Apollo, Lusha, Hunter, Clearbit, RocketReach, Gradient
  - Assert all 6 called via `asyncio.gather()`
  - Assert partial failures don't crash (return_exceptions=True)

- [ ] **6.2** Implement `fire_all_sources(entity_type: str, identifier: str) -> dict[str, dict | None]`
  - entity_type: "company" | "vendor" | "contact"
  - For company: fires Apollo, Clearbit, Gradient, Explorium enrichment
  - For contact: fires Apollo, Lusha, Hunter, RocketReach
  - Returns `{source_name: result_dict | None}`

- [ ] **6.3** Write test for `claude_merge()` — Claude picks best data per field
  - Input: 3 sources with conflicting phone numbers
  - Mock Claude response picking the one with highest confidence
  - Assert merged result has `{field: value, confidence: 0.95, source: "apollo"}`

- [ ] **6.4** Implement `claude_merge(raw_results: dict[str, dict | None], entity_type: str) -> list[dict]`
  - Send all non-None results to Claude Sonnet
  - Prompt: "You are a data quality expert. For each field, pick the most reliable value from the sources provided. Assign confidence 0.0-1.0. Explain your reasoning briefly."
  - Parse response into `[{field, value, confidence, source, reasoning}]`

- [ ] **6.5** Write test for `apply_confident_data()` — only applies data >= 90% confidence
  - Input: merged data with phone at 95% and fax at 60%
  - Assert phone applied to entity, fax rejected
  - Assert summary includes both in applied/rejected lists

- [ ] **6.6** Implement `apply_confident_data(entity, merged: list[dict], db: Session, threshold: float = 0.90) -> dict`
  - For each merged field: if confidence >= threshold, set on entity
  - Returns `{applied: [{field, value, confidence, source}], rejected: [{field, value, confidence, source, reason}], sources_used: [str]}`
  - Commit DB changes

- [ ] **6.7** Write test for top-level `enrich_on_demand(entity_type, entity_id, db)` end-to-end
  - Assert: fires sources → merges → applies → returns summary

- [ ] **6.8** Implement `enrich_on_demand(entity_type: str, entity_id: int, db: Session) -> dict`
  - Load entity from DB
  - Get identifier (domain for company, email/name for contact)
  - Call `fire_all_sources()` → `claude_merge()` → `apply_confident_data()`
  - Return enrichment summary

---

## Task 7: Enrichment Endpoint and UI

**Files:** `app/routers/enrichment.py` (modify), `app/templates/index.html` (modify)
**Tests:** `tests/test_enrichment_router.py` (add cases)

### TDD Steps

- [ ] **7.1** Write test for `POST /api/enrich/{entity_type}/{entity_id}` endpoint
  - Mock `enrich_on_demand()` to return a summary
  - Assert 200 response with `{applied: [...], rejected: [...], sources_used: [...]}`

- [ ] **7.2** Write test: invalid entity_type returns 400
  - `POST /api/enrich/invalid/123` → 400

- [ ] **7.3** Write test: entity not found returns 404
  - `POST /api/enrich/company/999999` → 404

- [ ] **7.4** Implement `POST /api/enrich/{entity_type}/{entity_id}` in `app/routers/enrichment.py`
  - Validate entity_type in ("company", "vendor", "contact")
  - Load entity from DB, 404 if not found
  - Call `enrich_on_demand()` from `app.services.enrichment_orchestrator`
  - Return enrichment summary JSON

- [ ] **7.5** Add HTMX enrichment button to company/vendor/contact detail views
  - Button: `<button hx-post="/api/enrich/company/{id}" hx-indicator="#enrich-spinner" hx-swap="innerHTML" hx-target="#enrich-result">Enrich Now</button>`
  - Spinner: loading indicator while enrichment runs
  - Result: display applied/rejected fields with confidence badges

- [ ] **7.6** Add feature flag `ON_DEMAND_ENRICHMENT_ENABLED` in `app/config.py`
  - Default: True
  - When False, endpoint returns 503 "Feature disabled"

- [ ] **7.7** Gate background enrichment jobs behind inverse flag
  - In `app/jobs/enrichment_jobs.py`, check if on-demand is enabled
  - If enabled, reduce background job frequency (daily instead of every 2h)
  - Log: "Background enrichment reduced — on-demand enrichment is primary"

---

## Task 8: Strengthen 8x8 VoIP

**Files:** `app/services/eight_by_eight_service.py` (modify), `app/jobs/eight_by_eight_jobs.py` (modify)
**Tests:** `tests/test_eight_by_eight.py` (add cases)

### TDD Steps

- [ ] **8.1** Write test for `reverse_lookup_phone(phone: str, db: Session) -> dict | None`
  - Input: phone number matching a `SiteContact.phone` → returns `{entity_type: "contact", entity_id, company_name, contact_name}`
  - Input: phone matching a `Company.phone` → returns company match
  - Input: unknown phone → returns None

- [ ] **8.2** Implement `reverse_lookup_phone()` in `app/services/eight_by_eight_service.py`
  - Normalize phone (strip +1, spaces, dashes, parens)
  - Query `SiteContact` by phone/mobile fields
  - Query `Company` by phone field
  - Query `VendorCard` by phone field
  - Return first match with entity context

- [ ] **8.3** Write test for CDR → CRM linking in `process_cdrs()`
  - CDR with phone matching a known contact → `ActivityLog` created with `company_id`, `contact_id`
  - CDR with unknown phone → `ActivityLog` created with `external_phone` only

- [ ] **8.4** Modify CDR processing in `app/jobs/eight_by_eight_jobs.py`
  - After fetching CDRs, run `reverse_lookup_phone()` on each caller/callee
  - If match found, set `company_id` and `contact_name` on the `ActivityLog` entry
  - Link to open requisitions: if company has open reqs, add `requisition_id` to log

- [ ] **8.5** Write test for extension → user mapping
  - Map extension "1234" to user "john@trio.com"
  - CDR with extension 1234 → attributed to John

- [ ] **8.6** Implement extension mapping in `app/services/eight_by_eight_service.py`
  - `get_extension_map(token: str, settings) -> dict[str, str]` — calls 8x8 user list API
  - Cache mapping in Redis (1h TTL)
  - Use in CDR processing to attribute calls to internal users

- [ ] **8.7** Write test: call history appears in vendor/contact detail API response
  - `GET /api/vendor-contacts/{id}` includes `recent_calls: [{date, duration, direction, user}]`

- [ ] **8.8** Add call history to vendor/contact detail endpoints
  - Query `ActivityLog` where `activity_type="call"` and matching company/contact
  - Return last 10 calls with date, duration, direction, internal user name

---

## Task 9: Strengthen M365/Outlook

**Files:** `app/utils/graph_client.py` (modify), `app/services/email_threads.py` (modify), `app/jobs/email_jobs.py` (modify)
**Tests:** `tests/test_graph_client.py` (add cases), `tests/test_email_threads.py` (add cases)

### TDD Steps

- [ ] **9.1** Write test for outbound email tracking — `scan_sent_folder()`
  - Mock Graph API response with 3 sent emails
  - Assert each creates/updates a tracking record with `direction="outbound"`, subject, recipient, timestamp

- [ ] **9.2** Implement `scan_sent_folder()` in `app/jobs/email_jobs.py`
  - Use `GraphClient.delta_query()` on `/me/mailFolders/SentItems/messages/delta`
  - For each sent message:
    - Check if subject contains `[AVAIL-` tag → link to requisition
    - Create `ActivityLog` with `activity_type="email_sent"`, `direction="outbound"`
    - Store recipient email for contact matching

- [ ] **9.3** Write test for email threading — `group_by_thread()`
  - 3 emails with matching `In-Reply-To` / `References` headers → grouped into 1 thread
  - 2 emails with no relation → 2 separate threads

- [ ] **9.4** Implement `group_by_thread()` in `app/services/email_threads.py`
  - Extract `internetMessageId`, `In-Reply-To`, `References` from Graph API message headers
  - Build thread graph: message → parent via In-Reply-To, siblings via shared References
  - Return `[{thread_id, messages: [{id, subject, from, date, direction}], message_count}]`

- [ ] **9.5** Write test for attachment detection — `detect_attachments()`
  - Email with `.xlsx` attachment → flagged for mining pipeline
  - Email with `.pdf` attachment → flagged
  - Email with inline image only → not flagged

- [ ] **9.6** Implement attachment detection in email scanning
  - Check `message.hasAttachments` flag from Graph API
  - If True, fetch attachment metadata: `GET /messages/{id}/attachments?$select=name,contentType,size`
  - Flag emails with file attachments (exclude inline images by contentType)
  - Queue flagged emails for `attachment_parser` processing pipeline

- [ ] **9.7** Write test for Graph API 429 retry — exponential backoff
  - Mock 429 response with `Retry-After: 5` header
  - Assert client waits and retries
  - Assert gives up after MAX_RETRIES

- [ ] **9.8** Enhance retry logic in `app/utils/graph_client.py`
  - Current: 3 retries with exponential backoff (already exists)
  - Add: honor `Retry-After` header when present (use max of backoff and Retry-After)
  - Add: distinguish 429 (rate limit, always retry) from 503 (service unavailable, retry) from 401 (auth expired, don't retry)

- [ ] **9.9** Write test: sent folder scan integrated into scheduler
  - Assert `scan_sent_folders` job added to scheduler at 30min interval

- [ ] **9.10** Add `scan_sent_folders` job to `app/jobs/email_jobs.py`
  - Runs every 30 minutes alongside inbox scan
  - Processes all users with `email_scan_enabled=True`
  - Stores delta token per user for incremental scanning

---

## Task 10: Integration Testing + Verification

**Files:** `tests/test_integration_phase4.py` (new)
**Tests:** All tests pass, coverage maintained

### TDD Steps

- [ ] **10.1** Write end-to-end test: search MPN → results from all 4 layers
  - Mock all connectors, affinity service, AI search
  - Assert results contain items with source_badge in {"Live Stock", "Historical", "Vendor Match", "AI Found"}
  - Assert results sorted by confidence_pct descending
  - Assert each result has confidence_color

- [ ] **10.2** Write end-to-end test: enrich company → all sources → Claude merge → 90% gate
  - Mock all enrichment sources with conflicting data
  - Mock Claude merge response
  - Assert: high-confidence fields applied, low-confidence rejected
  - Assert: summary includes both lists with reasoning

- [ ] **10.3** Write end-to-end test: 8x8 CDR → linked to company → visible in contact detail
  - Create company + contact with phone
  - Process CDR matching that phone
  - Query contact detail → assert call appears in `recent_calls`

- [ ] **10.4** Write end-to-end test: send RFQ → track outbound → receive reply → thread grouped
  - Send RFQ via email_service (mock Graph API)
  - Scan sent folder → assert outbound tracked
  - Receive reply with In-Reply-To header → scan inbox
  - Assert both messages grouped in same thread

- [ ] **10.5** Run full test suite + coverage check
  ```bash
  TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v
  TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
  ```
  - Assert: all tests pass, coverage >= 97%

- [ ] **10.6** Build and deploy, verify clean startup
  ```bash
  cd /root/availai && docker compose up -d --build && sleep 5 && docker compose logs --tail=50 app
  ```
  - Assert: no errors in startup logs
  - Assert: health endpoint returns 200

- [ ] **10.7** Verify search works end-to-end in production
  - Search for a known MPN
  - Verify results include source badges and confidence colors
  - Verify affinity suggestions appear (if applicable)

---

## File Summary

### New Files
| File | Purpose |
|------|---------|
| `app/services/vendor_affinity_service.py` | 3-level vendor affinity matching with Claude scoring |
| `app/services/enrichment_orchestrator.py` | On-demand Claude-orchestrated multi-source enrichment |
| `tests/test_vendor_affinity.py` | Tests for vendor affinity service |
| `tests/test_enrichment_orchestrator.py` | Tests for enrichment orchestrator |
| `tests/test_integration_phase4.py` | End-to-end integration tests |

### Modified Files
| File | Changes |
|------|---------|
| `app/search_service.py` | Add vendor affinity layer, smart AI trigger, unified scoring integration |
| `app/scoring.py` | Add `score_unified()`, `confidence_color()` |
| `app/connectors/ai_live_web.py` | Smart trigger support (no longer always fires) |
| `app/routers/enrichment.py` | Add `POST /api/enrich/{entity_type}/{entity_id}` |
| `app/services/eight_by_eight_service.py` | Add `reverse_lookup_phone()`, extension mapping |
| `app/jobs/eight_by_eight_jobs.py` | CRM context linking for CDRs |
| `app/utils/graph_client.py` | Enhanced 429 retry with Retry-After header |
| `app/services/email_threads.py` | Thread grouping by In-Reply-To/References |
| `app/jobs/email_jobs.py` | Sent folder scanning, attachment detection |
| `app/config.py` | `ON_DEMAND_ENRICHMENT_ENABLED` flag |
| `app/schemas/responses.py` | Add `source_badge`, `confidence_pct`, `confidence_color`, `reasoning` fields |

### Dependencies Between Tasks
```
Task 1 (affinity service) ──→ Task 2 (integrate into search)
                                          ↓
Task 3 (smart trigger) ────────→ Task 4 (unified scoring) ──→ Task 5 (presentation)
Task 6 (enrichment orchestrator) ──→ Task 7 (endpoint + UI)
Task 8 (8x8 strengthen) — independent
Task 9 (M365 strengthen) — independent
Tasks 1-9 ──→ Task 10 (integration testing)
```

Tasks 1, 3, 6, 8, 9 can run in parallel. Task 2 depends on 1. Task 4 depends on 2+3. Task 5 depends on 4. Task 7 depends on 6. Task 10 depends on all.
