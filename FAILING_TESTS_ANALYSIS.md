# Failing Test Analysis Report

## Root Cause Summary

The primary setting `mvp_mode: bool = True` in `app/config.py:235` causes the following routers
to be excluded from the FastAPI app at startup (`app/main.py:674-680`):

```python
if not settings.mvp_mode:
    app.include_router(apollo_sync_router)
    app.include_router(dashboard_router)       # /api/dashboard/*
    app.include_router(enrichment_router)      # /api/enrichment/*
    app.include_router(performance_router)     # /api/performance/*
    app.include_router(teams_actions_router)
    app.include_router(teams_alerts_router)
```

Since tests don't override `MVP_MODE=false` in their environment, all HTTP requests to
`/api/dashboard/*`, `/api/enrichment/*`, and `/api/performance/*` return **404 Not Found**.

No test in `conftest.py` sets `os.environ["MVP_MODE"] = "false"` — it only sets `TESTING=1`,
`RATE_LIMIT_ENABLED=false`, `DATABASE_URL=sqlite://`, and `REDIS_URL=""`.

---

## Per-File Analysis

### 1. tests/test_buyer_dashboard.py

| Item | Detail |
|------|--------|
| **Endpoint** | `GET /api/dashboard/buyer-brief` |
| **Expected keys** | `kpis` (sourcing_ratio, offer_quote_rate, quote_win_rate, buyplan_po_rate, total_reqs, sourced_reqs, total_offers, total_buyplans), `team_kpis`, `new_requirements`, `offers_to_review`, `reqs_at_risk`, `quotes_due_soon`, `pipeline` (active_reqs), `revenue_profit`, `buyplans_pending` |
| **Also tests** | `_age_label` helper imported directly from `app.routers.dashboard` |
| **Failing tests** | ALL 16 tests in `TestBuyerBrief` (404). `TestAgeLabel` (3 tests) pass because they import the function directly, not via HTTP. |
| **Failure reason** | **ONLY mvp_mode=True (404)**. The dashboard router is not registered. |
| **Fix needed** | Set `MVP_MODE=false` in test env, OR selectively register the dashboard router in test mode. |

---

### 2. tests/test_dashboard_attention_feed.py

| Item | Detail |
|------|--------|
| **Endpoints** | `GET /api/dashboard/attention-feed`, `GET /api/dashboard/team-leaderboard` |
| **Expected response** | attention-feed: list of items with `type`, `urgency`, `title`, `detail`, `link_type`, `link_id`. team-leaderboard: `{"entries": [...]}` where each entry has `user_role`, `user_name`. |
| **Failing tests** | ALL 10 tests in `TestAttentionFeed` + ALL 3 tests in `TestTeamLeaderboardUserRole` (404) |
| **Failure reason** | **ONLY mvp_mode=True (404)** |
| **Fix needed** | Set `MVP_MODE=false` in test env. |

---

### 3. tests/test_dashboard_kpi_all_statuses.py

| Item | Detail |
|------|--------|
| **Endpoint** | `GET /api/dashboard/buyer-brief` |
| **Expected keys** | Same as test_buyer_dashboard.py — `kpis` (total_reqs, sourced_reqs, sourcing_ratio, total_offers, total_buyplans, buyplan_po_rate), `reqs_at_risk`, `quotes_due_soon` |
| **Failing tests** | ALL 5 test classes (404) |
| **Failure reason** | **ONLY mvp_mode=True (404)** |
| **Fix needed** | Set `MVP_MODE=false` in test env. |

---

### 4. tests/test_dashboard_morning_brief.py

| Item | Detail |
|------|--------|
| **Endpoint** | `GET /api/dashboard/morning-brief` |
| **Expected keys** | `text`, `stats` (stale_accounts, quotes_awaiting, new_proactive_matches, won_this_week, lost_this_week), `generated_at` |
| **Failing tests** | ALL 4 tests (404) |
| **Failure reason** | **ONLY mvp_mode=True (404)** |
| **Fix needed** | Set `MVP_MODE=false` in test env. |

---

### 5. tests/test_dashboard_needs_attention.py

