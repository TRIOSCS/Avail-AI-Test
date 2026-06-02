# IBM Spec Code Resolver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Per `CLAUDE.md`: subagent-driven execution is mandatory — never inline, never ask.

**Goal:** Translate OEM spec codes (e.g. IBM `SPREJ`) to underlying approved MPNs on universal-zero connector results, fan out sourcing against the resolved MPNs, and queue LLM-discovered mappings for human approval before caching.

**Architecture:** New `SpecCodeResolver` service called from `app/search_service.py:search_requirement()` after the synchronous fanout returns zero sightings. Three new tables (`oem_spec_codes`, `oem_spec_codes_pending`, `oem_spec_codes_blacklist`). Lineage columns on `Sighting`/`Offer`; `Requirement.oem_hint` added. ICS/NC queue manager extended with `override_mpn` to enqueue resolved AVL MPNs. Admin HTMX page for the pending-approval queue. Feature-flagged off by default; flipped on in the final PR.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Alembic, PostgreSQL, Pydantic v2, Anthropic Claude (via `app/utils/claude_client.claude_json` with the `web_search_20250305` tool), Jinja2 + HTMX 2.x for the admin page, pytest with in-memory SQLite (`TESTING=1 PYTHONPATH=/root/availai`).

**Source spec:** `docs/superpowers/specs/2026-05-27-ibm-spec-code-resolver-design.md` (PR #167). All references to "the spec" below mean this file.

---

## File structure

### Created

| Path | Purpose |
|---|---|
| `alembic/versions/<rev>_add_oem_spec_code_resolver.py` | Migration: 3 new tables + `Requirement.oem_hint` + `Sighting`/`Offer` lineage columns |
| `app/services/spec_code_resolver.py` | `SpecCodeResolver` service class, `ResolverResult` dataclass, LLM prompt builder |
| `app/routers/admin/spec_codes.py` | 5 endpoints for the pending-approval queue |
| `app/templates/htmx/admin/spec_codes_pending.html` | Server-rendered table + HTMX row actions |
| `app/schemas/spec_codes.py` | Pydantic v2 schemas for AVL entries and admin actions |
| `tests/test_oem_spec_code_models.py` | Model invariants |
| `tests/test_spec_code_resolver.py` | Resolver unit tests |
| `tests/test_search_service_with_spec_resolver.py` | Integration tests for the zero-hit fallback |
| `tests/e2e/test_spec_code_resolver_e2e.py` | One end-to-end happy path |
| `tests/routers/admin/test_spec_codes_pending.py` | Admin UI smoke tests |

### Modified

| Path | Change |
|---|---|
| `app/models/sourcing.py` | Add `OemSpecCode`, `OemSpecCodePending`, `OemSpecCodeBlacklist`, `Requirement.oem_hint` |
| `app/models/intelligence.py` | Add `Sighting.resolved_via_spec_code`, `Sighting.source_mpn`, same on `Offer` |
| `app/search_service.py` | Inside `search_requirement()`: after `_save_sightings`, if zero sightings and flag on, call resolver, re-fanout, enqueue AVL MPNs |
| `app/services/search_worker_base/queue_manager.py` | `enqueue_search()` gains `override_mpn` + `resolved_via_spec_code` kwargs |
| `app/services/ics_worker/queue_manager.py` | Pass-through kwargs to base `enqueue_search` |
| `app/services/nc_worker/queue_manager.py` | Pass-through kwargs to base `enqueue_search` |
| `app/config.py` | New settings: `SPEC_RESOLVER_ENABLED`, `SPEC_RESOLVER_MIN_CONFIDENCE`, `SPEC_RESOLVER_MODEL` |
| `app/main.py` | Mount the new admin spec-codes router |
| `app/templates/htmx/admin/index.html` (or admin nav include) | Link to `/admin/spec-codes/pending` |
| `docs/APP_MAP_ARCHITECTURE.md` | Document new service + integration site |
| `docs/APP_MAP_DATABASE.md` | Document new tables + columns |
| `docs/APP_MAP_INTERACTIONS.md` | Document the zero-hit fallback flow |

---

## Phase 0 — Setup (one-time)

- [ ] **Step 0.1:** Confirm worktree exists at `/root/availai-worktrees/spec-ibm-resolver` on branch `docs/ibm-spec-code-resolver-design` (PR #167).
- [ ] **Step 0.2:** Create six stacked branches off the spec branch, one per PR. Branches base on the parent PR's branch (so each can be opened bottom-up as the previous merges):

```bash
cd /root/availai-worktrees/spec-ibm-resolver
git checkout docs/ibm-spec-code-resolver-design
git checkout -b feat/spec-resolver-1-migration
# Subsequent PRs branch off their predecessor at task-start time.
```

- [ ] **Step 0.3:** Confirm clean baseline by running the modules-only test slice that PR 1 will touch:

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -k "alembic or migration" -v --override-ini="addopts="
```

Expected: PASS (no migration tests fail on baseline).

---

## PR 1 — Migration (no models, no code)

**Branch:** `feat/spec-resolver-1-migration` off `docs/ibm-spec-code-resolver-design`.

**Files:**
- Create: `alembic/versions/<rev>_add_oem_spec_code_resolver.py`

The migration adds three new tables and columns on `requirements`, `sightings`, `offers`. No models yet — autogenerate is therefore unavailable; we author the migration by hand.

### Task 1.1 — Author migration upgrade

- [ ] **Step 1.1.1:** Determine the current alembic head:

```bash
cd /root/availai-worktrees/spec-ibm-resolver
alembic current 2>/dev/null || alembic heads
```

Expected: a single revision id, e.g. `f3fbddb04947`. Record it as `down_revision`.

- [ ] **Step 1.1.2:** Generate an empty migration:

```bash
alembic revision -m "add_oem_spec_code_resolver"
```

Expected: a new file under `alembic/versions/` is printed.

- [ ] **Step 1.1.3:** Write the full migration. Path: `alembic/versions/<new-rev>_add_oem_spec_code_resolver.py`. Content:

```python
"""add_oem_spec_code_resolver

Adds tables for the IBM spec code resolver (approved, pending,
blacklist) and lineage columns on requirements/sightings/offers.

Revision ID: <new-rev>
Revises: <down-rev>
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "<new-rev>"
down_revision = "<down-rev>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oem_spec_codes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("oem", sa.String(64), nullable=False),
        sa.Column("spec_code", sa.String(64), nullable=False),
        sa.Column("avl", postgresql.JSONB, nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column(
            "approved_by_user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("oem", "spec_code", name="uq_oem_spec_code"),
    )
    op.create_index("ix_oem_spec_codes_oem", "oem_spec_codes", ["oem"])
    op.create_index("ix_oem_spec_codes_spec_code", "oem_spec_codes", ["spec_code"])

    op.create_table(
        "oem_spec_codes_pending",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("oem", sa.String(64), nullable=False),
        sa.Column("spec_code", sa.String(64), nullable=False),
        sa.Column("proposed_avl", postgresql.JSONB, nullable=False),
        sa.Column("llm_confidence", sa.Float, nullable=False),
        sa.Column("citations", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "first_requirement_id",
            sa.Integer,
            sa.ForeignKey("requirements.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "used_in_requirement_ids",
            postgresql.JSONB,
            nullable=False,
            server_default="[]",
        ),
        sa.UniqueConstraint("oem", "spec_code", name="uq_pending_oem_spec_code"),
    )
    op.create_index(
        "ix_oem_spec_codes_pending_oem", "oem_spec_codes_pending", ["oem"]
    )
    op.create_index(
        "ix_oem_spec_codes_pending_spec_code",
        "oem_spec_codes_pending",
        ["spec_code"],
    )

    op.create_table(
        "oem_spec_codes_blacklist",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("oem", sa.String(64), nullable=False),
        sa.Column("spec_code", sa.String(64), nullable=False),
        sa.Column("rejected_mpns", postgresql.JSONB, nullable=False),
        sa.Column(
            "rejected_by_user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "rejected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("reason", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_oem_spec_codes_blacklist_oem", "oem_spec_codes_blacklist", ["oem"]
    )

    # Lineage columns — all nullable, no schema break for existing rows
    op.add_column(
        "requirements",
        sa.Column("oem_hint", sa.String(64), nullable=True),
    )
    op.add_column(
        "sightings",
        sa.Column("resolved_via_spec_code", sa.String(64), nullable=True),
    )
    op.add_column(
        "sightings",
        sa.Column("source_mpn", sa.String(255), nullable=True),
    )
    op.add_column(
        "offers",
        sa.Column("resolved_via_spec_code", sa.String(64), nullable=True),
    )
    op.add_column(
        "offers",
        sa.Column("source_mpn", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("offers", "source_mpn")
    op.drop_column("offers", "resolved_via_spec_code")
    op.drop_column("sightings", "source_mpn")
    op.drop_column("sightings", "resolved_via_spec_code")
    op.drop_column("requirements", "oem_hint")

    op.drop_index("ix_oem_spec_codes_blacklist_oem", table_name="oem_spec_codes_blacklist")
    op.drop_table("oem_spec_codes_blacklist")

    op.drop_index("ix_oem_spec_codes_pending_spec_code", table_name="oem_spec_codes_pending")
    op.drop_index("ix_oem_spec_codes_pending_oem", table_name="oem_spec_codes_pending")
    op.drop_table("oem_spec_codes_pending")

    op.drop_index("ix_oem_spec_codes_spec_code", table_name="oem_spec_codes")
    op.drop_index("ix_oem_spec_codes_oem", table_name="oem_spec_codes")
    op.drop_table("oem_spec_codes")
```

Replace `<new-rev>` and `<down-rev>` with the values from steps 1.1.1 and 1.1.2.

### Task 1.2 — Verify upgrade / downgrade cycle

- [ ] **Step 1.2.1:** Run upgrade against a clean test DB:

```bash
TESTING=1 PYTHONPATH=/root/availai alembic upgrade head
```

Expected: no errors; last line shows the new revision.

- [ ] **Step 1.2.2:** Run downgrade to the previous revision:

```bash
TESTING=1 PYTHONPATH=/root/availai alembic downgrade -1
```

Expected: no errors; all five lineage columns and three tables are dropped.

- [ ] **Step 1.2.3:** Run upgrade again:

```bash
TESTING=1 PYTHONPATH=/root/availai alembic upgrade head
```

Expected: PASS — confirms the cycle is idempotent.

- [ ] **Step 1.2.4:** Run `alembic heads`:

```bash
TESTING=1 PYTHONPATH=/root/availai alembic heads
```

Expected: a single head — the new revision.

### Task 1.3 — Commit and PR

- [ ] **Step 1.3.1:**

```bash
git add alembic/versions/
git -c commit.gpgsign=false commit -m "feat(db): migration for oem spec code resolver tables and lineage columns"
git push -u origin feat/spec-resolver-1-migration
gh pr create --base docs/ibm-spec-code-resolver-design \
  --title "feat(db): migration for OEM spec code resolver" \
  --body "Adds oem_spec_codes, oem_spec_codes_pending, oem_spec_codes_blacklist; adds Requirement.oem_hint, Sighting/Offer lineage columns. Migration only — no model wiring yet. Per spec §4."
```

- [ ] **Step 1.3.2:** Run PR-review pipeline per `CLAUDE.md` ("Run ALL pr-review-toolkit agents on every PR"). Subagent-dispatched: comment-analyzer, pr-test-analyzer, type-design-analyzer, silent-failure-hunter, code-simplifier, code-reviewer, feature-dev:code-reviewer.

---

## PR 2 — Models and schemas

**Branch:** `feat/spec-resolver-2-models` off `feat/spec-resolver-1-migration`.

**Files:**
- Modify: `app/models/sourcing.py`
- Modify: `app/models/intelligence.py`
- Create: `app/schemas/spec_codes.py`
- Create: `tests/test_oem_spec_code_models.py`

### Task 2.1 — Test scaffolding first (TDD)

- [ ] **Step 2.1.1:** Create `tests/test_oem_spec_code_models.py` with the test that the three models import and instantiate. This is the failing test:

```python
"""Model invariants for the IBM spec code resolver tables."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.models.sourcing import (
    OemSpecCode,
    OemSpecCodeBlacklist,
    OemSpecCodePending,
    Requirement,
    Requisition,
)


def _new_requisition(db) -> Requisition:
    req_set = Requisition(name="test")
    db.add(req_set)
    db.commit()
    db.refresh(req_set)
    return req_set


def test_oem_spec_code_unique_constraint(db_session):
    db_session.add(
        OemSpecCode(
            oem="IBM",
            spec_code="SPREJ",
            avl=[{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}],
            source="manual",
            approved_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    db_session.add(
        OemSpecCode(
            oem="IBM",
            spec_code="SPREJ",
            avl=[{"mpn": "Y", "manufacturer": "M", "rank": 1, "notes": None}],
            source="manual",
            approved_at=datetime.now(timezone.utc),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_oem_spec_code_pending_unique_constraint(db_session):
    db_session.add(
        OemSpecCodePending(
            oem="IBM",
            spec_code="SPREJ",
            proposed_avl=[{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}],
            llm_confidence=0.8,
        )
    )
    db_session.commit()

    db_session.add(
        OemSpecCodePending(
            oem="IBM",
            spec_code="SPREJ",
            proposed_avl=[{"mpn": "Y", "manufacturer": "M", "rank": 1, "notes": None}],
            llm_confidence=0.6,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_blacklist_no_unique_constraint(db_session):
    """Multiple blacklist entries for the same spec code are allowed
    (each entry represents one rejection event)."""
    for mpn in ["A", "B"]:
        db_session.add(
            OemSpecCodeBlacklist(
                oem="IBM",
                spec_code="SPREJ",
                rejected_mpns=[mpn],
                reason="incorrect",
            )
        )
    db_session.commit()
    rows = db_session.query(OemSpecCodeBlacklist).filter_by(spec_code="SPREJ").all()
    assert len(rows) == 2


def test_requirement_oem_hint_defaults_to_none(db_session):
    rset = _new_requisition(db_session)
    req = Requirement(
        requisition_id=rset.id, primary_mpn="ABC123", manufacturer="TI"
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)
    assert req.oem_hint is None
```

The `db_session` fixture already exists in `tests/conftest.py` (creates schema from models via `Base.metadata.create_all`). Since the new models don't exist yet, `Base.metadata.create_all` won't include them — these tests will fail with `ImportError` first.

- [ ] **Step 2.1.2:** Run the tests — confirm they fail:

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_oem_spec_code_models.py -v --override-ini="addopts="
```

Expected: ImportError on `OemSpecCode` (models not yet defined).

### Task 2.2 — Add models to `app/models/sourcing.py`

- [ ] **Step 2.2.1:** Append to `app/models/sourcing.py`:

```python
class OemSpecCode(Base):
    """Authoritative OEM spec code → approved MPN list.

    Only human-approved mappings live here. LLM proposals start in
    OemSpecCodePending and get promoted on approval.

    Called by: app/services/spec_code_resolver.py
    Depends on: User (foreign key)
    """

    __tablename__ = "oem_spec_codes"

    id = Column(Integer, primary_key=True)
    oem = Column(String(64), nullable=False, index=True)
    spec_code = Column(String(64), nullable=False, index=True)
    avl = Column(JSONB, nullable=False)
    source = Column(String(32), nullable=False)
    approved_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at = Column(UTCDateTime, nullable=False)
    created_at = Column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint("oem", "spec_code", name="uq_oem_spec_code"),
    )


class OemSpecCodePending(Base):
    """LLM-discovered mappings awaiting human approval.

    Speculatively used for sourcing while pending; promoted to
    OemSpecCode on approve; deleted on reject (with rejected MPNs
    copied into OemSpecCodeBlacklist).

    Called by: app/services/spec_code_resolver.py,
               app/routers/admin/spec_codes.py
    """

    __tablename__ = "oem_spec_codes_pending"

    id = Column(Integer, primary_key=True)
    oem = Column(String(64), nullable=False, index=True)
    spec_code = Column(String(64), nullable=False, index=True)
    proposed_avl = Column(JSONB, nullable=False)
    llm_confidence = Column(Float, nullable=False)
    citations = Column(JSONB, nullable=False, default=list)
    discovered_at = Column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    first_requirement_id = Column(
        Integer, ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True
    )
    used_in_requirement_ids = Column(JSONB, nullable=False, default=list)

    __table_args__ = (
        UniqueConstraint("oem", "spec_code", name="uq_pending_oem_spec_code"),
    )


class OemSpecCodeBlacklist(Base):
    """Rejected mappings — the resolver passes these to the LLM as an
    exclusion list so the same wrong MPNs aren't proposed again.

    Called by: app/services/spec_code_resolver.py,
               app/routers/admin/spec_codes.py
    """

    __tablename__ = "oem_spec_codes_blacklist"

    id = Column(Integer, primary_key=True)
    oem = Column(String(64), nullable=False, index=True)
    spec_code = Column(String(64), nullable=False)
    rejected_mpns = Column(JSONB, nullable=False)
    rejected_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    rejected_at = Column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    reason = Column(Text, nullable=True)
```

Add `Float` to the import block at the top of the file if not already present:

```python
from sqlalchemy import (
    JSON, Boolean, Column, Date, DateTime, FetchedValue, Float, ForeignKey,
    Integer, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
```

- [ ] **Step 2.2.2:** Add the `oem_hint` column to the existing `Requirement` class in the same file. Find the `customer_pn = Column(String(255))` line and add immediately after:

```python
    oem_hint = Column(String(64), nullable=True)  # which OEM's spec-code vocabulary applies; null → "IBM"
```

- [ ] **Step 2.2.3:** Run the model tests:

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_oem_spec_code_models.py -v --override-ini="addopts="
```

Expected: PASS for all four tests.

### Task 2.3 — Sighting and Offer lineage columns

- [ ] **Step 2.3.1:** Open `app/models/intelligence.py`. Locate the `Sighting` class. Add two columns adjacent to other nullable string fields:

```python
    resolved_via_spec_code = Column(String(64), nullable=True)
    source_mpn = Column(String(255), nullable=True)
```

- [ ] **Step 2.3.2:** Locate the `Offer` class in the same file (or wherever it lives — verify with `grep -n "class Offer" app/models/`). Add the same two columns.

- [ ] **Step 2.3.3:** Write a failing test for the lineage columns. Append to `tests/test_oem_spec_code_models.py`:

```python
def test_sighting_lineage_columns_nullable(db_session):
    from app.models.intelligence import Sighting
    from app.models.sourcing import Requisition, Requirement

    rset = _new_requisition(db_session)
    req = Requirement(
        requisition_id=rset.id, primary_mpn="ABC123", manufacturer="TI"
    )
    db_session.add(req)
    db_session.commit()

    s = Sighting(
        requirement_id=req.id,
        vendor_name="Mouser",
        manufacturer="TI",
        normalized_mpn="ABC123",
        # lineage columns left null
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    assert s.resolved_via_spec_code is None
    assert s.source_mpn is None


def test_sighting_lineage_columns_populated(db_session):
    from app.models.intelligence import Sighting
    from app.models.sourcing import Requisition, Requirement

    rset = _new_requisition(db_session)
    req = Requirement(
        requisition_id=rset.id, primary_mpn="SPREJ", manufacturer=""
    )
    db_session.add(req)
    db_session.commit()

    s = Sighting(
        requirement_id=req.id,
        vendor_name="Broker",
        manufacturer="Murata",
        normalized_mpn="GRM188R71H103KA01D",
        resolved_via_spec_code="SPREJ",
        source_mpn="GRM188R71H103KA01D",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    assert s.resolved_via_spec_code == "SPREJ"
    assert s.source_mpn == "GRM188R71H103KA01D"
```

- [ ] **Step 2.3.4:** Run all model tests:

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_oem_spec_code_models.py -v --override-ini="addopts="
```

Expected: 6 PASS.

### Task 2.4 — Pydantic schemas

- [ ] **Step 2.4.1:** Create `app/schemas/spec_codes.py`:

```python
"""Pydantic schemas for the OEM spec code resolver.

Used by: app/services/spec_code_resolver.py (LLM response validation),
         app/routers/admin/spec_codes.py (admin action payloads)
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AvlEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mpn: str = Field(..., min_length=1, max_length=255)
    manufacturer: str = Field(..., min_length=1, max_length=255)
    rank: int = Field(..., ge=1)
    notes: str | None = Field(default=None, max_length=1000)


class ResolverLlmResponse(BaseModel):
    """Strict schema for what the LLM must return."""

    model_config = ConfigDict(extra="forbid")

    avl: list[AvlEntry]
    confidence: float = Field(..., ge=0.0, le=1.0)
    citations: list[dict] = Field(default_factory=list)
    reasoning: str = Field(default="")


class ApproveActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edited_avl: list[AvlEntry] | None = None  # null = approve as-is


class RejectActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1, max_length=1000)
    rejected_mpns: list[str] = Field(default_factory=list)  # empty → all proposed MPNs


class ReResolveActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # No body fields; presence of the POST is enough


ResolverStatus = Literal["approved", "pending", "unresolved"]
ResolverSource = Literal["table", "llm", "none"]
```

- [ ] **Step 2.4.2:** Write a schema-validation test. Append to `tests/test_oem_spec_code_models.py`:

```python
def test_resolver_llm_response_rejects_extra_fields():
    from pydantic import ValidationError
    from app.schemas.spec_codes import ResolverLlmResponse

    with pytest.raises(ValidationError):
        ResolverLlmResponse.model_validate(
            {
                "avl": [],
                "confidence": 0.0,
                "citations": [],
                "reasoning": "",
                "extra_field": "should fail",
            }
        )


def test_resolver_llm_response_rejects_invalid_confidence():
    from pydantic import ValidationError
    from app.schemas.spec_codes import ResolverLlmResponse

    with pytest.raises(ValidationError):
        ResolverLlmResponse.model_validate(
            {"avl": [], "confidence": 1.5, "citations": [], "reasoning": ""}
        )
```

- [ ] **Step 2.4.3:** Run the full model+schema test file:

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_oem_spec_code_models.py -v --override-ini="addopts="
```

Expected: 8 PASS.

### Task 2.5 — Commit and PR

- [ ] **Step 2.5.1:**

```bash
git add app/models/sourcing.py app/models/intelligence.py app/schemas/spec_codes.py tests/test_oem_spec_code_models.py
git -c commit.gpgsign=false commit -m "feat(models): OemSpecCode, OemSpecCodePending, OemSpecCodeBlacklist; sighting/offer lineage; requirement.oem_hint"
git push -u origin feat/spec-resolver-2-models
gh pr create --base feat/spec-resolver-1-migration \
  --title "feat(models): models and schemas for OEM spec code resolver" \
  --body "Adds models matching the migration in PR-1 + Pydantic schemas for resolver IO and admin actions. Per spec §4 and §5.1."
```

- [ ] **Step 2.5.2:** Run PR-review pipeline (all 7 agents per `CLAUDE.md`).

---

## PR 3 — `SpecCodeResolver` service

**Branch:** `feat/spec-resolver-3-service` off `feat/spec-resolver-2-models`.

**Files:**
- Create: `app/services/spec_code_resolver.py`
- Modify: `app/config.py` (settings)
- Create: `tests/test_spec_code_resolver.py`

### Task 3.1 — Settings

- [ ] **Step 3.1.1:** Add settings to `app/config.py`, in the `Settings` class adjacent to `anthropic_model`:

```python
    spec_resolver_enabled: bool = False
    spec_resolver_min_confidence: float = 0.3
    spec_resolver_model: str = "claude-opus-4-7"
```

- [ ] **Step 3.1.2:** Verify the settings load:

```bash
TESTING=1 PYTHONPATH=/root/availai python -c "from app.config import settings; print(settings.spec_resolver_enabled, settings.spec_resolver_min_confidence, settings.spec_resolver_model)"
```

Expected: `False 0.3 claude-opus-4-7`.

### Task 3.2 — Test scaffolding first (TDD)

- [ ] **Step 3.2.1:** Create `tests/test_spec_code_resolver.py`:

```python
"""Unit tests for the SpecCodeResolver service.

Mocks claude_json and the DB session; covers every branch of resolve().
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.sourcing import (
    OemSpecCode,
    OemSpecCodeBlacklist,
    OemSpecCodePending,
)
from app.services.spec_code_resolver import ResolverResult, SpecCodeResolver


@pytest.fixture
def resolver(db_session):
    return SpecCodeResolver(db_session)


@pytest.fixture
def approved_mapping(db_session):
    row = OemSpecCode(
        oem="IBM",
        spec_code="SPREJ",
        avl=[
            {"mpn": "GRM188R71H103KA01D", "manufacturer": "Murata", "rank": 1, "notes": None}
        ],
        source="manual",
        approved_at=datetime.now(timezone.utc),
    )
    db_session.add(row)
    db_session.commit()
    return row


@pytest.fixture
def pending_mapping(db_session):
    row = OemSpecCodePending(
        oem="IBM",
        spec_code="SPREJ",
        proposed_avl=[
            {"mpn": "GRM188R71H103KA01D", "manufacturer": "Murata", "rank": 1, "notes": None}
        ],
        llm_confidence=0.7,
        citations=[],
    )
    db_session.add(row)
    db_session.commit()
    return row
```

- [ ] **Step 3.2.2:** Run to confirm the import fails (service not yet defined):

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_spec_code_resolver.py -v --override-ini="addopts="
```

Expected: ImportError on `SpecCodeResolver`.

### Task 3.3 — Service skeleton

- [ ] **Step 3.3.1:** Create `app/services/spec_code_resolver.py`:

```python
"""SpecCodeResolver — translate OEM spec codes to approved MPNs.

Called by: app/search_service.py:search_requirement() when the
synchronous connector fanout returns zero sightings.

Depends on: app/models/sourcing.py (OemSpecCode, OemSpecCodePending,
            OemSpecCodeBlacklist), app/utils/claude_client (claude_json),
            app/schemas/spec_codes (ResolverLlmResponse).

The resolver is read-mostly: it short-circuits at any cached layer
before issuing an LLM call. When it does call the LLM, it writes a row
to oem_spec_codes_pending; that row is consumed by the admin UI for
human approval before being promoted to oem_spec_codes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from loguru import logger
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.sourcing import (
    OemSpecCode,
    OemSpecCodeBlacklist,
    OemSpecCodePending,
)
from app.schemas.spec_codes import ResolverLlmResponse, ResolverSource, ResolverStatus


@dataclass
class ResolverResult:
    status: ResolverStatus
    avl: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    citations: list[dict] = field(default_factory=list)
    source: ResolverSource = "none"


_SYSTEM_PROMPT = """You are a parts-engineering expert with deep knowledge of IBM,
Cisco, HP, and Dell internal spec codes for electronic components. Given an OEM
spec code, return the Approved Vendor List (AVL) — the set of manufacturer part
numbers approved by that OEM for parts matching the spec.

Return STRICT JSON matching this schema:
{
  "avl": [{"mpn": "<MPN>", "manufacturer": "<Name>", "rank": <int>, "notes": "<str|null>"}],
  "confidence": <float 0..1>,
  "citations": [{"url": "<url>", "snippet": "<short verbatim quote>"}],
  "reasoning": "<one-paragraph explanation>"
}

Rules:
- If you are not reasonably confident, return {"avl": [], "confidence": 0.0, ...}.
- Lower `rank` = higher preference (1 is primary AVL).
- Use web_search to ground your answer in IBM redbooks, datasheets, or broker catalogs.
- NEVER propose an MPN from the user-provided blacklist.
- Do NOT include any field other than the four above. Extra fields cause rejection.
"""


def _build_user_prompt(spec_code: str, oem: str, blacklist_mpns: list[str]) -> str:
    return (
        f"OEM: {oem}\nSpec code: {spec_code}\n"
        f"Blacklisted MPNs (do NOT propose): {json.dumps(blacklist_mpns)}\n"
        f"Return the AVL as strict JSON per the system prompt."
    )


class SpecCodeResolver:
    """Resolution pipeline: table → pending → blacklist → LLM → pending row."""

    def __init__(self, db: Session, claude_call=None):
        self._db = db
        # Dependency-injected for testing; defaults to the project's claude_json
        if claude_call is None:
            from app.utils.claude_client import claude_json
            claude_call = claude_json
        self._claude_call = claude_call

    async def resolve(self, spec_code: str, oem: str = "IBM") -> ResolverResult:
        norm_code = (spec_code or "").strip().upper()
        norm_oem = (oem or "IBM").strip().upper()
        if not norm_code:
            return ResolverResult(status="unresolved")

        # 1. Authoritative table
        approved = (
            self._db.query(OemSpecCode)
            .filter_by(oem=norm_oem, spec_code=norm_code)
            .one_or_none()
        )
        if approved is not None:
            return ResolverResult(
                status="approved",
                avl=approved.avl,
                confidence=1.0,
                source="table",
            )

        # 2. Pending — reuse prior LLM result
        pending = (
            self._db.query(OemSpecCodePending)
            .filter_by(oem=norm_oem, spec_code=norm_code)
            .one_or_none()
        )
        if pending is not None:
            return ResolverResult(
                status="pending",
                avl=pending.proposed_avl,
                confidence=pending.llm_confidence,
                citations=pending.citations or [],
                source="llm",
            )

        # 3. Blacklist — accumulated rejected MPNs feed into the LLM prompt
        blacklist_mpns = self._load_blacklist(norm_oem, norm_code)

        # 4. LLM call
        llm_result = await self._call_llm(norm_code, norm_oem, blacklist_mpns)
        if llm_result is None:
            return ResolverResult(status="unresolved")

        # 5. Confidence floor
        adjusted_confidence = llm_result.confidence
        if not llm_result.citations:
            adjusted_confidence *= 0.7
        if adjusted_confidence < settings.spec_resolver_min_confidence or not llm_result.avl:
            logger.info(
                "spec_resolver: below floor or empty avl; oem={} code={} conf={}",
                norm_oem, norm_code, adjusted_confidence,
            )
            return ResolverResult(status="unresolved")

        # 6. Persist pending row (idempotent under concurrency)
        avl_payload = [entry.model_dump() for entry in llm_result.avl]
        row = OemSpecCodePending(
            oem=norm_oem,
            spec_code=norm_code,
            proposed_avl=avl_payload,
            llm_confidence=adjusted_confidence,
            citations=llm_result.citations,
        )
        self._db.add(row)
        try:
            self._db.commit()
        except IntegrityError:
            # Concurrent resolver wrote first; re-read the winning row
            self._db.rollback()
            row = (
                self._db.query(OemSpecCodePending)
                .filter_by(oem=norm_oem, spec_code=norm_code)
                .one()
            )
            return ResolverResult(
                status="pending",
                avl=row.proposed_avl,
                confidence=row.llm_confidence,
                citations=row.citations or [],
                source="llm",
            )

        return ResolverResult(
            status="pending",
            avl=avl_payload,
            confidence=adjusted_confidence,
            citations=llm_result.citations,
            source="llm",
        )

    def _load_blacklist(self, oem: str, spec_code: str) -> list[str]:
        rows = (
            self._db.query(OemSpecCodeBlacklist)
            .filter_by(oem=oem, spec_code=spec_code)
            .all()
        )
        flat: list[str] = []
        for r in rows:
            flat.extend(r.rejected_mpns or [])
        return flat

    async def _call_llm(
        self, spec_code: str, oem: str, blacklist_mpns: list[str]
    ) -> ResolverLlmResponse | None:
        try:
            raw = await self._claude_call(
                model=settings.spec_resolver_model,
                system=_SYSTEM_PROMPT,
                user=_build_user_prompt(spec_code, oem, blacklist_mpns),
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
                max_tokens=2000,
            )
        except Exception:
            logger.exception(
                "spec_resolver: LLM call failed; oem={} code={}", oem, spec_code
            )
            return None

        if raw is None:
            return None

        try:
            return ResolverLlmResponse.model_validate(raw)
        except ValidationError:
            logger.exception(
                "spec_resolver: LLM response failed schema validation; oem={} code={} raw={}",
                oem, spec_code, raw,
            )
            return None
```

- [ ] **Step 3.3.2:** Run the test file — the fixtures should now import. Tests in the file are not yet defined, but the collection should succeed:

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_spec_code_resolver.py --collect-only --override-ini="addopts="
```

Expected: 0 tests, 0 errors.

### Task 3.4 — Table-hit branch

- [ ] **Step 3.4.1:** Append to `tests/test_spec_code_resolver.py`:

```python
async def test_resolves_from_table_without_llm_call(resolver, approved_mapping):
    claude_mock = AsyncMock()
    resolver._claude_call = claude_mock

    result = await resolver.resolve("SPREJ")

    assert result.status == "approved"
    assert result.source == "table"
    assert result.confidence == 1.0
    assert result.avl[0]["mpn"] == "GRM188R71H103KA01D"
    claude_mock.assert_not_called()


async def test_normalizes_case_and_whitespace(resolver, approved_mapping):
    result = await resolver.resolve("  sprej  ", oem="ibm")
    assert result.status == "approved"
```

- [ ] **Step 3.4.2:**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_spec_code_resolver.py -v --override-ini="addopts="
```

Expected: 2 PASS.

### Task 3.5 — Pending-hit branch

- [ ] **Step 3.5.1:** Append:

```python
async def test_reuses_pending_row_without_llm_call(resolver, pending_mapping):
    claude_mock = AsyncMock()
    resolver._claude_call = claude_mock

    result = await resolver.resolve("SPREJ")

    assert result.status == "pending"
    assert result.source == "llm"
    assert result.confidence == 0.7
    claude_mock.assert_not_called()
```

- [ ] **Step 3.5.2:** Run; expected: PASS.

### Task 3.6 — Blacklist passes into LLM prompt

- [ ] **Step 3.6.1:** Append:

```python
async def test_blacklist_passes_into_llm_prompt(resolver, db_session):
    db_session.add(
        OemSpecCodeBlacklist(
            oem="IBM",
            spec_code="SPREJ",
            rejected_mpns=["BAD_MPN_1", "BAD_MPN_2"],
            reason="wrong package",
        )
    )
    db_session.commit()

    captured = {}

    async def fake_claude(**kwargs):
        captured.update(kwargs)
        return {
            "avl": [
                {"mpn": "GOOD_MPN", "manufacturer": "Murata", "rank": 1, "notes": None}
            ],
            "confidence": 0.9,
            "citations": [{"url": "https://example.com", "snippet": "..."}],
            "reasoning": "ok",
        }

    resolver._claude_call = fake_claude
    result = await resolver.resolve("SPREJ")

    assert "BAD_MPN_1" in captured["user"]
    assert "BAD_MPN_2" in captured["user"]
    assert result.status == "pending"
    assert result.avl[0]["mpn"] == "GOOD_MPN"
```

- [ ] **Step 3.6.2:** Run; expected: PASS.

### Task 3.7 — LLM empty AVL → unresolved

- [ ] **Step 3.7.1:** Append:

```python
async def test_empty_avl_treated_as_unresolved(resolver):
    async def fake_claude(**kwargs):
        return {"avl": [], "confidence": 0.0, "citations": [], "reasoning": ""}

    resolver._claude_call = fake_claude
    result = await resolver.resolve("UNKNOWN")
    assert result.status == "unresolved"

    # And no pending row should have been written
    from app.models.sourcing import OemSpecCodePending
    assert resolver._db.query(OemSpecCodePending).count() == 0
```

- [ ] **Step 3.7.2:** Run; expected: PASS.

### Task 3.8 — Confidence floor + WebSearch penalty

- [ ] **Step 3.8.1:** Append:

```python
async def test_confidence_below_floor_unresolved(resolver):
    async def fake_claude(**kwargs):
        return {
            "avl": [
                {"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}
            ],
            "confidence": 0.2,  # below default floor 0.3
            "citations": [{"url": "https://example.com", "snippet": "..."}],
            "reasoning": "low",
        }

    resolver._claude_call = fake_claude
    result = await resolver.resolve("FOO")
    assert result.status == "unresolved"


async def test_no_citations_applies_penalty(resolver):
    """confidence 0.5 with no citations → 0.5 * 0.7 = 0.35 ≥ floor 0.3."""
    async def fake_claude(**kwargs):
        return {
            "avl": [
                {"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}
            ],
            "confidence": 0.5,
            "citations": [],
            "reasoning": "no citations",
        }

    resolver._claude_call = fake_claude
    result = await resolver.resolve("FOO")
    assert result.status == "pending"
    assert result.confidence == pytest.approx(0.35)


async def test_no_citations_below_penalized_floor_unresolved(resolver):
    """confidence 0.4 with no citations → 0.4 * 0.7 = 0.28 < floor 0.3."""
    async def fake_claude(**kwargs):
        return {
            "avl": [
                {"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}
            ],
            "confidence": 0.4,
            "citations": [],
            "reasoning": "no citations, just below penalized floor",
        }

    resolver._claude_call = fake_claude
    result = await resolver.resolve("FOO")
    assert result.status == "unresolved"
```

- [ ] **Step 3.8.2:** Run; expected: PASS.

### Task 3.9 — LLM failure modes

- [ ] **Step 3.9.1:** Append:

```python
async def test_llm_exception_returns_unresolved(resolver):
    async def fake_claude(**kwargs):
        raise RuntimeError("api down")

    resolver._claude_call = fake_claude
    result = await resolver.resolve("FOO")
    assert result.status == "unresolved"


async def test_llm_none_returns_unresolved(resolver):
    async def fake_claude(**kwargs):
        return None

    resolver._claude_call = fake_claude
    result = await resolver.resolve("FOO")
    assert result.status == "unresolved"


async def test_llm_schema_invalid_returns_unresolved(resolver):
    async def fake_claude(**kwargs):
        return {"avl": "not a list", "confidence": 0.9}  # invalid

    resolver._claude_call = fake_claude
    result = await resolver.resolve("FOO")
    assert result.status == "unresolved"
```

- [ ] **Step 3.9.2:** Run; expected: PASS.

### Task 3.10 — Concurrent insert collision

- [ ] **Step 3.10.1:** Append:

```python
async def test_concurrent_pending_insert_recovers_via_reread(resolver, db_session):
    """Simulate a second resolver running for the same spec code by
    inserting a pending row out-of-band right before the LLM commit."""

    async def fake_claude(**kwargs):
        # Sneak a competing row in just before the resolver commits its own
        db_session.add(
            OemSpecCodePending(
                oem="IBM",
                spec_code="FOO",
                proposed_avl=[
                    {"mpn": "WINNER", "manufacturer": "M", "rank": 1, "notes": None}
                ],
                llm_confidence=0.8,
                citations=[],
            )
        )
        db_session.commit()
        return {
            "avl": [
                {"mpn": "LOSER", "manufacturer": "M", "rank": 1, "notes": None}
            ],
            "confidence": 0.9,
            "citations": [{"url": "https://example.com", "snippet": "..."}],
            "reasoning": "would have lost",
        }

    resolver._claude_call = fake_claude
    result = await resolver.resolve("FOO")

    assert result.status == "pending"
    assert result.avl[0]["mpn"] == "WINNER"  # the resolver re-read the winning row
```

- [ ] **Step 3.10.2:** Run; expected: PASS. (If the test fails because `db_session` is the same session as `resolver._db`, the simulated competing insert needs to happen in a separate session. Update the test to use a second `SessionLocal()` if needed.)

### Task 3.11 — Commit and PR

- [ ] **Step 3.11.1:**

```bash
git add app/services/spec_code_resolver.py app/config.py tests/test_spec_code_resolver.py
git -c commit.gpgsign=false commit -m "feat(services): SpecCodeResolver with table/pending/blacklist/LLM pipeline"
git push -u origin feat/spec-resolver-3-service
gh pr create --base feat/spec-resolver-2-models \
  --title "feat(services): SpecCodeResolver service" \
  --body "Implements the resolver pipeline per spec §5: table → pending → blacklist → LLM (Claude + web_search) → pending row. Not wired into search yet. 11+ unit tests covering every branch."
```

- [ ] **Step 3.11.2:** Run PR-review pipeline (all 7 agents).

---

## PR 4 — Wire into `search_service` and worker queues

**Branch:** `feat/spec-resolver-4-wire` off `feat/spec-resolver-3-service`.

**Files:**
- Modify: `app/services/search_worker_base/queue_manager.py`
- Modify: `app/services/ics_worker/queue_manager.py`
- Modify: `app/services/nc_worker/queue_manager.py`
- Modify: `app/search_service.py`
- Create: `tests/test_search_service_with_spec_resolver.py`
- Create: `tests/e2e/test_spec_code_resolver_e2e.py`

### Task 4.1 — Extend `QueueManager.enqueue_search()` signature

- [ ] **Step 4.1.1:** Write the failing test. Create `tests/test_queue_manager_override_mpn.py`:

```python
"""Verify QueueManager.enqueue_search supports override_mpn for
resolved-AVL enqueueing per spec §6.4."""

from __future__ import annotations

from app.services.ics_worker.queue_manager import enqueue_for_ics_search
from app.models.sourcing import Requirement, Requisition
from app.models.ics_search_queue import IcsSearchQueue


def test_default_uses_requirement_primary_mpn(db_session):
    rset = Requisition(name="test")
    db_session.add(rset)
    db_session.commit()
    req = Requirement(
        requisition_id=rset.id, primary_mpn="ABC123", manufacturer=""
    )
    db_session.add(req)
    db_session.commit()

    enqueue_for_ics_search(req.id, db_session)
    item = db_session.query(IcsSearchQueue).filter_by(requirement_id=req.id).one()
    assert item.normalized_mpn == "ABC123"
    assert getattr(item, "resolved_via_spec_code", None) is None


def test_override_mpn_used_when_provided(db_session):
    rset = Requisition(name="test")
    db_session.add(rset)
    db_session.commit()
    req = Requirement(
        requisition_id=rset.id, primary_mpn="SPREJ", manufacturer=""
    )
    db_session.add(req)
    db_session.commit()

    enqueue_for_ics_search(
        req.id, db_session, override_mpn="GRM188R71H103KA01D",
        resolved_via_spec_code="SPREJ",
    )
    items = db_session.query(IcsSearchQueue).filter_by(requirement_id=req.id).all()
    assert any(i.normalized_mpn == "GRM188R71H103KA01D" for i in items)
    item = next(i for i in items if i.normalized_mpn == "GRM188R71H103KA01D")
    assert item.resolved_via_spec_code == "SPREJ"


def test_primary_and_override_coexist_for_same_requirement(db_session):
    """Critical: the (requirement_id, normalized_mpn) dedup key must
    allow both the primary MPN and a resolved AVL MPN to coexist."""
    rset = Requisition(name="test")
    db_session.add(rset)
    db_session.commit()
    req = Requirement(
        requisition_id=rset.id, primary_mpn="SPREJ", manufacturer=""
    )
    db_session.add(req)
    db_session.commit()

    # Primary enqueue
    enqueue_for_ics_search(req.id, db_session)
    # AVL enqueue for the same requirement
    enqueue_for_ics_search(
        req.id, db_session, override_mpn="GRM188R71H103KA01D",
        resolved_via_spec_code="SPREJ",
    )

    items = db_session.query(IcsSearchQueue).filter_by(requirement_id=req.id).all()
    mpns = sorted(i.normalized_mpn for i in items)
    assert mpns == ["GRM188R71H103KA01D", "SPREJ"]


def test_repeat_override_same_mpn_dedups(db_session):
    """The same (requirement, override_mpn) enqueued twice should dedup
    on the second call (returns the existing row)."""
    rset = Requisition(name="test")
    db_session.add(rset)
    db_session.commit()
    req = Requirement(
        requisition_id=rset.id, primary_mpn="SPREJ", manufacturer=""
    )
    db_session.add(req)
    db_session.commit()

    a = enqueue_for_ics_search(
        req.id, db_session, override_mpn="GRM188R71H103KA01D",
        resolved_via_spec_code="SPREJ",
    )
    b = enqueue_for_ics_search(
        req.id, db_session, override_mpn="GRM188R71H103KA01D",
        resolved_via_spec_code="SPREJ",
    )
    assert a.id == b.id
    items = db_session.query(IcsSearchQueue).filter_by(
        requirement_id=req.id, normalized_mpn="GRM188R71H103KA01D"
    ).all()
    assert len(items) == 1
```

The second test will fail until the queue manager is extended.

- [ ] **Step 4.1.2:** Modify `app/services/search_worker_base/queue_manager.py:enqueue_search` (~line 81). The current dedup keys only on `requirement_id`, which would silently no-op every AVL enqueue (the primary-MPN row already exists). The dedup MUST be re-keyed on `(requirement_id, normalized_mpn)` so multiple queue rows can coexist per requirement.

Full updated function body:

```python
def enqueue_search(
    self,
    requirement_id: int,
    db: Session,
    override_mpn: str | None = None,
    resolved_via_spec_code: str | None = None,
):
    """Enqueue a requirement for browser-driven search.

    When override_mpn is None (default), the worker reads
    req.primary_mpn. When override_mpn is provided (resolved AVL
    MPN), the worker searches that MPN instead;
    resolved_via_spec_code is recorded on the queue item and
    propagated onto any sightings created.

    Dedup short-circuit keys on (requirement_id, normalized_mpn) so
    one requirement can have multiple queue rows (primary + resolved
    AVL MPNs).
    """
    req = db.get(Requirement, requirement_id)
    if not req:
        logger.debug("{} enqueue skip: requirement {} not found", self.log_prefix, requirement_id)
        return None

    mpn_to_search = override_mpn or req.primary_mpn
    if not mpn_to_search:
        logger.debug("{} enqueue skip: requirement {} has no MPN", self.log_prefix, requirement_id)
        return None

    norm_mpn = strip_packaging_suffixes(mpn_to_search)
    if not norm_mpn:
        return None

    model = self.queue_model

    # Already queued for THIS (requirement, mpn) pair?
    existing = (
        db.query(model)
        .filter_by(requirement_id=requirement_id, normalized_mpn=norm_mpn)
        .first()
    )
    if existing:
        logger.debug(
            "{} enqueue skip: requirement {} mpn {} already queued (id={})",
            self.log_prefix, requirement_id, norm_mpn, existing.id,
        )
        return existing

    # ... rest of the existing function body (the dedup-window check
    # on normalized_mpn, the link_sighting block, and the final
    # `item = model(...)` row creation), unchanged EXCEPT:
    # - the final item creation gets `resolved_via_spec_code=resolved_via_spec_code`
    # - replace every reference to `req.primary_mpn` with `mpn_to_search` from here down

    # ... (existing dedup-window block — verbatim from current file)

    item = model(
        requirement_id=requirement_id,
        normalized_mpn=norm_mpn,
        # ... other existing fields ...
        resolved_via_spec_code=resolved_via_spec_code,
    )
    db.add(item)
    db.commit()
    return item
```

When making this change, preserve the existing dedup-window block (the `cutoff` / `recent` query that links sightings from prior searches). Do not remove it. The only changes are:
1. Replace the per-requirement `existing` check with the per-(requirement, mpn) check shown above.
2. Replace `req.primary_mpn` → `mpn_to_search` throughout the function body.
3. Add `resolved_via_spec_code=resolved_via_spec_code` to the `model(...)` constructor.

- [ ] **Step 4.1.3:** Add `resolved_via_spec_code` column to both queue models. Open `app/models/ics_search_queue.py`; in the `IcsSearchQueue` class add:

```python
    resolved_via_spec_code = Column(String(64), nullable=True)
```

Same for `app/models/nc_search_queue.py`.

- [ ] **Step 4.1.4:** Add a small migration for the two new columns. Run:

```bash
alembic revision -m "add_resolved_via_spec_code_to_queue_tables"
```

Edit the generated file:

```python
def upgrade() -> None:
    op.add_column("ics_search_queue", sa.Column("resolved_via_spec_code", sa.String(64), nullable=True))
    op.add_column("nc_search_queue", sa.Column("resolved_via_spec_code", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("nc_search_queue", "resolved_via_spec_code")
    op.drop_column("ics_search_queue", "resolved_via_spec_code")
```

- [ ] **Step 4.1.5:** Update the wrappers. In `app/services/ics_worker/queue_manager.py`:

```python
def enqueue_for_ics_search(
    requirement_id: int,
    db: Session,
    override_mpn: str | None = None,
    resolved_via_spec_code: str | None = None,
) -> IcsSearchQueue | None:
    """Queue a requirement for ICsource search.

    Optional override_mpn enables enqueueing a resolved-AVL MPN
    distinct from req.primary_mpn (spec §6.4).
    """
    return _qm.enqueue_search(
        requirement_id, db,
        override_mpn=override_mpn,
        resolved_via_spec_code=resolved_via_spec_code,
    )
```

Same shape for `app/services/nc_worker/queue_manager.py`.

- [ ] **Step 4.1.6:** Run the queue tests:

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_queue_manager_override_mpn.py -v --override-ini="addopts="
```

Expected: 2 PASS.

### Task 4.2 — Wire resolver into `search_requirement`

- [ ] **Step 4.2.1:** Read `app/search_service.py` lines 270–420 to confirm the exact insertion site and surrounding code.

- [ ] **Step 4.2.2:** Write the failing integration test. Create `tests/test_search_service_with_spec_resolver.py`:

```python
"""Integration tests for the zero-hit spec-code fallback inside
search_requirement(). Mocks connectors and the resolver's LLM call;
leaves the resolver itself live so the table/pending writes happen."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.models.intelligence import Sighting
from app.models.sourcing import (
    OemSpecCodePending,
    Requirement,
    Requisition,
)


@pytest.fixture(autouse=True)
def enable_flag(monkeypatch):
    monkeypatch.setattr(settings, "spec_resolver_enabled", True)
    yield


@pytest.fixture
def requirement(db_session):
    rset = Requisition(name="test")
    db_session.add(rset)
    db_session.commit()
    req = Requirement(
        requisition_id=rset.id, primary_mpn="SPREJ", manufacturer=""
    )
    db_session.add(req)
    db_session.commit()
    return req


async def test_known_mpn_does_not_trigger_resolver(db_session, monkeypatch):
    """When synchronous fanout returns ≥1 sighting, resolver never runs."""
    from app import search_service

    rset = Requisition(name="test")
    db_session.add(rset)
    db_session.commit()
    req = Requirement(
        requisition_id=rset.id, primary_mpn="ABC123", manufacturer=""
    )
    db_session.add(req)
    db_session.commit()

    # Stub _fetch_fresh to return one fake result
    async def fake_fetch_fresh(mpns, db):
        return (
            [{"mpn": "ABC123", "vendor_name": "Mouser", "qty_available": 100, "unit_price": 1.0, "source_type": "mouser"}],
            [{"source": "mouser", "status": "ok"}],
        )

    monkeypatch.setattr(search_service, "_fetch_fresh", fake_fetch_fresh)
    resolve_spy = AsyncMock()
    monkeypatch.setattr(
        "app.services.spec_code_resolver.SpecCodeResolver.resolve",
        resolve_spy,
    )

    await search_service.search_requirement(req, db_session)

    resolve_spy.assert_not_called()


async def test_zero_hit_triggers_resolver_and_re_fanout(
    db_session, requirement, monkeypatch
):
    from app import search_service

    fetch_calls = []

    async def fake_fetch_fresh(mpns, db):
        fetch_calls.append(list(mpns))
        if mpns == ["SPREJ"]:
            return ([], [{"source": "mouser", "status": "ok"}])
        # AVL re-fanout returns hits
        return (
            [
                {"mpn": mpns[0], "vendor_name": "Broker", "qty_available": 1500,
                 "unit_price": 0.5, "source_type": "oemsecrets"}
            ],
            [{"source": "oemsecrets", "status": "ok"}],
        )

    monkeypatch.setattr(search_service, "_fetch_fresh", fake_fetch_fresh)

    async def fake_resolve(self, spec_code, oem="IBM"):
        from app.services.spec_code_resolver import ResolverResult
        return ResolverResult(
            status="pending",
            avl=[
                {"mpn": "GRM188R71H103KA01D", "manufacturer": "Murata", "rank": 1, "notes": None}
            ],
            confidence=0.8,
            citations=[{"url": "https://example.com", "snippet": "..."}],
            source="llm",
        )

    monkeypatch.setattr(
        "app.services.spec_code_resolver.SpecCodeResolver.resolve",
        fake_resolve,
    )

    await search_service.search_requirement(requirement, db_session)

    # _fetch_fresh called once for primary, once for AVL
    assert fetch_calls[0] == ["SPREJ"]
    assert fetch_calls[1] == ["GRM188R71H103KA01D"]

    # Sighting was written with lineage
    s = db_session.query(Sighting).filter_by(requirement_id=requirement.id).one()
    assert s.resolved_via_spec_code == "SPREJ"
    assert s.source_mpn == "GRM188R71H103KA01D"


async def test_flag_off_skips_resolver_on_zero(
    db_session, requirement, monkeypatch
):
    monkeypatch.setattr(settings, "spec_resolver_enabled", False)
    from app import search_service

    async def fake_fetch_fresh(mpns, db):
        return ([], [{"source": "mouser", "status": "ok"}])

    monkeypatch.setattr(search_service, "_fetch_fresh", fake_fetch_fresh)
    resolve_spy = AsyncMock()
    monkeypatch.setattr(
        "app.services.spec_code_resolver.SpecCodeResolver.resolve",
        resolve_spy,
    )

    await search_service.search_requirement(requirement, db_session)
    resolve_spy.assert_not_called()


async def test_resolver_pending_records_requirement_id(
    db_session, requirement, monkeypatch
):
    from app import search_service

    async def fake_fetch_fresh(mpns, db):
        return ([], [{"source": "mouser", "status": "ok"}])

    monkeypatch.setattr(search_service, "_fetch_fresh", fake_fetch_fresh)

    # Pre-seed a pending row so resolve() returns "pending" without an LLM call
    pending = OemSpecCodePending(
        oem="IBM",
        spec_code="SPREJ",
        proposed_avl=[
            {"mpn": "GRM188R71H103KA01D", "manufacturer": "Murata", "rank": 1, "notes": None}
        ],
        llm_confidence=0.8,
        citations=[],
    )
    db_session.add(pending)
    db_session.commit()

    await search_service.search_requirement(requirement, db_session)

    db_session.refresh(pending)
    assert requirement.id in (pending.used_in_requirement_ids or [])
```

- [ ] **Step 4.2.3:** Run; expected: all 4 tests fail (resolver not yet wired into `search_requirement`).

- [ ] **Step 4.2.4:** Modify `app/search_service.py:search_requirement`. After `_save_sightings(...)` and the existing material_card upsert block, before the worker enqueues, insert:

```python
        # --- Spec code resolver fallback (spec §6) ---
        if settings.spec_resolver_enabled and len(sightings) == 0:
            from app.services.spec_code_resolver import SpecCodeResolver

            resolver = SpecCodeResolver(write_db)
            resolution = await resolver.resolve(
                write_req.primary_mpn,
                oem=write_req.oem_hint or "IBM",
            )

            if resolution.status != "unresolved" and resolution.avl:
                avl_mpns = [entry["mpn"] for entry in resolution.avl]
                resolved_fresh, resolved_stats = await _fetch_fresh(avl_mpns, db)
                for s_row in resolved_fresh:
                    s_row["resolved_via_spec_code"] = write_req.primary_mpn
                    s_row["source_mpn"] = s_row.get("mpn")
                resolved_succeeded = {
                    stat["source"]
                    for stat in resolved_stats
                    if stat["status"] == SourceRunStatus.OK.value
                    and not stat.get("error")
                }
                resolved_sightings = _save_sightings(
                    resolved_fresh, write_req, write_db, resolved_succeeded
                )
                sightings.extend(resolved_sightings)
                source_stats.extend(resolved_stats)
                logger.info(
                    "spec_resolver: re-fanout produced {} sightings for req {} (spec_code={})",
                    len(resolved_sightings), req_id, write_req.primary_mpn,
                )

                # Enqueue each AVL MPN to async workers
                for mpn in avl_mpns:
                    try:
                        enqueue_for_ics_search(
                            req_id, write_db,
                            override_mpn=mpn,
                            resolved_via_spec_code=write_req.primary_mpn,
                        )
                        enqueue_for_nc_search(
                            req_id, write_db,
                            override_mpn=mpn,
                            resolved_via_spec_code=write_req.primary_mpn,
                        )
                    except Exception:
                        logger.warning(
                            "spec_resolver: AVL worker enqueue failed for mpn {}",
                            mpn, exc_info=True,
                        )

                # If pending, record this requirement id on the pending row
                if resolution.status == "pending":
                    from app.models.sourcing import OemSpecCodePending
                    pending_row = (
                        write_db.query(OemSpecCodePending)
                        .filter_by(
                            oem=(write_req.oem_hint or "IBM").upper(),
                            spec_code=write_req.primary_mpn.upper(),
                        )
                        .one_or_none()
                    )
                    if pending_row is not None:
                        used = list(pending_row.used_in_requirement_ids or [])
                        if req_id not in used:
                            used.append(req_id)
                            pending_row.used_in_requirement_ids = used
                write_db.commit()
        # --- end resolver block ---
```

Move the `enqueue_for_ics_search` / `enqueue_for_nc_search` imports to the top of `search_service.py` if they're currently inside the existing try-block — they're needed in the resolver block too. Also ensure the `_save_sightings` helper reads `resolved_via_spec_code` and `source_mpn` from the row dicts (see step 4.2.5).

- [ ] **Step 4.2.5:** Modify `_save_sightings` in `app/search_service.py`. Find where it constructs each `Sighting(...)`; add the two new fields:

```python
    sighting = Sighting(
        # ... existing fields ...
        resolved_via_spec_code=row.get("resolved_via_spec_code"),
        source_mpn=row.get("source_mpn"),
    )
```

- [ ] **Step 4.2.6:** Run the integration tests:

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_service_with_spec_resolver.py -v --override-ini="addopts="
```

Expected: 4 PASS.

### Task 4.3 — End-to-end happy path

- [ ] **Step 4.3.1:** Create `tests/e2e/test_spec_code_resolver_e2e.py`:

```python
"""End-to-end happy path: create a requisition with primary_mpn=SPREJ,
mock the LLM and connectors, verify a sighting is persisted with
spec-code lineage and a pending row exists."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.config import settings
from app.models.intelligence import Sighting
from app.models.sourcing import (
    OemSpecCodePending,
    Requirement,
    Requisition,
)


@pytest.fixture(autouse=True)
def enable_flag(monkeypatch):
    monkeypatch.setattr(settings, "spec_resolver_enabled", True)


async def test_e2e_sprej_resolution_persists_sighting_and_pending(
    db_session, monkeypatch
):
    from app import search_service

    rset = Requisition(name="e2e")
    db_session.add(rset)
    db_session.commit()
    req = Requirement(
        requisition_id=rset.id, primary_mpn="SPREJ", manufacturer=""
    )
    db_session.add(req)
    db_session.commit()

    async def fake_fetch_fresh(mpns, db):
        if mpns == ["SPREJ"]:
            return ([], [{"source": "mouser", "status": "ok"}])
        return (
            [
                {"mpn": mpns[0], "vendor_name": "OEMSecrets-Broker",
                 "qty_available": 1500, "unit_price": 0.42,
                 "source_type": "oemsecrets"}
            ],
            [{"source": "oemsecrets", "status": "ok"}],
        )

    async def fake_claude(**kwargs):
        return {
            "avl": [
                {"mpn": "GRM188R71H103KA01D", "manufacturer": "Murata",
                 "rank": 1, "notes": "primary"}
            ],
            "confidence": 0.9,
            "citations": [{"url": "https://www.ibm.com/redbook", "snippet": "SPREJ..."}],
            "reasoning": "matched IBM redbook",
        }

    monkeypatch.setattr(search_service, "_fetch_fresh", fake_fetch_fresh)
    with patch("app.utils.claude_client.claude_json", new=fake_claude):
        await search_service.search_requirement(req, db_session)

    sightings = db_session.query(Sighting).filter_by(requirement_id=req.id).all()
    assert len(sightings) >= 1
    assert any(s.resolved_via_spec_code == "SPREJ" for s in sightings)

    pending = (
        db_session.query(OemSpecCodePending)
        .filter_by(oem="IBM", spec_code="SPREJ").one()
    )
    assert pending.proposed_avl[0]["mpn"] == "GRM188R71H103KA01D"
    assert req.id in pending.used_in_requirement_ids
```

- [ ] **Step 4.3.2:** Run:

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/e2e/test_spec_code_resolver_e2e.py -v --override-ini="addopts="
```

Expected: PASS.

### Task 4.4 — Full test suite sanity check

- [ ] **Step 4.4.1:**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v
```

Expected: all tests pass. Any regression in unrelated test suites means the resolver wiring is leaking behavior; fix before commit.

### Task 4.5 — Commit and PR

- [ ] **Step 4.5.1:**

```bash
git add app/search_service.py app/services/search_worker_base/queue_manager.py \
       app/services/ics_worker/queue_manager.py app/services/nc_worker/queue_manager.py \
       app/models/ics_search_queue.py app/models/nc_search_queue.py \
       alembic/versions/ \
       tests/test_queue_manager_override_mpn.py \
       tests/test_search_service_with_spec_resolver.py \
       tests/e2e/test_spec_code_resolver_e2e.py
git -c commit.gpgsign=false commit -m "feat(sourcing): wire SpecCodeResolver into search_service; AVL worker enqueue"
git push -u origin feat/spec-resolver-4-wire
gh pr create --base feat/spec-resolver-3-service \
  --title "feat(sourcing): wire SpecCodeResolver into search_service and workers" \
  --body "Zero-hit fallback in search_requirement; QueueManager.enqueue_search gains override_mpn + resolved_via_spec_code. Flag-gated (default off). Per spec §6."
```

- [ ] **Step 4.5.2:** Run PR-review pipeline (all 7 agents).

---

## PR 5 — Admin UI for pending-approval queue

**Branch:** `feat/spec-resolver-5-admin` off `feat/spec-resolver-2-models` (no dependency on PR 3/4 wiring, so it can land in parallel).

**Files:**
- Create: `app/routers/admin/spec_codes.py`
- Create: `app/templates/htmx/admin/spec_codes_pending.html`
- Create: `tests/routers/admin/test_spec_codes_pending.py`
- Modify: `app/main.py` (mount router)
- Modify: existing admin index template (link to new page)

### Task 5.1 — Router scaffolding

- [ ] **Step 5.1.1:** Create `app/routers/admin/spec_codes.py`:

```python
"""Admin router — pending OEM spec-code mapping approval queue.

Routes (all require require_settings_access):
- GET  /admin/spec-codes/pending                     — list page
- POST /admin/spec-codes/pending/{id}/approve        — promote to OemSpecCode
- POST /admin/spec-codes/pending/{id}/approve-edited — promote with edited AVL
- POST /admin/spec-codes/pending/{id}/reject         — move MPNs to blacklist
- POST /admin/spec-codes/pending/{id}/re-resolve     — re-run LLM with current blacklist

Called by: app/main.py (router mount)
Depends on: app/models/sourcing.py, app/services/spec_code_resolver.py,
            app/schemas/spec_codes.py
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_settings_access
from app.models.sourcing import (
    OemSpecCode,
    OemSpecCodeBlacklist,
    OemSpecCodePending,
    User,
)
from app.schemas.spec_codes import (
    ApproveActionBody,
    RejectActionBody,
)

router = APIRouter(prefix="/admin/spec-codes", tags=["admin"])


@router.get("/pending", response_class=HTMLResponse)
async def list_pending(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_settings_access),
):
    rows = (
        db.query(OemSpecCodePending)
        .order_by(OemSpecCodePending.discovered_at.desc())
        .all()
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "htmx/admin/spec_codes_pending.html",
        {"rows": rows, "user": user},
    )


@router.post("/pending/{pending_id}/approve", response_class=HTMLResponse)
async def approve(
    pending_id: int,
    body: ApproveActionBody,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_settings_access),
):
    row = db.get(OemSpecCodePending, pending_id)
    if row is None:
        raise HTTPException(status_code=404, detail="pending mapping not found")

    avl_to_save = (
        [e.model_dump() for e in body.edited_avl]
        if body.edited_avl is not None
        else row.proposed_avl
    )
    approved = OemSpecCode(
        oem=row.oem,
        spec_code=row.spec_code,
        avl=avl_to_save,
        source="llm_approved",
        approved_by_user_id=user.id,
        approved_at=datetime.now(timezone.utc),
    )
    db.add(approved)
    db.delete(row)
    db.commit()
    return HTMLResponse("", status_code=200)


@router.post("/pending/{pending_id}/reject", response_class=HTMLResponse)
async def reject(
    pending_id: int,
    body: RejectActionBody,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_settings_access),
):
    row = db.get(OemSpecCodePending, pending_id)
    if row is None:
        raise HTTPException(status_code=404, detail="pending mapping not found")

    rejected_mpns = body.rejected_mpns or [
        entry["mpn"] for entry in row.proposed_avl
    ]
    db.add(
        OemSpecCodeBlacklist(
            oem=row.oem,
            spec_code=row.spec_code,
            rejected_mpns=rejected_mpns,
            rejected_by_user_id=user.id,
            reason=body.reason,
        )
    )
    db.delete(row)
    db.commit()
    return HTMLResponse("", status_code=200)


@router.post("/pending/{pending_id}/re-resolve", response_class=HTMLResponse)
async def re_resolve(
    pending_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_settings_access),
):
    from app.services.spec_code_resolver import SpecCodeResolver

    row = db.get(OemSpecCodePending, pending_id)
    if row is None:
        raise HTTPException(status_code=404, detail="pending mapping not found")

    # Delete the existing pending row first so the resolver writes a fresh one
    db.delete(row)
    db.commit()

    resolver = SpecCodeResolver(db)
    result = await resolver.resolve(row.spec_code, oem=row.oem)
    if result.status == "unresolved":
        return HTMLResponse(
            "<div class='alert alert-warning'>Re-resolution returned no result.</div>",
            status_code=200,
        )
    return HTMLResponse("", status_code=200)
```

- [ ] **Step 5.1.2:** Mount the router. In `app/main.py`, locate the existing admin router includes and add:

```python
from app.routers.admin import spec_codes as admin_spec_codes
app.include_router(admin_spec_codes.router)
```

### Task 5.2 — Router tests

- [ ] **Step 5.2.1:** Create `tests/routers/admin/__init__.py` (empty) if it doesn't already exist.

- [ ] **Step 5.2.2:** Create `tests/routers/admin/test_spec_codes_pending.py`:

```python
"""Smoke tests for the pending spec-code approval admin router."""

from __future__ import annotations

import pytest

from app.models.sourcing import (
    OemSpecCode,
    OemSpecCodeBlacklist,
    OemSpecCodePending,
)


@pytest.fixture
def pending_row(db_session):
    row = OemSpecCodePending(
        oem="IBM",
        spec_code="SPREJ",
        proposed_avl=[
            {"mpn": "GRM188R71H103KA01D", "manufacturer": "Murata", "rank": 1, "notes": None}
        ],
        llm_confidence=0.8,
        citations=[{"url": "https://example.com", "snippet": "..."}],
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_list_pending_returns_200(client_with_settings_user, pending_row):
    resp = client_with_settings_user.get("/admin/spec-codes/pending")
    assert resp.status_code == 200
    assert b"SPREJ" in resp.content


def test_approve_promotes_to_oem_spec_codes(
    client_with_settings_user, pending_row, db_session
):
    resp = client_with_settings_user.post(
        f"/admin/spec-codes/pending/{pending_row.id}/approve",
        json={"edited_avl": None},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.query(OemSpecCodePending).count() == 0
    promoted = (
        db_session.query(OemSpecCode).filter_by(oem="IBM", spec_code="SPREJ").one()
    )
    assert promoted.source == "llm_approved"
    assert promoted.approved_by_user_id is not None


def test_approve_with_edited_avl_uses_edited(
    client_with_settings_user, pending_row, db_session
):
    edited = [
        {"mpn": "CORRECTED_MPN", "manufacturer": "Murata", "rank": 1, "notes": "corrected by buyer"}
    ]
    resp = client_with_settings_user.post(
        f"/admin/spec-codes/pending/{pending_row.id}/approve",
        json={"edited_avl": edited},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    promoted = (
        db_session.query(OemSpecCode).filter_by(oem="IBM", spec_code="SPREJ").one()
    )
    assert promoted.avl[0]["mpn"] == "CORRECTED_MPN"


def test_reject_moves_mpns_to_blacklist(
    client_with_settings_user, pending_row, db_session
):
    resp = client_with_settings_user.post(
        f"/admin/spec-codes/pending/{pending_row.id}/reject",
        json={"reason": "wrong package", "rejected_mpns": []},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.query(OemSpecCodePending).count() == 0
    bl = (
        db_session.query(OemSpecCodeBlacklist)
        .filter_by(oem="IBM", spec_code="SPREJ")
        .one()
    )
    assert "GRM188R71H103KA01D" in bl.rejected_mpns


def test_reject_requires_reason(client_with_settings_user, pending_row):
    resp = client_with_settings_user.post(
        f"/admin/spec-codes/pending/{pending_row.id}/reject",
        json={"rejected_mpns": []},  # no reason
    )
    assert resp.status_code == 422
```

The `client_with_settings_user` fixture must provide a TestClient with a user that satisfies `require_settings_access`. Check `tests/conftest.py` for the existing pattern (most admin tests have one); if it doesn't exist, add to `tests/conftest.py`:

```python
@pytest.fixture
def client_with_settings_user(client, db_session):
    """TestClient with a logged-in user with settings access."""
    from app.models.sourcing import User
    u = User(email="admin@example.com", is_admin=True)
    db_session.add(u)
    db_session.commit()
    client.cookies.set("session", f"user_id={u.id}")  # adapt to actual session shape
    return client
```

(Verify the session-cookie shape against the existing auth fixture; this is illustrative.)

- [ ] **Step 5.2.3:** Run tests; expected: PASS.

### Task 5.3 — Template

- [ ] **Step 5.3.1:** Create `app/templates/htmx/admin/spec_codes_pending.html`:

```html
{% extends "base.html" %}

{% block title %}Pending Spec-Code Mappings · Admin · AvailAI{% endblock %}

{% block content %}
<div class="mx-auto max-w-7xl px-6 py-8">
  <header class="mb-6">
    <h1 class="text-2xl font-semibold">Pending OEM Spec-Code Mappings</h1>
    <p class="text-sm text-gray-600 mt-1">
      LLM-discovered mappings awaiting approval. Approve to promote to the
      authoritative table; reject to blacklist proposed MPNs.
    </p>
  </header>

  {% if not rows %}
    <p class="text-gray-600">No pending mappings.</p>
  {% else %}
    <table class="w-full border-collapse">
      <thead>
        <tr class="text-left text-xs uppercase tracking-wide text-gray-500 border-b">
          <th class="py-2 pr-4">OEM</th>
          <th class="py-2 pr-4">Spec code</th>
          <th class="py-2 pr-4">Proposed AVL</th>
          <th class="py-2 pr-4">Confidence</th>
          <th class="py-2 pr-4">Citations</th>
          <th class="py-2 pr-4">Used in</th>
          <th class="py-2 pr-4">Actions</th>
        </tr>
      </thead>
      <tbody>
        {% for row in rows %}
        <tr class="border-b align-top" id="spec-row-{{ row.id }}">
          <td class="py-3 pr-4 font-mono text-sm">{{ row.oem }}</td>
          <td class="py-3 pr-4 font-mono text-sm">{{ row.spec_code }}</td>
          <td class="py-3 pr-4 text-sm">
            <ul class="space-y-1">
              {% for entry in row.proposed_avl %}
                <li>
                  <code>{{ entry.mpn }}</code>
                  <span class="text-gray-500">— {{ entry.manufacturer }} (rank {{ entry.rank }})</span>
                </li>
              {% endfor %}
            </ul>
          </td>
          <td class="py-3 pr-4 text-sm">{{ "%.2f"|format(row.llm_confidence) }}</td>
          <td class="py-3 pr-4 text-sm">
            {% for c in row.citations %}
              <a href="{{ c.url }}" target="_blank" rel="noopener" class="text-blue-600 underline">link</a>{% if not loop.last %}, {% endif %}
            {% endfor %}
          </td>
          <td class="py-3 pr-4 text-sm text-gray-600">
            {{ (row.used_in_requirement_ids or [])|length }} req(s)
          </td>
          <td class="py-3 pr-4">
            <button
              class="text-sm text-green-700 hover:underline mr-2"
              hx-post="/admin/spec-codes/pending/{{ row.id }}/approve"
              hx-ext="json-enc"
              hx-vals='{"edited_avl": null}'
              hx-target="#spec-row-{{ row.id }}"
              hx-swap="outerHTML">Approve</button>
            <button
              class="text-sm text-red-700 hover:underline mr-2"
              hx-post="/admin/spec-codes/pending/{{ row.id }}/reject"
              hx-ext="json-enc"
              hx-vals='{"reason": "rejected from admin UI", "rejected_mpns": []}'
              hx-confirm="Reject this mapping?"
              hx-target="#spec-row-{{ row.id }}"
              hx-swap="outerHTML">Reject</button>
            <button
              class="text-sm text-blue-700 hover:underline"
              hx-post="/admin/spec-codes/pending/{{ row.id }}/re-resolve"
              hx-target="#spec-row-{{ row.id }}"
              hx-swap="outerHTML">Re-resolve</button>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  {% endif %}
</div>
{% endblock %}
```

- [ ] **Step 5.3.2:** Add a link to the admin index. Locate the existing admin landing template (likely `app/templates/htmx/admin/index.html` or similar; verify with `find app/templates -name "*admin*"`). Add:

```html
<a href="/admin/spec-codes/pending" class="..." hx-get="/admin/spec-codes/pending" hx-target="#main-content" hx-push-url="true">
  Spec-code approval queue
</a>
```

Match the styling and HTMX attributes of the surrounding admin links.

### Task 5.4 — Commit and PR

- [ ] **Step 5.4.1:**

```bash
git add app/routers/admin/spec_codes.py app/templates/htmx/admin/spec_codes_pending.html \
       app/main.py app/templates/htmx/admin/index.html \
       tests/routers/admin/__init__.py tests/routers/admin/test_spec_codes_pending.py
git -c commit.gpgsign=false commit -m "feat(admin): pending OEM spec-code approval queue page"
git push -u origin feat/spec-resolver-5-admin
gh pr create --base feat/spec-resolver-2-models \
  --title "feat(admin): pending OEM spec-code approval queue" \
  --body "Admin HTMX page at /admin/spec-codes/pending with approve/edit-approve/reject/re-resolve actions. Per spec §7."
```

- [ ] **Step 5.4.2:** Run PR-review pipeline (all 7 agents).

---

## PR 6 — Enable flag + APP_MAP doc updates

**Branch:** `feat/spec-resolver-6-enable` off `feat/spec-resolver-4-wire` (depends on the wired-up code) and rebased on top of `feat/spec-resolver-5-admin` (or merged after both).

**Files:**
- Modify: `app/config.py` (flip default)
- Modify: `docs/APP_MAP_ARCHITECTURE.md`
- Modify: `docs/APP_MAP_DATABASE.md`
- Modify: `docs/APP_MAP_INTERACTIONS.md`

### Task 6.1 — Flip the feature flag

- [ ] **Step 6.1.1:** In `app/config.py`, change the resolver flag default:

```python
    spec_resolver_enabled: bool = True
```

- [ ] **Step 6.1.2:** Run the full test suite to confirm nothing regresses with the flag on:

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v
```

Expected: all PASS.

### Task 6.2 — Update `APP_MAP_ARCHITECTURE.md`

- [ ] **Step 6.2.1:** Open `docs/APP_MAP_ARCHITECTURE.md`. In the services section, add:

```markdown
### `services/spec_code_resolver.py`

Resolution layer between requirement and connector fanout. Called from
`search_service.search_requirement()` when the synchronous fanout
returns zero sightings. Pipeline:

1. Lookup `OemSpecCode` (table hit → done).
2. Lookup `OemSpecCodePending` (reuse prior LLM result).
3. Load blacklist for `(oem, spec_code)` to use as exclusion set.
4. Call Claude via `claude_json` with the web_search tool.
5. Validate response against `ResolverLlmResponse`; apply confidence
   floor + no-citation penalty; persist to `OemSpecCodePending`.

Approved mappings live in `oem_spec_codes`; pending mappings live in
`oem_spec_codes_pending`; rejected mappings live in
`oem_spec_codes_blacklist`. Promotion happens via the admin queue at
`/admin/spec-codes/pending`.
```

### Task 6.3 — Update `APP_MAP_DATABASE.md`

- [ ] **Step 6.3.1:** Add a "Spec-code resolver" section describing the three new tables, their relationships, and the lineage columns on `requirements`, `sightings`, `offers`, `ics_search_queue`, `nc_search_queue`. Follow the format of existing sections in the file.

### Task 6.4 — Update `APP_MAP_INTERACTIONS.md`

- [ ] **Step 6.4.1:** Add a flow diagram for the zero-hit fallback:

```markdown
### Sourcing flow with spec-code resolution

1. Buyer creates Requirement(primary_mpn="SPREJ").
2. search_requirement() runs _fetch_fresh on synchronous connectors.
3. If synchronous returns 0 sightings AND spec_resolver_enabled:
   a. SpecCodeResolver.resolve("SPREJ", oem="IBM") →
      table → pending → blacklist → LLM → pending row.
   b. For each AVL MPN, _fetch_fresh fans out the synchronous
      connectors against that MPN; sightings tagged with
      resolved_via_spec_code="SPREJ" and source_mpn=<avl_mpn>.
   c. For each AVL MPN, ICS and NC workers are enqueued with
      override_mpn=<avl_mpn> and resolved_via_spec_code="SPREJ".
4. Existing primary-MPN ICS/NC enqueue still runs (preserves
   behavior for partial-miss scenarios).
5. Sightings render with cross_references showing the resolved AVL.
6. Admin reviews pending mappings at /admin/spec-codes/pending and
   approves (promote to oem_spec_codes) or rejects (move to
   oem_spec_codes_blacklist).
```

### Task 6.5 — Commit and PR

- [ ] **Step 6.5.1:**

```bash
git add app/config.py docs/APP_MAP_ARCHITECTURE.md docs/APP_MAP_DATABASE.md docs/APP_MAP_INTERACTIONS.md
git -c commit.gpgsign=false commit -m "chore: enable spec resolver in production; update APP_MAP docs"
git push -u origin feat/spec-resolver-6-enable
gh pr create --base feat/spec-resolver-4-wire \
  --title "chore: enable spec resolver + APP_MAP doc updates" \
  --body "Flips SPEC_RESOLVER_ENABLED to True and updates the three APP_MAP docs per the spec. Final PR in the stack."
```

- [ ] **Step 6.5.2:** Run PR-review pipeline (all 7 agents).

### Task 6.6 — Manual smoke test post-deploy

- [ ] **Step 6.6.1:** After PR 6 merges to main and `./deploy.sh` runs, verify the resolver end-to-end on the live app:

```bash
docker exec availai-app-1 sh -c 'PYTHONPATH=/app python -c "
import asyncio
from app.database import SessionLocal
from app.services.spec_code_resolver import SpecCodeResolver

async def main():
    db = SessionLocal()
    r = SpecCodeResolver(db)
    result = await r.resolve(\"SPREJ\")
    print(result.status, len(result.avl), result.confidence)
    db.close()

asyncio.run(main())"'
```

Expected: prints `pending <N> <confidence>` for an unknown spec; a pending row exists in the DB; the admin page renders it.

---

## Phase 7 — Final verification

- [ ] **Step 7.1:** All 6 PRs merged into `main` bottom-up.
- [ ] **Step 7.2:** `alembic heads` on production DB shows a single head matching the latest revision.
- [ ] **Step 7.3:** Sentry has no new uncaught exceptions tagged `spec_resolver_*` after 24 hours.
- [ ] **Step 7.4:** Admin queue page (`/admin/spec-codes/pending`) is reachable and renders.
- [ ] **Step 7.5:** A real SPREJ requisition (or another known unmatched spec) is resolved and surfaces sightings tagged with `resolved_via_spec_code`.

---

## Self-review checklist (for the planning agent before handing off)

1. **Spec coverage:**
   - §3 decisions → §4 data model → §5 service → §6 integration → §7 admin → §8 error handling → §9 testing → §10 build sequence all covered by tasks in PRs 1–6. ✓
   - §6.2 (sync-only zero-hit detection) → integration test case in §9 covered by `test_known_mpn_does_not_trigger_resolver` and `test_zero_hit_triggers_resolver_and_re_fanout`. ✓
   - §6.4 (`enqueue_search` extension) → Task 4.1 covers both the signature change and the queue-model columns. ✓
   - §6.5 (write-session discipline) → Task 4.2 instantiates the resolver with `write_db`. ✓
2. **Placeholder scan:** no TBDs, no "add error handling", no "similar to Task N". All code blocks present.
3. **Type consistency:** `ResolverResult.avl` is `list[dict]` throughout; `AvlEntry` is the Pydantic shape; `proposed_avl` JSONB matches the Pydantic dump. `override_mpn` named identically across `enqueue_search`, `enqueue_for_ics_search`, `enqueue_for_nc_search`.
4. **Execution model:** per `CLAUDE.md` — subagent-driven, no inline option. Plan handoff dispatches `superpowers:subagent-driven-development` automatically.
