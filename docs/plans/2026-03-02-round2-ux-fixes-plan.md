# Round 2 UX Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix all 17 Round 2 UI/UX issues across 5 subsystems in the AvailAI platform.

**Architecture:** Frontend-first approach — 14 of 17 issues are pure JS fixes in `app.js` and `crm.js`. Only R2-13 (account stats) requires a backend change to `companies.py`. No migrations needed.

**Tech Stack:** Vanilla JS (app.js, crm.js), FastAPI (companies.py), Jinja2 (index.html), pytest

**XSS note:** The existing codebase uses `esc()` and `escAttr()` helper functions to sanitize all user-controlled values before DOM insertion. All fixes in this plan follow the same pattern — every dynamic value passes through `esc()` before rendering.

---

## Task 1: Fix Sightings Filter Tabs — All Show Identical Results (R2-5)

**Files:**
- Modify: `app/static/app.js:5548-5557` — `_ddApplyFilters` type filter logic

**Step 1: Locate and read the current filter logic**

Read `app/static/app.js` lines 5536-5559 to see `_ddApplyFilters`.

**Step 2: Fix the filter logic**

In the type filter block (lines 5548-5557), change the `available` and `na` conditions:
- `available`: Change from `!s.is_unavailable` to `s.qty_available != null && s.qty_available > 0`
- `na`: Change from `!!s.is_unavailable` to `s.qty_available == null || s.qty_available <= 0`

The `exact` and `sub` filters comparing `mpn_matched` to `groupLabel` are already correct.

**Step 3: Commit**

```bash
git add app/static/app.js
git commit -m "fix(sightings): filter tabs now use qty_available instead of unset is_unavailable flag (R2-5)"
```

---

## Task 2: Fix Sightings Sort — Add Fulfillability as Primary Sort (R2-16)

**Files:**
- Modify: `app/static/app.js:5760-5765` — sort logic in `_renderSourcingDrillDown`
- Reference: `app/static/app.js:5775-5777` — `_ddReqCache` for target_qty

**Step 1: Read the current sort logic and target_qty access**

Read `app/static/app.js` lines 5760-5778.

**Step 2: Replace the sort with a two-tier fulfillability sort**

Move the `_req` / `_reqs` lookup (currently at lines 5775-5776) to BEFORE the sort at line 5760. Then replace the sort:
- Primary: vendors where `qty_available >= target_qty` sort first (can-fill tier)
- Secondary: vendor_score descending within each tier

The `_reqs` variable is `_ddReqCache[reqId] || []` and `_req` is `_reqs.find(r => r.id == rId)`. Use `_req?.target_qty || 0` as the target.

**Step 3: Commit**

```bash
git add app/static/app.js
git commit -m "fix(sightings): sort full-fill vendors above partial-fill, then by score (R2-16)"
```

---

## Task 3: Fix RFQ Button Count — Show Vendor Count Instead of Sighting Count (R2-1)

**Files:**
- Modify: `app/static/app.js:5843-5864` — `_updateDdBulkButton`

**Step 1: Read the current button update logic**

Read `app/static/app.js` lines 5843-5864.

**Step 2: Rewrite to count unique vendors, not individual sightings**

Group selected sightings by normalized vendor name (same logic as `ddSendBulkRfq` at line 5916). Count total unique vendors and how many have emails. Display:
- All have email: `Prepare RFQ (N vendor/vendors)`
- Some missing: `Prepare RFQ (X of N vendors)`

**Step 3: Commit**

```bash
git add app/static/app.js
git commit -m "fix(rfq): button shows vendor count instead of sighting count (R2-1)"
```

---

## Task 4: Fix Batch RFQ Modal — Filter to Selected Vendors Only (R2-2)

**Files:**
- Modify: `app/static/app.js:8219-8244` — post-prepare vendor filtering in `openBatchRfqModal`

**Step 1: Read the current rfqVendorData assignment**

Read `app/static/app.js` lines 8211-8250.

**Step 2: Add vendor name filter after rfq-prepare returns**

After the `rfqVendorData = data.vendors.map(...)` block (after line 8244), add:
- Build a Set of selected vendor names (normalized lowercase) from the `groups` parameter
- Filter `rfqVendorData` to only include vendors whose names match

**Step 3: Commit**

```bash
git add app/static/app.js
git commit -m "fix(rfq): batch modal now filters to selected vendors only (R2-2)"
```

---

## Task 5: Fix Batch RFQ Modal — Unstick "Finding Contacts" (R2-3)

**Files:**
- Modify: `app/static/app.js:8275-8300` — vendor lookup Promise.all

