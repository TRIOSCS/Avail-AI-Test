# Trouble Ticket Resolution — Final 21 Tickets Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix 21 remaining UX/functional issues across 5 workstreams — all frontend except one data fix already done.

**Architecture:** All fixes are in vanilla JS (app.js, crm.js, tickets.js), CSS (styles.css), and HTML (index.html). No new models, migrations, or backend changes. Each workstream targets independent files/sections, so workstreams can be parallelized.

**Tech Stack:** Vanilla JS, CSS, Jinja2 templates (index.html)

**Note:** All dynamic HTML in this plan follows existing codebase patterns with the project's `esc()` helper for XSS-safe escaping. Use `escAttr()` for HTML attribute values.

---

## Workstream 1: RFQ / Sourcing Fixes (7 tickets)

### Task 1.1: Vendor Autocomplete Dropdown in Sourcing Drill-Down (#646)

**Files:**
- Modify: `app/static/app.js:5758` (vendor filter input)
- Add functions near: `app/static/app.js:5560`

**Step 1: Wrap vendor input in position:relative container with dropdown**

At `app/static/app.js:5758`, wrap the vendor filter input in a relative-positioned span. Add an `ac-dropdown` div after the input (id: `ddVendorAc-${reqId}`). Wire the input's `oninput` to also call `_ddShowVendorSuggestions(reqId, this.value)`. Add `onblur` with 150ms delay to close the dropdown.

**Step 2: Add `_ddShowVendorSuggestions()` and `_ddSelectVendor()` functions**

Add near line 5560, before `_ddApplyFilters`. The suggestion function should:
- Collect unique vendor names from `_ddDrillData[reqId].sightings`
- Filter by typed value (case-insensitive includes)
- Show top 15 matches in dropdown with safe text rendering via `esc()`
- On mousedown, set input value and trigger filter
- Close dropdown on blur with 150ms delay

Follow the existing Log Offer autocomplete pattern at lines 6102-6143.

**Step 3: Add hover style for dropdown items**

In `app/static/styles.css`, add if not present:
```css
.ac-dropdown .ac-item:hover{background:var(--bg2)}
```

**Step 4: Commit**
```bash
git add app/static/app.js app/static/styles.css
git commit -m "feat: add vendor autocomplete in sourcing drill-down. Fixes #646."
```

---

### Task 1.2: Archive Offers Badge Clickable (#658)

**Files:**
- Modify: `app/static/app.js:4982`

**Step 1: Add onclick to offers badge**

At `app/static/app.js:4982`, add `style="cursor:pointer"`, update title to include "click to view", and add `onclick="event.stopPropagation();expandToSubTab(${r.id},'offers')"` to the offers badge span.

**Step 2: Commit**
```bash
git add app/static/app.js
git commit -m "fix: make OFFERS badge clickable to expand offers tab. Fixes #658."
```

---

### Task 1.3: Archive MATCHES Column Shows Offer Count (#661)

**Files:**
- Modify: `app/static/app.js:6585-6588`

**Step 1: Show offer_count for archive MATCHES**

At `app/static/app.js:6585`, change the match badge logic to prefer `offer_count` over `proactive_match_count` for the archive view:
```javascript
const pmCnt = r.proactive_match_count || 0;
const offerCnt = r.offer_count || 0;
const matchVal = offerCnt > 0 ? offerCnt : pmCnt;
const matchBadge = matchVal > 0
    ? `<span style="color:var(--green);font-weight:600">${matchVal}</span>`
    : '<span style="color:var(--muted)">\u2014</span>';
```

**Step 2: Commit**
```bash
git add app/static/app.js
git commit -m "fix: archive MATCHES column shows offer count. Fixes #661."
```

---

### Task 1.4: RFQ Filter Persistence Across Tabs (#660)

**Files:**
- Modify: `app/static/app.js:7069-7070`

**Step 1: Remove filter reset from setMainView**

At `app/static/app.js:7069-7070`, remove these two lines:
```javascript
    _activeFilters = {};
    _myReqsOnly = false;
```

Keep `_toolbarQuickFilter = '';` on the next line since quick filters are tab-specific.

**Step 2: Commit**
```bash
git add app/static/app.js
git commit -m "fix: preserve RFQ filters when switching between Open/Sourcing/Archive. Fixes #660."
```

---

### Task 1.5: Sightings "Available" Filter Fix (#668)

**Files:**
- Modify: `app/static/app.js:5589`

**Step 1: Fix Available filter to handle missing qty_available**

