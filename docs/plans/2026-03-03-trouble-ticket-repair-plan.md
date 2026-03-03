# Trouble Ticket Repair Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix 22 unique UI issues across 6 page areas, plus 1 production database bug.

**Architecture:** All fixes are frontend (app.js, crm.js, tickets.js, index.html) except the notification VARCHAR migration (Alembic) and one backend field inclusion fix. Each phase targets one page area and can be parallelized.

**Tech Stack:** Vanilla JS, Jinja2 templates, SQLAlchemy/Alembic, FastAPI

**Note:** All innerHTML usage in this plan follows existing codebase patterns with the project's `esc()` helper for XSS-safe escaping.

---

## Phase 1: Critical Security + Production Error

### Task 1.1: Fix Notification Title VARCHAR Overflow (PROD BUG)

**Files:**
- Modify: `app/models/notification.py:25`
- Modify: `app/services/notification_service.py:27-31`
- Create: `alembic/versions/049_*.py` (via autogenerate inside Docker)

**Step 1: Update model**

In `app/models/notification.py:25`, change:
```python
title = Column(String(500), nullable=False)
```

**Step 2: Create Alembic migration**

Run inside Docker:
```bash
docker compose exec -T app alembic revision --autogenerate -m "increase notification title to 500 chars"
```

Review the generated migration — should contain `op.alter_column` for title String(200) to String(500).

**Step 3: Add truncation safety in notification_service.py**

In `create_notification()`, before creating the Notification object:
```python
if title and len(title) > 500:
    title = title[:497] + "..."
```

**Step 4: Test migration round-trip**
```bash
docker compose exec -T app alembic upgrade head
docker compose exec -T app alembic downgrade -1
docker compose exec -T app alembic upgrade head
```

**Step 5: Run test suite**
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -x -q
```

**Step 6: Commit**
```bash
git add app/models/notification.py app/services/notification_service.py alembic/versions/049_*
git commit -m "fix: increase notification title to VARCHAR(500) with truncation safety

Fixes production StringDataRightTruncation error when self-heal ticket
diagnoses generate titles longer than 200 characters."
```

---

### Task 1.2: Hide Developer-Only Fields from Non-Admin Users (#652)

**Files:**
- Modify: `app/static/tickets.js:396-484`

**Step 1: Wrap sensitive sections in admin check**

In `showTicketDetail()`, add `&& window.__isAdmin` guard to these sections:

- ~line 396: `if (t.generated_prompt)` → `if (t.generated_prompt && window.__isAdmin)`
- ~line 402: `if (t.fix_branch || t.fix_pr_url)` → `if ((t.fix_branch || t.fix_pr_url) && window.__isAdmin)`
- ~line 424: `if (t.browser_info || t.screen_size || ...)` → add `&& window.__isAdmin`
- ~line 435: `if (t.console_errors)` → `if (t.console_errors && window.__isAdmin)`
- ~line 447: `if (t.ai_prompt)` → `if (t.ai_prompt && window.__isAdmin)`

**Step 2: Commit**
```bash
git add app/static/tickets.js
git commit -m "security: hide AI prompts, file paths, console errors from non-admin ticket views

Wraps 5 diagnostic/developer sections in window.__isAdmin check. Fixes #652."
```

---

### Task 1.3: Add User Profile Settings Tab (#651)

**Files:**
- Modify: `app/templates/index.html:1168-1176`
- Modify: `app/static/crm.js:5793-5814`

**Step 1: Add Profile tab button as first tab in index.html**

At ~line 1168, add before the Users tab:
```html
<button type="button" class="tab on" onclick="switchSettingsTab('profile',this)">My Profile</button>
```

Move the `on` class from Users tab to this new Profile tab.

**Step 2: Add Profile panel container in index.html**

After the settingsTabs div, add:
```html
<div class="settings-panel" id="settings-profile">
    <div style="max-width:600px;padding:16px">
        <h3>My Profile</h3>
        <div id="profileContent"><p class="empty">Loading...</p></div>
    </div>
</div>
```

**Step 3: Add handler in crm.js switchSettingsTab**

At ~line 5813, add:
```javascript
else if (name === 'profile') loadSettingsProfile();
```

Add `loadSettingsProfile()` function using `fetch('/api/users/me')` to render user's name, email, and role as read-only fields. Use safe DOM methods (`el()` helper or `textContent`).

**Step 4: Default to profile tab on settings open**

**Step 5: Commit**
```bash
git add app/templates/index.html app/static/crm.js
git commit -m "feat: add My Profile tab to Settings page

