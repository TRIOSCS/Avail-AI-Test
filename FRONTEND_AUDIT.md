# AVAIL AI — Frontend Audit Report

> Generated: 2026-02-22 | Phase 1 of Frontend Stabilization Plan

---

## 1. Template Inventory

### Architecture: Single-Page Application (SPA)

AVAIL is a **monolithic SPA** — one Jinja2 template serves the entire authenticated experience. There is no `base.html` inheritance chain.

| Template | Lines | Purpose | Extends | Includes | JS | CSS |
|----------|-------|---------|---------|----------|-----|-----|
| `app/templates/index.html` | 1,039 | Main SPA shell (login + all views) | None (standalone) | None | `app.js`, `crm.js`, `html2canvas` (CDN) | `styles.css`, Google Fonts (DM Sans, JetBrains Mono) |
| `app/templates/documents/rfq_summary.html` | 81 | Print-friendly RFQ report (PDF) | None (standalone) | None | None | Inline `<style>` |
| `app/templates/documents/quote_report.html` | 87 | Print-friendly quote report (PDF) | None (standalone) | None | None | Inline `<style>` |

### index.html Structure

- **Login screen** (lines 16–28): Microsoft OAuth, conditional on `{% if not logged_in %}`
- **Top navigation / toparea** (lines 31–67): Logo, view pills (RFQ/Sourcing/Archive), search, notifications
- **Mobile UI** (lines 74–102): Topbar, action toolbar, hamburger menu
- **Sidebar** (lines 104–125): Nav buttons (RFQs, Customers, Vendors, Materials, Buy Plans, Scorecards, Settings)
- **Main content** (lines 128–1001): 8 view divs toggled by JS (`view-list`, `view-customers`, `view-vendors`, `view-materials`, `view-buyplans`, `view-proactive`, `view-performance`, `view-settings`)
- **30 modals** for workflows (Log Offer, New Requisition, Batch RFQ, Vendor Card, etc.)
- **Inline event handlers: 181** (157 onclick, 9 oninput, 8 onchange, 10 onkeydown)
- **Inline `style="..."` attributes: ~143** (mostly display toggles, layout utilities, sizing)
- **Server context variables**: `logged_in`, `user_name`, `user_email`, `is_admin`, `is_manager`, `is_dev_assistant`, `app_version`

---

## 2. CSS Audit (`app/static/styles.css` — 1,556 lines)

### Architecture: Flat Global with Light Component Prefixes

No BEM, no CSS modules, no namespace prefixes. Uses semantic prefixes (`.sc-*`, `.vp-*`, `.req-*`, `.act-*`, `.dd-*`) for component scoping but no formal convention. CSS custom properties (`:root`) provide a theming layer.

### Generic Selectors — Collision Risk

| Selector | Line | Scope | Risk |
|----------|------|-------|------|
| `.btn` | 79 | Global base class | HIGH — extended by 6 variants |
| `.card` | 88 | Global base class | HIGH — any new card risks collision |
| `.field` | 283 | Global form field | HIGH — `.modal .field` creates coupling |
| `.modal` | 278 | Global modal base | MEDIUM — well-scoped via `.modal-bg` |
| `.tabs` / `.tab` | 94 | Global tab base | MEDIUM — used across multiple views |
| `.tbl` | 140 | Global table base | MEDIUM — drill-down overrides via `!important` |
| `.empty` | — | Global empty state text | LOW — simple utility |

### `!important` Overrides — 16 Instances

**Coupling symptoms (14):**
- Lines 412, 539, 1118 — `.main` margin-left conflicts between sidebar state, mobile media query, and settings view
- Lines 753, 1241, 1252, 1253 — Drill-down detail rows force-override base `.tbl` styling
- Lines 179, 445 — `.sc` padding overridden twice in mobile media queries
- Lines 1264–1266 — Deadline row highlighting overrides table styles
- Lines 1315–1317 — Performance table row highlights

**Intentional (2):**
- Line 988 — `.hidden { display: none !important }` (utility)
- Line 1331 — `.u-hidden { display: none !important }` (utility)

### Media Query Inventory

