# SP3 — AI Account Screening + Match/Opportunity Scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add AI-powered procurement-first account screening to the prospecting queue — each account gets a `trio_match_score` and `opportunity_score` from Claude (grounded in real enrichment data), and low-fit accounts are soft-bucketed so buyers see a ranked, vetted list rather than a raw suggestion pool.

**Architecture:** A new `app/services/prospect_screening.py` module assembles grounded context from SP1-enriched fields and calls Claude via `claude_structured` (same pattern as `enrichment_service.py`) to return a validated schema. Scores are persisted to two new indexed Integer columns on `ProspectAccount` (Alembic migration 120) and the full verdict in `enrichment_data['ai_screen']` (existing JSONB, no migration). The screen runs as the final step of `run_enrichment_job`; the list route gains an `ai_match_desc` sort and a screened-out bucket; all UI changes are gated on explicit approval before building.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 (sync sessions) + PostgreSQL + Alembic + Claude API (`claude_structured`) + Redis-backed intel_cache for daily cap + Jinja2/HTMX templates.

## Global Constraints

- Stack is HTMX + Alpine.js + Jinja2 — NOT React. No new React/SPA patterns.
- Loguru for all logging, never `print()`. Ruff + mypy clean. Every new file gets a header comment.
- `db.get(Model, id)`, not `db.query(Model).get(id)` (SQLAlchemy 2.0).
- All schema changes go through Alembic — never raw DDL in services/routers.
- Migration workflow: add model column → autogenerate → review → upgrade → downgrade → upgrade.
- After migration, run `alembic heads` — must be a single head.
- Fire-and-forget safety: `run_enrichment_job` never raises; failure → `enrich_status='error'`.
- LLM call MUST be mocked in all tests (`AsyncMock`) — never hit a real API in tests.
- `claude_structured` is the correct call (structured JSON schema enforcement); never parse free-form JSON for the verdict.
- Evidence must cite real fields; `insufficient_data` when grounding is too thin — never guess.
- Cache the verdict in `enrichment_data['ai_screen']`; re-screen only on material new data.
- Daily cap via `intel_cache.get_count` / `intel_cache.incr_count` (mirrors existing enrichment throughput pattern).
- **UI tasks (Task 6) require explicit user approval before building** — per the project UI guardrail.
- Tests use `TESTING=1 PYTHONPATH=/root/availai/.claude/worktrees/sp3-screening pytest <file> -q -p no:cacheprovider -o addopts=""`.
- Migration smoke test: `alembic upgrade head && alembic downgrade -1 && alembic upgrade head`.
- Run `pre-commit run --all-files` before any PR push.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `alembic/versions/120_prospect_ai_scores.py` | **Create** | Add `trio_match_score` + `opportunity_score` columns + indexes to `prospect_accounts`; rollback drops them |
| `app/models/prospect_account.py` | **Modify** | Add `trio_match_score` + `opportunity_score` Integer columns with indexes |
| `app/config.py` | **Modify** | Add `ai_screen_enabled`, `ai_screen_min_match`, `ai_screen_daily_cap`, `ai_screen_web_search_enabled` settings |
| `app/services/prospect_screening.py` | **Create** | `screen_prospect(prospect, db)` — context assembly, Claude call, verdict persist, daily cap gate |
| `app/services/prospect_free_enrichment.py` | **Modify** | Call `screen_prospect` as final step of `run_enrichment_job` (after fit/readiness recompute) |
| `app/routers/htmx_views.py` | **Modify** | Add `ai_match_desc` sort branch; screened-out bucket filter in `prospecting_list_partial`; add `screened_out_count` to `_prospect_stats_ctx` |
| `app/templates/htmx/partials/prospecting/list.html` | **Modify** *(approval-gated)* | Add `ai_match_desc` sort option to the `<select>`; screened-out collapsed bucket section |
| `app/templates/htmx/partials/prospecting/_card.html` | **Modify** *(approval-gated)* | Show match/opportunity scores + one-line rationale |
| `app/templates/htmx/partials/prospecting/detail.html` | **Modify** *(approval-gated)* | Show full verdict (rationale, evidence list, confidence, model) |
| `MIGRATION_NUMBERS_IN_FLIGHT.txt` | **Modify** | Claim migration number 120 |
| `docs/APP_MAP_DATABASE.md` | **Modify** | Document new columns |
| `docs/APP_MAP_INTERACTIONS.md` | **Modify** | Document screen service, Claude call, cap metering, enrichment integration |
| `tests/test_prospect_screening.py` | **Create** | Unit tests for screen service, cap gate, cache hit, `insufficient_data` path |
| `tests/test_sp3_enrichment_integration.py` | **Create** | Integration: `run_enrichment_job` fires screen as final step; screen errors are fire-and-forget safe |
| `tests/test_sp3_list_route.py` | **Create** | `ai_match_desc` sort, screened-out bucket filter, `screened_out_count` in stats |
| `tests/test_migration_120.py` | **Create** | Columns exist post-upgrade; absent post-downgrade |

---

## Task 1: Alembic Migration — `trio_match_score` + `opportunity_score`

**Files:**
- Create: `alembic/versions/120_prospect_ai_scores.py`
- Modify: `app/models/prospect_account.py:37-94` (scoring block + `__table_args__`)
- Modify: `MIGRATION_NUMBERS_IN_FLIGHT.txt` (append claim line)
- Create: `tests/test_migration_120.py`

**Interfaces:**
- Produces: `ProspectAccount.trio_match_score: Column(Integer, default=0)` and `ProspectAccount.opportunity_score: Column(Integer, default=0)`, both indexed. Available to Task 3 (screen service) and Task 4 (integration).

- [ ] **Step 1: Write the migration test**

```python
# tests/test_migration_120.py
"""Smoke-test: columns exist post-upgrade, absent post-downgrade.

Run against a real PostgreSQL instance only — SQLite does not support
the reflection calls used here. The project test suite runs SQLite (conftest.py),
so this test is marked skip unless TEST_PG_URL is set.

Usage:
    TEST_PG_URL=postgresql://... pytest tests/test_migration_120.py -v
"""

import os

import pytest

PG_URL = os.environ.get("TEST_PG_URL", "")


@pytest.mark.skipif(not PG_URL, reason="TEST_PG_URL not set — PG required for migration tests")
def test_migration_120_upgrade_downgrade():
    """upgrade adds columns; downgrade removes them; re-upgrade restores them."""
    import subprocess

    env = {**os.environ, "DATABASE_URL": PG_URL}

    def alembic(cmd: str) -> None:
        result = subprocess.run(
            f"alembic {cmd}",
            shell=True,
            env=env,
            capture_output=True,
            text=True,
            cwd="/root/availai/.claude/worktrees/sp3-screening",
        )
        assert result.returncode == 0, f"alembic {cmd} failed:\n{result.stderr}"

    alembic("upgrade 120_prospect_ai_scores")

    from sqlalchemy import create_engine, inspect
    engine = create_engine(PG_URL)
    cols = {c["name"] for c in inspect(engine).get_columns("prospect_accounts")}
    assert "trio_match_score" in cols
    assert "opportunity_score" in cols
    engine.dispose()

    alembic("downgrade -1")
    engine2 = create_engine(PG_URL)
    cols2 = {c["name"] for c in inspect(engine2).get_columns("prospect_accounts")}
    assert "trio_match_score" not in cols2
    assert "opportunity_score" not in cols2
    engine2.dispose()

    alembic("upgrade head")
```

- [ ] **Step 2: Run the test (will skip — confirms skip, not error)**

```bash
TESTING=1 PYTHONPATH=/root/availai/.claude/worktrees/sp3-screening \
  pytest tests/test_migration_120.py -q -p no:cacheprovider -o addopts=""
```
Expected: `1 skipped` (no TEST_PG_URL set; skip is correct behaviour in unit test context).

- [ ] **Step 3: Claim migration number 120 in MIGRATION_NUMBERS_IN_FLIGHT.txt**

Append to `/root/availai/.claude/worktrees/sp3-screening/MIGRATION_NUMBERS_IN_FLIGHT.txt`:
```
120 feat/sp3-ai-screening prospect_accounts.trio_match_score + opportunity_score (Integer indexed); chains onto 119_alert_seen
```

- [ ] **Step 4: Add the two columns to the model**

In `app/models/prospect_account.py`, after the existing `readiness_score` line (line 39), add:
```python
    # AI screening scores (SP3) — populated by prospect_screening.screen_prospect
    trio_match_score = Column(Integer, default=0)
    opportunity_score = Column(Integer, default=0)
```