Adds read-only profile view visible to all users. Fixes #651."
```

---

## Phase 2: RFQ/Sourcing Page (8 tickets)

### Task 2.1: Vendor Autocomplete Dropdown (#646)

**Files:**
- Modify: `app/static/app.js:5737` (input element)
- Add functions near: `app/static/app.js:5540-5549`

**Step 1: Add suggestions container after vendor filter input**

At ~line 5737, wrap the vendor input in a position:relative container and add an `.ac-dropdown` div for suggestions.

**Step 2: Add _ddShowVendorSuggestions() and _ddSelectVendor() functions**

- Collect unique vendor names from `_ddDrillData[reqId].sightings`
- Filter by typed value (case-insensitive includes)
- Show top 15 matches in dropdown with safe text rendering via `esc()`
- On click/mousedown, set input value and trigger filter
- Close on blur with 150ms delay

Pattern to follow: existing Log Offer autocomplete at lines 6084-6143.

**Step 3: Commit**
```bash
git add app/static/app.js
git commit -m "feat: add vendor autocomplete dropdown in sourcing drill-down

Collects unique vendor names from sightings and shows filterable dropdown. Fixes #646."
```

---

### Task 2.2: Archive Sub-Tabs + Offers Badge Click (#656, #658)

**Files:**
- Modify: `app/static/app.js:6626-6650`

**Step 1: Verify tab pills render for archive**

Tab pills ARE rendered at line 6649 for all views. Archive tabs ARE configured at line 2684. The issue may be that the archive drill-down header content displaces the tabs, or CSS hides them.

Check: expand an archive row in browser and inspect whether `.dd-tabs` div has content. If tabs render but are hidden, fix the CSS. If they don't render, trace the code path.

**Step 2: Wire offers badge to expand to offers tab**

Add onclick to the archive offers badge:
```javascript
onclick="event.stopPropagation();toggleDrillDown(${r.id});setTimeout(()=>_switchDdTab(${r.id},'offers'),100)"
```

**Step 3: Commit**
```bash
git add app/static/app.js
git commit -m "fix: enable archive sub-tabs and clickable offers badge. Fixes #656, #658."
```

---

### Task 2.3: Archive MATCHES Column (#661)

**Files:**
- Modify: `app/static/app.js:6545`

**Step 1: Show offer_count for archive instead of proactive_match_count**

```javascript
const matchVal = v === 'archive' ? (r.offer_count || 0) : (r.proactive_match_count || 0);
```

**Step 2: Commit**
```bash
git add app/static/app.js
git commit -m "fix: archive MATCHES column shows offer count. Fixes #661."
```

---

### Task 2.4: RFQ Filter State Reset (#660)

**Files:**
- Modify: `app/static/app.js:7010-7057`

**Step 1: Remove filter reset from setMainView**

Remove `_activeFilters = {};` and `_myReqsOnly = false;` from `setMainView()` (~lines 7035-7036). Keep `_toolbarQuickFilter = '';` reset since it's tab-specific.

**Step 2: Commit**
```bash
git add app/static/app.js
git commit -m "fix: preserve RFQ filters when switching tabs. Fixes #660."
```

---

### Task 2.5: Pipeline Chart Clickable (#647)

**Files:**
- Modify: `app/static/app.js:2311-2321`

**Step 1: Add onclick and cursor:pointer to pipeline spans**

- Active → `setMainView('rfq',null)`
- Quoted → `setMainView('rfq',null)`
- Won/Lost → `setMainView('archive',null)`
- Buy Plans → `sidebarNav('buyplans',...)`

Add `title` attributes for hover hints.

**Step 2: Commit**
```bash
git add app/static/app.js
git commit -m "feat: make pipeline chart items clickable. Fixes #647."
```

---

### Task 2.6: Verify Offers Counter (#664), Close #645

Manually verify `setToolbarQuickFilter('green')` works. Agent confirmed code is correct (lines 6391-6394). If working, resolve #664. Review #645 meta-ticket sub-issues, close if covered.

---

## Phase 3: Customers/Accounts Page (3 tickets)

### Task 3.1: Fix Strategic Filter Toggle (#659)

**Files:**
- Modify: `app/static/crm.js:125-128`

**Step 1: Add toggle-off logic**
```javascript
function setCustFilter(mode, btn) {
    _custFilterMode = (_custFilterMode === mode) ? 'all' : mode;
    document.querySelectorAll('#view-customers .chip-row .chip').forEach(c =>
        c.classList.toggle('on', c.dataset.value === _custFilterMode));
    renderCustomers();
}
```

**Step 2: Commit**
```bash
git add app/static/crm.js
git commit -m "fix: allow deactivating customer filter chips by re-clicking. Fixes #659."
```

---

### Task 3.2: Fix Duplicate Company Label (#662)

**Files:**
- Modify: `app/static/crm.js:3873-3876`

**Step 1: Deduplicate label when site name matches company name**
```javascript
label: sites.length > 1
    ? c.name + ' \u2014 ' + s.site_name
    : (s.site_name && s.site_name !== c.name ? c.name + ' \u2014 ' + s.site_name : c.name),
