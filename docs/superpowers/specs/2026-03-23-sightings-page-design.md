# Sightings Page Design Spec

**Date:** 2026-03-23
**Status:** Draft
**Purpose:** Buyer-facing sourcing command page for managing requirements across all sales people

---

## Overview

The Sightings page is the buyer's homescreen — a cross-requisition view of all open requirements that need sourcing. Buyers use it to prioritize work, group parts by type, send batch inquiries to vendors, track vendor responses, and enter confirmed offers. All activity is visible to other buyers, sales people, and managers on both this page and the RFQ page's Activity tab.

**Core principle:** Reuse existing models, services, and endpoints aggressively. No new models. Thin new router that delegates to existing services.

---

## Navigation

- Add "Sightings" as the **second item** in the bottom nav, between Reqs and Search
- Adjust mobile nav CSS to accommodate 6 primary items (tighter spacing / smaller text)
- Route: `/v2/sightings`
- Register `"sightings"` in `v2_page()` in `htmx_views.py` for direct URL navigation
- Icon: Eye or binoculars

**Bottom nav order:** Reqs | **Sightings** | Search | Buy Plans | Vendors | Customers

---

## Data Model Changes

### No new model. Two columns added to `Requirement`:

| Column | Type | Purpose |
|--------|------|---------|
| `priority_score` | Float, nullable | AI-computed priority (0-100) for sort order |
| `assigned_buyer_id` | FK → User, nullable | Which buyer is working this requirement |

### Existing models reused as-is (no changes):
- `Requirement` — the primary entity displayed on the page
- `Requisition` — parent, provides customer/sales person context
- `Sighting` — individual vendor sightings per requirement
- `VendorSightingSummary` — pre-aggregated vendor-level rollups per requirement
- `VendorCard` — vendor master with engagement_score, response_rate, brand_tags, commodity_tags, is_blacklisted
- `Contact` — outbound RFQ email records with Graph message IDs
- `VendorResponse` — parsed vendor replies with classification
- `Offer` — with existing `pending_review` status and approve/reject endpoints
- `ActivityLog` — already has `requirement_id` column and relationship
- `SourcingLead` — already has buyer_owner_user_id, buyer_status, buyer_feedback_summary

### Single Alembic migration:
- Add `priority_score` and `assigned_buyer_id` to `requirements` table

---

## Page Layout

### Pattern: Split Panel (reuse `split_panel.html`)

Left panel: requirements table. Right panel: requirement detail with vendor breakdown and activity timeline. Follows the established parts workspace pattern.

### Left Panel — Requirements Table

**Top bar (single row):**
- Four stat pills: **Open** | **Contacted** | **Awaiting Response** | **Offers In** — counts derived from `sighting_status.py` aggregation
- Group-by dropdown: Flat | Brand | Manufacturer | Commodity
- Filter controls: status, sales person, staleness, assigned buyer ("My Items" / "All Items" toggle)

**Table (compact-table class):**

| Column | Source | Display |
|--------|--------|---------|
| Checkbox | — | Multi-select for batch actions |
| MPN | `Requirement.mpn` | JetBrains Mono, font-medium |
| Description | `Requirement.description` | Truncated, text-gray-500 |
| Qty | `Requirement.target_qty` | JetBrains Mono |
| Customer | `Requisition.customer_name` | Link to RFQ page |
| Sales | `Requisition.user.name` | — |
| Top Vendor | Best-scored `VendorSightingSummary.vendor_name` | — |
| Vendor Score | `VendorCard.engagement_score` | Inline number |
| Response Rate | `VendorCard.response_rate` | Inline percentage |
| Status | `sighting_status.py` derived | `status_badge()` macro |
| Priority | `Requirement.priority_score` | High/Med/Low indicator |
| Stale | Computed: last ActivityLog > N days | Amber dot when stale, hidden otherwise |

