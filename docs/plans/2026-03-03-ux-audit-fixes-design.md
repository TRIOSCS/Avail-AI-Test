# UX Audit Fixes — Design Document

**Date:** 2026-03-03
**Scope:** 50 confirmed issues from 2-session UX audit (59 reported, 5 disproved, 1 already fixed, 3 duplicates)
**Strategy:** 3 waves — Workflow Blockers → Data Integrity → Polish

## Disproved / Already Fixed Issues

| Reported Issue | Status | Reason |
|----------------|--------|--------|
| Account row click opens wrong account | Not a bug | Click handler correctly binds `c.id`; tested in code |
| Apollo "Credits: unavailable" | Working as designed | Intentional error state with hover tooltip explanation |
| Search case-sensitivity | Not a bug | Both frontend (`.toLowerCase()`) and backend (`.ilike()`) are case-insensitive |
| Different nav flows (goToCompany vs openCustDrawer) | By design | Entry-point nav vs in-view drawer — intentional separation |
| Archive hides Offers/Quotes tabs | Already fixed | Commit `f82e675` on deep-cleaning branch |

---

## Wave 1 — Workflow Blockers (12 issues)

These prevent users from completing core tasks.

### W1-1: RFQ Modal Not Opening
**File:** `app/static/app.js:8250-8252, 5938-5957`
**Root cause:** `openBatchRfqModal()` returns silently when `groups.length === 0` (line 8252). No user feedback. Also, `ddSendBulkRfq()` at line 5938 builds groups from checked sightings — if no sightings are checked, groups is empty.
**Fix:** Add toast: `if (!groups.length) { showToast('Select vendors first', 'warn'); return; }`. Also verify the "Prepare RFQ" button is properly hidden when count is 0 (line 5869 logic).

### W1-2: Offers to Review Navigation Race Condition
**File:** `app/static/app.js:2359`
**Root cause:** Dashboard "Offers to Review" widget uses `sidebarNav('reqs') + setTimeout(expandToSubTab, 400)`. The 400ms timeout is insufficient — requisition list loads async and may take 200-800ms. If list isn't rendered, `expandToSubTab()` silently fails.
**Fix:** Replace setTimeout pattern with `goToReq(o.requisition_id)` which other dashboard cards already use successfully. Or use MutationObserver/polling to wait for element existence.

### W1-3: Build Quote Button Never Activates
**File:** `app/static/app.js:3172, 3237`
**Root cause:** Button renders at line 3172 with `disabled` when `sel.size === 0`. Checkbox handler `ddToggleOffer()` at line 3237 updates the Set but may not re-render the button text/state.
**Fix:** After `ddToggleOffer()` updates the selection Set, call a re-render function that updates button text and disabled state.

### W1-4: Notification Deep-Linking Broken
**File:** `app/static/app.js:11359-11380`
**Root cause:** Nested `setTimeout` chains (300ms outer + 400ms inner) create race conditions. `toggleDrillDown()` has CSS animation that may not complete before `_switchDdTab()` fires.
**Fix:** Replace setTimeout chains with event-driven approach: wait for DOM element existence before acting, or use `requestAnimationFrame` + polling.

### W1-5: Pipeline Funnel Not Clickable
**File:** `app/static/app.js:2311-2321`
**Root cause:** Static `<span>` elements with no onclick handlers.
**Fix:** Add `onclick` handlers that navigate to requisitions filtered by status. Add `cursor:pointer` and hover styles.

### W1-6: Open Reqs Count Not Clickable
**File:** `crm.js:400, 808`
**Root cause:** Display-only `<td>` and `<div>` elements.
**Fix:** Wrap count in clickable element: `onclick="event.stopPropagation();filterReqsByCompany(${c.id})"`. Navigate to requisitions view filtered by company.

### W1-7: Open Tab Parts View — No "Start Sourcing" Action
**File:** `app/static/app.js` — Parts sub-tab in drill-down
**Root cause:** Parts tab shows part details but has no action button to kick off sourcing.
**Fix:** Add "Search Sources" button next to each part that triggers the sourcing flow.

