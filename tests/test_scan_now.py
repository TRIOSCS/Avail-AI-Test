"""Tests for the real scan-now endpoints (TESTING guard => no Graph)."""

import pytest


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
    assert len(resp.text) > 0


@pytest.mark.asyncio
async def test_run_inbox_scan_now_calls_scanner_when_not_testing(monkeypatch):
    from types import SimpleNamespace

    import app.routers.htmx_views as hv

    monkeypatch.setenv("TESTING", "0")  # bypass the hermetic guard for this call
    calls = {"n": 0}

    async def fake_scan(user, db):
        calls["n"] += 1

    monkeypatch.setattr("app.jobs.email_jobs._scan_user_inbox", fake_scan)
    await hv._run_inbox_scan_now(SimpleNamespace(email="u@x.com"), object())
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_run_inbox_scan_now_swallows_timeout(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import app.routers.htmx_views as hv

    monkeypatch.setenv("TESTING", "0")

    async def slow_scan(user, db):
        raise asyncio.TimeoutError()  # simulate wait_for timing out

    # Patch asyncio.wait_for as used in htmx_views to raise TimeoutError deterministically
    async def fake_wait_for(coro, timeout):
        # close the passed coroutine to avoid 'never awaited' warning, then raise
        coro.close()
        raise asyncio.TimeoutError()

    monkeypatch.setattr("app.jobs.email_jobs._scan_user_inbox", slow_scan)
    monkeypatch.setattr(hv.asyncio, "wait_for", fake_wait_for)
    # must NOT raise — timeout is swallowed with a warning
    await hv._run_inbox_scan_now(SimpleNamespace(email="u@x.com"), object())