**Group-by behavior:** Server-side SQL aggregation. When grouped, rows collapse under group headers: `"Seagate — 4 parts"` with expand/collapse chevron and summary stats right-aligned.

**Pagination:** Paginated with `limit`/`offset` like existing patterns. Server-side sorting by priority_score (default), MPN, status, staleness.

### Right Panel — Requirement Detail

Appears when a row is selected in the left panel. Three sections:

**1. Part Header (fixed)**
- MPN, description, qty needed, target price
- Customer name + requisition ref (link to RFQ page)
- Sales person name
- Assigned buyer (editable inline)
- "Refresh Sightings" button

**2. Vendor Breakdown Table**
- All vendors with sightings for this requirement
- Columns: Vendor Name, Status (dot + tooltip), Qty Available, Best Price, Score, Response Rate, Phone (tel: link for click-to-call)
- Each vendor row has actions: Mark Unavailable, Enter Offer
- Pending-review offers shown with Approve/Reject buttons (calls existing `POST /api/offers/{id}/approve`)

**3. Activity Timeline**
- Extracted shared partial: `htmx/partials/shared/activity_timeline.html`
- Reused on both this page and the RFQ page Activity tab
- Queries `ActivityLog` filtered by `requirement_id`
- Displays: sighting created, RFQ sent, phone call made, vendor responded, buyer responded, offer entered, offer approved, part marked sold/unavailable
- Filled dot = human action, empty dot = system/automated
- Compact timestamps, newest first

### Action Bar (sticky bottom, appears on multi-select)
- Selected count | **Send to Vendors** | **Refresh Sightings** | **Mark Status** dropdown
- Uses existing `partsListSelection()` Alpine pattern

---

## Vendor Batching Workflow

Triggered when buyer selects requirements and clicks "Send to Vendors."

### Step 1: Vendor Selection Modal (`vendor_modal.html`)
- Uses existing global modal via `@open-modal` dispatch
- System suggests vendors based on `VendorCard.brand_tags`, `commodity_tags`, `engagement_score`, `response_rate`
- Excludes `is_blacklisted` vendors
- Vendors already sighted for selected requirements appear first
- Buyer can add/remove vendors, search for any vendor in the system
- Inline vendor metrics: name, response rate, engagement score, phone

### Step 2: Compose Email
- Single textarea — buyer writes in their own voice
- Parts table auto-populated below (MPN, qty, target price — read-only reference)
- "Clean Up" button calls `cleanup_rfq_email()` — AI polishes grammar, ensures part details referenced, preserves buyer's tone
- Buyer reviews cleaned version, can edit further or revert
- If multiple vendors selected: one compose, system sends personalized copies per vendor

### Step 3: Send
- Calls existing `email_service.send_batch_rfq()` via Microsoft Graph
- Creates `Contact` records per vendor (existing model)
- Creates `ActivityLog` entries per requirement per vendor (existing model, already has requirement_id)
- Status auto-updates to "Contacted" for each vendor-requirement pair via `sighting_status.py` derivation
- Requires `require_fresh_token` for Graph API access

---

## Status Lifecycle (Per Vendor Per Requirement)

All statuses derived by existing `sighting_status.py` — no manual updating except offer approval.

| Status | Trigger | Automation |
|--------|---------|------------|
| **Sighting** | `search_service` creates sighting records | Automatic on requirement save + manual refresh |
| **Contacted** | `Contact` record created (email sent) or 8x8 call logged | Automatic when email sent via Graph or call detected by 8x8 |
| **Vendor Responded** | `email_mining` detects reply from vendor domain; `VendorResponse` created by AI parser | Automatic — multi-part replies update all referenced requirements |
| **Offer In** | Buyer approves a `pending_review` Offer, or manually enters an offer | Manual buyer confirmation required |
| **Not Available** | AI parser classifies reply as `no_stock`, or buyer manually marks | Semi-automatic — auto from parser, manual fallback |
| **Blacklisted** | `VendorCard.is_blacklisted` flag | Existing vendor management |

