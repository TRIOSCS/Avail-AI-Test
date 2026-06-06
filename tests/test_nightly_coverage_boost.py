"""Tests targeting uncovered branches in key modules to boost coverage.

Modules targeted (all were 89–94%, all gaps are exception/edge branches):
- app/services/activity_digest_service.py
- app/services/sourcing_auto_progress.py
- app/services/ai_email_parser.py
- app/utils/vendor_helpers.py
- app/services/company_merge_service.py
- app/services/requisition_service.py

Called by: pytest
Depends on: tests/conftest.py (db_session fixture)
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import Company, Requirement, Requisition, VendorCard

# ── activity_digest_service ──────────────────────────────────────────


def _mk_activity(db, requisition_id=None, company_id=None):
    from datetime import datetime, timezone

    from app.models.intelligence import ActivityLog
    from app.models.sourcing import Requisition as Req

    if requisition_id:
        existing = db.get(Req, requisition_id)
        if existing is None:
            db.add(Req(id=requisition_id, name=f"REQ-{requisition_id}", status="active"))
            db.flush()

    a = ActivityLog(
        activity_type="sales_note",
        channel="manual",
        requisition_id=requisition_id,
        company_id=company_id,
        notes="note",
        is_meaningful=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(a)
    db.commit()
    return a


class TestActivityDigestEdgeCases:
    def test_system_prompt_invalid_entity_type_raises(self):
        """Line 90: _system_prompt raises ValueError for unknown entity types."""
        from app.services import activity_digest_service as svc

        with pytest.raises(ValueError):
            svc._system_prompt("unknown_type")  # type: ignore[arg-type]

    def test_get_redis_returns_none_in_test_env(self):
        """Lines 115-117: _get_redis() body is exercised directly."""
        from app.services import activity_digest_service as svc

        # CACHE_BACKEND=none / REDIS_URL="" → returns None
        result = svc._get_redis()
        assert result is None

    @pytest.mark.asyncio
    async def test_same_basis_expired_cooldown_returns_cached(self, db_session):
        """Line 170: When basis unchanged and cooldown expired, return cached digest."""
        from datetime import datetime, timedelta, timezone

        from app.constants import DigestEntityType
        from app.models.intelligence import ActivityDigest
        from app.services import activity_digest_service as svc

        calls = {"n": 0}

        async def fake_cs(*a, **k):
            calls["n"] += 1
            return {
                "headline": "initial",
                "narrative": "n",
                "highlights": [],
                "status_signal": "on_track",
            }

        monkeypatch_svc = svc
        with (
            patch.object(svc, "_get_redis", return_value=None),
            patch("app.utils.claude_client.claude_structured", fake_cs),
        ):
            _mk_activity(db_session, requisition_id=101)
            _mk_activity(db_session, requisition_id=101)
            out1 = await svc.get_or_build_digest(DigestEntityType.REQUISITION, 101, db_session)
            assert out1["state"] == "ready"
            assert calls["n"] == 1

            # Expire the cooldown WITHOUT adding activities (basis stays same)
            row = db_session.query(ActivityDigest).filter_by(entity_id=101, entity_type="requisition").first()
            row.cooldown_until = datetime.now(timezone.utc) - timedelta(seconds=5)
            db_session.commit()

            # Same basis → should return cached immediately (line 170), no AI call
            out2 = await svc.get_or_build_digest(DigestEntityType.REQUISITION, 101, db_session)
            assert out2["state"] == "ready"
            assert out2["headline"] == "initial"
            assert calls["n"] == 1  # no new AI call

    @pytest.mark.asyncio
    async def test_redis_lock_acquire_exception_falls_back_to_acquired(self, db_session):
        """Lines 178-180: Redis set() raising an exception treats lock as acquired."""
        from app.constants import DigestEntityType
        from app.services import activity_digest_service as svc

        ai_calls = {"n": 0}

        async def fake_cs(*a, **k):
            ai_calls["n"] += 1
            return {
                "headline": "h",
                "narrative": "n",
                "highlights": [],
                "status_signal": "on_track",
            }

        class BrokenRedis:
            def set(self, *a, **k):
                raise ConnectionError("redis down")

            def delete(self, *a, **k):
                pass

        with (
            patch.object(svc, "_get_redis", return_value=BrokenRedis()),
            patch("app.utils.claude_client.claude_structured", fake_cs),
        ):
            _mk_activity(db_session, requisition_id=102)
            _mk_activity(db_session, requisition_id=102)
            out = await svc.get_or_build_digest(DigestEntityType.REQUISITION, 102, db_session)
        # Even though redis.set raised, we treat as acquired and run AI
        assert out["state"] == "ready"
        assert ai_calls["n"] == 1

    @pytest.mark.asyncio
    async def test_invalid_status_signal_from_ai_is_discarded(self, db_session):
        """Lines 212-213: AI returning unrecognized status_signal → silently discarded."""
        from app.constants import DigestEntityType
        from app.services import activity_digest_service as svc

        async def fake_cs(*a, **k):
            return {
                "headline": "h",
                "narrative": "n",
                "highlights": [],
                "status_signal": "INVALID_SIGNAL_XYZ",  # not in DigestStatusSignal
            }

        with (
            patch.object(svc, "_get_redis", return_value=None),
            patch("app.utils.claude_client.claude_structured", fake_cs),
        ):
            _mk_activity(db_session, requisition_id=103)
            _mk_activity(db_session, requisition_id=103)
            out = await svc.get_or_build_digest(DigestEntityType.REQUISITION, 103, db_session)

        assert out["state"] == "ready"
        assert out.get("status_signal") is None  # invalid signal stripped

    @pytest.mark.asyncio
    async def test_redis_lock_release_exception_is_swallowed(self, db_session):
        """Lines 233-236: Redis delete() raising logs warning but doesn't crash."""
        from app.constants import DigestEntityType
        from app.services import activity_digest_service as svc

        ai_calls = {"n": 0}

        async def fake_cs(*a, **k):
            ai_calls["n"] += 1
            return {
                "headline": "h",
                "narrative": "n",
                "highlights": [],
                "status_signal": "on_track",
            }

        class FailingDeleteRedis:
            def set(self, *a, **k):
                return True  # acquire succeeds

            def delete(self, *a, **k):
                raise ConnectionError("redis gone on delete")

        with (
            patch.object(svc, "_get_redis", return_value=FailingDeleteRedis()),
            patch("app.utils.claude_client.claude_structured", fake_cs),
        ):
            _mk_activity(db_session, requisition_id=104)
            _mk_activity(db_session, requisition_id=104)
            # Should NOT raise even though redis.delete crashes
            out = await svc.get_or_build_digest(DigestEntityType.REQUISITION, 104, db_session)

        assert out["state"] == "ready"
        assert ai_calls["n"] == 1