**Step 1: Read the current lookup logic**

Read `app/static/app.js` lines 8256-8310.

**Step 2: Add per-vendor timeout to prevent indefinite hanging**

Wrap each vendor lookup in `Promise.race([fetchPromise, timeoutPromise])` with a 15-second timeout. On timeout, mark vendor as `no_email` with reason "Lookup timed out (15s)".

Also add a "Skip remaining" button that appears after 5 seconds. When clicked, it triggers `abortCtrl.abort()` to let the modal advance with partial results. Clear the skip timer in the `finally` block.

**Step 3: Commit**

```bash
git add app/static/app.js
git commit -m "fix(rfq): add 15s per-vendor timeout + skip button for contact lookup (R2-3)"
```

---

## Task 6: Fix RFQ Subject/Message Blank — Empty Draft Override (R2-4)

**Files:**
- Modify: `app/static/app.js:8675-8709` — `renderRfqMessage`

**Step 1: Read the current draft restoration logic**

Read `app/static/app.js` lines 8675-8718.

**Step 2: Fix draft validation to only restore non-empty drafts**

In the `if (saved)` block (lines 8683-8688):
- Only restore draft if BOTH `draft.subject` and `draft.body` are non-empty strings
- If empty/corrupt, remove from localStorage and set `saved = null`
- Change the `} else {` on the auto-generation block to `if (!saved) {` so it runs when draft was cleared

**Step 3: Commit**

```bash
git add app/static/app.js
git commit -m "fix(rfq): skip empty/corrupt drafts, fall through to auto-populated subject+body (R2-4)"
```

---

## Task 7: Fix Archive Outcome Prompt — Replace prompt() with Modal (R2-10)

**Files:**
- Modify: `app/templates/index.html` — add archive outcome modal markup
- Modify: `app/static/app.js:7368-7403` — `archiveFromList` to use modal

**Step 1: Add modal HTML to index.html**

Find an appropriate place after existing modals. Add an `archiveOutcomeModal` with three styled buttons (Won=green, Lost=red, Just Archive=gray) plus Cancel. Use the existing `modal-bg` / `modal` / `openModal` / `closeModal` patterns.

**Step 2: Replace the prompt() in archiveFromList with a modal**

Rewrite `archiveFromList`:
- For archive view: keep direct restore (no outcome needed)
- For non-archive views: populate the modal buttons with the reqId and show the modal via `openModal('archiveOutcomeModal')`
- Create `_archiveWithOutcome(reqId, outcome)` function that closes modal, sets outcome if won/lost, archives, removes from DOM, and re-renders

**Step 3: Export `_archiveWithOutcome` in the window/export list**

Near line 11823, add `_archiveWithOutcome` to the exports.

**Step 4: Commit**

```bash
git add app/static/app.js app/templates/index.html
git commit -m "fix(archive): replace prompt() with styled Win/Lost/Archive modal (R2-10)"
```

---

## Task 8: Fix Archive Expanded Row Tabs (R2-9)

**Files:**
- Modify: `app/static/app.js:2669-2672` — `_ddSubTabs`

**Step 1: Read the current tab logic**

Read `app/static/app.js` lines 2669-2677.

**Step 2: Add reqStatusFilter check to _ddSubTabs**

Change the `archive` condition from:
```
if (mainView === 'archive')
```
to:
```
if (mainView === 'archive' || _reqStatusFilter === 'archive')
```

This ensures archived items get all 5 tabs (parts, offers, quotes, activity, files) even when `_currentMainView` is 'rfq' but viewing archived items.

**Step 3: Commit**

```bash
git add app/static/app.js
git commit -m "fix(archive): show all tabs (offers, quotes, activity) for archived reqs (R2-9)"
```

---

## Task 9: Fix Re-quote Button — Ensure Visible Feedback (R2-11)

**Files:**
- Modify: `app/static/app.js:7416-7432` — `requoteFromList`

**Step 1: Read the current re-quote logic**

Read `app/static/app.js` lines 7416-7433.

**Step 2: Fix the race condition between loadRequisitions and expandToSubTab**

- Add `await` to `loadRequisitions()` (currently missing)
- After load completes, verify `resp.id` exists in `_reqListData` before calling `expandToSubTab`
- If not found, show a fallback toast with the new req ID
- Improve initial toast: "Re-quoted — opening now..."

**Step 3: Commit**

```bash
git add app/static/app.js
git commit -m "fix(rfq): re-quote now awaits data load before expanding, better feedback (R2-11)"
```

---

## Task 10: Fix Offers Empty State Text (R2-7)

