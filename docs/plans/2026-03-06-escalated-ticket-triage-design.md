# Escalated Trouble Ticket Triage — 44 Tickets

**Date**: 2026-03-06
**Status**: Triaged, ready for implementation plan

## Summary

44 escalated trouble tickets reviewed and categorized into 5 action groups.
Tickets span security vulnerabilities, broken UI features, data quality issues, and test noise.

## Action Groups

### GROUP 1: Close as Noise (13 tickets)

Playwright "Exception testing" tickets — all `ERR_CONNECTION_REFUSED` during server rebuild.
Not code bugs. Bulk-close as `resolved` with note "Server was down during test sweep."

Tickets: TT-075, TT-076, TT-077, TT-078, TT-079, TT-080, TT-081, TT-082, TT-083, TT-084, TT-085, TT-087, TT-091

### GROUP 2: Fix Now — Security (3 tickets)

| Ticket | Issue | Fix |
|--------|-------|-----|
| TT-110 | Admin can delete own account | Add self-deletion guard in delete endpoint |
| TT-034 | XSS in stock import vendor_name | Sanitize/escape HTML in vendor_name |
| TT-033 | 500 on long vendor_name | Add length validation, return 400 |

### GROUP 3: Fix Now — Broken Core Features (9 tickets)

| Ticket | Issue | Fix |
|--------|-------|-----|
| TT-004 | Load More broken — `loadMoreCustomers` undefined | Fix JS function name/reference |
| TT-005 | Contacts renders 10K+ rows, freezes browser | Add pagination (match accounts pattern) |
| TT-006 | Ticket rows not clickable | Add click handler + detail view |
| TT-012 | Vendor tier filter does nothing | Fix backend tier query filter |
| TT-013 | Requisition status filter broken | Fix status filter in core.py:330-333 |
| TT-026 | Bell badge always 0 | Wire to /api/notifications instead of /api/sales/notifications |
| TT-040 | needs-attention always empty | Fix query filter / user scoping |
| TT-102 | No delete confirmation for users | Add confirm dialog before DELETE |
| TT-105 | No validation on Create User form | Add client+server validation |

### GROUP 4: Fix Now — Data Quality / Display (7 tickets, 6 unique)

| Ticket | Issue | Fix |
|--------|-------|-----|
| TT-036 + TT-112 | Proactive scorecard $5B (DUPLICATE) | Fix aggregation calc, likely unit price * qty error |
| TT-037 | Hot offers: 15 items, same req, 100x prices | Filter test data or fix pricing |
| TT-032 | Attention feed polluted with test data | Filter/clean req 21702 test quotes |
| TT-100 | AI summary uses wrong user name | Pass selected user ID to AI prompt |
| TT-103 | Quotes Awaiting Response: 109 vs 0 | Align AI query with metric query |
| TT-043 | Call activities missing subject/contact | Fix log_call_activity to populate fields |

### GROUP 5: Defer (12 tickets)

**Data cleanup / infrastructure** (needs separate projects):
- TT-028: 336K orphaned records (batch strategy needed)
- TT-029: Mouser connector error (API key/config)
- TT-047: 94 duplicate vendor contacts (dedup strategy)
- TT-018: Duplicate vendor entries (vendor dedup)
- TT-020: Compound industry values (data normalization)
- TT-022: Revenue range null for all prospects (enrichment gap)
- TT-049: At-risk sites 999 sentinel (activity tracking needed)
- TT-015: Deadline mixed date/string (migration needed)

**Low impact / cosmetic**:
- TT-007: Ticket stats count mismatch
- TT-016: 125 tickets missing classification
- TT-023: API health recent_checks always zero
- TT-014: Vendor detail vs engagement data conflict

## Execution Order

1. Bulk-close noise tickets (GROUP 1) — 2 minutes
2. Security fixes (GROUP 2) — highest priority
3. Broken features (GROUP 3) — user-facing impact
4. Data quality (GROUP 4) — display issues
5. Update deferred tickets status to "deferred" with notes (GROUP 5)

## Success Criteria

- All 13 noise tickets closed
- 3 security vulnerabilities patched with tests
- 9 broken features restored with tests
- 6 data display issues fixed with tests
- 12 deferred tickets documented with rationale
- Full test suite passes, coverage maintained at 97%+
