"""Tests for tagging_ai_batch — batch AI classification and chunked result apply.

Covers: submit_targeted_backfill, apply_batch_results_chunked, _apply_chunked_batch
Depends on: conftest.py (db_session, test SQLite engine)
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.models.tags import MaterialTag, Tag
from app.services.tagging_ai_batch import _apply_chunked_batch, apply_batch_results_chunked, submit_targeted_backfill
from tests.conftest import engine  # noqa: F401

# ── Helpers ───────────────────────────────────────────────────────────


def _make_card(db: Session, mpn: str, manufacturer: str | None = None) -> MaterialCard:
    """Create a MaterialCard with the given MPN."""
    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        manufacturer=manufacturer,
        search_count=1,
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.flush()
    return card


def _make_brand_tag(db: Session, name: str) -> Tag:
    """Create a brand Tag."""
    tag = Tag(name=name, tag_type="brand", created_at=datetime.now(timezone.utc))
    db.add(tag)
    db.flush()
    return tag


def _make_commodity_tag(db: Session, name: str) -> Tag:
    """Create a commodity Tag."""
    tag = Tag(name=name, tag_type="commodity", created_at=datetime.now(timezone.utc))
    db.add(tag)
    db.flush()
    return tag


def _run(coro):
    """Run an async coroutine synchronously."""
    import asyncio

    loop = asyncio.get_event_loop()
    return loop.run_until_complete(coro)


def _make_fake_stream(tmp_path: str):
    """Create a fake async context manager for http.stream() that reads from
    tmp_path."""

    class _FakeStream:
        """Sync callable that returns an async context manager (matching
        httpx.stream)."""

        def __init__(self, path):
            self._path = path

        def __call__(self, method, url, **kwargs):
            return self

        async def __aenter__(self):
            with open(self._path, "rb") as f:
                self._content = f.read()
            return self

        async def __aexit__(self, *args):
            pass

        async def aiter_bytes(self, chunk_size=65536):
            yield self._content

    return _FakeStream(tmp_path)


# ── _apply_chunked_batch ─────────────────────────────────────────────


class TestApplyChunkedBatch:
    """Tests for _apply_chunked_batch — DB-level tag application."""

    def test_empty_classifications(self, db_session: Session):
        """Empty classifications list returns (0, 0)."""
        matched, unknown = _apply_chunked_batch([], db_session)
        assert matched == 0
        assert unknown == 0

    def test_classifications_with_no_mpn(self, db_session: Session):
        """Classifications without MPN key return (0, 0)."""
        classifications = [{"manufacturer": "TI", "category": "MCU"}]
        matched, unknown = _apply_chunked_batch(classifications, db_session)
        assert matched == 0
        assert unknown == 0

    def test_classifications_with_empty_mpn(self, db_session: Session):
        """Classifications with empty MPN string return (0, 0)."""
        classifications = [{"mpn": "", "manufacturer": "TI", "category": "MCU"}]
        matched, unknown = _apply_chunked_batch(classifications, db_session)
        assert matched == 0
        assert unknown == 0

    def test_known_manufacturer_creates_brand_tag(self, db_session: Session):
        """Known manufacturer creates a brand tag and returns matched=1."""
        card = _make_card(db_session, "STM32F103C8T6")
        db_session.commit()

        classifications = [
            {"mpn": "STM32F103C8T6", "manufacturer": "STMicroelectronics", "category": "Microcontrollers (MCU)"}
        ]
        matched, unknown = _apply_chunked_batch(classifications, db_session)

        assert matched == 1
        assert unknown == 0

        # Verify brand tag was created
        brand_tag = db_session.query(Tag).filter_by(tag_type="brand", name="STMicroelectronics").first()
        assert brand_tag is not None

        # Verify MaterialTag was created
        mt = db_session.query(MaterialTag).filter_by(material_card_id=card.id, tag_id=brand_tag.id).first()
        assert mt is not None
        assert mt.source == "ai_classified"
        assert mt.confidence == 0.92  # default when no model_confidence

    def test_unknown_manufacturer_counted_as_unknown(self, db_session: Session):
        """Unknown manufacturer is skipped (v2 schema: no junk tags)."""
        _make_card(db_session, "CUSTOM-PART-001")
        db_session.commit()

        classifications = [{"mpn": "CUSTOM-PART-001", "manufacturer": "Unknown", "category": "Miscellaneous"}]
        matched, unknown = _apply_chunked_batch(classifications, db_session)

        assert matched == 0
        assert unknown == 1

    def test_null_manufacturer_counted_as_unknown(self, db_session: Session):
        """Null/empty manufacturer is counted as unknown."""
        _make_card(db_session, "CUSTOM-PART-002")
        db_session.commit()

        classifications = [{"mpn": "CUSTOM-PART-002", "manufacturer": None, "category": "Miscellaneous"}]
        matched, unknown = _apply_chunked_batch(classifications, db_session)

        assert matched == 0
        assert unknown == 1

    def test_commodity_tag_applied_when_not_miscellaneous(self, db_session: Session):
        """Commodity tag is applied when category is not 'Miscellaneous'."""
        _make_card(db_session, "LM317T")
        # Pre-create the commodity tag (they are pre-seeded, not auto-created)
        commodity_tag = _make_commodity_tag(db_session, "Power Management ICs")
        db_session.commit()

        classifications = [{"mpn": "LM317T", "manufacturer": "Texas Instruments", "category": "Power Management ICs"}]
        matched, unknown = _apply_chunked_batch(classifications, db_session)

        assert matched == 1

        # Verify commodity tag was linked
        mt = db_session.query(MaterialTag).filter_by(tag_id=commodity_tag.id).first()
        assert mt is not None
        assert mt.source == "ai_classified"
        # Commodity confidence is capped at 0.95
        assert mt.confidence <= 0.95

    def test_miscellaneous_category_not_tagged(self, db_session: Session):
        """'Miscellaneous' category does not create a commodity tag."""
        _make_card(db_session, "LM317T")
        db_session.commit()

        classifications = [{"mpn": "LM317T", "manufacturer": "Texas Instruments", "category": "Miscellaneous"}]
        matched, unknown = _apply_chunked_batch(classifications, db_session)

        assert matched == 1
        # Only brand tag, no commodity tag
        commodity_tags = db_session.query(MaterialTag).join(Tag).filter(Tag.tag_type == "commodity").all()
        assert len(commodity_tags) == 0

    def test_manufacturer_field_updated_when_empty(self, db_session: Session):
        """Card.manufacturer is set when it was previously empty."""
        card = _make_card(db_session, "AD8232ACPZ", manufacturer=None)
        db_session.commit()

        classifications = [{"mpn": "AD8232ACPZ", "manufacturer": "Analog Devices", "category": "Analog ICs"}]
        _apply_chunked_batch(classifications, db_session)

        db_session.refresh(card)
        assert card.manufacturer == "Analog Devices"

    def test_manufacturer_field_not_overwritten(self, db_session: Session):
        """Card.manufacturer is NOT overwritten when already set."""
        card = _make_card(db_session, "AD8232ACPZ", manufacturer="Existing Mfr")
        db_session.commit()

        classifications = [{"mpn": "AD8232ACPZ", "manufacturer": "Analog Devices", "category": "Analog ICs"}]
        _apply_chunked_batch(classifications, db_session)

        db_session.refresh(card)
        assert card.manufacturer == "Existing Mfr"

    def test_model_confidence_used_when_high(self, db_session: Session):
        """Model-reported confidence >= 0.90 is used instead of default."""
        card = _make_card(db_session, "MAX232CPE")
        db_session.commit()

        classifications = [
            {"mpn": "MAX232CPE", "manufacturer": "Analog Devices", "category": "Interface ICs", "confidence": 0.97}
        ]
        matched, unknown = _apply_chunked_batch(classifications, db_session)

        assert matched == 1
        brand_tag = db_session.query(Tag).filter_by(tag_type="brand", name="Analog Devices").first()
        mt = db_session.query(MaterialTag).filter_by(material_card_id=card.id, tag_id=brand_tag.id).first()
        assert mt.confidence == 0.97

    def test_model_confidence_clamped_to_090(self, db_session: Session):
        """Model confidence below 0.90 is clamped to 0.90."""
        _make_card(db_session, "XYZ123")
        db_session.commit()

        classifications = [{"mpn": "XYZ123", "manufacturer": "Some Corp", "category": "Logic ICs", "confidence": 0.85}]
        _apply_chunked_batch(classifications, db_session)

        brand_tag = db_session.query(Tag).filter_by(tag_type="brand", name="Some Corp").first()
        mt = db_session.query(MaterialTag).join(Tag).filter(Tag.id == brand_tag.id).first()
        assert mt.confidence == 0.90

    def test_model_confidence_clamped_to_100(self, db_session: Session):
        """Model confidence above 1.0 is clamped to 1.0."""
        _make_card(db_session, "ABC456")
        db_session.commit()

        classifications = [{"mpn": "ABC456", "manufacturer": "Test Corp", "category": "Resistors", "confidence": 1.5}]
        _apply_chunked_batch(classifications, db_session)

        brand_tag = db_session.query(Tag).filter_by(tag_type="brand", name="Test Corp").first()
        mt = db_session.query(MaterialTag).join(Tag).filter(Tag.id == brand_tag.id).first()
        assert mt.confidence == 1.0

    def test_multiple_cards_in_batch(self, db_session: Session):
        """Multiple cards in a single batch are all processed."""
        _make_card(db_session, "STM32F103")
        _make_card(db_session, "LM317T")
        _make_card(db_session, "CUSTOM-001")
        db_session.commit()

        classifications = [
            {"mpn": "STM32F103", "manufacturer": "STMicroelectronics", "category": "MCU"},
            {"mpn": "LM317T", "manufacturer": "Texas Instruments", "category": "Regulators"},
            {"mpn": "CUSTOM-001", "manufacturer": "Unknown", "category": "Miscellaneous"},
        ]
        matched, unknown = _apply_chunked_batch(classifications, db_session)

        assert matched == 2
        assert unknown == 1

    def test_case_insensitive_mpn_matching(self, db_session: Session):
        """MPN matching is case-insensitive."""
        _make_card(db_session, "stm32f103")
        db_session.commit()

        classifications = [{"mpn": "STM32F103", "manufacturer": "STMicroelectronics", "category": "MCU"}]
        matched, unknown = _apply_chunked_batch(classifications, db_session)

        assert matched == 1

    def test_no_matching_card_in_db(self, db_session: Session):
        """Classifications for MPNs not in DB are silently ignored."""
        classifications = [{"mpn": "NONEXISTENT-PART", "manufacturer": "Texas Instruments", "category": "Analog ICs"}]
        matched, unknown = _apply_chunked_batch(classifications, db_session)

        assert matched == 0
        assert unknown == 0


# ── submit_targeted_backfill ─────────────────────────────────────────


class TestSubmitTargetedBackfill:
    """Tests for submit_targeted_backfill — Batch API submission."""

    @patch("app.services.credential_service.get_credential_cached", return_value=None)
    def test_no_api_key_returns_error(self, mock_cred, db_session: Session):
        """Returns error when no API key configured."""
        result = _run(submit_targeted_backfill(db_session))
        assert result == {"error": "No Anthropic API key configured"}

    @patch("app.services.credential_service.get_credential_cached", return_value="sk-test-key")
    def test_no_untagged_cards_returns_zero(self, mock_cred, db_session: Session):
        """Returns batch_id=None when no untagged cards exist."""
        result = _run(submit_targeted_backfill(db_session))
        assert result == {"batch_id": None, "total_submitted": 0}

    @patch("app.utils.claude_client.MODELS", {"fast": "claude-3-5-haiku-20241022"})
    @patch("app.http_client.http")
    @patch("app.services.credential_service.get_credential_cached", return_value="sk-test-key")
    def test_successful_submission(self, mock_cred, mock_http, db_session: Session):
        """Successful batch API submission returns batch_id and count."""
        # Create untagged cards with no manufacturer
        _make_card(db_session, "STM32F103C8T6", manufacturer=None)
        _make_card(db_session, "LM317T", manufacturer=None)
        db_session.commit()

        # Mock API response
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "batch_abc123"}

        mock_http.post = AsyncMock(return_value=mock_resp)

        result = _run(submit_targeted_backfill(db_session))

        assert result["batch_id"] == "batch_abc123"
        assert result["total_submitted"] == 2
        mock_http.post.assert_called_once()

    @patch("app.utils.claude_client.MODELS", {"fast": "claude-3-5-haiku-20241022"})
    @patch("app.http_client.http")
    @patch("app.services.credential_service.get_credential_cached", return_value="sk-test-key")
    def test_api_failure_returns_error(self, mock_cred, mock_http, db_session: Session):
        """API failure returns error with status code."""
        _make_card(db_session, "PART-001", manufacturer=None)
        db_session.commit()

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_http.post = AsyncMock(return_value=mock_resp)

        result = _run(submit_targeted_backfill(db_session))

        assert "error" in result
        assert "HTTP 500" in result["error"]

    @patch("app.services.credential_service.get_credential_cached", return_value="sk-test-key")
    def test_already_tagged_cards_excluded(self, mock_cred, db_session: Session):
        """Cards with existing MaterialTags are excluded from the batch."""
        card = _make_card(db_session, "STM32F103C8T6", manufacturer=None)
        tag = _make_brand_tag(db_session, "STMicroelectronics")
        db_session.add(
            MaterialTag(
                material_card_id=card.id,
                tag_id=tag.id,
                confidence=0.95,
                source="existing_data",
            )
        )
        db_session.commit()

        result = _run(submit_targeted_backfill(db_session))
        assert result == {"batch_id": None, "total_submitted": 0}

    @patch("app.services.credential_service.get_credential_cached", return_value="sk-test-key")
    def test_cards_with_manufacturer_excluded(self, mock_cred, db_session: Session):
        """Cards that already have a manufacturer field are excluded."""
        _make_card(db_session, "STM32F103", manufacturer="STMicroelectronics")
        db_session.commit()

        result = _run(submit_targeted_backfill(db_session))
        assert result == {"batch_id": None, "total_submitted": 0}


# ── apply_batch_results_chunked ──────────────────────────────────────


class TestApplyBatchResultsChunked:
    """Tests for apply_batch_results_chunked — download and apply batch results."""

    @patch("app.services.credential_service.get_credential_cached", return_value=None)
    def test_no_api_key_returns_error(self, mock_cred):
        """Returns error when no API key configured."""
        result = _run(apply_batch_results_chunked("batch_123"))
        assert result == {"error": "No Anthropic API key configured"}

    @patch("app.http_client.http")
    @patch("app.services.credential_service.get_credential_cached", return_value="sk-test-key")
    def test_batch_status_check_failure(self, mock_cred, mock_http):
        """Returns error when batch status check fails."""
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_http.get = AsyncMock(return_value=mock_resp)

        result = _run(apply_batch_results_chunked("batch_123"))
        assert "error" in result
        assert "HTTP 404" in result["error"]

    @patch("app.http_client.http")
    @patch("app.services.credential_service.get_credential_cached", return_value="sk-test-key")
    def test_batch_not_ended_yet(self, mock_cred, mock_http):
        """Returns error when batch is still processing."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"processing_status": "in_progress"}
        mock_http.get = AsyncMock(return_value=mock_resp)

        result = _run(apply_batch_results_chunked("batch_123"))
        assert "error" in result
        assert "not ready" in result["error"].lower()

    @patch("app.http_client.http")
    @patch("app.services.credential_service.get_credential_cached", return_value="sk-test-key")
    def test_batch_ended_no_results_url(self, mock_cred, mock_http):
        """Returns error when batch ended but no results_url provided."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"processing_status": "ended", "results_url": None}
        mock_http.get = AsyncMock(return_value=mock_resp)

        result = _run(apply_batch_results_chunked("batch_123"))
        assert "error" in result
        assert "results_url" in result["error"].lower()

    @patch("app.database.SessionLocal")
    @patch("app.http_client.http")
    @patch("app.services.credential_service.get_credential_cached", return_value="sk-test-key")
    def test_successful_apply_with_tool_use_results(
        self, mock_cred, mock_http, mock_session_local, db_session: Session
    ):
        """Successfully downloads and applies batch results with tool_use format."""
        # Create cards in the test DB
        _make_card(db_session, "stm32f103c8t6", manufacturer=None)
        _make_card(db_session, "lm317t", manufacturer=None)
        db_session.commit()

        # Mock SessionLocal to return our test session
        mock_session_local.return_value = db_session

        # Build JSONL results
        result_line_1 = json.dumps(
            {
                "custom_id": "backfill_0",
                "result": {
                    "type": "succeeded",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "structured_output",
                                "input": {
                                    "classifications": [
                                        {
                                            "mpn": "STM32F103C8T6",
                                            "manufacturer": "STMicroelectronics",
                                            "category": "MCU",
                                        },
                                        {
                                            "mpn": "LM317T",
                                            "manufacturer": "Texas Instruments",
                                            "category": "Regulators",
                                        },
                                    ]
                                },
                            }
                        ]
                    },
                },
            }
        )

        # Write temp JSONL file
        tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", dir="/tmp", delete=False, mode="w")
        tmp.write(result_line_1 + "\n")
        tmp.close()
        tmp_path = tmp.name

        # Mock status check
        status_resp = MagicMock()
        status_resp.status_code = 200
        status_resp.json.return_value = {
            "processing_status": "ended",
            "results_url": "https://api.anthropic.com/results/batch_123",
        }
        mock_http.get = AsyncMock(return_value=status_resp)
        mock_http.stream = _make_fake_stream(tmp_path)

        try:
            result = _run(apply_batch_results_chunked("batch_123"))
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        assert result["total_lines"] == 1
        assert result["matched"] == 2
        assert result["unknown"] == 0
        assert result["errors"] == 0

    @patch("app.database.SessionLocal")
    @patch("app.http_client.http")
    @patch("app.services.credential_service.get_credential_cached", return_value="sk-test-key")
    def test_failed_result_entries_counted_as_errors(
        self, mock_cred, mock_http, mock_session_local, db_session: Session
    ):
        """Entries with type != 'succeeded' are counted as errors."""
        mock_session_local.return_value = db_session

        result_line = json.dumps(
            {
                "custom_id": "backfill_0",
                "result": {"type": "errored", "error": {"message": "rate limited"}},
            }
        )

        tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", dir="/tmp", delete=False, mode="w")
        tmp.write(result_line + "\n")
        tmp.close()
        tmp_path = tmp.name

        status_resp = MagicMock()
        status_resp.status_code = 200
        status_resp.json.return_value = {
            "processing_status": "ended",
            "results_url": "https://api.anthropic.com/results/batch_123",
        }
        mock_http.get = AsyncMock(return_value=status_resp)
        mock_http.stream = _make_fake_stream(tmp_path)

        try:
            result = _run(apply_batch_results_chunked("batch_123"))
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        assert result["total_lines"] == 1
        assert result["errors"] == 1
        assert result["matched"] == 0

    @patch("app.database.SessionLocal")
    @patch("app.http_client.http")
    @patch("app.services.credential_service.get_credential_cached", return_value="sk-test-key")
    def test_no_tool_use_block_counted_as_error(self, mock_cred, mock_http, mock_session_local, db_session: Session):
        """Succeeded entries without a tool_use block are counted as errors."""
        mock_session_local.return_value = db_session

        result_line = json.dumps(
            {
                "custom_id": "backfill_0",
                "result": {
                    "type": "succeeded",
                    "message": {"content": [{"type": "text", "text": "Some plain text response"}]},
                },
            }
        )

        tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", dir="/tmp", delete=False, mode="w")
        tmp.write(result_line + "\n")
        tmp.close()
        tmp_path = tmp.name

        status_resp = MagicMock()
        status_resp.status_code = 200
        status_resp.json.return_value = {
            "processing_status": "ended",
            "results_url": "https://api.anthropic.com/results/batch_123",
        }
        mock_http.get = AsyncMock(return_value=status_resp)
        mock_http.stream = _make_fake_stream(tmp_path)

        try:
            result = _run(apply_batch_results_chunked("batch_123"))
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        assert result["total_lines"] == 1
        assert result["errors"] == 1
        assert result["matched"] == 0

    @patch("app.http_client.http")
    @patch("app.services.credential_service.get_credential_cached", return_value="sk-test-key")
    def test_download_failure_returns_error(self, mock_cred, mock_http):
        """Returns error when JSONL download fails."""
        status_resp = MagicMock()
        status_resp.status_code = 200
        status_resp.json.return_value = {
            "processing_status": "ended",
            "results_url": "https://api.anthropic.com/results/batch_123",
        }
        mock_http.get = AsyncMock(return_value=status_resp)

        class _FailingStream:
            def __call__(self, method, url, **kwargs):
                return self

            async def __aenter__(self):
                raise ConnectionError("Network error")

            async def __aexit__(self, *args):
                pass

        mock_http.stream = _FailingStream()

        result = _run(apply_batch_results_chunked("batch_123"))
        assert "error" in result
        assert "Download failed" in result["error"]

    @patch("app.database.SessionLocal")
    @patch("app.http_client.http")
    @patch("app.services.credential_service.get_credential_cached", return_value="sk-test-key")
    def test_malformed_json_line_counted_as_error(self, mock_cred, mock_http, mock_session_local, db_session: Session):
        """Malformed JSON lines are counted as errors, not crashes."""
        mock_session_local.return_value = db_session

        tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", dir="/tmp", delete=False, mode="w")
        tmp.write("this is not json\n")
        tmp.write("{invalid json too\n")
        tmp.close()
        tmp_path = tmp.name

        status_resp = MagicMock()
        status_resp.status_code = 200
        status_resp.json.return_value = {
            "processing_status": "ended",
            "results_url": "https://api.anthropic.com/results/batch_123",
        }
        mock_http.get = AsyncMock(return_value=status_resp)
        mock_http.stream = _make_fake_stream(tmp_path)

        try:
            result = _run(apply_batch_results_chunked("batch_123"))
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        assert result["total_lines"] == 2
        assert result["errors"] == 2
        assert result["matched"] == 0
