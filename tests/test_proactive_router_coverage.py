"""tests/test_proactive_router_coverage.py — Targeted coverage for
app/routers/htmx/proactive.py.

Covers the uncovered HTMX endpoint lines:
  110-117  proactive_batch_dismiss with non-empty match_ids
  156-178  proactive_prepare_page valid matches + site path
  203,215  proactive_prepare_page context rendering (same flow)
  348-436  proactive_draft_for_prepare draft generation + exception fallback
  466-473  proactive_send_offer sell_price_ form-field parsing
  477-528  proactive_send_offer token+service flow + success re-render
  602      proactive_convert ValueError not "already converted" → 403
  667-690  proactive_do_not_offer creates record, dedup, returns HTML

Coverage strategy: use ``httpx.AsyncClient`` (ASGITransport) so endpoint
coroutines run in the SAME event loop as the test.  Starlette's TestClient
spawns the ASGI event loop in a background thread; coverage's sys.settrace is
thread-local and does not follow the coroutine back into that thread, so async
lines after ``await`` are invisible to coverage.  AsyncClient keeps everything
on one event loop, so every statement is measured correctly.

Called by: pytest
Depends on: app/routers/htmx/proactive.py, tests/conftest.py
"""

from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.responses import HTMLResponse
from httpx import ASGITransport
from sqlalchemy.orm import Session
from starlette.requests import Request as _SR

from app.models import ProactiveMatch, SiteContact, User

# ── Async client fixture ──────────────────────────────────────────────


@pytest.fixture()
async def ac(db_session: Session, test_user: User):
    """httpx.AsyncClient backed by the ASGI app with auth overrides.

    Runs in the same event loop as the calling test so that coverage's sys.settrace
    traces every coroutine line inside the endpoint handlers. The
    ``_restore_dependency_overrides`` autouse fixture in conftest.py handles teardown of
    the overrides after each test.
    """
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    def _db():
        yield db_session

    async def _fresh_token():
        return "mock-token"

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: test_user
    app.dependency_overrides[require_admin] = lambda: test_user
    app.dependency_overrides[require_buyer] = lambda: test_user
    app.dependency_overrides[require_fresh_token] = _fresh_token

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


# ── Helpers ───────────────────────────────────────────────────────────


def _make_match(
    db: Session,
    user: User,
    requisition,
    offer,
    site,
    *,
    company_id: int | None = None,
) -> ProactiveMatch:
    """Create and commit a ProactiveMatch owned by *user*."""
    match = ProactiveMatch(
        offer_id=offer.id,
        requisition_id=requisition.id,
        customer_site_id=site.id,
        salesperson_id=user.id,
        mpn="LM317T",
        status="new",
        **({"company_id": company_id} if company_id is not None else {}),
    )
    db.add(match)
    db.commit()
    db.refresh(match)
    return match


_EMPTY_MATCHES: dict = {"groups": [], "stats": {"total": 0}}


def _html_ok() -> HTMLResponse:
    return HTMLResponse("<div>ok</div>")


# ── Lines 110-117: batch dismiss with match_ids ───────────────────────


class TestBatchDismissWithMatchIds:
    """POST /v2/partials/proactive/batch-dismiss with non-empty match_ids."""

    async def test_updates_status_to_dismissed(
        self, ac, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with (
            patch("app.services.proactive_service.get_matches_for_user", return_value=_EMPTY_MATCHES),
            patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()),
        ):
            resp = await ac.post(
                "/v2/partials/proactive/batch-dismiss",
                data={"match_ids": str(match.id)},
            )

        assert resp.status_code == 200
        db_session.refresh(match)
        assert match.status == "dismissed"
        assert match.dismiss_reason == "batch_dismiss"

    async def test_empty_match_ids_skips_update(
        self, ac, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        """Empty match_ids list → skips the bulk-update block; existing matches
        untouched."""
        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with (
            patch("app.services.proactive_service.get_matches_for_user", return_value=_EMPTY_MATCHES),
            patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()),
        ):
            resp = await ac.post("/v2/partials/proactive/batch-dismiss", data={})

        assert resp.status_code == 200
        db_session.refresh(match)
        assert match.status == "new"
        assert match.dismiss_reason is None


# ── Lines 156-178, 203, 215: prepare page happy path ─────────────────


