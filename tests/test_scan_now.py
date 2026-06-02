"""Tests for the real scan-now endpoints (TESTING guard => no Graph)."""


def test_settings_scan_now_returns_card(client, monkeypatch):
    # Under TESTING=1, the endpoint must NOT call Graph and must return the card partial.
    called = {"scan": 0}

    async def fake_scan(user, db):
        called["scan"] += 1

    monkeypatch.setattr("app.jobs.email_jobs._scan_user_inbox", fake_scan, raising=False)

    resp = client.post("/v2/partials/settings/inbox/scan-now")
    assert resp.status_code == 200
    assert "Mailbox sync" in resp.text
    assert called["scan"] == 0  # TESTING guard skipped the real scan


def test_requisition_poll_inbox_returns_responses_tab(client, test_requisition):
    resp = client.post(f"/v2/partials/requisitions/{test_requisition.id}/poll-inbox")
    assert resp.status_code == 200
