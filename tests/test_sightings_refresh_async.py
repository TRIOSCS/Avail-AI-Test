"""test_sightings_refresh_async.py — the sightings board refresh is now non-blocking.

The per-row Coverage refresh icon and the multi-select "Refresh Sightings" bulk action
no longer run the slow multi-supplier + AI ``search_requirement`` fan-out inline. Instead
they schedule it as a FastAPI ``BackgroundTasks`` job and return an immediate "Searching…"
state; the existing ``sighting-updated`` SSE stream swaps the fresh results in when each
search completes.

These tests pin that contract:
- POST /refresh (source=user) and /batch-refresh return immediately, scheduling
  ``search_requirement`` as a background job (NOT awaiting it inline), and the response
  marks the affected row(s) "Searching…".
- POST /refresh?source=sse renders the fresh detail panel WITHOUT scheduling a new search
  or publishing (breaking the SSE self-trigger loop).
- The background job runs the search and publishes a ``sighting-updated`` SSE per
  requirement, with concurrency capped at ``_SEARCH_FANOUT_LIMIT``.

Called by: pytest (asyncio_mode = auto)
Depends on: app/routers/sightings.py, conftest.py (client, db_session, test_user,
            test_requisition)
"""

import os

os.environ["TESTING"] = "1"

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import app.routers.sightings as sightings_router
from app.models import Requirement, Requisition, User


def _requirement(db: Session, req: Requisition) -> Requirement:
    return db.query(Requirement).filter_by(requisition_id=req.id).first()


def _add_requirement(db: Session, req: Requisition, mpn: str) -> Requirement:
    item = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        target_qty=25,
        sourcing_status="open",
        created_at=datetime.now(UTC),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


# ── Row refresh (per-row Coverage icon / detail Search button) ────────────────


