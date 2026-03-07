# Teams Q&A Routing + Daily Digest — Phase 2 Design

**Date**: 2026-03-07
**Status**: Approved
**Parent**: `docs/plans/2026-03-07-ai-intelligence-layer-design.md`

## Goal

Add Teams-based Q&A answering (Adaptive Cards in DM), batched question delivery to prevent buyer overload, daily question caps for sales, 4h nudge for unanswered questions, and a daily knowledge digest.

## Decisions

| Decision | Choice |
|----------|--------|
| Answer mechanism | Adaptive Card in Teams DM with inline text input + Answer button |
| Question delivery | Batched twice daily (digest hour and digest hour + 6), NOT real-time |
| Buyer overload protection | Batch delivery — one card with all pending questions, answer in one pass |
| Sales throttling | Admin-configurable daily question cap (default 10) with UI quota display |
| Nudge behavior | Single 4h nudge with urgency styling in next batch, no escalation chain |
| Digest content | Q&A focused only — unanswered questions assigned to you, answers to your questions |
| Digest timing | Configurable per-user via `knowledge_digest_hour` on TeamsAlertConfig |
| Digest opt-in/out | No toggle — everyone with Teams configured gets it |
| Morning briefing | NOT modified — Knowledge Digest is a separate DM |
| Answer notifications | Reuse existing `post_answer()` flow — no special Teams badge |

## Data Model Changes (migration 064)

### Modify `teams_alert_config`

| Column | Type | Notes |
|--------|------|-------|
| `knowledge_digest_hour` | INTEGER DEFAULT 14 | UTC hour 0-23 for digest + first question batch |

### Modify `knowledge_entries`

| Column | Type | Notes |
|--------|------|-------|
| `nudged_at` | TIMESTAMP NULL | When 4h nudge was sent (prevents duplicates) |
| `delivered_at` | TIMESTAMP NULL | When question was included in a batch card |

### New table: `knowledge_config`

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | |
| `key` | VARCHAR(50) UNIQUE NOT NULL | Config key |
| `value` | VARCHAR(255) NOT NULL | Config value |

Seeded with: `daily_question_cap` = `10`

## Batch Delivery Logic

Questions are NOT sent individually. They accumulate and deliver twice daily:

1. **First batch**: At buyer's `knowledge_digest_hour` (default 14:00 UTC)
2. **Second batch**: At `knowledge_digest_hour + 6` (default 20:00 UTC)

Each batch:
- Gathers all undelivered questions assigned to this buyer (`delivered_at IS NULL`, `is_resolved = false`)
- Includes questions >4h old with nudge styling ("Awaiting response")
- Builds ONE Adaptive Card with all questions listed, each with a text input
- Buyer answers what they can, skips what they can't
- On submission, each filled-in answer calls `post_answer()`
- Updates `delivered_at` on all included questions
- Questions >4h old also get `nudged_at` updated

Empty batches (no pending questions) are skipped — no empty DMs.

## Daily Question Cap

- Sales people limited to N questions per day (resets at midnight UTC)
- Default: 10 questions/day
- Admin-configurable via `knowledge_config` table
- `post_question()` checks cap before creating; returns 429 if exceeded
- Frontend shows quota in question modal: "7/10 questions remaining today"
- At cap: submit button disabled with message

## Daily Knowledge Digest

Separate DM from question batches. Sent at buyer's `knowledge_digest_hour`. Contains:

- Unanswered questions assigned to you (count + list)
- Questions you asked that got answered in the last 24h (content + answer)

No empty digests — skip users with nothing to report.

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/teams-bot/card-action` | Adaptive Card submission (HMAC validated) |
| GET | `/api/knowledge/quota` | Returns `{used, limit, remaining}` for current user today |
| PUT | `/api/admin/knowledge-config` | Update config values (admin only) |

## Service: `app/services/teams_qa_service.py`

- `send_question_batch(db, user_id)` — build + send batch Adaptive Card, mark `delivered_at`
- `send_nudge_batch(db, user_id)` — same but for >4h questions, adds urgency banner
- `send_knowledge_digest(db, user_id)` — digest card with recently-answered questions
- `handle_card_answer(db, payload)` — parse card action, call `post_answer()` per answer
- `check_question_cap(db, user_id)` — returns whether user can still ask today

## Modified Service

`knowledge_service.post_question()` — add cap check before creating entry. If over cap, raise HTTP 429.

## Background Jobs

| Job | Schedule | Purpose |
|-----|----------|---------|
| `deliver_question_batches` | Every 1h | Match users whose digest_hour or digest_hour+6 = current UTC hour, send batch |
| `send_knowledge_digests` | Every 1h | Match users whose digest_hour = current UTC hour, send digest |

Nudging is integrated into batch delivery — questions >4h old get urgency styling automatically.

## Frontend Changes

- Question modal: quota display "7/10 questions remaining today"
- At cap: submit button disabled with limit message
- Admin settings: field to adjust daily question cap

## Files to Create/Modify

### New files
- `app/services/teams_qa_service.py` — batch delivery, card handling, digest, cap check
- `alembic/versions/064_teams_qa_routing.py` — migration

### Modified files
- `app/models/knowledge.py` — add `nudged_at`, `delivered_at` columns
- `app/services/knowledge_service.py` — add cap check to `post_question()`
- `app/routers/knowledge.py` — add quota endpoint
- `app/routers/teams_bot.py` — add card-action endpoint
- `app/jobs/knowledge_jobs.py` — add batch delivery + digest jobs
- `app/jobs/__init__.py` — register new jobs
- `app/static/app.js` — quota display in question modal
- `app/templates/index.html` — admin config UI for question cap (if admin panel exists)

## Future Phases (not in scope)

- Escalation ladder (4h/8h/24h nudge-to-manager chain)
- Response rate as Avail Score factor
- Urgent question bypass (skip batching)