# ── sourcing_auto_progress ────────────────────────────────────────────


class TestSourcingAutoProgressEdgeCases:
    def test_invalid_transition_blocked_returns_false(self, db_session):
        """Lines 51-56: validate_transition returns False for non-allowed forward skip."""
        from app.constants import SourcingStatus
        from app.models.sourcing import Requirement as Req2
        from app.models.sourcing import Requisition as Req
        from app.services.sourcing_auto_progress import auto_progress_status

        req = Req(name="REQ-SKIP", status="active")
        db_session.add(req)
        db_session.flush()

        r = Req2(
            requisition_id=req.id,
            primary_mpn="TEST123",
            sourcing_status=SourcingStatus.OPEN,
        )
        db_session.add(r)
        db_session.commit()

        # OPEN → OFFERED: forward in order but OFFERED not in OPEN's allowed transitions
        result = auto_progress_status(r, SourcingStatus.OFFERED, db_session)
        assert result is False
        # Sourcing status must remain OPEN
        assert r.sourcing_status == SourcingStatus.OPEN


# ── ai_email_parser ────────────────────────────────────────────────────


class TestAiEmailParserExceptions:
    @pytest.mark.asyncio
    async def test_parse_email_claude_unavailable_returns_none(self):
        """Lines 127-129: ClaudeUnavailableError → return None gracefully."""
        from app.services.ai_email_parser import parse_email
        from app.utils.claude_errors import ClaudeUnavailableError

        with patch("app.services.ai_email_parser.claude_json") as mock_cj:
            mock_cj.side_effect = ClaudeUnavailableError("not configured")
            result = await parse_email("Hello vendor", "Subject")

        assert result is None

    @pytest.mark.asyncio
    async def test_parse_email_claude_error_returns_none(self):
        """Lines 130-132: ClaudeError → return None gracefully."""
        from app.services.ai_email_parser import parse_email
        from app.utils.claude_errors import ClaudeError

        with patch("app.services.ai_email_parser.claude_json") as mock_cj:
            mock_cj.side_effect = ClaudeError("api failure")
            result = await parse_email("Hello vendor", "Subject")

        assert result is None


