# Sightings Page Redesign — Buyer Command Center

**Date:** 2026-03-25
**Status:** Approved
**Scope:** Full redesign of `/v2/sightings` — performance, intelligence, workflow, real-time

---

## 1. Problem Statement

The sightings page is the primary workspace for buyers triaging open requirements across all active requisitions. Current pain points:

- **Performance:** 55 SQL queries per table load (50 lazy loads for `requisition.creator`), ~10 queries per detail click
- **Limited triage signals:** No coverage indicator, no aging heatmap, no urgency dashboard — buyers must click into each requirement to assess priority
- **Hidden data:** Vendor intelligence (response rate, ghost rate, source types, lead time, MOQ), requirement constraints (date codes, firmware, condition), MaterialCard enrichment (lifecycle, RoHS), and OOO contact status all exist in the database but are invisible on this page
- **Workflow friction:** No inline note/call logging, no status advancement, no per-vendor RFQ, no batch assign/status operations — buyers leave the page for routine actions
- **No real-time feedback:** After sending RFQs or triggering searches, buyers must manually refresh to see updates

---

## 2. Design Principles

1. **Scan → Triage → Act:** Dashboard strip for page-level awareness, heatmap rows for row-level triage, detail panel for evaluation and action
2. **Two-state row heatmap:** Table row backgrounds use only rose = needs attention, or no color = fine. No amber/green row noise. (Dashboard strip counters and detail panel elements may use semantic colors — amber, blue, red — as appropriate for their context.)
3. **Progressive disclosure:** Show 2 key metrics inline, expand for full details. Collapse sections only if empty
4. **Reuse shared components:** Replace all hand-rolled UI with existing shared partials and macros
5. **Forward-only auto-progress:** Status auto-advancement never goes backwards, never overrides manual status

---

## 3. Critical Issues to Fix First (Phase 0)

These are bugs/gaps that block the redesign and must be resolved before any feature work.

### 3.1 Add SourcingStatus Transition Map

**File:** `/root/availai/app/services/status_machine.py`

`require_valid_transition("requirement", ...)` currently has no entry for SourcingStatus — it silently allows any transition. Add:

```python
from ..constants import SourcingStatus

SOURCING_TRANSITIONS = {
    SourcingStatus.OPEN: [SourcingStatus.SOURCING, SourcingStatus.ARCHIVED],
    SourcingStatus.SOURCING: [SourcingStatus.OFFERED, SourcingStatus.OPEN, SourcingStatus.ARCHIVED],
    SourcingStatus.OFFERED: [SourcingStatus.QUOTED, SourcingStatus.SOURCING, SourcingStatus.ARCHIVED],
    SourcingStatus.QUOTED: [SourcingStatus.WON, SourcingStatus.LOST, SourcingStatus.OFFERED, SourcingStatus.ARCHIVED],
    SourcingStatus.WON: [SourcingStatus.ARCHIVED],
    SourcingStatus.LOST: [SourcingStatus.OPEN, SourcingStatus.ARCHIVED],
    SourcingStatus.ARCHIVED: [],  # terminal
}
```

Register as `"requirement"` in `transition_map`. Import `SourcingStatus` from `constants.py` to match the existing code style (other entries use enum constants like `OfferStatus.PENDING_REVIEW`).

### 3.2 Extend `log_rfq_activity` Signature

**File:** `/root/availai/app/services/activity_service.py`

Current signature: `log_rfq_activity(db, rfq_id, activity_type, description, metadata=None, user_id=None)` where `rfq_id` maps to `requisition_id`.

Add optional `requirement_id` parameter: `log_rfq_activity(db, rfq_id, activity_type, description, metadata=None, user_id=None, requirement_id=None)`.

**Why:** The current `sightings_send_inquiry` endpoint manually creates `ActivityLog` objects with both `requisition_id` and `requirement_id` set (line 492). The sightings activity timeline filters on `ActivityLog.requirement_id` (line 226-232). If we switch to `log_rfq_activity()` without adding `requirement_id`, the per-requirement activity timeline would show no RFQ activity. The extension also enables other future call sites to log per-requirement activity consistently.

