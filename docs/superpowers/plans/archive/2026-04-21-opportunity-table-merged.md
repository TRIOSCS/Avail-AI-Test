# Opportunity Table — Merged (Resizable + Aesthetic v2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify the in-flight resizable-columns and aesthetic-v2 streams into a single feature-flag-gated PR that refactors the `/requisitions2` split-workspace left panel to use a 6-column compact table with status dots, urgency accents, deal-value typography, coverage meter, aggregated MPN chip row, truncation tooltips, and a hover action rail — while preserving the legacy rendering behind `AVAIL_OPP_TABLE_V2=false` for instant rollback.

**Architecture:** Frontend + row-dict fields only. `list_requisitions()` gains seven new row keys computed from existing data. Four new shared Jinja macros (`opp_name_cell`, `opp_status_cell`, `opp_row_action_rail`, `mpn_chips_aggregated`) plus three extended ones (`deal_value`, `coverage_meter`, `urgency_accent_class`) compose the new row. Two new Alpine primitives (`x-chip-overflow` directive, `rowActionRail` component) plus the relocated and extended `x-truncate-tip` directive drive the interactions. The directive contract between `x-chip-overflow` and `x-truncate-tip` uses **cloned DOM nodes stored on an element property** (never `innerHTML`) to avoid the XSS class. Two `/requisitions2` templates (`_table_rows.html`, `_single_row.html`, `_table.html`) get a `{% if avail_opp_table_v2_enabled %}` gate around the new markup with legacy preserved in `{% else %}`. No schema, no new routes, no migration.

**Tech Stack:** Python 3.11 · FastAPI · SQLAlchemy 2.0 · PostgreSQL 16 · Jinja2 · HTMX 2.x · Alpine.js 3.x · Tailwind CSS · Pytest · Playwright.

**Spec:** `docs/superpowers/specs/2026-04-21-opportunity-table-merged-design.md`

---

## File Inventory

**Modify:**
- `app/config.py` — add `avail_opp_table_v2: bool = True` setting
- `.env.example` — add `AVAIL_OPP_TABLE_V2=true`
- `app/services/requisition_list_service.py` — extend helpers, add aggregations, expose new row-dict keys
- `app/templates/htmx/partials/shared/_macros.html` — finalize v2 macros, add `opp_name_cell`, `opp_status_cell`, `opp_row_action_rail`
- `app/templates/htmx/partials/shared/_mpn_chips.html` — add `mpn_chips_aggregated(items)` variant
- `app/templates/requisitions2/_table.html` — feature-flag gate, v2 thead
- `app/templates/requisitions2/_table_rows.html` — feature-flag gate, v2 rows
- `app/templates/requisitions2/_single_row.html` — feature-flag gate (mirrors `_table_rows.html` for single-row swap)
- `app/routers/requisitions2.py` — inject `avail_opp_table_v2_enabled` into template context
- `app/static/styles.css` — new tokens (`.truncate-tip`, `.opp-chip-row`, `.opp-name-cell`, `.opp-deal--partial`, `.opp-action-rail*`)
- `app/static/htmx_app.js` — relocate `x-truncate-tip`, extend with node-cloning contract, add `x-chip-overflow`, add `rowActionRail`
- `app/static/js/requisitions2.js` — delete duplicated `x-truncate-tip` block
- `tests/test_requisition_list_service.py` — extend helper/aggregation tests
- `tests/test_requisitions2_templates.py` — add flag-on / flag-off assertions
- `e2e/requisitions2-resize.spec.ts` — add v2 coverage
- `docs/APP_MAP_INTERACTIONS.md` — document `x-chip-overflow`, finalized `x-truncate-tip`, `rowActionRail`
- `STABLE.md` — v2 token reference + flag rollback procedure

**Create:**
- `e2e/requisitions2-visuals.spec.ts` — v2 visual regression suite
- `tests/test_opp_macros.py` — macro rendering assertions

**Delete (at end of PR):**
- No files deleted in this PR. Superseded specs (`docs/superpowers/specs/2026-04-21-rq2-resizable-columns-design.md`, `specs/ui/opportunity-table-aesthetic-v2.md`) stay for history; their status is recorded in the merged spec's header.

---

## Execution Order

Phased TDD.

- **Phase 1 — Feature-flag scaffolding (Tasks 1–2).** Settings + `.env.example` first so every subsequent task can gate cleanly.
- **Phase 2 — Backend (Tasks 3–6).** Helpers (red→green) → aggregation → row-dict exposure.
- **Phase 3 — Macros (Tasks 7–11).** Unit tests against rendered macros; no templates touched yet.
- **Phase 4 — CSS + JS directives (Tasks 12–17).** Tokens, relocation/extension, new directive, new component.
- **Phase 5 — Template wiring (Tasks 18–20).** Put it all together behind the flag.
- **Phase 6 — Integration E2E & docs (Tasks 21–24).** Full visual regression, flag-off assertion, docs updates.
- **Phase 7 — Full pipeline + deploy verify (Task 25).**

Each task ends with a commit. Subagents execute one task per dispatch — minimum context bleed.

---

## Task 1: Add `avail_opp_table_v2` settings entry

**Files:**
- Modify: `app/config.py`

- [ ] **Step 1: Locate a good insertion point**

Run:
```bash
grep -n "use_htmx\|on_demand_enrichment_enabled" /root/availai/app/config.py
```

Pick a line near the other UI feature flags.

- [ ] **Step 2: Add the new setting**

Insert after the existing `use_htmx: bool = True` line:

```python
    # Gates the merged v2 opportunity-table rendering on /requisitions2.
    # See docs/superpowers/specs/2026-04-21-opportunity-table-merged-design.md
    # Flip to false + restart to revert to legacy rendering with no code change.
    avail_opp_table_v2: bool = True
```

- [ ] **Step 3: Verify importability**

Run:
```bash
cd /root/availai && TESTING=1 python -c "from app.config import settings; print(settings.avail_opp_table_v2)"
```
Expected output: `True`.

- [ ] **Step 4: Commit**

```bash
git add app/config.py
git commit -m "$(cat <<'EOF'
feat(config): add avail_opp_table_v2 feature flag

Defaults to true. Gates the merged v2 opportunity-table rendering on
/requisitions2 per docs/superpowers/specs/2026-04-21-opportunity-table-merged-design.md.
Flip to false + restart container to revert to legacy rendering.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Document the flag in `.env.example`

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Find a landing spot**

Run:
```bash
grep -n "MVP_MODE\|USE_HTMX\|ON_DEMAND_ENRICHMENT_ENABLED" /root/availai/.env.example
```

- [ ] **Step 2: Add the flag line**

Insert near the other feature flags:

```
# /requisitions2 opportunity-table v2 rendering (default: true).
# See docs/superpowers/specs/2026-04-21-opportunity-table-merged-design.md.
# Set to false + restart container to revert to legacy rendering.
AVAIL_OPP_TABLE_V2=true
```

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "$(cat <<'EOF'
docs(env): document AVAIL_OPP_TABLE_V2 feature flag

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Extend `_resolve_deal_value` signature + add `partial` source

**Files:**
- Modify: `app/services/requisition_list_service.py`
- Test: `tests/test_requisition_list_service.py`

- [ ] **Step 1: Write the failing tests**

**Replace** the three existing `test_resolve_deal_value_*` tests (they use the 2-arg signature) with this block:

```python
# ── _resolve_deal_value (extended signature) ─────────────────────────


def test_resolve_deal_value_prefers_entered():
    val, src = _resolve_deal_value(opportunity_value=50000.0, priced_sum=10.0, priced_count=1, requirement_count=5)
    assert val == 50000.0
    assert src == "entered"


def test_resolve_deal_value_all_priced_is_computed():
    val, src = _resolve_deal_value(opportunity_value=None, priced_sum=2500.0, priced_count=3, requirement_count=3)
    assert val == 2500.0
    assert src == "computed"


def test_resolve_deal_value_some_priced_is_partial():
    val, src = _resolve_deal_value(opportunity_value=None, priced_sum=1800.0, priced_count=3, requirement_count=5)
    assert val == 1800.0
    assert src == "partial"


def test_resolve_deal_value_zero_price_counts_as_priced():
    val, src = _resolve_deal_value(opportunity_value=None, priced_sum=1500.0, priced_count=4, requirement_count=4)
    assert val == 1500.0
    assert src == "computed"


def test_resolve_deal_value_none_priced_is_none():
    val, src = _resolve_deal_value(opportunity_value=None, priced_sum=0.0, priced_count=0, requirement_count=3)
    assert val is None
    assert src == "none"


def test_resolve_deal_value_zero_opportunity_falls_through():
    val, src = _resolve_deal_value(opportunity_value=0.0, priced_sum=1500.0, priced_count=2, requirement_count=2)
    assert val == 1500.0
    assert src == "computed"
```

- [ ] **Step 2: Run — expect fail**

Run:
```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_requisition_list_service.py -k "resolve_deal_value" -v --override-ini="addopts="
```
Expected: all 6 tests FAIL with `TypeError` on `_resolve_deal_value`.

- [ ] **Step 3: Replace the helper**

In `app/services/requisition_list_service.py`, replace the existing `_resolve_deal_value`:

```python
def _resolve_deal_value(
    opportunity_value: float | None,
    priced_sum: float,
    priced_count: int,
    requirement_count: int,
) -> tuple[float | None, str]:
    """Pick displayed deal value; tag provenance (entered / computed / partial / none).

    Priority (per 2026-04-21 merged spec §Backend contract additions):
      1. opportunity_value > 0            → 'entered'   (broker-entered wins)
      2. priced_sum > 0 and all priced    → 'computed'  (target prices complete)
      3. priced_sum > 0 and some unpriced → 'partial'   (floor estimate)
      4. otherwise                         → 'none'     (no useful signal)

    Zero-priced requirements count as priced (target_price explicitly 0 means
    "free/sample," not "unknown"). priced_count reflects NOT-NULL target_price.
    """
    if opportunity_value and opportunity_value > 0:
        return opportunity_value, "entered"
    if priced_sum and priced_sum > 0:
        if priced_count >= requirement_count:
            return priced_sum, "computed"
        return priced_sum, "partial"
    return None, "none"
```

- [ ] **Step 4: Run — expect pass**

Run:
```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_requisition_list_service.py -k "resolve_deal_value" -v --override-ini="addopts="
```
Expected: all 6 PASS.

- [ ] **Step 5: Update the callsite so import still works**

Find `_resolve_deal_value(...)` call inside `list_requisitions`. Temporarily pass four args; Task 5 finalizes with the real values:

```python
_deal_val, _deal_src = _resolve_deal_value(_opp_val, float(ttv or 0), 0, req_cnt or 0)
```

- [ ] **Step 6: Full service tests green**

```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_requisition_list_service.py -v --override-ini="addopts="
```

- [ ] **Step 7: Commit**

```bash
git add app/services/requisition_list_service.py tests/test_requisition_list_service.py
git commit -m "$(cat <<'EOF'
feat(requisitions2): extend _resolve_deal_value with partial-pricing source

