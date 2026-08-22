"""test_po_confirm_paste.py — TDD tests for PO-confirm paste-prefill (survey idea #6).

Covers the inbound twin of the Copy-for-ERP chip on the Approvals PO pane:
  - parse_po_confirmation service: regex-first PO number (wins over the AI's),
    sanitized fields (date validated, payment method whitelisted, serials joined),
    advisory cross-check warnings vs the plan line (vendor/MPN/qty — only when both
    sides are present), None on AI failure.
  - POST /v2/partials/approvals/po/{line_id}/parse-confirmation: re-renders the PO
    pane with the confirm form pre-filled + warning banner; empty paste and AI
    failure render friendly notes; NEVER writes the line (po_number stays None,
    status stays awaiting_po — the buyer still clicks Confirm PO).
  - The awaiting_po pane renders the paste affordance; other statuses do not.

Called by: pytest (TESTING=1 PYTHONPATH=. pytest tests/test_po_confirm_paste.py -v)
Depends on: app.services.po_confirm_paste_service, app.routers.htmx.approvals_hub,
            conftest (client/db_session/test_user), seed helpers mirrored from
            tests/test_approvals_hub_tabs.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.constants import BuyPlanLineStatus, OfferStatus, SOVerificationStatus
from app.models import Offer, Requirement, User
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.quotes import Quote
from app.models.sourcing import Requisition
from app.models.vendors import VendorCard

_HX = {"HX-Request": "true"}

# ── Seed helpers (mirroring test_approvals_hub_tabs.py) ────────────────────────


def _seed_awaiting_po_line(db: Session, user: User) -> BuyPlanLine:
    req = Requisition(
        name=f"REQ-{uuid.uuid4().hex[:6]}",
        customer_name="AcmeCo",
        status="open",
        created_by=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    rq = Requirement(requisition_id=req.id, primary_mpn="LM317", created_at=datetime.now(UTC))
    db.add(rq)
    db.flush()
    q = Quote(
        requisition_id=req.id,
        quote_number=f"Q-{uuid.uuid4().hex[:8]}",
        line_items=[],
        status="sent",
        created_by_id=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(q)
    db.flush()
    bp = BuyPlan(
        requisition_id=req.id,
        quote_id=q.id,
        status="active",
        so_status=SOVerificationStatus.APPROVED.value,
        submitted_by_id=user.id,
        total_cost=1000.0,
        total_revenue=2000.0,
        total_margin_pct=50.0,
        created_at=datetime.now(UTC),
    )
    db.add(bp)
    db.flush()
    vc = VendorCard(normalized_name=f"vc-{uuid.uuid4().hex[:8]}", display_name="Acme Dist")
    db.add(vc)
    db.flush()
    off = Offer(
        requirement_id=rq.id,
        vendor_card_id=vc.id,
        vendor_name="Acme Dist",
        vendor_name_normalized="acme dist",
        mpn="LM317",
        normalized_mpn="LM317",
        unit_price=1.0,
        status=OfferStatus.ACTIVE.value,
    )
    db.add(off)
    db.flush()
    line = BuyPlanLine(
        buy_plan_id=bp.id,
        requirement_id=rq.id,
        offer_id=off.id,
        quantity=100,
        unit_cost=1.0,
        unit_sell=2.0,
        buyer_id=user.id,
        status=BuyPlanLineStatus.AWAITING_PO.value,
    )
    db.add(line)
    db.commit()
    return line


import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def restricted_client(db_session: Session, sales_user: User):
    """TestClient authenticated as a restricted-role (sales) user who owns nothing."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = lambda: sales_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)


_AI_RESULT = {
    "po_number": "PO-77812",
    "estimated_ship_date": "2026-09-04",
    "payment_method": "wire",
    "serial_numbers": ["ZC11AAAA", "ZC11BBBB"],
    "vendor_name": "Acme Dist",
    "mpn": "LM317",
    "quantity": 100,
    "unit_cost": 1.0,
}


