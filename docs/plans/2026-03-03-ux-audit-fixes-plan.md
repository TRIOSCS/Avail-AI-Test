# UX Audit Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix 50 confirmed UX issues from 2-session audit, prioritizing workflow blockers then data integrity.

**Architecture:** All changes are frontend JS/CSS/HTML plus 2 small backend fixes. No new models or migrations. Organized into 15 tasks across 3 waves.

**Tech Stack:** Vanilla JS (app.js, crm.js), CSS (styles.css), Jinja2 (index.html), Python (FastAPI services/routers)

---

## Task 1: Add `waitForElement` utility and fix navigation race conditions

**Files:**
- Modify: `app/static/app.js:2359, 2697-2709, 11359-11380`

**Context:** The #1 source of broken navigation is `setTimeout` chains that fire before the DOM is ready. This task adds a reusable utility and fixes all 3 broken navigation paths: dashboard Offers widget, notification deep-linking, and `expandToSubTab`.

**Step 1: Add `waitForElement` utility near top of app.js (after line ~50)**

Add this helper function early in app.js (near other utilities):

```javascript
function waitForElement(selector, timeoutMs = 2000) {
    return new Promise((resolve) => {
        const el = document.querySelector(selector);
        if (el) { resolve(el); return; }
        const observer = new MutationObserver(() => {
            const el = document.querySelector(selector);
            if (el) { observer.disconnect(); resolve(el); }
        });
        observer.observe(document.body, { childList: true, subtree: true });
        setTimeout(() => { observer.disconnect(); resolve(null); }, timeoutMs);
    });
}
```

**Step 2: Fix `expandToSubTab` (line 2697-2709)**

Replace the current function:

```javascript
// BEFORE (line 2697-2709):
async function expandToSubTab(reqId, tabName) {
    if (window.__isMobile) {
        _ddActiveTab[reqId] = tabName;
        _openMobileDrillDown(reqId);
        return;
    }
    const drow = document.getElementById('d-' + reqId);
    if (!drow) return;
    if (!drow.classList.contains('open')) {
        await toggleDrillDown(reqId);
    }
    _switchDdTab(reqId, tabName);
}

// AFTER:
async function expandToSubTab(reqId, tabName) {
    if (window.__isMobile) {
        _ddActiveTab[reqId] = tabName;
        _openMobileDrillDown(reqId);
        return;
    }
    let drow = document.getElementById('d-' + reqId);
    if (!drow) {
        drow = await waitForElement('#d-' + reqId, 2000);
        if (!drow) return;
    }
    if (!drow.classList.contains('open')) {
        await toggleDrillDown(reqId);
        // Wait for drill-down animation to complete
        await new Promise(r => setTimeout(r, 350));
    }
    _switchDdTab(reqId, tabName);
}
```

**Step 3: Fix dashboard Offers to Review onclick (line 2359)**

Replace the onclick handler:

```javascript
// BEFORE (line 2359):
html += '<div class="cc-row" onclick="sidebarNav(\'reqs\');setTimeout(()=>expandToSubTab(' + o.requisition_id + ',\'offers\'),400)">'

// AFTER:
html += '<div class="cc-row" onclick="sidebarNav(\'reqs\',document.getElementById(\'navReqs\'));expandToSubTab(' + o.requisition_id + ',\'offers\')">'
```

Note: `expandToSubTab` is now async and uses `waitForElement`, so the setTimeout is no longer needed.

**Step 4: Fix `_notifClickAction` (line 11359-11380)**

Replace the entire function:

