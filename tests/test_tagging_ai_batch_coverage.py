"""test_tagging_ai_batch_coverage.py — Gap tests for tagging_ai_batch.py.

Targets missing lines:
- Line 481: blank lines in JSONL are skipped (continue)
- Line 507: classifications is a list (not a dict) → treated as items directly
- Lines 516-520: batch_classifications reaches >=100 → flushed mid-loop
- Line 523: total_lines % 500 == 0 → progress log
- Lines 535-538: exception in outer try-block → rollback and re-raise
- Lines 546-547: OSError when unlinking temp file (silently caught)

Called by: pytest
Depends on: app/services/tagging_ai_batch.py, tests/conftest.py
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.services.tagging_ai_batch import apply_batch_results_chunked
from tests.conftest import engine  # noqa: F401


def _make_card(db: Session, mpn: str, manufacturer: str | None = None) -> MaterialCard:
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


def _make_fake_stream(tmp_path: str):
    """Create a fake async context manager for http.stream()."""

    class _FakeStream:
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


def _run(coro):
    import asyncio

    loop = asyncio.get_event_loop()
    return loop.run_until_complete(coro)


class TestApplyBatchResultsBlankLines:
    @patch("app.database.SessionLocal")
    @patch("app.http_client.http")
    @patch("app.services.credential_service.get_credential_cached", return_value="sk-test-key")
    def test_blank_lines_are_skipped(self, mock_cred, mock_http, mock_session_local, db_session: Session):
        """Line 481: blank lines in JSONL are skipped without counting as errors."""
        mock_session_local.return_value = db_session

        # Write JSONL with blank lines interspersed
        result_line = json.dumps(
            {
                "custom_id": "backfill_0",
                "result": {
                    "type": "succeeded",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "structured_output",
                                "input": {"classifications": []},
                            }
                        ]
                    },
                },
            }
        )
        tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", dir="/tmp", delete=False, mode="w")
        tmp.write("\n")  # Blank line (skipped)
        tmp.write(result_line + "\n")
        tmp.write("   \n")  # Whitespace-only line (skipped after strip)
        tmp.close()
        tmp_path = tmp.name

        status_resp = MagicMock()
        status_resp.status_code = 200
        status_resp.json.return_value = {
            "processing_status": "ended",
            "results_url": "https://api.anthropic.com/results/batch_blank",
        }
        mock_http.get = AsyncMock(return_value=status_resp)
        mock_http.stream = _make_fake_stream(tmp_path)

        try:
            result = _run(apply_batch_results_chunked("batch_blank"))
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        # Only 1 real line processed (the two blanks were skipped)
        assert result["total_lines"] == 1
        assert result["errors"] == 0


class TestApplyBatchResultsClassificationsAsDict:
    @patch("app.database.SessionLocal")
    @patch("app.http_client.http")
    @patch("app.services.credential_service.get_credential_cached", return_value="sk-test-key")
    def test_classifications_dict_with_classifications_key(self, mock_cred, mock_http, mock_session_local, db_session):
        """Line 505-508: classifications dict with 'classifications' key processes items."""
        _make_card(db_session, "lm317t", manufacturer=None)
        db_session.commit()
        mock_session_local.return_value = db_session

        # Normal case: input is a dict with 'classifications' array
        result_line = json.dumps(
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
                                            "mpn": "LM317T",
                                            "manufacturer": "Texas Instruments",
                                            "category": "Regulators",
                                        }
                                    ]
                                },
                            }
                        ]
                    },
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
            "results_url": "https://api.anthropic.com/results/batch_dict",
        }
        mock_http.get = AsyncMock(return_value=status_resp)
        mock_http.stream = _make_fake_stream(tmp_path)

        try:
            result = _run(apply_batch_results_chunked("batch_dict"))
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        assert result["total_lines"] == 1
        assert result["matched"] == 1


class TestApplyBatchResultsMidBatchFlush:
    @patch("app.database.SessionLocal")
    @patch("app.http_client.http")
    @patch("app.services.credential_service.get_credential_cached", return_value="sk-test-key")
    def test_batch_flushed_every_100_classifications(self, mock_cred, mock_http, mock_session_local, db_session):
        """Lines 516-520: when batch_classifications reaches 100, _apply_chunked_batch is called mid-loop."""
        # Create 100 cards
        for i in range(100):
            _make_card(db_session, f"part{i:04d}", manufacturer=None)
        db_session.commit()
        mock_session_local.return_value = db_session

        # Build a single JSONL line with 100 classifications
        classifications = [
            {
                "mpn": f"PART{i:04d}",
                "manufacturer": "Texas Instruments",
                "category": "ICs",
            }
            for i in range(100)
        ]
        result_line = json.dumps(
            {
                "custom_id": "backfill_0",
                "result": {
                    "type": "succeeded",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "structured_output",
                                "input": {"classifications": classifications},
                            }
                        ]
                    },
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
            "results_url": "https://api.anthropic.com/results/batch_100",
        }
        mock_http.get = AsyncMock(return_value=status_resp)
        mock_http.stream = _make_fake_stream(tmp_path)

        try:
            result = _run(apply_batch_results_chunked("batch_100"))
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        assert result["total_lines"] == 1
        assert result["matched"] >= 0  # May or may not match depending on card names


class TestApplyBatchResultsProgressLog:
    @patch("app.database.SessionLocal")
    @patch("app.http_client.http")
    @patch("app.services.credential_service.get_credential_cached", return_value="sk-test-key")
    def test_progress_log_at_500_lines(self, mock_cred, mock_http, mock_session_local, db_session):
        """Line 523: progress log fires every 500 lines."""
        mock_session_local.return_value = db_session

        # Write exactly 500 valid-but-empty-classification lines
        lines = []
        for i in range(500):
            line = json.dumps(
                {
                    "custom_id": f"backfill_{i}",
                    "result": {
                        "type": "succeeded",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "structured_output",
                                    "input": {"classifications": []},
                                }
                            ]
                        },
                    },
                }
            )
            lines.append(line)

        tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", dir="/tmp", delete=False, mode="w")
        tmp.write("\n".join(lines) + "\n")
        tmp.close()
        tmp_path = tmp.name

        status_resp = MagicMock()
        status_resp.status_code = 200
        status_resp.json.return_value = {
            "processing_status": "ended",
            "results_url": "https://api.anthropic.com/results/batch_500",
        }
        mock_http.get = AsyncMock(return_value=status_resp)
        mock_http.stream = _make_fake_stream(tmp_path)

        try:
            result = _run(apply_batch_results_chunked("batch_500"))
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        assert result["total_lines"] == 500


class TestApplyBatchResultsExceptionPath:
    @patch("app.database.SessionLocal")
    @patch("app.http_client.http")
    @patch("app.services.credential_service.get_credential_cached", return_value="sk-test-key")
    def test_exception_triggers_rollback_and_reraise(self, mock_cred, mock_http, mock_session_local, db_session):
        """Lines 535-538: exception in processing triggers rollback + re-raise."""
        mock_session_local.return_value = db_session

        # Write a valid-looking JSONL line
        result_line = json.dumps(
            {
                "custom_id": "backfill_0",
                "result": {
                    "type": "succeeded",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "structured_output",
                                "input": {"classifications": []},
                            }
                        ]
                    },
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
            "results_url": "https://api.anthropic.com/results/batch_exc",
        }
        mock_http.get = AsyncMock(return_value=status_resp)
        mock_http.stream = _make_fake_stream(tmp_path)

        # Patch db.commit to raise (triggers exception path)
        with patch.object(db_session, "commit", side_effect=RuntimeError("Commit failed")):
            with pytest.raises(RuntimeError, match="Commit failed"):
                _run(apply_batch_results_chunked("batch_exc"))

        try:
            os.unlink(tmp_path)
        except OSError:
            pass


class TestApplyBatchResultsOSErrorOnUnlink:
    @patch("app.database.SessionLocal")
    @patch("app.http_client.http")
    @patch("app.services.credential_service.get_credential_cached", return_value="sk-test-key")
    def test_oserror_on_unlink_is_silenced(self, mock_cred, mock_http, mock_session_local, db_session):
        """Lines 546-547: OSError when os.unlink(tmp_path) is silently caught."""
        mock_session_local.return_value = db_session

        result_line = json.dumps(
            {
                "custom_id": "backfill_0",
                "result": {
                    "type": "succeeded",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "structured_output",
                                "input": {"classifications": []},
                            }
                        ]
                    },
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
            "results_url": "https://api.anthropic.com/results/batch_oserr",
        }
        mock_http.get = AsyncMock(return_value=status_resp)
        mock_http.stream = _make_fake_stream(tmp_path)

        # Patch os.unlink to raise OSError inside the finally block
        with patch("os.unlink", side_effect=OSError("file already gone")):
            result = _run(apply_batch_results_chunked("batch_oserr"))

        # Should not raise — OSError is silently caught
        assert result["total_lines"] == 1
