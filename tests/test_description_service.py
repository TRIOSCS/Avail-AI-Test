"""test_description_service.py — Tests for AI-verified description generation.

Called by: pytest
Depends on: app/services/description_service.py, app/routers/ai.py
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ["TESTING"] = "1"


# ── Unit tests for generate_verified_description ──────────────────────


@pytest.mark.asyncio
async def test_generate_description_no_sources_no_existing():
    """Returns empty with 0 confidence when no sources and no existing description."""
    with patch(
        "app.services.description_service._collect_db_descriptions",
        return_value=[],
    ):
        from app.services.description_service import generate_verified_description

        result = await generate_verified_description("UNKNOWN123", "")
    assert result["description"] == ""
    assert result["confidence"] == 0.0
    assert result["verified"] is False


@pytest.mark.parametrize(
    (
        "mock_sources",
        "claude_return",
        "mpn",
        "manufacturer",
        "expected_confidence",
        "expected_sources_used",
        "expected_verified",
        "desc_contains",
    ),
    [
        pytest.param(
            [
                {"source": "digikey", "description": "IC MCU 32BIT 168MHZ 1MB LQFP100"},
                {"source": "mouser", "description": "IC MCU 32-BIT 168MHZ 1MB FLASH LQFP-100"},
                {"source": "element14", "description": "MCU 32BIT ARM 168MHZ 1MB FLASH"},
            ],
            "IC MCU 32-BIT 168MHZ 1MB FLASH LQFP-100",
            "STM32F407VGT6",
            "STMicroelectronics",
            0.98,
            3,
            True,
            "IC MCU",
            id="three_sources_verified",
        ),
        pytest.param(
            [
                {"source": "digikey", "description": "CAPACITOR MLCC 100NF 50V 0402"},
                {"source": "mouser", "description": "CAP MLCC 100NF 50V X7R 0402"},
            ],
            "CAP MLCC 100NF 50V X7R 0402",
            "CL05B104KO5NNNC",
            "Samsung",
            0.90,
            2,
            False,
            None,
            id="two_sources",
        ),
        pytest.param(
            [
                {"source": "oemsecrets", "description": "RES SMD 10K OHM 1% 0402"},
            ],
            "RES SMD 10K 1% 0402",
            "RC0402FR-0710KL",
            "Yageo",
            0.75,
            1,
            None,
            None,
            id="one_source",
        ),
    ],
)
@pytest.mark.asyncio
async def test_generate_description_source_confidence(
    mock_sources,
    claude_return,
    mpn,
    manufacturer,
    expected_confidence,
    expected_sources_used,
    expected_verified,
    desc_contains,
):
    """Confidence and verified flag scale with the number of corroborating sources."""
    with (
        patch(
            "app.services.description_service._collect_db_descriptions",
            return_value=mock_sources,
        ),
        patch(
            "app.utils.claude_client.claude_text",
            new_callable=AsyncMock,
            return_value=claude_return,
        ),
    ):
        from app.services.description_service import generate_verified_description

        result = await generate_verified_description(mpn, manufacturer)
    assert result["confidence"] == expected_confidence
    assert result["sources_used"] == expected_sources_used
    if expected_verified is not None:
        assert result["verified"] is expected_verified
    if desc_contains is not None:
        assert desc_contains in result["description"]


@pytest.mark.asyncio
async def test_generate_description_uses_existing_when_no_sources():
    """When no DB sources but user provided description, AI standardizes it."""
    with (
        patch(
            "app.services.description_service._collect_db_descriptions",
            return_value=[],
        ),
        patch(
            "app.utils.claude_client.claude_text",
            new_callable=AsyncMock,
            return_value="IC MCU ARM CORTEX-M4",
        ),
    ):
        from app.services.description_service import generate_verified_description

        result = await generate_verified_description("STM32F407", "ST", existing_description="microcontroller arm")
    assert result["confidence"] == 0.75
    assert result["description"] == "IC MCU ARM CORTEX-M4"


# ── Test backfill_descriptions skips in TESTING mode ──────────────────


def test_backfill_descriptions_noop_in_testing():
    """backfill_descriptions should return immediately when TESTING=1."""
    from app.services.description_service import backfill_descriptions

    # Should not raise even with invalid IDs
    backfill_descriptions([999, 1000])


# ── Test the API endpoints ────────────────────────────────────────────


def test_generate_description_endpoint_empty_mpn(client):
    """POST /api/ai/generate-description with empty MPN returns 400."""
    resp = client.post(
        "/api/ai/generate-description",
        json={"mpn": "", "manufacturer": ""},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_generate_description_endpoint_success(client):
    """POST /api/ai/generate-description returns verified description."""
    mock_result = {
        "description": "IC MCU 32-BIT 168MHZ LQFP-100",
        "confidence": 0.98,
        "sources_used": 3,
        "sources": ["digikey", "mouser", "element14"],
        "verified": True,
    }
    with patch(
        "app.services.description_service.generate_verified_description",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        resp = client.post(
            "/api/ai/generate-description",
            json={"mpn": "STM32F407VGT6", "manufacturer": "ST"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["verified"] is True
    assert data["confidence"] == 0.98


# ── Test requirement creation saves description ───────────────────────


def test_add_requirement_saves_description(client, db_session):
    """When creating a requirement with description, it should be persisted."""
    from app.models import Requirement, Requisition

    req = Requisition(name="Test Req", status="open", created_by=1)
    db_session.add(req)
    db_session.commit()

    with patch(
        "app.routers.requisitions.requirements.resolve_material_card",
        return_value=None,
    ):
        resp = client.post(
            f"/api/requisitions/{req.id}/requirements",
            json={
                "primary_mpn": "STM32F407VGT6",
                "manufacturer": "STMicroelectronics",
                "target_qty": 100,
                "description": "IC MCU 32-BIT ARM CORTEX-M4 168MHZ",
            },
        )

    assert resp.status_code == 200
    r = db_session.query(Requirement).filter_by(requisition_id=req.id).first()
    assert r is not None
    assert r.description == "IC MCU 32-BIT ARM CORTEX-M4 168MHZ"