At `app/static/app.js:5589`, change:
```javascript
if (tf === 'available') return !s.is_unavailable && s.qty_available != null && s.qty_available > 0;
```
to:
```javascript
if (tf === 'available') return !s.is_unavailable;
```

The issue: many connectors don't populate `qty_available`, so requiring `qty_available > 0` filters out most sightings. Treating any non-unavailable sighting as "available" matches user expectations.

**Step 2: Commit**
```bash
git add app/static/app.js
git commit -m "fix: Available filter includes sightings without explicit qty data. Fixes #668."
```

---

### Task 1.6: Verify Archive Sub-Tabs (#656)

**Files:**
- Check: `app/static/app.js:2700`

**Step 1: Verify archive tabs render**

Line 2700 returns `['parts', 'offers', 'quotes', 'activity', 'files']` for archive. Commit `f82e675` (R2-9) already added all archive tabs. Verify in browser, then resolve.

**Step 2: Resolve**
```sql
UPDATE trouble_tickets SET status='resolved', resolution_notes='Archive tabs already implemented in commit f82e675 (R2-9)', resolved_at=NOW() WHERE id=656 AND status != 'resolved';
```

---

### Task 1.7: RFQ Creation — Auto-Expand to Parts Tab (#667)

**Files:**
- Modify: `app/static/app.js:7393-7394`

**Step 1: After creation, expand to Parts tab instead of just toggling drill-down**

At `app/static/app.js:7393-7394`, change:
```javascript
        await loadRequisitions();
        toggleDrillDown(data.id);
```
to:
```javascript
        await loadRequisitions();
        expandToSubTab(data.id, 'parts');
        showToast('Requisition created \u2014 add parts below', 'info');
```

**Step 2: Commit**
```bash
git add app/static/app.js
git commit -m "fix: auto-expand to Parts tab after creating new req. Fixes #667."
```

---

## Workstream 2: Materials & Scorecard (5 tickets)

### Task 2.1: Fix Materials Nav Highlight (#642)

**Files:**
- Modify: `app/static/app.js:1123-1128`

**Step 1: Add explicit navHighlight call**

At `app/static/app.js:1124`, after `showView('view-materials');`, add:
```javascript
    navHighlight(document.getElementById('navMaterials'));
```

**Step 2: Commit**
```bash
git add app/static/app.js
git commit -m "fix: ensure Materials nav stays highlighted. Fixes #642."
```

---

### Task 2.2: Verify Import Stock Form Hidden by Default (#648)

**Files:**
- Check: `app/templates/index.html:499`

Line 499 has `class="card u-hidden"` — the form IS hidden by default. The toggle button at line 497 controls visibility. Verify in browser. If correct, resolve.

```sql
UPDATE trouble_tickets SET status='resolved', resolution_notes='Import Stock form already hidden by default (u-hidden class)', resolved_at=NOW() WHERE id=648 AND status != 'resolved';
```

---

### Task 2.3: Scorecard Tooltip + Guidance (#653, #654, #657)

**Files:**
- Modify: `app/static/crm.js:5310-5342`

**Step 1: Check if UNIFIED/Prize columns still exist**

Search crm.js for "UNIFIED", "$500", "$250", "Not Qualified". If these were removed in the scorecard refactor (`9058fa4`), resolve #653 and #657 as already fixed.

**Step 2: Add tooltip to Anticipated column header**

At `app/static/crm.js:5320`, add a `title` attribute to the "Anticipated" `<th>`:
```
title="Estimated revenue from open quoted deals"
```

**Step 3: Add conversion rate guidance text**

After the table closing tag (~line 5340), add a paragraph explaining rate thresholds: green >= 30%, amber >= 15%.

**Step 4: Commit**
```bash
git add app/static/crm.js
git commit -m "fix: add scorecard tooltips and conversion guidance. Fixes #653, #654, #657."
```

---

## Workstream 3: Accounts & CRM (3 tickets)

### Task 3.1: Add "+ Add Vendor" Button (#637)

**Files:**
- Modify: `app/templates/index.html:453-455`
- Modify: `app/static/crm.js` (add handler)

**Step 1: Add button to vendor view header**

At `app/templates/index.html:453-455`, add a button after `<h2>Vendors</h2>`:
```html
<button type="button" class="btn btn-primary btn-sm" onclick="openNewVendorModal()" style="margin-left:auto">+ Add Vendor</button>
```

**Step 2: Add openNewVendorModal() in crm.js**

Reuse the existing `addVendorContactModal` (found at index.html:672). The function opens the modal and sets the title to "Add New Vendor".

**Step 3: Export the function in crm.js**

Add `openNewVendorModal` to the crm.js export list.

