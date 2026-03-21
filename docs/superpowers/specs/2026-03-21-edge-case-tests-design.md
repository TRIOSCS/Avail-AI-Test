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

**Why**: 56K LOC with ~12 tests. The 95% confidence gate is a core business rule.

**Source files**:
- `app/services/enrichment_orchestrator.py`
- `app/services/enrichment.py`

**Test file**: `tests/test_enrichment_orchestrator.py` (expand), `tests/test_enrich_batch.py` (expand)

**Edge cases to add**:

### Confidence Gate (boundary)
- Confidence exactly 0.95 → should apply
- Confidence 0.9499 → should NOT apply
- Confidence 0.9501 → should apply
- Confidence None → should NOT apply, should not raise
- Confidence 0.0 → should NOT apply
- Confidence 1.0 → should apply
- Confidence > 1.0 → should reject/clamp

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

### Batch Enrichment (error paths)
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
- archived → won (should fail)
- lost → sourcing (should fail)
- won → archived → active (round-trip)
- Same status transition twice in rapid succession
- Transition with None actor

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

### Scoring Boundaries
- Margin exactly 0% → should score as lowest tier
- Margin exactly 10% → boundary between tiers
- Margin exactly 20% → boundary between tiers
- Negative margin (-5%) → should not generate match
- Recency exactly 365 days → boundary
- Recency exactly 730 days → boundary
- Frequency of 0 purchases → no match
- Purchase date in the future → handle gracefully

### Match Input (null/boundary)
- Offer with quantity = 0 → skip
- Offer with negative price → skip
- Offer with None material_card → skip (tested but verify)
- Batch with 0 offers → no-op
- Batch with 1 offer → works
- Batch with 10,000 offers → doesn't OOM

### Suppression/Throttle (business logic)
- DNO list with duplicate MPNs → deduped
- Throttle window exactly expired (boundary second)
- DNO + throttle both active → DNO takes precedence
- Empty DNO set → no suppression

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

### Circuit Breaker (boundary)
- Exactly at failure threshold → should open
- One below threshold → should stay closed
- Half-open state: success → close, failure → re-open
- Multiple connectors failing simultaneously → independent breakers

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
| 2 | Requisition Service | ~20 |
| 3 | Proactive Matching | ~18 |
| 4 | Sourcing Connectors | ~18 |
| 5 | Vendor/Customer Analysis | ~12 |
| 6 | Faceted Search | ~12 |
| **Total** | | **~105** |
