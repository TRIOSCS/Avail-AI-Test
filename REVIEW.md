# AVAIL v1.3.0 — Full Software Review

## Executive Summary

AVAIL is a **FastAPI monolith** (~13,400 LOC application code, 39 Python files) that serves as an AI-native electronic component sourcing platform. It aggregates vendor inventory from 7 API connectors, manages customer/vendor CRM relationships, automates RFQ outreach via Microsoft 365, and applies AI-powered intelligence for parsing, scoring, and routing.

**Overall assessment:** The business logic is well-designed and the domain modeling is strong. The codebase has grown organically through several feature phases and now carries structural debt that should be addressed before the next major feature push. Three runtime-crashing bugs were found and fixed during this review.

---

## Architecture At A Glance

```
┌─────────────────────────────────────────────────────────────┐
│  FastAPI (main.py — 4,430 lines, 143 functions, 80 routes) │
├──────────┬──────────┬───────────┬──────────┬────────────────┤
│ Search   │ Email    │ CRM       │ Intel    │ v1.3.0         │
│ Service  │ Service  │ (Company, │ Layer    │ Activity/      │
│ (7 APIs) │ (Graph)  │  Vendor,  │ (Claude, │ Ownership/     │
│          │          │  Offers,  │  Apollo, │ Routing        │
│          │          │  Quotes)  │  Cache)  │                │
├──────────┴──────────┴───────────┴──────────┴────────────────┤
│  PostgreSQL  │  Acctivate (SQL Server, read-only)           │
└──────────────┴──────────────────────────────────────────────┘
```

| Metric | Value |
|--------|-------|
| Application Python LOC | 13,387 |
| Test LOC | 4,977 |
| Migration SQL LOC | 431 |
| Python files (app/) | 39 |
| API routes | 80 |
| Database models | 24 |
| Test assertions | 704 (all passing) |
| External integrations | 7 search APIs, Microsoft Graph, Anthropic Claude, Apollo, Clay, Explorium, Acctivate |

---

## Bugs Found & Fixed During Review

### Critical (Runtime Crashers)

**1. `vendor.country` → AttributeError in routing_service.py**
- `_score_geography()` referenced `vendor.country` but VendorCard's column is `hq_country`
- Would crash with `AttributeError` on every routing score calculation involving geography
- Tests passed because MagicMock auto-creates missing attributes as truthy objects
- **Fixed:** Changed to `vendor.hq_country` in routing_service.py + all test mocks

**2. `vendor.company_name` → AttributeError in routing_service.py**
- `notify_routing_assignment()` referenced `vendor.company_name` but the column is `display_name`
- Would crash every time a routing notification email was sent
- **Fixed:** Changed to `vendor.display_name` in routing_service.py + smoke test

### Previously Fixed (This Session)

**3. `normalize_packaging("Tray")` → "reel" instead of "tray"**
- Dictionary iteration order bug: `"tr"` matched before `"tray"` in `_PACKAGING_MAP`
- **Fixed:** Reordered dictionary entries, longer patterns first

---

## What's Working Well

**Domain modeling is strong.** The Requisition → Requirement → Sighting → Offer → Quote pipeline accurately maps the electronic component sourcing workflow. VendorCards accumulate intelligence from multiple sources (API sightings, email mining, Acctivate sync, engagement scoring) into a single unified profile.

**The v1.3.0 feature design is thoughtful.** Activity logging is genuinely zero-manual-entry (auto-captures from Graph webhooks). The 30-day inactivity ownership rule with day-23 warnings solves a real sales ops problem. The buyer routing scoring algorithm (brand 40pts + commodity 25pts + geography 15pts + relationship 20pts) encodes real procurement domain expertise.

**Defensive coding patterns.** Every external API call uses retry-with-backoff via GraphClient. Token refresh happens automatically before Graph calls. All migrations use IF NOT EXISTS guards. Configuration is 100% environment variables with sensible defaults.

**No obvious security issues.** Zero hardcoded secrets, no bare excepts, no print statements, no TODO/FIXME debris. Admin-only routes are gated. Graph webhook validation uses client_state tokens.

**Test coverage is comprehensive** for logic that is tested. 704 assertions across scoring, normalization, activity matching, ownership rules, routing algorithms, and cross-module integration.

---

## Structural Issues (Technical Debt)

### 1. God File: main.py (4,430 lines)