```javascript
function _notifClickAction(n) {
    const close = `markNotifRead(${n.id});document.getElementById('notifPanel').classList.remove('open');`;
    if (n.type === 'offer_pending_review' && n.requisition_id)
        return close + `sidebarNav('reqs',document.getElementById('navReqs'));expandToSubTab(${n.requisition_id},'offers')`;
    if (n.type && n.type.startsWith('buyplan_') && n.buy_plan_id)
        return close + `showBuyPlans();setTimeout(()=>openBuyPlanDetailV3(${n.buy_plan_id}),300)`;
    if ((n.type === 'quote_won' || n.type === 'quote_lost') && n.requisition_id)
        return close + `sidebarNav('reqs',document.getElementById('navReqs'));expandToSubTab(${n.requisition_id},'quotes')`;
    if (n.vendor_card_id)
        return close + `openVendorPopup(${n.vendor_card_id})`;
    if (n.requisition_id)
        return close + `sidebarNav('reqs',document.getElementById('navReqs'));expandToSubTab(${n.requisition_id},'sourcing')`;
    if (n.company_id)
        return close + `goToCompany(${n.company_id})`;
    return `markNotifRead(${n.id})`;
}
```

**Step 5: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --tb=no -q 2>&1 | tail -5
```

Expected: All pass (no backend changes in this task).

**Step 6: Commit**

```bash
git add app/static/app.js
git commit -m "fix(nav): replace setTimeout chains with waitForElement for reliable deep-linking

Fixes: Offers to Review widget, notification deep-linking, expandToSubTab race condition"
```

---

## Task 2: Fix RFQ modal silent failures and Build Quote button

**Files:**
- Modify: `app/static/app.js:5938-5958, 8250-8252, 3172, 3237`

**Step 1: Add user feedback to `openBatchRfqModal` early return (line 8252)**

```javascript
// BEFORE:
if (!groups.length) return;

// AFTER:
if (!groups.length) { showToast('Select sightings first to send RFQs', 'warn'); return; }
```

**Step 2: Add feedback to `ddSendBulkRfq` early return (line 5940)**

```javascript
// BEFORE:
if (!sel || !sel.size) return;

// AFTER:
if (!sel || !sel.size) { showToast('Select sightings first', 'warn'); return; }
```

**Step 3: Fix Build Quote button re-render after offer toggle**

Find the `ddToggleOffer` function. It's called from checkbox onclick at line 3237. Search for `function ddToggleOffer` and ensure it updates the Build Quote button after toggling:

```javascript
// After the selection Set is updated in ddToggleOffer, add:
const btn = document.getElementById('ddBuildQuoteBtn-' + reqId);
if (btn) {
    const sel = _ddSelectedOffers[reqId] || new Set();
    btn.textContent = 'Build Quote (' + sel.size + ')';
    btn.disabled = sel.size === 0;
    btn.style.opacity = sel.size === 0 ? '.5' : '';
}
```

**Step 4: Run tests and commit**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --tb=no -q 2>&1 | tail -5
git add app/static/app.js
git commit -m "fix(rfq): add user feedback for empty selections, fix Build Quote button re-render"
```

---

## Task 3: Make pipeline funnel clickable and fix dashboard toggle label

**Files:**
- Modify: `app/static/app.js:2311-2321`
- Modify: `app/static/styles.css` (add hover style for pipe items)

**Step 1: Add onclick handlers to pipeline bar (lines 2311-2321)**

Replace the pipeline bar rendering:

```javascript
// AFTER:
html += '<div class="cc-pipeline-bar">'
    + '<span class="cc-pipe-item cc-pipe-click" onclick="sidebarNav(\'reqs\',document.getElementById(\'navReqs\'));loadReqList({status:\'open\'})"><span class="cc-pipe-num">' + (pipeline.active_reqs || 0) + '</span> Active</span>'
    + '<span class="cc-pipe-sep">&rarr;</span>'
    + '<span class="cc-pipe-item cc-pipe-click" onclick="sidebarNav(\'reqs\',document.getElementById(\'navReqs\'));loadReqList({status:\'quoted\'})"><span class="cc-pipe-num">' + (pipeline.quotes_out || 0) + '</span> Quoted</span>'
    + '<span class="cc-pipe-sep">&rarr;</span>'
    + '<span class="cc-pipe-item cc-pipe-click" style="color:var(--green)" onclick="sidebarNav(\'reqs\',document.getElementById(\'navReqs\'));loadReqList({status:\'won\'})"><span class="cc-pipe-num">' + (pipeline.won_this_month || 0) + '</span> Won</span>'
    + '<span class="cc-pipe-sep">/</span>'
    + '<span class="cc-pipe-item cc-pipe-click" style="color:var(--red)" onclick="sidebarNav(\'reqs\',document.getElementById(\'navReqs\'));loadReqList({status:\'lost\'})"><span class="cc-pipe-num">' + (pipeline.lost_this_month || 0) + '</span> Lost</span>'
    + '<span class="cc-pipe-sep">&rarr;</span>'
    + '<span class="cc-pipe-item cc-pipe-click" style="color:var(--purple)" onclick="showBuyPlans()"><span class="cc-pipe-num">' + (pipeline.buyplans_approved || 0) + '</span> Buy Plans</span>'
    + '</div>';
```

