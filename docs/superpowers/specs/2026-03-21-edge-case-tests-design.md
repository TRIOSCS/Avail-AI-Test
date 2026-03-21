# Edge Case Tests for Critical Modules

## Goal

Add edge case tests (boundary/null, error paths, data integrity, business logic boundaries) to the 6 most business-critical modules. Target: catch regressions at system boundaries where real-world data is messiest.

## Scope

Focus on modules where edge cases pose the highest risk of production bugs. All 4 edge case types per module:
1. **Boundary/null** — empty strings, None, zero-length lists, negative numbers, max-length strings
2. **Error paths** — missing DB records, failed API calls, malformed payloads
3. **Data integrity** — duplicates, concurrent modifications, orphaned records, unicode/special chars
4. **Business logic boundaries** — invalid state transitions, threshold boundaries, expired items

---

## Priority 1: Enrichment Module

**Why**: Enrichment-related files total ~450+ lines in orchestrator alone, with only ~7 tests. The confidence gate (default threshold 0.90) is a core business rule.

**Source files**:
- `app/services/enrichment_orchestrator.py`
- `app/services/enrichment.py`

**Test file**: `tests/test_enrichment_orchestrator.py` (expand), `tests/test_enrich_batch.py` (expand)

**Edge cases to add**:

### Confidence Gate (boundary — default threshold=0.90)
- Confidence exactly 0.90 → should apply
- Confidence 0.8999 → should NOT apply
- Confidence 0.9001 → should apply
- Confidence None → should NOT apply, should not raise
- Confidence 0.0 → should NOT apply
- Confidence 1.0 → should apply
- Confidence > 1.0 (e.g. 1.5) → should still apply (no upper bound in code)
- Custom threshold: pass threshold=0.95, confidence=0.94 → should NOT apply

### Input Validation (null/boundary)
- Empty company name for enrichment
- None domain for web enrichment
- Empty email list for email enrichment
- Unicode company names (CJK, emoji, diacritics)
- Extremely long company name (10K chars)

### Multi-Source Conflicts (data integrity)
- Two sources return conflicting phone numbers → merge strategy
- One source returns null, another returns data → non-null wins
- All sources return null → graceful empty result
- Partial source failures (2 of 4 sources fail) → use remaining

### Batch Enrichment (error paths — target: `tests/test_enrich_batch.py` → `app/services/enrichment.py`)
- Batch with 0 items → no-op, no crash
- Batch with 1 item → works like single
- Batch with item that causes DB integrity error → skip, continue rest
- Batch with all items failing → return empty results, no crash

---

## Priority 2: Requisition Service Core

**Why**: Core CRUD operations have minimal tests; only utilities are covered.

**Source files**:
- `app/services/requisition_service.py`
- `app/services/requisition_state.py`

**Test file**: `tests/test_requisition_service.py` (expand), `tests/test_requisition_state.py` (expand)

**Edge cases to add**:

### State Transitions (business logic)
- archived → won (should fail — archived only allows → active)
- lost → sourcing (should fail — lost allows → active, archived, reopened)
- won → archived → active (round-trip — both transitions allowed)
- Same status transition twice in rapid succession
- Note: "Transition with None actor" already tested — skip

### Create/Update (null/boundary)
- Create with empty name → should reject
- Create with name at max length (255 chars)
- Create with name containing only whitespace
- Update deadline to past date
- Update deadline to None (clear deadline)
- Create with zero requirements → allowed
- Clone requisition with 0 requirements
- Clone requisition with max requirements (1000+)

### Data Integrity
- Clone preserving offer mappings when offers reference deleted vendors
- Update requisition while concurrent search is running (no crash)
- Delete requisition that has active searches → cleanup

### Cache Invalidation
- Rapid create-then-list → cache reflects new item
- Update during active cache → stale data cleared
- Concurrent invalidation from two operations

---

## Priority 3: Proactive Matching Boundaries

**Why**: Well-tested but scoring thresholds are business-critical.

**Source files**:
- `app/services/proactive_matching.py`
- `app/services/proactive_helpers.py`

**Test file**: `tests/test_proactive_matching.py` (expand), `tests/test_proactive_helpers.py` (expand)

**Edge cases to add**:

### Scoring Boundaries (per _score_margin: >=30→100, >=20→80, >=10→60, >0→40, <=0→10; per _score_recency: <=180→100, <=365→80, <=730→60, >730→40)
- Margin exactly 0% → score 10 (<=0 tier)
- Margin exactly 10% → score 60 (>=10 tier boundary)
- Margin exactly 20% → score 80 (>=20 tier boundary)
- Margin exactly 30% → score 100 (>=30 tier boundary)
- Margin 9.99% → score 40 (>0 tier, just below >=10)
- Negative margin (-5%) with min_margin_pct=0 → filtered out (margin < min_margin)
- Negative margin (-5%) with min_margin_pct=-10 → NOT filtered, score 10
- Recency exactly 180 days → score 100 (<=180 tier boundary)
- Recency 181 days → score 80 (drops to <=365 tier)
- Recency exactly 365 days → score 80 (<=365 tier boundary)
- Recency exactly 730 days → score 60 (<=730 tier boundary)
- Recency 731 days → score 40 (>730 tier)
- Frequency of 0 purchases → no match
- Purchase date in the future → handle gracefully

