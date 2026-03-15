# HTMX Frontend Rebuild — Full Design Spec

**Date:** 2026-03-15
**Goal:** Rebuild the entire HTMX + Alpine.js frontend to match design documents, replace CDN loading with Vite pipeline, implement brand color system derived from the AVAIL logo, and build all missing UI components including sourcing engine views and prospecting.

**Current state:** `USE_HTMX` is already `True` in production — the current HTMX frontend is live but incomplete. This rebuild replaces the current HTMX templates in-place. No feature flag gating needed; the rebuild overwrites existing `/v2` templates and adds missing ones. The old vanilla JS frontend (`app.js` / `crm.js`) has already been removed.

**References:**
- Phase 3 plan: `docs/superpowers/plans/2026-03-13-phase3-frontend-rewrite.md`
- Wireframe spec: `docs/sourcing-engine-handoff/00_wireframe_spec.md`
- MVP strip-down: `docs/superpowers/specs/2026-03-13-mvp-strip-down-design.md`
- UX principles: `~/.claude/projects/-root/memory/feedback_ux_principles.md`

---

## 1. Brand Color System

### Source

Colors extracted from the AVAIL logo (`app/static/public/avail_logo.png`):
- Primary steel blue: `#3d6895` (dark letters)
- Secondary slate: `#8b9daf` (lighter gradient letters)
- Charcoal text: `#505050` (subtitle)

### Custom Tailwind Palette

Defined in `tailwind.config.js` as `brand`:

| Token | Hex | Use |
|-------|-----|-----|
| `brand-50` | `#f0f4f8` | Page backgrounds, subtle fills |
| `brand-100` | `#dce4ed` | Badge backgrounds, hover states |
| `brand-200` | `#b7c7d8` | Borders, dividers |
| `brand-300` | `#8b9daf` | Secondary text, muted labels |
| `brand-400` | `#6a8bad` | Placeholder text, icons |
| `brand-500` | `#3d6895` | Primary buttons, links, active nav |
| `brand-600` | `#345a82` | Button hover, active states |
| `brand-700` | `#2b4c6e` | Sidebar background |
| `brand-800` | `#1e3a56` | Sidebar hover, dark accents |
| `brand-900` | `#142a40` | Sidebar selected state |

### Status Colors (Semantic, One Meaning Each)

| Status | Background | Text | Used For |
|--------|-----------|------|----------|
| Success | `emerald-50` | `emerald-700` | Active, approved, verified, won, customer |
| Warning | `amber-50` | `amber-700` | Pending, hot, awaiting |
| Danger | `rose-50` | `rose-700` | Lost, rejected, critical, blacklisted, competitor |
| Info | `brand-100` | `brand-600` | Draft, sourcing, in progress, prospect |
| Neutral | `gray-100` | `gray-600` | Archived, cancelled, unknown |

### Source Badges (Distinct Hues, No Status Conflicts)

| Source | Color Family | Reason |
|--------|-------------|--------|
| BrokerBin | `sky` | Distinct from brand-blue |
| Nexar | `violet` | Unique, no status conflict |
| DigiKey | `orange` | Not confused with "danger" |
| Mouser | `teal` | Not confused with "success" |
| OEMSecrets | `fuchsia` | Distinct |
| Element14 | `lime` | Distinct |
| eBay | `yellow` | Marketplace feel |

### Color Rules

- **Total tokens: ~20** (down from 38 in current implementation)
- Every color has exactly one semantic job
- Gray palette reduced to 4 shades: `gray-50` (backgrounds), `gray-200` (borders), `gray-500` (secondary text), `gray-900` (body text)
- No inline CSS hex colors — everything through Tailwind config
- Margin indicators: emerald ≥30%, amber ≥15%, rose otherwise

---

## 2. App Shell

### Logo Usage

- **Sidebar header:** `avail_logo.png` (transparent bg), scaled ~180px wide, replacing text "AvailAI"
- **Login page:** Same logo, centered, larger
- **Mobile header:** Compact — logo only, no subtitle
- **Favicon/PWA:** Keep existing `icon-192.png` / `icon-512.png`
- Logo files: `app/static/public/avail_logo.png` (dark bg), `app/static/public/avail_logo_white_bg.png` (light bg)

### Sidebar

- Background: `brand-700` (replaces `gray-900`)
- **Collapsible** via toggle button:
  - Expanded: icon + label, logo visible
  - Collapsed: icon only, logo hidden or reduced to mark
- 9 navigation items with SVG icons:
  - **Active:** Requisitions, Part Search, Buy Plans, Vendors, Companies, Prospecting, Quotes, Settings
- Section label "Relationships" between Buy Plans and Vendors
- User avatar + name + email at bottom with logout button
- Active item: `brand-900` background, white text
- Hover: `brand-800` background
- All links use `hx-get` targeting `#main-content` with `hx-push-url`

### Topbar (New)

- **Left:** Breadcrumb trail (e.g., "Requisitions > REQ-2024-0142"). **Mechanism:** Each partial includes a `<div id="breadcrumb" hx-swap-oob="true">...</div>` element for out-of-band swap — when HTMX loads any partial, the breadcrumb updates automatically.
- **Center/Right:** Global search input
  - `hx-get="/v2/partials/search/global"` with `hx-trigger="keyup changed delay:300ms"`
  - Searches across requisitions, companies, vendors
  - Results dropdown grouped by type
  - Each result row: `hx-get` to relevant detail partial, `hx-target="#main-content"`, `hx-push-url` for history. Clicking a result also closes the dropdown.
