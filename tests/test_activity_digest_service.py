"""Tests for activity digest constants, service, and helpers."""

import pytest

from app.constants import DigestEntityType, DigestStatusSignal, InboxSyncHealth


def test_digest_constants_values():
    assert DigestEntityType.REQUISITION == "requisition"
    assert DigestEntityType.COMPANY == "company"
    assert set(DigestStatusSignal) == {"on_track", "stalled", "needs_attention"}
    assert set(InboxSyncHealth) == {"ok", "warning", "error"}


def test_digest_cooldown_setting_default():
    from app.config import settings

    assert settings.digest_cooldown_seconds == 120


def test_activity_digest_model_shape():
    from app.models import ActivityDigest

    cols = {c.name for c in ActivityDigest.__table__.columns}
    assert {
        "id",
        "entity_type",
        "entity_id",
        "headline",
        "narrative",
        "highlights",
        "next_step",
        "status_signal",
        "generated_at",
        "basis_last_activity_at",
        "basis_activity_count",
        "cooldown_until",
        "model",
    } <= cols
    uniques = [
        tuple(c.name for c in con.columns)
        for con in ActivityDigest.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    ]
    assert ("entity_type", "entity_id") in uniques


def test_build_prompt_uses_summary_over_notes(monkeypatch):
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
    assert "clean0" in body  # summary used when present
    assert "rawnotes1" in body  # notes fallback when summary None
    assert "rawnotes0" not in body  # raw notes NOT used when summary present


def test_select_system_prompt_by_entity():
    from app.constants import DigestEntityType
    from app.services import activity_digest_service as svc

    assert "sourcing" in svc._system_prompt(DigestEntityType.REQUISITION).lower()
    assert "relationship" in svc._system_prompt(DigestEntityType.COMPANY).lower()


class _LockHeldRedis:
    """Stub Redis whose lock is always held by someone else (SET NX fails)."""

    def set(self, *a, **k):
        return False

    def delete(self, *a, **k):
        return None


