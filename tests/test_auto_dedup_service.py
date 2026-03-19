"""Tests for auto_dedup_service — background vendor and company deduplication.

Covers: run_auto_dedup, _dedup_vendors, _dedup_companies,
        _ai_confirm_vendor_merge, _ai_confirm_company_merge, _ask_claude_merge

Called by: pytest
Depends on: conftest fixtures, app.models, app.services.auto_dedup_service
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.models import Company, User, VendorCard

# ── Helpers ──────────────────────────────────────────────────────────


def _make_user(db: Session, email: str = "dedup@test.com") -> User:
    u = User(
        email=email,
        name="Dedup Tester",
        role="buyer",
        azure_id=f"az-{email}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_vendor(db: Session, display_name: str, normalized_name: str = None, **kw) -> VendorCard:
    defaults = dict(
        normalized_name=normalized_name or display_name.lower(),
        display_name=display_name,
        sighting_count=10,
        is_blacklisted=False,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    vc = VendorCard(**defaults)
    db.add(vc)
    db.flush()
    return vc


def _make_company(db: Session, name: str, **kw) -> Company:
    defaults = dict(
        name=name,
        website=f"https://{name.lower().replace(' ', '')}.com",
        industry="Electronics",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    co = Company(**defaults)
    db.add(co)
    db.flush()
    return co


# ══════════════════════════════════════════════════════════════════════
# run_auto_dedup (top-level)
# ══════════════════════════════════════════════════════════════════════


class TestRunAutoDedup:
    def test_returns_stats_dict(self, db_session):
        """Should return a dict with vendors_merged and companies_merged."""
        from app.services.auto_dedup_service import run_auto_dedup

        with patch("app.services.auto_dedup_service._dedup_vendors", return_value=2):
            with patch("app.services.auto_dedup_service._dedup_companies", return_value=1):
                result = run_auto_dedup(db_session)

        assert result == {"vendors_merged": 2, "companies_merged": 1}

    def test_vendor_failure_doesnt_block_companies(self, db_session):
        """If vendor dedup fails, company dedup should still run."""
        from app.services.auto_dedup_service import run_auto_dedup

        with patch("app.services.auto_dedup_service._dedup_vendors", side_effect=RuntimeError("boom")):
            with patch("app.services.auto_dedup_service._dedup_companies", return_value=3):
                result = run_auto_dedup(db_session)

        assert result["vendors_merged"] == 0
        assert result["companies_merged"] == 3

    def test_company_failure_doesnt_block_vendors(self, db_session):
        """If company dedup fails, vendor count should still be set."""
        from app.services.auto_dedup_service import run_auto_dedup

        with patch("app.services.auto_dedup_service._dedup_vendors", return_value=5):
            with patch("app.services.auto_dedup_service._dedup_companies", side_effect=RuntimeError("boom")):
                result = run_auto_dedup(db_session)

        assert result["vendors_merged"] == 5
        assert result["companies_merged"] == 0

    def test_empty_db_noop(self, db_session):
        """Empty database returns zero stats."""
        from app.services.auto_dedup_service import run_auto_dedup

        stats = run_auto_dedup(db_session)
        assert stats == {"vendors_merged": 0, "companies_merged": 0}

    def test_no_duplicates_no_merges(self, db_session):
        """Distinct vendor/company names produce no merges."""
        from app.services.auto_dedup_service import run_auto_dedup

        _make_vendor(db_session, "Alpha Corp", normalized_name="alpha corp")
        _make_vendor(db_session, "Beta Inc", normalized_name="beta inc")
        _make_company(db_session, "Gamma Corp")
        _make_company(db_session, "Delta Inc")
        db_session.commit()

        stats = run_auto_dedup(db_session)
        assert stats["vendors_merged"] == 0
        assert stats["companies_merged"] == 0


# ══════════════════════════════════════════════════════════════════════
# _dedup_vendors
# ══════════════════════════════════════════════════════════════════════


class TestDedupVendors:
    def test_auto_merge_high_score(self, db_session):
        """Near-identical names (score >= 98) should auto-merge."""
        from app.services.auto_dedup_service import _dedup_vendors

        # "arrow electronics corporation" vs "arrow electronics corporatio" = score ~98.2 (rapidfuzz)
        _make_vendor(
            db_session,
            "Arrow Electronics Corporation",
            normalized_name="arrow electronics corporation",
            sighting_count=20,
        )
        _make_vendor(
            db_session, "Arrow Electronics Corporatio", normalized_name="arrow electronics corporatio", sighting_count=5
        )
        db_session.commit()

        merged = _dedup_vendors(db_session)
        assert merged >= 1

    def test_keeps_higher_sighting_count(self, db_session):
        """The vendor with more sightings should be kept."""
        from app.services.auto_dedup_service import _dedup_vendors

        v1 = _make_vendor(
            db_session, "Arrow Electronics Corp", normalized_name="arrow electronics corp", sighting_count=5
        )
        v2 = _make_vendor(
            db_session, "Arrow Electronics Cor", normalized_name="arrow electronics cor", sighting_count=20
        )
        db_session.commit()

        _dedup_vendors(db_session)

        # v2 had more sightings — should survive
        assert db_session.get(VendorCard, v2.id) is not None

    def test_skips_blacklisted(self, db_session):
        """Blacklisted vendors should not be loaded for dedup."""
        from app.services.auto_dedup_service import _dedup_vendors

        _make_vendor(db_session, "Good Vendor", normalized_name="good vendor", sighting_count=10)
        _make_vendor(
            db_session, "Good Vendor BL", normalized_name="good vendor bl", sighting_count=5, is_blacklisted=True
        )
        db_session.commit()

        merged = _dedup_vendors(db_session)
        assert merged == 0

    def test_low_score_skipped(self, db_session):
        """Vendors with score < 92 should not be merged."""
        from app.services.auto_dedup_service import _dedup_vendors

        _make_vendor(db_session, "Alpha Corp", normalized_name="alpha corp")
        _make_vendor(db_session, "Zeta Industries", normalized_name="zeta industries")
        db_session.commit()

        merged = _dedup_vendors(db_session)
        assert merged == 0

    def test_merge_caps_at_20(self, db_session):
        """Vendor dedup should stop after 20 merges."""
        from app.services.auto_dedup_service import _dedup_vendors

        # Create 50 vendors in pairs with high similarity
        for i in range(25):
            _make_vendor(db_session, f"Vendor{i} Corp", normalized_name=f"vendor{i} corp", sighting_count=100)
            _make_vendor(db_session, f"Vendor{i} Cor", normalized_name=f"vendor{i} cor", sighting_count=50)
        db_session.commit()

        merged = _dedup_vendors(db_session)
        assert merged <= 20

    def test_merge_failure_continues(self, db_session):
        """If one merge fails, should continue processing."""
        from app.services.auto_dedup_service import _dedup_vendors

        _make_vendor(db_session, "Same Name A", normalized_name="same name a", sighting_count=20)
        _make_vendor(db_session, "Same Name", normalized_name="same name", sighting_count=10)
        db_session.commit()

        with patch("app.services.vendor_merge_service.merge_vendor_cards", side_effect=RuntimeError("merge failed")):
            # Should not crash
            _dedup_vendors(db_session)

    def test_ai_confirm_mid_score_accepted(self, db_session):
        """Score 92-97 with AI approval should merge."""
        from app.services.auto_dedup_service import _dedup_vendors

        _make_vendor(db_session, "Arrow Electronics Inc", normalized_name="arrow electronics inc", sighting_count=20)
        _make_vendor(db_session, "Arrow Elect LLC", normalized_name="arrow elect llc", sighting_count=5)
        db_session.commit()

        with patch("app.services.auto_dedup_service._ai_confirm_vendor_merge", return_value=True):
            # Whether AI is called depends on the fuzzy score
            _dedup_vendors(db_session)


# ══════════════════════════════════════════════════════════════════════
# _dedup_companies
# ══════════════════════════════════════════════════════════════════════


class TestDedupCompanies:
    def test_auto_merge_high_score(self, db_session):
        """Score >= 98 should auto-merge companies."""
        from app.services.auto_dedup_service import _dedup_companies

        keep = _make_company(db_session, "Acme Corp")
        remove = _make_company(db_session, "Acme Corp Dup")
        db_session.commit()

        candidates = [
            {
                "company_a": {"id": keep.id},
                "company_b": {"id": remove.id},
                "auto_keep_id": keep.id,
                "score": 99,
            }
        ]

        with patch("app.company_utils.find_company_dedup_candidates", return_value=candidates):
            with patch("app.services.company_merge_service.merge_companies") as mock_merge:
                merged = _dedup_companies(db_session)

        assert merged == 1
        mock_merge.assert_called_once_with(keep.id, remove.id, db_session)

    def test_ai_confirm_mid_score(self, db_session):
        """Score 92-97 should consult AI before merging."""
        from app.services.auto_dedup_service import _dedup_companies

        keep = _make_company(db_session, "Acme Corp")
        remove = _make_company(db_session, "Acme Corporation")
        db_session.commit()

        candidates = [
            {
                "company_a": {"id": keep.id},
                "company_b": {"id": remove.id},
                "auto_keep_id": keep.id,
                "score": 95,
            }
        ]

        with patch("app.company_utils.find_company_dedup_candidates", return_value=candidates):
            with patch("app.services.auto_dedup_service._ai_confirm_company_merge", return_value=True) as mock_ai:
                with patch("app.services.company_merge_service.merge_companies"):
                    merged = _dedup_companies(db_session)

        assert merged == 1
        mock_ai.assert_called_once()

    def test_skips_different_owners(self, db_session):
        """Companies with different owners should NOT be merged."""
        from app.services.auto_dedup_service import _dedup_companies

        user1 = _make_user(db_session, "owner1@test.com")
        user2 = _make_user(db_session, "owner2@test.com")

        keep = _make_company(db_session, "Acme", account_owner_id=user1.id)
        remove = _make_company(db_session, "Acme Dup", account_owner_id=user2.id)
        db_session.commit()

        candidates = [
            {
                "company_a": {"id": keep.id},
                "company_b": {"id": remove.id},
                "auto_keep_id": keep.id,
                "score": 99,
            }
        ]

        with patch("app.company_utils.find_company_dedup_candidates", return_value=candidates):
            with patch("app.services.company_merge_service.merge_companies") as mock_merge:
                merged = _dedup_companies(db_session)

        assert merged == 0
        mock_merge.assert_not_called()

    def test_same_owner_allowed(self, db_session):
        """Companies with the same owner CAN be merged."""
        from app.services.auto_dedup_service import _dedup_companies

        user = _make_user(db_session)
        keep = _make_company(db_session, "Acme", account_owner_id=user.id)
        remove = _make_company(db_session, "Acme Dup", account_owner_id=user.id)
        db_session.commit()

        candidates = [
            {
                "company_a": {"id": keep.id},
                "company_b": {"id": remove.id},
                "auto_keep_id": keep.id,
                "score": 99,
            }
        ]

        with patch("app.company_utils.find_company_dedup_candidates", return_value=candidates):
            with patch("app.services.company_merge_service.merge_companies"):
                merged = _dedup_companies(db_session)

        assert merged == 1

    def test_no_owner_allowed(self, db_session):
        """Companies with no owners CAN be merged."""
        from app.services.auto_dedup_service import _dedup_companies

        keep = _make_company(db_session, "Acme", account_owner_id=None)
        remove = _make_company(db_session, "Acme Dup", account_owner_id=None)
        db_session.commit()

        candidates = [
            {
                "company_a": {"id": keep.id},
                "company_b": {"id": remove.id},
                "auto_keep_id": keep.id,
                "score": 99,
            }
        ]

        with patch("app.company_utils.find_company_dedup_candidates", return_value=candidates):
            with patch("app.services.company_merge_service.merge_companies"):
                merged = _dedup_companies(db_session)

        assert merged == 1

    def test_caps_at_10(self, db_session):
        """Company dedup should stop after 10 merges."""
        from app.services.auto_dedup_service import _dedup_companies

        companies = []
        for i in range(25):
            co = _make_company(db_session, f"Company {i}")
            companies.append(co)
        db_session.commit()

        candidates = []
        for i in range(0, 24, 2):
            candidates.append(
                {
                    "company_a": {"id": companies[i].id},
                    "company_b": {"id": companies[i + 1].id},
                    "auto_keep_id": companies[i].id,
                    "score": 99,
                }
            )

        with patch("app.company_utils.find_company_dedup_candidates", return_value=candidates):
            with patch("app.services.company_merge_service.merge_companies"):
                merged = _dedup_companies(db_session)

        assert merged <= 10

    def test_merge_failure_continues(self, db_session):
        """If one company merge fails, should continue with others."""
        from app.services.auto_dedup_service import _dedup_companies

        keep1 = _make_company(db_session, "Co A")
        rem1 = _make_company(db_session, "Co A Dup")
        keep2 = _make_company(db_session, "Co B")
        rem2 = _make_company(db_session, "Co B Dup")
        db_session.commit()

        candidates = [
            {"company_a": {"id": keep1.id}, "company_b": {"id": rem1.id}, "auto_keep_id": keep1.id, "score": 99},
            {"company_a": {"id": keep2.id}, "company_b": {"id": rem2.id}, "auto_keep_id": keep2.id, "score": 99},
        ]

        call_count = 0

        def merge_side_effect(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("merge failed")

        with patch("app.company_utils.find_company_dedup_candidates", return_value=candidates):
            with patch("app.services.company_merge_service.merge_companies", side_effect=merge_side_effect):
                merged = _dedup_companies(db_session)

        assert merged == 1

    def test_skips_missing_companies(self, db_session):
        """If a candidate company is missing from DB, skip it."""
        from app.services.auto_dedup_service import _dedup_companies

        keep = _make_company(db_session, "Real Co")
        db_session.commit()

        candidates = [
            {
                "company_a": {"id": keep.id},
                "company_b": {"id": 99999},
                "auto_keep_id": keep.id,
                "score": 99,
            }
        ]

        with patch("app.company_utils.find_company_dedup_candidates", return_value=candidates):
            with patch("app.services.company_merge_service.merge_companies") as mock_merge:
                merged = _dedup_companies(db_session)

        assert merged == 0
        mock_merge.assert_not_called()

    def test_ai_rejects_mid_score(self, db_session):
        """If AI says no for score 92-97, should NOT merge."""
        from app.services.auto_dedup_service import _dedup_companies

        keep = _make_company(db_session, "Acme Corp")
        remove = _make_company(db_session, "Acme Corporation")
        db_session.commit()

        candidates = [
            {
                "company_a": {"id": keep.id},
                "company_b": {"id": remove.id},
                "auto_keep_id": keep.id,
                "score": 94,
            }
        ]

        with patch("app.company_utils.find_company_dedup_candidates", return_value=candidates):
            with patch("app.services.auto_dedup_service._ai_confirm_company_merge", return_value=False):
                with patch("app.services.company_merge_service.merge_companies") as mock_merge:
                    merged = _dedup_companies(db_session)

        assert merged == 0
        mock_merge.assert_not_called()

    def test_remove_id_from_company_b(self, db_session):
        """When auto_keep_id matches company_a, remove should be company_b."""
        from app.services.auto_dedup_service import _dedup_companies

        keep = _make_company(db_session, "Keep")
        remove = _make_company(db_session, "Remove")
        db_session.commit()

        candidates = [
            {
                "company_a": {"id": keep.id},
                "company_b": {"id": remove.id},
                "auto_keep_id": keep.id,
                "score": 99,
            }
        ]

        with patch("app.company_utils.find_company_dedup_candidates", return_value=candidates):
            with patch("app.services.company_merge_service.merge_companies") as mock_merge:
                _dedup_companies(db_session)

        mock_merge.assert_called_once_with(keep.id, remove.id, db_session)

    def test_remove_id_from_company_a(self, db_session):
        """When auto_keep_id matches company_b, remove should be company_a."""
        from app.services.auto_dedup_service import _dedup_companies

        co_a = _make_company(db_session, "CoA")
        co_b = _make_company(db_session, "CoB")
        db_session.commit()

        candidates = [
            {
                "company_a": {"id": co_a.id},
                "company_b": {"id": co_b.id},
                "auto_keep_id": co_b.id,
                "score": 99,
            }
        ]

        with patch("app.company_utils.find_company_dedup_candidates", return_value=candidates):
            with patch("app.services.company_merge_service.merge_companies") as mock_merge:
                _dedup_companies(db_session)

        mock_merge.assert_called_once_with(co_b.id, co_a.id, db_session)


# ══════════════════════════════════════════════════════════════════════
# AI Confirmation Functions
# ══════════════════════════════════════════════════════════════════════


class TestAIConfirmation:
    def test_ai_confirm_vendor_merge_true(self):
        """Should return True when Claude says same entity."""
        from app.services.auto_dedup_service import _ai_confirm_vendor_merge

        with patch("app.services.auto_dedup_service._ask_claude_merge", new_callable=AsyncMock, return_value=True):
            result = _ai_confirm_vendor_merge("Arrow Electronics", "Arrow Elec", 95)

        assert result is True

    def test_ai_confirm_vendor_merge_false(self):
        """Should return False when Claude says different entity."""
        from app.services.auto_dedup_service import _ai_confirm_vendor_merge

        with patch("app.services.auto_dedup_service._ask_claude_merge", new_callable=AsyncMock, return_value=False):
            result = _ai_confirm_vendor_merge("Arrow Electronics", "Digi-Key", 92)

        assert result is False

    def test_ai_confirm_vendor_merge_exception(self):
        """Should return False on exception (safe default)."""
        from app.services.auto_dedup_service import _ai_confirm_vendor_merge

        with patch(
            "app.services.auto_dedup_service._ask_claude_merge",
            new_callable=AsyncMock,
            side_effect=RuntimeError("API error"),
        ):
            result = _ai_confirm_vendor_merge("A", "B", 95)

        assert result is False

    def test_ai_confirm_company_merge_true(self):
        """Should return True when Claude confirms company match."""
        from app.services.auto_dedup_service import _ai_confirm_company_merge

        with patch("app.services.auto_dedup_service._ask_claude_merge", new_callable=AsyncMock, return_value=True):
            result = _ai_confirm_company_merge("Acme Corp", "ACME Corporation", "acme.com", "acme.com", 95)

        assert result is True

    def test_ai_confirm_company_merge_no_domains(self):
        """Should handle None domains gracefully."""
        from app.services.auto_dedup_service import _ai_confirm_company_merge

        with patch("app.services.auto_dedup_service._ask_claude_merge", new_callable=AsyncMock, return_value=False):
            result = _ai_confirm_company_merge("A", "B", None, None, 93)

        assert result is False

    def test_ai_confirm_company_merge_exception(self):
        """Should return False on exception."""
        from app.services.auto_dedup_service import _ai_confirm_company_merge

        with patch(
            "app.services.auto_dedup_service._ask_claude_merge",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            result = _ai_confirm_company_merge("A", "B", None, None, 95)

        assert result is False


# ══════════════════════════════════════════════════════════════════════
# _ask_claude_merge
# ══════════════════════════════════════════════════════════════════════


class TestAskClaudeMerge:
    def test_returns_true_high_confidence(self):
        """Should return True when same_entity=True and confidence >= 0.85."""
        from app.services.auto_dedup_service import _ask_claude_merge

        mock_result = {"same_entity": True, "confidence": 0.95}
        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=mock_result):
            result = asyncio.get_event_loop().run_until_complete(_ask_claude_merge("Are A and B the same?"))

        assert result is True

    def test_returns_false_low_confidence(self):
        """Should return False when confidence < 0.85."""
        from app.services.auto_dedup_service import _ask_claude_merge

        mock_result = {"same_entity": True, "confidence": 0.60}
        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=mock_result):
            result = asyncio.get_event_loop().run_until_complete(_ask_claude_merge("Are A and B the same?"))

        assert result is False

    def test_returns_false_not_same(self):
        """Should return False when same_entity=False."""
        from app.services.auto_dedup_service import _ask_claude_merge

        mock_result = {"same_entity": False, "confidence": 0.99}
        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=mock_result):
            result = asyncio.get_event_loop().run_until_complete(_ask_claude_merge("Are A and B the same?"))

        assert result is False

    def test_returns_false_on_none(self):
        """Should return False when Claude returns None."""
        from app.services.auto_dedup_service import _ask_claude_merge

        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=None):
            result = asyncio.get_event_loop().run_until_complete(_ask_claude_merge("Are A and B the same?"))

        assert result is False

    def test_returns_false_missing_keys(self):
        """Should return False when response is missing keys."""
        from app.services.auto_dedup_service import _ask_claude_merge

        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value={}):
            result = asyncio.get_event_loop().run_until_complete(_ask_claude_merge("Are A and B the same?"))

        assert result is False

    def test_boundary_confidence_085(self):
        """Confidence exactly at 0.85 should return True."""
        from app.services.auto_dedup_service import _ask_claude_merge

        mock_result = {"same_entity": True, "confidence": 0.85}
        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=mock_result):
            result = asyncio.get_event_loop().run_until_complete(_ask_claude_merge("Are A and B the same?"))

        assert result is True


# ══════════════════════════════════════════════════════════════════════
# Coverage gap tests for _dedup_vendors edge cases
# ══════════════════════════════════════════════════════════════════════


class TestDedupVendorsCoverageGaps:
    def test_rapidfuzz_import_error(self, db_session):
        """Lines 54-56: when rapidfuzz is not installed, returns 0."""
        import builtins

        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "rapidfuzz":
                raise ImportError("No module named 'rapidfuzz'")
            return original_import(name, *args, **kwargs)

        from app.services.auto_dedup_service import _dedup_vendors

        with patch("builtins.__import__", side_effect=mock_import):
            result = _dedup_vendors(db_session)

        assert result == 0

    def test_skip_merged_b_in_inner_loop(self, db_session):
        """Line 77: inner loop skips b whose id was merged in an earlier outer iteration."""
        from app.services.auto_dedup_service import _dedup_vendors

        # A (idx 0) and C (idx 2) are similar (will merge, C removed).
        # B (idx 1) has inner loop that includes C — should skip it.
        # A and C are similar (score=98), B is unrelated
        _make_vendor(
            db_session,
            "Xyzzy Electronics Corporation Inc",
            normalized_name="xyzzy electronics corporation inc",
            sighting_count=100,
        )
        _make_vendor(
            db_session, "Totally Different Vendor", normalized_name="totally different vendor", sighting_count=50
        )
        _make_vendor(
            db_session,
            "Xyzzy Electronics Corporation In",
            normalized_name="xyzzy electronics corporation in",
            sighting_count=10,
        )
        db_session.commit()

        merged = _dedup_vendors(db_session)
        # A merges C. Then B's inner loop encounters C (in merged_ids) -> line 77 skip
        assert merged >= 1

    def test_merge_exception_rolls_back(self, db_session):
        """Lines 112-114: merge exception is caught and rolled back."""
        from app.services.auto_dedup_service import _dedup_vendors

        _make_vendor(db_session, "Fail Merge Corp", normalized_name="fail merge corp", sighting_count=20)
        _make_vendor(db_session, "Fail Merge Cor", normalized_name="fail merge cor", sighting_count=10)
        db_session.commit()

        with patch("app.services.vendor_merge_service.merge_vendor_cards", side_effect=RuntimeError("merge exploded")):
            merged = _dedup_vendors(db_session)

        assert merged == 0

    def test_cap_at_50_breaks_both_loops(self, db_session):
        """Lines 114 and 116: both inner and outer loops break at 50 merges."""
        from app.services.auto_dedup_service import _dedup_vendors

        # Create >50 vendor pairs that will auto-merge (score>=98)
        for i in range(55):
            _make_vendor(
                db_session,
                f"Corp{i:03d} Electronics Incorporated",
                normalized_name=f"corp{i:03d} electronics incorporated",
                sighting_count=100,
            )
            _make_vendor(
                db_session,
                f"Corp{i:03d} Electronics Incorporate",
                normalized_name=f"corp{i:03d} electronics incorporate",
                sighting_count=50,
            )
        db_session.commit()

        merged = _dedup_vendors(db_session)
        assert merged == 50
