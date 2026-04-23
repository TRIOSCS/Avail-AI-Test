"""Tests for app/management/reenrich.py — bulk re-enrichment command.

Tests the main() async function with mocked DB, models, and services.

Called by: pytest
Depends on: app/management/reenrich.py
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestReenrichMain:
    @pytest.mark.asyncio
    async def test_main_empty_cards_list(self):
        """main() with no cards still runs without error and doesn't call record_spec."""
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        mock_db.query.return_value.filter.return_value.all.return_value = []
        mock_session_cls = MagicMock(return_value=mock_db)

        with (
            patch("app.database.SessionLocal", mock_session_cls),
            patch("app.models.MaterialCard", MagicMock()),
            patch(
                "app.services.material_enrichment_service.enrich_material_cards",
                new_callable=AsyncMock,
                return_value={"enriched": 0, "failed": 0},
            ),
            patch("app.services.spec_write_service.record_spec") as mock_record,
        ):
            from app.management.reenrich import main

            await main(limit=10, batch_size=5)

        mock_record.assert_not_called()
        mock_db.commit.assert_called_once()
        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_main_calls_record_spec_for_spec_values(self):
        """main() calls record_spec for each non-None spec value in specs_structured."""
        mock_db = MagicMock()

        card = MagicMock()
        card.id = 42
        card.category = "capacitor"
        card.enrichment_source = "ai"
        card.specs_structured = {
            "capacitance": {"value": "100nF"},
            "voltage": {"value": "50V"},
            "tolerance": {"value": None},  # None value should be skipped
        }

        # First query returns card IDs
        mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            (42,)
        ]
        # Second query (after enrichment) returns full cards
        mock_db.query.return_value.filter.return_value.all.return_value = [card]

        mock_session_cls = MagicMock(return_value=mock_db)

        with (
            patch("app.database.SessionLocal", mock_session_cls),
            patch(
                "app.services.material_enrichment_service.enrich_material_cards",
                new_callable=AsyncMock,
                return_value={"enriched": 1},
            ),
            patch("app.services.spec_write_service.record_spec") as mock_record,
        ):
            from app.management.reenrich import main

            await main(limit=100, batch_size=10)

        # Should be called twice: capacitance and voltage (tolerance has None value)
        assert mock_record.call_count == 2
        call_args = [call.args for call in mock_record.call_args_list]
        spec_keys = [args[2] for args in call_args]  # db, card_id, spec_key, value...
        assert "capacitance" in spec_keys
        assert "voltage" in spec_keys

    @pytest.mark.asyncio
    async def test_main_skips_card_without_specs_structured(self):
        """main() skips record_spec for cards with None specs_structured."""
        mock_db = MagicMock()

        card = MagicMock()
        card.id = 10
        card.category = "resistor"
        card.specs_structured = None  # No specs

        mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            (10,)
        ]
        mock_db.query.return_value.filter.return_value.all.return_value = [card]
        mock_session_cls = MagicMock(return_value=mock_db)

        with (
            patch("app.database.SessionLocal", mock_session_cls),
            patch(
                "app.services.material_enrichment_service.enrich_material_cards",
                new_callable=AsyncMock,
                return_value={"enriched": 0},
            ),
            patch("app.services.spec_write_service.record_spec") as mock_record,
        ):
            from app.management.reenrich import main

            await main()

        mock_record.assert_not_called()

    @pytest.mark.asyncio
    async def test_main_skips_card_without_category(self):
        """main() skips record_spec for cards with None category."""
        mock_db = MagicMock()

        card = MagicMock()
        card.id = 20
        card.category = None  # No category
        card.specs_structured = {"voltage": {"value": "50V"}}

        mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            (20,)
        ]
        mock_db.query.return_value.filter.return_value.all.return_value = [card]
        mock_session_cls = MagicMock(return_value=mock_db)

        with (
            patch("app.database.SessionLocal", mock_session_cls),
            patch(
                "app.services.material_enrichment_service.enrich_material_cards",
                new_callable=AsyncMock,
                return_value={"enriched": 0},
            ),
            patch("app.services.spec_write_service.record_spec") as mock_record,
        ):
            from app.management.reenrich import main

            await main()

        mock_record.assert_not_called()

    @pytest.mark.asyncio
    async def test_main_handles_plain_string_spec_values(self):
        """main() handles specs_structured where values are plain strings (not dicts)."""
        mock_db = MagicMock()

        card = MagicMock()
        card.id = 30
        card.category = "transistor"
        card.enrichment_source = None  # tests the "reenrich" fallback source
        card.specs_structured = {
            "package": "TO-92",  # plain string value, not dict
            "gain": None,  # None plain value, should skip
        }

        mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            (30,)
        ]
        mock_db.query.return_value.filter.return_value.all.return_value = [card]
        mock_session_cls = MagicMock(return_value=mock_db)

        with (
            patch("app.database.SessionLocal", mock_session_cls),
            patch(
                "app.services.material_enrichment_service.enrich_material_cards",
                new_callable=AsyncMock,
                return_value={"enriched": 1},
            ),
            patch("app.services.spec_write_service.record_spec") as mock_record,
        ):
            from app.management.reenrich import main

            await main()

        # Only "package" has a non-None value
        assert mock_record.call_count == 1
        call = mock_record.call_args
        assert call.args[2] == "package"
        assert call.args[3] == "TO-92"
        assert call.kwargs.get("source") == "reenrich"

    @pytest.mark.asyncio
    async def test_main_db_always_closed(self):
        """main() closes DB session even if enrichment raises."""
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        mock_session_cls = MagicMock(return_value=mock_db)

        with (
            patch("app.database.SessionLocal", mock_session_cls),
            patch(
                "app.services.material_enrichment_service.enrich_material_cards",
                new_callable=AsyncMock,
                side_effect=RuntimeError("enrichment crashed"),
            ),
        ):
            from app.management.reenrich import main

            with pytest.raises(RuntimeError):
                await main()

        mock_db.close.assert_called_once()
