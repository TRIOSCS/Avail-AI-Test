"""Test the disconnected-mailbox banner on the requisitions list.

Tests the /v2/partials/requisitions route which uses require_user (overridden in the
test client fixture) and renders htmx/partials/requisitions/list.html.
"""


def test_requisitions_list_shows_banner_when_disconnected(client, monkeypatch):
    from app.constants import InboxSyncHealth

    monkeypatch.setattr(
        "app.services.activity_service.get_inbox_sync_status",
        lambda user: {
            "health": InboxSyncHealth.ERROR,
            "connected": False,
            "is_stale": True,
            "last_scan_at": None,
            "token_ok": False,
            "error_reason": None,
        },
    )
    resp = client.get("/v2/partials/requisitions")
    assert resp.status_code == 200
    assert "mailbox sync looks" in resp.text
    assert "disconnected" in resp.text


def test_requisitions_list_no_banner_when_healthy(client, monkeypatch):
    from app.constants import InboxSyncHealth

    monkeypatch.setattr(
        "app.services.activity_service.get_inbox_sync_status",
        lambda user: {
            "health": InboxSyncHealth.OK,
            "connected": True,
            "is_stale": False,
            "last_scan_at": None,
            "token_ok": True,
            "error_reason": None,
        },
    )
    resp = client.get("/v2/partials/requisitions")
    assert resp.status_code == 200
    # banner copy must NOT appear when healthy
    assert "mailbox sync looks" not in resp.text.lower()