### 3.3 Alembic Migration: Pre-aggregated Fields on VendorSightingSummary

**Migration 1 — Schema:**

New columns on `vendor_sighting_summary`:
- `vendor_card_id` — Integer, FK to `vendor_cards.id`, nullable, ON DELETE SET NULL
- `newest_sighting_at` — DateTime, nullable
- `best_lead_time_days` — Integer, nullable
- `min_moq` — Integer, nullable
- `has_contact_info` — Boolean, server_default="false"

New indexes:
- `ix_vss_vendor_card` on `(vendor_card_id)`
- `ix_vss_vendor_req` on `(vendor_name, requirement_id)` — for cross-requirement overlap

**Migration 2 — Backfill:**

```sql
-- Backfill vendor_card_id
UPDATE vendor_sighting_summary vss
SET vendor_card_id = vc.id
FROM vendor_cards vc
WHERE vss.vendor_name = vc.normalized_name;

-- Backfill vendor_phone from VendorCard where NULL
UPDATE vendor_sighting_summary vss
SET vendor_phone = (vc.phones->>0)
FROM vendor_cards vc
WHERE vss.vendor_name = vc.normalized_name
  AND vss.vendor_phone IS NULL
  AND vc.phones IS NOT NULL
  AND jsonb_array_length(vc.phones) > 0;

-- Backfill aggregated fields
UPDATE vendor_sighting_summary vss SET
  newest_sighting_at = sub.newest,
  best_lead_time_days = sub.best_lt,
  min_moq = sub.min_moq,
  has_contact_info = sub.has_contact
FROM (
  SELECT requirement_id, vendor_name,
    MAX(created_at) AS newest,
    MIN(lead_time_days) FILTER (WHERE lead_time_days IS NOT NULL) AS best_lt,
    MIN(moq) FILTER (WHERE moq IS NOT NULL) AS min_moq,
    BOOL_OR(vendor_email IS NOT NULL OR vendor_phone IS NOT NULL) AS has_contact
  FROM sightings WHERE NOT is_unavailable
  GROUP BY requirement_id, vendor_name
) sub
WHERE LOWER(TRIM(sub.vendor_name)) = vss.vendor_name
  AND sub.requirement_id = vss.requirement_id;
```

### 3.4 Update `rebuild_vendor_summaries()`

**File:** `/root/availai/app/services/sighting_aggregation.py`

Compute and set during rebuild:
- `vendor_card_id` from VendorCard query (already fetches for phones)
- `newest_sighting_at = max(s.created_at for s in group)`
- `best_lead_time_days = min(s.lead_time_days for s in group if s.lead_time_days)`
- `min_moq = min(s.moq for s in group if s.moq)`
- `has_contact_info = any(s.vendor_email or s.vendor_phone for s in group)`

### 3.5 Move `splitPanel` Alpine Registration

**File:** `/root/availai/app/static/htmx_app.js`

Move `Alpine.data('splitPanel', ...)` from inline `<script>` in `split_panel.html` to `htmx_app.js` as a permanent global registration. The current inline script fails to re-register on HTMX partial swaps because `alpine:init` fires only on full page loads.

---

## 4. Phase 1: Performance + Shared Components

### 4.1 Fix Eager Loading

**File:** `/root/availai/app/routers/sightings.py` — in `sightings_list()` query builder

```python
.options(
    joinedload(Requirement.requisition).joinedload(Requisition.creator)
)
```

**Impact:** Eliminates 50 lazy-load queries per page.

### 4.2 Cache `all_buyers` and `stat_counts`

**Do NOT use `@cached_endpoint`** — it is sync-only and will crash on async endpoints. It also injects `_uid` into cache keys, making global data per-user.

Instead, use a simple in-process TTL cache:

```python
_cache: dict[str, tuple[float, Any]] = {}

def _get_cached(key: str, ttl: float, factory):
    now = time.monotonic()
    if key in _cache and now - _cache[key][0] < ttl:
        return _cache[key][1]
    result = factory()
    _cache[key] = (now, result)
    return result
```

