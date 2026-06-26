"""Tests for NC Phase 3: AI Commodity Gate.

Called by: pytest
Depends on: conftest.py, nc_worker.ai_gate
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models import NcSearchQueue, Requirement, Requisition
from app.services.nc_worker.ai_gate import (
    classify_parts_batch,
    clear_classification_cache,
    process_ai_gate,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear classification cache before each test."""
    clear_classification_cache()
    yield
    clear_classification_cache()


def _make_queue_item(db_session, test_user, mpn, manufacturer=None, index=0):
    """Helper to create a pending queue item for testing."""
    req = Requisition(
        name=f"REQ-GATE-{mpn}-{index}",
        customer_name="Test",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    r = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        brand=manufacturer,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(r)
    db_session.flush()
    q = NcSearchQueue(
        requirement_id=r.id,
        requisition_id=req.id,
        mpn=mpn,
        normalized_mpn=mpn.upper().strip(),
        manufacturer=manufacturer,
        status="pending",
    )
    db_session.add(q)
    db_session.commit()
    return q


def test_classify_parts_batch_returns_valid_json(db_session):
    """classify_parts_batch returns valid classification list (mocked)."""
    mock_response = {
        "classifications": [
            {"mpn": "STM32F103C8T6", "search_nc": True, "commodity": "semiconductor", "reason": "MCU IC"},
            {"mpn": "RC0805FR-071KL", "search_nc": False, "commodity": "passive", "reason": "Standard resistor"},
        ]
    }

    with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock) as mock_claude:
        mock_claude.return_value = mock_response
        parts = [
            {"mpn": "STM32F103C8T6", "manufacturer": "STMicroelectronics", "description": "MCU"},
            {"mpn": "RC0805FR-071KL", "manufacturer": "Yageo", "description": "Resistor 1K"},
        ]
        result = asyncio.get_event_loop().run_until_complete(classify_parts_batch(parts))

    assert result is not None
    assert len(result) == 2
    assert result[0]["search_nc"] is True
    assert result[1]["search_nc"] is False


def test_classify_empty_batch():
    """Empty parts list returns empty list."""
    result = asyncio.get_event_loop().run_until_complete(classify_parts_batch([]))
    assert result == []


def test_classify_api_failure():
    """API failure returns None."""
    with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock) as mock_claude:
        mock_claude.side_effect = Exception("API timeout")
        parts = [{"mpn": "TEST", "manufacturer": "", "description": ""}]
        result = asyncio.get_event_loop().run_until_complete(classify_parts_batch(parts))
    assert result is None


def test_process_ai_gate_classifies_pending(db_session, test_user):
    """process_ai_gate classifies pending items and updates status."""
    q1 = _make_queue_item(db_session, test_user, "STM32F103C8T6", "STM", index=0)
    q2 = _make_queue_item(db_session, test_user, "RC0805FR-071KL", "Yageo", index=1)

    mock_response = {
        "classifications": [
            {"mpn": "STM32F103C8T6", "search_nc": True, "commodity": "semiconductor", "reason": "MCU IC"},
            {"mpn": "RC0805FR-071KL", "search_nc": False, "commodity": "passive", "reason": "Chip resistor"},
        ]
    }

    with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock) as mock_claude:
        mock_claude.return_value = mock_response
        asyncio.get_event_loop().run_until_complete(process_ai_gate(db_session))

    db_session.refresh(q1)
    db_session.refresh(q2)

    assert q1.status == "queued"
    assert q1.gate_decision == "search"
    assert q1.commodity_class == "semiconductor"

    assert q2.status == "gated_out"
    assert q2.gate_decision == "skip"
    assert q2.commodity_class == "passive"


def test_process_ai_gate_cache_hit(db_session, test_user):
    """Second call for same MPN uses cache — no API call."""
    q1 = _make_queue_item(db_session, test_user, "STM32F103C8T6", "STM", index=0)

    mock_response = {
        "classifications": [
            {"mpn": "STM32F103C8T6", "search_nc": True, "commodity": "semiconductor", "reason": "MCU IC"},
        ]
    }

    with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock) as mock_claude:
        mock_claude.return_value = mock_response
        asyncio.get_event_loop().run_until_complete(process_ai_gate(db_session))

    db_session.refresh(q1)
    assert q1.status == "queued"

    # Create second item with same MPN
    q2 = _make_queue_item(db_session, test_user, "STM32F103C8T6", "STM", index=1)

    with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock) as mock_claude:
        mock_claude.return_value = {"classifications": []}  # Should not be called
        asyncio.get_event_loop().run_until_complete(process_ai_gate(db_session))

    db_session.refresh(q2)
    assert q2.status == "queued"
    assert "[cached]" in q2.gate_reason
    # API should not have been called for the cached item
    mock_claude.assert_not_called()


def test_process_ai_gate_no_pending(db_session):
    """No pending items = no-op, no API call."""
    with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock) as mock_claude:
        asyncio.get_event_loop().run_until_complete(process_ai_gate(db_session))
    mock_claude.assert_not_called()


def test_process_ai_gate_api_failure_fail_open(db_session, test_user):
    """If API fails, items are set to 'queued' (fail-open so items aren't stuck)."""
    q1 = _make_queue_item(db_session, test_user, "AD8232ACPZ", index=0)

    with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock) as mock_claude:
        mock_claude.return_value = None
        asyncio.get_event_loop().run_until_complete(process_ai_gate(db_session))

    db_session.refresh(q1)
    assert q1.status == "queued"  # Fail-open: defaults to search
