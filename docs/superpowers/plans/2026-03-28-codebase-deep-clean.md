# Codebase Deep Clean — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove all dead code, fix all failing tests, and bring test coverage to 85%.

**Architecture:** Parallel-first — Tasks 1-6 are independent and can run simultaneously in worktrees. Tasks 7+ are sequential and depend on prior work merging cleanly.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0, PostgreSQL 16, Alembic, pytest

---

## Task 1: Delete Dead Service Files

**Files:**
- Delete: `app/services/enrichment_orchestrator.py`
- Delete: `app/services/sourcing_lead_engine.py`
- Delete: `app/services/vendor_spec_enrichment.py`
- Delete: `app/services/salesperson_scorecard.py`
- Delete: `app/services/buyplan_migration.py`
- Delete: `app/services/website_scraper.py`
- Delete: `app/services/free_text_parser.py`
- Delete: `app/services/self_repair_service.py`
- Delete: `app/services/tagging_nexar.py`
- Delete: `app/services/material_search_service.py`
- Delete: `app/services/rfq_compose_service.py`
- Delete: `app/services/vendor_detail_service.py`
- Delete: `app/services/company_detail_service.py`
- Delete: `app/services/credit_manager.py`
- Delete: `tests/test_enrichment_orchestrator.py`
- Delete: `tests/test_sourcing_lead_engine.py`
- Delete: `tests/test_vendor_spec_enrichment.py`
- Delete: `tests/test_buyplan_migration.py`
- Delete: `tests/test_website_scraper.py`
- Delete: `tests/test_free_text_parser.py`
- Delete: `tests/test_tagging_nexar.py`
- Delete: `tests/test_material_search_service.py`
- Delete: `tests/test_rfq_compose_service.py`
- Delete: `tests/test_credit_manager.py`
- Modify: `tests/test_integration_phase4.py` (remove orchestrator-only test)

- [ ] **Step 1: Delete 14 dead service files**

```bash
cd /root/availai
rm app/services/enrichment_orchestrator.py \
   app/services/sourcing_lead_engine.py \
   app/services/vendor_spec_enrichment.py \
   app/services/salesperson_scorecard.py \
   app/services/buyplan_migration.py \
   app/services/website_scraper.py \
   app/services/free_text_parser.py \
   app/services/self_repair_service.py \
   app/services/tagging_nexar.py \
   app/services/material_search_service.py \
   app/services/rfq_compose_service.py \
   app/services/vendor_detail_service.py \
   app/services/company_detail_service.py \
   app/services/credit_manager.py
```

- [ ] **Step 2: Delete 10 companion test files**

```bash
rm tests/test_enrichment_orchestrator.py \
   tests/test_sourcing_lead_engine.py \
   tests/test_vendor_spec_enrichment.py \
   tests/test_buyplan_migration.py \
   tests/test_website_scraper.py \
   tests/test_free_text_parser.py \
   tests/test_tagging_nexar.py \
   tests/test_material_search_service.py \
   tests/test_rfq_compose_service.py \
   tests/test_credit_manager.py
```

- [ ] **Step 3: Fix test_integration_phase4.py**

Remove only the `enrichment_orchestrator` import and the `test_enrich_company_pipeline` test class. Keep the remaining 12 search/scoring tests.

Remove from the imports at the top of the file:
```python
from app.services.enrichment_orchestrator import (
```
and the entire import block that follows it.

Remove the `TestEnrichCompanyPipeline` class (or whatever class contains `test_enrich_company_pipeline`).

- [ ] **Step 4: Verify**

```bash
ruff check app/ && TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_integration_phase4.py -v -o "addopts="
```

Expected: ruff clean, remaining 12 tests in test_integration_phase4.py pass.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "chore: remove 14 dead service files and 10 companion test files