This is the single biggest maintainability risk. `main.py` contains all 80 route handlers, 51 inline DDL migration statements in the startup block, seed data for API sources, and helper functions that should live in service modules. At 4,430 lines it's difficult to navigate, review, or refactor safely.

**Recommendation:** Split into a router-per-domain structure:

```
app/
  routers/
    requisitions.py    # ~300 lines
    vendors.py         # ~400 lines  
    email_mining.py    # ~200 lines
    crm.py             # ~400 lines (companies, sites, offers, quotes)
    intelligence.py    # ~300 lines (AI features)
    admin.py           # ~200 lines (sync, sources)
    auth.py            # ~100 lines
    routing.py         # ~200 lines (v1.3.0)
    ownership.py       # ~150 lines (v1.3.0)
  startup.py           # DDL migrations + seed data
  main.py              # ~50 lines (app factory + include_router calls)
```

### 2. Inline DDL in Application Startup

The startup block has 51 `ALTER TABLE` / `CREATE TABLE` / `CREATE INDEX` statements that run every time the app boots. These duplicate what the migration files already do, and the silent exception handlers (11 of them) swallow errors without logging, making it impossible to diagnose schema issues.

**Recommendation:** Remove the inline DDL entirely. Rely on the migration files (003–008) which are already idempotent. Run migrations as a deployment step, not on every app boot.

### 3. Test Methodology Gap: MagicMock Attribute Masking

The two `vendor.country` / `vendor.company_name` bugs went undetected because MagicMock auto-creates any attribute you access. When tests set `mock_vendor.country = "US"`, the test passes even though the real VendorCard model has no `.country` attribute.

**Recommendation:** For model-touching tests, use lightweight dataclass stubs or `SimpleNamespace` with explicit attributes instead of MagicMock. This way, accessing a nonexistent attribute would raise AttributeError, catching these bugs at test time.

```python
# Instead of MagicMock:
from types import SimpleNamespace
vendor = SimpleNamespace(hq_country="US", display_name="Test Vendor", ...)
```

### 4. No Async Database Sessions

The app uses synchronous SQLAlchemy sessions (`Session`) everywhere, including inside `async def` route handlers and service functions. This means every database query blocks the event loop. With multiple concurrent users this will become a bottleneck.

**Recommendation:** Migrate to `sqlalchemy.ext.asyncio.AsyncSession` with `asyncpg`. This is a significant lift but will matter as user count grows.

### 5. No Request Validation Layer

Route handlers do inline validation (checking for missing fields, type coercion) rather than using Pydantic request/response models. This means error messages are inconsistent and the API isn't self-documenting.

**Recommendation:** Add Pydantic `BaseModel` schemas for request bodies and response types on the most-used endpoints. FastAPI auto-generates OpenAPI docs from these.

---

## Module-by-Module Assessment

### Core Search Pipeline ✅
`search_service.py` (397 lines) + 7 connectors + `scoring.py` (65 lines)

Well-structured. BaseConnector pattern with retry. Scoring formula is transparent and configurable via env vars. MaterialCard upsert creates a durable "who sells what" memory. The connector architecture makes adding new sources straightforward.

### Email Service ✅
`email_service.py` (555 lines) + `graph_client.py` (163 lines)

Solid Graph API integration. Retry with exponential backoff, immutable IDs (H1), delta query support (H8), auto-pagination. The `[AVAIL-{req_id}]` subject token for reply matching is pragmatic.

### Email Mining Pipeline ✅
`email_mining.py` (547 lines) + `response_parser.py` (263 lines) + `attachment_parser.py` (377 lines)

Impressive three-tier parsing: message classification, structured quote extraction with confidence thresholds (auto-apply ≥0.8, flag 0.5–0.8, skip <0.5), and AI-powered attachment column detection with vendor-domain caching. The engagement scorer (377 lines) produces a useful 0–100 composite score.

### CRM Layer ✅
Companies, CustomerSites, Offers, Quotes — clean parent-child hierarchy. Quote builder with line items, margin calculation, and win/loss tracking. The Acctivate sync (274 lines) is clean — read-only, daily, under 200 lines of logic as designed.

### Intelligence Layer ✅
`ai_service.py` (266 lines) + `claude_client.py` (255 lines) + `apollo_client.py` (178 lines) + `intel_cache.py` (96 lines)

4 features (contact enrichment, reply parsing, company intel, smart RFQ drafts) with 7-day TTL caching. Claude calls use structured output. Apollo fallback to web search. The "AI fails gracefully" design rule is consistently followed.

