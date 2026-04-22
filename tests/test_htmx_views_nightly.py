"""test_htmx_views_nightly.py — Nightly coverage boost for app/routers/htmx_views.py.

Targets previously uncovered sections: RFQ send (test mode), sourcing workspace,
materials faceted search, quotes CRUD, prospecting, settings tabs, bulk actions,
inline edit, buy-plan workflow partials.

Called by: pytest
Depends on: conftest.py (client, db_session, test_user, test_requisition)
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.orm import Session

from app.constants import BuyPlanStatus, OfferStatus, QuoteStatus, RequisitionStatus, SourcingStatus  # noqa: F401
from app.models import (
    Offer,
    Quote,
    QuoteLine,
    Requirement,
    Requisition,
    User,
    VendorCard,
)
from app.models.intelligence import MaterialCard
from app.models.prospect_account import ProspectAccount
from app.models.sourcing_lead import SourcingLead

# ── Helpers ────────────────────────────────────────────────────────────


def _req(db: Session, user: User, **kw) -> Requisition:
    defaults = dict(
        name="NIGHTLY-REQ",
        customer_name="Nightly Corp",
        status=RequisitionStatus.ACTIVE,
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = Requisition(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _requirement(db: Session, req: Requisition, mpn="LM317T", **kw) -> Requirement:
    defaults = dict(
        requisition_id=req.id,
        primary_mpn=mpn,
        target_qty=100,
        sourcing_status=SourcingStatus.OPEN,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = Requirement(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _vendor(db: Session, name="Test Vendor", **kw) -> VendorCard:
    defaults = dict(
        normalized_name=name.lower().replace(" ", "_"),
        display_name=name,
        emails=[],
        phones=[],
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = VendorCard(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _offer(db: Session, req: Requisition, vendor: VendorCard, **kw) -> Offer:
    defaults = dict(
        requisition_id=req.id,
        mpn="LM317T",
        vendor_name=vendor.display_name,
        vendor_name_normalized=vendor.normalized_name,
        qty_available=500,
        unit_price=0.25,
        status=OfferStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = Offer(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _quote(db: Session, req: Requisition, user: User, **kw) -> Quote:
    import uuid

    defaults = dict(
        requisition_id=req.id,
        quote_number=f"Q-{uuid.uuid4().hex[:8].upper()}",
        status=QuoteStatus.DRAFT,
        created_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = Quote(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _quote_line(db: Session, quote: Quote, **kw) -> QuoteLine:
    defaults = dict(
        quote_id=quote.id,
        mpn="LM317T",
        qty=100,
        cost_price=0.20,
        sell_price=0.25,
        margin_pct=20.0,
    )
    defaults.update(kw)
    obj = QuoteLine(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _material_card(db: Session, mpn="TEST-MPN-001", **kw) -> MaterialCard:
    defaults = dict(
        normalized_mpn=mpn,
        display_mpn=mpn,
        manufacturer="TestCo",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = MaterialCard(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _sourcing_lead(db: Session, req: Requirement, vendor_name="Arrow Electronics", **kw) -> SourcingLead:
    import uuid

    defaults = dict(
        lead_id=f"lead-{uuid.uuid4().hex[:12]}",
        requirement_id=req.id,
        requisition_id=req.requisition_id,
        part_number_requested=req.primary_mpn or "LM317T",
        part_number_matched=req.primary_mpn or "LM317T",
        vendor_name=vendor_name,
        vendor_name_normalized=vendor_name.lower().replace(" ", "_"),
        primary_source_type="api",
        primary_source_name="brokerbin",
        confidence_score=0.85,
        confidence_band="high",
        buyer_status="new",
        corroborated=False,
    )
    defaults.update(kw)
    obj = SourcingLead(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _prospect(db: Session, domain="example.com", **kw) -> ProspectAccount:
    import uuid

    d = domain if "." in domain else f"{uuid.uuid4().hex[:8]}.{domain}"
    defaults = dict(
        name=f"Example Inc ({d})",
        domain=d,
        discovery_source="web_ai",
        status="suggested",
        fit_score=60,
        readiness_score=55,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = ProspectAccount(**defaults)
    db.add(obj)
    db.flush()
    return obj


# ── RFQ Send (test mode) ───────────────────────────────────────────────


class TestRfqSend:
    def test_rfq_send_test_mode_creates_contacts(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        _requirement(db_session, req)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/rfq-send",
            data={
                "vendor_names": ["Arrow Electronics"],
                "vendor_emails": ["sales@arrow.com"],
                "subject": "RFQ - LM317T",
                "body": "Please quote LM317T",
                "parts_summary": "LM317T x 100",
            },
        )
        assert resp.status_code == 200

    def test_rfq_send_no_vendors_raises_400(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/rfq-send",
            data={"subject": "RFQ"},
        )
        assert resp.status_code == 400

    def test_rfq_send_multiple_vendors(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/rfq-send",
            data={
                "vendor_names": ["Arrow", "Digi-Key"],
                "vendor_emails": ["arrow@arrow.com", "sales@digikey.com"],
                "subject": "Multi-vendor RFQ",
            },
        )
        assert resp.status_code == 200

    def test_rfq_send_empty_email_skipped(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/rfq-send",
            data={
                "vendor_names": ["Arrow", "No Email Vendor"],
                "vendor_emails": ["arrow@arrow.com", ""],
                "subject": "RFQ skip test",
            },
        )
        assert resp.status_code == 200


# ── Bulk Action ────────────────────────────────────────────────────────


class TestBulkAction:
    def test_bulk_archive(self, client, db_session: Session, test_user: User):
        r1 = _req(db_session, test_user)
        r2 = _req(db_session, test_user, name="NIGHTLY-REQ2")
        db_session.commit()

        resp = client.post(
            "/v2/partials/requisitions/bulk/archive",
            data={"ids": f"{r1.id},{r2.id}"},
        )
        assert resp.status_code == 200

    def test_bulk_activate(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user, status=RequisitionStatus.ARCHIVED)
        db_session.commit()

        resp = client.post(
            "/v2/partials/requisitions/bulk/activate",
            data={"ids": str(req.id)},
        )
        assert resp.status_code == 200

    def test_bulk_invalid_action_raises_400(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        db_session.commit()

        resp = client.post(
            "/v2/partials/requisitions/bulk/delete",
            data={"ids": str(req.id)},
        )
        assert resp.status_code == 400

    def test_bulk_no_ids_raises_400(self, client, db_session: Session, test_user: User):
        resp = client.post("/v2/partials/requisitions/bulk/archive", data={})
        assert resp.status_code == 400


# ── Sourcing Partials ─────────────────────────────────────────────────


class TestSourcingPartials:
    def test_sourcing_page_full_load(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/sourcing/{item.id}")
        assert resp.status_code == 200

    def test_sourcing_results_empty(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{item.id}")
        assert resp.status_code == 200

    def test_sourcing_results_with_leads(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        _sourcing_lead(db_session, item)
        _sourcing_lead(db_session, item, vendor_name="Mouser Electronics", confidence_band="medium")
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{item.id}")
        assert resp.status_code == 200

    def test_sourcing_results_filters(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        _sourcing_lead(db_session, item, confidence_band="high")
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{item.id}?confidence=high&source=api&sort=freshest")
        assert resp.status_code == 200

    def test_sourcing_results_filter_contactability(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        _sourcing_lead(db_session, item, contact_email="vendor@arrow.com")
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{item.id}?contactability=has_email")
        assert resp.status_code == 200

    def test_sourcing_results_filter_corroborated(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        _sourcing_lead(db_session, item, corroborated=True)
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{item.id}?corroborated=yes")
        assert resp.status_code == 200

    def test_sourcing_results_filter_freshness(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{item.id}?freshness=7d")
        assert resp.status_code == 200

    def test_sourcing_results_missing_req_404(self, client, db_session: Session):
        resp = client.get("/v2/partials/sourcing/999999")
        assert resp.status_code == 404

    def test_sourcing_workspace(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        _sourcing_lead(db_session, item)
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{item.id}/workspace")
        assert resp.status_code == 200

    def test_sourcing_workspace_page(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/sourcing/{item.id}/workspace")
        assert resp.status_code == 200

    def test_sourcing_workspace_list(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        _sourcing_lead(db_session, item)
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{item.id}/workspace-list")
        assert resp.status_code == 200

    def test_sourcing_workspace_list_empty(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{item.id}/workspace-list")
        assert resp.status_code == 200

    def test_sourcing_workspace_with_filters(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        _sourcing_lead(db_session, item, vendor_safety_band="safe")
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{item.id}/workspace?safety=safe&sort=safest&lead=0")
        assert resp.status_code == 200

    def test_lead_panel_partial(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        lead = _sourcing_lead(db_session, item)
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/leads/{lead.id}/panel")
        assert resp.status_code == 200

    def test_lead_panel_missing_404(self, client, db_session: Session):
        resp = client.get("/v2/partials/sourcing/leads/999999/panel")
        assert resp.status_code == 404

    def test_sourcing_search_trigger(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock(return_value=None)
        mock_broker.listen = AsyncMock(return_value=iter([]))

        with (
            patch("app.services.sse_broker.broker", mock_broker),
            patch("app.search_service.quick_search_mpn", AsyncMock(return_value=[])),
        ):
            resp = client.post(f"/v2/partials/sourcing/{item.id}/search")

        assert resp.status_code in (200, 303)

    def test_lead_detail_page(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        lead = _sourcing_lead(db_session, item)
        db_session.commit()

        resp = client.get(f"/v2/sourcing/leads/{lead.id}")
        assert resp.status_code == 200


# ── Materials Partials ────────────────────────────────────────────────


class TestMaterialsPartials:
    def test_materials_workspace(self, client, db_session: Session):
        resp = client.get("/v2/partials/materials/workspace")
        assert resp.status_code == 200

    def test_materials_faceted_empty(self, client, db_session: Session):
        resp = client.get("/v2/partials/materials/faceted")
        assert resp.status_code == 200

    def test_materials_faceted_with_query(self, client, db_session: Session, test_user: User):
        _material_card(db_session, mpn="LM317T-NIGHTLY")
        db_session.commit()

        resp = client.get("/v2/partials/materials/faceted?q=LM317T")
        assert resp.status_code == 200

    def test_materials_faceted_with_commodity(self, client, db_session: Session):
        resp = client.get("/v2/partials/materials/faceted?commodity=ic")
        assert resp.status_code == 200

    def test_material_detail(self, client, db_session: Session):
        card = _material_card(db_session)
        db_session.commit()

        resp = client.get(f"/v2/partials/materials/{card.id}")
        assert resp.status_code == 200

    def test_material_detail_not_found(self, client, db_session: Session):
        resp = client.get("/v2/partials/materials/999999")
        assert resp.status_code == 404

    def test_material_tab_vendors(self, client, db_session: Session):
        card = _material_card(db_session)
        db_session.commit()

        resp = client.get(f"/v2/partials/materials/{card.id}/tab/vendors")
        assert resp.status_code == 200

    def test_material_tab_customers(self, client, db_session: Session):
        card = _material_card(db_session)
        db_session.commit()

        resp = client.get(f"/v2/partials/materials/{card.id}/tab/customers")
        assert resp.status_code == 200

    def test_material_tab_sourcing(self, client, db_session: Session):
        card = _material_card(db_session)
        db_session.commit()

        resp = client.get(f"/v2/partials/materials/{card.id}/tab/sourcing")
        assert resp.status_code == 200

    def test_material_tab_price_history(self, client, db_session: Session):
        card = _material_card(db_session)
        db_session.commit()

        resp = client.get(f"/v2/partials/materials/{card.id}/tab/price_history")
        assert resp.status_code == 200

    def test_material_tab_unknown_returns_404(self, client, db_session: Session):
        card = _material_card(db_session)
        db_session.commit()

        resp = client.get(f"/v2/partials/materials/{card.id}/tab/unknown_tab")
        assert resp.status_code == 404

    def test_material_tab_missing_card(self, client, db_session: Session):
        resp = client.get("/v2/partials/materials/999999/tab/vendors")
        assert resp.status_code == 404

    def test_update_material_card(self, client, db_session: Session):
        card = _material_card(db_session)
        db_session.commit()

        resp = client.put(
            f"/v2/partials/materials/{card.id}",
            data={"description": "Updated description", "manufacturer": "NewCo"},
        )
        assert resp.status_code == 200

    def test_update_material_card_not_found(self, client, db_session: Session):
        resp = client.put(
            "/v2/partials/materials/999999",
            data={"description": "test"},
        )
        assert resp.status_code == 404

    def test_manufacturer_add(self, client, db_session: Session):
        resp = client.post("/v2/partials/manufacturers/add", data={"name": "NewMfr Corp"})
        assert resp.status_code == 200

    def test_manufacturer_add_empty_name(self, client, db_session: Session):
        resp = client.post("/v2/partials/manufacturers/add", data={"name": "  "})
        assert resp.status_code == 200

    def test_materials_filters_tree(self, client, db_session: Session):
        resp = client.get("/v2/partials/materials/filters/tree")
        assert resp.status_code == 200

    def test_materials_filters_sub_no_commodity(self, client, db_session: Session):
        resp = client.get("/v2/partials/materials/filters/sub")
        assert resp.status_code == 200
        assert resp.text == ""

    def test_materials_filters_sub_with_commodity(self, client, db_session: Session):
        resp = client.get("/v2/partials/materials/filters/sub?commodity=ic")
        assert resp.status_code == 200

    def test_materials_ai_interpret_short_query(self, client, db_session: Session):
        resp = client.get("/v2/partials/materials/ai-interpret?q=LM317")
        assert resp.status_code == 200

    def test_materials_ai_interpret_long_query(self, client, db_session: Session):
        with patch(
            "app.services.materials_ai_search.interpret_search_query",
            AsyncMock(return_value={"commodity": "ic", "q": "LM317T linear regulator"}),
        ):
            resp = client.get("/v2/partials/materials/ai-interpret?q=LM317T linear regulator")
        assert resp.status_code == 200


# ── Quotes Partials ───────────────────────────────────────────────────


class TestQuotesPartials:
    def test_quotes_list_empty(self, client, db_session: Session):
        resp = client.get("/v2/partials/quotes")
        assert resp.status_code == 200

    def test_quotes_list_with_query(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        _quote(db_session, req, test_user, quote_number="Q-FINDME")
        db_session.commit()

        resp = client.get("/v2/partials/quotes?q=FINDME")
        assert resp.status_code == 200

    def test_quotes_list_with_status_filter(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        _quote(db_session, req, test_user)
        db_session.commit()

        resp = client.get("/v2/partials/quotes?status=draft")
        assert resp.status_code == 200

    def test_quote_detail(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        q = _quote(db_session, req, test_user)
        db_session.commit()

        resp = client.get(f"/v2/partials/quotes/{q.id}")
        assert resp.status_code == 200

    def test_quote_detail_not_found(self, client, db_session: Session):
        resp = client.get("/v2/partials/quotes/999999")
        assert resp.status_code == 404

    def test_add_quote_line(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        q = _quote(db_session, req, test_user)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/quotes/{q.id}/lines",
            data={"mpn": "LM7805", "manufacturer": "TI", "qty": "50", "cost_price": "0.30", "sell_price": "0.40"},
        )
        assert resp.status_code == 200

    def test_add_quote_line_not_found(self, client, db_session: Session):
        resp = client.post(
            "/v2/partials/quotes/999999/lines",
            data={"mpn": "TEST", "qty": "1", "cost_price": "0", "sell_price": "0"},
        )
        assert resp.status_code == 404

    def test_update_quote_line(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        q = _quote(db_session, req, test_user)
        line = _quote_line(db_session, q)
        db_session.commit()

        resp = client.put(
            f"/v2/partials/quotes/{q.id}/lines/{line.id}",
            data={"qty": "200", "sell_price": "0.30"},
        )
        assert resp.status_code == 200

    def test_update_quote_line_not_found(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        q = _quote(db_session, req, test_user)
        db_session.commit()

        resp = client.put(
            f"/v2/partials/quotes/{q.id}/lines/999999",
            data={"qty": "1"},
        )
        assert resp.status_code == 404

    def test_update_quote_line_invalid_qty(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        q = _quote(db_session, req, test_user)
        line = _quote_line(db_session, q)
        db_session.commit()

        resp = client.put(
            f"/v2/partials/quotes/{q.id}/lines/{line.id}",
            data={"qty": "not-a-number"},
        )
        assert resp.status_code == 400

    def test_delete_quote_line(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        q = _quote(db_session, req, test_user)
        line = _quote_line(db_session, q)
        db_session.commit()

        resp = client.delete(f"/v2/partials/quotes/{q.id}/lines/{line.id}")
        assert resp.status_code == 200
        assert resp.text == ""

    def test_delete_quote_line_not_found(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        q = _quote(db_session, req, test_user)
        db_session.commit()

        resp = client.delete(f"/v2/partials/quotes/{q.id}/lines/999999")
        assert resp.status_code == 404

    def test_add_offer_to_quote(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        vendor = _vendor(db_session)
        offer = _offer(db_session, req, vendor)
        q = _quote(db_session, req, test_user)
        db_session.commit()

        resp = client.post(f"/v2/partials/quotes/{q.id}/add-offer/{offer.id}")
        assert resp.status_code == 200

    def test_add_offer_to_quote_offer_not_found(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        q = _quote(db_session, req, test_user)
        db_session.commit()

        resp = client.post(f"/v2/partials/quotes/{q.id}/add-offer/999999")
        assert resp.status_code == 404

    def test_send_quote(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        q = _quote(db_session, req, test_user, status=QuoteStatus.DRAFT)
        db_session.commit()

        resp = client.post(f"/v2/partials/quotes/{q.id}/send")
        assert resp.status_code == 200

    def test_send_quote_not_found(self, client, db_session: Session):
        resp = client.post("/v2/partials/quotes/999999/send")
        assert resp.status_code == 404

    def test_quote_result_won(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        q = _quote(db_session, req, test_user, status=QuoteStatus.SENT)
        db_session.commit()

        resp = client.post(f"/v2/partials/quotes/{q.id}/result", data={"result": "won"})
        assert resp.status_code == 200

    def test_quote_result_lost(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        q = _quote(db_session, req, test_user, status=QuoteStatus.SENT)
        db_session.commit()

        resp = client.post(f"/v2/partials/quotes/{q.id}/result", data={"result": "lost"})
        assert resp.status_code == 200

    def test_quote_result_invalid(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        q = _quote(db_session, req, test_user, status=QuoteStatus.SENT)
        db_session.commit()

        resp = client.post(f"/v2/partials/quotes/{q.id}/result", data={"result": "maybe"})
        assert resp.status_code == 400

    def test_revise_quote(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        q = _quote(db_session, req, test_user)
        db_session.commit()

        resp = client.post(f"/v2/partials/quotes/{q.id}/revise")
        assert resp.status_code == 200

    def test_revise_quote_not_found(self, client, db_session: Session):
        resp = client.post("/v2/partials/quotes/999999/revise")
        assert resp.status_code == 404

    def test_apply_markup(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        q = _quote(db_session, req, test_user)
        _quote_line(db_session, q)
        db_session.commit()

        resp = client.post(f"/v2/partials/quotes/{q.id}/apply-markup", data={"markup_pct": "30.0"})
        assert resp.status_code == 200

    def test_add_offers_to_draft_quote(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        vendor = _vendor(db_session)
        offer = _offer(db_session, req, vendor)
        q = _quote(db_session, req, test_user)
        db_session.commit()

        import json

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offers-to-quote",
            content=json.dumps({"offer_ids": [offer.id], "quote_id": q.id}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

    def test_add_offers_to_draft_quote_not_draft(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        vendor = _vendor(db_session)
        offer = _offer(db_session, req, vendor)
        q = _quote(db_session, req, test_user, status=QuoteStatus.SENT)
        db_session.commit()

        import json

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offers-to-quote",
            content=json.dumps({"offer_ids": [offer.id], "quote_id": q.id}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_add_offers_missing_ids(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        db_session.commit()

        import json

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offers-to-quote",
            content=json.dumps({}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_build_buy_plan_not_won(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        q = _quote(db_session, req, test_user, status=QuoteStatus.DRAFT)
        db_session.commit()

        resp = client.post(f"/v2/partials/quotes/{q.id}/build-buy-plan")
        assert resp.status_code == 400


# ── Prospecting Partials ──────────────────────────────────────────────


class TestProspectingPartials:
    def test_prospecting_list_empty(self, client, db_session: Session):
        resp = client.get("/v2/partials/prospecting")
        assert resp.status_code == 200

    def test_prospecting_list_with_data(self, client, db_session: Session):
        _prospect(db_session)
        db_session.commit()

        resp = client.get("/v2/partials/prospecting")
        assert resp.status_code == 200

    def test_prospecting_list_filter_status(self, client, db_session: Session):
        _prospect(db_session, status="claimed")
        db_session.commit()

        resp = client.get("/v2/partials/prospecting?status=claimed")
        assert resp.status_code == 200

    def test_prospecting_list_search(self, client, db_session: Session):
        _prospect(db_session, name="Unique Corp Name", domain="uniquecorp-nightly.com")
        db_session.commit()

        resp = client.get("/v2/partials/prospecting?q=Unique")
        assert resp.status_code == 200

    def test_prospecting_list_sort_fit(self, client, db_session: Session):
        _prospect(db_session)
        db_session.commit()

        resp = client.get("/v2/partials/prospecting?sort=fit_desc")
        assert resp.status_code == 200

    def test_prospecting_list_sort_recent(self, client, db_session: Session):
        _prospect(db_session)
        db_session.commit()

        resp = client.get("/v2/partials/prospecting?sort=recent_desc")
        assert resp.status_code == 200

    def test_prospecting_stats(self, client, db_session: Session):
        _prospect(db_session, fit_score=80, readiness_score=75)
        db_session.commit()

        resp = client.get("/v2/partials/prospecting/stats")
        assert resp.status_code == 200

    def test_prospecting_detail(self, client, db_session: Session):
        p = _prospect(db_session)
        db_session.commit()

        resp = client.get(f"/v2/partials/prospecting/{p.id}")
        assert resp.status_code == 200

    def test_prospecting_detail_not_found(self, client, db_session: Session):
        resp = client.get("/v2/partials/prospecting/999999")
        assert resp.status_code == 404

    def test_dismiss_prospect(self, client, db_session: Session):
        p = _prospect(db_session)
        db_session.commit()

        resp = client.post(f"/v2/partials/prospecting/{p.id}/dismiss")
        assert resp.status_code == 200

    def test_dismiss_prospect_not_found(self, client, db_session: Session):
        resp = client.post("/v2/partials/prospecting/999999/dismiss")
        assert resp.status_code == 404

    def test_enrich_prospect(self, client, db_session: Session):
        p = _prospect(db_session)
        db_session.commit()

        with (
            patch(
                "app.services.prospect_free_enrichment.run_free_enrichment",
                AsyncMock(return_value=None),
            ),
            patch(
                "app.services.prospect_warm_intros.detect_warm_intros",
                return_value={"connection": "none"},
            ),
            patch(
                "app.services.prospect_warm_intros.generate_one_liner",
                return_value="Great prospect",
            ),
        ):
            resp = client.post(f"/v2/partials/prospecting/{p.id}/enrich")

        assert resp.status_code == 200

    def test_enrich_prospect_not_found(self, client, db_session: Session):
        with (
            patch(
                "app.services.prospect_free_enrichment.run_free_enrichment",
                AsyncMock(return_value=None),
            ),
        ):
            resp = client.post("/v2/partials/prospecting/999999/enrich")

        assert resp.status_code == 404

    def test_add_prospect_domain(self, client, db_session: Session, test_user: User):
        with patch(
            "app.services.prospect_claim.add_prospect_manually",
            return_value=MagicMock(id=42),
        ):
            resp = client.post("/v2/partials/prospecting/add-domain", data={"domain": "newco.com"})

        assert resp.status_code == 200

    def test_add_prospect_domain_empty(self, client, db_session: Session):
        resp = client.post("/v2/partials/prospecting/add-domain", data={"domain": ""})
        assert resp.status_code == 400


# ── Settings Partials ─────────────────────────────────────────────────


class TestSettingsPartials:
    def test_settings_index(self, client, db_session: Session):
        resp = client.get("/v2/partials/settings")
        assert resp.status_code == 200

    def test_settings_index_tab_param(self, client, db_session: Session):
        resp = client.get("/v2/partials/settings?tab=profile")
        assert resp.status_code == 200

    def test_settings_sources(self, client, db_session: Session):
        resp = client.get("/v2/partials/settings/sources")
        assert resp.status_code == 200

    def test_settings_profile(self, client, db_session: Session):
        resp = client.get("/v2/partials/settings/profile")
        assert resp.status_code == 200

    def test_settings_system_non_admin_raises_403(self, client, db_session: Session):
        resp = client.get("/v2/partials/settings/system")
        assert resp.status_code == 403


# ── Inline Edit / Action ──────────────────────────────────────────────


class TestInlineEdit:
    def test_requisition_inline_edit_cell_name(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        db_session.commit()

        resp = client.get(f"/v2/partials/requisitions/{req.id}/edit/name")
        assert resp.status_code == 200

    def test_requisition_inline_edit_cell_invalid_field(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        db_session.commit()

        resp = client.get(f"/v2/partials/requisitions/{req.id}/edit/invalid_field")
        assert resp.status_code == 400
