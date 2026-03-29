# CRM Phase 2a: Interaction Data Gap Fixes + Teams Message Tracking

**Created:** 2026-03-29
**Status:** Approved
**Parent:** [CRM Master Roadmap](2026-03-29-crm-master-roadmap.md)

## Goal

Close data collection gaps so all customer/vendor interactions flow into ActivityLog with `company_id` set, and add real-time Teams message tracking via Graph webhooks. This ensures the staleness indicators from Phase 1 reflect all interaction types and prepares the data foundation for AI quality scoring in Phase 2b.

## Part 1: Fix company_id Gaps

### Problem

Three code paths write ActivityLog entries with `company_id = NULL`:

1. **`log_rfq_activity()`** in `app/services/activity_service.py:657` — logs RFQ sends and status changes with only `requisition_id` set
2. **Quote outcome logging** in `app/routers/crm/quotes.py:453` — logs quote won/lost with only `requisition_id` and `quote_id`
3. **Proactive match logging** in `app/services/proactive_matching.py:292` — logs matches with only `requisition_id`

This means the staleness indicators from Phase 1 (which use `Company.last_activity_at`) don't reflect RFQ, quote, or proactive activity.

### Fix

For each path, resolve `company_id` before writing the ActivityLog entry:

**Path 1 — `log_rfq_activity()`:**
- Look up: `Requisition → customer_site_id → CustomerSite → company_id`
- Guard: `customer_site_id` is nullable, so check at each step
- After writing ActivityLog, call `_update_last_activity()` if `company_id` is not None

**Path 2 — Quote outcome logging:**
- The `req` object is already in scope at line 453
- Look up: `db.get(CustomerSite, req.customer_site_id)` → `site.company_id`
- Same pattern as the `_record_quote_won_history()` helper at line 621

**Path 3 — Proactive match logging:**
- `cph.company_id` is already resolved and in scope at line 277
- Simply add `company_id=cph.company_id` to the ActivityLog constructor
- Call `_update_last_activity()` after

### No Backfill Needed

Database is empty — no historical NULL entries to fix.

## Part 2: Teams Message Tracking via Webhooks

### Architecture: Per-User Delegated Subscriptions

Use the same pattern as existing mail webhooks — per-user delegated token subscriptions to `/me/chats/getAllMessages`. This avoids the complexity of app-only tokens and admin consent for `ChatMessage.Read.All`.

The existing `Chat.ReadWrite` delegated scope (already in `GRAPH_SCOPES`) is sufficient.

### Subscription Management

**New function in `webhook_service.py`:** `create_teams_subscription(user, db)`
- Resource: `/me/chats/getAllMessages`
- Change type: `created`
- Notification URL: `{app_url}/api/webhooks/teams`
- Client state: `secrets.token_hex(16)` stored in existing `GraphSubscription` table
- Lifetime: 70 hours (same as mail, within Graph's 4230-minute max for chat subscriptions)

**Extend `ensure_all_users_subscribed()`** to also call `create_teams_subscription()` for each connected user who doesn't have one. Gate on `not settings.mvp_mode`.

**No new renewal job needed** — existing `_job_webhook_subscriptions` in `core_jobs.py:307` already renews all `GraphSubscription` records generically.

### Webhook Handler

**Extend `app/routers/v13_features/activity.py`** with new route:

```
POST /api/webhooks/teams
```

- Rate limit: `600/minute` (Microsoft sends from limited datacenter IPs)
- Validation: same HMAC `clientState` pattern as mail webhooks
- MVP mode gate: return 404 if `settings.mvp_mode`

**Processing flow on each notification:**

1. Extract `resource` URL (e.g., `/chats/{chatId}/messages/{messageId}`)
2. Look up `GraphSubscription` by `subscription_id` to get the owning user
3. Fetch full message via user's delegated token: `GET /{resource}?$select=id,body,from,createdDateTime,chatId`
4. Resolve sender: `from.user.id` is an Azure AD GUID, not an email. Call `GET /users/{userId}?$select=mail,userPrincipalName` to get the email. Cache userId→email mappings within the request scope.
5. Determine direction: if sender email matches a connected user in the DB → `outbound` (our team sent it), otherwise → `inbound`
6. Match sender to CRM entity via existing `match_email_to_entity(sender_email, db)`
7. Dedup via `external_id` = Teams message ID
8. Create ActivityLog:
   - `activity_type`: `"teams_message"` (13 chars, fits String(20))
   - `channel`: `"teams"`
   - `event_type`: `"message"`
   - `direction`: `"inbound"` or `"outbound"`
   - `external_id`: Teams message ID
   - `auto_logged`: `True`
   - `subject`: chat topic or first 100 chars of message body
   - `contact_email`, `contact_name`: from resolved sender
   - `company_id`, `vendor_card_id`: from entity match
9. Call `_update_last_activity()` if company/vendor matched

### Validation

**New function:** `validate_teams_notification(payload, db)` — structurally identical to existing `validate_notifications()` but filtered to Teams subscriptions (resource containing "chats"). Can share implementation with a resource filter parameter.

### Azure Portal Step

**Prerequisite (manual):** In Azure AD app registration, ensure `Chat.ReadWrite` delegated permission is consented. This is already in `GRAPH_SCOPES` and should already be consented from initial app setup. No new permissions needed since we're using delegated (per-user) subscriptions.

## Technical Architecture

### New Files

| File | Responsibility |
|------|---------------|
| `tests/test_teams_tracking.py` | Tests for Teams webhook handler, subscription creation, dedup |

### Modified Files

| File | Change |
|------|--------|
| `app/services/activity_service.py:657` | Fix `log_rfq_activity()` — add requisition→site→company_id lookup + `_update_last_activity()` |
| `app/routers/crm/quotes.py:453` | Fix quote outcome logging — add site lookup, set `company_id` |
| `app/services/proactive_matching.py:292` | Fix proactive logging — add `company_id=cph.company_id` |
| `app/services/webhook_service.py` | Add `create_teams_subscription()`, `validate_teams_notification()`, `handle_teams_notification()` |
| `app/services/webhook_service.py` | Extend `ensure_all_users_subscribed()` for Teams subscriptions |
| `app/routers/v13_features/activity.py` | Add `POST /api/webhooks/teams` handler |
| `app/main.py:258-272` | Add `/api/webhooks/teams` to CSRF exempt list |

### No New Models or Migrations

- `GraphSubscription.resource` already supports arbitrary resource strings
- `ActivityLog` already has `channel`, `event_type`, `direction`, `external_id`
- `activity_type` String(20) fits `"teams_message"` (13 chars)

### Build Sequence

**Part 1 first (zero risk, no external dependencies):**
1. Fix `log_rfq_activity()` company_id resolution
2. Fix quote outcome company_id
3. Fix proactive match company_id
4. Tests + commit

**Part 2 second (external dependency: Graph API):**
1. Add Teams subscription creation to webhook_service.py
2. Add Teams webhook handler to activity.py
3. Add CSRF exempt + rate limit config
4. Extend ensure_all_users_subscribed() with MVP gate
5. Tests + commit

## What This Does NOT Include

- App-only (client credentials) token flow — not needed with per-user delegated approach
- New scheduler jobs — existing renewal job handles Teams subscriptions
- AI quality scoring of interactions — Phase 2b
- Performance dashboard — Phase 4
- Teams channel monitoring (only 1:1 chats tracked)
- Call transcription or content analysis
