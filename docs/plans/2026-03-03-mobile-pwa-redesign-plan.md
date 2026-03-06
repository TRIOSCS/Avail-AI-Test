# Mobile PWA Redesign — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rebuild the mobile experience as a PWA with 5-tab bottom nav, card-based requisition/offer/quote/buy-plan workflows, and an alerts feed — all optimized for iPhone 13/14/15 (390px).

**Architecture:** Single codebase approach. Same `index.html` + `app.js` + `crm.js` — the existing `window.__isMobile` flag drives layout branching. Rewrite `mobile.css`, add mobile render functions in JS, update HTML bottom nav tabs, add PWA manifest + service worker.

**Tech Stack:** Vanilla JS, CSS custom properties, Jinja2 template, Vite bundler, Service Worker API

**Design doc:** `docs/plans/2026-03-03-mobile-pwa-redesign-design.md`

**Security note:** All dynamic content rendered via the existing `esc()` helper which HTML-escapes user input. This matches the existing pattern throughout `app.js` and `crm.js`. No raw user input is inserted into the DOM without escaping.

---

## Task 1: PWA Foundation — Manifest, Service Worker, Icons

**Files:**
- Create: `app/static/manifest.json`
- Create: `app/static/sw.js`
- Create: `app/static/offline.html`
- Create: `app/static/icons/` (placeholder PNGs — 192px, 512px, 180px apple-touch)
- Modify: `app/templates/index.html:1-15` (add manifest link, apple-touch-icon, SW registration)

**Step 1: Create manifest.json**

Create `app/static/manifest.json`:
```json
{
  "name": "AvailAI - Component Sourcing",
  "short_name": "AvailAI",
  "description": "Electronic component sourcing and CRM",
  "start_url": "/",
  "display": "standalone",
  "orientation": "portrait-primary",
  "theme_color": "#3b6ea8",
  "background_color": "#f8f9fa",
  "icons": [
    { "src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable" },
    { "src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable" }
  ]
}
```

**Step 2: Create service worker**

Create `app/static/sw.js` with:
- Cache-first for static assets (`/static/`)
- Network-first for API calls (`/api/`)
- Offline fallback page for HTML navigation
- `skipWaiting()` + `clients.claim()` for instant updates
- Old cache cleanup on activate

**Step 3: Create offline fallback page**

Create `app/static/offline.html` — simple centered message with retry button. Self-contained styles (no external dependencies).

**Step 4: Generate placeholder icons**

Create simple placeholder PNGs at:
- `app/static/icons/icon-192.png` (192x192)
- `app/static/icons/icon-512.png` (512x512)
- `app/static/icons/apple-touch-icon.png` (180x180)

These can be replaced with production icons later.

**Step 5: Update index.html head**

In `app/templates/index.html`, add after the existing `<meta name="theme-color">`:
```html
<link rel="manifest" href="/static/manifest.json">
<link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png">
```

Add service worker registration script before `</body>`:
```html
<script>
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/static/sw.js').catch(function(e) {
    console.warn('SW registration failed:', e);
  });
}
</script>
```

**Step 6: Verify manifest loads**

Run: `docker compose up -d --build && docker compose logs -f app`
Open Chrome DevTools > Application > Manifest — verify it shows correctly.
Open Application > Service Workers — verify it registers.

**Step 7: Commit**

```bash
git add app/static/manifest.json app/static/sw.js app/static/offline.html app/static/icons/ app/templates/index.html
git commit -m "feat: add PWA foundation — manifest, service worker, offline fallback, icons"
```

---

## Task 2: Redesign Bottom Nav — 5 Tabs (Reqs, Accounts, Offers, Alerts, More)

**Files:**
- Modify: `app/templates/index.html:1378-1410` (bottom nav HTML)
- Modify: `app/static/app.js:118-160` (`mobileTabNav`, `mobileMoreNav`, `toggleMobileMore`)
- Modify: `app/static/mobile.css:148-264` (bottom nav styles)

**Step 1: Replace bottom nav HTML**

In `index.html`, replace the `<nav class="m-bottomnav">` block (lines 1378-1399). New tabs:
- **Reqs** (clipboard icon) — navigates to requisition list
- **Accounts** (building icon) — navigates to companies list
- **Offers** (dollar-sign icon) — navigates to new offers feed view
- **Alerts** (bell icon) — navigates to new alerts feed view
- **More** (dots icon) — opens popover with Vendors, Materials, Dashboard, etc.

Each tab gets a `<span class="m-bottomnav-badge" id="bnBadge{Tab}">` for badge counts.

