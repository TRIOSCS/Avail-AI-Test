"""
test_coverage_gaps_services.py — Tests targeting specific uncovered lines in:
  - app/services/email_threads.py (lines 231-232, 244-270, 335-336, 362-363,
    399-401, 589-591)
  - app/services/gradient_service.py (lines 127-130, 228, 262-275)
  - app/services/engagement_scorer.py (lines 65, 192-193, 211, 216, 269-270,
    325-326, 431-433)
"""

import os

os.environ["TESTING"] = "1"

import json
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import (
    ActivityLog,
    BuyPlan,
    Company,
    Contact,
    CustomerSite,
    Offer,
    ProactiveOffer,
    Quote,
    Requirement,
    Requisition,
    Sighting,
    SiteContact,
    User,
    VendorCard,
    VendorResponse,
)

# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


def _make_vendor(db, name="Test Vendor", domain="testvendor.com", domain_aliases=None):
    vc = VendorCard(
        normalized_name=name.lower(),
        display_name=name,
        domain=domain,
        domain_aliases=domain_aliases or [],
        emails=[f"sales@{domain}"] if domain else [],
        created_at=datetime.now(timezone.utc),
    )
    db.add(vc)
    db.flush()
    return vc


def _make_user(db, email, role="buyer", name="Buyer"):
    u = User(
        email=email,
        name=name,
        role=role,
        azure_id=f"azure-{email}",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_requisition(db, user_id):
    r = Requisition(
        name="REQ-GAP",
        customer_name="Test Customer",
        status="open",
        created_by=user_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(r)
    db.flush()
    return r


def _make_requirement(db, requisition_id, mpn="LM358DR"):
    req = Requirement(
        requisition_id=requisition_id,
        primary_mpn=mpn,
        target_qty=100,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    return req


def _make_contact(
    db, requisition_id, user_id, vendor_name, contact_type="email", created_at=None, graph_conversation_id=None
):
    c = Contact(
        requisition_id=requisition_id,
        user_id=user_id,
        vendor_name=vendor_name,
        contact_type=contact_type,
        status="sent",
        created_at=created_at or datetime.now(timezone.utc),
        graph_conversation_id=graph_conversation_id,
    )
    db.add(c)
    db.flush()
    return c


def _make_offer(
    db,
    requisition_id,
    vendor_card_id,
    user_id,
    vendor_name="Test Vendor",
    status="active",
    unit_price=1.00,
    created_at=None,
):
    o = Offer(
        requisition_id=requisition_id,
        vendor_card_id=vendor_card_id,
        vendor_name=vendor_name,
        mpn="LM358DR",
        qty_available=100,
        unit_price=unit_price,
        entered_by_id=user_id,
        status=status,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(o)
    db.flush()
    return o


def _make_vendor_response(
    db,
    vendor_name,
    vendor_email,
    contact_id=None,
    requisition_id=None,
    received_at=None,
    status="new",
    graph_conversation_id=None,
):
    vr = VendorResponse(
        vendor_name=vendor_name,
        vendor_email=vendor_email,
        contact_id=contact_id,
        requisition_id=requisition_id,
        received_at=received_at or datetime.now(timezone.utc),
        status=status,
        graph_conversation_id=graph_conversation_id,
    )
    db.add(vr)
    db.flush()
    return vr


# ===========================================================================
#  EMAIL THREADS
# ===========================================================================


class TestEmailTier1Error:
    """Lines 231-232: Graph API error in Tier 1."""

    @pytest.mark.asyncio
    async def test_tier1_graph_error(self, db_session):
        from app.services.email_threads import clear_cache, fetch_threads_for_requirement

        clear_cache()
        user = _make_user(db_session, "t1e@t.com")
        rq = _make_requisition(db_session, user.id)
        requirement = _make_requirement(db_session, rq.id)
        _make_contact(db_session, rq.id, user.id, "V1", graph_conversation_id="conv-err-1")
        db_session.commit()

        with patch("app.services.email_threads.GraphClient") as gc_cls:
            gc = MagicMock()
            gc.get_all_pages = AsyncMock(side_effect=RuntimeError("Graph fail"))
            gc_cls.return_value = gc

            result = await fetch_threads_for_requirement(requirement.id, "tok", db_session, user_id=user.id)
        assert isinstance(result, list)


class TestEmailTier1b:
    """Lines 244-270: Tier 1b VendorResponse conversationId match."""

    @pytest.mark.asyncio
    async def test_tier1b_vendor_response_match(self, db_session):
        from app.services.email_threads import clear_cache, fetch_threads_for_requirement

        clear_cache()
        user = _make_user(db_session, "t1b@t.com")
        rq = _make_requisition(db_session, user.id)
        requirement = _make_requirement(db_session, rq.id)

        _make_vendor_response(
            db_session, "VR", "vr@vendor.com", requisition_id=rq.id, graph_conversation_id="vr-conv-1"
        )
        db_session.commit()

        msgs = [
            {
                "id": "m1",
                "subject": "RE: RFQ",
                "from": {"emailAddress": {"name": "VR", "address": "vr@vendor.com"}},
                "toRecipients": [{"emailAddress": {"address": "buy@trioscs.com"}}],
                "bodyPreview": "Quote",
                "receivedDateTime": "2026-02-20T10:00:00Z",
                "conversationId": "vr-conv-1",
            }
        ]

        async def _gap(*args, **kwargs):
            params = kwargs.get("params", {})
            f = params.get("$filter", "")
            if "vr-conv-1" in f:
                return msgs
            return []

        with patch("app.services.email_threads.GraphClient") as gc_cls:
            gc = MagicMock()
            gc.get_all_pages = AsyncMock(side_effect=_gap)
            gc_cls.return_value = gc

            result = await fetch_threads_for_requirement(requirement.id, "tok", db_session, user_id=user.id)

        assert len(result) >= 1
        assert result[0]["matched_via"] == "conversation_id"

    @pytest.mark.asyncio
    async def test_tier1b_graph_error(self, db_session):
        from app.services.email_threads import clear_cache, fetch_threads_for_requirement

        clear_cache()
        user = _make_user(db_session, "t1be@t.com")
        rq = _make_requisition(db_session, user.id)
        requirement = _make_requirement(db_session, rq.id)

        _make_vendor_response(db_session, "VR2", "vr2@v.com", requisition_id=rq.id, graph_conversation_id="vr-err-1")
        db_session.commit()

        async def _gap(*args, **kwargs):
            params = kwargs.get("params", {})
            f = params.get("$filter", "")
            if "vr-err-1" in f:
                raise RuntimeError("VR Graph fail")
            return []

        with patch("app.services.email_threads.GraphClient") as gc_cls:
            gc = MagicMock()
            gc.get_all_pages = AsyncMock(side_effect=_gap)
            gc_cls.return_value = gc

            result = await fetch_threads_for_requirement(requirement.id, "tok", db_session, user_id=user.id)
        assert isinstance(result, list)


class TestEmailTier3Error:
    """Lines 335-336: Part number search error."""

    @pytest.mark.asyncio
    async def test_tier3_error(self, db_session):
        from app.services.email_threads import clear_cache, fetch_threads_for_requirement

        clear_cache()
        user = _make_user(db_session, "t3e@t.com")
        rq = _make_requisition(db_session, user.id)
        requirement = _make_requirement(db_session, rq.id, mpn="TEST-PART-XYZ")
        db_session.commit()

        async def _gap(*args, **kwargs):
            params = kwargs.get("params", {})
            s = params.get("$search", "")
            if "TEST-PART-XYZ" in s:
                raise RuntimeError("PN search fail")
            return []

        with patch("app.services.email_threads.GraphClient") as gc_cls:
            gc = MagicMock()
            gc.get_all_pages = AsyncMock(side_effect=_gap)
            gc_cls.return_value = gc

            result = await fetch_threads_for_requirement(requirement.id, "tok", db_session, user_id=user.id)
        assert isinstance(result, list)


class TestEmailTier4CardDomain:
    """Lines 362-363: VendorCard.domain added to vendor_domains."""

    @pytest.mark.asyncio
    async def test_tier4_card_domain(self, db_session):
        from app.services.email_threads import clear_cache, fetch_threads_for_requirement

        clear_cache()
        user = _make_user(db_session, "t4c@t.com")
        rq = _make_requisition(db_session, user.id)
        requirement = _make_requirement(db_session, rq.id, mpn="XY")

        vc = _make_vendor(db_session, "T4V", "t4v.com")

        s = Sighting(
            requirement_id=requirement.id,
            vendor_name="t4v",
            vendor_email=None,
            mpn_matched="XY",
        )
        db_session.add(s)
        db_session.commit()

        msgs = [
            {
                "id": "m-t4",
                "subject": "Offer",
                "from": {"emailAddress": {"name": "T4", "address": "s@t4v.com"}},
                "toRecipients": [{"emailAddress": {"address": "b@trioscs.com"}}],
                "bodyPreview": "Stock",
                "receivedDateTime": "2026-02-20T10:00:00Z",
                "conversationId": "t4-c1",
            }
        ]

        async def _gap(*args, **kwargs):
            params = kwargs.get("params", {})
            s = params.get("$search", "")
            if "t4v.com" in s:
                return msgs
            return []

        with patch("app.services.email_threads.GraphClient") as gc_cls:
            gc = MagicMock()
            gc.get_all_pages = AsyncMock(side_effect=_gap)
            gc_cls.return_value = gc

            result = await fetch_threads_for_requirement(requirement.id, "tok", db_session, user_id=user.id)
        assert isinstance(result, list)


class TestEmailTier4DomainError:
    """Lines 399-401: Domain search error handler."""

    @pytest.mark.asyncio
    async def test_tier4_domain_search_error(self, db_session):
        from app.services.email_threads import clear_cache, fetch_threads_for_requirement

        clear_cache()
        user = _make_user(db_session, "t4e@t.com")
        rq = _make_requisition(db_session, user.id)
        requirement = _make_requirement(db_session, rq.id, mpn="ZZ")

        s = Sighting(
            requirement_id=requirement.id,
            vendor_name="ErrD",
            vendor_email="s@errd.com",
            mpn_matched="ZZ",
        )
        db_session.add(s)
        db_session.commit()

        async def _gap(*args, **kwargs):
            params = kwargs.get("params", {})
            s = params.get("$search", "")
            if "errd.com" in s:
                raise RuntimeError("Domain search fail")
            return []

        with patch("app.services.email_threads.GraphClient") as gc_cls:
            gc = MagicMock()
            gc.get_all_pages = AsyncMock(side_effect=_gap)
            gc_cls.return_value = gc

            result = await fetch_threads_for_requirement(requirement.id, "tok", db_session, user_id=user.id)
        assert isinstance(result, list)


class TestEmailVendorDomainError:
    """Lines 589-591: vendor domain search error in fetch_threads_for_vendor."""

    @pytest.mark.asyncio
    async def test_vendor_domain_search_error(self, db_session):
        from app.services.email_threads import clear_cache, fetch_threads_for_vendor

        clear_cache()
        vc = _make_vendor(db_session, "VDErr", "vderr.com")
        db_session.commit()

        with patch("app.services.email_threads.GraphClient") as gc_cls:
            gc = MagicMock()
            gc.get_all_pages = AsyncMock(side_effect=RuntimeError("VD fail"))
            gc_cls.return_value = gc

            result = await fetch_threads_for_vendor(vc.id, "tok", db_session, user_id=1)
        assert result == []


# ===========================================================================
#  GRADIENT SERVICE
# ===========================================================================


@pytest.fixture(autouse=True)
def _ensure_gradient_key():
    from app.config import settings

    orig = settings.do_gradient_api_key
    settings.do_gradient_api_key = "test-key"
    yield
    settings.do_gradient_api_key = orig


def _gmock(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text or json.dumps(json_data or {})
    resp.json.return_value = json_data or {}
    return resp


def _gchat(content):
    return {
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
        "model": "anthropic-claude-4.5-sonnet",
    }


class TestGradientExceptionExhausted:
    """Lines 127-130: All retries exhausted by exceptions."""

    @pytest.mark.asyncio
    async def test_all_exceptions_exhausted(self):
        from app.services.gradient_service import gradient_text

        with patch("app.services.gradient_service.http") as mh:
            mh.post = AsyncMock(
                side_effect=[
                    TimeoutError("T1"),
                    TimeoutError("T2"),
                    TimeoutError("T3"),
                ]
            )
            with patch("app.services.gradient_service.asyncio.sleep", new_callable=AsyncMock):
                result = await gradient_text("Test")

        assert result is None
        assert mh.post.call_count == 3


class TestGradientJsonParsedResult:
    """Line 228: _safe_json_parse returns valid data."""

    @pytest.mark.asyncio
    async def test_json_parsed_successfully(self):
        from app.services.gradient_service import gradient_json

        data = _gchat('{"vendor": "Mouser", "ok": true}')
        with patch("app.services.gradient_service.http") as mh:
            mh.post = AsyncMock(return_value=_gmock(200, data))
            result = await gradient_json("Check", system="You are a helper.")

        assert result == {"vendor": "Mouser", "ok": True}


class TestSafeJsonParseNewlines:
    """Lines 262-275: Newline-fixing regex and final return None."""

    def test_literal_newlines_in_json_strings(self):
        from app.services.gradient_service import _safe_json_parse

        text = '{"desc": "Line 1\nLine 2", "count": 5}'
        result = _safe_json_parse(text)
        assert result is not None
        assert result["count"] == 5

    def test_literal_tabs_in_json_strings(self):
        from app.services.gradient_service import _safe_json_parse

        text = '{"note": "col1\tcol2", "ok": true}'
        result = _safe_json_parse(text)
        assert result is not None
        assert result["ok"] is True

    def test_literal_carriage_return_in_json_strings(self):
        from app.services.gradient_service import _safe_json_parse

        text = '{"data": "val\rwith cr", "n": 1}'
        result = _safe_json_parse(text)
        assert result is not None
        assert result["n"] == 1

    def test_unfixable_json_returns_none(self):
        from app.services.gradient_service import _safe_json_parse

        text = "Preamble {invalid json that cannot be fixed} more"
        result = _safe_json_parse(text)
        assert result is None

    def test_array_with_newlines(self):
        from app.services.gradient_service import _safe_json_parse

        text = '[{"name": "first\nsecond"}, {"name": "ok"}]'
        result = _safe_json_parse(text)
        assert result is not None
        assert len(result) == 2

    def test_preamble_broken_json(self):
        from app.services.gradient_service import _safe_json_parse

        text = 'Result: {"key": value_no_quotes} done'
        result = _safe_json_parse(text)
        assert result is None


# ===========================================================================
#  ENGAGEMENT SCORER
# ===========================================================================


class TestEngagementNowDefault:
    """Line 65: now=None defaults to datetime.now(utc)."""

    def test_now_defaults(self):
        from app.services.engagement_scorer import compute_engagement_score

        result = compute_engagement_score(0, 0, 0, None, None)
        assert result["engagement_score"] == 50


class TestEngagementDomainAliases:
    """Lines 192-193: domain_aliases added to domain_to_norm."""

    @pytest.mark.asyncio
    async def test_aliases_used(self, db_session):
        from app.services.engagement_scorer import compute_all_engagement_scores

        user = _make_user(db_session, "ea@t.com")
        vc = _make_vendor(db_session, "EngA", "enga.com", domain_aliases=["enga-alt.com", "enga-old.com"])
        req = _make_requisition(db_session, user.id)
        for _ in range(3):
            _make_contact(db_session, req.id, user.id, "EngA")

        _make_vendor_response(db_session, "R", "r@enga-alt.com", received_at=datetime.now(timezone.utc))
        db_session.commit()

        await compute_all_engagement_scores(db_session)
        db_session.refresh(vc)
        assert vc.total_responses >= 1


class TestEngagementNoAtEmail:
    """Line 211: email without @ skipped."""

    @pytest.mark.asyncio
    async def test_no_at_skipped(self, db_session):
        from app.services.engagement_scorer import compute_all_engagement_scores

        user = _make_user(db_session, "na@t.com")
        _make_vendor(db_session, "NoAt", "noat.com")
        req = _make_requisition(db_session, user.id)
        for _ in range(3):
            _make_contact(db_session, req.id, user.id, "NoAt")

        _make_vendor_response(db_session, "Bad", "no-at-sign", received_at=datetime.now(timezone.utc))
        db_session.commit()

        result = await compute_all_engagement_scores(db_session)
        assert result["updated"] >= 1


class TestEngagementFallbackNormalize:
    """Line 216: Fallback normalize for unmatched domains."""

    @pytest.mark.asyncio
    async def test_fallback_normalize(self, db_session):
        from app.services.engagement_scorer import compute_all_engagement_scores

        user = _make_user(db_session, "fb@t.com")
        vc = VendorCard(
            normalized_name="fallbackv",
            display_name="FallbackV",
            domain=None,
            domain_aliases=[],
            emails=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vc)
        db_session.flush()

        req = _make_requisition(db_session, user.id)
        for _ in range(3):
            _make_contact(db_session, req.id, user.id, "FallbackV")

        _make_vendor_response(db_session, "SP", "sales@unknown.com", received_at=datetime.now(timezone.utc))
        db_session.commit()

        result = await compute_all_engagement_scores(db_session)
        assert result["updated"] >= 1


class TestEngagementWinMap:
    """Lines 269-270: win_map populated from won offers."""

    @pytest.mark.asyncio
    async def test_wins_counted(self, db_session):
        from app.services.engagement_scorer import compute_all_engagement_scores

        user = _make_user(db_session, "win@t.com")
        vc = _make_vendor(db_session, "WinV", "winv.com")
        req = _make_requisition(db_session, user.id)
        for _ in range(3):
            _make_contact(db_session, req.id, user.id, "WinV")

        o = Offer(
            requisition_id=req.id,
            vendor_card_id=vc.id,
            vendor_name="WinV",
            mpn="X",
            qty_available=100,
            unit_price=1.0,
            entered_by_id=user.id,
            status="won",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(o)
        _make_vendor_response(db_session, "R", "r@winv.com", received_at=datetime.now(timezone.utc))
        db_session.commit()

        await compute_all_engagement_scores(db_session)
        db_session.refresh(vc)
        assert vc.total_wins >= 1


class TestEngagementFlushError:
    """Lines 325-326: flush error during batch processing."""

    @pytest.mark.asyncio
    async def test_flush_error_handled(self, db_session):
        from app.services.engagement_scorer import compute_all_engagement_scores

        _make_vendor(db_session, "FlushE", "flushe.com")
        db_session.commit()

        count = [0]
        orig = db_session.flush

        def _bad(*a, **kw):
            count[0] += 1
            if count[0] >= 2:
                raise RuntimeError("Flush fail")
            return orig(*a, **kw)

        with patch.object(db_session, "flush", side_effect=_bad):
            result = await compute_all_engagement_scores(db_session)
        assert isinstance(result, dict)


class TestEngagementOutboundFlushError:
    """Lines 431-433: flush error in apply_outbound_stats."""

    def test_outbound_flush_error(self, db_session):
        from app.services.engagement_scorer import apply_outbound_stats

        _make_vendor(db_session, "OBFlush", "obflush.com")
        db_session.commit()

        def _raise(*a, **kw):
            raise RuntimeError("Flush fail")

        with patch.object(db_session, "flush", side_effect=_raise):
            result = apply_outbound_stats(db_session, {"obflush.com": 5})
        assert result == 1


class TestGradientJsonTextIsNone:
    """Line 228: gradient_json returns None when gradient_text returns None."""

    @pytest.mark.asyncio
    async def test_gradient_json_none_when_text_none(self):
        from app.services.gradient_service import gradient_json

        # Patch gradient_text to return None (e.g. API failure)
        with patch("app.services.gradient_service.gradient_text", new_callable=AsyncMock, return_value=None):
            result = await gradient_json("Parse this")

        assert result is None

    @pytest.mark.asyncio
    async def test_gradient_json_none_when_text_empty(self):
        from app.services.gradient_service import gradient_json

        # Patch gradient_text to return empty string
        with patch("app.services.gradient_service.gradient_text", new_callable=AsyncMock, return_value=""):
            result = await gradient_json("Parse this")

        assert result is None


class TestGradientCallLlmLoopExhausted:
    """Line 130: return None after for loop (when MAX_RETRIES=0)."""

    @pytest.mark.asyncio
    async def test_zero_retries_returns_none(self):
        from app.services.gradient_service import gradient_text

        # Patch MAX_RETRIES to 0 so the for loop never executes
        with patch("app.services.gradient_service.MAX_RETRIES", 0):
            with patch("app.services.gradient_service.http") as mh:
                mh.post = AsyncMock(return_value=_gmock(200, _gchat("ok")))
                result = await gradient_text("Test zero retries")

        assert result is None


class TestEngagementFlushErrorInBatch:
    """Lines 325-326: flush exception during batch processing (more targeted)."""

    @pytest.mark.asyncio
    async def test_batch_flush_exception_logged(self, db_session):
        from app.services.engagement_scorer import compute_all_engagement_scores

        _make_vendor(db_session, "FlushBatch", "flushbatch.com")
        db_session.commit()

        original_flush = db_session.flush.__func__ if hasattr(db_session.flush, "__func__") else None

        # Directly mock db.flush to raise on the batch flush call
        call_idx = [0]

        def _counting_flush(*a, **kw):
            call_idx[0] += 1
            # The batch flush happens after card updates - raise then
            raise RuntimeError("Batch flush error")

        with patch.object(db_session, "flush", side_effect=_counting_flush):
            result = await compute_all_engagement_scores(db_session)

        # The commit will also fail, so updated=0
        assert isinstance(result, dict)