- **Far right:** Notification bell (count badge) + user avatar dropdown (profile, settings, logout)
- Styling: White background, `brand-200` bottom border, fixed position

### Mobile

- **Top bar:** Hamburger button + compact AVAIL logo
- **Sidebar:** Slides in as overlay drawer from left (same content, brand colors)
- **Bottom nav bar:** 5 items with icons — Requisitions, Search, Buy Plans, Vendors, Companies
  - Active item highlighted with `brand-500`
  - Safe area insets for notched devices
- Sidebar closes on nav item click

### Shared Components (Reusable Partials)

All in `app/templates/partials/shared/`:

**`modal.html`:**
- Alpine `x-data` controls open/close
- `@open-modal.window` / `@close-modal.window` event listeners
- `@keydown.escape` closes
- Backdrop click closes (`@click.self`)
- `#modal-content` div as HTMX swap target for body content
- `x-trap.noscroll` for focus trapping
- Full-screen on mobile via media query

**`toast.html`:**
- Driven by Alpine store: `$store.toast.message`, `$store.toast.type`, `$store.toast.show`
- Auto-dismiss after 4 seconds
- Color-coded: success (emerald), error (rose), info (brand)
- Fixed position, top-right on desktop, top-center on mobile
- Smooth enter/exit transitions via `x-transition`

**`pagination.html`:**
- Receives template vars: `page`, `total_pages`, `target_id`, `url`
- Prev/Next buttons with `hx-get`
- Page count display: "Page X of Y"
- Disabled state on first/last page

**`empty_state.html`:**
- Receives: `message`, optional `action_url`, optional `action_label`
- Gray icon (SVG), message text, optional CTA button
- Centered in white card

---

## 3. Requisitions

### List View

**File:** `app/templates/partials/requisitions/list.html`

- Search input: live filtering with 300ms debounce, targets table body
- **Filter bar:**
  - Quick filter pills: All, Open, Awarded, Archived — each sends `hx-get` with `status` param
  - Owner dropdown (visible to buyers/admins, hidden from sales role): filter by creator
  - Urgency filter: Normal / Hot / Critical toggle pills
  - Date range: `date_from` and `date_to` date picker inputs
  - All filters use `hx-include="#req-filters"` to preserve state across sort/pagination interactions
  - All filter/sort/pagination interactions use `hx-push-url` so users can bookmark/share filtered views
- **Sortable column headers:** Click sends `hx-get` with `sort` and `dir` params, visual indicator (arrow) on active sort column
- Table columns: Name, Customer, Owner, Parts count, Offer count, Status badge, Urgency badge, Created date
- **Bulk operations:**
  - Checkbox column on each row + select-all checkbox in header
  - Alpine `x-data="{ selectedIds: new Set() }"` manages selection state
  - Bulk action bar appears when items selected: Archive, Assign (with owner dropdown), Activate
  - `hx-post="/v2/partials/requisitions/bulk/{action}"` with comma-separated IDs
  - Max 200 per bulk action
- Clickable rows: `hx-get` to detail, `hx-push-url` for history
- **Role-based visibility:** Sales role users see only their own requisitions (enforced server-side via `user_reqs_query()` in dependencies.py). Owner filter hidden for sales role.
- **"New Requisition" button:** Opens modal via `@click="$dispatch('open-modal')"` + `hx-get` to load create form into `#modal-content`
- Pagination via `{% include "partials/shared/pagination.html" %}`, supports configurable `per_page` (default 50, max 100)

**Row partial:** `app/templates/partials/requisitions/req_row.html`
- Single `<tr>` receiving `req` template var
- Used for: initial render, new row after create (prepend), search/filter results

### Create Modal

**File:** `app/templates/partials/requisitions/create_modal.html`

- Form fields: Name (required), Customer, Deadline, Urgency dropdown, Parts textarea (one part per line, format: `MPN, Qty` e.g. `LM317T, 500` — qty defaults to 1 if omitted)
- `hx-post` to create endpoint
- On success: `$dispatch('close-modal')`, toast "Requisition created", new row prepended to table
- Cancel button dispatches `close-modal`

### Detail View — Tabbed

**File:** `app/templates/partials/requisitions/detail.html`

