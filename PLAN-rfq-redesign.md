# RFQ Layout Full Redesign Plan

## Problem Summary

1. **No hat-switching** — Traders do both sales and sourcing but the UI doesn't acknowledge this. Everyone sees the same columns regardless of what they're trying to do.
2. **List doesn't prioritize** — Flat table treats all reqs equally. No visual signal for "this needs attention NOW" vs "this is progressing fine."
3. **RFQ drawer is invisible** — The core action (sending RFQs to vendors) is hidden behind a non-obvious flow: you have to expand a req → go to Sightings tab → select sightings → click a hidden bulk button → THEN a drawer slides out. Users don't even know it exists.
4. **7 sub-tabs is overwhelming** — Parts, Sightings, Offers, Activity, Quotes, Tasks, Files. Too many choices, unclear which to use when.
5. **Three personas, one view** — Sales, Buyers, and Traders all need different info at a glance, but get the same table.

---

## Redesign: Role-Aware Priority Queue

### 1. Top-Level Mode Toggle (replaces Active/Archive)

Replace the current `Active | Archive` pills with a **role-aware mode toggle**:

```
[ Sales View ]  [ Sourcing View ]  [ Archive ]
```

- **Sales View** — Optimized for customer-facing work: customer name prominent, quote status, bid due dates, dollar values, action = "Build Quote" / "Send Quote" / "Follow Up"
- **Sourcing View** — Optimized for finding parts: part coverage %, sightings found, RFQs sent/responded, action = "Source Parts" / "Send RFQs" / "Review Offers"
- **Archive** — Same as today

Traders toggle between views. Pure buyers default to Sourcing View. Pure sales default to Sales View. Preference saved in localStorage.

### 2. Priority Lanes (replaces flat table)

Instead of a flat list sorted by date, group reqs into **priority lanes** (collapsible sections):

**Sales View lanes:**
```
🔴 NEEDS ACTION (3)          — Overdue deadlines, new offers to review, quotes to send
   [Req cards with next-action buttons]

🟡 IN PROGRESS (5)           — Active sourcing, quotes in draft
   [Req cards with progress indicators]

🟢 WAITING (2)               — RFQs sent, waiting for responses
   [Req cards with response tracking]

⚪ NEW / DRAFT (1)           — Just created, needs parts added
   [Req cards with setup prompts]
```

**Sourcing View lanes:**
```
🔴 UNSOURCED (4)             — Parts with no sightings yet
   [Req cards with "Source All" buttons]

🟡 SIGHTINGS FOUND (3)      — Have sightings, need RFQs sent
   [Req cards with "Send RFQs" buttons]

🟢 AWAITING RESPONSES (2)   — RFQs sent, waiting on vendors
   [Req cards with response rates]

✅ OFFERS IN (3)             — Have vendor offers, ready to quote
   [Req cards with "Build Quote" buttons]
```

Each lane is collapsible. Count badges show totals. Smart sorting within lanes by deadline urgency.

### 3. Req Cards (replaces table rows)

Each req becomes a **card** instead of a table row. Cards show different info based on the active view mode:

**Sales View Card:**
```
┌─────────────────────────────────────────────────────┐
│ Siemens Medical - Feb 2026              Due: Mar 15 │
│ 12 parts · 8 sourced · 3 offers     Sales: John D.  │
│ Quote: Draft ($4,200)                                │
│                              [ Build Quote ] [ ··· ] │
└─────────────────────────────────────────────────────┘
```

**Sourcing View Card:**
```
┌─────────────────────────────────────────────────────┐
│ Siemens Medical - Feb 2026              Due: Mar 15 │
│ ████████░░ 8/12 sourced · 15 sightings · 3 offers   │
│ RFQs: 5 sent · 2 responded (40%)                    │
│                    [ Send RFQs ] [ Source Parts ] [ ··· ] │
└─────────────────────────────────────────────────────┘
```

