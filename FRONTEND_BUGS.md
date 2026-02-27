# AVAIL AI — Frontend Bug Report

> Generated: 2026-02-22 | Phase 2 of Frontend Stabilization Plan

---

## 1. CSS Collision Report (`app/static/styles.css`)

### 1.1 Generic Selector Collision Risks

| Selector | Line | Scope | Risk |
|----------|------|-------|------|
| `.btn` | 79 | Global base class — 6+ variants extend it | HIGH — any new `.btn-*` must be tested globally |
| `.card` | 88 | Global card styling with `:hover` left-border | HIGH — dynamic cards risk collision |
| `.field` | 283 | Global form field wrapper; `.field input/textarea/select` styled at 285 | HIGH — dynamic forms inherit unexpected styles |
| `.modal` | 278 | Global modal base | MEDIUM — well-scoped via `.modal-bg` |
| `.tabs` / `.tab` | 94 | Global tab container | MEDIUM — used across multiple views |
| `.tbl` | 140 | Global table base; `:hover` at 143 | MEDIUM — drill-down overrides require `!important` |
| `.empty` | 91 | Placeholder text | LOW — simple utility |

### 1.2 `!important` Overrides — 17 Instances

**Coupling symptoms (15):**

| Line | Selector | Property | Root Cause |
|------|----------|----------|------------|
| 179 | `.sc` | `padding: 4px 10px` | Mobile override fights base rule |
| 258 | `.act-card-email-preview.expanded` | `display: block` | Visibility toggle fights cascade |
| 412 | `.main` | `margin-left: 0` | Mobile sidebar state override |
| 413 | `.mobile-topbar` | `display: flex` | Media query show/hide conflict |
| 414 | `.mobile-toolbar` | `display: flex` | Same show/hide conflict |
| 445 | `.sc` | `padding: 6px 10px` | Duplicate of 179 in different media context |
| 539 | `body.sb-open .main` | `margin-left: 0` | Sidebar state override (again) |
| 547 | `.view-toolbar .sbox` | `width: 100%` | Search box fights parent constraints |
| 728 | `.si-contact-phone` | `color: var(--text2)` | Contact styling fights accent colors |
| 753 | `.ofr-detail>td` | `padding: 8px 14px` | Table expansion row forces padding |
| 1241 | `.drow>td` | `background`, `border`, `border-radius` | Drill-down detail rows force cell styling |
| 1252 | `.add-row td` | `background: var(--teal-light)` | New row highlight forced |
| 1253 | `.add-row input` | `background: var(--white)` | Input background forced |
| 1261 | `.dl-cell:hover` | `background: var(--blue-light)` | Deadline cell hover forced |
| 1264–1266 | `tr.dl-row-*>td` | `background: rgba(...)` | Deadline alert rows with animation |
| 1315–1317 | `.tbl tr.sc-*>td` | `background: rgba(...)` | Performance highlight rows |

**Intentional (2):** Line 988 `.hidden`, line 1331 `.u-hidden` — both `display:none !important` utility classes.

**Coupling chain:** Line 445 `.sc` override exists *because* line 179 also uses `!important`, creating a specificity war. Removing either breaks the other.

### 1.3 Fragmented Media Queries

**4 separate `@media(max-width:768px)` blocks:**

| Line | Content | Qualifier |
|------|---------|-----------|
| 407 | Main mobile layout (sidebar, toolbar, topbar) | `(hover:none)/(pointer:coarse)` — touch only |
| 852 | CRM forms, contacts, modals | None — all narrow screens |
| 963 | AI panel, toast positioning | None — all narrow screens |
| 1483 | Drill-down tabs, quote tables, thread formatting | None — all narrow screens |

**Additional breakpoints:** 420px (line 580), 640px (line 1430), 769–1024px tablet (line 972).

**Problem:** Same selector may appear in different blocks. CSS parser evaluates all 4 independently — cascade order determines winner, making it hard to reason about mobile styling.

### 1.4 z-index Stacking Issues

| z-index | Element | Line | Issue |
|---------|---------|------|-------|
| 10 | `.site-typeahead-list` | 843 | FAR below filter panels (300) and modals (500) — typeahead hidden behind other elements |
| 150 | `.mobile-toolbar` | 569 | Off-by-one below `.mobile-topbar` (151) — confusing but works due to different `top` positions |
| 200 | `.toparea` | 1082 | Tied with `.sidebar` (200) — ambiguous stacking |
| 9999 | `.skip-link` | 1473 | Collision with `#toastContainer` (9999) — non-deterministic z-order |

