# Phase 4: Page Rebuilds — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking. After each task, run the `simplify` skill to review changed code for reuse, quality, and efficiency. Use the `frontend-design` skill when rebuilding each page template for high-quality, production-grade UI output.

**Goal:** Rebuild all page templates against the design system for consistent, compact, dense data-forward layouts.

**Architecture:** Each page is a Jinja2 partial in `app/templates/htmx/partials/`. Pages are loaded into the `#main-content` area of the shell via HTMX. Every page follows the pattern: page header (40px) → filters → data table or card grid. The router (`htmx_views.py`) stays unchanged — we only rewrite templates.

**Tech Stack:** Jinja2, HTMX 2.0, Alpine.js 3.15, Tailwind CSS

**Depends on:** Phase 3 complete (shell redesigned)

---

### Task 1: Create Shared Page Header Partial

**Files:**
- Create: `app/templates/htmx/partials/shared/page_header.html`

- [ ] **Step 1: Create reusable page header component**

Every page uses the same header pattern. Create a reusable include:

```html
{# page_header.html — Reusable page header bar.
   Called by: all list page partials
   Params: title, subtitle (optional), count (optional), actions_slot (block)
#}
<div class="flex items-center justify-between h-10 mb-3">
  <div class="flex items-center gap-3">
    <h1 class="page-title">{{ title }}</h1>
    {% if count is defined and count is not none %}
    <span class="text-sm text-gray-400">({{ count }})</span>
    {% endif %}
    {% if subtitle %}
    <span class="page-subtitle">{{ subtitle }}</span>
    {% endif %}
  </div>
  <div class="flex items-center gap-2">
    {% block actions %}{% endblock %}
    {{ caller() if caller is defined else '' }}
  </div>
</div>
```

- [ ] **Step 2: Commit**

```bash
git add app/templates/htmx/partials/shared/page_header.html
git commit -m "design: add reusable page header partial"
```

---

### Task 2: Rebuild Requisitions Page (Split-Panel)

**Files:**
- Modify: `app/templates/htmx/partials/requisitions/list.html`
- Modify: `app/templates/htmx/partials/parts/workspace.html`
- Modify: `app/templates/htmx/partials/parts/list.html`
- Modify: `app/templates/htmx/partials/requisitions/detail.html`

- [ ] **Step 1: Rebuild requisitions list template**

Use the `frontend-design` skill to rebuild `list.html`:
- Page header: "Requisitions" + count + [+ New] button
- Filter bar: status pills, assignee dropdown, search input — all inline, compact
- Data table with `.data-table` class
- Columns: ID, Part#, Customer, Status (badge), Qty, Assigned, Updated
- 36px row height, sortable columns
- Checkbox column for bulk selection
- Bulk action bar: appears above table when rows selected

- [ ] **Step 2: Rebuild parts workspace (split panel)**

Update `workspace.html`:
- Left panel: requisitions list (resizable)
- Right panel: detail view with tabs
- Resizable divider (keep existing Alpine.js logic)
- Compact styling matching design system

- [ ] **Step 3: Rebuild detail view**

Update `detail.html` and tab partials:
- Header: Part# + status badge + action buttons on one line
- Tabs: Overview, Sourcing, Offers, Comms, Activity — underline style
- Each tab content uses compact spacing
- Use `.card-padded` for info sections within tabs

- [ ] **Step 4: Run tests and commit**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_views.py tests/test_htmx_core_pages.py -v --tb=short
git add app/templates/htmx/partials/requisitions/ app/templates/htmx/partials/parts/
git commit -m "pages: rebuild requisitions — split-panel, compact table, tabbed detail"
```

---

### Task 3: Rebuild Part Search Page

**Files:**
- Modify: `app/templates/htmx/partials/search/form.html`
- Modify: `app/templates/htmx/partials/search/results.html`
- Modify: `app/templates/htmx/partials/search/lead_detail.html`

- [ ] **Step 1: Rebuild search form**

Use `frontend-design` skill:
- Large search input at top (full width, prominent)
- Source filter checkboxes inline below
- Recent searches dropdown

- [ ] **Step 2: Rebuild search results**

- Data table: Part#, Vendor, Qty, Price, Source (color badge), Lead Time, Actions
- Source badges use `.source-badge-*` classes
- Row click → drawer with lead detail
- "Add to Requisition" button per row

- [ ] **Step 3: Rebuild lead detail**

- Header: Part# + vendor + source badge
- Pricing info in compact grid
- Action buttons: Add to Req, Create RFQ
- Related results section

- [ ] **Step 4: Test and commit**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_search_cards.py tests/test_htmx_sourcing.py -v --tb=short
git add app/templates/htmx/partials/search/ app/templates/htmx/partials/sourcing/
git commit -m "pages: rebuild part search — prominent input, compact results, source badges"
```