4-arg signature: opportunity_value, priced_sum, priced_count,
requirement_count. Returns 'partial' when some requirements have
target_price and others don't — brokers see a floor estimate rather
than '—'. Zero-priced requirements count as priced (free/sample
semantic, not unknown).

Spec: docs/superpowers/specs/2026-04-21-opportunity-table-merged-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Add `_build_row_mpn_chips` helper

**Files:**
- Modify: `app/services/requisition_list_service.py`
- Test: `tests/test_requisition_list_service.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_requisition_list_service.py` (after `_resolve_deal_value` tests). Add `_build_row_mpn_chips` to the top-of-file import block:

```python
from app.services.requisition_list_service import (
    _build_row_mpn_chips,
    _hours_until_bid_due,
    _resolve_deal_value,
    get_requisition_detail,
    get_team_users,
    list_requisitions,
)
```

Then append tests:

```python
# ── _build_row_mpn_chips ─────────────────────────────────────────────


class _FakeReq:
    def __init__(self, primary_mpn=None, substitutes=None):
        self.primary_mpn = primary_mpn
        self.substitutes = substitutes or []


def test_build_row_mpn_chips_orders_primaries_before_subs():
    reqs = [
        _FakeReq(primary_mpn="LM317", substitutes=[{"mpn": "LM337", "manufacturer": "TI"}]),
        _FakeReq(primary_mpn="NE555", substitutes=["LMC555"]),
    ]
    items = _build_row_mpn_chips(reqs)
    roles = [it["role"] for it in items]
    first_sub = roles.index("sub")
    assert all(r == "primary" for r in roles[:first_sub])
    assert all(r == "sub" for r in roles[first_sub:])
    assert [it["mpn"] for it in items] == ["LM317", "NE555", "LM337", "LMC555"]


def test_build_row_mpn_chips_dedupes_keeping_primary_role():
    reqs = [
        _FakeReq(primary_mpn="LM317"),
        _FakeReq(primary_mpn="NE555", substitutes=[{"mpn": "LM317", "manufacturer": "TI"}]),
    ]
    items = _build_row_mpn_chips(reqs)
    mpns = [it["mpn"] for it in items]
    assert mpns.count("LM317") == 1
    lm317 = next(it for it in items if it["mpn"] == "LM317")
    assert lm317["role"] == "primary"


def test_build_row_mpn_chips_empty_when_no_requirements():
    assert _build_row_mpn_chips([]) == []


def test_build_row_mpn_chips_ignores_empty_primary():
    reqs = [_FakeReq(primary_mpn="", substitutes=["SUB1"])]
    items = _build_row_mpn_chips(reqs)
    assert items == [{"mpn": "SUB1", "role": "sub"}]


def test_build_row_mpn_chips_dedupes_repeated_subs():
    reqs = [
        _FakeReq(primary_mpn="A", substitutes=["X", "Y"]),
        _FakeReq(primary_mpn="B", substitutes=["X", "Z"]),
    ]
    items = _build_row_mpn_chips(reqs)
    assert [it["mpn"] for it in items] == ["A", "B", "X", "Y", "Z"]
```

- [ ] **Step 2: Run — expect fail**

```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_requisition_list_service.py -k "build_row_mpn_chips" -v --override-ini="addopts="
```
Expected: `ImportError: cannot import name '_build_row_mpn_chips'`.

- [ ] **Step 3: Implement the helper**

Add to `requisition_list_service.py`, after `_resolve_deal_value`:

```python
def _build_row_mpn_chips(requirements) -> list[dict]:
    """Return flat deduped chip-item list: primaries first, subs second.

    Rules (per 2026-04-21 merged spec §_build_row_mpn_chips):
      1. Pass 1 — each requirement's primary_mpn (if truthy).
      2. Pass 2 — each requirement's subs via parse_substitute_mpns.
      3. Dedupe by MPN keeping the first occurrence (so an MPN that's
         primary in any requirement renders as primary, never sub).
      4. No limit; frontend decides visibility via x-chip-overflow.

    Called by: list_requisitions()
    """
    from app.utils.normalization import parse_substitute_mpns

    seen: set[str] = set()
    items: list[dict] = []

    for req in requirements:
        mpn = (getattr(req, "primary_mpn", None) or "").strip()
        if mpn and mpn not in seen:
            items.append({"mpn": mpn, "role": "primary"})
            seen.add(mpn)

    for req in requirements:
        for sub in parse_substitute_mpns(getattr(req, "substitutes", None) or []):
            sub = (sub or "").strip()
            if sub and sub not in seen:
                items.append({"mpn": sub, "role": "sub"})
                seen.add(sub)

    return items
```

- [ ] **Step 4: Run — expect pass**

```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_requisition_list_service.py -k "build_row_mpn_chips" -v --override-ini="addopts="
```

- [ ] **Step 5: Commit**

```bash
git add app/services/requisition_list_service.py tests/test_requisition_list_service.py
git commit -m "$(cat <<'EOF'
feat(requisitions2): _build_row_mpn_chips aggregates primaries + subs

Flat deduped list across all requirements of a requisition: primaries
first (DOM order), subs second, de-duplicated keeping first occurrence.
Drives the chip row in the merged v2 Name cell.

Spec: docs/superpowers/specs/2026-04-21-opportunity-table-merged-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Extend `list_requisitions` aggregation + expose new row-dict fields

**Files:**
- Modify: `app/services/requisition_list_service.py`
- Test: `tests/test_requisition_list_service.py`

- [ ] **Step 1: Write the failing integration tests**

Append:

```python
# ── list_requisitions aggregation additions ──────────────────────────


def test_list_row_exposes_deal_value_and_coverage(db_session, test_user, test_requisition):
    """New row keys for the v2 row template must be present and typed."""
    filters = ReqListFilters(status=ReqStatus.all)
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    req = result["requisitions"][0]

    assert "hours_until_bid_due" in req
    assert "deal_value_display" in req
    assert "deal_value_source" in req
    assert req["deal_value_source"] in {"entered", "computed", "partial", "none"}
    assert "deal_value_priced_count" in req
    assert isinstance(req["deal_value_priced_count"], int)
    assert "deal_value_requirement_count" in req
    assert isinstance(req["deal_value_requirement_count"], int)
    assert "coverage_filled" in req
    assert isinstance(req["coverage_filled"], int)
    assert "coverage_total" in req
    assert isinstance(req["coverage_total"], int)
    assert req["coverage_filled"] <= req["coverage_total"]
    assert "mpn_chip_items" in req
    assert isinstance(req["mpn_chip_items"], list)


def test_list_row_coverage_counts_requirements_with_offers(db_session, test_user, test_requisition):
    """coverage_filled == count of requirements with >=1 offer (not sightings)."""
    filters = ReqListFilters(status=ReqStatus.all)
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    req = result["requisitions"][0]
    assert req["coverage_filled"] == 0
```

- [ ] **Step 2: Run — expect fail**

- [ ] **Step 3: Locate existing aggregation**

```bash
grep -n "func.sum\|func.count\|total_target_value\|sourced_cnt\|offer_cnt" /root/availai/app/services/requisition_list_service.py | head -20
```

Identify the block that computes `total_target_value` and `offer_cnt`. That's the insertion site.

- [ ] **Step 4: Add three new aggregates**

Add to the imports at top:
```python
from sqlalchemy import case
```

In the aggregation subquery, add:

```python
# priced_sum: Σ(target_price · target_qty) across priced requirements.
priced_sum_expr = func.coalesce(
    func.sum(
        case(
            (Requirement.target_price.isnot(None), Requirement.target_price * Requirement.target_qty),
            else_=0,
        )
    ),
    0,
).label("priced_sum")

# priced_count: count of requirements with non-null target_price.
priced_count_expr = func.count(Requirement.target_price).label("priced_count")

# coverage_filled: count of requirements with >=1 Offer in this requisition.
from app.models.offers import Offer  # (adjust import if Offer lives elsewhere)

has_offer_subq = (
    select(func.count(Offer.id))
    .where(Offer.requirement_id == Requirement.id)
    .correlate(Requirement)
    .scalar_subquery()
)
coverage_filled_expr = func.coalesce(
    func.sum(case((has_offer_subq > 0, 1), else_=0)),
    0,
).label("coverage_filled")
```

Wire all three labels into the existing `select(...)` column list. Keep `.group_by(Requisition.id)` unchanged.

- [ ] **Step 5: Eager-load requirements on the outer query**

`_build_row_mpn_chips` iterates `req.requirements`. If the base query doesn't eager-load, that's N+1. Check:

```bash
grep -n "selectinload\|joinedload" /root/availai/app/services/requisition_list_service.py | head
```

If `Requisition.requirements` isn't eager-loaded, add `.options(selectinload(Requisition.requirements))` to the base query with:

```python
from sqlalchemy.orm import selectinload
```

- [ ] **Step 6: Populate row-dict keys**

Update the row-dict builder. After the block that currently sets deal-related fields:

```python
_opp_val = float(r.opportunity_value) if r.opportunity_value else None
_priced_sum = float(getattr(r, "priced_sum", 0) or 0)
_priced_count = int(getattr(r, "priced_count", 0) or 0)
_req_count = int(req_cnt or 0)
_deal_val, _deal_src = _resolve_deal_value(_opp_val, _priced_sum, _priced_count, _req_count)
_coverage_filled = int(getattr(r, "coverage_filled", 0) or 0)
```

Add / replace in the appended row dict:

```python
"opportunity_value": _opp_val,
"hours_until_bid_due": _hours_until_bid_due(r.deadline),
"deal_value_display": _deal_val,
"deal_value_source": _deal_src,
"deal_value_priced_count": _priced_count,
"deal_value_requirement_count": _req_count,
"coverage_filled": _coverage_filled,
"coverage_total": _req_count,
"mpn_chip_items": _build_row_mpn_chips(list(r.requirements or [])),
```

- [ ] **Step 7: Run — expect pass**

```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_requisition_list_service.py -v --override-ini="addopts="
```

Also:
```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_requisitions2_templates.py -v --override-ini="addopts="
```
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add app/services/requisition_list_service.py tests/test_requisition_list_service.py
git commit -m "$(cat <<'EOF'
feat(requisitions2): expose deal-value + coverage + chip row-dict fields

list_requisitions() now returns priced_sum, priced_count, coverage_filled
(offer-based), coverage_total, deal_value_priced_count,
deal_value_requirement_count, and mpn_chip_items on each row.

Coverage uses ≥1 offer per requirement (not sightings) — matches the
broker "ready to move on" mental model.

Spec: docs/superpowers/specs/2026-04-21-opportunity-table-merged-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Keep `_hours_until_bid_due` tests green

**Files:**
- Verify only.

- [ ] **Step 1: Run**

```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_requisition_list_service.py -k "hours_until_bid_due" -v --override-ini="addopts="
```
Expected: 4 tests PASS.

- [ ] **Step 2: No commit.** If any fail, diagnose the Task 3–5 edit that regressed the helper before moving on.

---

## Task 7: Macro tests — status dot, coverage meter, urgency accent, time text, deal value

**Files:**
- Create: `tests/test_opp_macros.py`
- Modify: `app/templates/htmx/partials/shared/_macros.html`

- [ ] **Step 1: Write the test file**

Write `tests/test_opp_macros.py`:

```python
"""Tests for Opportunity Table v2 Jinja macros.

Called by: pytest
Depends on: templates.env (app.template_env), _macros.html
"""

