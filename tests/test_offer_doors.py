"""tests/test_offer_doors.py — W3 §5.1 "TWO offer doors" regression pins.

The spec collapses offer intake to exactly two doors: the Responses tab (with a
flagged-AI-offers filter replacing the standalone review-queue page) and the ONE
Add-offer modal (manual fields + optional AI paste box). Pins here:

  - the deleted doors stay deleted (route registry + live 404s): the two AI paste
    modals (parse-email / paste-offer), the /v2/offers/review-queue page + partial,
    and the queue-only promote/reject endpoints;
  - the surviving door's paste box: keys-off honesty (spec §7 — "AI is off", never
    a 500), parse preview → save-parsed-offers wiring, and requisition-access IDOR;
  - the flagged-AI filter inside the Responses tab renders pending_review offers
    with approve/reject actions that stay in the Responses context.

Called by: pytest.
Depends on: app.main.app (route registry), tests._route_helpers, conftest fixtures
    (client, db_session, test_user, admin_user, test_requisition).
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import OfferStatus, UserRole
from app.main import app
from app.models import Offer, Requisition, User
from tests._route_helpers import iter_routes


def _paths() -> set[str]:
    """Set of registered route paths."""
    return {getattr(route, "path", None) for route in iter_routes(app.routes)}


# ── Deleted doors stay deleted (route registry) ─────────────────────────────


DELETED_PATHS = [
    # The two AI paste modals (spec §5.1)
    "/v2/partials/requisitions/{req_id}/parse-email-form",
    "/v2/partials/requisitions/{req_id}/paste-offer-form",
    "/v2/partials/requisitions/{req_id}/parse-email",
    "/v2/partials/requisitions/{req_id}/parse-offer",
    # The standalone review-queue page + partial and its queue-only twins
    "/v2/offers/review-queue",
    "/v2/partials/offers/review-queue",
    "/v2/partials/offers/{offer_id}/promote",
    "/v2/partials/offers/{offer_id}/reject",
]


@pytest.mark.parametrize("path", DELETED_PATHS)
def test_deleted_door_route_removed(path: str):
    assert path not in _paths()


def test_surviving_door_routes_still_present():
    """The two doors' routes must remain registered: the Add-offer modal chain (form →
    paste parse → preview save → create) and the Responses-tab review."""
    all_paths = _paths()
    assert "/v2/partials/requisitions/{req_id}/add-offer-form" in all_paths
    assert "/v2/partials/requisitions/{req_id}/add-offer" in all_paths
    assert "/v2/partials/requisitions/{req_id}/add-offer/parse" in all_paths
    assert "/v2/partials/requisitions/{req_id}/save-parsed-offers" in all_paths
    assert "/v2/partials/requisitions/{req_id}/offers/{offer_id}/review" in all_paths
    assert "/v2/partials/requisitions/{req_id}/tab/{tab}" in all_paths


# ── Deleted doors stay deleted (live requests 404) ──────────────────────────


class TestDeletedDoors404:
    def test_paste_modal_urls_404(self, client: TestClient, test_requisition: Requisition):
        rid = test_requisition.id
        assert client.get(f"/v2/partials/requisitions/{rid}/parse-email-form").status_code == 404
        assert client.get(f"/v2/partials/requisitions/{rid}/paste-offer-form").status_code == 404
        assert client.post(f"/v2/partials/requisitions/{rid}/parse-email", data={"email_body": "x"}).status_code == 404
        assert client.post(f"/v2/partials/requisitions/{rid}/parse-offer", data={"raw_text": "x"}).status_code == 404

    def test_review_queue_urls_404(self, client: TestClient):
        assert client.get("/v2/partials/offers/review-queue").status_code == 404
        assert client.get("/v2/offers/review-queue").status_code == 404

    def test_promote_reject_urls_404(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        offer = _pending_offer(db_session, test_requisition)
        assert client.post(f"/v2/partials/offers/{offer.id}/promote").status_code == 404
        assert client.post(f"/v2/partials/offers/{offer.id}/reject").status_code == 404

    def test_offers_tab_has_single_add_offer_door(self, client: TestClient, test_requisition: Requisition):
        """The offers tab launches ONE Add-offer modal — no Parse Email / Paste
        Offer."""
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/offers")
        assert resp.status_code == 200
        assert f"/v2/partials/requisitions/{test_requisition.id}/add-offer-form" in resp.text
        assert "Parse Email" not in resp.text
        assert "Paste Offer" not in resp.text
        assert "parse-email-form" not in resp.text
        assert "paste-offer-form" not in resp.text

    def test_sightings_workspace_has_no_review_queue_link(self, client: TestClient):
        resp = client.get("/v2/partials/sightings/workspace")
        assert resp.status_code == 200
        assert "review-queue" not in resp.text
        assert "/v2/follow-ups" in resp.text  # the surviving quick-link


# ── The Add-offer modal's optional paste box ────────────────────────────────


class TestAddOfferPasteBox:
    def test_form_shows_paste_box_when_ai_on(self, client: TestClient, test_requisition: Requisition):
        with patch("app.routers.htmx.offers.crud.claude_configured", return_value=True):
            resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/add-offer-form")
        assert resp.status_code == 200
        assert f"/v2/partials/requisitions/{test_requisition.id}/add-offer/parse" in resp.text
        assert 'name="raw_text"' in resp.text
        assert "Parse with AI" in resp.text
        # Manual fields keep working alongside the paste box
        assert 'name="vendor_name"' in resp.text
        assert 'name="mpn"' in resp.text

    def test_form_honest_when_ai_off(self, client: TestClient, test_requisition: Requisition):
        """Keys-off honesty (spec §7): no textarea, an honest note, manual fields
        intact."""
        with patch("app.routers.htmx.offers.crud.claude_configured", return_value=False):
            resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/add-offer-form")
        assert resp.status_code == 200
        assert "AI is off" in resp.text
        assert 'name="raw_text"' not in resp.text
        assert 'name="vendor_name"' in resp.text  # manual entry still works

    def test_parse_honest_when_ai_off(self, client: TestClient, test_requisition: Requisition):
        """POSTing the parse route with no AI key returns the banner — never a 500."""
        with patch("app.routers.htmx.offers.crud.claude_configured", return_value=False):
            resp = client.post(
                f"/v2/partials/requisitions/{test_requisition.id}/add-offer/parse",
                data={"raw_text": "LM317T 100pcs $0.50"},
            )
        assert resp.status_code == 200
        assert "AI is off" in resp.text

    def test_parse_empty_text_warns(self, client: TestClient, test_requisition: Requisition):
        with patch("app.routers.htmx.offers.crud.claude_configured", return_value=True):
            resp = client.post(
                f"/v2/partials/requisitions/{test_requisition.id}/add-offer/parse",
                data={"raw_text": "   "},
            )
        assert resp.status_code == 200
        assert "paste vendor text" in resp.text.lower()

    def test_parse_renders_editable_preview_wired_to_save(self, client: TestClient, test_requisition: Requisition):
        """Parsed rows render as editable cards whose Save posts to the surviving save-
        parsed-offers route (→ offer_service.create_offer)."""
        mock_result = {"offers": [{"mpn": "LM317T", "qty_available": 100, "unit_price": 0.5, "vendor_name": "Arrow"}]}
        with (
            patch("app.routers.htmx.offers.crud.claude_configured", return_value=True),
            patch(
                "app.services.freeform_parser_service.parse_freeform_offer",
                new_callable=AsyncMock,
                return_value=mock_result,
            ),
        ):
            resp = client.post(
                f"/v2/partials/requisitions/{test_requisition.id}/add-offer/parse",
                data={"raw_text": "LM317T 100pcs $0.50"},
            )
        assert resp.status_code == 200
        assert "Parsed 1 offer from pasted text" in resp.text
        assert 'value="LM317T"' in resp.text  # editable card prefilled
        assert f'hx-post="/v2/partials/requisitions/{test_requisition.id}/save-parsed-offers"' in resp.text

    def test_parse_failure_never_500s(self, client: TestClient, test_requisition: Requisition):
        with (
            patch("app.routers.htmx.offers.crud.claude_configured", return_value=True),
            patch(
                "app.services.freeform_parser_service.parse_freeform_offer",
                new_callable=AsyncMock,
                side_effect=Exception("AI exploded"),
            ),
        ):
            resp = client.post(
                f"/v2/partials/requisitions/{test_requisition.id}/add-offer/parse",
                data={"raw_text": "LM317T 100pcs"},
            )
        assert resp.status_code == 200
        assert "Parse failed" in resp.text

    def test_parse_blocks_non_owner(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User, admin_user: User
    ):
        """The paste-parse route enforces require_requisition_access (no read-IDOR)."""
        test_user.role = UserRole.SALES
        test_requisition.created_by = admin_user.id
        db_session.commit()
        with patch("app.routers.htmx.offers.crud.claude_configured", return_value=True):
            resp = client.post(
                f"/v2/partials/requisitions/{test_requisition.id}/add-offer/parse",
                data={"raw_text": "LM317T 100pcs"},
            )
        assert resp.status_code == 404


# ── Flagged-AI filter inside the Responses tab ──────────────────────────────


def _pending_offer(db_session: Session, req: Requisition, mpn: str = "STM32F103", vendor: str = "Mouser") -> Offer:
    """Seed a pending_review (flagged AI) offer on *req*."""
    offer = Offer(
        requisition_id=req.id,
        requirement_id=req.requirements[0].id if req.requirements else None,
        vendor_name=vendor,
        mpn=mpn,
        qty_available=1000,
        unit_price=3.50,
        status=OfferStatus.PENDING_REVIEW,
        parse_confidence=0.65,
        created_at=datetime.now(UTC),
    )
    db_session.add(offer)
    db_session.commit()
    db_session.refresh(offer)
    return offer


class TestResponsesFlaggedFilter:
    def test_responses_tab_shows_flagged_pill_with_count(
        self, client: TestClient, db_session: Session, test_requisition: Requisition
    ):
        _pending_offer(db_session, test_requisition)
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/responses")
        assert resp.status_code == 200
        assert "Flagged AI offers (1)" in resp.text
        assert f"/v2/partials/requisitions/{test_requisition.id}/tab/responses?flagged=1" in resp.text
        # Default view is the responses list, not the flagged rows
        assert "Pending review" not in resp.text

    def test_flagged_view_lists_only_pending_review_offers(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        flagged = _pending_offer(db_session, test_requisition)
        active = Offer(
            requisition_id=test_requisition.id,
            vendor_name="Arrow Electronics",
            mpn="NE555P",
            status=OfferStatus.ACTIVE,
            entered_by_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(active)
        db_session.commit()

        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/responses?flagged=1")
        assert resp.status_code == 200
        assert flagged.mpn in resp.text
        assert "Pending review" in resp.text
        assert active.mpn not in resp.text
        # Approve/reject act through the existing offer_service-backed review route,
        # staying in the Responses context.
        assert f"/v2/partials/requisitions/{test_requisition.id}/offers/{flagged.id}/review" in resp.text
        assert '"tab": "responses"' in resp.text

    def test_flagged_view_empty_state(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/responses?flagged=1")
        assert resp.status_code == 200
        assert "No offers pending review" in resp.text

    def test_approve_from_responses_promotes_and_stays_in_context(
        self, client: TestClient, db_session: Session, test_requisition: Requisition
    ):
        offer = _pending_offer(db_session, test_requisition)
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/offers/{offer.id}/review",
            data={"action": "approve", "tab": "responses"},
        )
        assert resp.status_code == 200
        db_session.refresh(offer)
        assert offer.status == OfferStatus.ACTIVE
        # Re-rendered in the Responses context with the flagged view now empty
        assert "No offers pending review" in resp.text

    def test_reject_from_responses_rejects_and_stays_in_context(
        self, client: TestClient, db_session: Session, test_requisition: Requisition
    ):
        offer = _pending_offer(db_session, test_requisition)
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/offers/{offer.id}/review",
            data={"action": "reject", "tab": "responses"},
        )
        assert resp.status_code == 200
        db_session.refresh(offer)
        assert offer.status == OfferStatus.REJECTED
        assert "No offers pending review" in resp.text
