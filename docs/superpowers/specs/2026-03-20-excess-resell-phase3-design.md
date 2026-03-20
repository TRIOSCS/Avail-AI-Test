# Excess Resell Tab — Phase 3: Bid Collection, Demand Matching & UX Improvements

## Problem

The Excess tab is currently a basic CRUD screen for uploading customer surplus inventory. The resell team (Derrick, Mikkel, Jenny) needs it to be a full workflow tool: find buyers, track bids, pick winners, and automatically surface excess parts as supply for open requisitions.

## Workflow

1. **Customer** emails excess parts list to a salesperson/trader
2. Salesperson uploads the list to the **Excess tab**
3. **On import**, system auto-matches parts against:
   - **Active requirements** → creates an Offer on the RFQ page (excess as supply source)
   - **Archived deals** → creates a ProactiveMatch entry
4. **Resell team** finds buyers via email (Graph API), broker boards, and CRM history
5. **Bids** come back — team records them, compares by price, picks a winner
6. Lifecycle: draft → active → bidding → closed

## Features

### 1. Import Preview (Two-Step Upload)

**Current**: Upload file → immediately imports all rows.

**New**: Upload file → preview step → confirm import.

**Preview step shows:**
- Summary bar: "23 valid rows, 2 problems" with green/red badges
- Compact table of first ~10 parsed rows with mapped columns
- Problem rows highlighted in red with inline error message (e.g. "Row 7: missing part number")
- Column mapping pills above table (e.g. `Col A → Part Number`, `Col C → Qty`). Clicking a pill opens a dropdown to re-map if auto-detect got it wrong.
- Two actions: "Import 23 rows" (green) and "Cancel" (gray)

**Column auto-detection** reuses existing `parse_tabular_file()` header aliases (mpn, pn, qty, unit_price, mfr, dc, etc.). The preview just makes the mapping visible and editable.

**Implementation:**
- New endpoint: `POST /api/excess-lists/{id}/preview-import` — parses file, returns JSON with mapped rows, errors, and detected column mapping
- New template: `excess/import_preview.html` — renders the preview table and mapping controls
- On confirm: `POST /api/excess-lists/{id}/confirm-import` (new endpoint) accepts the validated rows as JSON, not a file re-upload. The existing `/import` endpoint stays for direct file upload (backward compat). The new endpoint receives `{rows: [...], column_mapping: {...}}` from the preview step.

### 2. Demand Matching (On Import)

When line items are imported (or added manually), immediately match each part against:

**Active requirements:**
- Query `requirements` table for matching `normalized_mpn` where requisition status is active/open/sourcing
- For each match, create an `Offer` record:
  - `vendor_name`: Company name from the ExcessList (the customer selling)
  - `mpn`: Original part number as entered (not normalized)
  - `normalized_mpn`: normalize_mpn_key() result
  - `source`: "excess" (new source type)
  - `excess_line_item_id`: FK back to the excess line item (new nullable column on Offer)
  - `unit_price`: The asking price from the excess line item
  - `qty_available`: Quantity from excess line item

**Archived deals:**
- Query archived requisitions' requirements for matching `normalized_mpn`
- For each match, create a `ProactiveMatch` record linking to the excess-generated Offer
- `ProactiveMatch` requires non-nullable `customer_site_id` and `salesperson_id`:
  - `salesperson_id` = the requirement's requisition owner (the buyer who had the original demand)
  - `customer_site_id` = resolve from the archived requisition's company → default site. If no site exists, skip ProactiveMatch creation for that match (log a warning). Do NOT use the excess list's customer_site_id — that's the seller, not the buyer.

**Normalization display rule (cross-cutting):**
Wherever a normalized match is displayed (Offers tab, Proactive tab, Excess detail), always show:
- The **original part number** as entered by the user
- The **normalized form** that triggered the match (smaller, gray text below or beside)
- This lets users catch false positives (e.g. `LM358N` matching `LM358NA`)

**Implementation:**
- New service function: `match_excess_demand(db, excess_list_id)` in `excess_service.py`
- Uses `normalize_mpn_key()` from `app/utils/normalization.py` for matching
- Called automatically after import completes (sync, in same request)
- New nullable column on Offer: `excess_line_item_id` (FK to excess_line_items, SET NULL on delete)
- New column on Offer: `source` gets new value "excess" (alongside existing "manual", "email_parsed", etc.)
- On the excess detail page, show a "Demand Matches" count badge per line item linking to the matched requirement

### 3. Bid Solicitation (Email to Buyers)

Mirror the existing RFQ outbound flow, but for selling instead of buying.

**UI on excess detail page:**
- "Send Bid Request" button on each line item (or bulk-select multiple items)
- Opens a compose panel (similar to RFQ compose):
  - Search/select contacts from CRM (companies, vendor cards)
  - Pre-filled email template: "We have {qty} x {mpn} available. Please submit your bid by {date}."
  - AI draft option (reuse existing AI draft infrastructure)
  - Send via Graph API

**Backend:**
- Reuse `email_service.send_batch_rfq()` pattern but for bid solicitations
- New function: `send_bid_solicitations(db, token, user_id, excess_list_id, line_item_ids, contact_groups)`
- Updates `BidSolicitation` records (model already exists) with status tracking: pending → sent → responded → expired
- Tag emails with `[AVAIL-BID-{excess_list_id}]` for inbox parsing
- **Auto-parsing of bid responses is OUT OF SCOPE for SP3.** The existing `response_parser.py` routes to Offer/VendorResponse, not Bid records. SP3 delivers manual bid recording only. Auto-parsing bid responses from inbox is a future enhancement that requires extending response_parser.py with a new classification branch.