# ── Service: parse_po_confirmation ─────────────────────────────────────────────


class TestParsePoConfirmationService:
    async def test_regex_po_number_wins_over_ai(self):
        from app.services.po_confirm_paste_service import parse_po_confirmation

        ai = dict(_AI_RESULT, po_number="PO-WRONG")
        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=ai):
            result = await parse_po_confirmation(
                "Purchase Order PO# 77812 for Acme Dist ...",
                line_mpn="LM317",
                vendor_name="Acme Dist",
                quantity=100,
                unit_cost=1.0,
            )
        assert result is not None
        assert result["po_number"] == "77812"

    async def test_fields_sanitized_and_serials_joined(self):
        from app.services.po_confirm_paste_service import parse_po_confirmation

        ai = dict(
            _AI_RESULT,
            po_number="  PO-9001  ",
            estimated_ship_date="not-a-date",
            payment_method="bitcoin",
            serial_numbers=["  S1 ", "", None, "S2"],
        )
        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=ai):
            result = await parse_po_confirmation(
                "confirmation text without a labeled po token",
                line_mpn="LM317",
                vendor_name="Acme Dist",
                quantity=100,
                unit_cost=1.0,
            )
        assert result is not None
        assert result["po_number"] == "PO-9001"  # AI value used when regex finds nothing
        assert result["estimated_ship_date"] is None  # invalid date dropped
        assert result["payment_method"] is None  # not a PO_LINE_PAYMENT_METHODS value
        assert result["serial_numbers"] == "S1, S2"

    async def test_warnings_on_vendor_mpn_qty_mismatch(self):
        from app.services.po_confirm_paste_service import parse_po_confirmation

        ai = dict(_AI_RESULT, vendor_name="Other Corp", mpn="LM318", quantity=50)
        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=ai):
            result = await parse_po_confirmation(
                "text", line_mpn="LM317", vendor_name="Acme Dist", quantity=100, unit_cost=1.0
            )
        assert result is not None
        joined = " ".join(result["warnings"]).lower()
        assert "vendor" in joined
        assert "part" in joined or "mpn" in joined
        assert "quantity" in joined

    async def test_no_warnings_when_fields_match_or_absent(self):
        from app.services.po_confirm_paste_service import parse_po_confirmation

        ai = dict(_AI_RESULT, vendor_name=None, mpn="lm317", quantity=None, unit_cost=None)
        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=ai):
            result = await parse_po_confirmation(
                "text", line_mpn="LM317", vendor_name="Acme Dist", quantity=100, unit_cost=1.0
            )
        assert result is not None
        assert result["warnings"] == []

    async def test_ai_failure_returns_none(self):
        from app.services.po_confirm_paste_service import parse_po_confirmation

        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=None):
            assert (
                await parse_po_confirmation("text", line_mpn="LM317", vendor_name="V", quantity=1, unit_cost=1.0)
            ) is None

    async def test_multiple_labeled_tokens_prefer_the_ai_agreeing_one(self):
        """A customer-PO reference next to the real PO must not hijack the field:
        with several labeled tokens, the one the AI agrees with wins."""
        from app.services.po_confirm_paste_service import parse_po_confirmation

        text = "Customer PO: ACME-889   Ship to: Fremont\nOur PO# 77812 acknowledged."
        ai = dict(_AI_RESULT, po_number="PO-77812")
        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=ai):
            result = await parse_po_confirmation(
                text, line_mpn="LM317", vendor_name="Acme Dist", quantity=100, unit_cost=1.0
            )
        assert result is not None
        assert result["po_number"] == "77812"

    async def test_label_grammar_common_shapes_match(self):
        """'PO Number: X' / 'PO No: X' are the dominant label styles and must stay
        deterministic; a bare 'PO:' followed by a date must NOT capture the date."""
        from app.services.po_confirm_paste_service import parse_po_confirmation

        ai = dict(_AI_RESULT, po_number="PO-WRONG")
        cases = {
            "PO Number: 77812 confirmed": "77812",
            "PO No: 4512 thanks": "4512",
        }
        for text, expected in cases.items():
            with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=ai):
                result = await parse_po_confirmation(
                    text, line_mpn="LM317", vendor_name="Acme Dist", quantity=100, unit_cost=1.0
                )
            assert result is not None and result["po_number"] == expected, text

        ai_date = dict(_AI_RESULT, po_number="PO-9001")
        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=ai_date):
            result = await parse_po_confirmation(
                "PO: 2026-09-04 is the ship date",
                line_mpn="LM317",
                vendor_name="Acme Dist",
                quantity=100,
                unit_cost=1.0,
            )
        assert result is not None
        assert result["po_number"] == "PO-9001"  # date-shaped token rejected → AI value

    async def test_ship_date_normalized_to_iso(self):
        """Python 3.12 fromisoformat accepts '20260904' — normalize so the <input
        type=date> doesn't silently drop the value."""
        from app.services.po_confirm_paste_service import parse_po_confirmation

        ai = dict(_AI_RESULT, estimated_ship_date="20260904")
        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=ai):
            result = await parse_po_confirmation(
                "text", line_mpn="LM317", vendor_name="Acme Dist", quantity=100, unit_cost=1.0
            )
        assert result is not None
        assert result["estimated_ship_date"] == "2026-09-04"