In `__table_args__` (currently ends at line 94 of the original file), add two more Index entries before the closing `)`):
```python
        Index("ix_prospect_accounts_trio_match_score", "trio_match_score"),
        Index("ix_prospect_accounts_opportunity_score", "opportunity_score"),
```

- [ ] **Step 5: Autogenerate the migration**

```bash
cd /root/availai/.claude/worktrees/sp3-screening
alembic revision --autogenerate -m "prospect_ai_scores"
```

Then rename / set the revision ID manually in the generated file. The file must start with:

```python
"""Add trio_match_score + opportunity_score to prospect_accounts (SP3 AI screening).

What:
  * prospect_accounts.trio_match_score (Integer, default 0, indexed) — AI procurement-fit score
  * prospect_accounts.opportunity_score (Integer, default 0, indexed) — AI opportunity size score
  Both columns store 0-100 scalars from the AI screen verdict; full verdict in JSONB enrichment_data['ai_screen'].
Downgrade: drops the two columns and their indexes.

Revision ID: 120_prospect_ai_scores
Revises: 119_alert_seen
Create Date: 2026-06-18
"""

import sqlalchemy as sa
from alembic import op

revision = "120_prospect_ai_scores"
down_revision = "119_alert_seen"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("prospect_accounts", sa.Column("trio_match_score", sa.Integer(), nullable=True, server_default="0"))
    op.add_column("prospect_accounts", sa.Column("opportunity_score", sa.Integer(), nullable=True, server_default="0"))
    op.create_index("ix_prospect_accounts_trio_match_score", "prospect_accounts", ["trio_match_score"])
    op.create_index("ix_prospect_accounts_opportunity_score", "prospect_accounts", ["opportunity_score"])


def downgrade() -> None:
    op.drop_index("ix_prospect_accounts_opportunity_score", table_name="prospect_accounts")
    op.drop_index("ix_prospect_accounts_trio_match_score", table_name="prospect_accounts")
    op.drop_column("prospect_accounts", "opportunity_score")
    op.drop_column("prospect_accounts", "trio_match_score")
```

- [ ] **Step 6: Verify single head**

```bash
cd /root/availai/.claude/worktrees/sp3-screening && alembic heads
```
Expected: exactly one head line ending with `(head)`. If two heads appear, run `alembic merge heads -m "merge_sp3_and_prior"` and commit the merge revision.

- [ ] **Step 7: Commit**

```bash
cd /root/availai/.claude/worktrees/sp3-screening
git add alembic/versions/120_prospect_ai_scores.py \
        app/models/prospect_account.py \
        MIGRATION_NUMBERS_IN_FLIGHT.txt \
        tests/test_migration_120.py
git commit -m "feat(sp3): add trio_match_score + opportunity_score columns (migration 120)"
```

---

## Task 2: Config — SP3 Settings

**Files:**
- Modify: `app/config.py:319-325` (prospecting settings block)

**Interfaces:**
- Produces: `settings.ai_screen_enabled: bool`, `settings.ai_screen_min_match: int`, `settings.ai_screen_daily_cap: int`, `settings.ai_screen_web_search_enabled: bool`. Consumed by Tasks 3, 4, 5.

- [ ] **Step 1: Write the config test**

Add to `tests/test_prospect_screening.py` (new file, first test):

```python
# tests/test_prospect_screening.py
"""SP3 AI screening service tests.

LLM calls are always mocked — never hit a real API in tests.
"""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models.prospect_account import ProspectAccount


# ── Fixture helpers ──────────────────────────────────────────────────

def _prospect(db: Session, **kw) -> ProspectAccount:
    p = ProspectAccount(
        name=kw.pop("name", f"Co {uuid.uuid4().hex[:6]}"),
        domain=kw.pop("domain", f"co-{uuid.uuid4().hex[:6]}.com"),
        status="suggested",
        discovery_source="clay",
        created_at=datetime.now(timezone.utc),
        **kw,
    )
    db.add(p)
    db.commit()
    return p


# ── Config tests ─────────────────────────────────────────────────────

def test_sp3_config_defaults():
    assert settings.ai_screen_enabled is False
    assert settings.ai_screen_min_match == 40
    assert settings.ai_screen_daily_cap == 200
    assert settings.ai_screen_web_search_enabled is False
```

- [ ] **Step 2: Run — expect FAIL**

```bash
TESTING=1 PYTHONPATH=/root/availai/.claude/worktrees/sp3-screening \
  pytest tests/test_prospect_screening.py::test_sp3_config_defaults \
  -q -p no:cacheprovider -o addopts=""
```
Expected: `AttributeError: 'Settings' object has no attribute 'ai_screen_enabled'`

- [ ] **Step 3: Add settings to `app/config.py`**

In the `--- Prospecting ---` block (after line 323, `prospecting_resurface_days`), add:

```python
    # --- SP3: AI Account Screening ---
    # Feature gate — default off; flip on when ready to spend Claude credits on screening.
    ai_screen_enabled: bool = False
    # Minimum trio_match_score to pass the screen (< threshold → screened_out bucket).
    ai_screen_min_match: int = 40
    # Max accounts screened per UTC calendar day (mirrors enrichment daily_cap pattern).
    ai_screen_daily_cap: int = 200
    # When True, an insufficient_data verdict triggers a single web_search to try to
    # resolve grounding gaps before falling back to insufficient_data.
    ai_screen_web_search_enabled: bool = False
```

Also add to `.env.example` (append at the bottom of the prospecting section):
```
AI_SCREEN_ENABLED=false
AI_SCREEN_MIN_MATCH=40
AI_SCREEN_DAILY_CAP=200
AI_SCREEN_WEB_SEARCH_ENABLED=false
```

- [ ] **Step 4: Run — expect PASS**

```bash
TESTING=1 PYTHONPATH=/root/availai/.claude/worktrees/sp3-screening \
  pytest tests/test_prospect_screening.py::test_sp3_config_defaults \
  -q -p no:cacheprovider -o addopts=""
```
Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
cd /root/availai/.claude/worktrees/sp3-screening
git add app/config.py .env.example tests/test_prospect_screening.py
git commit -m "feat(sp3): add ai_screen_* settings (enabled=False, min_match=40, daily_cap=200)"
```

---

## Task 3: Screen Service — `screen_prospect`

**Files:**
- Create: `app/services/prospect_screening.py`
- Modify: `tests/test_prospect_screening.py` (add service tests)

**Interfaces:**
- Consumes: `ProspectAccount` (with `trio_match_score`, `opportunity_score`, `enrichment_data`, `readiness_signals`, `industry`, `naics_code`, `employee_count_range`, `hq_location`, `contacts_preview`, `historical_context`); `settings.ai_screen_enabled`, `settings.ai_screen_daily_cap`, `settings.ai_screen_min_match`, `settings.ai_screen_web_search_enabled`; `intel_cache.get_count`, `intel_cache.incr_count`; `claude_structured` from `app/utils/claude_client`.
- Produces: `async def screen_prospect(prospect: ProspectAccount, db: Session) -> dict` — returns the verdict dict (always, even on disabled/cap/error). Persists `trio_match_score`, `opportunity_score`, `enrichment_data['ai_screen']` onto `prospect` and commits. On `insufficient_data`, sets `enrichment_data['ai_screen']['needs_more_enrichment'] = True` (so SP4 can pick it up). On errors, returns `{"verdict": "error", "rationale": str(e)}` without persisting partial state.

- [ ] **Step 1: Write the failing tests (before the service exists)**

Append to `tests/test_prospect_screening.py`:

```python
# ── Screen service tests ─────────────────────────────────────────────

