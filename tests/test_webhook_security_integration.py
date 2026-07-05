"""Integration tests for webhook signature validation + replay protection + rate limits.

Runs against the full FastAPI app with in-memory SQLite, same harness as conftest. Tests
the HTTP-level behavior end-to-end (not just service functions).
"""

import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import GraphSubscription, User
from app.rate_limit import limiter
from app.services.webhook_service import _seen_notifications

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def admin_client(db_session: Session, admin_user: User) -> TestClient:
    """TestClient with admin + buyer auth overrides."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_settings_access, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_admin():
        return admin_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_admin
    app.dependency_overrides[require_admin] = _override_admin
    app.dependency_overrides[require_buyer] = _override_admin
    app.dependency_overrides[require_settings_access] = _override_admin

    limiter.reset()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in [get_db, require_user, require_admin, require_buyer, require_settings_access]:
            app.dependency_overrides.pop(dep, None)


@pytest.fixture(autouse=True)
def _clear_replay_cache():
    """Clear the replay protection cache between tests."""
    _seen_notifications.clear()
    yield
    _seen_notifications.clear()


def _make_sub(db_session: Session, user: User, subscription_id: str, client_state: str | None):
    sub = GraphSubscription(
        user_id=user.id,
        subscription_id=subscription_id,
        resource="/me/messages",
        change_type="created",
        expiration_dt=datetime.now(timezone.utc) + timedelta(hours=48),
        client_state=client_state,
    )
    db_session.add(sub)
    db_session.commit()
    return sub


@pytest.fixture()
def client_state_1():
    return secrets.token_hex(16)


@pytest.fixture()
def sub1(db_session: Session, admin_user: User, client_state_1: str):
    """A GraphSubscription with a known client_state."""
    return _make_sub(db_session, admin_user, "sub-sec-001", client_state_1)


@pytest.fixture()
def client_state_2():
    return secrets.token_hex(16)


@pytest.fixture()
def sub2(db_session: Session, admin_user: User, client_state_2: str):
    return _make_sub(db_session, admin_user, "sub-sec-002", client_state_2)


@pytest.fixture()
def client_state_3():
    return secrets.token_hex(16)


@pytest.fixture()
def sub3(db_session: Session, admin_user: User, client_state_3: str):
    return _make_sub(db_session, admin_user, "sub-sec-003", client_state_3)


@pytest.fixture()
def sub_no_state(db_session: Session, admin_user: User):
    """Subscription with no client_state set."""
    return _make_sub(db_session, admin_user, "sub-sec-004", None)


@pytest.fixture()
def client_state_5():
    return secrets.token_hex(16)


@pytest.fixture()
def sub5(db_session: Session, admin_user: User, client_state_5: str):
    return _make_sub(db_session, admin_user, "sub-sec-005", client_state_5)


# ── Helpers ──────────────────────────────────────────────────────────


def notif(sub_id, client_state, resource="Users('abc')/Messages('msg-001')"):
    return {
        "subscriptionId": sub_id,
        "clientState": client_state,
        "changeType": "created",
        "resource": resource,
    }


def webhook_post(client, payload):
    return client.post("/api/webhooks/graph", json=payload)


# ── Tests ────────────────────────────────────────────────────────────


def test_validation_handshake(admin_client):
    """Graph sends validation as POST with ?validationToken= query param."""
    r = admin_client.post("/api/webhooks/graph?validationToken=hello-graph-123")
    assert r.status_code == 200
    assert r.text == "hello-graph-123"


def test_empty_payload_returns_403(admin_client):
    r = webhook_post(admin_client, {"value": []})
    assert r.status_code == 403


def test_unknown_subscription_returns_403(admin_client):
    r = webhook_post(admin_client, {"value": [notif("sub-unknown-xyz", "anything")]})
    assert r.status_code == 403


def test_client_state_mismatch_returns_403(admin_client, sub1):
    r = webhook_post(admin_client, {"value": [notif("sub-sec-001", "wrong-state")]})
    assert r.status_code == 403


def test_valid_client_state_passes(admin_client, sub1, client_state_1):
    r = webhook_post(
        admin_client,
        {"value": [notif("sub-sec-001", client_state_1, "Users('a')/Messages('m-valid')")]},
    )
    assert r.status_code in (200, 502)


def test_replay_duplicate_rejected(admin_client, sub2, client_state_2):
    resource = "Users('b')/Messages('m-replay')"
    r1 = webhook_post(admin_client, {"value": [notif("sub-sec-002", client_state_2, resource)]})
    assert r1.status_code in (200, 502)

    r2 = webhook_post(admin_client, {"value": [notif("sub-sec-002", client_state_2, resource)]})
    assert r2.status_code == 403


def test_different_resource_accepted(admin_client, sub2, client_state_2):
    r1 = webhook_post(
        admin_client,
        {"value": [notif("sub-sec-002", client_state_2, "Users('b')/Messages('m-A')")]},
    )
    assert r1.status_code in (200, 502)

    r2 = webhook_post(
        admin_client,
        {"value": [notif("sub-sec-002", client_state_2, "Users('b')/Messages('m-B')")]},
    )
    assert r2.status_code in (200, 502)


def test_expired_replay_entry_re_accepted(admin_client, sub3, client_state_3):
    from app.services.webhook_service import REPLAY_WINDOW_SECONDS

    resource = "Users('c')/Messages('m-expire')"
    r1 = webhook_post(admin_client, {"value": [notif("sub-sec-003", client_state_3, resource)]})
    assert r1.status_code in (200, 502)

    # Manually expire the cache entry
    replay_key = f"sub-sec-003:{resource}"
    _seen_notifications[replay_key] = time.monotonic() - REPLAY_WINDOW_SECONDS - 1

    r2 = webhook_post(admin_client, {"value": [notif("sub-sec-003", client_state_3, resource)]})
    assert r2.status_code in (200, 502)


def test_null_client_state_accepts_any(admin_client, sub_no_state):
    r = webhook_post(
        admin_client,
        {"value": [notif("sub-sec-004", "literally-anything", "Users('d')/Messages('m-null')")]},
    )
    assert r.status_code in (200, 502)


def test_timing_safe_comparison(admin_client, sub1, client_state_1):
    with patch.object(hmac, "compare_digest", wraps=hmac.compare_digest) as mock_cmp:
        webhook_post(
            admin_client,
            {"value": [notif("sub-sec-001", client_state_1, "Users('e')/Messages('m-timing')")]},
        )
        assert mock_cmp.called, "hmac.compare_digest was not called"
        args = mock_cmp.call_args[0]
        assert args[0] == client_state_1
        assert args[1] == client_state_1


def test_mixed_batch_valid_and_invalid(admin_client, sub1, sub5, client_state_5):
    payload = {
        "value": [
            notif("sub-unknown-zzz", "bad", "Users('x')/Messages('m-bad')"),
            notif("sub-sec-005", "wrong", "Users('x')/Messages('m-bad2')"),
            notif("sub-sec-005", client_state_5, "Users('x')/Messages('m-good')"),
        ]
    }
    r = webhook_post(admin_client, payload)
    assert r.status_code in (200, 502)


def test_all_invalid_batch_returns_403(admin_client, sub1):
    payload = {
        "value": [
            notif("sub-unknown-aaa", "bad", "Users('y')/Messages('m1')"),
            notif("sub-sec-001", "wrong-state", "Users('y')/Messages('m2')"),
        ]
    }
    r = webhook_post(admin_client, payload)
    assert r.status_code == 403


def test_malformed_json_returns_400_or_422(admin_client):
    r = admin_client.post(
        "/api/webhooks/graph",
        content=b"not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code in (400, 422)


def test_admin_health_rate_limited_endpoint(admin_client):
    r = admin_client.get("/api/admin/health")
    assert r.status_code == 200


def test_admin_config_rate_limited_endpoint(admin_client):
    r = admin_client.get("/api/admin/config")
    assert r.status_code == 200


# ── Validation-echo hardening (HIGH-SEC-4) ───────────────────────────


def test_validation_handshake_is_text_plain_and_nosniff(admin_client):
    """A genuine handshake echoes the raw token as bounded text/plain + nosniff."""
    r = admin_client.post("/api/webhooks/graph?validationToken=hello-graph-123")
    assert r.status_code == 200
    assert r.text == "hello-graph-123"
    assert r.headers["content-type"] == "text/plain; charset=utf-8"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_oversized_validation_token_rejected(admin_client):
    """A validationToken longer than the bound is rejected (no oversized echo)."""
    huge = "a" * 5000
    r = admin_client.post("/api/webhooks/graph", params={"validationToken": huge})
    assert r.status_code == 400
    assert huge not in r.text


def test_html_validation_token_rejected(admin_client):
    """A validationToken carrying HTML/script is rejected, not reflected."""
    payload = "<script>alert(document.cookie)</script>"
    r = admin_client.post("/api/webhooks/graph", params={"validationToken": payload})
    assert r.status_code == 400
    assert "<script>" not in r.text


def test_control_char_validation_token_rejected(admin_client):
    """A validationToken with non-printable/control characters is rejected."""
    r = admin_client.post("/api/webhooks/graph", params={"validationToken": "abc\ndef\x00"})
    assert r.status_code == 400


def test_teams_validation_handshake_is_bounded(admin_client):
    """Teams shares the same bounded echo; HTML is rejected there too."""
    with patch("app.routers.v13_features.activity.settings") as mock_settings:
        mock_settings.mvp_mode = False
        good = admin_client.post("/api/webhooks/teams?validationToken=teams-ok-123")
        bad = admin_client.post("/api/webhooks/teams", params={"validationToken": "<img src=x onerror=alert(1)>"})
    assert good.status_code == 200
    assert good.text == "teams-ok-123"
    assert good.headers["x-content-type-options"] == "nosniff"
    assert bad.status_code == 400


def test_missing_client_state_rejected(admin_client, sub1):
    """A change notification omitting clientState (real sub has one) is rejected."""
    notification = {
        "subscriptionId": "sub-sec-001",
        "changeType": "created",
        "resource": "Users('a')/Messages('m-missing-state')",
    }  # no clientState key at all
    r = webhook_post(admin_client, {"value": [notification]})
    assert r.status_code == 403


def test_empty_client_state_rejected(admin_client, sub1):
    """An empty clientState against a secret-bearing subscription is rejected."""
    r = webhook_post(admin_client, {"value": [notif("sub-sec-001", "")]})
    assert r.status_code == 403


def test_is_safe_validation_token_bounds():
    """Unit-level charset/length bounds for the validation-echo guard."""
    from app.services.webhook_service import MAX_VALIDATION_TOKEN_LEN, is_safe_validation_token

    assert is_safe_validation_token("hello-graph-123") is True
    assert is_safe_validation_token("a" * MAX_VALIDATION_TOKEN_LEN) is True
    assert is_safe_validation_token("a" * (MAX_VALIDATION_TOKEN_LEN + 1)) is False
    assert is_safe_validation_token("") is False
    assert is_safe_validation_token("<b>") is False
    assert is_safe_validation_token("line\nbreak") is False
    assert is_safe_validation_token("nul\x00byte") is False
    assert is_safe_validation_token("héllo") is False  # non-ASCII


# ── Fail-open alerting for missing stored clientState (HIGH-SEC-4) ────
#
# Every subscription created by this app stores a random per-subscription
# clientState, so a row with NO stored secret means a legacy / mis-provisioned
# subscription. We deliberately FAIL OPEN there (accept + alert) rather than
# hard-reject, so a mis-provisioned subscription can't silently drop every
# notification and break live inbox/RFQ monitoring — but the bypass must be
# LOUD (ERROR → Sentry event) so it gets noticed and re-provisioned.


def test_missing_stored_client_state_fails_open_with_error_log(admin_client, sub_no_state):
    """No stored clientState → notification still accepted (fail-open) but an ERROR-
    level log fires so Sentry captures the unauthenticated processing."""
    from loguru import logger

    captured: list[str] = []
    sink_id = logger.add(lambda m: captured.append(str(m)), level="ERROR")
    try:
        r = webhook_post(
            admin_client,
            {"value": [notif("sub-sec-004", "literally-anything", "Users('d')/Messages('m-failopen')")]},
        )
    finally:
        logger.remove(sink_id)

    assert r.status_code in (200, 502)  # accepted (fail-open), not dropped
    joined = "\n".join(captured)
    assert "sub-sec-004" in joined
    assert "WITHOUT clientState authentication" in joined


def test_valid_client_state_emits_no_fail_open_error(admin_client, sub1, client_state_1):
    """A subscription WITH a stored secret + matching clientState must NOT trip the
    fail-open error path (only genuinely unauthenticated rows should alert)."""
    from loguru import logger

    captured: list[str] = []
    sink_id = logger.add(lambda m: captured.append(str(m)), level="ERROR")
    try:
        webhook_post(
            admin_client,
            {"value": [notif("sub-sec-001", client_state_1, "Users('a')/Messages('m-ok-noerr')")]},
        )
    finally:
        logger.remove(sink_id)

    assert "WITHOUT clientState authentication" not in "\n".join(captured)
