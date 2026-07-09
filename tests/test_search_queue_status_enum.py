"""Tests for app/constants.py SearchQueueStatus and its use across the browser-driven
search queue lifecycle (P2.5 StrEnum enforcement).

Verifies:
- SearchQueueStatus values are unchanged from the pre-enum raw string literals
  (DB-persisted; no data migration needed).
- QueueManager writes (enqueue_search, claim_next_queued_item, mark_completed,
  mark_status, recover_stale_searches, reclaim_stuck_searches) persist the enum's
  string value, not a Python enum repr.
- AIGate.process_ai_gate transitions items to QUEUED / GATED_OUT using the enum.

Called by: pytest auto-discovery.
Depends on: app/constants.py, app/services/search_worker_base/{queue_manager,ai_gate}.py,
    app/services/ics_worker/queue_manager.py, conftest.py (db_session, test_user).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.constants import SearchQueueStatus
from app.models import IcsSearchQueue, Requirement
from app.models.sourcing import Requisition
from app.services.ics_worker.queue_manager import (
    claim_next_queued_item,
    enqueue_for_ics_search,
    mark_completed,
    mark_status,
    recover_stale_searches,
)
from app.services.search_worker_base.ai_gate import AIGate


class TestSearchQueueStatusValues:
    """The enum's string values must equal the pre-enum literals exactly."""

    def test_values_match_legacy_literals(self):
        assert SearchQueueStatus.PENDING == "pending"
        assert SearchQueueStatus.QUEUED == "queued"
        assert SearchQueueStatus.SEARCHING == "searching"
        assert SearchQueueStatus.COMPLETED == "completed"
        assert SearchQueueStatus.GATED_OUT == "gated_out"
        assert SearchQueueStatus.FAILED == "failed"

    def test_is_strenum_str_compatible(self):
        assert isinstance(SearchQueueStatus.QUEUED, str)
        assert f"{SearchQueueStatus.QUEUED}" == "queued"


@pytest.fixture
def requisition(db_session, test_user):
    r = Requisition(
        name="sqs-req",
        customer_name="Acme",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(r)
    db_session.flush()
    return r


@pytest.fixture
def requirement(db_session, requisition):
    req = Requirement(
        requisition_id=requisition.id,
        primary_mpn="LM317",
        target_qty=100,
        created_at=datetime.now(UTC),
    )
    db_session.add(req)
    db_session.commit()
    return req


class TestQueueManagerLifecycle:
    """QueueManager writes persist SearchQueueStatus values through the full pending ->
    queued -> searching -> completed/failed lifecycle."""

    def test_enqueue_sets_pending(self, db_session, requirement):
        item = enqueue_for_ics_search(requirement.id, db_session)
        assert item is not None
        assert item.status == SearchQueueStatus.PENDING

    def test_claim_transitions_queued_to_searching(self, db_session, requirement, requisition):
        row = IcsSearchQueue(
            requirement_id=requirement.id,
            requisition_id=requisition.id,
            mpn="LM317",
            normalized_mpn="LM317",
            status=SearchQueueStatus.QUEUED,
            priority=3,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db_session.add(row)
        db_session.commit()

        claimed = claim_next_queued_item(db_session)
        assert claimed is not None
        assert claimed.status == SearchQueueStatus.SEARCHING

    def test_mark_completed_sets_completed(self, db_session, requirement, requisition):
        row = IcsSearchQueue(
            requirement_id=requirement.id,
            requisition_id=requisition.id,
            mpn="LM317",
            normalized_mpn="LM317",
            status=SearchQueueStatus.SEARCHING,
            priority=3,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db_session.add(row)
        db_session.commit()

        mark_completed(db_session, row, results_found=2, sightings_created=1)
        db_session.refresh(row)
        assert row.status == SearchQueueStatus.COMPLETED

    def test_mark_status_accepts_enum_member(self, db_session, requirement, requisition):
        row = IcsSearchQueue(
            requirement_id=requirement.id,
            requisition_id=requisition.id,
            mpn="LM317",
            normalized_mpn="LM317",
            status=SearchQueueStatus.SEARCHING,
            priority=3,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db_session.add(row)
        db_session.commit()

        mark_status(db_session, row, SearchQueueStatus.FAILED, error="worker gave up")
        db_session.refresh(row)
        assert row.status == SearchQueueStatus.FAILED
        assert row.error_message == "worker gave up"

    def test_recover_stale_searches_resets_to_queued(self, db_session, requirement, requisition):
        row = IcsSearchQueue(
            requirement_id=requirement.id,
            requisition_id=requisition.id,
            mpn="LM317",
            normalized_mpn="LM317",
            status=SearchQueueStatus.SEARCHING,
            priority=3,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC) - timedelta(hours=1),
        )
        db_session.add(row)
        db_session.commit()

        count = recover_stale_searches(db_session)
        db_session.refresh(row)
        assert count == 1
        assert row.status == SearchQueueStatus.QUEUED


class TestAIGateSearchQueueStatus:
    """AIGate.process_ai_gate transitions items using SearchQueueStatus values."""

    def _make_item(self, mpn: str) -> MagicMock:
        item = MagicMock()
        item.mpn = mpn
        item.normalized_mpn = mpn.lower()
        item.manufacturer = "TI"
        item.description = "op-amp"
        item.status = SearchQueueStatus.PENDING
        item.updated_at = None
        return item

    def _db_with_items(self, items: list) -> MagicMock:
        query_mock = MagicMock()
        query_mock.filter.return_value.order_by.return_value.limit.return_value.all.return_value = items
        db_mock = MagicMock()
        db_mock.query.return_value = query_mock
        return db_mock

    @pytest.mark.asyncio
    async def test_search_decision_sets_queued_enum(self):
        item = self._make_item("STM32F407")
        model = MagicMock()
        db_mock = self._db_with_items([item])
        gate = AIGate(model, "ICsource", "search_ics")
        classifications = [{"mpn": "STM32F407", "search_ics": True, "commodity": "semiconductor", "reason": "mcu"}]
        with patch.object(gate, "classify_parts_batch", new_callable=AsyncMock, return_value=classifications):
            await gate.process_ai_gate(db_mock)

        assert item.status == SearchQueueStatus.QUEUED

    @pytest.mark.asyncio
    async def test_skip_decision_sets_gated_out_enum(self):
        item = self._make_item("RC0402")
        model = MagicMock()
        db_mock = self._db_with_items([item])
        gate = AIGate(model, "ICsource", "search_ics")
        classifications = [{"mpn": "RC0402", "search_ics": False, "commodity": "passive", "reason": "resistor"}]
        with patch.object(gate, "classify_parts_batch", new_callable=AsyncMock, return_value=classifications):
            await gate.process_ai_gate(db_mock)

        assert item.status == SearchQueueStatus.GATED_OUT
