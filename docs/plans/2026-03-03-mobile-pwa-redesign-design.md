# Mobile PWA Redesign — Design Document

**Date:** 2026-03-03
**Approach:** A — Rewrite mobile.css + refactor JS render paths + PWA
**Target:** iPhone 13/14/15 (390px width baseline)
**Delivery:** Progressive Web App (responsive web + service worker + manifest)

## Goal

Rebuild the mobile experience for core workflows only: Requisitions, Offers/Quotes/Buy Plans, Accounts/CRM, and Notifications. Keep the same visual design language (colors, typography, component shapes) but with a phone-native layout optimized for touch.

## Scope

### In Scope
- 5-tab bottom navigation (Reqs, Accounts, Offers, Alerts, More)
- Requisition list as cards + drill-down detail with tabbed sections
- Offer recording, quote creation, buy plan submission via bottom sheets
- Company lookup with contact cards + quick actions
- Notifications feed with badge counts
- PWA manifest + service worker (cache shell, network-first API)
- iOS-specific meta tags and splash screens

### Out of Scope
- Dashboard/Scorecard (available via "More" tab but not redesigned)
- Admin Settings (desktop only)
- Prospecting module (desktop only)
- Offline data editing
- Push notifications (future)
- Android-specific optimizations

## Architecture

### Approach
Single codebase. Same `index.html`, same JS files. The existing `window.__isMobile` flag drives layout branching. Changes are:
1. **`mobile.css`** — gutted and rewritten for the new layout
2. **`app.js` / `crm.js`** — new mobile render functions for each workflow
3. **`index.html`** — updated bottom nav tabs, mobile header, view containers
4. **`touch.js`** — enhanced gestures (pull-to-refresh, swipe-to-dismiss alerts)
5. **New files:** `manifest.json`, `sw.js`, app icons, splash images

### No new backend changes
All data is already available via existing API endpoints. Mobile renders the same data differently.

## Navigation

### Bottom Tab Bar (5 tabs)
| Tab | Icon | View | Badge |
|-----|------|------|-------|
| Reqs | clipboard-list | Requisition list | Open req count |
| Accounts | building | Companies list | — |
| Offers | tag | Pending offers feed | Unreviewed count |
| Alerts | bell | Notifications | Unread count |
| More | dots-3 | Popover: Vendors, Materials, Dashboard, Settings | — |

### Top Bar (simplified)
- Logo (left, tappable → Reqs)
- Search icon (right, expands to full-width search input)
- User avatar circle (right, tap → logout popover)
- No pills, no filter row (those live inside each view)

### Drill-Down Pattern
Tap a card → full-screen overlay slides up from bottom. Back button (top-left) or swipe-down to dismiss. Same `m-fullscreen` component already exists.

## View Designs

### 1. Requisitions List
- **Filter row:** Horizontally scrollable pill tabs — Open | Sourcing | Archive
- **Summary row:** 3 stat boxes (count per status)
- **Card list:** Each req card:
  - Left border: status color (blue/amber/green/gray)
  - Title: Req name (truncated with ellipsis)
  - Subtitle: Customer + buyer initials
  - Bottom: Date + status chip + part count badge + chevron
- **FAB:** "+" button bottom-right for new req
- **Pull-to-refresh:** Triggers `loadReqs()`

### 2. Requisition Detail (drill-down)
Full-screen overlay with:
- Header: back button + req name + status badge
- Scrollable tabs: Parts | Offers | Quotes | Buy Plans | Activity
- **Parts:** Card per part — MPN, qty needed, best price, source count
- **Offers:** Card per offer — vendor, price, qty, date + Accept/Reject buttons
- **Quotes:** Card per quote — customer, total, status + actions
- **Buy Plans:** Card per plan — vendor, total + Submit button
- **Activity:** Timeline cards

### 3. Offers Feed (Bottom Nav Tab)
Dedicated pending-offers inbox across all reqs:
- Card per offer: Vendor, MPN, Qty, Price, Total, Date
- Tap → bottom sheet with details + action buttons:
  - Accept (green), Reject (red), Counter (blue), Flag (amber)
- After accept → "Create Quote?" prompt

### 4. Log Offer (from Req Detail)
- Bottom sheet form (single column)
- Fields: Vendor (autocomplete), MPN, Qty, Price, Lead Time, Notes
- 16px font inputs, 44px min-height
- Full-width "Save Offer" CTA button