**Files:**
- Modify: `app/static/app.js:3148` — offers empty state message

**Step 1: Verify current text**

Read line 3148. The current text already says "Sightings tab" (was fixed previously). If it still says "vendor tab", update it.

**Step 2: Ensure the text is correct**

Target text: `No offers yet — use + Log Offer above to record a vendor offer, or send RFQs from the Sightings tab to request quotes`

**Step 3: Commit (if changed)**

```bash
git add app/static/app.js
git commit -m "fix(offers): empty state references correct Sightings tab (R2-7)"
```

---

## Task 11: Fix Quotes Empty State Text (R2-8)

**Files:**
- Modify: `app/static/app.js:3821` — quotes empty state message

**Step 1: Update the empty state text**

Change from "select offers in the Offers tab and click Build Quote" to:
`No quotes yet — select offers in the Offers tab and click Build Quote to create a customer quote`

The "Build Quote" button DOES exist at line 3159 in the offers tab — the issue is that users land on Quotes without seeing the button. Make the text more actionable by mentioning the Offers tab first.

**Step 2: Commit**

```bash
git add app/static/app.js
git commit -m "fix(quotes): clarify empty state text to guide user to Offers tab first (R2-8)"
```

---

## Task 12: Fix Details Tab — Add Manufacturer Field (R2-17)

**Files:**
- Modify: `app/static/app.js:4895-4908` — `_renderDdDetails` part rendering

**Step 1: Read the current details rendering**

Read `app/static/app.js` lines 4895-4937.

**Step 2: Add manufacturer display after brand**

At line 4902, change the brand display logic:
- Use `r.brand || r.manufacturer` as display brand (so manufacturer shows if no brand)
- If BOTH brand and manufacturer exist and differ, show manufacturer as a separate line below brand: `Mfr: {manufacturer}` in muted text
- All values go through `esc()` for sanitization

**Step 3: Commit**

```bash
git add app/static/app.js
git commit -m "fix(details): show manufacturer field when available (R2-17)"
```

---

## Task 13: Fix Activity Tab — Add Tooltip to Check for Replies Button (R2-6)

**Files:**
- Modify: `app/static/app.js:2809, 2829` — checkForReplies button rendering

**Step 1: Read the current button rendering**

Read `app/static/app.js` lines 2805-2835.

**Step 2: Enhance the button**

- Line 2809 (empty state): Add a visible help text span after the button: `Scans your inbox for vendor responses` in muted italic
- Line 2829 (with activity): Change button text from "Check for Replies" to "Check Inbox" for clarity. Keep the existing `title` attribute for hover detail.

**Step 3: Commit**

```bash
git add app/static/app.js
git commit -m "fix(activity): add visible help text to Check for Replies button (R2-6)"
```

---

## Task 14: Fix Pipeline Tab Navigation — Make Breadcrumb More Visible (R2-12)

**Files:**
- Modify: `app/static/app.js:1976-1981` — `_renderBreadcrumb` styling
- Modify: `app/templates/index.html:185` — breadcrumb container styling

**Step 1: Read the breadcrumb system**

Read `app/static/app.js` lines 1969-1996 and `index.html` line 185.

**Step 2: Make the breadcrumb more prominent**

The system already works (goToReq -> _renderBreadcrumb -> _goBackFromBreadcrumb). Just make it visible:
- In `_renderBreadcrumb`: Change from `<span style="font-size:12px">` to a proper button with a left-arrow SVG icon, styled with `btn btn-ghost btn-sm`
- In `index.html`: Add `background:var(--bg);border-bottom:1px solid var(--border)` to the breadcrumb container
- All values go through `esc()` for sanitization

**Step 3: Commit**

```bash
git add app/static/app.js app/templates/index.html
git commit -m "fix(crm): make pipeline back-navigation breadcrumb more visible (R2-12)"
```

---

## Task 15: Fix Accounts List — Add Win Rate and Last RFQ (R2-13)

**Files:**
- Modify: `app/routers/crm/companies.py:89-128` — add stats to response
- Modify: `app/static/crm.js` — render the stats (find where `win_rate` / `last_req_date` are rendered)
- Test: existing CRM test file

**Step 1: Read the backend companies endpoint and models**

Read `app/routers/crm/companies.py` lines 42-140.
Check if `Requisition` model has `customer_site_id` and `outcome` fields.

**Step 2: Add aggregation sub-queries to the companies endpoint**

After companies are fetched (line 88), run a grouped query on `Requisition` joined through `CustomerSite` to compute:
- `won_count`: count of requisitions with outcome='won'
- `decided_count`: count with outcome in ('won', 'lost')
- `win_rate`: won/decided * 100 (null if no decisions)
- `last_req_date`: max(created_at) from requisitions

