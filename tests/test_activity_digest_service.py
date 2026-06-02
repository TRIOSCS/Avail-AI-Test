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