| Line | Breakpoint | Target | Qualifier |
|------|-----------|--------|-----------|
| 407–557 | `768px` | Primary mobile layout | `(hover:none)/(pointer:coarse)` — touch only |
| 580–592 | `420px` | Extra-small phones | `(hover:none)/(pointer:coarse)` — touch only |
| 852–870 | `768px` | CRM forms, contacts, modals | None — all narrow screens |
| 963–969 | `768px` | AI panel mobile | None — all narrow screens |
| 972–982 | `769px–1024px` | Tablet landscape | `min-width` + `max-width` |
| 1430 | `640px` | Detail context layout | None — all narrow screens |
| 1483–1531 | `768px` | Drill-down tabs, mobile deep fixes | None — all narrow screens |

**Issue:** 5 separate `@media(max-width:768px)` blocks are fragmented across the file. Should be consolidated.

### z-index Stacking Context

| z-index | Element | Purpose |
|---------|---------|---------|
| 2 | `.col-headers` | Sticky table header |
| 5 | `.dd-search-overlay` | Drill-down search |
| 10 | `.site-typeahead-list` | Typeahead dropdown |
| 150 | `.mobile-toolbar` | Mobile action toolbar |
| 151 | `.mobile-topbar` | Mobile hamburger bar |
| 199 | `.sidebar-overlay` | Click-away backdrop |
| 200 | `.sidebar`, `.toparea` | Fixed layout anchors |
| 201 | `.sb-rail` | Sidebar toggle rail |
| 300 | `.filter-panel`, `.filter-dropdown` | Dropdown panels |
| 310 | `.notif-panel-standalone` | Notification panel |
| 500 | `.modal-bg` | Modal overlay |
| 510 | `.ai-panel-bg` | AI panel overlay |
| 600 | `.ac-dropdown` | Autocomplete dropdown |
| 9999 | `#toastContainer`, `.skip-link` | Critical notifications, accessibility |

**Issues:**
- Typeahead (10) is far below filter panels (300) — could be obscured
- Mobile topbar (151) and toolbar (150) z-index ordering is inverted (works because they have different `top` values, but confusing)

---

## 3. JavaScript Audit

### File Statistics

| File | Lines | Module-Level Vars | Window Exports | `getElementById` Calls | API Endpoints |
|------|-------|-------------------|---------------|----------------------|---------------|
| `app/static/app.js` | 8,233 | ~73 | ~110 functions | 414 | ~40+ |
| `app/static/crm.js` | 4,979 | ~43 | ~60 functions | 200+ | ~45+ |
| **Total** | **13,212** | **~116** | **~170 functions** | **614+** | **~85+** |

### Module System

Both files are **ES Modules** loaded via `<script type="module">`:
- `app.js` — exports 20+ functions via `export`, no imports
- `crm.js` — imports 16 functions from `app.js`, exports its own set
- Both use `Object.assign(window, {...})` to expose functions for inline `onclick` handlers

### Global Variables (~116 total)

**app.js (~73 vars)** organized by feature:
- Request/Search state (15): `currentReqId`, `_reqListData`, `_reqStatusFilter`, `_reqAbort`, `searchResults`, `_sightingIndex`, etc.
- Vendor state (10): `_vendorListData`, `_vendorTierFilter`, `rfqVendorData`, etc.
- Material/Part state (6): `_materialListData`, `rfqAllParts`, `rfqSubsMap`, etc.
- Quote/Offer state (8): `_ddSelectedOffers`, `_ddQuoteData`, `_offerHistoryOffset`, etc.
- UI state (15): `_currentViewId`, `_currentMainView`, `_viewScrollPos`, `expandedGroups`, `_modalStack`, etc.
- Caches (8+): `_ddReqCache`, `_ddSightingsCache`, `_ddTabCache`, `searchResultsCache`, etc.
- Debounced functions (9): `debouncedMainSearch`, `debouncedRenderReqTable`, etc.

**crm.js (~43 vars)** organized by feature:
- Customer/Company (8): `crmCustomers`, `_custSortCol`, `_custAbort`, etc.
- Offer/Quote (6): `_hasNewOffers`, `_offerStatusFilter`, `_pendingOfferFiles`, etc.
- Buy Plan (7): `_currentBuyPlan`, `_buyPlans`, `_bpFilter`, `_bpPollInterval`, etc.
- Admin/Config (5): `_userListCache`, `_adminUsers`, etc.
- Vendor/Scorecard (5+): `_perfVendorSort`, `_salesScorecardData`, etc.
- AI/Enrichment (5+): `_proactiveMatches`, `_aiPanelContext`, etc.