**Step 2: Add Offers and Alerts view containers**

Add two new view divs inside `<main>` (before closing `</main>`):
- `<div id="view-offers" class="u-hidden m-mobile-only">` — with filter pills (Pending/All/Accepted), summary stats row, and offer list container
- `<div id="view-alerts" class="u-hidden m-mobile-only">` — with alerts list container

**Step 3: Update mobileTabNav in app.js**

Replace the `mobileTabNav` function (lines 118-134). New version handles:
- `'offers'` → `showView('view-offers')` + calls `loadOfferFeed()`
- `'alerts'` → `showView('view-alerts')` + calls `loadAlertsFeed()`
- `'reqs'` and `'customers'` → same as before (delegating to `sidebarNav`)

**Step 4: Update More popover**

In `index.html`, update the More popover items to include: Vendors, Materials, Buy Plans, Dashboard, Scorecard, Contacts, Settings.

**Step 5: Register new views in ALL_VIEWS**

In `app.js`, find the `ALL_VIEWS` array and add `'view-offers'` and `'view-alerts'` so `showView()` can manage them.

**Step 6: Test bottom nav tab switching**

Build and deploy. On 390px responsive mode verify:
- All 5 tabs render and highlight on tap
- Each tab switches to correct view
- More popover opens/closes correctly
- Badge elements exist (empty for now)

**Step 7: Commit**

```bash
git add app/templates/index.html app/static/app.js app/static/mobile.css
git commit -m "feat: redesign mobile bottom nav — 5 tabs with Offers and Alerts"
```

---

## Task 3: Mobile Top Bar Redesign

**Files:**
- Modify: `app/templates/index.html:79-101` (mobile-topbar and mobile-toolbar)
- Modify: `app/static/app.js` (add search toggle + user menu functions)
- Modify: `app/static/mobile.css` (top bar styles)

**Step 1: Replace mobile top bar HTML**

Replace the mobile-topbar div (lines 79-90) with simplified version:
- Logo (left, taps to Reqs)
- Spacer
- Search icon button (right, toggles expandable search bar)
- User avatar circle (right, taps to show logout)

Add expandable search bar div below topbar (hidden by default, slides in on toggle).

**Step 2: The mobile-toolbar is already hidden**

The `mobile-toolbar` div (lines 91-101) is already `display:none!important` in mobile.css. Keep it hidden — replaced by in-view pill tabs.

**Step 3: Add search toggle and user menu functions in app.js**

- `_toggleMobileSearch()` — toggles `.hidden` on search bar, focuses input
- `_showMobileUserMenu()` — simple confirm dialog for logout (or reuse existing user popover)

**Step 4: Initialize user avatar**

In DOMContentLoaded, set avatar initials from `window.__userName`.

**Step 5: Add CSS for new topbar components**

In `mobile.css`, add styles for:
- `.m-topbar-btn` — 36px circle, transparent bg, centered icon
- `.m-avatar` — 32px circle, blue bg, white initials
- `.m-search-bar` — flex row with input + Cancel button
- `.m-search-cancel` — text button, blue color

**Step 6: Test**

On 390px: Logo left, search + avatar right. Search toggles input. Avatar shows initials.

**Step 7: Commit**

```bash
git add app/templates/index.html app/static/app.js app/static/mobile.css
git commit -m "feat: redesign mobile top bar — expandable search, user avatar"
```

---

## Task 4: Mobile Requisition List — Card Redesign

**Files:**
- Modify: `app/static/app.js` (add `renderMobileReqList`, modify existing render to branch on `__isMobile`)
- Modify: `app/static/mobile.css` (req card styles, FAB button)
- Modify: `app/templates/index.html` (add mobile pill tabs + FAB)

**Step 1: Find current req rendering function**

In `app.js`, locate the function that renders requisition rows into `#reqList`. It uses grid-based layout with `.col-headers` and `.req-row`.

**Step 2: Add renderMobileReqList function**

New function that renders:
- Summary stats row (3 boxes: Open/Sourcing/Archived counts)
- Card per requisition with:
  - Left border color by status (blue=sourcing, amber=offers, green=quoted, gray=draft)
  - Title: req name (truncated) + status chip
  - Subtitle: customer + buyer initials
  - Footer: date + part count badge + chevron
- All dynamic content passed through existing `esc()` helper

**Step 3: Wire into existing render path**

At top of existing req render function, add early return:
```js
if (window.__isMobile) { renderMobileReqList(filteredReqs); return; }
```

**Step 4: Add in-view pill tabs for Open/Sourcing/Archive**

