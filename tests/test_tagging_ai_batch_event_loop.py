"""test_tagging_ai_batch_event_loop.py — P2.6 event-loop-protection coverage for
tagging_ai_batch.py.

Covers: _write_batch_meta / _read_batch_meta (the asyncio.to_thread-dispatched sync
helpers), submit_batch_backfill (meta write), check_and_apply_batch_results (meta
read), and the with-block NamedTemporaryFile cleanup in apply_batch_results_chunked
(exercised end-to-end already by test_tagging_ai_batch.py; this file targets the two
previously-untested (`# pragma: no cover`) functions directly).

Called by: pytest
Depends on: app/services/tagging_ai_batch.py, tests/conftest.py
"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.services.tagging_ai_batch import (
    _read_batch_meta,
    _write_batch_meta,
    check_and_apply_batch_results,
    submit_batch_backfill,
)
from tests.conftest import engine  # noqa: F401


def _make_card(db: Session, mpn: str) -> MaterialCard:
    from datetime import datetime, timezone

    card = MaterialCard(
        normalized_mpn=mpn.lower(), display_mpn=mpn, search_count=1, created_at=datetime.now(timezone.utc)
    )
    db.add(card)
    db.flush()
    return card


def _run(coro):
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(coro)


class TestWriteReadBatchMeta:
    """Direct unit coverage for the sync helpers dispatched via asyncio.to_thread."""

    def test_round_trip(self, tmp_path):
        meta_path = str(tmp_path / "meta.json")
        meta = {"batch_ids": ["b1", "b2"], "batch_meta": {"batch_0": [[1, "LM317T"]]}, "total_mpns": 1}

        _write_batch_meta(meta_path, meta)

        assert os.path.isfile(meta_path)
        with open(meta_path) as f:
            on_disk = json.load(f)
        assert on_disk == meta

        loaded = _read_batch_meta(meta_path)
        assert loaded == meta

    def test_read_missing_file_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _read_batch_meta(str(tmp_path / "does-not-exist.json"))

    def test_write_produces_valid_json_not_python_repr(self, tmp_path):
        """Guards against a to_thread refactor accidentally swapping json.dump for
        str()/repr()."""
        meta_path = str(tmp_path / "meta.json")
        _write_batch_meta(meta_path, {"batch_ids": [], "batch_meta": {}, "total_mpns": 0})
        with open(meta_path) as f:
            raw = f.read()
        assert json.loads(raw) == {"batch_ids": [], "batch_meta": {}, "total_mpns": 0}


class TestSubmitBatchBackfillMetaWrite:
    """submit_batch_backfill dispatches its meta-file write via asyncio.to_thread."""

    @patch("app.utils.claude_client.claude_batch_submit", new_callable=AsyncMock)
    def test_submit_writes_meta_file_to_disk(self, mock_submit, db_session: Session, tmp_path, monkeypatch):
        import tempfile

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        mock_submit.return_value = "batch_abc123"
        _make_card(db_session, "STM32F103C8T6")
        db_session.commit()

        result = _run(submit_batch_backfill(db_session, batch_size=10))

        assert result["batch_ids"] == ["batch_abc123"]
        meta_path = result["meta_path"]
        assert os.path.isfile(meta_path)
        with open(meta_path) as f:
            meta = json.load(f)
        assert meta["total_mpns"] == 1
        assert meta["batch_ids"] == ["batch_abc123"]

    def test_no_untagged_cards_skips_meta_write(self, db_session: Session):
        """No untagged cards → returns early with no batch/meta file created."""
        result = _run(submit_batch_backfill(db_session))
        assert result == {"batch_id": None, "total_requests": 0, "total_mpns": 0}


class TestCheckAndApplyBatchResultsMetaRead:
    """check_and_apply_batch_results dispatches its meta-file read via
    asyncio.to_thread."""

    def test_missing_meta_file_returns_error(self, tmp_path):
        result = _run(check_and_apply_batch_results(db=None, meta_path=str(tmp_path / "nope.json")))
        assert result == {
            "status": "error",
            "error": "No batch metadata found. Run submit_batch_backfill first.",
        }

    @patch("app.utils.claude_client.claude_batch_results", new_callable=AsyncMock)
    def test_still_processing_reads_meta_and_reports_status(self, mock_results, tmp_path):
        meta_path = str(tmp_path / "meta.json")
        _write_batch_meta(meta_path, {"batch_ids": ["batch_1"], "batch_meta": {}, "total_mpns": 5})
        mock_results.return_value = None  # still processing

        result = _run(check_and_apply_batch_results(db=None, meta_path=meta_path))

        assert result["status"] == "processing"
        assert result["total_processed"] == 0
        # meta file is left in place while still processing
        assert os.path.isfile(meta_path)
