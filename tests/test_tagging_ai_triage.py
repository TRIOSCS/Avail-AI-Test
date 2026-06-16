"""test_tagging_ai_triage.py — Tests for AI triage service.

Covers: heuristic triage (triage_internal_parts), batch submission
(submit_triage_batch), and result application (apply_triage_results).

Called by: pytest
Depends on: app.services.tagging_ai_triage, conftest fixtures
"""

import contextlib
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.models.tags import MaterialTag, Tag
from app.services.tagging_ai_triage import (
    apply_triage_results,
    submit_triage_batch,
    triage_internal_parts,
)

# ── Helpers ──────────────────────────────────────────────────────────


def _make_material_card(db: Session, mpn: str, is_internal: bool = False) -> MaterialCard:
    """Create a MaterialCard with the given normalized MPN."""
    mc = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        manufacturer="Test Mfg",
        description="Test part",
        search_count=1,
        is_internal_part=is_internal,
        created_at=datetime.now(timezone.utc),
    )
    db.add(mc)
    db.flush()
    return mc


def _make_tag(db: Session, name: str = "resistors", tag_type: str = "commodity") -> Tag:
    """Create a Tag for linking to material cards."""
    t = Tag(name=name, tag_type=tag_type, created_at=datetime.now(timezone.utc))
    db.add(t)
    db.flush()
    return t


def _tag_card(db: Session, card: MaterialCard, tag: Tag) -> MaterialTag:
    """Link a MaterialCard to a Tag."""
    mt = MaterialTag(
        material_card_id=card.id,
        tag_id=tag.id,
        confidence=0.9,
        source="existing_data",
    )
    db.add(mt)
    db.flush()
    return mt


@contextlib.contextmanager
def _mock_ai_batch(status_code: int, batch_id: str | None = None):
    """Patch credential/http/MODELS so the AI batch POST returns ``status_code``.

    When ``batch_id`` is given the response also exposes ``json() -> {"id": batch_id}``.
    """
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    if batch_id is not None:
        mock_resp.json.return_value = {"id": batch_id}

    with (
        patch("app.services.credential_service.get_credential_cached", return_value="sk-test-key"),
        patch("app.http_client.http") as mock_http,
        patch("app.utils.claude_client.MODELS", {"fast": "claude-3-5-haiku-20241022"}),
    ):
        mock_http.post = AsyncMock(return_value=mock_resp)
        yield


# ── triage_internal_parts (heuristic) ───────────────────────────────


class TestTriageInternalParts:
    """Tests for the heuristic triage classifier."""

    # expected_reason=None means the original test only asserted is_internal,
    # so the reason is intentionally left unchecked.
    @pytest.mark.parametrize(
        ("mpn", "expected_internal", "expected_reason"),
        [
            pytest.param("12345", True, "pure numeric sequence", id="pure_numeric"),
            pytest.param("AB", True, "too short for standard MPN", id="very_short"),
            pytest.param("X", True, "too short for standard MPN", id="single_char"),
            pytest.param("INT-00456", True, "contains internal marker", id="marker_int_dash"),
            pytest.param("CUST-WIDGET", True, "contains internal marker", id="marker_cust"),
            pytest.param("PO#123456", True, "contains internal marker", id="marker_po_hash"),
            pytest.param("ASSY-TOP-BOARD", True, "contains internal marker", id="marker_assy"),
            pytest.param("KIT-EVAL-001", True, "contains internal marker", id="marker_kit"),
            pytest.param("SAMPLE-LM317T", True, "contains internal marker", id="marker_sample"),
            pytest.param("TEST-PART-42", True, "contains internal marker", id="marker_test_dash"),
            pytest.param("PO-987654", True, "contains internal marker", id="marker_po_dash"),
            pytest.param("LM317[REV-A]", True, "contains unusual characters", id="unusual_brackets"),
            pytest.param("A=B", True, "contains unusual characters", id="unusual_equals"),
            pytest.param("PART{1}", True, "contains unusual characters", id="unusual_braces"),
            pytest.param("PART|ALT", True, "contains unusual characters", id="unusual_pipe"),
            pytest.param("-LM317T", True, "starts with special character", id="starts_special_char"),
            pytest.param("_INTERNAL", True, "starts with special character", id="starts_underscore"),
            pytest.param("A" * 41, True, "unusually long", id="unusually_long"),
            pytest.param("LM317T", False, "", id="real_mpn"),
            pytest.param("STM32F407VGT6", False, "", id="real_mpn_with_dash"),
            pytest.param("A" * 40, False, None, id="exactly_40_chars_not_long"),
            pytest.param("ABC", False, None, id="exactly_3_chars_not_short"),
        ],
    )
    def test_classification(self, mpn, expected_internal, expected_reason):
        results = triage_internal_parts([mpn])
        assert len(results) == 1
        assert results[0]["is_internal"] is expected_internal
        if expected_reason is not None:
            assert results[0]["reason"] == expected_reason

    def test_multiple_mpns(self):
        results = triage_internal_parts(["LM317T", "12345", "INT-001"])
        assert len(results) == 3
        assert results[0]["is_internal"] is False
        assert results[1]["is_internal"] is True
        assert results[2]["is_internal"] is True

    def test_empty_list(self):
        results = triage_internal_parts([])
        assert results == []

    def test_preserves_original_mpn(self):
        results = triage_internal_parts(["  lm317t  "])
        # Original MPN preserved in output even though upper().strip() used internally
        assert results[0]["mpn"] == "  lm317t  "


