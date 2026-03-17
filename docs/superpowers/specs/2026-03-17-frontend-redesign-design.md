# Frontend Redesign — Full Cleanup & Visual Overhaul

**Date:** 2026-03-17
**Status:** Approved
**Approach:** Big bang cleanup + design system first, then page-by-page rebuild

## Goals

- Delete all legacy/duplicate frontend code (single template system, single routing pattern)
- Unified design system: dense data-forward, clean modern, enterprise polish
- Consistent spacing, typography, and color usage across all pages
- Reduce unnecessary scrolling through compact layouts
- Rename Companies → Customers
- Bright, fresh, clean look with smart highlight/accent colors

## Architecture Overview

### Current State

The frontend has three routing layers and two template trees:

- **`app/routers/htmx_views.py`** — 198 routes, serves `/v2/*` pages. This is the PRIMARY router.
- **`app/routers/htmx/*.py`** — 13 sub-modules (136 routes) sharing one router with NO prefix. These handle domain-specific partials and actions (requisitions, vendors, sourcing, etc.).
- **`app/routers/requisitions2.py`** — 9 routes under `/requisitions2` prefix. LEGACY, being replaced.
- **`app/templates/htmx/partials/`** — 54 templates (newer HTMX system)
- **`app/templates/partials/`** — 122 templates (older system, still actively referenced by htmx routers)

### Target State

- **One routing system:** `htmx_views.py` for page shells + `htmx/*.py` modules for domain partials/actions. All under `/v2` prefix.
- **One template tree:** `app/templates/htmx/` — all partials consolidated here.
- **No requisitions2:** Fully replaced by `/v2/requisitions` + parts workspace.

## Phase 1: Code Cleanup

### Step 1a: Delete Dead Code (safe, no dependencies)

- `app/templates/requisitions2/` — entire directory
- `app/routers/requisitions2.py` — standalone router (remove from `main.py` include_router)
- `app/static/js/requisitions2.js` — standalone JS
- Dockerfile `RUN cp -r app/static/js/ app/static/dist/js/` line
- `tests/test_requisitions2_*.py` — all requisitions2 test files
- Any redirects from `/requisitions2` → update to redirect to `/v2/requisitions`

### Step 1b: Consolidate Templates

The `app/templates/partials/` tree is NOT dead — it's actively referenced by htmx routers. Move templates into `app/templates/htmx/partials/`, updating all router `TemplateResponse` paths:

**Templates to move (by domain):**

| From `partials/` | To `htmx/partials/` | Referenced by |
|-------------------|---------------------|---------------|
| `admin/` (2) | `admin/` | htmx_views.py |
| `companies/` (8) | `customers/` (rename) | htmx/companies.py, htmx_views.py |
| `emails/` (4) | `emails/` | htmx_views.py |
| `follow_ups/` (2) | `follow_ups/` | htmx_views.py |
| `knowledge/` (1) | `knowledge/` | htmx_views.py |
| `materials/` (2) | `materials/` (merge with existing 2) | htmx_views.py |
| `offers/` (2) | `offers/` | htmx_views.py |
| `proactive/` (4) | `proactive/` (merge with existing 2) | htmx_views.py |
| `prospecting/` (1) | `prospecting/` (merge with existing 3) | htmx_views.py |
| `quotes/` (2) | `quotes/` (merge with existing 3) | htmx_views.py |
| `requisitions/` (21) | `requisitions/` (merge with existing 3) | htmx/requisitions.py, htmx_views.py |
| `search/` (3) | `search/` (merge with existing 3, dedupe) | htmx/sourcing.py, htmx_views.py |
| `shared/` (16) | `shared/` (merge with existing 2) | all routers |
| `sourcing/` (7) | `sourcing/` | htmx/sourcing.py, htmx_views.py |
| `vendors/` (8) | `vendors/` (merge with existing 6) | htmx/vendors.py, htmx_views.py |

**3 true duplicates** (search/form.html, search/results.html, search/lead_detail.html) — keep the htmx/partials version, delete the partials version.

After all moves, delete `app/templates/partials/` entirely.

### Step 1c: Rename Companies → Customers

- Nav labels in `htmx/base.html` and `htmx/base_page.html`
- Route URLs: `/v2/companies` → `/v2/customers` (add 302 redirect from old URL)
- Router module: `htmx/companies.py` → `htmx/customers.py`
- Template directory: `htmx/partials/companies/` → `htmx/partials/customers/`
- All template references in routers
- Test files: update URLs in `test_htmx_company_vendor_crud.py`, `test_htmx_core_pages.py`, etc.

