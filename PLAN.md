# RFQ Layout Redesign Plan

## Overview
Four interconnected changes to simplify the RFQ workflow and reduce tab-switching.

## Change 1: Merge Open + Sourcing into Unified View
**What changes:**
- Remove the separate "Open" and "Sourcing" pill buttons, replace with single "Active" pill
- Combine sub-tabs from both views: `['parts', 'sightings', 'offers', 'activity', 'quotes', 'qa', 'files']`
- Keep "Archive" as a separate view (it serves a different purpose)
- Merge the column sets: use the RFQ (Open) table columns but add Sourcing Score + RFQs Sent + Resp %
- The drill-down header gets both sets of action buttons (Search All, Log Offer, Add Part, Upload, Paste)

**Files:**
- `app/static/app.js`: `_ddSubTabs()`, `_ddDefaultTab()`, `setMainView()`, `renderReqList()` thead, `_renderReqRow()`, `_renderDdTabPills()`
- `app/templates/index.html`: Remove "Open"/"Sourcing" pills, replace with "Active"; update mobile pills too
- `app/static/styles.css`: Minor adjustments

## Change 2: Split-Pane Parts + Offers
**What changes:**
- When the "parts" sub-tab is active, render a split-pane layout: parts table on the left, offers panel on the right
- Use the existing `.split-panel` CSS classes (already defined in styles.css lines 2103-2106)
- Left pane (40%): parts table (`_renderDrillDownTable`)
- Right pane (60%): offers list (`_renderDdOffers`) — or placeholder "No offers yet" if empty
- On mobile: stack vertically (already handled by existing CSS at line 2383)
- Remove "offers" as a standalone sub-tab since it's now visible alongside parts
- Updated sub-tabs become: `['parts', 'sightings', 'activity', 'quotes', 'qa', 'files']`

**Files:**
- `app/static/app.js`: Modify `_renderDdTab()` for 'parts' case to render split pane, update `_ddSubTabs()` to remove standalone 'offers'

## Change 3: RFQ Send as Slide-In Panel (not modal)
**What changes:**
- Convert the `#rfqModal` modal into a slide-in panel that appears on the right side of the screen (like a drawer)
- Give it a persistent class `.rfq-drawer` instead of `.modal-bg`
- The drawer slides in from the right, stays open while users interact with the main list behind it
- State is preserved if users click away (drawer just hides, doesn't reset)
- Add a minimize/restore toggle so users can collapse it to a small bar
- Keep all existing phases (prepare, ready, preview, results) — just change the container

**Files:**
- `app/templates/index.html`: Replace `#rfqModal .modal-bg` wrapper with `.rfq-drawer` panel
- `app/static/styles.css`: Add `.rfq-drawer` styles (slide from right, width: 520px)
- `app/static/app.js`: Update `ddSendBulkRfq()` to open drawer instead of modal, update close behavior

## Change 4: Summary Dashboard per Requisition
**What changes:**
- Add a summary stats bar at the top of each drill-down panel (above the sub-tabs)
- Shows 4-5 key metrics as compact stat cards:
  - Parts: X total
  - Sourced: X/Y (with progress bar)
  - Offers: X received
  - RFQs: X sent, Y% response rate
  - Quote: status badge (Draft/Sent/Won)
- This replaces the need to click through tabs to understand req status at a glance
- Rendered by a new `_renderDdSummary(reqId)` function

**Files:**
- `app/static/app.js`: New `_renderDdSummary()` function, called from `toggleDrillDown()` and `_openMobileDrillDown()`
- `app/static/styles.css`: `.dd-summary` stat card styles

## Implementation Order
1. Summary dashboard (smallest, no breaking changes)
2. Merge Open + Sourcing (medium, changes view model)
3. Split-pane Parts + Offers (medium, changes drill-down rendering)
4. RFQ drawer (largest, changes modal → panel)

## Testing
- Existing tests in `tests/test_routers_rfq.py` test backend endpoints — these are unaffected
- Frontend changes are UI-only, no API contract changes
- Manual testing checklist:
  - Verify all sub-tabs load correctly in unified view
  - Verify split-pane renders parts + offers side by side
  - Verify RFQ drawer opens, stays open, and sends correctly
  - Verify summary stats update when data changes
  - Verify mobile layouts still work (stacked split-pane, mobile drill-down)
