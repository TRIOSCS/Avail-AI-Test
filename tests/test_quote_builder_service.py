"""tests/test_quote_builder_service.py — Build-Quote tab service layer (Chunk A).

Covers the three additive service pieces the in-workspace Build-Quote tab needs:
  - ``best_cost_for`` / ``best_costs_for`` — MIN unit_price across a requirement's
    ACTIVE offers (compute-on-read; mirrors the resell ``best_offer_unit_price`` rollup).
  - ``quote_export_context`` — the clean customer-facing PDF whitelist (NO vendor /
    offer / source identity ever crosses into it; mirrors ``bid_back_export_context``).
  - ``margin_guardrail`` — pure helper warning on sell<cost and on thin margins.
  - The ``generate_quote_report_pdf`` rewiring renders from the whitelist and leaks
    no vendor name.

Called by: pytest
Depends on: conftest fixtures (db_session, test_user)
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Offer, Quote, Requirement, Requisition, User
from app.services.quote_builder_service import (
    best_cost_for,
    best_costs_for,
    margin_guardrail,
    quote_export_context,
)
from tests.conftest import engine

_ = engine


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def req_with_offers(db_session: Session, test_user: User):
    """A requisition + one requirement with three offers: two ACTIVE (0.55, 0.40) and
    one SOLD (0.10 — cheaper but inactive, must be ignored)."""
    req = Requisition(
        name="QB-A",
        customer_name="Chunk A Co",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    item = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        manufacturer="TI",
        target_qty=100,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.flush()

    def _offer(vendor, price, status):
        return Offer(
            requisition_id=req.id,
            requirement_id=item.id,
            vendor_name=vendor,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status=status,
            unit_price=price,
            qty_available=500,
            created_at=datetime.now(timezone.utc),
        )

    active_hi = _offer("Arrow", 0.55, "active")
    active_lo = _offer("Avnet", 0.40, "active")
    sold_cheap = _offer("Mouser", 0.10, "sold")
    db_session.add_all([active_hi, active_lo, sold_cheap])
    db_session.commit()
    for o in (active_hi, active_lo, sold_cheap):
        db_session.refresh(o)
    db_session.refresh(item)
    return req, item, {"active_hi": active_hi, "active_lo": active_lo, "sold": sold_cheap}


# ── best_cost_for / best_costs_for ────────────────────────────────────────────


class TestBestCostFor:
    def test_returns_min_across_active_offers(self, db_session: Session, req_with_offers):
        _req, item, offers = req_with_offers
        result = best_cost_for(db_session, item.id)
        assert result is not None
        assert result["unit_cost"] == pytest.approx(0.40)
        assert result["offer_id"] == offers["active_lo"].id

    def test_ignores_inactive_offers(self, db_session: Session, req_with_offers):
        """The 0.10 SOLD offer is the cheapest but must NOT win the rollup."""
        _req, item, offers = req_with_offers
        result = best_cost_for(db_session, item.id)
        assert result["offer_id"] != offers["sold"].id
        assert result["unit_cost"] > 0.10

    def test_none_when_no_offers(self, db_session: Session, test_user: User):
        req = Requisition(
            name="QB-A-EMPTY",
            customer_name="Empty Co",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="NOPART",
            target_qty=1,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        assert best_cost_for(db_session, item.id) is None

    def test_none_when_active_offers_have_no_price(self, db_session: Session, test_user: User):
        req = Requisition(
            name="QB-A-NOPRICE",
            customer_name="NoPrice Co",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="X",
            target_qty=1,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.flush()
        db_session.add(
            Offer(
                requisition_id=req.id,
                requirement_id=item.id,
                vendor_name="NoPrice",
                mpn="X",
                normalized_mpn="X",
                status="active",
                unit_price=None,
                created_at=datetime.now(timezone.utc),
            )
        )
        db_session.commit()
        db_session.refresh(item)
        assert best_cost_for(db_session, item.id) is None

    def test_batch_matches_singular_and_avoids_n_plus_1(self, db_session: Session, req_with_offers):
        _req, item, offers = req_with_offers
        batch = best_costs_for(db_session, [item.id, 999_999])
        assert batch[item.id]["unit_cost"] == pytest.approx(0.40)
        assert batch[item.id]["offer_id"] == offers["active_lo"].id
        # Requirement with no offers is simply absent from the map.
        assert 999_999 not in batch

    def test_batch_empty_input(self, db_session: Session):
        assert best_costs_for(db_session, []) == {}


# ── quote_export_context ──────────────────────────────────────────────────────

# Tokens that would betray vendor / offer / source identity if they ever leaked.
_LEAK_KEYS = {
    "vendor_name",
    "vendor",
    "vendor_card_id",
    "offer_id",
    "offer",
    "source",
    "selected_offer_id",
    "best_offer_id",
    "entered_by_id",
    "material_card_id",
}
_ALLOWED_LINE_KEYS = {
    "part_number",
    "manufacturer",
    "quantity",
    "condition",
    "cost",
    "sell",
    "margin",
    "extended",
}


def _seeded_quote(db_session: Session, test_user: User) -> Quote:
    req = Requisition(
        name="QB-A-EXPORT",
        customer_name="Export Co",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    quote = Quote(
        requisition_id=req.id,
        quote_number="Q-2026-0099",
        revision=2,
        line_items=[
            {
                # Deliberately seed leaky fields to prove they are STRIPPED.
                "mpn": "LM317T",
                "manufacturer": "TI",
                "qty": 100,
                "cost_price": 0.40,
                "sell_price": 0.60,
                "margin_pct": 33.33,
                "condition": "new",
                "vendor_name": "Arrow Electronics",
                "offer_id": 4242,
                "source": "manual",
                "material_card_id": 7,
            }
        ],
        subtotal=60.0,
        total_cost=40.0,
        total_margin_pct=33.33,
        created_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(quote)
    db_session.commit()
    db_session.refresh(quote)
    return quote


class TestQuoteExportContext:
    def test_only_whitelisted_line_keys(self, db_session: Session, test_user: User):
        quote = _seeded_quote(db_session, test_user)
        ctx = quote_export_context(quote)
        assert ctx["lines"], "expected at least one exported line"
        for line in ctx["lines"]:
            assert set(line.keys()) == _ALLOWED_LINE_KEYS

    def test_no_vendor_or_offer_key_appears(self, db_session: Session, test_user: User):
        quote = _seeded_quote(db_session, test_user)
        ctx = quote_export_context(quote)
        for line in ctx["lines"]:
            assert not (_LEAK_KEYS & set(line.keys()))

    def test_no_vendor_token_in_serialized_payload(self, db_session: Session, test_user: User):
        """A whole-payload scan: the seeded vendor name must not survive anywhere."""
        import json

        quote = _seeded_quote(db_session, test_user)
        blob = json.dumps(quote_export_context(quote)).lower()
        assert "arrow" not in blob
        assert "vendor" not in blob
        assert "4242" not in blob  # the seeded offer_id

    def test_header_carries_quote_metadata_only(self, db_session: Session, test_user: User):
        quote = _seeded_quote(db_session, test_user)
        ctx = quote_export_context(quote)
        assert ctx["quote_number"] == "Q-2026-0099"
        assert ctx["revision"] == 2

    def test_margins_and_extended_computed(self, db_session: Session, test_user: User):
        quote = _seeded_quote(db_session, test_user)
        line = quote_export_context(quote)["lines"][0]
        assert line["cost"] == pytest.approx(0.40)
        assert line["sell"] == pytest.approx(0.60)
        assert line["quantity"] == 100
        assert line["extended"] == pytest.approx(60.0)
        # margin% = (sell - cost) / sell * 100
        assert line["margin"] == pytest.approx(33.33, abs=0.01)


# ── margin_guardrail ──────────────────────────────────────────────────────────


class TestMarginGuardrail:
    def test_warns_when_sell_below_cost(self):
        msg = margin_guardrail(1.00, 0.80)
        assert msg is not None
        assert isinstance(msg, str)

    def test_warns_on_thin_margin(self):
        # 5% margin against a 10% default threshold.
        msg = margin_guardrail(0.95, 1.00)
        assert msg is not None

    def test_none_when_healthy(self):
        # 50% margin — comfortably above threshold.
        assert margin_guardrail(0.50, 1.00) is None

    def test_respects_custom_threshold(self):
        # 15% margin: warns at min 20, clears at min 10.
        assert margin_guardrail(0.85, 1.00, min_margin_pct=20.0) is not None
        assert margin_guardrail(0.85, 1.00, min_margin_pct=10.0) is None

    def test_none_when_sell_is_zero_or_missing(self):
        # No sell price → nothing to judge (avoid div-by-zero noise).
        assert margin_guardrail(1.00, 0) is None
        assert margin_guardrail(1.00, None) is None


# ── generate_quote_report_pdf rewiring ────────────────────────────────────────


class TestQuoteReportPdf:
    def test_returns_bytes_and_no_vendor_leak(self, db_session: Session, test_user: User):
        """The rewired PDF path returns bytes and the rendered HTML leaks no vendor.

        Patches ``_render_pdf`` to capture the HTML it renders (WeasyPrint itself is
        already exercised elsewhere; here we assert the whitelist reaches the template
        and the seeded vendor name never makes it into the customer doc).
        """
        from unittest.mock import patch

        import app.services.document_service as ds

        quote = _seeded_quote(db_session, test_user)

        captured: dict = {}

        def _fake_render(template_name, **context):
            template = ds._jinja_env.get_template(template_name)
            captured["html"] = template.render(generated_at="2026-06-23 00:00 UTC", **context)
            return b"%PDF-1.4 fake"

        with patch.object(ds, "_render_pdf", side_effect=_fake_render):
            pdf = ds.generate_quote_report_pdf(quote.id, db_session)

        assert isinstance(pdf, bytes)
        html = captured["html"]
        # Whitelisted fields rendered…
        assert "LM317T" in html
        assert quote.quote_number in html
        # …but no vendor / offer identity from the seeded leaky line.
        assert "Arrow" not in html
        assert "4242" not in html