### 5. Create/Edit Quote
- Bottom sheet form
- Fields: Customer, Parts (multi-select), Markup %, Notes
- Live total calculation
- "Save Draft" + "Submit Quote" stacked buttons

### 6. Buy Plan Submission
- Card list of selected offers
- Total cost, vendor breakdown, delivery estimate
- Single "Submit" CTA button

### 7. Accounts List
- Search bar (always visible, 44px)
- Card per company: Name, Owner, Open Reqs count, Site count
- Tap → full-screen drawer: Overview | Contacts | Sites | Pipeline
- Contact cards: Name, title, email/phone (tap to call/email)
- Quick actions bar: Call, Email, Add Note

### 8. Alerts Feed
- Section headers: "Needs Attention" | "Recent" | "System"
- Card per alert: Icon + Title + Desc + Timestamp + Action
- Types: New offers, Quote approvals, System health, Trouble tickets
- Swipe-left to dismiss/mark read
- Badge count on bottom nav tab

## PWA Configuration

### manifest.json
```json
{
  "name": "AvailAI - Component Sourcing",
  "short_name": "AvailAI",
  "start_url": "/",
  "display": "standalone",
  "theme_color": "#3b6ea8",
  "background_color": "#f8f9fa",
  "icons": [
    { "src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png" },
    { "src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png" }
  ]
}
```

### Service Worker (sw.js)
- **Install:** Cache app shell (HTML, CSS, JS, fonts, icons)
- **Fetch strategy:** Network-first for `/api/` calls, cache-first for static assets
- **Offline fallback:** Simple "You're offline" page
- **Update:** `skipWaiting()` + `clients.claim()` for instant updates

### iOS Meta Tags
- `apple-touch-icon` (180px)
- `apple-mobile-web-app-capable: yes` (already present)
- `apple-mobile-web-app-status-bar-style: black-translucent` (already present)
- `apple-touch-startup-image` for common iPhone sizes

## CSS Strategy

Gut `mobile.css` (726 lines) and rewrite. Keep the component primitives that work well:
- `.m-card`, `.m-chip`, `.m-list-item` (mostly good, need polish)
- `.m-bottomnav` (works, needs tab content wiring)
- `.m-detail-header`, `.m-fullscreen` (good)
- `.m-bottom-sheet` (good)
- `.m-kv` (good for detail views)
- `.m-tabs-scroll`, `.m-tab-pill` (good)

Add/rewrite:
- FAB button styles
- Pull-to-refresh indicator
- Offer action buttons (accept/reject/counter)
- Alert cards with swipe-to-dismiss
- Search expand animation
- Better card spacing and typography for 390px width
- Landscape adjustments

## JS Strategy

### New mobile render functions (in app.js/crm.js):
- `renderMobileReqList(reqs)` — card list with status borders
- `renderMobileReqDetail(req)` — full-screen with tabbed sections
- `renderMobileOfferCard(offer)` — offer feed cards
- `renderMobileOfferForm()` — log offer bottom sheet
- `renderMobileQuoteForm()` — create/edit quote bottom sheet
- `renderMobileBuyPlanForm()` — buy plan submission
- `renderMobileAccountList(companies)` — company cards
- `renderMobileAccountDetail(company)` — full-screen drawer
- `renderMobileAlerts(notifications)` — notification feed

### Modified functions:
- `showView()` — respect bottom nav tab state on mobile
- `_initBottomNav()` — wire up tab switching + badge updates
- `loadReqs()` — call mobile renderer when `__isMobile`
- `loadCustomers()` — call mobile renderer when `__isMobile`

### touch.js additions:
- Pull-to-refresh gesture on main scroll
- Swipe-left-to-dismiss on alert cards
- Improved momentum scrolling on tab containers

## Testing Plan
- Manual testing on iPhone 13/14/15 (Safari + Chrome)
- iOS Safari-specific: safe area insets, rubber-band scrolling, input zoom
- PWA: Add to Home Screen, app launch, cache behavior, offline fallback
- Gesture testing: swipe directions, pull-to-refresh, bottom sheet drag
- Landscape mode verification
- Desktop regression check: all views unchanged at >768px
