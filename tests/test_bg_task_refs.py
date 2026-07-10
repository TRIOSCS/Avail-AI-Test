"""tests/test_bg_task_refs.py — Regression tests for P0.4: fire-and-forget asyncio tasks
must be held in a strong reference until completion, otherwise the event loop can
garbage-collect them mid-flight and silently drop the work.

Targets:
  - app/email_service.py::_auto_create_offers_from_parse (SSE "sighting-updated" publish)
  - app/services/prepayment_notifications.py::schedule_prepayment_notify
  - app/utils/async_helpers.py::hold_bg_task (the shared canonical retention set)

Called by: pytest autodiscovery
Depends on: app.email_service, app.services.prepayment_notifications,
    app.utils.async_helpers, tests.conftest

Note: never assert an exact size on the shared `async_helpers._bg_tasks` set — other
tests running on the same xdist worker can leave a task in flight, so assertions here
capture membership before the action and assert the DELTA (the new task appears, then
drains once the event loop processes it).
"""

import asyncio
import gc
import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

os.environ["TESTING"] = "1"

from sqlalchemy.orm import Session

from app.constants import VendorResponseStatus
from app.email_service import _auto_create_offers_from_parse
from app.models import Requisition, User, VendorResponse
from app.services import prepayment_notifications
from app.utils import async_helpers
from tests.conftest import engine  # noqa: F401


def _make_vendor_response(db: Session, user: User, requisition: Requisition, confidence: float = 0.9) -> VendorResponse:
    vr = VendorResponse(
        requisition_id=requisition.id,
        vendor_name="TestVendor Inc",
        vendor_email="sales@testvendor.com",
        confidence=confidence,
        scanned_by_user_id=user.id,
        status=VendorResponseStatus.NEW,
        received_at=datetime.now(UTC),
        message_id=f"msg-bgtask-{id(requisition)}",
    )
    db.add(vr)
    db.commit()
    db.refresh(vr)
    return vr


class TestEmailServiceSSEPublishTaskRetained:
    async def test_sse_publish_task_survives_gc_and_completes(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """The SSE 'sighting-updated' publish task must run to completion even under
        aggressive GC — it is held via app.utils.async_helpers.hold_bg_task()."""
        vr = _make_vendor_response(db_session, test_user, test_requisition)
        draft = {"mpn": "LM317T", "vendor_name": "TestVendor Inc"}
        parsed = {"confidence": 0.9}

        mock_broker = AsyncMock()
        before = set(async_helpers._bg_tasks)

        with (
            patch("app.services.response_parser.extract_draft_offers", return_value=[draft]),
            patch("app.evidence_tiers.tier_for_parsed_offer", return_value=1),
            patch("app.services.task_service.on_email_offer_parsed"),
            patch("app.services.knowledge_service.capture_offer_fact"),
            patch("app.services.sse_broker.broker", mock_broker),
        ):
            _auto_create_offers_from_parse(vr, parsed, db_session)
            # The task was registered for retention before we let the loop run.
            new_tasks = async_helpers._bg_tasks - before
            assert len(new_tasks) == 1
            task = next(iter(new_tasks))
            gc.collect()
            # Let the event loop actually execute the scheduled task.
            await task

        mock_broker.publish.assert_awaited_once()
        # Done-callback must clean up the retention set once the task finishes.
        assert task not in async_helpers._bg_tasks


class TestSchedulePrepaymentNotifyTaskRetained:
    async def test_scheduled_task_survives_gc_and_completes(self):
        """schedule_prepayment_notify's task must run to completion even under
        aggressive GC — it is held via app.utils.async_helpers.hold_bg_task()."""
        ran = AsyncMock()

        async def _coro():
            await ran()

        loop = asyncio.get_event_loop()
        before = set(async_helpers._bg_tasks)
        with patch("asyncio.get_running_loop", return_value=loop):
            prepayment_notifications.schedule_prepayment_notify(_coro())

        new_tasks = async_helpers._bg_tasks - before
        assert len(new_tasks) == 1
        task = next(iter(new_tasks))
        gc.collect()
        await task

        ran.assert_awaited_once()
        assert task not in async_helpers._bg_tasks