Services imported by zero production code. Verified by grep.
~3,050 lines of dead code removed."
```

---

## Task 2: Delete Dead Pydantic Schemas

**Files:**
- Delete: `app/schemas/apollo.py`
- Delete: `app/schemas/enrichment.py`
- Delete: `app/schemas/explorium.py`
- Modify: `app/schemas/prospect_account.py` (remove 6 classes)
- Modify: `app/schemas/responses.py` (remove 8 classes)
- Modify: `app/schemas/crm.py` (remove 2 classes)
- Modify: `app/schemas/requisitions.py` (remove 1 class)
- Modify: `app/schemas/requisitions2.py` (remove 1 class)
- Modify: `app/schemas/task.py` (remove 2 classes)
- Modify: `app/schemas/proactive.py` (remove 1 class)
- Modify: `app/schemas/knowledge.py` (remove 1 class)
- Modify: `app/schemas/activity.py` (remove 1 class)
- Modify: `app/schemas/tags.py` (remove 1 class)

- [ ] **Step 1: Delete 3 entirely dead schema files**

```bash
cd /root/availai
rm app/schemas/apollo.py app/schemas/enrichment.py app/schemas/explorium.py
```

- [ ] **Step 2: Remove dead classes from partial files**

For each file below, remove the listed class definitions (the `class Name(BaseModel):` block and all its fields through the next blank line before the next class).

**`app/schemas/prospect_account.py`** — remove classes at these line ranges:
- `ProspectAccountList` (lines 68-77)
- `ProspectClaimRequest` (lines 91-94)
- `ProspectDismissRequest` (lines 97-100)
- `ProspectFilters` (lines 103-116)
- `ProspectAddRequest` (lines 119-122)
- `DiscoveryBatchRead` (lines 126-145)

**`app/schemas/responses.py`** — remove classes (KEEP `SimpleOkResponse`!):
- `OkResponse` (lines 23-24)
- `SightingItem` (lines 30-44)
- `RequirementListItem` (lines 65-71)
- `CompanyListItem` (lines 137-141)
- `OfferGroupItem` (lines 147-150)
- `QuoteSummaryResponse` (lines 197-222)
- `BuyerLeaderboardResponse` (lines 243-245)

Also check if `RequisitionListResponse` (lines 61-62) references `RequisitionListItem` — if so, remove it too.

**`app/schemas/crm.py`** — remove:
- `CompanyOut` (lines 130-132)
- `SiteOut` (lines 264-266)

**`app/schemas/requisitions.py`** — remove:
- `RequisitionArchiveOut` (lines 88-90)

**`app/schemas/requisitions2.py`** — remove:
- `BulkActionForm` (lines 91-111)

**`app/schemas/task.py`** — remove:
- `TaskResponse` (lines 53-76)
- `TaskSummary` (lines 93-96)

**`app/schemas/proactive.py`** — remove:
- `PrepareProactive` (lines 26-27)

**`app/schemas/knowledge.py`** — remove:
- `InsightsResponse` (lines 70-74)

**`app/schemas/activity.py`** — remove:
- `ActivityLogFilter` (lines 26-33)

**`app/schemas/tags.py`** — remove:
- `EntityTagSummary` (lines 37-41)

- [ ] **Step 3: Clean up any now-unused imports in modified schema files**

After removing classes, check each modified file for imports that are no longer used (e.g., `Optional`, `List`, `Field` if no remaining class uses them). Run:

```bash
ruff check app/schemas/ --fix
```

- [ ] **Step 4: Verify**

```bash
ruff check app/ && TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/ -q -o "addopts=" --timeout=60 2>&1 | tail -5
```

Expected: No new test failures introduced.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "chore: remove 48 dead Pydantic schemas

3 entire files deleted (apollo, enrichment, explorium).
Dead classes removed from 10 more schema files.
All verified by grep — zero references outside definitions."
```

---

## Task 3: Delete Dead Templates

**Files:**
- Delete: `app/templates/base.html`
- Delete: `app/templates/htmx/partials/ai/` (entire directory)
- Delete: `app/templates/htmx/partials/follow_ups/follow_ups/` (misplaced directory)
- Delete: `app/templates/htmx/partials/sightings/_constraints.html`
- Delete: `app/templates/htmx/partials/sightings/_enrichment_bar.html`
- Delete: `app/templates/htmx/partials/sightings/_suggested_action.html`
- Delete: `app/templates/htmx/partials/shared/column_picker.html`
- Delete: `app/templates/htmx/partials/shared/quote_builder_shell.html`
- Delete: `app/templates/htmx/partials/shared/split_panel.html`
- Delete: `app/templates/htmx/partials/requisitions/rfq_draft_result.html`
- Delete: `app/templates/htmx/partials/search/results.html`
- Delete: `app/templates/htmx/partials/sourcing/search_progress.html`

