# Avail Frontend Redesign — Multi-Phase Implementation Plan

## Scope

Absorb the standalone `/requisitions2` page into the HTMX + Alpine.js app shell, then build the missing features from PLAN-rfq-redesign.md. Delete the standalone page when done.

**Decisions locked in:**
- Build Sales + Sourcing views simultaneously
- Merge 7 tabs → 5 (Sourcing, Offers, Quote, Tasks, Activity)
- Full inline RFQ flow (sticky bar + slide-up compose, no modal)
- Delete `/requisitions2` entirely after feature parity

---

## Phase 1: Absorb requisitions2 into HTMX Shell (Foundation)

**Goal:** Feature parity with standalone requisitions2 inside the HTMX app shell. No visual redesign yet — just migration.

### 1a. Activate the domain-split router

| File | Action |
|------|--------|
| `app/main.py` | Register `app/routers/htmx/requisitions.py` under `/v2` prefix |
| `app/routers/htmx/requisitions.py` | Add missing endpoints from `requisitions2.py`: inline edit (GET form + PATCH save), row actions (claim/unclaim/won/lost/clone), SSE stream, bulk bar |

**New endpoints to add to `htmx/requisitions.py`:**
- `GET /v2/partials/requisitions/{id}/edit/{field}` — inline edit form
- `PATCH /v2/partials/requisitions/{id}/inline` — save inline edit, return row
- `POST /v2/partials/requisitions/{id}/action/{action}` — row actions (archive/claim/won/lost/clone/assign)
- `GET /v2/partials/requisitions/stream` — SSE for live table refresh

### 1b. Port templates into HTMX partials

| Source (requisitions2/) | Target (htmx/partials/requisitions/) | Notes |
|-------------------------|--------------------------------------|-------|
| `_filters.html` | Update `list.html` filter section | Rewrite with `hx-get="/v2/partials/requisitions"` targets |
| `_table.html` + `_table_rows.html` | New `table.html` + `table_rows.html` | Keep sortable columns, add `hx-target="#main-content"` |
| `_bulk_bar.html` | New `bulk_bar.html` | Replace `fetch()` calls with `hx-post`, use Alpine `selectedIds` |
| `_inline_cell.html` | New `inline_cell.html` | Same pattern: GET form → auto-focus → blur/Enter saves → PATCH |
| `_single_row.html` | New `single_row.html` | Row returned after inline save (outerHTML swap) |
| `_modal.html` | Use existing detail partial | Link name click → `hx-get="/v2/requisitions/{id}"` into `#main-content` instead of modal |

### 1c. Port Alpine component

| Source | Target | Notes |
|--------|--------|-------|
| `requisitions2.js` | Inline in `list.html` via `x-data` | Move `rq2Page()` logic (selectedIds Set, toggleSelection, toggleAll, showToast, getSelectedIdsString) directly into the partial |

### 1d. Wire SSE for live updates

- Connect `hx-ext="sse" sse-connect="/v2/partials/requisitions/stream"` on the table wrapper
- SSE event `table-refresh` triggers `hx-get` to reload table rows
- Reuse existing `sse_broker.py` with channel `requisitions:list`

### 1e. Tests

| File | Tests |
|------|-------|
| `tests/test_htmx_requisitions.py` | `test_list_partial_returns_html`, `test_inline_edit_name`, `test_inline_edit_status`, `test_bulk_archive`, `test_bulk_activate`, `test_row_action_claim`, `test_row_action_clone`, `test_sse_stream_endpoint`, `test_filters_preserve_query_params` |

### 1f. Delete standalone requisitions2

| File | Action |
|------|--------|
| `app/routers/requisitions2.py` | DELETE |
| `app/templates/requisitions2/` (all 8 files) | DELETE |
| `app/static/js/requisitions2.js` | DELETE |
| `app/schemas/requisitions2.py` | DELETE (if not shared) |
| `app/main.py` | Remove requisitions2 router registration |

**Deployable checkpoint:** All requisitions2 features work at `/v2/requisitions` inside the app shell. Old page is gone.

---

## Phase 2: Role-Aware Mode Toggle + Priority Lanes

**Goal:** Replace the flat table with Sales/Sourcing/Archive views and priority lane grouping.

### 2a. Mode toggle component