In `index.html`, at top of `#view-list`, add `m-mobile-only` scrollable pill tabs that call `setMainView()`.

**Step 5: Add FAB button**

Add floating "+" button in HTML (`m-mobile-only`), positioned above bottom nav. CSS: fixed, bottom-right, 56px circle, blue, shadow.

**Step 6: Hide desktop-only elements on mobile**

In `mobile.css`: hide `.col-headers`, `.toolbar-stats` on mobile.

**Step 7: Test on 390px**

Cards render with status borders, pill tabs switch views, FAB opens new req modal, desktop unchanged.

**Step 8: Commit**

```bash
git add app/static/app.js app/static/mobile.css app/templates/index.html
git commit -m "feat: mobile requisition list — card layout, pill tabs, summary stats, FAB"
```

---

## Task 5: Mobile Requisition Detail — Full-Screen Drill-Down

**Files:**
- Modify: `app/static/app.js` (add mobile req detail renderer)
- Modify: `app/static/crm.js` (mobile offer/quote/buy-plan tab renderers)
- Modify: `app/static/mobile.css` (detail view styles)

**Step 1: Add mobile req detail renderer**

When `openReqDetail(reqId)` is called on mobile, create a full-screen overlay (`m-fullscreen`) with:
- Header: back button + req name + status badge
- Scrollable tabs: Parts | Offers | Quotes | Buy Plans | Activity
- Body container for tab content

**Step 2: Add tab content renderers**

- `_renderMobilePartsList(parts)` — card per part with MPN, qty, best price, source count
- `_renderMobileOffersList(offerGroups)` — card per offer with vendor, price, qty, Accept/Reject chips
- `_renderMobileQuotesList(quotes)` — card per quote with customer, total, status
- `_renderMobileBuyPlansList(buyPlans)` — card per plan with vendor, total, Submit button
- `_renderMobileActivityList()` — timeline cards

All renderers use `esc()` for user content.

**Step 3: Wire into openReqDetail**

Add mobile branch at top of `openReqDetail`: fetch req data, store in window variables, call `renderMobileReqDetail`.

**Step 4: Add close function and gesture support**

`_closeMobileReqDetail()` removes the overlay. Swipe-down on header already handled by `touch.js`.

**Step 5: Test**

Tap req card → full-screen slides up. Tabs switch. Parts/Offers/Quotes/BuyPlans render as cards. Back button and swipe-down close.

**Step 6: Commit**

```bash
git add app/static/app.js app/static/crm.js app/static/mobile.css
git commit -m "feat: mobile requisition detail — full-screen drill-down with tabs"
```

---

## Task 6: Mobile Log Offer — Bottom Sheet Form

**Files:**
- Modify: `app/static/crm.js` (add `_openMobileOfferForm`, `_submitMobileOffer`)
- Modify: `app/static/mobile.css` (form styles within bottom sheet)

**Step 1: Add mobile offer form function**

Creates a `m-bottom-sheet-bg` overlay with form inside `m-bottom-sheet`:
- Fields: Vendor (text input), MPN, Qty (number, inputmode=numeric), Price (number, inputmode=decimal), Lead Time, Notes
- All inputs 16px font, 44px min-height (prevents iOS zoom)
- Full-width "Save Offer" primary button + Cancel ghost button
- Tapping backdrop dismisses

**Step 2: Add submit handler**

`_submitMobileOffer(reqId)` — reads form values, validates (vendor + MPN required), calls POST `/api/requisitions/{id}/offers`, shows toast, dismisses sheet, refreshes offer data.

**Step 3: Add trigger button in req detail offers tab**

At top of offers list rendering, add a "Log Offer" action button.

**Step 4: Test**

Open req detail → Offers tab → "Log Offer" → fill form → Save → toast → sheet closes → offers refresh.

**Step 5: Commit**

```bash
git add app/static/crm.js app/static/mobile.css
git commit -m "feat: mobile log offer — bottom sheet form"
```

---

## Task 7: Mobile Offers Feed (Bottom Nav Tab)

**Files:**
- Modify: `app/static/app.js` (add `loadOfferFeed`, `_renderOfferFeed`)
- Possibly create: backend endpoint `/api/offers/feed` if none exists
- Modify: `app/static/mobile.css` (offer feed styles)

**Step 1: Check if cross-requisition offers endpoint exists**

Search backend for an endpoint that returns recent offers across all requisitions. If not, create a lightweight one in `app/routers/crm.py`:
- `GET /api/offers/feed?limit=50` — returns offers sorted by created_at desc
- Each offer includes: id, vendor_name, mpn, unit_price, qty_available, lead_time, status, created_at, req_name, req_id
- Write test for this endpoint