- `all_buyers`: TTL 300s (5 min). Same for every user.
- `stat_counts`: TTL 30s. Invalidate explicitly after any `sourcing_status` write.

### 4.3 Merge `compute_vendor_statuses` — 4 Queries to 1

**File:** `/root/availai/app/services/sighting_status.py`

**Critical normalization requirement:** The 4 current queries use inconsistent vendor name matching. The blacklist check uses `VendorCard.normalized_name` (lowercase, trimmed). Offers, contacts, and sightings match against raw `vendor_name` strings.

Before merging, normalize all input `vendor_names` to lowercase/trimmed form and use `func.lower(func.trim(...))` consistently in the merged query. The merged query must preserve the different scope levels:
- Blacklisted: `VendorCard.is_blacklisted` (global)
- Offers: `Offer.requirement_id` (per-requirement)
- Contacts: `Contact.requisition_id` (per-requisition)
- Unavailable: `Sighting.requirement_id` (per-requirement)

### 4.4 Remove Vendor Phone Fallback

**File:** `/root/availai/app/routers/sightings.py` — in `sightings_detail()`, vendor phone fallback block

Replace the fallback dance with:
```python
vendor_phones = {s.vendor_name: s.vendor_phone for s in summaries if s.vendor_phone}
```

The backfill in Phase 0 ensures all existing VSS rows have `vendor_phone` populated.

### 4.5 Replace Hand-Rolled Components with Shared Partials

| Current | Replace With | File |
|---|---|---|
| Inline split panel (70 lines in `list.html`) | `{% include "htmx/partials/shared/split_panel.html" %}` with `panel_id="sightings"` | `list.html` |
| Inline stat pills (`table.html:12-28`) | `filter_pill` macro from `_macros.html` | `table.html` |
| Inline vendor status badges (`detail.html:96-112`) | `status_badge` macro with custom `status_map` | `detail.html` |
| Raw `<button>` elements | `btn_primary`/`btn_secondary`/`btn_danger` macros | All sightings templates |
| Inline empty states | `{% include "htmx/partials/shared/empty_state.html" %}` with CTAs | `table.html`, `detail.html` |
| Manual `ActivityLog()` in `send_inquiry` | `activity_service.log_rfq_activity()` (with new `requirement_id` param) | `sightings.py:487-495` |

### 4.6 Fix Pagination Target

**File:** `/root/availai/app/templates/htmx/partials/shared/pagination.html`

Add configurable `hx_target` parameter defaulting to `#main-content`. Sightings passes `hx_target="#sightings-table"`.

### 4.7 Add Batch Size Limit

**File:** `/root/availai/app/routers/sightings.py`

Add `MAX_BATCH_REFRESH = 50` constant. Return 400 if `len(requirement_ids) > MAX_BATCH_REFRESH`.

---

## 5. Phase 2: Table Visual Triage

### 5.1 Smart Priority Dashboard Strip

Replace the stat pills with 4 alert counters above the filter bar:

| Counter | Query | Color |
|---|---|---|
| Urgent | `priority_score >= 70` or `need_by_date` within 48h | Red badge |
| Stale | No `ActivityLog` in `sighting_stale_days` | Amber badge |
| Pending | `Offer.status == PENDING_REVIEW` | Blue badge |
| Unassigned | `assigned_buyer_id IS NULL` | Gray badge |

Each counter is a clickable filter. Clicking "3 Urgent" filters the table to those 3 rows.

These are 4 aggregate subqueries added to `sightings_list()`. The stale count must use a single `GROUP BY + HAVING` query, not the per-page pattern.

### 5.2 Fulfillment Coverage Bar (Replaces Top Vendor Column)

New column replacing "Top Vendor" in the table. Mini progress bar: `SUM(estimated_qty) / target_qty`.

**Visual design:** Monochrome gray fill on light gray track. Muted red fill if coverage is below 50%. No green/amber — that clashes with the row heatmap.