```

**Step 2: Commit**
```bash
git add app/static/crm.js
git commit -m "fix: deduplicate company label in dropdown. Fixes #662."
```

---

### Task 3.3: Investigate (PASS) Suffix (#639)

Agent found NO "(PASS)" text in code. Check if it's in the data itself. If not reproducible, resolve as already-fixed or data issue.

---

## Phase 4: Materials + Scorecard (4 unique tickets)

### Task 4.1: Fix Materials Nav Highlight (#642)

**Files:**
- Modify: `app/static/app.js:1107-1113`

**Step 1: Add explicit navHighlight call**
```javascript
function showMaterials() {
    showView('view-materials');
    navHighlight('navMaterials');
    // ... rest unchanged
}
```

**Step 2: Commit**
```bash
git add app/static/app.js
git commit -m "fix: preserve Materials nav highlight. Fixes #642."
```

---

### Task 4.2: Collapse Import Stock Form (#648, #649, #655)

**Files:**
- Modify: `app/static/app.js`

**Step 1: Ensure form hidden in showScorecard() too**

`showMaterials()` already hides it. Add same to `showScorecard()` if the form appears there.

**Step 2: Commit**
```bash
git add app/static/app.js
git commit -m "fix: collapse Import Stock form by default. Fixes #648, #649, #655."
```

---

### Task 4.3: Scorecard Improvements (#653, #654, #657)

**Files:**
- Modify: `app/static/crm.js:5271-5344`

**Step 1: Add tooltip to Score column header explaining the composite calculation**

**Step 2: Improve qualification display — add guidance text for "Not Qualified" users**

**Note: Confirm prize thresholds with Vinod before implementing specific values.**

**Step 3: Commit**
```bash
git add app/static/crm.js
git commit -m "fix: scorecard tooltips and qualification guidance. Fixes #653, #654, #657."
```

---

## Phase 5: Contacts + Vendors (2 unique tickets)

### Task 5.1: Add Vendor Button (#637, #638)

**Files:**
- Modify: `app/templates/index.html:452-454`

**Step 1: Add button to crm-header in vendor list**

Add `crm-header-actions` div with "+ Add Vendor" button matching the Accounts page pattern.

**Step 2: Wire to openNewVendorModal() or create if missing**

**Step 3: Commit**
```bash
git add app/templates/index.html app/static/crm.js
git commit -m "feat: add '+ Add Vendor' button. Fixes #637, #638."
```

---

### Task 5.2: Contact Quick-Action Buttons (#640)

**Files:**
- Modify: `app/static/app.js:1362-1412` or `app/static/crm.js` contact drawer

**Step 1: Add Email and Call buttons using mailto/tel links**

Only show when contact has email/phone. Use safe `esc()` escaping for values.

**Step 2: Commit**
```bash
git add app/static/app.js
git commit -m "feat: add Email/Call quick-action buttons to contact panel. Fixes #640."
```

---

## Phase 6: Tickets/Self-Heal + System (3 unique tickets)

### Task 6.1: Default Tickets Filter to Submitted (#650)

**Files:**
- Modify: `app/static/tickets.js:210`

**Step 1: Change default**
```javascript
var _adminFilter = 'submitted';
```

Update the filter pill rendering to mark 'submitted' as default `on`.

**Step 2: Commit**
```bash
git add app/static/tickets.js
git commit -m "fix: default tickets to 'Submitted' filter. Fixes #650."
```

---

### Task 6.2: Add Back Button to New Ticket Form (#644)

**Files:**
- Modify: `app/static/tickets.js:97-100`

**Step 1: Rename button**
```javascript
textContent: '\u2190 Back to Tickets',
```

**Step 2: Commit**
```bash
git add app/static/tickets.js
git commit -m "fix: rename 'My Tickets' to 'Back to Tickets' on submit form. Fixes #644."
```

---

### Task 6.3: API Health Badge Tooltip (#641, #663)

**Files:**
- Modify: `app/static/app.js:813-824`

**Step 1: Add tooltip with failing API names**

When `alerts.length > 0`, set `badge.title` to list the failing API source names joined by commas.

**Step 2: Commit**
```bash
git add app/static/app.js
git commit -m "fix: API Health badge tooltip shows failing API names. Fixes #641, #663."
```

---

## Post-Implementation

### Close Duplicate Tickets
Resolve tickets #638, #649, #655, #663 as duplicates.

### Run Full Test Suite
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=short -q
```

### Deploy
```bash
git push && docker compose up -d --build && docker compose logs -f app
```