### Step 1d: Route Prefix Consolidation

The `htmx/*.py` sub-modules currently have NO prefix. They need `/v2` added:

- Update the shared router in `_helpers.py`: `router = APIRouter(prefix="/v2", tags=["htmx-views"])`
- OR add prefix when including in `main.py`: `app.include_router(htmx_router, prefix="/v2")`
- Audit all 136 routes in htmx/*.py to ensure no path conflicts with htmx_views.py
- Update all test URLs accordingly

### Keep (do NOT delete)

- `/api/` routes that serve as JSON API for connectors, external integrations, mobile
- Vite build pipeline as-is
- HTMX + Alpine.js architecture
- All backend services, models, schemas
- `app/templates/documents/` (quote_report.html, rfq_summary.html — PDF generation)

## Phase 2: Design System

### Implementation

Design system lives in:
- `app/static/styles.css` — Tailwind `@layer components` with reusable classes
- `tailwind.config.js` — updated theme (fonts, spacing scale, colors)
- `app/templates/htmx/base.html` — Google Fonts `<link>` for Inter

### Typography

- **Font:** Inter (replaces Manrope/DM Sans/IBM Plex Mono)
- Update `tailwind.config.js` fontFamily:
  ```js
  fontFamily: {
    sans: ['Inter', 'system-ui', 'sans-serif'],
    mono: ['IBM Plex Mono', 'Menlo', 'monospace'],  // keep mono
  }
  ```
- Remove `font-display` and `font-body` class usage from templates (use default `font-sans`)
- **Scale (5 sizes only):**
  - `text-xs` (11px) — badges, metadata, timestamps
  - `text-sm` (13px) — table cells, form labels, secondary text
  - `text-base` (14px) — body text, nav items, inputs
  - `text-lg` (16px) — section headings, card titles
  - `text-xl` (20px) — page titles only
- **Line height:** 1.3–1.4 across the board
- **Weights:** 400 (normal), 500 (medium for labels), 600 (semibold for headings)

### Spacing Tokens

Compact system — ~30% tighter than current:

| Element | Current | New |
|---------|---------|-----|
| Page padding | `px-6 py-6` | `px-4 py-3` |
| Table row height | 48px | 36px |
| Card padding | `p-4` / `p-6` | `p-3` |
| Section gaps | `space-y-6` | `space-y-3` |
| Form field gaps | `gap-4` | `gap-2` |
| Sidebar collapsed | 64px | 56px |
| Sidebar expanded | 256px | 220px |

### Color System

**Brand blue (keep):** `#3d6895` — primary actions, active nav, links

**Neutrals:** gray-50 through gray-900 for backgrounds, borders, text

**Status highlights:**

| Color | Hex | Usage |
|-------|-----|-------|
| Green | `#16a34a` | Won, complete, active, success |
| Amber | `#d97706` | Pending, in-progress, warning |
| Red | `#dc2626` | Lost, overdue, error, urgent |
| Blue | `#2563eb` | Info, new, sourcing |
| Purple | `#7c3aed` | AI-generated, enriched, special |

**Background:** White cards on gray-50 page background
**Borders:** gray-200 default, brand-200 for active/focused elements

### Core Components (defined as Tailwind @apply classes in styles.css)

**Data table:**
- Compact rows (36px), sticky header
- Alternating row tint on hover, no heavy borders
- Subtle gray-100 dividers between rows
- Column-aligned text

**Badge:**
- Pill-shaped, `text-xs px-2 py-0.5`
- Solid background, white text
- Colored by status (see highlight colors above)

**Button:**
- Primary: brand blue bg, white text
- Secondary: gray outline
- Danger: red
- Default size: `text-sm px-3 py-1.5`

**Card:**
- White bg, 1px gray-200 border, rounded-lg, `p-3`
- No shadows except on hover

**Modal / Drawer:**
- Detail views: right-side drawer, 50% width desktop, full width mobile
- Confirmations: small centered modal (400px max)

**Form inputs:**
- Compact: `py-1.5 px-2.5 text-sm`
- Gray-200 border, brand-blue focus ring

**Tabs:**
- Underline style, compact, `text-sm`

**Empty state:**
- Centered icon + message + action button, subtle gray

## Phase 3: App Shell Redesign

Rebuild `app/templates/htmx/base.html` and `htmx/base_page.html`.

### Sidebar (left, fixed)

- Dark brand-700 background
- Logo at top, 40px height
- Nav sections with uppercase gray-400 labels:
  - **OPPORTUNITY:** Requisitions, Part Search, Proactive
  - **RELATIONSHIPS:** Vendors, Customers, Quotes
  - **SYSTEM:** Prospecting, Strategic, Settings
- Additional pages (Buy Plans, Materials, Follow-ups, Knowledge, Emails, Admin, Activity) accessible via secondary nav or sub-menus under their parent sections
- Nav items: icon + label, `text-sm`, 32px row height, rounded-md hover
- Active item: brand-900 bg, white text, 2px left accent bar (brand-400)
- Collapsed: 56px wide, icons only, tooltip on hover
- Bottom: user avatar circle + name + role badge

### Topbar (sticky, 48px)

- Left: breadcrumb trail (`text-sm`, gray-500 separators)
- Center: global search input, `Cmd+K` shortcut badge
- Right: notification bell + user dropdown
- Bottom border: 1px gray-200

### Main Content Area

- `max-w-full` — no max-width constraint (data apps need full width)
- Page header: title + subtitle + action buttons on one line
- Content starts immediately below with minimal gap

### Mobile

- **< 1024px:** Sidebar → hamburger menu (slide-over drawer)
- **< 1024px:** Bottom nav: 5 items (Requisitions, Search, Vendors, Customers, Settings)
- **< 1024px:** Topbar: hamburger + logo + search icon
- **< 768px:** Tables → card layout (responsive transformation)

## Phase 4: Page Rebuilds

All pages follow consistent pattern: **header bar → filters → data table/content**

### Page Header Pattern (single row, 40px)

```
[Page Title]  [count]                              [Filters] [+ New Button]
```

### Full Page List (all pages to rebuild)

**Primary pages (in sidebar nav):**

| Page | Layout | Key Columns/Content |
|------|--------|-------------------|
| Requisitions | Split-panel: list left, detail right. Resizable divider. Bulk action bar. | ID, Part#, Customer, Status, Qty, Assigned, Updated |
| Part Search | Prominent search bar + results table | Part#, Vendor, Qty, Price, Source badge, Lead Time |
| Proactive | Card-based, 2-3 per row | Match cards with metrics + actions |
| Vendors | Compact table → drawer detail with tabs | Name, Contact, Email, Phone, Status, Last Activity |
| Customers | Same as Vendors layout | Name, Contact, Email, Phone, Status, Last Activity |
| Quotes | Table with status badges | Quote#, Customer, Vendor, Part#, Qty, Price, Status, Date |
| Prospecting | Card-based | Prospect cards with metrics |
| Strategic | Card-based | Vendor relationship cards |
| Settings | Vertical tabs left, content right | Profile, Sources, Data Ops, System |

**Secondary pages (accessible but not primary nav):**

| Page | Layout | Notes |
|------|--------|-------|
| Buy Plans | Table → detail drawer | Plan list with status tracking |
| Materials | Table → detail drawer | Material card list with enrichment status |
| Follow-ups | Table | Pending follow-up actions |
| Knowledge | Table | Knowledge base entries |
| Emails | Thread viewer | Email thread display + reply |
| Admin | Settings-style tabs | API health, data ops, imports |
| Activity | Table/timeline | Activity log entries |

**Status badge colors (consistent across all pages):**
- Draft/New: gray
- Sent/In Progress: blue
- Won/Complete/Active: green
- Lost/Expired/Overdue: red
- Pending/Warning: amber
- AI/Enriched: purple

## Testing & Rollout

### Test Strategy

- Backend tests unchanged — no service/model/API changes
- **Phase 1 tests:**
  - Delete `tests/test_requisitions2_*.py`
  - Update all `test_htmx_*.py` files for new URL paths (companies → customers)
  - Update template path assertions where partials moved
- **Phase 2 tests:** No test changes (CSS-only)
- **Phase 3 tests:** Update `test_htmx_core_pages.py` for new shell structure
- **Phase 4 tests:** Update page-specific assertions as each page is rebuilt
- Add smoke tests: each page loads, returns 200, contains expected elements

### Rollout

- Each phase is a separate commit/PR
- Phase 1 deploys first — verify nothing breaks
- Phase 2 is CSS/config only — low risk
- Phase 3 — all pages get new shell immediately
- Phase 4 — page by page as each is done

### Rollback

- Git revert on any phase
- No database changes — pure frontend