# ── submit_triage_batch ─────────────────────────────────────────────


class TestSubmitTriageBatch:
    """Tests for the async batch triage submission."""

    @pytest.mark.asyncio
    async def test_no_candidates_returns_zeros(self, db_session: Session):
        """When no untagged cards exist, return all zeros."""
        result = await submit_triage_batch(db_session, limit=100)
        assert result == {"heuristic_flagged": 0, "ai_submitted": 0, "total_processed": 0}

    @pytest.mark.asyncio
    async def test_heuristic_flags_obvious_internals(self, db_session: Session):
        """Cards with pure numeric MPNs should be flagged by heuristics."""
        card = _make_material_card(db_session, "12345")
        db_session.commit()

        result = await submit_triage_batch(db_session, limit=100)

        assert result["heuristic_flagged"] == 1
        assert result["total_processed"] == 1
        db_session.refresh(card)
        assert card.is_internal_part is True

    @pytest.mark.asyncio
    async def test_already_tagged_cards_excluded(self, db_session: Session):
        """Cards with existing MaterialTag should not be processed."""
        card = _make_material_card(db_session, "LM317T")
        tag = _make_tag(db_session, "voltage_regulator")
        _tag_card(db_session, card, tag)
        db_session.commit()

        result = await submit_triage_batch(db_session, limit=100)
        assert result["total_processed"] == 0

    @pytest.mark.asyncio
    async def test_already_internal_cards_excluded(self, db_session: Session):
        """Cards already marked is_internal_part=True should not be processed."""
        _make_material_card(db_session, "12345", is_internal=True)
        db_session.commit()

        result = await submit_triage_batch(db_session, limit=100)
        assert result["total_processed"] == 0

    @pytest.mark.asyncio
    async def test_ambiguous_cards_submitted_to_ai(self, db_session: Session):
        """Cards that pass heuristics should be sent to the AI batch API."""
        _make_material_card(db_session, "LM317T")
        _make_material_card(db_session, "STM32F407VGT6")
        db_session.commit()

        with _mock_ai_batch(200, batch_id="batch_abc123"):
            result = await submit_triage_batch(db_session, limit=100)

        assert result["ai_submitted"] == 2
        assert result["heuristic_flagged"] == 0
        assert result["total_processed"] == 2

    @pytest.mark.asyncio
    async def test_ai_batch_failure_still_returns(self, db_session: Session):
        """If AI batch API returns an error, ai_submitted should be 0."""
        _make_material_card(db_session, "LM317T")
        db_session.commit()

        with _mock_ai_batch(500):
            result = await submit_triage_batch(db_session, limit=100)

        assert result["ai_submitted"] == 0
        assert result["heuristic_flagged"] == 0

    @pytest.mark.asyncio
    async def test_no_api_key_skips_ai(self, db_session: Session):
        """If no API key is configured, AI submission is skipped."""
        _make_material_card(db_session, "LM317T")
        db_session.commit()

        with patch("app.services.credential_service.get_credential_cached", return_value=None):
            result = await submit_triage_batch(db_session, limit=100)

        assert result["ai_submitted"] == 0
        assert result["heuristic_flagged"] == 0
        assert result["total_processed"] == 1

    @pytest.mark.asyncio
    async def test_mixed_heuristic_and_ai(self, db_session: Session):
        """Mix of obvious internals and ambiguous cards."""
        _make_material_card(db_session, "99999")  # heuristic: pure numeric
        _make_material_card(db_session, "LM317T")  # ambiguous: real MPN
        db_session.commit()

        with _mock_ai_batch(201, batch_id="batch_mixed"):
            result = await submit_triage_batch(db_session, limit=100)

        assert result["heuristic_flagged"] == 1
        assert result["ai_submitted"] == 1
        assert result["total_processed"] == 2

    @pytest.mark.asyncio
    async def test_limit_respected(self, db_session: Session):
        """Limit parameter should cap the number of processed cards."""
        for i in range(5):
            _make_material_card(db_session, f"PART{i:03d}")
        db_session.commit()

        with patch("app.services.credential_service.get_credential_cached", return_value=None):
            result = await submit_triage_batch(db_session, limit=2)

        assert result["total_processed"] == 2