- [ ] **Step 1: Delete all 16 dead templates**

```bash
cd /root/availai
rm app/templates/base.html
rm -rf app/templates/htmx/partials/ai/
rm -rf app/templates/htmx/partials/follow_ups/follow_ups/
rm app/templates/htmx/partials/sightings/_constraints.html \
   app/templates/htmx/partials/sightings/_enrichment_bar.html \
   app/templates/htmx/partials/sightings/_suggested_action.html
rm app/templates/htmx/partials/shared/column_picker.html \
   app/templates/htmx/partials/shared/quote_builder_shell.html \
   app/templates/htmx/partials/shared/split_panel.html
rm app/templates/htmx/partials/requisitions/rfq_draft_result.html
rm app/templates/htmx/partials/search/results.html
rm app/templates/htmx/partials/sourcing/search_progress.html
```

- [ ] **Step 2: Verify**

```bash
ruff check app/ && TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/ux_mega/ -q -o "addopts=" --timeout=60 2>&1 | tail -5
```

Expected: UX template compilation tests still pass (dead templates were never compiled).

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "chore: remove 16 dead templates

Legacy base.html, entire ai/ partial directory, misplaced follow_ups duplicate,
unused sightings/shared/search/sourcing partials.
All verified — no Python TemplateResponse or Jinja2 include references."
```

---

## Task 4: Delete Dead htmx Refactor Files

**Files:**
- Delete: `app/routers/htmx/_shared.py`
- Delete: `app/routers/htmx/_constants.py`
- Delete: `app/routers/htmx/page.py`

- [ ] **Step 1: Delete 3 stalled refactor files**

```bash
rm app/routers/htmx/_shared.py app/routers/htmx/_constants.py app/routers/htmx/page.py
```

Note: There is no `__init__.py` in this directory. After deletion, the directory may be empty — delete it if so:

```bash
rmdir app/routers/htmx/ 2>/dev/null || true
```

- [ ] **Step 2: Verify**

```bash
ruff check app/ && TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/ -q -o "addopts=" --timeout=60 -x 2>&1 | tail -5
```

Expected: No new failures (files were never imported).

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "chore: remove stalled htmx refactor files (3 files, 358 lines)

_shared.py, _constants.py, page.py — duplicates from 2-3% complete
htmx_views.py decomposition that was never finished or mounted."
```

---

## Task 5: Remove Dead Functions, Jobs, and Enums

**Files:**
- Modify: `app/cache/intel_cache.py` (remove `invalidate`, `flush_enrichment_cache`)
- Modify: `app/database.py` (remove `_set_timezone`, `_make_datetimes_aware`)
- Modify: `app/dependencies.py` (remove `require_sales`, `user_reqs_query`, `wants_html`, `is_htmx_boosted`)
- Modify: `app/routers/_lookup_helpers.py` (remove `get_requirement_or_404`, `get_offer_or_404`)
- Modify: `app/routers/crm/_helpers.py` (remove `get_last_quoted_price`)
- Modify: `app/routers/crm/__init__.py` (remove re-export line)
- Modify: `app/connectors/email_mining.py` (remove `deep_scan_inbox`, `_is_offer_email`)
- Modify: `app/services/search_worker_base/ai_gate.py` (remove `clear_classification_cache`)
- Modify: `app/services/search_worker_base/circuit_breaker.py` (remove `reset`)
- Modify: `app/jobs/core_jobs.py` (remove 2 dead functions + commented registrations)
- Modify: `app/jobs/email_jobs.py` (remove `_compute_vendor_scores_job`)
- Modify: `app/jobs/lifecycle_jobs.py` (remove `_job_lifecycle_sweep` + commented registration)
- Modify: `app/jobs/tagging_jobs.py` (remove 3 dead functions + commented registrations)
- Modify: `app/jobs/eight_by_eight_jobs.py` (remove `run_8x8_poll_dry_run`)
- Delete: `app/jobs/part_discovery_jobs.py` (if only contains `register_discovery_jobs`)
- Modify: `app/constants.py` (remove 4 dead enum classes)
- Modify: `tests/conftest.py` (remove `require_sales` override + `clear_classification_cache` call)
- Modify: Multiple test files (remove tests for dead functions)

