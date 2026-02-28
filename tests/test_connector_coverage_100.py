"""Tests targeting specific coverage gaps in connectors, file_utils, and vendor_utils.

Covers missing lines in:
  - app/connectors/element14.py (lines 33-47)
  - app/connectors/email_mining.py (lines 157, 171-184, 380, 586, 605, 666, 820, 824, 837)
  - app/connectors/mouser.py (line 51)
  - app/connectors/oemsecrets.py (line 49)
  - app/connectors/sourcengine.py (lines 26-40)
  - app/connectors/sources.py (lines 98, 335)
  - app/file_utils.py (lines 31-32)
  - app/vendor_utils.py (line 195)
"""

import asyncio
import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _mock_response(status_code=200, json_data=None, text=""):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text or str(json_data)
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    return resp


# ========================================================================
#  Element14 -- lines 33-47: _do_search with valid API key
# ========================================================================

class TestElement14DoSearchGap:
    @pytest.mark.asyncio
    async def test_do_search_success(self):
        from app.connectors.element14 import Element14Connector
        c = Element14Connector(api_key="test-key")
        resp_data = {
            "manufacturerPartNumberSearchReturn": {
                "products": [{
                    "translatedManufacturerPartNumber": "LM317T",
                    "brandName": "TI",
                    "displayName": "Voltage Reg",
                    "sku": "123",
                    "stock": {"level": "100"},
                    "prices": [{"cost": "0.50"}],
                }]
            }
        }
        resp = _mock_response(200, resp_data)
        with patch("app.connectors.element14.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp)
            results = await c._do_search("LM317T")
            assert len(results) == 1
            assert results[0]["mpn_matched"] == "LM317T"
            mock_http.get.assert_called_once()


# ========================================================================
#  Mouser -- line 51: _do_search success path
# ========================================================================

class TestMouserDoSearchGap:
    @pytest.mark.asyncio
    async def test_do_search_no_errors(self):
        from app.connectors.mouser import MouserConnector
        c = MouserConnector(api_key="test-key")
        resp_data = {
            "SearchResults": {
                "Parts": [{
                    "ManufacturerPartNumber": "LM317T",
                    "Manufacturer": "TI",
                    "MouserPartNumber": "595-LM317T",
                    "Availability": "100 In Stock",
                    "PriceBreaks": [{"Quantity": 1, "Price": "$0.89"}],
                    "ProductDetailUrl": "https://mouser.com/x",
                    "Description": "Reg",
                }]
            }
        }
        resp = _mock_response(200, resp_data)
        with patch("app.connectors.mouser.http") as mock_http:
            mock_http.post = AsyncMock(return_value=resp)
            results = await c._do_search("LM317T")
            assert len(results) == 1
            assert results[0]["mpn_matched"] == "LM317T"


# ========================================================================
#  OEMSecrets -- line 49: _do_search success path
# ========================================================================

class TestOEMSecretsDoSearchGap:
    @pytest.mark.asyncio
    async def test_do_search_200_json(self):
        from app.connectors.oemsecrets import OEMSecretsConnector
        c = OEMSecretsConnector(api_key="test-key")
        resp_data = {
            "stock": [{
                "distributor": {"name": "Arrow"},
                "mpn": "LM317T",
                "stock": 500,
                "price": 0.65,
            }]
        }
        resp = _mock_response(200, resp_data)
        with patch("app.connectors.oemsecrets.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp)
            results = await c._do_search("LM317T")
            assert len(results) == 1
            assert results[0]["vendor_name"] == "Arrow"


# ========================================================================
#  Sourcengine -- lines 26-40: _do_search with valid API key
# ========================================================================

class TestSourcengineDoSearchGap:
    @pytest.mark.asyncio
    async def test_do_search_success(self):
        from app.connectors.sourcengine import SourcengineConnector
        c = SourcengineConnector(api_key="test-key")
        resp_data = {
            "results": [{
                "supplier": {"name": "Future"},
                "mpn": "LM317T",
                "quantity": 1000,
                "unit_price": 0.55,
            }]
        }
        resp = _mock_response(200, resp_data)
        with patch("app.connectors.sourcengine.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp)
            results = await c._do_search("LM317T")
            assert len(results) == 1
            assert results[0]["vendor_name"] == "Future"


# ========================================================================
#  sources.py -- line 98 (abstract pass) and line 335 (_parse_full call)
# ========================================================================

class TestSourcesGaps:
    @pytest.mark.asyncio
    async def test_abstract_do_search_pass(self):
        from app.connectors.sources import BaseConnector
        result = await BaseConnector._do_search(MagicMock(), "PN")
        assert result is None

    @pytest.mark.asyncio
    async def test_nexar_do_search_aggregate_query_success(self):
        """_do_search skips full sellers query (DISTRIBUTOR role) and goes to aggregate."""
        from app.connectors.sources import NexarConnector
        c = NexarConnector(client_id="id", client_secret="secret")
        agg_resp = {
            "data": {
                "supSearchMpn": {
                    "results": [{
                        "part": {
                            "mpn": "LM317T",
                            "manufacturer": {"name": "TI"},
                            "totalAvail": 500000,
                            "medianPrice1000": {"price": 0.36, "currency": "USD"},
                            "octopartUrl": "https://octopart.com/lm317t",
                            "shortDescription": "Adj voltage regulator",
                        }
                    }]
                }
            }
        }
        with patch.object(c, "_rest_search", new_callable=AsyncMock, return_value=None):
            with patch.object(c, "_run_query", new_callable=AsyncMock, return_value=agg_resp):
                results = await c._do_search("LM317T")
                assert len(results) == 1
                assert results[0]["vendor_name"] == "Octopart (aggregate)"
                assert results[0]["qty_available"] == 500000


# ========================================================================
#  file_utils.py -- lines 31-32: exception handler
# ========================================================================

class TestFileUtilsParseErrorGap:
    def test_excel_parse_error_returns_empty(self):
        with patch("app.file_utils._parse_excel", side_effect=Exception("corrupt file")):
            from app.file_utils import parse_tabular_file
            rows = parse_tabular_file(b"fake-content", "data.xlsx")
            assert rows == []

    def test_csv_parse_error_returns_empty(self):
        with patch("app.file_utils._parse_csv", side_effect=Exception("encoding error")):
            from app.file_utils import parse_tabular_file
            rows = parse_tabular_file(b"bad-content", "data.csv")
            assert rows == []


# ========================================================================
#  vendor_utils.py -- line 195: seen_pairs continue
# ========================================================================

class TestVendorUtilsLine195Gap:
    def test_seen_pairs_continue_with_mocked_cards(self):
        from app.vendor_utils import find_vendor_dedup_candidates

        card_a = MagicMock()
        card_a.id = 1
        card_a.display_name = "Test Alpha"
        card_a.normalized_name = "test alpha"
        card_a.sighting_count = 10

        card_b = MagicMock()
        card_b.id = 2
        card_b.display_name = "Test Alpha Inc"
        card_b.normalized_name = "test alpha inc"
        card_b.sighting_count = 5

        # card_c has same id as card_a, so pair (card_b, card_c) => key (1,2) duplicates (card_a, card_b)
        card_c = MagicMock()
        card_c.id = 1
        card_c.display_name = "Test Alpha LLC"
        card_c.normalized_name = "test alpha llc"
        card_c.sighting_count = 3

        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_query.order_by.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.all.return_value = [card_a, card_b, card_c]
        mock_db.query.return_value = mock_query

        results = find_vendor_dedup_candidates(mock_db, threshold=50, limit=100)
        pair_keys = set()
        for r in results:
            key = (
                min(r["vendor_a"]["id"], r["vendor_b"]["id"]),
                max(r["vendor_a"]["id"], r["vendor_b"]["id"]),
            )
            assert key not in pair_keys
            pair_keys.add(key)


# ========================================================================
#  email_mining.py -- all missing lines
# ========================================================================

class TestEmailMiningGaps:
    def _make_miner(self, db=None, user_id=None):
        with patch("app.utils.graph_client.GraphClient") as MockGC:
            mock_gc = MagicMock()
            MockGC.return_value = mock_gc
            from app.connectors.email_mining import EmailMiner
            miner = EmailMiner("fake-token", db=db, user_id=user_id)
            miner.gc = mock_gc
        return miner

    # -- Line 157: _save_delta_token creates new SyncState --

    def test_save_delta_token_creates_new_record(self, db_session, test_user):
        from app.models import SyncState
        miner = self._make_miner(db=db_session, user_id=test_user.id)
        miner._save_delta_token("inbox_mining", "new-token-abc")
        sync = (
            db_session.query(SyncState)
            .filter(SyncState.user_id == test_user.id, SyncState.folder == "inbox_mining")
            .first()
        )
        assert sync is not None
        assert sync.delta_token == "new-token-abc"

    # -- Lines 171-184: _clear_delta_token body --

    def test_clear_delta_token_with_existing_sync(self, db_session, test_user):
        from app.models import SyncState
        db_session.add(
            SyncState(
                user_id=test_user.id,
                folder="inbox_mining",
                delta_token="stale-token",
                last_sync_at=datetime.now(timezone.utc),
            )
        )
        db_session.flush()
        miner = self._make_miner(db=db_session, user_id=test_user.id)
        miner._clear_delta_token("inbox_mining")
        sync = (
            db_session.query(SyncState)
            .filter(SyncState.user_id == test_user.id, SyncState.folder == "inbox_mining")
            .first()
        )
        assert sync.delta_token is None

    def test_clear_delta_token_no_existing_sync(self, db_session, test_user):
        miner = self._make_miner(db=db_session, user_id=test_user.id)
        miner._clear_delta_token("inbox_mining")

    # -- Line 380: scan_for_stock_lists skips processed --

    @pytest.mark.asyncio
    async def test_scan_stock_lists_skips_processed(self, db_session, test_user):
        from app.models import ProcessedMessage
        miner = self._make_miner(db=db_session, user_id=test_user.id)
        db_session.add(
            ProcessedMessage(
                message_id="already-done-msg",
                processing_type="attachment",
            )
        )
        db_session.flush()
        miner.gc.get_all_pages = AsyncMock(return_value=[
            {
                "id": "already-done-msg",
                "from": {"emailAddress": {"address": "v@parts.com", "name": "V"}},
                "subject": "Stock List",
                "attachments": [{"name": "stock.xlsx", "size": 100, "id": "a1"}],
            },
            {
                "id": "new-msg",
                "from": {"emailAddress": {"address": "v2@parts.com", "name": "V2"}},
                "subject": "Excess List",
                "receivedDateTime": "2026-01-10T00:00:00Z",
                "attachments": [{"name": "excess.csv", "size": 200, "id": "a2"}],
            },
        ])
        results = await miner.scan_for_stock_lists()
        assert len(results) == 1
        assert results[0]["stock_files"][0]["message_id"] == "new-msg"

    # -- Lines 586, 605, 666: deep_scan_inbox processed/mark flow --

    @pytest.mark.asyncio
    async def test_deep_scan_inbox_full_flow(self, db_session, test_user):
        from app.models import ProcessedMessage
        miner = self._make_miner(db=db_session, user_id=test_user.id)
        db_session.add(
            ProcessedMessage(
                message_id="old-deep-msg",
                processing_type="deep_mining",
            )
        )
        db_session.flush()
        miner.gc.get_all_pages = AsyncMock(return_value=[
            {
                "id": "old-deep-msg",
                "from": {"emailAddress": {"address": "vendor@chips.com", "name": "Chip Co"}},
                "subject": "Follow up",
                "body": {"content": "Hello from Chip Co."},
            },
            {
                "id": "new-deep-msg",
                "from": {"emailAddress": {"address": "sales@newvendor.com", "name": "New Vendor"}},
                "subject": "Stock available for LM317T",
                "body": {"content": "We have LM317T in stock."},
            },
            {
                "from": {"emailAddress": {"address": "x@y.com", "name": "X"}},
                "subject": "test",
                "body": {"content": "test"},
            },
        ])
        with patch("app.connectors.email_mining.EmailMiner._extract_vendor_info") as mock_vi:
            mock_vi.return_value = {
                "vendor_name": "New Vendor",
                "phones": ["+1-555-987-6543"],
                "websites": ["newvendor.com"],
            }
            with patch("app.services.specialty_detector.detect_brands_from_text", return_value={"TI"}):
                with patch("app.services.specialty_detector.detect_commodities_from_text", return_value=set()):
                    result = await miner.deep_scan_inbox(lookback_days=30, max_messages=50)
        assert result["contacts_found"] >= 1
        pm = (
            db_session.query(ProcessedMessage)
            .filter(
                ProcessedMessage.message_id == "new-deep-msg",
                ProcessedMessage.processing_type == "deep_mining",
            )
            .first()
        )
        assert pm is not None

    @pytest.mark.asyncio
    async def test_deep_scan_inbox_all_already_processed(self, db_session, test_user):
        from app.models import ProcessedMessage
        miner = self._make_miner(db=db_session, user_id=test_user.id)
        db_session.add(ProcessedMessage(message_id="dm1", processing_type="deep_mining"))
        db_session.add(ProcessedMessage(message_id="dm2", processing_type="deep_mining"))
        db_session.flush()
        miner.gc.get_all_pages = AsyncMock(return_value=[
            {
                "id": "dm1",
                "from": {"emailAddress": {"address": "a@vendor.com", "name": "A"}},
                "subject": "test",
                "body": {"content": "test"},
            },
            {
                "id": "dm2",
                "from": {"emailAddress": {"address": "b@vendor.com", "name": "B"}},
                "subject": "test",
                "body": {"content": "test"},
            },
        ])
        result = await miner.deep_scan_inbox(lookback_days=30, max_messages=50)
        assert result["contacts_found"] == 0

    # -- Line 820: _extract_part_numbers, candidate with no digits --

    def test_extract_part_numbers_no_digit(self):
        miner = self._make_miner()
        text = "Part ABCDE available"
        parts = miner._extract_part_numbers(text)
        assert "ABCDE" not in parts

    # -- Line 824: _extract_part_numbers, candidate with no alpha --

    def test_extract_part_numbers_no_alpha(self):
        miner = self._make_miner()
        text = "Qty: 123456 units"
        parts = miner._extract_part_numbers(text)
        assert "123456" not in parts

    # -- Line 837: _normalize_vendor_from_email single-part domain --

    def test_normalize_vendor_from_email_single_part_domain(self):
        miner = self._make_miner()
        result = miner._normalize_vendor_from_email("user@localhost")
        assert result == "localhost"

    # -- Line 820: inject short candidate via module-level MPN_PATTERN --

    def test_short_candidate_filtered_via_pattern_patch(self):
        import app.connectors.email_mining as em
        miner = self._make_miner()

        mock_pattern = MagicMock()
        mock_pattern.findall.return_value = ["AB1", "LM317T"]

        with patch.object(em, "MPN_PATTERN", mock_pattern):
            parts = miner._extract_part_numbers("test text")
            assert "AB1" not in parts
            assert "LM317T" in parts