- Breadcrumb: "Requisitions > {req.name}"
- Header card: Name, customer, due date, created by, status badge, urgency badge, edit button
- **Tabs** (Alpine `x-data` for active tab, HTMX loads tab content on click):
  - **Parts** (default): Requirements table
    - Columns: MPN (monospace), Brand, Qty (formatted), Target Price, Status badge, Sightings count
    - "Search" action button per row (triggers sourcing, shows spinner)
    - Inline "Add requirement" form at top: MPN, Qty, Manufacturer, Add button
  - **Offers**: Table of offers received — columns: Vendor, MPN, Qty, Unit Price, Lead Time, Date Received, Status badge. Empty state: "No offers received yet."
  - **Quotes**: Table of quotes generated — columns: Quote #, Customer, Total, Margin %, Status badge, Created date. Empty state: "No quotes generated."
  - **Buy Plans**: Table of linked buy plans — columns: Buy Plan #, SO#, Status badge, Lines count, Total Cost, Created date. Clickable rows → buy plan detail. Empty state: "No buy plans linked."
  - **Tasks**: Task board for this requisition (backend: `RequisitionTask` model, `routers/task.py`)
    - Filter buttons: All, To Do, In Progress, Done
    - Task list: each task shows type badge (sourcing/sales/general), priority indicator, title, assignee avatar, due date, AI risk flag (if set)
    - Inline "Add task" form: title, type dropdown, priority, assignee dropdown, due date
    - Quick actions per task: complete (checkbox), delete (x button)
    - AI priority score shown as subtle indicator if > 0
    - Empty state: "No tasks yet. Add a task to track work on this requisition."
  - **Activity**: Timeline of actions (searches, emails, status changes) — each entry: timestamp, user avatar, action description. Newest first.
- Each tab loads via `hx-get="/v2/partials/requisitions/{id}/tab/{tab_name}"` into tab content area

---

## 4. Companies

### List View

**File:** `app/templates/partials/companies/list.html`

- Search by name/domain (live, 300ms debounce)
- Table columns: Company name + domain (with initial avatar circle), Account Type badge, Industry, Owner name, Sites count, Open Reqs count
- Account type badges: Customer=emerald, Prospect=brand, Partner=brand-300, Competitor=rose
- Clickable rows → detail view
- Pagination

### Detail View

**File:** `app/templates/partials/companies/detail.html`

- Breadcrumb: "Companies > {company.name}"
- Header card: Company name, domain, industry, city, account type badge
- Quick info grid (4 cols): Account Owner, Credit Terms, Phone, Employees
- Stats row (3 cols): Sites count, Open Requisitions count, Created date
- **Enrich button:** `hx-post="/api/enrich/company/{company_id}"` with spinner indicator. Uses `partials/shared/enrich_button.html` shared component. Refreshes company detail on completion.
- **Tabs:** Sites, Contacts, Requisitions, Activity
- Sites table: Site Name, Type, City, Country
- Contacts tab: Name, Title, Email, Phone — with **click-to-call** (same pattern as vendor contacts: `tel:` links, 8x8 activity logging if enabled)
- Notes section (if present): preformatted text

---

## 5. Vendors

### List View

**File:** `app/templates/partials/vendors/list.html`

- Search by name (live, 300ms debounce)
- **Card grid** (3-col desktop, 2-col tablet, 1-col mobile)
- Blacklisted vendors **are shown** in the list (not filtered out) but visually distinct — rose-tinted card background, blacklisted badge prominent. A "Hide blacklisted" toggle filter is available at the top.
- Each card:
  - Vendor name + domain (truncated)
  - Score badge (emerald) OR blacklisted badge (rose)
  - 3-stat mini grid: Sightings count, Win Rate %, Location
  - Industry text (small, muted, truncated)
  - Entire card clickable → detail
  - `hover:shadow-md` transition
- Pagination

### Detail View

**File:** `app/templates/partials/vendors/detail.html`

- Breadcrumb: "Vendors > {vendor.name}"
- Header card: Vendor name, domain, city/country, industry, score (large) or blacklisted badge
- 4-stat row: Sightings, Win Rate, Total POs, Avg Response Time (white cards)
- **Safety review block** (reusable partial `partials/shared/safety_review.html`):
  - Safety band indicator with color (emerald=low risk, amber=medium, rose=high)
  - Summary text, positive signals list (checkmarks), caution signals list (warnings), recommended buyer action
  - **Data source for vendor detail:** Safety fields (`vendor_safety_score`, `vendor_safety_band`, `vendor_safety_summary`, `vendor_safety_flags`) live on `SourcingLead`, not `VendorCard`. For vendor detail view, aggregate safety data from the vendor's most recent `SourcingLead` records. If no leads exist for this vendor, show "No safety data available — safety is assessed when sourcing leads are created."
  - **Data source for lead detail:** Read directly from the `SourcingLead` record.
- Contact info card: Website (link), Emails (list), Phones (list)
- Contacts table: Name, Title, Email, Phone
  - **Click-to-call:** Phone numbers render as `tel:` links. If user has `eight_by_eight_enabled`, show a phone icon button that logs a click-to-call activity event via `hx-post="/api/activity"` with `origin=click_to_call` before opening the tel link.
- **Enrich button:** `hx-post="/api/enrich/vendor/{card_id}"` with spinner indicator. Refreshes vendor detail on completion. Uses `partials/shared/enrich_button.html` shared component.
- **Tabs** (Alpine-driven, HTMX-loaded):
  - **Overview** (default): Safety review block + contact info + recent sightings table (MPN, Qty, Price, Source badge, Date)
  - **Contacts**: Full contacts table with click-to-call
  - **Analytics**: Vendor scorecard (backend: `vendor_analytics.py`)
    - Stats grid: Win Rate, Response Rate, Quote Quality Rate, Avg Response Hours, Engagement Score, Vendor Score
    - Offer history table (from `/api/vendors/{card_id}/offer-history`): Part, Qty, Price, Date, Source
    - Parts summary (from `/api/vendors/{card_id}/parts-summary`): MPN, Times Seen, Last Seen, Sources
    - Empty state: "No analytics data yet — data builds as you interact with this vendor."
  - **Offers**: Historical offers from this vendor — columns: MPN, Qty, Unit Price, Lead Time, Confidence, Date, Status badge. Sourced from `Offer` model filtered by vendor.