# ── apply_triage_results ────────────────────────────────────────────


class TestApplyTriageResults:
    """Tests for applying batch triage results from the Anthropic Batch API."""

    @pytest.mark.asyncio
    async def test_no_api_key_returns_error(self):
        """Missing API key should return error dict."""
        with patch("app.services.credential_service.get_credential_cached", return_value=None):
            result = await apply_triage_results("batch_123")
        assert "error" in result
        assert "API key" in result["error"]

    @pytest.mark.asyncio
    async def test_batch_status_check_failure(self):
        """Non-200 status check should return error."""
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with (
            patch("app.services.credential_service.get_credential_cached", return_value="sk-test"),
            patch("app.http_client.http") as mock_http,
        ):
            mock_http.get = AsyncMock(return_value=mock_resp)
            result = await apply_triage_results("batch_123")

        assert "error" in result
        assert "404" in result["error"]

    @pytest.mark.asyncio
    async def test_batch_not_ended(self):
        """Batch still processing should return error."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"processing_status": "in_progress"}

        with (
            patch("app.services.credential_service.get_credential_cached", return_value="sk-test"),
            patch("app.http_client.http") as mock_http,
        ):
            mock_http.get = AsyncMock(return_value=mock_resp)
            result = await apply_triage_results("batch_123")

        assert "error" in result
        assert "not ready" in result["error"]

    @pytest.mark.asyncio
    async def test_batch_ended_no_results_url(self):
        """Batch ended but missing results_url should return error."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"processing_status": "ended"}

        with (
            patch("app.services.credential_service.get_credential_cached", return_value="sk-test"),
            patch("app.http_client.http") as mock_http,
        ):
            mock_http.get = AsyncMock(return_value=mock_resp)
            result = await apply_triage_results("batch_123")

        assert "error" in result
        assert "results_url" in result["error"]

    @pytest.mark.asyncio
    async def test_download_failure(self):
        """Stream download error should return error dict."""
        status_resp = MagicMock()
        status_resp.status_code = 200
        status_resp.json.return_value = {
            "processing_status": "ended",
            "results_url": "https://api.anthropic.com/results/abc",
        }

        # Mock the stream context manager to raise
        mock_stream_ctx = AsyncMock()
        mock_stream_ctx.__aenter__.side_effect = Exception("Connection reset")

        with (
            patch("app.services.credential_service.get_credential_cached", return_value="sk-test"),
            patch("app.http_client.http") as mock_http,
        ):
            mock_http.get = AsyncMock(return_value=status_resp)
            mock_http.stream = MagicMock(return_value=mock_stream_ctx)
            result = await apply_triage_results("batch_123")

        assert "error" in result
        assert "Download failed" in result["error"]

    @pytest.mark.asyncio
    async def test_successful_apply(self, db_session: Session):
        """Successfully apply triage results from JSONL data."""
        card_internal = _make_material_card(db_session, "int-part-001")
        card_real = _make_material_card(db_session, "lm317t")
        db_session.commit()

        # Build JSONL content
        jsonl_lines = []
        for mpn, is_internal in [("int-part-001", True), ("lm317t", False)]:
            line = {
                "custom_id": "triage_0",
                "result": {
                    "type": "succeeded",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps([{"mpn": mpn, "is_internal": is_internal, "reason": "test"}]),
                            }
                        ]
                    },
                },
            }
            jsonl_lines.append(json.dumps(line))

        jsonl_content = "\n".join(jsonl_lines) + "\n"

        result = await _run_apply_with_jsonl(db_session, jsonl_content)

        assert result["total_lines"] == 2
        assert result["flagged"] == 1
        assert result["real_mpns"] == 1
        assert result["errors"] == 0

        db_session.refresh(card_internal)
        assert card_internal.is_internal_part is True

    @pytest.mark.asyncio
    async def test_apply_with_failed_result_entry(self, db_session: Session):
        """Entries with type != 'succeeded' should count as errors."""
        _make_material_card(db_session, "lm317t")
        db_session.commit()

        line = {
            "custom_id": "triage_0",
            "result": {"type": "errored", "error": {"message": "rate limited"}},
        }
        result = await _run_apply_with_jsonl(db_session, json.dumps(line) + "\n")

        assert result["errors"] == 1
        assert result["total_lines"] == 1

    @pytest.mark.asyncio
    async def test_apply_with_empty_content(self, db_session: Session):
        """Entries with no text content should count as errors."""
        line = {
            "custom_id": "triage_0",
            "result": {
                "type": "succeeded",
                "message": {"content": [{"type": "image", "url": "..."}]},
            },
        }
        result = await _run_apply_with_jsonl(db_session, json.dumps(line) + "\n")

        assert result["errors"] == 1

    @pytest.mark.asyncio
    async def test_apply_with_invalid_json_content(self, db_session: Session):
        """Content text that isn't valid JSON should count as errors."""
        line = {
            "custom_id": "triage_0",
            "result": {
                "type": "succeeded",
                "message": {"content": [{"type": "text", "text": "not valid json"}]},
            },
        }
        result = await _run_apply_with_jsonl(db_session, json.dumps(line) + "\n")

        assert result["errors"] == 1

    @pytest.mark.asyncio
    async def test_apply_with_non_list_json(self, db_session: Session):
        """Content text that is valid JSON but not a list should count as errors."""
        line = {
            "custom_id": "triage_0",
            "result": {
                "type": "succeeded",
                "message": {"content": [{"type": "text", "text": '{"mpn": "LM317T"}'}]},
            },
        }
        result = await _run_apply_with_jsonl(db_session, json.dumps(line) + "\n")

        assert result["errors"] == 1

    @pytest.mark.asyncio
    async def test_apply_skips_empty_mpn(self, db_session: Session):
        """Items with empty MPN in results should be silently skipped."""
        line = {
            "custom_id": "triage_0",
            "result": {
                "type": "succeeded",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps([{"mpn": "", "is_internal": True, "reason": "test"}]),
                        }
                    ]
                },
            },
        }
        result = await _run_apply_with_jsonl(db_session, json.dumps(line) + "\n")

        assert result["flagged"] == 0
        assert result["real_mpns"] == 0
        assert result["errors"] == 0

    @pytest.mark.asyncio
    async def test_apply_card_not_found_in_db(self, db_session: Session):
        """MPN in results but not in DB should be silently handled."""
        line = {
            "custom_id": "triage_0",
            "result": {
                "type": "succeeded",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps([{"mpn": "NONEXISTENT999", "is_internal": True, "reason": "test"}]),
                        }
                    ]
                },
            },
        }
        result = await _run_apply_with_jsonl(db_session, json.dumps(line) + "\n")

        # Not an error — just doesn't match any card
        assert result["flagged"] == 0
        assert result["errors"] == 0