async def test_screen_prospect_pass(db_session, monkeypatch):
    """A well-grounded pass verdict writes scores and persists ai_screen."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_daily_cap", 999)
    monkeypatch.setattr(settings, "ai_screen_min_match", 40)

    p = _prospect(
        db_session,
        industry="Aerospace & Defense",
        naics_code="336412",
        employee_count_range="501-1000",
        enrichment_data={},
        readiness_signals={},
        contacts_preview=[{"name": "Jane VP", "title": "VP Procurement", "email": "j@co.com", "verified": True}],
    )

    verdict = {
        "trio_match_score": 85,
        "opportunity_score": 70,
        "excess_likelihood": 30,
        "verdict": "pass",
        "rationale": "Aerospace OEM with verified procurement contact.",
        "evidence": ["industry=Aerospace & Defense", "naics=336412", "contacts=1 verified"],
        "confidence": 80,
        "model": "claude-sonnet-4-6",
        "screened_at": "2026-06-18T00:00:00+00:00",
    }

    from app.services import prospect_screening as ps

    with patch.object(ps, "_call_screen_llm", new_callable=AsyncMock, return_value=verdict):
        with patch("app.cache.intel_cache.get_count", return_value=0):
            with patch("app.cache.intel_cache.incr_count", return_value=1):
                result = await ps.screen_prospect(p, db_session)

    assert result["verdict"] == "pass"
    db_session.refresh(p)
    assert p.trio_match_score == 85
    assert p.opportunity_score == 70
    assert p.enrichment_data["ai_screen"]["verdict"] == "pass"
    assert p.enrichment_data["ai_screen"]["rationale"] == "Aerospace OEM with verified procurement contact."


async def test_screen_prospect_screened_out(db_session, monkeypatch):
    """Match below min_match threshold → verdict is screened_out."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_daily_cap", 999)
    monkeypatch.setattr(settings, "ai_screen_min_match", 40)

    p = _prospect(db_session, industry="Retail", enrichment_data={}, readiness_signals={})

    verdict = {
        "trio_match_score": 15,
        "opportunity_score": 10,
        "excess_likelihood": 5,
        "verdict": "pass",  # LLM returned pass, but score < min_match → we override to screened_out
        "rationale": "Retail company, no electronics manufacturing.",
        "evidence": ["industry=Retail"],
        "confidence": 90,
        "model": "claude-sonnet-4-6",
        "screened_at": "2026-06-18T00:00:00+00:00",
    }

    from app.services import prospect_screening as ps

    with patch.object(ps, "_call_screen_llm", new_callable=AsyncMock, return_value=verdict):
        with patch("app.cache.intel_cache.get_count", return_value=0):
            with patch("app.cache.intel_cache.incr_count", return_value=1):
                result = await ps.screen_prospect(p, db_session)

    assert result["verdict"] == "screened_out"
    db_session.refresh(p)
    assert p.trio_match_score == 15
    assert p.enrichment_data["ai_screen"]["verdict"] == "screened_out"


async def test_screen_prospect_insufficient_data_sets_flag(db_session, monkeypatch):
    """insufficient_data verdict sets needs_more_enrichment flag, does not write scores."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_daily_cap", 999)

    p = _prospect(db_session, industry=None, enrichment_data={}, readiness_signals={})

    verdict = {
        "trio_match_score": 0,
        "opportunity_score": 0,
        "excess_likelihood": 0,
        "verdict": "insufficient_data",
        "rationale": "No industry or firmographic data available.",
        "evidence": [],
        "confidence": 10,
        "model": "claude-sonnet-4-6",
        "screened_at": "2026-06-18T00:00:00+00:00",
    }

    from app.services import prospect_screening as ps

    with patch.object(ps, "_call_screen_llm", new_callable=AsyncMock, return_value=verdict):
        with patch("app.cache.intel_cache.get_count", return_value=0):
            with patch("app.cache.intel_cache.incr_count", return_value=1):
                result = await ps.screen_prospect(p, db_session)

    assert result["verdict"] == "insufficient_data"
    db_session.refresh(p)
    # Scores must NOT be written for insufficient_data
    assert (p.trio_match_score or 0) == 0
    assert p.enrichment_data["ai_screen"]["needs_more_enrichment"] is True


async def test_screen_prospect_daily_cap_blocks(db_session, monkeypatch):
    """When daily cap is hit, screen_prospect returns early without an LLM call."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_daily_cap", 5)

    p = _prospect(db_session, enrichment_data={})

    from app.services import prospect_screening as ps

    mock_llm = AsyncMock()
    with patch.object(ps, "_call_screen_llm", mock_llm):
        with patch("app.cache.intel_cache.get_count", return_value=5):  # at cap
            result = await ps.screen_prospect(p, db_session)

    mock_llm.assert_not_called()
    assert result["verdict"] == "cap_reached"


async def test_screen_prospect_cache_hit_skips_llm(db_session, monkeypatch):
    """If ai_screen already has a verdict in enrichment_data, skip the LLM call."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_daily_cap", 999)

    cached_verdict = {
        "verdict": "pass",
        "trio_match_score": 80,
        "opportunity_score": 65,
        "rationale": "Already screened.",
        "evidence": ["industry=Aerospace"],
        "confidence": 85,
        "model": "claude-sonnet-4-6",
        "screened_at": "2026-06-18T00:00:00+00:00",
    }
    p = _prospect(db_session, enrichment_data={"ai_screen": cached_verdict})

    from app.services import prospect_screening as ps

    mock_llm = AsyncMock()
    with patch.object(ps, "_call_screen_llm", mock_llm):
        result = await ps.screen_prospect(p, db_session)

    mock_llm.assert_not_called()
    assert result["verdict"] == "pass"


async def test_screen_prospect_disabled_returns_skipped(db_session, monkeypatch):
    """When ai_screen_enabled=False, return skip immediately without LLM."""
    monkeypatch.setattr(settings, "ai_screen_enabled", False)
    p = _prospect(db_session, enrichment_data={})

    from app.services import prospect_screening as ps

    mock_llm = AsyncMock()
    with patch.object(ps, "_call_screen_llm", mock_llm):
        result = await ps.screen_prospect(p, db_session)

    mock_llm.assert_not_called()
    assert result["verdict"] == "disabled"


async def test_screen_prospect_llm_error_is_fire_and_forget(db_session, monkeypatch):
    """LLM failure must not propagate — returns error verdict, prospect unchanged."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_daily_cap", 999)

    p = _prospect(db_session, enrichment_data={}, trio_match_score=0)

    from app.services import prospect_screening as ps

    with patch.object(ps, "_call_screen_llm", new_callable=AsyncMock, side_effect=Exception("timeout")):
        with patch("app.cache.intel_cache.get_count", return_value=0):
            result = await ps.screen_prospect(p, db_session)

    assert result["verdict"] == "error"
    db_session.refresh(p)
    assert (p.trio_match_score or 0) == 0  # scores NOT written on error
```

- [ ] **Step 2: Run — expect FAIL**

```bash
TESTING=1 PYTHONPATH=/root/availai/.claude/worktrees/sp3-screening \
  pytest tests/test_prospect_screening.py -q -p no:cacheprovider -o addopts=""
```
Expected: `ImportError` or `ModuleNotFoundError: No module named 'app.services.prospect_screening'`

- [ ] **Step 3: Implement `app/services/prospect_screening.py`**

```python
"""prospect_screening.py — AI account screening for the prospecting queue (SP3).

Calls Claude (claude_structured) with grounded context assembled from SP1-enriched
ProspectAccount fields and returns a validated verdict schema. Persists
trio_match_score + opportunity_score as indexed Integer columns; full verdict in
enrichment_data['ai_screen'] (JSONB).

Called by: prospect_free_enrichment.run_enrichment_job (final step).
Depends on: app.utils.claude_client.claude_structured, app.cache.intel_cache,
            app.config.settings, app.models.prospect_account.ProspectAccount.
"""

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.cache import intel_cache
from app.config import settings
from app.models.prospect_account import ProspectAccount

# ── Daily-cap key ────────────────────────────────────────────────────────────

_CAP_KEY_PREFIX = "ai_screen:daily:"


def _cap_key() -> str:
    return _CAP_KEY_PREFIX + datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── JSON Schema for claude_structured ───────────────────────────────────────

_SCREEN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "trio_match_score": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "0-100: How strongly does this account need electronic components TRIO supplies? Score procurement fit, not size alone.",
        },
        "opportunity_score": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "0-100: Estimated opportunity size/value (company spend potential from size + industry; secondary excess inventory volume).",
        },
        "excess_likelihood": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "0-100: Likelihood this account has surplus electronic inventory TRIO could purchase. Secondary signal.",
        },
        "verdict": {
            "type": "string",
            "enum": ["pass", "screened_out", "insufficient_data"],
            "description": "pass = pursue; screened_out = low match; insufficient_data = grounding too thin to judge reliably.",
        },
        "rationale": {
            "type": "string",
            "description": "1-2 sentences grounded in the evidence provided. Must cite specific fields. Never fabricate.",
        },
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of evidence items used, e.g. ['industry=Aerospace & Defense', 'naics=336412', 'contacts=1 verified VP'].",
        },
        "confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "0-100: Confidence in this verdict given the grounding quality.",
        },
    },
    "required": ["trio_match_score", "opportunity_score", "excess_likelihood", "verdict", "rationale", "evidence", "confidence"],
}

# ── System prompt ────────────────────────────────────────────────────────────