### 1.5 Hardcoded Colors (30+)

Over 30 hex color values exist outside the CSS custom property system. Examples:

| Line | Selector | Hardcoded | Should Use |
|------|----------|-----------|------------|
| 81 | `.btn-success:hover` | `#0d9070` | `var(--green-dk)` (not defined) |
| 158–160 | `.req-badge-*` | `#dcfce7`, `#fef9c3`, `#fee2e2` | CSS vars |
| 203–210 | `.b-email`, `.b-hist`, `.sc-hist` | `#eaeff5`, `#3d6895`, `#c4a0e8` | CSS vars |
| 239–241 | `.act-badge-*` | `#fef3c7`, `#dbeafe`, `#fce4ec` | CSS vars |

**Impact:** Dark mode or rebranding requires editing 30+ lines instead of 10 CSS var definitions.

### 1.6 Duplicate Selector

| Line | Selector | Issue |
|------|----------|-------|
| 84, 816 | `.btn-danger` | Defined twice with identical properties — dead code at line 816 |

### 1.7 Duplicate Utility Classes

Lines 988 (`.hidden`) and 1331 (`.u-hidden`) both do `display:none !important`. Should consolidate to one.

---

## 2. JavaScript Conflict Report

### 2.1 `currentReqId` Global Variable Conflict

**Severity: CRITICAL**

| File | Line | Declaration |
|------|------|-------------|
| `app.js` | 91 | `let currentReqId = null;` (module-scoped) |
| `crm.js` | 62 | `currentReqId = null;` (no declaration — assignment only) |

`crm.js` modifies `currentReqId` without declaring or importing it. Since both are ES modules, `crm.js` is assigning to an undeclared variable. In strict mode this throws `ReferenceError`; without strict mode it creates an implicit global, decoupled from `app.js`'s module-scoped variable.

**Impact:** Both files think they own `currentReqId`. One file's writes may not be visible to the other, causing stale requisition references and wrong data loads.

### 2.2 Missing Null Checks on `getElementById`

**Severity: HIGH** — 15–20% of 614+ `getElementById` calls lack null safety.

**Worst offenders:**

| File | Lines | Pattern | Fields at Risk |
|------|-------|---------|----------------|
| `app.js` | 1379–1385 | `document.getElementById('loVendor').value = ...` | 7 Log Offer fields |
| `app.js` | 1692–1705 | `document.getElementById('ddEoVendor').value.trim()` | 12+ Edit Offer fields |
| `crm.js` | 362–375 | `document.getElementById('ncName').value.trim()` | New Company form |
| `crm.js` | 362 | `forEach(id => document.getElementById(id).value = '')` | Batch clear — crashes if any ID missing |
| `crm.js` | 393–410 | `document.getElementById('ecId').value = companyId` | 15+ Edit Company fields |

**Safer pattern exists** in other code: `document.getElementById('reqListFilter')?.value || ''`

### 2.3 Raw `fetch()` Bypasses `apiFetch()` Wrapper

**Severity: HIGH**

| File | Lines | Endpoint | Method |
|------|-------|----------|--------|
| `crm.js` | 1818 | `/api/buy-plans/token/{token}` | GET |
| `crm.js` | 1869 | `/api/buy-plans/token/{token}/approve` | POST |
| `crm.js` | 1882 | `/api/buy-plans/token/{token}/reject` | POST |

These use raw `fetch()` instead of the `apiFetch()` wrapper which handles CSRF tokens, auth headers, and error normalization. The POST calls may fail if CSRF protection is enforced.

### 2.4 Race Condition — Score Cache

**Severity: MEDIUM**

`app.js` lines 3248–3256: Sets `_ddScoreCache[reqId] = {}` (empty marker) synchronously, then populates it asynchronously via `.then()`. If `_renderSourcingDrillDown()` runs between the marker write and the `.then()` callback, it renders with empty scores.

### 2.5 Event Listener Cleanup

**Severity: LOW**

Only 2 `removeEventListener` calls found across 13,212 lines of JS. Most listeners persist for app lifetime (acceptable for SPA), but `toggleNotifications()` adds a click-outside handler on each open without guaranteed removal on panel close.

