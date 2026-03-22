# On-Demand User-Reviewed Enrichment System

**Date:** 2026-03-22
**Status:** Approved
**Priority:** Customers > Materials > Vendors

## Problem

The current enrichment system runs automatically in the background — 7 scheduler jobs, auto-triggers on entity creation, and silent enrichment on every search. This wastes API credits on inactive/irrelevant data, produces hallucinated results with no user verification, and targets 95% confidence which is insufficient for production data quality.

## Solution

Replace all auto-enrichment with an on-demand, user-initiated pipeline that:
- Fires ALL available sources in parallel for maximum coverage
- Uses Claude Sonnet/Opus as a verification layer with web search
- Requires 2+ independent source agreement for 98% confidence
- Presents results inline as a per-field diff for user review before saving
- Never writes enrichment data without explicit user approval

## Design Principles

1. **No background enrichment** — every enrichment is user-triggered
2. **Quality over quantity** — spend more tokens/credits per enrichment to ensure accuracy
3. **Hallucination prevention** — multi-source cross-validation + citation requirements
4. **User is final authority** — per-field approve/edit/remove before any data is saved
5. **Focus on active data** — only enrich what users are actively working with

---

## Phase 1: Remove Auto-Enrichment

### Scheduler Jobs to Remove

| File | Job | Interval |
|------|-----|----------|
| `app/jobs/tagging_jobs.py` | `ai_tagging` | 30min |
| `app/jobs/tagging_jobs.py` | `internal_confidence_boost` | 4h |
| `app/jobs/tagging_jobs.py` | `prefix_backfill` | 2h |
| `app/jobs/tagging_jobs.py` | `sighting_mining` | 2h |
| `app/jobs/tagging_jobs.py` | `material_enrichment` | 2h |
| `app/jobs/prospecting_jobs.py` | `enrich_pool` | monthly |
| `app/jobs/prospecting_jobs.py` | `find_contacts` | monthly |
| `app/jobs/email_jobs.py` | `email_reverification` | quarterly |
| `app/jobs/core_jobs.py` | dead `_job_batch_enrich_materials` + `_job_poll_material_batch` (lines 237-275) | (commented out — delete dead functions, keep other active jobs in file) |
| `app/jobs/lifecycle_jobs.py` | dead `_job_lifecycle_sweep` (lines 57-79) | (commented out — delete dead function, keep file if other jobs exist) |

### Router Auto-Triggers to Remove

| File | Lines | Trigger |
|------|-------|---------|
| `app/routers/crm/companies.py` | 489-545 | Auto-enrich on company creation |
| `app/routers/crm/sites.py` | 40-58 | Auto-enrich on site creation |
| `app/routers/crm/offers.py` | 302, 322-326, 441-445 | Auto-enrich vendor on offer creation |
| `app/routers/materials.py` | 524-534 | Auto-enrich vendor on stock import |
| `app/routers/vendor_contacts.py` | 601-611 | Auto-enrich vendor on contact add |
| `app/routers/crm/enrichment.py` | 51-64 | Customer waterfall on manual enrich |
| `app/search_service.py` | 217, 1641-1678 | Auto-enrich material on every search |

### Config Changes

**Remove:**
- `customer_enrichment_enabled`
- `customer_enrichment_cooldown_days`
- `customer_enrichment_contacts_per_account`
- `material_enrichment_enabled`
- `material_enrichment_batch_size`

**Add:**
- `on_demand_enrichment_timeout: int = 45`

---

## Phase 2: Model Changes

### EnrichmentQueue — 6 New Columns

```python
# Added to existing EnrichmentQueue model in app/models/enrichment.py
material_card_id = Column(Integer, ForeignKey("material_cards.id", ondelete="CASCADE"))
session_id = Column(String(100), index=True)       # UUID linking fields in one enrichment run
source_citations = Column(JSONB, default=list)      # [{url, title}] for verifiability
reasoning = Column(Text)                             # Claude's per-field explanation
agreement_count = Column(Integer, default=1)        # How many sources agreed
conflict_details = Column(JSONB)                     # What each source said when disagreeing
```

New indexes: `ix_eq_session` on `session_id`, `ix_eq_material` on `material_card_id`.

Also alter `EnrichmentQueue.proposed_value` to `nullable=True` (currently NOT NULL) — needed because the verification layer sets unverifiable fields to null.

**Note:** MaterialCard uses `enriched_at` (not `last_enriched_at` like Company/VendorCard). The `apply_approved_fields` function must handle this difference — use `enriched_at` for materials, `last_enriched_at` for companies/vendors. No column rename needed.

---