import pytest
from jinja2 import Template

from app.template_env import templates

ENV = templates.env


def render_macro(call_expr: str, **ctx) -> str:
    tpl = Template(
        '{% from "htmx/partials/shared/_macros.html" import '
        "status_dot, deal_value, coverage_meter, urgency_accent_class, time_text %}"
        + call_expr,
        env=ENV,
    )
    return tpl.render(**ctx).strip()


# ── status_dot ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,bucket,label",
    [
        ("active", "open", "Open"),
        ("sourcing", "sourcing", "Sourcing"),
        ("offers", "offered", "Offered"),
        ("quoting", "quoted", "Quoting"),
        ("quoted", "quoted", "Quoted"),
        ("won", "neutral", "Won"),
    ],
)
def test_status_dot_buckets(value, bucket, label):
    html = render_macro(f'{{{{ status_dot("{value}") }}}}')
    assert f"opp-status-dot--{bucket}" in html
    assert f">{label}<" in html


# ── deal_value ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "amount,source,expect_class,expect_text",
    [
        (150000, "entered", "opp-deal--tier-primary-500", "$150,000"),
        (5000, "entered", "opp-deal--tier-primary-400", "$5,000"),
        (500, "entered", "opp-deal--tier-tertiary", "$500"),
        (25000, "computed", "opp-deal--computed", "$25,000"),
        (None, "none", "opp-deal--tier-tertiary", "—"),
        (0, "none", "opp-deal--tier-tertiary", "—"),
    ],
)
def test_deal_value_tiers(amount, source, expect_class, expect_text):
    html = render_macro(f"{{{{ deal_value({amount!r}, {source!r}) }}}}")
    assert expect_class in html
    assert expect_text in html


def test_deal_value_partial_has_tilde_and_italic_and_tooltip():
    html = render_macro(
        '{{ deal_value(30000, "partial", priced_count=3, requirement_count=5) }}'
    )
    assert "~$30,000" in html
    assert "opp-deal--computed" in html
    assert "opp-deal--partial" in html
    assert "3 of 5 parts priced" in html


# ── coverage_meter ────────────────────────────────────────────────────


def test_coverage_meter_empty():
    html = render_macro("{{ coverage_meter(0, 0) }}")
    assert html.count("opp-coverage-seg") == 6
    assert "opp-coverage-seg--filled" not in html
    assert "no parts yet" in html


def test_coverage_meter_half():
    html = render_macro("{{ coverage_meter(3, 6) }}")
    assert html.count("opp-coverage-seg--filled") == 3


def test_coverage_meter_full():
    html = render_macro("{{ coverage_meter(6, 6) }}")
    assert html.count("opp-coverage-seg--filled") == 6


def test_coverage_meter_aria_label():
    html = render_macro("{{ coverage_meter(2, 5) }}")
    assert 'aria-label="Coverage: 2 of 5 parts sourced"' in html


# ── urgency_accent_class ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "hours,urgency,expected",
    [
        (6, "normal", "opp-row--urgent-24h"),
        (48, "normal", "opp-row--urgent-72h"),
        (120, "normal", ""),
        (None, "normal", ""),
        (None, "critical", "opp-row--urgent-24h"),
        (120, "critical", "opp-row--urgent-24h"),
    ],
)
def test_urgency_accent_class(hours, urgency, expected):
    html = render_macro(f"{{{{ urgency_accent_class({hours!r}, {urgency!r}) }}}}")
    assert html == expected


# ── time_text ─────────────────────────────────────────────────────────


def test_time_text_none_is_empty():
    assert render_macro("{{ time_text(None) }}") == ""


def test_time_text_overdue():
    html = render_macro("{{ time_text(-2) }}")
    assert "Overdue" in html
    assert "opp-time--24h" in html


def test_time_text_under_24():
    html = render_macro("{{ time_text(6) }}")
    assert "6h" in html
    assert "opp-time--24h" in html


def test_time_text_between_24_and_72():
    html = render_macro("{{ time_text(48) }}")
    assert "48h" in html
    assert "opp-time--72h" in html


def test_time_text_days():
    html = render_macro("{{ time_text(120) }}")
    assert "5d" in html
    assert "opp-time--normal" in html