### 2.6 Silent `.catch()` Blocks

**Severity: LOW**

Multiple `.catch(() => {})` patterns swallow errors silently (e.g., `app.js` line 3255). Prevents debugging API failures.

---

## 3. Template Fragility Report (`app/templates/index.html`)

### 3.1 Missing Navigation DOM IDs

**Severity: CRITICAL**

| JS Reference | Line (app.js) | Expected ID | Actual ID in HTML |
|--------------|---------------|-------------|-------------------|
| `getElementById('navPerformance')` | 470 | `navPerformance` | `navScorecards` (line 113) |
| `getElementById('navEnrichment')` | 478 | `navEnrichment` | Does not exist |

**Impact:** JS conditionally shows these nav items for eligible roles (admin, manager, trader). Since the IDs don't match, the nav buttons never become visible. Users cannot access Performance or Enrichment features via sidebar.

### 3.2 Modal Forms Not Reset on Reopen

**Severity: HIGH**

| Modal | Open Function | Clears Fields? | Risk |
|-------|---------------|----------------|------|
| `#newCompanyModal` | `openNewCompanyModal()` (crm.js:338) | No | Stale company name, website, linkedin, industry visible |
| `#logOfferModal` | `openModal('logOfferModal','loVendor')` (app.js:3544) | No | Stale vendor, qty, price, lead time, condition visible |
| `#rfqModal` | Varies | Partial | Some fields cleared, others persist |

**User impact:** After closing a modal without submitting, reopening shows old values. Users may accidentally submit stale data.

### 3.3 Modal Loading State Not Cleared on Error

**Severity: HIGH**

`index.html` line 336 — RFQ modal backdrop click guard:
```html
onclick="if(event.target===this&&!this.dataset.loading)closeModal('rfqModal')"
```

When submit sets `this.dataset.loading`, the flag is only removed on explicit Cancel click (app.js line 345: `delete ...dataset.loading`). If the API call fails, the flag persists and the modal cannot be closed by clicking the backdrop. Only the Cancel button works.

### 3.4 Inline `style="display:none"` Precedence (143 instances)

**Severity: HIGH (maintenance)**

~143 inline `style` attributes, mostly `display:none` for initial hide state. These defeat any CSS rule that tries to show the element, since inline styles have higher specificity than class selectors. JS must use `element.style.display = ''` to clear them — a CSS class toggle alone won't work.

**Examples:** Lines 46 (`#statusToggle`), 208, 244, 248, 252.

### 3.5 Dual Search Inputs Without Persistent Sync

**Severity: HIGH**

Desktop `#mainSearch` (line 42) and mobile `#mobileMainSearch` (line 81) both fire `debouncedMainSearch(this.value)` independently. The debounce function syncs values, but if a user types on desktop, then resizes to mobile (or rotates device), the mobile input may show stale text.

### 3.6 Inline Event Handler Export Dependency

**Severity: MEDIUM (future risk)**

181 inline handlers (`onclick`, `oninput`, `onchange`, `onkeydown`) call functions that must be on `window`. Currently all referenced functions are exported. However, any future function added to HTML handlers without adding to `Object.assign(window, {...})` will silently fail — no error, no feedback.

### 3.7 Accessibility Issues

**Severity: LOW**

| Line | Element | Issue |
|------|---------|-------|
| 40 | `<a class="bug-link" onclick="openTroubleChat()">` | Anchor without `href` — should be `<button>` or `role="button"` |
| 72 | `<div class="sb-rail" onclick="toggleSidebar()">` | Has `aria-label` but lacks `role="button"` and `tabindex="0"` |
| 103 | `<div class="sidebar-overlay" onclick="toggleMobileSidebar()">` | No `aria-label` or `role` |
| 626 | `<a onclick="browseOneDrive('')">Root</a>` | Anchor without `href` |
| 460 | `#ncDupWarning` | Missing `role="alert"` for screen readers |
| Multiple | Form `<label>` elements | Missing `for="id"` attributes for explicit association |

---

## 4. Prioritized Bug List

### CRITICAL — Breaks Functionality

