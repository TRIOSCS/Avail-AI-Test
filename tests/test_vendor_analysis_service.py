"""
test_vendor_analysis_service.py — Tests for vendor_analysis_service.py

Mock claude_json to return canned responses. Tests material tag
generation from MaterialVendorHistory and Sighting data.

Called by: pytest
Depends on: app/services/vendor_analysis_service.py, conftest.py
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models import (
    MaterialCard,
    MaterialVendorHistory,
    Requirement,
    Requisition,
    Sighting,
    User,
    VendorCard,
)
from app.services.vendor_analysis_service import _analyze_vendor_materials


# ── Helpers ─────────────────────────────────────────────────────────


def _make_vendor_card(db, name="test vendor"):
    card = VendorCard(
        normalized_name=name.lower(),
        display_name=name,
        brand_tags=[],
        commodity_tags=[],
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.flush()
    return card


def _make_material_card(db, mpn="LM317T", manufacturer="Texas Instruments"):
    mc = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        manufacturer=manufacturer,
        created_at=datetime.now(timezone.utc),
    )
    db.add(mc)
    db.flush()
    return mc


def _make_mvh(db, material_card_id, vendor_name, manufacturer=None):
    mvh = MaterialVendorHistory(
        material_card_id=material_card_id,
        vendor_name=vendor_name.lower(),
        last_manufacturer=manufacturer,
        times_seen=5,
        created_at=datetime.now(timezone.utc),
    )
    db.add(mvh)
    db.flush()
    return mvh


def _make_sighting(db, vendor_name, mpn, manufacturer=None):
    """Create a Sighting that requires a Requisition+Requirement chain."""
    user = db.query(User).first()
    if not user:
        user = User(
            email="sighting-user@test.com", name="Sighting User",
            role="buyer", azure_id="az-sighting",
            created_at=datetime.now(timezone.utc),
        )
        db.add(user)
        db.flush()

    req = db.query(Requisition).first()
    if not req:
        req = Requisition(
            name="REQ-SIGHTING", customer_name="Test",
            status="open", created_by=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(req)
        db.flush()

    requirement = db.query(Requirement).first()
    if not requirement:
        requirement = Requirement(
            requisition_id=req.id, primary_mpn=mpn,
            target_qty=100, created_at=datetime.now(timezone.utc),
        )
        db.add(requirement)
        db.flush()

    s = Sighting(
        requirement_id=requirement.id,
        vendor_name=vendor_name,
        mpn_matched=mpn,
        manufacturer=manufacturer,
        source_type="api",
        created_at=datetime.now(timezone.utc),
    )
    db.add(s)
    db.flush()
    return s


# ═══════════════════════════════════════════════════════════════════════
#  _analyze_vendor_materials
# ═══════════════════════════════════════════════════════════════════════


class TestAnalyzeVendorMaterials:
    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    async def test_nonexistent_vendor_card_no_call(self, mock_claude, db_session):
        await _analyze_vendor_materials(99999, db_session=db_session)
        mock_claude.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    async def test_vendor_with_no_parts_no_call(self, mock_claude, db_session):
        card = _make_vendor_card(db_session, "empty vendor")
        db_session.commit()

        await _analyze_vendor_materials(card.id, db_session=db_session)
        mock_claude.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    async def test_empty_parts_list_returns_early(self, mock_claude, db_session):
        """Vendor with NO MVH and NO matching Sightings → early return at line 74."""
        card = _make_vendor_card(db_session, "orphan vendor")
        db_session.commit()

        result = await _analyze_vendor_materials(card.id, db_session=db_session)
        assert result is None
        mock_claude.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    async def test_sighting_loop_body_covers_lines_67_72(self, mock_claude, db_session):
        """Sightings with matching vendor_name_normalized → loop body runs
        (lines 67-72) and feeds parts_list to Claude."""
        mock_claude.return_value = {"brands": ["Acme"], "commodities": ["Widgets"]}

        card = _make_vendor_card(db_session, "sighting vendor")
        # Create sightings whose vendor_name_normalized matches the card
        s1 = _make_sighting(db_session, "sighting vendor", "PART-A", "Acme")
        s1.vendor_name_normalized = "sighting vendor"
        s2 = _make_sighting(db_session, "sighting vendor", "PART-B", "Beta")
        s2.vendor_name_normalized = "sighting vendor"
        # Duplicate MPN to exercise the dedup (key already in seen_mpns)
        s3 = _make_sighting(db_session, "sighting vendor", "PART-A", "Acme")
        s3.vendor_name_normalized = "sighting vendor"
        db_session.commit()

        await _analyze_vendor_materials(card.id, db_session=db_session)

        mock_claude.assert_called_once()
        prompt_arg = mock_claude.call_args[0][0]
        assert "PART-A" in prompt_arg
        assert "PART-B" in prompt_arg

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    async def test_sighting_loop_breaks_at_200(self, mock_claude, db_session):
        """Line 72: break when parts_list hits 200 in the sighting loop."""
        mock_claude.return_value = {"brands": [], "commodities": []}

        card = _make_vendor_card(db_session, "bulk vendor")
        # Create 205 sightings with unique MPNs
        for i in range(205):
            s = _make_sighting(db_session, "bulk vendor", f"BULK-{i:04d}", "Mfr")
            s.vendor_name_normalized = "bulk vendor"
        db_session.commit()

        await _analyze_vendor_materials(card.id, db_session=db_session)

        mock_claude.assert_called_once()
        prompt_arg = mock_claude.call_args[0][0]
        # Should have exactly 200 parts (capped by the break)
        assert "200 samples" in prompt_arg

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    async def test_mvh_parts_included_in_prompt(self, mock_claude, db_session):
        mock_claude.return_value = {"brands": ["TI"], "commodities": ["Regulators"]}

        card = _make_vendor_card(db_session, "ti vendor")
        mc = _make_material_card(db_session, "LM317T", "Texas Instruments")
        _make_mvh(db_session, mc.id, "ti vendor", "Texas Instruments")
        db_session.commit()

        await _analyze_vendor_materials(card.id, db_session=db_session)

        mock_claude.assert_called_once()
        prompt_arg = mock_claude.call_args[0][0]
        assert "LM317T" in prompt_arg

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    async def test_sightings_deduped_against_mvh(self, mock_claude, db_session):
        """Same MPN from MVH and Sighting → only sent once."""
        mock_claude.return_value = {"brands": [], "commodities": []}

        card = _make_vendor_card(db_session, "dedup vendor")
        mc = _make_material_card(db_session, "LM317T")
        _make_mvh(db_session, mc.id, "dedup vendor")
        _make_sighting(db_session, "dedup vendor", "LM317T")
        db_session.commit()

        await _analyze_vendor_materials(card.id, db_session=db_session)

        prompt_arg = mock_claude.call_args[0][0]
        # LM317T should appear once in the parts list, not twice
        parts_section = prompt_arg.split("samples):\n")[1].split("\n\n")[0]
        assert parts_section.lower().count("lm317t") == 1

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    async def test_claude_response_saved_to_card(self, mock_claude, db_session):
        mock_claude.return_value = {
            "brands": ["Intel", "AMD"],
            "commodities": ["Server", "Networking"],
        }

        card = _make_vendor_card(db_session, "tagged vendor")
        mc = _make_material_card(db_session, "XEON-8380")
        _make_mvh(db_session, mc.id, "tagged vendor", "Intel")
        db_session.commit()

        await _analyze_vendor_materials(card.id, db_session=db_session)
        db_session.refresh(card)

        assert card.brand_tags == ["Intel", "AMD"]
        assert card.commodity_tags == ["Server", "Networking"]
        assert card.material_tags_updated_at is not None

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    async def test_max_5_tags_enforced(self, mock_claude, db_session):
        mock_claude.return_value = {
            "brands": ["A", "B", "C", "D", "E", "F", "G"],
            "commodities": ["X", "Y", "Z", "W", "V", "U", "T"],
        }

        card = _make_vendor_card(db_session, "many tags vendor")
        mc = _make_material_card(db_session, "TEST-001")
        _make_mvh(db_session, mc.id, "many tags vendor")
        db_session.commit()

        await _analyze_vendor_materials(card.id, db_session=db_session)
        db_session.refresh(card)

        assert len(card.brand_tags) == 5
        assert len(card.commodity_tags) == 5

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    async def test_claude_returns_none_card_unchanged(self, mock_claude, db_session):
        mock_claude.return_value = None

        card = _make_vendor_card(db_session, "none vendor")
        mc = _make_material_card(db_session, "TEST-002")
        _make_mvh(db_session, mc.id, "none vendor")
        db_session.commit()

        await _analyze_vendor_materials(card.id, db_session=db_session)
        db_session.refresh(card)

        assert card.brand_tags == []
        assert card.commodity_tags == []

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    async def test_claude_returns_invalid_no_crash(self, mock_claude, db_session):
        mock_claude.return_value = "not a dict"

        card = _make_vendor_card(db_session, "invalid vendor")
        mc = _make_material_card(db_session, "TEST-003")
        _make_mvh(db_session, mc.id, "invalid vendor")
        db_session.commit()

        # Should not raise
        await _analyze_vendor_materials(card.id, db_session=db_session)
        db_session.refresh(card)
        assert card.brand_tags == []

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    async def test_own_session_path(self, mock_claude):
        """When db_session=None, function creates its own session."""
        mock_claude.return_value = {"brands": ["Intel"], "commodities": ["Server"]}

        # Patch SessionLocal to return a mock
        mock_db = AsyncMock()
        mock_db.get = lambda model, id: None  # No card found -> early return

        with patch("app.database.SessionLocal", return_value=mock_db):
            await _analyze_vendor_materials(99999, db_session=None)
            mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    async def test_exception_with_own_session_rolls_back(self, mock_claude):
        """Exception during own_session path triggers rollback and close."""
        mock_claude.side_effect = Exception("API failure")

        mock_card = type('MockCard', (), {
            'id': 1, 'normalized_name': 'test', 'display_name': 'Test',
            'brand_tags': [], 'commodity_tags': [],
        })()

        mock_db = AsyncMock()
        mock_db.get = lambda model, id: mock_card
        mock_db.query = lambda *args: type('Q', (), {
            'join': lambda self, *a, **kw: self,
            'filter': lambda self, *a, **kw: self,
            'order_by': lambda self, *a, **kw: self,
            'limit': lambda self, *a, **kw: self,
            'all': lambda self: [type('Row', (), {'display_mpn': 'LM317T', 'manufacturer': 'TI', 'last_manufacturer': 'TI', 'times_seen': 1})()],
        })()

        with patch("app.database.SessionLocal", return_value=mock_db):
            await _analyze_vendor_materials(1, db_session=None)
            mock_db.rollback.assert_called()
            mock_db.close.assert_called()
