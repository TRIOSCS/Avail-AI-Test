"""Tests for accurate, graceful mailbox-sync status (the 'snag' fix).

Covers app/services/m365_status.py (error classification + actionable copy) and
the parts of app/services/activity_service.get_inbox_sync_status that surface it.

Regression guards for the original bug:
  - a raw exception string was pasted into a generic red "snag" banner
  - the banner told the user to reconnect even for transient/server-side errors
  - the banner was sticky: a self-healed error kept showing because nothing
    cleared User.m365_error_reason on a successful scan
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.constants import InboxSyncHealth
from app.services.activity_service import get_inbox_sync_status
from app.services.m365_status import (
    REASON_AUTH,
    REASON_SUBSCRIPTION,
    REASON_TRANSIENT,
    M365ErrorAction,
    M365ErrorCategory,
    action_for_reason,
    classify_m365_error,
    reason_for,
)


def _user(**kw):
    base = dict(
        m365_connected=True,
        last_inbox_scan=datetime.now(timezone.utc),
        token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        access_token="t",
        m365_error_reason=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── classify_m365_error ─────────────────────────────────────────────────


def test_auth_signals_classify_as_auth():
    for raw in ["invalid_grant", "Token refresh failed", "AADSTS700082", "401 Unauthorized", "token expired"]:
        assert classify_m365_error(raw) == M365ErrorCategory.AUTH, raw


def test_non_auth_errors_classify_as_transient():
    for raw in [Exception("read timeout"), "Graph API error", "503 Service Unavailable", "random parse error"]:
        assert classify_m365_error(raw) == M365ErrorCategory.TRANSIENT, raw


def test_reason_for_never_returns_raw_text():
    """The persisted reason must be a friendly sentence, never the raw error."""
    raw = "ValueError: kaboom at line 42 <object 0x7f>"
    reason = reason_for(Exception(raw))
    assert reason == REASON_TRANSIENT
    assert "kaboom" not in reason
    assert "0x7f" not in reason


def test_reason_for_auth_is_reconnect_copy():
    assert reason_for("invalid_grant") == REASON_AUTH


# ── action_for_reason (reverse map → template branch) ───────────────────


def test_action_for_known_reasons():
    assert action_for_reason(REASON_AUTH) == M365ErrorAction.RECONNECT
    assert action_for_reason(REASON_TRANSIENT) == M365ErrorAction.WAIT
    assert action_for_reason(REASON_SUBSCRIPTION) is None


def test_action_for_none_and_legacy():
    assert action_for_reason(None) is None
    assert action_for_reason("") is None
    # A stale legacy raw string must never resolve to RECONNECT.
    assert action_for_reason("Inbox scan timed out") == M365ErrorAction.WAIT


# ── get_inbox_sync_status surfaces action, degrades gracefully ──────────


def test_status_auth_error_surfaces_reconnect_action():
    s = get_inbox_sync_status(None, _user(m365_error_reason=REASON_AUTH))
    assert s["error_reason"] == REASON_AUTH
    assert s["error_action"] == "reconnect"


def test_status_transient_error_surfaces_wait_action():
    s = get_inbox_sync_status(None, _user(m365_error_reason=REASON_TRANSIENT))
    assert s["error_action"] == "wait"
    # transient ≠ "reconnect" — that was the misleading-instruction bug
    assert s["error_action"] != "reconnect"


def test_status_subscription_error_has_no_action():
    s = get_inbox_sync_status(None, _user(m365_error_reason=REASON_SUBSCRIPTION))
    assert s["error_reason"] == REASON_SUBSCRIPTION
    assert s["error_action"] is None


def test_status_no_error_has_no_action():
    s = get_inbox_sync_status(None, _user())
    assert s["error_reason"] is None
    assert s["error_action"] is None


def test_status_missing_token_is_error_with_reason():
    """No delegated token → ERROR health, and an auth reason → reconnect."""
    s = get_inbox_sync_status(None, _user(access_token=None, m365_error_reason=REASON_AUTH))
    assert s["health"] == InboxSyncHealth.ERROR
    assert s["token_ok"] is False
    assert s["error_action"] == "reconnect"


def test_status_does_not_crash_on_minimal_user():
    """A graph hiccup must not break the Profile page: missing optional attrs
    must not raise — the status still resolves."""
    minimal = SimpleNamespace()  # no m365_* attrs at all
    s = get_inbox_sync_status(None, minimal)
    assert s["connected"] is False
    assert s["health"] == InboxSyncHealth.ERROR
    assert s["error_reason"] is None
    assert s["error_action"] is None


# ── successful scan clears a self-healed error (sticky-banner fix) ───────


def _patch_successful_scan(monkeypatch):
    """Stub the inbox-scan seams so _scan_user_inbox's poll succeeds."""
    from unittest.mock import AsyncMock

    import app.jobs.email_jobs as ej

    monkeypatch.setattr("app.utils.token_manager.get_valid_token", AsyncMock(return_value="valid-token"))
    monkeypatch.setattr("app.email_service.poll_inbox", AsyncMock(return_value=[]))
    # _scan_stock_list_attachments is lazily imported from inventory_jobs;
    # the other two sub-ops are module-level names in email_jobs.
    monkeypatch.setattr("app.jobs.inventory_jobs._scan_stock_list_attachments", AsyncMock(return_value=None))
    monkeypatch.setattr(ej, "_mine_vendor_contacts", AsyncMock(return_value=None))
    monkeypatch.setattr(ej, "_scan_outbound_rfqs", AsyncMock(return_value=None))


async def test_successful_scan_clears_stale_transient_error(db_session, test_user, monkeypatch):
    """A healthy scan proves connectivity → a self-healed transient error is cleared, so
    the Settings card stops showing a resolved 'snag'."""
    from app.jobs.email_jobs import _scan_user_inbox

    test_user.access_token = "at"
    test_user.m365_connected = True
    test_user.m365_error_reason = REASON_TRANSIENT
    db_session.commit()

    _patch_successful_scan(monkeypatch)
    await _scan_user_inbox(test_user, db_session)

    db_session.refresh(test_user)
    assert test_user.m365_error_reason is None
    assert test_user.last_inbox_scan is not None


async def test_successful_scan_keeps_subscription_warning(db_session, test_user, monkeypatch):
    """A mailbox poll says nothing about Graph webhook health — the separate
    subscription warning must survive a successful scan."""
    from app.jobs.email_jobs import _scan_user_inbox

    test_user.access_token = "at"
    test_user.m365_connected = True
    test_user.m365_error_reason = REASON_SUBSCRIPTION
    db_session.commit()

    _patch_successful_scan(monkeypatch)
    await _scan_user_inbox(test_user, db_session)

    db_session.refresh(test_user)
    assert test_user.m365_error_reason == REASON_SUBSCRIPTION
