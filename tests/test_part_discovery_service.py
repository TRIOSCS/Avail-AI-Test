"""test_part_discovery_service.py — Comprehensive tests for
app/services/part_discovery_service.py.

Covers: expand_cross_references, expand_families, fill_commodity_gaps.

Called by: pytest
Depends on: app.services.part_discovery_service, conftest fixtures
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.intelligence import MaterialCard

# ── Helpers ────────────────────────────────────────────────────────────


def _create_material_card(
    db, mpn, normalized_mpn=None, manufacturer=None, cross_references=None, search_count=0, category=None
):
    """Helper to create a MaterialCard in the test DB."""
    from app.utils.normalization import normalize_mpn_key

    card = MaterialCard(
        display_mpn=mpn,
        normalized_mpn=normalized_mpn or normalize_mpn_key(mpn),
        manufacturer=manufacturer,
        cross_references=cross_references,
        search_count=search_count,
        category=category,
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


# ── expand_cross_references() ──────────────────────────────────────────


class TestExpandCrossReferences:
    """Tests for Strategy A: cross-reference expansion."""

    def test_no_cards_with_cross_refs(self, db_session):
        """Returns zero stats when no cards have cross_references."""
        from app.services.part_discovery_service import expand_cross_references

        result = asyncio.run(expand_cross_references(db_session, limit=100))
        assert result["checked"] == 0
        assert result["created"] == 0

    def test_creates_cards_from_cross_refs(self, db_session):
        """Creates new MaterialCards for cross-referenced MPNs."""
        _create_material_card(
            db_session,
            "LM317T",
            manufacturer="Texas Instruments",
            cross_references=[
                {"mpn": "LM317LZ", "manufacturer": "ON Semiconductor"},
                {"mpn": "UA317", "manufacturer": "TI"},
            ],
        )

        mock_card = MagicMock()
        mock_card.manufacturer = None

        with patch("app.search_service.resolve_material_card", return_value=mock_card) as mock_resolve:
            from app.services.part_discovery_service import expand_cross_references

            result = asyncio.run(expand_cross_references(db_session, limit=100))

        assert result["checked"] == 2
        assert result["created"] == 2
        assert mock_resolve.call_count == 2

    def test_skips_existing_mpns(self, db_session):
        """Does not create cards for MPNs that already exist."""
        _create_material_card(db_session, "LM317LZ")
        _create_material_card(
            db_session,
            "LM317T",
            cross_references=[{"mpn": "LM317LZ"}],
        )

        with patch("app.search_service.resolve_material_card") as mock_resolve:
            from app.services.part_discovery_service import expand_cross_references

            result = asyncio.run(expand_cross_references(db_session, limit=100))

        assert result["already_exists"] == 1
        assert result["created"] == 0
        mock_resolve.assert_not_called()

    def test_handles_non_list_cross_refs(self, db_session):
        """Gracefully handles cross_references that aren't a list."""
        _create_material_card(
            db_session,
            "LM317T",
            cross_references="not a list",
        )

        from app.services.part_discovery_service import expand_cross_references

        result = asyncio.run(expand_cross_references(db_session, limit=100))
        assert result["checked"] == 0

    def test_handles_non_dict_refs(self, db_session):
        """Gracefully handles cross_reference entries that aren't dicts."""
        _create_material_card(
            db_session,
            "LM317T",
            cross_references=["just a string", None, 42],
        )

        from app.services.part_discovery_service import expand_cross_references

        result = asyncio.run(expand_cross_references(db_session, limit=100))
        assert result["checked"] == 0

    def test_handles_missing_mpn_in_ref(self, db_session):
        """Skips cross_reference dicts without 'mpn' key."""
        _create_material_card(
            db_session,
            "LM317T",
            cross_references=[{"manufacturer": "TI"}],  # No "mpn"
        )

        from app.services.part_discovery_service import expand_cross_references

        result = asyncio.run(expand_cross_references(db_session, limit=100))
        assert result["checked"] == 0

    def test_sets_manufacturer_from_crossref(self, db_session):
        """Sets manufacturer on new card from cross_reference data."""
        _create_material_card(
            db_session,
            "LM317T",
            manufacturer="TI",
            cross_references=[{"mpn": "NEW-PART-123", "manufacturer": "ON Semi"}],
        )

        mock_card = MagicMock()
        mock_card.manufacturer = None

        with patch("app.search_service.resolve_material_card", return_value=mock_card):
            from app.services.part_discovery_service import expand_cross_references

            asyncio.run(expand_cross_references(db_session, limit=100))

        # Should set manufacturer from cross-ref
        assert mock_card.manufacturer == "ON Semi"

    def test_falls_back_to_parent_manufacturer(self, db_session):
        """Falls back to parent card manufacturer when cross-ref has none."""
        _create_material_card(
            db_session,
            "LM317T",
            manufacturer="Texas Instruments",
            cross_references=[{"mpn": "NEW-PART-456"}],
        )

        mock_card = MagicMock()
        mock_card.manufacturer = None

        with patch("app.search_service.resolve_material_card", return_value=mock_card):
            from app.services.part_discovery_service import expand_cross_references

            asyncio.run(expand_cross_references(db_session, limit=100))

        assert mock_card.manufacturer == "Texas Instruments"

    def test_resolve_returns_none(self, db_session):
        """Handles resolve_material_card returning None."""
        _create_material_card(
            db_session,
            "LM317T",
            cross_references=[{"mpn": "GHOST-PART"}],
        )

        with patch("app.search_service.resolve_material_card", return_value=None):
            from app.services.part_discovery_service import expand_cross_references

            result = asyncio.run(expand_cross_references(db_session, limit=100))

        assert result["checked"] == 1
        assert result["created"] == 0

    def test_resolve_exception_counts_as_error(self, db_session):
        """Exception during resolve_material_card increments errors count."""
        _create_material_card(
            db_session,
            "LM317T",
            cross_references=[{"mpn": "BAD-PART"}],
        )

        with patch("app.search_service.resolve_material_card", side_effect=Exception("API down")):
            from app.services.part_discovery_service import expand_cross_references

            result = asyncio.run(expand_cross_references(db_session, limit=100))

        assert result["errors"] == 1