**Step 2: Add CSS for clickable pipe items**

Append to `app/static/styles.css`:

```css
.cc-pipe-click{cursor:pointer;border-radius:4px;padding:2px 6px;transition:background var(--speed)}.cc-pipe-click:hover{background:var(--bg2)}
```

**Step 3: Find and fix the dashboard perspective toggle label**

Search for the toggle rendering around line 1955-1968 in app.js. Add a label if missing:

```javascript
// If the toggle has no label, add one. Example:
// Before the toggle input, add: <span style="font-size:11px;color:var(--muted);margin-right:6px">View:</span>
```

**Step 4: Commit**

```bash
git add app/static/app.js app/static/styles.css
git commit -m "fix(dashboard): make pipeline funnel clickable, add perspective toggle label"
```

---

## Task 4: Fix vendor score inflation — backend + frontend

**Files:**
- Modify: `app/services/engagement_scorer.py:65-73`
- Modify: `app/static/app.js:9935, 10089-10106`
- Test: `tests/test_engagement_scorer.py` (if exists, else verify via existing tests)

**Step 1: Fix backend — return null ghost_rate for 0-outreach vendors (line 69)**

```python
# BEFORE (line 65-73):
    if total_outreach < MIN_OUTREACH_FOR_SCORE:
        return {
            "engagement_score": COLD_START_SCORE,
            "response_rate": 0,
            "ghost_rate": 1.0 if total_outreach > 0 and total_responses == 0 else 0,
            "recency_score": 0,
            "velocity_score": 0,
            "win_rate": 0,
        }

# AFTER:
    if total_outreach < MIN_OUTREACH_FOR_SCORE:
        return {
            "engagement_score": COLD_START_SCORE,
            "response_rate": 0,
            "ghost_rate": 1.0 if total_outreach > 0 and total_responses == 0 else None,
            "recency_score": 0,
            "velocity_score": 0,
            "win_rate": 0,
        }
```

**Step 2: Fix frontend — show "0/100" for no-data vendor factors (lines 10089-10106)**

Where ghost_rate, delivery, velocity scores are computed, add the "no data" guard:

```javascript
// BEFORE (line 10089-10091):
const ghostScore = v.ghost_rate != null
    ? Math.round((1 - v.ghost_rate) * 100)
    : null;

// AFTER — this line is already correct since ghost_rate will now be null.
// But also fix the display at line 10105:
// BEFORE:
{ label: 'Ghost Rate', score: ghostScore, detail: v.ghost_rate != null ? Math.round(v.ghost_rate * 100) + '% ghost' : 'No data' },

// AFTER — change 'No data' to show 0/100 with explanation:
{ label: 'Ghost Rate', score: v.total_outreach > 0 ? ghostScore : 0, detail: v.total_outreach > 0 ? (v.ghost_rate != null ? Math.round(v.ghost_rate * 100) + '% ghost' : 'No data') : 'No RFQ history' },
```

Apply same pattern to delivery_reliability and response_velocity factors.

**Step 3: Fix vendor list score display (line 9935)**