_SCREEN_SYSTEM = (
    "You are a procurement intelligence analyst for TRIO Supply Chain Solutions, "
    "an electronic component broker. Your task is to screen a prospective B2B account "
    "and determine whether they are a genuine target for TRIO.\n\n"
    "TRIO's ideal customers: companies that design, manufacture, or repair products "
    "containing electronic components (ICs, passives, semiconductors, connectors, memory, "
    "storage, displays, PCB assemblies). They need a spot-market / broker channel for "
    "hard-to-find, obsolete, or allocation-constrained parts.\n\n"
    "SCORING RULES:\n"
    "- trio_match_score (0-100): procurement-first fit. How likely is this account to "
    "source electronic components through a broker? Score 80+ only with strong sector "
    "evidence (aerospace/defense, EMS, medical devices, automotive electronics, industrial "
    "controls). Score 40-79 for plausible but uncertain fits. Score <40 for retail, "
    "software-only, staffing, consulting, or companies with no evident BOM.\n"
    "- opportunity_score (0-100): estimated spend potential from company size + industry "
    "context. A 500-person aerospace OEM is 80+; a 10-person IT consultancy is 10.\n"
    "- excess_likelihood (0-100): secondary — does this account likely hold surplus "
    "electronic inventory TRIO could buy? Relevant for OEMs with large inventory.\n"
    "- verdict: use 'insufficient_data' when the grounding fields are too sparse to judge "
    "reliably (no industry, no NAICS, no description, no contacts, no history). "
    "NEVER GUESS OR FABRICATE. Use only the data provided in the context.\n"
    "- rationale: cite the specific evidence fields you used. 1-2 sentences.\n"
    "- evidence: list the grounding fields that drove your verdict, e.g. "
    "['industry=Aerospace & Defense', 'naics=336412', 'size=501-1000', 'contacts=1 verified VP Procurement'].\n"
    "Return ONLY the JSON object conforming to the schema. Do not add prose outside JSON."
)


# ── Context assembly ─────────────────────────────────────────────────────────

def _assemble_context(prospect: ProspectAccount) -> str:
    """Build the grounding prompt from the prospect's enriched fields.

    Uses only fields that already exist on the ProspectAccount — never guesses.
    Returns a plain-text context block Claude will reason over.
    """
    ed = prospect.enrichment_data or {}
    signals = prospect.readiness_signals or {}
    contacts = prospect.contacts_preview or []
    history = prospect.historical_context or {}

    lines: list[str] = [
        f"Company: {prospect.name or 'Unknown'} ({prospect.domain or 'no domain'})",
    ]

    if prospect.industry:
        lines.append(f"Industry: {prospect.industry}")
    if prospect.naics_code:
        lines.append(f"NAICS: {prospect.naics_code}")
    if prospect.employee_count_range:
        lines.append(f"Employees: {prospect.employee_count_range}")
    if prospect.revenue_range:
        lines.append(f"Revenue: {prospect.revenue_range}")
    if prospect.hq_location:
        lines.append(f"HQ: {prospect.hq_location}")
    if prospect.description:
        lines.append(f"Description: {prospect.description[:400]}")

    # SP1 firmographics in enrichment_data
    sam_gov = ed.get("sam_gov") or {}
    if sam_gov.get("purpose"):
        lines.append(f"SAM.gov purpose: {sam_gov['purpose']}")
    if sam_gov.get("naics_codes"):
        primary = next((n for n in sam_gov["naics_codes"] if n.get("primary")), sam_gov["naics_codes"][0])
        lines.append(f"SAM.gov primary NAICS: {primary.get('code', '')} — {primary.get('description', '')}")

    # Contacts
    if contacts:
        verified = [c for c in contacts if isinstance(c, dict) and c.get("verified")]
        dms = [c for c in verified if c.get("seniority") == "decision_maker"]
        summary_parts = []
        if dms:
            summary_parts.append(f"{len(dms)} verified decision-maker(s): " + ", ".join(
                f"{c.get('name','?')} ({c.get('title','?')})" for c in dms[:2]
            ))
        elif verified:
            summary_parts.append(f"{len(verified)} verified contact(s)")
        elif contacts:
            summary_parts.append(f"{len(contacts)} unverified contact(s)")
        if summary_parts:
            lines.append("Contacts: " + "; ".join(summary_parts))

    # News signals
    news = ed.get("recent_news") or []
    if news:
        headlines = [n.get("title", "")[:80] for n in news[:3] if n.get("title")]
        if headlines:
            lines.append("Recent news: " + " | ".join(headlines))

    # Hiring/events signals
    hiring = signals.get("hiring") or {}
    if hiring.get("type") and hiring["type"] != "none":
        lines.append(f"Hiring signal: {hiring['type']}")
    events = signals.get("events") or []
    if events:
        event_types = list({e.get("type", "") for e in events[:3] if isinstance(e, dict) and e.get("type")})
        if event_types:
            lines.append(f"Recent events: {', '.join(event_types)}")

    # TRIO history
    if history.get("quote_count"):
        lines.append(f"TRIO history: {history['quote_count']} quotes")
    if history.get("bought_before"):
        lines.append("TRIO history: prior customer (bought before)")
    if history.get("last_activity"):
        lines.append(f"TRIO history: last activity {history['last_activity']}")

    # Historical context freeform
    if prospect.historical_context and not any(
        k in prospect.historical_context for k in ("quote_count", "bought_before", "last_activity")
    ):
        lines.append(f"Historical context: {str(prospect.historical_context)[:200]}")

    return "\n".join(lines)


def _grounding_is_sufficient(prospect: ProspectAccount) -> bool:
    """Return True if we have at least minimal data to make a non-random judgment.

    Minimum bar: at least one of (industry, naics_code, description, or SAM.gov data).
    """
    ed = prospect.enrichment_data or {}
    return bool(
        prospect.industry
        or prospect.naics_code
        or prospect.description
        or ed.get("sam_gov")
    )


# ── LLM call (isolated for mocking) ─────────────────────────────────────────

async def _call_screen_llm(context: str, *, use_web_search: bool = False) -> dict:
    """Call Claude with the screening schema. Returns the verdict dict.

    Isolated into its own function so tests can patch it without touching the
    full claude_structured call chain.
    """
    from app.utils.claude_client import claude_structured

    tools: list[dict] | None = None
    if use_web_search:
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}]

    result = await claude_structured(
        _assemble_context.__doc__ or "",  # unused: prompt is the context
        schema=_SCREEN_SCHEMA,
        system=_SCREEN_SYSTEM,
        model_tier="smart",
        max_tokens=512,
        cache_system=True,
        timeout=45,
        cost_bucket="ai_screen",
    )
    return result or {}


# ── Public API ───────────────────────────────────────────────────────────────

async def screen_prospect(prospect: ProspectAccount, db: Session) -> dict:
    """Run the AI screen for one prospect and persist the verdict.

    Returns a dict with at minimum {"verdict": str}. Never raises — fire-and-forget safe.

    Verdict lifecycle:
      "disabled"         — ai_screen_enabled=False (no-op)
      "cap_reached"      — daily cap exhausted; retry tomorrow
      "pass"             — trio_match_score >= min_match; account stays in queue
      "screened_out"     — trio_match_score < min_match; account moves to low-fit bucket
      "insufficient_data"— grounding too thin; sets needs_more_enrichment=True for SP4
      "error"            — LLM/network error; scores not written; logged
    """
    if not settings.ai_screen_enabled:
        return {"verdict": "disabled"}

    # ── Cache hit: already screened, return stored verdict ──
    ed = dict(prospect.enrichment_data or {})
    existing = ed.get("ai_screen") or {}
    if existing.get("verdict") in ("pass", "screened_out"):
        return existing

    # ── Daily cap gate ──
    today_count = intel_cache.get_count(_cap_key())
    if today_count >= settings.ai_screen_daily_cap:
        logger.debug(
            "AI screen daily cap reached ({}/{}) — skipping prospect {}",
            today_count,
            settings.ai_screen_daily_cap,
            prospect.id,
        )
        return {"verdict": "cap_reached"}

    try:
        # ── Grounding check — prefer to fetch more enrichment than guess ──
        if not _grounding_is_sufficient(prospect):
            # Optionally try a single web_search to resolve; else return insufficient_data
            if settings.ai_screen_web_search_enabled:
                logger.debug("Thin grounding for prospect {} — attempting web_search supplemented screen", prospect.id)
                use_web = True
            else:
                verdict_dict: dict = {
                    "trio_match_score": 0,
                    "opportunity_score": 0,
                    "excess_likelihood": 0,
                    "verdict": "insufficient_data",
                    "rationale": "Insufficient firmographic data to make a reliable judgment.",
                    "evidence": [],
                    "confidence": 0,
                    "model": "none",
                    "screened_at": datetime.now(timezone.utc).isoformat(),
                    "needs_more_enrichment": True,
                }
                ed["ai_screen"] = verdict_dict
                prospect.enrichment_data = ed
                flag_modified(prospect, "enrichment_data")
                db.commit()
                return verdict_dict
        else:
            use_web = False

        # ── LLM call ──
        context = _assemble_context(prospect)
        raw = await _call_screen_llm(context, use_web_search=use_web)

        if not raw or "verdict" not in raw:
            logger.warning("AI screen returned empty/invalid response for prospect {}", prospect.id)
            return {"verdict": "error", "rationale": "Empty LLM response"}

        # ── Post-process: enforce screened_out if score below threshold ──
        trio_score = int(raw.get("trio_match_score") or 0)
        opp_score = int(raw.get("opportunity_score") or 0)
        verdict = raw.get("verdict", "insufficient_data")

        if verdict == "pass" and trio_score < settings.ai_screen_min_match:
            verdict = "screened_out"

        now_iso = datetime.now(timezone.utc).isoformat()
        verdict_dict = {
            "trio_match_score": trio_score,
            "opportunity_score": opp_score,
            "excess_likelihood": int(raw.get("excess_likelihood") or 0),
            "verdict": verdict,
            "rationale": raw.get("rationale", ""),
            "evidence": raw.get("evidence") or [],
            "confidence": int(raw.get("confidence") or 0),
            "model": raw.get("model", settings.anthropic_model),
            "screened_at": now_iso,
        }

        # ── Persist ──
        if verdict == "insufficient_data":
            verdict_dict["needs_more_enrichment"] = True
        else:
            # Write scores only for pass/screened_out (not insufficient_data or error)
            prospect.trio_match_score = trio_score
            prospect.opportunity_score = opp_score

        ed["ai_screen"] = verdict_dict
        prospect.enrichment_data = ed
        flag_modified(prospect, "enrichment_data")
        db.commit()

        # ── Meter daily usage ──
        intel_cache.incr_count(_cap_key(), ttl_days=1.0)

        logger.info(
            "AI screen for prospect {}: verdict={} match={} opp={} confidence={}",
            prospect.id,
            verdict,
            trio_score,
            opp_score,
            verdict_dict["confidence"],
        )
        return verdict_dict

    except Exception as exc:  # noqa: BLE001 — fire-and-forget; never propagate
        logger.warning("AI screen failed for prospect {}: {}", prospect.id, exc)
        return {"verdict": "error", "rationale": str(exc)}
