# Opportunity Table — Merged Design (Resizable + Aesthetic v2)

**Supersedes:**
- `docs/superpowers/specs/2026-04-21-rq2-resizable-columns-design.md`
- `specs/ui/opportunity-table-aesthetic-v2.md`

Both originals described the same page (`/requisitions2` list). Their combined scope was splitting the implementation mid-stream. This doc replaces them; those files remain for history but are no longer authoritative.

---

## Purpose

Make the requisitions list table legible at a glance and eliminate column cutoff, in one coherent change. "Legible at a glance" means typography, color, and spacing carry state pre-attentively. "No cutoff" means every piece of information is reachable — either by fitting in the cell, by user-resizable column widths, or by hover reveal.

## Why merge

Two streams on the same `<tr>` fragment — one adding user-draggable column widths, one reshaping the visuals — were fragmenting commits and tests. Each stream left half its work uncommitted. A single spec lets us ship one PR with complete green tests instead of two stalled branches.

## Governing principle

> Data is the darkest thing on the page; chrome is the lightest. Every pixel of non-data is noise the brain must filter.

Resizability is a user-controlled escape hatch, not a primary visual tool. The default widths are tuned for density; users adjust only when a specific requisition has unusually long content.

---

## Scope

**In scope**
- All 8 v2 visual elements: status dot + label, status cell time-text, row-edge urgency accent, deal-value typographic tiers, coverage meter, MPN chip aggregation in name cell, chrome strip, restyled column headers.
- Resizable columns (already partly shipped) and row-level split divider (shipped).
- `x-truncate-tip` hover tooltip for overflow — extended to also serve the "+N" chip overflow reveal.
- New backend row fields driving the above.
- Playwright coverage for every new visual variant and every interaction.

**Out of scope**
- Mobile-width behavior. Desktop only for this pass; narrow-width review is a separate spec.
- Parts tab inside the detail panel — v2 tokens may migrate there later, but this doc only covers the list.
- Column preferences per user (reset-to-default only; no per-user persistence server-side).
- Search / filter chrome redesign (v2 spec mentioned it; deferring).
- Dark-mode variants for the new tokens (CSS vars ready, but no dark palette defined here).
- Command palette.
- Hover action rail on rows.
- Density toggle (compact / comfortable).
- Row grouping by requisition or customer.

---

## Architecture

All changes are frontend + row-dict fields. No migrations, no new routes, no schema.

```
┌─ requisition_list_service.py ───────────────────────────────┐
│ list_requisitions() adds to each row dict:                  │
│   hours_until_bid_due,                                      │
│   deal_value_display, deal_value_source,                    │
│   deal_value_priced_count, deal_value_requirement_count,    │
│   coverage_filled, coverage_total,                          │
│   mpn_chip_items                                            │
└──────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─ _macros.html ───────────────────────────────────────────────┐
│ opp_name_cell(row)      ← chips + name + match badges        │
│ opp_status_cell(row)    ← dot + label + time text            │
│ deal_value(amt, src)                                         │
│ coverage_meter(f, t)                                         │
│ urgency_accent_class(hours, urgency)  → <tr> class           │
│ mpn_chips_aggregated(items)   ← in _mpn_chips.html           │
└──────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─ requisitions2/_table_rows.html ────────────────────────────┐
│ <tr class="{{ urgency_accent_class(...) }}">                 │
│   6-col compact set (no role variation in workspace)         │
│   hover action rail absolutely positioned over last cells    │
│ </tr>                                                         │
│ + requisitions2/_table.html (thead)                          │
│ + requisitions2/_single_row.html (HTMX swap target)          │
└──────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─ htmx_app.js ────────────────────────────────────────────────┐
│ x-truncate-tip (existing, relocated from requisitions2.js)   │
│   extended: reads el._tipNodes (DocumentFragment) for rich   │
│   body, falls back to textContent when not set               │
│ x-chip-overflow (new directive)                              │
│   measures chip widths vs container, hides overflow, sets    │
│   +N label + attaches cloned-DOM fragment to el._tipNodes    │
│   (no innerHTML, no HTML-string attributes — XSS-safe)       │
│ rowActionRail (new Alpine component)                         │
│   row-hover reveal + keyboard (focus+Enter) alternative path │
└──────────────────────────────────────────────────────────────┘
```

---

## File inventory

**Target surface:** `/requisitions2` split-screen workspace only. Left panel table is the compact list. The legacy `/v2/...` full list (`app/templates/htmx/partials/requisitions/req_row.html`) is **out of scope** for this PR; that's a separate future spec.