```javascript
// BEFORE:
const score = c.vendor_score != null ? Math.round(c.vendor_score) : 0;

// AFTER:
const score = c.vendor_score != null ? Math.round(c.vendor_score) : null;
```

And in the HTML rendering, show "New" instead of "0":

```javascript
// Where score is displayed in vendor row, change:
score != null ? score : '<span style="color:var(--muted);font-size:10px">New</span>'
```

**Step 4: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_engagement_scorer.py tests/test_vendor_score.py -v --tb=short 2>&1 | tail -20
```

Update any failing test expectations for the `ghost_rate: None` change.

**Step 5: Commit**

```bash
git add app/services/engagement_scorer.py app/static/app.js tests/
git commit -m "fix(vendors): show 0/100 with explanation for vendors with no RFQ history

Backend: return ghost_rate=None for 0-outreach vendors
Frontend: display 0 score with 'No RFQ history' tooltip instead of inflated 100"
```

---

## Task 5: Fix "Needs Attention" vs "No Recent Activity" filters

**Files:**
- Modify: `app/static/crm.js:322-326`

**Step 1: Differentiate the two filters**

```javascript
// BEFORE (line 322-326):
if (_custFilterMode === 'at-risk') filtered = filtered.filter(c => _custHealthColor(c) === 'red');
if (_custFilterMode === 'stale') {
    const daysSince = window.daysSince || (() => 999);
    filtered = filtered.filter(c => daysSince(c.last_enriched_at) > 30);
}

// AFTER:
if (_custFilterMode === 'at-risk') {
    // "Needs Attention" = has open reqs but no recent offer/quote activity (14+ days)
    filtered = filtered.filter(c => {
        const openReqs = c.open_req_count || 0;
        const daysSince = window.daysSince || (() => 999);
        const lastActivity = daysSince(c.last_req_date || c.last_enriched_at);
        return openReqs > 0 && lastActivity > 14;
    });
}
if (_custFilterMode === 'stale') {
    // "No Recent Activity" = no requisitions or activity in 90+ days
    const daysSince = window.daysSince || (() => 999);
    filtered = filtered.filter(c => daysSince(c.last_req_date || c.last_enriched_at) > 90);
}
```

**Step 2: Add tooltips to filter chips in index.html**

Find the chips (around line 271-272 of index.html):

```html
<!-- BEFORE: -->
<span class="chip" data-value="at-risk" onclick="setCustFilter('at-risk',this)">Needs Attention</span>
<span class="chip" data-value="stale" onclick="setCustFilter('stale',this)">No Recent Activity</span>

<!-- AFTER: -->
<span class="chip" data-value="at-risk" onclick="setCustFilter('at-risk',this)" title="Accounts with open reqs but no activity in 14+ days">Needs Attention</span>
<span class="chip" data-value="stale" onclick="setCustFilter('stale',this)" title="Accounts with no requisition activity in 90+ days">No Recent Activity</span>
```

**Step 3: Commit**

```bash
git add app/static/crm.js app/templates/index.html
git commit -m "fix(accounts): differentiate 'Needs Attention' vs 'No Recent Activity' filters

Needs Attention = open reqs with no activity 14+ days
No Recent Activity = no req activity in 90+ days"
```

---

## Task 6: Make Open Reqs count clickable + fix pipeline req links

**Files:**
- Modify: `app/static/crm.js:400, 808, 1121`

**Step 1: Make Open Reqs clickable in table (line 400)**

```javascript
// BEFORE:
<td>${openReqs || '<span style="color:var(--muted)">0</span>'}</td>

// AFTER:
<td>${openReqs > 0 ? '<a href="javascript:void(0)" onclick="event.stopPropagation();sidebarNav(\'reqs\',document.getElementById(\'navReqs\'));loadReqList({company_id:' + c.id + '})" style="color:var(--blue);font-weight:600;text-decoration:none" title="View open requisitions">' + openReqs + '</a>' : '<span style="color:var(--muted)">0</span>'}</td>
```

**Step 2: Make Open Reqs clickable in drawer (line 808)**

```javascript
// BEFORE:
<div><div style="font-size:22px;font-weight:700;color:var(--blue)">${openReqs}</div><div style="font-size:10px;color:var(--muted)">Open Reqs</div></div>

