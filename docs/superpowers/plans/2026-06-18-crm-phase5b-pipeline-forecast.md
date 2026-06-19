# CRM Phase 5b — Pipeline / Forecast Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an honest, real-time pipeline/forecast rollup to the Reporting section — the Requisition IS the opportunity (value × stage win-probability), rolled up by account and by owner, plus an interactions→RFQs→quotes→orders conversion funnel.

**Architecture:** A new `app/services/forecast_service.py` computes all rollups from `Requisition` + `Quote` data (no new tables — the Requisition is the opportunity, per the locked CRM decision). The existing `reporting_dashboard` route (`app/routers/crm/views.py`) is extended to also build the forecast context, and a new `reporting/pipeline.html` partial (included by `reporting/dashboard.html`) renders it. Forecast dollars reuse the canonical `_resolve_deal_value` so they reconcile with the requisition list. Lives in Reporting only — never the daily hub (locked decision).

**Tech Stack:** FastAPI, SQLAlchemy 2.0 (classic `Column()` style), Jinja2 partials, HTMX, Alpine.js, Tailwind. Python 3.13 (CI) / 3.12 local. In-memory SQLite for tests.

## Global Constraints

- **No migration.** The Requisition is the opportunity; reuse `Requisition.opportunity_value`, `.status`, `.company_id`, `.claimed_by_id`, `.created_by`, and the `Quote` relationship. Do NOT add a Deal/Opportunity model or any column.
- **Win-probability is a documented, tunable constant** named `STAGE_WIN_PROBABILITY` in `forecast_service.py`. Use EXACTLY these values (standard CRM stage-weighting):
  `draft`=0.05, `active`=0.10, `sourcing`=0.25, `offers`=0.40, `quoting`=0.60, `quoted`=0.75, `reopened`=0.50, `won`=1.00, `lost`=0.0, `archived`=0.0, `cancelled`=0.0.
- **Open** = non-terminal = statuses with `0.0 < probability < 1.0` (draft, active, sourcing, offers, quoting, quoted, reopened). `won` is realized (reported separately as "won this period"); `lost`/`archived`/`cancelled` are dead (excluded).
- **Forecast dollar basis** = the canonical deal value from `app.services.requisition_list_service._resolve_deal_value(opportunity_value, priced_sum, priced_count, requirement_count)`, so forecast totals reconcile with what the requisition list shows. Requisitions whose resolved value is `None` contribute `$0` to dollar figures but still count in counts/funnel. Compute the per-requisition priced_sum/priced_count/requirement_count in BULK (grouped queries over `Requirement`), NOT per-req in a loop (no N+1).
- **Owner** = `claimed_by_id` (the rep working the req); requisitions with `claimed_by_id IS NULL` roll up under a single "Unassigned" bucket. Resolve owner display name from `User.name or User.email`.
- All money is rendered as whole-dollar USD with thousands separators (e.g. `$1,250,000`). Probabilities render as integer percents (e.g. `60%`).
- Reuse the existing brand palette / Tailwind classes and the visual idiom of `reporting/dashboard.html` and `crm/performance_tab.html`. No new external JS libs.
- The pipeline section must be lazy-load-safe: it is rendered inline inside the reporting partial (same request), reusing the already-loaded `db` session.

---

### Task 1: forecast_service — rollup computations

**Files:**
- Create: `app/services/forecast_service.py`
- Test: `tests/test_forecast_service.py`

**Interfaces:**
- Consumes: `app.models.sourcing.Requisition`, `app.models.sourcing.Requirement`, `app.models.crm.Quote` (or wherever `Quote` lives — confirm via import used in `requisition_list_service.py`), `app.models.auth.User`, `app.models.crm.Company`, `app.constants.RequisitionStatus`, `app.services.requisition_list_service._resolve_deal_value`.
- Produces:
  - `STAGE_WIN_PROBABILITY: dict[str, float]` (module constant, exact values from Global Constraints)
  - `OPEN_STATUSES: frozenset[str]` (derived: statuses with `0.0 < p < 1.0`)
  - `stage_probability(status: str | None) -> float`
  - `bulk_deal_values(db, req_ids: list[int]) -> dict[int, float]` — resolved deal value per req id (0.0 when `_resolve_deal_value` returns None)
  - `pipeline_summary(db, *, owner_id: int | None = None) -> dict` with keys: `open_value` (float), `weighted_value` (float), `open_count` (int), `won_value` (float), `won_count` (int), `lost_count` (int), `win_rate` (float 0..1), `by_stage` (list of `{status, label, count, value, weighted}` for OPEN stages, ordered by the lifecycle order draft→reopened)
  - `pipeline_by_account(db, *, limit: int = 10) -> list[dict]` — `{company_id, company_name, open_count, open_value, weighted_value}` for accounts with open reqs, sorted by `weighted_value` desc
  - `pipeline_by_owner(db) -> list[dict]` — `{owner_id, owner_name, open_count, open_value, weighted_value, won_value}` sorted by `weighted_value` desc; null owner → `owner_id=None, owner_name="Unassigned"`
  - `conversion_funnel(db, *, days: int = 90) -> dict` — counts over requisitions created within `days`: `{opportunities, sourcing, quoted, won}` where `sourcing` = status not in {draft, active}, `quoted` = has ≥1 Quote OR status in {quoted, won}, `won` = status == won. Include `window_days` echoing `days`.

