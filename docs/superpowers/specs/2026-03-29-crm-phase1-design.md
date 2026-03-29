# CRM Phase 1: Shell + Customer Sales Workspace

**Created:** 2026-03-29
**Status:** Approved (revised after architect, frontend, simplify, and data model reviews)
**Parent:** [CRM Master Roadmap](2026-03-29-crm-master-roadmap.md)

## Goal

Replace the separate Vendors, Customers, and My Vendors bottom nav tabs with a single CRM tab. Inside it, provide a Customers | Vendors tab bar. Add staleness indicators to the customer list so salespeople instantly see which accounts need attention.

## Navigation Changes

### Bottom Nav

Remove 3 tabs:
- Vendors
- Customers
- My Vendors

Add 1 tab:
- **CRM** (net reduction of 2 tabs, from 10 to 8)

Remaining bottom nav: Reqs, Sightings, Search, Buy Plans, CRM, Proactive, Quotes, Prospect

**File:** `app/templates/htmx/partials/shared/mobile_nav.html` (NOT `navigation/` — that directory doesn't exist)

Required changes:
- Remove Vendors, Customers, My Vendors entries
- Add CRM entry pointing to `/v2/crm` with partial `/v2/partials/crm/shell`
- Update the `urlToNav` JavaScript map: remove `/v2/vendors`, `/v2/customers`, `/v2/my-vendors`; add `'/v2/crm': 'crm'`
- Also map `/v2/customers` and `/v2/vendors` to `'crm'` in `urlToNav` so back-navigation from detail views highlights the CRM tab

### CRM Tab Bar

Top tab bar inside CRM: **Customers** | **Vendors**

- Default tab: Customers for all users in Phase 1 (role-based defaults deferred — one click to switch is fine)
- No localStorage persistence in Phase 1 (deferred)
- Each tab loads its own HTMX partial independently — no shared state

**Tab bar style:** Use Pattern C (Settings-style) from `settings/index.html` — `bg-brand-50` fill on active tab with `rounded-t-lg`. This distinguishes the shell-level tabs from inner list-level tabs (like "My Vendors" toggle inside the vendor list).

**Inner content target:** Tab buttons use `hx-target="#crm-tab-content"` (NOT `#main-content`). The shell template defines a `<div id="crm-tab-content">` container below the tab bar. This prevents tab switches from blowing away the shell.

**Initial tab load:** Follow the `materials/detail.html` pattern — default tab uses `hx-trigger="load, click once"`, other tab uses `hx-trigger="click once"`. Loading spinner placeholder inside `#crm-tab-content` until first partial arrives.

### v2_page Dispatcher

`app/routers/htmx_views.py` must register `/v2/crm` in the `v2_page` route decorator stack (lines 148-172) and add a `crm` branch in the `current_view` logic (lines 179-232) that sets `partial_url = "/v2/partials/crm/shell"` and `current_view = "crm"`.

## Customer List with Staleness

### Staleness Tiers

Thresholds as Python constants (configurable via SystemConfig deferred to later):

```python
STALENESS_OVERDUE_DAYS = 30
STALENESS_DUE_SOON_DAYS = 14
```

| Tier | Dot Color | Badge Style | Condition |
|------|-----------|-------------|-----------|
| Overdue | `bg-rose-500` | `bg-rose-50 text-rose-700` | 30+ days since last interaction |
| Due Soon | `bg-amber-400` | `bg-amber-50 text-amber-700` | 14-30 days since last interaction |
| Recent | `bg-emerald-400` | `bg-emerald-50 text-emerald-700` | Within 14 days |
| New | `bg-brand-300` | `bg-brand-100 text-brand-600` | No interaction history (`last_activity_at` is NULL) |

Colors match the existing toast/badge semantic system (rose=error, amber=warning, emerald=success, brand=info).

### Data Source

Use `Company.last_activity_at` directly — **no ActivityLog JOIN needed**.

This field is already maintained by `app/services/activity_service.py` via `_update_last_activity()` for:
- Emails (inbound + outbound via Graph API)
- Phone calls (8x8 integration + manual logging)
- Manual notes
- Site contact notes

**Known gap:** `last_activity_at` is NOT updated for RFQ sends, quote status changes, or proactive match events. This is acceptable for Phase 1 — the primary cadence signals for customer relationships are calls, emails, and notes. Phase 2 (Interaction Intelligence) will close these gaps.

**Staleness calculation:** Simple Python per-row after the query returns:

```python
from datetime import datetime, timezone

def staleness_tier(last_activity_at: datetime | None) -> str:
    if last_activity_at is None:
        return "new"
    days = (datetime.now(timezone.utc) - last_activity_at).days
    if days >= STALENESS_OVERDUE_DAYS:
        return "overdue"
    if days >= STALENESS_DUE_SOON_DAYS:
        return "due_soon"
    return "recent"
```

### List Layout

Modify the **existing** `app/templates/htmx/partials/customers/list.html` — do NOT create a parallel template. Add the staleness column and change default sort.

Table columns:
1. **Staleness dot** — `w-3 h-3 rounded-full` colored dot, first column
2. **Company** — name, clickable to detail (`hx-get="/v2/partials/customers/{{ c.id }}"`)
3. **Account Type** — badge using existing `type_colors` dict (emerald/brand/rose)
4. **Industry**
5. **Owner** — account owner name
6. **Sites** — count
7. **Last Contact** — use existing `|timeago` Jinja2 filter, with `"Never"` fallback for NULL

Default sort: `Company.last_activity_at.asc().nullsfirst()` (most overdue first, never-contacted at top).

Existing search and filters remain functional.

### Owner Scoping

Deferred to Phase 1b. The full list with Owner column is sufficient. Salespeople can visually scan for their accounts. Adding "My Accounts" toggle later follows the same pattern as the vendor list's "My Vendors" toggle (`?my_only=1`).

## Vendor Tab (Phase 1 — Minimal)

The CRM shell's Vendors tab calls the existing vendor list endpoint directly via HTMX:

```html
<button hx-get="/v2/partials/vendors"
        hx-target="#crm-tab-content"
        ...>Vendors</button>
```

**No proxy route needed.** The existing `/v2/partials/vendors` endpoint already supports search, sorting, My Vendors toggle, blacklist filtering.

**Known issue:** The existing vendor list template has hardcoded `hx-target="#main-content"` and `hx-push-url="/v2/vendors"` on its search, pagination, and sort interactions. When rendered inside the CRM shell's `#crm-tab-content`, these interactions will break out of the shell.

**Fix required:** Parameterize the vendor list template's `hx-target` and `hx-push-url` values. Pass `hx_target` and `push_url_base` as context variables from the route handler, defaulting to `#main-content` and `/v2/vendors`. When loaded inside CRM, the shell passes `hx_target="#crm-tab-content"` and `push_url_base="/v2/crm"` as query parameters that the vendor list route forwards to the template.

The same parameterization should be applied to the customer list template for consistency (its search/pagination should also stay within `#crm-tab-content`).

## Technical Architecture

### New Files

**`app/routers/crm/views.py`** — CRM shell views (cannot be `crm.py` because `app/routers/crm/` is already a package)
- `GET /v2/partials/crm/shell` — renders CRM shell with tab bar
- Wired into `app/routers/crm/__init__.py` alongside existing sub-routers

**`app/templates/htmx/partials/crm/shell.html`** — CRM page layout
- Tab bar (Customers | Vendors) using Pattern C style
- `#crm-tab-content` container for tab partials
- Default tab loads on page render via `hx-trigger="load, click once"`
- Loading spinner placeholder

**`tests/test_crm_views.py`** — route tests
- `GET /v2/partials/crm/shell` returns 200 with HTML
- Verify tab bar renders both Customers and Vendors buttons
- Verify default tab content loads

### Modified Files

- `app/templates/htmx/partials/shared/mobile_nav.html` — replace 3 tabs with 1 CRM tab, update `urlToNav`
- `app/routers/crm/__init__.py` — include new `views` sub-router
- `app/routers/htmx_views.py` — add `/v2/crm` to `v2_page` decorator and dispatcher
- `app/templates/htmx/partials/customers/list.html` — add staleness dot column, change default sort, use `|timeago` for Last Contact
- `app/routers/htmx_views.py` (`companies_list_partial`) — change default sort to `last_activity_at ASC NULLS FIRST`, compute staleness tier per row, pass to template
- `app/templates/htmx/partials/vendors/list.html` — parameterize `hx-target` and `hx-push-url` values
- `app/routers/htmx_views.py` (`vendors_list_partial`) — accept and forward `hx_target`/`push_url_base` context

### Unchanged

- Customer detail view (stays in htmx_views.py)
- Vendor detail view (stays in htmx_views.py)
- No model changes, no migrations
- No new Python dependencies

### Performance

- No query changes to customer list (just sort order change)
- Staleness tier computed in Python from already-loaded `last_activity_at` field
- No JOIN, no GROUP BY, no subquery
- No background jobs, no caching layer needed

## What This Does NOT Include

- Role-based tab defaults (deferred — everyone starts on Customers)
- localStorage tab persistence (deferred)
- "My Accounts" owner filter (deferred to Phase 1b)
- SystemConfig threshold tuning (deferred — constants for now)
- Interaction intelligence engine (Phase 2)
- Vendor discovery rethink (Phase 3)
- Performance dashboards (Phase 4)
- Any changes to customer or vendor detail views
- New models or migrations
- Teams/8x8 integration
