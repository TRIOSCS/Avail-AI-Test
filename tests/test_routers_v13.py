"""test_routers_v13.py — Tests for v1.3 Feature Routes.

Covers: _activity_to_dict serialization + null handling, the surviving
GET /api/vendors/{id}/activities timeline, and the Graph webhook endpoint.

(The company/user timeline, manual call/note/email logging, unmatched
queue, and activity-status routes were removed in the Wave-2
orphan-route cleanup.)
"""

from datetime import UTC, datetime
from types import SimpleNamespace

# ═══════════════════════════════════════════════════════════════════════
#  _activity_to_dict unit tests (existing)
# ═══════════════════════════════════════════════════════════════════════


def _make_activity(**overrides):
    """Build a fake ActivityLog with sensible defaults."""
    defaults = dict(
        id=1,
        user_id=10,
        user=SimpleNamespace(name="Test Buyer"),
        activity_type="email_sent",
        channel="graph_api",
        company_id=5,
        vendor_card_id=3,
        contact_email="vendor@acme.com",
        contact_phone="+1-555-0100",
        contact_name="Jane Vendor",
        subject="RFQ for LM317T",
        duration_seconds=None,
        created_at=datetime(2026, 2, 14, 12, 0, 0, tzinfo=UTC),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_activity_to_dict_full():
    from app.routers.v13_features import _activity_to_dict

    d = _activity_to_dict(_make_activity())
    assert d["id"] == 1
    assert d["user_name"] == "Test Buyer"
    assert d["created_at"] == "2026-02-14T12:00:00+00:00"


def test_activity_to_dict_null_user():
    from app.routers.v13_features import _activity_to_dict

    assert _activity_to_dict(_make_activity(user=None))["user_name"] is None


def test_activity_to_dict_null_created_at():
    from app.routers.v13_features import _activity_to_dict

    assert _activity_to_dict(_make_activity(created_at=None))["created_at"] is None


def test_activity_to_dict_phone_call():
    from app.routers.v13_features import _activity_to_dict

    d = _activity_to_dict(
        _make_activity(
            activity_type="phone_call",
            channel="8x8",
            duration_seconds=342,
        )
    )
    assert d["duration_seconds"] == 342


def test_activity_to_dict_includes_all_keys():
    from app.routers.v13_features import _activity_to_dict

    expected = {
        "id",
        "user_id",
        "user_name",
        "activity_type",
        "channel",
        "company_id",
        "vendor_card_id",
        "vendor_contact_id",
        "site_contact_id",
        "contact_email",
        "contact_phone",
        "contact_name",
        "subject",
        "notes",
        "duration_seconds",
        "requisition_id",
        "direction",
        "event_type",
        "summary",
        "source_url",
        "dismissed_at",
        "created_at",
    }
    assert set(_activity_to_dict(_make_activity()).keys()) == expected


# ═══════════════════════════════════════════════════════════════════════
#  Activity endpoint integration tests (Step 1)
# ═══════════════════════════════════════════════════════════════════════


def test_get_vendor_activities_empty(client, test_vendor_card):
    resp = client.get(f"/api/vendors/{test_vendor_card.id}/activities")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


def test_get_vendor_activities_with_data(client, db_session, test_user, test_vendor_card):
    from app.models import ActivityLog

    act = ActivityLog(
        user_id=test_user.id,
        activity_type="email_sent",
        channel="email",
        vendor_card_id=test_vendor_card.id,
        contact_email="sales@arrow.com",
        subject="RFQ for TPS65150",
        created_at=datetime.now(UTC),
    )
    db_session.add(act)
    db_session.commit()
    resp = client.get(f"/api/vendors/{test_vendor_card.id}/activities")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["vendor_card_id"] == test_vendor_card.id


# ═══════════════════════════════════════════════════════════════════════
#  Additional Coverage Tests
# ═══════════════════════════════════════════════════════════════════════


def test_graph_webhook_validation_token(client):
    """POST /api/webhooks/graph with validationToken returns plain text."""
    resp = client.post("/api/webhooks/graph?validationToken=test-token-xyz")
    assert resp.status_code == 200
    assert resp.text == "test-token-xyz"


def test_graph_webhook_invalid_json(client):
    """POST /api/webhooks/graph with invalid JSON body returns 400."""
    resp = client.post(
        "/api/webhooks/graph",
        content="not valid json!!!",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400


def test_graph_webhook_no_valid_notifications(client):
    """POST /api/webhooks/graph with empty value list returns 403."""
    from unittest.mock import patch

    with patch("app.services.webhook_service.validate_notifications", return_value=[]):
        resp = client.post("/api/webhooks/graph", json={"value": []})
    assert resp.status_code == 403


def test_graph_webhook_processing_error(client):
    """POST /api/webhooks/graph processing failure returns 500 so Microsoft Graph
    retries."""
    from unittest.mock import AsyncMock, patch

    with (
        patch("app.services.webhook_service.validate_notifications", return_value=[{"id": 1}]),
        patch(
            "app.services.webhook_service.handle_notification", new_callable=AsyncMock, side_effect=RuntimeError("fail")
        ),
    ):
        resp = client.post("/api/webhooks/graph", json={"value": [{"resource": "test"}]})
    assert resp.status_code == 500


def test_graph_webhook_success(client):
    """POST /api/webhooks/graph success returns accepted."""
    from unittest.mock import AsyncMock, patch

    with (
        patch("app.services.webhook_service.validate_notifications", return_value=[{"id": 1}]),
        patch("app.services.webhook_service.handle_notification", new_callable=AsyncMock, return_value=None),
    ):
        resp = client.post("/api/webhooks/graph", json={"value": [{"resource": "test"}]})
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
