# Horizontal-Space Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make all AvailAI pages use horizontal space responsively — dense pages fill wide monitors, reading/form pages keep a comfortable measure — with no page-structure changes.

**Architecture:** Two semantic width classes (`.page-fluid`, `.page-readable`) added to the Tailwind component layer; each page-shell's outer `max-w-*xl mx-auto` wrapper is swapped to one of them; the shell gets responsive side padding; card grids gain `xl:`/`2xl:` column steps. A static-analysis test locks the convention in.

**Tech Stack:** Tailwind CSS 3 (`@apply` in `app/static/styles.css`), Jinja2 templates, pytest.

## Global Constraints

- No UI elements added, removed, or rearranged — class-level changes only.
- Preserve `x-data` / `x-init` / `id` attributes on any wrapper being re-classed.
- `.page-readable` = `max-w-6xl` (72rem ≈ 1152px), centered.
- `.page-fluid` = full width (`w-full`).
- Do NOT touch: fragments (tabs/modals/row partials/results fragments/macros), `customers/detail`, `customers/create_form`, `customers/edit_form`, `search/form`, `quotes/preview`, `sourcing/workspace`, `requisitions2/page.html`, document templates, login.
- Run pytest **from inside the worktree** (`TESTING=1 PYTHONPATH=$(pwd) pytest …`).
- Tailwind purges unused classes — both new classes are referenced in templates + styles.css, so they survive `npm run build`.

---

### Task 1: Width classes + responsive shell padding

**Files:**
- Modify: `app/static/styles.css` (`@layer components`)
- Modify: `app/templates/htmx/base.html` (the `<main id="main-content">` class)

- [ ] **Step 1:** In `app/static/styles.css`, inside the existing `@layer components { … }`, add:

```css
  /* ── Page-width policy ───────────────────────────────────────────────
     Single knob for horizontal-space usage. Applied on a page-shell's
     outermost wrapper. Dense data pages fill the viewport; reading/form
     pages stay within a comfortable measure. */
  .page-fluid    { @apply w-full; }
  .page-readable { @apply mx-auto w-full max-w-6xl; }
```

- [ ] **Step 2:** In `app/templates/htmx/base.html`, change the main element class from `main-content p-4 pb-[52px] bg-white` to `main-content p-4 lg:px-6 2xl:px-8 pb-[52px] bg-white` (horizontal padding only; keep `hx-target`/`hx-swap`/`id`).

- [ ] **Step 3:** Commit: `git add app/static/styles.css app/templates/htmx/base.html && git commit -m "feat(ui): add page-width classes + responsive shell padding"`

---

### Task 2: Static-analysis guard test (TDD red)

**Files:**
- Modify/Create: `tests/test_static_analysis.py` (add `test_page_shells_use_width_classes`)

**Interfaces:**
- Produces: a test that scans the 27 page-shell partials and asserts none contain `max-w-{7xl|6xl|5xl|4xl|3xl|2xl}` combined with `mx-auto` on the shell, and that `app/static/styles.css` defines `.page-fluid` and `.page-readable`.

- [ ] **Step 1:** Add a test enumerating the page-shell file list (the 21 fluid + 6 readable files) and asserting, for each, that the file does not match the regex `max-w-(7xl|6xl|5xl|4xl|3xl|2xl)\b[^"]*mx-auto|mx-auto[^"]*max-w-(7xl|6xl|5xl|4xl|3xl|2xl)\b`. Also assert `.page-fluid` and `.page-readable` appear in `styles.css`.
- [ ] **Step 2:** Run it — expect FAIL (shells still carry the old caps): `TESTING=1 PYTHONPATH=$(pwd) pytest tests/test_static_analysis.py::test_page_shells_use_width_classes -v --override-ini="addopts="`
- [ ] **Step 3:** Commit the test: `git commit -am "test(ui): guard page-shell width classes"`

---

### Task 3: Swap 21 dense page-shells → `.page-fluid`

**Files (Modify — outer wrapper only):**

| File | Current cap | Preserve |
|---|---|---|
| `app/templates/htmx/partials/admin/spec_codes_pending.html` | `max-w-7xl mx-auto` | — |
| `app/templates/htmx/partials/buy_plans/detail.html` | `max-w-7xl mx-auto` | `x-data` |
| `app/templates/htmx/partials/buy_plans/hub.html` | `max-w-7xl mx-auto` | `x-data` |
| `app/templates/htmx/partials/dashboard.html` | `max-w-4xl mx-auto` | — |
| `app/templates/htmx/partials/emails/intelligence_dashboard.html` | `max-w-7xl mx-auto` | — |
| `app/templates/htmx/partials/excess/detail.html` | `max-w-5xl mx-auto` | — |
| `app/templates/htmx/partials/excess/list.html` | `max-w-7xl mx-auto` | — |
| `app/templates/htmx/partials/follow_ups/list.html` | `max-w-7xl mx-auto` | — |
| `app/templates/htmx/partials/materials/detail.html` | `max-w-7xl mx-auto` | `x-data` |
| `app/templates/htmx/partials/proactive/list.html` | `max-w-7xl mx-auto` | — |
| `app/templates/htmx/partials/prospecting/list.html` | `max-w-7xl mx-auto` | — |
| `app/templates/htmx/partials/quotes/detail.html` | `max-w-7xl mx-auto` | — |
| `app/templates/htmx/partials/requisitions/detail.html` | `max-w-7xl mx-auto` | — |
| `app/templates/htmx/partials/requisitions/list.html` | `max-w-7xl mx-auto` | `x-data` |
| `app/templates/htmx/partials/search/full_results.html` | `max-w-6xl mx-auto` (+`px-4 py-6`, `x-data`) | `x-data`, keep `px-4 py-6` |
| `app/templates/htmx/partials/settings/index.html` | `max-w-7xl mx-auto` | `x-data` |
| `app/templates/htmx/partials/sourcing/lead_detail.html` | `max-w-4xl` | — |
| `app/templates/htmx/partials/tickets/workspace.html` | `max-w-6xl mx-auto` | — |
| `app/templates/htmx/partials/vendors/detail.html` | `max-w-5xl mx-auto` | — |
| `app/templates/htmx/partials/vendors/list.html` | `max-w-7xl mx-auto` | — |
| `app/templates/htmx/partials/offers/review_queue.html` | `max-w-7xl mx-auto` | `id="review-queue-content"` |

