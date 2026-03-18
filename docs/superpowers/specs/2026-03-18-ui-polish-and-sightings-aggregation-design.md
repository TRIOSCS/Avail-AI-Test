# Sub-Project 1: UI Polish & Sightings Aggregation

**Date:** 2026-03-18
**Status:** Approved
**Scope:** Targeted fixes and a sightings aggregation layer — no layout or workflow changes

This is the first of 7 sub-projects completing the requisitions workspace. It fixes existing bugs, adds missing functionality, and builds a solid aggregation foundation that Sub-Projects 4 (Vendor Outreach) and 6 (Activity Tab) will depend on.

---

## 1. Column Chooser Fix + Gear Icon

### Problem
Column chooser save fails with "request failed please try again." Icon should be a gear.

### Root Cause
Column preferences are stored in **two places**:
- Client: `localStorage` (in the `column_picker.html` Alpine component)
- Server: `User.parts_column_prefs` JSON column, saved via `POST /v2/partials/parts/column-prefs`

The save endpoint (`htmx_views.py:7197`) reads `form.getlist("columns")` and commits to DB. The "request failed" error is a server-side issue — likely a form data mismatch, missing CSRF token, or DB commit failure. Must debug the actual POST request/response.

### Design
- Debug the `POST /v2/partials/parts/column-prefs` endpoint — inspect what the form sends vs. what the endpoint expects
- Fix the root cause (form serialization, validation, or DB issue)
- Replace current column chooser icon with a gear icon (from existing Heroicons set)
- Ensure localStorage and server stay in sync on save

---

## 2. Sightings Aggregation Layer

### Problem
Sightings show duplicate vendor rows. Need one row per vendor with aggregated data.

### New Service: `app/services/sighting_aggregation.py`

Groups sightings by `(vendor_name, requirement_id)`. For each vendor group, computes:

| Field | Description |
|-------|-------------|
| **Aggregated qty** | AI-estimated total available from that vendor (Claude interprets varied qty formats like "2000+", "call", "in stock") |
| **Averaged price** | Weighted average across all sightings from that vendor for that part |
| **Best price** | Lowest listed price |
| **Listing count** | How many raw sightings were rolled up |
| **Source breakdown** | Which connectors found listings (BB, Nexar, DigiKey, etc.) |

### New Model: `VendorSightingSummary`

Materialized aggregation tied to `(requirement_id, vendor_name)`. Schema:

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | Surrogate key |
| `requirement_id` | FK → Requirement | Indexed |
| `vendor_name` | String | Normalized lowercase |
| `vendor_phone` | String, nullable | Main phone from `VendorCard.phone` (joined via normalized vendor name) |
| `estimated_qty` | Integer, nullable | AI-estimated total |
| `avg_price` | Float, nullable | Weighted average |
| `best_price` | Float, nullable | Lowest listed |
| `listing_count` | Integer | Raw sighting count |
| `source_types` | JSON | List of connector names |
| `score` | Float | Aggregated sighting score (0-100) — **max** of all raw sighting scores for this vendor+part (best lead quality wins) |
| `tier` | String | Derived tier label from score thresholds |
| `updated_at` | UTCTimestamp | Last rebuild time |

**Unique constraint:** `(requirement_id, vendor_name)`
**Indexes:** `requirement_id`, `vendor_name`, `score`

Relationships:
- `requirement` → Requirement (many-to-one)
- Access raw sightings via: `Sighting.query.filter_by(requirement_id=X, vendor_name=Y)`

### Rebuild Trigger
- Called **synchronously** from `search_service._save_sightings()` after sightings are upserted
- Rebuilds summaries **only for affected vendors** on that requirement (not all vendors)
- Also triggered on sighting deletion or update
- AI qty estimation uses Claude haiku for speed/cost — prompt: "Given these quantity listings [list], estimate the total available inventory as a single integer. Listings: {qty_values}"
- **Fallback if AI call fails:** Sum of all non-null `qty_available` values from raw sightings

### Alembic Migration
- New migration to create `vendor_sighting_summary` table
- No changes to existing `sighting` table

### Display Contract
- Sourcing tab queries `VendorSightingSummary` instead of raw sightings
- Each row: Vendor Name | Phone | Qty (clickable) | Price (clickable) | Score | Tier
- Clicking Qty → popover showing all raw listings with individual quantities and sources
- Clicking Price → popover showing all raw listings with individual prices and sources

**Note on existing columns:** The current sourcing tab shows Vendor | Source | Price | Qty | Score | Tier (evidence_tier T1-T7). The new layout:
- **Keeps:** Vendor, Qty, Price, Score
- **Adds:** Phone (clickable)
- **Replaces:** "Source" column → source info moves into Qty/Price popovers; "Tier" column changes from evidence_tier (T1-T7 provenance) to score-derived tier (Excellent/Good/Fair/Poor). Evidence tier still visible in the raw sighting popovers.

---

## 3. Scoring Fix + Tier Labels

### Problem
Scores displaying as 9350% — raw score rendered without formatting.

### Root Cause (Confirmed)
Two separate scoring systems exist:

1. **`app/scoring.py` → `score_sighting_v2()`** — scores individual sightings with **5 factors**: trust (0.30), price (0.25), qty (0.20), freshness (0.15), completeness (0.10). Returns **0-100**. This is what the sourcing tab displays.
2. **`app/services/sourcing_score.py` → `score_requirement()`** — scores buyer *effort* per requirement with **6 factors**: sightings, offers, RFQs, replies, calls, emails. Different concept entirely.

The bug: `sourcing.html:43` does `{% set score_pct = (s.score * 100)|int %}` — multiplying a 0-100 score by 100, producing 9350%.

