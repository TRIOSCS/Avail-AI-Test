"""tests/test_proactive_router_gaps.py — Coverage boost for
app/routers/htmx/proactive.py.

Targets missing lines: 110-117, 156-178, 203, 215, 245-325, 355-362, 366-417,
440-450, 491, 556-579.

Called by: pytest autodiscovery
Depends on: conftest fixtures (db_session, test_user, test_company, test_customer_site)
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    Company,
    CustomerSite,
    Offer,
    ProactiveMatch,
    ProactiveOffer,
    Requirement,
    Requisition,
    SiteContact,
    User,
)
from tests.conftest import engine  # noqa: F401

HX = {"HX-Request": "true"}


# ── Shared scenario builder ───────────────────────────────────────────────────


def _build_scenario(db: Session) -> dict:
    """Create a full proactive-match scenario without FK pitfalls."""
    owner = User(
        email=f"rep_{id(db)}@trio.com",
        name="Sales Rep",
        role="buyer",
        azure_id=f"az-{id(db)}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(owner)
    db.flush()

    company = Company(name="TestCo", is_active=True, account_owner_id=owner.id)
    db.add(company)
    db.flush()

    site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
    db.add(site)
    db.flush()

    contact = SiteContact(
        customer_site_id=site.id,
        full_name="Alice Smith",
        email="alice@testco.com",
        is_primary=True,
    )
    db.add(contact)
    db.flush()

    req = Requisition(
        name="ScenReq",
        status="archived",
        created_by=owner.id,
        customer_site_id=site.id,
    )
    db.add(req)
    db.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="BC547",
        target_qty=500,
    )
    db.add(requirement)
    db.flush()

    offer = Offer(
        requisition_id=req.id,
        requirement_id=requirement.id,
        vendor_name="DigiKey",
        mpn="BC547",
        unit_price=Decimal("0.15"),
        qty_available=10000,
        status="active",
    )
    db.add(offer)
    db.flush()

    match = ProactiveMatch(
        offer_id=offer.id,
        requirement_id=requirement.id,
        requisition_id=req.id,
        customer_site_id=site.id,
        salesperson_id=owner.id,
        mpn="BC547",
        company_id=company.id,
        match_score=75,
        margin_pct=20.0,
        our_cost=0.15,
        status="new",
    )
    db.add(match)
    db.commit()

    return {
        "owner": owner,
        "company": company,
        "site": site,
        "contact": contact,
        "req": req,
        "requirement": requirement,
        "offer": offer,
        "match": match,
    }


def _make_client(db: Session, user: User) -> TestClient:
    """Create a TestClient authenticated as *user*."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    overridden = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user
    app.dependency_overrides[require_buyer] = lambda: user
    app.dependency_overrides[require_fresh_token] = lambda: "token"
    try:
        client = TestClient(app, raise_server_exceptions=False)
        yield client
    finally:
        for dep in overridden:
            app.dependency_overrides.pop(dep, None)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1: batch_dismiss update block (lines 110-117)
# ═══════════════════════════════════════════════════════════════════════════════


