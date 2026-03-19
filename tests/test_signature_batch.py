"""Tests for batch signature parsing — submit and process results.

Verifies batch_parse_signatures() and process_signature_batch_results()
use the BatchQueue + claude_batch_submit/results flow correctly.

Called by: pytest
Depends on: app.services.signature_parser, conftest fixtures
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models import EmailSignatureExtract
from app.services.signature_parser import (
    batch_parse_signatures,
    process_signature_batch_results,
)
from tests.conftest import engine  # noqa: F401 — ensures SQLite engine is used


@pytest.fixture()
def low_confidence_extracts(db_session):
    """Create 5 low-confidence regex-parsed signature extracts."""
    records = []
    for i in range(5):
        record = EmailSignatureExtract(
            sender_email=f"user{i}@example.com",
            sender_name=f"User {i}",
            full_name=f"User {i}" if i % 2 == 0 else None,
            title="Manager" if i == 0 else None,
            company_name=f"Company{i}" if i % 3 == 0 else None,
            phone=f"555-000-{i:04d}" if i == 1 else None,
            extraction_method="regex",
            confidence=0.4 + (i * 0.05),  # 0.4, 0.45, 0.5, 0.55, 0.6
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(record)
        records.append(record)
    db_session.commit()
    for r in records:
        db_session.refresh(r)
    return records


@pytest.fixture()
def high_confidence_extracts(db_session):
    """Create 3 high-confidence extracts that should NOT be picked up."""
    records = []
    for i in range(3):
        record = EmailSignatureExtract(
            sender_email=f"good{i}@example.com",
            sender_name=f"Good User {i}",
            full_name=f"Good User {i}",
            title="Director",
            company_name=f"GoodCo{i}",
            extraction_method="regex",
            confidence=0.8,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(record)
        records.append(record)
    db_session.commit()
    return records


@pytest.fixture()
def batch_api_extracts(db_session):
    """Create 2 extracts already processed by batch_api (different method)."""
    records = []
    for i in range(2):
        record = EmailSignatureExtract(
            sender_email=f"batch{i}@example.com",
            sender_name=f"Batch User {i}",
            full_name=f"Batch User {i}",
            extraction_method="batch_api",
            confidence=0.5,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(record)
        records.append(record)
    db_session.commit()
    return records


@pytest.fixture()
def no_body_extracts(db_session):
    """Create extracts with no matching VendorResponse (no body available)."""
    extract = EmailSignatureExtract(
        sender_email="nobody@example.com",
        sender_name="No Body",
        extraction_method="regex",
        confidence=0.3,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(extract)
    db_session.commit()
    db_session.refresh(extract)
    return [extract]


# ── batch_parse_signatures tests ─────────────────────────────────────


@patch("app.services.signature_parser._get_redis")
def test_batch_parse_skips_when_inflight(mock_redis, db_session, low_confidence_extracts):
    """Returns None when a batch is already inflight (Redis key exists)."""
    mock_r = MagicMock()
    mock_r.get.return_value = b"batch_sig_existing"
    mock_redis.return_value = mock_r

    result = asyncio.get_event_loop().run_until_complete(batch_parse_signatures(db_session))
    assert result is None


def test_batch_parse_no_records(db_session):
    """Returns None when no low-confidence records exist."""
    result = asyncio.get_event_loop().run_until_complete(batch_parse_signatures(db_session))
    assert result is None


@patch("app.services.signature_parser._get_redis")
@patch("app.services.signature_parser.claude_batch_submit")
def test_batch_parse_no_body_still_submits(mock_submit, mock_redis, db_session, no_body_extracts):
    """Records without VendorResponse body still get submitted — prompt uses existing
    fields."""
    mock_submit.return_value = "batch_sig_nobody"
    mock_r = MagicMock()
    mock_r.get.return_value = None  # No inflight batch
    mock_redis.return_value = mock_r

    result = asyncio.get_event_loop().run_until_complete(batch_parse_signatures(db_session))

    assert result == "batch_sig_nobody"
    requests = mock_submit.call_args[0][0]
    assert len(requests) == 1
    assert requests[0]["custom_id"].startswith("sig_parse:")


@patch("app.services.signature_parser._get_redis")
@patch("app.services.signature_parser.claude_batch_submit")
def test_batch_parse_submits_batch(mock_submit, mock_redis, db_session, low_confidence_extracts):
    """Submits a batch and stores batch_id in Redis."""
    mock_submit.return_value = "batch_sig_123"
    mock_r = MagicMock()
    mock_r.get.return_value = None  # No inflight batch
    mock_redis.return_value = mock_r

    result = asyncio.get_event_loop().run_until_complete(batch_parse_signatures(db_session))

    assert result == "batch_sig_123"
    mock_submit.assert_called_once()

    # Check the requests — one per low-confidence record
    requests = mock_submit.call_args[0][0]
    assert len(requests) == 5  # All 5 have confidence < 0.7
    for req in requests:
        assert "custom_id" in req
        assert req["custom_id"].startswith("sig_parse:")
        assert "prompt" in req
        assert "schema" in req
        assert "system" in req

    # Verify Redis set was called
    mock_r.set.assert_called_once_with("batch:signature_parse:current", "batch_sig_123")


@patch("app.services.signature_parser._get_redis")
@patch("app.services.signature_parser.claude_batch_submit")
def test_batch_parse_skips_high_confidence(
    mock_submit, mock_redis, db_session, low_confidence_extracts, high_confidence_extracts
):
    """Only picks up records with confidence < 0.7."""
    mock_submit.return_value = "batch_sig_456"
    mock_r = MagicMock()
    mock_r.get.return_value = None  # No inflight batch
    mock_redis.return_value = mock_r

    result = asyncio.get_event_loop().run_until_complete(batch_parse_signatures(db_session))

    assert result == "batch_sig_456"
    requests = mock_submit.call_args[0][0]
    # Should have 5 requests (low confidence only, all < 0.7), not 8
    assert len(requests) == 5


@patch("app.services.signature_parser._get_redis")
@patch("app.services.signature_parser.claude_batch_submit")
def test_batch_parse_submit_fails(mock_submit, mock_redis, db_session, low_confidence_extracts):
    """Returns None when claude_batch_submit fails."""
    mock_submit.return_value = None
    mock_r = MagicMock()
    mock_r.get.return_value = None  # No inflight batch
    mock_redis.return_value = mock_r

    result = asyncio.get_event_loop().run_until_complete(batch_parse_signatures(db_session))

    assert result is None
    mock_r.set.assert_not_called()


# ── process_signature_batch_results tests ────────────────────────────


@patch("app.services.signature_parser._get_redis")
def test_process_results_no_batch_id(mock_redis, db_session):
    """Returns None when no batch_id is stored in Redis."""
    mock_r = MagicMock()
    mock_r.get.return_value = None
    mock_redis.return_value = mock_r

    result = asyncio.get_event_loop().run_until_complete(process_signature_batch_results(db_session))
    assert result is None


@patch("app.services.signature_parser._get_redis")
@patch("app.services.signature_parser.claude_batch_results")
def test_process_results_still_processing(mock_results, mock_redis, db_session):
    """Returns None when batch is still processing."""
    mock_r = MagicMock()
    mock_r.get.return_value = b"batch_sig_123"
    mock_redis.return_value = mock_r
    mock_results.return_value = None

    result = asyncio.get_event_loop().run_until_complete(process_signature_batch_results(db_session))
    assert result is None
    mock_r.delete.assert_not_called()


@patch("app.services.signature_parser._get_redis")
@patch("app.services.signature_parser.claude_batch_results")
def test_process_results_applies_data(mock_results, mock_redis, db_session, low_confidence_extracts):
    """Applies AI results to EmailSignatureExtract records and clears Redis key."""
    mock_r = MagicMock()
    mock_r.get.return_value = b"batch_sig_123"
    mock_redis.return_value = mock_r

    results_dict = {}
    for extract in low_confidence_extracts:
        custom_id = f"sig_parse:{extract.id}"
        results_dict[custom_id] = {
            "full_name": f"AI Parsed {extract.sender_name}",
            "title": "Sales Manager",
            "company_name": "Acme Corp",
            "phone": "555-1234",
            "mobile": "555-5678",
            "website": "acme.com",
            "address": "123 Main St",
            "linkedin_url": "https://linkedin.com/in/test",
        }

    mock_results.return_value = results_dict

    result = asyncio.get_event_loop().run_until_complete(process_signature_batch_results(db_session))

    assert result["applied"] == 5
    assert result["errors"] == 0

    # Verify records were updated
    for extract in low_confidence_extracts:
        db_session.refresh(extract)
        assert extract.extraction_method == "batch_api"
        assert extract.confidence > 0.7  # Should be high now with 8 fields
        assert extract.title == "Sales Manager"
        assert extract.company_name == "Acme Corp"

    # Redis key should be cleared
    mock_r.delete.assert_called_once_with("batch:signature_parse:current")


@patch("app.services.signature_parser._get_redis")
@patch("app.services.signature_parser.claude_batch_results")
def test_process_results_handles_none_entry(mock_results, mock_redis, db_session, low_confidence_extracts):
    """Skips records with None results (errors) without crashing."""
    mock_r = MagicMock()
    mock_r.get.return_value = b"batch_sig_123"
    mock_redis.return_value = mock_r

    extract = low_confidence_extracts[0]
    results_dict = {
        f"sig_parse:{extract.id}": None,  # Error entry
    }
    mock_results.return_value = results_dict

    result = asyncio.get_event_loop().run_until_complete(process_signature_batch_results(db_session))

    assert result["applied"] == 0
    assert result["errors"] == 1
    db_session.refresh(extract)
    assert extract.extraction_method == "regex"  # Unchanged

    # Redis key should still be cleared since results were returned
    mock_r.delete.assert_called_once()


@patch("app.services.signature_parser._get_redis")
@patch("app.services.signature_parser.claude_batch_results")
def test_process_results_commit_failure_keeps_redis_key(mock_results, mock_redis, db_session, low_confidence_extracts):
    """On commit failure, returns stats but does NOT clear Redis key."""
    mock_r = MagicMock()
    mock_r.get.return_value = b"batch_sig_123"
    mock_redis.return_value = mock_r

    extract = low_confidence_extracts[0]
    results_dict = {
        f"sig_parse:{extract.id}": {
            "full_name": "Test Person",
            "title": "Engineer",
            "company_name": "TestCo",
            "phone": None,
            "mobile": None,
            "website": None,
            "address": None,
            "linkedin_url": None,
        },
    }
    mock_results.return_value = results_dict

    # Make commit raise an exception
    with patch.object(db_session, "commit", side_effect=Exception("DB error")):
        result = asyncio.get_event_loop().run_until_complete(process_signature_batch_results(db_session))

    assert result is not None
    assert result["applied"] == 1
    # Redis key should NOT be cleared on commit failure
    mock_r.delete.assert_not_called()


@patch("app.services.signature_parser._get_redis")
@patch("app.services.signature_parser.claude_batch_results")
def test_process_results_partial_fields(mock_results, mock_redis, db_session, low_confidence_extracts):
    """Handles partial results where some fields are null."""
    mock_r = MagicMock()
    mock_r.get.return_value = b"batch_sig_123"
    mock_redis.return_value = mock_r

    extract = low_confidence_extracts[0]
    results_dict = {
        f"sig_parse:{extract.id}": {
            "full_name": "John Doe",
            "title": None,
            "company_name": "SomeCorp",
            "phone": "555-9999",
            "mobile": None,
            "website": None,
            "address": None,
            "linkedin_url": None,
        },
    }
    mock_results.return_value = results_dict

    result = asyncio.get_event_loop().run_until_complete(process_signature_batch_results(db_session))

    assert result["applied"] == 1
    db_session.refresh(extract)
    assert extract.full_name == "John Doe"
    assert extract.company_name == "SomeCorp"
    assert extract.phone == "555-9999"
    assert extract.extraction_method == "batch_api"
    # Confidence based on 3 non-null fields (full_name, company_name, phone)
    # plus any that were already set
    assert extract.confidence >= 0.5
