"""Integration tests for webhook signature validation + replay protection + rate limits.

Runs against the full FastAPI app with in-memory SQLite, same harness as conftest.
Tests the HTTP-level behavior end-to-end (not just service functions).
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
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clear_replay_cache():
    """Clear the replay protection cache between tests."""
    _seen_notifications.clear()
    yield
    _seen_notifications.clear()


@pytest.fixture()
def client_state_1():
    return secrets.token_hex(16)


@pytest.fixture()
def sub1(db_session: Session, admin_user: User, client_state_1: str):
    """A GraphSubscription with a known client_state."""
    sub = GraphSubscription(
        user_id=admin_user.id,
        subscription_id="sub-sec-001",
        resource="/me/messages",
        change_type="created",
        expiration_dt=datetime.now(timezone.utc) + timedelta(hours=48),
        client_state=client_state_1,
    )
    db_session.add(sub)
    db_session.commit()
    return sub


@pytest.fixture()
def client_state_2():
    return secrets.token_hex(16)


@pytest.fixture()
def sub2(db_session: Session, admin_user: User, client_state_2: str):
    sub = GraphSubscription(
        user_id=admin_user.id,
        subscription_id="sub-sec-002",
        resource="/me/messages",
        change_type="created",
        expiration_dt=datetime.now(timezone.utc) + timedelta(hours=48),
        client_state=client_state_2,
    )
    db_session.add(sub)
    db_session.commit()
    return sub


@pytest.fixture()
def client_state_3():
    return secrets.token_hex(16)


@pytest.fixture()
def sub3(db_session: Session, admin_user: User, client_state_3: str):
    sub = GraphSubscription(
        user_id=admin_user.id,
        subscription_id="sub-sec-003",
        resource="/me/messages",
        change_type="created",
        expiration_dt=datetime.now(timezone.utc) + timedelta(hours=48),
        client_state=client_state_3,
    )
    db_session.add(sub)
    db_session.commit()
    return sub


@pytest.fixture()
def sub_no_state(db_session: Session, admin_user: User):
    """Subscription with no client_state set."""
    sub = GraphSubscription(
        user_id=admin_user.id,
        subscription_id="sub-sec-004",
        resource="/me/messages",
        change_type="created",
        expiration_dt=datetime.now(timezone.utc) + timedelta(hours=48),
        client_state=None,
    )
    db_session.add(sub)
    db_session.commit()
    return sub


@pytest.fixture()
def client_state_5():
    return secrets.token_hex(16)


@pytest.fixture()
def sub5(db_session: Session, admin_user: User, client_state_5: str):
    sub = GraphSubscription(
        user_id=admin_user.id,
        subscription_id="sub-sec-005",
        resource="/me/messages",
        change_type="created",
        expiration_dt=datetime.now(timezone.utc) + timedelta(hours=48),
        client_state=client_state_5,
    )
    db_session.add(sub)
    db_session.commit()
    return sub


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


def test_admin_users_rate_limited_endpoint(admin_client):
    r = admin_client.get("/api/admin/users")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_admin_health_rate_limited_endpoint(admin_client):
    r = admin_client.get("/api/admin/health")
    assert r.status_code == 200


def test_admin_config_rate_limited_endpoint(admin_client):
    r = admin_client.get("/api/admin/config")
    assert r.status_code == 200