**Query:** Single aggregate added to `sightings_list()`:
```python
coverage = (
    db.query(
        VendorSightingSummary.requirement_id,
        sqlfunc.sum(VendorSightingSummary.estimated_qty).label("total_qty"),
    )
    .filter(VendorSightingSummary.requirement_id.in_(req_ids))
    .group_by(VendorSightingSummary.requirement_id)
    .all()
)
coverage_map = {c.requirement_id: c.total_qty or 0 for c in coverage}
```

### 5.3 Heatmap Rows — Two-State Only

**Rose tint** (`bg-rose-50/30`) for rows meeting ANY of:
- `need_by_date` is within 48 hours
- Stale (in `stale_req_ids`) AND `priority_score >= 40`
- `Requisition.urgency` is "critical" or "hot"

**No color** for everything else. No amber, no green. Absence of color IS the "fine" state.

Computed in Python from existing data — zero new queries.

### 5.4 Actionable Stale Indicator

For rows in `stale_req_ids`, show a small refresh icon overlaid on the coverage bar. Clicking triggers `POST /v2/partials/sightings/{id}/refresh` for that single requirement via HTMX.

### 5.5 Persistent Multi-Select

Move `selectedIds` from inline `x-data` to `$store.sightingSelection`:

```javascript
Alpine.store('sightingSelection', {
    ids: new Set(),
    toggle(id) { this.ids.has(id) ? this.ids.delete(id) : this.ids.add(id) },
    has(id) { return this.ids.has(id) },
    clear() { this.ids = new Set() },
    get count() { return this.ids.size },
    get array() { return Array.from(this.ids) },
})
```

**No `@persist`** — selections should not survive across page navigations. Clear on batch-send success.

Update all `selectedIds` references in `table.html` to `$store.sightingSelection.ids`.

### 5.6 Collapse Vendor List at 5

Show top 5 vendors by score. "Show all N vendors" text link below, expanding via Alpine `x-show` + `x-collapse`.

### 5.7 Final Table Columns (8 total)

| # | Column | Width | Content |
|---|---|---|---|
| 1 | Checkbox | 32px | Multi-select |
| 2 | MPN | flex | Monospace, primary identifier |
| 3 | Qty | 60px | Target quantity |
| 4 | Customer | flex | From requisition |
| 5 | Sales | 80px | Creator name |
| 6 | Coverage | 80px | Mini progress bar |
| 7 | Status | 80px | Status badge via macro |
| 8 | Priority | 60px | High/Med/Low pill |

Row background: rose or none (heatmap). Stale indicator: refresh icon on coverage bar.

---

## 6. Phase 3: Detail Panel Intelligence

### 6.1 Requirement Constraints Section

Collapsible section between header and vendor table. **Open by default if any constraint is non-null.**

Fields from existing models:
- `need_by_date` — from `Requirement` model, with urgency coloring
- `urgency` — from `Requisition` model (accessed via `requirement.requisition.urgency`), rendered via existing `urgency_badge` macro
- `condition`, `date_codes`, `firmware`, `packaging` — as label:value pairs
- `sale_notes` — if present
- `substitutes` — if present

**Template:** Extract to `sightings/_constraints.html`.

### 6.2 Vendor Intelligence Upgrade

Enhance each vendor row in the breakdown table:

**Always visible inline (2 key metrics):**
- Response rate (small text under vendor name)
- Best price with freshness timestamp ("$2.50 — 3d ago")

**Expandable per-vendor detail (on row click):**
- `listing_count` — "Found on 4 sources"
- `source_types` — via existing `source_badge.html` partial
- `tier` — evidence tier label
- `avg_price` vs `best_price` — price range
- `response_rate`, `ghost_rate` — from VendorCard
- `explain_lead()` output — plain-English quality label from `scoring.py`
- `best_lead_time_days`, `min_moq` — from new VSS columns

**Backend:** Extend the VendorCard batch query in `sightings_detail()` to also pull `response_rate`, `ghost_rate`, `engagement_score`, `avg_response_hours`. Call `explain_lead()` per summary. Net +0 queries (piggybacked on existing phone lookup query, which now also fetches intelligence fields).