- [ ] **Step 1: Remove dead utility functions from app/ files**

Remove from each file the function at the line ranges identified in the spec. For each file:

**`app/cache/intel_cache.py`**: Remove `invalidate()` (lines 138-159) and `flush_enrichment_cache()` (lines 160-201).

**`app/database.py`**: Remove `_set_timezone()` (lines 56-62) and `_make_datetimes_aware()` (lines 64-73). After removal, check if `datetime`, `timezone`, or `event` imports become unused — if so remove them too.

**`app/dependencies.py`**: Remove `require_sales()` (lines 101-108), `user_reqs_query()` (lines 113-122), `wants_html()` (lines 195-200), `is_htmx_boosted()` (lines 201-206). Also remove the section comment `# -- HTMX Detection Utilities` at line 195 if both HTMX functions are gone.

**`app/routers/_lookup_helpers.py`**: Remove `get_requirement_or_404()` (lines 18-26) and `get_offer_or_404()` (lines 27-35). Keep remaining functions.

**`app/routers/crm/_helpers.py`**: Remove `get_last_quoted_price()` (lines 40-70).

**`app/routers/crm/__init__.py`**: Remove line 19 (`    get_last_quoted_price,`).

**`app/connectors/email_mining.py`**: Remove `deep_scan_inbox()` (around line 555) and `_is_offer_email()` (around line 809).

**`app/services/search_worker_base/ai_gate.py`**: Remove `clear_classification_cache()` method (around line 244).

**`app/services/search_worker_base/circuit_breaker.py`**: Remove `reset()` method (around line 61).

- [ ] **Step 2: Remove dead job functions**

**`app/jobs/core_jobs.py`**: Remove `_job_batch_enrich_materials()` and `_job_poll_material_batch()` and their commented-out scheduler registrations.

**`app/jobs/email_jobs.py`**: Remove `_compute_vendor_scores_job()`.

**`app/jobs/lifecycle_jobs.py`**: Remove `_job_lifecycle_sweep()` and its commented-out registration.

**`app/jobs/tagging_jobs.py`**: Remove `_job_connector_enrichment()`, `_job_nexar_backfill()`, `_job_tagging_backfill()` and their commented-out registrations.

**`app/jobs/eight_by_eight_jobs.py`**: Remove `run_8x8_poll_dry_run()`.

**`app/jobs/part_discovery_jobs.py`**: If the file only contains `register_discovery_jobs()`, delete the entire file.

- [ ] **Step 3: Remove 4 dead enum classes from constants.py**

Remove from `app/constants.py`:
- `MatchMethod` (lines 41-49)
- `OfferSource` (lines 52-61)
- `ResponseClassification` (lines 64-73)
- `DiscoveryBatchStatus` (lines 311-317)

Keep `ExcessListStatus` — it has a real import.

- [ ] **Step 4: Clean up companion test code**

**`tests/conftest.py`**: Remove the `require_sales` import (around line 243) and its override (around line 263). Remove the `clear_classification_cache` call (around line 121).

**`tests/test_role_permissions.py`**: Remove the test class/functions that test `require_sales`.

**`tests/test_dependencies.py`**: Remove tests that call `user_reqs_query`.

**`tests/test_routers_crm.py`**: Remove tests for `get_last_quoted_price`.

**`tests/test_cache_intel.py`**: Remove tests for `invalidate()` (around lines 181-227, 340-350).

**`tests/test_connectors.py`**: Remove tests for `deep_scan_inbox` (~7 tests, lines 2393-2497) and `_is_offer_email` (~lines 2051-2054).

**`tests/test_email_intelligence_phase4.py`**: Remove `deep_scan_inbox` tests (~4 calls).

**`tests/test_email_intelligence_phase1.py`**: Remove `deep_scan_inbox` test (~1 call).

**`tests/test_email_intelligence_phase2.py`**: Remove `_is_offer_email` tests.

**`tests/test_search_worker_base.py`**: Remove `clear_classification_cache` test (~line 663) and `reset()` test (~line 89, 540).

**`tests/test_ics_worker_full.py`**: Remove `breaker.reset()` call (~line 571).

**`tests/test_nc_phase6.py`**: Remove `breaker.reset()` call (~line 218).

