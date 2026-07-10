"""test_vendor_find_contacts_async.py — the vendor "Find Contacts" tab must return
instantly.

The bug: POST /v2/partials/vendors/{id}/ai/find-contacts ran the AI web-search contact
finder INLINE (``enrich_contacts_websearch``: Claude + the web_search tool, commonly >15s)
before responding, so it blew past htmx's 15s client timeout (``htmx.config.timeout``) and
the Find Contacts tab spun then errored out. The fix schedules that call as a FastAPI
BackgroundTask and returns the "Finding contacts…" polling partial immediately; the
find-contacts-status poller then swaps in the discovered contacts (or the none-found /
error state) once the background run finishes.

Covers:
  * endpoint returns immediately — the web search is scheduled, NOT awaited inline;
  * double-enqueue is guarded (a search already in flight does not stack another);
  * the AI gate ("off") still short-circuits before scheduling any work;
  * the background runner persists + dedupes the contacts and records ``new_count`` on a
    success (including a legitimate empty "none found" result), records an error outcome on
    an AI failure, and NEVER raises;
  * the runner passes the title-keyword string through to the search service;
  * the poller reflects in-progress (keep polling) then terminal (swap the results + stop),
    surfaces the none-found and error panels, and stops polling when no run is tracked
    (process restarted mid-run) or the vendor is gone.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user),
            app.routers.htmx.vendors, app.services.vendor_contact_runs.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models import VendorCard
from app.models.enrichment import ProspectContact
from app.services.vendor_contact_runs import VendorContactRunOutcome, vendor_contact_runs


@pytest.fixture(autouse=True)
def _clear_vendor_contact_runs():
    """Reset the process-wide in-flight registry around every test (isolation)."""
    vendor_contact_runs._state.clear()
    yield
    vendor_contact_runs._state.clear()


@pytest.fixture()
def vendor(db_session: Session) -> VendorCard:
    """A vendor card with a domain for AI contact search."""
    card = VendorCard(
        normalized_name="digikey",
        display_name="DigiKey Electronics",
        domain="digikey.com",
        sighting_count=10,
        created_at=datetime.now(UTC),
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    return card


def _prospect_count(db: Session, vendor_id: int) -> int:
    return db.query(ProspectContact).filter(ProspectContact.vendor_card_id == vendor_id).count()


def _make_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "query_string": b"",
        }
    )


# ── Endpoint: returns immediately, schedules the web search, no inline block ──────────────


@pytest.mark.asyncio
async def test_find_contacts_schedules_background_and_does_not_run_inline(db_session, test_user, monkeypatch):
    """The handler must register a background task and return WITHOUT awaiting the >15s
    web search.

    Proven by: enrich_contacts_websearch is not awaited during the handler, a task is queued
    on the BackgroundTasks object, and the response is the "Finding contacts…" poller.
    """
    from app.routers.htmx import vendors as ven

    card = VendorCard(normalized_name="acme", display_name="Acme", domain="acme.com", sighting_count=1)
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)

    search_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("app.services.ai_service.enrich_contacts_websearch", search_mock)
    monkeypatch.setattr("app.database.SessionLocal", lambda: MagicMock())

    bg = BackgroundTasks()
    resp = await ven.vendor_find_contacts(
        _make_request(), card.id, bg, title_keywords="buyer", user=test_user, db=db_session
    )

    # Web search NOT awaited inline.
    search_mock.assert_not_called()
    # A background task WAS registered, and the immediate response is the polling partial.
    assert len(bg.tasks) == 1
    body = resp.body.decode()
    assert "Finding contacts" in body
    assert "find-contacts-status" in body  # poller is active
    # A run is claimed (double-enqueue guard armed).
    assert vendor_contact_runs.is_running(card.id) is True

    # Running the scheduled task now performs the web search.
    await bg()
    search_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_find_contacts_double_click_does_not_stack_second_run(db_session, test_user, vendor):
    """A search already in flight must not enqueue a second background run."""
    from app.routers.htmx import vendors as ven

    assert vendor_contact_runs.begin(vendor.id) is True  # simulate a run already in flight

    bg = BackgroundTasks()
    await ven.vendor_find_contacts(_make_request(), vendor.id, bg, title_keywords="", user=test_user, db=db_session)

    assert len(bg.tasks) == 0  # no second run stacked


@pytest.mark.asyncio
async def test_find_contacts_ai_off_short_circuits(db_session, test_user, vendor, monkeypatch):
    """With AI features off the handler returns the disabled banner and schedules
    nothing."""
    from app.routers.htmx import vendors as ven

    class _FakeSettings:
        ai_features_enabled = "off"

    monkeypatch.setattr("app.config.settings", _FakeSettings())

    bg = BackgroundTasks()
    resp = await ven.vendor_find_contacts(
        _make_request(), vendor.id, bg, title_keywords="", user=test_user, db=db_session
    )

    assert "AI features are currently disabled" in resp.body.decode()
    assert len(bg.tasks) == 0
    assert vendor_contact_runs.is_running(vendor.id) is False


def test_find_contacts_nonexistent_still_404(client):
    resp = client.post("/v2/partials/vendors/999999/ai/find-contacts", headers={"HX-Request": "true"})
    assert resp.status_code == 404


def test_find_contacts_returns_finding_partial_immediately(client, db_session, vendor, monkeypatch):
    """Via HTTP: the response is the "Finding contacts…" polling partial, returned without
    running the (neutralised) background search."""
    from app.routers.htmx import vendors as ven

    # Neutralise the background runner so this test only asserts the immediate response
    # (TestClient runs background tasks synchronously after the response).
    monkeypatch.setattr(ven, "_run_vendor_find_contacts", AsyncMock())

    resp = client.post(f"/v2/partials/vendors/{vendor.id}/ai/find-contacts", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "Finding contacts" in resp.text
    assert "every 3s" in resp.text  # poller is active
    assert f"/v2/partials/vendors/{vendor.id}/ai/find-contacts-status" in resp.text


# ── Background runner: persists / dedupes results, records outcome for the poller ────────


@pytest.mark.asyncio
async def test_background_run_persists_and_dedupes_and_marks_done(db_session, vendor, monkeypatch):
    """The runner dedupes by email within the batch, persists the survivors, and records
    ``new_count``."""
    from app.routers.htmx.vendors import _run_vendor_find_contacts

    vid = vendor.id  # capture before the runner closes (and detaches from) this shared session
    monkeypatch.setattr("app.database.SessionLocal", lambda: db_session)
    monkeypatch.setattr(
        "app.services.ai_service.enrich_contacts_websearch",
        AsyncMock(
            return_value=[
                {"full_name": "Bob Jones", "email": "bob@digikey.com", "source": "web_search", "confidence": "medium"},
                {
                    "full_name": "Robert Jones",
                    "email": "bob@digikey.com",
                    "source": "web_search",
                },  # dup email → dropped
                {"full_name": "Alice Test", "email": "alice@digikey.com", "source": "web_search"},
            ]
        ),
    )

    vendor_contact_runs.begin(vid)
    await _run_vendor_find_contacts(vid, "buyer")

    outcome = vendor_contact_runs.consume_outcome(vid)
    assert outcome == VendorContactRunOutcome(new_count=2, error=None)
    assert _prospect_count(db_session, vid) == 2  # session reusable after the runner's close()


@pytest.mark.asyncio
async def test_background_run_empty_result_marks_done_none_found(db_session, vendor, monkeypatch):
    """A legitimate no-results search records ``new_count=0`` with no error — the poller
    renders 'none found', it must not spin forever."""
    from app.routers.htmx.vendors import _run_vendor_find_contacts

    vid = vendor.id
    monkeypatch.setattr("app.database.SessionLocal", lambda: db_session)
    monkeypatch.setattr("app.services.ai_service.enrich_contacts_websearch", AsyncMock(return_value=[]))

    vendor_contact_runs.begin(vid)
    await _run_vendor_find_contacts(vid, None)

    assert vendor_contact_runs.consume_outcome(vid) == VendorContactRunOutcome(new_count=0, error=None)
    assert _prospect_count(db_session, vid) == 0


@pytest.mark.asyncio
async def test_background_run_records_error_on_exception(monkeypatch):
    """The web search raising must be swallowed (never crash the worker) and recorded as
    an error outcome."""
    from app.routers.htmx.vendors import _run_vendor_find_contacts

    fake_session = MagicMock()
    monkeypatch.setattr("app.database.SessionLocal", lambda: fake_session)
    monkeypatch.setattr(
        "app.services.ai_service.enrich_contacts_websearch",
        AsyncMock(side_effect=RuntimeError("API timeout")),
    )

    vendor_contact_runs.begin(7001)
    await _run_vendor_find_contacts(7001, None)  # must not raise

    outcome = vendor_contact_runs.consume_outcome(7001)
    assert outcome is not None and outcome.error is not None
    assert "AI search failed" in outcome.error
    fake_session.rollback.assert_called_once()
    fake_session.close.assert_called_once()


@pytest.mark.asyncio
async def test_background_run_passes_keywords_through(db_session, vendor, monkeypatch):
    """The title-keyword string reaches the search service (the filter is honoured)."""
    from app.routers.htmx.vendors import _run_vendor_find_contacts

    search_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("app.database.SessionLocal", lambda: db_session)
    monkeypatch.setattr("app.services.ai_service.enrich_contacts_websearch", search_mock)

    vendor_contact_runs.begin(vendor.id)
    await _run_vendor_find_contacts(vendor.id, "procurement, buyer")

    # enrich_contacts_websearch(display_name, domain, keywords, limit=...)
    assert search_mock.await_args.args[2] == "procurement, buyer"


# ── Poller: in-progress keeps polling, terminal swaps results + stops ─────────────────────


def test_poller_in_progress_keeps_polling(client, db_session, vendor):
    vendor_contact_runs.begin(vendor.id)  # running, no outcome yet

    resp = client.get(f"/v2/partials/vendors/{vendor.id}/ai/find-contacts-status")
    assert resp.status_code == 200
    assert "Finding contacts" in resp.text
    assert "every 3s" in resp.text


def test_poller_terminal_swaps_results_and_stops(client, db_session, vendor):
    """On a success outcome the poller returns the discovered contacts and stops polling
    (286)."""
    db_session.add(
        ProspectContact(
            vendor_card_id=vendor.id,
            full_name="Carol Poller",
            email="carol@digikey.com",
            source="web_search",
            confidence="low",
            created_at=datetime.now(UTC),
        )
    )
    db_session.commit()
    vendor_contact_runs.finish(vendor.id, VendorContactRunOutcome(new_count=1))  # worker just finished

    resp = client.get(f"/v2/partials/vendors/{vendor.id}/ai/find-contacts-status")
    assert resp.status_code == 286  # htmx stop-polling
    assert "Carol Poller" in resp.text  # results swapped in
    assert "every 3s" not in resp.text  # no longer polling
    assert vendor_contact_runs.consume_outcome(vendor.id) is None  # outcome consumed


def test_poller_none_found_state(client, db_session, vendor):
    """A zero-result success renders the 'no contacts found' state and stops polling."""
    vendor_contact_runs.finish(vendor.id, VendorContactRunOutcome(new_count=0))

    resp = client.get(f"/v2/partials/vendors/{vendor.id}/ai/find-contacts-status")
    assert resp.status_code == 286
    assert "No contacts found" in resp.text
    assert "every 3s" not in resp.text


def test_poller_error_state(client, db_session, vendor):
    """A failed run returns the error panel and stops polling (286)."""
    vendor_contact_runs.finish(vendor.id, VendorContactRunOutcome(error="AI search failed: boom"))

    resp = client.get(f"/v2/partials/vendors/{vendor.id}/ai/find-contacts-status")
    assert resp.status_code == 286
    assert "AI search failed" in resp.text
    assert "every 3s" not in resp.text


def test_poller_no_run_stops_polling(client, db_session, vendor):
    """No run tracked (e.g. process restarted mid-run) → stop polling, render current
    prospects rather than spinning forever."""
    resp = client.get(f"/v2/partials/vendors/{vendor.id}/ai/find-contacts-status")
    assert resp.status_code == 286
    assert "every 3s" not in resp.text


def test_poller_missing_vendor_stops_polling(client):
    resp = client.get("/v2/partials/vendors/999999/ai/find-contacts-status")
    assert resp.status_code == 286
    assert resp.text == ""