**`explain_lead()` field mapping** (signature: `explain_lead(vendor_name, **kwargs)`):

| `explain_lead()` param | Source |
|---|---|
| `vendor_name` | `VendorSightingSummary.vendor_name` |
| `is_authorized` | `False` (not on VSS; derive from `source_types` containing "digikey"/"mouser" if needed) |
| `vendor_score` | `VendorCard.vendor_score` |
| `unit_price` | `VendorSightingSummary.best_price` |
| `median_price` | `None` (not available on VSS — omit, function handles None) |
| `qty_available` | `VendorSightingSummary.estimated_qty` |
| `target_qty` | `Requirement.target_qty` |
| `has_contact` | `VendorSightingSummary.has_contact_info` (new column) |
| `evidence_tier` | `VendorSightingSummary.tier` |
| `source_type` | First entry from `VendorSightingSummary.source_types` JSON |
| `age_days` | `(now - VendorSightingSummary.newest_sighting_at).days` (new column) |

**Template:** Extract vendor row to `sightings/_vendor_row.html`.

### 6.3 OOO Contact Detection

**Query:** Batch-fetch `VendorContact` records for all vendor names in the summary list in ONE query (not N+1):

```python
contacts_with_ooo = (
    db.query(VendorContact)
    .join(VendorCard, VendorContact.vendor_card_id == VendorCard.id)
    .filter(
        VendorCard.normalized_name.in_(normalized_names),
        VendorContact.is_ooo.is_(True),
    )
    .all()
)
ooo_map = {c.vendor_card.normalized_name: c for c in contacts_with_ooo}
```

**Display:** Amber "OOO until {date}" badge on vendor row. Warning in RFQ vendor modal checkbox label.

### 6.4 Sighting Freshness Timestamps

Show relative timestamp per vendor row from `newest_sighting_at` (new VSS column). Color amber if older than `sighting_stale_days`, red if older than 2x threshold.

### 6.5 MaterialCard Enrichment Bar

Slim info bar below part header. Only shown if `requirement.material_card_id` is set.

Fields (all exist on `MaterialCard`):
- `lifecycle_status` — badge, warning if EOL/obsolete
- `category`
- `rohs_status`
- `datasheet_url` — external link

**Open by default if lifecycle is EOL/obsolete.** Collapsed otherwise.

### 6.6 Suggested Next Action

State-machine-driven single-line prompt in detail header:

| State | Prompt |
|---|---|
| open, has sightings | "N vendors available — send RFQs" |
| sourcing, >3 days since RFQ | "RFQs pending for N days — follow up" |
| offered, has pending offers | "N offers received — review and accept/reject" |
| open, no sightings | "No vendors found — run search" |
| quoted | "Quote sent — awaiting customer response" |

Computed server-side, passed as `suggested_action` string in template context.

### 6.7 Better Empty States

Use shared `empty_state.html` with contextual CTAs:

| Context | Message | CTA |
|---|---|---|
| Zero sightings on requirement | "No vendors found yet" | "Run Search Now" button |
| Zero results after filtering | "No requirements match your filters" | "Clear Filters" button |
| Empty activity timeline | "No activity recorded yet" | "Log a note to start tracking" link |
| Vendor modal with zero suggested vendors | "No matching vendors found" | "Search vendors manually" link |

### 6.8 Template Decomposition

To prevent `detail.html` from growing past 600 lines, extract into sub-partials:

- `sightings/_constraints.html` — requirement constraints section
- `sightings/_vendor_row.html` — single vendor row with expandable intelligence
- `sightings/_quick_actions.html` — inline note/call form
- `sightings/_suggested_action.html` — next action prompt

---

## 7. Phase 4: Workflow Actions

### 7.1 Inline Quick Actions

**UI:** Collapsed "Log Note" button at top of activity timeline. Expands on click to: text input + channel dropdown (note/call/email) + optional vendor_name + submit button. On submit, collapses back. New entry animates into timeline (slide down, 300ms).