| Item | Detail |
|------|--------|
| **Endpoints** | `GET /api/dashboard/needs-attention`, `GET /api/dashboard/hot-offers` |
| **Expected response** | needs-attention: list with `company_name`, `days_since_contact`, `is_strategic`, `open_req_count`, `open_quote_value`. hot-offers: list with `vendor_name`, `mpn`, `age_label`, `unit_price`. |
| **Failing tests** | ALL 12 tests in `TestNeedsAttention` + ALL 6 tests in `TestHotOffers` (404) |
| **Failure reason** | **ONLY mvp_mode=True (404)** |
| **Fix needed** | Set `MVP_MODE=false` in test env. |

---

### 6. tests/test_coverage_100.py — failing test only

| Item | Detail |
|------|--------|
| **Failing test** | `TestDashboardBuyPlanImportError::test_attention_feed_without_buy_plan` (line 230) |
| **Endpoint** | `GET /api/dashboard/attention-feed` |
| **What it tests** | That attention-feed handles ImportError when `app.models.buy_plan` is missing |
| **Failure reason** | **ONLY mvp_mode=True (404)** — the dashboard router is not registered |
| **Other tests** | The other 7 tests in this file (vendor fuzzy fallback, search service, offers, quotes, NC enqueue, avail score, company dedup, prospecting) all PASS. |
| **Fix needed** | Set `MVP_MODE=false` in test env. |

---

### 7. tests/test_coverage_gaps.py — enrichment backfill tests

| Item | Detail |
|------|--------|
| **Failing tests** | ALL 22 tests in `TestEnrichmentBackfillEmails` + `TestEnrichmentDeepScan` (lines 384-791) |
| **Endpoints** | `POST /api/enrichment/backfill-emails`, `POST /api/enrichment/deep-email-scan/{user_id}` |
| **Failure reason** | **ONLY mvp_mode=True (404)** — the enrichment router is not registered |
| **Other tests** | Admin, CRM, requisition tests in the same file all PASS. |
| **Fix needed** | Set `MVP_MODE=false` in test env. |

---

### 8. tests/test_coverage_gaps_final.py — failing tests

| Item | Detail |
|------|--------|
| **Failing tests** | 6 tests: `TestDashboardInvalidDeadline` (1), `TestDashboardBuyPlanAttentionFeed` (3), `TestBuyerBriefBuyPlanNames` (1), `TestBuyerBriefPendingBPNames` (1) |
| **Endpoints** | `GET /api/dashboard/attention-feed`, `GET /api/dashboard/buyer-brief` |
| **Failure reason** | **ONLY mvp_mode=True (404)** — all hit dashboard endpoints |
| **Other tests** | Company, quote, offer, requisition tests in the same file all PASS. |
| **Fix needed** | Set `MVP_MODE=false` in test env. |

---

### 9. tests/test_coverage_quick_wins.py — failing test

| Item | Detail |
|------|--------|
| **Failing test** | `TestDashboardTimezoneAware::test_needs_attention_with_tz_aware_last_at` (line 251) |
| **Endpoint** | `GET /api/dashboard/needs-attention?days=7` |
| **Failure reason** | **ONLY mvp_mode=True (404)** |
| **Other tests** | Admin vendor merge, buy plan, site unassign, claim site, prospecting assign, vendor tag filter, substitutes, search cache, customer analysis, deep enrichment, ownership, vite URL tests all PASS. |
| **Fix needed** | Set `MVP_MODE=false` in test env. |

---

### 10. tests/test_coverage_remaining.py — performance router tests

| Item | Detail |
|------|--------|
| **Failing tests** | ALL 16 tests in `TestPerformanceRouter` |
| **Endpoints** | `GET /api/performance/avail-scores`, `POST /api/performance/avail-scores/refresh`, `GET /api/performance/multiplier-scores`, `POST /api/performance/multiplier-scores/refresh`, `GET /api/performance/bonus-winners` |
| **Failure reason** | **ONLY mvp_mode=True (404)** — the performance router is not registered |
| **Other tests** | Scheduler, vendor material CRUD, cache flush tests all PASS. |
| **Fix needed** | Set `MVP_MODE=false` in test env. |

