"""
test_routers_v13.py — Tests for v1.3 Feature Routes

Step 1: _activity_to_dict serializer + Activity API endpoints
Step 2-4: Sales/ownership, routing, buyer profiles (separate steps)

Covers: activity serialization, null handling, GET/POST activity endpoints
"""

from datetime import datetime, timezone
from types import SimpleNamespace

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
        "requisition_id", "dismissed_at", "created_at",
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
    resp = client.get(f"/api/users/{test_activity.user_id}/activities")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


def test_log_phone_call_no_match(client):
    """Phone number that doesn't match any known contact — still logged (unmatched queue)."""
    resp = client.post("/api/activities/call", json={
        "direction": "outbound",
        "phone": "+1-999-000-0000",
        "duration_seconds": 120,
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "logged"


def test_log_phone_call_missing_required(client):
    """Empty phone string (schema default) — still logged as unmatched."""
    resp = client.post("/api/activities/call", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "logged"


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


# ═══════════════════════════════════════════════════════════════════════
#  Activity Logging Endpoints
# ═══════════════════════════════════════════════════════════════════════


def test_log_company_call(client, test_company):
    """POST /api/companies/{id}/activities/call logs a phone call."""
    resp = client.post(
        f"/api/companies/{test_company.id}/activities/call",
        json={"phone": "+1-555-1234", "duration_seconds": 180, "notes": "Discussed pricing"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "logged"


def test_log_company_note(client, test_company):
    """POST /api/companies/{id}/activities/note logs a note."""
    resp = client.post(
        f"/api/companies/{test_company.id}/activities/note",
        json={"notes": "Met at trade show"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "logged"


def test_log_vendor_call(client, test_vendor_card):
    """POST /api/vendors/{id}/activities/call logs a vendor phone call."""
    resp = client.post(
        f"/api/vendors/{test_vendor_card.id}/activities/call",
        json={"phone": "+1-555-9876", "duration_seconds": 60},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "logged"


def test_log_vendor_note(client, test_vendor_card):
    """POST /api/vendors/{id}/activities/note logs a vendor note."""
    resp = client.post(
        f"/api/vendors/{test_vendor_card.id}/activities/note",
        json={"notes": "Confirmed availability for LM317T"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "logged"


def test_vendor_activity_status(client, test_vendor_card):
    """GET /api/vendors/{id}/activity-status returns status info."""
    resp = client.get(f"/api/vendors/{test_vendor_card.id}/activity-status")
    assert resp.status_code == 200
    data = resp.json()
    assert "vendor_card_id" in data
    assert "status" in data


def test_log_email_click(client):
    """POST /api/activities/email logs an email click event."""
    resp = client.post("/api/activities/email", json={
        "email": "vendor@example.com",
        "subject": "Re: RFQ LM317T",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "logged"


# ═══════════════════════════════════════════════════════════════════════
#  Sales / Ownership Endpoints
# ═══════════════════════════════════════════════════════════════════════


def test_my_accounts(client):
    """GET /api/sales/my-accounts returns user's owned accounts."""
    resp = client.get("/api/sales/my-accounts")
    assert resp.status_code == 200
    data = resp.json()
    assert "accounts" in data or isinstance(data, list)


def test_at_risk_accounts(client):
    """GET /api/sales/at-risk returns accounts at risk of going stale."""
    resp = client.get("/api/sales/at-risk")
    assert resp.status_code == 200
    data = resp.json()
    assert "accounts" in data or isinstance(data, list)


def test_open_pool_accounts(client):
    """GET /api/sales/open-pool returns unowned accounts."""
    resp = client.get("/api/sales/open-pool")
    assert resp.status_code == 200
    data = resp.json()
    assert "accounts" in data or isinstance(data, list)


def test_claim_account(client, db_session, test_company, test_user):
    """POST /api/sales/claim/{id} claims an unowned account (needs sales role)."""
    # claim_account requires role='sales' or 'trader'
    original_role = test_user.role
    test_user.role = "sales"
    db_session.commit()
    try:
        test_company.account_owner_id = None
        db_session.commit()
        resp = client.post(f"/api/sales/claim/{test_company.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ok") is True or data.get("status") == "claimed"
    finally:
        test_user.role = original_role
        db_session.commit()


def test_claim_account_forbidden_for_buyer(client, test_company):
    """POST /api/sales/claim/{id} returns 403 for buyer role."""
    resp = client.post(f"/api/sales/claim/{test_company.id}")
    assert resp.status_code == 403


def test_toggle_strategic(client, db_session, test_company, test_user):
    """PUT /api/companies/{id}/strategic toggles strategic flag (admin only)."""
    original_role = test_user.role
    test_user.role = "admin"
    db_session.commit()
    try:
        resp = client.put(
            f"/api/companies/{test_company.id}/strategic",
            json={"is_strategic": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ok") is True or "is_strategic" in data
    finally:
        test_user.role = original_role
        db_session.commit()


def test_toggle_strategic_forbidden_for_buyer(client, test_company):
    """PUT /api/companies/{id}/strategic returns 403 for non-admin."""
    resp = client.put(
        f"/api/companies/{test_company.id}/strategic",
        json={"is_strategic": True},
    )
    assert resp.status_code == 403


# ═══════════════════════════════════════════════════════════════════════
#  Notifications
# ═══════════════════════════════════════════════════════════════════════


def test_sales_notifications_empty(client):
    """GET /api/sales/notifications returns empty list when none exist."""
    resp = client.get("/api/sales/notifications")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 0


def test_mark_all_notifications_read(client):
    """POST /api/sales/notifications/read-all succeeds."""
    resp = client.post("/api/sales/notifications/read-all")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is True or data.get("count") is not None


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
    from unittest.mock import patch, MagicMock

    with patch("app.services.webhook_service.validate_notifications", return_value=[]):
        resp = client.post("/api/webhooks/graph", json={"value": []})
    assert resp.status_code == 403


def test_graph_webhook_processing_error(client):
    """POST /api/webhooks/graph processing failure returns 502."""
    from unittest.mock import AsyncMock, patch

    with patch("app.services.webhook_service.validate_notifications", return_value=[{"id": 1}]), \
         patch("app.services.webhook_service.handle_notification", new_callable=AsyncMock, side_effect=RuntimeError("fail")):
        resp = client.post("/api/webhooks/graph", json={"value": [{"resource": "test"}]})
    assert resp.status_code == 502


def test_graph_webhook_success(client):
    """POST /api/webhooks/graph success returns accepted."""
    from unittest.mock import AsyncMock, patch

    with patch("app.services.webhook_service.validate_notifications", return_value=[{"id": 1}]), \
         patch("app.services.webhook_service.handle_notification", new_callable=AsyncMock, return_value=None):
        resp = client.post("/api/webhooks/graph", json={"value": [{"resource": "test"}]})
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


def test_log_company_call_not_found(client):
    """POST /api/companies/99999/activities/call returns 404."""
    resp = client.post(
        "/api/companies/99999/activities/call",
        json={"phone": "+1-555-1234"},
    )
    assert resp.status_code == 404


def test_log_company_note_not_found(client):
    """POST /api/companies/99999/activities/note returns 404."""
    resp = client.post(
        "/api/companies/99999/activities/note",
        json={"notes": "test note"},
    )
    assert resp.status_code == 404


def test_log_vendor_call_not_found(client):
    """POST /api/vendors/99999/activities/call returns 404."""
    resp = client.post(
        "/api/vendors/99999/activities/call",
        json={"phone": "+1-555-1234"},
    )
    assert resp.status_code == 404


def test_log_vendor_note_not_found(client):
    """POST /api/vendors/99999/activities/note returns 404."""
    resp = client.post(
        "/api/vendors/99999/activities/note",
        json={"notes": "test note"},
    )
    assert resp.status_code == 404


def test_log_email_click_empty_email(client):
    """POST /api/activities/email with empty email returns skipped."""
    resp = client.post("/api/activities/email", json={"email": ""})
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"


def test_log_email_click_no_match(client):
    """POST /api/activities/email with unknown email returns no_match or logged."""
    resp = client.post("/api/activities/email", json={
        "email": "nobody@nonexistent.com",
    })
    assert resp.status_code == 200
    # The log_email_activity may return None (no_match) or a record (logged)
    assert resp.json()["status"] in ("logged", "no_match")


def test_vendor_activity_status_not_found(client):
    """GET /api/vendors/99999/activity-status returns 404."""
    resp = client.get("/api/vendors/99999/activity-status")
    assert resp.status_code == 404


def test_company_activity_status_not_found(client):
    """GET /api/companies/99999/activity-status returns 404."""
    resp = client.get("/api/companies/99999/activity-status")
    assert resp.status_code == 404


def test_company_activity_status_strategic(client, db_session, test_company, test_activity):
    """Strategic companies use different inactivity limit."""
    test_company.is_strategic = True
    db_session.commit()
    resp = client.get(f"/api/companies/{test_company.id}/activity-status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_strategic"] is True
    # Strategic has higher inactivity limit (90 vs 30)
    assert data["inactivity_limit"] >= 90


def test_claim_account_not_found(client, db_session, test_user):
    """POST /api/sales/claim/99999 returns 404."""
    test_user.role = "sales"
    db_session.commit()
    try:
        resp = client.post("/api/sales/claim/99999")
        assert resp.status_code == 404
    finally:
        test_user.role = "buyer"
        db_session.commit()


def test_claim_account_already_owned(client, db_session, test_company, test_user):
    """Claiming an already-owned account returns 409."""
    original_role = test_user.role
    test_user.role = "sales"
    test_company.account_owner_id = test_user.id
    db_session.commit()
    try:
        resp = client.post(f"/api/sales/claim/{test_company.id}")
        assert resp.status_code == 409
    finally:
        test_user.role = original_role
        test_company.account_owner_id = None
        db_session.commit()


def test_toggle_strategic_not_found(client, db_session, test_user):
    """PUT /api/companies/99999/strategic returns 404."""
    test_user.role = "admin"
    db_session.commit()
    try:
        resp = client.put("/api/companies/99999/strategic", json={"is_strategic": True})
        assert resp.status_code == 404
    finally:
        test_user.role = "buyer"
        db_session.commit()


def test_toggle_strategic_toggle_mode(client, db_session, test_company, test_user):
    """PUT /api/companies/{id}/strategic with None flips the current value."""
    test_user.role = "admin"
    test_company.is_strategic = False
    db_session.commit()
    try:
        resp = client.put(
            f"/api/companies/{test_company.id}/strategic",
            json={},  # is_strategic defaults to None -> flip
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_strategic"] is True

        # Flip again
        resp2 = client.put(
            f"/api/companies/{test_company.id}/strategic",
            json={},
        )
        assert resp2.status_code == 200
        assert resp2.json()["is_strategic"] is False
    finally:
        test_user.role = "buyer"
        db_session.commit()


def test_manager_digest_requires_admin(client):
    """GET /api/sales/manager-digest returns 403 for non-admin."""
    resp = client.get("/api/sales/manager-digest")
    assert resp.status_code == 403


def test_manager_digest_success(client, db_session, test_user):
    """GET /api/sales/manager-digest returns data for admin."""
    from unittest.mock import patch
    test_user.role = "admin"
    db_session.commit()
    try:
        with patch("app.services.ownership_service.get_manager_digest", return_value={"summary": []}):
            resp = client.get("/api/sales/manager-digest")
        assert resp.status_code == 200
    finally:
        test_user.role = "buyer"
        db_session.commit()


def test_sales_notifications_with_data(client, db_session, test_user):
    """GET /api/sales/notifications returns notification records."""
    from app.models import ActivityLog
    notif = ActivityLog(
        user_id=test_user.id,
        activity_type="ownership_warning",
        channel="system",
        contact_name="Acme Corp",
        subject="Account at risk",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(notif)
    db_session.commit()

    resp = client.get("/api/sales/notifications")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["type"] == "ownership_warning"


def test_mark_notification_read(client, db_session, test_user):
    """POST /api/sales/notifications/{id}/read marks it as read."""
    from app.models import ActivityLog
    notif = ActivityLog(
        user_id=test_user.id,
        activity_type="buyplan_pending",
        channel="system",
        subject="Buy plan awaiting",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(notif)
    db_session.commit()

    resp = client.post(f"/api/sales/notifications/{notif.id}/read")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_mark_notification_read_not_found(client):
    """POST /api/sales/notifications/99999/read returns 404."""
    resp = client.post("/api/sales/notifications/99999/read")
    assert resp.status_code == 404


def test_mark_notification_read_wrong_user(client, db_session, test_user):
    """Cannot mark another user's notification as read."""
    from app.models import ActivityLog, User
    other = User(
        email="other_notif@trioscs.com", name="Other Notif", role="buyer",
        azure_id="az-other-notif", created_at=datetime.now(timezone.utc),
    )
    db_session.add(other)
    db_session.flush()

    notif = ActivityLog(
        user_id=other.id,
        activity_type="ownership_warning",
        channel="system",
        subject="Not yours",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(notif)
    db_session.commit()

    resp = client.post(f"/api/sales/notifications/{notif.id}/read")
    assert resp.status_code == 404


def test_unmatched_activities_admin_required(client):
    """GET /api/activities/unmatched requires admin."""
    resp = client.get("/api/activities/unmatched")
    assert resp.status_code in (401, 403)


def test_attribute_activity_company(client, db_session, test_user, test_company):
    """POST /api/activities/{id}/attribute attributes to company."""
    from unittest.mock import patch
    from app.models import ActivityLog

    test_user.role = "admin"
    db_session.commit()

    act = ActivityLog(
        user_id=test_user.id,
        activity_type="email_sent",
        channel="email",
        contact_email="unknown@example.com",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(act)
    db_session.commit()

    try:
        with patch("app.services.activity_service.attribute_activity", return_value=act):
            from app.dependencies import require_admin
            from app.main import app
            app.dependency_overrides[require_admin] = lambda: test_user
            try:
                resp = client.post(
                    f"/api/activities/{act.id}/attribute",
                    json={"entity_type": "company", "entity_id": test_company.id},
                )
                assert resp.status_code == 200
                assert resp.json()["status"] == "attributed"
            finally:
                app.dependency_overrides.pop(require_admin, None)
    finally:
        test_user.role = "buyer"
        db_session.commit()


def test_attribute_activity_vendor_not_found(client, db_session, test_user):
    """POST /api/activities/{id}/attribute for non-existent vendor returns 404."""
    test_user.role = "admin"
    db_session.commit()
    try:
        from app.dependencies import require_admin
        from app.main import app
        app.dependency_overrides[require_admin] = lambda: test_user
        try:
            resp = client.post(
                "/api/activities/1/attribute",
                json={"entity_type": "vendor", "entity_id": 99999},
            )
            assert resp.status_code == 404
        finally:
            app.dependency_overrides.pop(require_admin, None)
    finally:
        test_user.role = "buyer"
        db_session.commit()


def test_dismiss_activity_not_found(client, db_session, test_user):
    """POST /api/activities/99999/dismiss returns 404."""
    from unittest.mock import patch
    test_user.role = "admin"
    db_session.commit()
    try:
        from app.dependencies import require_admin
        from app.main import app
        app.dependency_overrides[require_admin] = lambda: test_user
        try:
            with patch("app.services.activity_service.dismiss_activity", return_value=None):
                resp = client.post("/api/activities/99999/dismiss")
            assert resp.status_code == 404
        finally:
            app.dependency_overrides.pop(require_admin, None)
    finally:
        test_user.role = "buyer"
        db_session.commit()


def test_vendor_activity_status_with_recent_activity(client, db_session, test_user, test_vendor_card):
    """Vendor with recent activity shows green status."""
    from app.models import ActivityLog
    act = ActivityLog(
        user_id=test_user.id,
        activity_type="email_sent",
        channel="email",
        vendor_card_id=test_vendor_card.id,
        contact_email="sales@arrow.com",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(act)
    db_session.commit()

    resp = client.get(f"/api/vendors/{test_vendor_card.id}/activity-status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "green"
    assert data["days_since_activity"] is not None
    assert data["days_since_activity"] <= 1


def test_log_vendor_call_with_requisition(client, test_vendor_card, test_requisition):
    """Log vendor call with requisition_id links activity."""
    resp = client.post(
        f"/api/vendors/{test_vendor_card.id}/activities/call",
        json={
            "phone": "+1-555-9876",
            "duration_seconds": 60,
            "requisition_id": test_requisition.id,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "logged"


def test_log_vendor_note_with_contact_id(client, test_vendor_card, test_vendor_contact):
    """Log vendor note with vendor_contact_id."""
    resp = client.post(
        f"/api/vendors/{test_vendor_card.id}/activities/note",
        json={
            "notes": "Follow up on quote",
            "vendor_contact_id": test_vendor_contact.id,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "logged"
