# API Stability & Monitoring System — Design

**Date**: 2026-03-01
**Goal**: Make all API integrations visible, monitored, and impossible to silently fail.

## Problem

APIs go dead (expired keys, service changes, rate limits) and there's no awareness until a user search returns nothing. The system has good infrastructure (ApiSource model, circuit breaker, health endpoints) but lacks **proactive monitoring** and **in-your-face alerting**.

## Solution

Scheduled health checks every 15 minutes + a full API Health dashboard tab + a persistent warning banner across all pages when any API is down.

---

## 1. Backend: Scheduled Health Monitor

**New service**: `app/services/health_monitor.py`

Two check types:
- **Ping (every 15 min)**: Validates credentials are set and API endpoint accepts auth. Lightweight — no data returned.
- **Deep test (every 2 hours)**: Searches a known MPN ("LM317") end-to-end. Confirms full pipeline works.

Each check updates `ApiSource` fields: `status`, `last_success`, `last_error`, `last_error_at`, `error_count_24h`, `avg_response_ms`, `last_ping_at`, `last_deep_test_at`.

Scheduler jobs added to `app/scheduler.py`:
- `health_check_ping` — IntervalTrigger(minutes=15)
- `health_check_deep` — IntervalTrigger(hours=2)
- `reset_error_counts` — CronTrigger(hour=0, minute=0) (midnight UTC)
- `cleanup_usage_log` — CronTrigger(day=1) (monthly, delete >90 days)

## 2. Database: Usage Tracking (Migration 038)

**New table `api_usage_log`:**
- `id` (PK), `source_id` (FK → api_sources), `timestamp`, `endpoint`, `status_code`, `response_ms`, `success` (bool), `error_message`, `check_type` ("ping" | "deep" | "user_search")
- Index: `(source_id, timestamp)`

**New columns on `api_sources`:**
- `monthly_quota` (nullable int) — API's monthly call limit
- `calls_this_month` (int, default 0) — rolling count, reset on 1st
- `last_ping_at` (datetime) — last health check timestamp
- `last_deep_test_at` (datetime) — last deep test timestamp

## 3. Backend: Endpoints

**`GET /api/system/alerts`** (no admin required — all users see banner):
```json
{
  "alerts": [
    {
      "source_name": "digikey",
      "display_name": "DigiKey",
      "status": "error",
      "last_error": "401 Unauthorized",
      "since": "2026-03-01T04:55:00Z"
    }
  ],
  "count": 1
}
```

**`GET /api/admin/api-health/dashboard`** (admin only):
- All sources with full stats, usage data, last 24h error rate, response time trends.

## 4. Frontend: Persistent Warning Banner

- Fixed below header bar on ALL pages
- Polled via `/api/system/alerts` every 60 seconds
- Red if any source is `error`, amber if only `degraded`
- Shows: "**2 APIs need attention** — DigiKey (401), TME (missing creds)" + link to API Health tab
- Dismissible per-session (reappears if new alerts arrive)

## 5. Frontend: API Health Dashboard Tab

New sidebar nav item in Operations section. Badge shows alert count.

**Three sections:**

**a) Status Grid** — Card per active API:
- Green/amber/red dot + status
- Display name + category
- Last successful call ("2 min ago")
- Last error (if any)
- Response time (avg ms)
- "Test Now" button

**b) Usage Overview** — Progress bars for APIs with quotas:
- `calls_this_month / monthly_quota`
- Warning at >80% usage

**c) Health Timeline** — Recent check log:
- Timestamp, source, pass/fail, response time
- Filterable, last 24h default

## 6. Enhanced Settings > Sources Panel

Add to each existing source card:
- "Last Checked" timestamp
- Response time indicator
- Usage count (e.g., "450 / 500 calls this month")
- Mini health history (last 5 results as green/red dots)

## 7. Error Handling

- Health checks use dedicated httpx client (10s ping timeout, 30s deep timeout)
- No-creds → `disabled` status, not `error`
- Respects circuit breaker — if open, records open state without hitting API
- Deep tests use MPN "LM317" (universally available part)
- Usage log auto-cleaned: entries >90 days deleted monthly

## 8. Testing

- Unit tests for `health_monitor.py` with mocked connectors
- `/api/system/alerts` endpoint tests with various source states
- Dashboard endpoint tests
- Scheduler job registration tests
- 100% coverage maintained