class TestPreparePage:
    """POST /v2/proactive/prepare/{site_id} — valid match_ids."""

    async def test_renders_prepare_template_for_valid_match(
        self, ac, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()) as mock_tpl:
            resp = await ac.post(
                f"/v2/proactive/prepare/{test_customer_site.id}",
                data={"match_ids": str(match.id)},
            )

        assert resp.status_code == 200
        # Lines 203-215: verify template context was built with matches + site info
        template_name, ctx = mock_tpl.call_args[0]
        assert ctx["site_id"] == test_customer_site.id
        assert "matches" in ctx
        assert len(ctx["matches"]) == 1
        assert ctx["matches"][0]["mpn"] == "LM317T"

    async def test_prepare_with_contacts_includes_contact_data(
        self, ac, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        """Contacts at the site are serialised into the template context."""
        contact = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Jane Buyer",
            email="jane@acme.com",
            is_primary=True,
        )
        db_session.add(contact)
        db_session.flush()
        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()) as mock_tpl:
            resp = await ac.post(
                f"/v2/proactive/prepare/{test_customer_site.id}",
                data={"match_ids": str(match.id)},
            )

        assert resp.status_code == 200
        _, ctx = mock_tpl.call_args[0]
        assert len(ctx["contacts"]) == 1
        assert ctx["contacts"][0]["full_name"] == "Jane Buyer"
        assert ctx["contacts"][0]["has_email"] is True

    async def test_no_match_ids_redirects(self, ac, test_customer_site):
        """Empty match_ids → 303 redirect to /v2/proactive."""
        resp = await ac.post(
            f"/v2/proactive/prepare/{test_customer_site.id}",
            data={},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def test_match_not_found_in_db_redirects(self, ac, test_customer_site):
        """match_ids non-empty but salesperson filter returns nothing → 303 redirect."""
        resp = await ac.post(
            f"/v2/proactive/prepare/{test_customer_site.id}",
            data={"match_ids": "99999"},
            follow_redirects=False,
        )
        assert resp.status_code == 303


# ── Lines 348-436: draft generation ──────────────────────────────────


class TestDraftForPrepare:
    """POST /v2/partials/proactive/draft — match found, AI draft path."""

    async def test_draft_success_injects_subject_and_body_via_script(
        self, ac, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with patch(
            "app.services.proactive_email.draft_proactive_email",
            new_callable=AsyncMock,
            return_value={"subject": "Parts Available", "body": "Hello! We have LM317T."},
        ):
            resp = await ac.post(
                "/v2/partials/proactive/draft",
                data={"match_ids": str(match.id)},
            )

        assert resp.status_code == 200
        assert b"<script>" in resp.content
        assert b"Parts Available" in resp.content

    async def test_draft_with_contact_id_passes_first_name(
        self, ac, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        """Lines 361-367: contact_ids[0] first name is resolved and passed to AI."""
        contact = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Alice Smith",
            email="alice@acme.com",
            is_primary=True,
        )
        db_session.add(contact)
        db_session.flush()
        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with patch(
            "app.services.proactive_email.draft_proactive_email",
            new_callable=AsyncMock,
            return_value={"subject": "Hi Alice!", "body": "We have parts for you."},
        ) as mock_draft:
            resp = await ac.post(
                "/v2/partials/proactive/draft",
                data={"match_ids": str(match.id), "contact_ids": str(contact.id)},
            )

        assert resp.status_code == 200
        assert mock_draft.called
        _, kwargs = mock_draft.call_args
        assert kwargs.get("contact_name") == "Alice"

    async def test_draft_with_sell_price_field_passes_value_to_service(
        self, ac, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        """Lines 369-379: sell_price_<id> form field overrides the default markup."""
        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with patch(
            "app.services.proactive_email.draft_proactive_email",
            new_callable=AsyncMock,
            return_value={"subject": "Offer", "body": "Details inside."},
        ) as mock_draft:
            resp = await ac.post(
                "/v2/partials/proactive/draft",
                data={"match_ids": str(match.id), f"sell_price_{match.id}": "3.25"},
            )

        assert resp.status_code == 200
        assert mock_draft.called
        _, kwargs = mock_draft.call_args
        assert kwargs["parts"][0]["sell_price"] == pytest.approx(3.25)

    async def test_draft_ai_exception_returns_fallback_html(
        self, ac, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        """Lines 433-436: exception from AI service → fallback 'unavailable' partial."""
        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with patch(
            "app.services.proactive_email.draft_proactive_email",
            new_callable=AsyncMock,
            side_effect=RuntimeError("AI service down"),
        ):
            resp = await ac.post(
                "/v2/partials/proactive/draft",
                data={"match_ids": str(match.id)},
            )

        assert resp.status_code == 200
        body_lower = resp.content.lower()
        assert b"unavailable" in body_lower or b"manually" in body_lower

    async def test_draft_no_match_ids_returns_error_html(self, ac):
        resp = await ac.post("/v2/partials/proactive/draft", data={})
        assert resp.status_code == 200
        assert b"No matches selected" in resp.content

    async def test_draft_match_not_in_db_returns_error_html(self, ac):
        resp = await ac.post(
            "/v2/partials/proactive/draft",
            data={"match_ids": "99999"},
        )
        assert resp.status_code == 200
        assert b"No valid matches" in resp.content


# ── Lines 466-528: proactive_send_offer ──────────────────────────────


class TestSendOffer:
    """POST /v2/proactive/send — sell-price parsing and full service flow."""

    async def test_sell_price_field_parsed_and_forwarded(
        self, ac, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        """Lines 466-473: sell_price_<id> form field is parsed and passed to service."""
        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch(
                "app.services.proactive_service.send_proactive_offer",
                new_callable=AsyncMock,
                return_value={"line_items": [], "recipient_emails": ["a@b.com"]},
            ) as mock_send,
            patch("app.services.proactive_service.get_matches_for_user", return_value=_EMPTY_MATCHES),
            patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()),
        ):
            resp = await ac.post(
                "/v2/proactive/send",
                data={
                    "match_ids": str(match.id),
                    "contact_ids": "1",
                    f"sell_price_{match.id}": "2.50",
                },
            )

        assert resp.status_code == 200
        _, kwargs = mock_send.call_args
        assert kwargs.get("sell_prices", {}).get(str(match.id)) == pytest.approx(2.50)

    async def test_full_flow_renders_success_message(
        self, ac, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        """Lines 477-522: token fetched → service called → success banner rendered."""
        _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="mock-token"),
            patch(
                "app.services.proactive_service.send_proactive_offer",
                new_callable=AsyncMock,
                return_value={"line_items": [{"mpn": "LM317T"}], "recipient_emails": ["a@b.com"]},
            ),
            patch("app.services.proactive_service.get_matches_for_user", return_value=_EMPTY_MATCHES),
            patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()) as mock_tpl,
        ):
            resp = await ac.post(
                "/v2/proactive/send",
                data={
                    "match_ids": "1",
                    "contact_ids": "99",
                    "subject": "Proactive Offer",
                    "body": "Hello, please review.",
                },
            )

        assert resp.status_code == 200
        assert mock_tpl.called
        _, ctx = mock_tpl.call_args[0]
        assert "Offer sent" in ctx.get("success_msg", "")

    async def test_no_match_ids_raises_400(self, ac):
        resp = await ac.post("/v2/proactive/send", data={"contact_ids": "1"})
        assert resp.status_code == 400

    async def test_no_contact_ids_raises_400(self, ac):
        resp = await ac.post("/v2/proactive/send", data={"match_ids": "1"})
        assert resp.status_code == 400

    async def test_value_error_from_service_raises_400(self, ac):
        """Lines 524-525: ValueError from send_proactive_offer → 400."""
        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch(
                "app.services.proactive_service.send_proactive_offer",
                new_callable=AsyncMock,
                side_effect=ValueError("No contacts found"),
            ),
        ):
            resp = await ac.post(
                "/v2/proactive/send",
                data={"match_ids": "1", "contact_ids": "1"},
            )
        assert resp.status_code == 400

    async def test_unexpected_exception_raises_500(self, ac):
        """Lines 526-528: unexpected (non-ValueError) exception → 500."""
        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch(
                "app.services.proactive_service.send_proactive_offer",
                new_callable=AsyncMock,
                side_effect=RuntimeError("network failure"),
            ),
        ):
            resp = await ac.post(
                "/v2/proactive/send",
                data={"match_ids": "1", "contact_ids": "1"},
            )
        assert resp.status_code == 500


