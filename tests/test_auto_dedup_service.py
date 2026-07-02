"""Tests for auto_dedup_service — background vendor and company deduplication.

Covers: run_auto_dedup, _dedup_vendors, _dedup_companies,
        _ai_confirm_vendor_merge, _ai_confirm_company_merge, _ask_claude_merge

Called by: pytest
Depends on: conftest fixtures, app.models, app.services.auto_dedup_service
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
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


def _candidate(keep_id: int, remove_id: int, score: int = 99, *, auto_keep_id: int = None) -> dict:
    """Build a company dedup candidate.

    company_a is the keeper unless auto_keep_id overrides.
    """
    return {
        "company_a": {"id": keep_id},
        "company_b": {"id": remove_id},
        "auto_keep_id": keep_id if auto_keep_id is None else auto_keep_id,
        "score": score,
    }


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

        # normalized_name values are already suffix-stripped (as in production, where
        # normalize_vendor_name() populates the column). fuzzy_score_vendor re-normalizes
        # idempotently, so a 1-char tail diff scores >= 98 → auto-merge.
        _make_vendor(
            db_session,
            "Arrow Electronics Distribution",
            normalized_name="arrow electronics distribution",
            sighting_count=20,
        )
        _make_vendor(
            db_session,
            "Arrow Electronics Distributio",
            normalized_name="arrow electronics distributio",
            sighting_count=5,
        )
        db_session.commit()

        merged = _dedup_vendors(db_session)
        assert merged >= 1

    def test_keeps_higher_sighting_count(self, db_session):
        """The vendor with more sightings should be kept."""
        from app.services.auto_dedup_service import _dedup_vendors

        v1 = _make_vendor(
            db_session, "Arrow Electronics Worldwide", normalized_name="arrow electronics worldwide", sighting_count=5
        )
        v2 = _make_vendor(
            db_session, "Arrow Electronics Worldwid", normalized_name="arrow electronics worldwid", sighting_count=20
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
            _make_vendor(
                db_session,
                f"Vendor{i} Electronics Group",
                normalized_name=f"vendor{i} electronics group",
                sighting_count=100,
            )
            _make_vendor(
                db_session,
                f"Vendor{i} Electronics Grp",
                normalized_name=f"vendor{i} electronics grp",
                sighting_count=50,
            )
        db_session.commit()

        merged = _dedup_vendors(db_session)
        assert merged <= 20

    def test_merge_failure_continues(self, db_session):
        """If one merge fails, should continue processing."""
        from app.services.auto_dedup_service import _dedup_vendors

        _make_vendor(db_session, "Same Brand Electronics", normalized_name="same brand electronics", sighting_count=20)
        _make_vendor(db_session, "Same Brand Electronic", normalized_name="same brand electronic", sighting_count=10)
        db_session.commit()

        with patch("app.services.vendor_merge_service.merge_vendor_cards", side_effect=RuntimeError("merge failed")):
            # Should not crash
            _dedup_vendors(db_session)

    def test_ai_confirm_mid_score_accepted(self, db_session):
        """Score 92-97 with AI approval performs a REAL merge (only the AI call mocked).

        End-to-end: nothing but ``_ai_confirm_vendor_merge`` is stubbed, so the real
        ``merge_vendor_cards`` runs. Asserts the AI was consulted for the mid-score band,
        the merge actually happened, the lower-sighting card was deleted, and its
        sightings were folded into the survivor — a real scoring/merge regression now
        fails this test instead of passing green.
        """
        from app.services.auto_dedup_service import _dedup_vendors
        from app.vendor_utils import fuzzy_score_vendor

        keep = _make_vendor(
            db_session, "Arrow Electronics Group", normalized_name="arrow electronics group", sighting_count=20
        )
        remove = _make_vendor(
            db_session, "Arrow Electronics Grp", normalized_name="arrow electronics grp", sighting_count=5
        )
        db_session.commit()

        # Pin the branch under test: this pair must score in the AI-confirm band
        # (92-97), NOT the >=98 auto-merge band — otherwise the AI is never consulted.
        assert 92 <= fuzzy_score_vendor("arrow electronics group", "arrow electronics grp") < 98

        with patch("app.services.auto_dedup_service._ai_confirm_vendor_merge", return_value=True) as mock_ai:
            merged = _dedup_vendors(db_session)

        mock_ai.assert_called_once()  # AI was consulted for the mid-score pair
        assert merged == 1  # and its approval drove exactly one real merge
        assert db_session.get(VendorCard, remove.id) is None  # lower-sighting card deleted
        survivor = db_session.get(VendorCard, keep.id)
        assert survivor is not None
        assert survivor.sighting_count == 25  # 20 + 5 folded in


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

        candidates = [_candidate(keep.id, remove.id, score=99)]

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

        candidates = [_candidate(keep.id, remove.id, score=95)]

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

        candidates = [_candidate(keep.id, remove.id, score=99)]

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

        candidates = [_candidate(keep.id, remove.id, score=99)]

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

        candidates = [_candidate(keep.id, remove.id, score=99)]

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

        candidates = [_candidate(companies[i].id, companies[i + 1].id, score=99) for i in range(0, 24, 2)]

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
            _candidate(keep1.id, rem1.id, score=99),
            _candidate(keep2.id, rem2.id, score=99),
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

        candidates = [_candidate(keep.id, 99999, score=99)]

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

        candidates = [_candidate(keep.id, remove.id, score=94)]

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

        candidates = [_candidate(keep.id, remove.id, score=99, auto_keep_id=keep.id)]

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

        candidates = [_candidate(co_a.id, co_b.id, score=99, auto_keep_id=co_b.id)]

        with patch("app.company_utils.find_company_dedup_candidates", return_value=candidates):
            with patch("app.services.company_merge_service.merge_companies") as mock_merge:
                _dedup_companies(db_session)

        mock_merge.assert_called_once_with(co_b.id, co_a.id, db_session)


# ══════════════════════════════════════════════════════════════════════
# AI Confirmation Functions
# ══════════════════════════════════════════════════════════════════════


class TestAIConfirmation:
    @pytest.mark.parametrize(
        ("claude_return", "claude_side_effect", "expected"),
        [
            pytest.param(True, None, True, id="true"),
            pytest.param(False, None, False, id="false"),
            pytest.param(None, RuntimeError("API error"), False, id="exception"),
        ],
    )
    def test_ai_confirm_vendor_merge(self, claude_return, claude_side_effect, expected):
        """Mirrors _ask_claude_merge; exception falls back to False (safe default)."""
        from app.services.auto_dedup_service import _ai_confirm_vendor_merge

        with patch(
            "app.services.auto_dedup_service._ask_claude_merge",
            new_callable=AsyncMock,
            return_value=claude_return,
            side_effect=claude_side_effect,
        ):
            result = _ai_confirm_vendor_merge("Arrow Electronics", "Arrow Elec", 95)

        assert result is expected

    @pytest.mark.parametrize(
        ("domain_a", "domain_b", "claude_return", "claude_side_effect", "expected"),
        [
            pytest.param("acme.com", "acme.com", True, None, True, id="true"),
            pytest.param(None, None, False, None, False, id="no_domains"),
            pytest.param(None, None, None, RuntimeError("boom"), False, id="exception"),
        ],
    )
    def test_ai_confirm_company_merge(self, domain_a, domain_b, claude_return, claude_side_effect, expected):
        """Mirrors _ask_claude_merge; None domains handled gracefully; exception →
        False."""
        from app.services.auto_dedup_service import _ai_confirm_company_merge

        with patch(
            "app.services.auto_dedup_service._ask_claude_merge",
            new_callable=AsyncMock,
            return_value=claude_return,
            side_effect=claude_side_effect,
        ):
            result = _ai_confirm_company_merge("Acme Corp", "ACME Corporation", domain_a, domain_b, 95)

        assert result is expected


# ══════════════════════════════════════════════════════════════════════
# _ask_claude_merge
# ══════════════════════════════════════════════════════════════════════


class TestAskClaudeMerge:
    @pytest.mark.parametrize(
        ("claude_response", "expected"),
        [
            pytest.param({"same_entity": True, "confidence": 0.95}, True, id="high_confidence"),
            pytest.param({"same_entity": True, "confidence": 0.60}, False, id="low_confidence"),
            pytest.param({"same_entity": False, "confidence": 0.99}, False, id="not_same"),
            pytest.param(None, False, id="none"),
            pytest.param({}, False, id="missing_keys"),
            pytest.param({"same_entity": True, "confidence": 0.85}, True, id="boundary_confidence_085"),
        ],
    )
    def test_decision(self, claude_response, expected):
        """same_entity=True AND confidence >= 0.85 (inclusive) returns True; otherwise
        False."""
        from app.services.auto_dedup_service import _ask_claude_merge

        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=claude_response):
            result = asyncio.get_event_loop().run_until_complete(_ask_claude_merge("Are A and B the same?"))

        assert result is expected


# ══════════════════════════════════════════════════════════════════════
# Coverage gap tests for _dedup_vendors edge cases
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.slow
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
            "Xyzzy Electronics Distribution",
            normalized_name="xyzzy electronics distribution",
            sighting_count=100,
        )
        _make_vendor(
            db_session, "Totally Different Vendor", normalized_name="totally different vendor", sighting_count=50
        )
        _make_vendor(
            db_session,
            "Xyzzy Electronics Distributio",
            normalized_name="xyzzy electronics distributio",
            sighting_count=10,
        )
        db_session.commit()

        merged = _dedup_vendors(db_session)
        # A merges C. Then B's inner loop encounters C (in merged_ids) -> line 77 skip
        assert merged >= 1

    def test_merge_exception_rolls_back(self, db_session):
        """Lines 112-114: merge exception is caught and rolled back."""
        from app.services.auto_dedup_service import _dedup_vendors

        _make_vendor(db_session, "Fail Merge Electronics", normalized_name="fail merge electronics", sighting_count=20)
        _make_vendor(db_session, "Fail Merge Electronic", normalized_name="fail merge electronic", sighting_count=10)
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
                f"Corp{i:03d} Electronics Distribution",
                normalized_name=f"corp{i:03d} electronics distribution",
                sighting_count=100,
            )
            _make_vendor(
                db_session,
                f"Corp{i:03d} Electronics Distributio",
                normalized_name=f"corp{i:03d} electronics distributio",
                sighting_count=50,
            )
        db_session.commit()

        merged = _dedup_vendors(db_session)
        assert merged == 50


# ══════════════════════════════════════════════════════════════════════
# Shared fuzzy scorer usage (project rule: never inline fuzzy)
# ══════════════════════════════════════════════════════════════════════


class TestUsesSharedFuzzyScorer:
    def test_dedup_vendors_calls_shared_helper(self, db_session):
        """_dedup_vendors must score via vendor_utils.fuzzy_score_vendor, not inline
        fuzz."""
        from app.services.auto_dedup_service import _dedup_vendors

        _make_vendor(
            db_session, "Nimbus Electronics Group", normalized_name="nimbus electronics group", sighting_count=20
        )
        _make_vendor(db_session, "Nimbus Electronics Grp", normalized_name="nimbus electronics grp", sighting_count=5)
        db_session.commit()

        # _dedup_vendors imports the helper lazily, so patch it at the source module.
        with patch("app.vendor_utils.fuzzy_score_vendor", return_value=99) as mock_score:
            merged = _dedup_vendors(db_session)

        # Shared helper was consulted, and its returned score (>= 98) drove an auto-merge.
        assert mock_score.called
        assert merged == 1

    def test_score_matches_shared_helper(self, db_session):
        """The score threshold uses exactly what fuzzy_score_vendor returns.

        A pair the shared helper scores < 92 must NOT merge even though a raw inline
        token_sort_ratio (no re-normalization) would score >= 92.
        """
        from app.services.auto_dedup_service import _dedup_vendors
        from app.vendor_utils import fuzzy_score_vendor

        # Shared helper strips the "corporation" suffix, dropping the score below 92;
        # a naive inline fuzz.token_sort_ratio on the raw strings would clear 92.
        a, b = "arrow electronics corporation", "arrow electronics corporatio"
        assert fuzzy_score_vendor(a, b) < 92

        _make_vendor(db_session, "Arrow Electronics Corporation", normalized_name=a, sighting_count=20)
        _make_vendor(db_session, "Arrow Electronics Corporatio", normalized_name=b, sighting_count=5)
        db_session.commit()

        merged = _dedup_vendors(db_session)
        assert merged == 0