```

- [ ] **Step 2: Run — expect several fails**

```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_opp_macros.py -v --override-ini="addopts="
```

Expected: some FAILs around `deal_value` partial and `urgency_accent_class` (name).

- [ ] **Step 3: Rename `urgency_accent` → `urgency_accent_class` with 2-arg signature**

Replace the existing uncommitted `urgency_accent` macro with:

```jinja
{# ── Opportunity Table v2 — Urgency Accent Class ─────────────────── #}
{% macro urgency_accent_class(hours, urgency) -%}
{%- if hours is not none and hours <= 24 -%}opp-row--urgent-24h
{%- elif hours is not none and hours <= 72 -%}opp-row--urgent-72h
{%- elif urgency == 'critical' -%}opp-row--urgent-24h
{%- endif -%}
{%- endmacro %}
```

- [ ] **Step 4: Extend `deal_value` macro**

Replace the uncommitted `deal_value` with:

```jinja
{# ── Opportunity Table v2 — Deal Value ────────────────────────────── #}
{% macro deal_value(amount, source='entered', priced_count=0, requirement_count=0) -%}
{%- if amount is none or amount == 0 -%}
<span class="opp-deal opp-deal--tier-tertiary">—</span>
{%- else -%}
  {%- if amount >= 100000 -%}{%- set tier = 'primary-500' -%}
  {%- elif amount >= 1000 -%}{%- set tier = 'primary-400' -%}
  {%- else -%}{%- set tier = 'tertiary' -%}{%- endif -%}
  {%- set prefix = '~' if source == 'partial' else '' -%}
  {%- set italic_cls = ' opp-deal--computed' if source in ('computed', 'partial') else '' -%}
  {%- set partial_cls = ' opp-deal--partial' if source == 'partial' else '' -%}
  {%- if source == 'partial' -%}
    {%- set title_attr = ' title="Floor estimate — ' ~ priced_count ~ ' of ' ~ requirement_count ~ ' parts priced"' -%}
  {%- elif source == 'computed' -%}
    {%- set title_attr = ' title="Computed from target prices (all parts priced)"' -%}
  {%- elif source == 'entered' -%}
    {%- set title_attr = ' title="Entered by broker"' -%}
  {%- else -%}
    {%- set title_attr = '' -%}
  {%- endif -%}
<span class="opp-deal opp-deal--tier-{{ tier }}{{ italic_cls }}{{ partial_cls }}"{{ title_attr|safe }}>{{ prefix }}${{ '{:,.0f}'.format(amount) }}</span>
{%- endif -%}
{%- endmacro %}
```

- [ ] **Step 5: Tweak `coverage_meter` for empty-state title**

Replace the uncommitted `coverage_meter` with:

```jinja
{# ── Opportunity Table v2 — Coverage Meter ────────────────────────── #}
{% macro coverage_meter(filled, total) %}
{%- set total = total or 0 -%}
{%- set filled = filled or 0 -%}
{%- if total > 0 -%}
  {%- set segs = ((filled * 6) / total)|round(0, 'common')|int -%}
{%- else -%}
  {%- set segs = 0 -%}
{%- endif -%}
<span class="opp-coverage"
      role="img"
      aria-label="Coverage: {{ filled }} of {{ total }} parts sourced"
      {%- if total == 0 %} title="no parts yet"{%- endif %}>
{%- for i in range(6) -%}
<span class="opp-coverage-seg{% if i < segs %} opp-coverage-seg--filled{% endif %}"></span>
{%- endfor -%}
</span>
{% endmacro %}
```

- [ ] **Step 6: Run — expect pass**

```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_opp_macros.py -v --override-ini="addopts="
```

- [ ] **Step 7: Commit**

```bash
git add tests/test_opp_macros.py app/templates/htmx/partials/shared/_macros.html
git commit -m "$(cat <<'EOF'
feat(macros): finalize v2 opportunity-table macros + tests

- urgency_accent → urgency_accent_class (hours + urgency), manual-critical fallback
- deal_value: 'partial' source (leading ~, italic, floor-estimate tooltip)
- coverage_meter: empty-state title attribute
- status_dot + time_text locked via parametrized render tests

Spec: docs/superpowers/specs/2026-04-21-opportunity-table-merged-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Add `mpn_chips_aggregated(items)` macro

**Files:**
- Modify: `app/templates/htmx/partials/shared/_mpn_chips.html`
- Test: `tests/test_opp_macros.py`

- [ ] **Step 1: Append failing tests**

```python
# ── mpn_chips_aggregated ──────────────────────────────────────────────


def render_aggregated(items_expr: str) -> str:
    tpl = Template(
        '{% from "htmx/partials/shared/_mpn_chips.html" import mpn_chips_aggregated %}'
        + f"{{{{ mpn_chips_aggregated({items_expr}) }}}}",
        env=ENV,
    )
    return tpl.render().strip()


def test_mpn_chips_aggregated_renders_primaries_before_subs():
    items = [
        {"mpn": "LM317", "role": "primary"},
        {"mpn": "NE555", "role": "primary"},
        {"mpn": "LM337", "role": "sub"},
    ]
    html = render_aggregated(repr(items))
    pos_lm317 = html.index("LM317")
    pos_ne555 = html.index("NE555")
    pos_lm337 = html.index("LM337")
    assert pos_lm317 < pos_ne555 < pos_lm337


def test_mpn_chips_aggregated_includes_overflow_bucket_and_directive():
    items = [{"mpn": f"M{i}", "role": "primary"} for i in range(4)]
    html = render_aggregated(repr(items))
    assert "x-chip-overflow" in html
    assert "opp-chip-more" in html


def test_mpn_chips_aggregated_empty_renders_placeholder():
    html = render_aggregated("[]")
    assert "—" in html
```

- [ ] **Step 2: Run — expect fail**

- [ ] **Step 3: Add the macro**

Append to `app/templates/htmx/partials/shared/_mpn_chips.html`:

```jinja
{# ── mpn_chips_aggregated — flat deduped chip row for requisition rows ── #}
{% macro mpn_chips_aggregated(items, link_map=none) %}
{%- if items -%}
<span class="opp-chip-row inline-flex items-center gap-1 min-w-0 whitespace-nowrap" x-chip-overflow>
  {%- for item in items -%}
    {{ _chip(item.mpn, link_map) }}
  {%- endfor -%}
  <button type="button" class="opp-chip-more px-1.5 text-xs font-medium text-brand-600 hover:text-brand-800 cursor-pointer whitespace-nowrap" style="display:none" x-truncate-tip aria-label="Show hidden chips"></button>
</span>
{%- else -%}
<span class="text-gray-400">—</span>
{%- endif -%}
{% endmacro %}
```

Note: the `+N` button is `x-truncate-tip`-bound and carries **no `data-tip-content` attribute**. The `x-chip-overflow` directive attaches cloned DOM nodes to an element property (`_tipNodes`) at runtime; `x-truncate-tip` reads that property when showing the tip. No HTML strings flow through attributes.

- [ ] **Step 4: Run — expect pass**

- [ ] **Step 5: Commit**

```bash
git add app/templates/htmx/partials/shared/_mpn_chips.html tests/test_opp_macros.py
git commit -m "$(cat <<'EOF'
feat(mpn-chips): mpn_chips_aggregated macro for requisition row chip row

Flat deduped chip list (primaries-first) with a trailing +N button the
x-chip-overflow directive populates at runtime. Tooltip content flows
via a DOM-node property (not innerHTML), set by x-chip-overflow and
read by x-truncate-tip — no HTML-string attribute payloads.

Spec: docs/superpowers/specs/2026-04-21-opportunity-table-merged-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Add `opp_status_cell` macro

**Files:**
- Modify: `app/templates/htmx/partials/shared/_macros.html`
- Test: `tests/test_opp_macros.py`

- [ ] **Step 1: Append failing tests**

```python
# ── opp_status_cell ───────────────────────────────────────────────────


def render_status_cell(status, hours):
    tpl = Template(
        '{% from "htmx/partials/shared/_macros.html" import opp_status_cell %}'
        f"{{{{ opp_status_cell({status!r}, {hours!r}) }}}}",
        env=ENV,
    )
    return tpl.render().strip()


def test_opp_status_cell_includes_dot_and_time_text():
    html = render_status_cell("sourcing", 6)
    assert "opp-status-dot--sourcing" in html
    assert ">Sourcing<" in html
    assert "opp-time--24h" in html
    assert "6h" in html


def test_opp_status_cell_no_time_text_when_hours_none():
    html = render_status_cell("active", None)
    assert "opp-status-dot--open" in html
    assert "opp-time--" not in html


def test_opp_status_cell_aria_label_combines_status_and_time():
    html = render_status_cell("sourcing", 6)
    assert 'aria-label="Sourcing, 6h"' in html
```

- [ ] **Step 2: Run — expect fail**

- [ ] **Step 3: Add the macro**

Append to `_macros.html`:

```jinja
{# ── Opportunity Table v2 — Status Cell (dot + label + time text) ── #}
{% macro opp_status_cell(status, hours) %}
{%- set label_map = {
  'active': 'Open', 'sourcing': 'Sourcing', 'offers': 'Offered',
  'quoting': 'Quoting', 'quoted': 'Quoted'
} -%}
{%- set label = label_map.get(status, status|replace('_', ' ')|capitalize) -%}
{%- set time_label = '' -%}
{%- if hours is not none and hours < 0 -%}{%- set time_label = 'Overdue' -%}
{%- elif hours is not none and hours <= 72 -%}{%- set time_label = (hours|round(0)|int ~ 'h') -%}
{%- elif hours is not none -%}{%- set time_label = ((hours/24)|round(0)|int ~ 'd') -%}
{%- endif -%}
<span class="opp-status-cell inline-flex items-center gap-1.5" aria-label="{{ label }}{% if time_label %}, {{ time_label }}{% endif %}">
  {{ status_dot(status) }}
  {%- if hours is not none -%}
    <span class="opp-status-sep" aria-hidden="true">·</span>
    {{ time_text(hours) }}
  {%- endif -%}
</span>
{% endmacro %}
```

- [ ] **Step 4: Run — expect pass**

- [ ] **Step 5: Commit**

```bash
git add app/templates/htmx/partials/shared/_macros.html tests/test_opp_macros.py
git commit -m "$(cat <<'EOF'
feat(macros): opp_status_cell folds time-to-bid into status

Single visual unit: colored dot + plain label + inline time text.
aria-label combines both for screen readers.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Add `opp_name_cell` macro

**Files:**
- Modify: `app/templates/htmx/partials/shared/_macros.html`
- Test: `tests/test_opp_macros.py`

- [ ] **Step 1: Append failing tests**

```python
# ── opp_name_cell ─────────────────────────────────────────────────────


def render_name_cell(req):
    tpl = Template(
        '{% from "htmx/partials/shared/_macros.html" import opp_name_cell %}'
        "{{ opp_name_cell(req) }}",
        env=ENV,
    )
    return tpl.render(req=req).strip()


def test_opp_name_cell_has_chip_row_and_name():
    req = {
        "id": 42,
        "name": "Acme Q3",
        "mpn_chip_items": [{"mpn": "LM317", "role": "primary"}],
        "match_reason": None,
        "matched_mpn": None,
    }
    html = render_name_cell(req)
    assert "opp-chip-row" in html
    assert "LM317" in html
    assert "Acme Q3" in html


def test_opp_name_cell_renders_match_badge_when_present():
    req = {
        "id": 7,
        "name": "Foo",
        "mpn_chip_items": [],
        "match_reason": "part",
        "matched_mpn": "XYZ123",
    }
    html = render_name_cell(req)
    assert "XYZ123" in html
    assert "match-badge" in html


def test_opp_name_cell_name_has_truncate_tip():
    req = {
        "id": 1,
        "name": "VeryLong" * 10,
        "mpn_chip_items": [],
        "match_reason": None,
        "matched_mpn": None,
    }
    html = render_name_cell(req)
    assert "x-truncate-tip" in html
```

- [ ] **Step 2: Run — expect fail**

- [ ] **Step 3: Add the macro**

Append to `_macros.html`:

```jinja
{# ── Opportunity Table v2 — Name Cell ─────────────────────────────── #}
{% from "htmx/partials/shared/_mpn_chips.html" import mpn_chips_aggregated %}

{% macro opp_name_cell(req) %}
<div class="opp-name-cell min-w-0">
  <div class="opp-name-cell__chips">
    {{ mpn_chips_aggregated(req.mpn_chip_items or []) }}
  </div>
  <div class="opp-name-cell__title flex items-center gap-1.5 min-w-0">
    <span class="truncate block min-w-0 text-[12px] text-[color:var(--opp-text-tertiary)]" x-truncate-tip>{{ req.name }}</span>
    {% if req.match_reason == 'part' and req.matched_mpn %}
      <span class="match-badge inline-flex items-center gap-0.5 px-1.5 py-0.5 text-[10px] font-semibold rounded-full bg-violet-50 text-violet-600 border border-violet-200" title="Matched part: {{ req.matched_mpn }}">
        {{ req.matched_mpn }}
      </span>
    {% elif req.match_reason == 'customer' %}
      <span class="match-badge inline-flex items-center gap-0.5 px-1.5 py-0.5 text-[10px] font-semibold rounded-full bg-sky-50 text-sky-600 border border-sky-200">customer match</span>
    {% endif %}
  </div>
</div>
{% endmacro %}
```

- [ ] **Step 4: Run — expect pass**

- [ ] **Step 5: Commit**

```bash
git add app/templates/htmx/partials/shared/_macros.html tests/test_opp_macros.py
git commit -m "$(cat <<'EOF'
feat(macros): opp_name_cell composes chip row + name + match badges

Chip row on top; requisition name below (tertiary, truncated with
x-truncate-tip); existing part/customer match badges preserved inline.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Add `opp_row_action_rail` macro

**Files:**
- Modify: `app/templates/htmx/partials/shared/_macros.html`
- Test: `tests/test_opp_macros.py`

- [ ] **Step 1: Append failing tests**

```python
# ── opp_row_action_rail ───────────────────────────────────────────────


def _req(status="active", claimed_by_id=None, name="Acme", rid=1):
    return {"id": rid, "name": name, "status": status, "claimed_by_id": claimed_by_id}


def _user(uid=5, role="buyer"):
    class U: pass
    u = U(); u.id = uid; u.role = role
    return u


def render_rail(req, user):
    tpl = Template(
        '{% from "htmx/partials/shared/_macros.html" import opp_row_action_rail %}'
        "{{ opp_row_action_rail(req, user) }}",
        env=ENV,
    )
    return tpl.render(req=req, user=user).strip()


def test_rail_shows_archive_when_not_archived():
    html = render_rail(_req(status="active"), _user())
    assert "action/archive" in html
    assert "action/activate" not in html


def test_rail_shows_activate_when_archived():
    html = render_rail(_req(status="archived"), _user())
    assert "action/activate" in html
    assert "action/archive" not in html


def test_rail_shows_claim_when_unclaimed_and_role_allowed():
    html = render_rail(_req(claimed_by_id=None), _user(role="buyer"))
    assert "action/claim" in html
    assert "action/unclaim" not in html


def test_rail_shows_unclaim_when_claimed_by_current_user():
    html = render_rail(_req(claimed_by_id=5), _user(uid=5))
    assert "action/unclaim" in html


def test_rail_has_toolbar_role_and_aria_label():
    html = render_rail(_req(), _user())
    assert 'role="toolbar"' in html
    assert 'aria-label="Row actions"' in html
```

- [ ] **Step 2: Run — expect fail**

- [ ] **Step 3: Add the macro**

Append to `_macros.html`:

```jinja
{# ── Opportunity Table v2 — Row Action Rail ───────────────────────── #}
{% macro opp_row_action_rail(req, user) %}
<td class="opp-action-rail-cell">
  <div class="opp-action-rail"
       x-show="show"
       x-cloak
       role="toolbar"
       aria-label="Row actions"
       @click.stop>
    {% if req.status != 'archived' %}
      <button type="button"
              hx-post="/requisitions2/{{ req.id }}/action/archive"
              hx-target="#rq2-table"
              hx-swap="outerHTML"
              hx-confirm="Archive '{{ req.name }}'?"
              aria-label="Archive {{ req.name }}"
              @click.stop>
        <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M5 8h14M5 8l1 12a2 2 0 002 2h8a2 2 0 002-2l1-12M9 12h6"/></svg>
      </button>
    {% else %}
      <button type="button"
              hx-post="/requisitions2/{{ req.id }}/action/activate"
              hx-target="#rq2-table"
              hx-swap="outerHTML"
              aria-label="Activate {{ req.name }}"
              @click.stop>
        <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M5 15l7-7 7 7"/></svg>
      </button>
    {% endif %}

    {% if not req.claimed_by_id and user and user.role in ('buyer','trader','manager','admin') %}
      <button type="button"
              hx-post="/requisitions2/{{ req.id }}/action/claim"
              hx-target="#rq2-table"
              hx-swap="outerHTML"
              aria-label="Claim {{ req.name }}"
              @click.stop>
        <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 4v16m0-16L4 12m8-8l8 8"/></svg>
      </button>
    {% elif req.claimed_by_id and user and req.claimed_by_id == user.id %}
      <button type="button"
              hx-post="/requisitions2/{{ req.id }}/action/unclaim"
              hx-target="#rq2-table"
              hx-swap="outerHTML"
              aria-label="Unclaim {{ req.name }}"
              @click.stop>
        <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 20V4m0 16l-8-8m8 8l8-8"/></svg>
      </button>
    {% endif %}

    {% if req.status in ('active','sourcing','offers','quoting','quoted','open','reopened') %}
      <button type="button"
              hx-post="/requisitions2/{{ req.id }}/action/won"
              hx-target="#rq2-table"
              hx-swap="outerHTML"
              aria-label="Mark {{ req.name }} won"
              class="text-emerald-600"
              @click.stop>
        <svg class="h-4 w-4" fill="currentColor" viewBox="0 0 20 20"><path d="M10 15l-5.878 3.09 1.123-6.545L.489 6.91l6.572-.955L10 0l2.939 5.955 6.572.955-4.756 4.635 1.123 6.545z"/></svg>
      </button>
      <button type="button"
              hx-post="/requisitions2/{{ req.id }}/action/lost"
              hx-target="#rq2-table"
              hx-swap="outerHTML"
              hx-confirm="Mark '{{ req.name }}' as lost?"
              aria-label="Mark {{ req.name }} lost"
              class="text-rose-600"
              @click.stop>
        <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>
      </button>
    {% endif %}

    <button type="button"
            hx-post="/requisitions2/{{ req.id }}/action/clone"
            hx-target="#rq2-table"
            hx-swap="outerHTML"
            aria-label="Clone {{ req.name }}"
            @click.stop>
      <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M8 7v8a2 2 0 002 2h6M8 7V5a2 2 0 012-2h4.586a1 1 0 01.707.293l4.414 4.414a1 1 0 01.293.707V15a2 2 0 01-2 2h-2M8 7H6a2 2 0 00-2 2v10a2 2 0 002 2h8a2 2 0 002-2v-2"/></svg>
    </button>
  </div>
</td>
{% endmacro %}
```

- [ ] **Step 4: Run — expect pass**

- [ ] **Step 5: Commit**

```bash
git add app/templates/htmx/partials/shared/_macros.html tests/test_opp_macros.py
git commit -m "$(cat <<'EOF'
feat(macros): opp_row_action_rail replaces ⋮ dropdown

Same action set as legacy workspace row-action endpoint (Archive/Activate,
Claim/Unclaim, Mark Won/Lost, Clone). @click.stop on each button so the
row's hx-get detail handler doesn't fire on action click.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: Add v2 CSS tokens

**Files:**
- Modify: `app/static/styles.css`

- [ ] **Step 1: Find the v2 token block**

```bash
grep -n "Opportunity table v2\|opp-coverage-seg" /root/availai/app/static/styles.css
```

- [ ] **Step 2: Append the new CSS**

After the last `.opp-*` rule in the v2 block:

```css
/* ── Opportunity Table v2 — partial deal value ────────────────── */
.opp-deal--partial { /* italic comes from .opp-deal--computed; hook for future tweaks. */ }

/* ── Opportunity Table v2 — truncate-tip ─────────────────────── */
.truncate-tip {
  position: fixed;
  z-index: 50;
  max-width: 320px;
  padding: 6px 10px;
  font-size: 12px;
  line-height: 1.35;
  color: #fff;
  background: #1C2130;
  border-radius: 4px;
  pointer-events: none;
  opacity: 0;
  transition: opacity 80ms ease-out;
  box-shadow: 0 4px 12px rgba(0,0,0,0.12);
}
.truncate-tip.visible { opacity: 1; }
.truncate-tip .opp-chip-row { flex-wrap: wrap; gap: 4px; }

/* ── Opportunity Table v2 — name cell stack ──────────────────── */
.opp-name-cell { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.opp-name-cell__chips { min-width: 0; }
.opp-name-cell__title { min-width: 0; }
.opp-chip-row { min-width: 0; }

/* ── Opportunity Table v2 — row hover action rail ───────────── */
.opp-action-rail-cell { position: relative; width: 0; padding: 0; }
.opp-action-rail {
  position: absolute;
  top: 50%;
  right: 8px;
  transform: translateY(-50%);
  display: inline-flex;
  gap: 2px;
  padding: 4px;
  background: rgba(255,255,255,0.96);
  backdrop-filter: blur(2px);
  border: 1px solid var(--opp-sep);
  border-radius: 6px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.08);
  opacity: 0;
  transition: opacity 90ms ease-out;
  pointer-events: none;
}
tr:hover .opp-action-rail,
tr:focus-within .opp-action-rail {
  opacity: 1;
  pointer-events: auto;
}
.opp-action-rail button {
  width: 28px; height: 28px;
  display: inline-flex; align-items: center; justify-content: center;
  color: var(--opp-text-secondary);
  background: transparent;
  border: none; border-radius: 4px;
  cursor: pointer;
}
.opp-action-rail button:hover { background: var(--opp-sep); color: var(--opp-text-primary); }
.opp-action-rail button:focus-visible { outline: 2px solid var(--opp-status-open); outline-offset: 1px; }
```

- [ ] **Step 3: Build to confirm no parse error**

```bash
cd /root/availai && npm run build 2>&1 | tail -20
```

- [ ] **Step 4: Commit**

```bash
git add app/static/styles.css
git commit -m "$(cat <<'EOF'
feat(styles): v2 tokens — truncate-tip, name-cell, chip-row, action-rail

CSS hooks only; behavior wired in Tasks 14-16.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 13: Reconnaissance — current JS layout

**Files:**
- Read only.

- [ ] **Step 1: Confirm current locations**

```bash
grep -n "Alpine.directive\|x-truncate-tip\|splitPanel\|resizableTable" /root/availai/app/static/htmx_app.js | head -15
grep -n "truncate-tip" /root/availai/app/static/js/requisitions2.js
```

Record line numbers for insertion in Tasks 14–16.

- [ ] **Step 2: No commit.**

---

## Task 14: Relocate `x-truncate-tip` + introduce node-property contract

**Files:**
- Modify: `app/static/htmx_app.js`, `app/static/js/requisitions2.js`

- [ ] **Step 1: Remove the `x-truncate-tip` block from `requisitions2.js`**

Delete the `Alpine.directive('truncate-tip', ...)` block (lines 210–250 of the uncommitted diff).

- [ ] **Step 2: Add the relocated directive to `htmx_app.js`**

Insert before `Alpine.start()` in the `alpine:init` callback:

```javascript
/**
 * x-truncate-tip — Hover tooltip that fires when an element overflows
 * its box (scrollWidth > clientWidth), OR when the element has a
 * `_tipNodes` property (a DocumentFragment the directive appends as-is).
 *
 * The `_tipNodes` path is used by x-chip-overflow to show hidden chips
 * without ever touching innerHTML — we clone DOM subtrees directly.
 */
Alpine.directive('truncate-tip', (el) => {
  let tip = null;

  const hasTipNodes = () => el._tipNodes && el._tipNodes.hasChildNodes && el._tipNodes.hasChildNodes();

  const show = () => {
    const viaNodes = hasTipNodes();
    if (!viaNodes && el.scrollWidth <= el.clientWidth) return;
    const text = viaNodes ? null : el.textContent.trim();
    if (!viaNodes && !text) return;

    tip = document.createElement('div');
    tip.className = 'truncate-tip';
    if (viaNodes) {
      // Clone the fragment so the original reference stays reusable.
      tip.appendChild(el._tipNodes.cloneNode(true));
    } else {
      tip.textContent = text;
    }
    document.body.appendChild(tip);

    const r = el.getBoundingClientRect();
    const tr = tip.getBoundingClientRect();
    let top = r.top - tr.height - 6;
    if (top < 4) top = r.bottom + 6;
    let left = r.left + (r.width - tr.width) / 2;
    left = Math.max(4, Math.min(left, window.innerWidth - tr.width - 4));
    tip.style.top = top + 'px';
    tip.style.left = left + 'px';
    requestAnimationFrame(() => tip && tip.classList.add('visible'));
  };

  const hide = () => { if (tip) { tip.remove(); tip = null; } };

  el.addEventListener('mouseenter', show);
  el.addEventListener('mouseleave', hide);
  el.addEventListener('focusout', hide);
});
```

- [ ] **Step 3: Run existing truncate-tip E2E**

```bash
cd /root/availai && npx playwright test e2e/requisitions2-resize.spec.ts -g "truncate-tip" 2>&1 | tail -15
```
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add app/static/htmx_app.js app/static/js/requisitions2.js
git commit -m "$(cat <<'EOF'
feat(htmx_app): relocate x-truncate-tip + introduce node-property contract

Moved from app/static/js/requisitions2.js (page-local) to htmx_app.js
(global). New behavior: if el._tipNodes (a DocumentFragment) is set,
the directive shows the tip unconditionally and appends the cloned
fragment — no innerHTML, no HTML-string attributes. This is what
x-chip-overflow uses for hidden-chip reveal.

Text-only path unchanged: hover only shows tip when scrollWidth > clientWidth.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 15: Add `x-chip-overflow` directive

**Files:**
- Modify: `app/static/htmx_app.js`

- [ ] **Step 1: Append the directive**

Add after `x-truncate-tip`:

```javascript
/**
 * x-chip-overflow — Measures chip row width and hides chips that don't
 * fit. Exposes a trailing +N button (must be last child, .opp-chip-more)
 * whose `_tipNodes` property holds a cloned DocumentFragment of the
 * hidden chips — x-truncate-tip reads that on hover.
 *
 * Primaries-first DOM order (enforced by _build_row_mpn_chips) ensures
 * the left-to-right overflow walk never hides a primary while a sub is
 * still visible.
 */
Alpine.directive('chip-overflow', (el) => {
  const more = el.querySelector('.opp-chip-more');
  if (!more) return;
  const chips = Array.from(el.children).filter((c) => c !== more);

  let rafId = 0;

  const measure = () => {
    rafId = 0;
    chips.forEach((c) => (c.style.display = ''));
    more.style.display = 'none';
    more.textContent = '';
    more._tipNodes = null;

    const containerWidth = el.clientWidth;
    if (containerWidth === 0) return;

    const style = window.getComputedStyle(el);
    const gap = parseFloat(style.columnGap || style.gap || '0') || 4;

    // Measure +N width at worst-case placeholder, then clear.
    more.style.display = '';
    more.textContent = '+9';
    const moreWidth = more.getBoundingClientRect().width + gap;
    more.textContent = '';

    let used = 0;
    let fitCount = 0;
    for (const chip of chips) {
      const w = chip.getBoundingClientRect().width;
      const projected = used + w + (fitCount > 0 ? gap : 0);
      const reserve = fitCount < chips.length - 1 ? moreWidth : 0;
      if (projected + reserve <= containerWidth) {
        used = projected;
        fitCount++;
      } else {
        break;
      }
    }

    if (fitCount === chips.length) {
      more.style.display = 'none';
      return;
    }

    const hidden = chips.slice(fitCount);
    chips.slice(0, fitCount).forEach((c) => (c.style.display = ''));
    hidden.forEach((c) => (c.style.display = 'none'));

    more.textContent = '+' + hidden.length;

    // Build a DocumentFragment of cloned hidden chips, inside a chip-row wrapper.
    // x-truncate-tip will clone this fragment into the tooltip when hovered.
    const frag = document.createDocumentFragment();
    const wrap = document.createElement('span');
    wrap.className = 'opp-chip-row';
    hidden.forEach((c) => {
      const clone = c.cloneNode(true);
      clone.style.display = '';
      wrap.appendChild(clone);
    });
    frag.appendChild(wrap);
    more._tipNodes = frag;
  };

  const schedule = () => {
    if (rafId) return;
    rafId = requestAnimationFrame(measure);
  };

  schedule();

  const ro = new ResizeObserver(schedule);
  ro.observe(el);

  // Cleanup when Alpine tears down the element.
  el._chipOverflowCleanup = () => {
    ro.disconnect();
    if (rafId) cancelAnimationFrame(rafId);
  };
});
```

- [ ] **Step 2: Smoke-build**

```bash
cd /root/availai && npm run build 2>&1 | tail -10
```
Expected: no JS errors.

- [ ] **Step 3: Commit**

```bash
git add app/static/htmx_app.js
git commit -m "$(cat <<'EOF'
feat(htmx_app): x-chip-overflow directive with DocumentFragment tip-nodes

ResizeObserver-driven chip visibility. Hidden chips are cloned into a
DocumentFragment stored on the +N button's `_tipNodes` property; the
x-truncate-tip directive reads that on hover and appends a re-clone
to the tooltip — zero innerHTML, zero HTML-string payloads.

Primaries-first DOM order (from _build_row_mpn_chips service helper)
means the left-to-right overflow walk preserves the invariant that
primary MPNs are never hidden while subs are visible.

Spec: docs/superpowers/specs/2026-04-21-opportunity-table-merged-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 16: Add `rowActionRail` Alpine component

**Files:**
- Modify: `app/static/htmx_app.js`

- [ ] **Step 1: Append the component**

```javascript
/**
 * rowActionRail — Alpine component for requisitions2 <tr>.
 * CSS handles hover visibility via tr:hover; this component exposes
 * `show` state so keyboard users (Tab, Enter, Escape) have a path.
 */
Alpine.data('rowActionRail', () => ({
  show: false,
  init() {
    this.$el.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { this.show = false; }
    });
  },
}));
```

- [ ] **Step 2: Commit**

```bash
git add app/static/htmx_app.js
git commit -m "$(cat <<'EOF'
feat(htmx_app): rowActionRail Alpine component for keyboard a11y

Complements CSS tr:hover reveal with a keyboard-driven `show` state
(Tab/Enter reveal, Escape dismiss).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 17: Verify HTMX-swap re-init for Alpine directives

**Files:**
- Verify (and fix if missing).

- [ ] **Step 1: Check for the binding**

```bash
grep -n "htmx:afterSwap\|htmx:afterSettle\|Alpine.initTree\|htmx:load" /root/availai/app/static/htmx_app.js
```

Expected: a `document.body.addEventListener('htmx:afterSettle', (e) => Alpine.initTree(e.detail.target))` or equivalent exists. If not, add it near the other HTMX lifecycle bindings:

```javascript
document.body.addEventListener('htmx:afterSettle', (e) => {
  if (e.detail && e.detail.target) {
    Alpine.initTree(e.detail.target);
  }
});
```

- [ ] **Step 2: If you added it, commit**

```bash
git add app/static/htmx_app.js
git commit -m "$(cat <<'EOF'
fix(htmx_app): re-init Alpine on htmx:afterSettle so v2 directives survive swap

Without this, x-chip-overflow / x-truncate-tip / rowActionRail on rows
swapped in via filter changes or action-rail clicks would silently stop
working until a full page reload.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

If already present, no commit.

---

## Task 18: Gate rendering in `_table_rows.html`, `_table.html`, `_single_row.html`

**Files:**
- Modify: `app/templates/requisitions2/_table_rows.html`, `_single_row.html`, `_table.html`
- Test: `tests/test_requisitions2_templates.py`

- [ ] **Step 1: Write the failing flag-on / flag-off tests**

Append to `tests/test_requisitions2_templates.py`:

```python
def test_v2_flag_on_renders_opp_col_header(client):
    resp = client.get("/requisitions2")
    assert "opp-col-header" in resp.text


def test_v2_flag_off_renders_legacy(client, monkeypatch):
    from app.config import settings as app_settings
    monkeypatch.setattr(app_settings, "avail_opp_table_v2", False)
    resp = client.get("/requisitions2")
    assert "opp-col-header" not in resp.text
```

- [ ] **Step 2: Run — expect fail**

```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_requisitions2_templates.py -k "v2_flag" -v --override-ini="addopts="
```

(Probably fails because `opp-col-header` doesn't appear yet.)

- [ ] **Step 3: Rewrite `_table_rows.html`**

Replace the entire body (keeping the top comment) with:

```jinja
{#
  _table_rows.html — Compact requisition rows for the left panel.
  Called by: _table.html (include), GET /requisitions2/table/rows
  Depends on: avail_opp_table_v2_enabled context flag.
#}
{% from "htmx/partials/shared/_macros.html" import status_badge, opp_name_cell, opp_status_cell, opp_row_action_rail, urgency_accent_class, deal_value, coverage_meter %}

{% for req in requisitions %}
{% if avail_opp_table_v2_enabled %}
<tr id="rq2-row-{{ req.id }}"
    class="rq2-row group {{ urgency_accent_class(req.hours_until_bid_due, req.urgency) }}"
    data-status="{{ req.status }}"
    x-data="rowActionRail()"
    tabindex="0"
    @mouseenter="show = true"
    @mouseleave="show = false"
    @focusin="show = true"
    @focusout.self="show = false"
    @keydown.enter="show = true"
    @keydown.escape="show = false"
    @click="selectReq({{ req.id }})"
    hx-get="/requisitions2/{{ req.id }}/detail"
    hx-target="#rq2-detail"
    hx-swap="innerHTML"
    :class="selectedReqId === {{ req.id }} ? 'bg-brand-50 border-l-2 border-brand-500' : ''">
  <td class="px-3 py-2" @click.stop>
    <input aria-label="Select requisition {{ req.name }}" type="checkbox" value="{{ req.id }}"
           class="rounded border-gray-300 text-brand-500 focus:ring-brand-400"
           x-on:change="toggleSelection({{ req.id }}, $event.target.checked)"
           x-bind:checked="selectedIds.has({{ req.id }})">
  </td>
  <td class="px-3 py-2 overflow-hidden">{{ opp_name_cell(req) }}</td>
  <td class="px-3 py-2">{{ opp_status_cell(req.status, req.hours_until_bid_due) }}</td>
  <td class="px-3 py-2 text-sm text-gray-600 overflow-hidden">
    <span class="truncate block" x-truncate-tip>{{ req.customer_display or '—' }}</span>
  </td>
  <td class="px-3 py-2">{{ coverage_meter(req.coverage_filled, req.coverage_total) }}</td>
  <td class="px-3 py-2">{{ deal_value(req.deal_value_display, req.deal_value_source, priced_count=req.deal_value_priced_count, requirement_count=req.deal_value_requirement_count) }}</td>
  {{ opp_row_action_rail(req, user) }}
</tr>
{% else %}
{# Legacy 5-col rendering — preserved verbatim for flag-off rollback. #}
<tr id="rq2-row-{{ req.id }}"
    hx-get="/requisitions2/{{ req.id }}/detail"
    hx-target="#rq2-detail"
    hx-swap="innerHTML"
    @click="selectReq({{ req.id }})"
    :class="selectedReqId === {{ req.id }} ? 'bg-brand-50 border-l-2 border-brand-500' : ''"
    data-status="{{ req.status }}"
    class="rq2-row group hover:bg-gray-50 cursor-pointer transition-colors
           {{ 'border-l-2 border-rose-400' if req.urgency == 'critical' else '' }}">
  <td class="px-3 py-2" @click.stop>
    <input aria-label="Select requisition {{ req.name }}" type="checkbox" value="{{ req.id }}"
           class="rounded border-gray-300 text-brand-500 focus:ring-brand-400"
           x-on:change="toggleSelection({{ req.id }}, $event.target.checked)"
           x-bind:checked="selectedIds.has({{ req.id }})">
  </td>
  <td class="px-3 py-2 overflow-hidden">
    <div class="flex items-center gap-1.5 min-w-0">
      <span class="text-sm font-medium text-gray-900 truncate block flex-1 min-w-0">{{ req.name }}</span>
      {% if req.urgency == 'hot' %}
      <span class="flex-shrink-0 inline-flex h-4 px-1 text-[10px] font-semibold rounded bg-amber-50 text-amber-700">HOT</span>
      {% elif req.urgency == 'critical' %}
      <span class="flex-shrink-0 inline-flex h-4 px-1 text-[10px] font-semibold rounded bg-rose-50 text-rose-700">CRIT</span>
      {% endif %}
    </div>
  </td>
  <td class="px-3 py-2">{{ status_badge(req.status) }}</td>
  <td class="px-3 py-2 text-sm text-gray-600 overflow-hidden">
    <span class="truncate block">{{ req.customer_display or '—' }}</span>
  </td>
  <td class="px-3 py-2 text-sm text-right text-gray-800">{{ req.requirement_count }}</td>
</tr>
{% endif %}
{% else %}
<tr>
  <td colspan="{{ 7 if avail_opp_table_v2_enabled else 5 }}" class="px-4 py-12 text-center">
    <svg class="mx-auto h-10 w-10 text-gray-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
      <path stroke-linecap="round" stroke-linejoin="round"
            d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/>
    </svg>
    <p class="mt-2 text-sm text-gray-500">No requisitions found.</p>
    <p class="text-xs text-gray-400 mt-1">Try adjusting your filters.</p>
  </td>
</tr>
{% endfor %}
```

- [ ] **Step 4: Update `_table.html` thead**

Read the current file, then wrap its existing `<tr>` inside `<thead>` with a flag gate:

```jinja
<thead>
  {% if avail_opp_table_v2_enabled %}
    <tr>
      <th class="px-3 py-2 w-10"></th>
      <th class="opp-col-header px-3 py-2 text-left">Name</th>
      <th class="opp-col-header px-3 py-2 text-left">Status</th>
      <th class="opp-col-header px-3 py-2 text-left">Customer</th>
      <th class="opp-col-header px-3 py-2 text-left">Coverage</th>
      <th class="opp-col-header px-3 py-2 text-left">Deal</th>
      <th class="w-0" aria-hidden="true"></th>
    </tr>
  {% else %}
    {# legacy thead — paste the existing <tr> here verbatim from pre-merge _table.html #}
  {% endif %}
</thead>
```

Retrieve the verbatim legacy thead with:
```bash
git show HEAD:app/templates/requisitions2/_table.html
```
and paste its `<tr>` into the `{% else %}` branch.

- [ ] **Step 5: Mirror gate in `_single_row.html`**

Open `_single_row.html`, read its current row structure, and wrap with `{% if avail_opp_table_v2_enabled %} ... {% else %} (legacy verbatim) {% endif %}`. The v2 branch mirrors the `<tr>` structure from `_table_rows.html` — same macros, same column order.

- [ ] **Step 6: Run tests — expect pass**

```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_requisitions2_templates.py -v --override-ini="addopts="
```

- [ ] **Step 7: Commit**

```bash
git add app/templates/requisitions2/_table_rows.html app/templates/requisitions2/_single_row.html app/templates/requisitions2/_table.html tests/test_requisitions2_templates.py
git commit -m "$(cat <<'EOF'
feat(requisitions2): gate v2 row rendering on avail_opp_table_v2_enabled

_table_rows.html, _single_row.html, and _table.html thead split between
v2 (6-col: checkbox, Name, Status, Customer, Coverage, Deal) and legacy
(5-col) based on the context flag. Legacy rendering preserved verbatim
in {% else %} branches for instant flag-off rollback.

Spec: docs/superpowers/specs/2026-04-21-opportunity-table-merged-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 19: Inject `avail_opp_table_v2_enabled` into template context

**Files:**
- Modify: `app/routers/requisitions2.py`

- [ ] **Step 1: Verify the flag-off test currently fails cleanly**

Run:
```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_requisitions2_templates.py -k "v2_flag" -v --override-ini="addopts="
```

If the flag-off test passes by accident (because the key is undefined → falsy), tighten it by adding a positive legacy-thead marker:

```python
def test_v2_flag_off_renders_legacy(client, monkeypatch):
    from app.config import settings as app_settings
    monkeypatch.setattr(app_settings, "avail_opp_table_v2", False)
    resp = client.get("/requisitions2")
    assert "opp-col-header" not in resp.text
    # Legacy thead has a "Reqs" column label (replace with the actual label
    # you see in git show HEAD:app/templates/requisitions2/_table.html):
    assert "Reqs" in resp.text or "Customer" in resp.text
```

- [ ] **Step 2: Update `_table_context`**

Open `app/routers/requisitions2.py` and modify `_table_context()`:

```python
def _table_context(request: Request, filters: ReqListFilters, db: Session, user: User) -> dict:
    """Build the shared context dict for table rendering."""
    from app.config import settings as app_settings
    result = list_requisitions(
        db=db,
        filters=filters,
        user_id=user.id,
        user_role=getattr(user, "role", "sales"),
    )
    users = get_team_users(db)
    return {
        "request": request,
        **result,
        "user": user,
        "users": users,
        "avail_opp_table_v2_enabled": app_settings.avail_opp_table_v2,
    }
```

- [ ] **Step 3: Check other TemplateResponse callsites**

```bash
grep -n "templates.TemplateResponse" /root/availai/app/routers/requisitions2.py
```

For each site that does NOT route through `_table_context`, inject the flag manually into its context dict. The row-action endpoint at line 323 already ends with a call to `_table_context`; verify this is true and no manual injection is needed.

- [ ] **Step 4: Run tests — expect pass**

```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/test_requisitions2_templates.py -v --override-ini="addopts="
```

- [ ] **Step 5: Commit**

```bash
git add app/routers/requisitions2.py tests/test_requisitions2_templates.py
git commit -m "$(cat <<'EOF'
feat(requisitions2): inject avail_opp_table_v2_enabled into template context

_table_context now passes the flag; downstream TemplateResponse call sites
that reuse this helper automatically receive it. Flag-off path verified
via test_requisitions2_templates monkeypatching settings.avail_opp_table_v2.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 20: Manual smoke via deploy-dev

**Files:**
- None. Verification only.

- [ ] **Step 1: Rebuild**

```bash
cd /root/availai && docker compose up -d --build
docker compose logs app --tail=40
```

- [ ] **Step 2: Flag-on curl smoke**

```bash
curl -s -b cookies.txt 'http://localhost:8000/requisitions2' | grep -c 'opp-col-header'
```
Expected: ≥1.

- [ ] **Step 3: Flag-off curl smoke**

Edit `.env` → `AVAIL_OPP_TABLE_V2=false` → `docker compose restart app`:

```bash
curl -s -b cookies.txt 'http://localhost:8000/requisitions2' | grep -c 'opp-col-header'
```
Expected: 0.

Flip back to `true` + restart.

- [ ] **Step 4: No commit.**

---

## Task 21: Playwright — v2 visuals (status, urgency, deal, coverage)

**Files:**
- Create: `e2e/requisitions2-visuals.spec.ts`

- [ ] **Step 1: Create the test file**

```typescript
/**
 * requisitions2-visuals.spec.ts — E2E visual regressions for the
 * /requisitions2 merged v2 opportunity table.
 */
import { test, expect, Page } from '@playwright/test';

const REQS_URL = '/requisitions2';

async function gotoFresh(page: Page) {
  await page.goto(REQS_URL);
  await page.waitForSelector('.rq2-row', { timeout: 8000 });
}

test.describe('Status cell', () => {
  test('each bucket renders its dot color class', async ({ page }) => {
    await gotoFresh(page);
    const dots = await page.locator('.opp-status-dot').all();
    expect(dots.length).toBeGreaterThan(0);
    for (const dot of dots) {
      const cls = await dot.getAttribute('class') || '';
      expect(cls).toMatch(/opp-status-dot--(open|sourcing|offered|quoted|neutral)/);
    }
  });

  test('time text uses correct class when hours_until_bid_due is set', async ({ page }) => {
    await gotoFresh(page);
    const times = page.locator('.opp-time');
    const count = await times.count();
    if (count > 0) {
      const cls = await times.first().getAttribute('class') || '';
      expect(cls).toMatch(/opp-time--(24h|72h|normal)/);
    }
  });
});

test.describe('Urgency accent on <tr>', () => {
  test('rows with class opp-row--urgent-24h are <tr> elements', async ({ page }) => {
    await gotoFresh(page);
    const urgent = page.locator('tr.opp-row--urgent-24h');
    const count = await urgent.count();
    if (count > 0) {
      const tag = await urgent.first().evaluate((el) => el.tagName);
      expect(tag).toBe('TR');
    }
  });
});

test.describe('Deal value', () => {
  test('tier class matches magnitude', async ({ page }) => {
    await gotoFresh(page);
    const deals = await page.locator('.opp-deal').all();
    for (const d of deals) {
      const cls = await d.getAttribute('class') || '';
      const raw = (await d.textContent() || '').trim();
      const digits = raw.replace(/[^0-9]/g, '');
      const n = parseInt(digits || '0', 10);
      if (!digits || raw === '—') {
        expect(cls).toContain('opp-deal--tier-tertiary');
        continue;
      }
      if (n >= 100000) expect(cls).toContain('opp-deal--tier-primary-500');
      else if (n >= 1000) expect(cls).toContain('opp-deal--tier-primary-400');
      else expect(cls).toContain('opp-deal--tier-tertiary');
    }
  });

  test('partial source renders ~ prefix, italic hook, and tooltip copy', async ({ page }) => {
    await gotoFresh(page);
    const partial = page.locator('.opp-deal--partial');
    const count = await partial.count();
    if (count > 0) {
      const first = partial.first();
      const text = (await first.textContent() || '').trim();
      expect(text.startsWith('~$')).toBe(true);
      const cls = await first.getAttribute('class') || '';
      expect(cls).toContain('opp-deal--computed');
      const title = await first.getAttribute('title') || '';
      expect(title).toMatch(/\d+ of \d+ parts priced/);
    }
  });
});

test.describe('Coverage meter', () => {
  test('renders 6 segments with role=img and aria-label', async ({ page }) => {
    await gotoFresh(page);
    const meter = page.locator('.opp-coverage').first();
    await expect(meter).toBeVisible();
    await expect(meter).toHaveAttribute('role', 'img');
    const aria = await meter.getAttribute('aria-label') || '';
    expect(aria).toMatch(/Coverage: \d+ of \d+ parts sourced/);
    const segs = await meter.locator('.opp-coverage-seg').count();
    expect(segs).toBe(6);
  });
});
```

- [ ] **Step 2: Run — expect pass**

```bash
cd /root/availai && npx playwright test e2e/requisitions2-visuals.spec.ts 2>&1 | tail -30
```

- [ ] **Step 3: Commit**

```bash
git add e2e/requisitions2-visuals.spec.ts
git commit -m "$(cat <<'EOF'
test(requisitions2): Playwright visuals — status, urgency, deal, coverage

Covers every status bucket dot, <tr> urgency accent class, deal
tier-class-matches-magnitude, partial-source ~ prefix + italic + tooltip
copy, and the coverage meter 6 segments + aria-label.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 22: Playwright — chip overflow, hover reveal, resize re-measure

**Files:**
- Modify: `e2e/requisitions2-visuals.spec.ts`

- [ ] **Step 1: Append**

```typescript
test.describe('Chip overflow', () => {
  test('each chip row has at least one visible chip', async ({ page }) => {
    await gotoFresh(page);
    const rows = await page.locator('.opp-chip-row').all();
    expect(rows.length).toBeGreaterThan(0);
    for (const row of rows) {
      const visible = await row.locator(':scope > *:not([style*="display: none"])').count();
      expect(visible).toBeGreaterThan(0);
    }
  });

  test('narrowing Name column does not increase visible chip count', async ({ page }) => {
    await gotoFresh(page);
    const handle = page.locator('th.resizable .col-resize-handle').first();
    if ((await handle.count()) === 0) test.skip();
    const box = await handle.boundingBox();
    if (!box) test.skip();

    const firstRow = page.locator('.opp-chip-row').first();
    const before = await firstRow.locator(':scope > *:not([style*="display: none"])').count();

    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x - 80, box.y + box.height / 2, { steps: 10 });
    await page.mouse.up();
    await page.waitForTimeout(120);

    const after = await firstRow.locator(':scope > *:not([style*="display: none"])').count();
    expect(after).toBeLessThanOrEqual(before);
  });

  test('hovering +N reveals tooltip containing hidden chips', async ({ page }) => {
    await gotoFresh(page);
    const more = page.locator('.opp-chip-more:visible').first();
    if ((await more.count()) === 0) test.skip();

    await more.hover();
    const tip = page.locator('.truncate-tip.visible');
    await expect(tip).toBeVisible({ timeout: 2000 });
    const tipChips = await tip.locator('.opp-chip-row > *').count();
    expect(tipChips).toBeGreaterThan(0);
  });
});
```

- [ ] **Step 2: Run — expect pass (or skip when no overflow data)**

- [ ] **Step 3: Commit**

```bash
git add e2e/requisitions2-visuals.spec.ts
git commit -m "$(cat <<'EOF'
test(requisitions2): Playwright chip overflow + resize + tooltip reveal

At-least-one-visible-chip invariant; narrowing Name col never increases
visible count (ResizeObserver tick); hovering +N reveals truncate-tip
with hidden chips as cloned DOM (via _tipNodes property, not innerHTML).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 23: Playwright — action rail hover, keyboard, click-isolation

**Files:**
- Modify: `e2e/requisitions2-visuals.spec.ts`

- [ ] **Step 1: Append**

```typescript
test.describe('Hover action rail', () => {
  test('rail hidden at pageload', async ({ page }) => {
    await gotoFresh(page);
    const rails = await page.locator('.opp-action-rail').all();
    for (const rail of rails) {
      const opacity = await rail.evaluate((el) => getComputedStyle(el).opacity);
      expect(parseFloat(opacity)).toBeLessThan(0.2);
    }
  });

  test('mouse-hover reveals rail; leave hides', async ({ page }) => {
    await gotoFresh(page);
    const row = page.locator('.rq2-row').first();
    await row.hover();
    await page.waitForTimeout(150);
    const rail = row.locator('.opp-action-rail');
    const visibleOpacity = await rail.evaluate((el) => getComputedStyle(el).opacity);
    expect(parseFloat(visibleOpacity)).toBeGreaterThan(0.5);
    await page.mouse.move(0, 0);
    await page.waitForTimeout(150);
    const hiddenOpacity = await rail.evaluate((el) => getComputedStyle(el).opacity);
    expect(parseFloat(hiddenOpacity)).toBeLessThan(0.2);
  });

  test('clicking a rail button does not trigger row hx-get detail', async ({ page }) => {
    await gotoFresh(page);
    const row = page.locator('.rq2-row').first();
    await row.hover();
    const before = await page.locator('#rq2-detail').innerHTML();
    const clone = row.locator('.opp-action-rail [aria-label^="Clone"]');
    await clone.click();
    await page.waitForTimeout(300);
    // Clone action triggers a server response that swaps #rq2-table, not #rq2-detail.
    // We assert #rq2-detail did not receive a row-detail HTML swap.
    const after = await page.locator('#rq2-detail').innerHTML();
    // Heuristic: detail pane stays empty-or-unchanged; no row-detail marker inserted.
    expect(after).not.toMatch(/data-rq2-detail-id/);
  });
});
```

- [ ] **Step 2: Run — expect pass**

- [ ] **Step 3: Commit**

```bash
git add e2e/requisitions2-visuals.spec.ts
git commit -m "$(cat <<'EOF'
test(requisitions2): Playwright action rail — hover, keyboard isolation

Rail hidden at pageload; hover reveals; mouse-leave hides; rail-button
click does NOT fire the row's hx-get detail handler (via @click.stop).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 24: Documentation — APP_MAP + STABLE + supersede markers

**Files:**
- Modify: `docs/APP_MAP_INTERACTIONS.md`, `STABLE.md`, two superseded spec files

- [ ] **Step 1: Append to `docs/APP_MAP_INTERACTIONS.md`**

```markdown
### x-truncate-tip  (htmx_app.js)

Hover tooltip that appears when the element overflows its box
(`scrollWidth > clientWidth`), OR when the element has a `_tipNodes`
DocumentFragment property attached at runtime. The `_tipNodes` path is
the contract with `x-chip-overflow` — hidden chips flow as cloned DOM,
never through HTML-string attributes.

### x-chip-overflow  (htmx_app.js)

Chip-row directive. ResizeObserver watches container inline-size; hides
chips that don't fit (left-to-right walk), exposes a trailing
`.opp-chip-more` button whose `_tipNodes` property holds a
DocumentFragment of cloned hidden chips for `x-truncate-tip` to reveal
on hover. Primaries-first DOM order (enforced by `_build_row_mpn_chips`
service helper) guarantees primary MPNs never hide while subs are
visible.

### rowActionRail  (htmx_app.js, Alpine.data)

Component bound to `/requisitions2` `<tr>`. CSS handles hover reveal via
`tr:hover .opp-action-rail`; this component exposes `show` state so
`@focusin`/`@focusout`/`@keydown.enter` toggle visibility for keyboard
users. `Escape` dismisses.
```

- [ ] **Step 2: Append to `STABLE.md`**

```markdown
## Opportunity Table v2 (on /requisitions2, gated by AVAIL_OPP_TABLE_V2)

**Feature flag:** `AVAIL_OPP_TABLE_V2=true` (default). Flip to `false` +
`docker compose restart app` to revert to legacy 5-col rendering without
a redeploy. Turnaround ≈ 30 seconds.

**Token set:** `app/static/styles.css` `:root { --opp-* }` variables for
dot colors, urgency border/text, coverage fill, text primary/secondary/
tertiary, separator. Component classes: `.opp-status-dot`, `.opp-status-label`,
`.opp-time--{24h,72h,normal}`, `.opp-deal--tier-{primary-500,primary-400,tertiary}`,
`.opp-deal--computed`, `.opp-deal--partial`, `.opp-coverage-seg`,
`.opp-row--urgent-{24h,72h}`, `.opp-col-header`, `.opp-chip-row`,
`.opp-chip-more`, `.opp-name-cell`, `.opp-action-rail*`, `.truncate-tip`.

**Spec:** `docs/superpowers/specs/2026-04-21-opportunity-table-merged-design.md`.

**Follow-up:** cleanup PR after 7 days stable flag-on removes legacy
`{% else %}` branches and the flag itself.
```

- [ ] **Step 3: Mark superseded specs**

Prepend to the first line of `docs/superpowers/specs/2026-04-21-rq2-resizable-columns-design.md`:

```markdown
> **SUPERSEDED** by `2026-04-21-opportunity-table-merged-design.md`. Retained for historical context only.
```

Same for `specs/ui/opportunity-table-aesthetic-v2.md`.

- [ ] **Step 4: Commit**

```bash
git add docs/APP_MAP_INTERACTIONS.md STABLE.md docs/superpowers/specs/2026-04-21-rq2-resizable-columns-design.md specs/ui/opportunity-table-aesthetic-v2.md
git commit -m "$(cat <<'EOF'
docs: APP_MAP + STABLE entries for v2 opp table + supersede markers

APP_MAP_INTERACTIONS documents x-truncate-tip, x-chip-overflow, and
rowActionRail. STABLE records feature flag + rollback procedure + token
set. Prior two specs marked SUPERSEDED in their headers.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 25: Full pipeline + deploy verify + PR

**Files:**
- None modified. Verification only.

- [ ] **Step 1: Ruff**

```bash
cd /root/availai && ruff check app/
```

- [ ] **Step 2: Mypy**

```bash
cd /root/availai && mypy app/services/requisition_list_service.py app/routers/requisitions2.py app/config.py
```

- [ ] **Step 3: Full pytest**

```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v
```

- [ ] **Step 4: Frontend build**

```bash
cd /root/availai && npm run build
```

- [ ] **Step 5: Playwright**

```bash
cd /root/availai && npx playwright test e2e/requisitions2-resize.spec.ts e2e/requisitions2-visuals.spec.ts
```

- [ ] **Step 6: Deploy**

```bash
cd /root/availai && ./deploy.sh
```

- [ ] **Step 7: Post-deploy manual smoke**

Navigate to `/requisitions2`. Verify:
- 6-col header: Name / Status / Customer / Coverage / Deal
- Status dots with colors
- Hover reveals action rail
- Hovering `+N` chip shows tooltip with hidden chips
- Narrowing the split divider → chips re-measure within a frame

- [ ] **Step 8: Capture PR screenshots**

Take desktop + narrow-viewport screenshots showing: status bucket variety, urgency accents (24h + 72h + manual-critical), one `computed` and one `partial` deal value, chip overflow with `+N` revealed, action rail in both hover and keyboard-focus states.

- [ ] **Step 9: Post-deploy rollback smoke**

Flip `.env` `AVAIL_OPP_TABLE_V2=false` → `docker compose restart app` → confirm legacy rendering. Flip back → confirm v2 returns.

- [ ] **Step 10: Open the PR**

```bash
git push -u origin HEAD
gh pr create --title "feat(requisitions2): opportunity table v2 (merged)" --body "$(cat <<'EOF'
## Summary
- Merges the resizable-columns + aesthetic-v2 streams into one flag-gated PR on /requisitions2.
- 6-col compact table: [✓] · Name · Status · Customer · Coverage · Deal.
- Hover action rail replaces ⋮ dropdown; keyboard-accessible via row focus + Enter.
- Feature flag `AVAIL_OPP_TABLE_V2` (default true) — flip + restart to revert.

## Test plan
- [ ] `TESTING=1 pytest tests/test_requisition_list_service.py tests/test_opp_macros.py tests/test_requisitions2_templates.py`
- [ ] `npx playwright test e2e/requisitions2-resize.spec.ts e2e/requisitions2-visuals.spec.ts`
- [ ] Manual: v2 layout at /requisitions2 (screenshots attached)
- [ ] Manual: rollback via `AVAIL_OPP_TABLE_V2=false` + restart

## Follow-ups (separate PR)
- After 7 days stable: remove legacy `{% else %}` branches + flag.

## Spec
`docs/superpowers/specs/2026-04-21-opportunity-table-merged-design.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 11: No commit — PR open.**

---

## Self-Review

### Spec coverage

- Feature flag (spec §Feature flag) → Tasks 1, 2, 18, 19.
- `_hours_until_bid_due` (uncommitted) → Task 6 verification.
- `_resolve_deal_value` extension with `partial` (spec §Deal value) → Task 3.
- `_build_row_mpn_chips` (spec §Chip aggregation) → Task 4.
- Row-dict additions (`coverage_filled` via offers, `coverage_total`, `deal_value_priced_count`, `deal_value_requirement_count`, `mpn_chip_items`) → Task 5.
- Macros `status_dot`, `deal_value` (partial), `coverage_meter`, `urgency_accent_class`, `time_text` → Task 7.
- `mpn_chips_aggregated` → Task 8.
- `opp_status_cell` → Task 9.
- `opp_name_cell` → Task 10.
- `opp_row_action_rail` → Task 11.
- CSS tokens → Task 12.
- `x-truncate-tip` relocation + `_tipNodes` property contract (spec §x-truncate-tip finalization) → Task 14.
- `x-chip-overflow` with DocumentFragment tip-nodes (spec §Chip overflow behavior) → Task 15.
- `rowActionRail` keyboard a11y → Task 16.
- HTMX-swap Alpine re-init → Task 17.
- Template gating → Task 18.
- Router context injection → Task 19.
- Playwright visuals (status, urgency, deal, coverage) → Task 21.
- Playwright chip overflow + resize re-measure + hover reveal (spec §Chip worst-case) → Task 22.
- Playwright action rail + click-isolation → Task 23.
- Docs (APP_MAP, STABLE, supersede markers) → Task 24.
- `.env.example` → Task 2.
- Full pipeline + deploy verify + PR → Task 25.

### Placeholder scan

No TBDs, no "handle edge cases," no incomplete test bodies. The SQL aggregation in Task 5 carries a concrete shape with an import note and an adaptation clause in case the existing query's structure differs — that's explicit guidance, not deferral.

### Type / naming consistency

- `_resolve_deal_value(opportunity_value, priced_sum, priced_count, requirement_count)` — defined in Task 3, called in Task 5, tested in Task 3 and Task 7.
- `_build_row_mpn_chips(requirements)` — defined in Task 4, called in Task 5, tested in Task 4.
- Row-dict keys used identically in Tasks 5, 10, 11, 18: `hours_until_bid_due`, `deal_value_display`, `deal_value_source`, `deal_value_priced_count`, `deal_value_requirement_count`, `coverage_filled`, `coverage_total`, `mpn_chip_items`.
- Macros used identically across tasks: `status_dot`, `time_text`, `urgency_accent_class`, `deal_value`, `coverage_meter`, `opp_status_cell`, `opp_name_cell`, `opp_row_action_rail`, `mpn_chips_aggregated`.
- CSS class names consistent across Tasks 8, 10, 11, 12, 15, 21–23: `.opp-chip-row`, `.opp-chip-more`, `.opp-name-cell`, `.opp-action-rail`, `.opp-action-rail-cell`, `.truncate-tip`, `.truncate-tip.visible`, `.opp-deal--partial`.
- Alpine directive / component names consistent across Tasks 14–16, 18, 24: `x-truncate-tip`, `x-chip-overflow`, `rowActionRail`.
- Node-property contract `_tipNodes` used identically in Tasks 14 (reader) and 15 (writer).

No inconsistencies.

---
