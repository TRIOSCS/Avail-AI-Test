# Activity Feature Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an AI timeline digest (requisition + account) and make the inbox-logging pipeline's health visible and recoverable.

**Architecture:** A new `ActivityDigest` table caches one structured digest per entity, regenerated lazily on view only when the timeline's "basis" (max activity timestamp + count) changes, guarded by a Redis `nx` lock (anti-stampede) and a short cooldown (anti-burst). The digest renders via an HTMX lazy-loaded card. Feature A adds a status helper over existing `User` fields, a Settings health card, a real "scan now" replacing a test-mode no-op, and a conditional disconnected banner.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, PostgreSQL (Alembic), HTMX, Alpine.js, Jinja2, Tailwind, Anthropic (Sonnet via `claude_structured`), Redis.

**Spec:** `docs/superpowers/specs/2026-06-01-activity-feature-optimization-design.md`

**Conventions (verified against codebase):**
- StrEnum constants in `app/constants.py`; never raw strings.
- `UTCDateTime` from `app/database.py` for datetime columns; `Column(..., default=lambda: datetime.now(timezone.utc))`.
- `db.get(Model, id)`; routers thin, logic in `app/services/`; Loguru; header comment on every new file.
- Tests: `TESTING=1 PYTHONPATH=/root/availai-worktrees/activity-optimization pytest …`; in-memory SQLite; mock lazy imports at the source module.

---

## File Structure

- Create: `app/services/activity_digest_service.py` — digest schema, prompts, `get_or_build_digest`.
- Create: `app/templates/htmx/partials/shared/activity_digest_card.html` — digest card (all states).
- Create: `app/templates/htmx/partials/settings/_mailbox_sync_card.html` — inbox health card.
- Create: `app/templates/htmx/partials/shared/inbox_disconnected_banner.html` — banner.
- Create: `alembic/versions/<rev>_add_activity_digest.py` — migration.
- Create tests: `tests/test_activity_digest_service.py`, `tests/test_activity_digest_endpoints.py`, `tests/test_inbox_sync_status.py`, `tests/test_scan_now.py`.
- Modify: `app/constants.py` (3 StrEnums), `app/config.py` (`digest_cooldown_seconds`), `app/models/intelligence.py` (`ActivityDigest`), `app/models/__init__.py` (export), `app/services/activity_service.py` (`get_inbox_sync_status`), `app/routers/htmx_views.py` (digest endpoints, scan-now, settings + requisitions ctx, placeholders), the requisition + customer activity templates, `app/templates/htmx/partials/settings/index.html`, the requisitions list template, `docs/APP_MAP_DATABASE.md`, `docs/APP_MAP_INTERACTIONS.md`.

---

## Task 1: Constants + config setting

**Files:**
- Modify: `app/constants.py` (after the `ActivityType` StrEnum, ~line 343)
- Modify: `app/config.py:132` (near `inbox_scan_interval_min`)
- Test: `tests/test_activity_digest_service.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_activity_digest_service.py`:

```python
"""Tests for activity digest constants, service, and helpers."""
from app.constants import DigestEntityType, DigestStatusSignal, InboxSyncHealth


def test_digest_constants_values():
    assert DigestEntityType.REQUISITION == "requisition"
    assert DigestEntityType.COMPANY == "company"
    assert set(DigestStatusSignal) == {"on_track", "stalled", "needs_attention"}
    assert set(InboxSyncHealth) == {"ok", "warning", "error"}


def test_digest_cooldown_setting_default():
    from app.config import settings
    assert settings.digest_cooldown_seconds == 120
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_activity_digest_service.py -v`
Expected: FAIL with `ImportError: cannot import name 'DigestEntityType'`.

- [ ] **Step 3: Add the StrEnums to `app/constants.py`** (immediately after `ActivityType`)

```python
class DigestEntityType(StrEnum):
    """Entity kinds an ActivityDigest can summarize."""

    REQUISITION = "requisition"
    COMPANY = "company"


class DigestStatusSignal(StrEnum):
    """Digest semantic state — drives the card's color."""

    ON_TRACK = "on_track"
    STALLED = "stalled"
    NEEDS_ATTENTION = "needs_attention"


class InboxSyncHealth(StrEnum):
    """Inbox-sync health for the Settings card and disconnected banner."""

    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
```

- [ ] **Step 4: Add the config field to `app/config.py`** (next to `inbox_scan_interval_min: int = 30`)

```python
    digest_cooldown_seconds: int = 120  # min seconds between AI digest regenerations per entity
```