---

### Task 4: Rebuild Vendors Page

**Files:**
- Modify: `app/templates/htmx/partials/vendors/list.html`
- Modify: `app/templates/htmx/partials/vendors/detail.html`
- Modify: `app/templates/htmx/partials/vendors/*.html` (tab partials)

- [ ] **Step 1: Rebuild vendor list**

Use `frontend-design` skill:
- Page header: "Vendors" + count + [+ Add Vendor] button
- Compact data table: Name, Primary Contact, Email, Phone, Status badge, Last Activity
- Filter row: search + status dropdown
- Row click → drawer detail

- [ ] **Step 2: Rebuild vendor detail (drawer)**

- Header: vendor name + status badge + edit button
- Tabs: Overview, Contacts, Quotes, Activity — underline style
- Overview tab: key info in compact 2-column grid
- Contacts tab: compact table of vendor contacts

- [ ] **Step 3: Test and commit**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_vendor_contact_crud.py tests/test_htmx_vendor_contacts.py -v --tb=short
git add app/templates/htmx/partials/vendors/
git commit -m "pages: rebuild vendors — compact table, tabbed drawer detail"
```

---

### Task 5: Rebuild Customers Page

**Files:**
- Modify: `app/templates/htmx/partials/customers/list.html`
- Modify: `app/templates/htmx/partials/customers/detail.html`
- Modify: `app/templates/htmx/partials/customers/*.html` (tab/form partials)

- [ ] **Step 1: Rebuild customer list**

Same layout pattern as vendors:
- Page header: "Customers" + count + [+ Add Customer] button
- Compact data table: Name, Contact, Email, Phone, Status badge, Last Activity
- Row click → drawer detail

- [ ] **Step 2: Rebuild customer detail (drawer)**

- Header: company name + status + edit
- Tabs: Overview, Sites, Contacts, Activity
- Site cards in compact grid
- Contact notes in timeline format

- [ ] **Step 3: Test and commit**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_company_vendor_crud.py -v --tb=short
git add app/templates/htmx/partials/customers/
git commit -m "pages: rebuild customers — compact table, tabbed drawer detail"
```

---

### Task 6: Rebuild Quotes Page

**Files:**
- Modify: `app/templates/htmx/partials/quotes/list.html`
- Modify: `app/templates/htmx/partials/quotes/detail.html`
- Modify: `app/templates/htmx/partials/quotes/line_row.html`

- [ ] **Step 1: Rebuild quotes list**

Use `frontend-design` skill:
- Page header: "Quotes" + count + [+ New Quote] button
- Data table: Quote#, Customer, Vendor, Part#, Qty, Unit Price, Total, Status badge, Date
- Status badges: Draft (gray), Sent (blue), Won (green), Lost (red), Expired (amber)

- [ ] **Step 2: Rebuild quote detail**

- Header: Quote# + customer name + status badge
- Line items table (editable)
- Pricing summary section
- Action buttons: Send, Clone, Convert

- [ ] **Step 3: Test and commit**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_offers_quotes.py -v --tb=short
git add app/templates/htmx/partials/quotes/
git commit -m "pages: rebuild quotes — status badges, editable line items"
```

---

### Task 7: Rebuild Proactive / Prospecting / Strategic Pages

**Files:**
- Modify: `app/templates/htmx/partials/proactive/list.html`
- Modify: `app/templates/htmx/partials/proactive/_match_card.html`
- Modify: `app/templates/htmx/partials/prospecting/list.html`
- Modify: `app/templates/htmx/partials/prospecting/detail.html`
- Modify: `app/templates/htmx/partials/prospecting/_card.html`
- Modify: `app/templates/htmx/partials/strategic/list.html`
- Modify: `app/templates/htmx/partials/strategic/_vendor_card.html`

- [ ] **Step 1: Rebuild proactive match list**

Card-based layout:
- Page header: "Proactive Matches" + count
- Cards: 2-3 per row, compact (`.card-padded`)
- Each card: part# + match info + confidence badge + action buttons
- Filter: confidence threshold, date range

- [ ] **Step 2: Rebuild prospecting list**

Card-based layout:
- Page header: "Prospecting" + count
- Prospect cards with key metrics
- Score badge (green/amber/red based on score)
- Quick actions: Email, Call, Add to Sequence

- [ ] **Step 3: Rebuild strategic vendors**

Card-based layout:
- Page header: "Strategic Vendors" + count
- Vendor relationship cards
- Metrics: total spend, response rate, lead time avg
- Action: View Detail, Compare

- [ ] **Step 4: Test and commit**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_proactive_strategic.py -v --tb=short
git add app/templates/htmx/partials/proactive/ app/templates/htmx/partials/prospecting/ app/templates/htmx/partials/strategic/
git commit -m "pages: rebuild proactive/prospecting/strategic — card layouts, compact metrics"
```

---

### Task 8: Rebuild Settings Page

**Files:**
- Modify: `app/templates/htmx/partials/settings/index.html`
- Modify: `app/templates/htmx/partials/settings/profile.html`
- Modify: `app/templates/htmx/partials/settings/sources.html`
- Modify: `app/templates/htmx/partials/settings/system.html`
- Modify: `app/templates/htmx/partials/settings/data_ops.html`

- [ ] **Step 1: Rebuild settings layout**

Use `frontend-design` skill:
- Vertical tabs on left (200px sidebar within content area)
- Content area on right
- Tabs: Profile, Sources, Data Ops, System
- Compact form layout: label above input, tight spacing (`gap-2`)
- Use `.input-field` class for all inputs
- Save buttons at bottom of each section

- [ ] **Step 2: Test and commit**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_core_pages.py -v --tb=short
git add app/templates/htmx/partials/settings/
git commit -m "pages: rebuild settings — vertical tabs, compact forms"
```

---

### Task 9: Rebuild Secondary Pages

**Files:**
- Modify: `app/templates/htmx/partials/buy_plans/list.html`
- Modify: `app/templates/htmx/partials/buy_plans/detail.html`
- Modify: `app/templates/htmx/partials/materials/list.html`
- Modify: `app/templates/htmx/partials/materials/detail.html`
- Modify: `app/templates/htmx/partials/follow_ups/list.html`
- Modify: `app/templates/htmx/partials/knowledge/list.html`
- Modify: `app/templates/htmx/partials/emails/*.html`
- Modify: `app/templates/htmx/partials/admin/*.html`

- [ ] **Step 1: Rebuild buy plans (table → detail)**

Same pattern as requisitions but simpler — table with status tracking, detail drawer.

- [ ] **Step 2: Rebuild materials (table → detail)**

Material card list with enrichment status badges. Detail drawer with metadata.

- [ ] **Step 3: Rebuild follow-ups, knowledge, emails, admin**

Each follows the standard table pattern with design system components.

- [ ] **Step 4: Test and commit**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short 2>&1 | tail -30
git add app/templates/htmx/partials/
git commit -m "pages: rebuild secondary pages — buy plans, materials, follow-ups, knowledge, emails, admin"
```

---

### Task 10: Final Verification & Deploy

- [ ] **Step 1: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
```

Expected: All tests pass, no coverage regression.

- [ ] **Step 2: Build frontend**

```bash
cd /root/availai && npm run build 2>&1 | tail -10
```

- [ ] **Step 3: Run simplify skill on all changed templates**

Use `simplify` skill to review all modified templates for consistency, reuse opportunities, and quality.

- [ ] **Step 4: Deploy**

```bash
git push origin main
docker compose up -d --build
docker compose logs app --tail 10
```

- [ ] **Step 5: Full visual smoke test**

Test every page in browser:
- [ ] `/v2/requisitions` — split panel, table, detail drawer
- [ ] `/v2/search` — search bar, results table, source badges
- [ ] `/v2/proactive` — card layout, match cards
- [ ] `/v2/vendors` — table, drawer detail with tabs
- [ ] `/v2/customers` — table, drawer detail with tabs
- [ ] `/v2/quotes` — table, status badges, line items
- [ ] `/v2/prospecting` — card layout
- [ ] `/v2/strategic` — card layout
- [ ] `/v2/settings` — vertical tabs, forms
- [ ] `/v2/buy-plans` — table, detail
- [ ] `/v2/materials` — table, detail
- [ ] Mobile responsive: resize to 768px, verify bottom nav + card layouts
