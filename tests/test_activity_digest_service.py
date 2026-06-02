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
    assert "clean0" in body  # summary used when present
    assert "rawnotes1" in body  # notes fallback when summary None
    assert "rawnotes0" not in body  # raw notes NOT used when summary present


def test_select_system_prompt_by_entity():
    from app.constants import DigestEntityType
    from app.services import activity_digest_service as svc

    assert "sourcing" in svc._system_prompt(DigestEntityType.REQUISITION).lower()
    assert "relationship" in svc._system_prompt(DigestEntityType.COMPANY).lower()