- [ ] **Step 5: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_activity_digest_service.py -v`
Expected: PASS (both tests).

- [ ] **Step 6: Commit**

```bash
git add app/constants.py app/config.py tests/test_activity_digest_service.py
git commit -m "feat(activity): add digest/inbox StrEnums and cooldown setting"
```

---

## Task 2: `ActivityDigest` model + export

**Files:**
- Modify: `app/models/intelligence.py` (after `ActivityLog`, ~line 312)
- Modify: `app/models/__init__.py:48` (intelligence import block)
- Test: `tests/test_activity_digest_service.py`

- [ ] **Step 1: Write the failing test** (append to `tests/test_activity_digest_service.py`)

```python
def test_activity_digest_model_shape():
    from app.models import ActivityDigest
    cols = {c.name for c in ActivityDigest.__table__.columns}
    assert {
        "id", "entity_type", "entity_id", "headline", "narrative", "highlights",
        "next_step", "status_signal", "generated_at", "basis_last_activity_at",
        "basis_activity_count", "cooldown_until", "model",
    } <= cols
    # one digest per entity
    uniques = [
        tuple(c.name for c in con.columns)
        for con in ActivityDigest.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    ]
    assert ("entity_type", "entity_id") in uniques
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_activity_digest_service.py::test_activity_digest_model_shape -v`
Expected: FAIL with `ImportError: cannot import name 'ActivityDigest'`.

- [ ] **Step 3: Add the model to `app/models/intelligence.py`** (after the `ActivityLog` class)

```python
class ActivityDigest(Base):
    """AI-generated digest of an entity's activity timeline (cache).

    One row per (entity_type, entity_id). Regenerated lazily on view when the
    timeline basis changes; see app/services/activity_digest_service.py.
    """

    __tablename__ = "activity_digest"
    id = Column(Integer, primary_key=True)
    entity_type = Column(String(20), nullable=False)  # DigestEntityType
    entity_id = Column(Integer, nullable=False)

    headline = Column(String(300))
    narrative = Column(Text)
    highlights = Column(JSON)  # list[{"label": str, "value": str}]
    next_step = Column(String(500))
    status_signal = Column(String(20))  # DigestStatusSignal

    generated_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))
    basis_last_activity_at = Column(UTCDateTime)
    basis_activity_count = Column(Integer, default=0)
    cooldown_until = Column(UTCDateTime)
    model = Column(String(50))

    @validates("entity_type")
    def _validate_entity_type(self, key, value):
        from ..constants import DigestEntityType

        return DigestEntityType(value).value  # raises ValueError on unknown

    __table_args__ = (
        Index("uq_activity_digest_entity", "entity_type", "entity_id", unique=True),
    )