- [ ] **Step 1:** For each file, read the outer wrapper line and replace the `max-w-*xl mx-auto` tokens with `page-fluid`, leaving every other class and attribute intact (Alpine roots keep `x-data`; `review_queue` keeps its `id`; `full_results`/`lead_detail` keep their extra spacing/`px`/`py` classes; `lead_detail` has no `mx-auto` to remove — just `max-w-4xl` → `page-fluid`).
- [ ] **Step 2:** Commit: `git commit -am "feat(ui): dense page-shells fill viewport (.page-fluid)"`

---

### Task 4: Swap 6 reading page-shells → `.page-readable`

**Files (Modify — outer wrapper only):**

| File | Current cap |
|---|---|
| `app/templates/htmx/partials/admin/data_ops.html` | `max-w-3xl mx-auto` |
| `app/templates/htmx/partials/knowledge/list.html` | `max-w-3xl mx-auto` |
| `app/templates/htmx/partials/proactive/prepare.html` | `max-w-4xl mx-auto` |
| `app/templates/htmx/partials/prospecting/detail.html` | `max-w-7xl mx-auto` |
| `app/templates/htmx/partials/search/dossier_shell.html` | `max-w-5xl mx-auto` |
| `app/templates/htmx/partials/tickets/detail.html` | `max-w-4xl mx-auto` |

- [ ] **Step 1:** For each, replace the `max-w-*xl mx-auto` tokens with `page-readable` (it bundles `mx-auto`), preserving other classes/attrs.
- [ ] **Step 2:** Commit: `git commit -am "feat(ui): reading page-shells use comfortable measure (.page-readable)"`

---

### Task 5: Card-grid column steps (verified per grid)

**Files (Modify):**
- `app/templates/htmx/partials/prospecting/list.html`: card grid `grid-cols-1 md:grid-cols-2 lg:grid-cols-3` → append `xl:grid-cols-4 2xl:grid-cols-5`.
- `app/templates/htmx/partials/vendors/list.html`: stat-card grid `grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4` → append `2xl:grid-cols-5`.
- `app/templates/htmx/partials/quotes/detail.html`: **read first.** If `grid-cols-1 lg:grid-cols-2` is a card/tile grid, append `xl:grid-cols-3 2xl:grid-cols-4`. If it is a 2-pane section layout, SKIP and note it.
- `app/templates/htmx/partials/sourcing/lead_detail.html`: **read first.** If `grid-cols-2 sm:grid-cols-4` is a small KPI-tile row, append `xl:grid-cols-8` (or `xl:grid-cols-6` if 8 is too dense). If not a tile row, SKIP.

- [ ] **Step 1:** Apply the verified grid edits.
- [ ] **Step 2:** Commit: `git commit -am "feat(ui): card grids add columns on wide screens"`

---

### Task 6: Make the guard green + full verification

- [ ] **Step 1:** Run the guard test — expect PASS: `TESTING=1 PYTHONPATH=$(pwd) pytest tests/test_static_analysis.py -v --override-ini="addopts="`
- [ ] **Step 2:** Run the full suite from inside the worktree: `TESTING=1 PYTHONPATH=$(pwd) pytest tests/ -q`
- [ ] **Step 3:** `npm run build` — confirm build succeeds and `.page-fluid` / `.page-readable` appear in the built CSS: `grep -o 'page-fluid\|page-readable' app/static/dist/assets/*.css | sort -u`
- [ ] **Step 4:** `pre-commit run --all-files` (twice if docformatter rewraps).
- [ ] **Step 5:** Commit any formatting fixes.

---

## Self-Review

- **Spec coverage:** classes (T1), shell padding (T1), 21 fluid (T3), 6 readable (T4), grids (T5), guard test (T2/T6), build + suite verification (T6). Leave-as-is set is excluded by omission and protected by the guard's file list. ✓
- **Placeholder scan:** grid tasks have explicit "read first / skip if layout" decision rules, not vague TODOs. ✓
- **Type consistency:** class names `.page-fluid` / `.page-readable` and `max-w-6xl` used consistently across T1, T3, T4, T2 regex. ✓
