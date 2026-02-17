"""
test_v13_activities.py — Activity endpoint tests (v1.3.0)

Tests the 5 activity-related routes in v13_features router:
GET /api/companies/{id}/activities
GET /api/vendors/{id}/activities
GET /api/users/{id}/activities
POST /api/activities/call
GET /api/companies/{id}/activity-status

Called by: pytest
Depends on: conftest (client, test_user, test_company, test_vendor_card, test_activity)
"""

from datetime import datetime, timedelta, timezone

from app.models import ActivityLog


# ── GET /api/companies/{id}/activities ────────────────────────────────

def test_get_company_activities_returns_list(client, test_activity):
    resp = client.get(f"/api/companies/{test_activity.company_id}/activities")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    assert data[0]["activity_type"] == "email_sent"
    assert data[0]["contact_email"] == "vendor@example.com"


def test_get_company_activities_empty(client, test_company):
    """Company with no activities returns empty list, not 404."""
    resp = client.get(f"/api/companies/{test_company.id}/activities")
    assert resp.status_code == 200
    assert resp.json() == []


# ── GET /api/vendors/{id}/activities ──────────────────────────────────

def test_get_vendor_activities_returns_list(client, db_session, test_user, test_vendor_card):
    activity = ActivityLog(
        user_id=test_user.id,
        activity_type="email_sent",
        channel="email",
        vendor_card_id=test_vendor_card.id,
        contact_email="sales@arrow.com",
        subject="RFQ for LM317T",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(activity)
    db_session.commit()

    resp = client.get(f"/api/vendors/{test_vendor_card.id}/activities")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["vendor_card_id"] == test_vendor_card.id


def test_get_vendor_activities_empty(client, test_vendor_card):
    resp = client.get(f"/api/vendors/{test_vendor_card.id}/activities")
    assert resp.status_code == 200
    assert resp.json() == []


# ── GET /api/users/{id}/activities ────────────────────────────────────

def test_get_user_activities_returns_list(client, test_user, test_activity):
    resp = client.get(f"/api/users/{test_user.id}/activities")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["user_id"] == test_user.id


def test_get_user_activities_other_user_empty(client, sales_user):
    """Querying a user with no activities returns empty list."""
    resp = client.get(f"/api/users/{sales_user.id}/activities")
    assert resp.status_code == 200
    assert resp.json() == []


# ── POST /api/activities/call ─────────────────────────────────────────

def test_log_phone_call_no_match(client):
    """Phone number that doesn't match any contact — still logged (unmatched queue)."""
    resp = client.post("/api/activities/call", json={
        "direction": "outbound",
        "phone": "+1-555-999-0000",
        "duration_seconds": 120,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "logged"


def test_log_phone_call_invalid_direction(client):
    """Invalid direction value triggers validation error."""
    resp = client.post("/api/activities/call", json={
        "direction": "sideways",
        "phone": "+1-555-0100",
    })
    assert resp.status_code == 422


# ── GET /api/companies/{id}/activity-status ───────────────────────────

def test_activity_status_with_recent_activity(client, test_activity):
    """Company with very recent activity should show green status."""
    resp = client.get(f"/api/companies/{test_activity.company_id}/activity-status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "green"
    assert data["days_since_activity"] == 0
    assert data["inactivity_limit"] == 30  # non-strategic default


def test_activity_status_no_activity(client, test_company):
    """Company with zero activity returns no_activity status."""
    resp = client.get(f"/api/companies/{test_company.id}/activity-status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "no_activity"
    assert data["days_since_activity"] is None


def test_activity_status_strategic_company(client, db_session, test_user, test_company):
    """Strategic company uses 90-day inactivity limit."""
    test_company.is_strategic = True
    db_session.commit()

    # Add an old activity (25 days ago — green for strategic, yellow for standard)
    old = ActivityLog(
        user_id=test_user.id, activity_type="email_sent", channel="email",
        company_id=test_company.id, contact_email="x@example.com",
        created_at=datetime.now(timezone.utc) - timedelta(days=25),
    )
    db_session.add(old)
    db_session.commit()

    resp = client.get(f"/api/companies/{test_company.id}/activity-status")
    data = resp.json()
    assert data["is_strategic"] is True
    assert data["inactivity_limit"] == 90
    assert data["status"] == "yellow"  # day 25 > warning_days (23) but < 90


def test_activity_status_nonexistent_company(client):
    resp = client.get("/api/companies/99999/activity-status")
    assert resp.status_code == 404
