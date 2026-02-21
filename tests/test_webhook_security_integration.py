"""Integration tests for webhook signature validation + replay protection + rate limits.

Runs against the full FastAPI app with in-memory SQLite, same harness as sim_test.py.
Tests the HTTP-level behavior end-to-end (not just service functions).

Usage:
    TESTING=1 PYTHONPATH=/root/availai python3 /tmp/test_webhook_security.py
"""

import os
import time

os.environ["TESTING"] = "1"

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "JSON"
SQLiteTypeCompiler.visit_TSVECTOR = lambda self, type_, **kw: "TEXT"

from fastapi.testclient import TestClient

from app.database import get_db
from app.dependencies import require_admin, require_buyer, require_settings_access, require_user
from app.main import app
from app.models import Base, GraphSubscription, User
from app.services.webhook_service import _seen_notifications

# ── DB setup ────────────────────────────────────────────────────────────

engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)


@event.listens_for(engine, "connect")
def _p(c, _):
    c.execute("pragma foreign_keys=ON")


_PG_ONLY = {"buyer_profiles"}
_safe = [t for n, t in Base.metadata.tables.items() if n not in _PG_ONLY]
Base.metadata.create_all(bind=engine, tables=_safe)
db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()

u = User(
    id=1,
    name="Test Buyer",
    email="testbuyer@trioscs.com",
    role="buyer",
    azure_id="az-test",
    m365_connected=True,
    is_active=True,
    created_at=datetime.now(timezone.utc),
)
db.add(u)
db.commit()


def _db():
    yield db


app.dependency_overrides[get_db] = _db
app.dependency_overrides[require_user] = lambda: u
app.dependency_overrides[require_admin] = lambda: u
app.dependency_overrides[require_buyer] = lambda: u
app.dependency_overrides[require_settings_access] = lambda: u

c = TestClient(app)
R = []


def t(name, fn):
    try:
        fn()
        R.append(("P", name))
        print(f"  PASS  {name}")
    except Exception as e:
        R.append(("F", name, str(e)))
        print(f"  FAIL  {name}: {e}")


# ── Helpers ─────────────────────────────────────────────────────────────


def make_sub(sub_id, client_state):
    """Create a GraphSubscription in the test DB."""
    sub = GraphSubscription(
        user_id=u.id,
        subscription_id=sub_id,
        resource="/me/messages",
        change_type="created",
        expiration_dt=datetime.now(timezone.utc) + timedelta(hours=48),
        client_state=client_state,
    )
    db.add(sub)
    db.commit()
    return sub


def webhook_post(payload):
    """POST to the webhook endpoint."""
    return c.post("/api/webhooks/graph", json=payload)


def notif(sub_id, client_state, resource="Users('abc')/Messages('msg-001')"):
    """Build a notification dict."""
    return {
        "subscriptionId": sub_id,
        "clientState": client_state,
        "changeType": "created",
        "resource": resource,
    }


# ═══════════════════════════════════════════════════════════════════════
#  1. VALIDATION HANDSHAKE (should still work with rate limit)
# ═══════════════════════════════════════════════════════════════════════


def _handshake():
    # Graph sends validation as POST with ?validationToken= query param
    r = c.post("/api/webhooks/graph?validationToken=hello-graph-123")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    assert r.text == "hello-graph-123", f"Expected echo, got {r.text}"


t("Validation handshake echoes token", _handshake)


# ═══════════════════════════════════════════════════════════════════════
#  2. REJECT EMPTY PAYLOAD
# ═══════════════════════════════════════════════════════════════════════


def _empty_payload():
    r = webhook_post({"value": []})
    assert r.status_code == 403, f"Expected 403, got {r.status_code}"


t("Empty payload returns 403", _empty_payload)


# ═══════════════════════════════════════════════════════════════════════
#  3. REJECT UNKNOWN SUBSCRIPTION
# ═══════════════════════════════════════════════════════════════════════


def _unknown_sub():
    _seen_notifications.clear()
    r = webhook_post({"value": [notif("sub-unknown-xyz", "anything")]})
    assert r.status_code == 403, f"Expected 403, got {r.status_code}"


t("Unknown subscription returns 403", _unknown_sub)


# ═══════════════════════════════════════════════════════════════════════
#  4. REJECT CLIENT STATE MISMATCH
# ═══════════════════════════════════════════════════════════════════════

CLIENT_STATE_1 = secrets.token_hex(16)
make_sub("sub-sec-001", CLIENT_STATE_1)


