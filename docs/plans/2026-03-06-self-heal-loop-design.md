# Self-Heal Loop Redesign

**Date**: 2026-03-06
**Status**: Approved
**Goal**: Replace broken subprocess-based execution with a working Claude API + host watcher pipeline that tests, fixes, verifies, and creates follow-up tickets automatically.

## Problem

The self-heal execution service fails on every ticket with `Permission denied: '/root/availai'` because:
1. It tries to run `claude -p` CLI inside Docker — CLI not installed
2. `cwd="/root/availai"` is the host path, not the container path (`/app`)
3. Even if fixed, container can't persist file changes (source baked into image)

Additionally, the rollback service is a stub (always returns healthy) and there are no post-fix verification tests.

## Architecture

```
App (Docker container)                 Host watcher (systemd/cron)
──────────────────────                 ─────────────────────────────
1. SiteTester finds bug
2. Auto-diagnose (existing)
3. Claude API generates patch    →
4. Write fix JSON to shared vol  →     5. Read fix JSON
                                       6. Apply patch to /root/availai
                                       7. git add + commit (fix branch)
                                       8. docker compose up -d --build
                                       9. POST /api/trouble-tickets/{id}/verify-retest
                                 ←
10. SiteTester retests area
11. Pass → resolve ticket
    Fail → revert commit, escalate,
           create regression ticket
```

## Components

### 1. Rewritten execution_service.py (in-container)

Replaces the broken subprocess approach. Uses Anthropic SDK directly:

- Reads affected source files from `/app/` (container filesystem)
- Sends diagnosis + file contents + fix prompt to Claude API (Haiku for cost)
- Parses structured response: `{file_path, original, replacement}` patches
- Writes fix payload as JSON to shared volume `/app/fix_queue/{ticket_id}.json`
- Updates ticket status to `fix_queued`

Fix JSON format:
```json
{
  "ticket_id": 123,
  "ticket_number": "TT-20260306-001",
  "risk_tier": "low",
  "patches": [
    {
      "file": "app/static/tickets.js",
      "search": "original code block",
      "replace": "fixed code block"
    }
  ],
  "test_area": "tickets",
  "created_at": "2026-03-06T12:00:00Z"
}
```

### 2. Host watcher script (scripts/self_heal_watcher.sh)

Simple bash loop that:
- Watches `/root/availai/fix_queue/` for new `.json` files
- For each fix file:
  - Creates branch `fix/ticket-{id}`
  - Applies patches using Python helper (search/replace in files)
  - Commits with descriptive message
  - Runs `docker compose up -d --build`
  - Waits for health check to pass
  - Calls verify-retest endpoint
  - On failure: `git revert`, rebuild, move fix file to `fix_queue/failed/`
  - On success: move fix file to `fix_queue/applied/`
- Runs as systemd timer (every 2 min) or simple cron

### 3. Verify-retest endpoint

New endpoint: `POST /api/trouble-tickets/{id}/verify-retest`

- Reads ticket's `tested_area` field
- Runs SiteTester on JUST that one area (not full sweep)
- If no issues found → resolve ticket, create notification
- If issues found → revert status to `diagnosed`, create regression child ticket linked to parent
- Returns `{passed: bool, issues: [...], regression_ticket_id?: int}`

### 4. Shared volume for fix queue

Add to docker-compose.yml:
```yaml
app:
  volumes:
    - ./fix_queue:/app/fix_queue
```

Simple directory-based queue. No Redis/DB needed.

### 5. Simplified file structure

**Remove:**
- Nothing removed — keep existing services but gut/rewrite execution_service.py

**Modify:**
- `app/services/execution_service.py` — full rewrite (Claude API + JSON queue)
- `app/services/rollback_service.py` — replace stub with real SiteTester retest
- `app/routers/trouble_tickets.py` — add verify-retest endpoint

**Create:**
- `scripts/self_heal_watcher.sh` — host-side fix applier
- `scripts/apply_patches.py` — helper to apply search/replace patches
- `app/services/patch_generator.py` — Claude API patch generation logic

### 6. Cost control

- Use Claude Haiku for patch generation (~$0.01-0.05 per ticket)
- Keep existing budget checks ($2/ticket, $50/week)
- Track actual API token usage via response metadata

### 7. Safety rails

- Only low/medium risk tickets auto-execute
- Host watcher creates a git branch per fix (easy revert)
- Verify-retest MUST pass before ticket resolves
- Max 3 fix attempts per ticket before escalation
- Regression tickets auto-escalate to human review

## What changes for the user

- Tickets actually get fixed instead of stuck at "diagnosed"
- After fix, the system retests and either confirms or creates a follow-up
- All fixes are git commits on branches (auditable, revertable)
- Dashboard shows real fix success/failure instead of Permission denied errors

## Test plan

- Unit tests for patch_generator (mock Anthropic API)
- Unit tests for verify-retest endpoint (mock SiteTester)
- Unit tests for fix JSON serialization/parsing
- Integration test: full loop with mock Claude response
- Host watcher: manual test on staging
