# CRM Phase 1: Shell + Customer Sales Workspace

**Created:** 2026-03-29
**Status:** Approved
**Parent:** [CRM Master Roadmap](2026-03-29-crm-master-roadmap.md)

## Goal

Replace the separate Vendors, Customers, and My Vendors bottom nav tabs with a single CRM tab. Inside it, provide a Customers | Vendors tab bar with role-based defaults. Redesign the customer list with visual staleness indicators so salespeople can instantly see which accounts need attention.

## Navigation Changes

### Bottom Nav

Remove 3 tabs:
- Vendors
- Customers
- My Vendors

Add 1 tab:
- **CRM** (net reduction of 2 tabs)

Remaining bottom nav: Reqs, Sightings, Search, Buy Plans, CRM, Proactive, Quotes, Prospect

### CRM Tab Bar

Top tab bar inside CRM: **Customers** | **Vendors**

- Role-based default: users with role `sales` or `account_manager` land on Customers; all other roles (`admin`, `sourcing`, `operations`) land on Vendors
- Tab selection persists in `localStorage` key `crm_default_tab` (role default only on first-ever visit)
- Each tab loads its own HTMX partial independently — no shared state

## Customer List with Staleness

### Staleness Tiers

Thresholds configurable via `SystemConfig`:

| Tier | Color | Condition | Meaning |
|------|-------|-----------|---------|
| Overdue | Red | 30+ days since last interaction | Needs immediate attention |
| Due Soon | Amber | 14-30 days since last interaction | Approaching stale |
| Recent | Green | Within 14 days | Healthy |
| New | Blue | No interaction history | Never contacted |

### Data Source

Staleness is computed from existing data — no new tracking needed for Phase 1:
- `ActivityLog` records (emails, RFQs, quotes, proactive offers)
- `Company.last_activity_at` (verify it updates on all interaction types)
- Graph email monitoring (already parsing replies and logging activity)

### List Layout

Table format with columns:
1. **Staleness indicator** — colored dot/badge, first visual element
2. **Company** — name, clickable to detail
3. **Account Type** — Customer/Prospect/Partner/Competitor badge
4. **Industry**
5. **Owner** — account owner name
6. **Sites** — count
7. **Last Contact** — relative time ("3 days ago", "2 weeks ago", "Never")

Default sort: staleness descending (most overdue first).

Existing search and filters remain functional.

### Owner Scoping

- Salespeople: "My Accounts" filter on by default, can toggle to see all
- Admins/managers: full list with owner column, no default filter

## Vendor Tab (Phase 1 — Minimal)

Existing vendor list renders inside the CRM Vendors tab. No feature changes:
- "My Vendors" toggle stays inline
- Search, filtering, card/table view all preserved
- Detail view navigation unchanged

The only change is entry point: CRM → Vendors tab instead of dedicated bottom nav tab.

## Technical Architecture

### New Files

**`app/routers/crm.py`** — dedicated CRM router
- `GET /v2/crm` — full page shell (tab bar + content container)
- `GET /v2/partials/crm/customers` — customer list with staleness
- `GET /v2/partials/crm/vendors` — renders existing vendor list partial

**`app/templates/htmx/partials/crm/shell.html`** — CRM page layout
- Tab bar (Customers | Vendors)
- Alpine.js for tab state + localStorage persistence
- HTMX lazy-loads the active tab partial

**`app/templates/htmx/partials/crm/customer_list.html`** — redesigned customer list
- Staleness indicators
- Owner-scoped filtering
- Staleness-sorted table

### Modified Files

- `app/templates/htmx/partials/navigation/mobile_nav.html` — replace 3 tabs with 1 CRM tab
- `app/main.py` — register `crm_router`

### Unchanged

- Customer detail view (stays in htmx_views.py)
- Vendor detail view (stays in htmx_views.py)
- Vendor list logic (reused, rendered inside CRM shell)
- No model changes, no migrations

### Staleness Query

Computed server-side per request:

```sql
SELECT c.*, MAX(al.created_at) AS last_interaction
FROM companies c
LEFT JOIN activity_logs al ON al.company_id = c.id
GROUP BY c.id
ORDER BY last_interaction ASC NULLS FIRST
```

Staleness tier derived from `now() - last_interaction` vs configurable thresholds.

Verify index exists on `activity_logs.company_id`; add if missing.

### Performance

- One additional JOIN + GROUP BY vs current simple company query
- Existing index on `activity_logs.company_id` should be sufficient
- No background jobs, no caching layer needed at this scale

## What This Does NOT Include

- Interaction intelligence engine (Phase 2)
- Vendor discovery rethink (Phase 3)
- Performance dashboards (Phase 4)
- Any changes to customer or vendor detail views
- New models or migrations
- Teams/8x8 integration
