"""Tests for app/services/tagging_ai_classify.py — AI classification with mocked Claude.

Called by: pytest
Depends on: conftest fixtures, mocked claude_client
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from app.models.intelligence import MaterialCard
from app.models.tags import MaterialTag, Tag
from app.services.tagging_ai_classify import _apply_ai_results, classify_parts_with_ai


class TestClassifyPartsWithAi:
    async def test_successful_classification(self):
        mock_result = [
            {
                "mpn": "STM32F103",
                "manufacturer": "STMicroelectronics",
                "category": "Microcontrollers (MCU)",
                "confidence": 0.95,
            },
            {
                "mpn": "LM317T",
                "manufacturer": "Texas Instruments",
                "category": "Voltage Regulators",
                "confidence": 0.92,
            },
        ]
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_result):
            result = await classify_parts_with_ai(["STM32F103", "LM317T"])
        assert len(result) == 2
        assert result[0]["manufacturer"] == "STMicroelectronics"
        assert result[1]["category"] == "Voltage Regulators"

    async def test_claude_unavailable_returns_unknown(self):
        from app.utils.claude_errors import ClaudeUnavailableError

        with patch(
            "app.utils.claude_client.claude_json",
            new_callable=AsyncMock,
            side_effect=ClaudeUnavailableError("not configured"),
        ):
            result = await classify_parts_with_ai(["ABC123"])
        assert len(result) == 1
        assert result[0]["manufacturer"] == "Unknown"
        assert result[0]["category"] == "Miscellaneous"

    async def test_claude_error_returns_fallback(self):
        from app.utils.claude_errors import ClaudeError

        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, side_effect=ClaudeError("timeout")):
            result = await classify_parts_with_ai(["XYZ789"])
        assert len(result) == 1
        assert result[0]["manufacturer"] == "Unknown"

    async def test_invalid_response_returns_fallback(self):
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value="not a list"):
            result = await classify_parts_with_ai(["BAD123"])
        assert len(result) == 1
        assert result[0]["manufacturer"] == "Unknown"

    async def test_empty_response_returns_fallback(self):
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=None):
            result = await classify_parts_with_ai(["EMPTY1"])
        assert len(result) == 1
        assert result[0]["manufacturer"] == "Unknown"

    async def test_null_manufacturer_normalized(self):
        mock_result = [
            {"mpn": "CUSTOM123", "manufacturer": None, "category": None, "confidence": 0.3},
        ]
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_result):
            result = await classify_parts_with_ai(["CUSTOM123"])
        assert len(result) == 1
        assert result[0]["manufacturer"] == "Unknown"
        assert result[0]["category"] == "Miscellaneous"

    async def test_multiple_parts_batch(self):
        mpns = ["STM32F103", "LM317T", "IRF540N", "GRM188R61E106MA73"]
        mock_result = [
            {"mpn": m, "manufacturer": f"Mfr-{i}", "category": f"Cat-{i}", "confidence": 0.95}
            for i, m in enumerate(mpns)
        ]
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_result):
            result = await classify_parts_with_ai(mpns)
        assert len(result) == 4
        assert all(r["mpn"] for r in result)

    async def test_non_dict_items_skipped(self):
        """Non-dict items in the AI response are skipped, valid ones kept."""
        mock_result = ["junk string", None, 42, {"mpn": "OK1", "manufacturer": "TI", "category": "MCU"}]
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_result):
            result = await classify_parts_with_ai(["OK1"])
        assert len(result) == 1
        assert result[0]["mpn"] == "OK1"
        assert result[0]["manufacturer"] == "TI"


def _make_card(db, mpn, manufacturer=None):
    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        manufacturer=manufacturer,
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


class TestApplyAiResults:
    """Exercise _apply_ai_results — applies classification to DB and tags cards."""

    def test_matched_part_tags_card_and_sets_manufacturer(self, db_session):
        # Commodity tags are pre-seeded; get_or_create_commodity_tag won't create them.
        db_session.add(Tag(name="Microcontrollers (MCU)", tag_type="commodity", created_at=datetime.now(timezone.utc)))
        db_session.commit()
        card = _make_card(db_session, "STM32F103")
        classified = [
            {"mpn": "stm32f103", "manufacturer": "STMicroelectronics", "category": "Microcontrollers (MCU)"},
        ]
        batch = [(card.id, "stm32f103")]

        matched, unknown = _apply_ai_results(classified, batch, db_session)

        assert (matched, unknown) == (1, 0)
        # _apply_ai_results mutates the in-session card; caller commits.
        assert card.manufacturer == "STMicroelectronics"
        db_session.flush()
        # Brand + commodity tags applied at confidence 0.92
        tags = db_session.query(MaterialTag).filter_by(material_card_id=card.id).all()
        assert len(tags) == 2
        assert all(t.source == "ai_classified" for t in tags)
        assert all(abs(t.confidence - 0.92) < 1e-6 for t in tags)
        tag_types = {db_session.get(Tag, t.tag_id).tag_type for t in tags}
        assert tag_types == {"brand", "commodity"}

    def test_unknown_part_low_confidence_brand_only(self, db_session):
        card = _make_card(db_session, "CUSTOMPART9")
        classified = [
            {"mpn": "custompart9", "manufacturer": "Unknown", "category": "Miscellaneous"},
        ]
        batch = [(card.id, "custompart9")]

        matched, unknown = _apply_ai_results(classified, batch, db_session)

        assert (matched, unknown) == (0, 1)
        # Unknown manufacturer is not written onto the card
        assert not card.manufacturer
        tags = db_session.query(MaterialTag).filter_by(material_card_id=card.id).all()
        # Only the brand tag — Miscellaneous category produces no commodity tag
        assert len(tags) == 1
        assert abs(tags[0].confidence - 0.3) < 1e-6

    def test_missing_classification_falls_back_to_unknown(self, db_session):
        """A card with no matching AI result is treated as Unknown."""
        card = _make_card(db_session, "NORESULT1")
        # classified does not contain this MPN
        batch = [(card.id, "noresult1")]

        matched, unknown = _apply_ai_results([], batch, db_session)

        assert (matched, unknown) == (0, 1)
        tags = db_session.query(MaterialTag).filter_by(material_card_id=card.id).all()
        assert len(tags) == 1

    def test_matched_part_keeps_existing_manufacturer(self, db_session):
        """A matched part does not overwrite a manufacturer already on the card."""
        card = _make_card(db_session, "LM317T", manufacturer="Texas Instruments")
        classified = [
            {"mpn": "lm317t", "manufacturer": "TI Clone Corp", "category": "Voltage Regulators"},
        ]
        batch = [(card.id, "lm317t")]

        matched, unknown = _apply_ai_results(classified, batch, db_session)

        assert (matched, unknown) == (1, 0)
        db_session.refresh(card)
        assert card.manufacturer == "Texas Instruments"

    def test_mixed_batch_counts(self, db_session):
        c1 = _make_card(db_session, "KNOWNPART1")
        c2 = _make_card(db_session, "UNKNOWNPART2")
        classified = [
            {"mpn": "knownpart1", "manufacturer": "Murata", "category": "Capacitors"},
            {"mpn": "unknownpart2", "manufacturer": "Unknown", "category": "Miscellaneous"},
        ]
        batch = [(c1.id, "knownpart1"), (c2.id, "unknownpart2")]

        matched, unknown = _apply_ai_results(classified, batch, db_session)

        assert (matched, unknown) == (1, 1)