---

## 6. Buy Plans

### List View

**File:** `app/templates/partials/buy_plans/list.html`

- Status filter tabs (Alpine-driven): All, Draft, Pending, Active, Completed, Cancelled
- "My Only" toggle checkbox (Alpine + HTMX)
- Search by SO#, customer, quote number (live)
- Table columns: Customer, Quote #, SO#, Lines count, Cost, Margin %, Status badge, SO Verification badge, Submitted by, Date
- Margin color: emerald ≥30%, amber ≥15%, rose otherwise
- Clickable rows → detail

### Detail View

**File:** `app/templates/partials/buy_plans/detail.html`

- Breadcrumb: "Buy Plans > #{id}"
- Header: Buy Plan #ID, status badge, stock sale badge (if applicable)
- 6-stat card grid: Customer, Quote #, SO#, Total Cost, Revenue, Margin %
- SO Verification status with rejection note if present
- AI Summary box (`brand-50` background, if present)
- AI Flags list (severity color-coded: rose=critical, amber=warning, brand=info)
- Workflow action bar: Context-aware buttons based on status + user role (Submit, Approve, Reject, Finalize, Reset to Draft)

---

## 7. Part Search

**File:** `app/templates/partials/search/form.html` + `search/results.html`

- Large search input + "Search All Sources" button (disabled until text entered)
- Button shows spinner during search (`htmx-indicator`)
- Results table columns: Vendor, MPN, Manufacturer, Qty Available, Unit Price, Source badge, Lead Time
- Source badges use distinct hues (sky=BrokerBin, violet=Nexar, orange=DigiKey, teal=Mouser, fuchsia=OEMSecrets, lime=Element14, yellow=eBay)
- Price: "$X.XXXX" if known, "RFQ" if not
- Qty formatted with commas
- Search elapsed time displayed in results header
- Empty state: icon + "Enter a part number to search all sources"

---

## 8. Quotes & Offers

**Backend:** Fully implemented — `Quote` model (models/quotes.py), `Offer` model (models/offers.py), `QuoteLine` model, routes in `routers/crm/quotes.py` and `routers/crm/offers.py`, buy plan creation from quotes.

### Quotes List View

**File:** `app/templates/partials/quotes/list.html`

- Search by quote number, customer name (live, 300ms debounce)
- Filter pills: All, Draft, Sent, Won, Lost
- Table columns: Quote #, Revision, Requisition name, Customer, Total, Margin %, Status badge, Created date
- Margin color-coded: emerald ≥30%, amber ≥15%, rose otherwise
- Clickable rows → quote detail
- Pagination

### Quote Detail View

**File:** `app/templates/partials/quotes/detail.html`

- Breadcrumb: "Quotes > {quote.quote_number}"
- Header card: Quote number, revision number, status badge, customer, requisition link
- **Inline line item editing:**
  - Table: MPN, Manufacturer, Qty, Cost Price, Sell Price, Margin %, linked Offer (if any)
  - Double-click any cell to edit inline (`hx-trigger="dblclick"`, `hx-put` to update line item)
  - Auto-recalculates margin on price changes
  - Add line item row at bottom
  - Delete line via `hx-delete` with `hx-swap="delete"` (row removed from DOM)
- **Global markup input:** Apply markup % across all line items at once
- **Offer gallery:**
  - Offers for this requisition displayed as expandable cards
  - Each offer card: Vendor, MPN, Qty, Price, Lead Time, Confidence badge, Evidence tier badge
  - Expand to see: full offer details, attachments, parse confidence
  - "Select for Quote" action on each offer → adds as quote line item
  - Approval workflow: Approve / Reject buttons on pending offers
- **Quote actions bar:**
  - Send (generates email HTML, opens send modal)
  - Mark Result (Won/Lost with notes)
  - Revise (creates new revision, increments revision number)
  - Copy Table (copies line items to clipboard)
- **Followup alerts:** If `followup_alert_at` is set, show countdown/alert banner
- **Pricing history:** Show previous revision prices for comparison

### Offers (Standalone Access)

**File:** `app/templates/partials/offers/list.html`

- Accessed from requisition detail Offers tab (see Section 3)
- Also accessible as offer cards within quote detail
- Offer card component (`partials/shared/offer_card.html`):
  - Vendor name, MPN, Qty, Unit Price, Lead Time
  - Evidence tier badge (T1-T7)
  - Parse confidence indicator
  - Status badge (draft/pending_review/approved/promoted)
  - Attachments list (if any)
  - Expand/collapse for full details
  - Actions: Approve, Promote to Quote, Reject

---

## 9. Dashboard

**File:** `app/templates/partials/dashboard.html`

- AVAIL logo centered at top (large, brand moment)
- 3 stat cards in a row: Open Requisitions (count), Active Vendors (count), Companies (count)
- Each stat card: large number, label, subtle brand-100 background, clickable → navigates to respective list
- Quick actions card below: "Create Requisition" button, "Search Parts" button
- Welcome message: "Welcome back, {user_name}"

---

## 9. Sourcing Engine Views (Wireframe Spec)