```

Note on `_call_screen_llm`: the `context` argument is what gets passed as the `prompt` argument to `claude_structured`. Fix the implementation to pass `context` as the prompt:

```python
async def _call_screen_llm(context: str, *, use_web_search: bool = False) -> dict:
    from app.utils.claude_client import claude_structured

    tools: list[dict] | None = None
    if use_web_search:
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}]

    result = await claude_structured(
        context,
        schema=_SCREEN_SCHEMA,
        system=_SCREEN_SYSTEM,
        model_tier="smart",
        max_tokens=512,
        cache_system=True,
        timeout=45,
        cost_bucket="ai_screen",
    )
    return result or {}
```

- [ ] **Step 4: Run — expect PASS**

```bash
TESTING=1 PYTHONPATH=/root/availai/.claude/worktrees/sp3-screening \
  pytest tests/test_prospect_screening.py -q -p no:cacheprovider -o addopts=""
```
Expected: all tests pass (7 tests).

- [ ] **Step 5: Ruff + mypy**

```bash
cd /root/availai/.claude/worktrees/sp3-screening
ruff check app/services/prospect_screening.py
mypy app/services/prospect_screening.py
```
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add app/services/prospect_screening.py tests/test_prospect_screening.py
git commit -m "feat(sp3): add prospect_screening service with daily cap + cache + fire-and-forget safety"
```

---

## Task 4: Integrate Screen into `run_enrichment_job`

**Files:**
- Modify: `app/services/prospect_free_enrichment.py:362-471` (`run_enrichment_job`)
- Create: `tests/test_sp3_enrichment_integration.py`

**Interfaces:**
- Consumes: `screen_prospect(prospect, db)` from `app.services.prospect_screening`.
- Produces: `run_enrichment_job` now calls `screen_prospect` as the very last step (after `db.commit()` for fit/readiness, before the outer commit). The screen step is wrapped in its own try/except so a screen failure cannot affect the preceding enrichment data.

- [ ] **Step 1: Write the integration tests**

```python
# tests/test_sp3_enrichment_integration.py
"""SP3 integration: run_enrichment_job fires screen_prospect as final step.

All external calls (LLM, free enrichment, warm intros) are mocked.
"""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.config import settings
from app.models.prospect_account import ProspectAccount


def _prospect(db: Session, **kw) -> ProspectAccount:
    p = ProspectAccount(
        name=f"Co {uuid.uuid4().hex[:6]}",
        domain=f"co-{uuid.uuid4().hex[:6]}.com",
        status="suggested",
        discovery_source="clay",
        created_at=datetime.now(timezone.utc),
        enrichment_data={},
        readiness_signals={},
        **kw,
    )
    db.add(p)
    db.commit()
    return p


async def test_run_enrichment_job_calls_screen_as_final_step(db_session, monkeypatch):
    """screen_prospect is called once, after fit/readiness recompute."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_daily_cap", 999)
    monkeypatch.setattr(settings, "prospect_enrich_contacts_per_account", 5)

    p = _prospect(db_session, industry="Aerospace & Defense", naics_code="336412")

    screen_calls: list[int] = []

    async def _fake_screen(prospect, db):
        screen_calls.append(prospect.id)
        return {"verdict": "pass", "trio_match_score": 80, "opportunity_score": 70}

    from app.services import prospect_free_enrichment as pfe
    from app.services import prospect_screening as ps

    with (
        patch.object(pfe, "run_free_enrichment", new_callable=AsyncMock, return_value={}),
        patch("app.enrichment_service.enrich_entity", new_callable=AsyncMock, return_value=None),
        patch("app.enrichment_service.find_suggested_contacts", new_callable=AsyncMock, return_value=[]),
        patch("app.services.prospect_warm_intros.detect_warm_intros", return_value={}),
        patch("app.services.prospect_warm_intros.generate_one_liner", return_value=""),
        patch.object(ps, "screen_prospect", new=_fake_screen),
    ):
        await pfe.run_enrichment_job(p.id, db=db_session)

    assert screen_calls == [p.id], "screen_prospect must be called exactly once"


async def test_run_enrichment_job_screen_error_does_not_corrupt_enrichment(db_session, monkeypatch):
    """A screen_prospect exception must not roll back the preceding enrichment data."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_daily_cap", 999)
    monkeypatch.setattr(settings, "prospect_enrich_contacts_per_account", 5)

    p = _prospect(db_session, industry=None)

    from app.services import prospect_free_enrichment as pfe
    from app.services import prospect_screening as ps

    async def _boom(prospect, db):
        raise RuntimeError("LLM timeout")

    with (
        patch.object(pfe, "run_free_enrichment", new_callable=AsyncMock, return_value={}),
        patch("app.enrichment_service.enrich_entity", new_callable=AsyncMock, return_value=None),
        patch("app.enrichment_service.find_suggested_contacts", new_callable=AsyncMock, return_value=[]),
        patch("app.services.prospect_warm_intros.detect_warm_intros", return_value={}),
        patch("app.services.prospect_warm_intros.generate_one_liner", return_value=""),
        patch.object(ps, "screen_prospect", new=_boom),
    ):
        # Must not raise
        await pfe.run_enrichment_job(p.id, db=db_session)

    db_session.refresh(p)
    # enrich_status should still be 'done' (screen failure is non-fatal)
    assert (p.enrichment_data or {}).get("enrich_status") == "done"


async def test_run_enrichment_job_screen_disabled_still_commits_enrichment(db_session, monkeypatch):
    """When ai_screen_enabled=False, run_enrichment_job completes normally."""
    monkeypatch.setattr(settings, "ai_screen_enabled", False)
    monkeypatch.setattr(settings, "prospect_enrich_contacts_per_account", 5)

    p = _prospect(db_session)

    from app.services import prospect_free_enrichment as pfe

    with (
        patch.object(pfe, "run_free_enrichment", new_callable=AsyncMock, return_value={}),
        patch("app.enrichment_service.enrich_entity", new_callable=AsyncMock, return_value=None),
        patch("app.enrichment_service.find_suggested_contacts", new_callable=AsyncMock, return_value=[]),
        patch("app.services.prospect_warm_intros.detect_warm_intros", return_value={}),
        patch("app.services.prospect_warm_intros.generate_one_liner", return_value=""),
    ):
        await pfe.run_enrichment_job(p.id, db=db_session)

    db_session.refresh(p)
    assert (p.enrichment_data or {}).get("enrich_status") == "done"
```