**Step 4: Commit**
```bash
git add app/templates/index.html app/static/crm.js
git commit -m "feat: add '+ Add Vendor' button to vendor list header. Fixes #637."
```

---

### Task 3.2: Verify Contact Quick-Action Buttons (#640)

**Files:**
- Check: `app/static/crm.js:1228-1233`

Lines 1228-1233 already have Email, Phone, Edit, and Archive buttons. If functional, resolve.

```sql
UPDATE trouble_tickets SET status='resolved', resolution_notes='Contact cards already have Email/Phone/Edit/Archive quick-action buttons (crm.js:1228-1233)', resolved_at=NOW() WHERE id=640 AND status != 'resolved';
```

---

### Task 3.3: Fix Duplicate Company Label in Dropdown (#662)

**Files:**
- Modify: `app/static/crm.js:3877`

**Step 1: Deduplicate when site name matches company name**

At `app/static/crm.js:3877`, change:
```javascript
label: c.name + (sites.length > 1 ? ' \u2014 ' + s.site_name : ''),
```
to:
```javascript
label: sites.length > 1 && s.site_name !== c.name ? c.name + ' \u2014 ' + s.site_name : c.name,
```

**Step 2: Commit**
```bash
git add app/static/crm.js
git commit -m "fix: deduplicate company label when site name matches company name. Fixes #662."
```

---

## Workstream 4: Tickets / Self-Heal + System (5 tickets)

### Task 4.1: Default Tickets Filter to "Submitted" (#650)

**Files:**
- Modify: `app/static/tickets.js:210`

**Step 1: Change default**

Change `var _adminFilter = '';` to `var _adminFilter = 'submitted';`

**Step 2: Commit**
```bash
git add app/static/tickets.js
git commit -m "fix: default tickets view to 'Submitted' filter. Fixes #650."
```

---

### Task 4.2: Add Back Button to New Ticket Form (#644)

**Files:**
- Modify: `app/static/tickets.js:96`

**Step 1: Change button text**

Change `textContent: 'My Tickets',` to `textContent: '\u2190 Back to Tickets',`

**Step 2: Commit**
```bash
git add app/static/tickets.js
git commit -m "fix: rename 'My Tickets' to 'Back to Tickets' on ticket submit form. Fixes #644."
```

---

### Task 4.3: Verify Offers Counter (#664)

**Files:**
- Check: `app/static/app.js:6469`

The offers counter calls `setToolbarQuickFilter('green')` on click. Verify it filters the req list in browser. If working, resolve.

```sql
UPDATE trouble_tickets SET status='resolved', resolution_notes='Offers counter onclick calls setToolbarQuickFilter which filters list - verified functional', resolved_at=NOW() WHERE id=664 AND status != 'resolved';
```

---

### Task 4.4: Improve Proactive Offers Empty State (#665)

**Files:**
- Modify: `app/static/crm.js:4922`

**Step 1: Rewrite message**

Change the empty state text to something clearer for new users:
```
No matches yet this week. This page shows you when a vendor offers parts that your past customers have requested \u2014 so you can reconnect and close a sale.
```

**Step 2: Commit**
```bash
git add app/static/crm.js
git commit -m "fix: clearer Proactive Offers empty state message. Fixes #665."
```

---

### Task 4.5: API Health Badge Tooltip (#641)

**Files:**
- Modify: `app/static/app.js:831-834`

**Step 1: Add tooltip with failing API names**

At line 832, after setting badge.textContent, add:
```javascript
badge.title = alerts.map(a => a.source_name || a.message || 'Unknown').join(', ');
```

**Step 2: Commit**
```bash
git add app/static/app.js
git commit -m "fix: API Health badge tooltip shows failing API names. Fixes #641."
```

---

## Workstream 5: Accessibility (1 ticket)

### Task 5.1: Add Focus Ring to Modal Inputs (#666)

**Files:**
- Modify: `app/static/styles.css`

**Step 1: Add modal-specific focus-visible styles**

After line 1767, add:
```css
.modal input:focus-visible,.modal select:focus-visible,.modal textarea:focus-visible{outline:2px solid var(--blue);outline-offset:1px}
```

This restores focus rings inside modals, overriding the blanket removal at line 1767.

**Step 2: Commit**
```bash
git add app/static/styles.css
git commit -m "fix: add visible focus ring to modal form inputs. Fixes #666."
```

---

## Post-Implementation

### Run Full Test Suite
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=short -q
```

### Close All Tickets
After each workstream, update resolved tickets in the database.

### Deploy
```bash
git push && docker compose up -d --build && docker compose logs -f app
```
