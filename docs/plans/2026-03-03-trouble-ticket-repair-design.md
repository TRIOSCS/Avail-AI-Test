# Trouble Ticket Repair Plan — 28 Tickets, 6 Phases

**Date**: 2026-03-03
**Status**: Approved
**Scope**: 28 open trouble tickets (22 unique after dedup) + 1 production bug

## Summary

All 28 tickets are UI category, submitted via the report button. Grouped by page area
for systematic repair. 4 duplicate pairs identified (#637/#638, #648/#649/#655, #641/#663).

One production error discovered: `StringDataRightTruncation` on notifications.title VARCHAR(200).

## Ticket Inventory

| Risk | Count | Unique |
|------|-------|--------|
| High | 4 | 3 (after #651 moves to Phase 1) |
| Medium | 12 | 10 |
| Low | 11 | 8 |
| Unclassified | 1 | 1 |
| **Total** | **28** | **22 + 1 prod bug** |

## Phase 1: Critical Security + Production Error

**Priority**: BLOCKING — must deploy before other phases.

| Ticket | Risk | Issue | Fix |
|--------|------|-------|-----|
| #652 | HIGH | Ticket detail exposes raw AI prompts, internal file paths, console errors | Hide `generated_prompt`, `ai_prompt`, `file_mapping`, `console_errors`, `browser_info` from non-admin view in `tickets.js` |
| #651 | HIGH | Missing User Settings — only admin settings visible | Add user profile section (display name, notification prefs, default view) to settings |
| PROD BUG | CRIT | `StringDataRightTruncation: VARCHAR(200)` on notifications.title | Alembic migration: `String(200)` → `String(500)`, truncate in `notification_service.py` |

**Files**: `app/static/tickets.js`, `app/static/crm.js`, `app/templates/index.html`, `app/models/notification.py`, `app/services/notification_service.py`, `alembic/versions/049_*.py`

## Phase 2: RFQ/Sourcing Page (8 tickets)

**Heaviest page — all fixes in `app/static/app.js`**

| Ticket | Risk | Issue | Fix |
|--------|------|-------|-----|
| #646 | HIGH | Vendor autocomplete invisible | Replace plain `<input>` (~line 5722) with dropdown popover of unique vendor names from sightings |
| #656 | MED | Archive view missing Offers/Quotes/Activity sub-tabs | Add `_renderDdTabPills()` call for archive rows (~line 6612) |
| #658 | MED | Archive Offers badge not clickable | Fixed by #656 — tab pills enable click navigation |
| #661 | MED | Archive MATCHES column always "—" | Populate `proactive_match_count` in API for archived reqs; backend fix in requisitions router |
| #660 | MED | RFQ filter persistence — resets on tab switch | Fix `setMainView()` (~line 7025) to not reset filters; persist to localStorage |
| #647 | MED | Pipeline chart non-interactive, scroll issues | Add onclick to `cc-pipe-item` spans (~line 2298), fix scroll container |
| #664 | LOW | '74 Offers' counter not functional | Verify `setToolbarQuickFilter('green')` click handler works (~line 6412) |
| #645 | HIGH | UX Audit: 13 issues (meta-ticket) | Close after above fixes; any remaining sub-issues tracked separately |

**Backend files**: `app/routers/requisitions/` (proactive_match_count for archive)

## Phase 3: Customers/Accounts Page (3 unique tickets)

| Ticket | Risk | Issue | Fix |
|--------|------|-------|-----|
| #659 | MED | Strategic filter stuck — can't deactivate | Fix `setCustFilter()` toggle in `crm.js` — clicking active chip should deactivate |
| #662 | LOW | Duplicate "Pivot International — Pivot International" | Fix company label rendering — deduplicate `name — name` pattern |
| #639 | LOW | "(PASS)" suffix on prospecting names | Add tooltip or remove if vestigial |

**Files**: `app/static/crm.js`

## Phase 4: Materials + Scorecard Page (4 unique tickets)

| Ticket | Risk | Issue | Fix |
|--------|------|-------|-----|
| #642 | MED | Materials nav tabs disappear on #rfqs route | Fix route handler to preserve tabs when navigating back |
| #648 | LOW | Import Stock form always expanded (=#649, #655) | Collapse by default, add toggle, reset on `showMaterials()` |
| #653 | MED | Scorecard prize amounts wrong + "Not Qualified" for all | Fix qualification threshold logic and prize tier config |
| #657 | MED | UNIFIED column lacks tooltip | Add title attribute explaining composite score calc |

#654 (demotivating display) covered by fixing #653 qualification logic.

**Files**: `app/static/app.js`, `app/static/crm.js`

## Phase 5: Contacts + Vendors Page (2 unique tickets)

| Ticket | Risk | Issue | Fix |
|--------|------|-------|-----|
| #637 | MED | Missing "+ Add Vendor" button (=#638) | Add button to vendor list header, wire to new vendor modal |
| #640 | MED | Missing quick-action buttons in contact detail | Add Call/Email/Edit buttons to contact slide-in panel |

#643 (Vendors/RFQ UX) close after #637 if no specific remaining items.

**Files**: `app/static/crm.js`, `app/templates/index.html`

## Phase 6: Tickets/Self-Heal + System (3 unique tickets)

| Ticket | Risk | Issue | Fix |
|--------|------|-------|-----|
| #650 | LOW | Tickets defaults to "In Progress" | Change `_adminFilter` default to `'submitted'` in `tickets.js` |
| #644 | LOW | New Ticket opens full page instead of modal | Add Cancel/Back button, or convert to modal |
| #641 | LOW | API Health badge lacks info (=#663) | Add tooltip with failing API name + last check time |

**Files**: `app/static/tickets.js`, `app/static/crm.js`

## Execution Strategy

1. **Phase 1 first** (blocking — security + production crash)
2. **Phases 2-6 parallelizable** via subagents (each phase = independent page area)
3. After each phase: run test suite, resolve tickets in DB, deploy
4. Close duplicate tickets (#638, #649, #655, #663) immediately with note pointing to primary

## Duplicate Ticket Resolution

| Duplicate | Primary | Action |
|-----------|---------|--------|
| #638 | #637 | Close as duplicate |
| #649 | #648 | Close as duplicate |
| #655 | #648 | Close as duplicate |
| #663 | #641 | Close as duplicate |