### Match Input (null/boundary)
- Offer with negative price → skip or score 0
- Offer with None margin_pct → handle gracefully (code checks `if margin_pct is not None`)
- Batch with 0 offers → no-op
- Batch with 1 offer → works
- Batch with 10,000 offers → doesn't OOM

### Suppression/Throttle (business logic)
- DNO list with duplicate MPNs → deduped
- Throttle window exactly expired (boundary second)
- Both DNO and throttle active for same company → both checked independently, company skipped
- Empty DNO set → no suppression
- Empty throttle set → no throttle

---

## Priority 4: Sourcing Connectors

**Why**: External API boundaries are where data is most unpredictable.

**Source files**:
- `app/connectors/sources.py` (BaseConnector, CircuitBreaker)
- Individual connector files

**Test file**: `tests/test_connectors.py` (expand), `tests/test_search_streaming.py` (expand)

**Edge cases to add**:

### Malformed Responses (error paths)
- Connector returns empty JSON `{}`
- Connector returns HTML error page (string, not JSON)
- Connector returns truncated JSON (incomplete)
- Connector returns 200 with error body
- Connector returns null for price/quantity fields
- Connector timeout mid-response

### Input Validation (boundary)
- MPN with unicode characters (accented, CJK)
- MPN with special chars (`/`, `#`, `&`, spaces)
- Extremely long MPN (500+ chars)
- Empty MPN → should reject before API call
- MPN with only whitespace

### Circuit Breaker (boundary — first two already covered, only add new ones)
- Half-open state: failure → re-open (not yet tested)
- Multiple connectors failing simultaneously → independent breakers (not yet tested)

### Rate Limiting (boundary)
- Request at exactly the rate limit
- Burst of requests just under limit
- Rate limit reset at boundary second

---

## Priority 5: Vendor/Customer Analysis

**Why**: Minimal test coverage on analysis features.

**Source files**:
- `app/services/vendor_analysis_service.py`
- `app/services/customer_analysis_service.py`

**Test file**: `tests/test_vendor_analysis_service.py` (expand), `tests/test_customer_analysis_service.py` (expand)

**Edge cases to add**:

### Vendor Analysis
- Vendor with zero purchase history → empty analysis
- Vendor with single transaction → valid but limited analysis
- Vendor with all-null fields (no name, no domain, no contacts)
- Vendor with mixed currency data → handle or flag
- Vendor with 10,000+ transactions → no timeout

### Customer Analysis
- Customer with zero requisitions → empty analysis
- Customer with sites but no parts → valid response
- Claude returns None → graceful fallback
- Claude returns empty string → graceful fallback
- Analysis truncation at exactly 5 items

---

## Priority 6: Faceted Search

**Why**: New feature with limited edge case coverage.

**Source files**:
- `app/services/faceted_search_service.py`

**Test file**: `tests/test_faceted_search_service.py` (expand)

**Edge cases to add**:

### Filter Validation (boundary)
- Empty commodity string → return all
- Commodity with special characters → SQL-safe
- Numeric range with min > max → reject or swap
- Numeric range with negative values → handle
- Numeric range with min = max → exact match
- Filter with unicode manufacturer name

### Pagination (boundary)
- Offset 0 → first page
- Offset beyond total results → empty page, no crash
- Limit = 0 → reject or default
- Limit = 10,000 → cap to max

### Search (data integrity)
- MPN search with packaging suffixes → deduped
- Manufacturer search with multiple word variations
- Text search with SQL injection attempt → sanitized

---

## Testing Patterns

All new tests follow existing project conventions:
- Use `from tests.conftest import engine` for SQLite test engine
- Mock lazy imports at source module
- Error responses check `["error"]` not `["detail"]`
- Use `db.get(Model, id)` pattern (SQLAlchemy 2.0)
- Tests run in parallel with pytest-xdist — ensure no shared state

## Estimated Test Count

| Priority | Module | New Tests |
|----------|--------|-----------|
| 1 | Enrichment | ~25 |
| 2 | Requisition Service | ~18 |
| 3 | Proactive Matching | ~20 |
| 4 | Sourcing Connectors | ~14 |
| 5 | Vendor/Customer Analysis | ~12 |
| 6 | Faceted Search | ~12 |
| **Total** | | **~101** |