### W1-8: Log Offer PART Not Pre-Selected
**File:** `app/static/app.js:6151`
**Root cause:** Log Offer modal opens with PART dropdown at default state even when the req has a single part.
**Fix:** Auto-select the part when req has only one requirement. Pre-populate from context when opening from a specific part row.

### W1-9: Log Offer No "In Response To RFQ" Field
**File:** `app/static/app.js:6151-6200`
**Root cause:** Offer form has no field linking it to an originating RFQ.
**Fix:** Add optional "In Response To" dropdown showing sent RFQs for this req. Populate from `/api/requisitions/{id}/rfq-history`.

### W1-10: Pipeline Req Link Goes to Unfiltered List
**File:** `crm.js` — Pipeline tab in company drawer
**Root cause:** Clicking a req in the pipeline tab navigates to the generic requisitions list, not the specific req.
**Fix:** Use `goToReq(reqId)` to navigate directly to the expanded requisition.

### W1-11: Dashboard "Purchasing/Sales" Toggle Has No Label
**File:** `app/static/app.js` — Dashboard header
**Root cause:** Toggle switch has no visible text label explaining what it does.
**Fix:** Add label text: "View: Purchasing | Sales" with active state styling.

### W1-12: Active Filter Lock Bug (Accounts)
**File:** `crm.js` — Filter logic
**Root cause:** Active filter chip cannot be deselected while search text is present.
**Fix:** Allow filter toggle independent of search state. Clear search when filter is toggled, or support combined filter+search.

---

## Wave 2 — Data Integrity & Trust (10 issues)

Wrong data shown to users.

### W2-1: Vendor Score Inflation (0/100 with explanation)
**File:** `app/static/app.js:10089`, `app/services/engagement_scorer.py:69`
**Root cause:** Backend returns `ghost_rate=0` for vendors with 0 outreach. Frontend calculates `(1 - 0) * 100 = 100%`. Should show 0/100 with "No RFQ history" tooltip.
**Fix:** Frontend: check `total_outreach === 0` before displaying factor scores. Show "0/100" with tooltip "No RFQ history to calculate score". Backend: return `ghost_rate=null` when outreach is 0.

### W2-2: All Developing Vendors Show "50"
**File:** `app/services/engagement_scorer.py:34`
**Root cause:** `COLD_START_SCORE = 50` assigned to all vendors below min outreach threshold. All new vendors cluster at "Developing" tier (33-66 range).
**Fix:** Frontend: show "New" badge instead of numeric score for vendors with `is_new_vendor=true`. Don't map cold-start engagement_score to tier.

### W2-3: Revenue/Win Rate/Last RFQ Columns All Show "—"
**File:** `crm.js:384-403`, `app/routers/crm/companies.py:89-156`
**Root cause:** `revenue_90d` field is never computed or returned by the API. `win_rate` and `last_req_date` are only populated when company has requisitions via customer sites.
**Fix:** Compute `revenue_90d` from won quotes in last 90 days. Ensure win_rate returns 0% (not null) when company has sites but no decided reqs.

### W2-4: "Needs Attention" and "No Recent Activity" Return Identical Results
**File:** `crm.js:322-326`
**Root cause:** Both filters use `last_enriched_at` (system enrichment date). "Needs Attention" checks `_custHealthColor === 'red'` (>90 days), "No Recent Activity" checks `daysSince > 30`. Massive overlap.
**Fix:** Differentiate: "Needs Attention" = has open reqs with no activity in 14+ days. "No Recent Activity" = no requisitions or offers in 30+ days. Use business activity dates, not enrichment dates.

### W2-5: Notification Text Shows Raw API Metadata
**File:** `app/static/app.js:11416-11430`
**Root cause:** Cleanup regex at line 11418 only strips simple `key=value` patterns. Structured JSON or deeper metadata leaks through.
**Fix:** Expand regex to handle JSON-like patterns. Or better: format notification messages on the backend before storing.

### W2-6: "Dev" Filter Abbreviation Unclear
**File:** `app/static/app.js` — Vendor tier filters
**Root cause:** Filter chip shows "Dev" instead of "Developing". No tooltip explaining tier criteria.
**Fix:** Show full text "Developing" and add tooltips to all tier chips explaining criteria.