// AFTER:
<div ${openReqs > 0 ? 'style="cursor:pointer" onclick="sidebarNav(\'reqs\',document.getElementById(\'navReqs\'));loadReqList({company_id:' + c.id + '})" title="View open requisitions"' : ''}><div style="font-size:22px;font-weight:700;color:var(--blue)">${openReqs}</div><div style="font-size:10px;color:var(--muted)">Open Reqs</div></div>
```

**Step 3: Verify pipeline req link uses goToReq (line 1121)**

Check if the onclick at line 1121 of crm.js already uses `goToReq()`. If it does, this is already correct. If not, fix it to navigate directly to the requisition.

**Step 4: Commit**

```bash
git add app/static/crm.js
git commit -m "fix(accounts): make Open Reqs count clickable, navigates to filtered req list"
```

---

## Task 7: Fix Log Offer UX — auto-select part, vendor autocomplete indicator

**Files:**
- Modify: `app/static/app.js:6084-6165`

**Step 1: Auto-select part when req has single requirement**

Find the function that opens the Log Offer modal (search for `openLogOffer` or the modal opening logic). When populating the part dropdown, if there's only one option, auto-select it:

```javascript
// After populating the part select dropdown:
const partSel = document.getElementById('loReqPart');
if (partSel && partSel.options.length === 2) { // 1 option + placeholder
    partSel.selectedIndex = 1;
}
```

**Step 2: Add visual indicator to vendor autocomplete field**

Find the vendor input field in index.html (search for `loVendor`). Add a search icon or placeholder:

```html
<!-- Change the vendor input to show it's an autocomplete: -->
<input id="loVendor" placeholder="Type to search vendors..." autocomplete="off">
```

And add a subtle search icon via CSS or inline style.

**Step 3: Commit**

```bash
git add app/static/app.js app/templates/index.html
git commit -m "fix(offers): auto-select part for single-part reqs, add vendor search indicator"
```

---

## Task 8: Add inline form validation styles

**Files:**
- Modify: `app/static/app.js:6157-6158, 7335-7338`
- Modify: `app/static/styles.css`

**Step 1: Add CSS for inline validation**

Append to styles.css:

```css
.field-error{border-color:var(--red)!important;box-shadow:0 0 0 2px rgba(239,68,68,.15)}.field-error-msg{color:var(--red);font-size:11px;margin-top:2px}
```

**Step 2: Add inline error highlighting to Log Offer validation (lines 6157-6158)**

```javascript
// BEFORE:
if (!vendor) { showToast('Vendor name is required', 'error'); return; }
if (!mpn) { showToast('Select a part', 'error'); return; }

// AFTER:
const vendorEl = document.getElementById('loVendor');
const partEl = document.getElementById('loReqPart');
if (vendorEl) vendorEl.classList.remove('field-error');
if (partEl) partEl.classList.remove('field-error');
if (!vendor) { showToast('Vendor name is required', 'error'); if (vendorEl) { vendorEl.classList.add('field-error'); vendorEl.focus(); } return; }
if (!mpn) { showToast('Select a part', 'error'); if (partEl) { partEl.classList.add('field-error'); partEl.focus(); } return; }
```

**Step 3: Add inline error highlighting to New Req validation (lines 7335-7338)**

Same pattern — add `field-error` class to invalid fields.

**Step 4: Commit**

```bash
git add app/static/app.js app/static/styles.css
git commit -m "fix(forms): add inline validation with red borders on invalid fields"
```

---

## Task 9: Add sticky header for sightings scroll

**Files:**
- Modify: `app/static/styles.css`
- Modify: `app/static/app.js` (drill-down header area)

**Step 1: Add sticky sub-header for drill-down panels**

The drill-down panel for a requisition contains tabs and action buttons at the top. Make this area sticky:

```css
.dd-panel .dd-header{position:sticky;top:0;background:var(--bg);z-index:3;padding-bottom:4px;border-bottom:1px solid var(--border)}
```

Find where the drill-down header (tab pills + action buttons) is rendered in app.js and ensure it has a wrapping div with class `dd-header`.

**Step 2: Commit**

```bash
git add app/static/styles.css app/static/app.js
git commit -m "fix(sightings): add sticky header for drill-down panels to prevent scroll trap"
```

---

## Task 10: Fix vendor destructive actions — add confirmation dialogs

**Files:**
- Modify: `app/static/app.js:9048-9056`

**Step 1: Add confirmation to Delete Vendor (line 9055)**

```javascript
// BEFORE:
`<button class="btn btn-danger btn-sm" onclick="deleteVendor(${card.id},'${escAttr(card.display_name)}')" ...>Delete Vendor</button>`