**Modify**
- `app/config.py` — add `avail_opp_table_v2: bool = True` setting with comment pointer to this spec.
- `app/services/requisition_list_service.py` — extend `_resolve_deal_value` signature (4 args, `partial` source); add `_build_row_mpn_chips`; add `coverage_filled`/`coverage_total`/`deal_value_priced_count`/`deal_value_requirement_count` row-dict fields.
- `app/templates/htmx/partials/shared/_macros.html` — finalize uncommitted v2 macros; add `opp_name_cell`, `opp_status_cell`, `opp_row_action_rail`; extend `deal_value` macro to take optional `priced_count`/`requirement_count` for `partial` source tooltip; update `urgency_accent` → `urgency_accent_class` signature (accepts both `hours` and `urgency`).
- `app/templates/htmx/partials/shared/_mpn_chips.html` — add `mpn_chips_aggregated(items)` variant.
- `app/templates/requisitions2/_table_rows.html` — gate new rendering on `avail_opp_table_v2_enabled`; preserve the existing 5-col rendering verbatim in `{% else %}` branch; add hover-action-rail markup to the `<tr>`.
- `app/templates/requisitions2/_single_row.html` — identical gating to `_table_rows.html` since this partial is swapped in after inline edits / create actions and must match the surrounding rows.
- `app/templates/requisitions2/_table.html` — update `<thead>` for 6-col v2 set + `opp-col-header` styling, gated by flag; legacy 5-col header preserved in `{% else %}`.
- `app/routers/requisitions2.py` — update `_table_context()` to inject `avail_opp_table_v2_enabled=app_settings.avail_opp_table_v2`.
- `app/static/styles.css` — add `.truncate-tip`, `.opp-chip-row`, `.opp-name-cell`, `.opp-deal--partial`, `.opp-action-rail` + hover-reveal rules; minor adjustments to existing tokens.
- `app/static/htmx_app.js` — move `x-truncate-tip` here (currently uncommitted in `requisitions2.js`); extend to read an optional `_tipNodes` DocumentFragment property on the element (cloned into the tooltip, never via `innerHTML`); add `x-chip-overflow` directive; add `rowActionRail` Alpine component.
- `app/static/js/requisitions2.js` — delete the duplicated `x-truncate-tip` block.
- `tests/test_requisition_list_service.py` — keep uncommitted helper tests; add tests for chip aggregation, coverage (offers semantic), partial deal value, priced counts.
- `tests/test_requisitions2_templates.py` — add assertions for v2 markup presence when flag on, legacy markup when flag off.
- `e2e/requisitions2-resize.spec.ts` — add v2 coverage (chip overflow, status cell, deal tiers, accent colors, action rail).
- `.env.example` — add `AVAIL_OPP_TABLE_V2=true` with one-line comment and spec link.
- `docs/APP_MAP_INTERACTIONS.md` — document `x-chip-overflow`, final `x-truncate-tip`, `resizableTable`, `rowActionRail`.
- `STABLE.md` — append v2 token table reference + feature-flag name/default/rollback procedure.

**Create**
- `e2e/requisitions2-visuals.spec.ts` — new Playwright file dedicated to v2 visual regressions (status, urgency, deal, coverage, chip overflow). Keeps resize-focused file lean.

**Not touched**
- Models, schemas, migrations. No new routes — only context-dict additions inside the existing `requisitions2.py` router.
- Detail panel (`_detail_panel.html`, parts tab, offers tab).
- Unrelated list views (parts, sightings, vendors, excess, follow-ups).
- `/v2/...` full requisitions list — separate future spec.

---

## Backend contract additions

`requisition_list_service.list_requisitions()` returns, per row, these additional keys:

| Key | Type | Source |
|---|---|---|
| `hours_until_bid_due` | `float \| None` | `_hours_until_bid_due(req.deadline)` — already uncommitted |
| `deal_value_display` | `float \| None` | `_resolve_deal_value(opportunity_value, priced_sum, priced_count, requirement_count)` — extended signature, see below |
| `deal_value_source` | `'entered' \| 'computed' \| 'partial' \| 'none'` | same helper — `partial` added for mixed-pricing case |
| `deal_value_priced_count` | `int` | number of requirements with non-null `target_price`. Drives the `partial` tooltip copy ("3 of 5 parts priced"). |
| `deal_value_requirement_count` | `int` | total requirement count for the requisition (same as `req_cnt`, duplicated under a clearer name for macro consumption). |
| `coverage_filled` | `int` | count of requirements with ≥1 **offer** — see "Coverage semantic" below for rationale. |
| `coverage_total` | `int` | total requirements (reuse existing `req_cnt`) |
| `mpn_chip_items` | `list[{"mpn": str, "role": "primary" \| "sub"}]` | `_build_row_mpn_chips(requirements)` — new |

`_resolve_deal_value(opportunity_value, priced_sum, priced_count, requirement_count)` rules:

1. `opportunity_value > 0` → return `(opportunity_value, 'entered')`. Broker-entered total wins unconditionally.
2. Else if `priced_sum > 0` **and** `priced_count == requirement_count` (every requirement has a target_price) → `(priced_sum, 'computed')`.
3. Else if `priced_sum > 0` **and** `priced_count < requirement_count` (some requirements priced, some not) → `(priced_sum, 'partial')`. Broker sees a **floor estimate** — the sum of what has prices. This matches the request for "data should be the darkest thing on the page" by refusing to hide a known signal behind `—`.
4. Else → `(None, 'none')`.

Zero-priced requirements (target_price explicitly set to 0) count as priced for `priced_count` — they contribute `0` to `priced_sum` but do not trigger `partial`. Brokers who enter `0` are declaring "free/sample," not "unknown."

