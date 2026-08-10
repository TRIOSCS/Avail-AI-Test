"""tests/test_remediation_p0.py — QC 2026-08-10 P0 (bleeding-now) regressions.

- P0-1 proactive offer email always includes the parts table (AI/user body is an
  intro, no longer a replacement that drops the parts).
- P0-2 email-parsed offers are always PENDING_REVIEW (never auto-ACTIVE on the
  model's self-graded confidence).
- P0-3 nightly auto-dedup does NOT perform irreversible merges while the
  default-off flag is set.
- P0-4b quote email shows the unit price at full precision (never rounds a real
  sub-cent price to "$0"); P0-4c internal notes never reach the customer email.

Called by: pytest autodiscovery
Depends on: conftest fixtures (db_session).
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

from tests.conftest import engine  # noqa: F401

# ── P0-1 · parts table always rendered ───────────────────────────────────


def test_proactive_email_keeps_parts_table_with_ai_intro():
    from app.services.proactive_service import _template_email_html

    line_items = [
        {
            "mpn": "GSOT36C-E3-08",
            "manufacturer": "Vishay",
            "qty": 1000,
            "sell_price": 0.05,
            "condition": "New",
            "lead_time": "stock",
        }
    ]
    html = _template_email_html("Rep", [], line_items, None, intro_html="<p>Hi — here are parts you asked about.</p>")
    assert "Hi — here are parts you asked about." in html  # AI intro preserved
    assert "GSOT36C-E3-08" in html  # parts table STILL rendered (the bug was dropping it)
    assert "<table" in html


def test_proactive_email_default_intro_when_no_body():
    from app.services.proactive_service import _template_email_html

    html = _template_email_html("Rep", [], [{"mpn": "X", "qty": 1, "sell_price": 1.0}], None)
    assert "parts available" in html and "X" in html


# ── P0-2 · email-parsed offers never auto-ACTIVE ─────────────────────────


def test_email_parsed_offer_is_pending_review_even_at_high_confidence(db_session):
    from app.constants import OfferStatus
    from app.email_service import _auto_create_offers_from_parse
    from app.models import Offer, Requisition, User
    from app.models.offers import VendorResponse

    user = User(email="scan@trioscs.com", name="Scanner", role="buyer", azure_id="az-scan")
    db_session.add(user)
    db_session.flush()
    req = Requisition(name="REQ-P0", status="open", created_by=user.id)
    db_session.add(req)
    db_session.flush()
    vr = VendorResponse(
        requisition_id=req.id,
        vendor_name="Acme",
        vendor_email="s@acme.com",
        confidence=0.99,
        scanned_by_user_id=user.id,
        status="parsed",
    )
    db_session.add(vr)
    db_session.flush()

    draft = {"mpn": "LM317T", "vendor_name": "Acme", "qty_available": 500, "unit_price": 0.42, "currency": "USD"}
    with patch("app.services.response_parser.extract_draft_offers", return_value=[draft]):
        _auto_create_offers_from_parse(vr, {"overall_confidence": 0.99}, db_session)

    offer = db_session.query(Offer).filter(Offer.vendor_response_id == vr.id).one()
    # 0.99 confidence used to mint ACTIVE — now it must wait for human review.
    assert offer.status == OfferStatus.PENDING_REVIEW


# ── P0-3 · nightly dedup does not merge while gated off ───────────────────


def test_auto_dedup_gated_off_does_not_merge(db_session):
    from app.services import auto_dedup_service as ad

    candidate = {
        "company_a": {"id": 1, "name": "Foo Inc"},
        "company_b": {"id": 2, "name": "Foo Incorporated"},
        "auto_keep_id": 1,
        "score": 95,
    }
    with (
        patch.object(ad.settings, "auto_dedup_merge_enabled", False),
        patch("app.company_utils.find_company_dedup_candidates", return_value=[candidate]),
        patch("app.services.auto_dedup_service._ai_confirm_company_merge", return_value=True),
        patch("app.services.company_merge_service.merge_companies") as mock_merge,
    ):
        # Companies must exist for the guard's owner check.
        from app.models import Company

        for cid, name in [(1, "Foo Inc"), (2, "Foo Incorporated")]:
            db_session.add(Company(id=cid, name=name, is_active=True, created_at=datetime.now(UTC)))
        db_session.flush()
        merged = ad._dedup_companies(db_session)

    assert merged == 0
    mock_merge.assert_not_called()  # the irreversible merge never ran


def test_auto_dedup_merges_when_explicitly_enabled(db_session):
    from app.services import auto_dedup_service as ad

    candidate = {
        "company_a": {"id": 3, "name": "Bar LLC"},
        "company_b": {"id": 4, "name": "Bar L.L.C."},
        "auto_keep_id": 3,
        "score": 99,
    }
    with (
        patch.object(ad.settings, "auto_dedup_merge_enabled", True),
        patch("app.company_utils.find_company_dedup_candidates", return_value=[candidate]),
        patch("app.services.company_merge_service.merge_companies") as mock_merge,
    ):
        from app.models import Company

        for cid, name in [(3, "Bar LLC"), (4, "Bar L.L.C.")]:
            db_session.add(Company(id=cid, name=name, is_active=True, created_at=datetime.now(UTC)))
        db_session.flush()
        ad._dedup_companies(db_session)

    mock_merge.assert_called_once()  # opt-in restores the merge


# ── P0-4 · quote email: price precision + no internal-note leak ───────────


def _quote_for_email(db_session, notes, customer_message, line_items):
    from app.models import Quote, Requisition, User

    user = db_session.query(User).first() or User(email="q@x.com", name="Q", role="sales", azure_id="az-q")
    if user.id is None:
        db_session.add(user)
        db_session.flush()
    req = Requisition(name="rq-email", status="quoted", created_by=user.id)
    db_session.add(req)
    db_session.flush()
    quote = Quote(
        requisition_id=req.id,
        quote_number="Q-EMAIL-1",
        status="draft",
        line_items=line_items,
        notes=notes,
        customer_message=customer_message,
        subtotal=Decimal("237.50"),
    )
    db_session.add(quote)
    db_session.flush()
    return quote, user


def test_quote_email_shows_subcent_price_not_zero(db_session):
    from app.services.quote_send import _build_quote_email_html

    quote, user = _quote_for_email(
        db_session,
        None,
        None,
        [{"mpn": "PARTA", "sell_price": 0.0475, "qty": 5000}, {"mpn": "PARTB", "sell_price": 0.004, "qty": 1000}],
    )
    html = _build_quote_email_html(quote, "Buyer", "Beckhoff", user)
    assert "$0.0475" in html  # full precision, not rounded to $0.05
    assert "$0.004" in html  # a real sub-half-cent price is NOT shown as "$0"


def test_quote_email_never_includes_internal_notes(db_session):
    from app.services.quote_send import _build_quote_email_html

    quote, user = _quote_for_email(
        db_session,
        "INTERNAL: cost 0.30, hold firm, do not disclose",
        "Thanks for the RFQ — pricing below.",
        [{"mpn": "PARTC", "sell_price": 1.25, "qty": 100}],
    )
    html = _build_quote_email_html(quote, "Buyer", "Beckhoff", user)
    assert "Thanks for the RFQ" in html  # customer_message IS shown
    assert "do not disclose" not in html  # internal notes NEVER reach the customer
    assert "INTERNAL" not in html