class TestRowRefreshAsync:
    def test_returns_searching_state_and_schedules_background_search(
        self, client: TestClient, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """POST /refresh (source=user) returns a "Searching…" panel and schedules the
        search as a background job — it must NOT await search_requirement inline."""
        item = _requirement(db_session, test_requisition)
        scheduled = MagicMock()
        real_search = AsyncMock(return_value={"mpn_results": {}})
        with (
            patch("app.routers.sightings._run_search_and_publish", new=scheduled),
            patch("app.search_service.search_requirement", new=real_search),
        ):
            resp = client.post(
                f"/v2/partials/sightings/{item.id}/refresh?source=user",
                headers={"HX-Request": "true"},
            )

        assert resp.status_code == 200
        # Immediate acknowledgment: the searching panel, not the finished detail.
        assert "Searching suppliers" in resp.text
        # Correlation header so the client beforeSwap guard swaps it into the right row.
        assert resp.headers.get("X-Rendered-Req-Id") == str(item.id)
        # Search was scheduled as a background job (bg task ran under TestClient) …
        scheduled.assert_called_once_with([item.id], test_user.id)
        # … and NOT awaited inline in the request handler.
        real_search.assert_not_called()

    def test_default_source_is_user(
        self, client: TestClient, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """No ?source → treated as a user click: still schedules + returns searching."""
        item = _requirement(db_session, test_requisition)
        scheduled = MagicMock()
        with patch("app.routers.sightings._run_search_and_publish", new=scheduled):
            resp = client.post(
                f"/v2/partials/sightings/{item.id}/refresh",
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200
        assert "Searching suppliers" in resp.text
        scheduled.assert_called_once_with([item.id], test_user.id)

    def test_sse_source_renders_detail_without_scheduling_or_publishing(
        self, client: TestClient, db_session: Session, test_requisition: Requisition
    ):
        """POST /refresh?source=sse paints the fresh detail panel (the background search
        already ran) and must NOT schedule a new search or publish an SSE."""
        item = _requirement(db_session, test_requisition)
        scheduled = MagicMock()
        with (
            patch("app.routers.sightings._run_search_and_publish", new=scheduled),
            patch("app.routers.sightings.broker") as mock_broker,
        ):
            mock_broker.publish = AsyncMock()
            resp = client.post(
                f"/v2/partials/sightings/{item.id}/refresh?source=sse",
                headers={"HX-Request": "true"},
            )

        assert resp.status_code == 200
        assert resp.headers.get("X-Rendered-Req-Id") == str(item.id)
        # The detail panel, not the searching acknowledgment.
        assert "Searching suppliers" not in resp.text
        scheduled.assert_not_called()
        mock_broker.publish.assert_not_called()

    def test_missing_requirement_is_404(self, client: TestClient):
        resp = client.post("/v2/partials/sightings/999999/refresh", headers={"HX-Request": "true"})
        assert resp.status_code == 404


# ── Bulk refresh (multi-select action bar) ────────────────────────────────────


class TestBulkRefreshAsync:
    def test_schedules_background_and_marks_rows_searching(
        self, client: TestClient, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """/batch-refresh (HX-Target: sightings-table) re-renders the table with the
        scheduled rows flagged "Searching…" and schedules the fan-out as a background
        job instead of awaiting it inline."""
        item1 = _requirement(db_session, test_requisition)
        item2 = _add_requirement(db_session, test_requisition, "BULK-ASYNC-2")
        scheduled = MagicMock()
        real_search = AsyncMock(return_value={"mpn_results": {}})
        with (
            patch("app.routers.sightings._run_search_and_publish", new=scheduled),
            patch("app.search_service.search_requirement", new=real_search),
        ):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([item1.id, item2.id])},
                headers={"HX-Target": "sightings-table"},
            )

        assert resp.status_code == 200
        # Table re-rendered (both rows present) with the searching badge.
        assert "BULK-ASYNC-2" in resp.text
        assert "Searching" in resp.text
        # Fan-out scheduled once with both ids; NOT awaited inline.
        scheduled.assert_called_once()
        args = scheduled.call_args[0]
        assert set(args[0]) == {item1.id, item2.id}
        assert args[1] == test_user.id
        real_search.assert_not_called()
        # Immediate toast acknowledges the click.
        assert "Searching 2 requirements" in resp.headers.get("HX-Trigger", "")

    def test_non_table_caller_returns_toast_only(
        self, client: TestClient, db_session: Session, test_requisition: Requisition
    ):
        """The requisition parts-tab caller (hx-swap=none, no HX-Target) gets an empty
        body plus the "Searching…" toast."""
        item = _requirement(db_session, test_requisition)
        scheduled = MagicMock()
        with patch("app.routers.sightings._run_search_and_publish", new=scheduled):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([item.id])},
            )
        assert resp.status_code == 200
        assert resp.text == ""
        assert "Searching 1 requirement" in resp.headers.get("HX-Trigger", "")
        scheduled.assert_called_once()

    def test_missing_ids_schedule_nothing(self, client: TestClient, db_session: Session):
        """Non-existent requirement IDs are dropped; nothing is scheduled."""
        scheduled = MagicMock()
        with patch("app.routers.sightings._run_search_and_publish", new=scheduled):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([88888])},
            )
        assert resp.status_code == 200
        assert "No requirements to search" in resp.headers.get("HX-Trigger", "")
        scheduled.assert_not_called()

    def test_sse_source_is_inert(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        """Source=sse schedules no search and publishes nothing (self-trigger gate)."""
        item = _requirement(db_session, test_requisition)
        scheduled = MagicMock()
        with (
            patch("app.routers.sightings._run_search_and_publish", new=scheduled),
            patch("app.routers.sightings.broker") as mock_broker,
        ):
            mock_broker.publish = AsyncMock()
            resp = client.post(
                "/v2/partials/sightings/batch-refresh?source=sse",
                data={"requirement_ids": json.dumps([item.id])},
            )
        assert resp.status_code == 200
        assert resp.text == ""
        scheduled.assert_not_called()
        mock_broker.publish.assert_not_called()

    def test_exceeds_max_batch_size_is_400(self, client: TestClient):
        ids = list(range(sightings_router.MAX_BATCH_SIZE + 1))
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": json.dumps(ids)},
        )
        assert resp.status_code == 400