# ── Route: POST /v2/partials/approvals/po/{line_id}/parse-confirmation ─────────


class TestParseConfirmationRoute:
    def test_parse_prefills_confirm_form_with_warning_banner(self, client, db_session, test_user):
        line = _seed_awaiting_po_line(db_session, test_user)
        prefill = {
            "po_number": "PO-77812",
            "estimated_ship_date": "2026-09-04",
            "payment_method": "wire",
            "serial_numbers": "ZC11AAAA, ZC11BBBB",
            "warnings": ["Vendor on the confirmation ('Other Corp') does not match this line ('Acme Dist')."],
        }
        with patch(
            "app.routers.htmx.approvals_hub.parse_po_confirmation", new_callable=AsyncMock, return_value=prefill
        ):
            resp = client.post(
                f"/v2/partials/approvals/po/{line.id}/parse-confirmation",
                data={"pasted_text": "PO confirmation text"},
                headers=_HX,
            )
        assert resp.status_code == 200
        body = resp.text
        assert 'value="PO-77812"' in body
        assert 'value="2026-09-04"' in body
        assert "ZC11AAAA, ZC11BBBB" in body
        assert "Other Corp" in body  # warning banner rendered
        # The confirm form is still there for the human to review + submit.
        assert "Confirm PO" in body
        # Nothing was written.
        db_session.refresh(line)
        assert line.po_number is None
        assert line.status == BuyPlanLineStatus.AWAITING_PO.value

    def test_parse_prefill_selects_payment_method(self, client, db_session, test_user):
        line = _seed_awaiting_po_line(db_session, test_user)
        prefill = {
            "po_number": "PO-1",
            "estimated_ship_date": None,
            "payment_method": "ach",
            "serial_numbers": None,
            "warnings": [],
        }
        with patch(
            "app.routers.htmx.approvals_hub.parse_po_confirmation", new_callable=AsyncMock, return_value=prefill
        ):
            resp = client.post(
                f"/v2/partials/approvals/po/{line.id}/parse-confirmation",
                data={"pasted_text": "x"},
                headers=_HX,
            )
        assert resp.status_code == 200
        assert '<option value="ach" selected' in resp.text

    def test_parse_success_retargets_full_pane(self, client, db_session, test_user):
        """Success swaps the whole pane (HX-Retarget) so the prefilled form renders; the
        paste form itself targets only the small #po-paste-result note div."""
        line = _seed_awaiting_po_line(db_session, test_user)
        prefill = {
            "po_number": "PO-1",
            "estimated_ship_date": None,
            "payment_method": None,
            "serial_numbers": None,
            "warnings": [],
        }
        with patch(
            "app.routers.htmx.approvals_hub.parse_po_confirmation", new_callable=AsyncMock, return_value=prefill
        ):
            resp = client.post(
                f"/v2/partials/approvals/po/{line.id}/parse-confirmation", data={"pasted_text": "x"}, headers=_HX
            )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Retarget") == "#aw-pane"

    def test_parse_empty_paste_returns_note_fragment_without_ai(self, client, db_session, test_user):
        """Empty paste → a tiny note into #po-paste-result — NOT a pane re-render, so
        the buyer's typed-but-unsubmitted values and the open fold stay untouched."""
        line = _seed_awaiting_po_line(db_session, test_user)
        with patch("app.routers.htmx.approvals_hub.parse_po_confirmation", new_callable=AsyncMock) as mock_parse:
            resp = client.post(
                f"/v2/partials/approvals/po/{line.id}/parse-confirmation", data={"pasted_text": "  "}, headers=_HX
            )
        assert resp.status_code == 200
        assert "nothing to parse" in resp.text.lower()
        assert "Confirm PO" not in resp.text  # fragment, not the pane
        assert resp.headers.get("HX-Retarget") is None
        mock_parse.assert_not_called()

    def test_parse_ai_failure_returns_note_fragment(self, client, db_session, test_user):
        """AI failure → note fragment only; the pasted text stays in the textarea
        because nothing re-renders (the 'try trimming' advice stays honest)."""
        line = _seed_awaiting_po_line(db_session, test_user)
        with patch("app.routers.htmx.approvals_hub.parse_po_confirmation", new_callable=AsyncMock, return_value=None):
            resp = client.post(
                f"/v2/partials/approvals/po/{line.id}/parse-confirmation", data={"pasted_text": "x"}, headers=_HX
            )
        assert resp.status_code == 200
        assert "could not parse" in resp.text.lower()
        assert "Confirm PO" not in resp.text
        assert resp.headers.get("HX-Retarget") is None

    def test_parse_restricted_non_owner_404_without_ai_call(self, restricted_client, db_session, test_user):
        """The ownership gate runs BEFORE the paid AI call (review finding)."""
        line = _seed_awaiting_po_line(db_session, test_user)
        with patch("app.routers.htmx.approvals_hub.parse_po_confirmation", new_callable=AsyncMock) as mock_parse:
            resp = restricted_client.post(
                f"/v2/partials/approvals/po/{line.id}/parse-confirmation", data={"pasted_text": "x"}, headers=_HX
            )
        assert resp.status_code == 404
        mock_parse.assert_not_called()

    def test_parse_non_awaiting_po_line_short_circuits_without_ai(self, client, db_session, test_user):
        """A line that moved on (confirmed elsewhere) renders its current pane state
        without burning an AI call whose output nothing would display."""
        line = _seed_awaiting_po_line(db_session, test_user)
        line.status = BuyPlanLineStatus.PENDING_VERIFY.value
        line.po_number = "PO-9"
        line.po_confirmed_at = datetime.now(UTC)
        db_session.commit()
        with patch("app.routers.htmx.approvals_hub.parse_po_confirmation", new_callable=AsyncMock) as mock_parse:
            resp = client.post(
                f"/v2/partials/approvals/po/{line.id}/parse-confirmation", data={"pasted_text": "x"}, headers=_HX
            )
        assert resp.status_code == 200
        mock_parse.assert_not_called()

    def test_parse_rate_limited_returns_note_without_ai(self, client, db_session, test_user):
        line = _seed_awaiting_po_line(db_session, test_user)
        with (
            patch("app.routers.htmx.approvals_hub.check_rate_limit", return_value=False),
            patch("app.routers.htmx.approvals_hub.parse_po_confirmation", new_callable=AsyncMock) as mock_parse,
        ):
            resp = client.post(
                f"/v2/partials/approvals/po/{line.id}/parse-confirmation", data={"pasted_text": "x"}, headers=_HX
            )
        assert resp.status_code == 200
        assert "too many parse attempts" in resp.text.lower()
        mock_parse.assert_not_called()

    def test_parse_unknown_line_404(self, client):
        resp = client.post(
            "/v2/partials/approvals/po/999999/parse-confirmation", data={"pasted_text": "x"}, headers=_HX
        )
        assert resp.status_code == 404

    def test_parse_unauthenticated_401(self, unauthenticated_client, db_session, test_user):
        line = _seed_awaiting_po_line(db_session, test_user)
        resp = unauthenticated_client.post(
            f"/v2/partials/approvals/po/{line.id}/parse-confirmation", data={"pasted_text": "x"}, headers=_HX
        )
        assert resp.status_code == 401


