# Sightings Manual Refresh with Cooldown

**Date**: 2026-03-25
**Status**: Draft (v2 — revised after spec review, type analysis, architecture review, silent failure audit)
**Author**: Claude (with user direction)

## Problem

Users need a manual "Refresh Sightings" button to re-run sourcing searches on demand. However, each refresh triggers API calls to up to 10 external sources (Nexar, BrokerBin, DigiKey, etc.), so unrestricted manual refreshes would burn through API quotas quickly.

## Solution

Add manual refresh at two levels — workspace-wide and per-requirement — with a **3-day per-requirement cooldown** for non-manager users. Managers and admins bypass the cooldown entirely.

## Cooldown Rules

| Rule | Detail |
|------|--------|
| Scope | Per requirement (each requirement has its own timer) |
| Duration | 3 days (new `sighting_refresh_cooldown_days` config, separate from `sighting_stale_days`) |
| Trigger | Set **optimistically before search starts** — cleared if search fails entirely |
| Bypass | Users with `role in (UserRole.MANAGER, UserRole.ADMIN)` bypass cooldown entirely |
| Storage | New `last_refreshed_at` DateTime(timezone=True) column on `requirements` table |
| Auto-refresh interaction | Nightly 3 AM job runs on its own 24h cycle freely; it also sets `last_refreshed_at`, which affects manual cooldown |

## UI Behavior

### Per-Requirement Detail Panel

The existing refresh button on the requirement detail panel gains cooldown awareness:

- **Cooldown expired or manager/admin**: Button enabled, normal "Refresh" label
- **Cooldown active (non-manager)**: Button disabled, shows "Refresh available in Xd Xh"
- **Refresh in progress**: Button disabled, shows spinner

### Workspace "Refresh All" Button

New button in the sightings workspace toolbar:

- Collects all requirement IDs currently loaded in the table (via `hx-include` on hidden inputs, matching the existing vendor modal pattern)
- Backend filters out requirements still within cooldown (for non-managers)
- Shows result summary via OOB toast: "Refreshed 12 of 18 requirements (6 skipped — recently refreshed)"
- If some searches fail: "Refreshed 10 of 18 (6 skipped, 2 failed)"
- Disabled while refresh is in progress (spinner)
- Managers/admins: all requirements eligible, none skipped for cooldown

**Note**: The sightings workspace is cross-requisition — there is no "current requisition." The button operates on whatever requirements are in the current table view.

## Data Model Changes

### Migration: Add `last_refreshed_at` to `requirements`

```python
# New column on requirements table
last_refreshed_at = Column(DateTime(timezone=True), nullable=True)
```

- Nullable: requirements that have never been refreshed will have `NULL`
- `NULL` is treated as "cooldown expired" (eligible for refresh)
- No index needed — queries filter by requisition_id first, small result sets
- **Backfill**: Migration populates `last_refreshed_at` from most recent `Sighting.created_at` per requirement to prevent a burst of API calls on first deploy

### New Config Value

```python
# In app/config.py
sighting_refresh_cooldown_days: int = 3  # Separate from sighting_stale_days (display concern)
```

`sighting_stale_days` controls the visual stale indicator. `sighting_refresh_cooldown_days` controls the manual refresh rate limit. These can diverge independently.

## Backend Changes

### 1. Cooldown Helper

New file: `app/services/sighting_helpers.py`

```python
from typing import NamedTuple
from datetime import timedelta

class RefreshEligibility(NamedTuple):
    allowed: bool
    remaining: timedelta | None

def is_refresh_allowed(
    requirement: Requirement,
    user: User,
    cooldown_days: int,
) -> RefreshEligibility:
    """
    Returns RefreshEligibility(allowed, remaining_cooldown).
    - Manager/Admin always get RefreshEligibility(True, None)
    - NULL last_refreshed_at => RefreshEligibility(True, None)
    - Past cooldown => RefreshEligibility(True, None)
    - Within cooldown => RefreshEligibility(False, remaining_timedelta)
    """

def format_cooldown_remaining(remaining: timedelta) -> str:
    """Returns '2d 14h' or '3h' format."""
```

Uses `NamedTuple` for clean named access (`result.allowed`, `result.remaining`) instead of bare tuple.

### 2. Single Refresh Endpoint (existing)

`POST /v2/partials/sightings/{requirement_id}/refresh`