## Phase 3: Core Services

### `app/services/enrichment_verifier.py` (new)

The Claude verification layer. Separated for independent testing.

**Interface:**
```python
async def verify_company_fields(
    name: str,
    domain: str,
    source_results: dict[str, dict],  # {source_name: raw_result}
) -> list[VerifiedField]

async def verify_material_fields(
    mpn: str,
    manufacturer_hint: str | None,
    connector_results: dict[str, dict],
) -> list[VerifiedField]
```

**Verification prompt instructions:**
- Cross-check each field across all source results
- For disagreements: flag `contradiction=True`, explain which source is likely wrong and why
- Provide `citations` list with at least one verifiable URL per field
- Never return a value without a verifiable source — set to null if unverifiable
- Use web search tool (up to 8 queries) for deep research

**Confidence scoring (deterministic code, NOT Claude output):**
```python
def _calculate_confidence(
    field: str,
    source_values: list[tuple[str, str]],  # [(source_name, value), ...]
    has_citation: bool,
    contradiction: bool,
) -> float:
    if contradiction:
        return 0.0   # triggers "conflict" status — user must resolve
    # Count how many sources agree on the most common value
    from collections import Counter
    values = [v for _, v in source_values if v]
    if not values:
        return 0.0
    most_common_count = Counter(values).most_common(1)[0][1]
    if most_common_count >= 2:
        return min(0.98, 0.90 + (most_common_count - 2) * 0.02)
    return 0.85 if has_citation else 0.65
```

### `app/services/deep_enrichment_pipeline.py` (new)

Orchestrates all sources in parallel, runs verification, stages results.

**Interface:**
```python
async def run_company_enrichment(company_id: int, db: Session) -> EnrichmentStageResult
async def run_vendor_enrichment(card_id: int, db: Session) -> EnrichmentStageResult
async def run_material_enrichment(card_id: int, db: Session) -> EnrichmentStageResult
async def apply_approved_fields(
    session_id: str,
    approved_fields: list[ApprovalItem],
    user_id: int,
    db: Session,
) -> ApplyResult
```

**Data types:**
```python
@dataclass
class EnrichmentStageResult:
    session_id: str           # UUID for this enrichment run
    entity_type: str          # "company" | "vendor" | "material"
    entity_id: int
    fields: list[StagedField]
    sources_fired: list[str]
    sources_succeeded: list[str]
    elapsed_seconds: float
    error: str | None

@dataclass
class StagedField:
    field_name: str
    current_value: str | None
    proposed_value: str
    confidence: float
    sources: list[str]
    source_citations: list[dict]  # [{url: str, title: str}] — matches JSONB model column
    reasoning: str
    status: str               # "proposed" | "conflict" | "no_change"
    agreement_count: int

@dataclass
class ApprovalItem:
    queue_id: int
    approved_value: str | None  # null = reject

@dataclass
class ApplyResult:
    applied: int
    rejected: int
    entity_id: int
```

**Pipeline for company/vendor:**
1. `asyncio.gather()` fires: Explorium + Claude AI with web search (max 8 queries)
2. Normalize all results through `normalize_company_output()`
3. `enrichment_verifier.verify_company_fields()` cross-checks with Claude Sonnet + web search
4. Confidence scored by `_calculate_confidence()` (code, not Claude)
5. Each field written to `EnrichmentQueue` with shared `session_id`

**Pipeline for materials:**
1. `asyncio.gather()` fires: DigiKey, Mouser, Element14, OEMSecrets, Nexar + prefix lookup + sighting mining
2. `enrichment_verifier.verify_material_fields()` with Claude Sonnet + web search
3. Same confidence scoring and staging logic

**Timeout strategy:**
- Per-source: 12s individual timeout via `asyncio.wait_for()`
- All sources: `asyncio.gather(..., return_exceptions=True)` — one failure doesn't block others
- Outer endpoint: `asyncio.wait_for(..., timeout=45)` — on timeout, return partial results
- Partial results are still staged in EnrichmentQueue, still reviewable

**Reused infrastructure:**
- `app/enrichment_service.py` — Explorium/AI source functions (`_explorium_find_company`, `_ai_find_company`), normalization, apply functions
- `app/utils/claude_client.py` — `claude_structured()` with thinking budget support

**Modified infrastructure:**
- `app/services/enrichment_orchestrator.py` — existing `fire_all_sources()` uses hardcoded `COMPANY_SOURCES` dict with deprecated providers (Apollo, Clearbit). Must be updated to use current source set (Explorium + Claude AI). Move from "Reused As-Is" to "Modified Files".

---