### Sourcing Results View

**Entry point:** Requisition detail → "Search" button on a requirement row
**File:** `app/templates/partials/sourcing/results.html`

**Header:**
- Part number (monospace, prominent), manufacturer
- Run timestamp, total results count

**Filter bar:**
- Confidence: High / Medium / Low toggle pills
- Safety: Low Risk / Medium Risk / High Risk toggle pills
- Freshness: Last 24h / 7 days / 30 days / All
- Source: Checkboxes per connector (BrokerBin, Nexar, DigiKey, Mouser, OEMSecrets, Element14)
- Status: New / Contacted / Replied / All
- Contactability: Has Email / Has Phone / Any
- Corroborated: Yes / No / All
- All filters send `hx-get` with updated params, swap results area

**Sort dropdown:** Best Overall, Freshest, Safest, Easiest to Contact, Most Proven

**Lead cards** (not table rows — cards convey richer info):

Each card (`partials/sourcing/lead_card.html`):
- Vendor name + domain
- Confidence score: percentage + colored progress bar (emerald ≥70, amber ≥40, rose <40)
- Safety band badge (emerald=low risk, amber=medium, rose=high risk)
- Source badges (which connectors found this lead)
- Freshness indicator ("2 hours ago", "3 days ago" — relative time)
- Qty available + unit price (or "RFQ") — **data source:** these fields live on `Sighting`, not `SourcingLead`. Query the lead's linked sightings (via `requirement_id` + `vendor_name_normalized`) and display the best available qty and lowest price from the most recent sighting.
- Contact preview: first email or phone, truncated
- Corroboration badge: if `corroborated=True`, show "Corroborated ({evidence_count} signals)" in emerald
- **Suggested next action** text (derived from lead state + contact info availability):
  - Has email + new → "Send RFQ"
  - Has phone + no email → "Call vendor"
  - No contact info → "Research contact"
  - Already contacted → "Follow up"
- **Quick actions:** Claim, Dismiss, Send RFQ (inline buttons)
- Clicking card body → lead detail view

**Empty state:** "No leads found for this part. Try broadening your search or check back after the next sourcing run." with icon.

**Error state:** If search fails, show toast notification "Search failed — please try again" with retry button inline in the results area.

**Loading state — SSE streaming progress** (backend: `sse_broker.py`, `EventSourceResponse`):
- When search starts, connect via `hx-ext="sse"` with `sse-connect="/v2/partials/sourcing/{requirement_id}/stream"`
- Server sends SSE events as each connector completes: `event: source-complete`, `data: {"source": "BrokerBin", "count": 42, "elapsed_ms": 1200}`
- UI renders a progress partial (`partials/sourcing/search_progress.html`):
  - Per-source row: Source badge + status (searching.../done ✓/failed ✗) + result count + elapsed time
  - Overall progress bar showing completed/total sources
  - Results stream in as each source completes (prepended to results area via `hx-swap="afterbegin"`)
- On completion: SSE connection closes, progress panel collapses, full results visible
- Fallback: If SSE fails to connect, degrade to simple spinner with "Searching {N} sources..." text.

### Lead Detail View

**File:** `app/templates/partials/sourcing/lead_detail.html`

**Lead summary card:**
- Vendor name, part number, confidence %, safety band, buyer status badge, owner avatar

**Evidence list** (all `LeadEvidence` records):
- Signal type icon (stock listing, vendor history, web discovery, email signal, etc.)
- Source name + source badge (colored by connector)
- Explanation text (human-readable)
- Observed date + freshness ("3 days ago")
- Confidence impact: "+12%" or "+5%" showing contribution
- Verification state badge: raw (gray), inferred (brand), buyer_confirmed (emerald), rejected (rose)
- Sorted by confidence_impact descending

**Source attribution table:**
- Grouped by source category (API, Marketplace, History, Web/AI)
- Shows which connectors contributed what evidence

**Contact information:**
- Emails (list), phones (list), website link
- Pulled from associated vendor card

**Safety review block:**
- Same reusable `partials/shared/safety_review.html` component as vendor detail
- Safety band, summary, positive signals, caution signals, recommended action

**Buyer actions panel:**
- Status update dropdown: new → contacted → replied → has_stock / no_stock / bad_lead / do_not_contact
- Feedback note: text input + reason code dropdown (price_too_high, lead_time_too_long, wrong_part, no_response, etc.)
- Contact method selector: email / phone / LinkedIn
- "Send RFQ" button (pre-fills from lead data)
- All actions submit via `hx-post`, update lead card in results view via `hx-swap="outerHTML"` on the card

**Activity timeline:**
- All `LeadFeedbackEvent` records
- Each event: timestamp, user avatar, status change, note, contact method
- Append-only display, newest first

### Buyer Followup Queue

**File:** `app/templates/partials/sourcing/followup_queue.html`
**Access:** Tab within requisition detail OR standalone view

**Status tabs:** New, Contacted, Awaiting Response, Replied, All

**Queue table:**
- Vendor name
- Part number (monospace)
- Confidence % (with small colored bar)
- Safety band badge
- Last action + timestamp
- Days since last contact (amber if >3 days, rose if >7 days)
- Next suggested step (text)
- Owner (avatar)
- Sortable by: confidence, freshness, days-since-contact

