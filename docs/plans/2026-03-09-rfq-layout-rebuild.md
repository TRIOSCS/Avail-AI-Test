# RFQ Layout Rebuild + Task Sidebar Restyle — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Roll back the broken RFQ layout redesign and rebuild with clear Sales/Purchasing views, white cards, no priority lanes, no hidden sub-menus, and a cleaner task sidebar.

**Architecture:** Pure frontend changes across 3 files: `app/static/app.js` (view modes, sub-tabs, priority lanes, notification bar, task sidebar), `app/static/styles.css` (card colors, task sidebar styles), `app/templates/index.html` (view toggle buttons, notification bar HTML, sidebar HTML). No backend changes needed.

**Tech Stack:** Vanilla JS, CSS, Jinja2 templates

---

### Task 1: Rename view modes — "Sales" stays, "Sourcing" becomes "Purchasing"

**Files:**
- Modify: `app/templates/index.html:101-103` (desktop pills), `162-164` (mobile pills)
- Modify: `app/static/app.js:2992` (default view), `3108-3124` (column config), `3156-3166` (sub-tabs)

**Step 1: Update index.html desktop pills**

Change line 102 from:
```html
<button type="button" class="fp" data-view="sourcing" onclick="setMainView('sourcing',this)" title="Sourcing view — part coverage, sightings, RFQs, vendor responses">Sourcing</button>
```
to:
```html
<button type="button" class="fp" data-view="purchasing" onclick="setMainView('purchasing',this)" title="Purchasing view — part coverage, sightings, RFQs, vendor responses">Purchasing</button>
```

**Step 2: Update index.html mobile pills**

Change line 163 similarly — `data-view="purchasing"`, `setMainView('purchasing',...)`, label "Purchasing".

**Step 3: Update app.js — replace all `'sourcing'` view references with `'purchasing'`**

Key locations:
- Line 2992: default view — `'sales'` stays, but any stored `'sourcing'` should map to `'purchasing'`
- Line 2993: delete `_laneCollapseState` entirely
- Lines 3108-3124: `_applyColVisCSS` and `_colGearDropdown` — change `v === 'sourcing'` checks
- Lines 3156-3166: `_ddSubTabs` and `_ddDefaultTab` — update view name references
- All other `=== 'sourcing'` or `=== 'sales'` in the requisition list flow

Use find-and-replace carefully: only replace `'sourcing'` when it refers to the main view mode, NOT when it refers to the sub-tab name "sourcing" (which is the consolidated parts+sightings tab that also needs changing — see Task 2).

**Step 4: Verify** — search for remaining `'sourcing'` references in app.js to ensure none are stale view-mode references.

**Step 5: Commit**
```bash
git add app/templates/index.html app/static/app.js
git commit -m "refactor: rename Sourcing view to Purchasing"
```

---

### Task 2: Restore original separate sub-tabs (un-consolidate)

**Files:**
- Modify: `app/static/app.js:3156-3180` (sub-tab definitions)
- Modify: `app/static/app.js:3377-3460` (`_renderDdTab` switch)

**Step 1: Replace `_ddSubTabs`**

```javascript
function _ddSubTabs(mainView) {
    if (mainView === 'archive' || _reqStatusFilter === 'archive') return ['parts', 'offers', 'quotes', 'activity', 'tasks', 'files'];
    if (mainView === 'purchasing') return ['details', 'sightings', 'activity', 'offers', 'tasks', 'files'];
    // Sales view
    return ['parts', 'offers', 'quotes', 'tasks', 'files'];
}
```

**Step 2: Replace `_ddDefaultTab`**

```javascript
function _ddDefaultTab(mainView) {
    return mainView === 'purchasing' ? 'sightings' : 'parts';
}
```

**Step 3: Replace `_ddTabLabel`**

```javascript
function _ddTabLabel(tab) {
    const map = {details:'Details', sightings:'Sightings', activity:'Activity', offers:'Offers', parts:'Parts', quotes:'Quotes', buyplans:'Buy Plans', files:'Files', tasks:'Tasks'};
    return map[tab] || tab;
}
```

**Step 4: Simplify `_renderDdTab` switch**

Remove the consolidated `'sourcing'`, `'quote'`, and merged `'activity'` cases. Restore individual cases:
```javascript
case 'parts':
    _renderDrillDownTable(reqId, panel);  // or split-pane
    break;
case 'sightings':
    if (data && !_ddSightingsCache[reqId]) _ddSightingsCache[reqId] = data;
    _renderSourcingDrillDown(reqId, panel);
    break;
case 'activity':
    _renderDdActivity(reqId, data, panel);
    _autoPollReplies(reqId, data, panel);
    break;
case 'offers':
    _renderDdOffers(reqId, data, panel);
    break;
case 'quotes':
    _renderDdQuotes(reqId, data, panel);
    break;
case 'tasks':
    _renderDdTasks(reqId, data, panel);
    break;
case 'files':
    _renderDdFiles(reqId, data, panel);
    break;
```

