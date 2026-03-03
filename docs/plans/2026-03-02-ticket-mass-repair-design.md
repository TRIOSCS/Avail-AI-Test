# Trouble Ticket Mass Repair — 116 Tickets in 8 Phases

**Date:** 2026-03-02
**Status:** Approved
**Scope:** 116 active trouble tickets (111 diagnosed, 5 escalated)

## Breakdown

| Risk | Count |
|------|-------|
| High | 7 |
| Medium | 34 |
| Low | 75 |

## Phases

### Phase 1: HIGH-RISK ESCALATED (7 tickets)
- #258 Needs Attention filter (backend+frontend)
- #309, #330, #355, #344 Log Offer missing fields (quote_date, offer_expiry, received_date)
- #293 RFQ send confirmation dialog
- #306 RFQ 23438 sourcing issue

### Phase 2: TOOLTIPS & LABELS — Batch 1 (17 tickets)
SOURCING column, RESP%, column headers, dashboard metrics

### Phase 3: TOOLTIPS & LABELS — Batch 2 (16 tickets)
Vendor scores, clickable badges, tier labels, sidebar icons

### Phase 4: LOG OFFER MODAL (11 tickets)
Vendor dropdown, part pre-population, escape key, cancel UX

### Phase 5: SORTING, FILTERING & VIEWS (12 tickets)
Priority sort, Ready to Quote filter, My Work view

### Phase 6: NAVIGATION, TABS & LAYOUT (15 tickets)
Context tabs, sidebar, archive view, sub-tabs consistency

### Phase 7: EXPAND TARGETS + VENDOR + DUPLICATES (9 tickets)
44px touch targets, vendor ranking, sighting dedup

### Phase 8: DASHBOARD, ONBOARDING & REMAINING (24 tickets)
Onboarding, dashboard metrics, misc small fixes

## Execution Model

- 4-5 parallel worktree agents per phase
- File-region ownership prevents merge conflicts
- Ralph Loop monitors agent progress
- Test suite + deploy after each phase
- Bulk ticket closure per phase