# ── Async iteration helper ──────────────────────────────────────────


async def _async_iter(items):
    """Helper to create an async iterator from a list."""
    for item in items:
        yield item


async def _run_apply_with_jsonl(db_session: Session, jsonl_content: str, batch_id: str = "batch_abc"):
    """Run apply_triage_results against a mocked batch that streams ``jsonl_content``.

    Mocks the Anthropic Batch API status check (ended, with results_url) and the
    streaming download, and routes the service's own DB session to ``db_session``.
    """
    status_resp = MagicMock()
    status_resp.status_code = 200
    status_resp.json.return_value = {
        "processing_status": "ended",
        "results_url": "https://api.anthropic.com/results/abc",
    }

    with (
        patch("app.services.credential_service.get_credential_cached", return_value="sk-test"),
        patch("app.http_client.http") as mock_http,
        patch("app.database.SessionLocal", return_value=db_session),
    ):
        mock_http.get = AsyncMock(return_value=status_resp)
        fake_stream = AsyncMock()
        fake_stream.aiter_bytes = lambda chunk_size=65536: _async_iter([jsonl_content.encode()])
        stream_ctx = AsyncMock()
        stream_ctx.__aenter__.return_value = fake_stream
        mock_http.stream = MagicMock(return_value=stream_ctx)

        with patch.object(db_session, "close", lambda: None):
            return await apply_triage_results(batch_id)