The service must produce both `priced_sum` and `priced_count` in the same SQL aggregation that currently produces `total_target_value`. Suggested shape: `func.coalesce(func.sum(case((Requirement.target_price.isnot(None), Requirement.target_price * Requirement.target_qty), else_=0)), 0).label('priced_sum')` plus `func.count(Requirement.target_price).label('priced_count')` — `count()` on a nullable column already excludes nulls.

`_build_row_mpn_chips(requirements)` rules:
1. Iterate requirements in natural order (whatever `list_requisitions` currently loads — order by id asc).
2. Pass 1: for each req, append `{mpn: req.primary_mpn, role: "primary"}` if `primary_mpn` non-empty.
3. Pass 2: for each req, for each sub in `parse_substitute_mpns(req.substitutes)`, append `{mpn: sub, role: "sub"}`.
4. Dedupe by `mpn` **keeping the first occurrence** — so if an MPN is a primary in req #1 and a sub in req #2, it renders as primary. All primaries appear before any sub.
5. Return the flat list. No limit — the frontend decides visibility.

---

## Coverage semantic — recommendation

The coverage meter is the broker's glanceable read on "how far has sourcing gotten on this requisition?" Three candidate definitions for `coverage_filled`:

| Definition | What it means for the broker |
|---|---|
| ≥1 **sighting** | A vendor has been surfaced for this part. No price, no commitment — just "we know someone has it somewhere." |
| ≥1 **offer** | A vendor has given us a concrete price for this part. Actionable — we can quote the customer. |
| ≥1 active sourcing conversation | RFQ sent, reply pending, or in-flight. Mid-process, not finished. |

**Recommendation: `≥1 offer`.**

Reasoning: the user framed the mental model as *"ready to move on."* Of the three, only an offer answers "can I stop working on this part?" with yes. A sighting without a price still leaves work — call the vendor, negotiate, confirm stock. An active conversation is explicitly *not* done. Offers are the boundary between "sourcing is doing it" and "sourcing has done it."

Second-order evidence from the existing codebase: `app/scoring.py`'s sourcing score weights `offer_cnt` heavily (it's the state that advances requirement lifecycle from `sourcing` to `offered`). The coverage meter should align with that lifecycle signal, not with the upstream discovery step.

Consequence: `coverage_filled = COUNT(DISTINCT requirement_id) FROM offers WHERE requirement.requisition_id = req.id`. Implementation can add this as a subquery alongside the existing aggregations. If an existing `offer_cnt` per requisition already matches this exact semantic, reuse; otherwise add the subquery.

---

## Feature flag — `AVAIL_OPP_TABLE_V2`

Instant rollback without a code change. Wrap the entire new `<tr>` rendering path in a template gate; keep the old rendering alive during this PR.

**Settings entry** (`app/config.py`):

```python
avail_opp_table_v2: bool = True  # Gate for merged opportunity table visuals.
                                  # See docs/superpowers/specs/2026-04-21-opportunity-table-merged-design.md
                                  # Set to false and restart to revert to legacy rendering.
```

**Template gate** in `requisitions2/_table_rows.html` (identical pattern also applied to `_single_row.html` and `_table.html` thead):

```jinja
{% if avail_opp_table_v2_enabled %}
  {# new merged v2 rendering — this doc #}
  <tr id="rq2-row-{{ req.id }}"
      class="rq2-row group {{ urgency_accent_class(req.hours_until_bid_due, req.urgency) }}"
      x-data="rowActionRail()"
      tabindex="0"
      @mouseenter="show = true"
      @mouseleave="show = false"
      @focusin="show = true"
      @focusout.self="show = false"
      @keydown.enter="show = true"
      @keydown.escape="show = false"
      hx-get="/requisitions2/{{ req.id }}/detail"
      hx-target="#rq2-detail"
      hx-swap="innerHTML">
    {# checkbox, name cell, status cell, customer, coverage, deal #}
    {{ opp_name_cell(req) }}
    {{ opp_status_cell(req) }}
    ...
    {{ opp_row_action_rail(req, user) }}  {# absolutely positioned, x-show="show" #}
  </tr>
{% else %}
  {# legacy 5-col rendering — preserved verbatim from pre-merge _table_rows.html #}
  <tr id="rq2-row-{{ req.id }}" ...>
    {# existing cells unchanged #}
  </tr>
{% endif %}
```

**Context injection:** `app/routers/requisitions2.py` `_table_context()` already builds the context dict for `_table.html`, `_table_rows.html`, `_single_row.html`, and filter-triggered swaps — one injection point covers all four.

```python
# in _table_context() — add:
from app.config import settings as app_settings
...
return {
    "request": request,
    **result,
    "user": user,
    "users": users,
    "avail_opp_table_v2_enabled": app_settings.avail_opp_table_v2,
}
```

The legacy rendering in the `{% else %}` branch is verbatim-copied from the pre-merge version of each template (preserved to a comment block at the top of the file — see execution plan).

**Rollback procedure (documented in PR description and STABLE.md):**

```bash
# In production .env:
AVAIL_OPP_TABLE_V2=false

# Bounce the app container:
docker compose restart app

# Verify legacy rendering:
curl -s https://app/... | grep -c 'opp-status-dot'   # expect 0
```

