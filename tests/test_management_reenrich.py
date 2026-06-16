"""Tests for app/management/reenrich.py — bulk re-enrichment command.

Tests the main() async function with mocked DB, models, and services.

Called by: pytest
Depends on: app/management/reenrich.py
"""

import os
import runpy
import sys

os.environ["TESTING"] = "1"

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_db(card_ids=(), cards=()):
    """Build a mock DB session whose two query chains return the given card IDs (first
    query) and full card objects (second, post-enrichment query)."""
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = list(
        card_ids
    )
    mock_db.query.return_value.filter.return_value.all.return_value = list(cards)
    return mock_db


@contextmanager
def _patched_main(mock_db, *, enrich_return=None, enrich_side_effect=None, record_side_effect=None):
    """Patch SessionLocal → mock_db, enrich_material_cards, and record_spec, then yield
    the record_spec mock. Patch targets stay at the source modules.

    NOTE: do NOT patch app.models.MaterialCard here — main() lazily imports
    app.services.spec_write_service, and if that module's FIRST import in this xdist
    worker happens inside such a patch window, its module-level
    `from app.models import MaterialCard` captures the MagicMock permanently, breaking
    every later record_spec test on the same worker. The mocked db makes it unnecessary.
    """
    enrich_kwargs = {"new_callable": AsyncMock}
    if enrich_side_effect is not None:
        enrich_kwargs["side_effect"] = enrich_side_effect
    else:
        enrich_kwargs["return_value"] = enrich_return if enrich_return is not None else {"enriched": 0, "failed": 0}

    with (
        patch("app.database.SessionLocal", MagicMock(return_value=mock_db)),
        patch("app.services.material_enrichment_service.enrich_material_cards", **enrich_kwargs),
        patch("app.services.spec_write_service.record_spec", side_effect=record_side_effect) as mock_record,
    ):
        yield mock_record