// AFTER:
`<button class="btn btn-danger btn-sm" onclick="if(confirm('Permanently delete ${escAttr(card.display_name)}? This cannot be undone.'))deleteVendor(${card.id},'${escAttr(card.display_name)}')" ...>Delete Vendor</button>`
```

**Step 2: Add confirmation to Blacklist toggle (line 9051)**

```javascript
// BEFORE:
onclick="vpToggleBlacklist(${card.id}, ${!blOn})"

// AFTER (only for enabling blacklist, not removing):
onclick="${blOn ? '' : 'if(!confirm(\'Blacklist ' + escAttr(card.display_name) + '? They will be hidden from all search results.\'))return;'}vpToggleBlacklist(${card.id}, ${!blOn})"
```

**Step 3: Reorder — move destructive actions to bottom of modal**

Move the blacklist/delete section from the top of the vendor popup to the bottom (after the scorecard section).

**Step 4: Commit**

```bash
git add app/static/app.js
git commit -m "fix(vendors): add confirmation dialogs for blacklist/delete, move to bottom of modal"
```

---

## Task 11: Fix notification metadata cleanup and empty state copy

**Files:**
- Modify: `app/static/app.js:11416-11419, 3161`

**Step 1: Expand notification cleanup regex (line 11418)**

```javascript
// BEFORE:
cleanNotes = cleanNotes.replace(/\b\w+_id=\d+/g, '').replace(/\bstatus=\w+/g, '').replace(/\s{2,}/g, ' ').trim();

// AFTER:
cleanNotes = cleanNotes
    .replace(/\{[^}]*\}/g, '')            // Remove JSON objects
    .replace(/\b\w+_id[=:]\s*\d+/g, '')   // Remove id fields (key=val or key: val)
    .replace(/\bstatus[=:]\s*\w+/g, '')    // Remove status fields
    .replace(/\b(null|undefined|NaN)\b/g, '')  // Remove null/undefined
    .replace(/[,;]+\s*/g, ' ')             // Replace delimiters with spaces
    .replace(/\s{2,}/g, ' ')
    .trim();
```

**Step 2: Fix offers empty state copy (line 3161)**

```javascript
// BEFORE:
'No offers yet — use <b>+ Log Offer</b> above to record a vendor offer, or send RFQs from the <b>Sightings</b> tab to request quotes'

// AFTER (remove reference to non-existent "vendor tab"):
'No offers yet — use <b>+ Log Offer</b> above to record a vendor offer, or send RFQs from the <b>Sourcing</b> tab to request quotes'
```

**Step 3: Commit**

```bash
git add app/static/app.js
git commit -m "fix(ui): expand notification metadata cleanup, fix offers empty state copy"
```

---

## Task 12: Fix terminology and modal titles

**Files:**
- Modify: `app/templates/index.html:742`
- Modify: `app/static/app.js` (vendor tier display)

**Step 1: Fix "New Company" modal title (index.html line 742)**

```html
<!-- BEFORE: -->
<h2>New Company</h2>

