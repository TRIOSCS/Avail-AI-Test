# Concurrent Cleanup — Remaining Open Items

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Resolve all remaining unfinished work items across frontend, tests, and codebase cleanup.

**Architecture:** Phase 0 commits existing deep-cleaning work, then Phase 1 dispatches 4 parallel agents in isolated worktrees (CSS, HTML, JS, Tests). Phase 2 merges and verifies.

**Tech Stack:** FastAPI, Jinja2, vanilla JS, CSS, pytest

---

## Pre-Flight: Items Already Completed (No Action Needed)

- ~~Scheduler split~~ → `app/jobs/` exists (10 modules, 144-line coordinator)
- ~~Apollo Phase 2 merge~~ → already on main
- ~~CSP tightening~~ → moved to FastAPI middleware with nonce support
- ~~.btn-danger duplicate~~ → single definition at styles.css:104
- ~~.u-hidden consolidation~~ → only `.hidden` exists
- ~~buyplan_v3_notifications tests~~ → 36+ tests exist
- ~~z-index H7~~ → `.site-typeahead-list` at 501, toparea/sidebar at 201/200

---

## Phase 0: Commit Deep-Cleaning Branch (Sequential — Gate)

**27 modified + 9 untracked files on `deep-cleaning` branch.**

### Task 0.1: Stage and commit all deep-cleaning work

```bash
cd /root/availai
git add app/jobs/ app/routers/explorium.py app/routers/materials.py \
       app/routers/vendor_contacts.py app/routers/vendors_crud.py \
       app/schemas/explorium.py app/utils/vendor_helpers.py \
       tests/test_explorium.py
git add -u  # stage all modified tracked files
git commit -m "feat: deep-cleaning — scheduler split, new routers, test fixes"
```

### Task 0.2: Run test suite to verify clean state

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -x -q --tb=short
```

---

## Phase 1: Four Parallel Agents (Isolated Worktrees)

### Agent A: CSS Cleanup (styles.css + mobile.css)

**Files:** `app/static/styles.css`, `app/static/mobile.css`

**Work items:**
1. **!important reduction** — Remove unnecessary `!important` in styles.css (25 instances).
   Keep justified mobile overrides in mobile.css (safe-area-inset, visibility toggles).
   Target removals: button disabled states, split-panel adjustments, modal padding,
   row highlighting — restructure specificity instead.
2. **@media consolidation** — Merge 3 separate `@media(max-width:768px)` blocks in
   styles.css (lines ~443, ~976, ~2355) into 1 organized block at end of file.
3. **CSS variable extraction** — Extract 30+ hardcoded hex colors into CSS custom
   properties in the `:root` block. Audit existing `var(--*)` usage and extend.

### Agent B: HTML/Template Cleanup (index.html)

**Files:** `app/templates/index.html`

**Work items:**
1. **display:none → .hidden** — Migrate 20 inline `style="display:none"` to class="hidden".
   Update corresponding JS that sets `.style.display = ''` to use
   `classList.remove('hidden')` / `classList.add('hidden')` instead.
2. **Accessibility** — Add `role="button"` + `tabindex="0"` to clickable non-button
   elements. Add `for=` attributes to labels missing them.

### Agent C: JavaScript Fixes (app.js, crm.js, tickets.js)

**Files:** `app/static/app.js`, `app/static/crm.js`, `app/static/tickets.js`

**Work items:**
1. **Score cache race condition** — Use sentinel value pattern to prevent stale reads.
2. **Silent .catch(() => {})** — Replace with `console.error` logged handlers.
3. **Event listener cleanup** — Fix `toggleNotifications()` to remove listeners on close.
4. **Dual search sync** — Sync desktop/mobile search inputs on responsive resize.

### Agent D: Test Stability & Coverage

**Files:** `tests/test_routers_requisitions.py`, `tests/test_nc_worker_full.py`,
`tests/test_ics_worker_full.py`, plus coverage gap files

**Work items:**
1. **Fix `test_delete_requirement`** — Add fixture isolation, ensure DB state is clean.
2. **Fix `TestAiGate` (3 tests)** — Reset `_last_api_failure` and `_classification_cache`
   in autouse fixture for both NC and ICS worker test files.
3. **Coverage gaps** — After fixing flaky tests, run coverage report and write tests for
   any modules below 95%.

---

## Phase 2: Merge & Verify (Sequential)

### Task 2.1: Merge all worktree branches
### Task 2.2: Run full test suite + coverage check
### Task 2.3: Commit, push, deploy

---

## Blocked Items (Not Actionable)

- **TME connector** — API secret expired, needs new credentials
- **eBay connector** — Needs EBAY_CLIENT_ID + EBAY_CLIENT_SECRET
- **Sourcengine connector** — Needs SOURCENGINE_API_KEY
- **RFQ UX improvements** — Needs specific requirements from user
- **Vibe/Explorium Phase 3** — Deeper integration pending scope definition
