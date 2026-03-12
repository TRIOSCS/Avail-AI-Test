# CHANGELOG

All notable changes to the project are logged here.

## 2026-03-12 — System optimization & tech debt cleanup

### Bug Fixes
- **Fixed quota_map key mismatch** in `main.py`: `hunter` → `hunter_enrichment`, `clearbit` → `clearbit_enrichment` — quotas were not being applied to these enrichment sources
- **Fixed sync blocking call** in `email_jobs.py`: `run_site_ownership_sweep` now runs in executor instead of blocking the event loop
- **Fixed deprecated `asyncio.get_event_loop()`** in `email_jobs.py` and `tagging_admin.py` (6 occurrences) — replaced with `asyncio.get_running_loop()`

### Code Cleanup
- **Extracted 350-line inline SOURCES list** from `main.py` into `app/data/api_sources.json` (already existed) — `main.py` reduced from ~1090 to ~684 lines
- **Consolidated router registration** in `main.py`: grouped all imports at top, organized `include_router` calls by core vs MVP-gated
- **Added logging to silent `except` blocks** in `teams_alert_service.py`, `teams.py`, `dashboard_briefing.py`, `vendor_email_lookup.py` — failures were being swallowed silently
- **Removed one-off data fix** from `startup.py` (req 23446 deadline correction) — should not run on every boot
- **Fixed duplicate section header** in `startup.py`
- **Fixed `_exec` docstring** in `startup.py`: said "DDL" but function executes data operations
- **Deleted dead `selfheal_jobs.py`** module (stub with only a "REMOVED" comment) and its stub test `test_retry_stuck_diagnosed.py`
- **Added auto-expiry** to Clear-Site-Data header: self-disables after 2026-03-17 instead of requiring manual removal

### Unchanged (pre-existing — flagged for future work)
- 336 pre-existing test failures (no regressions introduced)
- Bare `except` blocks in NC/ICS worker code (browser automation — intentional)
- Duplicate monitoring/scheduler code across `nc_worker`/`ics_worker` (candidates for consolidation into `search_worker_base`)
- `type: ignore` comments (5 instances, mostly in test schemas)
- Pre-existing F821 lint errors in `teams_alert_service.py` (undefined names from planned intelligence layer)

## 2026-03-12

- Prep for human testing: added STABLE.md (registry of critical files) and this CHANGELOG. No code behavior change.