def _mk_activity(db, **kw):
    from datetime import datetime, timezone

    from app.models.intelligence import ActivityLog
    from app.models.sourcing import Requisition

    req_id = kw.get("requisition_id")
    if req_id is not None:
        existing = db.get(Requisition, req_id)
        if existing is None:
            req = Requisition(id=req_id, name=f"REQ-TEST-{req_id}", status="open")
            db.add(req)
            db.flush()

    a = ActivityLog(
        activity_type=kw.get("activity_type", "sales_note"),
        channel="manual",
        requisition_id=req_id,
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
    from app.constants import DigestEntityType
    from app.services import activity_digest_service as svc

    called = {"n": 0}

    async def fake_cs(*a, **k):
        called["n"] += 1
        return {}

    monkeypatch.setattr("app.utils.claude_client.claude_structured", fake_cs)

    _mk_activity(db_session, requisition_id=1)  # only 1 activity
    out = await svc.get_or_build_digest(DigestEntityType.REQUISITION, 1, db_session)
    assert out["state"] == "insufficient"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_generates_then_serves_cache(db_session, monkeypatch):
    from app.constants import DigestEntityType
    from app.services import activity_digest_service as svc

    calls = {"n": 0}

    async def fake_cs(*a, **k):
        calls["n"] += 1
        return {"headline": "h", "narrative": "n", "highlights": [], "status_signal": "on_track"}

    monkeypatch.setattr("app.utils.claude_client.claude_structured", fake_cs)
    monkeypatch.setattr(svc, "_get_redis", lambda: None)

    _mk_activity(db_session, requisition_id=2)
    _mk_activity(db_session, requisition_id=2)
    out1 = await svc.get_or_build_digest(DigestEntityType.REQUISITION, 2, db_session)
    assert out1["state"] == "ready" and out1["headline"] == "h"
    assert calls["n"] == 1
    out2 = await svc.get_or_build_digest(DigestEntityType.REQUISITION, 2, db_session)
    assert out2["state"] == "ready"
    assert calls["n"] == 1  # within cooldown window → cached (cooldown guard fires first)


@pytest.mark.asyncio
async def test_force_bypasses_cooldown(db_session, monkeypatch):
    from app.constants import DigestEntityType
    from app.services import activity_digest_service as svc

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
    from app.constants import DigestEntityType
    from app.models.intelligence import ActivityDigest
    from app.services import activity_digest_service as svc

    async def fake_cs(*a, **k):
        return None

    monkeypatch.setattr("app.utils.claude_client.claude_structured", fake_cs)
    monkeypatch.setattr(svc, "_get_redis", lambda: None)

    _mk_activity(db_session, requisition_id=4)
    _mk_activity(db_session, requisition_id=4)
    out = await svc.get_or_build_digest(DigestEntityType.REQUISITION, 4, db_session)
    assert out["state"] == "error"
    assert db_session.query(ActivityDigest).filter_by(entity_id=4).first() is None


@pytest.mark.asyncio
async def test_lock_miss_without_existing_returns_generating(db_session, monkeypatch):
    from app.constants import DigestEntityType
    from app.services import activity_digest_service as svc

    ai_calls = {"n": 0}

    async def fake_cs(*a, **k):
        ai_calls["n"] += 1
        return {"headline": "h", "narrative": "n", "highlights": [], "status_signal": "on_track"}

    monkeypatch.setattr("app.utils.claude_client.claude_structured", fake_cs)

    monkeypatch.setattr(svc, "_get_redis", lambda: _LockHeldRedis())

    _mk_activity(db_session, requisition_id=5)
    _mk_activity(db_session, requisition_id=5)
    out = await svc.get_or_build_digest(DigestEntityType.REQUISITION, 5, db_session)
    assert out["state"] == "generating"
    assert ai_calls["n"] == 0  # lock miss must NOT call the AI


@pytest.mark.asyncio
async def test_ai_raised_exception_returns_error(db_session, monkeypatch):
    from app.constants import DigestEntityType
    from app.models.intelligence import ActivityDigest
    from app.services import activity_digest_service as svc
    from app.utils.claude_errors import ClaudeError

    async def boom(*a, **k):
        raise ClaudeError("boom")

    monkeypatch.setattr("app.utils.claude_client.claude_structured", boom)
    monkeypatch.setattr(svc, "_get_redis", lambda: None)
    _mk_activity(db_session, requisition_id=20)
    _mk_activity(db_session, requisition_id=20)
    out = await svc.get_or_build_digest(DigestEntityType.REQUISITION, 20, db_session)
    assert out["state"] == "error"
    assert db_session.query(ActivityDigest).filter_by(entity_id=20).first() is None


@pytest.mark.asyncio
async def test_basis_change_regenerates_after_cooldown(db_session, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.constants import DigestEntityType
    from app.models.intelligence import ActivityDigest
    from app.services import activity_digest_service as svc

    calls = {"n": 0}

    async def fake_cs(*a, **k):
        calls["n"] += 1
        return {"headline": f"h{calls['n']}", "narrative": "n", "highlights": [], "status_signal": "on_track"}

    monkeypatch.setattr("app.utils.claude_client.claude_structured", fake_cs)
    monkeypatch.setattr(svc, "_get_redis", lambda: None)

    _mk_activity(db_session, requisition_id=21)
    _mk_activity(db_session, requisition_id=21)
    await svc.get_or_build_digest(DigestEntityType.REQUISITION, 21, db_session)
    assert calls["n"] == 1
    # expire the cooldown and grow the timeline
    row = db_session.query(ActivityDigest).filter_by(entity_id=21).first()
    row.cooldown_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()
    _mk_activity(db_session, requisition_id=21)
    await svc.get_or_build_digest(DigestEntityType.REQUISITION, 21, db_session)
    assert calls["n"] == 2  # basis changed → regenerated
    row2 = db_session.query(ActivityDigest).filter_by(entity_id=21).first()
    assert row2.basis_activity_count == 3


@pytest.mark.asyncio
async def test_lock_miss_with_existing_serves_stale(db_session, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.constants import DigestEntityType
    from app.models.intelligence import ActivityDigest
    from app.services import activity_digest_service as svc

    calls = {"n": 0}

    async def fake_cs(*a, **k):
        calls["n"] += 1
        return {"headline": "first", "narrative": "n", "highlights": [], "status_signal": "on_track"}

    monkeypatch.setattr("app.utils.claude_client.claude_structured", fake_cs)
    # first build with no lock contention
    monkeypatch.setattr(svc, "_get_redis", lambda: None)
    _mk_activity(db_session, requisition_id=22)
    _mk_activity(db_session, requisition_id=22)
    await svc.get_or_build_digest(DigestEntityType.REQUISITION, 22, db_session)

    # now expire cooldown, grow timeline, and make the lock unavailable
    row = db_session.query(ActivityDigest).filter_by(entity_id=22).first()
    row.cooldown_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()
    _mk_activity(db_session, requisition_id=22)

    monkeypatch.setattr(svc, "_get_redis", lambda: _LockHeldRedis())

    out = await svc.get_or_build_digest(DigestEntityType.REQUISITION, 22, db_session)
    assert out["state"] == "ready"
    assert out["headline"] == "first"  # served the stale cached row
    assert calls["n"] == 1  # no new AI call under lock contention


@pytest.mark.asyncio
async def test_company_path_filters_non_meaningful(db_session, monkeypatch):
    from app.constants import DigestEntityType
    from app.models import Company
    from app.models.intelligence import ActivityLog
    from app.services import activity_digest_service as svc

    captured = {"prompt": None}

    async def fake_cs(prompt, **k):
        captured["prompt"] = prompt
        return {"headline": "h", "narrative": "n", "highlights": [], "status_signal": "on_track"}

    monkeypatch.setattr("app.utils.claude_client.claude_structured", fake_cs)
    monkeypatch.setattr(svc, "_get_redis", lambda: None)

    c = Company(name="Acme")
    db_session.add(c)
    db_session.commit()
    for meaningful, subj in [(True, "good1"), (None, "good2"), (False, "JUNK_NOISE")]:
        db_session.add(
            ActivityLog(
                activity_type="email_received",
                channel="email",
                company_id=c.id,
                subject=subj,
                is_meaningful=meaningful,
            )
        )
    db_session.commit()

    out = await svc.get_or_build_digest(DigestEntityType.COMPANY, c.id, db_session)
    assert out["state"] == "ready"
    assert "JUNK_NOISE" not in captured["prompt"]
    assert "good1" in captured["prompt"] and "good2" in captured["prompt"]