# ── Background job: runs the search + publishes SSE, bounded concurrency ───────


class TestBackgroundJob:
    async def test_runs_search_and_publishes_sighting_updated(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """_run_search_and_publish runs search_requirement for the requirement and then
        publishes a sighting-updated SSE carrying its requirement_id."""
        item = _requirement(db_session, test_requisition)
        sm = sessionmaker(bind=db_session.get_bind(), autoflush=False)
        search_mock = AsyncMock(return_value={"mpn_results": {}})
        with (
            patch("app.database.SessionLocal", sm),
            patch("app.search_service.search_requirement", new=search_mock),
            patch("app.routers.sightings.broker") as mock_broker,
        ):
            mock_broker.publish = AsyncMock()
            await sightings_router._run_search_and_publish([item.id], test_user.id)

        search_mock.assert_awaited_once()
        assert search_mock.await_args[0][0].id == item.id
        mock_broker.publish.assert_awaited_once()
        channel, event, data = mock_broker.publish.await_args[0]
        assert channel == f"user:{test_user.id}"
        assert event == "sighting-updated"
        assert json.loads(data)["requirement_id"] == item.id

    async def test_sse_source_suppresses_publish(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """The background job honours the source gate: source='sse' never publishes."""
        item = _requirement(db_session, test_requisition)
        sm = sessionmaker(bind=db_session.get_bind(), autoflush=False)
        with (
            patch("app.database.SessionLocal", sm),
            patch("app.search_service.search_requirement", new=AsyncMock(return_value={})),
            patch("app.routers.sightings.broker") as mock_broker,
        ):
            mock_broker.publish = AsyncMock()
            await sightings_router._run_search_and_publish([item.id], test_user.id, source="sse")
        mock_broker.publish.assert_not_called()

    async def test_publishes_even_when_search_fails(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """A failed search must not strand the board in "Searching…": the SSE still
        fires so the client re-renders."""
        item = _requirement(db_session, test_requisition)
        sm = sessionmaker(bind=db_session.get_bind(), autoflush=False)
        with (
            patch("app.database.SessionLocal", sm),
            patch("app.search_service.search_requirement", new=AsyncMock(side_effect=RuntimeError("boom"))),
            patch("app.routers.sightings.broker") as mock_broker,
        ):
            mock_broker.publish = AsyncMock()
            await sightings_router._run_search_and_publish([item.id], test_user.id)
        mock_broker.publish.assert_awaited_once()

    async def test_fanout_concurrency_is_bounded(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """More requirements than the cap → peak concurrent searches equals
        _SEARCH_FANOUT_LIMIT (never higher), and every requirement is published."""
        cap = sightings_router._SEARCH_FANOUT_LIMIT
        n = cap + 3
        ids = [_add_requirement(db_session, test_requisition, f"FANOUT-{i}").id for i in range(n)]
        sm = sessionmaker(bind=db_session.get_bind(), autoflush=False)

        inflight = 0
        peak = 0

        async def slow_search(req, db):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            try:
                import asyncio

                await asyncio.sleep(0.02)
                return {"mpn_results": {}}
            finally:
                inflight -= 1

        with (
            patch("app.database.SessionLocal", sm),
            patch("app.search_service.search_requirement", side_effect=slow_search),
            patch("app.routers.sightings.broker") as mock_broker,
        ):
            mock_broker.publish = AsyncMock()
            await sightings_router._run_search_and_publish(ids, test_user.id)

        assert peak == cap, f"peak concurrency {peak} != cap {cap}"
        assert mock_broker.publish.await_count == n