**Step 5: Update the `_loadDdSubTab` fetch URLs** — the consolidated tabs used special combined endpoints. Restore individual fetch paths:
- `sourcing` tab was fetching `/api/requisitions/${reqId}/parts` + sightings combined. Split back to `parts` → `/api/requisitions/${reqId}/parts` and `sightings` → `/api/requisitions/${reqId}/sightings`.
- `quote` tab was fetching quotes+files combined. Split to `quotes` → `/api/requisitions/${reqId}/quotes` and `files` → `/api/requisitions/${reqId}/files`.
- `activity` tab was fetching activity+tasks combined. Split to `activity` → `/api/requisitions/${reqId}/activity` and `tasks` → `/api/requisitions/${reqId}/tasks`.

**Step 6: Commit**
```bash
git add app/static/app.js
git commit -m "refactor: restore separate sub-tabs — un-consolidate sourcing/quote/activity"
```

---

### Task 3: Remove priority lanes + onboarding banner + notification bar

**Files:**
- Modify: `app/static/app.js:8773-8776` (lane rendering in `renderReqList`)
- Delete: `app/static/app.js:8827-8938` (`_classifyIntoLanes`, `_renderPriorityLanes`, `togglePriorityLane`, `_isDeadlineUrgent` — but keep `_isDeadlineUrgent` for card coloring)
- Delete: `app/static/app.js:8940-8990` (`_renderNotifActionBar`)
- Modify: `app/static/app.js:929-941` (onboarding hint)
- Modify: `app/templates/index.html:269-272` (notification bar HTML)

**Step 1: In `renderReqList`**, replace the priority lane branch:

```javascript
// Was:
} else if ((v === 'sales' || v === 'purchasing') && !_reqSortCol && !_serverSearchActive) {
    const lanes = _classifyIntoLanes(data, v);
    rowsHtml = _renderPriorityLanes(lanes, v);
} else {
```
Replace with just:
```javascript
} else {
```
So it always falls through to `data.map(r => _renderReqRow(r)).join('')`.

**Step 2: Delete** `_classifyIntoLanes`, `_renderPriorityLanes`, `togglePriorityLane` functions entirely.

**Step 3: Keep `_isDeadlineUrgent`** — we need it for card soft-red coloring.

**Step 4: Delete `_renderNotifActionBar`** function and remove calls to it in `renderReqList` (line ~8793, ~8800).

**Step 5: Remove notification bar HTML** from index.html (line 270).

**Step 6: Remove onboarding hint code** (lines 929-941 in app.js).

**Step 7: Delete `_laneCollapseState`** variable (line 2993).

**Step 8: Remove from exports** — clean up any `togglePriorityLane`, `_renderNotifActionBar`, `_classifyIntoLanes`, `_renderPriorityLanes` from the Object.assign exports at the bottom of app.js.

**Step 9: Commit**
```bash
git add app/static/app.js app/templates/index.html
git commit -m "fix: remove priority lanes, notification bar, onboarding banner"
```

---

### Task 4: White card styling with soft-red for nearly-late

**Files:**
- Modify: `app/static/app.js` — `_renderReqRow` function
- Modify: `app/static/styles.css` — req row styles

**Step 1: Find `_renderReqRow`** and add deadline-based background coloring.

In the `<tr>` tag of `_renderReqRow`, add a dynamic style:
```javascript
const urgency = _isDeadlineUrgent(r, new Date());
const rowStyle = (urgency === 'overdue' || urgency === 'today' || urgency === 'soon')
    ? 'background:#FEF2F2;border-left:3px solid #FECACA'
    : 'background:#fff';
```
Apply `style="${rowStyle}"` to the `<tr class="rrow">` element.

**Step 2: Update CSS** — ensure `.rrow` has white background by default:
```css
.rrow { background: #fff; }
.rrow:hover { background: #f8fafc; }
```
Remove any grey/beige backgrounds on `.rrow` or related classes.

**Step 3: Commit**
```bash
git add app/static/app.js app/static/styles.css
git commit -m "style: white cards by default, soft red for nearly-late requisitions"
```

---

### Task 5: Restyle task sidebar — smaller, white, structured

**Files:**
- Modify: `app/static/styles.css:1666-1692` (sidebar styles)
- Modify: `app/static/app.js:4476-4565` (task list rendering)
- Modify: `app/templates/index.html:251-263` (sidebar HTML)

**Step 1: Update sidebar CSS**