| File | Change |
|------|--------|
| `app/templates/htmx/partials/requisitions/list.html` | Add mode toggle above filters: `[Sales View] [Sourcing View] [Archive]` |
| Alpine x-data | Add `mode` property, persist to localStorage via `$persist` plugin |
| `hx-get` | Pass `?mode=sales\|sourcing\|archive` to server for lane grouping |

### 2b. Priority lane grouping (server-side)

| File | Change |
|------|--------|
| `app/routers/htmx/requisitions.py` | New `_group_into_lanes(requisitions, mode)` helper |
| `app/services/requisition_list_service.py` | Add lane assignment logic |

**Sales lanes** (determined by status + sourcing state):
- **Needs Action** — overdue deadline OR has unreviewed offers OR unsent quotes
- **In Progress** — active sourcing, quotes in draft
- **Waiting** — RFQs sent, pending responses
- **New/Draft** — just created, no parts or draft status

**Sourcing lanes** (determined by part coverage):
- **Unsourced** — has parts with zero sightings
- **Sightings Found** — has sightings but no RFQs sent
- **Awaiting Responses** — RFQs sent, no offers yet
- **Offers In** — has vendor offers ready for quoting

### 2c. Lane template

| File | Purpose |
|------|---------|
| `app/templates/htmx/partials/requisitions/lane.html` | NEW — collapsible section: header (color dot + name + count badge), content area for cards/rows |

Each lane is collapsible via Alpine `x-show` + `x-collapse`. Count badges in header. Empty lanes hidden.

### 2d. Tests

| File | Tests |
|------|-------|
| `tests/test_requisition_lanes.py` | `test_sales_lanes_grouping`, `test_sourcing_lanes_grouping`, `test_empty_lane_hidden`, `test_mode_toggle_preserves_filters`, `test_archive_mode_shows_closed` |

**Deployable checkpoint:** Users can toggle between Sales/Sourcing/Archive views. Requisitions grouped into priority lanes with collapsible sections.

---

## Phase 3: Card Layout for Requisitions

**Goal:** Replace table rows with rich cards showing view-specific information.

### 3a. Card templates (view-specific)

| File | Purpose |
|------|---------|
| `app/templates/htmx/partials/requisitions/card_sales.html` | NEW — Sales view card: customer name, parts/sourced/offers counts, quote status + value, due date, action button (Build Quote / Send Quote / Follow Up) |
| `app/templates/htmx/partials/requisitions/card_sourcing.html` | NEW — Sourcing view card: coverage progress bar (X/Y sourced), sightings count, RFQ stats (sent/responded %), action buttons (Source Parts / Send RFQs) |

### 3b. Card data enrichment

| File | Change |
|------|--------|
| `app/routers/htmx/requisitions.py` | Compute card metrics per requisition: `parts_count`, `sourced_count`, `sightings_count`, `offers_count`, `rfqs_sent`, `rfqs_responded`, `quote_status`, `quote_value`, `coverage_pct` |
| `app/services/requisition_list_service.py` | Add `enrich_card_metrics(requisitions, db)` — batch query to avoid N+1 |

### 3c. Card interactions

- Click card → `hx-get="/v2/requisitions/{id}"` loads detail into `#main-content`
- `···` menu on card → dropdown with row actions (same as Phase 1 actions)
- Checkbox on card for bulk selection (same Alpine Set pattern)
- Action button on card → direct HTMX action (e.g., "Source Parts" → `hx-get="/v2/requisitions/{id}?tab=sourcing"`)

### 3d. Tests

| File | Tests |
|------|-------|
| `tests/test_requisition_cards.py` | `test_sales_card_renders_quote_status`, `test_sourcing_card_renders_coverage`, `test_card_action_button_links`, `test_card_metrics_batch_query` |

**Deployable checkpoint:** Requisition list shows rich cards instead of table rows, with view-specific information and inline actions.

---

## Phase 4: Tab Merge (7 → 5) + Inline Sightings

**Goal:** Simplify the requisition detail view. Merge Parts+Sightings→Sourcing, Quotes+BuyPlans→Quote, Activity+Responses→Activity. Keep Tasks and Offers as-is.

### 4a. Sourcing tab (Parts + Sightings merged)

| File | Purpose |
|------|---------|
| `app/templates/htmx/partials/requisitions/tabs/sourcing.html` | NEW — Parts table where each row expands to show its sightings inline |
| `app/routers/htmx/requisitions.py` | Update tab endpoint: when `tab=sourcing`, return parts with their sighting counts; add sub-endpoint for expanding sightings per part |