class TestReenrichMain:
    @pytest.mark.asyncio
    async def test_main_empty_cards_list(self):
        """Main() with no cards still runs without error and doesn't call
        record_spec."""
        mock_db = _mock_db()

        with _patched_main(mock_db) as mock_record:
            from app.management.reenrich import main

            await main(limit=10, batch_size=5)

        mock_record.assert_not_called()
        mock_db.commit.assert_called_once()
        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_main_calls_record_spec_for_spec_values(self):
        """Main() calls record_spec for each non-None spec value in specs_structured."""
        card = MagicMock()
        card.id = 42
        card.category = "capacitor"
        card.enrichment_source = "ai"
        card.specs_structured = {
            "capacitance": {"value": "100nF"},
            "voltage": {"value": "50V"},
            "tolerance": {"value": None},  # None value should be skipped
        }
        mock_db = _mock_db(card_ids=[(42,)], cards=[card])

        with _patched_main(mock_db, enrich_return={"enriched": 1}) as mock_record:
            from app.management.reenrich import main

            await main(limit=100, batch_size=10)

        # Should be called twice: capacitance and voltage (tolerance has None value)
        assert mock_record.call_count == 2
        call_args = [call.args for call in mock_record.call_args_list]
        spec_keys = [args[2] for args in call_args]  # db, card_id, spec_key, value...
        assert "capacitance" in spec_keys
        assert "voltage" in spec_keys

    @pytest.mark.asyncio
    async def test_backfill_counts_only_persisted_writes(self):
        """A record_spec rejection (no-schema / enum drift / ladder loss) counts as
        skipped, never as backfilled — the closing log must not overstate what the facet
        projection actually holds."""
        card = MagicMock()
        card.id = 42
        card.category = "dram"
        card.specs_structured = {
            "ddr_type": {"value": "DDR4"},  # persists
            "bogus_key": {"value": "x"},  # rejected by the gates → skipped
            "speed_mhz": {"value": "junk"},  # rejected → skipped
        }
        mock_db = _mock_db(card_ids=[(42,)], cards=[card])

        from loguru import logger

        messages: list[str] = []
        sink_id = logger.add(lambda m: messages.append(str(m)), level="INFO")
        try:
            with _patched_main(
                mock_db, enrich_return={"enriched": 1}, record_side_effect=[True, False, False]
            ) as mock_record:
                from app.management.reenrich import main

                await main(limit=10, batch_size=5)
        finally:
            logger.remove(sink_id)

        assert mock_record.call_count == 3
        assert any("Backfilled 1 facet rows (2 entries skipped by schema/enum/ladder gates)" in m for m in messages), (
            messages
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("card_id", "category", "specs_structured"),
        [
            pytest.param(10, "resistor", None, id="no_specs_structured"),
            pytest.param(20, None, {"voltage": {"value": "50V"}}, id="no_category"),
        ],
    )
    async def test_main_skips_card_without_required_fields(self, card_id, category, specs_structured):
        """Main() skips record_spec for cards missing specs_structured or category."""
        card = MagicMock()
        card.id = card_id
        card.category = category
        card.specs_structured = specs_structured
        mock_db = _mock_db(card_ids=[(card_id,)], cards=[card])

        with _patched_main(mock_db, enrich_return={"enriched": 0}) as mock_record:
            from app.management.reenrich import main

            await main()

        mock_record.assert_not_called()

    @pytest.mark.asyncio
    async def test_main_handles_plain_string_spec_values(self):
        """Main() handles specs_structured where values are plain strings (not
        dicts)."""
        card = MagicMock()
        card.id = 30
        card.category = "transistor"
        card.enrichment_source = None
        card.specs_structured = {
            "package": "TO-92",  # plain string value, not dict
            "gain": None,  # None plain value, should skip
        }
        mock_db = _mock_db(card_ids=[(30,)], cards=[card])

        with _patched_main(mock_db, enrich_return={"enriched": 1}) as mock_record:
            from app.management.reenrich import main

            await main()

        # Only "package" has a non-None value. A plain (non-dict) legacy value carries
        # no per-entry provenance, so the backfill re-records it as spec_extraction —
        # a REGISTERED ladder source (an arbitrary tag like "reenrich" would rank at
        # tier 0 and lose to every ranked source).
        assert mock_record.call_count == 1
        call = mock_record.call_args
        assert call.args[2] == "package"
        assert call.args[3] == "TO-92"
        assert call.kwargs.get("source") == "spec_extraction"
        assert call.kwargs.get("confidence") == 0.85

    @pytest.mark.asyncio
    async def test_main_preserves_explicit_zero_confidence(self):
        """An entry with stored confidence 0.0 re-records at 0.0 — a falsy-`or` fallback
        would inflate it to 0.85, and the same-source equal-tier re-record would then
        PERSIST that manufactured confidence (0.85 > 0.0 wins the ladder), letting the
        entry beat same-tier sources it never legitimately outranked.

        The 0.85 default applies only to entries with NO stored confidence.
        """
        card = MagicMock()
        card.id = 50
        card.category = "dram"
        card.specs_structured = {
            "ddr_type": {"value": "DDR4", "source": "spec_extraction", "confidence": 0.0},
            "capacity_gb": {"value": 16, "source": "spec_extraction"},  # no confidence key
        }
        mock_db = _mock_db(card_ids=[(50,)], cards=[card])

        with _patched_main(mock_db, enrich_return={"enriched": 1}) as mock_record:
            from app.management.reenrich import main

            await main()

        assert mock_record.call_count == 2
        by_key = {call.args[2]: call for call in mock_record.call_args_list}
        assert by_key["ddr_type"].kwargs["confidence"] == 0.0  # preserved, NOT inflated
        assert by_key["capacity_gb"].kwargs["confidence"] == 0.85  # default only when absent

    @pytest.mark.asyncio
    async def test_main_db_always_closed(self):
        """Main() closes DB session even if enrichment raises."""
        mock_db = _mock_db()

        with _patched_main(mock_db, enrich_side_effect=RuntimeError("enrichment crashed")):
            from app.management.reenrich import main

            with pytest.raises(RuntimeError):
                await main()

        mock_db.close.assert_called_once()


class TestReenrichEntrypoint:
    def test_main_block_runs_asyncio(self):
        """The __main__ block parses args and calls asyncio.run(main(...))."""
        with (
            patch.object(sys, "argv", ["reenrich", "--limit", "10", "--batch-size", "5"]),
            patch("asyncio.run") as mock_run,
        ):
            sys.modules.pop("app.management.reenrich", None)
            runpy.run_module("app.management.reenrich", run_name="__main__", alter_sys=False)

        mock_run.assert_called_once()

    def test_main_block_uses_default_args(self):
        """The __main__ block defaults to limit=500, batch_size=30."""
        import asyncio

        with (
            patch.object(sys, "argv", ["reenrich"]),
            patch("asyncio.run") as mock_run,
        ):
            sys.modules.pop("app.management.reenrich", None)
            runpy.run_module("app.management.reenrich", run_name="__main__", alter_sys=False)

        mock_run.assert_called_once()
        coro = mock_run.call_args[0][0]
        assert asyncio.iscoroutine(coro)
        coro.close()