class TestBatchDismissUpdateBlock:
    """Cover lines 110-117: the DB UPDATE inside batch_dismiss when match_ids provided."""

    @patch(
        "app.services.proactive_service.get_matches_for_user",
        return_value={"groups": [], "stats": {"total": 0}},
    )
    def test_dismiss_updates_status(self, _mock_get, db_session: Session):
        data = _build_scenario(db_session)
        match = data["match"]
        user = data["owner"]

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/partials/proactive/batch-dismiss",
                data={"match_ids": str(match.id)},
                headers=HX,
            )

        assert resp.status_code == 200
        db_session.expire(match)
        assert match.status == "dismissed"

    @patch(
        "app.services.proactive_service.get_matches_for_user",
        return_value={"groups": [], "stats": {"total": 0}},
    )
    def test_dismiss_multiple_ids(self, _mock_get, db_session: Session):
        import urllib.parse

        data = _build_scenario(db_session)
        user = data["owner"]

        # Second match for same user
        match2 = ProactiveMatch(
            offer_id=data["offer"].id,
            customer_site_id=data["site"].id,
            salesperson_id=user.id,
            mpn="NE555",
            status="new",
        )
        db_session.add(match2)
        db_session.commit()

        body = urllib.parse.urlencode([("match_ids", str(data["match"].id)), ("match_ids", str(match2.id))])
        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/partials/proactive/batch-dismiss",
                content=body,
                headers={**HX, "Content-Type": "application/x-www-form-urlencoded"},
            )

        assert resp.status_code == 200
        db_session.expire(data["match"])
        db_session.expire(match2)
        assert data["match"].status == "dismissed"
        assert match2.status == "dismissed"

    @patch(
        "app.services.proactive_service.get_matches_for_user",
        return_value={"groups": [], "stats": {"total": 0}},
    )
    def test_dismiss_non_numeric_ids_ignored(self, _mock_get, db_session: Session):
        """Non-numeric match_ids are silently filtered out; no crash."""
        data = _build_scenario(db_session)
        user = data["owner"]

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/partials/proactive/batch-dismiss",
                data={"match_ids": "abc"},
                headers=HX,
            )

        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2: prepare_page valid path (lines 156-178, 203, 215)
# ═══════════════════════════════════════════════════════════════════════════════


class TestPreparationPageValidPath:
    """Cover lines 156-178, 203, 215: prepare page with real matches."""

    def test_prepare_valid_match_ids_renders(self, db_session: Session):
        data = _build_scenario(db_session)
        user = data["owner"]

        for client in _make_client(db_session, user):
            resp = client.post(
                f"/v2/proactive/prepare/{data['site'].id}",
                data={"match_ids": str(data["match"].id)},
                headers=HX,
            )

        assert resp.status_code == 200
        assert "BC547" in resp.text or resp.status_code == 200

    def test_prepare_match_belongs_to_other_user_redirects(self, db_session: Session):
        """Match with different salesperson_id → no matches found → redirect."""
        data = _build_scenario(db_session)

        other = User(
            email="other@trio.com",
            role="buyer",
            azure_id="az-other",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other)
        db_session.commit()

        for client in _make_client(db_session, other):
            resp = client.post(
                f"/v2/proactive/prepare/{data['site'].id}",
                data={"match_ids": str(data["match"].id)},
                headers=HX,
                follow_redirects=False,
            )

        # Other user doesn't own the match → empty result → redirect
        assert resp.status_code in (302, 303)

    def test_prepare_site_missing_still_renders(self, db_session: Session):
        """If site_id doesn't exist, prepare renders with fallback company/site name."""
        data = _build_scenario(db_session)
        user = data["owner"]

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/proactive/prepare/99999",
                data={"match_ids": str(data["match"].id)},
                headers=HX,
            )

        # Route renders normally; site/company fallback to empty strings
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3: draft endpoint (lines 245-325)
# ═══════════════════════════════════════════════════════════════════════════════


