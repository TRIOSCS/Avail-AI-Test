# Test Suite Cleanup Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce 9,291 tests across 307 files to ~8,500 tests across ~280 files by removing duplicate coverage-chasing files, dead feature tests, and consolidating overlapping test files. Add pytest markers for fast/slow test runs.

**Architecture:** Pure deletion and file merging — no behavior changes, no new test logic. Coverage-chasing files are duplicates of proper domain tests and can be safely removed. Consolidation merges tests for the same module into a single file.

**Tech Stack:** pytest, pytest-timeout (already installed)

---

## Task 1: Delete coverage-chasing test files

**Files to delete (20 files, ~600 tests):**

- [ ] **Step 1: Delete all coverage-chasing files**

```bash
rm tests/test_coverage_100.py
rm tests/test_coverage_final.py
rm tests/test_coverage_final_gaps.py
rm tests/test_coverage_gaps.py
rm tests/test_coverage_gaps_final.py
rm tests/test_coverage_gaps_services.py
rm tests/test_coverage_quick_wins.py
rm tests/test_coverage_remaining.py
rm tests/test_coverage_routers_final.py
rm tests/test_coverage_services_100.py
rm tests/test_coverage_utils_final.py
rm tests/test_final_coverage_100.py
rm tests/test_connector_coverage_100.py
rm tests/test_auth_coverage.py
rm tests/test_database_coverage.py
rm tests/test_dependencies_coverage.py
rm tests/test_enrichment_coverage.py
rm tests/test_scoring_coverage.py
rm tests/test_vite_coverage.py
rm tests/test_website_scraper_coverage.py
```

- [ ] **Step 2: Run tests to verify nothing broke**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -q --tb=no --timeout=30 2>&1 | tail -5
```

- [ ] **Step 3: Commit**

```bash
git add -u tests/
git commit -m "test: delete 20 coverage-chasing test files (pure duplicates of domain tests)"
```

## Task 2: Delete dead feature and deprecated test files

- [ ] **Step 1: Delete test files for removed features**

```bash
rm tests/test_command_center.py
rm tests/test_buyplan_service.py
rm tests/test_normalization_helpers_coverage.py
rm tests/test_signature_parser_coverage.py
rm tests/test_schemas_requisitions_coverage.py
rm tests/test_teams_coverage.py
rm tests/test_main_coverage.py
```

- [ ] **Step 2: Run tests to verify nothing broke**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -q --tb=no --timeout=30 2>&1 | tail -5
```

- [ ] **Step 3: Commit**

```bash
git add -u tests/
git commit -m "test: delete 7 dead/deprecated test files"
```

## Task 3: Consolidate startup test files

Merge `test_startup_full.py` and `test_startup_gaps.py` into `test_startup.py`.

**Files:**
- Modify: `tests/test_startup.py`
- Delete: `tests/test_startup_full.py`, `tests/test_startup_gaps.py`

- [ ] **Step 1: Append unique test classes from _full and _gaps into test_startup.py**

Read all three files. Identify test classes/functions in _full and _gaps that don't exist in test_startup.py. Append them to test_startup.py with their imports.

- [ ] **Step 2: Delete the merged files**

```bash
rm tests/test_startup_full.py tests/test_startup_gaps.py
```

- [ ] **Step 3: Run startup tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_startup.py -v --timeout=30
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_startup.py -u tests/
git commit -m "test: consolidate 3 startup test files into test_startup.py"
```

## Task 4: Consolidate main test files

Merge `test_main_full.py` into `test_main_coverage.py`, then rename to `test_main.py`.

**Files:**
- Modify: `tests/test_main_coverage.py` → rename to `tests/test_main.py`
- Delete: `tests/test_main_full.py`

- [ ] **Step 1: Append unique test classes from _full into test_main_coverage.py**

- [ ] **Step 2: Rename and delete**

```bash
mv tests/test_main_coverage.py tests/test_main.py
rm tests/test_main_full.py
```

- [ ] **Step 3: Run main tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_main.py -v --timeout=30
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_main.py -u tests/
git commit -m "test: consolidate main test files into test_main.py"
```

## Task 5: Consolidate vendor score test files

Merge `test_vendor_score_gaps.py` into `test_vendor_score.py`.

**Files:**
- Modify: `tests/test_vendor_score.py`
- Delete: `tests/test_vendor_score_gaps.py`

- [ ] **Step 1: Append unique tests from _gaps into test_vendor_score.py**

- [ ] **Step 2: Delete merged file**

```bash
rm tests/test_vendor_score_gaps.py
```

- [ ] **Step 3: Run vendor score tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_vendor_score.py -v --timeout=30
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_vendor_score.py -u tests/
git commit -m "test: consolidate vendor score test files"
```

## Task 6: Consolidate duplicate buy plan service tests

Merge `test_buy_plan_v3_service.py` into `test_buy_plan_service_v3.py`.

**Files:**
- Modify: `tests/test_buy_plan_service_v3.py`
- Delete: `tests/test_buy_plan_v3_service.py`

- [ ] **Step 1: Append unique tests from v3_service into service_v3**

- [ ] **Step 2: Delete merged file**

```bash
rm tests/test_buy_plan_v3_service.py
```

- [ ] **Step 3: Run buy plan service tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buy_plan_service_v3.py -v --timeout=30
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_buy_plan_service_v3.py -u tests/
git commit -m "test: consolidate duplicate buy plan service tests"
```

## Task 7: Add pytest markers for fast test runs

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add markers config to pyproject.toml**

Add under `[tool.pytest.ini_options]`:
```toml
markers = [
    "slow: marks tests as slow (deselect with '-m \"not slow\"')",
    "integration: marks integration tests",
]
```

- [ ] **Step 2: Add @pytest.mark.slow to integration test files**

Add `pytestmark = pytest.mark.slow` at the top of:
- `tests/test_integration_crm.py`
- `tests/test_integration_phase4.py`
- `tests/test_integration_prospecting.py`
- `tests/test_integration_quote_workflow.py`
- `tests/test_integration_requisitions.py`
- `tests/test_integration_smoke.py`
- `tests/test_e2e_sourcing_flow.py`
- `tests/test_nc_worker_full.py`
- `tests/test_ics_worker_full.py`

- [ ] **Step 3: Run quick tests to verify marker works**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -m "not slow" -q --tb=no --timeout=30 2>&1 | tail -5
```

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml tests/test_integration_*.py tests/test_e2e_sourcing_flow.py tests/test_nc_worker_full.py tests/test_ics_worker_full.py
git commit -m "test: add slow/integration markers for fast test runs"
```