No code change, no migration, no data touch. Rollback time ≈ 30 seconds.

**Follow-up PR (out of scope for this one):** after 7 days of production stability at `AVAIL_OPP_TABLE_V2=true`, a cleanup PR removes the `else` branch, the legacy row cells, and the flag itself. Tracked as a known follow-up in the PR description; not part of this spec's definition of done.

---

## Visuals — per cell

### Name cell (`opp_name_cell`)

```
┌────────────────────────────────────────────────────┐
│ [MPN1] [MPN2] [SUB1] [SUB2]  +5              ← row 1
│ Acme Q3 — power mgmt  [part match]            ← row 2
└────────────────────────────────────────────────────┘
```

- **Row 1** = `mpn_chips_aggregated(row.mpn_chip_items)`. Chip style unchanged from existing `_chip` macro: mono, 1px slate border, white bg, rounded. Primaries first, subs second, deduped. All chips render at equal weight. Visible count is dynamic — computed by `x-chip-overflow` directive at runtime (see below).
- **Row 2** = requisition name text (`text-[12px] text-[var(--opp-text-tertiary)]`) + inline match badges (existing component, unchanged).
- Cell gets `x-truncate-tip` on the name-text element only (chips use their own overflow directive).

### Status cell (`opp_status_cell`)

```
● Sourcing · 6h
```

- Dot: 8px, color per `.opp-status-dot--{bucket}` (already in styles.css).
- Label: plain text, 12px secondary. Buckets per uncommitted macro: `active→Open`, `sourcing→Sourcing`, `offers→Offered`, `quoting→Quoting`, `quoted→Quoted`. Other status values render as neutral gray with the raw status as label (titlecased).
- Time text (appended inline, `·` separator): `time_text(hours_until_bid_due)` — `Overdue`/`Nh`/`Nd` with color from `.opp-time--24h`/`--72h`/`--normal`.
- `aria-label` on the outer span carries `"{label}, {time text}"` for screen readers.
- Cell still inline-editable (dblclick → status picker); no change to editing behavior.

### Deal Value cell (`deal_value`)

Signature: `deal_value(amount, source)`. Rendered output per source:

| Source | Format | Classes | `title` attribute |
|---|---|---|---|
| `entered` | `$50,000` | `opp-deal opp-deal--tier-{...}` | `"Entered by broker"` |
| `computed` | `$50,000` (italic) | `opp-deal opp-deal--tier-{...} opp-deal--computed` | `"Computed from target prices (all parts priced)"` |
| `partial` | `~$30,000` (italic, leading tilde) | `opp-deal opp-deal--tier-{...} opp-deal--computed opp-deal--partial` | `"Floor estimate — N of M parts priced"` |
| `none` | `—` | `opp-deal opp-deal--tier-tertiary` | *(none)* |

Tier class by `amount` magnitude: `≥100k → primary-500`; `1k–99,999 → primary-400`; `<1k → tertiary`. For `partial`, magnitude is based on the shown (floor) amount, not a projected full value.

Visual rule for `partial`: the italic comes from the existing `opp-deal--computed` class (already in `styles.css`). The tilde is rendered **inside the span as literal text** (`~$30,000`), not via `::before`, so it's copy-paste friendly. The `opp-deal--partial` class exists as a CSS hook for future tweaks but starts with no additional styles — the tilde + italic already differentiates.

For screen readers, `aria-label` on the span reads: `"Estimated thirty thousand dollars, floor"` for partial; plain amount otherwise.

Title tooltip for `partial` substitutes the actual counts: `"Floor estimate — 3 of 5 parts priced"` — rendered by the macro from passed-in `priced_count` and `requirement_count`. This means the macro takes those two extra params **only when source == 'partial'**; they're optional for other sources. Plumb them from the row dict.

### Coverage cell (`coverage_meter`)

- 6 vertical segments, 6px × 14px, 2px gap. Filled = `round(6 × filled / total)` green; empty gray.
- When `total == 0`: render 6 empty segments + title=`"no parts yet"`.
- `role="img"` with `aria-label="Coverage: N of M parts sourced"`.

### Urgency accent — on `<tr>`

`urgency_accent_class(hours, urgency_field)` returns one of `opp-row--urgent-24h`, `opp-row--urgent-72h`, or `""`.

Resolution order (most urgent wins):
1. If `hours is not None and hours <= 24` → `urgent-24h`.
2. Else if `hours is not None and hours <= 72` → `urgent-72h`.
3. Else if `urgency_field == 'critical'` (manual override when deadline unset/unparseable like `"ASAP"`) → `urgent-24h`.
4. Else `""`.

This preserves the manual-critical override that exists in today's `<tr class="... border-l-2 border-rose-400 ...">` — brokers flag "this is urgent regardless of date" and we honor it.

### Other cells (Customer)