## Phase 4: API Endpoints

### New v2 Endpoints (in `app/routers/crm/enrichment.py`)

**Step 1 — Trigger enrichment:**
```
POST /api/enrich/v2/company/{company_id}
POST /api/enrich/v2/vendor/{card_id}
POST /api/enrich/v2/material/{card_id}
```
- Auth: `require_user` dependency
- Returns: HTMX partial (diff panel HTML) swapped inline
- Timeout: 45s, returns partial results on timeout
- Creates `EnrichmentQueue` rows with `status="pending"`

**Step 2 — Apply approved fields:**
```
POST /api/enrich/v2/apply
Body: {session_id: str, approvals: [{queue_id: int, approved_value: str | null}]}
```
- Security: verifies each `queue_id` belongs to a row linked to an entity the user can edit
- `approved_value` = string → write to entity, set queue row `status="applied"`
- `approved_value` = null → set queue row `status="rejected"`
- Updates `last_enriched_at` and `enrichment_source` on entity
- Returns: `{applied: N, rejected: N, entity_id: N}`

**Step 3 — Diff panel partial (for re-rendering):**
```
GET /v2/partials/enrich/review/{session_id}
```
- Returns the diff panel HTML for a given session
- Used for initial render and for returning to a pending review

### Existing Endpoints

Keep `POST /api/enrich/company/{id}` and `POST /api/enrich/vendor/{id}` but deprecate them. They should NOT auto-apply anymore — redirect internally to v2 flow.

---

## Phase 5: Frontend — Inline Diff Panel

### Template: `app/templates/htmx/partials/shared/enrich_diff_panel.html`

**Loading state:**
- Pulsing skeleton rows replacing the enrich button area
- Progress text: "Searching sources... Cross-validating... Verifying with AI..."
- HTMX `hx-indicator` on the enrich button, 45s timeout configured via `hx-request`

**Diff table (Alpine.js local state):**

| Field | Current | Enriched | Confidence | Sources | Action |
|-------|---------|----------|------------|---------|--------|
| Domain | — | acme.com | ████ 98% | Explorium, Web | [✓] [Edit] [✗] |
| Industry | Electronics | Electronic Components | ███░ 96% | Explorium, AI | [✓] [Edit] [✗] |

**Visual encoding:**
- Confidence bar: thin horizontal fill — green (95%+), amber (85-94%), red (<85%)
- Source pills: small colored tags per source
- Current value: muted text, strikethrough when different from enriched
- Enriched value: bold
- Conflict rows: red highlight, no pre-filled value, user must type resolution
- Low-confidence fields (<65%): pre-unchecked checkbox, warning badge
- Source citations: clickable links per field
- "View reasoning" expandable per row (`x-show` toggle)

**Edit mode:** Clicking Edit transforms the enriched cell into an inline text input.

**Sticky footer:**
- "Save Approved (N fields)" button — HTMX POST to `/api/enrich/v2/apply`
- "Discard All" button — clears the panel, no server call needed

**HTMX flow (single long-running request, NOT two-phase):**

The v2 enrichment endpoint is a single async FastAPI request that takes 10-30s. HTMX handles this natively — the button shows a spinner via `hx-indicator` while waiting, then the full diff panel HTML is returned as the response and swapped in. No SSE, no polling, no skeleton-then-replace.

1. "Enrich Now" click → `hx-post` fires, button shows spinner via `hx-indicator`, `hx-request='{"timeout": 45000}'`
2. Server runs full pipeline (10-30s) → returns complete diff panel HTML
3. HTMX swaps response into `#enrich-results-{entity_id}` (existing target div on detail pages)
4. All approve/edit/remove happens client-side in Alpine.js (no server round-trips)
5. "Save Approved" → POST with JSON of approvals → success confirmation replaces panel

**Note:** If Caddy proxy timeout is an issue (default 30s), add `request_timeout 60s` to the Caddyfile for `/api/enrich/v2/*` routes.

**Dedup guard:** The enrich button is disabled via Alpine.js `x-bind:disabled="enriching"` during the request. Server-side, the endpoint checks for an existing pending `EnrichmentQueue` session for the same entity within the last 5 minutes and returns it instead of re-running.

### Enrich Button Updates

Update `app/templates/htmx/partials/shared/enrich_button.html`:
- `hx-post` target: `/api/enrich/v2/{entity_type}/{entity_id}`
- `hx-target`: `#enrich-results-{{ entity_id }}` (matches existing target div ID on detail pages)
- `hx-request`: `{"timeout": 45000}`

### Page Integration

