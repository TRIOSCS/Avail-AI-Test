"""test_material_enrich_async.py — the material-card Enrich button must return
instantly.

The bug: POST /v2/partials/materials/{id}/enrich ran the authoritative ladder +
structured-spec pass INLINE (~30s of web extraction) before responding, so the click
looked frozen. The fix schedules that heavy work as a FastAPI BackgroundTask, flips the
card to the ``unenriched`` ("Queued for enrichment") marker, and returns the detail
partial immediately; the enrich-status poller then lands the refreshed detail (or a
"couldn't complete" toast) once the background run finishes.

Covers:
  * endpoint returns immediately — heavy work is scheduled, NOT awaited inline;
  * queued/in-progress status is set and the returned partial shows the polling badge;
  * double-enqueue is guarded (a run already in flight does not stack another);
  * the background runner marks the run 'done' on success and 'blocked' on no-op/outage;
  * the poller reflects in-progress (keep polling) then terminal (refresh detail + stop);
  * source-unavailable still surfaces the existing "couldn't complete" toast — now from
    the poller path, not a 30s inline block.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user, test_material_card),
            app.routers.htmx.materials, app.services.material_enrich_runs.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.intelligence import MaterialCard
from app.services import material_enrich_runs
from app.services.material_enrich_runs import enrich_runs


@pytest.fixture(autouse=True)
def _clear_enrich_runs():
    """Reset the process-wide in-flight registry around every test (isolation)."""
    enrich_runs._state.clear()
    yield
    enrich_runs._state.clear()


def _make_card(db: Session, mpn: str, *, status: str = "unenriched", enriched: bool = False) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn.upper(),
        manufacturer="TI",
        search_count=0,
        enrichment_status=status,
        enriched_at=datetime.now(timezone.utc) if enriched else None,
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


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


# ── Endpoint: returns immediately, schedules the heavy work, sets queued status ──────


@pytest.mark.asyncio
async def test_enrich_schedules_background_and_does_not_run_inline(db_session, test_user, monkeypatch):
    """The handler must register a background task and return WITHOUT awaiting the
    enrichment.

    Proven by: enrich_cards is not called during the handler, a task is
    queued on the BackgroundTasks object, and the card is left in the queued state.
    """
    from app.routers.htmx import materials as mat

    card = _make_card(db_session, "async-001", status="web_sourced", enriched=True)

    enrich_cards_mock = AsyncMock(return_value={"web_sourced": 1})
    specs_mock = AsyncMock()
    fake_bg_session = MagicMock()
    monkeypatch.setattr("app.services.authoritative_enrichment_service.enrich_cards", enrich_cards_mock)
    monkeypatch.setattr("app.services.spec_enrichment_service.enrich_card_specs", specs_mock)
    monkeypatch.setattr("app.database.SessionLocal", lambda: fake_bg_session)
    # Skip the (heavy) template render — this test only inspects scheduling + status.
    monkeypatch.setattr(mat, "material_detail_partial", AsyncMock(return_value=HTMLResponse("queued")))

    bg = BackgroundTasks()
    await mat.enrich_material(_make_request(), card.id, bg, test_user, db_session)

    # Heavy work NOT awaited inline.
    enrich_cards_mock.assert_not_called()
    specs_mock.assert_not_called()
    # A background task WAS registered.
    assert len(bg.tasks) == 1
    # Card flipped to the queued/in-progress marker.
    db_session.refresh(card)
    assert card.enrichment_status == "unenriched"

    # Running the scheduled task now performs the heavy work with the expected args.
    await bg()
    enrich_cards_mock.assert_awaited_once()
    assert enrich_cards_mock.call_args.args[0] == [card.id]
    assert enrich_cards_mock.call_args.kwargs.get("refresh") is True
    specs_mock.assert_awaited_once()
    assert specs_mock.call_args.kwargs.get("force") is True


@pytest.mark.asyncio
async def test_enrich_double_click_does_not_stack_second_run(db_session, test_user, monkeypatch):
    """A run already in flight must not enqueue a second background enrichment."""
    from app.routers.htmx import materials as mat

    card = _make_card(db_session, "async-002")
    monkeypatch.setattr(mat, "material_detail_partial", AsyncMock(return_value=HTMLResponse("queued")))

    # Simulate a run already in flight.
    assert enrich_runs.begin(card.id) is True

    bg = BackgroundTasks()
    await mat.enrich_material(_make_request(), card.id, bg, test_user, db_session)

    assert len(bg.tasks) == 0  # no second run stacked


def test_enrich_nonexistent_still_404(client):
    resp = client.post("/v2/partials/materials/999999/enrich", headers={"HX-Request": "true"})
    assert resp.status_code == 404


def test_enrich_returns_queued_badge_immediately(client, db_session, monkeypatch):
    """Via HTTP: the response is the detail partial showing the polling 'Queued' badge,
    and the card is left unenriched (the worker hasn't produced a terminal status yet)."""
    from app.routers.htmx import materials as mat

    card = _make_card(db_session, "async-003", status="verified", enriched=True)
    # Neutralise the background runner so this test only asserts the immediate response.
    monkeypatch.setattr(mat, "_run_card_enrichment", AsyncMock())

    resp = client.post(f"/v2/partials/materials/{card.id}/enrich", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "Queued for enrichment" in resp.text
    assert "every 15s" in resp.text  # poller is active
    db_session.refresh(card)
    assert card.enrichment_status == "unenriched"


# ── Background runner: outcome recorded for the poller ───────────────────────────────


@pytest.mark.asyncio
async def test_background_run_marks_done_on_success(monkeypatch):
    from app.routers.htmx.materials import _run_card_enrichment

    fake_session = MagicMock()
    monkeypatch.setattr("app.database.SessionLocal", lambda: fake_session)
    monkeypatch.setattr(
        "app.services.authoritative_enrichment_service.enrich_cards",
        AsyncMock(return_value={"web_sourced": 1}),
    )
    monkeypatch.setattr("app.services.spec_enrichment_service.enrich_card_specs", AsyncMock())

    enrich_runs.begin(4242)
    await _run_card_enrichment(4242)

    assert enrich_runs.consume_outcome(4242) == material_enrich_runs.DONE
    fake_session.close.assert_called_once()


@pytest.mark.asyncio
async def test_background_run_marks_blocked_on_noop(monkeypatch):
    """No status tally landed (empty counts) → the run is 'blocked' (source
    unavailable)."""
    from app.routers.htmx.materials import _run_card_enrichment

    monkeypatch.setattr("app.database.SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(
        "app.services.authoritative_enrichment_service.enrich_cards",
        AsyncMock(return_value={"claude_error": 1}),
    )
    monkeypatch.setattr("app.services.spec_enrichment_service.enrich_card_specs", AsyncMock())

    enrich_runs.begin(4243)
    await _run_card_enrichment(4243)

    assert enrich_runs.consume_outcome(4243) == material_enrich_runs.BLOCKED


@pytest.mark.asyncio
async def test_background_run_marks_blocked_on_exception(monkeypatch):
    """enrich_cards raising must be swallowed (never crash the worker) and mark
    blocked."""
    from app.routers.htmx.materials import _run_card_enrichment

    monkeypatch.setattr("app.database.SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(
        "app.services.authoritative_enrichment_service.enrich_cards",
        AsyncMock(side_effect=RuntimeError("backend down")),
    )
    monkeypatch.setattr("app.services.spec_enrichment_service.enrich_card_specs", AsyncMock())

    enrich_runs.begin(4244)
    await _run_card_enrichment(4244)  # must not raise

    assert enrich_runs.consume_outcome(4244) == material_enrich_runs.BLOCKED


# ── Poller: in-progress keeps polling, terminal refreshes detail, blocked toasts ─────


def test_poller_in_progress_keeps_polling(client, db_session):
    card = _make_card(db_session, "poll-async-1")
    enrich_runs.begin(card.id)  # running, no outcome yet

    resp = client.get(f"/v2/partials/materials/{card.id}/enrich-status")
    assert resp.status_code == 200
    assert "Queued for enrichment" in resp.text
    assert "every 15s" in resp.text
    assert "HX-Trigger" not in resp.headers


def test_poller_terminal_refreshes_full_detail_and_stops(client, db_session):
    """On terminal status the poller returns the WHOLE refreshed detail (not just the
    badge), retargeted to #main-content, and stops polling (286)."""
    card = _make_card(db_session, "poll-async-2", status="web_sourced", enriched=True)
    enrich_runs.finish(card.id, blocked=False)  # worker just finished

    resp = client.get(f"/v2/partials/materials/{card.id}/enrich-status")
    assert resp.status_code == 286  # htmx stop-polling
    assert resp.headers.get("HX-Retarget") == "#main-content"
    assert resp.headers.get("HX-Reswap") == "innerHTML"
    # Full detail, not just the badge: the Specifications section + tab bar are present.
    assert "Specifications" in resp.text
    assert "WEB-SOURCED" in resp.text  # terminal badge rendered
    assert "every 15s" not in resp.text  # no longer polling
    # Registry cleaned up.
    assert enrich_runs.consume_outcome(card.id) is None


def test_poller_blocked_surfaces_toast_once(client, db_session):
    """A blocked/no-op run leaves the card unenriched; the poller surfaces the existing
    'couldn't complete' toast exactly once, then stops repeating it."""
    card = _make_card(db_session, "poll-async-3")
    enrich_runs.finish(card.id, blocked=True)

    resp = client.get(f"/v2/partials/materials/{card.id}/enrich-status")
    assert resp.status_code == 200
    trigger = resp.headers.get("HX-Trigger", "")
    assert "showToast" in trigger
    assert "couldn't complete" in trigger

    # Outcome consumed → the next poll does not repeat the toast.
    resp2 = client.get(f"/v2/partials/materials/{card.id}/enrich-status")
    assert "couldn't complete" not in resp2.headers.get("HX-Trigger", "")