- Customer: plain text, 13px secondary, `x-truncate-tip` for overflow. Chrome removed (drop any filter-pill-style backgrounds). `req.customer_display or '—'` (matches current template's field name).

### Row-hover action rail (`opp_row_action_rail`)

Replaces the Actions column from the original v2 column set. Absolutely positioned over the rightmost cells of the row; hidden by default; revealed on row hover or row keyboard focus.

**DOM structure** (emitted by the macro, inside the `<tr>` but after all `<td>` cells):

```html
<td class="opp-action-rail-cell" colspan="0">  <!-- colspan 0 = renders zero-width, rail escapes via absolute positioning -->
  <div class="opp-action-rail"
       x-show="show"
       x-transition:enter="opp-action-rail--enter"
       role="toolbar"
       aria-label="Row actions">
    <!-- icon buttons, see Actions list below -->
  </div>
</td>
```

**CSS** (new):

```css
.opp-action-rail-cell { position: relative; width: 0; padding: 0; }
.opp-action-rail {
  position: absolute;
  top: 50%;
  right: 8px;
  transform: translateY(-50%);
  display: inline-flex;
  gap: 2px;
  padding: 4px;
  background: rgba(255,255,255,0.96);
  backdrop-filter: blur(2px);
  border: 1px solid var(--opp-sep);
  border-radius: 6px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.08);
  opacity: 0;
  transition: opacity 90ms ease-out, transform 90ms ease-out;
  pointer-events: none;
}
.opp-action-rail[style*="display: inline-flex"],
tr:hover .opp-action-rail,
tr:focus-within .opp-action-rail {
  opacity: 1;
  pointer-events: auto;
}
.opp-action-rail button {
  width: 28px; height: 28px;
  display: inline-flex; align-items: center; justify-content: center;
  color: var(--opp-text-secondary);
  background: transparent;
  border: none; border-radius: 4px;
  cursor: pointer;
}
.opp-action-rail button:hover { background: var(--opp-sep); color: var(--opp-text-primary); }
.opp-action-rail button:focus-visible { outline: 2px solid var(--opp-status-open); outline-offset: 1px; }
```

**Actions list** — migrated from the existing workspace bulk/row-action endpoints already in `app/routers/requisitions2.py:323` (`POST /requisitions2/{req_id}/action/{action_name}`, `RowActionName` enum). Same endpoints, same server behavior; only the trigger UI is new. All buttons target `#rq2-table` with `hx-swap="outerHTML"` (matches the existing router's response of `_table.html`).

| Action | Icon | Visible when | `action_name` enum |
|---|---|---|---|
| Archive | heroicon `archive-box` | `req.status != 'archived'` | `archive` |
| Activate | heroicon `arrow-uturn-up` | `req.status == 'archived'` | `activate` |
| Claim | heroicon `hand-raised` | `not req.claimed_by_id and user.role in ('buyer','trader','manager','admin')` | `claim` |
| Unclaim | heroicon `hand-thumb-down` | `req.claimed_by_id == user.id` | `unclaim` |
| Mark Won | heroicon `trophy` (emerald) | status in `(active,sourcing,offers,quoting,quoted,open,reopened)` | `won` |
| Mark Lost | heroicon `x-circle` (rose) | same status set | `lost` (with `hx-confirm`) |
| Clone | heroicon `document-duplicate` | always | `clone` |

Each button carries an `aria-label` (e.g., `aria-label="Archive Acme Q3"`) so screen-reader users get the action + subject on focus. `hx-confirm` is preserved on Mark Lost (and optionally Archive per existing workspace behavior) — same UX as today's bulk bar actions. The rail is a pure rendering/affordance swap, not a behavior change — the seven enum members above already exist in `RowActionName`; no new routes are introduced.

**Keyboard accessibility:**

- `<tr>` gets `tabindex="0"`. Entering focus (`Tab` into the row) triggers `@focusin → show=true`, revealing the rail — so the buttons themselves become tabbable next.
- `Enter` on the row (when focused) also sets `show=true`, redundant but guarantees the rail is discoverable via keyboard-only.
- `Escape` dismisses: `show=false`. Row focus is preserved.
- Without the rail visible, the `<tr>`'s own `hx-get="/requisitions2/{id}/detail"` handler is what `Enter` triggers on the row — so keyboard users who just want to open the detail pane can `Tab → Enter` once. Revealing the rail is `Tab → (focus first rail button) → Enter`.

To avoid double-triggering (row click = open detail; rail-button click = action), each rail button has `@click.stop` so the action stops propagation.

**Alpine component** `rowActionRail()`:

```js
Alpine.data('rowActionRail', () => ({
  show: false,
  init() {
    // Listen for a global Escape as a safety net; some browsers route Escape
    // to the active element and we want to close even if focus moved out.
    this.$el.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { this.show = false; }
    });
  },
}));
```

**Chrome removed from legacy:** the `⋮` dropdown button on each row disappears in v2. Its contents migrate to the rail. Behavior parity is the bar.

### Row-level chrome strip

- `border-bottom: 0.5px solid var(--opp-sep)` (already defined), no zebra striping, no row background except on hover: `hover:bg-[rgba(18,127,191,0.04)]` (very light brand tint).
- Remove the thick `border-l-2 border-rose-400` rule for `urgency='critical'` — replaced by `urgency_accent_class` output.

### `<thead>` restyle

- Header cells get class `opp-col-header` (already defined: 10px, tertiary, letter-spacing 0.8px, sentence case — not uppercase).
- Drop any header background color.
- Sort chevrons keep working; just inherit the lighter color.

---

## Column set and order (confirmed: 6-col compact, no role variation)

The `/requisitions2` workspace is split-screen: the left panel is a **compact scan list**, the right panel is the action surface. There's no need for role-specific column ordering in a workspace context — the left panel answers one question for every role: "what requisition should I look at next?"

**Final set** (6 cells total):

`[✓] · Name · Status · Customer · Coverage · Deal`

Actions column removed — the row-hover action rail covers it with zero idle visual weight (see "Row-hover action rail" above).

**Dropped vs. legacy workspace layout (today's 5 cols):**
- **Requirement count** replaced by **Coverage** (same denominator + sourcing progress signal in one cell).

**Added vs. legacy workspace layout:**
- **Coverage** (6-segment meter).
- **Deal** (magnitude-typed dollar amount).

**Dropped vs. original v2 proposal (column-set A):**
- **Owner** — not meaningful in a workspace view; the user is inherently the owner-ish of their own queue. Owner stays visible in the detail panel.
- **Offers count** — low signal when Coverage (which is offers-based) already carries the offer-progress read.
- **Deadline (raw date)** — folded into Status cell time text. The urgency accent on `<tr>` conveys priority; brokers who need the exact date click into detail.
- **Updated** — low signal for scan; detail panel shows it.
- **Urgency** (already dropped in v1 column-set A decision).
- **Created** (already dropped).
- **Actions** — replaced by hover action rail.

**Default column widths** (localStorage-persistable via existing `resizableTable` plumbing):

| Col | Default width | Min | Fixed? |
|---|---|---|---|
| Checkbox | 40px | 40px | yes |
| Name | 1fr | **320px** (see chip worst-case below) | no |
| Status | 130px | 100px | no |
| Customer | 140px | 100px | no |
| Coverage | 70px | 60px | no |
| Deal | 110px | 80px | no |

Total of fixed minima = 40 + 320 + 100 + 100 + 60 + 80 = 700px. The `/requisitions2` left panel default width is 55% of viewport (≈ 880px at 1600px viewport), so default widths fit with ~180px slack that flows into Name. When a user drags the split divider to narrow the left panel, columns (except Name) float toward their min and Name absorbs the deficit before hitting its own 320px floor; further narrowing beyond that point triggers horizontal scroll inside the table wrapper, not visual breakage.

Users can drag to resize anything not "fixed." Reset menu restores defaults.

---

## Chip overflow behavior — `x-chip-overflow` directive

The chip row's visible count is dynamic — it depends on (a) the current Name column width, (b) the rendered width of each chip's label (varies with MPN length).

**Algorithm (runs on mount and on container resize, via `ResizeObserver`):**

1. The directive binds to the chip-row container. Container children are the chips in spec order (primaries first, subs second) followed by a hidden `+N` element.
2. On mount and on each `ResizeObserver` callback (container inline-size change), the directive:
   a. Clears `display: none` from all chips; hides `+N`.
   b. Measures container inner width.
   c. Walks chips left→right, summing widths + gaps. When the next chip would exceed the width minus the measured `+N` element width, stop.
   d. Hides all remaining chips.
   e. If any are hidden, shows `+N` (text = `+{hidden count}`) and attaches a cloned-DOM `DocumentFragment` of the hidden chips to `+N` via an `_tipNodes` property on the element (a runtime property, not an HTML attribute). `x-truncate-tip` consumes this on hover by cloning the fragment again and appending it — zero `innerHTML`, zero HTML-string attributes, XSS-safe.
3. **Primaries-first guarantee:** Because primaries are rendered first in DOM order, a left-to-right overflow walk naturally preserves the rule that primaries never hide while subs are visible.
4. **Edge case — not even all primaries fit:** Primaries still get priority over subs (DOM order). If only 2 of 5 primaries fit, the `+N` count is `(3 primaries + all subs)`. Rule intent is preserved: no sub is ever shown while a primary is hidden.
5. **Empty chip list:** Render `—` tertiary placeholder. Directive does nothing.
6. **Resize cadence:** `ResizeObserver` fires independently of the `resizableTable` internals — no coupling to a custom event contract. Callback work is batched to `requestAnimationFrame` to avoid thrash during drag.
7. **Cleanup:** directive disconnects the observer on element removal (HTMX swap, row re-render).

**Reuses `x-truncate-tip` for the hover reveal:** the `+N` element carries `x-truncate-tip` and receives a `_tipNodes` DocumentFragment property at runtime from `x-chip-overflow`. The directive is extended:

- If `el._tipNodes` is a DocumentFragment with children, clone it and `appendChild` into the tooltip (no `innerHTML`, no string-attribute payloads — XSS-safe).
- Else fall back to the element's own `textContent` (existing behavior).
- Hover activation always shows the tooltip when `_tipNodes` is set — the "only when clipped" gate only applies to the textContent path.

**Why not `data-tip-content`?** An earlier draft of this spec proposed a `data-tip-content` HTML attribute whose value was raw HTML read via `innerHTML`. That pattern is an XSS anti-pattern regardless of whether the current callers produce trusted content. The DOM-property approach sidesteps the `innerHTML` surface entirely.

### Chip worst-case analysis (drives Name min-width of 320px)

The Name cell fights chips vs. name vs. match badges for a ~half-viewport column. Calculating what actually fits at min-width:

**Chip dimensions** (from the existing `_chip` macro in `_mpn_chips.html`):
- Classes: `px-1.5 py-0.5 font-mono text-slate-800 bg-white rounded border border-slate-300`
- `px-1.5` = 6px left + 6px right = **12px horizontal padding**
- `border` = 1px × 2 = **2px border**
- Font: 13px mono (`font-mono` + default 13px from styles.css)
- Inter-chip gap: 4px (from `gap-1` on the flex row — `0.25rem` = 4px)

**Character width at 13px monospace** ≈ 7.8px per char (SF Mono / Menlo, rough empirical).

**Chip width formula:** `12 + 2 + (chars × 7.8)` px. Rounded examples:

| MPN chars | Chip px |
|---|---|
| 6 (short, e.g., `LM317`) | 61 |
| 8 (e.g., `BAV99LT1G`) | 76 |
| 10 (typical, e.g., `LM317BT/NOPB`) | 92 |
| 12 (long) | 108 |
| 15 (outlier) | 131 |
| 20 (very long) | 170 |

**`+N` element width:** `+NN` at 12px sans ≈ 24px (plus ~4px gap to preceding chip).

**At Name column min-width = 320px:**
- Subtract cell padding: `px-3` on `<td>` = 12px × 2 = 24px → inner width 296px.
- Chip row inner width: `296px - 24px (+N reserve) - 4px (gap before +N) = 268px` working budget for chips.
- Typical 10-char MPN = 92px per chip → **2 full chips** fit (92+4+92 = 188px), third won't (272px > 268px). Result: `[chip][chip] +N`.
- Long 12-char MPN = 108px per chip → **2 chips** still fit (108+4+108 = 220px < 268px). Result: `[chip][chip] +N`.
- Outlier 15-char MPN = 131px per chip → **2 chips** fit (131+4+131 = 266px < 268px, just barely). Result: `[chip][chip] +N`.
- 20-char extreme = 170px per chip → **1 chip** fits. Result: `[chip] +N`.

**Conclusion:** at the 320px Name min-width, the user reliably sees **2 chips** for MPNs up to 15 chars, and 1 chip in the degenerate 20+ char case. The primaries-first DOM order guarantees those 2 visible chips are always the most-important primary MPNs. Hovering `+N` reveals the rest. This is acceptable: the worst-case broker sees 2 primaries + a tooltip, not 0.

**Original spec's 240px Name min would have degraded this to 1 chip even for typical 10-char MPNs** — below the "primaries visible" promise. Hence the bump to 320px.

**If 15+ char MPNs turn out to be common in real data** (flag during implementation — grep `SELECT MAX(CHAR_LENGTH(primary_mpn)) FROM requirements` in staging): mitigations in priority order:
1. Shrink chip font to 12px mono — cuts chip width by ~15%.
2. Tighten chip padding to `px-1` (8px total) — cuts another ~4px per chip.
3. Internally truncate chip text to 12 chars with ellipsis; full MPN on chip hover via `x-truncate-tip`.
4. Bump Name min-width further (to 380px) and live with the narrower Status/Customer cells.

None of those go in this PR — they're listed so future tuners don't re-derive the math.

---

## `x-truncate-tip` finalization

Final location: `app/static/htmx_app.js` (globally registered, consistent with `splitPanel` and `resizableTable`). The uncommitted copy in `app/static/js/requisitions2.js` is deleted.

Signature unchanged from uncommitted version except for the `_tipNodes` property-read extension described above. Styling — new rule in `styles.css`:

```css
.truncate-tip {
  position: fixed;
  z-index: 50;
  max-width: 320px;
  padding: 6px 10px;
  font-size: 12px;
  line-height: 1.35;
  color: #fff;
  background: #1C2130;
  border-radius: 4px;
  pointer-events: none;
  opacity: 0;
  transition: opacity 80ms ease-out;
  box-shadow: 0 4px 12px rgba(0,0,0,0.12);
}
.truncate-tip.visible { opacity: 1; }
.truncate-tip .opp-chip-row { flex-wrap: wrap; gap: 4px; }
```

The nested `.opp-chip-row` rule lets chip-overflow tooltips render the hidden chips wrapped.

---

## Tests

### Unit (`tests/test_requisition_list_service.py`)

- Existing uncommitted tests for `_hours_until_bid_due`, `_resolve_deal_value`, and `test_list_row_exposes_v2_visual_fields` — keep.
- Add: `test_build_row_mpn_chips_orders_primaries_before_subs`.
- Add: `test_build_row_mpn_chips_dedupes_keeping_primary_role` (MPN that's primary in one req and sub in another → emitted once, role='primary').
- Add: `test_build_row_mpn_chips_empty_when_no_requirements`.
- Add: `test_list_row_exposes_coverage_and_chip_fields` (expect `coverage_filled`, `coverage_total`, `mpn_chip_items` keys).
- Add: `test_resolve_deal_value_all_priced_is_computed`.
- Add: `test_resolve_deal_value_some_priced_is_partial`.
- Add: `test_resolve_deal_value_zero_price_counts_as_priced` (free/sample parts don't taint full → partial).
- Add: `test_resolve_deal_value_none_priced_is_none`.
- Add: `test_list_row_exposes_deal_value_priced_counts` (expect `deal_value_priced_count`, `deal_value_requirement_count`).
- Add: `test_coverage_filled_counts_requirements_with_offers` — assert semantic from the "Coverage semantic" section (offers, not sightings).

### Macro (`tests/test_macros.py` — create if absent)

- Render `opp_status_cell` for each of the 5 buckets + unknown; assert correct class + aria-label.
- Render `deal_value` for each tier including zero and null; assert tier class, italic flag when `source=computed`, and the `~` + `--partial` class + tooltip copy `"Floor estimate — N of M parts priced"` when `source=partial`.
- Render `coverage_meter` for 0/0, 3/6, 6/6; assert filled-segment count matches rounding rule.
- Render `urgency_accent_class` for the 4 resolution paths including the manual-critical fallback.

### Playwright E2E — `e2e/requisitions2-visuals.spec.ts` (new)

- Each status bucket renders the expected dot color.
- Status cell shows time text with correct color for <24h, <72h, normal, Overdue.
- Row accent appears on rows with `hours ≤ 24` (red) and `hours ≤ 72` (amber); absent otherwise.
- Manual `urgency='critical'` row renders red accent even when deadline is "ASAP".
- Deal value: renders italic for `deal_value_source='computed'`; tier classes match magnitude; for `partial`, renders `~` prefix + italic + tooltip text containing "N of M parts priced".
- Coverage meter: seg count matches ratio; `role=img` + aria-label present.
- Chip row: primaries render before subs; **at default Name column width with typical 10-char MPNs, exactly 2 chips are visible**; hover `+N` shows tooltip containing all hidden chips.
- Dragging Name column narrower triggers chip re-measure within one animation frame (assert via `waitForFunction` on visible chip count change).
- Hover action rail:
  - Rail is not visible at pageload (no hover, no focus).
  - `mouseenter` on row reveals rail; `mouseleave` hides it.
  - Keyboard `Tab` onto row reveals rail (focus-based); `Escape` hides it.
  - Rail action set matches today's `⋮` dropdown — one assertion per action being present conditionally by status/role.
  - Clicking a rail button does NOT fire the row's `hx-get detail` handler (assert detail pane's URL didn't change).
- Flag-off path: with `AVAIL_OPP_TABLE_V2=false`, the list renders legacy classes (`opp-status-dot` **absent**, old `status_badge` and `⋮` dropdown markup **present**). One assertion is enough — presence of legacy vs. v2 class families.

### Playwright E2E — `e2e/requisitions2-resize.spec.ts` (existing)

- Existing split + column resize tests unchanged.
- The current red-phase `x-truncate-tip` test (commit `0f265d81`) keeps passing after directive is moved to `htmx_app.js`.

---

## Migration / rollback

- Frontend-only plus row-dict additions. No migration, no data reshape.
- Row-dict additions are pure reads; no writes. Legacy templates ignore the new fields.
- **Primary rollback — no code change:** set `AVAIL_OPP_TABLE_V2=false` in the environment, `docker compose restart app`. The template falls through to the preserved legacy `<tr>` rendering. Expected turnaround ≈ 30 seconds.
- **Secondary rollback — revert PR:** if the feature-flag branch itself is broken (e.g., template syntax error blocks both paths), revert the merge commit. No data cleanup required.
- Follow-up cleanup PR (after 7 days of stable flag-on in production) removes the legacy `else` branch, the legacy row cells, and the flag.

---

## Definition of done

- All tests green: unit + macro + both Playwright files.
- One desktop screenshot + one narrow-viewport screenshot attached to the PR, showing at least: all 5 status buckets, both urgency accents + manual-critical, **all four** deal-value sources including `partial` (with tilde + italic), three coverage ratios, chip overflow with +N revealed, and the hover action rail in both hover-revealed and keyboard-focus-revealed states.
- Feature flag `AVAIL_OPP_TABLE_V2` documented in `.env.example` with its default (`true`) and a one-line comment pointing at this spec.
- `STABLE.md` appends a pointer to this doc's token table (reuses existing v2 CSS vars) **and** documents the feature flag name, default, and rollback procedure.
- Two superseded specs marked in this doc's header — deletion of the two files happens in the same PR.
- `docs/APP_MAP_INTERACTIONS.md` documents `x-chip-overflow` and the extended `x-truncate-tip`.
- Uncommitted `app/static/js/requisitions2.js` `x-truncate-tip` block deleted.
- Legacy rendering still reachable via `AVAIL_OPP_TABLE_V2=false` — verified by a Playwright test that sets the env via a fixture and asserts legacy classes render.

---

## Open questions

None. All design decisions locked. Coverage semantic, deal-value partial behavior, feature-flag gating, chip ordering within-requirement (`id asc`, since Requirement has no user-settable line_number / sort_order field), 6-col compact set with hover action rail, and 320px Name min-width (derived from chip worst-case analysis) are all resolved above.

## Checkpoint

Stop. Await user review of this doc before implementation plan is written.