```

- [ ] **Step 4: Export from `app/models/__init__.py`** (add to the `from .intelligence import (...)` block, alphabetical)

```python
from .intelligence import (  # noqa: F401
    ActivityDigest,
    ActivityLog,
    ChangeLog,
    MaterialCard,
    MaterialCardAudit,
    MaterialVendorHistory,
    ProactiveDoNotOffer,
    ProactiveMatch,
    ProactiveOffer,
    ProactiveThrottle,
)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_activity_digest_service.py::test_activity_digest_model_shape -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/models/intelligence.py app/models/__init__.py tests/test_activity_digest_service.py
git commit -m "feat(activity): add ActivityDigest model"
```

---

## Task 3: Alembic migration

**Files:**
- Create: `alembic/versions/<rev>_add_activity_digest.py`

- [ ] **Step 1: Autogenerate the migration**

Run: `PYTHONPATH=$PWD alembic revision --autogenerate -m "add activity_digest"`
Open the generated file. It must create `activity_digest` with the columns from Task 2 and the unique index. Ensure `upgrade()` matches this reference (adjust the autogen if it differs):

```python
def upgrade() -> None:
    op.create_table(
        "activity_digest",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=20), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("headline", sa.String(length=300), nullable=True),
        sa.Column("narrative", sa.Text(), nullable=True),
        sa.Column("highlights", sa.JSON(), nullable=True),
        sa.Column("next_step", sa.String(length=500), nullable=True),
        sa.Column("status_signal", sa.String(length=20), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("basis_last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("basis_activity_count", sa.Integer(), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model", sa.String(length=50), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_activity_digest_entity", "activity_digest", ["entity_type", "entity_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_activity_digest_entity", table_name="activity_digest")
    op.drop_table("activity_digest")
```

- [ ] **Step 2: Verify single head**

Run: `PYTHONPATH=$PWD alembic heads`
Expected: exactly one head. If two, `alembic merge heads -m "merge"`.

- [ ] **Step 3: Test upgrade → downgrade → upgrade**

Run:
```bash
PYTHONPATH=$PWD alembic upgrade head
PYTHONPATH=$PWD alembic downgrade -1
PYTHONPATH=$PWD alembic upgrade head
```
Expected: all succeed with no errors.

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/*_add_activity_digest.py
git commit -m "feat(activity): migration for activity_digest table"
```

---

## Task 4: Digest service — schema, prompts, prompt builder

**Files:**
- Create: `app/services/activity_digest_service.py`
- Test: `tests/test_activity_digest_service.py`

- [ ] **Step 1: Write the failing test** (append)

```python
def test_build_prompt_uses_summary_over_notes_and_caps(monkeypatch):
    from app.services import activity_digest_service as svc

    class FakeAct:
        def __init__(self, i):
            self.activity_type = "email_received"
            self.created_at = None
            self.occurred_at = None
            self.direction = "inbound"
            self.contact_name = f"c{i}"
            self.subject = f"s{i}"
            self.summary = f"clean{i}" if i % 2 == 0 else None
            self.notes = f"rawnotes{i}"

    acts = [FakeAct(i) for i in range(10)]
    body = svc._build_activity_lines(acts)
    assert "clean0" in body            # summary used when present
    assert "rawnotes1" in body         # notes fallback when summary None
    assert "rawnotes0" not in body     # raw notes NOT used when summary present


def test_select_system_prompt_by_entity():
    from app.services import activity_digest_service as svc
    from app.constants import DigestEntityType

    assert "sourcing" in svc._system_prompt(DigestEntityType.REQUISITION).lower()
    assert "relationship" in svc._system_prompt(DigestEntityType.COMPANY).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_activity_digest_service.py -k "prompt" -v`
Expected: FAIL (`ModuleNotFoundError`/`AttributeError`).

- [ ] **Step 3: Create `app/services/activity_digest_service.py`**

```python
"""AI activity-timeline digest service.

Builds and caches one structured ActivityDigest per (entity_type, entity_id),
regenerated lazily on view when the timeline basis changes. Guarded by a Redis
nx-lock (anti-stampede) and a short cooldown (anti-burst).

Called by: digest HTMX endpoints in app/routers/htmx_views.py
Depends on: app/utils/claude_client.py (claude_structured), app/services/activity_service.py,
            app/cache/intel_cache.py (_get_redis), app/models/intelligence.py
"""

from datetime import datetime, timedelta, timezone
from enum import StrEnum

from loguru import logger
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import DigestEntityType
from ..models.intelligence import ActivityDigest

ACTIVITY_CAP = 30


class DigestState(StrEnum):
    READY = "ready"
    INSUFFICIENT = "insufficient"
    GENERATING = "generating"
    ERROR = "error"


DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One-line summary, <= 200 chars."},
        "narrative": {"type": "string", "description": "2-4 sentence plain-language summary."},
        "highlights": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["label", "value"],
            },
        },
        "next_step": {"type": "string", "description": "Suggested next action, or empty."},
        "status_signal": {
            "type": "string",
            "enum": ["on_track", "stalled", "needs_attention"],
        },
    },
    "required": ["headline", "narrative", "highlights", "status_signal"],
}

_REQ_SYSTEM = """You summarize the sourcing progress of an electronic-component RFQ for a buyer.
Given a requisition's recent activity timeline, produce a tight digest: which vendors were
contacted, who replied, the best offer seen, what is blocked or outstanding, and the single
most useful next action. status_signal: 'on_track' when progressing, 'stalled' when no recent
inbound movement, 'needs_attention' when replies/offers await a decision."""

_ACCOUNT_SYSTEM = """You summarize the relationship with a customer account for a salesperson.
Given the account's recent activity timeline, produce a tight digest: recent engagement,
responsiveness, sentiment trend, open RFQs, and the single most useful follow-up. status_signal:
'on_track' for healthy engagement, 'stalled' when contact has gone quiet, 'needs_attention'
when something awaits a reply."""


def _system_prompt(entity_type: DigestEntityType) -> str:
    return _REQ_SYSTEM if entity_type == DigestEntityType.REQUISITION else _ACCOUNT_SYSTEM


def _build_activity_lines(activities) -> str:
    """One line per activity, newest-first, reusing the AI-cleaned summary."""
    lines = []
    for a in activities:
        when = (a.occurred_at or a.created_at)
        when_s = when.strftime("%Y-%m-%d") if when else "?"
        text = a.summary or (a.notes[:200] if a.notes else "")
        parts = [when_s, a.activity_type]
        if a.direction:
            parts.append(a.direction)
        if a.contact_name:
            parts.append(a.contact_name)
        if a.subject:
            parts.append(a.subject)
        if text:
            parts.append(f"— {text}")
        lines.append(" | ".join(str(p) for p in parts))
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_activity_digest_service.py -k "prompt" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/activity_digest_service.py tests/test_activity_digest_service.py
git commit -m "feat(activity): digest schema, prompts, prompt builder"
```

---

## Task 5: Digest service — `get_or_build_digest` (cooldown, basis, lock, upsert)

**Files:**
- Modify: `app/services/activity_digest_service.py`
- Test: `tests/test_activity_digest_service.py`

- [ ] **Step 1: Write the failing tests** (append)

```python
import pytest


def _mk_activity(db, **kw):
    from app.models.intelligence import ActivityLog
    from datetime import datetime, timezone
    a = ActivityLog(
        activity_type=kw.get("activity_type", "sales_note"),
        channel="manual",
        requisition_id=kw.get("requisition_id"),
        company_id=kw.get("company_id"),
        notes=kw.get("notes", "note"),
        is_meaningful=True,
        created_at=kw.get("created_at", datetime.now(timezone.utc)),
    )
    db.add(a)
    db.commit()
    return a


@pytest.mark.asyncio
async def test_insufficient_short_circuits_without_ai(db_session, monkeypatch):
    from app.services import activity_digest_service as svc
    from app.constants import DigestEntityType

    called = {"n": 0}
    async def fake_cs(*a, **k):
        called["n"] += 1
        return {}
    monkeypatch.setattr(svc, "claude_structured", fake_cs, raising=False)
    monkeypatch.setattr("app.utils.claude_client.claude_structured", fake_cs)

    _mk_activity(db_session, requisition_id=1)  # only 1 activity
    out = await svc.get_or_build_digest(DigestEntityType.REQUISITION, 1, db_session)
    assert out["state"] == "insufficient"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_generates_then_serves_cache(db_session, monkeypatch):
    from app.services import activity_digest_service as svc
    from app.constants import DigestEntityType

    calls = {"n": 0}
    async def fake_cs(*a, **k):
        calls["n"] += 1
        return {"headline": "h", "narrative": "n", "highlights": [], "status_signal": "on_track"}
    monkeypatch.setattr("app.utils.claude_client.claude_structured", fake_cs)
    monkeypatch.setattr(svc, "_get_redis", lambda: None)  # no redis in tests → no lock contention

    _mk_activity(db_session, requisition_id=2)
    _mk_activity(db_session, requisition_id=2)
    out1 = await svc.get_or_build_digest(DigestEntityType.REQUISITION, 2, db_session)
    assert out1["state"] == "ready" and out1["headline"] == "h"
    assert calls["n"] == 1
    # second view, basis unchanged AND within cooldown → cached, no new AI call
    out2 = await svc.get_or_build_digest(DigestEntityType.REQUISITION, 2, db_session)
    assert out2["state"] == "ready"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_force_bypasses_cooldown(db_session, monkeypatch):
    from app.services import activity_digest_service as svc
    from app.constants import DigestEntityType

    calls = {"n": 0}
    async def fake_cs(*a, **k):
        calls["n"] += 1
        return {"headline": f"h{calls['n']}", "narrative": "n", "highlights": [], "status_signal": "on_track"}
    monkeypatch.setattr("app.utils.claude_client.claude_structured", fake_cs)
    monkeypatch.setattr(svc, "_get_redis", lambda: None)

    _mk_activity(db_session, requisition_id=3)
    _mk_activity(db_session, requisition_id=3)
    await svc.get_or_build_digest(DigestEntityType.REQUISITION, 3, db_session)
    out = await svc.get_or_build_digest(DigestEntityType.REQUISITION, 3, db_session, force=True)
    assert calls["n"] == 2 and out["headline"] == "h2"


@pytest.mark.asyncio
async def test_ai_failure_returns_error_no_row(db_session, monkeypatch):
    from app.services import activity_digest_service as svc
    from app.constants import DigestEntityType
    from app.models.intelligence import ActivityDigest

    async def fake_cs(*a, **k):
        return None
    monkeypatch.setattr("app.utils.claude_client.claude_structured", fake_cs)
    monkeypatch.setattr(svc, "_get_redis", lambda: None)

    _mk_activity(db_session, requisition_id=4)
    _mk_activity(db_session, requisition_id=4)
    out = await svc.get_or_build_digest(DigestEntityType.REQUISITION, 4, db_session)
    assert out["state"] == "error"
    assert db_session.query(ActivityDigest).filter_by(entity_id=4).first() is None
```

> If `db_session` fixture is named differently in `conftest.py`, use the existing session fixture name. Confirm with `grep -n "def db_session\|def db\b" tests/conftest.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_activity_digest_service.py -k "insufficient or cache or force or failure" -v`
Expected: FAIL (`AttributeError: get_or_build_digest`).

- [ ] **Step 3: Append the implementation to `app/services/activity_digest_service.py`**

```python
def _get_redis():
    from ..cache.intel_cache import _get_redis as _r
    return _r()


def _load_activities(entity_type: DigestEntityType, entity_id: int, db: Session):
    from .activity_service import get_company_activities, get_requisition_activities

    if entity_type == DigestEntityType.REQUISITION:
        acts = get_requisition_activities(entity_id, db, limit=ACTIVITY_CAP, meaningful_only=True)
    else:
        acts = get_company_activities(entity_id, db, limit=ACTIVITY_CAP)
        acts = [a for a in acts if a.is_meaningful in (True, None)]
    return acts


def _digest_to_dict(row: ActivityDigest) -> dict:
    return {
        "state": DigestState.READY,
        "headline": row.headline,
        "narrative": row.narrative,
        "highlights": row.highlights or [],
        "next_step": row.next_step,
        "status_signal": row.status_signal,
        "generated_at": row.generated_at,
    }


async def get_or_build_digest(
    entity_type: DigestEntityType, entity_id: int, db: Session, force: bool = False
) -> dict:
    """Return a cached or freshly-built digest dict. See module docstring for the algorithm."""
    now = datetime.now(timezone.utc)
    existing = (
        db.query(ActivityDigest)
        .filter(ActivityDigest.entity_type == entity_type, ActivityDigest.entity_id == entity_id)
        .first()
    )

    # Cooldown guard (skip when force)
    if existing and not force and existing.cooldown_until and existing.cooldown_until > now:
        return _digest_to_dict(existing)

    activities = _load_activities(entity_type, entity_id, db)
    if len(activities) < 2:
        return {"state": DigestState.INSUFFICIENT}

    basis_last = max((a.created_at for a in activities if a.created_at), default=None)
    basis_count = len(activities)

    # Freshness check (skip when force)
    if (
        existing and not force
        and existing.basis_last_activity_at == basis_last
        and existing.basis_activity_count == basis_count
    ):
        return _digest_to_dict(existing)

    # Stampede guard
    r = _get_redis()
    lock_key = f"lock:digest:{entity_type}:{entity_id}"
    acquired = False
    if r is not None:
        try:
            acquired = bool(r.set(lock_key, "1", nx=True, ex=30))
        except Exception as e:  # redis hiccup → proceed without lock
            logger.warning("Digest lock acquire failed ({}): {}", lock_key, e)
            acquired = True
    else:
        acquired = True

    if not acquired:
        if existing:
            return _digest_to_dict(existing)
        return {"state": DigestState.GENERATING}

    try:
        from ..utils.claude_client import claude_structured

        prompt = "Recent activity (newest first):\n" + _build_activity_lines(activities)
        result = await claude_structured(
            prompt=prompt,
            schema=DIGEST_SCHEMA,
            system=_system_prompt(entity_type),
            model_tier="smart",
            max_tokens=700,
            cache_system=True,
        )
        if not result:
            logger.error("Digest AI returned no result for {} {}", entity_type, entity_id)
            return {"state": DigestState.ERROR}

        cooldown = now + timedelta(seconds=settings.digest_cooldown_seconds)
        row = existing or ActivityDigest(entity_type=entity_type, entity_id=entity_id)
        row.headline = (result.get("headline") or "")[:300] or None
        row.narrative = result.get("narrative") or None
        row.highlights = result.get("highlights") or []
        row.next_step = (result.get("next_step") or "")[:500] or None
        row.status_signal = result.get("status_signal") or None
        row.generated_at = now
        row.basis_last_activity_at = basis_last
        row.basis_activity_count = basis_count
        row.cooldown_until = cooldown
        row.model = "smart"
        if existing is None:
            db.add(row)
        db.commit()
        return _digest_to_dict(row)
    finally:
        if r is not None and acquired:
            try:
                r.delete(lock_key)
            except Exception as e:
                logger.warning("Digest lock release failed ({}): {}", lock_key, e)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_activity_digest_service.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add app/services/activity_digest_service.py tests/test_activity_digest_service.py
git commit -m "feat(activity): get_or_build_digest with cooldown, lock, basis cache"
```

---

## Task 6: Digest endpoints + card partial + lazy-load placeholders

**Files:**
- Modify: `app/routers/htmx_views.py` (add two endpoints near the requisition tab routes, ~line 1268)
- Create: `app/templates/htmx/partials/shared/activity_digest_card.html`
- Modify: `app/templates/htmx/partials/requisitions/tabs/activity.html` (top of `#activity-tab-content`)
- Modify: `app/templates/htmx/partials/customers/tabs/activity_tab.html` (top)
- Test: `tests/test_activity_digest_endpoints.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_activity_digest_endpoints.py`:

```python
"""Tests for the digest HTMX endpoints."""
import pytest


@pytest.mark.asyncio
async def test_requisition_digest_endpoint_renders(client, monkeypatch, seed_requisition_with_activities):
    req_id = seed_requisition_with_activities  # fixture creates a req with >=2 meaningful activities
    from app.services import activity_digest_service as svc

    async def fake(*a, **k):
        return {"state": "ready", "headline": "3 vendors contacted",
                "narrative": "Summary.", "highlights": [{"label": "Replies", "value": "2"}],
                "next_step": "Call vendor X", "status_signal": "needs_attention",
                "generated_at": None}
    monkeypatch.setattr(svc, "get_or_build_digest", fake)

    resp = client.get(f"/v2/partials/requisitions/{req_id}/activity-digest")
    assert resp.status_code == 200
    assert "3 vendors contacted" in resp.text
    assert "Call vendor X" in resp.text


@pytest.mark.asyncio
async def test_digest_endpoint_insufficient_state(client, monkeypatch, seed_requisition_with_activities):
    req_id = seed_requisition_with_activities
    from app.services import activity_digest_service as svc

    async def fake(*a, **k):
        return {"state": "insufficient"}
    monkeypatch.setattr(svc, "get_or_build_digest", fake)
    resp = client.get(f"/v2/partials/requisitions/{req_id}/activity-digest")
    assert resp.status_code == 200
    assert "Not enough activity" in resp.text
```

> Reuse existing auth/client fixtures from `conftest.py`. If no `seed_requisition_with_activities` fixture exists, add a small one in this test file creating a `Requisition` + 2 `ActivityLog` rows.

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_activity_digest_endpoints.py -v`
Expected: FAIL (404 — route not defined).

- [ ] **Step 3: Create `app/templates/htmx/partials/shared/activity_digest_card.html`**

```html
{# activity_digest_card.html — AI digest of an entity's activity timeline.
   Receives: digest (dict with 'state' and, when ready, headline/narrative/highlights/
   next_step/status_signal/generated_at), refresh_url (str).
   Called by: digest endpoints in htmx_views.py. #}
{% set signal = digest.status_signal if digest.state == 'ready' else none %}
{% set ring = {'on_track': 'border-green-200 bg-green-50',
               'stalled': 'border-amber-200 bg-amber-50',
               'needs_attention': 'border-red-200 bg-red-50'} %}
<div class="rounded-lg border p-4 {{ ring.get(signal, 'border-brand-200 bg-white') }}">
  {% if digest.state == 'ready' %}
    <div class="flex items-start justify-between gap-3">
      <p class="text-sm font-semibold text-gray-900">{{ digest.headline }}</p>
      <a hx-get="{{ refresh_url }}?force=1" hx-target="closest div" hx-swap="outerHTML"
         class="text-xs text-brand-600 hover:text-brand-700 cursor-pointer whitespace-nowrap">Refresh</a>
    </div>
    {% if digest.narrative %}
    <p class="text-xs text-gray-600 mt-1 leading-relaxed">{{ digest.narrative }}</p>
    {% endif %}
    {% if digest.highlights %}
    <ul class="mt-2 grid grid-cols-2 gap-1">
      {% for h in digest.highlights %}
      <li class="text-xs text-gray-700"><span class="text-gray-400">{{ h.label }}:</span> {{ h.value }}</li>
      {% endfor %}
    </ul>
    {% endif %}
    {% if digest.next_step %}
    <p class="text-xs font-medium text-brand-700 mt-2">Next: {{ digest.next_step }}</p>
    {% endif %}
  {% elif digest.state == 'insufficient' %}
    <p class="text-xs text-gray-400">Not enough activity to summarize yet.</p>
  {% elif digest.state == 'generating' %}
    <div hx-get="{{ refresh_url }}" hx-trigger="load delay:3s" hx-target="this" hx-swap="innerHTML">
      <p class="text-xs text-gray-400">Summary is being prepared…</p>
    </div>
  {% else %}
    <p class="text-xs text-gray-400">Couldn't generate a summary —
      <a hx-get="{{ refresh_url }}?force=1" hx-target="closest div" hx-swap="outerHTML"
         class="text-brand-600 cursor-pointer">try Refresh</a>.</p>
  {% endif %}
</div>
```

- [ ] **Step 4: Add the two endpoints to `app/routers/htmx_views.py`** (after the requisition tab route, ~line 1268)

```python
@router.get("/v2/partials/requisitions/{req_id}/activity-digest", response_class=HTMLResponse)
async def requisition_activity_digest(
    request: Request,
    req_id: int,
    force: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """AI digest card for a requisition's activity timeline (HTMX lazy-load)."""
    from ..constants import DigestEntityType
    from ..services.activity_digest_service import get_or_build_digest

    get_requisition_or_404(db, req_id)
    digest = await get_or_build_digest(DigestEntityType.REQUISITION, req_id, db, force=bool(force))
    ctx = _base_ctx(request, user, "requisitions")
    ctx["digest"] = digest
    ctx["refresh_url"] = f"/v2/partials/requisitions/{req_id}/activity-digest"
    return template_response("htmx/partials/shared/activity_digest_card.html", ctx)


@router.get("/v2/partials/customers/{company_id}/activity-digest", response_class=HTMLResponse)
async def customer_activity_digest(
    request: Request,
    company_id: int,
    force: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """AI digest card for a company's activity timeline (HTMX lazy-load)."""
    from ..constants import DigestEntityType
    from ..services.activity_digest_service import get_or_build_digest

    digest = await get_or_build_digest(DigestEntityType.COMPANY, company_id, db, force=bool(force))
    ctx = _base_ctx(request, user, "customers")
    ctx["digest"] = digest
    ctx["refresh_url"] = f"/v2/partials/customers/{company_id}/activity-digest"
    return template_response("htmx/partials/shared/activity_digest_card.html", ctx)
```

- [ ] **Step 5: Add the lazy-load placeholder to `requisitions/tabs/activity.html`** (right after `<div id="activity-tab-content" ...>`, before the summary bar)

```html
  {# ── AI digest (lazy-loaded) ───────────────────────────── #}
  <div hx-get="/v2/partials/requisitions/{{ req.id }}/activity-digest"
       hx-trigger="load" hx-target="this" hx-swap="innerHTML">
    <div class="rounded-lg border border-brand-200 bg-white p-4 animate-pulse">
      <div class="h-3 w-2/3 bg-gray-100 rounded"></div>
    </div>
  </div>
```

- [ ] **Step 6: Add the same placeholder to `customers/tabs/activity_tab.html`** (at the very top of the tab's root container, using `company.id`)

```html
  <div hx-get="/v2/partials/customers/{{ company.id }}/activity-digest"
       hx-trigger="load" hx-target="this" hx-swap="innerHTML">
    <div class="rounded-lg border border-brand-200 bg-white p-4 animate-pulse">
      <div class="h-3 w-2/3 bg-gray-100 rounded"></div>
    </div>
  </div>
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_activity_digest_endpoints.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/routers/htmx_views.py app/templates/htmx/partials/shared/activity_digest_card.html \
  app/templates/htmx/partials/requisitions/tabs/activity.html \
  app/templates/htmx/partials/customers/tabs/activity_tab.html \
  tests/test_activity_digest_endpoints.py
git commit -m "feat(activity): digest endpoints, card partial, lazy-load placeholders"
```

---

## Task 7: `get_inbox_sync_status` helper

**Files:**
- Modify: `app/services/activity_service.py` (append helper)
- Test: `tests/test_inbox_sync_status.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_inbox_sync_status.py`:

```python
"""Tests for the inbox sync status helper."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.constants import InboxSyncHealth
from app.services.activity_service import get_inbox_sync_status


def _user(**kw):
    base = dict(m365_connected=True, last_inbox_scan=datetime.now(timezone.utc),
                token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                access_token="t", m365_error_reason=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_status_ok():
    s = get_inbox_sync_status(_user())
    assert s["health"] == InboxSyncHealth.OK
    assert s["connected"] is True


def test_status_error_when_disconnected():
    s = get_inbox_sync_status(_user(m365_connected=False))
    assert s["health"] == InboxSyncHealth.ERROR


def test_status_error_when_token_expired():
    s = get_inbox_sync_status(_user(token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    assert s["health"] == InboxSyncHealth.ERROR


def test_status_warning_when_stale():
    old = datetime.now(timezone.utc) - timedelta(hours=6)
    s = get_inbox_sync_status(_user(last_inbox_scan=old))
    assert s["health"] == InboxSyncHealth.WARNING
    assert s["is_stale"] is True


def test_status_stale_when_never_scanned():
    s = get_inbox_sync_status(_user(last_inbox_scan=None))
    assert s["is_stale"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_inbox_sync_status.py -v`
Expected: FAIL (`ImportError: cannot import name 'get_inbox_sync_status'`).

- [ ] **Step 3: Append the helper to `app/services/activity_service.py`**

```python
def get_inbox_sync_status(user) -> dict:
    """Derive inbox-sync health for the Settings card / disconnected banner.

    Reads existing User fields (no new columns). See app/jobs/core_jobs.py:_job_inbox_scan
    for the scheduled poll this surfaces.
    """
    from datetime import datetime, timezone

    from ..config import settings
    from ..constants import InboxSyncHealth
    from ..utils.token_manager import _utc

    now = datetime.now(timezone.utc)
    connected = bool(getattr(user, "m365_connected", False))
    last_scan = getattr(user, "last_inbox_scan", None)

    token_ok = bool(getattr(user, "access_token", None))
    exp = getattr(user, "token_expires_at", None)
    if exp is not None and _utc(exp) <= now:
        token_ok = False

    interval = settings.inbox_scan_interval_min
    if last_scan is None:
        is_stale = True
    else:
        is_stale = (now - _utc(last_scan)) > timedelta(minutes=2 * interval)

    if not connected or not token_ok:
        health = InboxSyncHealth.ERROR
    elif is_stale:
        health = InboxSyncHealth.WARNING
    else:
        health = InboxSyncHealth.OK

    return {
        "connected": connected,
        "last_scan_at": _utc(last_scan) if last_scan else None,
        "is_stale": is_stale,
        "token_ok": token_ok,
        "error_reason": getattr(user, "m365_error_reason", None),
        "health": health,
    }
```

> Add `from datetime import timedelta` to the function's imports if not already imported at module top — verify with `grep -n "^from datetime" app/services/activity_service.py` and reuse the existing import line.

- [ ] **Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_inbox_sync_status.py -v`
Expected: PASS (all 5).

- [ ] **Step 5: Commit**

```bash
git add app/services/activity_service.py tests/test_inbox_sync_status.py
git commit -m "feat(activity): get_inbox_sync_status helper"
```

---

## Task 8: Settings mailbox-sync card + real Scan-now endpoints

**Files:**
- Create: `app/templates/htmx/partials/settings/_mailbox_sync_card.html`
- Modify: `app/templates/htmx/partials/settings/index.html` (include the card in the profile section)
- Modify: `app/routers/htmx_views.py` — (a) pass `inbox_status` into the settings profile route (~line 7997); (b) add `POST /v2/partials/settings/inbox/scan-now`; (c) replace the `poll_inbox_htmx` no-op body (~line 2807) with a real scan.
- Test: `tests/test_scan_now.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_scan_now.py`:

```python
"""Tests for the real scan-now endpoints (TESTING guard => no Graph)."""
import pytest


def test_settings_scan_now_returns_card(client, monkeypatch):
    # Under TESTING=1, the endpoint must NOT call Graph and must return the card partial.
    import app.routers.htmx_views as hv
    called = {"scan": 0}

    async def fake_scan(user, db):
        called["scan"] += 1
    monkeypatch.setattr(hv, "_scan_user_inbox", fake_scan, raising=False)

    resp = client.post("/v2/partials/settings/inbox/scan-now")
    assert resp.status_code == 200
    assert "Mailbox sync" in resp.text
    assert called["scan"] == 0  # TESTING guard skipped the real scan


def test_requisition_poll_inbox_returns_responses_tab(client, seed_requisition):
    req_id = seed_requisition
    resp = client.post(f"/v2/partials/requisitions/{req_id}/poll-inbox")
    assert resp.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_scan_now.py -v`
Expected: FAIL (404 for scan-now route).

- [ ] **Step 3: Create `app/templates/htmx/partials/settings/_mailbox_sync_card.html`**

```html
{# _mailbox_sync_card.html — inbox-sync health for Settings → Profile.
   Receives: inbox_status (dict from get_inbox_sync_status). #}
{% set dot = {'ok': 'bg-green-500', 'warning': 'bg-amber-500', 'error': 'bg-red-500'} %}
<div id="mailbox-sync-card" class="bg-white rounded-lg border border-brand-200 p-4">
  <div class="flex items-center justify-between">
    <h3 class="text-sm font-semibold text-gray-900 flex items-center gap-2">
      <span class="w-2.5 h-2.5 rounded-full {{ dot.get(inbox_status.health, 'bg-gray-300') }}"></span>
      Mailbox sync
    </h3>
    <button hx-post="/v2/partials/settings/inbox/scan-now"
            hx-target="#mailbox-sync-card" hx-swap="outerHTML" data-loading-disable
            class="px-3 py-1.5 text-xs font-medium text-brand-600 bg-brand-50 border border-brand-200 rounded-lg hover:bg-brand-100">
      Scan now
    </button>
  </div>
  <dl class="mt-2 space-y-1 text-xs text-gray-600">
    <div>{{ 'Connected' if inbox_status.connected else 'Not connected' }}</div>
    <div>Last inbox scan:
      {% if inbox_status.last_scan_at %}{{ inbox_status.last_scan_at|timeago }}{% else %}never{% endif %}
    </div>
    {% if inbox_status.error_reason %}
    <div class="text-red-600">{{ inbox_status.error_reason }}</div>
    {% endif %}
  </dl>
</div>
```

- [ ] **Step 4: Include the card in `settings/index.html`** — within the profile tab/section markup, add:

```html
        {% include "htmx/partials/settings/_mailbox_sync_card.html" %}
```

And in the settings profile route (`settings_profile_tab`, ~line 7997) compute and pass it:

```python
    from ..services.activity_service import get_inbox_sync_status
    ctx["inbox_status"] = get_inbox_sync_status(user)
```

- [ ] **Step 5: Add the scan-now endpoint + helper, and fix the no-op** in `app/routers/htmx_views.py`

Add near the other settings partial routes:

```python
async def _run_inbox_scan_now(user: User, db: Session) -> None:
    """Run a real on-demand inbox scan for the current user, unless under TESTING."""
    import os

    if os.getenv("TESTING") == "1":
        return  # hermetic tests: do not touch Graph
    from ..jobs.email_jobs import _scan_user_inbox

    try:
        await asyncio.wait_for(_scan_user_inbox(user, db), timeout=90)
    except asyncio.TimeoutError:
        logger.warning("Manual inbox scan timed out for {}", user.email)


@router.post("/v2/partials/settings/inbox/scan-now", response_class=HTMLResponse)
async def settings_scan_now(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manual inbox scan from the Settings mailbox-sync card."""
    from ..services.activity_service import get_inbox_sync_status

    await _run_inbox_scan_now(user, db)
    db.refresh(user)
    ctx = _base_ctx(request, user, "settings")
    ctx["inbox_status"] = get_inbox_sync_status(user)
    return template_response("htmx/partials/settings/_mailbox_sync_card.html", ctx)
```

Replace the body of `poll_inbox_htmx` (~line 2807) — keep returning the responses tab, but run the real scan first:

```python
    get_requisition_or_404(db, req_id)  # validates existence
    logger.info("Inbox poll requested for req {} by {}", req_id, user.email)
    await _run_inbox_scan_now(user, db)
    return await requisition_tab(request=request, req_id=req_id, tab="responses", user=user, db=db)
```

> Confirm `asyncio` and `logger` are imported at the top of `htmx_views.py` (they are used elsewhere); if not, add `import asyncio` and `from loguru import logger`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_scan_now.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/routers/htmx_views.py app/templates/htmx/partials/settings/_mailbox_sync_card.html \
  app/templates/htmx/partials/settings/index.html tests/test_scan_now.py
git commit -m "feat(activity): settings mailbox-sync card + real scan-now"
```

---

## Task 9: Disconnected banner on the requisitions list

**Files:**
- Create: `app/templates/htmx/partials/shared/inbox_disconnected_banner.html`
- Modify: requisitions list template (the one rendered by `requisitions_list_partial`, ~line 344, and/or the `/v2/requisitions` page at line 155) to include the banner at the top.
- Modify: the requisitions list route to pass `inbox_status`.
- Test: extend `tests/test_inbox_sync_status.py` (render assertion)

- [ ] **Step 1: Write the failing test** (append to `tests/test_inbox_sync_status.py`)

```python
def test_requisitions_list_shows_banner_when_disconnected(client, monkeypatch):
    import app.routers.htmx_views as hv
    from app.constants import InboxSyncHealth

    monkeypatch.setattr(hv, "get_inbox_sync_status",
                        lambda user: {"health": InboxSyncHealth.ERROR, "connected": False,
                                      "is_stale": True, "last_scan_at": None,
                                      "token_ok": False, "error_reason": None},
                        raising=False)
    resp = client.get("/v2/requisitions")
    assert resp.status_code == 200
    assert "mailbox" in resp.text.lower()
```

> If `get_inbox_sync_status` is referenced via `from ..services.activity_service import get_inbox_sync_status` inside the route, patch it at that import path instead. Adjust the patch target to match the actual call site.

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_inbox_sync_status.py -k banner -v`
Expected: FAIL (banner text absent).

- [ ] **Step 3: Create `app/templates/htmx/partials/shared/inbox_disconnected_banner.html`**

```html
{# inbox_disconnected_banner.html — shown only when inbox sync is unhealthy/stale.
   Receives: inbox_status (dict). Dismissible per-session via Alpine. #}
{% if inbox_status and inbox_status.health == 'error' or (inbox_status and inbox_status.is_stale) %}
<div x-data="{ show: true }" x-show="show"
     class="flex items-center justify-between gap-3 rounded-lg border border-amber-300 bg-amber-50 px-4 py-2 mb-3">
  <p class="text-xs text-amber-800">
    Your mailbox sync looks {{ 'disconnected' if inbox_status.health == 'error' else 'stale' }} —
    new vendor replies may not appear.
    <a hx-get="/v2/partials/settings/profile" hx-target="#main-content"
       class="font-medium underline cursor-pointer">Open Settings → Profile</a> to reconnect.
  </p>
  <button @click="show = false" class="text-amber-700 text-sm">&times;</button>
</div>
{% endif %}
```

- [ ] **Step 4: Include the banner + pass `inbox_status`** — at the top of the requisitions list template, add:

```html
{% include "htmx/partials/shared/inbox_disconnected_banner.html" %}
```

In the requisitions list route, add to the context:

```python
    from ..services.activity_service import get_inbox_sync_status
    ctx["inbox_status"] = get_inbox_sync_status(user)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_inbox_sync_status.py -k banner -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/templates/htmx/partials/shared/inbox_disconnected_banner.html app/routers/htmx_views.py \
  $(git diff --name-only -- app/templates/htmx/partials/requisitions) tests/test_inbox_sync_status.py
git commit -m "feat(activity): disconnected-mailbox banner on requisitions list"
```

---

## Task 10: APP_MAP docs

**Files:**
- Modify: `docs/APP_MAP_DATABASE.md`, `docs/APP_MAP_INTERACTIONS.md`

- [ ] **Step 1: Add `activity_digest` to `docs/APP_MAP_DATABASE.md`** in the models/tables section: name, purpose (AI digest cache, one row per entity), key columns (`entity_type`, `entity_id`, basis fields, `cooldown_until`), unique `(entity_type, entity_id)`.

- [ ] **Step 2: Add flows to `docs/APP_MAP_INTERACTIONS.md`:**
  - Digest: tab lazy-loads → endpoint → `get_or_build_digest` (cooldown → basis → Redis lock → Sonnet via `claude_structured` → upsert). Auto-invalidates on timeline change.
  - Inbox observability: `get_inbox_sync_status` over `User` fields → Settings card + banner; "scan now" runs `_scan_user_inbox` (the same path as the scheduled `_job_inbox_scan`).

- [ ] **Step 3: Commit**

```bash
git add docs/APP_MAP_DATABASE.md docs/APP_MAP_INTERACTIONS.md
git commit -m "docs: APP_MAP updates for activity digest + inbox observability"
```

---

## Task 11: Full pipeline gate

- [ ] **Step 1: Format + lint + types**

Run:
```bash
pre-commit run --all-files
```
Expected: all hooks pass (ruff, ruff-format, mypy, docformatter). Fix any findings.

- [ ] **Step 2: Full test suite**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/ -q`
Expected: all pass (no regressions).

- [ ] **Step 3: Frontend build (Tailwind classes used in new templates)**

Run: `npm run build`
Expected: build succeeds; new utility classes present in the bundle.

- [ ] **Step 4: PR-review agents** (per CLAUDE.md): run comment-analyzer, pr-test-analyzer, type-design-analyzer, silent-failure-hunter, code-simplifier, code-reviewer, and feature-dev:code-reviewer on the diff. Fix ALL findings.

- [ ] **Step 5: Simplify pass** — run `/simplify` (or the code-simplifier agent) on the changed files; apply quality fixes.

- [ ] **Step 6: Open PR** (only when asked) targeting `main` from `feat/activity-optimization`.

---

## Self-Review (completed during authoring)

- **Spec coverage:** A1→Task 7; A2→Task 8; A3→Task 8; A4→Task 9; B0→Task 1; B1→Task 2; migration→Task 3; B2→Task 5; B3→Task 4; B4→Task 6; B5→Task 5 (`model_tier="smart"`); testing→tasks' tests + Task 11; docs→Task 10.
- **Placeholders:** none — every code step shows full code; template HTML is concrete.
- **Type consistency:** `get_or_build_digest(entity_type, entity_id, db, force)` and its `{"state": ...}` return shape are used identically in Tasks 5 and 6; `get_inbox_sync_status` dict keys match between Task 7, the card (Task 8), and the banner (Task 9); `DigestState` values (`ready`/`insufficient`/`generating`/`error`) match the card template branches.
- **Open verification points flagged inline** (fixture names, exact import/patch targets, requisitions list template path) are confirm-at-implementation lookups, not design ambiguity.
</content>
