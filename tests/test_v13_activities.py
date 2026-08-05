"""test_v13_activities.py — Activity endpoint tests (v1.3.0)

Tests the surviving activity route in the v13_features router:
GET /api/vendors/{id}/activities

(The company/user timeline, POST /api/activities/call, and activity-status
routes were removed in the Wave-2 orphan-route cleanup.)

Called by: pytest
Depends on: conftest (client, test_user, test_vendor_card)
"""

from datetime import UTC, datetime

from app.models import ActivityLog

# ── GET /api/vendors/{id}/activities ──────────────────────────────────


def test_get_vendor_activities_returns_list(client, db_session, test_user, test_vendor_card):
    activity = ActivityLog(
        user_id=test_user.id,
        activity_type="email_sent",
        channel="email",
        vendor_card_id=test_vendor_card.id,
        contact_email="sales@arrow.com",
        subject="RFQ for LM317T",
        created_at=datetime.now(UTC),
    )
    db_session.add(activity)
    db_session.commit()

    resp = client.get(f"/api/vendors/{test_vendor_card.id}/activities")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert data["items"][0]["vendor_card_id"] == test_vendor_card.id


def test_get_vendor_activities_empty(client, test_vendor_card):
    resp = client.get(f"/api/vendors/{test_vendor_card.id}/activities")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0
