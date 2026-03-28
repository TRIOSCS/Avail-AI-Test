"""Tests for app/services/enrichment_utils.py — batch processing, credential checks,
dedup.

Called by: pytest
Depends on: conftest fixtures, mocked credential_service
"""

from unittest.mock import patch

from app.services.enrichment_utils import (
    check_enrichment_credentials,
    deduplicate_contacts,
    run_enrichment_batch,
)


class TestRunEnrichmentBatch:
    async def test_empty_batch(self):
        result = await run_enrichment_batch([], lambda x: x)
        assert result["total"] == 0
        assert result["processed"] == 0
        assert result["errors"] == []

    async def test_successful_batch(self):
        items = [1, 2, 3, 4, 5]

        async def process(item):
            return {"ok": True}

        result = await run_enrichment_batch(items, process, label="test")
        assert result["total"] == 5
        assert result["processed"] == 5
        assert result["errors"] == []

    async def test_batch_with_errors(self):
        items = [1, 2, 3]

        async def process(item):
            if item == 2:
                raise ValueError("item 2 failed")
            return {"ok": True}

        result = await run_enrichment_batch(items, process, label="test")
        assert result["total"] == 3
        assert result["processed"] == 3  # all processed (some with errors)
        assert len(result["errors"]) == 1
        assert "item 2 failed" in result["errors"][0]

    async def test_batch_concurrency(self):
        """Verify semaphore-bounded concurrency works."""
        items = list(range(10))
        processed = []

        async def process(item):
            processed.append(item)

        result = await run_enrichment_batch(items, process, concurrency=2, label="conc")
        assert result["processed"] == 10
        assert len(processed) == 10

    async def test_progress_logging(self):
        """Batch size controls progress log frequency."""
        items = list(range(5))

        async def process(item):
            pass

        result = await run_enrichment_batch(items, process, batch_size=2, label="progress")
        assert result["processed"] == 5


class TestCheckEnrichmentCredentials:
    def test_known_source_with_cred(self):
        with patch(
            "app.services.enrichment_utils.get_credential_cached",
            return_value="fake-key",
        ):
            result = check_enrichment_credentials(["apollo"])
        assert result["apollo"] is True

    def test_known_source_without_cred(self):
        with patch(
            "app.services.enrichment_utils.get_credential_cached",
            return_value=None,
        ):
            result = check_enrichment_credentials(["apollo"])
        assert result["apollo"] is False

    def test_unknown_source(self):
        result = check_enrichment_credentials(["nonexistent_source"])
        assert result["nonexistent_source"] is False

    def test_multi_key_source(self):
        def mock_get(src, key):
            if key == "NEXAR_CLIENT_ID":
                return "id123"
            if key == "NEXAR_CLIENT_SECRET":
                return "secret456"
            return None

        with patch("app.services.enrichment_utils.get_credential_cached", side_effect=mock_get):
            result = check_enrichment_credentials(["nexar"])
        assert result["nexar"] is True

    def test_multi_key_partial(self):
        """If one key of a multi-key source is missing, result is False."""

        def mock_get(src, key):
            if key == "NEXAR_CLIENT_ID":
                return "id123"
            return None  # missing secret

        with patch("app.services.enrichment_utils.get_credential_cached", side_effect=mock_get):
            result = check_enrichment_credentials(["nexar"])
        assert result["nexar"] is False

    def test_multiple_sources(self):
        def mock_get(src, key):
            return "key" if "APOLLO" in key else None

        with patch("app.services.enrichment_utils.get_credential_cached", side_effect=mock_get):
            result = check_enrichment_credentials(["apollo", "hunter"])
        assert result["apollo"] is True
        assert result["hunter"] is False


class TestDeduplicateContacts:
    def test_no_duplicates(self):
        contacts = [
            {"email": "a@test.com", "name": "A"},
            {"email": "b@test.com", "name": "B"},
        ]
        result = deduplicate_contacts(contacts)
        assert len(result) == 2

    def test_removes_duplicates(self):
        contacts = [
            {"email": "a@test.com", "name": "First"},
            {"email": "a@test.com", "name": "Duplicate"},
            {"email": "b@test.com", "name": "B"},
        ]
        result = deduplicate_contacts(contacts)
        assert len(result) == 2
        assert result[0]["name"] == "First"  # first wins

    def test_case_insensitive(self):
        contacts = [
            {"email": "A@Test.com", "name": "Upper"},
            {"email": "a@test.com", "name": "Lower"},
        ]
        result = deduplicate_contacts(contacts)
        assert len(result) == 1

    def test_custom_key(self):
        contacts = [
            {"phone": "+1555", "name": "A"},
            {"phone": "+1555", "name": "B"},
            {"phone": "+1666", "name": "C"},
        ]
        result = deduplicate_contacts(contacts, key="phone")
        assert len(result) == 2

    def test_empty_key_skipped(self):
        contacts = [
            {"email": "", "name": "Empty"},
            {"email": "valid@test.com", "name": "Valid"},
        ]
        result = deduplicate_contacts(contacts)
        assert len(result) == 1
        assert result[0]["name"] == "Valid"

    def test_none_key_skipped(self):
        contacts = [
            {"name": "No Email"},  # missing key
            {"email": "valid@test.com", "name": "Valid"},
        ]
        result = deduplicate_contacts(contacts)
        assert len(result) == 1

    def test_empty_list(self):
        assert deduplicate_contacts([]) == []

    def test_preserves_order(self):
        contacts = [
            {"email": "c@test.com", "name": "C"},
            {"email": "a@test.com", "name": "A"},
            {"email": "b@test.com", "name": "B"},
        ]
        result = deduplicate_contacts(contacts)
        assert [r["name"] for r in result] == ["C", "A", "B"]