```css
.my-tasks-sidebar { position: fixed; top: 80px; right: 0; bottom: 0; z-index: 250; }
.my-tasks-toggle { position: fixed; right: 0; top: 50%; transform: translateY(-50%); background: #fff; border: 1px solid var(--border); border-right: none; border-radius: 6px 0 0 6px; padding: 10px 8px; cursor: pointer; z-index: 251; box-shadow: -2px 0 8px rgba(0,0,0,.05); transition: right .2s cubic-bezier(.4,0,.2,1), background .15s; }
.my-tasks-toggle:hover { background: #f8fafc; }
.my-tasks-panel { position: fixed; top: 80px; right: 0; bottom: 0; width: 220px; background: #fff; border-left: 1px solid var(--border); box-shadow: -2px 0 12px rgba(0,0,0,.06); transform: translateX(100%); transition: transform .2s cubic-bezier(.4,0,.2,1); display: flex; flex-direction: column; }
.my-tasks-sidebar.open .my-tasks-panel { transform: translateX(0); }
.my-tasks-sidebar.open .my-tasks-toggle { right: 220px; }
body.tasks-open .main { margin-right: 220px; transition: margin-right .2s cubic-bezier(.4,0,.2,1); }
body.tasks-open .toparea { margin-right: 220px; transition: margin-right .2s cubic-bezier(.4,0,.2,1); }
```

Key changes: width 240→220, background `#e8eaed`→`#fff`, border `var(--blue)`→`var(--border)`.

**Step 2: Add section headers with colored dots**

Update the group header styles:
```css
.my-task-group-header { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .5px; padding: 10px 4px 4px; color: var(--muted); display: flex; align-items: center; gap: 6px; }
.my-task-group-header::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--muted); flex-shrink: 0; }
.my-task-group-header.overdue::before { background: var(--red); }
.my-task-group-header.today::before { background: var(--amber, #f59e0b); }
```

**Step 3: Add requisition name to task items**

In `_renderMyTaskItem`, add the requisition name below the title:
```javascript
var reqName = document.createElement('div');
reqName.className = 'my-task-item-req';
reqName.textContent = task.requisition_name || 'Req #' + task.requisition_id;
item.appendChild(reqName);
```

Add CSS:
```css
.my-task-item-req { font-size: 10px; color: var(--blue); margin-top: 2px; }
```

**Step 4: Add type pill to task meta**

In `_renderMyTaskItem`, render the type as a small pill:
```javascript
if (task.task_type && task.task_type !== 'general') {
    var pill = document.createElement('span');
    pill.className = 'my-task-type-pill';
    pill.textContent = task.task_type;
    meta.appendChild(pill);
}
```

CSS:
```css
.my-task-type-pill { font-size: 9px; background: var(--bg2, #f1f5f9); border-radius: 3px; padding: 1px 5px; color: var(--muted); text-transform: capitalize; }
```

**Step 5: Filter auto-generated noise** — in `loadMyTasks`, filter tasks with source `'auto'` that have status `'done'`:
```javascript
tasks = tasks.filter(t => !(t.source === 'auto' && t.status === 'done'));
```

**Step 6: Commit**
```bash
git add app/static/app.js app/static/styles.css app/templates/index.html
git commit -m "style: restyle task sidebar — white, smaller, structured sections"
```

---

### Task 6: Column headers — differentiate Sales vs Purchasing

**Files:**
- Modify: `app/static/app.js:3105-3133` (`_applyColVisCSS`, `_colGearDropdown`)
- Modify: `app/static/app.js` — table header rendering in `renderReqList`

**Step 1: Define different column sets per view**

Sales columns: Customer, Parts, Quote, Offers, Bid Due, Sales, Age
Purchasing columns: Customer, Parts, Sourced, RFQs Sent, Response, Offers, Sales, Age

Update the `thead` construction in `renderReqList` to use view-specific columns.

**Step 2: Update `_applyColVisCSS` and `_colGearDropdown`** with the correct column indexes per view.

**Step 3: Commit**
```bash
git add app/static/app.js
git commit -m "feat: differentiated column headers for Sales vs Purchasing views"
```

---

### Task 7: Final cleanup + test

**Step 1: Search for stale references** — grep for `'sourcing'` (as view mode, not sub-tab), `priorityLane`, `laneCollapse`, `notifActionBar`, `v8Hint`, `v8LayoutSeen`, `onboardingDismissed` in app.js and remove any orphans.

**Step 2: Run the test suite**
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short 2>&1 | tail -20
```

**Step 3: Build and verify in browser**
```bash
cd /root/availai && docker compose up -d --build && docker compose logs -f app 2>&1 | head -30
```

**Step 4: Final commit**
```bash
git add -A
git commit -m "chore: cleanup stale references from RFQ layout rebuild"
```