### v1.3.0: Activity Service ✅
`activity_service.py` (273 lines)

Clean contact-matching waterfall: exact email → vendor contacts → company domain → vendor domain. Phone matching by normalized suffix. Proper dedup on external_id. Auto-updates `last_activity_at` on matched entities. Generic domain exclusion list.

### v1.3.0: Ownership Service ✅
`ownership_service.py` (447 lines)

The 30/90-day inactivity sweep with day-23 warnings, open pool claiming, and manager digest is well-implemented. Warning dedup prevents spam. `get_accounts_at_risk()` sorts by urgency. Strategic accounts get the longer 90-day window.

### v1.3.0: Routing Service ⚠️
`routing_service.py` (606 lines)

Good algorithm design (4-factor scoring, 48-hour waterfall, collision detection). The two attribute bugs are now fixed. The `_BRAND_COMMODITY_MAP` and `_COUNTRY_REGION_MAP` are hardcoded — consider making these configurable or database-driven as the team grows.

### v1.3.0: Webhook Service ✅
`webhook_service.py` (281 lines)

Subscription lifecycle management (create, renew before expiry, ensure all users subscribed). Client state validation on incoming notifications. Proper Graph message fetch + auto-logging via activity_service.

### Scheduler ✅
`scheduler.py` (669 lines)

5-minute tick loop running: token refresh (30min), inbox scan (30min), contacts sync (24h), engagement scoring (12h), webhook renewals (every tick), ownership sweep (12h), routing/offer expiration (every tick). Each task has independent error handling. Module-level `_last_ownership_sweep` timestamp is simple but adequate.

---

## Configuration Review

The `config.py` (89 lines) is well-organized. All settings from env vars with reasonable defaults. The v1.3.0 tuning knobs (`customer_inactivity_days`, `routing_window_hours`, `collision_lookback_days`, etc.) give operational flexibility without code changes.

One note: `ai_features_enabled` supports `"mike_only"` as a value, which is fine for now but should become role-based before adding more users.

---

## Migration Chain

```
003_enrichment_fields.sql      → Company/VendorCard enrichment columns
004_acctivate_sync.sql         → Acctivate behavioral fields + inventory tables
005_email_pipeline_v2.sql      → Email mining infrastructure (119 lines)
006_activity_routing_foundation.sql → Activity log, buyer profiles, ownership columns (157 lines)
007_routing_assignments.sql    → Routing assignments table (57 lines)
008_offer_attribution_columns.sql  → Offer TTL/reconfirmation fields (24 lines)
```

All idempotent, all transactional where appropriate. Clean dependency chain. Ready to apply sequentially.

---

## Prioritized Recommendations

### Do Now (Before Next Feature Work)
1. **Fix the test methodology** — replace MagicMock with explicit stubs for model attributes to prevent attribute-name bugs from hiding
2. **Remove inline DDL from main.py startup** — rely on migration files instead
3. **Add logging to silent exception handlers** — the 11 swallowed exceptions in main.py startup should at minimum log warnings

### Do Soon (Next Sprint)
4. **Split main.py into routers** — the 4,430-line god file is the biggest maintainability risk
5. **Add Pydantic request models** to the top 10 most-used endpoints for validation and documentation
6. **Make routing maps configurable** — move `_BRAND_COMMODITY_MAP` and `_COUNTRY_REGION_MAP` to the database or a config file

### Do Eventually (Scaling Prep)
7. **Async database sessions** — switch to AsyncSession before concurrent user count grows
8. **Proper test framework** — migrate from simulation scripts to pytest with fixtures, for better isolation and CI integration
9. **API versioning** — prefix routes with `/api/v1/` before external integrations depend on the current paths
10. **Observability** — add structured logging, request tracing, and endpoint latency metrics

---

## Files Changed During This Review

| File | Change |
|------|--------|
| `app/services/routing_service.py` | `vendor.country` → `vendor.hq_country`, `vendor.company_name` → `vendor.display_name` |
| `app/utils/normalization.py` | Reordered `_PACKAGING_MAP` (Tray bug fix, earlier session) |
| `scripts/simulation_test.py` | Updated Tray assertion (earlier session) |
| `scripts/simulation_test_phase3.py` | `vendor.country` → `vendor.hq_country` in mocks |
| `scripts/simulation_test_master.py` | `v.country` → `v.hq_country` in mocks |
| `scripts/smoke_test_live.py` | `vendor.company_name` → `vendor.display_name` |

**All 704 tests passing. Zero regressions.**