- [ ] **Step 1: Write failing tests** for `stage_probability` (known + unknown status → 0.0), `pipeline_summary` (build 3 reqs with known statuses + opportunity_values, assert open/weighted/won/win_rate math), `pipeline_by_account`/`pipeline_by_owner` (grouping + Unassigned bucket), `conversion_funnel` (status progression + quote presence). Use the `db_session` fixture; create `Requisition`/`Quote`/`Company`/`User` rows directly. Assert exact weighted math, e.g. one `sourcing` req with value 100000 → `weighted_value == 25000.0`.

- [ ] **Step 2: Run tests, verify they fail** (`ModuleNotFoundError` / `AttributeError`).

- [ ] **Step 3: Implement `forecast_service.py`.** Constant + derivations first, then bulk deal-value helper (grouped `Requirement` query like `requisition_list_service` does, applying `_resolve_deal_value`), then the four rollups using bulk values. Keep queries to a small constant count (no per-req loops hitting the DB).

- [ ] **Step 4: Run tests, verify they pass.**

- [ ] **Step 5: Commit** (`feat(crm): forecast_service — pipeline/forecast rollups (P5b)`).

---

### Task 2: Reporting route + pipeline partial (UI)

**Files:**
- Modify: `app/routers/crm/views.py` (the `reporting_dashboard` route — add forecast context)
- Create: `app/templates/htmx/partials/reporting/pipeline.html`
- Modify: `app/templates/htmx/partials/reporting/dashboard.html` (include the pipeline partial as a new section above Performance)
- Test: `tests/test_reporting.py` (extend — add a `TestPipelineSection` class)

**Interfaces:**
- Consumes: `app.services.forecast_service` (Task 1).
- Produces: the `reporting_dashboard` context gains `pipeline` (from `pipeline_summary`), `pipeline_accounts` (from `pipeline_by_account`), `pipeline_owners` (from `pipeline_by_owner`), `funnel` (from `conversion_funnel`).

- [ ] **Step 1: Write failing tests** in `tests/test_reporting.py`: `GET /v2/partials/reporting` returns 200 and the HTML contains a "Pipeline" / "Forecast" header, an "Open Pipeline" figure, a "Weighted Forecast" figure, and a "Win Rate" figure; with one seeded `sourcing` requisition of value 100000, the rendered weighted forecast shows `$25,000`. Add a test that the funnel section renders the four stage labels (Opportunities / Sourcing / Quoted / Won).

- [ ] **Step 2: Run tests, verify they fail** (text not present).

- [ ] **Step 3: Implement.** Extend `reporting_dashboard` to build the four forecast context keys. Create `reporting/pipeline.html` with: a 4-card top strip (Open Pipeline, Weighted Forecast, Won this period, Win Rate), a by-stage list (status label, count, value, weighted — visual bar widths proportional to weighted), a top-accounts table, an owner-leaderboard table (only when >1 owner or any non-null owner), and the conversion funnel (4 horizontal stages with counts + conversion %). Include it in `dashboard.html` as the first `<section>` (above Performance) so management sees the forecast first. Render money/percent per Global Constraints. Use the brand palette.

- [ ] **Step 4: Run tests, verify they pass.** Also run `tests/test_reporting.py` whole-file and `tests/test_static_analysis.py` (template guards).

- [ ] **Step 5: Commit** (`feat(crm): pipeline/forecast section in Reporting (P5b)`).

---

## Self-Review notes
- Spec coverage: roadmap Phase 5b = "pipeline/forecast — requisition-as-opportunity rollup (value × win-probability), account & team level" (Tasks 1+2) + "outcome correlation: interactions → RFQs → quotes → orders" (conversion_funnel). Covered.
- No migration (Global Constraint). No new model.
- Type consistency: `pipeline_summary` keys are consumed verbatim by `pipeline.html`; owner null → "Unassigned" handled in both service and template.