# ── Line 602: proactive_convert non-already-converted ValueError → 403 ─


class TestConvertValueError403:
    """POST /v2/partials/proactive/{offer_id}/convert — ValueError without 'already
    converted'."""

    async def test_value_error_not_already_converted_returns_403(self, ac, test_proactive_offer):
        with patch(
            "app.services.proactive_service.convert_proactive_to_win",
            side_effect=ValueError("Not authorized to convert"),
        ):
            resp = await ac.post(f"/v2/partials/proactive/{test_proactive_offer.id}/convert")

        assert resp.status_code == 403

    async def test_already_converted_value_error_returns_409(self, ac, test_proactive_offer):
        with patch(
            "app.services.proactive_service.convert_proactive_to_win",
            side_effect=ValueError("Offer already converted"),
        ):
            resp = await ac.post(f"/v2/partials/proactive/{test_proactive_offer.id}/convert")

        assert resp.status_code == 409

    async def test_offer_not_found_returns_404(self, ac):
        resp = await ac.post("/v2/partials/proactive/99999/convert")
        assert resp.status_code == 404


# ── Lines 667-690: proactive_do_not_offer ────────────────────────────


class TestDoNotOffer:
    """POST /v2/partials/proactive/do-not-offer — creates record, dedup, returns
    HTML."""

    async def test_creates_record_and_returns_hidden_row(self, ac, db_session, test_user, test_company):
        test_company.account_owner_id = test_user.id
        db_session.commit()

        with patch("app.services.proactive_helpers.is_do_not_offer", return_value=False):
            resp = await ac.post(
                "/v2/partials/proactive/do-not-offer",
                data={"mpn": "LM317T", "company_id": str(test_company.id)},
            )

        assert resp.status_code == 200
        assert b"<tr" in resp.content

    async def test_dedup_when_already_suppressed_still_returns_html(self, ac, db_session, test_user, test_company):
        test_company.account_owner_id = test_user.id
        db_session.commit()

        with patch("app.services.proactive_helpers.is_do_not_offer", return_value=True):
            resp = await ac.post(
                "/v2/partials/proactive/do-not-offer",
                data={"mpn": "BC547", "company_id": str(test_company.id)},
            )

        assert resp.status_code == 200
        assert b"<tr" in resp.content

    async def test_accepts_customer_site_id_as_company_id_fallback(self, ac, db_session, test_user, test_company):
        """customer_site_id form field is accepted when company_id is absent."""
        from app.models.intelligence import ProactiveDoNotOffer

        test_company.account_owner_id = test_user.id
        db_session.commit()

        with patch("app.services.proactive_helpers.is_do_not_offer", return_value=False):
            resp = await ac.post(
                "/v2/partials/proactive/do-not-offer",
                data={"mpn": "LM7805", "customer_site_id": str(test_company.id)},
            )

        assert resp.status_code == 200
        assert b"<tr" in resp.content
        dno = db_session.query(ProactiveDoNotOffer).filter_by(mpn="LM7805").one()
        assert dno.company_id == test_company.id  # fallback field became the company linkage
        assert dno.created_by_id == test_user.id

    async def test_invalid_company_id_returns_400(self, ac):
        """Lines 669-670: non-integer company_id → 400."""
        resp = await ac.post(
            "/v2/partials/proactive/do-not-offer",
            data={"mpn": "LM317T", "company_id": "not-a-number"},
        )
        assert resp.status_code == 400

    async def test_missing_mpn_returns_400(self, ac):
        resp = await ac.post(
            "/v2/partials/proactive/do-not-offer",
            data={"mpn": "", "company_id": "1"},
        )
        assert resp.status_code == 400

    async def test_missing_company_id_returns_400(self, ac):
        resp = await ac.post(
            "/v2/partials/proactive/do-not-offer",
            data={"mpn": "LM317T"},
        )
        assert resp.status_code == 400

    async def test_unauthorized_company_returns_403(self, ac, db_session, test_company):
        """Lines 676-677: user doesn't own the company → 403."""
        test_company.account_owner_id = None
        db_session.commit()

        resp = await ac.post(
            "/v2/partials/proactive/do-not-offer",
            data={"mpn": "LM317T", "company_id": str(test_company.id)},
        )
        assert resp.status_code == 403

    async def test_nonexistent_company_returns_403(self, ac):
        resp = await ac.post(
            "/v2/partials/proactive/do-not-offer",
            data={"mpn": "LM317T", "company_id": "99999"},
        )
        assert resp.status_code == 403