Detail pages already have `<div id="enrich-results-{{ entity_id }}">` swap targets (used by the existing enrich button). The new diff panel will swap into these existing divs — no page template changes needed for the swap target itself.

---

## Phase 6: Hallucination Prevention

1. **Multi-source cross-validation** — 2+ sources must agree for 98% confidence
2. **Citation requirement** — Claude must provide verifiable URLs for every field; fields without citations get warning badges and are pre-unchecked
3. **Deterministic confidence scoring** — confidence is computed by Python code examining source agreement counts, NOT by Claude's self-assessment
4. **Null over guess** — verification prompt explicitly instructs: "If you cannot find a verifiable source for a value, set it to null — do not guess"
5. **Human review gate** — nothing is saved without explicit user approval per field
6. **Conflict surfacing** — contradictions between sources are flagged in red, user must manually resolve

---

## Phase 7: Testing

| Test File | What It Tests |
|-----------|---------------|
| `tests/test_enrichment_verifier.py` | `_calculate_confidence()` pure function, mock Claude responses, citation extraction |
| `tests/test_deep_enrichment_pipeline.py` | Full pipeline with mocked sources, EnrichmentQueue staging, partial timeout results |
| `tests/test_enrichment_router_v2.py` | v2 endpoints, approval flow, security checks |

All tests follow existing `conftest.py` patterns: in-memory SQLite, no real API calls. External sources mocked at module boundary.

---

## Build Sequence

1. **Remove auto-triggers** — 15 removal points across routers + jobs + config. Run tests.
2. **Alembic migration** — 6 new columns on EnrichmentQueue.
3. **Build enrichment_verifier.py** — pure verification logic + confidence scoring + tests.
4. **Build deep_enrichment_pipeline.py** — orchestration + staging + tests.
5. **Add v2 API endpoints** — trigger, apply, partial render + tests.
6. **Build diff panel template** — enrich_diff_panel.html + update enrich_button.html.
7. **Wire into pages** — customer detail, material detail, vendor detail.
8. **Config cleanup** — remove deprecated flags.
9. **End-to-end testing** — full flow from button click to approved save.

---

## Files Summary

### New Files
- `app/services/deep_enrichment_pipeline.py`
- `app/services/enrichment_verifier.py`
- `app/templates/htmx/partials/shared/enrich_diff_panel.html`
- `tests/test_enrichment_verifier.py`
- `tests/test_deep_enrichment_pipeline.py`
- `tests/test_enrichment_router_v2.py`
- Alembic migration (generated)

### Modified Files
- `app/models/enrichment.py` — 6 new columns on EnrichmentQueue
- `app/routers/crm/enrichment.py` — v2 endpoints
- `app/routers/crm/companies.py` — remove auto-enrich (lines 489-545)
- `app/routers/crm/sites.py` — remove auto-enrich (lines 40-58)
- `app/routers/crm/offers.py` — remove auto-enrich (lines 302, 322-326, 441-445)
- `app/routers/materials.py` — remove auto-enrich (lines 524-534)
- `app/routers/vendor_contacts.py` — remove auto-enrich (lines 601-611)
- `app/search_service.py` — remove auto-enrich (lines 217, 1641-1678)
- `app/jobs/tagging_jobs.py` — remove all scheduler registrations
- `app/jobs/prospecting_jobs.py` — remove enrichment jobs
- `app/jobs/email_jobs.py` — remove reverification job
- `app/jobs/core_jobs.py` — remove dead functions
- `app/jobs/lifecycle_jobs.py` — remove dead functions
- `app/config.py` — remove 5 flags, add timeout
- `app/templates/htmx/partials/shared/enrich_button.html` — update to v2
- `app/templates/htmx/partials/materials/enrich_result.html` — use diff panel
- `app/services/enrichment_orchestrator.py` — update `COMPANY_SOURCES` dict to use current providers (Explorium + Claude AI), remove deprecated Apollo/Clearbit refs

### Reused As-Is
- `app/enrichment_service.py` — Explorium/AI source functions, normalization, apply functions
- `app/utils/claude_client.py` — claude_structured with thinking support

### Error Response Format

v2 enrichment endpoints return HTML partials on success (for HTMX swap). On error, they return an HTML error fragment (not JSON) that renders an inline error message in the diff panel area. This follows the existing HTMX error pattern — the error partial includes a "Retry" button. The standard JSON error format (`{"error": "message", "status_code": N}`) is NOT used for these HTMX endpoints.

### Credit Tracking

Each source call in the pipeline should update `EnrichmentCreditUsage` via the existing credit manager. The diff panel footer should show total credits consumed for the enrichment run (informational, not blocking).
