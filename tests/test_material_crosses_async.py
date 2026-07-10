"""test_material_crosses_async.py — the material-card "Find Crosses" button must return
instantly.

The bug: POST /v2/partials/materials/{id}/find-crosses ran the Claude crosses/substitutes
lookup INLINE (``asyncio.wait_for`` up to 30s) before responding, so the Crosses &
Substitutes section spun and the whole page felt frozen. The fix schedules that Claude
call as a FastAPI BackgroundTask and returns the "Finding crosses…" polling partial
immediately; the crosses-status poller then swaps in the results (or the retry/error
state) once the background run finishes.

Covers:
  * endpoint returns immediately — the Claude lookup is scheduled, NOT awaited inline;
  * a cache hit (already-populated cross_references, no refresh) still returns the loaded
    section synchronously — no background work;
  * double-enqueue is guarded (a lookup already in flight does not stack another);
  * the background runner persists + dedupes the crosses and marks the run 'done'
    (including a legitimate empty result), and marks 'blocked' on an AI failure;
  * the poller reflects in-progress (keep polling) then terminal (swap the section + stop),
    surfaces the retry/error state on a blocked run, and stops polling when no run is
    tracked (process restarted mid-run).

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user),
            app.routers.htmx.materials, app.services.material_enrich_runs.
"""

from datetime import UTC
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.intelligence import MaterialCard
from app.services import material_enrich_runs
from app.services.material_enrich_runs import crosses_runs


@pytest.fixture(autouse=True)
def _clear_crosses_runs():
    """Reset the process-wide in-flight registry around every test (isolation)."""
    crosses_runs._state.clear()
    yield
    crosses_runs._state.clear()


def _make_card(db: Session, mpn: str, *, crosses=None) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn.upper(),
        manufacturer="TI",
        search_count=0,
        enrichment_status="verified",
        cross_references=crosses,
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


# ── Endpoint: returns immediately, schedules the Claude lookup, no inline block ──────────


@pytest.mark.asyncio
async def test_find_crosses_schedules_background_and_does_not_run_inline(db_session, test_user, monkeypatch):
    """The handler must register a background task and return WITHOUT awaiting the
    Claude lookup.

    Proven by: claude_json is not called during the handler, a task is queued on the
    BackgroundTasks object, and the response is the "Finding crosses…" polling partial.
    """
    from app.routers.htmx import materials as mat

    card = _make_card(db_session, "cross-001")  # no cross_references → not a cache hit

    ai_mock = AsyncMock(return_value={"crosses": []})
    monkeypatch.setattr("app.utils.claude_client.claude_json", ai_mock)
    monkeypatch.setattr("app.database.SessionLocal", lambda: MagicMock())

    bg = BackgroundTasks()
    resp = await mat.find_crosses(_make_request(), card.id, bg, refresh=False, user=test_user, db=db_session)

    # Claude lookup NOT awaited inline.
    ai_mock.assert_not_called()
    # A background task WAS registered, and the immediate response is the polling partial.
    assert len(bg.tasks) == 1
    body = resp.body.decode()
    assert "Finding crosses" in body
    assert "crosses-status" in body  # poller is active
    # A run is claimed (double-enqueue guard armed).
    assert crosses_runs.is_running(card.id) is True

    # Running the scheduled task now performs the Claude lookup.
    await bg()
    ai_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_find_crosses_cache_hit_returns_section_synchronously(db_session, test_user, monkeypatch):
    """A card with cached cross_references (and no refresh) returns the loaded section
    with NO background work scheduled."""
    from app.routers.htmx import materials as mat

    card = _make_card(db_session, "cross-002", crosses=[{"mpn": "ALT-123", "manufacturer": "ADI"}])
    ai_mock = AsyncMock()
    monkeypatch.setattr("app.utils.claude_client.claude_json", ai_mock)

    bg = BackgroundTasks()
    resp = await mat.find_crosses(_make_request(), card.id, bg, refresh=False, user=test_user, db=db_session)

    assert len(bg.tasks) == 0  # no background task on a cache hit
    ai_mock.assert_not_called()
    body = resp.body.decode()
    assert "ALT-123" in body  # loaded chip rendered
    assert "Finding crosses" not in body
    assert crosses_runs.is_running(card.id) is False


