"""test_htmx_views_nightly.py — Nightly coverage boost for app/routers/htmx_views.py.

Targets previously uncovered sections: RFQ send (test mode), sourcing workspace,
materials faceted search, quotes CRUD, prospecting, settings tabs, bulk actions,
inline edit, buy-plan workflow partials.

Called by: pytest
Depends on: conftest.py (client, db_session, test_user, test_requisition)
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime, timedelta
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
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
        status=RequisitionStatus.OPEN,
        created_by=user.id,
        created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
    )
    defaults.update(kw)
    obj = ProspectAccount(**defaults)
    db.add(obj)
    db.flush()
    return obj


# ── RFQ Send (test mode) ───────────────────────────────────────────────


class TestRfqSend:
    def test_rfq_send_test_mode_creates_contacts(self, client, db_session: Session, test_user: User):
        from app.models.offers import Contact as RfqContact

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
        assert "RFQ sent to 1 vendor(s)" in resp.text
        assert "Arrow Electronics" in resp.text
        contact = db_session.query(RfqContact).filter_by(requisition_id=req.id).one()
        assert contact.vendor_name == "Arrow Electronics"
        assert contact.vendor_contact == "sales@arrow.com"
        assert contact.status == "sent"

    def test_rfq_send_no_vendors_raises_400(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/rfq-send",
            data={"subject": "RFQ"},
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize(
        ("vendor_names", "vendor_emails", "subject", "expected_sent", "vendor_present", "vendor_absent"),
        [
            (
                ["Arrow", "Digi-Key"],
                ["arrow@arrow.com", "sales@digikey.com"],
                "Multi-vendor RFQ",
                2,
                ["Arrow", "Digi-Key"],
                None,
            ),
            (
                ["Arrow", "No Email Vendor"],
                ["arrow@arrow.com", ""],
                "RFQ skip test",
                1,
                ["Arrow"],
                "No Email Vendor",
            ),
        ],
        ids=["multiple_vendors", "empty_email_skipped"],
    )
    def test_rfq_send_multi_vendor(
        self,
        client,
        db_session: Session,
        test_user: User,
        vendor_names,
        vendor_emails,
        subject,
        expected_sent,
        vendor_present,
        vendor_absent,
    ):
        from app.models.offers import Contact as RfqContact

        req = _req(db_session, test_user)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/rfq-send",
            data={
                "vendor_names": vendor_names,
                "vendor_emails": vendor_emails,
                "subject": subject,
            },
        )
        assert resp.status_code == 200
        assert f"RFQ sent to {expected_sent} vendor(s)" in resp.text
        for name in vendor_present:
            assert name in resp.text
        if vendor_absent:
            # No email address given -- never sent, never wrote a Contact row.
            assert vendor_absent not in resp.text
        contacts = db_session.query(RfqContact).filter_by(requisition_id=req.id).all()
        assert len(contacts) == expected_sent


# ── Bulk Action ────────────────────────────────────────────────────────


class TestBulkAction:
    def test_bulk_invalid_action_raises_400(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        db_session.commit()

        resp = client.post(
            "/v2/partials/requisitions/bulk/delete",
            data={"ids": str(req.id)},
        )
        assert resp.status_code == 400

    def test_bulk_no_ids_raises_400(self, client, db_session: Session, test_user: User):
        resp = client.post("/v2/partials/requisitions/bulk/assign", data={})
        assert resp.status_code == 400


# ── Sourcing Partials ─────────────────────────────────────────────────


class TestSourcingPartials:
    def test_sourcing_page_full_load(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        # This route calls get_user(request, db) directly rather than via Depends, so
        # the client fixture's require_user override never applies here — patch it
        # like the rest of the suite does for this same route family.
        with patch("app.routers.htmx.sourcing.get_user", return_value=test_user):
            resp = client.get(f"/v2/sourcing/{item.id}")
        assert resp.status_code == 200
        # The shell lazy-loads the results partial for THIS requirement, not a stale/wrong one.
        assert f'hx-get="/v2/partials/sourcing/{item.id}"' in resp.text

    def test_sourcing_results_empty(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{item.id}")
        assert resp.status_code == 200
        assert "No leads found for this part" in resp.text

    def test_sourcing_results_with_leads(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        _sourcing_lead(db_session, item)
        _sourcing_lead(db_session, item, vendor_name="Mouser Electronics", confidence_band="medium")
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{item.id}")
        assert resp.status_code == 200
        assert "2 leads found" in resp.text
        assert "Arrow Electronics" in resp.text
        assert "Mouser Electronics" in resp.text

    def test_sourcing_results_filters(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        _sourcing_lead(db_session, item, confidence_band="high")
        # Excluded by confidence=high — must not survive the filter.
        _sourcing_lead(db_session, item, vendor_name="Weakling Vendor", confidence_band="low")
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{item.id}?confidence=high&source=api&sort=freshest")
        assert resp.status_code == 200
        assert "1 lead found" in resp.text
        assert "Arrow Electronics" in resp.text
        assert "Weakling Vendor" not in resp.text

    def test_sourcing_results_filter_contactability(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        _sourcing_lead(db_session, item, contact_email="vendor@arrow.com")
        # No email on file — must be excluded by has_email.
        _sourcing_lead(db_session, item, vendor_name="No Email Vendor", contact_email=None)
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{item.id}?contactability=has_email")
        assert resp.status_code == 200
        assert "1 lead found" in resp.text
        assert "Arrow Electronics" in resp.text
        assert "No Email Vendor" not in resp.text

    def test_sourcing_results_filter_corroborated(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        _sourcing_lead(db_session, item, corroborated=True)
        # Single-source lead — must be excluded by corroborated=yes.
        _sourcing_lead(db_session, item, vendor_name="Uncorroborated Vendor", corroborated=False)
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{item.id}?corroborated=yes")
        assert resp.status_code == 200
        assert "1 lead found" in resp.text
        assert "Arrow Electronics" in resp.text
        assert "Uncorroborated Vendor" not in resp.text

    def test_sourcing_results_filter_freshness(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        _sourcing_lead(db_session, item, source_last_seen_at=datetime.now(UTC))
        # Seen 30 days ago — outside the 7d freshness window, must be excluded.
        _sourcing_lead(
            db_session,
            item,
            vendor_name="Stale Vendor",
            source_last_seen_at=datetime.now(UTC) - timedelta(days=30),
        )
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{item.id}?freshness=7d")
        assert resp.status_code == 200
        assert "1 lead found" in resp.text
        assert "Arrow Electronics" in resp.text
        assert "Stale Vendor" not in resp.text

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
        assert "Arrow Electronics" in resp.text
        assert "1 lead" in resp.text

    def test_sourcing_workspace_filters_target_rows_only_endpoint(self, client, db_session: Session, test_user: User):
        """SOURCING-WS-FILTER-WRONG-TARGET: in the workspace the filter/sort/source
        controls must hit the rows-only '/workspace-list' endpoint and swap into
        #lead-list-content — NOT the full card-grid partial (which nested the whole
        results page inside the left list panel)."""
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        _sourcing_lead(db_session, item)
        db_session.commit()

        body = client.get(f"/v2/partials/sourcing/{item.id}/workspace").text
        ws_list = f"/v2/partials/sourcing/{item.id}/workspace-list"
        # The filter controls (pills, sort select, source checkboxes) target the
        # rows-only endpoint and the left list container.
        assert f'hx-get="{ws_list}' in body
        assert 'hx-target="#lead-list-content"' in body
        # The full-grid partial URL must NOT be a filter hx-get target inside the
        # workspace filter bar (that was the nesting bug). It only appears as the
        # "Grid view" link href.
        assert f'hx-get="/v2/partials/sourcing/{item.id}?' not in body
        # push-url points at the real (resolvable) workspace page URL, not the naked
        # partial fragment (which would 404 on F5).
        assert f'hx-push-url="/v2/sourcing/{item.id}/workspace"' in body

    def test_sourcing_workspace_page(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        with patch("app.routers.htmx.sourcing.get_user", return_value=test_user):
            resp = client.get(f"/v2/sourcing/{item.id}/workspace")
        assert resp.status_code == 200
        assert f'hx-get="/v2/partials/sourcing/{item.id}/workspace"' in resp.text

    def test_sourcing_workspace_list(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        _sourcing_lead(db_session, item)
        # A second, differently-named lead must also render as its own row.
        _sourcing_lead(db_session, item, vendor_name="Mouser Electronics")
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{item.id}/workspace-list")
        assert resp.status_code == 200
        assert "Arrow Electronics" in resp.text
        assert "Mouser Electronics" in resp.text

    def test_sourcing_workspace_list_empty(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{item.id}/workspace-list")
        assert resp.status_code == 200
        assert "No leads found" in resp.text

    def test_sourcing_workspace_with_filters(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        _sourcing_lead(db_session, item, vendor_safety_band="safe")
        # Unsafe vendor — must be excluded by safety=safe.
        _sourcing_lead(db_session, item, vendor_name="Risky Vendor", vendor_safety_band="high_risk")
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/{item.id}/workspace?safety=safe&sort=safest&lead=0")
        assert resp.status_code == 200
        assert "Arrow Electronics" in resp.text
        assert "Risky Vendor" not in resp.text

    def test_lead_panel_partial(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        lead = _sourcing_lead(db_session, item)
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/leads/{lead.id}/panel")
        assert resp.status_code == 200
        assert "Arrow Electronics" in resp.text
        assert lead.part_number_matched in resp.text

    def test_lead_panel_missing_404(self, client, db_session: Session):
        resp = client.get("/v2/partials/sourcing/leads/999999/panel")
        assert resp.status_code == 404

    def test_sourcing_search_trigger(self, client, db_session: Session, test_user: User):
        """Re-search delegates to search_requirement() (which persists sightings +
        leads) and redirects back to the results page."""
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        mock_search = AsyncMock(return_value={"sightings": [], "source_stats": [], "mpn_results": {}})
        with patch("app.search_service.search_requirement", mock_search):
            resp = client.post(f"/v2/partials/sourcing/{item.id}/search")

        assert resp.status_code in (200, 303)
        # The persistence path was actually invoked (old code discarded results).
        mock_search.assert_awaited_once()
        assert resp.headers.get("HX-Redirect") == f"/v2/sourcing/{item.id}"

    def test_lead_detail_page(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        lead = _sourcing_lead(db_session, item)
        db_session.commit()

        with patch("app.routers.htmx.sourcing.get_user", return_value=test_user):
            resp = client.get(f"/v2/sourcing/leads/{lead.id}")
        assert resp.status_code == 200
        assert f'hx-get="/v2/partials/sourcing/leads/{lead.id}"' in resp.text


# ── Materials Partials ────────────────────────────────────────────────


class TestMaterialsPartials:
    def test_materials_workspace(self, client, db_session: Session):
        _material_card(db_session, mpn="WORKSPACE-COUNT-001")
        db_session.commit()

        resp = client.get("/v2/partials/materials/workspace")
        assert resp.status_code == 200
        # "All Materials (N)" reflects the real non-deleted card count.
        assert "All Materials" in resp.text
        assert "(1)" in resp.text

    def test_materials_faceted_empty(self, client, db_session: Session):
        resp = client.get("/v2/partials/materials/faceted")
        assert resp.status_code == 200
        assert "0 results" in resp.text

    def test_materials_faceted_with_query(self, client, db_session: Session, test_user: User):
        _material_card(db_session, mpn="LM317T-NIGHTLY")
        # A non-matching MPN must not appear in a query-scoped result set.
        _material_card(db_session, mpn="UNRELATED-PART-9")
        db_session.commit()

        resp = client.get("/v2/partials/materials/faceted?q=LM317T")
        assert resp.status_code == 200
        assert "1 result" in resp.text
        assert "LM317T-NIGHTLY" in resp.text
        assert "UNRELATED-PART-9" not in resp.text

    def test_materials_faceted_with_commodity(self, client, db_session: Session):
        _material_card(db_session, mpn="DRAM-PART-1", category="dram")
        # A different commodity's card must not leak into a commodity-scoped result.
        _material_card(db_session, mpn="SSD-PART-1", category="ssd")
        db_session.commit()

        resp = client.get("/v2/partials/materials/faceted?commodity=dram")
        assert resp.status_code == 200
        assert "1 result" in resp.text
        assert "DRAM-PART-1" in resp.text
        assert "SSD-PART-1" not in resp.text

    def test_material_detail(self, client, db_session: Session):
        card = _material_card(db_session)
        db_session.commit()

        resp = client.get(f"/v2/partials/materials/{card.id}")
        assert resp.status_code == 200
        assert card.display_mpn in resp.text
        assert card.manufacturer in resp.text

    def test_material_detail_not_found(self, client, db_session: Session):
        resp = client.get("/v2/partials/materials/999999")
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        ("tab", "expected_status"),
        [
            ("vendors", 200),
            ("customers", 200),
            ("sourcing", 200),
            ("price_history", 200),
            ("unknown_tab", 404),
        ],
    )
    def test_material_tab(self, client, db_session: Session, tab: str, expected_status: int):
        card = _material_card(db_session)
        db_session.commit()

        resp = client.get(f"/v2/partials/materials/{card.id}/tab/{tab}")
        assert resp.status_code == expected_status

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
        assert "Updated description" in resp.text
        db_session.refresh(card)
        assert card.description == "Updated description"
        assert card.manufacturer == "NewCo"

    def test_update_material_card_not_found(self, client, db_session: Session):
        resp = client.put(
            "/v2/partials/materials/999999",
            data={"description": "test"},
        )
        assert resp.status_code == 404

    def test_manufacturer_add(self, client, db_session: Session):
        from app.models.sourcing import Manufacturer

        resp = client.post("/v2/partials/manufacturers/add", data={"name": "NewMfr Corp"})
        assert resp.status_code == 200
        assert "Added: NewMfr Corp" in resp.text
        mfr = db_session.query(Manufacturer).filter_by(canonical_name="NewMfr Corp").one()
        assert mfr.canonical_name == "NewMfr Corp"

    def test_manufacturer_add_empty_name(self, client, db_session: Session):
        from app.models.sourcing import Manufacturer

        resp = client.post("/v2/partials/manufacturers/add", data={"name": "  "})
        assert resp.status_code == 200
        assert "Name required" in resp.text
        assert db_session.query(Manufacturer).count() == 0

    def test_materials_filters_tree(self, client, db_session: Session):
        _material_card(db_session, mpn="CAP-PART-1", category="capacitors")
        db_session.commit()

        resp = client.get("/v2/partials/materials/filters/tree")
        assert resp.status_code == 200
        # A category with a seeded card shows its display name + live count...
        assert "Capacitors" in resp.text
        assert "(1)" in resp.text
        # ...a zero-count sibling category is hidden entirely (count > 0 gate).
        assert "Resistors" not in resp.text

    def test_materials_filters_sub_no_commodity(self, client, db_session: Session):
        resp = client.get("/v2/partials/materials/filters/sub")
        assert resp.status_code == 200
        # Server-rendered placeholder nudge (replaced the old empty-string response).
        assert "Select a category to unlock spec filters" in resp.text

    def test_materials_filters_sub_with_commodity(self, client, db_session: Session):
        from app.models.faceted_search import CommoditySpecSchema

        db_session.add(
            CommoditySpecSchema(
                commodity="dram",
                spec_key="ddr_type",
                display_name="DDR Type",
                data_type="enum",
                enum_values=["DDR4", "DDR5"],
                sort_order=1,
                is_filterable=True,
                is_primary=True,
            )
        )
        db_session.commit()

        resp = client.get("/v2/partials/materials/filters/sub?commodity=dram")
        assert resp.status_code == 200
        # The seeded schema's own facet renders; the placeholder nudge must be gone
        # now that a commodity is scoped.
        assert "DDR Type" in resp.text
        assert "Select a category to unlock spec filters" not in resp.text

    def test_materials_ai_interpret_short_query(self, client, db_session: Session):
        resp = client.get("/v2/partials/materials/ai-interpret?q=LM317")
        assert resp.status_code == 200
        # Fewer than 3 words never calls the AI — no interpreted chip renders.
        assert "AI interpreted:" not in resp.text

    def test_materials_ai_interpret_long_query(self, client, db_session: Session):
        with patch(
            "app.services.materials_ai_search.interpret_search_query",
            AsyncMock(
                return_value={
                    "commodity": "dram",
                    "filters": {},
                    "summary": "DRAM matching LM317T linear regulator",
                }
            ),
        ):
            resp = client.get("/v2/partials/materials/ai-interpret?q=LM317T linear regulator")
        assert resp.status_code == 200
        assert "AI interpreted:" in resp.text
        assert "DRAM matching LM317T linear regulator" in resp.text
        assert 'd.commodity = "dram"' in resp.text


# ── Quotes Partials ───────────────────────────────────────────────────


class TestQuotesPartials:
    # /v2/partials/quotes standalone list was retired (quotes-relocation).
    # Quotes are now surfaced via /v2/partials/parts/{id}/tab/quotes and
    # /v2/partials/customers/{id}/tab/quotes. See test_quotes_relocation.py.

    def test_quote_detail(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        q = _quote(db_session, req, test_user)
        db_session.commit()

        resp = client.get(f"/v2/partials/quotes/{q.id}")
        assert resp.status_code == 200
        assert q.quote_number in resp.text

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
        assert "LM7805" in resp.text
        line = db_session.query(QuoteLine).filter_by(quote_id=q.id, mpn="LM7805").one()
        assert line.qty == 50
        assert float(line.sell_price) == 0.40

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
        assert "200" in resp.text
        db_session.refresh(line)
        assert line.qty == 200
        assert float(line.sell_price) == 0.30

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
        assert offer.mpn in resp.text
        line = db_session.query(QuoteLine).filter_by(quote_id=q.id, offer_id=offer.id).one()
        assert line.mpn == offer.mpn
        assert float(line.cost_price) == float(offer.unit_price)

    def test_add_offer_to_quote_offer_not_found(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        q = _quote(db_session, req, test_user)
        db_session.commit()

        resp = client.post(f"/v2/partials/quotes/{q.id}/add-offer/999999")
        assert resp.status_code == 404

    def test_add_offer_to_quote_same_requisition(self, client, db_session: Session, test_user: User):
        """Same-requisition offer attaches successfully (200, QuoteLine created)."""
        req = _req(db_session, test_user)
        vendor = _vendor(db_session)
        offer = _offer(db_session, req, vendor)
        q = _quote(db_session, req, test_user)
        db_session.commit()

        resp = client.post(f"/v2/partials/quotes/{q.id}/add-offer/{offer.id}")
        assert resp.status_code == 200

        line = db_session.query(QuoteLine).filter_by(quote_id=q.id, offer_id=offer.id).first()
        assert line is not None

    def test_add_offer_to_quote_null_requisition_allowed(self, client, db_session: Session, test_user: User):
        """Null-requisition offer (unsolicited Tier-5) attaches successfully (Option A
        allows it)."""
        req = _req(db_session, test_user)
        vendor = _vendor(db_session)
        offer = _offer(db_session, req, vendor, requisition_id=None)
        q = _quote(db_session, req, test_user)
        db_session.commit()

        resp = client.post(f"/v2/partials/quotes/{q.id}/add-offer/{offer.id}")
        assert resp.status_code == 200

        line = db_session.query(QuoteLine).filter_by(quote_id=q.id, offer_id=offer.id).first()
        assert line is not None

    def test_add_offer_to_quote_cross_requisition_forbidden(self, client, db_session: Session, test_user: User):
        """Cross-requisition offer → 403, no QuoteLine created."""
        req_a = _req(db_session, test_user)
        req_b = _req(db_session, test_user)
        vendor = _vendor(db_session)
        offer = _offer(db_session, req_b, vendor)  # belongs to req_b
        q = _quote(db_session, req_a, test_user)  # quote is for req_a
        db_session.commit()

        resp = client.post(f"/v2/partials/quotes/{q.id}/add-offer/{offer.id}")
        assert resp.status_code == 403

        line = db_session.query(QuoteLine).filter_by(quote_id=q.id, offer_id=offer.id).first()
        assert line is None

    def test_send_quote(self, client, db_session: Session, test_user: User, test_customer_site):
        # send now actually emails (S1 fix): give the quote a site with a recipient so the
        # canonical service can resolve a customer and mark it sent under TESTING.
        req = _req(db_session, test_user)
        q = _quote(db_session, req, test_user, status=QuoteStatus.DRAFT, customer_site_id=test_customer_site.id)
        db_session.commit()

        resp = client.post(f"/v2/partials/quotes/{q.id}/send")
        assert resp.status_code == 200
        db_session.refresh(q)
        assert q.status == QuoteStatus.SENT

    def test_send_quote_not_found(self, client, db_session: Session):
        resp = client.post("/v2/partials/quotes/999999/send")
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        ("result", "expected_status"),
        [
            ("won", 200),
            ("lost", 200),
            ("maybe", 400),
        ],
        ids=["won", "lost", "invalid"],
    )
    def test_quote_result(self, client, db_session: Session, test_user: User, result: str, expected_status: int):
        req = _req(db_session, test_user)
        q = _quote(db_session, req, test_user, status=QuoteStatus.SENT)
        db_session.commit()

        resp = client.post(f"/v2/partials/quotes/{q.id}/result", data={"result": result})
        assert resp.status_code == expected_status

    def test_revise_quote(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        q = _quote(db_session, req, test_user)
        original_number = q.quote_number
        db_session.commit()

        resp = client.post(f"/v2/partials/quotes/{q.id}/revise")
        assert resp.status_code == 200
        new_quote = db_session.query(Quote).filter_by(requisition_id=req.id).filter(Quote.id != q.id).one()
        assert new_quote.revision == 2
        assert new_quote.quote_number != original_number
        assert new_quote.quote_number in resp.text

    def test_revise_quote_not_found(self, client, db_session: Session):
        resp = client.post("/v2/partials/quotes/999999/revise")
        assert resp.status_code == 404

    def test_apply_markup(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        q = _quote(db_session, req, test_user)
        line = _quote_line(db_session, q, cost_price=0.20)
        db_session.commit()

        resp = client.post(f"/v2/partials/quotes/{q.id}/apply-markup", data={"markup_pct": "30.0"})
        assert resp.status_code == 200
        db_session.refresh(line)
        assert float(line.sell_price) == pytest.approx(0.26, abs=1e-4)

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
        assert "Offers added to quote" in resp.text
        line = db_session.query(QuoteLine).filter_by(quote_id=q.id, offer_id=offer.id).one()
        assert line.mpn == offer.mpn

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
        assert "No prospects found" in resp.text

    @pytest.mark.parametrize(
        "query",
        ["", "?sort=fit_desc", "?sort=recent_desc"],
        ids=["with_data", "sort_fit", "sort_recent"],
    )
    def test_prospecting_list_with_data(self, client, db_session: Session, query: str):
        _prospect(db_session, name="Sortable Prospect Co", domain="sortable-nightly.com")
        # dismissed is outside the default suggested+claimed set -- excluded regardless of sort.
        _prospect(db_session, name="Dismissed Prospect Co", domain="dismissed-nightly.com", status="dismissed")
        db_session.commit()

        resp = client.get(f"/v2/partials/prospecting{query}")
        assert resp.status_code == 200
        assert "Sortable Prospect Co" in resp.text
        assert "Dismissed Prospect Co" not in resp.text

    def test_prospecting_list_filter_status(self, client, db_session: Session):
        _prospect(db_session, name="Claimed Prospect Co", domain="claimed-nightly.com", status="claimed")
        # suggested must not leak into a status=claimed filter.
        _prospect(db_session, name="Suggested Prospect Co", domain="suggested-nightly.com", status="suggested")
        db_session.commit()

        resp = client.get("/v2/partials/prospecting?status=claimed")
        assert resp.status_code == 200
        assert "Claimed Prospect Co" in resp.text
        assert "Suggested Prospect Co" not in resp.text

    def test_prospecting_list_search(self, client, db_session: Session):
        _prospect(db_session, name="Unique Corp Name", domain="uniquecorp-nightly.com")
        # A non-matching name/domain must not surface under an unrelated search term.
        _prospect(db_session, name="Other Company Inc", domain="othercompany-nightly.com")
        db_session.commit()

        resp = client.get("/v2/partials/prospecting?q=Unique")
        assert resp.status_code == 200
        assert "Unique Corp Name" in resp.text
        assert "Other Company Inc" not in resp.text

    def test_prospecting_stats(self, client, db_session: Session):
        _prospect(db_session, fit_score=80, readiness_score=75)
        db_session.commit()

        resp = client.get("/v2/partials/prospecting/stats")
        assert resp.status_code == 200
        # Suggested = 1 (the seeded prospect); Call now = 1 (readiness 75 >= 70).
        assert '<p class="text-2xl font-bold text-gray-900">1</p>' in resp.text
        assert '<p class="figure-accent text-2xl font-bold">1</p>' in resp.text

    def test_prospecting_detail(self, client, db_session: Session):
        p = _prospect(db_session, name="Detail View Prospect Co")
        db_session.commit()

        resp = client.get(f"/v2/partials/prospecting/{p.id}")
        assert resp.status_code == 200
        assert "Detail View Prospect Co" in resp.text

    def test_prospecting_detail_not_found(self, client, db_session: Session):
        resp = client.get("/v2/partials/prospecting/999999")
        assert resp.status_code == 404

    def test_dismiss_prospect(self, client, db_session: Session):
        import json

        p = _prospect(db_session, name="Dismiss Me Corp")
        db_session.commit()

        resp = client.post(f"/v2/partials/prospecting/{p.id}/dismiss")
        assert resp.status_code == 200
        trigger = json.loads(resp.headers["HX-Trigger"])
        assert trigger["showToast"]["message"] == "Dismissed Dismiss Me Corp"
        db_session.refresh(p)
        assert p.status == "dismissed"

    def test_dismiss_prospect_not_found(self, client, db_session: Session):
        resp = client.post("/v2/partials/prospecting/999999/dismiss")
        assert resp.status_code == 404

    def test_enrich_prospect(self, client, db_session: Session):
        # Enrich spawns a background job and returns the status poller (200).
        p = _prospect(db_session)
        db_session.commit()

        with (
            patch("app.services.prospect_free_enrichment.run_enrichment_job"),
            patch("app.utils.async_helpers.safe_background_task", new_callable=AsyncMock),
        ):
            resp = client.post(f"/v2/partials/prospecting/{p.id}/enrich")

        assert resp.status_code == 200
        assert "enrich-status" in resp.text

    def test_enrich_prospect_not_found(self, client, db_session: Session):
        resp = client.post("/v2/partials/prospecting/999999/enrich")
        assert resp.status_code == 404

    def test_add_prospect_domain(self, client, db_session: Session, test_user: User):
        mock_add = MagicMock(
            return_value={
                "prospect_id": 42,
                "name": "Newco Inc",
                "domain": "newco.com",
                "status": "suggested",
                "is_new": True,
            }
        )
        with patch("app.services.prospect_claim.add_prospect_manually", mock_add):
            resp = client.post("/v2/partials/prospecting/add-domain", data={"domain": "newco.com"})

        assert resp.status_code == 200
        assert "Added" in resp.text
        assert "Newco Inc" in resp.text
        assert "/v2/prospecting/42" in resp.text
        mock_add.assert_called_once_with("newco.com", test_user.id, ANY)

    def test_add_prospect_domain_empty(self, client, db_session: Session):
        # Empty domain returns an inline error chip (200), not a 400.
        resp = client.post("/v2/partials/prospecting/add-domain", data={"domain": ""})
        assert resp.status_code == 200
        assert "domain" in resp.text.lower()


# ── Settings Partials ─────────────────────────────────────────────────


class TestSettingsPartials:
    @pytest.mark.parametrize(
        ("path", "expected_status"),
        [
            ("/v2/partials/settings", 200),
            ("/v2/partials/settings?tab=profile", 200),
            ("/v2/partials/settings/profile", 200),
            ("/v2/partials/settings/system", 403),  # non-admin
            ("/v2/partials/settings/connectors", 403),  # admin-only, buyer role → 403
        ],
        ids=["index", "index_tab_param", "profile", "system_non_admin", "connectors_non_admin"],
    )
    def test_settings(self, client, db_session: Session, path: str, expected_status: int):
        resp = client.get(path)
        assert resp.status_code == expected_status

    @pytest.mark.parametrize(
        "path",
        [
            "/v2/partials/settings/sources",
            "/v2/partials/settings/api-keys",
        ],
        ids=["sources_redirect", "api_keys_redirect"],
    )
    def test_settings_old_tabs_redirect_to_connectors(self, client, path: str):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 302
        assert "/connectors" in resp.headers["location"]


# ── Inline Edit / Action ──────────────────────────────────────────────


class TestInlineEdit:
    @pytest.mark.parametrize(
        ("field", "expected_status"),
        [
            ("name", 200),
            ("invalid_field", 400),
        ],
    )
    def test_requisition_inline_edit_cell(
        self, client, db_session: Session, test_user: User, field: str, expected_status: int
    ):
        req = _req(db_session, test_user)
        db_session.commit()

        resp = client.get(f"/v2/partials/requisitions/{req.id}/edit/{field}")
        assert resp.status_code == expected_status