---

### 11. tests/test_coverage_routers_final.py — failing tests

| Item | Detail |
|------|--------|
| **Failing tests** | `test_backfill_emails_skips_empty_vendor_name` (line 657), `test_backfill_emails_skips_vendor_name_normalizes_to_empty` (line 852) |
| **Endpoint** | `POST /api/enrichment/backfill-emails` |
| **Failure reason** | **ONLY mvp_mode=True (404)** — the enrichment router is not registered |
| **Other tests** | 21 other tests in the file (clone, requirements, sightings, stock import, vendor search, quote preview, RFQ prepare, etc.) all PASS. |
| **Fix needed** | Set `MVP_MODE=false` in test env. |

---

### 12. tests/test_coverage_final_gaps.py — logging config test

| Item | Detail |
|------|--------|
| **Failing test** | `TestLoggingConfigJsonStdout::test_production_json_logging` (line 670) |
| **What it tests** | `setup_logging()` with `APP_URL=https://app.availai.net` and `EXTRA_LOGS=1` (production mode) |
| **Failure reason** | **NOT mvp_mode related.** This fails because `setup_logging()` in production mode tries to create a file log handler at `/var/log/avail/` and gets `PermissionError: [Errno 13] Permission denied: '/var/log/avail'` |
| **Fix needed** | Mock the file handler creation, or create the directory before test, or patch `logger.add` to prevent the file sink from being created. The test needs to either (a) mock `logger.add` for the file path, or (b) use a temp directory for log output. |

---

## Summary Table

| # | File | Failing Tests | Root Cause | Schema Change? |
|---|------|--------------|------------|----------------|
| 1 | test_buyer_dashboard.py | 16 of 19 | mvp_mode=True (404) | No |
| 2 | test_dashboard_attention_feed.py | 13 of 13 | mvp_mode=True (404) | No |
| 3 | test_dashboard_kpi_all_statuses.py | 5 of 5 | mvp_mode=True (404) | No |
| 4 | test_dashboard_morning_brief.py | 4 of 4 | mvp_mode=True (404) | No |
| 5 | test_dashboard_needs_attention.py | 18 of 18 | mvp_mode=True (404) | No |
| 6 | test_coverage_100.py | 1 of 16 | mvp_mode=True (404) | No |
| 7 | test_coverage_gaps.py | 22 of ~40 | mvp_mode=True (404) | No |
| 8 | test_coverage_gaps_final.py | 6 of ~14 | mvp_mode=True (404) | No |
| 9 | test_coverage_quick_wins.py | 1 of ~16 | mvp_mode=True (404) | No |
| 10 | test_coverage_remaining.py | 16 of ~30 | mvp_mode=True (404) | No |
| 11 | test_coverage_routers_final.py | 2 of 23 | mvp_mode=True (404) | No |
| 12 | test_coverage_final_gaps.py | 1 of ~20 | PermissionError on /var/log/avail | No |

**Total failures: ~105 tests**
- **~104 caused by mvp_mode=True** (dashboard: ~64, enrichment: ~24, performance: ~16)
- **1 caused by PermissionError** on log directory creation

## Recommended Fix

### For all mvp_mode failures (104 tests):

Add one line to `tests/conftest.py` before any app imports:

```python
os.environ["MVP_MODE"] = "false"
```

This will cause `settings.mvp_mode` to be `False`, which registers all routers including
dashboard, enrichment, performance, teams_actions, teams_alerts, and apollo_sync.

**No response schema changes are needed** — the tests' expected response shapes match what
the actual router endpoints return. The only issue is that the routes are not registered.

### For the logging config test (1 test):

In `test_coverage_final_gaps.py::TestLoggingConfigJsonStdout::test_production_json_logging`,
patch the file logger to avoid writing to `/var/log/avail`. For example:

```python
with patch.dict(os.environ, {"APP_URL": "https://app.availai.net", "EXTRA_LOGS": "1", "LOG_LEVEL": "INFO"}):
    with patch("loguru.logger.add") as mock_add:
        setup_logging()
```

Or mock the path to use a temp directory.
