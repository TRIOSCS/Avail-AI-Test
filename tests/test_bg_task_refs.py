"""tests/test_bg_task_refs.py — Regression tests for P0.4: fire-and-forget asyncio
tasks must be held in a strong reference until completion, otherwise the event loop
can garbage-collect them mid-flight and silently drop the work.

Targets:
  - app/email_service.py::_auto_create_offers_from_parse (SSE "sighting-updated" publish)
  - app/services/prepayment_notifications.py::schedule_prepayment_notify

Called by: pytest autodiscovery
Depends on: app.email_service, app.services.prepayment_notifications, tests.conftest
"""

import asyncio
import gc
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

os.environ["TESTING"] = "1"

from sqlalchemy.orm import Session

from app.email_service import _auto_create_offers_from_parse
from app.email_service import _bg_tasks as email_bg_tasks
from app.models import Requisition, User, VendorResponse
from app.services import prepayment_notifications
from tests.conftest import engine  # noqa: F401


def _make_vendor_response(db: Session, user: User, requisition: Requisition, confidence: float = 0.9) -> VendorResponse:
    vr = VendorResponse(
        requisition_id=requisition.id,
        vendor_name="TestVendor Inc",
        vendor_email="sales@testvendor.com",
        confidence=confidence,
        scanned_by_user_id=user.id,
        status="new",
        received_at=datetime.now(timezone.utc),
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
        """The SSE 'sighting-updated' publish task must run to completion even
        under aggressive GC — it is held via app.email_service._bg_tasks."""
        vr = _make_vendor_response(db_session, test_user, test_requisition)
        draft = {"mpn": "LM317T", "vendor_name": "TestVendor Inc"}
        parsed = {"confidence": 0.9}

        mock_broker = AsyncMock()

        with (
            patch("app.services.response_parser.extract_draft_offers", return_value=[draft]),
            patch("app.evidence_tiers.tier_for_parsed_offer", return_value=1),
            patch("app.services.task_service.on_email_offer_parsed"),
            patch("app.services.knowledge_service.capture_offer_fact"),
            patch("app.services.sse_broker.broker", mock_broker),
        ):
            _auto_create_offers_from_parse(vr, parsed, db_session)
            # The task was registered for retention before we let the loop run.
            assert len(email_bg_tasks) == 1
            gc.collect()
            # Let the event loop actually execute the scheduled task.
            for _ in range(5):
                await asyncio.sleep(0)

        mock_broker.publish.assert_awaited_once()
        # Done-callback must clean up the retention set once the task finishes.
        assert len(email_bg_tasks) == 0


class TestSchedulePrepaymentNotifyTaskRetained:
    async def test_scheduled_task_survives_gc_and_completes(self):
        """schedule_prepayment_notify's task must run to completion even under
        aggressive GC — it is held via prepayment_notifications._bg_tasks."""
        ran = AsyncMock()

        async def _coro():
            await ran()

        loop = asyncio.get_event_loop()
        with patch("asyncio.get_running_loop", return_value=loop):
            prepayment_notifications.schedule_prepayment_notify(_coro())

        assert len(prepayment_notifications._bg_tasks) == 1
        gc.collect()
        for _ in range(5):
            await asyncio.sleep(0)

        ran.assert_awaited_once()
        assert len(prepayment_notifications._bg_tasks) == 0
