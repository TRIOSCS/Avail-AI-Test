# 9-Bug Audit Fix Design — 2026-03-03

## Bugs & Root Causes

### crm.js Fixes

| # | Bug | Line | Root Cause | Fix |
|---|-----|------|-----------|-----|
| 1 | `$NaN` offer headers | 1803 | `last_quoted` exists but `sell_price` is null | Check `sell_price != null` in ternary |
| 2 | `Edited by ?` | 1833 | Backend returns `"?"` or null for unattributed offers | Display nothing when value is falsy or `"?"` |
| 5 | Strategic filter stuck | 125 | `setCustFilter` sets mode but never toggles back to `all` | Toggle: if same mode clicked, reset to `all` |
| 6 | Empty Vendor Name | 2060 | No validation before PUT | Guard: require non-empty `vendor_name` |
| 9 | Won quote no buttons | 2169 | `statusActions.won` has only text, no buttons | Add Re-quote, Copy, Edit buttons |

### app.js Fixes

| # | Bug | Line | Root Cause | Fix |
|---|-----|------|-----------|-----|
| 3 | Condition filter broken | 5559 | Filter checks `s.condition` only; column shows condition + date_code; most sightings have null condition | Search both `condition` and `date_code` |
| 4 | No batch RFQ button | 6623 | Button only in sourcing header, not in sightings panel | Add inline bulk button in sightings filter bar |
| 7 | Available filter no-op | 5568 | Checks `qty > 0` but nearly all sightings have qty; ignores `is_unavailable` | Also exclude `is_unavailable` sightings |
| 8 | Sort collapses rows | 6386 | Restore uses `toggleDrillDown()` which reloads content | Use CSS class restoration only |