# ── expand_families() ──────────────────────────────────────────────────


class TestExpandFamilies:
    """Tests for Strategy B: AI-driven cross/substitute expansion."""

    def test_no_seed_cards(self, db_session):
        """Returns zero stats when no seed cards qualify."""
        from app.services.part_discovery_service import expand_families

        result = asyncio.run(expand_families(db_session, batch_size=10))
        assert result["seed_cards"] == 0
        assert result["created"] == 0

    def test_happy_path_creates_cards(self, db_session):
        """Creates new cards from AI-discovered family members."""
        _create_material_card(
            db_session,
            "STM32F103C8T6",
            manufacturer="STMicroelectronics",
            search_count=5,
            category="microcontrollers",
        )

        ai_response = {
            "families": [
                {
                    "seed_mpn": "STM32F103C8T6",
                    "family_members": ["GD32F103C8T6", "APM32F103C8T6"],
                }
            ]
        }
        mock_card = MagicMock()
        mock_card.manufacturer = None

        with (
            patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=ai_response),
            patch("app.search_service.resolve_material_card", return_value=mock_card) as mock_resolve,
        ):
            from app.services.part_discovery_service import expand_families

            result = asyncio.run(expand_families(db_session, batch_size=10))

        assert result["seed_cards"] == 1
        assert result["discovered"] == 2
        assert result["created"] == 2

    def test_skips_existing_family_members(self, db_session):
        """Does not create cards for family members that already exist."""
        _create_material_card(
            db_session,
            "STM32F103C8T6",
            manufacturer="STMicroelectronics",
            search_count=5,
            category="microcontrollers",
        )
        _create_material_card(db_session, "GD32F103C8T6")

        ai_response = {
            "families": [
                {
                    "seed_mpn": "STM32F103C8T6",
                    "family_members": ["GD32F103C8T6"],
                }
            ]
        }

        with (
            patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=ai_response),
            patch("app.search_service.resolve_material_card") as mock_resolve,
        ):
            from app.services.part_discovery_service import expand_families

            result = asyncio.run(expand_families(db_session, batch_size=10))

        assert result["already_exists"] == 1
        mock_resolve.assert_not_called()

    def test_ai_returns_none(self, db_session):
        """Handles AI returning None gracefully."""
        _create_material_card(
            db_session,
            "LM317T",
            search_count=5,
            category="voltage_regulators",
        )

        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=None):
            from app.services.part_discovery_service import expand_families

            result = asyncio.run(expand_families(db_session, batch_size=10))

        assert result["created"] == 0

    def test_ai_returns_no_families_key(self, db_session):
        """Handles AI response without 'families' key."""
        _create_material_card(
            db_session,
            "LM317T",
            search_count=5,
            category="voltage_regulators",
        )

        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value={"error": "bad"}):
            from app.services.part_discovery_service import expand_families

            result = asyncio.run(expand_families(db_session, batch_size=10))

        assert result["created"] == 0

    def test_skips_empty_or_non_string_members(self, db_session):
        """Skips None, empty, and non-string family members."""
        _create_material_card(
            db_session,
            "LM317T",
            search_count=5,
            category="voltage_regulators",
        )

        ai_response = {
            "families": [
                {
                    "seed_mpn": "LM317T",
                    "family_members": [None, "", 42, "VALID-PART"],
                }
            ]
        }
        mock_card = MagicMock()

        with (
            patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=ai_response),
            patch("app.search_service.resolve_material_card", return_value=mock_card),
        ):
            from app.services.part_discovery_service import expand_families

            result = asyncio.run(expand_families(db_session, batch_size=10))

        assert result["discovered"] == 1  # Only "VALID-PART"
        assert result["created"] == 1

    def test_resolve_error_increments_errors(self, db_session):
        """Exception during resolve_material_card is counted as error."""
        _create_material_card(
            db_session,
            "LM317T",
            search_count=5,
            category="voltage_regulators",
        )

        ai_response = {"families": [{"seed_mpn": "LM317T", "family_members": ["BAD-PART"]}]}

        with (
            patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=ai_response),
            patch("app.search_service.resolve_material_card", side_effect=Exception("resolve failed")),
        ):
            from app.services.part_discovery_service import expand_families

            result = asyncio.run(expand_families(db_session, batch_size=10))

        assert result["errors"] == 1

    def test_ai_api_exception(self, db_session):
        """AI API failure for a batch increments errors, continues."""
        _create_material_card(
            db_session,
            "LM317T",
            search_count=5,
            category="voltage_regulators",
        )

        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, side_effect=Exception("API error")):
            from app.services.part_discovery_service import expand_families

            result = asyncio.run(expand_families(db_session, batch_size=10))

        assert result["errors"] >= 1


