"""test_qcm_graph_sent_lookup.py — Graph sent-message lookup fixes.

Two 2026-08-08 QC findings (Graph behavior is always mocked in CI — these assert
query CONSTRUCTION + Python-side filtering, not live Graph):
- search_sent_messages built a Graph-invalid OData query (contains() on body/content
  + $filter combined with $orderby) so PO sent-detection errored every call.
- _find_sent_message had no time floor, so a retry could match a PRIOR campaign's
  message sharing the subject+recipient.

Called by: pytest
Depends on: app.utils.graph_client, app.email_service
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.anyio
async def test_search_sent_messages_uses_search_not_filter_orderby():
    from app.utils.graph_client import GraphClient

    gc = GraphClient("tok")
    captured: dict = {}

    async def _get(path, params=None):
        captured["params"] = params
        return {
            "value": [
                {"id": "a", "sentDateTime": "2026-08-10T00:00:00Z"},
                {"id": "b", "sentDateTime": "2026-08-13T00:00:00Z"},
            ]
        }

    gc.get_json = _get
    out = await gc.search_sent_messages("PO-123")
    p = captured["params"]
    assert p["$search"] == '"PO-123"'
    assert "$filter" not in p and "$orderby" not in p  # the Graph-invalid pair is gone
    assert [m["id"] for m in out] == ["b", "a"]  # sorted newest-first in Python


def _msg(mid, when, subj="RFQ X", addr="v@x.com"):
    return {"id": mid, "subject": subj, "sentDateTime": when, "toRecipients": [{"emailAddress": {"address": addr}}]}


@pytest.mark.anyio
async def test_find_sent_message_time_floor_excludes_prior_campaign():
    from app import email_service

    gc = AsyncMock()
    gc.get_json = AsyncMock(
        return_value={"value": [_msg("old", "2026-08-01T12:00:00Z"), _msg("new", "2026-08-13T12:00:00Z")]}
    )
    floor = datetime(2026, 8, 10, tzinfo=UTC)
    with patch("app.email_service.asyncio.sleep", new=AsyncMock()):
        with_floor = await email_service._find_sent_message(gc, "RFQ X", "v@x.com", sent_after=floor)
        no_floor = await email_service._find_sent_message(gc, "RFQ X", "v@x.com")
    assert with_floor["id"] == "new"  # the pre-floor "old" is skipped
    assert no_floor["id"] == "old"  # default: first match wins → proves the floor changed behavior


def test_sent_at_or_after_helper():
    from app.email_service import _sent_at_or_after

    floor = datetime(2026, 8, 10, tzinfo=UTC)
    assert _sent_at_or_after("2026-08-13T00:00:00Z", floor) is True
    assert _sent_at_or_after("2026-08-01T00:00:00Z", floor) is False
    assert _sent_at_or_after(None, floor) is True  # unknown → don't exclude
    assert _sent_at_or_after("garbage", floor) is True
    assert _sent_at_or_after("2026-08-13T00:00:00Z", datetime(2026, 8, 10)) is True  # naive floor coerced