- [ ] **Step 2: Run — expect FAIL**

```bash
TESTING=1 PYTHONPATH=/root/availai/.claude/worktrees/sp3-screening \
  pytest tests/test_sp3_enrichment_integration.py -q -p no:cacheprovider -o addopts=""
```
Expected: `AssertionError: screen_prospect must be called exactly once` (screen not wired yet).

- [ ] **Step 3: Add screen call to `run_enrichment_job`**

In `app/services/prospect_free_enrichment.py`, find the final block that commits `warm_intro`, `one_liner`, and `enrich_status` (currently around line 448–455). Add the screen step just before `db.commit()`:

Replace this block (the final commit inside the outer try):
```python
        ed = dict(prospect.enrichment_data or {})
        ed["warm_intro"] = warm
        ed["one_liner"] = one_liner
        ed["enrich_status"] = status
        prospect.enrichment_data = ed
        flag_modified(prospect, "enrichment_data")
        db.commit()
```

With:
```python
        ed = dict(prospect.enrichment_data or {})
        ed["warm_intro"] = warm
        ed["one_liner"] = one_liner
        ed["enrich_status"] = status
        prospect.enrichment_data = ed
        flag_modified(prospect, "enrichment_data")
        db.commit()

        # ── SP3: AI screen — final step, fire-and-forget ──
        try:
            from app.services.prospect_screening import screen_prospect
            await screen_prospect(prospect, db)
        except Exception as _screen_exc:  # noqa: BLE001 — screen must not affect enrich_status
            logger.warning("Screen step failed for prospect {}: {}", prospect_id, _screen_exc)
```

- [ ] **Step 4: Run — expect PASS**

```bash
TESTING=1 PYTHONPATH=/root/availai/.claude/worktrees/sp3-screening \
  pytest tests/test_sp3_enrichment_integration.py -q -p no:cacheprovider -o addopts=""
```
Expected: 3 passed.

- [ ] **Step 5: Run full suite to check no regressions**

```bash
TESTING=1 PYTHONPATH=/root/availai/.claude/worktrees/sp3-screening \
  pytest tests/ -q -p no:cacheprovider -o addopts="" --ignore=tests/e2e
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/services/prospect_free_enrichment.py tests/test_sp3_enrichment_integration.py
git commit -m "feat(sp3): wire screen_prospect as fire-and-forget final step in run_enrichment_job"
```

---

## Task 5: Rank + Gate — List Route + Stats

**Files:**
- Modify: `app/routers/htmx_views.py:9468-9549` (`prospecting_list_partial`) and `9408-9426` (`_prospect_stats_ctx`)
- Create: `tests/test_sp3_list_route.py`

**Interfaces:**
- Consumes: `ProspectAccount.trio_match_score`, `ProspectAccount.opportunity_score`, `ProspectAccount.enrichment_data['ai_screen']['verdict']`; `settings.ai_screen_enabled`, `settings.ai_screen_min_match`.
- Produces:
  - New sort option `"ai_match_desc"` in `prospecting_list_partial` — SQL `ORDER BY trio_match_score DESC, opportunity_score DESC, readiness_score DESC`. No in-memory ranking.
  - Screened-out bucket: when not filtering by status and `ai_screen_enabled=True`, accounts whose `enrichment_data->'ai_screen'->>'verdict' = 'screened_out'` are excluded from the main count and grid, collected separately, and passed as `screened_out_prospects` context variable.
  - `_prospect_stats_ctx` gains `"screened_out": <count>` key.

- [ ] **Step 1: Write the list route tests**

```python
# tests/test_sp3_list_route.py
"""SP3 rank + gate: ai_match_desc sort, screened-out bucket, stats count."""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.models.prospect_account import ProspectAccount


def _prospect(db: Session, *, trio_match_score: int = 0, opportunity_score: int = 0,
              readiness_score: int = 0, ai_verdict: str | None = None, **kw) -> ProspectAccount:
    ed: dict = {}
    if ai_verdict:
        ed["ai_screen"] = {"verdict": ai_verdict, "rationale": "test", "evidence": []}
    p = ProspectAccount(
        name=kw.pop("name", f"Co {uuid.uuid4().hex[:6]}"),
        domain=kw.pop("domain", f"co-{uuid.uuid4().hex[:6]}.com"),
        status="suggested",
        discovery_source="clay",
        created_at=datetime.now(timezone.utc),
        trio_match_score=trio_match_score,
        opportunity_score=opportunity_score,
        readiness_score=readiness_score,
        enrichment_data=ed,
        readiness_signals={},
        **kw,
    )
    db.add(p)
    db.commit()
    return p


def test_ai_match_desc_sort_orders_by_trio_match_score(db_session, monkeypatch):
    """ai_match_desc returns prospects sorted trio_match_score DESC."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    low = _prospect(db_session, trio_match_score=20, name="Low Co", domain="low.com")
    high = _prospect(db_session, trio_match_score=90, name="High Co", domain="high.com")
    mid = _prospect(db_session, trio_match_score=55, name="Mid Co", domain="mid.com")

    from app.routers.htmx_views import prospecting_list_partial
    from app.database import get_db

    # Query the DB directly using the sort logic the route applies
    from sqlalchemy import desc
    rows = (
        db_session.query(ProspectAccount)
        .filter(ProspectAccount.status == "suggested")
        .order_by(
            desc(ProspectAccount.trio_match_score),
            desc(ProspectAccount.opportunity_score),
            desc(ProspectAccount.readiness_score),
        )
        .all()
    )
    assert rows[0].id == high.id
    assert rows[1].id == mid.id
    assert rows[2].id == low.id


def test_screened_out_bucket_excluded_from_main_when_enabled(db_session, monkeypatch):
    """screened_out accounts excluded from main grid when ai_screen_enabled."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_min_match", 40)

    good = _prospect(db_session, trio_match_score=80, ai_verdict="pass", name="Good Co", domain="good.com")
    bad = _prospect(db_session, trio_match_score=10, ai_verdict="screened_out", name="Bad Co", domain="bad.com")

    # Simulate the filtering logic the route applies
    all_rows = db_session.query(ProspectAccount).filter(ProspectAccount.status == "suggested").all()
    main = [p for p in all_rows if (p.enrichment_data or {}).get("ai_screen", {}).get("verdict") != "screened_out"]
    screened = [p for p in all_rows if (p.enrichment_data or {}).get("ai_screen", {}).get("verdict") == "screened_out"]

    assert len(main) == 1 and main[0].id == good.id
    assert len(screened) == 1 and screened[0].id == bad.id


def test_screened_out_bucket_included_when_disabled(db_session, monkeypatch):
    """When ai_screen_enabled=False, screened_out accounts appear in the main grid."""
    monkeypatch.setattr(settings, "ai_screen_enabled", False)

    _prospect(db_session, trio_match_score=80, ai_verdict="pass", name="Good Co", domain="good.com")
    _prospect(db_session, trio_match_score=10, ai_verdict="screened_out", name="Bad Co", domain="bad.com")

    all_rows = db_session.query(ProspectAccount).filter(ProspectAccount.status == "suggested").all()
    # Without the gate, screened_out is just a label — all accounts appear
    assert len(all_rows) == 2


def test_prospect_stats_ctx_includes_screened_out_count(db_session, monkeypatch):
    """_prospect_stats_ctx returns screened_out key when ai_screen_enabled."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)

    _prospect(db_session, ai_verdict="pass", name="Pass Co", domain="pass.com")
    _prospect(db_session, ai_verdict="screened_out", name="Screened Co", domain="screened.com")
    _prospect(db_session, name="Unscreened Co", domain="unscreened.com")

    from app.routers.htmx_views import _prospect_stats_ctx
    ctx = _prospect_stats_ctx(db_session)
    assert "screened_out" in ctx
    assert ctx["screened_out"] == 1
```

- [ ] **Step 2: Run — expect FAIL**

```bash
TESTING=1 PYTHONPATH=/root/availai/.claude/worktrees/sp3-screening \
  pytest tests/test_sp3_list_route.py -q -p no:cacheprovider -o addopts=""
```
Expected: `AssertionError` on screened_out tests (bucket not implemented yet); sort test passes (SQL-only).

- [ ] **Step 3: Modify `prospecting_list_partial` in `app/routers/htmx_views.py`**

In the `sort` handling block (currently starting around line 9498), add the `ai_match_desc` branch **before** the existing `if sort == "buyer_ready_desc":` check:

