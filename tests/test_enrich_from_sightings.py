"""Tests for scripts/enrich_from_sightings.py extraction logic.

Tests the description, manufacturer, and datasheet_url extraction from
sighting raw_data without needing a real database.

Called by: pytest
Depends on: scripts/enrich_from_sightings.py
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.enrich_from_sightings import (
    _extract_datasheet_url,
    _extract_description,
    enrich_card_from_sightings,
)

# ── _extract_description tests ────────────────────────────────────────


class TestExtractDescription:
    @pytest.mark.parametrize(
        ("raw", "source", "expected"),
        [
            (
                {"description": "IC MCU 32BIT 256KB FLASH 100LQFP", "vendor_name": "DigiKey"},
                "digikey",
                "IC MCU 32BIT 256KB FLASH 100LQFP",
            ),
            (
                {"description": "ARM Cortex-M4 STM32F4 Microcontroller IC", "vendor_name": "Mouser"},
                "mouser",
                "ARM Cortex-M4 STM32F4 Microcontroller IC",
            ),
            (
                {"ebay_title": "Samsung 16GB DDR4-3200 ECC RDIMM Memory Module"},
                "ebay",
                "Samsung 16GB DDR4-3200 ECC RDIMM Memory Module",
            ),
            (None, "digikey", None),
            ({}, "digikey", None),
            ({"description": "IC"}, "digikey", None),
            ({"description": 12345}, "digikey", None),
            ({"description": "  padded description  "}, "digikey", "padded description"),
        ],
        ids=[
            "digikey_description",
            "mouser_description",
            "ebay_title_fallback",
            "none_raw_data",
            "empty_dict",
            "short_description_skipped",
            "non_string_description",
            "stripped",
        ],
    )
    def test_extract_description(self, raw, source, expected):
        assert _extract_description(raw, source) == expected

    def test_truncated_to_1000(self):
        raw = {"description": "A" * 1500}
        result = _extract_description(raw, "digikey")
        assert len(result) == 1000


# ── _extract_datasheet_url tests ──────────────────────────────────────


class TestExtractDatasheetUrl:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ({"datasheet_url": "https://example.com/datasheet.pdf"}, "https://example.com/datasheet.pdf"),
            ({}, None),
            ({"datasheet_url": "ftp://example.com/file"}, None),
            (None, None),
        ],
        ids=["valid_url", "no_url", "non_http_url", "none_raw_data"],
    )
    def test_extract_datasheet_url(self, raw, expected):
        assert _extract_datasheet_url(raw) == expected

    def test_truncated_to_1000(self):
        raw = {"datasheet_url": "https://example.com/" + "a" * 1500}
        result = _extract_datasheet_url(raw)
        assert len(result) == 1000


# ── enrich_card_from_sightings tests ──────────────────────────────────


class _FakeCard:
    """Minimal stand-in for MaterialCard."""

    def __init__(self, description=None, manufacturer=None, datasheet_url=None, enrichment_source=None):
        self.description = description
        self.manufacturer = manufacturer
        self.datasheet_url = datasheet_url
        self.enrichment_source = enrichment_source


class TestEnrichCardFromSightings:
    def test_fills_empty_description(self):
        card = _FakeCard()
        sightings = [
            ("digikey", "Samsung", True, {"description": "16GB DDR4 RDIMM"}),
        ]
        updates = enrich_card_from_sightings(card, sightings, dry_run=True)
        assert updates["description"] == "16GB DDR4 RDIMM"

    def test_prefers_authorized_source(self):
        card = _FakeCard()
        sightings = [
            ("brokerbin", None, False, {"description": "DDR4 module broker listing"}),
            ("digikey", "Samsung", True, {"description": "Samsung 16GB DDR4-3200"}),
        ]
        updates = enrich_card_from_sightings(card, sightings, dry_run=True)
        assert updates["description"] == "Samsung 16GB DDR4-3200"

    def test_overwrites_claude_ai_with_authorized(self):
        card = _FakeCard(description="Old claude desc", enrichment_source="claude_ai")
        sightings = [
            ("digikey", "Samsung", True, {"description": "Samsung 16GB DDR4-3200 ECC"}),
        ]
        updates = enrich_card_from_sightings(card, sightings, dry_run=True)
        assert "description" in updates

    def test_keeps_existing_non_claude(self):
        card = _FakeCard(description="Manually entered description", enrichment_source="manual")
        sightings = [
            ("digikey", "Samsung", True, {"description": "Samsung 16GB DDR4-3200 ECC"}),
        ]
        updates = enrich_card_from_sightings(card, sightings, dry_run=True)
        assert "description" not in updates

    def test_fills_empty_manufacturer(self):
        card = _FakeCard()
        sightings = [
            ("digikey", "Texas Instruments", True, {"description": "LM317"}),
        ]
        updates = enrich_card_from_sightings(card, sightings, dry_run=True)
        assert updates["manufacturer"] == "Texas Instruments"

    def test_manufacturer_prefers_authorized(self):
        card = _FakeCard()
        sightings = [
            ("brokerbin", "TI", False, {}),
            ("mouser", "Texas Instruments", True, {}),
        ]
        updates = enrich_card_from_sightings(card, sightings, dry_run=True)
        assert updates["manufacturer"] == "Texas Instruments"

    def test_fills_datasheet_url(self):
        card = _FakeCard()
        sightings = [
            ("oemsecrets", None, True, {"datasheet_url": "https://example.com/ds.pdf"}),
        ]
        updates = enrich_card_from_sightings(card, sightings, dry_run=True)
        assert updates["datasheet_url"] == "https://example.com/ds.pdf"

    def test_no_updates_when_all_populated(self):
        card = _FakeCard(
            description="Existing desc",
            manufacturer="Existing Mfg",
            datasheet_url="https://existing.com/ds.pdf",
            enrichment_source="manual",
        )
        sightings = [
            ("digikey", "Samsung", True, {"description": "New desc"}),
        ]
        updates = enrich_card_from_sightings(card, sightings, dry_run=True)
        assert updates == {}

    def test_empty_sightings(self):
        card = _FakeCard()
        updates = enrich_card_from_sightings(card, [], dry_run=True)
        assert updates == {}

    def test_dry_run_does_not_modify_card(self):
        card = _FakeCard()
        sightings = [
            ("digikey", "Samsung", True, {"description": "New description"}),
        ]
        enrich_card_from_sightings(card, sightings, dry_run=True)
        assert card.description is None  # Not modified in dry run

    def test_apply_modifies_card(self):
        card = _FakeCard()
        sightings = [
            ("digikey", "Samsung", True, {"description": "New description"}),
        ]
        enrich_card_from_sightings(card, sightings, dry_run=False)
        assert card.description == "New description"
        assert card.enrichment_source == "sighting_extraction"
