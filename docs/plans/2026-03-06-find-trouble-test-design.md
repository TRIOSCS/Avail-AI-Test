# Find Trouble Test — Design Doc

## Overview

"Find Trouble" is an in-app automated testing loop that exhaustively tests every area of the application, creates trouble tickets for issues found, auto-heals low/medium risk items, and repeats until no new issues are discovered.

## Architecture

```
[Find Trouble Button] -> [Backend Loop Manager]
                              |
                    +---------+---------+
                    |                   |
              Phase 1: Quick        Phase 2: Deep
              Playwright Sweep      Claude Agent Testing
              (SiteTester)          (test-site.sh subprocess)
                    |                   |
                    +----> Tickets <----+
                              |
                    [Dedup via consolidation]
                              |
                    [Auto-process: diagnose + execute]
                              |
                    [Watcher applies fixes + rebuild]
                              |
                    [Re-sweep until clean]
```

## Components

### 1. Loop Manager Service (`app/services/find_trouble_service.py`)

Orchestrates the full test-fix-retest cycle as a background asyncio task.

**Loop logic:**
1. Run Phase 1 (Playwright sweep) — ~2-3 min
2. Run Phase 2 (Claude agent deep tests) — ~10 min
3. Create tickets with dedup (check existing open tickets in same area before creating)
4. Auto-process tickets (diagnose, queue fixes for low/medium risk)
5. Wait for fix_queue to drain (poll every 15s, max 5 min)
6. Repeat from step 1
7. Stop conditions: 2 consecutive clean rounds OR max 10 rounds OR user cancels

**State tracking:**
- `_active_job: dict | None` — singleton, only one Find Trouble run at a time
- Tracks: round number, phase, per-area status, tickets created, fixes applied
- Cancellable via flag check between phases

### 2. Backend Endpoints (in `app/routers/trouble_tickets.py`)

- `POST /api/trouble-tickets/find-trouble` — start the loop (admin only)
- `GET /api/trouble-tickets/find-trouble/stream` — SSE stream for live progress
- `POST /api/trouble-tickets/find-trouble/stop` — cancel running job
- `GET /api/trouble-tickets/find-trouble/prompts` — agent prompt panel data

### 3. Phase 1: Playwright Sweep (existing `site_tester.py`)

Already built. Visits all 17 areas, clicks every visible button (skipping destructive ones), captures:
- Console errors
- Network failures
- Slow page loads (>3s)
- Click exceptions

No changes needed to SiteTester itself.

### 4. Phase 2: Claude Agent Deep Testing

Calls `scripts/test-site.sh` as a subprocess from within the app container. The script:
- Launches up to 8 parallel Claude Code agents
- Each agent gets Playwright MCP browser access + area-specific prompt
- Agents follow detailed test scripts (search real MPNs, verify drawer tabs, check currency formatting, test filters, etc.)
- Agents file tickets via `POST /api/trouble-tickets` with dedup check against `/api/trouble-tickets/similar`

**Subprocess approach**: The app spawns `test-site.sh` via `asyncio.create_subprocess_exec`, captures stdout for progress parsing. The script already handles agent lifecycle, timeouts, and result collection.

### 5. Ticket Dedup (enhanced `create_tickets_from_issues`)

Before creating a Playwright-sourced ticket, check:
1. Is there an open ticket with the same area + similar title? (fuzzy match on title, exact on area)
2. If yes, skip creation and log "duplicate skipped"
3. If no, create and auto-process

This prevents the ticket count from ballooning across rounds.

### 6. Frontend (`tickets.js`)

**Find Trouble button** in admin dashboard header (red button, next to "+ New Ticket").

**On click:**
- Confirmation dialog explaining what will happen
- Button changes to "Running... (Stop)" with red pulsing dot
- Progress panel appears below stats bar showing:
  - Current round / max rounds
  - Current phase (Sweep / Deep Test / Healing / Waiting)
  - 17-area grid: each area shows status icon (pending/testing/pass/fail)
  - Live ticket count: created / healed / remaining
  - Log stream of recent events

**SSE connection** for live updates. Reconnects on disconnect.

**Stop button** sends POST to /find-trouble/stop, shows "Stopping after current phase..."

### 7. Fix Integration

The loop doesn't apply fixes directly. It:
1. Creates tickets -> auto_process_ticket diagnoses and queues fixes
2. Execution service writes fix JSON to `fix_queue/`
3. `self_heal_watcher.sh` (running on host via cron) picks up fixes, applies patches, rebuilds, triggers verify-retest
4. The loop waits for `fix_queue/` to empty before next round

If the watcher isn't running, the loop still completes all rounds — it just won't see fixes applied between rounds.

## Data Flow

```
Round N:
  Phase 1 -> issues[] -> dedup -> new tickets -> auto_process
  Phase 2 -> agents file tickets via API -> dedup built into similar endpoint
  Wait for fix_queue to drain (or timeout)
Round N+1:
  Phase 1 -> fewer issues (fixes applied) -> ...
  ...
Stop when: 0 new tickets for 2 consecutive rounds
```

## Error Handling

- Phase 1 failure: log error, skip to Phase 2
- Phase 2 failure (test-site.sh crash): log error, continue to next round
- Individual agent timeout: test-site.sh handles retries internally
- App crash during sweep: job state lost (in-memory), user can restart
- Watcher not running: loop still works, just no fixes between rounds

## Config

No new env vars. Uses existing:
- `SELF_HEAL_ENABLED` — gates auto-processing
- `SELF_HEAL_AUTO_DIAGNOSE` — gates auto-diagnosis
- `SELF_HEAL_AUTO_EXECUTE_LOW` — gates auto-execution of low/medium risk

## Files to Create/Modify

1. **Create** `app/services/find_trouble_service.py` — loop manager
2. **Modify** `app/routers/trouble_tickets.py` — add 4 endpoints
3. **Modify** `app/static/tickets.js` — Find Trouble button + progress UI
4. **Modify** `app/services/site_tester.py` — add dedup to `create_tickets_from_issues`
5. **Fix** 3 broken test files (import errors)
6. **Restore** (done) scripts from deep-cleaning branch