**Behavior:**
- Clicking row → lead detail view
- Designed for rapid triage — buyer works down the list updating statuses
- Filters and sorts persist via URL params

---

## 10. Prospecting

### List View

**File:** `app/templates/partials/prospecting/list.html`

**Backend:** Routes exist in `app/routers/prospect_suggested.py` and `app/routers/prospect_pool.py`. Model: `ProspectAccount`.

- Search by company name/domain (live, 300ms debounce)
- Filter pills: All, Suggested, Claimed, Dismissed
- Sort dropdown: Best Fit, Most Ready, Newest
- **Card grid** (3-col desktop, 2-col tablet, 1-col mobile):
  - Company name + domain
  - Fit score: percentage + small colored bar (emerald ≥70, amber ≥40, rose <40)
  - Readiness score: same treatment
  - Industry + region text
  - Discovery source badge
  - Status badge (suggested=brand, claimed=emerald, dismissed=neutral)
  - Quick actions: Claim (brand button), Dismiss (neutral button)
- Pagination

### Detail View

**File:** `app/templates/partials/prospecting/detail.html`

- Breadcrumb: "Prospecting > {prospect.name}"
- Header card: Company name, domain, industry, region, status badge
- Fit + Readiness scores: large, prominent, with colored bars
- Discovery source + discovery date
- Claimed by (user avatar + name, if claimed)
- **Enrichment data card** (if available):
  - SAM.gov data
  - Google News mentions
  - Signal indicators (hiring, events, intent)
- Warm intro section (if warm intro data exists in `enrichment_data` JSONB under key `warm_intro` (singular), computed by `app/services/prospect_warm_intros.py`): intro path, suggested one-liner
- **Action buttons:**
  - Claim / Release
  - Dismiss
  - Enrich (triggers free enrichment via `hx-post`)
  - Create Requisition (pre-fills customer from prospect)

---

## 11. Settings

**Backend:** Admin config via `routers/admin/system.py`, source settings via `routers/sources.py`. No user preferences endpoint yet.

### Settings View

**File:** `app/templates/partials/settings/index.html`

- **Tabs** (Alpine-driven):
  - **Sources** (default): Data connector enable/disable toggles
    - List of all connectors: BrokerBin, Nexar, DigiKey, Mouser, OEMSecrets, Element14, eBay, Sourcengine, TME
    - Each row: Source name, status badge (active/disabled/error), toggle switch (`hx-post` to enable/disable)
    - Health indicator: last successful call timestamp, error message if failing
    - Requires `require_settings_access` permission
  - **System** (admin only):
    - Config key/value editor (from `system_config` table)
    - Encrypted values shown as masked
    - Edit via inline form, `hx-put` to update
    - Requires `require_admin` permission — tab hidden for non-admins
  - **Profile**:
    - User info display: name, email, role badge
    - 8x8 VoIP toggle (if applicable): enable/disable click-to-call
    - Read-only for now — no user preference editing yet (stub for future)

---

## 12. Vite Build Pipeline

### Dependencies

```bash
npm install htmx.org@^2 alpinejs@^3 @alpinejs/trap@^3 tailwindcss@^3 postcss autoprefixer
```

### Entry Point

**File:** `app/static/htmx_app.js`

```js
import htmx from 'htmx.org';
import Alpine from 'alpinejs';
import trap from '@alpinejs/trap';

Alpine.plugin(trap);

window.htmx = htmx;
window.Alpine = Alpine;

// Alpine stores
Alpine.store('sidebar', { open: true, collapsed: false, active: '' });
Alpine.store('toast', { message: '', type: 'info', show: false });

// HTMX config
htmx.config.defaultSwapStyle = 'innerHTML';
htmx.config.historyCacheSize = 0;
htmx.config.selfRequestsOnly = true;

// Error handling → toast
htmx.on('htmx:responseError', (evt) => {
    Alpine.store('toast').message = 'Request failed. Please try again.';
    Alpine.store('toast').type = 'error';
    Alpine.store('toast').show = true;
});

// 401 → redirect to login
document.body.addEventListener('htmx:beforeSwap', (evt) => {
    if (evt.detail.xhr.status === 401) {
        window.location.href = '/auth/login';
    }
});

Alpine.start();
```

### Tailwind Config

**File:** `tailwind.config.js`

```js
module.exports = {
  content: ['./app/templates/**/*.html'],
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#f0f4f8',
          100: '#dce4ed',
          200: '#b7c7d8',
          300: '#8b9daf',
          400: '#6a8bad',
          500: '#3d6895',
          600: '#345a82',
          700: '#2b4c6e',
          800: '#1e3a56',
          900: '#142a40',
        }
      }
    }
  },
  plugins: [],
}
```

### Vite Config

**File:** `vite.config.js` — add `htmx_app` entry point to `rollupOptions.input`, configure PostCSS with Tailwind.

### Build Output

- Built assets go to `app/static/dist/`
- Hashed filenames for cache busting
- Base template references built assets via Jinja2 `url_for('static', path='dist/...')`

### Current State

`use_htmx` is already `True` in production. The old vanilla JS frontend has been removed. This rebuild replaces the current HTMX templates in-place — no feature flag toggle needed. The Vite pipeline replaces the current CDN script tags.

---

## 13. HTMX View Router

**File:** `app/routers/htmx_views.py`