# ── Template: paste affordance on the awaiting_po pane ─────────────────────────


def test_awaiting_po_pane_renders_paste_affordance(client, db_session, test_user):
    line = _seed_awaiting_po_line(db_session, test_user)
    resp = client.get(f"/v2/partials/approvals/po/{line.id}/pane", headers=_HX)
    assert resp.status_code == 200
    body = resp.text
    assert f"/v2/partials/approvals/po/{line.id}/parse-confirmation" in body
    assert 'name="pasted_text"' in body


def test_awaiting_po_pane_parse_button_has_inflight_state(client, db_session, test_user):
    """The Parse button disables + relabels during the 45s AI call (loading-states on
    the BUTTON, not the form, plus the Alpine 'parsing' flag — the codebase's
    paste_offer_form idiom)."""
    line = _seed_awaiting_po_line(db_session, test_user)
    resp = client.get(f"/v2/partials/approvals/po/{line.id}/pane", headers=_HX)
    body = resp.text
    assert "parsing:" in body or "parsing =" in body  # Alpine flag in x-data
    assert ':disabled="parsing"' in body
    assert 'id="po-paste-result"' in body  # failure-note target inside the fold


def test_serials_prefill_never_overwrites_saved_qp_serials(client, db_session, test_user):
    """A QP that already records serial numbers keeps them — the parsed serials are
    suppressed with an advisory line instead of silently replacing a saved value."""
    from app.models.quality_plan import QualityPlan

    line = _seed_awaiting_po_line(db_session, test_user)
    qp = QualityPlan(
        buy_plan_id=line.buy_plan_id,
        vendor_card_id=line.offer.vendor_card_id,  # qp_for_line matches per-vendor
        created_by_id=test_user.id,
        status="draft",
        order_type="new",
        purchasing_serial_numbers="SAVED-001",
    )
    db_session.add(qp)
    db_session.commit()
    prefill = {
        "po_number": "PO-1",
        "estimated_ship_date": None,
        "payment_method": None,
        "serial_numbers": "NEW-111, NEW-222",
        "warnings": [],
    }
    with patch("app.routers.htmx.approvals_hub.parse_po_confirmation", new_callable=AsyncMock, return_value=prefill):
        resp = client.post(
            f"/v2/partials/approvals/po/{line.id}/parse-confirmation", data={"pasted_text": "x"}, headers=_HX
        )
    body = resp.text
    assert 'value="SAVED-001"' in body  # saved QP value wins in the input
    assert 'value="NEW-111, NEW-222"' not in body
    assert "not applied" in body  # advisory tells the buyer to reconcile


def test_pending_verify_pane_has_no_paste_affordance(client, db_session, test_user):
    line = _seed_awaiting_po_line(db_session, test_user)
    line.status = BuyPlanLineStatus.PENDING_VERIFY.value
    line.po_number = "PO-9"
    line.po_confirmed_at = datetime.now(UTC)
    db_session.commit()
    resp = client.get(f"/v2/partials/approvals/po/{line.id}/pane", headers=_HX)
    assert resp.status_code == 200
    assert "parse-confirmation" not in resp.text