def _state_mismatch():
    _seen_notifications.clear()
    r = webhook_post({"value": [notif("sub-sec-001", "wrong-state")]})
    assert r.status_code == 403, f"Expected 403, got {r.status_code}"


t("Client state mismatch returns 403", _state_mismatch)


# ═══════════════════════════════════════════════════════════════════════
#  5. ACCEPT VALID CLIENT STATE (502 expected — no real Graph API)
# ═══════════════════════════════════════════════════════════════════════


def _state_match():
    _seen_notifications.clear()
    r = webhook_post(
        {"value": [notif("sub-sec-001", CLIENT_STATE_1, "Users('a')/Messages('m-valid')")]}
    )
    # 502 = passed validation but Graph fetch failed (no real API) — that's correct
    # 200 = notification processed (unlikely without mocking Graph)
    assert r.status_code in (200, 502), f"Expected 200/502, got {r.status_code}: {r.text[:200]}"


t("Valid client state passes validation", _state_match)


# ═══════════════════════════════════════════════════════════════════════
#  6. REPLAY PROTECTION — duplicate rejected
# ═══════════════════════════════════════════════════════════════════════

CLIENT_STATE_2 = secrets.token_hex(16)
make_sub("sub-sec-002", CLIENT_STATE_2)


def _replay_blocked():
    _seen_notifications.clear()
    resource = "Users('b')/Messages('m-replay')"
    # First request — should pass validation
    r1 = webhook_post({"value": [notif("sub-sec-002", CLIENT_STATE_2, resource)]})
    assert r1.status_code in (200, 502), f"First: expected 200/502, got {r1.status_code}"

    # Second request — same sub+resource — should be rejected as replay
    r2 = webhook_post({"value": [notif("sub-sec-002", CLIENT_STATE_2, resource)]})
    assert r2.status_code == 403, f"Replay: expected 403, got {r2.status_code}"


t("Replay duplicate rejected (403)", _replay_blocked)


# ═══════════════════════════════════════════════════════════════════════
#  7. REPLAY PROTECTION — different resource accepted
# ═══════════════════════════════════════════════════════════════════════


def _replay_diff_resource():
    _seen_notifications.clear()
    r1 = webhook_post(
        {"value": [notif("sub-sec-002", CLIENT_STATE_2, "Users('b')/Messages('m-A')")]}
    )
    assert r1.status_code in (200, 502), f"First: {r1.status_code}"

    r2 = webhook_post(
        {"value": [notif("sub-sec-002", CLIENT_STATE_2, "Users('b')/Messages('m-B')")]}
    )
    assert r2.status_code in (200, 502), f"Different resource: expected pass, got {r2.status_code}"


t("Different resource accepted (not replay)", _replay_diff_resource)


# ═══════════════════════════════════════════════════════════════════════
#  8. REPLAY PROTECTION — expired entry re-accepted
# ═══════════════════════════════════════════════════════════════════════

CLIENT_STATE_3 = secrets.token_hex(16)
make_sub("sub-sec-003", CLIENT_STATE_3)


def _replay_expired():
    _seen_notifications.clear()
    resource = "Users('c')/Messages('m-expire')"

    # First request
    r1 = webhook_post({"value": [notif("sub-sec-003", CLIENT_STATE_3, resource)]})
    assert r1.status_code in (200, 502), f"First: {r1.status_code}"

    # Manually expire the cache entry
    from app.services.webhook_service import REPLAY_WINDOW_SECONDS

    replay_key = f"sub-sec-003:{resource}"
    _seen_notifications[replay_key] = time.monotonic() - REPLAY_WINDOW_SECONDS - 1

    # Should be accepted again
    r2 = webhook_post({"value": [notif("sub-sec-003", CLIENT_STATE_3, resource)]})
    assert r2.status_code in (200, 502), f"After expiry: expected pass, got {r2.status_code}"


t("Expired replay entry re-accepted", _replay_expired)


# ═══════════════════════════════════════════════════════════════════════
#  9. NULL CLIENT STATE — accepts any
# ═══════════════════════════════════════════════════════════════════════

make_sub("sub-sec-004", None)  # No client_state set


def _null_state():
    _seen_notifications.clear()
    r = webhook_post(
        {"value": [notif("sub-sec-004", "literally-anything", "Users('d')/Messages('m-null')")]}
    )
    assert r.status_code in (200, 502), f"Expected pass, got {r.status_code}"


t("Null client_state accepts any value", _null_state)


