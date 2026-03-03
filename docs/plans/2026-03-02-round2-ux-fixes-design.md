# Round 2 UX Fixes — Design Document

**Date**: 2026-03-02
**Scope**: 17 issues across 5 subsystems (RFQ flow, sightings, archive/re-quote, CRM lists, details/empty states)
**Approach**: Frontend-first — fix in `app.js` and `crm.js` with minimal backend changes

---

## Cluster 1 — RFQ Send Flow (R2-1 through R2-4)

### R2-1: Button count misleads (3 vendors selected, shows "1")

**Root cause**: `_updateDdBulkButton()` at `app.js:5843` counts individual sightings with emails, not unique vendor groups. Three sightings from 3 vendors where only 1 has email → "(1)".

**Fix**: Group selected sightings by vendor name (same logic as `ddSendBulkRfq`), then display vendor count. Show "Prepare RFQ (3 vendors)" when all have emails, or "Prepare RFQ (2 of 3 vendors have email)" when some don't.

**File**: `app/static/app.js:5843-5864`

### R2-2: Batch RFQ modal ignores vendor selection — loads all 50

**Root cause**: `ddSendBulkRfq()` correctly groups selected sightings by vendor and passes to `openBatchRfqModal(vendorGroups)`. The `/api/requisitions/{id}/rfq-prepare` endpoint returns data for ALL known vendors, not just the submitted list. The `rfqVendorData` gets populated with all vendors from the response at line 8219.

**Fix**: After `rfq-prepare` returns, filter `data.vendors` to only vendors whose names match the original `groups` vendor names (normalized lowercase match). Discard extra vendors from the response.

**File**: `app/static/app.js:8219-8244`

### R2-3: Modal stuck on "Finding contacts" — never advances

**Root cause**: `Promise.all(needsLookup.map(...))` at line 8275 waits for ALL vendor lookups. If one `/api/vendor-contact` call stalls or hangs, the entire modal stays frozen on "Finding contacts…".

**Fix**:
1. Add a 15-second per-vendor timeout via `Promise.race([fetch, timeout])`
2. When timeout fires, mark vendor as `lookup_status: 'no_email'` with `lookup_fail_reason: 'Lookup timed out'`
3. After all lookups settle (success or timeout), immediately transition to step 2 (line 8307)
4. Show "Skip" button after 5 seconds of any vendor still loading, allowing user to advance with partial results

**File**: `app/static/app.js:8256-8305`

### R2-4: Subject and Message fields blank

**Root cause**: `renderRfqMessage()` at line 8675 checks localStorage for a saved draft (`rfq_draft_{reqId}`). If a draft exists with empty `subject`/`body` (e.g., user cleared fields previously), it restores blank values and never falls through to auto-generation.

**Fix**: Only restore draft if BOTH `subject` and `body` are non-empty strings. Otherwise fall through to auto-population. Also add a "Reset to default" link near the subject field that clears the draft and regenerates.

**File**: `app/static/app.js:8675-8709`

---

## Cluster 2 — Sightings Filters & Sort (R2-5, R2-16)

### R2-5: Filter tabs all show identical results

**Root cause**: `_ddApplyFilters()` at `app.js:5536` has two problems:
1. **Available/N/A**: Checks `s.is_unavailable` which is never set on sightings. Both return same results as "All".
2. **Exact/Substitute**: Compares `s.mpn_matched` to `groupLabel`. Works correctly in theory, but when the searched MPN matches the returned MPN (common case), "Exact" returns everything.