**`tests/test_nc_worker_full.py`**: Remove `breaker.reset()` call (~line 2402).

**`tests/test_core_jobs.py`**: Remove tests for `_job_batch_enrich_materials` and `_job_poll_material_batch`.

**`tests/test_email_jobs.py`** and **`tests/test_jobs_email.py`**: Remove tests for `_compute_vendor_scores_job`.

**`tests/test_jobs_part_discovery.py`**: Delete entire file if `part_discovery_jobs.py` was deleted.

**`tests/test_jobs_tagging.py`**: Remove tests for the 3 dead tagging jobs.

- [ ] **Step 5: Run ruff fix and verify**

```bash
ruff check app/ --fix && ruff check tests/ --fix
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/ -q -o "addopts=" --timeout=60 2>&1 | tail -5
```

Expected: No new failures from dead code removal.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "chore: remove 24 dead functions, 9 dead jobs, 4 dead enums

15 utility functions, 9 disabled job functions, 4 unused enum classes.
Also cleaned 16 companion test files that tested dead code."
```

---

## Task 6: Consolidate Duplicate Functions

**Files:**
- Create: `app/utils/text_utils.py`
- Modify: `app/services/ai_email_parser.py` (remove local `_clean_email_body`)
- Modify: `app/services/response_parser.py` (remove local `_clean_email_body`, import shared)
- Modify: `app/email_service.py` (update import)
- Modify: `app/services/sourcing_score.py` (add null-safe wrapper)
- Modify: `app/routers/requisitions/core.py` (remove `_compute_sourcing_score`, import from service)
- Modify: `app/services/requisition_list_service.py` (remove `_compute_sourcing_score`, import from service)

- [ ] **Step 1: Create shared `_clean_email_body` in utils**

Create `app/utils/text_utils.py`:

```python
"""text_utils.py — Shared text cleaning utilities.

Called by: services/ai_email_parser.py, services/response_parser.py, email_service.py
Depends on: re (stdlib)
"""

import re