# ── vendor_helpers exception paths ────────────────────────────────────


class TestVendorHelpersCommitExceptions:
    def test_domain_match_commit_exception_logs_and_continues(self, db_session):
        """Lines 67-69: commit failure when updating domain-matched alt name."""
        from app.utils.vendor_helpers import get_or_create_card

        # Create a card with a known domain
        existing = VendorCard(
            normalized_name="existing vendor",
            display_name="Existing Vendor",
            domain="existing-vendor.com",
            emails=[],
            phones=[],
            alternate_names=[],
        )
        db_session.add(existing)
        db_session.commit()

        # Patch db.commit to raise on the first call inside the domain match path
        original_commit = db_session.commit
        commit_calls = {"n": 0}

        def failing_commit():
            commit_calls["n"] += 1
            if commit_calls["n"] == 1:
                raise Exception("DB constraint violation")
            return original_commit()

        with patch.object(db_session, "commit", side_effect=failing_commit):
            with patch.object(db_session, "rollback"):
                # Should still return the matched card despite commit failure
                result = get_or_create_card("New Name For Existing Vendor", db_session, domain="existing-vendor.com")
        assert result.id == existing.id

    def test_new_vendor_card_commit_exception_reraises(self, db_session):
        """Lines 150-153: commit failure creating new VendorCard re-raises."""
        from app.utils.vendor_helpers import get_or_create_card

        original_commit = db_session.commit
        commit_calls = {"n": 0}

        def failing_commit():
            commit_calls["n"] += 1
            if commit_calls["n"] == 1:
                raise Exception("disk full")
            return original_commit()

        with patch.object(db_session, "commit", side_effect=failing_commit):
            with patch.object(db_session, "rollback"):
                with patch.dict("sys.modules", {"rapidfuzz": None}):
                    with pytest.raises(Exception, match="disk full"):
                        get_or_create_card("Brand New Vendor XYZ123", db_session)

    def test_fuzzy_match_commit_exception_logs_and_continues(self, db_session):
        """Lines 133-135: commit failure adding fuzzy-matched alt name logs and continues."""
        from app.utils.vendor_helpers import get_or_create_card

        card = VendorCard(
            normalized_name="acme corp",
            display_name="Acme Corp",
            emails=[],
            phones=[],
            alternate_names=[],
        )
        db_session.add(card)
        db_session.commit()

        mock_fuzz = MagicMock()
        mock_fuzz.token_sort_ratio.return_value = 95
        mock_rapidfuzz_module = MagicMock()
        mock_rapidfuzz_module.fuzz = mock_fuzz

        original_commit = db_session.commit
        commit_calls = {"n": 0}

        def maybe_failing_commit():
            commit_calls["n"] += 1
            if commit_calls["n"] == 1:
                raise Exception("lock timeout")
            return original_commit()

        with patch.dict(
            "sys.modules",
            {"rapidfuzz": mock_rapidfuzz_module, "rapidfuzz.fuzz": mock_fuzz},
        ):
            with patch.object(db_session, "commit", side_effect=maybe_failing_commit):
                with patch.object(db_session, "rollback"):
                    result = get_or_create_card("Acme Corpp", db_session)

        # Should return the matched card even though alt name commit failed
        assert result.id == card.id

    @pytest.mark.asyncio
    async def test_background_enrich_commit_exception_returns_early(self):
        """Lines 176-179: commit failure in _background_enrich_vendor logs and returns."""
        from app.utils.vendor_helpers import _background_enrich_vendor

        mock_enrichment = {"website": "https://example.com", "emails": ["info@example.com"]}
        mock_db = MagicMock()
        mock_card = MagicMock()
        mock_card.id = 999
        mock_db.get.return_value = mock_card
        mock_db.commit.side_effect = Exception("transaction aborted")
        mock_db.rollback = MagicMock()

        with patch(
            "app.enrichment_service.enrich_entity",
            new=AsyncMock(return_value=mock_enrichment),
        ):
            with patch("app.enrichment_service.apply_enrichment_to_vendor"):
                with patch("app.database.SessionLocal", return_value=mock_db):
                    # Should NOT raise
                    await _background_enrich_vendor(999, "example.com", "Example Co")