### W2-7: "Proven" and "Caution" Tiers Return 0 Vendors
**File:** `app/static/app.js` — Vendor scoring
**Root cause:** Related to W2-2. Since all vendors get cold-start score of 50, none reach "Proven" (>=66) and none drop to "Caution" (<33).
**Fix:** Resolves naturally once W2-1 and W2-2 are fixed — vendors with real data will distribute across tiers.

### W2-8: Scorecard 100/100 on Ghost Rate + Delivery for 0-RFQ Vendors
**Same root cause as W2-1.** Fix: show "0/100" with "No RFQ history" when outreach is 0.

### W2-9: Destructive Actions No Confirmation (Vendors)
**File:** `app/static/app.js` — Vendor modal actions
**Root cause:** "Blacklist" and "Delete Vendor" buttons at top of modal with no confirmation dialog.
**Fix:** Add confirmation modal: "Are you sure you want to [blacklist/delete] {vendor}? This action [can/cannot] be undone."

### W2-10: Header Counters "0 Offers" / "0 Due" Not Interactive
**File:** `app/templates/index.html` or `app/static/app.js` — Global header
**Root cause:** Static display counters with no click handlers.
**Fix:** Add onclick to navigate to relevant filtered views (offers list, upcoming deadlines).

---

## Wave 3 — Polish & UX Quality (28 issues)

Visual feedback, missing features, minor navigation improvements.

### W3-1: Form Validation Silent (New Req + Log Offer)
Toast-only feedback → add inline red borders on invalid fields.

### W3-2: Sightings No Sticky Header
Limited sticky implementation → make req name, sub-tabs, and action buttons sticky at scroll.

### W3-3: VENDOR NAME in Log Offer Looks Like Plain Text
Hidden autocomplete → add visible search icon or "Type to search" placeholder.

### W3-4: Offers Empty State References Non-Existent "Vendor Tab"
Wrong copy → update to correct tab name.

### W3-5: Modal Title "New Company" vs Button "+ New Account"
Terminology mismatch → rename modal title to "New Account".

### W3-6: Vendor Side Panel Shows Only 3 Data Points
→ Show key metrics (score, sighting count, last contact) inline in panel.

### W3-7: No "+ Add Vendor" Button on Vendors List
→ Add button that opens vendor creation form.

### W3-8: Filter Tags No Tooltips (Accounts)
→ Add title attributes explaining each filter's criteria.

### W3-9: Bulk Actions Limited (Accounts)
Only Assign Owner + Export → add Merge Duplicates, Tag Strategic, Bulk Archive.

### W3-10: Escape Closes Whole Modal Instead of Dropdown
→ Check if dropdown is open before closing modal on Escape.

### W3-11–W3-28: Remaining Low/Medium Issues
Various minor UI fixes: tooltip additions, label improvements, hover states, consistency fixes. Each is a 1-5 line change in HTML/CSS/JS.

---

## Technical Approach

### Pattern: Replace setTimeout Navigation with Event-Driven
The #1 source of bugs is `setTimeout` chains for navigation. Replace all instances with:
```javascript
async function navigateToReqTab(reqId, tab) {
    sidebarNav('reqs');
    await waitForElement(`#req-${reqId}`, 2000);  // poll for existence
    toggleDrillDown(reqId);
    await waitForElement(`#dd-tab-${reqId}-${tab}`, 1000);
    _switchDdTab(reqId, tab);
}
```

### Pattern: Vendor Score Display Guard
```javascript
function displayFactorScore(value, hasData) {
    if (!hasData) return '<span class="score-na" title="No RFQ history">0</span>';
    return `<span>${Math.round(value)}</span>`;
}
```

### Files Modified
- `app/static/app.js` — ~40 changes (Waves 1-3)
- `app/static/crm.js` — ~15 changes (Waves 1-3)
- `app/static/styles.css` — ~10 additions (sticky headers, form validation states, hover styles)
- `app/templates/index.html` — ~5 changes (modal titles, labels)
- `app/services/engagement_scorer.py` — 1 change (return null ghost_rate for 0 outreach)
- `app/routers/crm/companies.py` — 1 change (compute revenue_90d)

### Testing
- All existing 7856 tests must pass
- Add tests for any backend changes (engagement_scorer, companies endpoint)
- Frontend changes tested via manual workflow verification
