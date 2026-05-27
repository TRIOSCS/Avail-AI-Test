# Activity Timeline — Plan 6: Unified Frontend Timeline

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan. The template task is UI work — the implementer should also use the `frontend-design` skill. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Turn the requisition Activity tab's flat two-section list ("RFQ History" + "Activity Log") into a single chronological timeline: events grouped by date, each row with a canonical type icon and a vendor/contact label, the Plan-4 meaningful/show-all toggle retained. This is the final build step.

**Background (verified):** Plans 1–5 are all present on this branch. RFQ sends, vendor email replies, offers, status changes, calls, etc. are all `ActivityLog` rows now (`rfq_sent`, `email_received`, `offer_created`, …). The separate `Contact`-based "RFQ History" section is therefore redundant — its events are represented in the activity stream. The spec's build step 6 explicitly says "RFQ history merged into the same timeline." So Plan 6 removes the `Contact` section and renders one timeline from `activities`. The template's current per-type icon dicts key on 3 legacy strings (`note`/`phone_call`/`email_sent`) — they must be rebuilt around the 12 canonical `ActivityType` values.

**Tech Stack:** HTMX 2.x, Alpine.js 3.x, Jinja2, Tailwind CSS. No backend logic change beyond a route cleanup. No schema migration.

**Spec:** `docs/superpowers/specs/2026-05-20-activity-timeline-design.md` (build step 6).

**Branch:** Create `feat/activity-timeline-6` off `feat/activity-timeline-5` (or `main` if Plans 1–5 merged).

---

### Task 1: Route — drop the redundant `contacts` query

The activity-tab branch of `requisition_tab` (`app/routers/htmx_views.py`, `else: # activity`, ~lines 1261-1276) queries `Contact` rows for the old "RFQ History" section. The unified timeline renders only `activities`. Remove the now-dead `contacts` query and `ctx["contacts"]`.

**Files:**
- Modify: `app/routers/htmx_views.py`
- Test: `tests/test_activity_write_path.py`

- [ ] **Step 1: Write the failing/guard test**

Append to `tests/test_activity_write_path.py` a test that logs an event via `log_activity(... rfq_sent ...)` then `client.get`s `/v2/partials/requisitions/{id}/tab/activity`, asserts 200 and that the event's note text appears. (This guards the render after the route change; if a similar test already exists, extend rather than duplicate.) Run it — it should PASS now (the route already works); it is a regression guard for Task 2.

- [ ] **Step 2: Make the route change**

In the `else: # activity` branch: delete the `Contact`/`RfqContact` query and the `ctx["contacts"] = contacts` line and the now-unused `from ..models.offers import Contact as RfqContact` import (verify it is not used elsewhere in that function). Keep `ctx["activities"]`, `ctx["show_all"]`, `ctx["req"]`. The branch should end up: resolve `show_all`, set `ctx["activities"] = get_requisition_activities(req_id, db, meaningful_only=not show_all)`, `ctx["show_all"]`, `ctx["req"]`, return the template.

- [ ] **Step 3: Run tests**

`TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py -v --override-ini="addopts="`
Expected: PASS. (The template still references `contacts` at this point — that is fine; Jinja treats an undefined `contacts` in an `{% if contacts %}` as falsy. Task 2 removes the reference. If the template errors on undefined, do Task 2's template change in the same PR before relying on this — but the commit order here is route-first; if the test fails on an undefined-`contacts` StrictUndefined error, note it and proceed to Task 2, then re-run.)

- [ ] **Step 4: Lint and commit**

`ruff check app/routers/htmx_views.py` — fix issues. `git status --short` first; do NOT stage `Caddyfile`.
```bash
git add app/routers/htmx_views.py tests/test_activity_write_path.py
git commit -m "refactor: activity tab route serves a single activities list"
```

---

### Task 2: Template — unified chronological timeline

Rewrite the `#activity-timeline` region of `app/templates/htmx/partials/requisitions/tabs/activity.html` into one date-grouped chronological timeline. **Use the `frontend-design` skill** for this — the result must be elegant and match the app's existing visual identity (DM Sans, brand palette, the card/row styling used by `app/templates/htmx/partials/shared/activity_timeline.html` and `parts/tabs/activity.html`).

**Files:**
- Modify: `app/templates/htmx/partials/requisitions/tabs/activity.html`
- Modify: `app/templates/htmx/partials/shared/_macros.html` (add an `activity_icon` macro)
- Test: `tests/test_activity_timeline.py` (or wherever requisition-tab render tests fit)

- [ ] **Step 1: Write the failing test**

Add a render test: seed a requisition with two `ActivityLog` rows of different `activity_type` and different dates, `client.get` the activity tab partial, assert 200 and that (a) both events' text renders, (b) a date-group header is present (e.g. "Today" or a date string), (c) the old "RFQ History" heading is GONE. Run it — expect FAIL (no date-group header / "RFQ History" still present) before the rewrite.

- [ ] **Step 2: Confirm the failure reason.**

- [ ] **Step 3: Add the `activity_icon` macro**

In `app/templates/htmx/partials/shared/_macros.html`, add a macro `activity_icon(activity_type)` that returns an inline heroicons `<svg>` inside an `h-8 w-8 rounded-full` colored circle, mapping each of the 12 canonical `ActivityType` values to an icon + accent color. Mapping (icon = heroicon name; pick the matching heroicon outline path):

| activity_type | heroicon | accent |
|---|---|---|
| `rfq_sent` | paper-airplane | brand |
| `email_received` | envelope | blue |
| `call_logged` | phone | emerald |
| `status_changed` | arrow-path | gray |
| `offer_created` | document-plus | amber |
| `offer_status_changed` | document-check | amber |
| `sighting_added` | magnifying-glass | indigo |
| `sales_note` | pencil-square | gray |
| `task_completed` | check-circle | emerald |
| `assignment_changed` | user | gray |
| `req_archived` | archive-box | gray |
| `req_unarchived` | archive-box-arrow-down | gray |

Any unknown type → a neutral default (information-circle, gray). Use the inline-SVG style already used elsewhere in `_macros.html` / the timeline partials (no external icon dependency).

- [ ] **Step 4: Rewrite the timeline region of `activity.html`**

Keep unchanged: the header comment, the 3-card stat bar (but update its third card — `activity_count` stays; the first two cards reference `contacts` which is gone — replace the "RFQs Sent"/"Responses" cards with counts derived from `activities` by `activity_type`, e.g. "RFQ events" = activities where `activity_type == 'rfq_sent'`, "Offers" = `offer_created`; OR collapse to a single "Events" count — keep it simple and honest, no `contacts`), the "Log Activity" Alpine form, and the empty state (now keyed on `not activities` only).

Replace the two-section `#activity-timeline` block with ONE timeline:
- `{% from "htmx/partials/shared/_macros.html" import activity_icon %}` at the top.
- Iterate `activities` (already newest-first). Group by calendar date of `(a.occurred_at or a.created_at)`. Render a date-group header when the date changes — "Today", "Yesterday", else e.g. `%b %d, %Y` — using a running-date variable (`{% set ns = namespace(day=None) %}` and compare). Date-group headers are small uppercase gray labels.
- Each row: the `activity_icon(a.activity_type)` circle on the left; a vertical connector line between rows is optional (Tailwind only — no new CSS file). Body: a title = `a.activity_type|replace('_',' ')|title`; a vendor/contact label when available (`a.vendor_card.name` if `a.vendor_card_id` else `a.contact_name`); a small `channel` pill when `channel` is not `system`; the body text `a.summary or a.notes` (`line-clamp-2`); and a right-aligned timestamp using the registered `|timeago` filter on `(a.occurred_at or a.created_at)`.
- Move the Plan-4 show-all toggle into this single timeline's header (keep its exact `hx-get`/`hx-target="#tab-content"`/`hx-swap` attributes and the `show_all` conditional — do not change its behavior).
- Remove the "RFQ History" section, its `Contact`-based loop, the `status_icon`/`st_colors` inline dicts, and the dead `{% set all_items = [] %}`.

Follow the `frontend-design` skill: spacing, hierarchy, the brand palette, hover states consistent with the app. Do NOT add new CSS classes to `styles.css` unless genuinely needed — Tailwind utilities inline, matching the sibling timeline partials.

- [ ] **Step 5: Run tests + build check**

`TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_timeline.py tests/test_activity_write_path.py -v --override-ini="addopts="`
Expected: PASS — the render test passes; the activity tab still returns 200.
Confirm no Jinja2 `Undefined` errors for `contacts` (it must be fully removed from the template).

- [ ] **Step 6: Update the APP_MAP doc**

In `docs/APP_MAP_INTERACTIONS.md`, note that the requisition Activity tab renders a single date-grouped chronological timeline (RFQ history merged in as `rfq_sent` events; per-type icons via the `activity_icon` macro). Match the doc's style.

- [ ] **Step 7: Lint and commit**

`ruff check app/routers/htmx_views.py` (no Python changed here, but harmless). Verify the templates have no obvious Jinja errors by loading the tab in the test (Step 5). `git status --short` first; do NOT stage `Caddyfile`.
```bash
git add app/templates/htmx/partials/requisitions/tabs/activity.html app/templates/htmx/partials/shared/_macros.html docs/APP_MAP_INTERACTIONS.md tests/test_activity_timeline.py
git commit -m "feat: unified chronological activity timeline on the requisition tab"
```

---

## Self-Review

**Spec coverage (build step 6 — "Frontend timeline"):**
- Unified chronological render → Task 2 ✓
- Source/type icons (canonical 12-type `activity_icon` macro) → Task 2 ✓
- Vendor/contact labels → Task 2 ✓
- Date grouping → Task 2 ✓
- Meaningful-default + "Show all" toggle → retained from Plan 4 ✓
- RFQ history merged into the timeline → the `Contact` section is removed; RFQ sends render as `rfq_sent` events (Task 1 drops the query, Task 2 drops the section) ✓

**UI guardrail:** removing the "RFQ History" section and restructuring the Activity Log into a timeline is a UI change — but it is exactly what the approved spec's build step 6 specifies ("unified chronological render … RFQ history merged"), so it is pre-approved. The stat bar, Log Activity form, and empty state are preserved (the stat bar's `contacts`-derived cards are re-derived from `activities`).

**Placeholder scan:** Step 3's icon table gives a concrete type→heroicon→accent mapping; the implementer renders each as an inline heroicons SVG (the codebase's established icon style) — this is normal icon work, not a placeholder. Every other step is concrete.

**No migration / no backend logic change:** Task 1 only deletes a dead query; Task 2 is template + a macro. `get_requisition_activities()` (Plan 4) already returns the curated, ordered list.

**Scope:** Plan 6 restyles the requisition Activity tab only. Converging the other three timeline partials (`shared/activity_timeline.html`, `vendors/contact_timeline.html`, `parts/tabs/activity.html`) onto the new macro is a future cleanup, not this plan.
