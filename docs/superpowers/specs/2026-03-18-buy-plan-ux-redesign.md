# Buy Plan UX Redesign

**Date:** 2026-03-18
**Status:** Approved
**Approach:** Hybrid (Progressive Disclosure + Role-Focused Actions)

## Problem

The buy plan module serves three roles (salesperson, buyer, ops/manager) but presents the same dense layout to all of them. Key issues:

1. **Information overload** — everything shown at once regardless of what the user needs
2. **Unclear workflow** — no visual indication of where a deal stands in its lifecycle
3. **Dense line items table** — 10 columns with cramped inline PO forms
4. **No visual hierarchy** — every section has the same weight
5. **List view too wide** — 10 columns requiring horizontal scroll

## Design

### Detail View — Hybrid Layout

Replace the current stacked-sections layout with a compact, role-aware design:

**1. Compact Header with Inline Stats**
- Title, status badge, and key stats (cost, margin, line/vendor counts) on a single row
- Customer, Quote#, SO# as secondary text beside the title
- Replaces the 6-card stat grid that consumed significant vertical space

**2. Progress Bar Stepper**
- 5-segment horizontal bar: Draft → Submitted → Approved → PO Execution → Complete
- Completed segments filled green, current segment amber, future segments gray
- Labels below each segment, current stage label in bold
- Edge cases: Halted shows red segment, Cancelled grays entire bar with strikethrough label

**3. Role-Aware Action Banner**
- Blue callout card shown only when the current user has pending actions
- Contains a count badge and plain-English description of what needs doing
- Examples by role:
  - **Buyer:** "2 POs need to be cut. STM32F407VG (DigiKey) and MAX232CPE (Nexar)"
  - **Manager (pending plan):** "This plan needs your approval. $12,450 total, 5 lines."
  - **Ops (SO pending):** "Sales Order SO-4821 needs verification."
  - **Salesperson (rejected):** "Plan was rejected. Review notes and resubmit."
- Hidden when the user has no actions (e.g., salesperson viewing an active plan with no issues)

**4. Simplified Line Items Table**
- Reduce from 10 columns to 6: Part (MPN), Vendor, Qty, Unit Cost, Status, Action
- Remove: Unit Sell, Margin, Buyer, PO# as separate columns
- PO# shown inline in Status column when present (below the status badge)
- Buyer shown as a subtle label below vendor name
- Action column: contextual link text ("Enter PO →", "Verify →", checkmark for done)
- PO entry: clicking "Enter PO →" expands an inline row below (not a cramped cell form)

**5. Collapsible Secondary Sections**
- AI Insights, Notes & History, SO Verification collapsed by default
- Each shows a badge count on the collapsed header (e.g., "AI Insights (1 warning)")
- SO Verification shows a green checkmark badge when approved
- Click to expand; state persisted via Alpine.js `x-data`

**6. Workflow Action Buttons**
- Moved into the action banner when relevant (approve/reject buttons inside the manager banner)
- Cancel and Reset remain as subtle buttons in the header area
- Modals unchanged (submit, approve, reject, verify-so, cancel)

### List View — 7-Column Compact Table

Replace the 10-column table with a denser, more scannable layout:

| Column | Content | Notes |
|--------|---------|-------|
| Customer | Company name | Primary text, bold |
| SO# / Quote | SO# primary, Quote# secondary (stacked) | Mono font for SO# |
| Value | Dollar amount primary, line count secondary (stacked) | Right-aligned |
| Margin | Percentage in color-coded pill | Right-aligned |
| Progress | Mini 5-segment bar + stage label | Replaces Status + SO Verification columns |
| By | Submitter name | Compact |
| Date | Created date | Compact |

Key changes:
- SO# and Quote# merged into one stacked cell
- Cost and Lines merged into one stacked cell
- Status and SO Verification replaced by progress bar (one column instead of two)
- Eliminates horizontal scroll on most screens

### Progress Bar Mapping

| Plan Status | SO Status | Segments Filled | Label |
|-------------|-----------|-----------------|-------|
| draft | * | 1/5 | Draft |
| pending | * | 2/5 | Pending Approval |
| active | pending | 3/5 | Active (SO Pending) |
| active | approved, no POs cut | 3/5 | PO Execution |
| active | approved, some POs | 4/5 | PO Execution |
| active | approved, all verified | 5/5 (auto-completes) | — |
| completed | * | 5/5 green | Complete |
| halted | * | segment 3 red | Halted |
| cancelled | * | all gray | Cancelled |

### Action Banner Logic

```
if user.role in (buyer, trader):
    lines_needing_po = [line for line in plan.lines
                        if line.buyer_id == user.id
                        and line.status == 'awaiting_po']
    if lines_needing_po:
        show "{count} POs need to be cut. {part list}"

elif user.role in (manager, admin) and plan.status == 'pending':
    show "This plan needs your approval. ${total} total, {line_count} lines."

elif is_ops_member and plan.so_status == 'pending' and plan.status == 'active':
    show "Sales Order {SO#} needs verification."

elif is_ops_member:
    lines_needing_verify = [line for line in plan.lines
                            if line.status == 'pending_verify']
    if lines_needing_verify:
        show "{count} POs need verification."

elif user.id == plan.submitted_by_id:
    if plan.status == 'draft' and plan.cancellation_reason:
        show "Plan was rejected. Review notes and resubmit."
    elif plan.status == 'draft':
        show "Ready to submit. Add SO# to proceed."
```

## Files Changed

### Templates (modify)
- `app/templates/htmx/partials/buy_plans/detail.html` — full rewrite with hybrid layout
- `app/templates/htmx/partials/buy_plans/list.html` — 7-column layout with progress bars

### Backend (modify)
- `app/routers/htmx_views.py` — pass `is_ops_member` and user role context to detail template (already passed, may need minor additions for action banner data)

### No new files needed
- All changes are template-only with minor route context additions
- Progress bar and action banner are pure Jinja2 + Tailwind + Alpine.js
- No new models, schemas, or services required

## Testing

- Visual testing: verify all 6 plan statuses render correct progress bars
- Role testing: verify action banner shows correct content for each role
- Collapse testing: verify sections expand/collapse and badge counts are accurate
- Mobile: verify 7-column list fits without horizontal scroll on 375px+ screens
- Edge cases: empty plans, plans with no lines, halted/cancelled states