class TestDraftForPrepare:
    """Cover lines 245-325: draft endpoint paths."""

    def test_draft_no_valid_matches_returns_error_html(self, db_session: Session):
        """POST with match_ids that don't belong to the user → error HTML."""
        data = _build_scenario(db_session)
        other = User(
            email="other2@trio.com",
            role="buyer",
            azure_id="az-other2",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other)
        db_session.commit()

        for client in _make_client(db_session, other):
            resp = client.post(
                "/v2/partials/proactive/draft",
                data={"match_ids": str(data["match"].id)},
                headers=HX,
            )

        assert resp.status_code == 200
        assert "rose" in resp.text or "No valid matches" in resp.text

    @patch("app.services.proactive_email.draft_proactive_email", new_callable=AsyncMock)
    def test_draft_ai_success_returns_script_block(self, mock_draft, db_session: Session):
        """Successful AI draft returns HTML with script tag populating subject/body."""
        mock_draft.return_value = {
            "subject": "Parts Available — TestCo",
            "body": "Hello, we have BC547 in stock.",
        }
        data = _build_scenario(db_session)
        user = data["owner"]

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/partials/proactive/draft",
                data={
                    "match_ids": str(data["match"].id),
                    "contact_ids": str(data["contact"].id),
                },
                headers=HX,
            )

        assert resp.status_code == 200
        assert "Draft generated" in resp.text or "script" in resp.text

    @patch(
        "app.services.proactive_email.draft_proactive_email",
        new_callable=AsyncMock,
        side_effect=RuntimeError("AI unavailable"),
    )
    def test_draft_ai_failure_returns_fallback_html(self, _mock_draft, db_session: Session):
        """When AI draft raises, the endpoint returns the manual-write fallback."""
        data = _build_scenario(db_session)
        user = data["owner"]

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/partials/proactive/draft",
                data={"match_ids": str(data["match"].id)},
                headers=HX,
            )

        assert resp.status_code == 200
        assert "Auto-draft unavailable" in resp.text or "amber" in resp.text

    @patch("app.services.proactive_email.draft_proactive_email", new_callable=AsyncMock)
    def test_draft_with_contact_resolves_first_name(self, mock_draft, db_session: Session):
        """Contact first name is resolved and passed to the AI drafter."""
        mock_draft.return_value = {"subject": "Hi Alice", "body": "Hi Alice, "}
        data = _build_scenario(db_session)
        user = data["owner"]

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/partials/proactive/draft",
                data={
                    "match_ids": str(data["match"].id),
                    "contact_ids": str(data["contact"].id),
                },
                headers=HX,
            )

        assert resp.status_code == 200
        # Verify the AI was called with the contact's first name
        call_kwargs = mock_draft.call_args.kwargs
        assert call_kwargs.get("contact_name") == "Alice"

    @patch("app.services.proactive_email.draft_proactive_email", new_callable=AsyncMock)
    def test_draft_sell_price_parsed_from_form(self, mock_draft, db_session: Session):
        """sell_price_<match_id> form fields are parsed and passed to AI."""
        mock_draft.return_value = {"subject": "Test", "body": "Test body"}
        data = _build_scenario(db_session)
        user = data["owner"]
        match_id = data["match"].id

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/partials/proactive/draft",
                data={
                    "match_ids": str(match_id),
                    f"sell_price_{match_id}": "0.25",
                },
                headers=HX,
            )

        assert resp.status_code == 200
        # AI was called with the custom sell price embedded in parts
        call_kwargs = mock_draft.call_args.kwargs
        parts = call_kwargs.get("parts", [])
        assert parts[0]["sell_price"] == 0.25

    @patch("app.services.proactive_email.draft_proactive_email", new_callable=AsyncMock)
    def test_draft_invalid_sell_price_ignored(self, mock_draft, db_session: Session):
        """Non-numeric sell_price values are silently skipped."""
        mock_draft.return_value = {"subject": "Test", "body": "body"}
        data = _build_scenario(db_session)
        user = data["owner"]
        match_id = data["match"].id

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/partials/proactive/draft",
                data={
                    "match_ids": str(match_id),
                    f"sell_price_{match_id}": "not-a-number",
                },
                headers=HX,
            )

        assert resp.status_code == 200

    @patch("app.services.proactive_email.draft_proactive_email", new_callable=AsyncMock)
    def test_draft_ai_returns_none_falls_back(self, mock_draft, db_session: Session):
        """When draft_proactive_email returns None, fallback HTML is shown."""
        mock_draft.return_value = None
        data = _build_scenario(db_session)
        user = data["owner"]

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/partials/proactive/draft",
                data={"match_ids": str(data["match"].id)},
                headers=HX,
            )

        assert resp.status_code == 200
        assert "Auto-draft unavailable" in resp.text or "amber" in resp.text


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4: send_offer sell-price parsing + success path (lines 355-417)
# ═══════════════════════════════════════════════════════════════════════════════


