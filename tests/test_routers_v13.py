"""
test_routers_v13.py — Tests for v1.3 Feature Routes

Step 1: _activity_to_dict serializer + Activity API endpoints
Step 2-4: Sales/ownership, routing, buyer profiles (separate steps)

Covers: activity serialization, null handling, GET/POST activity endpoints
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


# ═══════════════════════════════════════════════════════════════════════
#  _activity_to_dict unit tests (existing)
# ═══════════════════════════════════════════════════════════════════════

def _make_activity(**overrides):
    """Build a fake ActivityLog with sensible defaults."""
    defaults = dict(
        id=1, user_id=10,
        user=SimpleNamespace(name="Test Buyer"),
        activity_type="email_sent", channel="graph_api",
        company_id=5, vendor_card_id=3,
        contact_email="vendor@acme.com", contact_phone="+1-555-0100",
        contact_name="Jane Vendor", subject="RFQ for LM317T",
        duration_seconds=None,
        created_at=datetime(2026, 2, 14, 12, 0, 0, tzinfo=timezone.utc),
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
    d = _activity_to_dict(_make_activity(
        activity_type="phone_call", channel="8x8", duration_seconds=342,
    ))
    assert d["duration_seconds"] == 342


def test_activity_to_dict_includes_all_keys():
    from app.routers.v13_features import _activity_to_dict
    expected = {
        "id", "user_id", "user_name", "activity_type", "channel",
        "company_id", "vendor_card_id", "vendor_contact_id",
        "contact_email", "contact_phone",
        "contact_name", "subject", "notes", "duration_seconds",
        "requisition_id", "created_at",
    }
    assert set(_activity_to_dict(_make_activity()).keys()) == expected


# ═══════════════════════════════════════════════════════════════════════
#  Activity endpoint integration tests (Step 1)
# ═══════════════════════════════════════════════════════════════════════

def test_get_company_activities_empty(client, test_company):
    resp = client.get(f"/api/companies/{test_company.id}/activities")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_company_activities_with_data(client, test_company, test_activity):
    resp = client.get(f"/api/companies/{test_company.id}/activities")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["activity_type"] == "email_sent"
    assert data[0]["contact_email"] == "vendor@example.com"
    assert data[0]["subject"] == "RFQ for LM317T"


def test_get_vendor_activities_empty(client, test_vendor_card):
    resp = client.get(f"/api/vendors/{test_vendor_card.id}/activities")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_vendor_activities_with_data(client, db_session, test_user, test_vendor_card):
    from app.models import ActivityLog
    act = ActivityLog(
        user_id=test_user.id, activity_type="email_sent", channel="email",
        vendor_card_id=test_vendor_card.id, contact_email="sales@arrow.com",
        subject="RFQ for TPS65150", created_at=datetime.now(timezone.utc),
    )
    db_session.add(act)
    db_session.commit()
    resp = client.get(f"/api/vendors/{test_vendor_card.id}/activities")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["vendor_card_id"] == test_vendor_card.id


def test_get_user_activities(client, test_activity):
    from app.models import User
    resp = client.get(f"/api/users/{test_activity.user_id}/activities")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


def test_log_phone_call_no_match(client):
    """Phone number that doesn't match any known contact."""
    resp = client.post("/api/activities/call", json={
        "direction": "outbound",
        "phone": "+1-999-000-0000",
        "duration_seconds": 120,
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "no_match"


def test_log_phone_call_missing_required(client):
    """Empty phone string (schema default) returns no_match."""
    resp = client.post("/api/activities/call", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "no_match"


def test_company_activity_status_no_activity(client, test_company):
    resp = client.get(f"/api/companies/{test_company.id}/activity-status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["company_id"] == test_company.id
    assert data["days_since_activity"] is None
    assert data["status"] == "no_activity"


def test_company_activity_status_with_activity(client, test_company, test_activity):
    resp = client.get(f"/api/companies/{test_company.id}/activity-status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["days_since_activity"] is not None
    assert data["days_since_activity"] >= 0
    assert data["status"] in ("green", "yellow", "red")