### Cross-File Dependencies

```
crm.js ──imports──> app.js
  ├── apiFetch, debounce, esc, escAttr, logCatchError
  ├── showToast, fmtDate, fmtDateTime, fmtRelative
  ├── openModal, closeModal, showView, sidebarNav, navHighlight
  ├── loadRequisitions, toggleDrillDown, notifyStatusChange
  └── openVendorPopup, loadVendorContacts, autoLogEmail
       initNameAutocomplete, guardBtn, refreshProactiveBadge

Shared state via window:
  ├── window.__userName, __userEmail, __isAdmin, __isManager, __isDevAssistant
  ├── window.userRole, window.__userId
  └── window.__errorBuffer
```

### Global Event Listeners (12 total)

**app.js (9):**
- `DOMContentLoaded` ×3 — init, auth check, autocomplete setup
- `click` ×2 — filter panel close, autocomplete selection
- `popstate` — browser back/forward
- `beforeunload` — cleanup M365 timer
- `keydown` — ESC to close modals
- `unhandledrejection` — catch unhandled promise errors
- `offline`/`online` — connectivity banner

**crm.js (3):**
- `DOMContentLoaded` ×2 — CRM init, gallery keyboard
- `click` ×1 — global delegate handler

**Cleanup:** Only M365 timer and notification click listener have explicit cleanup. All others persist for app lifetime (acceptable for SPA).

### Missing Null Checks — High-Risk Areas

~15–20% of `getElementById` calls lack null safety. Key examples:

1. **Log Offer form** (app.js ~line 3522): 15 sequential `.value` assignments, zero null checks
2. **Edit Offer form** (app.js ~line 1692): 12+ fields read without null check
3. **Dynamic IDs** (`ddSendEmail-${reqId}`, `ddNewEmail-${reqId}`): fragile if element missing
4. **crm.js company forms**: `ncName`, `ecId`, etc. — direct `.value` chaining
5. **Batch clear patterns**: `forEach(id => document.getElementById(id).value = '')` — breaks if any ID missing

**Safer patterns exist** but aren't consistent: `document.getElementById('reqListFilter')?.value || ''`

### API Endpoints Called (~85+ unique)

**app.js (~40+ endpoints):**
- Requisitions: 12 (CRUD, search, archive, clone, upload, RFQ send)
- Vendors: 15 (CRUD, contacts, activities, reviews, blacklist)
- Offers/Quotes: 10 (CRUD, send, result, buy plan)
- Materials: 7 (CRUD, import, pricing history)
- AI: 4 (draft, compare, normalize, parse)
- Notifications: 3 (list, read, read-all)
- Email: 3 (reply, thread, vendor emails)
- Other: auth status, error reports, follow-ups, proactive count, autocomplete

**crm.js (~45+ endpoints):**
- Companies/Sites: 12 (CRUD, typeahead, contacts)
- Buy Plans: 9 (approve, reject, complete, cancel, PO)
- Enrichment: 7 (backfill, scrape, queue, stats)
- Performance: 5 (vendors, buyers, sales scorecards)
- Proactive: 6 (matches, send, convert, dismiss)
- Admin: 12 (users, config, Teams, import, sources)
- Quotes: 9 (CRUD, send, result, revise)
- Activity: 4 (call, note, attribute, dismiss)

---

## 4. Vite Configuration

### `vite.config.js`

**Build Strategy:** Multi-entry bundling (3 independent entry points)

| Entry Point | Output | Size (minified) |
|-------------|--------|-----------------|
| `app.js` | `assets/app-Du8NYw8j.js` | 265 KB |
| `crm.js` | `assets/crm-BNfPAATu.js` | 164 KB |
| `styles.css` | `assets/styles-DlzexklN.css` | 95 KB |