**Endpoint:** `POST /v2/partials/sightings/{requirement_id}/log-activity`

```python
@router.post("/v2/partials/sightings/{requirement_id}/log-activity")
async def sightings_log_activity(
    request: Request,
    requirement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    form = await request.form()
    activity_type = form.get("type", "note")  # note | call | email
    notes = form.get("notes", "").strip()
    vendor_name = form.get("vendor_name", "")
    # Validate, create ActivityLog, return updated detail
```

### 7.2 Per-Vendor RFQ Button

Small RFQ icon button in the vendor table Actions column. Opens the vendor modal pre-populated with that single requirement + vendor pre-selected:

```html
<button @click="$dispatch('open-modal', {
  url: '/v2/partials/sightings/vendor-modal?requirement_ids={{ requirement.id }}&preselect={{ s.vendor_name|urlencode }}'
})">
```

### 7.3 Advance Status Dropdown

Dropdown in detail panel header next to the buyer assignment. Uses `require_valid_transition()` with the new SourcingStatus transitions from Phase 0.

**Endpoint:** `PATCH /v2/partials/sightings/{requirement_id}/advance-status`

Validates transition, updates status, logs `activity_type="status_change"` to ActivityLog, returns updated detail panel.

### 7.4 Auto-Progress Sourcing Status