@pytest.mark.asyncio
async def test_find_crosses_refresh_reschedules_even_with_cache(db_session, test_user, monkeypatch):
    """An explicit refresh bypasses the cache and schedules a fresh background
    lookup."""
    from app.routers.htmx import materials as mat

    card = _make_card(db_session, "cross-003", crosses=[{"mpn": "OLD-1", "manufacturer": "TI"}])
    monkeypatch.setattr("app.utils.claude_client.claude_json", AsyncMock(return_value={"crosses": []}))
    monkeypatch.setattr("app.database.SessionLocal", lambda: MagicMock())

    bg = BackgroundTasks()
    resp = await mat.find_crosses(_make_request(), card.id, bg, refresh=True, user=test_user, db=db_session)

    assert len(bg.tasks) == 1
    assert "Finding crosses" in resp.body.decode()


@pytest.mark.asyncio
async def test_find_crosses_double_click_does_not_stack_second_run(db_session, test_user):
    """A lookup already in flight must not enqueue a second background run."""
    from app.routers.htmx import materials as mat

    card = _make_card(db_session, "cross-004")
    assert crosses_runs.begin(card.id) is True  # simulate a run already in flight

    bg = BackgroundTasks()
    await mat.find_crosses(_make_request(), card.id, bg, refresh=False, user=test_user, db=db_session)

    assert len(bg.tasks) == 0  # no second run stacked


def test_find_crosses_nonexistent_still_404(client):
    resp = client.post("/v2/partials/materials/999999/find-crosses", headers={"HX-Request": "true"})
    assert resp.status_code == 404


