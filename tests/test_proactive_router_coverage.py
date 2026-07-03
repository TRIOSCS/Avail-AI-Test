"""tests/test_proactive_router_coverage.py — Targeted coverage for app/routers/htmx/proactive.py.

Covers the uncovered HTMX endpoint lines:
  110-117  proactive_batch_dismiss with non-empty match_ids
  156-178  proactive_prepare_page valid matches + site path
  203,215  proactive_prepare_page context rendering (same flow)
  356-436  proactive_draft_for_prepare draft generation + exception fallback
  466-473  proactive_send_offer sell_price_ form-field parsing
  477-528  proactive_send_offer token+service flow + success re-render
  551-561  proactive_send_legacy marks match sent + returns partial
  602      proactive_convert ValueError not "already converted" → 403
  667-690  proactive_do_not_offer creates record, dedup, returns HTML

NOTE: httpx (Starlette's TestClient transport) interprets data=[tuple,...] as raw
bytes, not form data. All form posts use data={dict} so Starlette parses them
correctly as application/x-www-form-urlencoded.

Called by: pytest
Depends on: app/routers/htmx/proactive.py, tests/conftest.py
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.models import ProactiveMatch, SiteContact, User

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


_EMPTY_MATCHES = {"groups": [], "stats": {"total": 0}}


def _html_ok() -> HTMLResponse:
    return HTMLResponse("<div>ok</div>")


# ── Lines 110-117: batch dismiss with match_ids ───────────────────────


class TestBatchDismissWithMatchIds:
    """POST /v2/partials/proactive/batch-dismiss with non-empty match_ids."""

    def test_updates_status_to_dismissed(
        self, client, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with (
            patch("app.services.proactive_service.get_matches_for_user", return_value=_EMPTY_MATCHES),
            patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()),
        ):
            resp = client.post(
                "/v2/partials/proactive/batch-dismiss",
                data={"match_ids": str(match.id)},
            )

        assert resp.status_code == 200
        db_session.refresh(match)
        assert match.status == "dismissed"
        assert match.dismiss_reason == "batch_dismiss"

    def test_empty_match_ids_skips_update(self, client):
        """Empty match_ids list → skips the bulk-update block, still returns 200."""
        with (
            patch("app.services.proactive_service.get_matches_for_user", return_value=_EMPTY_MATCHES),
            patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()),
        ):
            resp = client.post("/v2/partials/proactive/batch-dismiss", data={})

        assert resp.status_code == 200


# ── Lines 156-178, 203, 215: prepare page happy path ─────────────────


class TestPreparePage:
    """POST /v2/proactive/prepare/{site_id} — valid match_ids."""

    def test_renders_prepare_template_for_valid_match(
        self, client, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()) as mock_tpl:
            resp = client.post(
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

    def test_prepare_with_contacts_includes_contact_data(
        self, client, db_session, test_user, test_requisition, test_offer, test_customer_site
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
            resp = client.post(
                f"/v2/proactive/prepare/{test_customer_site.id}",
                data={"match_ids": str(match.id)},
            )

        assert resp.status_code == 200
        _, ctx = mock_tpl.call_args[0]
        assert len(ctx["contacts"]) == 1
        assert ctx["contacts"][0]["full_name"] == "Jane Buyer"
        assert ctx["contacts"][0]["has_email"] is True


# ── Lines 356-436: draft generation ──────────────────────────────────


class TestDraftForPrepare:
    """POST /v2/partials/proactive/draft — match found, AI draft path."""

    def test_draft_success_injects_subject_and_body_via_script(
        self, client, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with patch(
            "app.services.proactive_email.draft_proactive_email",
            new_callable=AsyncMock,
            return_value={"subject": "Parts Available", "body": "Hello! We have LM317T."},
        ):
            resp = client.post(
                "/v2/partials/proactive/draft",
                data={"match_ids": str(match.id)},
            )

        assert resp.status_code == 200
        assert b"<script>" in resp.content
        assert b"Parts Available" in resp.content

    def test_draft_with_contact_id_passes_first_name(
        self, client, db_session, test_user, test_requisition, test_offer, test_customer_site
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
            resp = client.post(
                "/v2/partials/proactive/draft",
                # dict keys are unique so both values reach the form parser correctly
                data={"match_ids": str(match.id), "contact_ids": str(contact.id)},
            )

        assert resp.status_code == 200
        assert mock_draft.called
        _, kwargs = mock_draft.call_args
        assert kwargs.get("contact_name") == "Alice"

    def test_draft_with_sell_price_field_passes_value_to_service(
        self, client, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        """Lines 369-379: sell_price_<id> form field overrides the default markup."""
        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with patch(
            "app.services.proactive_email.draft_proactive_email",
            new_callable=AsyncMock,
            return_value={"subject": "Offer", "body": "Details inside."},
        ) as mock_draft:
            resp = client.post(
                "/v2/partials/proactive/draft",
                data={"match_ids": str(match.id), f"sell_price_{match.id}": "3.25"},
            )

        assert resp.status_code == 200
        assert mock_draft.called
        _, kwargs = mock_draft.call_args
        assert kwargs["parts"][0]["sell_price"] == pytest.approx(3.25)

    def test_draft_ai_exception_returns_fallback_html(
        self, client, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        """Lines 433-445: exception from AI service → fallback 'unavailable' partial."""
        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with patch(
            "app.services.proactive_email.draft_proactive_email",
            new_callable=AsyncMock,
            side_effect=RuntimeError("AI service down"),
        ):
            resp = client.post(
                "/v2/partials/proactive/draft",
                data={"match_ids": str(match.id)},
            )

        assert resp.status_code == 200
        body_lower = resp.content.lower()
        assert b"unavailable" in body_lower or b"manually" in body_lower

    def test_draft_no_match_ids_returns_error_html(self, client):
        resp = client.post("/v2/partials/proactive/draft", data={})
        assert resp.status_code == 200
        assert b"No matches selected" in resp.content

    def test_draft_match_not_in_db_returns_error_html(self, client):
        resp = client.post(
            "/v2/partials/proactive/draft",
            data={"match_ids": "99999"},
        )
        assert resp.status_code == 200
        assert b"No valid matches" in resp.content


# ── Lines 466-528: proactive_send_offer ──────────────────────────────


class TestSendOffer:
    """POST /v2/proactive/send — sell-price parsing and full service flow."""

    def test_sell_price_field_parsed_and_forwarded(
        self, client, db_session, test_user, test_requisition, test_offer, test_customer_site
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
            resp = client.post(
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

    def test_full_flow_renders_success_message(
        self, client, db_session, test_user, test_requisition, test_offer, test_customer_site
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
            resp = client.post(
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

    def test_no_match_ids_raises_400(self, client):
        resp = client.post("/v2/proactive/send", data={"contact_ids": "1"})
        assert resp.status_code == 400

    def test_no_contact_ids_raises_400(self, client):
        resp = client.post("/v2/proactive/send", data={"match_ids": "1"})
        assert resp.status_code == 400

    def test_value_error_from_service_raises_400(self, client):
        """Lines 524-525: ValueError from send_proactive_offer → 400."""
        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch(
                "app.services.proactive_service.send_proactive_offer",
                new_callable=AsyncMock,
                side_effect=ValueError("No contacts found"),
            ),
        ):
            resp = client.post(
                "/v2/proactive/send",
                data={"match_ids": "1", "contact_ids": "1"},
            )
        assert resp.status_code == 400

    def test_unexpected_exception_raises_500(self, client):
        """Lines 526-528: unexpected (non-ValueError) exception → 500."""
        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch(
                "app.services.proactive_service.send_proactive_offer",
                new_callable=AsyncMock,
                side_effect=RuntimeError("network failure"),
            ),
        ):
            resp = client.post(
                "/v2/proactive/send",
                data={"match_ids": "1", "contact_ids": "1"},
            )
        assert resp.status_code == 500


# ── Lines 551-561: proactive_send_legacy ─────────────────────────────


class TestSendLegacy:
    """POST /v2/partials/proactive/{match_id}/send — marks match sent."""

    def test_marks_match_sent_and_returns_partial(
        self, client, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with patch("app.routers.htmx.proactive.template_response", return_value=_html_ok()):
            resp = client.post(
                f"/v2/partials/proactive/{match.id}/send",
                data={"body": "Hello, we have LM317T available."},
            )

        assert resp.status_code == 200
        db_session.refresh(match)
        assert match.status == "sent"

    def test_empty_body_returns_400(
        self, client, db_session, test_user, test_requisition, test_offer, test_customer_site
    ):
        match = _make_match(db_session, test_user, test_requisition, test_offer, test_customer_site)
        resp = client.post(
            f"/v2/partials/proactive/{match.id}/send",
            data={"body": ""},
        )
        assert resp.status_code == 400

    def test_unknown_match_returns_404(self, client):
        resp = client.post(
            "/v2/partials/proactive/99999/send",
            data={"body": "Hello!"},
        )
        assert resp.status_code == 404


# ── Line 602: proactive_convert non-already-converted ValueError → 403 ─


class TestConvertValueError403:
    """POST /v2/partials/proactive/{offer_id}/convert — ValueError without 'already converted'."""

    def test_value_error_not_already_converted_returns_403(self, client, test_proactive_offer):
        with patch(
            "app.services.proactive_service.convert_proactive_to_win",
            side_effect=ValueError("Not authorized to convert"),
        ):
            resp = client.post(f"/v2/partials/proactive/{test_proactive_offer.id}/convert")

        assert resp.status_code == 403

    def test_already_converted_value_error_returns_409(self, client, test_proactive_offer):
        with patch(
            "app.services.proactive_service.convert_proactive_to_win",
            side_effect=ValueError("Offer already converted"),
        ):
            resp = client.post(f"/v2/partials/proactive/{test_proactive_offer.id}/convert")

        assert resp.status_code == 409

    def test_offer_not_found_returns_404(self, client):
        resp = client.post("/v2/partials/proactive/99999/convert")
        assert resp.status_code == 404


# ── Lines 667-690: proactive_do_not_offer ────────────────────────────


class TestDoNotOffer:
    """POST /v2/partials/proactive/do-not-offer — creates record, dedup, returns HTML."""

    def test_creates_record_and_returns_hidden_row(self, client, db_session, test_user, test_company):
        test_company.account_owner_id = test_user.id
        db_session.commit()

        with patch("app.services.proactive_helpers.is_do_not_offer", return_value=False):
            resp = client.post(
                "/v2/partials/proactive/do-not-offer",
                data={"mpn": "LM317T", "company_id": str(test_company.id)},
            )

        assert resp.status_code == 200
        assert b"<tr" in resp.content

    def test_dedup_when_already_suppressed_still_returns_html(self, client, db_session, test_user, test_company):
        test_company.account_owner_id = test_user.id
        db_session.commit()

        with patch("app.services.proactive_helpers.is_do_not_offer", return_value=True):
            resp = client.post(
                "/v2/partials/proactive/do-not-offer",
                data={"mpn": "BC547", "company_id": str(test_company.id)},
            )

        assert resp.status_code == 200
        assert b"<tr" in resp.content

    def test_accepts_customer_site_id_as_company_id_fallback(self, client, db_session, test_user, test_company):
        """customer_site_id form field is accepted when company_id is absent (line 662)."""
        test_company.account_owner_id = test_user.id
        db_session.commit()

        with patch("app.services.proactive_helpers.is_do_not_offer", return_value=False):
            resp = client.post(
                "/v2/partials/proactive/do-not-offer",
                data={"mpn": "LM7805", "customer_site_id": str(test_company.id)},
            )

        assert resp.status_code == 200

    def test_invalid_company_id_returns_400(self, client):
        """Lines 669-670: non-integer company_id → 400."""
        resp = client.post(
            "/v2/partials/proactive/do-not-offer",
            data={"mpn": "LM317T", "company_id": "not-a-number"},
        )
        assert resp.status_code == 400

    def test_missing_mpn_returns_400(self, client):
        resp = client.post(
            "/v2/partials/proactive/do-not-offer",
            data={"mpn": "", "company_id": "1"},
        )
        assert resp.status_code == 400

    def test_missing_company_id_returns_400(self, client):
        resp = client.post(
            "/v2/partials/proactive/do-not-offer",
            data={"mpn": "LM317T"},
        )
        assert resp.status_code == 400

    def test_unauthorized_company_returns_403(self, client, db_session, test_company):
        """Lines 676-677: user doesn't own the company → 403."""
        test_company.account_owner_id = None
        db_session.commit()

        resp = client.post(
            "/v2/partials/proactive/do-not-offer",
            data={"mpn": "LM317T", "company_id": str(test_company.id)},
        )
        assert resp.status_code == 403

    def test_nonexistent_company_returns_403(self, client):
        resp = client.post(
            "/v2/partials/proactive/do-not-offer",
            data={"mpn": "LM317T", "company_id": "99999"},
        )
        assert resp.status_code == 403