**Step 2: Add loadOfferFeed function**

Fetches from the feed endpoint, stores in `_offerFeedData`, calls `_renderOfferFeed()`.

**Step 3: Add filter and render functions**

- `_setOfferFeedFilter(status, btn)` — filters by pending/all/accepted
- `_renderOfferFeed()` — renders summary stats + card list using `esc()` for all user content
- Updates `bnBadgeOffers` badge count

**Step 4: Test**

Tap Offers tab → summary shows counts → cards show offers → filter pills work → badge shows pending count.

**Step 5: Commit**

```bash
git add app/static/app.js app/static/mobile.css app/routers/crm.py tests/
git commit -m "feat: mobile offers feed — dedicated tab with pending/accepted filters"
```

---

## Task 8: Mobile Accounts List & Detail

**Files:**
- Modify: `app/static/crm.js` (add `renderMobileAccountList`, enhance drawer for mobile)
- Modify: `app/static/mobile.css` (account card + contact card styles)
- Modify: `app/templates/index.html` (mobile search bar in accounts view)

**Step 1: Add renderMobileAccountList**

Card per company: Name, Owner, Open Reqs count, Site count. Tap opens `openCustDrawer()`.

**Step 2: Wire into renderCustomers**

At top of `renderCustomers()`, branch on `__isMobile`.

**Step 3: Add mobile search bar**

In `index.html`, inside `#view-customers`, add mobile-only search input that filters the list.

**Step 4: Enhance drawer contacts for mobile**

Add `_renderMobileContact()` — list items with name, title, and tap-to-call/email action icons.

**Step 5: Test**

Accounts tab → cards with search → tap → full-screen drawer → contacts with call/email buttons.

**Step 6: Commit**

```bash
git add app/static/crm.js app/static/mobile.css app/templates/index.html
git commit -m "feat: mobile accounts — card list with search, enhanced drawer"
```

---

## Task 9: Mobile Alerts Feed

**Files:**
- Modify: `app/static/app.js` (add `loadAlertsFeed`, `_renderAlertsFeed`)
- Modify: `app/static/touch.js` (swipe-to-dismiss on alert cards)
- Modify: `app/static/mobile.css` (alert card styles)

**Step 1: Add loadAlertsFeed**

Fetch from existing `/api/notifications` endpoint. Store data, call renderer.

**Step 2: Add renderer**

Group notifications into "Needs Attention" (unread) and "Recent" (read). Each card shows title, body, timestamp. Unread cards get blue left border.

**Step 3: Add tap handler**

Tapping an alert marks it as read via POST `/api/notifications/{id}/read`. Removes blue border.

**Step 4: Add swipe-to-dismiss in touch.js**

Touch event listeners on `.m-alert-card` elements:
- Swipe left → card translates and fades → removed from DOM
- Marks as read via API

**Step 5: Update badge**

After rendering, set `bnBadgeAlerts` count to unread notifications.

**Step 6: Test**

Alerts tab → grouped notifications → tap marks read → swipe dismisses → badge updates.

**Step 7: Commit**

```bash
git add app/static/app.js app/static/touch.js app/static/mobile.css
git commit -m "feat: mobile alerts feed — grouped notifications with swipe-to-dismiss"
```

---

## Task 10: Mobile Quote & Buy Plan Forms

**Files:**
- Modify: `app/static/crm.js` (add quote + buy plan bottom sheet forms)
- Modify: `app/static/mobile.css` (form styles)

**Step 1: Add mobile quote form**