### 4. Bid Recording & Comparison

**Recording bids:**
- "Record Bid" button on each line item → modal form:
  - Bidder (company or vendor card from CRM)
  - Price per unit, quantity wanted, lead time (days)
  - Source: manual / email_parsed / phone
  - Notes
- Uses existing `Bid` model (already has all these fields)

**Bid list on line item:**
- Expanding a line item row shows its bids, sorted by unit_price ascending (best price first)
- Each bid row: bidder name, price, qty, lead time, status badge, accept/reject buttons
- "Accept" sets bid status to "accepted", sets line item status to "awarded", rejects all other bids
- Visual: accepted bid highlighted in green, rejected in gray

**No separate comparison view** — the sorted list IS the comparison. Best price is visually obvious at the top.

### 5. List View Enhancements

**Summary stats** (above the table):
- 4 stat cards: Total Lists, Total Line Items, Total Asking Value ($), Active Bids

**Sortable columns:**
- Click any column header to sort (toggle asc/desc)
- Use `hx-get` with `sort=field&dir=asc|desc` params
- Service layer accepts `sort_by` and `sort_dir` parameters

**Owner filter:**
- Dropdown next to status pills: "All Owners" / Derrick / Mikkel / Jenny / etc.
- Filters by `ExcessList.owner_id`

### 6. Detail View Enhancements

**Bulk delete:**
- Checkbox column on line items table
- When items selected, floating "Delete X items" bar appears at bottom
- Uses `DELETE /api/excess-lists/{id}/line-items/bulk` with list of IDs

**Note tooltips:**
- If a line item has notes, show a small note icon in the row
- Hover to see the full note in a tooltip (CSS `title` attribute or Alpine.js popover)
- No expandable rows — keeps the table compact

## Data Model Changes

### New columns

**Offer table:**
- `excess_line_item_id` — nullable FK to `excess_line_items.id`, SET NULL on delete
- `source` already exists as a string field; add "excess" as a valid value

**ExcessLineItem table:**
- `demand_match_count` — integer, default 0. Cached count of Offers created from this item.

### No new tables

All existing models (Offer, ProactiveMatch, Bid, BidSolicitation) are sufficient.

## API Endpoints (New)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/excess-lists/{id}/preview-import` | Parse file, return preview JSON |
| POST | `/api/excess-lists/{id}/confirm-import` | Import validated rows from preview (JSON) |
| POST | `/api/excess-lists/{id}/match-demand` | Trigger demand matching manually |
| POST | `/api/excess-lists/{id}/line-items/bulk-delete` | Delete multiple line items |
| POST | `/api/excess-lists/{id}/send-solicitations` | Send bid request emails |
| POST | `/api/excess-lists/{id}/line-items/{item_id}/bids` | Record a bid |
| PATCH | `/api/excess-lists/{id}/line-items/{item_id}/bids/{bid_id}` | Accept/reject a bid |
| GET | `/api/excess-lists/{id}/line-items/{item_id}/bids` | List bids for a line item |
| GET | `/api/excess-lists/{id}/stats` | Summary stats for list view |

## HTMX Partials (New)

| Path | Purpose |
|------|---------|
| `/v2/partials/excess/import-preview` | Import preview table with column mapping |
| `/v2/partials/excess/{id}/line-items/{item_id}/bids` | Bid list for a line item (expandable) |
| `/v2/partials/excess/{id}/solicitation-compose` | Bid solicitation email compose panel |
| `/v2/partials/excess/{id}/line-items/{item_id}/bid-form` | Record bid modal form |

## Normalization Display Rule (Cross-Cutting)

This rule applies everywhere in the app, not just excess:

When displaying a match that was found via normalization, always show:
1. **Original value** as entered (primary, full size)
2. **Normalized key** that triggered the match (secondary, smaller gray text)

Example in Offers tab:
```
LM358N/NOPB          ← original MPN from vendor/excess
matched as: lm358nnopb  ← normalized key (gray, smaller)
```

This lets users catch false positives where normalization strips meaningful suffixes.

**Implementation:** Add a `display_original_mpn` or equivalent field/logic wherever normalized matches are shown. The Offer model already stores both `mpn` (original) and `normalized_mpn` (key).

## Sub-Project Breakdown

This spec has 4 natural sub-projects that can be planned and implemented independently:

1. **SP1: Import Preview** — Two-step upload with column mapping, preview table, error highlighting
2. **SP2: Demand Matching** — Auto-match on import, create Offers/ProactiveMatches, normalization display rule, Offer.excess_line_item_id migration
3. **SP3: Bid Collection** — Solicitation emails via Graph API, bid recording, bid list/comparison, accept/reject
4. **SP4: List & Detail UX** — Summary stats, sortable columns, owner filter, bulk delete, note tooltips

**Dependency order:** SP1 → SP2 (matching runs after import) → SP3 (bids are the next workflow step) → SP4 (polish, can be done anytime)

SP4 has no dependencies on SP1-3 and can be done in parallel.