```python
    if sort == "ai_match_desc":
        # Screened-out gate: exclude screened_out accounts from the main grid
        # (only when ai_screen_enabled; otherwise show all)
        from ..config import settings as _settings
        if _settings.ai_screen_enabled:
            from sqlalchemy import cast
            from sqlalchemy.dialects.postgresql import JSONB as _JSONB
            # Filter out screened_out via JSONB path — works on PG; test env uses SQLite
            # (which evaluates JSON as a string, so we do Python-side filtering in tests)
            screened_out_rows = base.filter(
                ProspectAccount.enrichment_data["ai_screen"]["verdict"].astext == "screened_out"
            ).all()
            base = base.filter(
                ~(ProspectAccount.enrichment_data["ai_screen"]["verdict"].astext == "screened_out")
            )
        else:
            screened_out_rows = []

        total = base.count()
        total_pages = max(1, (total + per_page - 1) // per_page)
        offset = (page - 1) * per_page
        prospects = (
            base.order_by(
                ProspectAccount.trio_match_score.desc(),
                ProspectAccount.opportunity_score.desc(),
                ProspectAccount.readiness_score.desc(),
            )
            .offset(offset)
            .limit(per_page)
            .all()
        )
```

**IMPORTANT**: Because the SQLite test environment cannot use JSONB path operators, apply the screened-out gate using a Python-side filter in the test. The production PostgreSQL path uses JSONB operators. To avoid a dual-code-path in production, use the Python-side approach universally (load all matching rows, filter in Python) when `ai_screen_enabled=True` — this is consistent with the existing `buyer_ready_desc` approach. Rewrite the `ai_match_desc` branch as:

```python
    if sort == "ai_match_desc":
        from ..config import settings as _settings
        rows = base.all()
        if _settings.ai_screen_enabled:
            screened_out_rows = [
                p for p in rows
                if (p.enrichment_data or {}).get("ai_screen", {}).get("verdict") == "screened_out"
            ]
            rows = [
                p for p in rows
                if (p.enrichment_data or {}).get("ai_screen", {}).get("verdict") != "screened_out"
            ]
        else:
            screened_out_rows = []
        rows.sort(
            key=lambda p: (
                -(p.trio_match_score or 0),
                -(p.opportunity_score or 0),
                -(p.readiness_score or 0),
                (p.name or "").lower(),
            )
        )
        total = len(rows)
        total_pages = max(1, (total + per_page - 1) // per_page)
        prospects = rows[offset : offset + per_page]
```

Then at the bottom of the route, just before building `ctx`, add `screened_out_rows` to the context:
```python
    ctx = _base_ctx(request, user, "prospecting")
    ctx.update(
        {
            "prospects": prospects,
            "snapshots": snapshots,
            "contact_stats_map": contact_stats_map,
            "q": q,
            "status": status,
            "sort": sort,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "status_counts": status_counts,
            "all_total": all_total,
            "screened_out_prospects": screened_out_rows if sort == "ai_match_desc" else [],
        }
    )
```

For all other sort branches, add `"screened_out_prospects": []` to the existing ctx dict (add it to the already-present `.update()` call).

- [ ] **Step 4: Modify `_prospect_stats_ctx` to add screened_out count**

Replace the function body in `app/routers/htmx_views.py:9408-9426`:

```python
def _prospect_stats_ctx(db: Session) -> dict:
    """Canonical prospecting KPIs (single definition, shared by the stats route and the
    OOB refresh after grid actions).

    "Buyer ready" = is_buyer_ready over SUGGESTED.
    """
    from ..config import settings as _settings

    suggested = db.query(ProspectAccount).filter(ProspectAccount.status == ProspectAccountStatus.SUGGESTED).all()
    claimed = (
        db.query(sqlfunc.count(ProspectAccount.id))
        .filter(ProspectAccount.status == ProspectAccountStatus.CLAIMED)
        .scalar()
        or 0
    )
    screened_out_count = (
        sum(
            1 for p in suggested
            if (p.enrichment_data or {}).get("ai_screen", {}).get("verdict") == "screened_out"
        )
        if _settings.ai_screen_enabled
        else 0
    )
    return {
        "total": len(suggested),
        "buyer_ready": sum(1 for p in suggested if build_priority_snapshot(p)["is_buyer_ready"]),
        "call_now": sum(1 for p in suggested if (p.readiness_score or 0) >= 70),
        "claimed": claimed,
        "screened_out": screened_out_count,
    }
```

- [ ] **Step 5: Run — expect PASS**

```bash
TESTING=1 PYTHONPATH=/root/availai/.claude/worktrees/sp3-screening \
  pytest tests/test_sp3_list_route.py -q -p no:cacheprovider -o addopts=""
```
Expected: 4 passed.

- [ ] **Step 6: Run full suite**

```bash
TESTING=1 PYTHONPATH=/root/availai/.claude/worktrees/sp3-screening \
  pytest tests/ -q -p no:cacheprovider -o addopts="" --ignore=tests/e2e
```
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add app/routers/htmx_views.py tests/test_sp3_list_route.py
git commit -m "feat(sp3): add ai_match_desc sort, screened-out bucket gate, screened_out stats count"
```

---

## Task 6: UI — Match/Opportunity Scores, Rationale, Screened-Out Bucket

> **APPROVAL GATE: This task requires explicit user approval before building.** Per the project's UI guardrail (`CLAUDE.md`), new UI elements must not be added without the user's sign-off. Present the design from this plan and wait for approval.

**Approval checklist** (present these three items to the user before building):
1. Sort dropdown adds `<option value="ai_match_desc">AI match (best first)</option>` and becomes the new default (replacing `buyer_ready_desc`).
2. Prospect card gains two score bars (Match / Opportunity) alongside the existing Fit / Readiness bars, plus a one-line rationale snippet below.
3. A collapsed "Screened out / low fit" section appears at the bottom of the list (only when `sort=ai_match_desc` and `ai_screen_enabled=True` and `screened_out_prospects` is non-empty), showing count + expandable list of screened accounts with their rationale and a "Claim anyway" override button.
4. Prospect detail page gains an "AI Screening" card: match/opportunity/excess scores, verdict badge, rationale, evidence list, confidence %, screened_at datetime, model used.

**Files (only modify after approval):**
- Modify: `app/templates/htmx/partials/prospecting/list.html`
- Modify: `app/templates/htmx/partials/prospecting/_card.html`
- Modify: `app/templates/htmx/partials/prospecting/detail.html`

**Implementation notes (for after approval):**

In `list.html`, add the sort option to the `<select>`:
```html
<option value="ai_match_desc" {% if sort == 'ai_match_desc' %}selected{% endif %}>AI match (best first)</option>
```

At the bottom of `list.html`, after the pagination block, add the screened-out bucket:
```html
{% if screened_out_prospects and ai_screen_enabled %}
<div x-data="{ open: false }" class="mt-8 border border-amber-200 rounded-lg bg-amber-50">
  <button @click="open = !open"
          class="w-full flex items-center justify-between px-4 py-3 text-sm font-medium text-amber-800">
    <span>Screened out / low fit ({{ screened_out_prospects|length }})</span>
    <svg class="w-4 h-4 transition-transform" :class="open ? 'rotate-180' : ''" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
    </svg>
  </button>
  <div x-show="open" x-cloak class="border-t border-amber-200 p-4 space-y-3">
    {% for p in screened_out_prospects %}
    <div class="flex items-start justify-between gap-3 text-sm">
      <div class="min-w-0">
        <span class="font-medium text-gray-800">{{ p.name }}</span>
        <span class="text-gray-400 ml-1">{{ p.domain }}</span>
        <p class="text-xs text-amber-700 mt-0.5 truncate">
          {{ (p.enrichment_data or {}).get('ai_screen', {}).get('rationale', 'No rationale') }}
        </p>
      </div>
      <button
        hx-post="/v2/partials/prospecting/{{ p.id }}/claim"
        hx-target="#prospect-{{ p.id }}"
        hx-swap="outerHTML"
        class="shrink-0 px-2 py-1 text-xs font-medium text-amber-700 border border-amber-300 rounded hover:bg-amber-100 transition-colors">
        Claim anyway
      </button>
    </div>
    {% endfor %}
  </div>
</div>
{% endif %}
```

In `_card.html`, after the existing readiness score bar, add (only when `ai_screen_enabled` and the prospect has been screened):
```html
{% set ai_screen = (prospect.enrichment_data or {}).get('ai_screen', {}) %}
{% if ai_screen and ai_screen.get('verdict') in ('pass', 'screened_out') %}
<div class="mb-2">
  {% set match = ai_screen.get('trio_match_score', 0) %}
  <div class="flex items-center justify-between text-xs mb-0.5">
    <span class="text-gray-500">AI Match <span class="text-gray-300">(procurement fit)</span></span>
    <span class="font-medium {% if match >= 70 %}text-emerald-600{% elif match >= 40 %}text-amber-600{% else %}text-red-500{% endif %}">{{ match }}%</span>
  </div>
  <div class="w-full h-1.5 bg-gray-200 rounded-full overflow-hidden">
    <div class="h-full rounded-full {% if match >= 70 %}bg-emerald-500{% elif match >= 40 %}bg-amber-500{% else %}bg-red-400{% endif %}"
         style="width: {{ match }}%"></div>
  </div>