Bottom sheet with:
- Customer name input
- Offer selection checkboxes (from req's accepted offers)
- Markup % input with live total calculation
- "Save Draft" + "Submit Quote" stacked buttons

**Step 2: Add mobile buy plan form**

Bottom sheet with:
- Selected offers summary (vendor, price, qty per line)
- Total cost display
- Single "Submit Buy Plan" button

**Step 3: Wire into req detail tabs**

Quotes tab gets "Create Quote" button. Buy Plans tab gets "Submit Buy Plan" button. Both open respective bottom sheet forms.

**Step 4: Test**

From req detail: create quote with markup → submit → toast. Submit buy plan → toast.

**Step 5: Commit**

```bash
git add app/static/crm.js app/static/mobile.css
git commit -m "feat: mobile quote & buy plan forms — bottom sheet with offer selection"
```

---

## Task 11: Pull-to-Refresh & Polish

**Files:**
- Modify: `app/static/touch.js` (pull-to-refresh handler)
- Modify: `app/static/mobile.css` (pull-to-refresh indicator, final polish)
- Modify: `app/static/app.js` (register refresh callbacks)

**Step 1: Add pull-to-refresh in touch.js**

Listen for touch events on `.main-scroll`:
- Track pull distance when scrollTop is 0
- Show indicator with "Pull to refresh" text
- On release past threshold (80px), trigger view-specific refresh callback
- Show "Refreshing..." then hide on completion

**Step 2: Add CSS for pull-to-refresh indicator**

`.m-ptr-indicator` — centered text, transitions for height/opacity.

**Step 3: Register refresh callbacks per view**

In `mobileTabNav()`, set `window._mobileRefreshCallback` based on active tab (loadRequisitions, loadCustomers, loadOfferFeed, loadAlertsFeed).

**Step 4: Final CSS polish**

Review all components at 390px:
- Consistent card padding (14px 16px, 8px gap)
- Font hierarchy (14px title, 12px subtitle/meta, 11px badge)
- All touch targets >= 44px
- No horizontal overflow
- Bottom nav padding correct with safe area
- Hide `#troublePill` on mobile (conflicts with FAB)

**Step 5: Test full end-to-end flow**

1. Reqs → pull refresh → cards reload
2. Tap req → detail → parts/offers/quotes tabs
3. Log offer → submit → refresh
4. Accounts → search → tap → drawer
5. Offers feed → filter → tap offer
6. Alerts → swipe dismiss
7. More → all sub-pages accessible
8. PWA: Add to Home Screen → standalone launch
9. Desktop: everything unchanged at >768px

**Step 6: Commit**

```bash
git add app/static/touch.js app/static/mobile.css app/static/app.js
git commit -m "feat: pull-to-refresh, mobile polish, hide trouble pill on mobile"
```

---

## Task 12: Vite Build & Desktop Regression Check

**Files:**
- Possibly modify: `vite.config.js` (if new entry points needed)
- Run: test suite

**Step 1: Run Vite build**

```bash
cd /root/availai && npx vite build
```

Verify no build errors. The `checkExportsPlugin` will catch undefined function references in `Object.assign(window, {...})` blocks and onclick handlers in index.html.

**Step 2: Fix any build errors**

If new functions are exposed via `Object.assign`, add them. If imports between `app.js` and `crm.js` are broken, fix export/import chain.

**Step 3: Desktop regression test**

Open at >768px and verify all views render correctly. Sidebar, drawers, modals, search all functional. No mobile-only elements visible.

**Step 4: Run existing test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v
```

All tests should pass. If `/api/offers/feed` endpoint was added (Task 7), verify its test passes.

**Step 5: Coverage check**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
```

Ensure no coverage regression.

**Step 6: Deploy and verify**

```bash
docker compose up -d --build
docker compose logs -f app
```

Check logs for clean startup. Test on actual iPhone if available.

**Step 7: Final commit**

```bash
git add -A
git commit -m "chore: vite build, desktop regression verified, all tests passing"
```

---

## Summary

| # | Task | Key Files | Steps |
|---|------|-----------|-------|
| 1 | PWA Foundation | manifest.json, sw.js, offline.html, icons | 7 |
| 2 | Bottom Nav Redesign | index.html, app.js, mobile.css | 7 |
| 3 | Top Bar Redesign | index.html, app.js, mobile.css | 7 |
| 4 | Req List Cards | app.js, mobile.css, index.html | 8 |
| 5 | Req Detail Drill-Down | app.js, crm.js, mobile.css | 6 |
| 6 | Log Offer Form | crm.js, mobile.css | 5 |
| 7 | Offers Feed Tab | app.js, mobile.css, possibly crm.py | 5 |
| 8 | Accounts List & Detail | crm.js, mobile.css, index.html | 6 |
| 9 | Alerts Feed | app.js, touch.js, mobile.css | 7 |
| 10 | Quote & Buy Plan Forms | crm.js, mobile.css | 5 |
| 11 | Pull-to-Refresh & Polish | touch.js, mobile.css, app.js | 6 |
| 12 | Vite Build & Regression | vite.config.js, tests | 7 |

**Total: 12 tasks, ~76 steps**

**Parallelization:** Tasks 1-3 are foundational and independent of each other (can be parallelized). Tasks 4-10 are feature tasks (can be done in any order after 1-3, many parallelizable). Tasks 11-12 are finalization (after all features).

**Backend changes:** Only Task 7 may require a new endpoint (`/api/offers/feed`). Everything else is frontend-only.
