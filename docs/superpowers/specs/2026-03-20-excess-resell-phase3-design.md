# Excess Resell Tab — Phase 3: Bid Collection, Demand Matching & UX Improvements

## Problem

The Excess tab is currently a basic CRUD screen for uploading customer surplus inventory. The resell team (Derrick, Mikkel, Jenny) needs it to be a full workflow tool: find buyers, track bids, pick winners, and automatically surface excess parts as supply for open requisitions.

## Workflow

1. **Customer** emails excess parts list to a salesperson/trader
2. Salesperson uploads the list to the **Excess tab**
3. **On import**, system auto-matches parts against **active requirements** → creates Offers on the RFQ page (excess as supply source)
4. **Resell team** finds buyers manually (email, phone, broker boards)
5. **Bids** come back — team records them in the app, compares by price, picks a winner
6. Lifecycle: draft → active → bidding → closed

## Features

### 1. Import Preview (Two-Step Upload)

**Current**: Upload file → immediately imports all rows.

**New**: Upload file → read-only preview step → confirm import.

**Preview step shows:**
- Summary bar: "23 valid rows, 2 problems" with green/red badges
- Static column mapping summary: `Part Number (Col A), Qty (Col C), Price (Col F)` — read-only, no re-mapping (auto-detect via `parse_tabular_file()` is sufficient; if wrong, user re-uploads a fixed file)
- Compact table of first ~10 parsed rows
- Problem rows highlighted in red with inline error message (e.g. "Row 7: missing part number")
- Two actions: "Import 23 rows" (green) and "Cancel" (gray)

**Implementation:**
- New endpoint: `POST /api/excess-lists/{id}/preview-import` — parses file, returns JSON with mapped rows, errors, and detected column mapping
- New template: `excess/import_preview.html` — renders the read-only preview table
- On confirm: `POST /api/excess-lists/{id}/confirm-import` — accepts validated rows as JSON (not a file re-upload). The existing `/import` endpoint stays for direct file upload (backward compat).

### 2. Demand Matching (On Import — Active Requirements Only)

When line items are imported (or added manually), immediately match each part against **active requirements only**.

**Matching logic:**
- Query `requirements` table for matching `normalized_mpn` where requisition status is active/open/sourcing
- Use `normalize_mpn_key()` from `app/utils/normalization.py` for matching
- For each match, create an `Offer` record:
  - `vendor_name`: Company name from the ExcessList (the customer selling)
  - `mpn`: Original part number as entered (not normalized)
  - `normalized_mpn`: normalize_mpn_key() result
  - `source`: "excess" (new source type — no schema change, source is already a string column)
  - `unit_price`: The asking price from the excess line item
  - `qty_available`: Quantity from excess line item

**No new FK on Offer.** Link excess offers via `source="excess"` + `normalized_mpn`. Query: `Offer.query.filter(source="excess", normalized_mpn=key)`. This avoids a migration.

**No ProactiveMatch creation** (deferred to Phase 4). Phase 3 matches active requirements only — the immediate value for the resell team.

**Normalization display rule:** On the excess detail page, when showing demand match counts per line item, display a tooltip with the matched requirement's original MPN alongside the excess item's original MPN so users can catch false positives. This is scoped to the excess detail only — cross-cutting normalization display is deferred to Phase 4.

**Implementation:**
- New service function: `match_excess_demand(db, excess_list_id)` in `excess_service.py`
- Called automatically after import completes (sync, in same request)
- `ExcessLineItem.demand_match_count` — integer, default 0. Cached count of Offers created from this item.
- On the excess detail page, show a "X matches" badge per line item linking to the matched requirement

### 3. Bid Recording & Comparison

**Recording bids:**
- "Record Bid" button on each line item → modal form:
  - Bidder (company or vendor card from CRM — searchable dropdown)
  - Price per unit, quantity wanted, lead time (days)
  - Source: manual / phone
  - Notes
- Uses existing `Bid` model (already has all these fields)

**Bid list per line item — modal, not expanding rows:**
- Line item row shows "X bids" link
- Clicking opens a **modal** with the bid list, sorted by unit_price ascending (best price first)
- Each bid row: bidder name, price, qty, lead time, status badge, accept/reject buttons
- "Accept" sets bid status to "accepted", sets line item status to "awarded", auto-rejects all other pending bids
- Visual: accepted bid highlighted in green, rejected in gray

**Why modal over expanding rows:** Expanding rows create nested tables that are hard to scan and break on mobile. Modal keeps the line items table compact and makes bid comparison clear.

**No email solicitation** (deferred to Phase 4). Resell team finds buyers manually and records bids in the app. Graph API email integration + auto-parsing of bid responses will be added together in Phase 4 when both halves are ready.

### 4. List & Detail UX Enhancements

**Sortable columns:**
- Click any column header to sort (toggle asc/desc)
- Use `hx-get` with `sort=field&dir=asc|desc` params
- Service layer accepts `sort_by` and `sort_dir` parameters

**Owner filter:**
- Dropdown next to status pills, populated from users with excess lists
- **Defaults to current user** — "Your Excess Lists" is the safe default
- Can switch to "All" or another team member
- Filters by `ExcessList.owner_id`

**Bulk delete:**
- Checkbox column on line items table
- Static action bar above table (not floating): "X items selected — Delete"
- Uses `POST /api/excess-lists/{id}/line-items/bulk-delete` with list of IDs

## Data Model Changes

### New columns

**ExcessLineItem table:**
- `demand_match_count` — integer, default 0. Cached count of Offers created from this item.

### No schema changes to Offer

Use existing `source` string column with value "excess". Link back to excess items via `normalized_mpn` match. No new FK needed.

### No new tables

Existing models (Offer, Bid) are sufficient.

## API Endpoints (New)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/excess-lists/{id}/preview-import` | Parse file, return preview JSON |
| POST | `/api/excess-lists/{id}/confirm-import` | Import validated rows from preview (JSON) |
| POST | `/api/excess-lists/{id}/line-items/bulk-delete` | Delete multiple line items |
| POST | `/api/excess-lists/{id}/line-items/{item_id}/bids` | Record a bid |
| PATCH | `/api/excess-lists/{id}/line-items/{item_id}/bids/{bid_id}` | Accept/reject a bid |
| GET | `/api/excess-lists/{id}/line-items/{item_id}/bids` | List bids for a line item |

## HTMX Partials (New)

| Path | Purpose |
|------|---------|
| `/v2/partials/excess/import-preview` | Import preview table (read-only) |
| `/v2/partials/excess/{id}/line-items/{item_id}/bids` | Bid list modal content |
| `/v2/partials/excess/{id}/line-items/{item_id}/bid-form` | Record bid modal form |

## Sub-Project Breakdown

3 sub-projects, down from the original 4:

1. **SP1: Import Preview + Demand Matching** — Two-step upload (no re-mapping), auto-match active requirements on import, create Offers, show match counts
2. **SP2: Bid Recording** — Record bids via modal, bid list modal sorted by price, accept/reject with auto-cascade
3. **SP3: List & Detail UX** — Sortable columns, owner filter (default to user), bulk delete

**Dependency order:** SP1 → SP2 (bids are the next workflow step after matching). SP3 has no dependencies and can run in parallel with SP1 or SP2.

## Deferred to Phase 4

- **Bid solicitation emails** via Graph API (requires auto-parsing on the return path to be worth the effort)
- **ProactiveMatch creation** for archived deals
- **Cross-cutting normalization display rule** (show original + normalized MPN everywhere)
- **Stats cards** on list view
- **Note tooltips** on line items
- **Offer.excess_line_item_id FK** (if source+mpn linkage proves insufficient)