- **Cache busting:** Content-hash in filenames (8 chars), resolved via `.vite/manifest.json`
- **Manifest:** `manifest: true` — server reads hashed paths at runtime
- **Root:** `app/static`
- **Base URL:** `/static/`
- **Dev server:** Port 5173, proxies `/api`, `/auth`, `/health` to `:8000`
- **Plugins:** None
- **No code splitting** beyond the 3 entry points — both JS bundles load on every page

### `app/vite.py` — Asset Injection

Three modes:
1. **`VITE_DEV=1`** — dev server client + raw entry points from `:5173`
2. **Production** — reads `manifest.json`, outputs hashed `<link>`/`<script>` tags
3. **Fallback** — raw source files with `?v={app_version}` cache bust

---

## 5. Dependency Map

```
┌─────────────────────────────────────────────────────────────┐
│ index.html (SPA Shell — 1,039 lines)                        │
│                                                             │
│  ┌─── Login Screen (conditional)                            │
│  │                                                          │
│  ├─── Toparea (desktop: logo + pills + search + notifs)     │
│  ├─── Mobile Topbar + Mobile Toolbar (touch devices only)   │
│  ├─── Sidebar (nav to 8 views)                              │
│  │                                                          │
│  ├─── view-list          ─── Requisition list + drill-downs │
│  ├─── view-customers     ─── Company/site CRM               │
│  ├─── view-vendors       ─── Vendor directory                │
│  ├─── view-materials     ─── Material cards                  │
│  ├─── view-buyplans      ─── Buy plan management             │
│  ├─── view-proactive     ─── Proactive offers                │
│  ├─── view-performance   ─── Scorecards                      │
│  ├─── view-settings      ─── Admin (10 sub-tabs)             │
│  │                                                          │
│  └─── 30 Modals (always in DOM, toggled by JS)              │
│                                                             │
│  Assets:                                                    │
│  ├── {{ vite_css_tags }}  →  styles.css (1,556 lines)       │
│  └── {{ vite_js_tags }}   →  app.js (8,233 lines)           │
│                               └── crm.js (4,979 lines)      │
│                                    imports 16 fns from app   │
└─────────────────────────────────────────────────────────────┘

Standalone report templates (no JS/CSS sharing):
├── documents/rfq_summary.html  (inline styles, no JS)
└── documents/quote_report.html (inline styles, no JS)

CSS file → pages:  styles.css → ALL views (single stylesheet)
JS files → pages:  app.js + crm.js → ALL views (both always loaded)
```

---

## 6. Key Findings Summary

### By the Numbers

| Metric | Value |
|--------|-------|
| Templates | 3 (1 SPA + 2 PDF reports) |
| CSS files | 1 (1,556 lines) |
| JS files | 2 (13,212 lines combined) |
| Module-level variables | ~116 |
| Functions on `window` | ~170 |
| Inline event handlers | 181 |
| Inline `style` attributes | ~143 |
| `!important` overrides | 16 (14 coupling symptoms) |
| `getElementById` calls | 614+ |
| API endpoints called | ~85+ |
| Media query blocks | 7 (5 fragmented at 768px) |
| z-index values used | 14 distinct values |
| Modals in DOM | 30 |

### Architecture Implications for Stabilization

This is a **monolithic SPA** — there is no multi-page template inheritance to lock down. The entire UI lives in one HTML file, one CSS file, and two JS files. This means:

1. **Phase 3A (Lock Down Base Template)** — Not applicable in the traditional sense. The equivalent is establishing clear boundaries between the 8 view sections and 30 modals within `index.html`.

2. **Phase 3B (Namespace CSS)** — Can be done by wrapping each `#view-*` section with scoped selectors. The existing ID-based view system (`#view-list`, `#view-customers`, etc.) already provides natural boundaries.

3. **Phase 3C (Isolate JS)** — The 116 module-level globals and 170 window exports are the primary risk. Both files run on every view. Isolation means adding null checks and guarding DOM access by view context.

4. **CSS coupling is the biggest risk** — 14 `!important` overrides, 5 fragmented 768px media query blocks, and flat global selectors mean any CSS change can cascade unpredictably.

5. **JS stability is moderate** — ES module system is sound, cross-file API is explicit, but 15–20% of DOM accesses lack null safety and 116 globals create hidden coupling.
