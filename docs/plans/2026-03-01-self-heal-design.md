# AVAIL Self-Heal — Design Document

**Date:** 2026-03-01
**Status:** Approved
**Scope:** 5-phase AI-managed trouble ticket and self-repair pipeline

---

## Overview

Users submit trouble tickets describing bugs. The system auto-captures context (recent errors, page route, browser info), diagnoses issues via Claude AI, generates constrained repair prompts, and executes fixes locally via subprocess (with pluggable backend for future GitHub Actions support). Human oversight at every risk tier. Alert-only post-deploy monitoring — no auto-revert on single-server deployment.

## Architecture Decisions

### UI: SPA Views (not separate pages)
AVAIL is a single-page app. All trouble ticket UI lives in `app/static/tickets.js` as JS-rendered panels, matching how requisitions/companies/contacts work. Nav pills added to `index.html`.

### Services: Stateless Functions (not classes)
All existing services use stateless functions with `db: Session` passed in. Self-heal services follow this pattern.

### Claude API: Existing Integration
Uses `claude_json()` / `claude_structured()` from `app/utils/claude_client.py` with `model_tier="smart"` (Sonnet) for diagnosis and classification.

---

## Phase 1: Ticket Model + Submission UI + Auto-Context

### New Files
- `app/models/trouble_ticket.py` — TroubleTicket model (Base, indexes, FKs)
- `app/schemas/trouble_ticket.py` — Create/Update/Response/List schemas
- `app/services/trouble_ticket_service.py` — CRUD + auto-context capture + sanitization
- `app/routers/trouble_tickets.py` — REST endpoints (require_user / require_admin)
- `app/static/tickets.js` — SPA views: submit form, my-tickets list, admin dashboard
- `alembic/versions/039_add_trouble_tickets.py` — Migration
- `tests/test_trouble_tickets.py` — Full test coverage

### Modified Files
- `app/templates/index.html` — Nav pills, script include
- `app/main.py` — Include trouble_tickets router

### Model: TroubleTicket
Fields: id, ticket_number (TT-YYYYMMDD-NNN), submitted_by (FK users), status (9 states), risk_tier (low/medium/high), category (5 types), title, description, current_page, user_agent, auto_captured_context (JSON), sanitized_context (JSON), diagnosis (JSON), generated_prompt (Text), file_mapping (JSON), fix_branch, fix_pr_url, iterations_used, cost_tokens, cost_usd, resolution_notes, parent_ticket_id (self-FK), timestamps.

### Auto-Context Capture
On submit, captures: recent API errors (stub for Sentry), frontend JS errors (from hidden form field), user role, server info, page route pattern.

### Sanitization
Strips: API keys, Bearer tokens, connection strings, passwords, secrets. Replaces emails with [EMAIL], IPs with [IP]. Truncates messages to 500 chars.

### Endpoints
- POST /api/trouble-tickets — create (any user)
- GET /api/trouble-tickets — list all (admin, with status filter + pagination)
- GET /api/trouble-tickets/{id} — single (admin or submitter)
- PATCH /api/trouble-tickets/{id} — update (admin)
- GET /api/trouble-tickets/my-tickets — current user's tickets
- POST /api/trouble-tickets/{id}/verify — user confirms fix or reports "still broken"

---

## Phase 2: AI Diagnosis + Risk Classification

### New Files
- `app/services/file_mapper.py` — Route-to-file mapping from router scan
- `app/services/diagnosis_service.py` — Two-stage AI diagnosis (classify → diagnose)
- `app/models/self_heal_log.py` — Append-only pattern tracking table
- `alembic/versions/040_add_self_heal_log.py`
- `tests/test_diagnosis_service.py`

### Two-Stage Diagnosis
1. **Classification** (fast): category, risk_tier, confidence via claude_structured
2. **Detailed diagnosis** (low/medium only): root_cause, affected_files, fix_approach, test_strategy

### Risk Overrides
- Confidence < 0.6 → bump risk tier up
- STABLE_FILES reference → force high
- REQUIRES_MIGRATION warning → force high
- Complex + low → bump to medium

---

## Phase 3: Prompt Templates + Notifications + Review Dashboard

### New Files
- `app/services/prompt_generator.py` — Category-specific prompt templates with base constraints
- `app/models/notification.py` — In-app notification model
- `app/services/notification_service.py` — Create/read/mark-read notifications
- `app/routers/notifications.py` — Notification REST endpoints
- `alembic/versions/041_add_notifications.py`
- `tests/test_prompt_generator.py`, `tests/test_notification_service.py`

### Prompt Templates
Base constraints (file allowlist, no destructive ops, must write tests) + category-specific rules (UI, API, data, performance). Each ends with `<promise>FIXED</promise>` or `<promise>ESCALATE</promise>`.

### Notifications
Bell icon (extends existing `notif-btn-global`), 30s polling, admin notifications for diagnosis/prompt/escalation events.

---

## Phase 4: Local Execution Pipeline (Pluggable Backend)

### New Files
- `app/services/execution_service.py` — Orchestration: approve → lock → run locally via subprocess → handle result
- `app/services/cost_controller.py` — Per-ticket ($2) and weekly ($50) budget caps
- `app/services/rollback_service.py` — Post-deploy health monitoring (alert-only, no auto-revert)
- `tests/test_execution_service.py`, `tests/test_rollback_service.py`

### Execution Model
Local subprocess (`claude -p`) on the server. Service has a pluggable `_run_fix()` method that can be swapped for GitHub Actions later. No `.github/workflows/` file in v1.

### Rollback Model
Alert-only. Checks Sentry for new errors post-deploy (stub initially). Notifies admin if issues detected. Does NOT auto-revert merge commits or rebuild containers — unsafe on single-server Docker Compose deployment.

### Config (added to Settings)
SELF_HEAL_ENABLED (False), SELF_HEAL_AUTO_DIAGNOSE (False), SELF_HEAL_AUTO_EXECUTE_LOW (False), SELF_HEAL_TICKET_BUDGET (2.00), SELF_HEAL_WEEKLY_BUDGET (50.00), SELF_HEAL_MAX_ITERATIONS_LOW (5), SELF_HEAL_MAX_ITERATIONS_MEDIUM (10).

---

## Phase 5: Weekly Reports + Pattern Tracker + Polish

### New Files
- `app/services/pattern_tracker.py` — Weekly stats, recurring patterns, risk tier recommendations
- `tests/test_pattern_tracker.py`, `tests/test_ticket_lifecycle.py`

### Enhancements
- Auto-close stale tickets (48h awaiting_verification, 7d undiagnosed)
- Common Issues quick-select on submit form
- Keyboard shortcuts on admin dashboard (a=approve, r=reject, n=next)
- System Health indicator (green/yellow/red based on ticket volume)
- Satisfaction survey on resolved tickets

### Modified Files
- `app/scheduler.py` — Weekly report + auto-close tasks

---

## Migration Order
039 → trouble_tickets table
040 → self_heal_log table
041 → notifications table

## Rollout Order
1. Ticket submission only (1-2 weeks)
2. Auto-diagnosis (SELF_HEAL_AUTO_DIAGNOSE=True)
3. Prompt generation (manual copy to Claude Code)
4. Low-risk auto-execution (SELF_HEAL_AUTO_EXECUTE_LOW=True)
5. Medium-risk approval workflow
6. Full autonomous operation (as confidence builds)