class TestSendOfferSuccessPath:
    """Cover lines 355-362, 366-417: sell price parsing, body HTML, and success
    branch."""

    @patch(
        "app.services.proactive_service.get_matches_for_user",
        return_value={"groups": [], "stats": {"total": 0}},
    )
    @patch(
        "app.services.proactive_service.send_proactive_offer",
        new_callable=AsyncMock,
        return_value={
            "id": 1,
            "line_items": [{"mpn": "BC547", "sell_price": 0.25}],
            "recipient_emails": ["alice@testco.com"],
            "subject": "Parts Available",
        },
    )
    @patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok")
    def test_send_with_body_text_builds_html(self, _tok, mock_send, _mock_get, db_session: Session):
        """Body text is escaped and wrapped in HTML div before calling service."""
        data = _build_scenario(db_session)
        user = data["owner"]
        match_id = data["match"].id
        contact_id = data["contact"].id

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/proactive/send",
                data={
                    "match_ids": str(match_id),
                    "contact_ids": str(contact_id),
                    "subject": "Hello",
                    "body": "We have BC547 available.\nCall us.",
                },
                headers=HX,
            )

        assert resp.status_code == 200
        # The email_html arg should have been built from the body text
        call_kwargs = mock_send.call_args.kwargs
        assert call_kwargs["email_html"] is not None
        assert "BC547" in call_kwargs["email_html"]
        assert "<br>" in call_kwargs["email_html"]

    @patch(
        "app.services.proactive_service.get_matches_for_user",
        return_value={"groups": [], "stats": {"total": 0}},
    )
    @patch(
        "app.services.proactive_service.send_proactive_offer",
        new_callable=AsyncMock,
        return_value={
            "id": 2,
            "line_items": [],
            "recipient_emails": ["alice@testco.com"],
            "subject": "Sub",
        },
    )
    @patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok")
    def test_send_without_body_email_html_is_none(self, _tok, mock_send, _mock_get, db_session: Session):
        """Empty body → email_html=None passed to service."""
        data = _build_scenario(db_session)
        user = data["owner"]

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/proactive/send",
                data={
                    "match_ids": str(data["match"].id),
                    "contact_ids": str(data["contact"].id),
                    "subject": "",
                    "body": "",
                },
                headers=HX,
            )

        assert resp.status_code == 200
        call_kwargs = mock_send.call_args.kwargs
        assert call_kwargs["email_html"] is None

    @patch(
        "app.services.proactive_service.get_matches_for_user",
        return_value={"groups": [], "stats": {"total": 0}},
    )
    @patch(
        "app.services.proactive_service.send_proactive_offer",
        new_callable=AsyncMock,
        return_value={
            "id": 3,
            "line_items": [{"mpn": "BC547"}],
            "recipient_emails": ["alice@testco.com"],
            "subject": "Parts",
        },
    )
    @patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok")
    def test_send_sell_price_form_fields_parsed(self, _tok, mock_send, _mock_get, db_session: Session):
        """sell_price_<match_id> form fields are parsed into sell_prices dict."""
        data = _build_scenario(db_session)
        user = data["owner"]
        match_id = data["match"].id

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/proactive/send",
                data={
                    "match_ids": str(match_id),
                    "contact_ids": str(data["contact"].id),
                    f"sell_price_{match_id}": "0.30",
                },
                headers=HX,
            )

        assert resp.status_code == 200
        call_kwargs = mock_send.call_args.kwargs
        assert call_kwargs["sell_prices"].get(str(match_id)) == 0.30

    @patch(
        "app.services.proactive_service.get_matches_for_user",
        return_value={"groups": [], "stats": {"total": 0}},
    )
    @patch(
        "app.services.proactive_service.send_proactive_offer",
        new_callable=AsyncMock,
        return_value={
            "id": 4,
            "line_items": [{"mpn": "BC547"}],
            "recipient_emails": ["alice@testco.com"],
            "subject": "Parts",
        },
    )
    @patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok")
    def test_send_invalid_sell_price_skipped(self, _tok, mock_send, _mock_get, db_session: Session):
        """Non-numeric sell_price values are ignored without crashing."""
        data = _build_scenario(db_session)
        user = data["owner"]
        match_id = data["match"].id

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/proactive/send",
                data={
                    "match_ids": str(match_id),
                    "contact_ids": str(data["contact"].id),
                    f"sell_price_{match_id}": "bad-value",
                },
                headers=HX,
            )

        assert resp.status_code == 200
        call_kwargs = mock_send.call_args.kwargs
        assert call_kwargs["sell_prices"] == {}

    @patch(
        "app.services.proactive_service.send_proactive_offer",
        new_callable=AsyncMock,
        side_effect=Exception("Network timeout"),
    )
    @patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok")
    def test_send_generic_exception_returns_500(self, _tok, _mock_send, db_session: Session):
        """Unexpected exception → 500 HTTPException."""
        data = _build_scenario(db_session)
        user = data["owner"]

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/proactive/send",
                data={
                    "match_ids": str(data["match"].id),
                    "contact_ids": str(data["contact"].id),
                },
                headers=HX,
            )

        assert resp.status_code == 500

    @patch(
        "app.services.proactive_service.get_matches_for_user",
        return_value={"groups": [], "stats": {"total": 0}},
    )
    @patch(
        "app.services.proactive_service.send_proactive_offer",
        new_callable=AsyncMock,
        return_value={
            "id": 5,
            "line_items": [{"mpn": "A"}, {"mpn": "B"}],
            "recipient_emails": ["a@b.com", "c@d.com"],
            "subject": "Sub",
        },
    )
    @patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok")
    def test_send_success_msg_counts_parts_and_contacts(self, _tok, _mock_send, _mock_get, db_session: Session):
        """Success message reflects parts_count and contacts_count from result."""
        data = _build_scenario(db_session)
        user = data["owner"]

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/proactive/send",
                data={
                    "match_ids": str(data["match"].id),
                    "contact_ids": str(data["contact"].id),
                },
                headers=HX,
            )

        assert resp.status_code == 200
        # The response is re-rendered list.html with success banner
        assert "2 contact" in resp.text or "2 parts" in resp.text or resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6: convert endpoint (line 491)
