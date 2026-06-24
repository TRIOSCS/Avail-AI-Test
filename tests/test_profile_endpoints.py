# tests/test_profile_endpoints.py
# What: tests for the profile mutation endpoints (display name + 8x8 extension,
#       buy-plan email and new-offer-alert notification toggles).
# Called by: pytest.
# Depends on: app.routers.htmx_views endpoints POST /api/user/profile,
#             /api/user/toggle-buyplan-email, /api/user/toggle-new-offer-alert;
#             the `client`, `db_session`, `test_user` fixtures from conftest.
import json


def _toast(resp):
    """Parse the showToast payload from an HX-Trigger header."""
    return json.loads(resp.headers["HX-Trigger"])["showToast"]


def test_update_display_name(client, db_session, test_user):
    resp = client.post("/api/user/profile", data={"name": "New Name", "extension": "1234"})
    assert resp.status_code == 200
    db_session.refresh(test_user)
    assert test_user.name == "New Name"
    assert test_user.eight_by_eight_extension == "1234"
    assert _toast(resp)["message"] == "Profile updated."


def test_profile_trims_whitespace(client, db_session, test_user):
    resp = client.post("/api/user/profile", data={"name": "  Padded  ", "extension": "  77  "})
    assert resp.status_code == 200
    db_session.refresh(test_user)
    assert test_user.name == "Padded"
    assert test_user.eight_by_eight_extension == "77"


def test_blank_extension_clears_it(client, db_session, test_user):
    test_user.eight_by_eight_extension = "999"
    db_session.commit()
    resp = client.post("/api/user/profile", data={"name": "Keeper", "extension": ""})
    assert resp.status_code == 200
    db_session.refresh(test_user)
    assert test_user.eight_by_eight_extension == ""


def test_blank_name_rejected(client, db_session, test_user):
    resp = client.post("/api/user/profile", data={"name": "  ", "extension": ""})
    assert resp.status_code == 400
    assert "error" in resp.json()
    db_session.refresh(test_user)
    assert test_user.name == "Test Buyer"  # unchanged


def test_overlong_name_rejected(client):
    resp = client.post("/api/user/profile", data={"name": "x" * 256, "extension": ""})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_overlong_extension_rejected(client):
    resp = client.post("/api/user/profile", data={"name": "Fine", "extension": "1" * 21})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_toggle_buyplan_email_off(client, db_session, test_user):
    resp = client.post("/api/user/toggle-buyplan-email")
    assert resp.status_code == 200
    db_session.refresh(test_user)
    assert test_user.notify_buyplan_email_enabled is False  # default True -> toggled off
    assert _toast(resp)["message"] == "Buy-plan email notifications disabled."


def test_toggle_buyplan_email_on(client, db_session, test_user):
    test_user.notify_buyplan_email_enabled = False
    db_session.commit()
    resp = client.post("/api/user/toggle-buyplan-email")
    assert resp.status_code == 200
    db_session.refresh(test_user)
    assert test_user.notify_buyplan_email_enabled is True
    assert _toast(resp)["message"] == "Buy-plan email notifications enabled."


def test_toggle_new_offer_alert_off(client, db_session, test_user):
    resp = client.post("/api/user/toggle-new-offer-alert")
    assert resp.status_code == 200
    db_session.refresh(test_user)
    assert test_user.notify_new_offer_alert_enabled is False
    assert _toast(resp)["message"] == "New-offer alerts disabled."


def test_toggle_new_offer_alert_on(client, db_session, test_user):
    test_user.notify_new_offer_alert_enabled = False
    db_session.commit()
    resp = client.post("/api/user/toggle-new-offer-alert")
    assert resp.status_code == 200
    db_session.refresh(test_user)
    assert test_user.notify_new_offer_alert_enabled is True
    assert _toast(resp)["message"] == "New-offer alerts enabled."
