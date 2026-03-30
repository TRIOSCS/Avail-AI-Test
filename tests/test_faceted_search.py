"""Tests for faceted search service — FTS upgrade.

Called by: pytest
Depends on: conftest.py, MaterialCard model
"""

from app.models.intelligence import MaterialCard
from app.services.faceted_search_service import search_materials_faceted


class TestFacetedSearchFTS:
    def test_search_by_description_keyword(self, db_session):
        """FTS finds cards by description content (ILIKE fallback in SQLite)."""
        card = MaterialCard(
            normalized_mpn="stm32f407vgt6",
            display_mpn="STM32F407VGT6",
            manufacturer="STMicroelectronics",
            description="32-bit ARM Cortex-M4 microcontroller with FPU",
            category="microcontroller",
        )
        db_session.add(card)
        db_session.commit()

        # ILIKE fallback searches description for substring
        results, total = search_materials_faceted(db_session, q="microcontroller")
        assert total >= 1
        assert any(r.normalized_mpn == "stm32f407vgt6" for r in results)

    def test_search_by_partial_mpn(self, db_session):
        """Partial MPN prefix search works."""
        card = MaterialCard(
            normalized_mpn="lm7805ct",
            display_mpn="LM7805CT",
            manufacturer="Texas Instruments",
            category="voltage_regulator",
        )
        db_session.add(card)
        db_session.commit()

        results, total = search_materials_faceted(db_session, q="LM78")
        assert total >= 1

    def test_search_no_results(self, db_session):
        """Search for nonexistent term returns empty."""
        results, total = search_materials_faceted(db_session, q="ZZZZNOTFOUND")
        assert total == 0
        assert results == []

    def test_filter_by_commodity(self, db_session):
        """Commodity filter narrows to matching category."""
        card1 = MaterialCard(
            normalized_mpn="stm32f4",
            display_mpn="STM32F4",
            category="microcontroller",
        )
        card2 = MaterialCard(
            normalized_mpn="lm7805",
            display_mpn="LM7805",
            category="voltage_regulator",
        )
        db_session.add_all([card1, card2])
        db_session.commit()

        results, total = search_materials_faceted(db_session, commodity="microcontroller")
        assert total == 1
        assert results[0].normalized_mpn == "stm32f4"