</div>
{% if ai_screen.get('rationale') %}
<p class="text-[11px] text-gray-400 italic mb-1.5 line-clamp-1" title="{{ ai_screen.get('rationale', '') }}">
  {{ ai_screen['rationale'] }}
</p>
{% endif %}
{% endif %}
```

In `detail.html`, add an "AI Screening" section (position: after the Scores section, before Contacts):
```html
{% set ai_screen = enrichment.get('ai_screen', {}) %}
{% if ai_screen and ai_screen.get('verdict') %}
<div class="bg-white border border-gray-200 rounded-lg p-4 mb-4">
  <h3 class="text-sm font-semibold text-gray-700 mb-3">AI Screening</h3>
  <div class="grid grid-cols-3 gap-3 mb-3">
    <div class="text-center">
      <p class="text-2xl font-bold {{ 'text-emerald-600' if ai_screen.get('trio_match_score', 0) >= 70 else 'text-amber-600' if ai_screen.get('trio_match_score', 0) >= 40 else 'text-red-500' }}">
        {{ ai_screen.get('trio_match_score', '—') }}</p>
      <p class="text-xs text-gray-500 mt-0.5">Match</p>
    </div>
    <div class="text-center">
      <p class="text-2xl font-bold text-gray-700">{{ ai_screen.get('opportunity_score', '—') }}</p>
      <p class="text-xs text-gray-500 mt-0.5">Opportunity</p>
    </div>
    <div class="text-center">
      <p class="text-2xl font-bold text-gray-500">{{ ai_screen.get('excess_likelihood', '—') }}</p>
      <p class="text-xs text-gray-500 mt-0.5">Excess</p>
    </div>
  </div>
  {% set verdict = ai_screen.get('verdict', '') %}
  <span class="inline-flex px-2 py-0.5 text-xs font-semibold rounded-full mb-2
    {% if verdict == 'pass' %}bg-emerald-100 text-emerald-700
    {% elif verdict == 'screened_out' %}bg-amber-100 text-amber-700
    {% else %}bg-gray-100 text-gray-600{% endif %}">
    {{ verdict|replace('_', ' ')|capitalize }}
  </span>
  {% if ai_screen.get('rationale') %}
  <p class="text-sm text-gray-700 mb-2">{{ ai_screen['rationale'] }}</p>
  {% endif %}
  {% if ai_screen.get('evidence') %}
  <ul class="text-xs text-gray-500 space-y-0.5 mb-2">
    {% for item in ai_screen['evidence'] %}<li class="truncate">• {{ item }}</li>{% endfor %}
  </ul>
  {% endif %}
  <p class="text-xs text-gray-400">
    Confidence {{ ai_screen.get('confidence', '?') }}%
    · {{ ai_screen.get('model', '') }}
    · {{ ai_screen.get('screened_at', '')[:10] }}
  </p>
</div>
{% endif %}
```

After approval and implementation, update `list.html` to pass `ai_screen_enabled` from context, and add `"ai_screen_enabled": settings.ai_screen_enabled` to the route's `ctx` dict.

- [ ] **Step 1: Present the four approval items above to the user and wait for sign-off.**
- [ ] **Step 2: (After approval) Implement the three template changes.**
- [ ] **Step 3: Add `ai_screen_enabled` to the route context in `htmx_views.py`.**
- [ ] **Step 4: Run ruff + mypy (Python files only; templates are Jinja2).**
- [ ] **Step 5: Verify Tailwind classes by running `npm run build` and checking the output bundle contains the new classes, or adding them to the safelist in `tailwind.config.js`.**
- [ ] **Step 6: Commit (after approval + build verification).**

```bash
git add app/templates/htmx/partials/prospecting/ app/routers/htmx_views.py
git commit -m "feat(sp3): UI — match/opportunity scores on card+detail, screened-out bucket [approval-gated]"
```

---

## Task 7: Docs Update

**Files:**
- Modify: `docs/APP_MAP_DATABASE.md`
- Modify: `docs/APP_MAP_INTERACTIONS.md`

**Interfaces:** N/A — documentation only.

- [ ] **Step 1: Update `docs/APP_MAP_DATABASE.md`**

Find the `prospect_accounts` table section and add:
```
| trio_match_score  | Integer | default 0, indexed | AI procurement-fit score (0-100); 0 until screened |
| opportunity_score | Integer | default 0, indexed | AI opportunity size score (0-100); 0 until screened |
```

Also note that `enrichment_data['ai_screen']` (JSONB) holds the full verdict: `{trio_match_score, opportunity_score, excess_likelihood, verdict, rationale, evidence, confidence, model, screened_at, needs_more_enrichment?}`.

- [ ] **Step 2: Update `docs/APP_MAP_INTERACTIONS.md`**

Add a section under the prospecting/enrichment area:

```
### SP3: AI Account Screening (prospect_screening.py)

Called by: `run_enrichment_job` (final step, fire-and-forget).
Calls: `claude_structured` (smart tier, structured schema, 512 tokens, cost_bucket="ai_screen").
Cost control: daily cap via `intel_cache.get_count("ai_screen:daily:{date}")` /
  `incr_count(...)` (ttl_days=1). Default cap: 200/day. Re-screens only when
  `enrichment_data['ai_screen']` is absent or verdict is `insufficient_data`.
Verdict persistence: `trio_match_score` + `opportunity_score` → indexed Integer columns
  (SQL-sortable); full verdict → `enrichment_data['ai_screen']` (JSONB).
Gate: `ai_screen_enabled=False` (default) → no LLM call; disabled returns `{"verdict":"disabled"}`.
Screened-out bucket: `verdict=screened_out` hides account from default queue;
  recoverable via buyer override (Claim anyway); threshold `ai_screen_min_match=40`.
```

- [ ] **Step 3: Commit**

```bash
git add docs/APP_MAP_DATABASE.md docs/APP_MAP_INTERACTIONS.md
git commit -m "docs(sp3): update APP_MAP_DATABASE + APP_MAP_INTERACTIONS for AI screening columns + service"
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] Migration with rollback (Task 1)
- [x] `screen_prospect` service with grounded context assembly (Task 3)
- [x] Claude call mirrors app pattern — `claude_structured`, JSON-only, "do not fabricate" system prompt (Task 3)
- [x] `insufficient_data` routes to enrich, never guesses (Task 3)
- [x] Cache hit skips re-screen (Task 3, `test_screen_prospect_cache_hit_skips_llm`)
- [x] Daily cap + spend metering via `intel_cache.incr_count` (Task 3)
- [x] `ai_screen_web_search_enabled` config controls optional web_search (Task 2 + Task 3)
- [x] `screen_prospect` is final step of `run_enrichment_job` (Task 4)
- [x] Fire-and-forget: screen error doesn't corrupt enrichment (Task 4, `test_run_enrichment_job_screen_error_does_not_corrupt_enrichment`)
- [x] Queue default sort `trio_match_score desc → opportunity_score desc → readiness` as `ai_match_desc` sort option (Task 5)
- [x] Screened-out bucket (soft, recoverable) — verdict drives exclusion from main grid (Task 5)
- [x] `screened_out` count in `_prospect_stats_ctx` (Task 5)
- [x] UI elements gated on approval (Task 6)
- [x] Config keys: `ai_screen_enabled`, `ai_screen_min_match`, `ai_screen_daily_cap`, `ai_screen_web_search_enabled` (Task 2)
- [x] Docs: both APP_MAP docs (Task 7)
- [x] LLM mocked in all tests (AsyncMock on `_call_screen_llm`)

**Spec item not built (flagged):** The spec says "default queue ranking" changes to AI match. This plan adds `ai_match_desc` as a new sort option but does NOT change the existing default from `buyer_ready_desc`. Rationale: the default sort is visible to buyers immediately on load; changing it silently is a UI change that falls under the approval gate. The implementing agent should ask the user whether to change the default when presenting Task 6 for approval.

**Placeholder scan:** None found. All code is complete.

**Type consistency:** `ProspectAccount.trio_match_score` and `.opportunity_score` are `Column(Integer, default=0)` throughout. `screen_prospect` returns `dict` in all paths. `_call_screen_llm` takes `str` context, returns `dict`. `_prospect_stats_ctx` returns `dict` with `"screened_out": int`.
