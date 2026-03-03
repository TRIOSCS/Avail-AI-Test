# Trouble Ticket Resolution — Final 21 Tickets

**Date:** 2026-03-03
**Scope:** 21 remaining unresolved trouble tickets across 5 workstreams
**Approach:** Execute existing `trouble-ticket-repair-plan.md` tasks + 4 new items

## Summary

Started with 35 unresolved tickets. After triage:
- 11 closed (already fixed by recent commits, meta-tickets, or duplicates)
- 3 closed (data cleanup — stripped "(PASS)" from 188 company names, profile tab exists, pipeline clickable)
- 21 remain → grouped into 5 workstreams below

## Workstream 1: RFQ/Sourcing (7 tickets — HIGH PRIORITY)

| Task | Ticket | Fix |
|------|--------|-----|
| W1-1 | #646 | Vendor autocomplete dropdown in sourcing drill-down |
| W1-2 | #656 | Verify archive sub-tabs render (may already work post R2-9) |
| W1-3 | #658 | Add onclick to archive OFFERS badge → expand to offers tab |
| W1-4 | #660 | Remove filter reset from setMainView() |
| W1-5 | #661 | Archive MATCHES column → show offer_count |
| W1-6 | #667 | Add parts section to New Req modal (or quick-add after create) |
| W1-7 | #668 | Fix sightings "Available" filter — verify qty_available logic |

## Workstream 2: Materials & Scorecard (5 tickets)

| Task | Ticket | Fix |
|------|--------|-----|
| W2-1 | #642 | Add navHighlight('navMaterials') to showMaterials() |
| W2-2 | #648 | Collapse Import Stock form by default |
| W2-3 | #653 | Fix scorecard prize amounts / qualification display |
| W2-4 | #654 | Improve "Not Qualified" messaging with guidance |
| W2-5 | #657 | Add tooltip explaining UNIFIED column |

## Workstream 3: Accounts & CRM (3 tickets)

| Task | Ticket | Fix |
|------|--------|-----|
| W3-1 | #637 | Add "+ Add Vendor" button to vendor list header |
| W3-2 | #640 | Add Email/Call quick-action buttons to contact panel |
| W3-3 | #662 | Deduplicate company label when site name matches company name |

## Workstream 4: Tickets/Self-Heal + System (5 tickets)

| Task | Ticket | Fix |
|------|--------|-----|
| W4-1 | #644 | Add back/cancel button on new ticket form |
| W4-2 | #650 | Default tickets filter to 'submitted' |
| W4-3 | #664 | Verify 74 Offers counter works, fix if not |
| W4-4 | #665 | Rewrite Proactive Offers empty state message |
| W4-5 | #641 | API Health badge tooltip with failing API names |

## Workstream 5: Accessibility (1 ticket — NEW)

| Task | Ticket | Fix |
|------|--------|-----|
| W5-1 | #666 | Add visible focus ring CSS for New Requisition modal inputs |

## Execution Order

1. W1 (RFQ) — most impactful, touches app.js heavily
2. W2 (Materials/Scorecard) — independent of W1
3. W3 (CRM) — touches crm.js, independent
4. W4 (Tickets/System) — small fixes across tickets.js + app.js
5. W5 (Focus ring) — CSS only

Commit after each workstream. Run test suite after W1 and W3.
Full coverage check before final push.