**Note:** Routes marked (EXISTING) already exist in `htmx_views.py` and need template updates. Routes marked (NEW) must be added.

### Full Page Endpoints (return base.html wrapping a partial)

- `GET /v2` → Dashboard (EXISTING — update template)
- `GET /v2/requisitions` → Requisitions list (EXISTING — update template)
- `GET /v2/requisitions/{id}` → Requisition detail (EXISTING — update template)
- `GET /v2/search` → Part search (EXISTING — update template)
- `GET /v2/vendors` → Vendors list (EXISTING — update template)
- `GET /v2/vendors/{id}` → Vendor detail (EXISTING — update template)
- `GET /v2/companies` → Companies list (EXISTING — update template)
- `GET /v2/companies/{id}` → Company detail (EXISTING — update template)
- `GET /v2/buy-plans` → Buy plans list (EXISTING — update template)
- `GET /v2/buy-plans/{id}` → Buy plan detail (EXISTING — update template)
- `GET /v2/quotes` → Quotes list (NEW)
- `GET /v2/quotes/{id}` → Quote detail (NEW)
- `GET /v2/settings` → Settings (NEW)
- `GET /v2/prospecting` → Prospecting list (NEW)
- `GET /v2/prospecting/{id}` → Prospect detail (NEW)
- `GET /v2/sourcing/{requirement_id}` → Sourcing results (NEW)
- `GET /v2/sourcing/leads/{lead_id}` → Lead detail (NEW)
- `GET /v2/sourcing/followup` → Buyer followup queue (NEW)

### Partial Endpoints (return just the partial, for HTMX swaps)

All prefixed `/v2/partials/`:

- `GET /partials/requisitions` — list (EXISTING — update template)
- `GET /partials/requisitions/{id}` — detail (EXISTING — update template)
- `GET /partials/requisitions/create-form` — create modal form (NEW)
- `POST /partials/requisitions/create` — create, return new row (EXISTING — update response)
- `POST /partials/requisitions/{id}/requirements` — add requirement (EXISTING — update response)
- `GET /partials/requisitions/{id}/tab/{tab}` — tab content: parts, offers, quotes, buy_plans, activity (NEW)
- `GET /partials/search` — search form (EXISTING)
- `POST /partials/search/run` — execute search, return results (EXISTING — update template)
- `GET /partials/search/global` — global search for topbar, return grouped results dropdown (NEW)
- `GET /partials/vendors` — list (EXISTING — update template)
- `GET /partials/vendors/{id}` — detail (EXISTING — update template)
- `GET /partials/companies` — list (EXISTING — update template)
- `GET /partials/companies/{id}` — detail (EXISTING — update template)
- `GET /partials/buy-plans` — list (EXISTING — update template)
- `GET /partials/buy-plans/{id}` — detail (EXISTING — update template)
- `GET /partials/quotes` — list (NEW)
- `GET /partials/quotes/{id}` — detail (NEW)
- `PUT /partials/quotes/{id}/lines/{line_id}` — inline edit line item (NEW)
- `DELETE /partials/quotes/{id}/lines/{line_id}` — delete line item (NEW)
- `POST /partials/quotes/{id}/send` — send quote (NEW)
- `POST /partials/quotes/{id}/result` — mark won/lost (NEW)
- `POST /partials/quotes/{id}/revise` — create revision (NEW)
- `GET /partials/settings` — settings page (NEW)
- `GET /partials/settings/sources` — sources tab (NEW)
- `GET /partials/settings/system` — system config tab, admin only (NEW)
- `GET /partials/settings/profile` — user profile tab (NEW)
- `POST /partials/requisitions/bulk/{action}` — bulk archive/assign/activate (NEW)
- `GET /partials/sourcing/{requirement_id}/stream` — SSE search progress (NEW)
- `GET /partials/prospecting` — list (NEW)
- `GET /partials/prospecting/{id}` — detail (NEW)
- `POST /partials/prospecting/{id}/claim` — claim prospect (NEW)
- `POST /partials/prospecting/{id}/dismiss` — dismiss prospect (NEW)
- `POST /partials/prospecting/{id}/enrich` — trigger enrichment (NEW)
- `GET /partials/sourcing/{requirement_id}` — sourcing results, params: confidence, safety, freshness, source, status, contactability, corroborated, sort, page (NEW)
- `GET /partials/sourcing/leads/{lead_id}` — lead detail (NEW)
- `POST /partials/sourcing/leads/{lead_id}/status` — update buyer status (NEW)
- `POST /partials/sourcing/leads/{lead_id}/feedback` — add feedback event (NEW)
- `GET /partials/sourcing/followup` — followup queue, params: status, sort, page (NEW)

### Helper Utilities

**File:** `app/dependencies.py` — these helpers already exist (lines 181, 186). No changes needed:
```python
def wants_html(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"

def is_htmx_boosted(request: Request) -> bool:
    return request.headers.get("HX-Boosted") == "true"
```

---

## 14. Template Directory Structure

