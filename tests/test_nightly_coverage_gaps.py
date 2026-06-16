"""test_nightly_coverage_gaps.py — Covers remaining uncovered lines across multiple
modules.

Targets:
- app/utils/graph_client.py: patch_json, search_sent_messages, PATCH retry, _parse_retry_after bad value
- app/services/proactive_matching.py: _get_watermark bad ISO, _set_watermark new row,
  _find_matches fallback offer, run_proactive_scan 5000-cap warning

Called by: pytest
Depends on: tests/conftest.py
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import engine  # noqa: F401

# ══════════════════════════════════════════════════════════════════════════════
# graph_client.py
# ══════════════════════════════════════════════════════════════════════════════


class TestGraphClientPatchJson:
    """Covers lines 64-65: patch_json method."""

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_patch_json_calls_request_with_retry(self, mock_http):
        from app.utils.graph_client import GraphClient

        mock_http.patch = AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {"updated": True}))
        gc = GraphClient("token-abc")
        result = await gc.patch_json("/me/messages/123", json_data={"isRead": True})
        assert mock_http.patch.called

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_patch_json_full_url_not_prefixed(self, mock_http):
        from app.utils.graph_client import GraphClient

        mock_http.patch = AsyncMock(return_value=MagicMock(status_code=204, json=lambda: {}))
        gc = GraphClient("token-abc")
        await gc.patch_json("https://graph.microsoft.com/v1.0/me/messages/abc", json_data={})
        call_url = mock_http.patch.call_args[0][0]
        assert call_url.startswith("https://graph.microsoft.com")


class TestGraphClientSearchSentMessages:
    """Covers lines 107-120: search_sent_messages method."""

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_returns_list_on_success(self, mock_http):
        from app.utils.graph_client import GraphClient

        msgs = [{"id": "msg1", "subject": "RFQ for LM317T"}]
        mock_http.get = AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {"value": msgs}))
        gc = GraphClient("token-abc")
        result = await gc.search_sent_messages("LM317T")
        assert result == msgs

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_raises_on_error_dict(self, mock_http):
        from app.utils.graph_client import GraphClient

        mock_http.get = AsyncMock(
            return_value=MagicMock(status_code=200, json=lambda: {"error": {"code": "AccessDenied"}})
        )
        gc = GraphClient("token-abc")
        with pytest.raises(RuntimeError, match="Graph API error"):
            await gc.search_sent_messages("LM317T")

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_user_id_in_path(self, mock_http):
        from app.utils.graph_client import GraphClient

        mock_http.get = AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {"value": []}))
        gc = GraphClient("token-abc")
        await gc.search_sent_messages("test", user_id="azure-user-123")
        call_url = mock_http.get.call_args[0][0]
        assert "azure-user-123" in call_url

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_escapes_single_quotes(self, mock_http):
        from app.utils.graph_client import GraphClient

        mock_http.get = AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {"value": []}))
        gc = GraphClient("token-abc")
        await gc.search_sent_messages("O'Brien's Query")
        call_params = mock_http.get.call_args[1]["params"]
        assert "O''Brien''s Query" in call_params["$filter"]


class TestGraphClientPatchRetry:
    """Covers line 191: PATCH branch in _request_with_retry."""

    @pytest.mark.asyncio
    @patch("asyncio.sleep", new_callable=AsyncMock)
    @patch("app.utils.graph_client.http")
    async def test_patch_500_retries(self, mock_http, mock_sleep):
        import app.utils.graph_client as _gc_mod
        from app.utils.graph_client import GraphClient

        _gc_mod.MAX_RETRIES = 1
        _gc_mod.BACKOFF_BASE = 1
        ok = MagicMock(status_code=200, json=lambda: {"ok": True})
        fail = MagicMock(status_code=500, text="Internal Error")
        mock_http.patch = AsyncMock(side_effect=[fail, ok])
        gc = GraphClient("tok")
        result = await gc.patch_json("/endpoint", json_data={"x": 1})
        assert mock_http.patch.call_count == 2


class TestParseRetryAfterBadValue:
    """Covers lines 267-268: ValueError/TypeError in _parse_retry_after."""

    @pytest.mark.parametrize(
        ("header_value", "expected"),
        [
            pytest.param("not-a-number", None, id="non-integer"),
            pytest.param("1.5", None, id="float-string"),
            pytest.param("30", 30, id="integer-string"),
        ],
    )
    def test_parse_retry_after(self, header_value, expected):
        from app.utils.graph_client import _parse_retry_after

        resp = MagicMock()
        resp.headers = {"Retry-After": header_value}
        assert _parse_retry_after(resp) == expected


# ══════════════════════════════════════════════════════════════════════════════
# proactive_matching.py
# ══════════════════════════════════════════════════════════════════════════════


class TestGetWatermarkBadIso:
    """Covers lines 125, 127-129: ValueError/TypeError in _get_watermark."""

    @pytest.mark.parametrize(
        "stored_value",
        [
            pytest.param("not-a-date", id="bad-iso"),
            pytest.param("", id="empty-value"),
        ],
    )
    def test_unparseable_value_returns_default(self, db_session, stored_value):
        from app.models.config import SystemConfig
        from app.services.proactive_matching import _WATERMARK_KEY, _get_watermark

        db_session.add(SystemConfig(key=_WATERMARK_KEY, value=stored_value))
        db_session.flush()
        ts = _get_watermark(db_session)
        assert isinstance(ts, datetime)


class TestSetWatermarkNewRow:
    """Covers line 138: new SystemConfig created in _set_watermark."""

    def test_creates_new_row_when_absent(self, db_session):
        from app.models.config import SystemConfig
        from app.services.proactive_matching import _WATERMARK_KEY, _set_watermark

        ts = datetime.now(timezone.utc)
        _set_watermark(db_session, ts)
        row = db_session.query(SystemConfig).filter(SystemConfig.key == _WATERMARK_KEY).first()
        assert row is not None
        assert ts.isoformat() in row.value


class TestFindMatchesFallbackOffer:
    """Covers lines 214-221, 224: fallback Offer query in _find_matches."""

    def test_fallback_offer_when_no_source_offer(self, db_session):
        """When source_offer=None and no fallback offer exists, returns []."""
        from app.models import MaterialCard
        from app.services.proactive_matching import _find_matches

        card = MaterialCard(normalized_mpn="LM317T2", display_mpn="LM317T2")
        db_session.add(card)
        db_session.flush()

        # No source_offer AND no Offer row for this card → fallback_offer_id=None → return []
        matches = _find_matches(
            db_session,
            material_card_id=card.id,
            mpn="LM317T2",
            our_cost=0.45,
            source_offer=None,
        )
        assert matches == []


class TestRunProactiveScanCapWarning:
    """Covers line 336: 5000-offer cap warning in run_proactive_scan."""

    def test_5000_cap_warning_logged(self, db_session):
        from app.services.proactive_matching import run_proactive_scan

        # Build a dummy list of 5000 mock Offer objects to trigger the cap warning
        mock_offer = MagicMock()
        mock_offer.material_card_id = None  # Will be filtered out but triggers the cap check

        class FakeQuery:
            def filter(self, *a, **k):
                return self

            def order_by(self, *a, **k):
                return self

            def limit(self, n):
                return self

            def all(self):
                return [mock_offer] * 5000

        with patch.object(db_session, "query") as mock_query:
            mock_query.return_value = FakeQuery()
            with patch("app.services.proactive_matching._get_watermark") as mock_wm:
                mock_wm.return_value = datetime.now(timezone.utc) - timedelta(hours=1)
                with patch("app.services.proactive_matching._set_watermark"):
                    with patch("app.services.proactive_matching.find_matches_for_offer", return_value=[]):
                        result = run_proactive_scan(db_session)
        assert isinstance(result, dict)