# ──────────────────────────────────────────────────────────────────────────────
# DIRECT CALL TESTS
#
# These call async endpoint functions directly (bypassing ASGI dispatch) so that
# coverage.py traces every statement including post-await resumptions.  Using
# TestClient or AsyncClient routes through ASGI transport which creates a task/
# thread boundary that prevents coverage from recording coroutine-resume lines.
# Direct await in the same async test function avoids that boundary entirely.
# ──────────────────────────────────────────────────────────────────────────────


def _post_req(form_data: dict | list) -> _SR:
    """Minimal Starlette POST request with URL-encoded form body."""
    items = list(form_data.items()) if isinstance(form_data, dict) else form_data
    body = urlencode(items).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/test",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"content-length", str(len(body)).encode()),
        ],
    }
    buf = [body]

    async def receive():
        return {"type": "http.request", "body": buf.pop() if buf else b"", "more_body": False}

    return _SR(scope=scope, receive=receive)


def _get_req(query_string: str = "") -> _SR:
    """Minimal Starlette GET request with optional query string."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/test",
        "query_string": query_string.encode() if query_string else b"",
        "headers": [],
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return _SR(scope=scope, receive=receive)


# ── Direct: proactive_list_partial (lines 55-68) ──────────────────────────────


class TestDirectListPartial:
    """Direct calls to proactive_list_partial — covers lines 55-68."""

    async def test_matches_tab_builds_context(self, db_session, test_user):
        from app.routers.htmx.proactive import proactive_list_partial

        with (
            patch("app.services.proactive_service.get_matches_for_user", return_value=_EMPTY_MATCHES),
            patch("app.services.proactive_service.get_sent_offers", return_value=[]),
            patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()) as mock_tpl,
        ):
            resp = await proactive_list_partial(_get_req(), tab="matches", user=test_user, db=db_session)

        assert resp.status_code == 200
        _, ctx = mock_tpl.call_args[0]
        assert ctx["tab"] == "matches"
        assert ctx["match_count"] == 0

    async def test_sent_tab_calls_get_sent_offers(self, db_session, test_user):
        from app.routers.htmx.proactive import proactive_list_partial

        with (
            patch("app.services.proactive_service.get_matches_for_user", return_value=_EMPTY_MATCHES),
            patch("app.services.proactive_service.get_sent_offers", return_value=[{"id": 1}]) as mock_sent,
            patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()),
        ):
            await proactive_list_partial(_get_req(), tab="sent", user=test_user, db=db_session)

        mock_sent.assert_called_once()

    async def test_success_msg_from_query_string(self, db_session, test_user):
        from app.routers.htmx.proactive import proactive_list_partial

        with (
            patch("app.services.proactive_service.get_matches_for_user", return_value=_EMPTY_MATCHES),
            patch("app.services.proactive_service.get_sent_offers", return_value=[]),
            patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()) as mock_tpl,
        ):
            await proactive_list_partial(
                _get_req("success_msg=Offer+sent"), tab="matches", user=test_user, db=db_session
            )

        _, ctx = mock_tpl.call_args[0]
        assert ctx["success_msg"] == "Offer sent"


# ── Direct: proactive_batch_dismiss (lines 110-117) ───────────────────────────


class TestDirectBatchDismiss:
    """Direct calls to proactive_batch_dismiss — covers lines 110-117."""

    async def test_updates_db_when_match_ids_provided(
        self, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        from app.routers.htmx.proactive import proactive_batch_dismiss

        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)
        req = _post_req({"match_ids": str(match.id)})

        with (
            patch("app.services.proactive_service.get_matches_for_user", return_value=_EMPTY_MATCHES),
            patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()),
        ):
            resp = await proactive_batch_dismiss(req, user=test_user, db=db_session)

        assert resp.status_code == 200
        db_session.refresh(match)
        assert match.status == "dismissed"
        assert match.dismiss_reason == "batch_dismiss"

    async def test_multiple_ids_dismissed(
        self, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        from app.routers.htmx.proactive import proactive_batch_dismiss

        m1 = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)
        m2 = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)
        req = _post_req([("match_ids", str(m1.id)), ("match_ids", str(m2.id))])

        with (
            patch("app.services.proactive_service.get_matches_for_user", return_value=_EMPTY_MATCHES),
            patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()),
        ):
            await proactive_batch_dismiss(req, user=test_user, db=db_session)

        db_session.refresh(m1)
        db_session.refresh(m2)
        assert m1.status == "dismissed"
        assert m2.status == "dismissed"

    async def test_empty_match_ids_does_not_update(
        self, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        from app.routers.htmx.proactive import proactive_batch_dismiss

        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with (
            patch("app.services.proactive_service.get_matches_for_user", return_value=_EMPTY_MATCHES),
            patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()),
        ):
            resp = await proactive_batch_dismiss(_post_req({}), user=test_user, db=db_session)

        assert resp.status_code == 200
        db_session.refresh(match)
        assert match.status == "new"
        assert match.dismiss_reason is None


# ── Direct: proactive_prepare_page (lines 156-178, 203, 215) ─────────────────


class TestDirectPreparePage:
    """Direct calls to proactive_prepare_page — covers lines 156-178, 203, 215."""

    async def test_valid_match_renders_prepare_template(
        self, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        from app.routers.htmx.proactive import proactive_prepare_page

        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)
        req = _post_req({"match_ids": str(match.id)})

        with patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()) as mock_tpl:
            resp = await proactive_prepare_page(test_customer_site.id, req, user=test_user, db=db_session)

        assert resp.status_code == 200
        _, ctx = mock_tpl.call_args[0]
        assert ctx["site_id"] == test_customer_site.id
        assert len(ctx["matches"]) == 1
        assert ctx["matches"][0]["mpn"] == "LM317T"

    async def test_redirects_on_empty_match_ids(self, db_session, test_user, test_customer_site):
        from app.routers.htmx.proactive import proactive_prepare_page

        resp = await proactive_prepare_page(test_customer_site.id, _post_req({}), user=test_user, db=db_session)
        assert resp.status_code == 303

    async def test_redirects_when_match_not_owned(self, db_session, test_user, test_customer_site):
        from app.routers.htmx.proactive import proactive_prepare_page

        resp = await proactive_prepare_page(
            test_customer_site.id, _post_req({"match_ids": "99999"}), user=test_user, db=db_session
        )
        assert resp.status_code == 303

    async def test_contact_data_serialised_in_context(
        self, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        from app.routers.htmx.proactive import proactive_prepare_page

        contact = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Bob Smith",
            email="bob@acme.com",
            is_primary=True,
        )
        db_session.add(contact)
        db_session.flush()
        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()) as mock_tpl:
            await proactive_prepare_page(
                test_customer_site.id,
                _post_req({"match_ids": str(match.id)}),
                user=test_user,
                db=db_session,
            )

        _, ctx = mock_tpl.call_args[0]
        assert ctx["contacts"][0]["email"] == "bob@acme.com"
        assert ctx["contacts"][0]["has_email"] is True


# ── Direct: proactive_draft_for_prepare (lines 348-436) ──────────────────────


class TestDirectDraftForPrepare:
    """Direct calls to proactive_draft_for_prepare — covers lines 348-436."""

    async def test_ai_success_returns_script_block(
        self, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        from app.routers.htmx.proactive import proactive_draft_for_prepare

        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with patch(
            "app.services.proactive_email.draft_proactive_email",
            new_callable=AsyncMock,
            return_value={"subject": "Stock Alert", "body": "We have LM317T available."},
        ):
            resp = await proactive_draft_for_prepare(
                _post_req({"match_ids": str(match.id)}), user=test_user, db=db_session
            )

        assert resp.status_code == 200
        assert b"<script>" in resp.body
        assert b"Stock Alert" in resp.body

    async def test_ai_failure_returns_fallback_html(
        self, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        from app.routers.htmx.proactive import proactive_draft_for_prepare

        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with patch(
            "app.services.proactive_email.draft_proactive_email",
            new_callable=AsyncMock,
            side_effect=RuntimeError("AI down"),
        ):
            resp = await proactive_draft_for_prepare(
                _post_req({"match_ids": str(match.id)}), user=test_user, db=db_session
            )

        assert resp.status_code == 200
        body_lower = resp.body.lower()
        assert b"unavailable" in body_lower or b"manually" in body_lower

    async def test_ai_returns_none_gives_fallback(
        self, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        from app.routers.htmx.proactive import proactive_draft_for_prepare

        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with patch(
            "app.services.proactive_email.draft_proactive_email",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = await proactive_draft_for_prepare(
                _post_req({"match_ids": str(match.id)}), user=test_user, db=db_session
            )

        assert resp.status_code == 200
        assert b"unavailable" in resp.body.lower() or b"Auto-draft" in resp.body

    async def test_contact_id_resolves_first_name(
        self, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        from app.routers.htmx.proactive import proactive_draft_for_prepare

        contact = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Carol Chen",
            email="carol@acme.com",
            is_primary=True,
        )
        db_session.add(contact)
        db_session.flush()
        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with patch(
            "app.services.proactive_email.draft_proactive_email",
            new_callable=AsyncMock,
            return_value={"subject": "Hi Carol", "body": "Hello!"},
        ) as mock_ai:
            await proactive_draft_for_prepare(
                _post_req({"match_ids": str(match.id), "contact_ids": str(contact.id)}),
                user=test_user,
                db=db_session,
            )

        _, kwargs = mock_ai.call_args
        assert kwargs.get("contact_name") == "Carol"

    async def test_sell_price_field_parsed_into_parts(
        self, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        from app.routers.htmx.proactive import proactive_draft_for_prepare

        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with patch(
            "app.services.proactive_email.draft_proactive_email",
            new_callable=AsyncMock,
            return_value={"subject": "s", "body": "b"},
        ) as mock_ai:
            await proactive_draft_for_prepare(
                _post_req({"match_ids": str(match.id), f"sell_price_{match.id}": "1.99"}),
                user=test_user,
                db=db_session,
            )

        _, kwargs = mock_ai.call_args
        assert kwargs["parts"][0]["sell_price"] == pytest.approx(1.99)

    async def test_invalid_sell_price_silently_skipped(
        self, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        from app.routers.htmx.proactive import proactive_draft_for_prepare

        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with patch(
            "app.services.proactive_email.draft_proactive_email",
            new_callable=AsyncMock,
            return_value={"subject": "s", "body": "b"},
        ) as mock_ai:
            await proactive_draft_for_prepare(
                _post_req({"match_ids": str(match.id), f"sell_price_{match.id}": "bad"}),
                user=test_user,
                db=db_session,
            )

        _, kwargs = mock_ai.call_args
        # Falls back to cost*1.3, not 0 or "bad"
        assert kwargs["parts"][0]["sell_price"] >= 0

    async def test_no_match_ids_returns_error_html(self, db_session, test_user):
        from app.routers.htmx.proactive import proactive_draft_for_prepare

        resp = await proactive_draft_for_prepare(_post_req({}), user=test_user, db=db_session)
        assert b"No matches selected" in resp.body

    async def test_match_not_owned_returns_error_html(self, db_session, test_user):
        from app.routers.htmx.proactive import proactive_draft_for_prepare

        resp = await proactive_draft_for_prepare(_post_req({"match_ids": "99999"}), user=test_user, db=db_session)
        assert b"No valid matches" in resp.body


# ── Direct: proactive_send_offer (lines 456, 458, 460-528) ───────────────────


class TestDirectSendOffer:
    """Direct calls to proactive_send_offer — covers lines 456, 458, 460-528."""

    async def test_sell_price_fields_parsed_and_forwarded(
        self, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        from app.routers.htmx.proactive import proactive_send_offer

        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)
        req = _post_req(
            {
                "match_ids": str(match.id),
                "contact_ids": "1",
                f"sell_price_{match.id}": "3.50",
            }
        )

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch(
                "app.services.proactive_service.send_proactive_offer",
                new_callable=AsyncMock,
                return_value={"line_items": [], "recipient_emails": ["a@b.com"]},
            ) as mock_svc,
            patch("app.services.proactive_service.get_matches_for_user", return_value=_EMPTY_MATCHES),
            patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()),
        ):
            resp = await proactive_send_offer(req, user=test_user, db=db_session)

        assert resp.status_code == 200
        _, kwargs = mock_svc.call_args
        assert kwargs["sell_prices"].get(str(match.id)) == pytest.approx(3.50)

    async def test_body_text_converted_to_html(self, db_session, test_user):
        from app.routers.htmx.proactive import proactive_send_offer

        req = _post_req({"match_ids": "1", "contact_ids": "1", "body": "Hello!\nLine2."})

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch(
                "app.services.proactive_service.send_proactive_offer",
                new_callable=AsyncMock,
                return_value={"line_items": [], "recipient_emails": ["x@y.com"]},
            ) as mock_svc,
            patch("app.services.proactive_service.get_matches_for_user", return_value=_EMPTY_MATCHES),
            patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()),
        ):
            await proactive_send_offer(req, user=test_user, db=db_session)

        _, kwargs = mock_svc.call_args
        assert kwargs["email_html"] is not None
        assert "<br>" in kwargs["email_html"]

    async def test_empty_body_passes_none_html(self, db_session, test_user):
        from app.routers.htmx.proactive import proactive_send_offer

        req = _post_req({"match_ids": "1", "contact_ids": "1", "body": ""})

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch(
                "app.services.proactive_service.send_proactive_offer",
                new_callable=AsyncMock,
                return_value={"line_items": [], "recipient_emails": []},
            ) as mock_svc,
            patch("app.services.proactive_service.get_matches_for_user", return_value=_EMPTY_MATCHES),
            patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()),
        ):
            await proactive_send_offer(req, user=test_user, db=db_session)

        _, kwargs = mock_svc.call_args
        assert kwargs["email_html"] is None

    async def test_invalid_sell_price_silently_skipped(self, db_session, test_user):
        from app.routers.htmx.proactive import proactive_send_offer

        req = _post_req({"match_ids": "1", "contact_ids": "1", "sell_price_1": "not-a-number"})

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch(
                "app.services.proactive_service.send_proactive_offer",
                new_callable=AsyncMock,
                return_value={"line_items": [], "recipient_emails": []},
            ) as mock_svc,
            patch("app.services.proactive_service.get_matches_for_user", return_value=_EMPTY_MATCHES),
            patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()),
        ):
            await proactive_send_offer(req, user=test_user, db=db_session)

        _, kwargs = mock_svc.call_args
        assert kwargs["sell_prices"] == {}

    async def test_value_error_raises_http_400(self, db_session, test_user):
        from fastapi import HTTPException

        from app.routers.htmx.proactive import proactive_send_offer

        req = _post_req({"match_ids": "1", "contact_ids": "1"})

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch(
                "app.services.proactive_service.send_proactive_offer",
                new_callable=AsyncMock,
                side_effect=ValueError("No contacts found"),
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await proactive_send_offer(req, user=test_user, db=db_session)

        assert exc_info.value.status_code == 400

    async def test_runtime_error_raises_http_500(self, db_session, test_user):
        from fastapi import HTTPException

        from app.routers.htmx.proactive import proactive_send_offer

        req = _post_req({"match_ids": "1", "contact_ids": "1"})

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch(
                "app.services.proactive_service.send_proactive_offer",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Network failure"),
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await proactive_send_offer(req, user=test_user, db=db_session)

        assert exc_info.value.status_code == 500

    async def test_success_message_shows_parts_and_contacts(self, db_session, test_user):
        from app.routers.htmx.proactive import proactive_send_offer

        req = _post_req({"match_ids": "1", "contact_ids": "1", "subject": "Offer"})

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch(
                "app.services.proactive_service.send_proactive_offer",
                new_callable=AsyncMock,
                return_value={
                    "line_items": [{"mpn": "A"}, {"mpn": "B"}],
                    "recipient_emails": ["a@b.com", "c@d.com"],
                },
            ),
            patch("app.services.proactive_service.get_matches_for_user", return_value=_EMPTY_MATCHES),
            patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()) as mock_tpl,
        ):
            await proactive_send_offer(req, user=test_user, db=db_session)

        _, ctx = mock_tpl.call_args[0]
        assert "2 contact" in ctx["success_msg"] or "2 parts" in ctx["success_msg"]

    async def test_no_match_ids_raises_400(self, db_session, test_user):
        from fastapi import HTTPException

        from app.routers.htmx.proactive import proactive_send_offer

        with pytest.raises(HTTPException) as exc_info:
            await proactive_send_offer(_post_req({"contact_ids": "1"}), user=test_user, db=db_session)

        assert exc_info.value.status_code == 400

    async def test_no_contact_ids_raises_400(self, db_session, test_user):
        from fastapi import HTTPException

        from app.routers.htmx.proactive import proactive_send_offer

        with pytest.raises(HTTPException) as exc_info:
            await proactive_send_offer(_post_req({"match_ids": "1"}), user=test_user, db=db_session)

        assert exc_info.value.status_code == 400


# ── Direct: proactive_do_not_offer (lines 661-690) ───────────────────────────


class TestDirectDoNotOffer:
    """Direct calls to proactive_do_not_offer — covers lines 661-690."""

    async def test_creates_dno_record(self, db_session, test_user, test_company):
        from app.models.intelligence import ProactiveDoNotOffer
        from app.routers.htmx.proactive import proactive_do_not_offer

        test_company.account_owner_id = test_user.id
        db_session.commit()

        with patch("app.services.proactive_helpers.is_do_not_offer", return_value=False):
            resp = await proactive_do_not_offer(
                _post_req({"mpn": "LM317T", "company_id": str(test_company.id)}),
                user=test_user,
                db=db_session,
            )

        assert resp.status_code == 200
        dno = db_session.query(ProactiveDoNotOffer).filter_by(mpn="LM317T", company_id=test_company.id).first()
        assert dno is not None
        assert dno.created_by_id == test_user.id

    async def test_dedup_skips_second_insert(self, db_session, test_user, test_company):
        from app.models.intelligence import ProactiveDoNotOffer
        from app.routers.htmx.proactive import proactive_do_not_offer

        test_company.account_owner_id = test_user.id
        db_session.commit()

        with patch("app.services.proactive_helpers.is_do_not_offer", return_value=True):
            resp = await proactive_do_not_offer(
                _post_req({"mpn": "BC547", "company_id": str(test_company.id)}),
                user=test_user,
                db=db_session,
            )

        assert resp.status_code == 200
        assert db_session.query(ProactiveDoNotOffer).filter_by(mpn="BC547").first() is None

    async def test_customer_site_id_field_accepted(self, db_session, test_user, test_company):
        from app.models.intelligence import ProactiveDoNotOffer
        from app.routers.htmx.proactive import proactive_do_not_offer

        test_company.account_owner_id = test_user.id
        db_session.commit()

        with patch("app.services.proactive_helpers.is_do_not_offer", return_value=False):
            resp = await proactive_do_not_offer(
                _post_req({"mpn": "NE555", "customer_site_id": str(test_company.id)}),
                user=test_user,
                db=db_session,
            )

        assert resp.status_code == 200
        dno = db_session.query(ProactiveDoNotOffer).filter_by(mpn="NE555").one()
        assert dno.company_id == test_company.id  # customer_site_id fallback became the company linkage
        assert dno.created_by_id == test_user.id

    async def test_missing_mpn_raises_400(self, db_session, test_user):
        from fastapi import HTTPException

        from app.routers.htmx.proactive import proactive_do_not_offer

        with pytest.raises(HTTPException) as exc_info:
            await proactive_do_not_offer(_post_req({"mpn": "", "company_id": "1"}), user=test_user, db=db_session)

        assert exc_info.value.status_code == 400

    async def test_non_integer_company_id_raises_400(self, db_session, test_user):
        from fastapi import HTTPException

        from app.routers.htmx.proactive import proactive_do_not_offer

        with pytest.raises(HTTPException) as exc_info:
            await proactive_do_not_offer(
                _post_req({"mpn": "LM317T", "company_id": "abc"}), user=test_user, db=db_session
            )

        assert exc_info.value.status_code == 400

    async def test_unauthorized_company_raises_403(self, db_session, test_user, test_company):
        from fastapi import HTTPException

        from app.routers.htmx.proactive import proactive_do_not_offer

        test_company.account_owner_id = None
        db_session.commit()

        with pytest.raises(HTTPException) as exc_info:
            await proactive_do_not_offer(
                _post_req({"mpn": "LM317T", "company_id": str(test_company.id)}),
                user=test_user,
                db=db_session,
            )

        assert exc_info.value.status_code == 403

    async def test_nonexistent_company_raises_403(self, db_session, test_user):
        from fastapi import HTTPException

        from app.routers.htmx.proactive import proactive_do_not_offer

        with pytest.raises(HTTPException) as exc_info:
            await proactive_do_not_offer(
                _post_req({"mpn": "LM317T", "company_id": "99999"}), user=test_user, db=db_session
            )

        assert exc_info.value.status_code == 403