```
app/templates/
├── htmx/
│   ├── base.html                    # Full app shell (sidebar, topbar, main content area) — the primary layout
│   ├── base_page.html               # Minimal wrapper for standalone pages (login, error pages) — no sidebar/topbar
│   └── login.html                   # Login page (with AVAIL logo)
├── partials/
│   ├── shared/
│   │   ├── sidebar.html             # Collapsible sidebar nav
│   │   ├── topbar.html              # Breadcrumb + search + user menu
│   │   ├── mobile_nav.html          # Bottom nav for mobile
│   │   ├── modal.html               # Reusable modal shell
│   │   ├── toast.html               # Toast notifications
│   │   ├── pagination.html          # HTMX pagination
│   │   ├── empty_state.html         # Empty state with icon + message
│   │   ├── safety_review.html       # Vendor safety review block (reused in vendor detail + lead detail)
│   │   ├── enrich_button.html       # Reusable enrich button (companies, vendors, prospects)
│   │   ├── offer_card.html          # Expandable offer card (quotes detail, req offers tab)
│   │   └── search_results.html      # Global search dropdown
│   ├── requisitions/
│   │   ├── list.html
│   │   ├── req_row.html
│   │   ├── detail.html
│   │   ├── create_modal.html
│   │   └── tabs/
│   │       ├── parts.html
│   │       ├── offers.html
│   │       ├── quotes.html
│   │       ├── buy_plans.html
│   │       ├── tasks.html
│   │       └── activity.html
│   ├── companies/
│   │   ├── list.html
│   │   └── detail.html
│   ├── vendors/
│   │   ├── list.html
│   │   └── detail.html
│   ├── buy_plans/
│   │   ├── list.html
│   │   └── detail.html
│   ├── search/
│   │   ├── form.html
│   │   └── results.html
│   ├── quotes/
│   │   ├── list.html
│   │   ├── detail.html
│   │   └── line_row.html           # Inline-editable quote line item
│   ├── sourcing/
│   │   ├── results.html
│   │   ├── lead_card.html
│   │   ├── lead_detail.html
│   │   ├── search_progress.html    # SSE progress during multi-source search
│   │   └── followup_queue.html
│   ├── prospecting/
│   │   ├── list.html
│   │   └── detail.html
│   ├── settings/
│   │   ├── index.html
│   │   ├── sources.html
│   │   ├── system.html
│   │   └── profile.html
│   └── dashboard.html
```

---

## 15. UX Principles (Enforced Throughout)

From user feedback, applied consistently:

- **Scannability:** Key data visible at a glance in every list/table/card. No hunting for information.
- **Flow efficiency:** Minimal clicks from list → detail → action. Breadcrumbs for quick navigation back.
- **Clean detail views:** Whitespace, clear hierarchy, no clutter. Cards group related info.
- **Tables for lists:** Dense but readable. Sortable where it matters.
- **Context-aware content:** Show what's relevant to the current view, not everything at once.
- **Crisp, bright, clean:** Brand palette (steel blue), white surfaces, minimal shadow, sharp borders.
- **Consistent patterns:** Every list view has search + filters + table/cards + pagination. Every detail view has breadcrumb + header card + content sections.

---

## 16. Mobile Responsive Design

**Breakpoints:**
- Mobile: < 768px
- Tablet: 768px - 1024px
- Desktop: > 1024px

**Mobile behaviors:**
- Sidebar hidden, accessible via hamburger drawer
- Bottom nav bar with 5 key items
- Tables convert to card layout below 768px (via `htmx_mobile.css`)
- 44px minimum touch targets
- Full-screen modals
- Safe area insets for notched devices

**File:** `app/static/htmx_mobile.css` — updated to use brand colors. This is a separate hand-written CSS file imported into the Vite entry point (`import './htmx_mobile.css'` in `htmx_app.js`). It uses raw CSS media queries, not Tailwind `@apply` — this is intentional since responsive table-to-card transformations are complex layout overrides that don't map cleanly to utility classes.

---

## 17. Verification Criteria

The rebuild is complete when:

1. All 13 page views render correctly (requisitions, companies, vendors, buy plans, quotes, search, sourcing results, lead detail, followup queue, prospecting, settings, dashboard, login)
2. AVAIL logo displays in sidebar, login, and mobile header
3. Brand color system applied consistently — no leftover `gray-900` sidebar, no `blue-600` buttons
4. All shared components work: modal (open/close/escape), toast (auto-dismiss), pagination, empty states, enrich button, offer card, safety review
5. Sidebar collapses/expands, persists state, all 9 nav items present
6. Topbar breadcrumb updates per page via OOB swap, global search returns grouped results
7. Mobile: hamburger drawer works, bottom nav works, tables → cards below 768px
8. Sourcing: SSE progress streams, filters work, lead cards display all data, lead detail shows evidence + safety + actions
9. Prospecting: list/detail renders, claim/dismiss/enrich actions work
10. Quotes: list/detail renders, inline line editing works, offer gallery works, send/result/revise actions work
11. Requisitions: all filters work (status, owner, urgency, date range), bulk operations work, role-based visibility enforced, all 6 tabs load (parts, offers, quotes, buy plans, tasks, activity)
12. Vendors: all 4 tabs work (overview, contacts, analytics, offers), click-to-call logs activity, enrich button works
13. Companies: contacts tab has click-to-call, enrich button works
14. Settings: sources tab shows connector status, system tab admin-only, profile tab renders
15. Vite builds successfully, no CDN dependencies remain
16. All existing backend tests still pass
17. New HTMX endpoint tests pass for all partial routes