**Rules:**
1. After successful RFQ send (confirmed by `send_batch_rfq` return, not before): set `sourcing_status = "sourcing"` IF current status is "open"
2. After offer approval: set `sourcing_status = "offered"` IF current status is "open" or "sourcing"
3. **Never override a manual status** that is already ahead (e.g., don't set "sourcing" on something already "offered")
4. **Never go backwards** (e.g., don't set "sourcing" on something marked "won")
5. Log `ActivityLog` entry: `activity_type="status_change"`, `notes="Auto-set to sourcing after RFQ send"`
6. Show subtle toast: "Status auto-updated to Sourcing"

**Gate on email success:** The current `sightings_send_inquiry` try/except swallows email failures. Auto-progress must be placed INSIDE the `try` block, after `sent_count = len(results)` and before the ActivityLog loop:

```python
try:
    results = await send_batch_rfq(...)
    sent_count = len(results)

    # AUTO-PROGRESS: only on confirmed send success, forward-only
    for r in requirements:
        if r.sourcing_status == SourcingStatus.OPEN:
            r.sourcing_status = SourcingStatus.SOURCING
            db.add(ActivityLog(
                user_id=user.id, activity_type="status_change",
                requirement_id=r.id, requisition_id=r.requisition_id,
                notes="Auto-set to sourcing after RFQ send",
            ))

    # existing ActivityLog loop continues here...
except Exception:
    # email failed — do NOT advance status
    ...
```

### 7.5 Batch Operations

Three new buttons in the multi-select action bar:

**Batch Assign:** `POST /v2/partials/sightings/batch-assign`
- Form: `requirement_ids` (JSON list) + `buyer_id`
- Updates `assigned_buyer_id` on all requirements
- Returns toast with count

**Batch Status:** `POST /v2/partials/sightings/batch-status`
- Form: `requirement_ids` (JSON list) + `status`
- Validates each transition via `require_valid_transition()` — skips invalid, reports count
- Returns toast: "Updated N of M requirements. K skipped (invalid transition)."

**Batch Notes:** `POST /v2/partials/sightings/batch-notes`
- Form: `requirement_ids` (JSON list) + `notes`
- Creates ActivityLog per requirement
- Returns toast with count

All batch endpoints enforce `MAX_BATCH_SIZE = 50`.

### 7.6 Email Preview Before Send

Add a preview step between compose and send in `vendor_modal.html`. Multi-step Alpine flow:

1. **Step 1 (current):** Compose email body, select vendors
2. **Step 2 (new):** Preview rendered email per vendor with recipients, subject, body. "Back" to edit, "Send" to confirm
3. **Step 3:** Send confirmation toast

New endpoint: `POST /v2/partials/sightings/preview-inquiry`

**Request** (form data, same as compose step):
- `requirement_ids` — list of int IDs
- `vendor_names` — list of vendor name strings
- `email_body` — composed email text

**Response:** HTML partial rendering the preview for each vendor: recipient email (or "No email on file" warning), subject line, rendered body with parts table. Returns `sightings/preview.html` template.

**Error cases:**
- 400 if `requirement_ids`, `vendor_names`, or `email_body` is empty
- Vendor with no email: show amber warning "No email found for {vendor}" with option to enter manually or skip

### 7.7 Cross-Requirement Vendor Overlap

Precompute as a dict per page load (NOT per-row):

```python
overlap_counts = dict(
    db.query(
        VendorSightingSummary.vendor_name,
        sqlfunc.count(sqlfunc.distinct(VendorSightingSummary.requirement_id)),
    )
    .join(Requirement, VendorSightingSummary.requirement_id == Requirement.id)
    .join(Requisition, Requirement.requisition_id == Requisition.id)
    .filter(Requisition.status == RequisitionStatus.ACTIVE)
    .group_by(VendorSightingSummary.vendor_name)
    .having(sqlfunc.count(sqlfunc.distinct(VendorSightingSummary.requirement_id)) > 1)
    .all()
)
```

Display as badge on vendor row: "Also on 3 other reqs" — clickable tooltip shows which MPNs.

### 7.8 Parallelize Batch-Refresh

Replace sequential loop with `asyncio.gather` using per-task sessions:

```python
sem = asyncio.Semaphore(5)

async def _refresh_one(req_id: int):
    async with sem:
        task_db = SessionLocal()
        try:
            req_obj = task_db.get(Requirement, req_id)
            if not req_obj:
                return False
            # search_requirement is async but uses sync SQLAlchemy internally.
            # It runs connector calls via asyncio.gather (network I/O) and
            # sync DB writes between awaits, which is safe in a single-worker
            # deployment. Each task gets its own session to prevent cross-task
            # interference.
            await search_requirement(req_obj, task_db)
            return True
        finally:
            task_db.close()

results = await asyncio.gather(*[_refresh_one(rid) for rid in requirement_ids])
```

Each task gets its own `SessionLocal()` and re-fetches its ORM objects. The pre-fetched `reqs_by_id` dict from the original session is NOT shared.

**Note on sync/async:** `search_requirement` is an `async def` that uses sync SQLAlchemy sessions between awaited network calls. This is the established pattern throughout the codebase (single Gunicorn worker). The per-task session isolation prevents the actual issue (concurrent access to the SAME session object), not the sync-in-async pattern which is already accepted in this codebase.

---

## 8. Phase 5: Real-Time + Polish

### 8.1 SSE Live Updates

Use existing `SSEBroker` and `user:{id}` channel. Publish from:

- `search_service.py` after VendorSightingSummary rebuild: `broker.publish(f"user:{user_id}", "sighting-updated", json.dumps({"requirement_id": rid}))`
- `inbox_monitor.py` when offers are auto-created: same channel, event type `offer-created`
- All mutation endpoints in Phase 4: add `broker.publish()` calls

Frontend: Alpine listener on the global SSE stream (already connected in `base.html`). Conditionally triggers HTMX reload of `#sightings-detail` if `selectedReqId` matches incoming `requirement_id`.

SSE disconnect banner after 30s: thin amber bar below dashboard strip with "Live updates paused. [Refresh]".

### 8.2 Loading & Transition Polish

- **Vendor data loading:** 5 skeleton rows (gray shimmer) while detail panel loads
- **Detail panel swap:** Crossfade (150ms opacity transition), not hard swap
- **Action bar appearance:** Slide up from bottom (200ms) on first selection
- **Collapsible sections:** Use Alpine `x-collapse` (200ms height transition)
- **Status change animation:** Badge color transition (200ms ease)
- **Note logged:** New entry slides down into activity timeline (300ms)
- **Search refresh:** Spinner on coverage bar for that row during refresh

### 8.3 Multi-Select Awareness

"N selected on other pages" badge in the action bar when selections span pages:

```html
<span x-show="$store.sightingSelection.count > visibleSelectedCount"
      class="text-xs text-gray-500">
  + <span x-text="$store.sightingSelection.count - visibleSelectedCount"></span> on other pages
</span>
```

### 8.4 Responsive Breakpoints

| Breakpoint | Layout |
|---|---|
| 1280px+ | Split panel, all 8 columns |
| 1024–1279px | Split panel, 5 columns (drop Customer, Sales, Priority) |
| Below 1024px | Stacked: full-width table (4 cols) → full-screen detail on tap |
| Below 640px | Card list: MPN + status badge + coverage bar per card |

---

## 9. Test Plan — ~80 New Tests

| Phase | Feature | Tests |
|---|---|---|
| 0 | SourcingStatus transition map | 6 |
| 1 | Merged vendor statuses query | 4 |
| 1 | Cache helpers | 3 |
| 1 | Phone fallback removal | 2 |
| 2 | List endpoint new context vars | 6 |
| 3 | Detail endpoint new context vars | 8 |
| 4 | `POST /{id}/log-activity` | 8 |
| 4 | `PATCH /{id}/advance-status` | 10 |
| 4 | `POST /batch-assign` | 7 |
| 4 | `POST /batch-status` | 9 |
| 4 | `POST /batch-notes` | 7 |
| 4 | Auto-progress sourcing status | 7 |
| 5 | SSE + transitions | 3 |
| **Total** | | **80** |

---

## 10. Files Modified/Created

### Modified
- `app/routers/sightings.py` — all endpoint changes
- `app/schemas/sightings.py` — new request schemas
- `app/services/sighting_status.py` — merged query
- `app/services/sighting_aggregation.py` — new column computation
- `app/services/status_machine.py` — SourcingStatus transitions
- `app/services/activity_service.py` — `requirement_id` param
- `app/static/htmx_app.js` — `sightingSelection` store, `splitPanel` registration
- `app/templates/htmx/partials/sightings/list.html` — use shared split panel
- `app/templates/htmx/partials/sightings/table.html` — dashboard strip, coverage bar, heatmap, 8 columns
- `app/templates/htmx/partials/sightings/detail.html` — all intelligence sections, quick actions, suggested action
- `app/templates/htmx/partials/sightings/vendor_modal.html` — OOO warnings, email preview step
- `app/templates/htmx/partials/shared/pagination.html` — configurable `hx_target`
- `tests/test_sightings_router.py` — ~60 new tests
- `tests/test_sighting_aggregation.py` — ~6 updated tests

### Created
- `app/templates/htmx/partials/sightings/_constraints.html`
- `app/templates/htmx/partials/sightings/_vendor_row.html`
- `app/templates/htmx/partials/sightings/_quick_actions.html`
- `app/templates/htmx/partials/sightings/_suggested_action.html`
- `app/templates/htmx/partials/sightings/preview.html`
- `alembic/versions/XXX_add_vss_preaggregated_fields.py`
- `alembic/versions/XXX_backfill_vss_preaggregated_fields.py`
- `tests/test_workflow_state_clarity.py` — 6 sourcing transition tests (create new file if not existing, otherwise add new test class `TestSourcingStatusTransitions`)

---

## 11. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| `compute_vendor_statuses` merge breaks due to normalization mismatch | Normalize all vendor names before query; test with mixed-case data |
| Batch-refresh `asyncio.gather` causes session corruption | Per-task `SessionLocal()`, re-fetch ORM objects, semaphore(5) |
| Auto-progress overrides manual status | Forward-only rule, skip if current >= target |
| `detail.html` grows unmanageable | Decompose into 4 sub-partials from the start |
| SSE broker is in-process only (fails with multiple Gunicorn workers) | Use `user:{id}` channel on existing global stream; document single-worker requirement |
| Fulfillment coverage shows stale qty estimates | Show `newest_sighting_at` freshness alongside coverage bar |
| Table overflow on smaller screens | Responsive breakpoints drop columns at 1024px and 640px |
