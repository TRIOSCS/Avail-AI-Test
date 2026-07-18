"""test_search_streaming_deadline.py — Phase-0 FIX A: the interactive SSE search's
aggregate deadline + straggler cancellation + ApiSource telemetry, plus the paired
Retry-After cap.

Before this fix stream_search_mpn's `while pending: await asyncio.wait(...)` had NO
timeout, so one hung/rate-limited connector delayed the terminal `done` SSE event 60s
to ~10min and recorded zero ApiSource telemetry.

Covers:
- _await_next_within_budget (the extracted, unit-testable deadline helper)
- stream_search_mpn end-to-end with a hung connector (timeout chip + done + telemetry)
- _parse_retry_after cap lowered 300s -> 30s -> 8s (search budget is 12s aggregate;
  a 30s sleep always outlives it and gets cancelled anyway)

Called by: pytest
Depends on: app/search_service.py, app/connectors/sources.py, tests/conftest.py
"""

import asyncio
import json
import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import ApiSource
from app.search_service import _await_next_within_budget, stream_search_mpn
from tests.conftest import engine  # noqa: F401

# ── _await_next_within_budget: the deadline helper in isolation ──────────────


class TestAwaitNextWithinBudget:
    async def test_fast_task_completes_no_timeout(self):
        """A task that finishes within the budget is returned in `done`, not timed
        out."""

        async def _quick():
            return "ok"

        task = asyncio.create_task(_quick())
        done, pending, timed_out = await _await_next_within_budget({task}, remaining=5.0)

        assert task in done
        assert pending == set()
        assert timed_out == set()

    async def test_hung_task_past_budget_is_cancelled_and_returned(self):
        """When the budget expires with a task still running, it is cancelled, drained,
        and handed back in `timed_out` (mirrors _fetch_fresh straggler handling)."""

        async def _hang():
            await asyncio.sleep(30)

        task = asyncio.create_task(_hang())
        done, pending, timed_out = await _await_next_within_budget({task}, remaining=0.05)

        assert done == set()
        assert pending == set()
        assert task in timed_out
        assert task.cancelled()

    async def test_zero_budget_short_circuits_without_awaiting(self):
        """Remaining<=0 must not block — it immediately times out any pending work."""

        async def _hang():
            await asyncio.sleep(30)

        task = asyncio.create_task(_hang())
        done, pending, timed_out = await _await_next_within_budget({task}, remaining=0.0)

        assert task in timed_out
        assert task.cancelled()

    async def test_mixed_fast_then_slow(self):
        """A fast task is delivered first; a later round with a spent budget times out
        the straggler."""

        async def _quick():
            return 1

        async def _hang():
            await asyncio.sleep(30)

        fast = asyncio.create_task(_quick())
        slow = asyncio.create_task(_hang())
        done, pending, timed_out = await _await_next_within_budget({fast, slow}, remaining=5.0)
        assert fast in done
        assert slow in pending
        assert timed_out == set()

        # Next round with no budget left cancels the straggler.
        done2, pending2, timed_out2 = await _await_next_within_budget(pending, remaining=0.0)
        assert slow in timed_out2
        assert slow.cancelled()


# ── stream_search_mpn end-to-end with a hung connector ───────────────────────


class TestStreamingBudgetIntegration:
    @pytest.fixture(autouse=True)
    def _own_session(self, db_session):
        with patch("app.search_service.SessionLocal", lambda: db_session):
            yield

    @pytest.fixture()
    def _tiny_budget(self):
        from app.config import settings

        original = settings.search_total_timeout_s
        settings.search_total_timeout_s = 0.1
        yield
        settings.search_total_timeout_s = original

    async def test_hung_connector_times_out_publishes_chip_done_and_telemetry(self, db_session: Session, _tiny_budget):
        """A connector that never returns is cancelled at the budget; the loop publishes
        an error chip for it, the terminal `done`, and records the error in
        ApiSource."""
        src = ApiSource(
            name="brokerbin",
            display_name="BrokerBin",
            category="market_data",
            source_type="api",
            status="live",
            env_vars=["BROKERBIN_API_KEY"],
            total_searches=0,
            total_results=0,
            avg_response_ms=0,
        )
        db_session.add(src)
        db_session.commit()

        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        hung = MagicMock()
        hung.source_name = "brokerbin"
        hung.search = lambda pn: asyncio.sleep(30)  # never completes within the budget

        with (
            patch("app.search_service._build_connectors", return_value=([hung], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
        ):
            await asyncio.wait_for(stream_search_mpn("search-budget-1", "LM317T"), timeout=5.0)

        events = [(c[0][1], c[0][2]) for c in mock_broker.publish.call_args_list]
        kinds = [e[0] for e in events]
        assert "done" in kinds, f"expected terminal done event, got {kinds}"

        # The hung source got an error/timeout chip.
        status_chips = [json.loads(payload) for kind, payload in events if kind == "source-status"]
        timeout_chip = [c for c in status_chips if c.get("error") == "search budget exceeded"]
        assert timeout_chip, f"expected a budget-exceeded chip, got {status_chips}"
        assert timeout_chip[0]["source"] == "brokerbin"
        assert timeout_chip[0]["status"] == "error"

        # Telemetry recorded the failure in ApiSource (was: zero telemetry on this path).
        # stream_search_mpn closes its own session in finally, so re-query fresh.
        reloaded = db_session.query(ApiSource).filter_by(name="brokerbin").first()
        assert reloaded.last_error == "search budget exceeded"
        assert reloaded.total_searches == 1

    async def test_fast_connector_records_success_telemetry(self, db_session: Session, _tiny_budget):
        """A connector that returns quickly records a success in ApiSource telemetry."""
        src = ApiSource(
            name="brokerbin",
            display_name="BrokerBin",
            category="market_data",
            source_type="api",
            status="live",
            env_vars=["BROKERBIN_API_KEY"],
            total_searches=0,
            total_results=0,
            avg_response_ms=0,
        )
        db_session.add(src)
        db_session.commit()

        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        conn = MagicMock()
        conn.source_name = "brokerbin"
        conn.search = AsyncMock(return_value=[{"mpn_matched": "LM317T", "vendor_name": "Arrow", "qty_available": 5}])

        with (
            patch("app.search_service._build_connectors", return_value=([conn], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
            patch("app.search_service._render_search_vendor_cards_html", return_value="<div></div>"),
            patch("app.search_service._incremental_dedup", return_value=([], [])),
            patch("app.search_service._score_raw_hit", side_effect=lambda r, vm: r),
        ):
            await asyncio.wait_for(stream_search_mpn("search-budget-2", "LM317T"), timeout=5.0)

        reloaded = db_session.query(ApiSource).filter_by(name="brokerbin").first()
        assert reloaded.total_searches == 1
        assert reloaded.last_error is None
        assert reloaded.last_success is not None


# ── FIX A pair: Retry-After cap 300s -> 30s -> 8s ────────────────────────────


class TestRetryAfterCap:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("600", 8.0),  # multi-minute upstream advertisement is capped
            ("9", 8.0),
            ("8", 8.0),
            ("5", 5.0),  # under the cap is unchanged
            ("0.1", 1.0),  # floor still 1.0
        ],
    )
    def test_header_capped_at_8(self, header, expected):
        from app.connectors.sources import _parse_retry_after

        resp = MagicMock()
        resp.headers = {"Retry-After": header}
        assert _parse_retry_after(resp) == expected
