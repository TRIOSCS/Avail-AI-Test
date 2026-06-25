"""Tests for the curated System settings tab (Task 11).

Covers the typed admin UI that replaced the raw key/value editor:
- The 4 user-facing flags render as friendly labelled controls.
- Internal watermark keys never appear as editable controls.
- The interval write rejects values < 5 with a structured 400 error.
- A boolean toggle write emits the shared showToast HX-Trigger.

Called by: pytest.
Depends on: app.main (FastAPI app), conftest `client`/`db_session` fixtures,
            app.models.config.SystemConfig.
"""

from datetime import datetime, timezone

from app.constants import UserRole
from app.models.config import SystemConfig


def _make_admin(test_user):
    """Promote the client fixture's user to admin (the system tab is admin-only)."""
    test_user.role = UserRole.ADMIN


def _seed(db_session, key, value):
    db_session.add(SystemConfig(key=key, value=value, updated_at=datetime.now(timezone.utc)))
    db_session.commit()


def test_system_renders_friendly_toggles(client, test_user):
    _make_admin(test_user)
    html = client.get("/v2/partials/settings/system").text
    assert "Email mining" in html
    assert "Proactive offer matching" in html
    assert "CRM activity tracking" in html
    assert "Inbox scan interval (minutes)" in html


def test_system_renders_help_text(client, test_user):
    _make_admin(test_user)
    html = client.get("/v2/partials/settings/system").text
    # Each control surfaces its one-line help description.
    assert "Mine connected inboxes" in html
    assert "Auto-match inbound offers" in html
    assert "How often connected inboxes are scanned" in html


def test_system_hides_watermark_rows(client, test_user, db_session):
    _make_admin(test_user)
    _seed(db_session, "teams_calls_last_poll", "2026-06-24T00:00:00+00:00")
    _seed(db_session, "8x8_last_poll", "2026-06-24T00:00:00+00:00")
    _seed(db_session, "proactive_last_scan", "2026-06-24T00:00:00+00:00")
    html = client.get("/v2/partials/settings/system").text
    # Watermark keys must not appear as editable controls (no inline write form
    # posting to their config endpoint).
    assert "/api/admin/config/teams_calls_last_poll" not in html
    assert "/api/admin/config/8x8_last_poll" not in html
    assert "/api/admin/config/proactive_last_scan" not in html


def test_system_drops_cosmetic_masking(client, test_user):
    _make_admin(test_user)
    html = client.get("/v2/partials/settings/system").text
    # The old masked-value affordance (12 asterisks) is gone.
    assert "*" * 12 not in html


def test_interval_below_min_rejected(client, test_user, db_session):
    _make_admin(test_user)
    _seed(db_session, "inbox_scan_interval_min", "30")
    resp = client.put("/api/admin/config/inbox_scan_interval_min", json={"value": "2"})
    assert resp.status_code == 400
    body = resp.json()
    assert "error" in body
    assert "at least 5 minutes" in body["error"]


def test_interval_at_min_accepted(client, test_user, db_session):
    _make_admin(test_user)
    _seed(db_session, "inbox_scan_interval_min", "30")
    resp = client.put("/api/admin/config/inbox_scan_interval_min", json={"value": "5"})
    assert resp.status_code == 200
    assert resp.json()["value"] == "5"


def test_interval_non_integer_rejected(client, test_user, db_session):
    _make_admin(test_user)
    _seed(db_session, "inbox_scan_interval_min", "30")
    resp = client.put("/api/admin/config/inbox_scan_interval_min", json={"value": "abc"})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_boolean_only_accepts_true_false(client, test_user, db_session):
    _make_admin(test_user)
    _seed(db_session, "email_mining_enabled", "false")
    resp = client.put("/api/admin/config/email_mining_enabled", json={"value": "maybe"})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_toggle_write_emits_toast(client, test_user, db_session):
    _make_admin(test_user)
    _seed(db_session, "email_mining_enabled", "false")
    resp = client.put("/api/admin/config/email_mining_enabled", json={"value": "true"})
    assert resp.status_code == 200
    assert "showToast" in resp.headers.get("HX-Trigger", "")


def test_restart_note_on_scheduler_flags(client, test_user):
    _make_admin(test_user)
    html = client.get("/v2/partials/settings/system").text
    # Scheduler-read flags carry a restart note; email mining (per-request) does not
    # need one, but the page must surface the note at least once.
    assert "Applies after the next restart" in html