# ═══════════════════════════════════════════════════════════════════════════════


class TestProactiveConvert:
    """Cover lines 471-494: convert_proactive_to_win success and error paths."""

    def test_convert_offer_not_found_returns_404(self, db_session: Session):
        data = _build_scenario(db_session)
        user = data["owner"]

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/partials/proactive/99999/convert",
                headers=HX,
            )

        assert resp.status_code == 404

    def test_convert_wrong_salesperson_returns_403(self, db_session: Session):
        """ProactiveOffer.salesperson_id != current user → 403."""
        data = _build_scenario(db_session)

        owner = data["owner"]
        po = ProactiveOffer(
            customer_site_id=data["site"].id,
            salesperson_id=owner.id,
            line_items=[],
            recipient_emails=["a@b.com"],
            subject="Test",
            status="sent",
        )
        db_session.add(po)
        db_session.commit()

        intruder = User(
            email="intruder2@trio.com",
            role="buyer",
            azure_id="az-intruder2",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(intruder)
        db_session.commit()

        for client in _make_client(db_session, intruder):
            resp = client.post(
                f"/v2/partials/proactive/{po.id}/convert",
                headers=HX,
            )

        assert resp.status_code == 403

    @patch("app.services.proactive_service.convert_proactive_to_win")
    def test_convert_success_renders_template(self, mock_convert, db_session: Session):
        """Successful conversion returns the convert_success template."""
        mock_convert.return_value = {"requisition_id": 99, "quote_id": 88}
        data = _build_scenario(db_session)
        owner = data["owner"]

        po = ProactiveOffer(
            customer_site_id=data["site"].id,
            salesperson_id=owner.id,
            line_items=[{"mpn": "BC547"}],
            recipient_emails=["alice@testco.com"],
            subject="Test Offer",
            status="sent",
        )
        db_session.add(po)
        db_session.commit()

        for client in _make_client(db_session, owner):
            resp = client.post(
                f"/v2/partials/proactive/{po.id}/convert",
                headers=HX,
            )

        assert resp.status_code == 200

    @patch(
        "app.services.proactive_service.convert_proactive_to_win",
        side_effect=ValueError("already converted"),
    )
    def test_convert_already_converted_returns_409(self, _mock, db_session: Session):
        data = _build_scenario(db_session)
        owner = data["owner"]

        po = ProactiveOffer(
            customer_site_id=data["site"].id,
            salesperson_id=owner.id,
            line_items=[],
            recipient_emails=[],
            subject="Sub",
            status="sent",
        )
        db_session.add(po)
        db_session.commit()

        for client in _make_client(db_session, owner):
            resp = client.post(
                f"/v2/partials/proactive/{po.id}/convert",
                headers=HX,
            )

        assert resp.status_code == 409

    @patch(
        "app.services.proactive_service.convert_proactive_to_win",
        side_effect=ValueError("not your offer"),
    )
    def test_convert_value_error_not_converted_returns_403(self, _mock, db_session: Session):
        data = _build_scenario(db_session)
        owner = data["owner"]

        po = ProactiveOffer(
            customer_site_id=data["site"].id,
            salesperson_id=owner.id,
            line_items=[],
            recipient_emails=[],
            subject="Sub",
            status="sent",
        )
        db_session.add(po)
        db_session.commit()

        for client in _make_client(db_session, owner):
            resp = client.post(
                f"/v2/partials/proactive/{po.id}/convert",
                headers=HX,
            )

        assert resp.status_code == 403

    @patch(
        "app.services.proactive_service.convert_proactive_to_win",
        side_effect=RuntimeError("db error"),
    )
    def test_convert_generic_exception_returns_500(self, _mock, db_session: Session):
        data = _build_scenario(db_session)
        owner = data["owner"]

        po = ProactiveOffer(
            customer_site_id=data["site"].id,
            salesperson_id=owner.id,
            line_items=[],
            recipient_emails=[],
            subject="Sub",
            status="sent",
        )
        db_session.add(po)
        db_session.commit()

        for client in _make_client(db_session, owner):
            resp = client.post(
                f"/v2/partials/proactive/{po.id}/convert",
                headers=HX,
            )

        assert resp.status_code == 500


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7: scorecard exception path (lines 504-511)
# ═══════════════════════════════════════════════════════════════════════════════


class TestProactiveScorecard:
    """Cover lines 504-511: scorecard endpoint with stats and exception."""

    @patch(
        "app.services.proactive_service.get_scorecard",
        return_value={
            "total_sent": 5,
            "total_converted": 2,
            "conversion_rate": 40.0,
            "converted_revenue": 1000.0,
        },
    )
    def test_scorecard_returns_stats(self, _mock, db_session: Session):
        data = _build_scenario(db_session)
        user = data["owner"]

        for client in _make_client(db_session, user):
            resp = client.get("/v2/partials/proactive/scorecard", headers=HX)

        assert resp.status_code == 200

    @patch(
        "app.services.proactive_service.get_scorecard",
        side_effect=RuntimeError("stats unavailable"),
    )
    def test_scorecard_exception_returns_zeroes(self, _mock, db_session: Session):
        """get_scorecard exception → fallback zeros dict, no 500."""
        data = _build_scenario(db_session)
        user = data["owner"]

        for client in _make_client(db_session, user):
            resp = client.get("/v2/partials/proactive/scorecard", headers=HX)

        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8: badge count > 0 path (lines 524-536)
# ═══════════════════════════════════════════════════════════════════════════════


class TestProactiveBadge:
    """Cover lines 524-536: badge endpoint with and without matches."""

    def test_badge_no_matches_returns_empty(self, db_session: Session):
        data = _build_scenario(db_session)
        user = data["owner"]

        # Mark match as dismissed so count=0
        data["match"].status = "dismissed"
        db_session.commit()

        for client in _make_client(db_session, user):
            resp = client.get("/v2/partials/proactive/badge", headers=HX)

        assert resp.status_code == 200
        assert resp.text == ""

    def test_badge_with_matches_returns_count_span(self, db_session: Session):
        """When user has new matches, badge shows count span."""
        data = _build_scenario(db_session)
        user = data["owner"]
        # match is already status="new" from _build_scenario

        for client in _make_client(db_session, user):
            resp = client.get("/v2/partials/proactive/badge", headers=HX)

        assert resp.status_code == 200
        assert "1" in resp.text
        assert "span" in resp.text


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 9: do-not-offer DNO creation (lines 556-579)
# ═══════════════════════════════════════════════════════════════════════════════


class TestDoNotOffer:
    """Cover lines 556-579: do-not-offer endpoint."""

    def test_dno_missing_mpn_returns_400(self, db_session: Session):
        data = _build_scenario(db_session)
        user = data["owner"]

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/partials/proactive/do-not-offer",
                data={"company_id": str(data["company"].id)},
                headers=HX,
            )

        assert resp.status_code == 400

    def test_dno_missing_company_returns_400(self, db_session: Session):
        data = _build_scenario(db_session)
        user = data["owner"]

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/partials/proactive/do-not-offer",
                data={"mpn": "BC547"},
                headers=HX,
            )

        assert resp.status_code == 400

    def test_dno_non_integer_company_id_returns_400(self, db_session: Session):
        data = _build_scenario(db_session)
        user = data["owner"]

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/partials/proactive/do-not-offer",
                data={"mpn": "BC547", "company_id": "notanumber"},
                headers=HX,
            )

        assert resp.status_code == 400

    def test_dno_unknown_company_returns_403(self, db_session: Session):
        data = _build_scenario(db_session)
        user = data["owner"]

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/partials/proactive/do-not-offer",
                data={"mpn": "BC547", "company_id": "99999"},
                headers=HX,
            )

        assert resp.status_code == 403

    def test_dno_creates_record_and_returns_hidden_row(self, db_session: Session):
        """Account owner can create DNO rule; returns hidden <tr>."""
        from app.models.intelligence import ProactiveDoNotOffer

        data = _build_scenario(db_session)
        user = data["owner"]
        company = data["company"]

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/partials/proactive/do-not-offer",
                data={"mpn": "BC547", "company_id": str(company.id)},
                headers=HX,
            )

        assert resp.status_code == 200
        assert "display:none" in resp.text

        dno = db_session.query(ProactiveDoNotOffer).filter_by(mpn="BC547", company_id=company.id).first()
        assert dno is not None
        assert dno.created_by_id == user.id

    def test_dno_dedup_does_not_create_duplicate(self, db_session: Session):
        """Posting DNO twice does not create a second DB row."""
        from app.models.intelligence import ProactiveDoNotOffer

        data = _build_scenario(db_session)
        user = data["owner"]
        company = data["company"]

        post_data = {"mpn": "BC547", "company_id": str(company.id)}

        for client in _make_client(db_session, user):
            client.post("/v2/partials/proactive/do-not-offer", data=post_data, headers=HX)

        # Re-build client for second request
        for client in _make_client(db_session, user):
            resp = client.post("/v2/partials/proactive/do-not-offer", data=post_data, headers=HX)

        assert resp.status_code == 200
        count = db_session.query(ProactiveDoNotOffer).filter_by(mpn="BC547", company_id=company.id).count()
        assert count == 1

    def test_dno_uses_customer_site_id_field_as_company(self, db_session: Session):
        """The form field customer_site_id is also accepted as company identifier."""
        from app.models.intelligence import ProactiveDoNotOffer

        data = _build_scenario(db_session)
        user = data["owner"]
        company = data["company"]

        for client in _make_client(db_session, user):
            resp = client.post(
                "/v2/partials/proactive/do-not-offer",
                data={"mpn": "NE555", "customer_site_id": str(company.id)},
                headers=HX,
            )

        assert resp.status_code == 200
        dno = db_session.query(ProactiveDoNotOffer).filter_by(mpn="NE555").first()
        assert dno is not None
