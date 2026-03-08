# AI Intelligence Layer — Phase 4 Design

**Date**: 2026-03-08
**Status**: Approved
**Parent**: `docs/plans/2026-03-07-ai-intelligence-layer-design.md`

## Goal

Complete the AI Intelligence Layer with three features: extract durable facts from parsed emails, replace the daily Excel handoff with a role-aware morning briefing, and surface cross-customer part history inline wherever MPNs appear.

## Decisions

| Decision | Choice |
|----------|--------|
| Email fact extraction | Hook into existing `process_email_intelligence()` pipeline, add third step after classification + pricing |
| Briefing scope | Full Excel replacement — role-aware (buyer vs sales), delivered in-app + Teams DM |
| Resurfacing UX | Inline text hints in muted style — no modals, no separate pages, zero workflow disruption |
| AI model | Haiku for fact extraction (speed), no AI for resurfacing (pure SQL) |
| Storage | All facts/briefings stored as `knowledge_entry` records |
| New migrations | None — existing schema sufficient |
| Caching | Redis 1h TTL for MPN hints, 24h for briefings |

---

## Feature 1: Email Fact Extraction

**What:** When emails are parsed by the existing pipeline (response_parser or email_mining), AI extracts durable facts — lead times, MOQs, pricing notes, availability signals, vendor policies — and stores them as knowledge entries.

**Why:** The pricing parser captures structured data (MPN, qty, price) but misses unstructured intelligence like "Lead time is 16-20 weeks on this family", "This part is EOL, last time buy by June", "MOQ 2500, but we can do 1000 at 15% premium".

**Hook point:** `email_intelligence_service.py` → `process_email_intelligence()`. After classification + pricing extraction, add: `extract_durable_facts()`.

**AI prompt:** Claude Haiku, given the email body + any structured pricing already extracted, returns a JSON array of facts:
```json
[
  {"fact_type": "lead_time", "content": "16-20 weeks for LM317 family", "mpn": "LM317", "confidence": 0.9, "expiry_days": 180},
  {"fact_type": "eol_notice", "content": "Last time buy by June 2026", "mpn": "LM317", "confidence": 0.95, "expiry_days": null},
  {"fact_type": "moq_flexibility", "content": "MOQ 2500, can do 1000 at 15% premium", "mpn": null, "confidence": 0.85, "expiry_days": 90}
]
```

**Fact types:** lead_time, moq, moq_flexibility, eol_notice, availability, pricing_note, vendor_policy, warehouse_location, date_code, condition_note

**Storage:** Each fact → `knowledge_entry` with:
- `entry_type = "fact"`, `source = "email_parsed"`
- `mpn` set if fact references a specific part
- `vendor_card_id` from sender domain lookup (existing `_resolve_vendor()`)
- `confidence` from AI
- `expires_at` per fact type (price: 90d, lead time: 180d, EOL: none)

**Dedup guard:** Before creating, check for existing fact with same MPN + vendor + fact_type created in last 7 days with similar content. Skip if duplicate.

**Cost control:** Only run extraction on emails classified as offer, quote_reply, or stock_list (not general/ooo/spam). Skip if email body < 50 chars.

---

## Feature 2: Morning Briefing — The Excel Killer

**What:** A personalized, role-aware daily briefing that gives each user a complete picture of what needs attention. Replaces the daily Excel handoff between sales and purchasing.

**Delivery channels:**
1. **In-app dashboard card** — visible on login, always fresh
2. **Teams DM** — sent at user's configured `digest_hour` (field already exists on `teams_alert_config`)

### Buyer Briefing Sections

1. **Vendor emails needing action** — unreviewed offers/quotes received since last briefing
2. **Unanswered questions** — Q&A entries assigned to this user, still unresolved (with age: "6h ago")
3. **Stalling deals** — reqs they own with no new quotes in 7+ days (from `deal_risk.py`)
4. **Resurfaced parts** — new sightings/offers for MPNs they've been actively sourcing
5. **Price movement** — significant price changes on parts they're tracking

### Sales Briefing Sections

1. **Customer follow-ups needed** — customer emails/inquiries with no response
2. **New answers from buyers** — Q&A answers posted since last briefing
3. **Customers going quiet** — no engagement in 10+ days (from `activity_insights.py`)
4. **Deals at risk** — reqs where risk score went to red (from `deal_risk.py`)
5. **Quotes ready to send** — offers received but not forwarded to customer

### Implementation

- New service: `app/services/dashboard_briefing.py`
  - `generate_briefing(user_id, db)` → aggregates from existing services + direct queries
  - Returns structured `BriefingResponse` with sections and items
  - Each item: `{title, detail, entity_type, entity_id, priority, age_hours}`
- New endpoint: `GET /api/dashboard/briefing`
- Scheduler job in `knowledge_jobs.py`: pre-compute at 6 AM UTC, store as `knowledge_entry` (type=`ai_insight`, 24h expiry)
- Teams delivery: extend existing `_job_send_knowledge_digests()` to include briefing sections in the adaptive card
- Frontend: collapsible briefing card on dashboard, sections with item counts

### No-AI path

The briefing is **pure data aggregation** — no AI calls needed. All data comes from existing tables and services. This keeps it fast and free.

---

## Feature 3: Cross-Customer Resurfacing — Inline Hints

**What:** Wherever an MPN renders in the app, a subtle one-liner appears underneath if history exists. No clicks, no modals — the context just appears.

### Examples

| Surface | Hint |
|---------|------|
| Search results | "Last quoted $2.40 from Arrow, 3 months ago" |
| Req part list | "Also on Req #380 (Acme) — quoted $1.80" |
| Offer review | "⚠ 3x higher than last quote ($1.80, Arrow, Feb 14)" |
| RFQ compose | "Already have $2.15 quote from this vendor (Req #340)" |

### Implementation

- Extend `build_mpn_context()` in `knowledge_service.py` to include cross-req offer/quote history
- New function in `resurfacing_service.py`:
  - `get_mpn_hints(mpns: list[str], exclude_req_id: int | None, db) -> dict[str, str | None]`
  - Pure SQL, no AI. Sub-50ms for batch of 20 MPNs.
  - Returns `{"LM317": "Last quoted $2.40 from Arrow, 3 months ago", "TPS54331": null}`
- New batch endpoint: `GET /api/resurfacing/hints?mpns=X,Y,Z&exclude_req=123`
- Redis cache: 1h TTL per MPN, invalidated when new offers/quotes arrive (hook into offer creation)
- Frontend: after rendering part rows, call hints endpoint once per view load, append `.text-muted` hint under MPN

### Hint generation logic (pure SQL)

1. Query `offers` for this MPN: latest price, best price, vendor, date, customer
2. Query `requirements` for other open reqs containing this MPN (exclude current)
3. Query `knowledge_entries` for high-confidence facts (lead time, EOL)
4. Format into a single human-readable string, prioritized:
   - Price outlier warning (if current context has a price to compare) → highest priority
   - Cross-req alert ("Also on Req #X") → high
   - Last quoted price/vendor → medium
   - Lead time / EOL fact → low

### What we DON'T build

- No separate resurfacing page or panel
- No MPN history modal
- No "click to see more" — full context already lives in Phase 3 insight cards