---

## AI Features

### 1. Priority Scoring — `scoring.py`
New function `score_requirement_priority()` in existing `scoring.py`:
- Inputs: requisition urgency/due date, customer value, sighting count, time since creation, whether any vendors contacted
- Output: 0-100 score stored on `Requirement.priority_score`
- Runs when SourcingWorkItem-equivalent is needed (requirement save, periodic job)
- Uses existing `score_unified()` patterns, not a Claude API call — pure SQL/Python scoring

### 2. Vendor Suggestions — No AI, just smart queries
When buyer opens "Send to Vendors" modal:
- Query `VendorCard` by `brand_tags` and `commodity_tags` matching selected parts
- Rank by `engagement_score` and `response_rate`
- Exclude `is_blacklisted`
- Existing sighting vendors for those requirements appear first
- No Claude call needed — just database queries

### 3. Stale Detection — Date math, no AI
Computed at query time:
- Check last `ActivityLog` entry for each `requirement_id`
- If older than configurable threshold (e.g., 3 days), flag as stale
- Displayed as amber dot in table, invisible otherwise
- No stored field — derived each time

### 4. Email Cleanup — `email_service.py`
New function `cleanup_rfq_email()` in existing `email_service.py`:
- Alternatively: add `user_draft` parameter to existing `draft_rfq()` function
- When `user_draft` provided: AI cleans grammar/formatting, ensures all part details referenced, preserves buyer's tone
- When `user_draft` is None: existing behavior (AI generates from scratch) — kept as fallback
- Uses `claude_client` FAST model (Haiku) — lightweight text task

### 5. Auto-Follow-Up Drafts
When a requirement is stale and buyer clicks "Send to Vendors":
- Pre-fill compose textarea with a follow-up template instead of blank
- Based on existing `draft_rfq()` with vendor history context
- Buyer edits and sends as normal

---

## Cross-Page Integration

### RFQ Page Activity Tab
- Already queries `ActivityLog` by `requisition_id`
- Per-requirement filtering: add a requirement-level section that filters `ActivityLog` by `requirement_id` (column already exists)
- Uses the same shared `activity_timeline.html` partial

### Bidirectional Navigation
- Sightings page: customer name + requisition ref link → RFQ detail page
- RFQ Activity tab: "View in Sightings" link → Sightings page filtered to that requisition

### Cross-Buyer Visibility
- Default view: "All Items" — all open requirements across all buyers
- Toggle: "My Items" — filtered to `assigned_buyer_id = current_user`
- Buyer name shown on each row

### Phone Click-to-Call
- Vendor phone numbers as `tel:` links in detail panel vendor table
- 8x8 integration auto-detects calls and logs to ActivityLog
- No additional code needed — existing 8x8 polling handles it

---

## Router: `app/routers/sightings.py`

Thin view router. Delegates to existing services.

| Method | Path | Purpose | Delegates To |
|--------|------|---------|-------------|
| GET | `/v2/partials/sightings` | Table partial (paginated, group-by) | DB queries on Requirement + VendorSightingSummary |
| GET | `/v2/partials/sightings/{requirement_id}/detail` | Detail panel | DB queries + `sighting_status.py` + ActivityLog |
| POST | `/v2/partials/sightings/send-inquiry` | Compose + send batch | `email_service.send_batch_rfq()` |
| POST | `/v2/partials/sightings/{requirement_id}/refresh` | Re-run search pipeline | `search_service.search_requirement()` |
| POST | `/v2/partials/sightings/{requirement_id}/mark-unavailable` | Set sighting unavailable | `Sighting.is_unavailable = True` |

**Existing endpoints called directly from templates via hx-post (no duplication):**
- `POST /api/offers/{id}/approve` — approve pending offer
- `POST /api/offers/{id}/reject` — reject pending offer
- `PUT /v2/partials/offer-review/{id}/promote` — HTMX promote handler

