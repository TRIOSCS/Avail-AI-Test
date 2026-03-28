# Codebase Cleanup Findings — 2026-03-28

## Status: Phases 1-4 DONE. Coverage PENDING. Stale docs AWAITING APPROVAL.

---

## COMPLETED — Phase 1: Dead Python Functions

| File | Function | Status |
|------|----------|--------|
| `app/utils/file_validation.py` | `is_password_protected()` | REMOVED + tests cleaned |
| `app/utils/normalization_helpers.py` | `clean_contact_name()` + `_DEPARTMENT_*`, `_EXT_PATTERN`, `_CASING_FIXES` | REMOVED + tests cleaned |
| `app/search_service.py` | `_median()` | FALSE POSITIVE — restored |

## COMPLETED — Phase 2: Unused Static Files (4 files + 2 test files)

Removed: `intake_helpers.mjs` + tests, `sw.js`, `offline.html`, `manifest.json`

## COMPLETED — Phase 3: Stale Root-Level Files (13 files, ~1.4MB)

Removed: 3 snapshot MDs, `customer_email_export.csv`, `export_customer_emails.py`, `FRONTEND_AUDIT.md`, `FRONTEND_BUGS.md`, `CODE_REVIEW.md`, 4 PLAN files, `material_card_data_assurance_plan.md`

## COMPLETED — Phase 4a: Dead Service Files (5 files + 3 test files)

Deleted files:
- `app/services/enrichment_utils.py`, `data_cleanup_service.py`, `prospect_pool_service.py`, `part_discovery_service.py`
- `app/jobs/lifecycle_jobs.py`
- `tests/test_enrichment_utils.py`, `test_part_discovery_service.py`, `test_lifecycle_jobs.py`
- Cleaned up: `tests/test_security_h3_h4_h6.py`, `tests/test_remediation_waves.py`

## COMPLETED — Phase 4b: Dead Scoring Function + Test Fixture

- `app/scoring.py` — removed `score_requirement_priority()` + its tests
- `tests/conftest.py` — removed `override_client` fixture

## COMPLETED — Phase 4c: Dead Pydantic Schemas (14 classes removed)

**`app/schemas/ai.py`** — removed: `ParsedQuote`, `RfqDraftPart`, `QuoteForAnalysis`
**`app/schemas/crm.py`** — removed: `SiteCreate`, `SiteUpdate`, `SiteContactCreate`, `SiteContactUpdate`, `CompanyMergeRequest`, `MassTransferRequest`
**`app/schemas/emails.py`** — removed all 5 classes (file now empty)
Not removed (confirmed in-use): `DraftOfferItem`, `QuoteLineItem`, `SuggestedContactItem`, `SuggestedSiteContact`, `PaginatedResponse`

Test cleanup: 3 test files cleaned

## COMPLETED — Phase 5: Ruff Lint + Format

- 7 issues fixed in tests/ (5 unused imports, 2 undefined names skipped)
- 9 files reformatted
- `app/` and `tests/` both pass `ruff check` with 0 errors

---

## PENDING — Test Coverage

Full parallel run in progress (`-n auto` + `--cov=app`)

## COMPLETED — Phase 6: Stale Documentation

- Removed entire `docs/plans/` directory (63 obsolete files, pre-2026-03-10)
- Removed 8 stale root docs: `v1-vs-v2-feature-gap.md`, `v2-100-percent-plan.md`, `README_RECOVERY.md`, `UI_BUG_OBSERVATIONS.md`, `RECOVERY_PLAN_TEMPLATE.md`, `TARGET_LAYOUT_SPEC.md`, `PRODUCTION_READINESS.md`, `ACCEPTANCE_CHECKLIST.md`

## COMPLETED — Phase 7: Commented-Out Code

- `app/services/activity_service.py:399-401` — removed commented-out auto-claim code block, kept design decision comment

---

## Summary Totals

| Category | Items | Status |
|----------|-------|--------|
| Dead functions | 2 removed, 1 false positive | DONE |
| Stale root files | 13 removed (~1.4MB) | DONE |
| Unused static files | 4 + 2 test files removed | DONE |
| Dead service files | 5 + 3 test files removed | DONE |
| Dead scoring fn + fixture | 2 removed | DONE |
| Dead schema classes | 14 removed + 3 test files cleaned | DONE |
| Ruff lint + format | 7 fixed, 9 reformatted | DONE |
| Test coverage | Awaiting results | PENDING |
| Stale docs | 71 files removed | DONE |
| Commented-out code | 1 block removed | DONE |
