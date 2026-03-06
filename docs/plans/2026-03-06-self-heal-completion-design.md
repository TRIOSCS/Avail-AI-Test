# Self-Heal Pipeline Completion — Design Doc

## Overview

Complete the self-heal pipeline so it can run the full test-fix-retest loop autonomously. Three infrastructure gaps block this today: missing agent auth endpoint, broken patch validation, and 18 real app bugs found by the agent sweep.

## Architecture (unchanged)

```
[Find Trouble Button] -> [Loop Manager]
                              |
                    +---------+---------+
                    |                   |
              Phase 1: Quick        Phase 2: Deep
              Playwright Sweep      Claude Agent Testing
              (SiteTester)          (test-site.sh)
                    |                   |
                    +----> Tickets <----+
                              |
                    [Dedup via consolidation]
                              |
                    [Auto-process: diagnose + execute]
                              |
                    [Patch generator + validation] <-- NEW
                              |
                    [fix_queue/ volume]
                              |
                    [Host watcher applies + rebuild]
                              |
                    [Verify-retest via SiteTester]
                              |
                    [Re-sweep until clean]
```

## Phase 1: Agent Session Endpoint

### Problem
`test-site.sh` calls `POST /auth/agent-session` to get a session cookie for Playwright agents. The endpoint doesn't exist. Result: 3 areas completely blocked (rfq, tagging, upload), all others limited to API-only testing.

### Solution
Add `POST /auth/agent-session` to `app/routers/auth.py`:
1. Validate `x-agent-key` header against `settings.agent_api_key`
2. Look up `agent@availai.local` user (already seeded at startup)
3. Set `request.session["user_id"]` to agent user's ID
4. Return 200 — Starlette SessionMiddleware signs the cookie automatically

No new config. Uses existing `AGENT_API_KEY` env var.

### Files
- `app/routers/auth.py` — add endpoint
- `tests/test_auth.py` — add test

## Phase 2: Patch Validation Pipeline

### Problem
Claude generates patches with search strings that don't match the actual file content (escaped `\n` vs real newlines). No validation before queuing, so bad patches reach the watcher and fail at apply time. 3 of 4 fixes failed this way.

### Solution — Three Layers

**Layer 1: `patch_generator.py` — Validate after generation**
After Claude returns patches, read each target file and verify every `search` string is literally present. If any search string doesn't match, reject the entire patch set and return None.

**Layer 2: `execution_service.py` — Gate before queue write**
Before writing fix JSON to `fix_queue/`, call validation. If it fails, set ticket back to `diagnosed`, log the error, and return without queuing.

**Layer 3: `apply_patches.py` — Pre-flight dry-run**
Before applying ANY patch, validate ALL patches against current file state. All-or-nothing: if any search string is missing, abort entirely. Add diagnostics showing expected vs actual content.

### Files
- `app/services/patch_generator.py` — add `_validate_patches()`
- `app/services/execution_service.py` — call validation before queue write
- `scripts/apply_patches.py` — add pre-flight validation + better error messages
- `tests/test_execution_service.py` — test validation rejects bad patches

## Phase 3: Bug Triage & Fix

### Pipeline attempts (5 simpler bugs)
- Ticket detail API missing `updated_at` field
- Phone number formatting (55 contacts with raw digits)
- Requisitions draft count missing from `/counts` endpoint
- Status filter only handling `archive` vs all
- Deadline field accepting freeform text ("ASAP")

### Manual fixes (13 complex bugs)
- Pipeline scoring inflation + test data pollution (5 bugs)
- Admin orphaned data — 39K requirements + 214K sightings (data cleanup)
- Vendor sort/filter broken (4 bugs)
- Duplicate contacts — 94 records (data cleanup, not code)
- 125 tickets with null risk_tier + category (backfill)

## Verification

After all three phases:
1. Run `test-site.sh` — agents authenticate via agent-session, test all 17 areas
2. Confirm pipeline tickets get validated patches written to queue
3. Watcher applies patches, rebuilds, retests
4. Find Trouble loop runs clean (0 new tickets for 2 consecutive rounds)

## Config
No new env vars. Uses existing:
- `AGENT_API_KEY` — agent auth
- `SELF_HEAL_ENABLED` — gates auto-processing
- `SELF_HEAL_AUTO_DIAGNOSE` — gates auto-diagnosis
- `SELF_HEAL_AUTO_EXECUTE_LOW` — gates auto-execution
