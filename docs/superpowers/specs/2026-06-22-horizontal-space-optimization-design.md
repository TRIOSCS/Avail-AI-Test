# Horizontal-Space Optimization (all pages) — Design

**Date:** 2026-06-22
**Status:** Approved (design)
**Scope:** App-wide, class-level refinement. No page-structure changes (no UI
elements added, removed, or rearranged). HTMX + Alpine + Jinja2 + Tailwind.

## Problem

On wide monitors (1440px / 1920px / ultrawide) most pages show large empty side
gutters. Root cause: the shell `<main id="main-content" class="main-content p-4 …">`
is already full-width, but each page wraps its content in `max-w-7xl mx-auto`
(≈1280px, centered) — or smaller caps (`6xl/5xl/4xl/3xl/2xl`). Card grids also stop
at `lg:grid-cols-3` and add no columns on wider screens, and wide data tables
(`min-w-[1400px]`) are trapped inside the cap and forced to scroll even when the
viewport could show them fully.

There is no custom Tailwind `screens`/`container` config and no `.main-content`
max-width in CSS — the gutters come purely from per-page wrapper classes, so this
is fixable cleanly and consistently.

## Policy: per-page-type

- **Dense data pages** (tables, multi-column data grids, lists, dashboards, hubs):
  drop the centering cap → fill the viewport.
- **Reading / form pages** (forms, single-column text/reading): keep a comfortable
  centered measure (~1152px) for line-length readability.
- **Card grids on dense pages**: add `xl:`/`2xl:` column steps so cards fill wide
  screens instead of stopping at 3-across.
- **Intentionally narrow** content (short forms, document previews, split-panel
  panes) and **fragments** (tabs/modals/rows that render inside a parent shell):
  left untouched.

## Mechanism

### 1. Two semantic width classes (single knob each)

Added to the existing "canonical component layer" of `app/static/styles.css`
(`@layer components`, where `@apply` already works and the design tokens live):

```css
/* Page-width policy — one place to tune horizontal-space usage.
   Applied on a page-shell's outermost wrapper. */
.page-fluid    { @apply w-full; }                    /* dense pages: fill viewport */
.page-readable { @apply mx-auto w-full max-w-6xl; }  /* 72rem ≈ 1152px reading measure */
```

`max-w-6xl` = 72rem = 1152px (the approved readable cap).

### 2. Swap each page-shell's outer wrapper

Replace the outer `max-w-{7xl|6xl|5xl|4xl|3xl|2xl} mx-auto` with `.page-fluid` or
`.page-readable` per the treatment table. **Preserve any `x-data` / `x-init` / `id`
on that same div** — several wrappers are Alpine roots (flagged below).

### 3. Responsive shell padding

`.main-content` horizontal padding scales up so full-bleed content does not hug the
screen edge on large displays. In `app/templates/htmx/base.html`:

```
class="main-content p-4 pb-[52px] bg-white"
→ class="main-content p-4 lg:px-6 2xl:px-8 pb-[52px] bg-white"
```

Horizontal only; top padding and bottom-nav clearance (`pb-[52px]`) unchanged.

### 4. Card-grid column steps (verified per grid)

Only genuine card/tile/stat grids — never layout-splits:

| File | Current | Target |
|---|---|---|
| `prospecting/list` | `grid-cols-1 md:grid-cols-2 lg:grid-cols-3` | `+ xl:grid-cols-4 2xl:grid-cols-5` |
| `vendors/list` | `grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4` | `+ 2xl:grid-cols-5` |
| `quotes/detail` | `grid-cols-1 lg:grid-cols-2` | `+ xl:grid-cols-3 2xl:grid-cols-4` **only if a card grid, not a 2-pane layout** |
| `sourcing/lead_detail` | `grid-cols-2 sm:grid-cols-4` | `+ xl:grid-cols-8` **only if a small KPI-tile row; else skip** |

## Treatment table

### → `.page-fluid` (21 dense page-shells)

`admin/spec_codes_pending`, `buy_plans/detail`*, `buy_plans/hub`*, `dashboard`,
`emails/intelligence_dashboard`, `excess/detail`, `excess/list`, `follow_ups/list`,
`materials/detail`*, `proactive/list`, `prospecting/list`, `quotes/detail`,
`requisitions/detail`, `requisitions/list`*, `search/full_results`, `settings/index`*,
`sourcing/lead_detail`, `tickets/workspace`, `vendors/detail`, `vendors/list` †,
`offers/review_queue` †

- `*` = wrapper is an Alpine `x-data` root — preserve the attribute.
- `†` = the audit first labeled these as fragments; confirmed full pages via
  `template_response(...)` in `app/routers/htmx_views.py`. `offers/review_queue`'s
  wrapper also carries `id="review-queue-content"` — preserve it.

### → `.page-readable` (6 reading page-shells, normalize to ~1152px)

`admin/data_ops` (3xl→readable), `knowledge/list` (3xl→readable),
`proactive/prepare` (4xl→readable), `prospecting/detail` (7xl→readable),
`search/dossier_shell` (5xl→readable), `tickets/detail` (4xl→readable)

### → Leave as-is (do NOT touch)

- **Intentionally narrow forms:** `customers/create_form` (2xl), `customers/edit_form`
  (2xl), `search/form` (2xl). Widening a short form hurts readability.
- **Document preview:** `quotes/preview` (3xl) — mimics a printable page.
- **Split-panel pane:** `customers/detail` (3xl) — explicit code comment:
  "must fit the CDM right panel".
- **Already full-bleed:** `sourcing/workspace`, `requisitions2/page.html`.
- **All fragments** (32): tabs, modals, row partials, results fragments, macros —
  width is owned by the parent page-shell. Their `max-w-[...]` values are inner
  cell-truncation widths (e.g. `max-w-[200px]`), not page caps.

## Verification & safety

- **Static-analysis guard:** extend `tests/test_static_analysis.py` (or add a focused
  test) asserting that page-shell partials in the `.page-fluid` / `.page-readable`
  sets do not contain `max-w-{7xl|6xl|5xl|4xl|3xl|2xl} mx-auto` on their shell — the
  new classes must be used instead. Prevents the gutter pattern creeping back.
- **Full suite:** `TESTING=1 PYTHONPATH=<worktree> pytest tests/ -v` run **from inside
  the worktree** (templates/static-analysis resolve via cwd).
- **Frontend build:** `npm run build` — confirm `.page-fluid` / `.page-readable` and
  the new responsive padding land in the built CSS bundle (Tailwind purges unused
  classes; both are referenced in templates + styles.css so they survive).
- **Post-deploy:** `deploy.sh` verifies new Tailwind classes appear in built CSS.
- **No table edits needed:** wide tables (`min-w-[1400px]`) simply fill once their
  shell is fluid.

## Out of scope

- No Tailwind `screens`/`container` config changes.
- No new/removed/moved UI elements.
- No changes to fragments, document templates, login, or the v2 requisitions page.

## Files touched

- `app/static/styles.css` — 2 new component classes.
- `app/templates/htmx/base.html` — `.main-content` horizontal padding.
- 27 page-shell partials — outer wrapper class swap.
- ~2–4 card grids — column steps.
- `tests/test_static_analysis.py` (or new test) — guard.
- `docs/APP_MAP_*` — note the page-width convention if relevant.