Changes:
- Check `is_refresh_allowed()` before running search
- If blocked: return 200 with the detail panel (button rendered disabled) + OOB toast "Refresh available in Xd Xh" (NOT 429 — HTMX doesn't swap on non-2xx without extra config)
- On refresh start: set `requirement.last_refreshed_at = utcnow()` **before** calling `search_requirement()` (optimistic lock prevents race conditions)
- Define "success": `search_requirement()` returns without exception AND at least 1 source has `status == "ok"` in `source_stats`
- On total failure (all connectors fail or exception): clear `last_refreshed_at` back to previous value, return detail panel + OOB error toast "Search failed — please try again"
- Log manager/admin cooldown bypasses with user ID and requirement ID

### 3. Batch Refresh Endpoint (existing)

`POST /v2/partials/sightings/batch-refresh`

Changes:
- Keep accepting `requirement_ids` form parameter (JSON array) — no breaking change to existing callers
- Filter out cooldown-blocked requirements (for non-managers/admins)
- Run search sequentially on eligible requirements (no `asyncio.gather` across requirements — existing pattern, avoids API rate limit exhaustion)
- Set `last_refreshed_at` per requirement (optimistically before each search, cleared on failure)
- Track per-requirement results: refreshed count, skipped count, failed count, failed requirement IDs
- Return OOB toast with summary
- Log failed requirement IDs at error level for Sentry

### 4. Auto-Refresh Job

`app/jobs/sourcing_refresh_jobs.py`

Changes:
- Set `last_refreshed_at` when the daily 3 AM job successfully refreshes a requirement
- This means auto-refresh resets the manual cooldown timer — intentional, since the data is now fresh
- Add `exc_info=True` to exception logging for full tracebacks
- The job's own 24h staleness check runs independently of the manual cooldown

## Template Changes

### 1. Sightings Workspace (`list.html`)

- Add "Refresh All" button in the toolbar area
- Button uses `hx-include` to collect requirement IDs from the table (hidden inputs), matching existing vendor modal pattern
- `hx-post="/v2/partials/sightings/batch-refresh"`, `hx-indicator` for spinner
- Pass `is_manager` to template context

### 2. Requirement Detail (`detail.html`)

- Modify existing refresh button with Jinja2 conditional (no Alpine.js needed — countdown is server-rendered as "Xd Xh" string)
- Pass `refresh_allowed` (bool) and `refresh_available_at` (formatted string or None) to template context
- Disabled state: `disabled` attribute + countdown text + muted styling

### 3. No New Partial Needed

- Toast messages use existing `_oob_toast()` helper — no new partial required
- Cooldown state rendered server-side in the detail partial

## Error Handling (Critical)

The existing refresh endpoints use bare `except Exception` that silently swallow search failures. This must be fixed as part of this feature:

| Scenario | Current Behavior | Required Behavior |
|----------|-----------------|-------------------|
| Single refresh search fails | Silent — returns detail panel as if nothing happened | Return detail panel + OOB error toast, do NOT set `last_refreshed_at` |
| Batch refresh per-requirement failure | Increments `failed` counter, no details | Track which IDs failed, include in toast, log at error level |
| All connectors fail (no exception) | Returns empty sightings, looks like "no results" | Check `source_stats` — if 0 sources OK, treat as failure |

## Edge Cases

| Case | Handling |
|------|----------|
| Requirement with no sightings ever | `last_refreshed_at` is NULL → eligible for refresh |
| Concurrent refresh on same requirement | Optimistic `last_refreshed_at` set before search prevents duplicate API calls |
| Manager refreshes → non-manager tries | Cooldown applies to non-manager (cooldown is per-requirement, not per-user) |
| Batch refresh with 0 eligible | Toast shows "0 of N refreshed — all recently refreshed" |
| Search returns 0 sightings but sources OK | Treated as success — `last_refreshed_at` stays set (data is just empty) |
| Config `sighting_refresh_cooldown_days` changed | Cooldown adjusts automatically since helper reads from config |
| Post-deploy burst risk | Migration backfills `last_refreshed_at` from latest sighting `created_at` |

## Security

- Existing `require_user` dependency on sightings routes handles authentication
- Bypass check uses `user.role in (UserRole.MANAGER, UserRole.ADMIN)` — explicit in code
- Manager/admin bypass logged for audit trail

## Testing

- Unit test `is_refresh_allowed()` with: manager bypass, admin bypass, non-manager within cooldown, non-manager past cooldown, NULL `last_refreshed_at`
- Unit test `format_cooldown_remaining()` with various timedelta values
- Integration test single refresh: 200 with disabled button + toast when blocked, 200 with refreshed panel when allowed
- Integration test single refresh failure: `last_refreshed_at` cleared, error toast returned
- Integration test batch refresh: correct skip/fail counts, `last_refreshed_at` set only on success
- Verify `last_refreshed_at` is set after auto-refresh job
- Verify concurrent refresh is prevented by optimistic lock

## No New Dependencies

Uses existing: search service, HTMX patterns, OOB toast helper, `UserRole` enum. Adds one config value (`sighting_refresh_cooldown_days`).
