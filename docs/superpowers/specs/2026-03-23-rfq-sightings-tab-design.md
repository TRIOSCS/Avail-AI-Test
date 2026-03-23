# RFQ Sightings Tab Redesign

**Date:** 2026-03-23
**Status:** Approved
**Context:** The RFQ page (parts/workspace.html) is for salespeople. The buyer side will be built separately later. This design prepares the sightings tab with derived status infrastructure that will light up as buyer workflows are built.

## Problem

The RFQ workspace Sourcing tab has a manual "Run Search" button and shows no outreach status. Salespeople need to see at a glance which vendors have been contacted, which have sent offers back, and which are unavailable or blacklisted — without manual data entry.

## Changes

### 1. Rename "Sourcing" tab to "Sightings"

**File:** `app/templates/htmx/partials/parts/workspace.html`

Change tab label from "Sourcing" to "Sightings". Tab key stays `sourcing` to avoid route changes.

### 2. Add derived status column to vendor table

**File:** `app/templates/htmx/partials/parts/tabs/sourcing.html`

Add a "Status" column to the existing vendor summary table. Status is **read-only** — derived from system activity, not manually set.

**Status values (in priority order):**

| Status | Badge Color | Derived From |
|--------|-------------|--------------|
| Blacklisted | Red | Vendor's company record has blacklisted flag |
| Offer-in | Green | An Offer record exists for this requirement + vendor |
| Contacted | Blue | An RFQ Contact record was sent to this vendor for this requirement's requisition |
| Unavailable | Gray | Sighting marked `is_unavailable=True` for this vendor |
| Sighting | Neutral/brand-100 | Default — no buyer action taken yet |

Priority matters: if a vendor is blacklisted AND has an offer, show "Blacklisted". If contacted AND offer-in, show "Offer-in" (higher signal).

### 3. Remove "Run Search" button from Sightings tab

**File:** `app/templates/htmx/partials/parts/tabs/sourcing.html`

Remove the "Run Search" button and its container div. Search is now automatic.

### 4. Auto-search on requirement save

**File:** `app/routers/requisitions/requirements.py`

After a requirement is created or updated (add/edit), fire `search_requirement()` as a background task. The sightings tab auto-populates without user action.

### 5. Remove Search button from req_row

**File:** `app/templates/htmx/partials/requisitions/tabs/req_row.html`

Remove the Search button and the hidden `search-results-{id}` row from the old requisition detail parts tab. Keep only the delete button in the Actions column.

### 6. Backend: compute vendor status

**File:** `app/routers/htmx_views.py` (sourcing tab route handler)

For each `VendorSightingSummary`, compute status by checking:
- `Company.is_blacklisted` (or equivalent) on the matched vendor company
- `Offer` records for this requirement + matching vendor name
- `Contact` records for this requisition + matching vendor
- `Sighting.is_unavailable` for this vendor + requirement

Pass a `vendor_statuses` dict (keyed by vendor_name) to the template.

**No model changes needed** — status is computed at query time, always fresh.

## What This Does NOT Include

- Buyer-side workflows (sending RFQs, entering offers, marking unavailable) — built separately
- Until buyer workflows exist, most vendors will show "Sighting" status
- The blacklisted check will work immediately if vendor company records have a blacklisted flag

## Files Changed

1. `app/templates/htmx/partials/parts/workspace.html` — rename tab label
2. `app/templates/htmx/partials/parts/tabs/sourcing.html` — add status column, remove Run Search
3. `app/routers/requisitions/requirements.py` — add background search on save
4. `app/templates/htmx/partials/requisitions/tabs/req_row.html` — remove Search button
5. `app/routers/htmx_views.py` — compute vendor statuses in tab route
6. Tests for status derivation logic