def clean_email_body(body: str) -> str:
    """Strip HTML, excessive whitespace, and email disclaimers.

    Preserves newlines so tabular data and list formatting survive intact.
    """
    if not body:
        return ""
    text = re.sub(r"<br\s*/?>|</p>|</tr>|</li>", "\n", body, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    disclaimer_patterns = [
        r"(?i)this email and any attachments.*?(?=\n\n|\Z)",
        r"(?i)confidentiality notice.*?(?=\n\n|\Z)",
        r"(?i)DISCLAIMER.*?(?=\n\n|\Z)",
    ]
    for pat in disclaimer_patterns:
        text = re.sub(pat, "", text, flags=re.DOTALL)
    return text.strip()
```

- [ ] **Step 2: Update callers of `_clean_email_body`**

**`app/services/ai_email_parser.py`**: Remove the local `_clean_email_body` function (lines 214-236). Add import at top: `from app.utils.text_utils import clean_email_body`. Update call at line 108: `body = clean_email_body(email_body)`.

**`app/services/response_parser.py`**: Remove the local `_clean_email_body` function (lines 259-277). Add import at top: `from app.utils.text_utils import clean_email_body`. Update call at line 135: `body_truncated = clean_email_body(email_body)[:4000]`.

**`app/email_service.py`**: Update import at line 801 from `from app.services.response_parser import RESPONSE_PARSE_SCHEMA, SYSTEM_PROMPT, _clean_email_body` to `from app.services.response_parser import RESPONSE_PARSE_SCHEMA, SYSTEM_PROMPT` and add `from app.utils.text_utils import clean_email_body`. Update call at line 809: `body_truncated = clean_email_body(vr.body or "")[:4000]`.

- [ ] **Step 3: Consolidate `_compute_sourcing_score`**

Add null-safe wrapper to `app/services/sourcing_score.py`:

```python
def compute_sourcing_score_safe(req_cnt, sourced_cnt, rfq_sent, reply_cnt, offer_cnt, call_cnt, email_act_cnt):
    """Null-safe wrapper for list views."""
    return compute_requisition_score_fast(
        req_count=req_cnt or 0,
        sourced_count=sourced_cnt or 0,
        rfq_sent_count=rfq_sent or 0,
        reply_count=reply_cnt or 0,
        offer_count=offer_cnt or 0,
        call_count=call_cnt or 0,
        email_count=email_act_cnt or 0,
    )
```

**`app/routers/requisitions/core.py`**: Remove `_compute_sourcing_score` (lines 52-64). Add import: `from app.services.sourcing_score import compute_sourcing_score_safe`. Update caller at line 468 to use `compute_sourcing_score_safe(...)`.

**`app/services/requisition_list_service.py`**: Remove `_compute_sourcing_score` (lines 34-46). Add import: `from app.services.sourcing_score import compute_sourcing_score_safe`. Update caller at line 400 to use `compute_sourcing_score_safe(...)`.

- [ ] **Step 4: Verify**

```bash
ruff check app/ --fix
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_requisitions.py tests/test_rfq.py tests/test_search_service.py -v -o "addopts=" --timeout=60 2>&1 | tail -10
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "refactor: consolidate 2 duplicate function pairs

_clean_email_body → app/utils/text_utils.py (newline-preserving version)
_compute_sourcing_score → app/services/sourcing_score.py (null-safe wrapper)"
```

---

## Task 7: Alembic Migration — Drop 7 Dead Tables + 1 Column

**Depends on:** Tasks 1-6 merged (models/__init__.py must be clean first)

**Files:**
- Modify: `app/models/__init__.py` (remove 7 import lines)
- Delete: `app/models/error_report.py`
- Delete: `app/models/ics_classification_cache.py`
- Delete: `app/models/nc_classification_cache.py`
- Delete: `app/models/teams_notification_log.py`
- Delete: `app/models/teams_alert_config.py`
- Delete: `app/models/risk_flag.py`
- Modify: `app/models/intelligence.py` (remove `ReactivationSignal` class)
- Modify: `app/models/trouble_ticket.py` (remove `legacy_error_report_id` column)
- Create: `alembic/versions/XXXX_drop_dead_tables.py`

- [ ] **Step 1: Remove dead model imports from `app/models/__init__.py`**

Remove these lines:
- Line 40: `from .error_report import ErrorReport  # noqa: F401`
- Line 49: `from .ics_classification_cache import IcsClassificationCache  # noqa: F401`
- Line 63: `    ReactivationSignal,` (inside the intelligence import block)
- Line 70: `from .nc_classification_cache import NcClassificationCache  # noqa: F401`
- Line 105: `from .risk_flag import RiskFlag  # noqa: F401`
- Line 132: `from .teams_alert_config import TeamsAlertConfig  # noqa: F401`
- Line 135: `from .teams_notification_log import TeamsNotificationLog  # noqa: F401`

Also remove the section comments that become orphaned (e.g., line 39 `# Error Reports / Trouble Tickets` if ErrorReport was the only import, line 104 `# Risk Flags...`).

- [ ] **Step 2: Delete 5 dead model files**

```bash
rm app/models/error_report.py \
   app/models/ics_classification_cache.py \
   app/models/nc_classification_cache.py \
   app/models/teams_notification_log.py \
   app/models/teams_alert_config.py \
   app/models/risk_flag.py
```

- [ ] **Step 3: Remove `ReactivationSignal` from intelligence.py**

Find the `class ReactivationSignal(Base):` block in `app/models/intelligence.py` (around line 380) and remove the entire class definition.

- [ ] **Step 4: Remove `legacy_error_report_id` from trouble_ticket.py**

Remove line 79 in `app/models/trouble_ticket.py`:
```python
    legacy_error_report_id = Column(Integer)  # traceability to old error_reports
```

- [ ] **Step 5: Generate Alembic migration**

```bash
cd /root/availai
alembic revision --autogenerate -m "drop 7 dead tables and legacy_error_report_id column"
```

Review the generated migration. It should contain:
- `op.drop_table("error_reports")`
- `op.drop_table("reactivation_signals")`
- `op.drop_table("ics_classification_cache")`
- `op.drop_table("nc_classification_cache")`
- `op.drop_table("teams_notification_log")`
- `op.drop_table("teams_alert_config")`
- `op.drop_table("risk_flags")`
- `op.drop_column("trouble_tickets", "legacy_error_report_id")`

- [ ] **Step 6: Verify**

```bash
ruff check app/
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/ -q -o "addopts=" --timeout=60 2>&1 | tail -5
```

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "chore: drop 7 dead database tables and 1 dead column

Tables: error_reports, reactivation_signals, ics_classification_cache,
nc_classification_cache, teams_notification_log, teams_alert_config, risk_flags
Column: trouble_tickets.legacy_error_report_id
All verified — no FKs, no production imports, no raw SQL dependencies."
```

---

## Task 8: Fix Failing Tests

**Depends on:** Tasks 1-7 merged (dead code removal may fix or change failure count)

**Files:** Multiple test files — exact changes depend on root cause analysis per test.

- [ ] **Step 1: Establish baseline failure count**

```bash
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/ -q -o "addopts=" --timeout=60 2>&1 | tail -5
```

Record the exact count after dead code removal.

- [ ] **Step 2: Fix test_core_jobs.py failures (37 → 0)**

Run the first failure with full traceback to identify the root cause:
```bash
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_core_jobs.py -x --tb=long -o "addopts=" 2>&1 | tail -60
```

Apply the fix for the identified AttributeError (likely a model/fixture column rename). After fixing, run:
```bash
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_core_jobs.py -v -o "addopts=" 2>&1 | tail -5
```

Expected: All core_jobs tests pass.

- [ ] **Step 3: Fix test_knowledge_service_comprehensive.py failures (24 → 0)**

```bash
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_knowledge_service_comprehensive.py -x --tb=long -o "addopts=" 2>&1 | tail -60
```

Apply fix. Verify all 24 pass.

- [ ] **Step 4: Fix test_prospect_free_enrichment.py failures (16 → 0)**

```bash
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_prospect_free_enrichment.py -x --tb=long -o "addopts=" 2>&1 | tail -60
```

Apply fix. Verify all 16 pass.

- [ ] **Step 5: Fix test_buyplan_workflow.py failures (11 → 0)**

```bash
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_buyplan_workflow.py -x --tb=long -o "addopts=" 2>&1 | tail -60
```

Apply fix. Verify all 11 pass.

- [ ] **Step 6: Fix test_enrichment_service.py failures (11 → 0)**

Same pattern — run with `-x --tb=long`, identify root cause, fix, verify.

- [ ] **Step 7: Fix test_health_monitor.py failures (7 → 0)**

Same pattern.

- [ ] **Step 8: Fix remaining ~40 individual failures**

Work through remaining failures one test file at a time:
```bash
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/ -q -o "addopts=" --timeout=60 2>&1 | grep "FAILED" | head -20
```

Fix each, verify, continue until 0 failures.

- [ ] **Step 9: Fix parallel isolation issues**

Run with `-n auto` and identify tests that fail only in parallel:
```bash
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/ -q --timeout=30 2>&1 | tail -5
```

Compare failure count. For tests failing only in parallel, add proper fixture isolation (per-test DB cleanup, unique constraint handling).

- [ ] **Step 10: Commit**

```bash
git add -A && git commit -m "fix: resolve all failing tests

Fixed N test failures across M test files.
Root causes: [summarize actual root causes found]"
```

---

## Task 9: New Tests to 85% Coverage

**Depends on:** Task 8 complete (clean test baseline)

**Files:** New test files for untested modules.

- [ ] **Step 1: Measure baseline coverage**

```bash
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/ -o "addopts=--ignore=tests/e2e --ignore=tests/test_browser_e2e.py --timeout=60 --timeout-method=thread" --cov=app --cov-report=term -q --no-header 2>&1 | tail -5
```

Record the TOTAL line percentage.

- [ ] **Step 2: Write tests for sourcing_leads.py (936 lines, 0 tests)**

Create `tests/test_sourcing_leads_service.py` with tests covering:
- Lead creation from sighting data
- Lead scoring and ranking
- Lead status transitions
- Vendor safety computation
- Edge cases (null vendor, zero sightings)

Use `db_session` fixture, mock external services.

- [ ] **Step 3: Write tests for quote_builder.py (276 lines, 0 tests)**

Create `tests/test_quote_builder.py` with tests covering:
- Quote data endpoint
- Quote save endpoint
- Excel/PDF export endpoints
- Error cases (invalid req_id, empty quotes)

Use `client` fixture with auth overrides.

- [ ] **Step 4: Write tests for prospect_pool_service.py + prospect_pool.py (270 lines, 0 tests)**

Create `tests/test_prospect_pool.py` covering claim, dismiss, list, stats endpoints.

- [ ] **Step 5: Expand tests for buyplan_workflow.py (979 lines, ~15% ratio)**

Add to existing `tests/test_buyplan_workflow.py`:
- Submit/approve/reject flows
- PO verification
- Line-level operations
- Error and edge cases

- [ ] **Step 6: Write tests for nc_admin.py (136 lines, 0 tests)**

Create `tests/test_nc_admin.py` covering queue stats, items, force-search, skip endpoints.

- [ ] **Step 7: Write tests for tagging_ai_classify.py (137 lines, 0 tests)**

Create `tests/test_tagging_ai_classify.py` covering classification logic with mocked AI responses.

- [ ] **Step 8: Write tests for enrichment_utils.py (132 lines, 0 tests)**

Create `tests/test_enrichment_utils.py` covering utility functions.

- [ ] **Step 9: Expand htmx_views.py coverage**

Add to `tests/test_htmx_views.py` — focus on untested endpoint groups:
- Customer tab endpoints
- Vendor tab endpoints
- Quote management partials
- Buy plan partials
- Settings/admin partials

- [ ] **Step 10: Measure final coverage**

```bash
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/ -o "addopts=--ignore=tests/e2e --ignore=tests/test_browser_e2e.py --timeout=60 --timeout-method=thread" --cov=app --cov-report=term -q --no-header 2>&1 | grep "^TOTAL"
```

Target: TOTAL line shows >= 85%.

- [ ] **Step 11: Commit**

```bash
git add -A && git commit -m "test: bring coverage to 85%

New test files for sourcing_leads, quote_builder, prospect_pool,
nc_admin, tagging_ai_classify, enrichment_utils.
Expanded coverage for buyplan_workflow and htmx_views."
```

---

## Task 10: Code Quality Fixes

**Depends on:** Task 9 complete

**Files:**
- Modify: `app/services/prospect_free_enrichment.py`
- Modify: `app/services/nc_worker/session_manager.py`
- Modify: `app/services/ics_worker/session_manager.py`
- Modify: `app/models/sourcing.py`
- Modify: `app/routers/excess.py`

- [ ] **Step 1: Fix prospect_free_enrichment.py null-safety**

After the Session guard clause (around line 195), add:

```python
if owns_session:
    db = SessionLocal()
assert db is not None  # narrowed: either passed in or just created
```

- [ ] **Step 2: Fix nc_worker/session_manager.py typing**

Add type annotation to `self._page`:
```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from patchright.async_api import Page

# In __init__:
self._page: Page | None = None
```

Add guard at start of methods that use `self._page`:
```python
if self._page is None:
    raise RuntimeError("Browser not started — call start() first")
```

- [ ] **Step 3: Fix ics_worker/session_manager.py typing**

Same pattern as nc_worker.

- [ ] **Step 4: Fix misleading Legacy comment on customer_name**

In `app/models/sourcing.py`, find the `customer_name` column and remove the `# Legacy` comment (the column is heavily used — 40+ references).

- [ ] **Step 5: Fix excess.py missing template bug**

Read `app/routers/excess.py` around line 155-185 to understand the `partial_import_preview` endpoint. Either:
- (a) Create a minimal `app/templates/htmx/partials/excess/import_preview.html` template, or
- (b) Remove the dead endpoint if it's unreachable from any UI

Determine which by checking if any template references the endpoint URL.

- [ ] **Step 6: Verify**

```bash
ruff check app/ && TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/ -q -o "addopts=" --timeout=60 2>&1 | tail -5
```

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "fix: code quality improvements

- Add null-safety assertion in prospect_free_enrichment.py
- Add Page | None typing + guards in ICS/NC session managers
- Remove misleading Legacy comment on customer_name
- Fix missing import_preview.html template"
```

---

## Verification Checklist

After all tasks are complete:

- [ ] `ruff check app/` — passes
- [ ] `TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/ -q --timeout=30` — 0 failures
- [ ] `TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/ -o "addopts=..." --cov=app` — >= 85%
- [ ] `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` — migration works
- [ ] No dead imports: `ruff check app/ --select F401`
