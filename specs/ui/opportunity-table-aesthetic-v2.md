> **SUPERSEDED** by `2026-04-21-opportunity-table-merged-design.md`. Retained for historical context only.

# Opportunity Table — Aesthetic Refactor v2

## Purpose
Refactor the opportunity management list view to prioritize aesthetic information density. No new features, no behavior changes, no new data fields unless explicitly flagged below. Pure visual refactor so brokers can scan-process req state faster.

## Why
The current table treats every row with equal visual weight. Brokers scanning for "what needs me today" or "where's the money" have to read every row to find out. The goal is to let typography, color, and spacing carry that information pre-attentively — before the conscious read.

The governing principle: **data should be the darkest thing on the page; chrome should be the lightest.** Every pixel of non-data is noise the brain has to filter.

---

## Pre-coding discovery (stop after this and wait for approval)

Before touching any code, reply with:

1. The file paths for:
   - The opportunity management table template/component
   - Any partials it imports (row, cell, badge, chip)
   - The CSS / Tailwind config it depends on
   - The FastAPI router + service feeding data to the view
2. The current data contract — list every field available for a row (MPN list, description, status, qty, unit price, bid due date, coverage meter numerator/denominator, time-in-status, deal total if computed).
3. A step-by-step plan naming each file you'll change and in what order. Keep each step under ~150 lines per playbook.
4. Any of the visual elements below that require data contract changes (e.g., if `hours_until_bid_due` isn't computed anywhere, flag it — I'll decide whether to add it or drop the feature).

Do not start coding until I approve the plan.

---

## Design principles

1. **Typographic magnitude encoding on Deal Value column.** Font weight and color scale with dollar magnitude. The column becomes a heatmap — the eye finds the money without reading numbers.
2. **Status as colored dot + plain label**, not a chip. Chips fight the row; dots disappear until needed.
3. **Time-to-bid merged into status cell.** Same lifecycle question, one token. Urgency expressed through text color.
4. **Row-edge urgency accent.** 3px left border on rows due <24h (red) or <72h (amber). Peripheral-vision trigger.
5. **Vertical stack in Part cell.** MPN on top (mono, weight 500, primary), description below (sans, 11px, tertiary). Recovers a whole column.
6. **Secondary data fades.** Qty and Unit Price are tertiary gray 12px. Only primary data (MPN, Deal Value) gets full contrast.
7. **Chrome removed.** No filter-pill backgrounds, no chip borders, no column-header backgrounds, no zebra stripes. Separators hair-thin (0.5px).
8. **Coverage as 6-segment meter** — if already implemented, keep. Otherwise build: 6 vertical segments, filled = green, empty = gray.

---

## Exact tokens — use these, do not improvise

### Colors

| Purpose | Hex |
|---|---|
| TRIO blue (active filter, primary button only) | `#127FBF` |
| Status dot — Open | `#378ADD` |
| Status dot — Sourcing | `#EF9F27` |
| Status dot — Offered | `#1D9E75` |
| Status dot — Quoted | `#7F77DD` |
| Urgency border — <24h | `#E24B4A` |
| Urgency border — <72h | `#854F0B` |
| Time text — <24h (bold) | `#A32D2D` |
| Time text — <72h | `#854F0B` |
| Time text — otherwise | tertiary gray (theme var) |
| Coverage meter — filled | `#639922` |
| Coverage meter — empty | `#D3D1C7` |

### Deal-value typography thresholds

| Range | Weight | Color |
|---|---|---|
| ≥ $100,000 | 500 | primary |
| $10,000 – $99,999 | 400 | primary |
| $1,000 – $9,999 | 400 | primary |
| $100 – $999 | 400 | tertiary |
| < $100 | 400 | tertiary |

### Typography

- MPN: mono, 13px, weight 500, primary color
- Description: sans, 11px, weight 400, tertiary color
- Status label: sans, 12px, weight 400, secondary color
- Qty, Unit Price: tabular-nums, sans, 12px, secondary color
- Deal Value: tabular-nums, sans, 13px, weight per table above
- Column headers: sans, 10px, tertiary color, letter-spacing 0.8px, sentence case (not uppercase)

### Layout

- Grid template: `16px 1fr 108px 38px 60px 82px 48px`, column gap 10px
- Row padding: 13px vertical, 20px horizontal
- Urgency-accented rows: reduce left padding to 17px, add `border-left: 3px solid [color]`
- Row separator: `0.5px solid` border-tertiary only. No zebra.

---

## Implementation constraints (from AVAIL playbook)

- Write tests alongside the refactor. Playwright visual regression for the table view is sufficient; add at least one test per status-color variant and one per urgency-accent variant.
- Max ~150 lines per response. Break into steps.
- Any new file needs a header docstring: purpose, description, business rules, called-by, depends-on.
- Use Loguru, not print().
- Flag on every response:
  - N+1 queries introduced by new template logic
  - Data contract changes needed (e.g., computed `hours_until_bid_due`, computed `deal_value`)
  - Any spec drift from this document
  - Accessibility regressions (color-only state encoding needs an `aria-label` or `title` fallback — each status dot must carry its label as accessible text, not just color)
- Update `STABLE.md` with the new visual token set so future changes don't drift.
- Changelog entry at the end, git commands included.

---

## Definition of done

- Playwright suite green; new visual-regression snapshots committed.
- One desktop screenshot + one narrow-width screenshot attached to the PR, showing all four status colors and both urgency-accent variants represented.
- Any columns dropped from the main view for narrow-width behavior are called out in the PR description with a note on where the data now lives (detail pane or hover).
- Spec file `specs/ui/opportunity-table-aesthetic-v2.md` (this file) committed to the repo if not already present.
- `STABLE.md` updated with the visual token table above.

---

## Checkpoint

Stop after discovery + plan. Wait for my approval before writing any code.