def test_find_crosses_returns_finding_partial_immediately(client, db_session, monkeypatch):
    """Via HTTP: the response is the "Finding crosses…" polling partial, returned without
    running the (neutralised) background lookup."""
    from app.routers.htmx import materials as mat

    card = _make_card(db_session, "cross-005")
    # Neutralise the background runner so this test only asserts the immediate response
    # (TestClient runs background tasks synchronously after the response).
    monkeypatch.setattr(mat, "_run_card_crosses", AsyncMock())

    resp = client.post(f"/v2/partials/materials/{card.id}/find-crosses", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "Finding crosses" in resp.text
    assert "every 3s" in resp.text  # poller is active
    assert f"/v2/partials/materials/{card.id}/crosses-status" in resp.text


# ── Background runner: persists / dedupes results, records outcome for the poller ────────


@pytest.mark.asyncio
async def test_background_run_persists_and_dedupes_and_marks_done(db_session, monkeypatch):
    """The runner persists the deduplicated crosses (own MPN excluded) and marks
    'done'."""
    from app.routers.htmx.materials import _run_card_crosses

    card = _make_card(db_session, "cross-run-1")
    cid = card.id  # capture before the runner closes (and detaches from) this shared session
    monkeypatch.setattr("app.database.SessionLocal", lambda: db_session)
    monkeypatch.setattr(
        "app.utils.claude_client.claude_json",
        AsyncMock(
            return_value={
                "crosses": [
                    {"mpn": "CROSS-RUN-1", "manufacturer": "TI"},  # the card's own MPN → dropped
                    {"mpn": "ALT-999", "manufacturer": "ADI"},
                    {"no_mpn": "skip"},  # malformed → dropped
                ]
            }
        ),
    )

    crosses_runs.begin(cid)
    await _run_card_crosses(cid)

    assert crosses_runs.consume_outcome(cid) == material_enrich_runs.DONE
    refreshed = db_session.get(MaterialCard, cid)  # session is reusable after the runner's close()
    assert refreshed.cross_references == [{"mpn": "ALT-999", "manufacturer": "ADI"}]


@pytest.mark.asyncio
async def test_background_run_empty_result_still_marks_done(db_session, monkeypatch):
    """A legitimate no-results lookup persists an empty list and marks 'done' (NOT
    blocked) — the poller must not spin forever on a genuinely empty answer."""
    from app.routers.htmx.materials import _run_card_crosses

    card = _make_card(db_session, "cross-run-2")
    cid = card.id  # capture before the runner closes (and detaches from) this shared session
    monkeypatch.setattr("app.database.SessionLocal", lambda: db_session)
    monkeypatch.setattr("app.utils.claude_client.claude_json", AsyncMock(return_value={"crosses": []}))

    crosses_runs.begin(cid)
    await _run_card_crosses(cid)

    assert crosses_runs.consume_outcome(cid) == material_enrich_runs.DONE
    assert db_session.get(MaterialCard, cid).cross_references == []


@pytest.mark.asyncio
async def test_background_run_marks_blocked_on_exception(monkeypatch):
    """The Claude lookup raising must be swallowed (never crash the worker) and mark
    blocked."""
    from app.routers.htmx.materials import _run_card_crosses

    fake_session = MagicMock()
    monkeypatch.setattr("app.database.SessionLocal", lambda: fake_session)
    monkeypatch.setattr(
        "app.utils.claude_client.claude_json",
        AsyncMock(side_effect=RuntimeError("backend down")),
    )

    crosses_runs.begin(7001)
    await _run_card_crosses(7001)  # must not raise

    assert crosses_runs.consume_outcome(7001) == material_enrich_runs.BLOCKED
    fake_session.rollback.assert_called_once()
    fake_session.close.assert_called_once()


# ── Poller: in-progress keeps polling, terminal swaps section + stops, blocked → retry ───


def test_crosses_poller_in_progress_keeps_polling(client, db_session):
    card = _make_card(db_session, "poll-cross-1")
    crosses_runs.begin(card.id)  # running, no outcome yet

    resp = client.get(f"/v2/partials/materials/{card.id}/crosses-status")
    assert resp.status_code == 200
    assert "Finding crosses" in resp.text
    assert "every 3s" in resp.text


def test_crosses_poller_terminal_swaps_section_and_stops(client, db_session):
    """On a 'done' outcome the poller returns the refreshed crosses section and stops
    polling (286)."""
    card = _make_card(db_session, "poll-cross-2", crosses=[{"mpn": "ALT-42", "manufacturer": "ADI"}])
    crosses_runs.finish(card.id, blocked=False)  # worker just finished

    resp = client.get(f"/v2/partials/materials/{card.id}/crosses-status")
    assert resp.status_code == 286  # htmx stop-polling
    assert "ALT-42" in resp.text  # loaded results swapped in
    assert "every 3s" not in resp.text  # no longer polling
    assert crosses_runs.consume_outcome(card.id) is None  # outcome consumed


def test_crosses_poller_blocked_shows_retry_and_stops(client, db_session):
    """A blocked run returns the error/retry state and stops polling (286)."""
    card = _make_card(db_session, "poll-cross-3")
    crosses_runs.finish(card.id, blocked=True)

    resp = client.get(f"/v2/partials/materials/{card.id}/crosses-status")
    assert resp.status_code == 286
    assert "Retry" in resp.text
    assert "failed" in resp.text.lower()
    assert "every 3s" not in resp.text


def test_crosses_poller_no_run_stops_polling(client, db_session):
    """No run tracked (e.g. process restarted mid-run) → stop polling, render current
    section rather than spinning forever."""
    card = _make_card(db_session, "poll-cross-4")

    resp = client.get(f"/v2/partials/materials/{card.id}/crosses-status")
    assert resp.status_code == 286
    assert "every 3s" not in resp.text


def test_crosses_poller_deleted_card_stops_polling(client, db_session):
    from datetime import datetime

    card = _make_card(db_session, "poll-cross-5")
    card.deleted_at = datetime.now(UTC)
    db_session.commit()
    crosses_runs.begin(card.id)

    resp = client.get(f"/v2/partials/materials/{card.id}/crosses-status")
    assert resp.status_code == 286
    assert resp.text == ""