**UX pattern:**
- Part row shows: MPN, brand, qty, target price, sighting count badge, sourcing status
- Click expand chevron → `hx-get="/v2/partials/requisitions/{id}/requirements/{req_id}/sightings"` loads sightings below the row
- Sightings have checkboxes for RFQ selection (feeds into Phase 5 inline RFQ)
- "Add Part" / "Upload" / "Paste" actions in tab header (already exist)

### 4b. Quote tab (Quotes + Buy Plans merged)

| File | Purpose |
|------|---------|
| `app/templates/htmx/partials/requisitions/tabs/quote.html` | NEW — Quote builder + buy plan + attachments in sections |

**Sections:**
1. Quote builder (existing quote creation UI)
2. Buy plan (existing buy plan UI, shown below quote)
3. Attachments (file upload/list, moved from standalone Files tab)

### 4c. Activity tab (Activity + Responses merged)

| File | Purpose |
|------|---------|
| `app/templates/htmx/partials/requisitions/tabs/activity.html` | NEW — Unified timeline with filter pills: All | RFQs | Replies | Notes |

**Data sources merged:**
- RFQ send events (from Contact records)
- Vendor responses (from parsed replies)
- Manual notes/activity log entries
- System events (status changes, claims)

Filter via `hx-get` with `?activity_type=rfqs|replies|notes|all`.

### 4d. Update tab navigation

| File | Change |
|------|--------|
| `app/templates/htmx/partials/requisitions/detail.html` | Change tab buttons from 7 to 5: Sourcing, Offers, Quote, Tasks, Activity |
| `app/routers/htmx/requisitions.py` | Update tab routing: `sourcing` → new merged template, `quote` → merged template, `activity` → merged template. Keep `offers` and `tasks` as-is. Remove standalone `parts`, `responses`, `buy_plans` tab routes. |

### 4e. Tests

| File | Tests |
|------|-------|
| `tests/test_requisition_tabs.py` | `test_sourcing_tab_shows_parts_with_sighting_counts`, `test_expand_sightings_for_part`, `test_quote_tab_shows_buy_plan_section`, `test_activity_tab_filters`, `test_old_tab_names_redirect` |

**Deployable checkpoint:** Requisition detail has 5 clean tabs. Parts expand to show sightings inline. Quote tab includes buy plan. Activity tab is a unified timeline.

---

## Phase 5: Inline RFQ Flow

**Goal:** Send RFQs directly from the Sourcing tab without leaving context. Sticky bottom bar + slide-up compose panel.

### 5a. Sighting selection → sticky bar

| File | Purpose |
|------|---------|
| `app/templates/htmx/partials/requisitions/tabs/sourcing.html` | Add checkboxes to sighting rows; Alpine tracks `selectedSightings` |
| `app/templates/htmx/partials/requisitions/rfq_sticky_bar.html` | NEW — Fixed bottom bar: "Send RFQ to N vendors (M parts)" + [Compose →] button. Shows via `x-show="selectedSightings.length > 0"` with `x-transition`. |

### 5b. Slide-up compose panel

| File | Purpose |
|------|---------|
| `app/templates/htmx/partials/requisitions/rfq_compose_inline.html` | NEW — Slide-up panel (not modal, not drawer). Contains: vendor list with emails, AI-drafted email body, condition selector, preview toggle, Send button. |
| `app/routers/htmx/requisitions.py` | Add `GET /v2/partials/requisitions/{id}/rfq-compose-inline` — accepts `sighting_ids` query param, returns compose panel with vendor grouping |

**UX flow:**
1. User checks sightings in Sourcing tab
2. Sticky bar slides up from bottom
3. Click "Compose" → `hx-get` loads compose panel, pushes down the sighting list
4. User reviews/edits email, clicks "Send"
5. `hx-post` sends RFQs → returns success summary inline
6. Panel collapses, sighting checkboxes clear

### 5c. Send + feedback

| File | Change |
|------|--------|
| `app/routers/htmx/requisitions.py` | Add `POST /v2/partials/requisitions/{id}/rfq-send-inline` — calls `email_service.send_batch_rfq()`, returns result summary partial |
| `app/templates/htmx/partials/requisitions/rfq_send_result.html` | NEW — Success/failure summary: "Sent to 3 vendors ✓, 1 failed ✗" with retry option |