| # | Category | Description | Location | Status |
|---|----------|-------------|----------|--------|
| C1 | JS Global | `currentReqId` used without declaration in `crm.js` — implicit global decoupled from `app.js` module-scoped variable | crm.js:62 vs app.js:91 | **FIXED** — `currentReqId` exported from app.js, imported in crm.js with setter |
| C2 | DOM ID Mismatch | `navPerformance` and `navEnrichment` IDs missing from HTML — sidebar nav items for admin/manager roles never shown | app.js:470,478 vs index.html:113 | **FIXED** — JS updated to use `navScorecards` matching HTML |

### HIGH — Visual/UX Break or Data Risk

| # | Category | Description | Location | Status |
|---|----------|-------------|----------|--------|
| H1 | Null Safety | ~250 of 614+ `getElementById` calls lack null checks — crashes on missing elements | app.js ~120 calls, crm.js ~130 calls | **FIXING** |
| H2 | CSRF Bypass | 3 buy-plan endpoints use raw `fetch()` instead of `apiFetch()` — missing auth/CSRF headers | crm.js:1818,1869,1882 | **FIXED** — all buy-plan token endpoints now use `apiFetch()` |
| H3 | Modal Reset | Log Offer and New Company modals don't clear fields on reopen — stale data risk | app.js:3544; crm.js:338 | **FIXED** — both modals clear all fields in open functions |
| H4 | Modal Loading | RFQ modal `dataset.loading` flag not cleared on API error — traps user | index.html:336; app.js:345 | **FIXED** — loading flag cleared in `finally` block |
| H5 | CSS Coupling | 15 `!important` overrides from specificity wars — any CSS change cascades unpredictably | styles.css:179,412,445,539,753,1241 | Restructure base rules to eliminate need |
| H6 | Fragmented CSS | 4 separate `@media(max-width:768px)` blocks — conflicting mobile rules | styles.css:407,852,963,1483 | Consolidate into single block |
| H7 | z-index | `.site-typeahead-list` at z-index 10 — hidden behind filter panels (300) and modals (500) | styles.css:843 | Raise to 501+ |
| H8 | Inline Styles | ~143 inline `style="display:none"` — defeat CSS class toggles, require JS `style.display=''` to clear | index.html throughout | Migrate to `.u-hidden` class |
| H9 | Search Sync | Dual search inputs (desktop/mobile) not synced on responsive resize | index.html:42,81 | Sync on viewport change |

### LOW — Code Smell / Tech Debt

| # | Category | Description | Location | Fix |
|---|----------|-------------|----------|-----|
| L1 | Colors | 30+ hardcoded hex colors outside CSS var system — blocks theming | styles.css throughout | Create CSS vars |
| L2 | Duplicate | `.btn-danger` defined twice identically | styles.css:84,816 | Remove duplicate |
| L3 | Duplicate | `.hidden` and `.u-hidden` both do `display:none !important` | styles.css:988,1331 | Consolidate |
| L4 | Race | Score cache set to empty before async populate — brief empty render | app.js:3248–3256 | Use sentinel value |
| L5 | Silent Catch | `.catch(() => {})` swallows errors silently | app.js:3255 and others | Log or surface errors |
| L6 | Event Leaks | Click-outside handler in `toggleNotifications()` not always removed | app.js toggleNotifications | Track and remove handler |
| L7 | Accessibility | Clickable divs/anchors without `role="button"`, missing `for` on labels | index.html:40,72,103,626 | Add semantic attributes |
| L8 | Export Risk | 181 inline handlers depend on window exports — future additions may silently break | Both JS files | Migrate to delegated event listeners |
| L9 | Media Queries | Desktop-first approach requires `!important` for mobile overrides | styles.css architecture | Consider mobile-first refactor |

---

## 5. Recommended Fix Order

**Phase 3D should address bugs in this order:**

1. **C1 + C2** — Fix `currentReqId` ownership and missing nav IDs (immediate functionality fixes)
2. **H2** — Replace raw `fetch()` with `apiFetch()` (security fix)
3. **H1** — Add null checks to high-risk `getElementById` calls (stability)
4. **H3 + H4** — Fix modal reset and loading state cleanup (UX fixes)
5. **H5 + H6 + H7** — CSS restructuring (addressed by Phase 3B namespace work)
6. **H8 + H9** — Inline style migration and search sync (addressed by Phase 3A/3C work)
7. **L1–L9** — Tech debt cleanup (ongoing)