Build a `stats_map[company_id]` dict and add `win_rate` and `last_req_date` to each item in the response.

Note: `revenue_90d` requires quote/order value data which may not exist. If the field isn't available, skip it.

**Step 3: Update frontend rendering**

In `crm.js`, find where accounts table renders `a.win_rate` and `a.last_req_date` and ensure it uses the new API fields.

**Step 4: Write a test**

Create a test that sets up companies + customer sites + requisitions with outcomes, calls the companies endpoint, and verifies `win_rate` and `last_req_date` are returned.

**Step 5: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v -k "company" --tb=short
```

**Step 6: Commit**

```bash
git add app/routers/crm/companies.py app/static/crm.js tests/
git commit -m "feat(crm): compute and display win_rate + last_req_date on accounts list (R2-13)"
```

---

## Task 16: Fix Vendor List — Response Rate, Duplicates, Irrelevant Vendors (R2-14)

**Files:**
- Modify: `app/static/crm.js:5449-5514` — `renderVendorScorecards`

**Step 1: Read the vendor scorecard rendering**

Read `app/static/crm.js` lines 5449-5514.

**Step 2: Fix the "Insufficient Data" display**

For vendors with `is_sufficient_data: false`:
- Still show `response_rate` if available (using `metricCell`)
- Change label from "Insufficient Data" to "Low data (N interactions)"
- Collapse the other 4 columns into a single "Low data" cell

**Step 3: Add "Active only" filter toggle**

- Add `let _perfActiveOnly = true;` near the vendor scorecard variables
- Add a checkbox labeled "Active only" next to the search bar
- When checked, filter out vendors with `interaction_count === 0` before rendering
- Wire checkbox `onchange` to toggle `_perfActiveOnly` and reload

**Step 4: Commit**

```bash
git add app/static/crm.js
git commit -m "fix(vendors): show response rate for low-data vendors, add active-only filter (R2-14)"
```

---

## Task 17: Fix Contacts List — Clean Prefixes and Truncation (R2-15)

**Files:**
- Modify: `app/static/app.js:1295-1319` — `renderContacts` row rendering

**Step 1: Read the current contact row rendering**

Read `app/static/app.js` lines 1295-1320.

**Step 2: Add name/title sanitization**

Add a helper function `_sanitizeContactField(val)` that strips leading punctuation (`!:;.-`) and whitespace from strings. Apply it to `c.full_name` and `c.title` in the rendering loop.

For the title column:
- Add `max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap` CSS
- Add `title` attribute with the full (sanitized) title text for hover
- Truncate display text at 50 characters with "..."

All values go through `esc()` for sanitization.

**Step 3: Commit**

```bash
git add app/static/app.js
git commit -m "fix(contacts): sanitize name/title prefixes, fix column truncation (R2-15)"
```

---

## Task 18: Final Verification — Run Full Test Suite + Deploy

**Step 1: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short
```

Expected: All existing tests pass. No regressions.

**Step 2: Run coverage check**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
```

Expected: Coverage stays at or above current level.

**Step 3: Rebuild and deploy**

```bash
cd /root/availai && docker compose up -d --build
```

Wait 30 seconds, then check logs:

```bash
docker compose logs -f app --tail=50
```

Expected: Clean startup, no errors.

---

## Summary

| Task | Issue(s) | File(s) | Risk |
|------|----------|---------|------|
| 1 | R2-5 | app.js | Low — filter logic only |
| 2 | R2-16 | app.js | Low — sort logic only |
| 3 | R2-1 | app.js | Low — display text |
| 4 | R2-2 | app.js | Low — post-filter |
| 5 | R2-3 | app.js | Medium — timeout logic |
| 6 | R2-4 | app.js | Low — draft validation |
| 7 | R2-10 | app.js + index.html | Medium — new modal |
| 8 | R2-9 | app.js | Low — tab array |
| 9 | R2-11 | app.js | Low — await fix |
| 10 | R2-7 | app.js | Low — text change |
| 11 | R2-8 | app.js | Low — text change |
| 12 | R2-17 | app.js | Low — add field |
| 13 | R2-6 | app.js | Low — tooltip |
| 14 | R2-12 | app.js + index.html | Low — styling |
| 15 | R2-13 | companies.py + crm.js + tests | Medium — backend query |
| 16 | R2-14 | crm.js | Low — display logic |
| 17 | R2-15 | app.js | Low — sanitization |
| 18 | — | — | Verification |
