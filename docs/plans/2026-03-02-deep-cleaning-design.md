# Deep Cleaning Design — Full Stack Hybrid Approach

**Date**: 2026-03-02
**Scope**: Backend routers/services, frontend JS/CSS, tests, patterns
**Strategy**: Split oversized files + consolidate duplicates + modernize patterns
**Compatibility**: Clean break — no re-export shims, all imports updated in place

## Phase 1: Split Oversized Routers

Split 5 routers (>1,000 lines each) into cohesive sub-modules within `app/routers/`:

| Original | Lines | Split Into |
|----------|-------|-----------|
| `vendors.py` (2,289) | CRUD, materials, contacts, validation | `vendors_crud.py`, `vendors_materials.py`, `vendors_contacts.py`, `vendors_validation.py` |
| `requisitions.py` (1,648) | CRUD, line items, search, workflow | `requisitions_crud.py`, `requisitions_items.py`, `requisitions_search.py` |
| `dashboard.py` (1,533) | Summary, charts, exports | `dashboard_summary.py`, `dashboard_charts.py`, `dashboard_exports.py` |
| `admin.py` (1,125) | Users, system, data management | `admin_users.py`, `admin_system.py`, `admin_data.py` |
| `v13_features.py` (1,003) | Feature-gated endpoints | Merge into appropriate domain routers or split by feature |

Update all `include_router()` calls in `main.py`. No backward-compat re-exports.

## Phase 2: Consolidate Duplicate Services

- **Enrichment**: Extract `BaseEnrichmentService` with shared lookup/caching/retry from:
  - `enrichment.py`, `deep_enrichment_service.py`, `customer_enrichment_service.py`, `customer_enrichment_batch.py`, `material_enrichment_service.py`
- **Scoring**: Document hierarchy, remove dead code:
  - `avail_score_service.py` → `unified_score_service.py` → `multiplier_score_service.py` → `sourcing_score.py`
- **Buy Plans**: Audit v2 usage. If dead, deprecate `buyplan_service.py` and migrate to v3.

## Phase 3: Modernize Backend Patterns

- Replace `.all()` with `select()` statements (SQLAlchemy 2.0) in ~10+ files
- Add pagination/limits to unbounded queries on large tables
- Remove dead imports and unused re-exports from `scheduler.py`
- Clean up `_reference/` directory if unused

## Phase 4: Frontend Cleanup

- Split `app.js` (11,560 lines) → `search.js`, `requisitions.js`, `vendors.js`, `upload.js`
- Split `crm.js` (8,378 lines) → `companies.js`, `quotes.js`, `activity.js`, `apollo.js`
- Replace 143+ `style="display:none"` with `.u-hidden` class
- Eliminate 15+ `!important` overrides
- Extract 30+ hardcoded colors to CSS variables
- Remove duplicate CSS rules (e.g., `.btn-danger` line 816)

## Phase 5: Test Reorganization

- Split `conftest.py` (13,242 lines) → `conftest.py` (core), `conftest_db.py`, `conftest_mocks.py`, `conftest_factories.py`
- Split `test_scheduler.py` (5,365) to match `jobs/` modules
- Split `test_routers_crm.py` (4,071) and `test_routers_vendors.py` (3,513) by sub-domain
- Fix 4 flaky order-dependent tests (fixture isolation)
- Fill `buyplan_v3_notifications.py` coverage gap (19% → 100%)

## Phase 6: CSS & Security Hardening

- Verify/implement CSP nonce middleware in `main.py`
- Remove `unsafe-inline` from CSP
- Migrate remaining inline styles to CSS classes
- Add accessibility attributes (`role`, `tabindex`, `for`)

## Concurrency Plan

```
Phase 1 (routers) ──→ Phase 2 (services) ──→ Phase 3 (patterns)
                  ╲                        ╱
Phase 4 (frontend)  ──── independent ────
Phase 5 (tests)     ──── independent ────
Phase 6 (security)  ──── independent ────
```

Phases 4, 5, 6 can run concurrently with each other and with Phase 2/3.

## Success Criteria

- All tests pass (7,296+) after each phase
- No file >800 lines in routers
- No duplicate enrichment/scoring patterns
- Frontend JS files <3,000 lines each
- conftest.py <3,000 lines
- 100% test coverage maintained
- CSP nonce implemented, unsafe-inline removed
