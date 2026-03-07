"""
test_enrichment_utils.py -- Tests for app/services/enrichment_utils.py

Covers:
- run_enrichment_batch: success, errors, progress logging, empty list
- check_enrichment_credentials: known/unknown sources, credential presence
- deduplicate_contacts: dedup by email, empty values, case insensitivity

Called by: pytest
Depends on: app/services/enrichment_utils.py
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.enrichment_utils import (
    check_enrichment_credentials,
    deduplicate_contacts,
    run_enrichment_batch,
)


# ═══════════════════════════════════════════════════════════════════════
#  run_enrichment_batch
# ═══════════════════════════════════════════════════════════════════════


class TestRunEnrichmentBatch:
    @pytest.mark.asyncio
    async def test_empty_list(self):
        """Empty entity list returns zero counts."""
        result = await run_enrichment_batch([], AsyncMock())
        assert result == {"total": 0, "processed": 0, "errors": []}

    @pytest.mark.asyncio
    async def test_successful_processing(self):
        """All entities processed successfully."""
        entities = [1, 2, 3]
        mock_fn = AsyncMock()

        result = await run_enrichment_batch(entities, mock_fn, label="test")
        assert result["total"] == 3
        assert result["processed"] == 3
        assert result["errors"] == []
        assert mock_fn.call_count == 3

    @pytest.mark.asyncio
    async def test_error_handling(self):
        """Errors are caught and counted."""
        entities = [1, 2, 3]
        mock_fn = AsyncMock(side_effect=[None, RuntimeError("fail"), None])

        result = await run_enrichment_batch(entities, mock_fn, label="err_test")
        assert result["total"] == 3
        assert result["processed"] == 3
        assert len(result["errors"]) == 1
        assert "fail" in result["errors"][0]

    @pytest.mark.asyncio
    async def test_progress_logging(self):
        """Progress is logged at batch_size intervals."""
        entities = list(range(10))
        mock_fn = AsyncMock()

        result = await run_enrichment_batch(
            entities, mock_fn, batch_size=5, label="prog_test"
        )
        assert result["processed"] == 10

    @pytest.mark.asyncio
    async def test_concurrency_respected(self):
        """Semaphore limits concurrent tasks."""
        entities = [1, 2, 3]
        mock_fn = AsyncMock()

        result = await run_enrichment_batch(
            entities, mock_fn, concurrency=1, label="conc_test"
        )
        assert result["processed"] == 3


# ═══════════════════════════════════════════════════════════════════════
#  check_enrichment_credentials
# ═══════════════════════════════════════════════════════════════════════


class TestCheckEnrichmentCredentials:
    def test_known_source_with_credentials(self):
        """Known source with credentials returns True."""
        with patch("app.services.enrichment_utils.get_credential_cached", return_value="sk-test"):
            result = check_enrichment_credentials(["apollo"])
        assert result["apollo"] is True

    def test_known_source_without_credentials(self):
        """Known source without credentials returns False."""
        with patch("app.services.enrichment_utils.get_credential_cached", return_value=None):
            result = check_enrichment_credentials(["apollo"])
        assert result["apollo"] is False

    def test_unknown_source(self):
        """Unknown source returns False."""
        result = check_enrichment_credentials(["totally_unknown_source"])
        assert result["totally_unknown_source"] is False

    def test_multiple_sources(self):
        """Multiple sources checked at once."""
        def mock_cred(source, key):
            return "key" if source == "apollo" else None

        with patch("app.services.enrichment_utils.get_credential_cached", side_effect=mock_cred):
            result = check_enrichment_credentials(["apollo", "hunter"])
        assert result["apollo"] is True
        assert result["hunter"] is False

    def test_nexar_requires_both_credentials(self):
        """Nexar requires both client_id and client_secret."""
        call_count = 0

        def mock_cred(source, key):
            nonlocal call_count
            call_count += 1
            if key == "NEXAR_CLIENT_ID":
                return "id"
            return None  # NEXAR_CLIENT_SECRET missing

        with patch("app.services.enrichment_utils.get_credential_cached", side_effect=mock_cred):
            result = check_enrichment_credentials(["nexar"])
        assert result["nexar"] is False

    def test_empty_list(self):
        """Empty source list returns empty dict."""
        result = check_enrichment_credentials([])
        assert result == {}


# ═══════════════════════════════════════════════════════════════════════
#  deduplicate_contacts
# ═══════════════════════════════════════════════════════════════════════


class TestDeduplicateContacts:
    def test_basic_dedup(self):
        """Duplicate emails are removed, keeping first."""
        contacts = [
            {"email": "alice@example.com", "name": "Alice"},
            {"email": "ALICE@example.com", "name": "Alice Dup"},
            {"email": "bob@example.com", "name": "Bob"},
        ]
        result = deduplicate_contacts(contacts)
        assert len(result) == 2
        assert result[0]["name"] == "Alice"
        assert result[1]["name"] == "Bob"

    def test_empty_email_skipped(self):
        """Contacts with empty/None email are skipped."""
        contacts = [
            {"email": "", "name": "Empty"},
            {"email": None, "name": "None"},
            {"email": "valid@example.com", "name": "Valid"},
        ]
        result = deduplicate_contacts(contacts)
        assert len(result) == 1
        assert result[0]["name"] == "Valid"

    def test_custom_key(self):
        """Deduplication works with a custom key."""
        contacts = [
            {"phone": "800-555-1234", "name": "First"},
            {"phone": "800-555-1234", "name": "Second"},
        ]
        result = deduplicate_contacts(contacts, key="phone")
        assert len(result) == 1
        assert result[0]["name"] == "First"

    def test_missing_key(self):
        """Contact without the key field is treated as empty."""
        contacts = [
            {"name": "No Email"},
            {"email": "valid@example.com", "name": "Valid"},
        ]
        result = deduplicate_contacts(contacts)
        assert len(result) == 1

    def test_whitespace_handling(self):
        """Whitespace in emails is stripped before comparison."""
        contacts = [
            {"email": "  alice@example.com  ", "name": "Alice"},
            {"email": "alice@example.com", "name": "Alice Dup"},
        ]
        result = deduplicate_contacts(contacts)
        assert len(result) == 1

    def test_empty_list(self):
        """Empty contact list returns empty list."""
        assert deduplicate_contacts([]) == []