<!-- AFTER: -->
<h2>New Account</h2>
```

**Step 2: Fix "Dev" vendor tier label to "Developing"**

Search app.js for where tier badges are rendered in the vendor list. Change abbreviated "Dev" to full "Developing".

**Step 3: Commit**

```bash
git add app/templates/index.html app/static/app.js
git commit -m "fix(ui): rename 'New Company' to 'New Account', expand 'Dev' to 'Developing'"
```

---

## Task 13: Compute revenue_90d for accounts

**Files:**
- Modify: `app/routers/crm/companies.py:89-156`
- Test: Run existing company tests

**Step 1: Add revenue_90d computation**

In the stats query (around line 95-112), add a subquery for 90-day revenue from won quotes:

```python
# After the existing stats_rows query, add:
from datetime import timedelta
ninety_days_ago = datetime.now(timezone.utc) - timedelta(days=90)

rev_rows = (
    db.query(
        CustomerSite.company_id,
        sqlfunc.coalesce(sqlfunc.sum(Quote.subtotal), 0).label("revenue_90d"),
    )
    .join(Quote, Quote.customer_site_id == CustomerSite.id)
    .filter(
        CustomerSite.company_id.in_(company_ids),
        Quote.status == "won",
        Quote.created_at >= ninety_days_ago,
    )
    .group_by(CustomerSite.company_id)
    .all()
)
rev_map = {r.company_id: float(r.revenue_90d) for r in rev_rows}
```

Then in the response mapping (around line 152), add:

```python
"revenue_90d": rev_map.get(c.id),
```

**Step 2: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_crm*.py -v --tb=short 2>&1 | tail -20
```

**Step 3: Commit**

```bash
git add app/routers/crm/companies.py
git commit -m "feat(accounts): compute revenue_90d from won quotes in last 90 days"
```

---

## Task 14: Add header counter click handlers and remaining polish

**Files:**
- Modify: `app/static/app.js` (header counters)
- Modify: `app/static/styles.css` (various small fixes)
- Modify: `app/templates/index.html` (filter tooltips, labels)

**Step 1: Make header "Offers" and "Due" counters clickable**

Find where these counters are rendered and add onclick handlers:
- "Offers" → navigate to offers view
- "Due" → navigate to upcoming deadlines

**Step 2: Add tooltips to all filter chips (accounts and vendors)**

Add `title` attributes explaining each filter's criteria.

**Step 3: Fix vendor side panel — show key metrics inline**

In the vendor list side panel, add sighting_count, last_contact_at, and score alongside display_name.

**Step 4: Add "+ Add Vendor" button to vendor list**

Add a button next to the search bar that opens a simple vendor creation form.

**Step 5: Commit**

```bash
git add app/static/app.js app/static/styles.css app/templates/index.html
git commit -m "fix(ui): clickable header counters, filter tooltips, vendor list enhancements"
```

---

## Task 15: Full test suite + coverage check + final commit

**Files:**
- All modified files

**Step 1: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: All tests pass.

**Step 2: Run coverage check**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q 2>&1 | tail -10
```

Expected: Coverage ≥ 98%.

**Step 3: Fix any failing tests**

If engagement_scorer tests fail due to `ghost_rate: None` change, update assertions.

**Step 4: Verify no regressions**

Check that the app starts cleanly:

```bash
cd /root/availai && docker compose up -d --build && sleep 10 && docker compose logs --tail 30 app
```

---

## Parallelization Guide

These task groups can run concurrently (different files):

| Group | Tasks | Primary File |
|-------|-------|-------------|
| A | 1, 2, 3 | app.js (navigation + dashboard) |
| B | 4 | engagement_scorer.py + app.js (vendor scores) |
| C | 5, 6 | crm.js (accounts) |
| D | 7, 8, 9, 10, 11, 12 | app.js (forms + modals + polish) |
| E | 13 | companies.py (backend) |

**Recommended execution order:**
1. Tasks 1-3 first (workflow blockers — highest impact)
2. Tasks 4-6 next (data integrity)
3. Tasks 7-14 in parallel batches (polish)
4. Task 15 last (verification)