**Fix**: Redefine filter semantics based on actually-available data:
- **Exact**: `mpn_matched` equals group label (keep — already correct)
- **Substitute**: `mpn_matched` differs from group label (keep — already correct)
- **Available**: `qty_available > 0` (has stock, regardless of `is_unavailable` flag)
- **N/A**: `qty_available == null || qty_available <= 0` OR no price (can't fill)

**File**: `app/static/app.js:5548-5557`

### R2-16: Sort ignores fulfillability

**Root cause**: Sort at line 5760 is purely by `vendor_score`. A vendor with 40 qty (need 100) at score 80 outranks a vendor with 100+ qty at score 70.

**Fix**: Two-tier sort:
1. **Primary**: Can-fill flag — vendors where `qty_available >= target_qty` sort first
2. **Secondary**: Vendor score descending (within each tier)

Compute per-sighting: look up `target_qty` from `_ddReqCache[reqId]` for the matching requirement. If `qty_available >= target_qty`, sighting goes in "can fill" tier.

**File**: `app/static/app.js:5760-5765`

---

## Cluster 3 — Archive/Re-quote (R2-9 through R2-11)

### R2-9: Archive expanded rows only show Parts + Files

**Root cause**: `_ddSubTabs(mainView)` at line 2669 returns tabs based on `_currentMainView`. When viewing archive from the RFQ tab with `_reqStatusFilter === 'archive'`, `_currentMainView` remains 'rfq' → returns `['parts', 'offers', 'quotes', 'files']` (no activity). The user may also see empty content in offers/quotes tabs since archived reqs may have no offers yet.

**Fix**: Modify `_ddSubTabs()` to also check `_reqStatusFilter`. If `_reqStatusFilter === 'archive'`, return the archive tab set regardless of `_currentMainView`. The archive set should be: `['parts', 'offers', 'quotes', 'activity', 'files']`.

**File**: `app/static/app.js:2669-2672`

### R2-10: Archive silently — no outcome prompt

**Root cause**: Two archive functions exist:
- `toggleArchive(id)` at line 7352 — NO prompt, just archives directly
- `archiveFromList(reqId)` at line 7368 — HAS outcome prompt (`prompt()`)

The archive button on the requisition row calls `toggleArchive`, skipping the outcome prompt entirely.

**Fix**:
1. Replace `toggleArchive` calls with `archiveFromList` for non-archive views
2. Upgrade the `prompt()` in `archiveFromList` to a proper modal with 3 buttons: **Won** (green), **Lost** (red), **Just Archive** (gray), **Cancel**
3. The modal sets the outcome before archiving

**File**: `app/static/app.js:7352-7403` + `index.html` (add modal markup)

### R2-11: Re-quote button is silent

**Root cause**: `requoteFromList()` at line 7417 uses `confirm()` which can be missed, and `expandToSubTab(resp.id, 'sightings')` may fail silently because `_reqListData` hasn't been updated yet after `loadRequisitions()`.

**Fix**:
1. Ensure `await loadRequisitions()` completes before calling `expandToSubTab`
2. After `loadRequisitions`, verify the new req ID exists in `_reqListData`
3. Add a more visible toast + auto-scroll to the new row

**File**: `app/static/app.js:7416-7432`

---

## Cluster 4 — CRM Lists (R2-12 through R2-15)

### R2-12: Pipeline tab req link navigates away — no back path

**Root cause**: Pipeline tab links call `setMainView()` + `expandToSubTab()` which navigates away from the Accounts view entirely. No breadcrumb or back button.

**Fix**: Open req links from the pipeline tab in a new browser tab (`window.open()` with `_blank`). This preserves the Accounts context. Add `target="_blank"` and a small external link icon to signal the behavior.

**File**: `app/static/crm.js:1121` (pipeline tab link handler)

### R2-13: Accounts list — Revenue, Win Rate, Last RFQ blank

**Root cause**: Backend `companies.py` never computes or returns `revenue_90d`, `win_rate`, `last_req_date`. Frontend shows "—" for all.

**Fix (backend)**: Add a lightweight sub-query to the companies list endpoint that computes:
- `revenue_90d`: Sum of won requisition values in last 90 days (from requisitions with outcome='won')
- `win_rate`: `count(won) / count(won + lost)` (null if no closes)
- `last_req_date`: Max `created_at` from requisitions for this company

This is a small backend change to `app/routers/crm/companies.py` — add a joined aggregation or post-processing step.

**File**: `app/routers/crm/companies.py:89-128`, `app/static/crm.js:336-340`

### R2-14: Vendor response rate blank + duplicates + irrelevant vendors

**Root cause**:
- Response rate: The vendor scorecard endpoint (`/api/performance/vendors`) returns `response_rate` but many vendors have `is_sufficient_data: false` → shown as "Insufficient Data"
- Duplicates: Multiple VendorCard records for same vendor (different name casing)
- Irrelevant: Vendors with zero RFQ history clog the list

**Fix**:
1. Lower the threshold for "sufficient data" or always show response rate even with low data (with a "(low data)" qualifier)
2. Add server-side deduplication by `normalized_name` in the performance vendors endpoint
3. Add a filter toggle "Active vendors only" (default on) that hides vendors with 0 interactions
4. Show "No RFQs sent" instead of blank for vendors with 0 RFQ history

**File**: `app/static/crm.js:5449-5514`, `app/routers/crm/performance.py` (if backend dedup needed)

### R2-15: Contacts list prefixes + truncated column + bad title data

**Root cause**: The contacts list at `app.js:1295` renders `c.full_name` and `c.title` directly from API data. "!" and ":" prefixes are data quality issues — vendor contact names imported from email parsing or enrichment with leading punctuation. Truncated column = CSS overflow. Bad title data = enrichment returned job descriptions instead of titles.

**Fix**:
1. Sanitize display: strip leading "!", ":", and whitespace from `full_name` and `title` in `renderContacts()`
2. Add `text-overflow: ellipsis` + `title` attribute for the Title column so truncated text shows on hover
3. Truncate overly long titles (>50 chars) with "…"
4. Add `max-width` to title column

**File**: `app/static/app.js:1295-1319`

---

## Cluster 5 — Details + Empty States + Activity (R2-6, R2-7, R2-8, R2-17)

### R2-6: "Check for Replies" button has no label/tooltip/explanation

**Root cause**: Button at `app.js:2829` has `title` attribute but appears as just "↻ Check for Replies" text. The label is there but may be too cryptic for new users.

**Fix**: Add a descriptive tooltip: "Scan your inbox for vendor email replies to RFQs sent for this requisition". Also add a subtle help-text line below the button: "Checks for new vendor responses to your RFQs".

**File**: `app/static/app.js:2809, 2829`

### R2-7: Offers empty state references non-existent "vendor tab"

**Root cause**: Empty state at `app.js:3148` says "log from vendor tab" — no vendor tab exists in the drill-down.

**Fix**: Change to: "No offers yet — use **+ Log Offer** above or send an RFQ from the **Sightings** tab"

**File**: `app/static/app.js:3148`

### R2-8: Quotes empty state references non-existent "Build Quote" button

**Root cause**: Empty state says "use Build Quote" but no button named that exists.

**Fix**: Change to: "No quotes yet — select offers in the **Offers** tab, then use the **Quote** action to build a quote"

**File**: `app/static/app.js:3821`

### R2-17: Details tab missing TARGET PRICE and MANUFACTURER

**Root cause**: `_renderDdDetails()` at line 4915 already shows `target_price`. It shows `r.brand` at line 4902 but NOT `r.manufacturer` separately. The issue may be that `brand` and `manufacturer` are different fields, and the user expects to see manufacturer.

**Fix**: Add manufacturer display after brand in the details tab. If `r.manufacturer` exists and differs from `r.brand`, show it as a separate line. Also verify `target_price` is actually rendering (the code exists at line 4915 — may be a data issue where requirements don't have target prices set).

**File**: `app/static/app.js:4895-4908`

---

## Files Modified Summary

| File | Issues | Change Scope |
|------|--------|-------------|
| `app/static/app.js` | R2-1 to R2-11, R2-15 to R2-17 | 14 issues — bulk of fixes |
| `app/static/crm.js` | R2-12, R2-14 | 2 issues — pipeline links, vendor list |
| `app/routers/crm/companies.py` | R2-13 | Add aggregation for revenue/win_rate/last_req |
| `app/templates/index.html` | R2-10 | Archive outcome modal markup |
| Tests | R2-13 | Test new companies aggregation |

## Not Changing

- No database migrations
- No new models
- No new Alembic revisions
- No changes to scheduler or background jobs
