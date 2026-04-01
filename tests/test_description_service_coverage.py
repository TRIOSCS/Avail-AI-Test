"""test_description_service_coverage.py — Coverage for _collect_db_descriptions.

Targets lines 30-99 (DB collection logic) by patching SessionLocal to use
the test in-memory DB instead of a real PostgreSQL connection.

Called by: pytest
Depends on: app/services/description_service.py, tests/conftest.py
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import TestSessionLocal

# ── _collect_db_descriptions via patched SessionLocal ─────────────


def test_collect_db_descriptions_empty_db():
    """Returns empty list when DB has no matching records."""
    from app.services.description_service import _collect_db_descriptions

    with patch("app.services.description_service.SessionLocal", TestSessionLocal):
        results = _collect_db_descriptions("NOSUCHMPN999", "NoMfr")

    assert results == []


def test_collect_db_descriptions_with_material_card():
    """Picks up description from MaterialCard when normalized_mpn matches."""
    from app.models.intelligence import MaterialCard
    from app.services.description_service import _collect_db_descriptions

    # Use the SAME session that _collect_db_descriptions will use (avoids StaticPool isolation)
    sess = TestSessionLocal()
    try:
        card = MaterialCard(
            normalized_mpn="LM317T",
            display_mpn="LM317T",
            description="IC VOLT REG ADJ 1.5A TO-220",
            enrichment_source="digikey",
        )
        sess.add(card)
        sess.commit()

        with patch("app.services.description_service.SessionLocal", lambda: sess):
            results = _collect_db_descriptions("LM317T", "TI")
    finally:
        sess.close()

    assert len(results) >= 1
    sources = [r["source"] for r in results]
    assert any("material_card" in s for s in sources)
    descriptions = [r["description"] for r in results]
    assert any("IC VOLT REG" in d for d in descriptions)


def _make_user_in_sess(sess, suffix: str = ""):
    """Helper: create a user in a given session and return it."""
    import time
    from datetime import datetime, timezone

    from app.models import User

    uid = f"{suffix}{time.time_ns()}"
    user = User(
        email=f"desc_test_{uid}@test.com",
        name="Desc Tester",
        role="buyer",
        azure_id=f"desc-test-azure-{uid}",
        created_at=datetime.now(timezone.utc),
    )
    sess.add(user)
    sess.flush()
    return user


def test_collect_db_descriptions_with_sighting_raw_data():
    """Picks up description from Sighting.raw_data."""
    from app.models import Requirement, Requisition
    from app.models.sourcing import Sighting
    from app.services.description_service import _collect_db_descriptions

    sess = TestSessionLocal()
    try:
        user = _make_user_in_sess(sess)
        req = Requisition(name="Test Req", status="active", created_by=user.id)
        sess.add(req)
        sess.flush()

        requirement = Requirement(
            requisition_id=req.id,
            primary_mpn="ATmega328P",
            normalized_mpn="ATMEGA328P",
            target_qty=10,
        )
        sess.add(requirement)
        sess.flush()

        sighting = Sighting(
            requirement_id=requirement.id,
            normalized_mpn="ATMEGA328P",
            vendor_name="Mouser",
            source_type="mouser",
            unit_price=3.50,
            qty_available=100,
            raw_data={"description": "IC MCU 8BIT 32KB FLASH 32TQFP"},
        )
        sess.add(sighting)
        sess.commit()

        with patch("app.services.description_service.SessionLocal", lambda: sess):
            results = _collect_db_descriptions("ATmega328P", "Microchip")
    finally:
        sess.close()

    assert len(results) >= 1
    descriptions = [r["description"] for r in results]
    assert any("IC MCU" in d for d in descriptions)


def test_collect_db_descriptions_with_requirement_description():
    """Picks up user-entered description from another Requirement for same MPN."""
    from app.models import Requirement, Requisition
    from app.services.description_service import _collect_db_descriptions

    sess = TestSessionLocal()
    try:
        user = _make_user_in_sess(sess)
        req = Requisition(name="Old Req", status="active", created_by=user.id)
        sess.add(req)
        sess.flush()

        requirement = Requirement(
            requisition_id=req.id,
            primary_mpn="STM32F407VGT6",
            normalized_mpn="STM32F407VGT6",
            target_qty=5,
            description="IC MCU 32-BIT ARM CORTEX-M4",
        )
        sess.add(requirement)
        sess.commit()

        with patch("app.services.description_service.SessionLocal", lambda: sess):
            results = _collect_db_descriptions("STM32F407VGT6", "ST")
    finally:
        sess.close()

    descriptions = [r["description"] for r in results]
    assert any("IC MCU" in d for d in descriptions)
    sources = [r["source"] for r in results]
    assert "user_input" in sources


def test_collect_db_descriptions_deduplicates():
    """Does not add duplicate descriptions (case-insensitive dedup)."""
    from app.models import Requirement, Requisition
    from app.models.intelligence import MaterialCard
    from app.services.description_service import _collect_db_descriptions

    sess = TestSessionLocal()
    try:
        user = _make_user_in_sess(sess)
        card = MaterialCard(
            normalized_mpn="NE555",
            display_mpn="NE555",
            description="IC TIMER 555 8-DIP",
            enrichment_source="manual",
        )
        sess.add(card)

        req = Requisition(name="R1", status="active", created_by=user.id)
        sess.add(req)
        sess.flush()

        requirement = Requirement(
            requisition_id=req.id,
            primary_mpn="NE555",
            normalized_mpn="NE555",
            target_qty=1,
            description="ic timer 555 8-dip",  # same but lowercase
        )
        sess.add(requirement)
        sess.commit()

        with patch("app.services.description_service.SessionLocal", lambda: sess):
            results = _collect_db_descriptions("NE555", "TI")
    finally:
        sess.close()

    # Should only have the card description, not the duplicate from requirement
    descriptions_upper = [r["description"].upper() for r in results]
    assert descriptions_upper.count("IC TIMER 555 8-DIP") == 1


def test_collect_db_descriptions_short_descriptions_skipped():
    """Descriptions shorter than 5 chars are ignored."""
    from app.models.intelligence import MaterialCard
    from app.services.description_service import _collect_db_descriptions

    sess = TestSessionLocal()
    try:
        card = MaterialCard(
            normalized_mpn="XYZ",
            display_mpn="XYZ",
            description="N/A",  # too short
            enrichment_source="manual",
        )
        sess.add(card)
        sess.commit()

        with patch("app.services.description_service.SessionLocal", lambda: sess):
            results = _collect_db_descriptions("XYZ", "")
    finally:
        sess.close()

    # "N/A" is 3 chars, should be skipped
    assert all(len(r["description"]) >= 5 for r in results)


def test_collect_db_descriptions_db_exception_returns_empty():
    """Returns empty list (not exception) when DB query fails."""
    from app.services.description_service import _collect_db_descriptions

    bad_session = MagicMock()
    bad_session.execute.side_effect = Exception("DB connection lost")

    class BadSessionLocal:
        def __call__(self):
            return bad_session

    bad_sl = BadSessionLocal()

    with patch("app.services.description_service.SessionLocal", bad_sl):
        results = _collect_db_descriptions("ABC123", "Mfr")

    assert results == []


# ── _collect_db_descriptions: 5-source limit ─────────────────────


def test_collect_db_descriptions_limits_to_five_sources():
    """Stops collecting after 5 sources to avoid over-loading the prompt."""
    from app.models import Requirement, Requisition
    from app.models.sourcing import Sighting
    from app.services.description_service import _collect_db_descriptions

    sess = TestSessionLocal()
    try:
        user = _make_user_in_sess(sess, suffix="limit")
        req = Requisition(name="Req Multi", status="active", created_by=user.id)
        sess.add(req)
        sess.flush()

        requirement = Requirement(
            requisition_id=req.id,
            primary_mpn="BC547MULTI",
            normalized_mpn="BC547MULTI",
            target_qty=10,
        )
        sess.add(requirement)
        sess.flush()

        # Add 7 sightings with distinct descriptions (only 4 should be collected
        # because first source from MaterialCard doesn't exist here, so max from
        # sightings+requirements is 5)
        for i in range(7):
            sighting = Sighting(
                requirement_id=requirement.id,
                normalized_mpn="BC547MULTI",
                vendor_name=f"Vendor{i}",
                source_type=f"source{i}",
                unit_price=float(i + 1),
                qty_available=100,
                raw_data={"description": f"TRANSISTOR NPN GENERAL PURPOSE #{i}"},
            )
            sess.add(sighting)
        sess.commit()

        with patch("app.services.description_service.SessionLocal", lambda: sess):
            results = _collect_db_descriptions("BC547MULTI", "")
    finally:
        sess.close()

    # Should collect at most 5 sources total
    assert len(results) <= 5


# ── backfill_descriptions: test actual logic ──────────────────────


def test_backfill_descriptions_runs_when_not_testing():
    """backfill_descriptions processes requirements when TESTING is unset."""
    import os
    from unittest.mock import AsyncMock, MagicMock

    from app.services.description_service import backfill_descriptions

    mock_req = MagicMock()
    mock_req.primary_mpn = "LM317T"
    mock_req.manufacturer = "TI"
    mock_req.description = ""
    mock_req.material_card_id = None

    mock_db = MagicMock()
    mock_db.get.return_value = mock_req
    mock_db.commit = MagicMock()

    mock_result = {
        "description": "IC VOLT REG ADJ 1.5A TO-220",
        "confidence": 0.98,
        "sources_used": 3,
    }

    with (
        patch.dict(os.environ, {}, clear=False),
        patch("app.services.description_service.SessionLocal", return_value=mock_db),
        patch(
            "app.services.description_service.generate_verified_description",
            new_callable=AsyncMock,
            return_value=mock_result,
        ),
    ):
        # Temporarily remove TESTING to test the real function body
        saved = os.environ.pop("TESTING", None)
        try:
            backfill_descriptions([1])
        finally:
            if saved is not None:
                os.environ["TESTING"] = saved

    mock_db.commit.assert_called_once()
    assert mock_req.description == "IC VOLT REG ADJ 1.5A TO-220"


def test_backfill_descriptions_skips_existing_description():
    """backfill_descriptions skips requirements that already have a description."""
    import os
    from unittest.mock import AsyncMock, MagicMock

    from app.services.description_service import backfill_descriptions

    mock_req = MagicMock()
    mock_req.primary_mpn = "LM317T"
    mock_req.manufacturer = "TI"
    mock_req.description = "Already has description"
    mock_req.material_card_id = None

    mock_db = MagicMock()
    mock_db.get.return_value = mock_req

    with (
        patch("app.services.description_service.SessionLocal", return_value=mock_db),
        patch(
            "app.services.description_service.generate_verified_description",
            new_callable=AsyncMock,
        ) as mock_gen,
    ):
        saved = os.environ.pop("TESTING", None)
        try:
            backfill_descriptions([1])
        finally:
            if saved is not None:
                os.environ["TESTING"] = saved

    # generate_verified_description should NOT be called since description exists
    mock_gen.assert_not_called()


def test_backfill_descriptions_skips_missing_requirement():
    """backfill_descriptions skips non-existent requirement IDs gracefully."""
    import os
    from unittest.mock import AsyncMock, MagicMock

    from app.services.description_service import backfill_descriptions

    mock_db = MagicMock()
    mock_db.get.return_value = None  # Requirement not found

    with (
        patch("app.services.description_service.SessionLocal", return_value=mock_db),
        patch(
            "app.services.description_service.generate_verified_description",
            new_callable=AsyncMock,
        ) as mock_gen,
    ):
        saved = os.environ.pop("TESTING", None)
        try:
            backfill_descriptions([999])
        finally:
            if saved is not None:
                os.environ["TESTING"] = saved

    mock_gen.assert_not_called()


def test_backfill_descriptions_handles_exception_gracefully():
    """backfill_descriptions catches exceptions and continues."""
    import os
    from unittest.mock import AsyncMock, MagicMock

    from app.services.description_service import backfill_descriptions

    mock_req = MagicMock()
    mock_req.primary_mpn = "FAILMPN"
    mock_req.manufacturer = ""
    mock_req.description = ""
    mock_req.material_card_id = None

    mock_db = MagicMock()
    mock_db.get.return_value = mock_req

    with (
        patch("app.services.description_service.SessionLocal", return_value=mock_db),
        patch(
            "app.services.description_service.generate_verified_description",
            new_callable=AsyncMock,
            side_effect=Exception("AI service unavailable"),
        ),
    ):
        saved = os.environ.pop("TESTING", None)
        try:
            # Should not raise even when generate_verified_description fails
            backfill_descriptions([1])
        finally:
            if saved is not None:
                os.environ["TESTING"] = saved


def test_backfill_descriptions_updates_material_card():
    """backfill_descriptions updates linked MaterialCard when card has no description."""
    import os
    from unittest.mock import AsyncMock, MagicMock

    from app.services.description_service import backfill_descriptions

    mock_card = MagicMock()
    mock_card.description = ""  # no description

    mock_req = MagicMock()
    mock_req.primary_mpn = "STM32"
    mock_req.manufacturer = "ST"
    mock_req.description = ""
    mock_req.material_card_id = 42

    mock_db = MagicMock()
    mock_db.get.side_effect = lambda model, id_: mock_req if id_ != 42 else mock_card

    mock_result = {"description": "IC MCU 32-BIT", "confidence": 0.90, "sources_used": 2}

    with (
        patch("app.services.description_service.SessionLocal", return_value=mock_db),
        patch(
            "app.services.description_service.generate_verified_description",
            new_callable=AsyncMock,
            return_value=mock_result,
        ),
    ):
        saved = os.environ.pop("TESTING", None)
        try:
            backfill_descriptions([1])
        finally:
            if saved is not None:
                os.environ["TESTING"] = saved

    assert mock_card.description == "IC MCU 32-BIT"


# ── generate_verified_description edge cases ──────────────────────


@pytest.mark.asyncio
async def test_generate_description_existing_desc_deduped():
    """existing_description already in sources is not added again."""
    from unittest.mock import AsyncMock

    existing = "IC MCU 32-BIT 168MHZ 1MB FLASH LQFP-100"
    mock_sources = [{"source": "digikey", "description": existing}]

    with (
        patch(
            "app.services.description_service._collect_db_descriptions",
            return_value=mock_sources,
        ),
        patch(
            "app.utils.claude_client.claude_text",
            new_callable=AsyncMock,
            return_value="IC MCU 32-BIT 168MHZ",
        ),
    ):
        from app.services.description_service import generate_verified_description

        result = await generate_verified_description("STM32", "ST", existing_description=existing)

    # existing was same as the DB source — should not be double-counted
    assert result["sources_used"] == 1
    assert result["confidence"] == 0.75


@pytest.mark.asyncio
async def test_generate_description_claude_returns_none_fallback():
    """Falls back to existing_description when Claude returns None."""
    from unittest.mock import AsyncMock

    mock_sources = [{"source": "digikey", "description": "RESISTOR 10K 0402"}]

    with (
        patch(
            "app.services.description_service._collect_db_descriptions",
            return_value=mock_sources,
        ),
        patch(
            "app.utils.claude_client.claude_text",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        from app.services.description_service import generate_verified_description

        result = await generate_verified_description("RC0402", "Yageo", existing_description="res 10k")

    # Claude returned None, falls back to existing_description.upper()
    assert result["description"] == "RES 10K"
