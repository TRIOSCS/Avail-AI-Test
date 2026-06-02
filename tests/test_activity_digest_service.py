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