# ── fill_commodity_gaps() ──────────────────────────────────────────────


class TestFillCommodityGaps:
    """Tests for Strategy C: commodity gap fill."""

    def test_no_small_categories(self, db_session):
        """Returns zero stats when all categories have >= 1000 cards."""
        # Don't create any cards — COMMODITY_MAP categories will have 0 cards
        # which means they're "small", so we need to mock COMMODITY_MAP to be empty
        with patch("app.services.part_discovery_service.COMMODITY_MAP", {"other": []}):
            from app.services.part_discovery_service import fill_commodity_gaps

            result = asyncio.run(fill_commodity_gaps(db_session))

        assert result["categories_checked"] == 0

    def test_happy_path_creates_cards(self, db_session):
        """Creates new cards from AI-discovered commodity parts."""
        ai_response = {
            "parts": [
                {"mpn": "GRM188R71H104KA93D", "manufacturer": "Murata"},
                {"mpn": "CL10B104KB8NNNC", "manufacturer": "Samsung"},
            ]
        }

        mock_card = MagicMock()
        mock_card.manufacturer = None
        mock_card.category = None

        with (
            patch(
                "app.services.part_discovery_service.COMMODITY_MAP",
                {"capacitors": ["capacitor"], "other": []},
            ),
            patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=ai_response),
            patch("app.search_service.resolve_material_card", return_value=mock_card),
        ):
            from app.services.part_discovery_service import fill_commodity_gaps

            result = asyncio.run(fill_commodity_gaps(db_session))

        assert result["categories_checked"] == 1
        assert result["discovered"] == 2
        assert result["created"] == 2

    def test_skips_existing_parts(self, db_session):
        """Does not create cards for parts that already exist."""
        _create_material_card(db_session, "GRM188R71H104KA93D")

        ai_response = {
            "parts": [
                {"mpn": "GRM188R71H104KA93D", "manufacturer": "Murata"},
            ]
        }

        with (
            patch(
                "app.services.part_discovery_service.COMMODITY_MAP",
                {"capacitors": ["capacitor"], "other": []},
            ),
            patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=ai_response),
            patch("app.search_service.resolve_material_card") as mock_resolve,
        ):
            from app.services.part_discovery_service import fill_commodity_gaps

            result = asyncio.run(fill_commodity_gaps(db_session))

        assert result["already_exists"] == 1
        mock_resolve.assert_not_called()

    def test_ai_returns_none(self, db_session):
        """AI returning None for a category is handled gracefully."""
        with (
            patch(
                "app.services.part_discovery_service.COMMODITY_MAP",
                {"capacitors": ["capacitor"], "other": []},
            ),
            patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=None),
        ):
            from app.services.part_discovery_service import fill_commodity_gaps

            result = asyncio.run(fill_commodity_gaps(db_session))

        assert result["created"] == 0

    def test_ai_returns_no_parts_key(self, db_session):
        """AI response without 'parts' key is handled gracefully."""
        with (
            patch(
                "app.services.part_discovery_service.COMMODITY_MAP",
                {"capacitors": ["capacitor"], "other": []},
            ),
            patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value={"error": "oops"}),
        ):
            from app.services.part_discovery_service import fill_commodity_gaps

            result = asyncio.run(fill_commodity_gaps(db_session))

        assert result["created"] == 0

    def test_skips_empty_mpn(self, db_session):
        """Skips parts with empty MPN strings."""
        ai_response = {"parts": [{"mpn": "", "manufacturer": "Murata"}, {"mpn": "  ", "manufacturer": "TI"}]}

        with (
            patch(
                "app.services.part_discovery_service.COMMODITY_MAP",
                {"capacitors": ["capacitor"], "other": []},
            ),
            patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=ai_response),
        ):
            from app.services.part_discovery_service import fill_commodity_gaps

            result = asyncio.run(fill_commodity_gaps(db_session))

        assert result["discovered"] == 0

    def test_resolve_exception_handled(self, db_session):
        """Exception during resolve_material_card doesn't stop processing."""
        ai_response = {
            "parts": [
                {"mpn": "BAD-PART", "manufacturer": "Unknown"},
                {"mpn": "GOOD-PART", "manufacturer": "TI"},
            ]
        }
        mock_card = MagicMock()
        mock_card.manufacturer = None
        mock_card.category = None

        call_count = [0]

        def _side_effect(mpn, db):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Resolve failed")
            return mock_card

        with (
            patch(
                "app.services.part_discovery_service.COMMODITY_MAP",
                {"capacitors": ["capacitor"], "other": []},
            ),
            patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=ai_response),
            patch("app.search_service.resolve_material_card", side_effect=_side_effect),
        ):
            from app.services.part_discovery_service import fill_commodity_gaps

            result = asyncio.run(fill_commodity_gaps(db_session))

        assert result["created"] == 1

    def test_ai_exception_for_category(self, db_session):
        """AI API error for one category doesn't stop others."""
        with (
            patch(
                "app.services.part_discovery_service.COMMODITY_MAP",
                {"capacitors": ["capacitor"], "resistors": ["resistor"], "other": []},
            ),
            patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, side_effect=Exception("API down")),
        ):
            from app.services.part_discovery_service import fill_commodity_gaps

            result = asyncio.run(fill_commodity_gaps(db_session))

        # Should handle error and return — no exception raised
        assert result["categories_checked"] >= 1

    def test_sets_manufacturer_and_category(self, db_session):
        """Sets manufacturer and category on newly created cards."""
        ai_response = {
            "parts": [
                {"mpn": "NEW-CAP-001", "manufacturer": "Murata Manufacturing"},
            ]
        }
        mock_card = MagicMock()
        mock_card.manufacturer = None
        mock_card.category = None

        with (
            patch(
                "app.services.part_discovery_service.COMMODITY_MAP",
                {"capacitors": ["capacitor"], "other": []},
            ),
            patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=ai_response),
            patch("app.search_service.resolve_material_card", return_value=mock_card),
        ):
            from app.services.part_discovery_service import fill_commodity_gaps

            asyncio.run(fill_commodity_gaps(db_session))

        assert mock_card.manufacturer == "Murata Manufacturing"
        assert mock_card.category == "capacitors"

    def test_limits_to_10_categories(self, db_session):
        """Processes at most 10 categories per run."""
        many_categories = {f"cat_{i}": [f"kw_{i}"] for i in range(15)}
        many_categories["other"] = []

        with (
            patch("app.services.part_discovery_service.COMMODITY_MAP", many_categories),
            patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value={"parts": []}),
        ):
            from app.services.part_discovery_service import fill_commodity_gaps

            result = asyncio.run(fill_commodity_gaps(db_session))

        assert result["categories_checked"] <= 10