**Also required:** Add `"sightings"` to `v2_page()` in `htmx_views.py` for direct URL navigation.

---

## Templates

| Template | Pattern | Purpose |
|----------|---------|---------|
| `htmx/partials/sightings/list.html` | Split panel layout | Main page content |
| `htmx/partials/sightings/table.html` | compact-table, HTMX partial | Requirements table with group-by |
| `htmx/partials/sightings/detail.html` | Panel content | Requirement detail + vendor breakdown |
| `htmx/partials/sightings/vendor_modal.html` | Global modal | Vendor selection + email compose |
| `htmx/partials/shared/activity_timeline.html` | Extracted from existing | Shared timeline (sightings + RFQ page) |

### Frontend Patterns to Follow
- `split_panel.html` — reusable split layout with Alpine `splitPanel()` component
- `compact-table` CSS class — JetBrains Mono for data, DM Sans for labels
- `_macros.html` → `status_badge()`, `btn_primary()`, `stat_card()`
- `source_badge.html` for sighting source indicators
- `partsListSelection()` Alpine pattern for multi-select
- `cell_edit.html`/`cell_display.html` for inline editing (assigned buyer, notes)
- `$store.toast` for notifications
- `@open-modal` dispatch for vendor modal
- Light theme, `#5B8FB8` brand color, existing badge/filter pill patterns
- `hx-push-url="true"` on filter changes for browser history
- `hx-include` pattern for carrying filter state on sort/pagination

---

## Existing Code Reused (No Modifications)

| Component | Location |
|-----------|----------|
| `sighting_status.py` | Vendor status derivation per requirement |
| `sighting_aggregation.py` | Vendor summary rebuilds |
| `email_service.send_batch_rfq()` | Sending via Microsoft Graph |
| `search_service.search_requirement()` | Refresh sightings pipeline (9 connectors) |
| `eight_by_eight_service` | Auto call logging + reverse phone lookup |
| `email_mining` + `ai_email_parser` | Auto vendor response detection + parsing |
| `score_unified()` in `scoring.py` | Sighting scoring factors |
| `VendorCard` model | Vendor master with metrics |
| `VendorSightingSummary` model | Pre-aggregated vendor rollups |
| `Sighting` model | Individual sighting records |
| `Contact` model | Outbound RFQ tracking |
| `VendorResponse` model | Parsed vendor replies |
| `Offer` model + approve/reject endpoints | Full offer lifecycle |
| `ActivityLog` model (with `requirement_id`) | Activity tracking |
| `SourcingLead` model | Buyer status/feedback fields |
| `split_panel.html` | Resizable split layout |
| `_macros.html` | Status badges, buttons, stat cards |
| `source_badge.html` | Source type indicators |
| `empty_state.html` | No-results state |
| `modal.html` | Global modal framework |
| `toast.html` | Toast notifications |
| `pagination` controls | Offset-based pagination |

---

## New Code Summary

| What | Where | Size |
|------|-------|------|
| 2 columns on Requirement | Migration | ~10 lines |
| `score_requirement_priority()` | `scoring.py` | ~30 lines |
| `cleanup_rfq_email()` (or `user_draft` param on `draft_rfq`) | `email_service.py` | ~40 lines |
| Sightings router (5 endpoints) | `app/routers/sightings.py` | ~200 lines |
| `"sightings"` case in `v2_page()` | `htmx_views.py` | ~5 lines |
| Nav item addition + CSS adjustment | `mobile_nav.html` | ~10 lines |
| Table partial | `sightings/table.html` | ~150 lines |
| List/split layout | `sightings/list.html` | ~80 lines |
| Detail panel | `sightings/detail.html` | ~150 lines |
| Vendor modal | `sightings/vendor_modal.html` | ~120 lines |
| Shared activity timeline (extracted) | `shared/activity_timeline.html` | ~60 lines (moved, not new) |