# ── company_merge_service ─────────────────────────────────────────────


class TestCompanyMergeEdgeCases:
    def test_reassignment_exception_logged_continues(self, db_session):
        """Lines 146-147: FK reassignment failing logs warning but merge continues."""
        from app.services.company_merge_service import merge_companies

        keep = Company(name="KeepCo", domain="keepco.com")
        remove = Company(name="RemoveCo", domain="removeco.com")
        db_session.add_all([keep, remove])
        db_session.commit()

        # Patch one of the update calls to raise
        original_query = db_session.query
        call_count = {"n": 0}

        class PatchedQuery:
            def __init__(self, model):
                self._inner = original_query(model)
                self._model = model

            def filter(self, *args, **kwargs):
                self._inner = self._inner.filter(*args, **kwargs)
                return self

            def update(self, *args, **kwargs):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise Exception("FK constraint violation")
                return self._inner.update(*args, **kwargs)

            def all(self, *args, **kwargs):
                return self._inner.all(*args, **kwargs)

            def first(self, *args, **kwargs):
                return self._inner.first(*args, **kwargs)

        with patch.object(db_session, "query", side_effect=PatchedQuery):
            # Should not raise — warnings are logged, merge continues
            result = merge_companies(keep.id, remove.id, db_session)

        assert result is not None

    def test_cache_invalidation_exception_logged(self, db_session):
        """Lines 158-159: cache invalidation failure is logged, merge still succeeds."""
        from app.services.company_merge_service import merge_companies

        keep = Company(name="KeepCo2", domain="keepco2.com")
        remove = Company(name="RemoveCo2", domain="removeco2.com")
        db_session.add_all([keep, remove])
        db_session.commit()

        with patch(
            "app.services.company_merge_service.invalidate_prefix",
            side_effect=Exception("cache error"),
            create=True,
        ):
            with patch(
                "app.cache.decorators.invalidate_prefix",
                side_effect=Exception("cache error"),
            ):
                result = merge_companies(keep.id, remove.id, db_session)

        assert result is not None


# ── requisition_service ────────────────────────────────────────────────


class TestRequisitionServiceSubstituteDedup:
    def test_clone_deduplicates_duplicate_substitutes(self, db_session):
        """Lines 186-188: duplicate normalized substitutes are deduplicated on clone."""
        from app.services.requisition_service import clone_requisition

        req = Requisition(name="ORIG-REQ", status="active")
        db_session.add(req)
        db_session.flush()

        # Requirement with duplicate substitutes (same MPN different case)
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            # substitutes include duplicates that normalize to same key
            substitutes=["LM317T", "lm317t", "LM317", "NE555P"],
        )
        db_session.add(r)
        db_session.commit()

        cloned = clone_requisition(db_session, req, user_id=None)
        assert cloned is not None
        # The cloned requirement should have deduplicated substitutes
        cloned_req = cloned.requirements[0]
        subs = cloned_req.substitutes or []
        # No duplicates after normalization
        assert len(subs) == len(set(s.upper() for s in subs))