### Fix
- Remove the `* 100` from the template: `{% set score_pct = s.score|int %}`
- Score already in 0-100 range from `score_sighting_v2()`
- This fix applies to both the new `VendorSightingSummary` template AND any raw sighting score displays in popovers

### Tier System

Tiers derived from sighting-level scores (`score_sighting_v2`, 5 factors). Thresholds aligned with existing color bands in `sourcing.html` template (green >= 70, amber >= 40):

| Tier | Score Range | Color | Meaning |
|------|-----------|-------|---------|
| Excellent | 70-100 | Green (text-green-600) | Strong availability, competitive pricing, reliable vendor |
| Good | 40-69 | Amber (text-amber-600) | Decent option, minor gaps |
| Fair | 20-39 | Gray (text-gray-500) | Worth considering, notable trade-offs |
| Poor | 0-19 | Red (text-red-500) | Low confidence, last resort |

### Display
- Score column shows the tier label (Excellent, Good, etc.) with color
- Hover over "Score" or "Tier" column header → popover explaining the 5 sighting scoring factors and their weights
- Numeric score still visible alongside tier label

---

## 4. Tooltips / Popovers

### One Reusable Pattern

Lightweight Alpine.js popover used in three places:

1. **Score/Tier column headers** — hover shows explanation of 5 scoring factors (trust 30%, price 25%, qty 20%, freshness 15%, completeness 10%)
2. **Qty cell** — click shows breakdown of all raw listings from that vendor (individual quantities + sources)
3. **Price cell** — click shows breakdown of all raw listings from that vendor (individual prices + sources)

### Behavior
- Header tooltips: show on hover, dismiss on mouse leave
- Qty/Price popovers: show on click, dismiss on click outside or Escape
- Positioned below trigger, auto-flips near viewport edge
- Content loaded inline from `VendorSightingSummary` + raw sightings — no extra HTMX requests
- Styled to match existing UI (borders, shadows, fonts, colors)

### Implementation
- Pure Alpine.js `x-show` + positioning logic
- No new dependencies

---

## 5. Archive System

### Enum Addition
Add `archived` to `RequirementSourcingStatus` in `app/enums.py`:
```python
class RequirementSourcingStatus(str, enum.Enum):
    open = "open"
    sourcing = "sourcing"
    offered = "offered"
    quoted = "quoted"
    won = "won"
    lost = "lost"
    archived = "archived"  # NEW
```

No migration needed — this is a Python-side enum; the DB column is a string.

### Archive Pill
- Add "Archived" pill to existing status pill row: All, Open, Awarded, **Archived**
- Remove the separate "Show Archived" toggle — unified design language
- Clicking "Archived" filters to archived requirements only
- Archived items hidden from other pills by default

### Three Archive Actions

| Action | Trigger | Scope |
|--------|---------|-------|
| **Single part** | Row action button | Archives one requirement |
| **Whole requisition** | Row action option | Archives the requisition + all child requirements |
| **Multi-select** | Checkbox selection + bulk button | Archives any mix of selected parts |

### Confirmation UX
- **Single part:** No confirmation — show undo toast (5 second window)
- **Whole requisition:** Confirmation dialog — "This will archive [name] and all [N] parts. Continue?"
- **Multi-select:** Confirmation dialog — "Archive [N] selected items?"

### Backend

**Endpoints:**
- `PATCH /v2/partials/parts/{requirement_id}/archive` — single part archive
- `PATCH /v2/partials/requisitions/{req_id}/archive` — whole requisition archive (cascades to children)
- `POST /v2/partials/parts/bulk-archive` — batch archive, body: `{"requirement_ids": [int], "requisition_ids": [int]}` — two separate arrays, no ambiguity

**Logic:**
- Single part: set `sourcing_status → archived`
- Whole requisition: set requisition `status → archived`, set all child requirements `sourcing_status → archived`
- Multi-select: resolve each ID list to the correct operation

### Unarchive
- Same three patterns in reverse, available from the Archived pill view
- Unarchived requirements return to `open` status (safe default — user can update from there)
- Unarchived requisitions return to `active` status

---

## Testing Strategy

Every fix gets coverage before moving on:

- **Column chooser:** Test `POST /v2/partials/parts/column-prefs` endpoint with valid/invalid form data, verify DB persistence
- **Sightings aggregation:** Unit tests for grouping logic, AI qty estimation mocked, verify summary rebuilds on new sightings, test fallback when AI fails
- **Scoring:** Test score formatting output (no more `* 100`), verify tier thresholds at boundaries (0, 19, 20, 39, 40, 69, 70, 100)
- **Popovers:** Test data passed to template context is correct (popover rendering is manual/visual testing)
- **Archive:** Test all three archive paths, test cascade behavior, test unarchive returns to correct status, test pill filtering, test bulk endpoint with mixed IDs

Target: 100% coverage maintained — no regressions.

---

## Dependencies on Future Sub-Projects

| This Sub-Project Provides | Used By |
|--------------------------|---------|
| `VendorSightingSummary` model | Sub-Project 4 (Vendor Outreach) — outreach targets aggregated vendor rows |
| Tier labels + scoring fix | Sub-Project 4 — prioritization of which vendors to contact |
| Archive system | Sub-Project 4 — archived parts excluded from outreach |
| Popover pattern | Sub-Projects 2, 3, 4, 5 — reused for offer details, quote preview, vendor card |

---

## Constraints

- **No layout or workflow changes** — existing page structure, tab placement preserved. Only column changes are in the sourcing tab (documented in Section 2 Display Contract)
- **No new dependencies** — Alpine.js + HTMX + existing stack only
- **Design language unity** — all new UI elements match existing pills, colors, fonts, spacing
- **Multi-select checkboxes** added to parts table rows — minor addition within existing table structure, not a layout overhaul
