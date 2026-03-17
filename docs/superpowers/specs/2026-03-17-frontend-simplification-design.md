# Frontend Simplification Design

**Date:** 2026-03-17
**Status:** Approved
**Stack:** HTMX + Alpine.js (no React)

## Goal

Simplify and finish the AvailAI frontend: stabilize tests, decompose the monolithic router, build a reusable component library, and rebuild the 4 MVP pages with a consistent split-screen list/detail pattern.

## Decisions

- **Stay on HTMX + Alpine.js** — no React migration
- **Keep all current pages** — focus polish on 4 MVP pillars, leave others as-is
- **Break up `htmx_views.py`** — split 7,388-line monolith into domain routers
- **Split-screen for 3 pages** — Requisitions, Customers, Vendors get list/detail panels
- **Search stays full-width** — no split-screen
- **Component library** — 13 reusable Jinja2 macro partials

## Phase 1: Stabilize

Fix 54 remaining test failures from the MVP strip-down. Rules:
- Referencing deleted feature → delete the test
- Testing MVP-gated endpoint → add `skipif` for `MVP_MODE`
- HTML element removed → delete the test
- Pre-existing bug → fix assertion

Commit all Phase 1 changes. Green test suite = baseline.

## Phase 2: Router Decomposition

Split `app/routers/htmx_views.py` (198 routes, 7,388 lines) into `app/routers/views/` package:

| File | Domain | Routes |
|------|--------|--------|
| `_common.py` | Shared utilities (`_vite_assets`, `_base_ctx`, `_is_htmx`, `_timesince_filter`, helpers) | 0 |
| `__init__.py` | Page-level catch-all (`/v2/*` → `base.html`), imports all sub-routers | ~20 |
| `requisitions.py` | `/v2/partials/requisitions/*` | 38 |
| `quotes.py` | `/v2/partials/quotes/*` | 16 |
| `vendors.py` | `/v2/partials/vendors/*` | 14 |
| `companies.py` | `/v2/partials/companies/*` | 14 |
| `search.py` | `/v2/partials/search/*`, `/v2/partials/parts/*` | 14 |
| `buy_plans.py` | `/v2/partials/buy-plans/*` | 10 |
| `sourcing.py` | `/v2/partials/sourcing/*` | 9 |
| `secondary.py` | Everything else (proactive, prospecting, settings, admin, strategic, follow-ups, materials, etc.) | ~83 |

Update `main.py` to mount the new package. Delete `htmx_views.py`. Tests must stay green after each extraction.

## Phase 3: Component Library

Build 13 reusable Jinja2 macro components in `app/templates/htmx/components/`:

### Foundation (build first)
| Component | Purpose |
|-----------|---------|
| `split_screen.html` | Two-panel shell with resizable divider (left list, right detail). Alpine.js handles resize drag. |
| `data_table.html` | Sortable, filterable table with bulk select checkboxes, pagination, HTMX row-click. |
| `detail_panel.html` | Right-side detail view: header area, tab bar, content area, action bar. |

### Core
| Component | Purpose |
|-----------|---------|
| `action_bar.html` | Top bar with title, search input, filter dropdowns, bulk action buttons. |
| `tabs.html` | HTMX-powered tab switcher — clicking a tab fetches its partial. |
| `modal.html` | Slide-over or centered modal with HTMX form submission. |
| `form_field.html` | Labeled input with validation error display. |

### Supporting
| Component | Purpose |
|-----------|---------|
| `card.html` | Generic content card for grid layouts. |
| `empty_state.html` | "No results" placeholder with icon and call-to-action. |
| `toast.html` | Flash notification (success/error/info). |
| `badge.html` | Status badges (open, closed, quoted, sourcing, etc.). |
| `pagination.html` | Page controls with HTMX navigation. |
| `confirm_dialog.html` | "Are you sure?" confirmation before destructive actions. |

### Usage Pattern

Each component is a Jinja2 macro:

```jinja2
{% from "htmx/components/data_table.html" import data_table %}
{{ data_table(
    columns=["MPN", "Qty", "Status"],
    rows=requisitions,
    hx_target="#detail-panel",
    row_click_url="/v2/partials/requisitions/{id}"
) }}
```

Alpine.js handles local interactivity (resize, collapse, selection state). HTMX handles server round-trips.

## Phase 4: Page Rebuilds

### Requisitions (`/v2/requisitions`) — Split-Screen
- **Left panel**: `data_table` of requisitions (MPN, customer, status, date, qty). Filterable by status. Bulk select for close/archive.
- **Right panel**: `detail_panel` with selected requisition. Header (customer, status badge, dates). Tabs: Requirements, Offers, Sourcing Results, Quotes. Inline edit for key fields.
- **Default**: Most recent requisition auto-selected on load.

### Customers (`/v2/companies`) — Split-Screen
- **Left panel**: `data_table` of companies (name, site count, contact count, last activity). Search box filters instantly.
- **Right panel**: `detail_panel` with company detail. Header (name, address). Tabs: Sites & Contacts, Requisition History, Quotes, Activity.

### Vendors (`/v2/vendors`) — Split-Screen
- **Left panel**: `data_table` of vendors (name, reliability score, specialties, last contact). Search + filter by specialty.
- **Right panel**: `detail_panel` with vendor detail. Header (name, score badge). Tabs: Contacts, Offer History, Pricing Trends, Notes.

### Search (`/v2/search`) — Full-Width
- `action_bar` with search input at top.
- `data_table` of results with connector source badges.
- Click a result to expand inline details (no split-screen).

### All Other Pages
Keep current layouts unchanged. No modifications to Buy Plans, Prospecting, Proactive, Strategic, Settings, Quotes, etc.

## Phasing & Deployability

Each phase is independently deployable:

1. **Phase 1** → tests green, MVP strip-down committed
2. **Phase 2** → same functionality, cleaner code structure
3. **Phase 3** → components exist but aren't used yet (no user-visible change)
4. **Phase 4** → new page layouts using components, old partials deleted

## Out of Scope

- React migration
- New database models or API endpoints
- Changes to non-MVP pages
- Mobile-specific layouts (existing mobile CSS stays)
- Authentication changes