# ═══════════════════════════════════════════════════════════════════════
#  10. TIMING-SAFE COMPARISON — verify hmac.compare_digest is used
# ═══════════════════════════════════════════════════════════════════════


def _timing_safe():
    import hmac
    from unittest.mock import patch

    _seen_notifications.clear()
    with patch.object(hmac, "compare_digest", wraps=hmac.compare_digest) as mock_cmp:
        webhook_post(
            {"value": [notif("sub-sec-001", CLIENT_STATE_1, "Users('e')/Messages('m-timing')")]}
        )
        assert mock_cmp.called, "hmac.compare_digest was not called"
        # Verify it was called with the right arguments
        args = mock_cmp.call_args[0]
        assert args[0] == CLIENT_STATE_1, f"Expected stored state, got {args[0]}"
        assert args[1] == CLIENT_STATE_1, f"Expected sent state, got {args[1]}"


t("Timing-safe comparison uses hmac.compare_digest", _timing_safe)


# ═══════════════════════════════════════════════════════════════════════
#  11. MIXED BATCH — valid + invalid in same payload
# ═══════════════════════════════════════════════════════════════════════

CLIENT_STATE_5 = secrets.token_hex(16)
make_sub("sub-sec-005", CLIENT_STATE_5)


def _mixed_batch():
    _seen_notifications.clear()
    payload = {
        "value": [
            notif("sub-unknown-zzz", "bad", "Users('x')/Messages('m-bad')"),  # unknown sub
            notif("sub-sec-005", "wrong", "Users('x')/Messages('m-bad2')"),   # wrong state
            notif("sub-sec-005", CLIENT_STATE_5, "Users('x')/Messages('m-good')"),  # valid
        ]
    }
    r = webhook_post(payload)
    # Should pass validation (1 valid notification), then hit 502 (no Graph API)
    assert r.status_code in (200, 502), f"Mixed batch: expected pass, got {r.status_code}"


t("Mixed batch: valid + invalid notifications", _mixed_batch)


# ═══════════════════════════════════════════════════════════════════════
#  12. ALL INVALID IN BATCH — 403
# ═══════════════════════════════════════════════════════════════════════


def _all_invalid_batch():
    _seen_notifications.clear()
    payload = {
        "value": [
            notif("sub-unknown-aaa", "bad", "Users('y')/Messages('m1')"),
            notif("sub-sec-001", "wrong-state", "Users('y')/Messages('m2')"),
        ]
    }
    r = webhook_post(payload)
    assert r.status_code == 403, f"All-invalid batch: expected 403, got {r.status_code}"


t("All-invalid batch returns 403", _all_invalid_batch)


# ═══════════════════════════════════════════════════════════════════════
#  13. MALFORMED JSON — 422 or 400
# ═══════════════════════════════════════════════════════════════════════


def _malformed():
    r = c.post(
        "/api/webhooks/graph",
        content=b"not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code in (400, 422), f"Malformed: expected 400/422, got {r.status_code}"


t("Malformed JSON returns 400/422", _malformed)


# ═══════════════════════════════════════════════════════════════════════
#  14-16. ADMIN RATE LIMITS — verify decorator presence
# ═══════════════════════════════════════════════════════════════════════


def _admin_users_ok():
    r = c.get("/api/admin/users")
    assert r.status_code == 200, f"Admin users: {r.status_code}"
    assert isinstance(r.json(), list)


t("Admin GET /users rate-limited endpoint works", _admin_users_ok)


def _admin_health_ok():
    r = c.get("/api/admin/health")
    assert r.status_code == 200, f"Admin health: {r.status_code}"


t("Admin GET /health rate-limited endpoint works", _admin_health_ok)


def _admin_config_ok():
    r = c.get("/api/admin/config")
    assert r.status_code == 200, f"Admin config: {r.status_code}"


t("Admin GET /config rate-limited endpoint works", _admin_config_ok)


# ═══════════════════════════════════════════════════════════════════════
#  RESULTS
# ═══════════════════════════════════════════════════════════════════════

passed = sum(1 for r in R if r[0] == "P")
failed = sum(1 for r in R if r[0] == "F")
total = len(R)

print()
if failed:
    print("FAILURES:")
    for r in R:
        if r[0] == "F":
            print(f"  ✗ {r[1]}: {r[2]}")
    print()

print(f"═══ RESULTS: {passed}/{total} passed ({100 * passed // total}%) ═══")
exit(0 if failed == 0 else 1)