Clicking a card expands the drill-down (same as today, but with fewer tabs — see below).

### 4. Simplified Sub-Tabs (7 → 4)

Reduce from 7 tabs to 4 by merging related concerns:

**Current:** Parts | Sightings | Offers | Activity | Quotes | Tasks | Files
**Proposed:** Sourcing | Offers | Quote | Activity

- **Sourcing** = Parts table + Sightings inline per part + "Send RFQ" button right there
  - Each part row expands to show its sightings
  - Select sightings → "Send RFQ" button appears inline (no hidden drawer)
  - Upload/Paste/Add Part actions in the tab header

- **Offers** = Confirmed offers (same as today, works well)

- **Quote** = Quote builder + Buy Plan (merged, since buy plan comes from quote)
  - Draft → Send → Track outcome all in one tab
  - Files/attachments shown as a section within Quote tab

- **Activity** = Timeline + Tasks + RFQ send history (merged)
  - Filterable: All | RFQs | Replies | Notes | Tasks

### 5. Inline RFQ Flow (replaces hidden drawer)

The biggest change: **sending RFQs happens inside the Sourcing tab**, not a separate drawer.

Flow:
1. User is in Sourcing tab, sees parts and their sightings
2. User checks sightings they want to RFQ (checkboxes already exist)
3. A **sticky bottom bar** appears: "Send RFQ to 3 vendors (5 parts)" [Compose →]
4. Clicking "Compose" opens a **slide-up panel within the tab** (not a separate drawer)
5. Panel shows: vendor list, email template, condition selector, preview
6. User clicks "Send" — results show inline

This keeps the user in context. No mysterious drawer. No lost state.

### 6. Smart Notifications Row

Add a **notification bar** at the top of the list (below the mode toggle) that surfaces urgent items:

```
┌──────────────────────────────────────────────────────────────┐
│ ⚡ 2 new vendor responses · 1 quote due today · 3 unsourced │
│    [Review Responses]  [View Quote]  [Source Parts]          │
└──────────────────────────────────────────────────────────────┘
```

Dismissible. Only shows when there's something actionable.

---

## Implementation Phases

### Phase 1: Priority Lanes + Mode Toggle (biggest impact, least risk)
- Add Sales/Sourcing/Archive toggle
- Implement priority lane grouping logic
- Save mode preference in localStorage
- Keep existing drill-down and sub-tabs unchanged
- **Files changed:** `app/static/app.js` (renderReqList, mode toggle), `app/templates/index.html` (top pills)

### Phase 2: Simplified Sub-Tabs (4 instead of 7)
- Merge Parts + Sightings → "Sourcing" tab
- Merge Quotes + Buy Plans → "Quote" tab
- Merge Activity + Tasks → "Activity" tab
- Move Files into Quote tab as a section
- **Files changed:** `app/static/app.js` (sub-tab functions, render functions)

### Phase 3: Inline RFQ Flow (replaces drawer)
- Build sticky bottom bar for sighting selection
- Build inline compose panel
- Deprecate rfqDrawer
- **Files changed:** `app/static/app.js`, `app/templates/index.html`

### Phase 4: Card Layout + Smart Notifications
- Replace table rows with cards
- Add notification bar component
- **Files changed:** `app/static/app.js`, `app/templates/index.html`, CSS in template

---

## What This Preserves

- All existing API endpoints (no backend changes in Phase 1-2)
- Mobile layout (cards already exist for mobile, extend to desktop)
- Archive view (mostly unchanged)
- All existing functionality (just reorganized, not removed)

## What This Removes

- The hidden RFQ drawer (replaced by inline flow)
- 3 sub-tabs (merged into others, not deleted)
- Flat unsorted list (replaced by priority lanes)

## Risks

- Large JS changes in a 14K-line file — need careful testing
- Users accustomed to current layout — consider a "classic view" toggle during transition
- Priority lane logic needs tuning — what counts as "needs action" vs "in progress"