### 5d. Tests

| File | Tests |
|------|-------|
| `tests/test_inline_rfq.py` | `test_compose_panel_loads_with_sighting_ids`, `test_compose_groups_by_vendor`, `test_send_inline_calls_email_service`, `test_send_result_shows_status`, `test_sticky_bar_hidden_when_no_selection` |

**Deployable checkpoint:** RFQs can be sent directly from the Sourcing tab. No drawer, no page change. Full send-and-confirm loop inline.

---

## Phase 6: Notifications (Bell + Smart Row)

**Goal:** Add notification bell in topbar + smart notification row on requisition list.

### 6a. Notification bell in topbar

| File | Change |
|------|--------|
| `app/templates/htmx/base.html` | Add bell icon with count badge in topbar (right side, before user menu) |
| `app/templates/htmx/partials/notifications/dropdown.html` | NEW — Dropdown panel: recent notifications list, "Mark all read" link, "View all" link |
| `app/routers/htmx/notifications.py` | NEW — `GET /v2/partials/notifications/dropdown` (recent 10), `POST /v2/partials/notifications/{id}/read` (mark read), `POST /v2/partials/notifications/read-all` |

**Bell behavior:**
- Shows unread count badge (red dot with number)
- Click toggles dropdown via Alpine
- Dropdown loads via `hx-get` on first open (lazy)
- SSE channel `notifications:{user_id}` pushes count updates

### 6b. Smart notification row on requisition list

| File | Change |
|------|--------|
| `app/templates/htmx/partials/requisitions/notification_row.html` | NEW — Contextual bar: "2 new vendor responses · 1 quote due today · 3 unsourced" with action buttons |
| `app/routers/htmx/requisitions.py` | Add `GET /v2/partials/requisitions/notifications` — computes notification summary counts |

**Notification types:**
- New vendor responses (unread parsed replies)
- Quotes due today/overdue
- Unsourced parts (requirements with zero sightings)
- Expiring strategic vendors (if Phase from PLAN.md is done)

Dismissible per session via Alpine `dismissed` flag.

### 6c. Notification service

| File | Purpose |
|------|---------|
| `app/services/notification_service.py` | NEW — `get_unread_count(db, user_id)`, `get_recent(db, user_id, limit)`, `mark_read(db, notification_id)`, `mark_all_read(db, user_id)`, `get_requisition_alerts(db, user_id)` (for smart row) |

Uses existing `Notification` model (`app/models/notification.py`). Extend `event_type` enum to include: `vendor_response`, `quote_due`, `rfq_failed`, `strategic_expiring`.

### 6d. Tests

| File | Tests |
|------|-------|
| `tests/test_notifications.py` | `test_unread_count`, `test_mark_read`, `test_mark_all_read`, `test_dropdown_returns_recent`, `test_requisition_alerts_counts`, `test_bell_badge_updates_via_sse` |

**Deployable checkpoint:** Notification bell in topbar with dropdown. Smart notification row on requisition list showing actionable items.

---

## Phase Summary

| Phase | What | Risk | Backend Changes | New Templates | Tests |
|-------|------|------|-----------------|---------------|-------|
| **1** | Absorb requisitions2 into HTMX shell | Low | Router endpoints, delete old files | 5 partials | 9 |
| **2** | Mode toggle + priority lanes | Low | Lane grouping logic | 1 partial | 5 |
| **3** | Card layout | Medium | Metrics enrichment query | 2 partials | 4 |
| **4** | Tab merge 7→5 | Medium | Tab routing, sighting expansion | 3 partials | 5 |
| **5** | Inline RFQ flow | High | Compose + send endpoints | 3 partials | 5 |
| **6** | Notifications | Low | Notification service + router | 2 partials + base.html edit | 6 |

**Total: ~16 new template partials, ~34 tests, 6 deployable checkpoints.**

---

## Execution Notes

- Each phase is independently deployable and testable
- Phase 1 is prerequisite for all others (establishes the HTMX foundation)
- Phases 2-3 build on each other (lanes → cards) but Phase 4-6 are independent
- After Phase 1, parallel work is possible: one track for list view (2→3), another for detail view (4→5), another for notifications (6)
- All templates use Tailwind + brand color system from base.html
- All Alpine state is inline in partials (no separate JS files)
- All server interactions via HTMX attributes (no fetch() calls)
