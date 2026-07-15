"""test_htmx_views.py — Comprehensive tests for app/routers/htmx_views.py.

Targets 85%+ line coverage across all route groups: full-page views,
requisitions, vendors, customers, buy-plans, quotes, search, settings,
prospecting, proactive, materials, trouble-tickets, sourcing, parts,
knowledge, and admin endpoints.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import (
    BuyPlanStatus,
    OfferStatus,
    QuoteStatus,
    RequisitionStatus,
    SourcingStatus,
    UserRole,
)
from app.models import (
    BuyPlan,
    ChangeLog,
    Company,
    CustomerSite,
    Offer,
    Quote,
    Requirement,
    Requisition,
    User,
    VendorCard,
)

# ── Helpers ───────────────────────────────────────────────────────────────


def _make_requisition(db: Session, user: User, **kw) -> Requisition:
    defaults = dict(
        name="REQ-TEST",
        customer_name="Acme",
        status=RequisitionStatus.OPEN,
        created_by=user.id,
        claimed_by_id=user.id,
        created_at=datetime.now(UTC),
    )
    defaults.update(kw)
    req = Requisition(**defaults)
    db.add(req)
    db.flush()
    return req


def _make_requirement(db: Session, req: Requisition, **kw) -> Requirement:
    defaults = dict(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=1000,
        sourcing_status=SourcingStatus.OPEN,
        created_at=datetime.now(UTC),
    )
    defaults.update(kw)
    r = Requirement(**defaults)
    db.add(r)
    db.flush()
    return r


def _make_vendor_card(db: Session, **kw) -> VendorCard:
    defaults = dict(
        normalized_name="arrow electronics",
        display_name="Arrow Electronics",
        emails=["sales@arrow.com"],
        phones=["+1-555-0100"],
        sighting_count=42,
        is_blacklisted=False,
        created_at=datetime.now(UTC),
    )
    defaults.update(kw)
    vc = VendorCard(**defaults)
    db.add(vc)
    db.flush()
    return vc


def _make_offer(db: Session, req: Requisition, user: User, **kw) -> Offer:
    defaults = dict(
        requisition_id=req.id,
        vendor_name="Arrow Electronics",
        mpn="LM317T",
        qty_available=1000,
        unit_price=0.50,
        entered_by_id=user.id,
        status=OfferStatus.ACTIVE,
        created_at=datetime.now(UTC),
    )
    defaults.update(kw)
    o = Offer(**defaults)
    db.add(o)
    db.flush()
    return o


def _make_company(db: Session, **kw) -> Company:
    defaults = dict(
        name="Acme Electronics",
        is_active=True,
        created_at=datetime.now(UTC),
    )
    defaults.update(kw)
    co = Company(**defaults)
    db.add(co)
    db.flush()
    return co


def _make_customer_site(db: Session, company: Company, **kw) -> CustomerSite:
    defaults = dict(
        company_id=company.id,
        site_name="HQ",
    )
    defaults.update(kw)
    site = CustomerSite(**defaults)
    db.add(site)
    db.flush()
    return site


def _make_quote(db: Session, req: Requisition, user: User, **kw) -> Quote:
    defaults = dict(
        requisition_id=req.id,
        quote_number=f"Q-{req.id}-1",
        status=QuoteStatus.DRAFT,
        created_by_id=user.id,
        created_at=datetime.now(UTC),
    )
    defaults.update(kw)
    q = Quote(**defaults)
    db.add(q)
    db.flush()
    return q


def _make_buy_plan(db: Session, quote: Quote, user: User, **kw) -> BuyPlan:
    defaults = dict(
        quote_id=quote.id,
        requisition_id=quote.requisition_id,
        status=BuyPlanStatus.PENDING,
        submitted_by_id=user.id,
        total_cost=500.0,
        created_at=datetime.now(UTC),
    )
    defaults.update(kw)
    bp = BuyPlan(**defaults)
    db.add(bp)
    db.flush()
    return bp


def _assert_lazy_load_targets_self(html: str, hx_get: str) -> None:
    """A lazy-load ``hx-get`` must pair with ``hx-target="this"`` so its swap does not
    inherit ``<main id="main-content" hx-target="this">`` and replace the page."""
    marker = f'hx-get="{hx_get}"'
    assert marker in html
    start = html.index(marker)
    assert 'hx-target="this"' in html[start : start + 280]


# ══════════════════════════════════════════════════════════════════════════
# Full Page Entry Points
# ══════════════════════════════════════════════════════════════════════════


class TestV2FullPages:
    """Test the multi-decorated v2_page handler for all entry URLs.

    v2_page authenticates via get_user (session), not require_user — patch it so the
    shell renders instead of the login page. Each entry URL must wire #main-content's
    lazy-load to the RIGHT module partial (base_page.html hx-get).
    """

    @pytest.mark.parametrize(
        "url, expected_partial",
        [
            ("/v2", "/v2/partials/parts/workspace"),
            ("/v2/requisitions", "/v2/partials/parts/workspace"),
            ("/v2/search", "/v2/partials/search"),
            ("/v2/vendors", "/v2/partials/vendors"),
            ("/v2/customers", "/v2/partials/customers"),
            ("/v2/buy-plans", "/v2/partials/buy-plans"),
            ("/v2/resell", "/v2/partials/resell/workspace"),
            ("/v2/quotes", "/v2/partials/parts/workspace"),  # 307 → /v2/requisitions
            ("/v2/settings", "/v2/partials/settings"),
            ("/v2/prospecting", "/v2/partials/prospecting"),
            ("/v2/proactive", "/v2/partials/proactive"),
            ("/v2/materials", "/v2/partials/materials"),
            ("/v2/follow-ups", "/v2/partials/follow-ups"),
            ("/v2/sightings", "/v2/partials/sightings/workspace"),
        ],
    )
    def test_v2_page(self, client: TestClient, test_user: User, url: str, expected_partial: str):
        with patch("app.routers.htmx_views.get_user", return_value=test_user):
            resp = client.get(url)
        assert resp.status_code == 200
        assert f'hx-get="{expected_partial}"' in resp.text

    def test_v2_page_trouble_tickets_admin_only(self, client: TestClient, db_session: Session, test_user: User):
        """/v2/trouble-tickets 403s for non-admins; admins get the workspace shell."""
        with patch("app.routers.htmx_views.get_user", return_value=test_user):
            denied = client.get("/v2/trouble-tickets")
        assert denied.status_code == 403
        test_user.role = "admin"
        db_session.commit()
        with patch("app.routers.htmx_views.get_user", return_value=test_user):
            resp = client.get("/v2/trouble-tickets")
        assert resp.status_code == 200
        assert 'hx-get="/v2/partials/trouble-tickets/workspace"' in resp.text

    def test_v2_page_unauthenticated_renders_login(self, client: TestClient):
        """Without a session user the shell route serves the login page, not the app."""
        resp = client.get("/v2")
        assert resp.status_code == 200
        assert "/auth/login" in resp.text
        assert 'hx-get="/v2/partials/parts/workspace"' not in resp.text

    def test_v2_requisitions_detail(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        with patch("app.routers.htmx_views.get_user", return_value=test_user):
            resp = client.get(f"/v2/requisitions/{req.id}")
        assert resp.status_code == 200
        assert f'hx-get="/v2/partials/requisitions/{req.id}"' in resp.text

    def test_v2_vendors_detail(self, client: TestClient, db_session: Session, test_user: User):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        with patch("app.routers.htmx_views.get_user", return_value=test_user):
            resp = client.get(f"/v2/vendors/{vc.id}")
        assert resp.status_code == 200
        assert f'hx-get="/v2/partials/vendors/{vc.id}"' in resp.text

    def test_v2_customers_detail(self, client: TestClient, db_session: Session, test_user: User):
        co = _make_company(db_session)
        db_session.commit()
        with patch("app.routers.htmx_views.get_user", return_value=test_user):
            resp = client.get(f"/v2/customers/{co.id}")
        assert resp.status_code == 200
        assert f'hx-get="/v2/partials/customers/{co.id}"' in resp.text


# ══════════════════════════════════════════════════════════════════════════
# Helper / Utility Functions
# ══════════════════════════════════════════════════════════════════════════


class TestHelperFunctions:
    """Test _parse_filter_json, _safe_int, _safe_float, _parse_date_safe."""

    def test_parse_filter_json_empty(self):
        from app.routers.htmx.materials import _parse_filter_json

        assert _parse_filter_json("") == {}
        assert _parse_filter_json(None) == {}

    def test_parse_filter_json_valid(self):
        from app.routers.htmx.materials import _parse_filter_json

        result = _parse_filter_json('{"key": "val"}')
        assert result == {"key": "val"}

    def test_parse_filter_json_invalid(self):
        from app.routers.htmx.materials import _parse_filter_json

        assert _parse_filter_json("not json") == {}

    def test_parse_filter_json_coerce_numeric(self):
        from app.routers.htmx.materials import _parse_filter_json

        result = _parse_filter_json('{"price_min": "10.5", "price_max": "bad", "name": "test"}', coerce_numeric=True)
        assert result["price_min"] == 10.5
        assert "price_max" not in result  # bad value dropped
        assert result["name"] == "test"

    def test_safe_int(self):
        from app.routers.htmx_views import _safe_int

        assert _safe_int("42") == 42
        assert _safe_int("") is None
        assert _safe_int(None) is None
        assert _safe_int("abc") is None

    def test_safe_float(self):
        from app.routers.htmx._shared import _safe_float

        assert _safe_float("3.14") == 3.14
        assert _safe_float("") is None
        assert _safe_float(None) is None
        assert _safe_float("abc") is None

    def test_parse_date_safe(self):
        from datetime import date

        from app.routers.htmx._shared import _parse_date_safe

        assert _parse_date_safe("", date) is None
        assert _parse_date_safe(None, date) is None
        assert _parse_date_safe("bad-date", date) is None
        assert _parse_date_safe("2026-03-28", date) == date(2026, 3, 28)

    def test_is_htmx(self):
        from app.routers.htmx_views import _is_htmx

        class FakeReq:
            def __init__(self, headers):
                self.headers = headers

        assert _is_htmx(FakeReq({"HX-Request": "true"})) is True
        assert _is_htmx(FakeReq({})) is False


# ══════════════════════════════════════════════════════════════════════════
# Global Search
# ══════════════════════════════════════════════════════════════════════════


class TestGlobalSearch:
    """Test the global search endpoints."""

    def test_global_search_empty(self, client: TestClient):
        """An empty query renders NO dropdown content (the outer query-length guard)."""
        resp = client.get("/v2/partials/search/global?q=")
        assert resp.status_code == 200
        assert resp.text.strip() == ""

    def test_global_search_with_query(self, client: TestClient, db_session: Session):
        _make_vendor_card(db_session, display_name="Arrow Electronics")
        _make_vendor_card(
            db_session,
            normalized_name="digikey",
            display_name="Digi-Key",
            emails=["sales@digikey.com"],
            phones=[],
        )
        db_session.commit()
        resp = client.get("/v2/partials/search/global?q=arrow")
        assert resp.status_code == 200
        assert "Arrow Electronics" in resp.text
        assert "Digi-Key" not in resp.text

    def test_ai_search_endpoint(self, client: TestClient):
        mock_result = {"best_match": None, "groups": {}, "total_count": 0}
        with patch("app.services.global_search_service.ai_search", new_callable=AsyncMock, return_value=mock_result):
            resp = client.post("/v2/partials/search/ai", data={"q": "test search"})
            assert resp.status_code == 200
            assert 'No results for "<strong class="text-gray-600">test search</strong>"' in resp.text

    def test_search_results_page_empty(self, client: TestClient):
        resp = client.get("/v2/partials/search/results?q=")
        assert resp.status_code == 200
        assert "No results found" in resp.text

    def test_search_results_page_with_query(self, client: TestClient, db_session: Session):
        _make_vendor_card(db_session, display_name="Arrow Electronics")
        db_session.commit()
        resp = client.get("/v2/partials/search/results?q=arrow")
        assert resp.status_code == 200
        assert "Arrow Electronics" in resp.text
        assert "No results found" not in resp.text

    def test_global_search_renders_material_and_sighting_groups(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """A part-number query renders the new Material Hub + Sightings groups end-to-
        end."""
        from app.models.intelligence import MaterialCard
        from app.models.sourcing import Requirement, Requisition, Sighting

        req = Requisition(name="REQ-UNI", customer_name="Acme", created_by=test_user.id)
        db_session.add(req)
        db_session.flush()
        mc = MaterialCard(normalized_mpn="uni999", display_mpn="UNI-999", manufacturer="ACME Semi")
        db_session.add(mc)
        db_session.flush()
        part = Requirement(
            requisition_id=req.id, material_card_id=mc.id, primary_mpn="UNI-999", normalized_mpn="uni999"
        )
        db_session.add(part)
        db_session.flush()
        db_session.add(
            Sighting(
                requirement_id=part.id,
                vendor_name="Distro Inc",
                vendor_name_normalized="distro inc",
                mpn_matched="UNI-999",
                normalized_mpn="uni999",
            )
        )
        db_session.commit()

        resp = client.get("/v2/partials/search/global?q=UNI-999")
        assert resp.status_code == 200
        body = resp.text
        assert "Material Hub" in body
        assert "Sightings" in body
        assert "UNI-999" in body


# ══════════════════════════════════════════════════════════════════════════
# Parts Workspace
# ══════════════════════════════════════════════════════════════════════════


class TestPartsWorkspace:
    """Test the parts workspace partial."""

    def test_workspace(self, client: TestClient):
        resp = client.get("/v2/partials/parts/workspace")
        assert resp.status_code == 200
        # Split-panel shell: Sales Hub eyebrow + lazy-loaded parts list.
        assert "Sales Hub" in resp.text
        assert 'hx-get="/v2/partials/parts"' in resp.text


# ══════════════════════════════════════════════════════════════════════════
# Requisitions
# ══════════════════════════════════════════════════════════════════════════


class TestRequisitionsListPartial:
    """Test the requisitions list partial with filters."""

    def test_list_no_filters(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get("/v2/partials/requisitions")
        assert resp.status_code == 200
        assert "REQ-TEST" in resp.text
        assert "Acme" in resp.text

    def test_list_with_search(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user, name="RFQ-ARROW-001")
        _make_requisition(db_session, test_user, name="RFQ-OTHER-002")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?q=arrow")
        assert resp.status_code == 200
        assert "RFQ-ARROW-001" in resp.text
        assert "RFQ-OTHER-002" not in resp.text

    def test_list_with_status_filter(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user, name="OPEN-REQ", status=RequisitionStatus.OPEN)
        _make_requisition(db_session, test_user, name="WON-REQ", status=RequisitionStatus.WON)
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?status=open")
        assert resp.status_code == 200
        assert "OPEN-REQ" in resp.text
        assert "WON-REQ" not in resp.text

    def test_list_with_owner_filter(self, client: TestClient, db_session: Session, test_user: User, admin_user: User):
        _make_requisition(db_session, test_user, name="MINE-REQ")
        _make_requisition(db_session, admin_user, name="THEIRS-REQ", created_by=admin_user.id, claimed_by_id=None)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions?owner={test_user.id}")
        assert resp.status_code == 200
        assert "MINE-REQ" in resp.text
        assert "THEIRS-REQ" not in resp.text

    def test_list_with_urgency_filter(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user, name="HOT-REQ", urgency="hot")
        _make_requisition(db_session, test_user, name="CALM-REQ", urgency="normal")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?urgency=hot")
        assert resp.status_code == 200
        assert "HOT-REQ" in resp.text
        assert "CALM-REQ" not in resp.text

    def test_list_with_date_filters(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user, name="IN-RANGE-REQ")
        _make_requisition(db_session, test_user, name="OLD-REQ", created_at=datetime(2019, 6, 1, tzinfo=UTC))
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?date_from=2020-01-01&date_to=2030-12-31")
        assert resp.status_code == 200
        assert "IN-RANGE-REQ" in resp.text
        assert "OLD-REQ" not in resp.text

    def test_list_with_invalid_date(self, client: TestClient, db_session: Session, test_user: User):
        """Unparseable dates degrade to no date filter — the row still renders."""
        _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?date_from=bad&date_to=bad")
        assert resp.status_code == 200
        assert "REQ-TEST" in resp.text

    def test_list_sort_by_name_asc(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user, name="AAA-REQ")
        _make_requisition(db_session, test_user, name="ZZZ-REQ")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?sort=name&dir=asc")
        assert resp.status_code == 200
        assert resp.text.index("AAA-REQ") < resp.text.index("ZZZ-REQ")

    def test_list_sort_desc(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user, name="OLDER-REQ", created_at=datetime(2024, 1, 1, tzinfo=UTC))
        _make_requisition(db_session, test_user, name="NEWER-REQ", created_at=datetime(2025, 1, 1, tzinfo=UTC))
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?sort=created_at&dir=desc")
        assert resp.status_code == 200
        assert resp.text.index("NEWER-REQ") < resp.text.index("OLDER-REQ")

    def test_list_sort_by_req_count(self, client: TestClient, db_session: Session, test_user: User):
        """Sort by parts count (correlated subquery)."""
        r1 = _make_requisition(db_session, test_user, name="FEW-PARTS")
        r2 = _make_requisition(db_session, test_user, name="MANY-PARTS")
        _make_requirement(db_session, r1)
        for i in range(3):
            _make_requirement(db_session, r2, primary_mpn=f"MPN-{i}")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?sort=req_count&dir=desc")
        assert resp.status_code == 200
        assert resp.text.index("MANY-PARTS") < resp.text.index("FEW-PARTS")


class TestRequisitionsGroupByCustomer:
    """The By-Customer nested grouping (Customer → Requisition → requirement lines),
    both-level collapse, and the Clean & reset control on the Sales Hub list."""

    def test_group_by_customer_renders_nested_tree(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user, name="ACME-REQ-1", customer_name="Acme Corp")
        _make_requirement(db_session, req, primary_mpn="GRP-LINE-001")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?group_by=customer")
        assert resp.status_code == 200
        body = resp.text
        # Customer (level 1) + requisition (level 2) + requirement-line leaf all render.
        assert "Acme Corp" in body
        assert "ACME-REQ-1" in body
        assert "GRP-LINE-001" in body
        # Both levels are collapsible against the inherited persisted map, keyed cust:/req:.
        assert 'data-gkey="cust:Acme Corp"' in body
        assert f'data-gkey="req:{req.id}"' in body
        assert "collapsed[gkey] = !collapsed[gkey]" in body

    def test_group_by_customer_splits_customers_into_separate_groups(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        r1 = _make_requisition(db_session, test_user, name="REQ-A", customer_name="Alpha Inc")
        r2 = _make_requisition(db_session, test_user, name="REQ-B", customer_name="Beta LLC")
        _make_requirement(db_session, r1)
        _make_requirement(db_session, r2, primary_mpn="BETA-1")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?group_by=customer")
        body = resp.text
        assert 'data-gkey="cust:Alpha Inc"' in body
        assert 'data-gkey="cust:Beta LLC"' in body

    def test_group_by_customer_missing_name_bucketed_as_unknown(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_requisition(db_session, test_user, name="NOCUST-REQ", customer_name=None)
        _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?group_by=customer")
        assert 'data-gkey="cust:Unknown customer"' in resp.text

    def test_persist_and_reset_wired_on_root_scope(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user)
        db_session.commit()
        body = client.get("/v2/partials/requisitions").text
        # Per-user, per-surface persisted collapse map on the list root.
        assert "$persist({}).as('saleshub-group-collapse')" in body
        # Clean & reset: full server reset + expand-all + clear selection.
        assert "Clean &amp; reset" in body
        assert "Object.keys(collapsed).forEach(k => collapsed[k] = false)" in body
        # Group-by control present.
        assert 'name="group_by"' in body
        assert ">By Customer</option>" in body

    def test_flat_view_has_no_group_keys(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get("/v2/partials/requisitions")
        assert "data-gkey" not in resp.text

    def test_grouped_view_respects_ownership_scoping(
        self, client: TestClient, db_session: Session, test_user: User, admin_user: User
    ):
        """Restricted roles group only their OWN requisitions — grouping reuses the same
        ownership-filtered query, so a foreign req never leaks into the tree."""
        _make_requisition(db_session, test_user, name="MINE-GRP", customer_name="MyCo")
        _make_requisition(db_session, admin_user, name="FOREIGN-GRP", customer_name="TheirCo")
        test_user.role = UserRole.TRADER
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?group_by=customer")
        assert resp.status_code == 200
        assert "MINE-GRP" in resp.text
        assert "FOREIGN-GRP" not in resp.text
        assert 'data-gkey="cust:TheirCo"' not in resp.text

    def test_list_sort_by_offer_count(self, client: TestClient, db_session: Session, test_user: User):
        """Sort by offers count (correlated subquery)."""
        r1 = _make_requisition(db_session, test_user, name="NO-OFFERS")
        r2 = _make_requisition(db_session, test_user, name="HAS-OFFERS")
        _make_offer(db_session, r2, test_user)
        _make_offer(db_session, r2, test_user, mpn="MPN-2")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?sort=offer_count&dir=desc")
        assert resp.status_code == 200
        assert resp.text.index("HAS-OFFERS") < resp.text.index("NO-OFFERS")

    def test_list_sort_by_deadline(self, client: TestClient, db_session: Session, test_user: User):
        """Sort by deadline — ASAP sorts before dates, NULLs last."""
        _make_requisition(db_session, test_user, name="ASAP-REQ", deadline="ASAP")
        _make_requisition(db_session, test_user, name="DATE-REQ", deadline="2026-12-31")
        _make_requisition(db_session, test_user, name="NO-DEADLINE")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?sort=deadline&dir=asc")
        assert resp.status_code == 200
        # ASAP should appear before dated deadlines, NULLs last
        assert resp.text.index("ASAP-REQ") < resp.text.index("DATE-REQ")
        assert resp.text.index("DATE-REQ") < resp.text.index("NO-DEADLINE")

    def test_list_sort_by_updated_at(self, client: TestClient, db_session: Session, test_user: User):
        """Sort by updated_at — updated rows first, NULLs sort last."""
        _make_requisition(db_session, test_user, name="UPDATED-REQ", updated_at=datetime.now(UTC))
        _make_requisition(db_session, test_user, name="NEVER-UPDATED")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?sort=updated_at&dir=desc")
        assert resp.status_code == 200
        assert resp.text.index("UPDATED-REQ") < resp.text.index("NEVER-UPDATED")

    def test_list_sort_invalid_key_falls_back(self, client: TestClient, db_session: Session, test_user: User):
        """Invalid sort key falls back to created_at without crashing."""
        _make_requisition(db_session, test_user, name="OLDER-REQ", created_at=datetime(2024, 1, 1, tzinfo=UTC))
        _make_requisition(db_session, test_user, name="NEWER-REQ", created_at=datetime(2025, 1, 1, tzinfo=UTC))
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?sort=bogus&dir=desc")
        assert resp.status_code == 200
        # Fallback ordering is created_at desc — newest row renders first.
        assert resp.text.index("NEWER-REQ") < resp.text.index("OLDER-REQ")

    def test_list_sort_invalid_dir_returns_422(self, client: TestClient, db_session: Session, test_user: User):
        """Invalid dir value is rejected by FastAPI Literal validation."""
        _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?sort=name&dir=bogus")
        assert resp.status_code == 422

    def test_list_deadline_asap_renders_amber(self, client: TestClient, db_session: Session, test_user: User):
        """ASAP deadline renders with amber styling."""
        _make_requisition(db_session, test_user, deadline="ASAP")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions")
        assert resp.status_code == 200
        assert "text-amber-600" in resp.text
        assert "ASAP" in resp.text

    def test_list_pagination(self, client: TestClient, db_session: Session, test_user: User):
        for i in range(5):
            _make_requisition(
                db_session, test_user, name=f"PAGEREQ-{i}", created_at=datetime(2025, 1, i + 1, tzinfo=UTC)
            )
        db_session.commit()
        # Default order is created_at desc → page 2 (limit=2 offset=2) is PAGEREQ-2, PAGEREQ-1.
        resp = client.get("/v2/partials/requisitions?limit=2&offset=2")
        assert resp.status_code == 200
        assert "PAGEREQ-2" in resp.text
        assert "PAGEREQ-1" in resp.text
        for skipped in ("PAGEREQ-4", "PAGEREQ-3", "PAGEREQ-0"):
            assert skipped not in resp.text

    def test_list_search_match_reason_customer(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user, name="CUSTMATCH-REQ", customer_name="Acme Corp")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?q=acme")
        assert resp.status_code == 200
        assert "CUSTMATCH-REQ" in resp.text
        # Search scope indicator counts the hit under Customers.
        assert "Matched:" in resp.text
        assert '<span class="tabular-nums">1</span> Customers' in resp.text

    def test_list_search_match_reason_part(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user, name="RFQ-99", customer_name="NotThis")
        _make_requirement(db_session, req, primary_mpn="LM317T")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?q=LM317T")
        assert resp.status_code == 200
        assert "RFQ-99" in resp.text
        assert '<span class="tabular-nums">1</span> Parts' in resp.text


class TestRequisitionCreateForm:
    """Test the create/import form endpoints (both serve the unified modal)."""

    def test_create_form(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/create-form")
        assert resp.status_code == 200
        assert "New Requisition" in resp.text
        assert 'hx-post="/v2/partials/requisitions/import-save"' in resp.text

    def test_import_form(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/import-form")
        assert resp.status_code == 200
        assert "New Requisition" in resp.text
        assert 'hx-post="/v2/partials/requisitions/import-save"' in resp.text


class TestRequisitionCreate:
    """Test creating a requisition via POST."""

    def test_create_basic(self, client: TestClient, db_session: Session, test_user: User):
        resp = client.post(
            "/v2/partials/requisitions/create",
            data={"name": "Test Req", "customer_name": "Acme", "urgency": "normal", "parts_text": ""},
        )
        assert resp.status_code == 200
        assert "Test Req" in resp.text
        created = db_session.query(Requisition).filter_by(name="Test Req").one()
        assert created.customer_name == "Acme"
        assert created.created_by == test_user.id
        assert created.status == RequisitionStatus.OPEN

    def test_create_with_parts(self, client: TestClient, db_session: Session):
        resp = client.post(
            "/v2/partials/requisitions/create",
            data={
                "name": "Test Req Parts",
                "customer_name": "Acme",
                "urgency": "normal",
                "parts_text": "LM317T, 1000\nNE555P, 500",
            },
        )
        assert resp.status_code == 200
        assert "Test Req Parts" in resp.text
        created = db_session.query(Requisition).filter_by(name="Test Req Parts").one()
        by_mpn = {r.primary_mpn: r.target_qty for r in created.requirements}
        assert by_mpn == {"LM317T": 1000, "NE555P": 500}

    def test_create_with_invalid_qty(self, client: TestClient, db_session: Session):
        """An unparseable qty degrades to 1 — the part is still created."""
        resp = client.post(
            "/v2/partials/requisitions/create",
            data={
                "name": "Test Req Bad Qty",
                "parts_text": "LM317T, notanumber",
            },
        )
        assert resp.status_code == 200
        created = db_session.query(Requisition).filter_by(name="Test Req Bad Qty").one()
        assert [(r.primary_mpn, r.target_qty) for r in created.requirements] == [("LM317T", 1)]


class TestRequisitionDetail:
    """Test requisition detail and tabs."""

    def test_detail(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}")
        assert resp.status_code == 200
        assert "REQ-TEST" in resp.text
        assert 'id="tab-content"' in resp.text
        _assert_lazy_load_targets_self(resp.text, f"/v2/partials/requisitions/{req.id}/insights")

    def test_detail_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/999999")
        assert resp.status_code == 404

    def test_tab_parts(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/parts")
        assert resp.status_code == 200
        assert 'id="parts-tbody"' in resp.text
        assert "LM317T" in resp.text

    def test_tab_offers(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_offer(db_session, req, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/offers")
        assert resp.status_code == 200
        assert "Arrow Electronics" in resp.text
        assert "LM317T" in resp.text
        assert "No offers received yet" not in resp.text

    def test_tab_offers_empty_shows_search_cta(self, client: TestClient, db_session: Session, test_user: User):
        """Empty offers tab points to sourcing (Search all sources → Parts tab) instead
        of dead-ending with a message the user can't act on."""
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/offers")
        assert resp.status_code == 200
        assert "No offers received yet" in resp.text
        assert "Search all sources" in resp.text
        assert f"/v2/partials/requisitions/{req.id}/tab/parts" in resp.text

    @pytest.mark.parametrize(
        "tab, expected_status",
        [
            ("quotes", 200),
            ("buy_plans", 200),
            ("tasks", 200),
            ("activity", 200),
            ("responses", 200),
            ("invalid_tab", 404),
        ],
    )
    def test_tab(self, client: TestClient, db_session: Session, test_user: User, tab: str, expected_status: int):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/{tab}")
        assert resp.status_code == expected_status


class TestRequisitionInlineEdit:
    """Test inline edit cell and save."""

    @pytest.mark.parametrize(
        "field, expected_status",
        [
            ("name", 200),
            ("status", 200),
            ("owner", 200),
            ("bogus", 400),
        ],
    )
    def test_edit_cell(
        self, client: TestClient, db_session: Session, test_user: User, field: str, expected_status: int
    ):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/edit/{field}")
        assert resp.status_code == expected_status

    def test_edit_cell_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/999999/edit/name")
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "field, value, context, attr, expected",
        [
            ("name", "New Name", "row", "name", "New Name"),
            ("urgency", "hot", "row", "urgency", "hot"),
            ("deadline", "2026-04-01", "row", "deadline", "2026-04-01"),
            ("deadline", "", "row", "deadline", None),  # clear deadline
            ("name", "Renamed", "header", "name", "Renamed"),
            ("name", "Renamed Tab", "tab", "name", "Renamed Tab"),
        ],
        ids=["name_row", "urgency_row", "deadline_row", "deadline_clear", "name_header", "name_tab"],
    )
    def test_inline_save(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        field: str,
        value: str,
        context: str,
        attr: str,
        expected,
    ):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/inline",
            data={"field": field, "value": value, "context": context},
        )
        assert resp.status_code == 200
        assert "showToast" in resp.headers.get("HX-Trigger", "")
        db_session.expire_all()
        assert getattr(db_session.get(Requisition, req.id), attr) == expected
        # Row/header contexts re-render the fragment; tab responds empty (trigger-only).
        if context == "tab":
            assert resp.text == ""
        elif field == "urgency":
            # Urgency renders as icon/color, not text — assert the row re-rendered.
            assert f'id="req-row-{req.id}"' in resp.text
        elif expected:
            assert str(expected) in resp.text

    def test_inline_save_owner(self, client: TestClient, db_session: Session, test_user: User, admin_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        test_user.role = UserRole.MANAGER
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/inline",
            data={"field": "owner", "value": str(admin_user.id), "context": "row"},
        )
        assert resp.status_code == 200
        assert "Owner reassigned" in resp.headers.get("HX-Trigger", "")
        db_session.expire_all()
        assert db_session.get(Requisition, req.id).created_by == admin_user.id

    def test_inline_save_status(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        with patch("app.services.requisition_state.transition") as mock_transition:
            resp = client.patch(
                f"/v2/partials/requisitions/{req.id}/inline",
                data={"field": "status", "value": "open", "context": "row"},
            )
            assert resp.status_code == 200
        mock_transition.assert_called_once()
        assert mock_transition.call_args.args[1] == "open"
        toast = json.loads(resp.headers["HX-Trigger"])["showToast"]["message"]
        assert toast == "Status → open"
        assert "REQ-TEST" in resp.text  # re-rendered row

    def test_inline_save_not_found(self, client: TestClient):
        resp = client.patch(
            "/v2/partials/requisitions/999999/inline",
            data={"field": "name", "value": "X", "context": "row"},
        )
        assert resp.status_code == 404


class TestRequisitionRowActions:
    """Test row-level actions (claim, unclaim, won, lost, clone)."""

    def test_action_claim(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user, claimed_by_id=None)
        db_session.commit()
        resp = client.post(f"/v2/partials/requisitions/{req.id}/action/claim", data={})
        assert resp.status_code == 200
        toast = json.loads(resp.headers["HX-Trigger"])["showToast"]["message"]
        assert toast == "Claimed 'REQ-TEST'"
        db_session.expire_all()
        assert db_session.get(Requisition, req.id).claimed_by_id == test_user.id

    def test_action_unclaim(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(f"/v2/partials/requisitions/{req.id}/action/unclaim", data={})
        assert resp.status_code == 200
        toast = json.loads(resp.headers["HX-Trigger"])["showToast"]["message"]
        assert toast == "Unclaimed 'REQ-TEST'"
        db_session.expire_all()
        assert db_session.get(Requisition, req.id).claimed_by_id is None

    def test_action_clone(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(f"/v2/partials/requisitions/{req.id}/action/clone", data={})
        assert resp.status_code == 200
        clones = db_session.query(Requisition).filter(Requisition.id != req.id).all()
        assert len(clones) == 1
        toast = json.loads(resp.headers["HX-Trigger"])["showToast"]["message"]
        assert toast == f"Cloned → REQ-{clones[0].id:03d}"

    def test_action_invalid(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(f"/v2/partials/requisitions/{req.id}/action/invalid", data={})
        assert resp.status_code == 400

    def test_action_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/requisitions/999999/action/claim", data={})
        assert resp.status_code == 404

    def test_action_return_format_detail(self, client: TestClient, db_session: Session, test_user: User):
        """Return=detail responds with an empty body — only the toast trigger fires."""
        req = _make_requisition(db_session, test_user, claimed_by_id=None)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/action/claim",
            data={"return": "detail"},
        )
        assert resp.status_code == 200
        assert resp.text == ""
        toast = json.loads(resp.headers["HX-Trigger"])["showToast"]["message"]
        assert toast == "Claimed 'REQ-TEST'"
        db_session.expire_all()
        assert db_session.get(Requisition, req.id).claimed_by_id == test_user.id


class TestRequisitionBulkActions:
    """Test bulk actions on requisitions."""

    def test_bulk_assign(self, client: TestClient, db_session: Session, test_user: User, admin_user: User):
        r1 = _make_requisition(db_session, test_user)
        db_session.commit()
        test_user.role = UserRole.MANAGER
        db_session.commit()
        resp = client.post(
            "/v2/partials/requisitions/bulk/assign",
            data={"ids": str(r1.id), "owner_id": str(admin_user.id)},
        )
        assert resp.status_code == 200
        assert "REQ-TEST" in resp.text  # refreshed list renders the row
        db_session.expire_all()
        assert db_session.get(Requisition, r1.id).created_by == admin_user.id

    def test_bulk_no_ids(self, client: TestClient):
        resp = client.post("/v2/partials/requisitions/bulk/assign", data={"ids": ""})
        assert resp.status_code == 400

    def test_bulk_invalid_ids(self, client: TestClient):
        resp = client.post("/v2/partials/requisitions/bulk/assign", data={"ids": "abc,def"})
        assert resp.status_code == 400

    def test_bulk_invalid_action(self, client: TestClient):
        # "archive" is no longer a valid bulk action (requisition archiving removed).
        resp = client.post("/v2/partials/requisitions/bulk/archive", data={"ids": "1"})
        assert resp.status_code == 400

    def test_bulk_too_many(self, client: TestClient):
        ids = ",".join(str(i) for i in range(201))
        resp = client.post("/v2/partials/requisitions/bulk/assign", data={"ids": ids})
        assert resp.status_code == 400


class TestAddRequirement:
    """Test adding a requirement to a requisition."""

    def test_add_requirement_success(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/requirements",
            data={
                "primary_mpn": "NE555P",
                "manufacturer": "Texas Instruments",
                "target_qty": "100",
            },
        )
        assert resp.status_code == 200
        assert "<tr" in resp.text
        assert "NE555P" in resp.text
        created = db_session.query(Requirement).filter_by(requisition_id=req.id).one()
        assert created.primary_mpn == "NE555P"
        assert created.manufacturer == "Texas Instruments"
        assert created.target_qty == 100

    def test_add_requirement_missing_manufacturer(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/requirements",
            data={"primary_mpn": "NE555P", "manufacturer": "", "target_qty": "100"},
        )
        assert resp.status_code == 422

    def test_add_requirement_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/999999/requirements",
            data={"primary_mpn": "NE555P", "manufacturer": "TI", "target_qty": "100"},
        )
        assert resp.status_code == 404

    def test_zero_parts_tab_renders_stable_tbody_target(self, client: TestClient, db_session: Session, test_user: User):
        """REQ-02 regression: the Add Requirement form targets hx-target="#parts-tbody"
        (unchanged). On a zero-parts requisition the tbody must still be present in
        the served DOM or htmx aborts with targetError and the form is dead — verify
        the tab always renders a real #parts-tbody, even with no requirements."""
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/parts")
        assert resp.status_code == 200
        assert 'id="parts-tbody"' in resp.text
        assert 'hx-target="#parts-tbody"' in resp.text

    def test_zero_parts_tab_add_requirement_end_to_end(self, client: TestClient, db_session: Session, test_user: User):
        """REQ-02: Add Requirement must actually work starting from a zero-parts
        requisition (the form's hx-target now resolves, so the POST completes and
        returns a swappable <tr> fragment for #parts-tbody)."""
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        tab_resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/parts")
        assert 'id="parts-tbody"' in tab_resp.text

        add_resp = client.post(
            f"/v2/partials/requisitions/{req.id}/requirements",
            data={"primary_mpn": "NE555P", "manufacturer": "Texas Instruments", "target_qty": "100"},
        )
        assert add_resp.status_code == 200
        assert "<tr" in add_resp.text
        assert "NE555P" in add_resp.text

    def test_has_parts_tab_still_renders_tbody(self, client: TestClient, db_session: Session, test_user: User):
        """Regression: the has-parts case must keep rendering #parts-tbody with its
        rows (not just the zero-parts placeholder)."""
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req, primary_mpn="LM317T")
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/parts")
        assert resp.status_code == 200
        assert 'id="parts-tbody"' in resp.text
        assert "LM317T" in resp.text
        assert 'id="parts-empty-state"' not in resp.text


class TestSearchAll:
    """Test search-all requirements in a requisition."""

    def test_search_all(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req)
        db_session.commit()
        resp = client.post(f"/v2/partials/requisitions/{req.id}/search-all")
        assert resp.status_code == 200
        # Re-rendered parts tab with the auto-refresh banner + the part row.
        assert "Searching all sources" in resp.text
        assert "LM317T" in resp.text

    def test_search_all_no_requirements(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(f"/v2/partials/requisitions/{req.id}/search-all")
        assert resp.status_code == 200
        assert "No requirements to search" in resp.text

    def test_search_all_button_targets_tab_content(self, client: TestClient, db_session: Session, test_user: User):
        """REQ-05 regression: the toolbar 'Search All Sources' button used to target
        hx-target="closest div" (the toolbar row), so the full-tab response it gets
        back nested a duplicate table inside the toolbar. It must target the stable
        #tab-content ancestor, matching detail_header.html's working wiring."""
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/parts")
        assert resp.status_code == 200
        assert 'hx-target="closest div"' not in resp.text
        assert f'hx-post="/v2/partials/requisitions/{req.id}/search-all"' in resp.text
        assert 'hx-target="#tab-content"' in resp.text

    def test_search_all_refresh_banner_targets_tab_content(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """REQ-05 regression: the 8s auto-refresh banner shown after triggering a
        search used to target hx-target="closest div" (the banner itself), nesting
        a duplicate table inside the banner. It must target #tab-content."""
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req)
        db_session.commit()
        resp = client.post(f"/v2/partials/requisitions/{req.id}/search-all")
        assert resp.status_code == 200
        assert "Searching all sources" in resp.text
        assert 'hx-target="closest div"' not in resp.text
        # Both the toolbar button and the refresh banner must now target #tab-content.
        assert resp.text.count('hx-target="#tab-content"') == 2


class TestRequisitionImport:
    """Test AI import parse and save."""

    def test_import_parse_no_data(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/import-parse",
            data={"name": "Test", "raw_text": ""},
        )
        assert resp.status_code == 200
        assert "No data" in resp.text

    def test_import_parse_json_mode_no_data(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/import-parse?format=json",
            data={"name": "Test", "raw_text": ""},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["error"] == "No data provided"

    def test_import_parse_with_text(self, client: TestClient):
        mock_result = {"requirements": [{"primary_mpn": "LM317T", "target_qty": 100}], "name": "AI Name"}
        with patch(
            "app.routers.htmx.requisitions.parse_freeform_rfq", new_callable=AsyncMock, return_value=mock_result
        ) as mock_parse:
            resp = client.post(
                "/v2/partials/requisitions/import-parse",
                data={"name": "Import", "raw_text": "LM317T 100pcs"},
                files={"file": ("", b"", "application/octet-stream")},
            )
            assert resp.status_code == 200
            # The AI parser is invoked with the pasted text and the modal re-renders.
            mock_parse.assert_awaited_once_with("LM317T 100pcs")
            assert "New Requisition" in resp.text

    def test_import_parse_json_mode(self, client: TestClient):
        mock_result = {
            "requirements": [{"primary_mpn": "LM317T", "target_qty": 100}],
            "name": "AI Name",
            "customer_name": "AI Customer",
        }
        with patch(
            "app.routers.htmx.requisitions.parse_freeform_rfq", new_callable=AsyncMock, return_value=mock_result
        ):
            resp = client.post(
                "/v2/partials/requisitions/import-parse?format=json",
                data={"name": "Import", "raw_text": "LM317T 100pcs"},
                files={"file": ("", b"", "application/octet-stream")},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "requirements" in data

    def test_import_save_no_parts(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/import-save",
            data={"name": "Test"},
        )
        assert resp.status_code == 200
        assert "No valid parts" in resp.text

    def test_import_save_with_parts(self, client: TestClient, db_session: Session):
        resp = client.post(
            "/v2/partials/requisitions/import-save",
            data={
                "name": "Import Test",
                "customer_name": "Acme",
                "customer_site_id": "",
                "deadline": "",
                "urgency": "normal",
                "reqs[0].primary_mpn": "LM317T",
                "reqs[0].target_qty": "100",
                "reqs[0].brand": "",
                "reqs[0].target_price": "",
                "reqs[0].condition": "new",
                "reqs[0].customer_pn": "",
                "reqs[0].date_codes": "",
                "reqs[0].packaging": "",
                "reqs[0].manufacturer": "TI",
                "reqs[0].substitutes": "",
                "reqs[0].firmware": "",
                "reqs[0].hardware_codes": "",
                "reqs[0].description": "",
                "reqs[0].package_type": "",
                "reqs[0].revision": "",
                "reqs[0].need_by_date": "",
                "reqs[0].sale_notes": "",
            },
        )
        assert resp.status_code == 200
        # Success snippet closes the modal + toasts, and triggers a list refresh.
        assert "Requisition created with 1 parts" in resp.text
        assert resp.headers.get("HX-Trigger") == "reqListRefresh"
        created = db_session.query(Requisition).filter_by(name="Import Test").one()
        assert created.customer_name == "Acme"
        line = db_session.query(Requirement).filter_by(requisition_id=created.id).one()
        assert (line.primary_mpn, line.target_qty, line.manufacturer) == ("LM317T", 100, "TI")


class TestParseEmailOffer:
    """Test email/offer parsing endpoints."""

    def test_parse_email_form(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/parse-email-form")
        assert resp.status_code == 200
        assert "Parse Vendor Email" in resp.text
        assert f'hx-post="/v2/partials/requisitions/{req.id}/parse-email"' in resp.text

    def test_paste_offer_form(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/paste-offer-form")
        assert resp.status_code == 200
        assert "Paste Vendor Offer" in resp.text
        assert f'hx-post="/v2/partials/requisitions/{req.id}/parse-offer"' in resp.text

    def test_parse_email_empty_body(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/parse-email",
            data={"email_body": "", "vendor_name": "Arrow"},
        )
        assert resp.status_code == 200
        assert "paste the email" in resp.text.lower()

    def test_parse_email_success(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        mock_result = {
            "quotes": [{"part_number": "LM317T", "quantity_available": 100, "unit_price": 0.5, "confidence": 0.9}],
            "overall_confidence": 0.95,
            "email_type": "quote",
        }
        with patch("app.services.ai_email_parser.parse_email", new_callable=AsyncMock, return_value=mock_result):
            resp = client.post(
                f"/v2/partials/requisitions/{req.id}/parse-email",
                data={"email_body": "We can offer LM317T at $0.50", "vendor_name": "Arrow"},
            )
            assert resp.status_code == 200
            assert "Parsed 1 offer from Quote email" in resp.text
            assert "95% confidence" in resp.text
            assert 'value="LM317T"' in resp.text  # editable card prefilled

    def test_parse_email_no_result(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        with patch("app.services.ai_email_parser.parse_email", new_callable=AsyncMock, return_value=None):
            resp = client.post(
                f"/v2/partials/requisitions/{req.id}/parse-email",
                data={"email_body": "Hello", "vendor_name": "Arrow"},
            )
            assert resp.status_code == 200
            # A None parse renders the zero-offers state, not an error.
            assert "Parsed 0 offers from Unclear email" in resp.text
            assert "0% confidence" in resp.text

    def test_parse_email_exception(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        with patch(
            "app.services.ai_email_parser.parse_email", new_callable=AsyncMock, side_effect=Exception("AI error")
        ):
            resp = client.post(
                f"/v2/partials/requisitions/{req.id}/parse-email",
                data={"email_body": "Hello", "vendor_name": "Arrow"},
            )
            assert resp.status_code == 200
            assert "Parse failed" in resp.text

    def test_parse_offer_empty(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/parse-offer",
            data={"raw_text": ""},
        )
        assert resp.status_code == 200
        assert "paste vendor text" in resp.text.lower()

    def test_parse_offer_success(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req)
        db_session.commit()
        mock_result = {"offers": [{"mpn": "LM317T", "qty_available": 100, "unit_price": 0.5, "vendor_name": "Arrow"}]}
        with patch(
            "app.services.freeform_parser_service.parse_freeform_offer",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            resp = client.post(
                f"/v2/partials/requisitions/{req.id}/parse-offer",
                data={"raw_text": "LM317T 100pcs $0.50"},
            )
            assert resp.status_code == 200
            assert "Parsed 1 offer from pasted text" in resp.text
            assert "LM317T" in resp.text
            assert f'hx-post="/v2/partials/requisitions/{req.id}/save-parsed-offers"' in resp.text

    def test_parse_offer_exception(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        with patch(
            "app.services.freeform_parser_service.parse_freeform_offer",
            new_callable=AsyncMock,
            side_effect=Exception("fail"),
        ):
            resp = client.post(
                f"/v2/partials/requisitions/{req.id}/parse-offer",
                data={"raw_text": "LM317T 100pcs"},
            )
            assert resp.status_code == 200
            assert "Parse failed" in resp.text


class TestSaveParsedOffers:
    """Test saving parsed offers."""

    def test_save_parsed_offers(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req, primary_mpn="LM317T")
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/save-parsed-offers",
            data={
                "vendor_name": "Arrow",
                "offers[0].mpn": "LM317T",
                "offers[0].vendor_name": "Arrow",
                "offers[0].qty_available": "1000",
                "offers[0].unit_price": "0.50",
                "offers[0].condition": "new",
            },
        )
        assert resp.status_code == 200
        assert "1 offer saved to this requisition." in resp.text
        saved = db_session.query(Offer).filter_by(requisition_id=req.id).one()
        assert (saved.vendor_name, saved.mpn) == ("Arrow", "LM317T")
        assert saved.qty_available == 1000
        assert saved.unit_price == 0.50

    def test_save_parsed_offers_no_data(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/save-parsed-offers",
            data={},
        )
        assert resp.status_code == 200
        assert "No offers to save" in resp.text


# ══════════════════════════════════════════════════════════════════════════
# Quote Endpoints
# ══════════════════════════════════════════════════════════════════════════


class TestCreateQuoteFromOffers:
    """Test creating a quote from offers."""

    def test_create_quote_no_offers(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/create-quote",
            data={},
        )
        assert resp.status_code == 400

    def test_create_quote_success(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/create-quote",
            data={"offer_ids": str(offer.id)},
        )
        assert resp.status_code == 200
        quote = db_session.query(Quote).filter_by(requisition_id=req.id).one()
        assert quote.quote_number == f"Q-{req.id}-1"
        assert quote.created_by_id == test_user.id
        assert quote.line_items and quote.line_items[0]["mpn"] == "LM317T"
        assert quote.quote_number in resp.text  # quote detail rendered


class TestDeleteRequirement:
    """Test deleting a requirement."""

    def test_delete_requirement(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.delete(f"/v2/partials/requisitions/{req.id}/requirements/{item.id}")
        assert resp.status_code == 200
        assert resp.text == ""  # htmx removes the row via empty swap
        db_session.expire_all()
        assert db_session.get(Requirement, item.id) is None


# ══════════════════════════════════════════════════════════════════════════
# Vendor Endpoints
# ══════════════════════════════════════════════════════════════════════════


class TestVendorsList:
    """Test the vendors list partial."""

    def test_list(self, client: TestClient, db_session: Session):
        _make_vendor_card(db_session)
        db_session.commit()
        resp = client.get("/v2/partials/vendors")
        assert resp.status_code == 200
        assert "Arrow Electronics" in resp.text

    def test_list_with_search(self, client: TestClient, db_session: Session):
        _make_vendor_card(db_session, display_name="Arrow Electronics")
        _make_vendor_card(db_session, normalized_name="digikey", display_name="Digi-Key")
        db_session.commit()
        resp = client.get("/v2/partials/vendors?q=arrow")
        assert resp.status_code == 200
        assert "Arrow Electronics" in resp.text
        assert "Digi-Key" not in resp.text

    def test_list_show_blacklisted(self, client: TestClient, db_session: Session):
        _make_vendor_card(db_session, display_name="Bad Vendor Inc", is_blacklisted=True)
        db_session.commit()
        hidden = client.get("/v2/partials/vendors")
        shown = client.get("/v2/partials/vendors?hide_blacklisted=false")
        assert shown.status_code == 200
        assert "Bad Vendor Inc" in shown.text
        assert "Bad Vendor Inc" not in hidden.text

    def test_list_sort_by_name(self, client: TestClient, db_session: Session):
        _make_vendor_card(db_session, normalized_name="zeta", display_name="Zeta Components")
        _make_vendor_card(db_session, normalized_name="alpha", display_name="Alpha Parts")
        db_session.commit()
        resp = client.get("/v2/partials/vendors?sort=display_name&dir=asc")
        assert resp.status_code == 200
        assert resp.text.index("Alpha Parts") < resp.text.index("Zeta Components")


class TestVendorDetail:
    """Test vendor detail."""

    def test_detail(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}")
        assert resp.status_code == 200
        assert "Arrow Electronics" in resp.text
        _assert_lazy_load_targets_self(resp.text, f"/v2/partials/vendors/{vc.id}/insights")

    def test_detail_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/vendors/999999")
        assert resp.status_code == 404


class TestVendorTabs:
    """Test vendor tab endpoints."""

    @pytest.mark.parametrize(
        "tab, expected_status",
        [
            ("contacts", 200),
            ("overview", 200),
            ("offers", 200),
            ("bogus", 404),
        ],
    )
    def test_tab(self, client: TestClient, db_session: Session, tab: str, expected_status: int):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}/tab/{tab}")
        assert resp.status_code == expected_status


class TestVendorEdit:
    """Test vendor edit form and save."""

    def test_edit_form(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}/edit-form")
        assert resp.status_code == 200
        assert "Arrow Electronics" in resp.text  # prefilled current name
        assert f"/v2/partials/vendors/{vc.id}/edit" in resp.text

    def test_edit_save(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/vendors/{vc.id}/edit",
            data={"display_name": "Updated Name", "website": "https://new.com"},
        )
        assert resp.status_code == 200
        assert "Updated Name" in resp.text  # refreshed detail
        db_session.expire_all()
        vendor = db_session.get(VendorCard, vc.id)
        assert vendor.display_name == "Updated Name"
        assert vendor.website == "https://new.com"

    def test_toggle_blacklist(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session, is_blacklisted=False)
        db_session.commit()
        resp = client.post(f"/v2/partials/vendors/{vc.id}/toggle-blacklist")
        assert resp.status_code == 200
        assert "Arrow Electronics" in resp.text  # refreshed detail
        db_session.expire_all()
        assert db_session.get(VendorCard, vc.id).is_blacklisted is True


# ══════════════════════════════════════════════════════════════════════════
# Customer Endpoints
# ══════════════════════════════════════════════════════════════════════════


class TestCompaniesRedirect:
    """Test legacy /v2/companies redirect to /v2/customers."""

    def test_companies_redirect(self, client: TestClient):
        resp = client.get("/v2/companies", follow_redirects=False)
        assert resp.status_code == 301

    def test_companies_redirect_with_path(self, client: TestClient):
        resp = client.get("/v2/companies/123", follow_redirects=False)
        assert resp.status_code == 301

    def test_partials_companies_redirect(self, client: TestClient):
        resp = client.get("/v2/partials/companies", follow_redirects=False)
        assert resp.status_code == 301


class TestCustomersList:
    """Test customers list partial."""

    def test_list(self, client: TestClient, db_session: Session):
        _make_company(db_session)
        db_session.commit()
        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200
        assert "Acme Electronics" in resp.text

    def test_list_with_search(self, client: TestClient, db_session: Session):
        _make_company(db_session, name="Acme Electronics")
        _make_company(db_session, name="Beta Industrial")
        db_session.commit()
        resp = client.get("/v2/partials/customers?search=acme")
        assert resp.status_code == 200
        assert "Acme Electronics" in resp.text
        assert "Beta Industrial" not in resp.text


@pytest.fixture()
def _grant_account_management(test_user: User, db_session: Session) -> None:
    """Promote the buyer ``test_user`` to MANAGER so it can_manage every account.

    Company detail + tab partials (``GET /v2/partials/customers/{id}`` and
    ``.../tab/{tab}``) now gate on ``can_manage_account``. The class below GETs those
    endpoints as ``test_user`` on companies it creates without assigning ownership, so
    promote the actor to MANAGER (``can_manage_account`` is True for managers, exactly as
    for the account owner) to exercise the authorized render path. Applied per-class via
    ``@pytest.mark.usefixtures`` — scoped narrowly so role-based list tests are untouched.
    """
    test_user.role = "manager"
    db_session.commit()


@pytest.mark.usefixtures("_grant_account_management")
class TestCustomerDetail:
    """Test customer detail and tabs."""

    def test_detail(self, client: TestClient, db_session: Session):
        co = _make_company(db_session)
        _make_customer_site(db_session, co)
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200
        assert "Acme Electronics" in resp.text
        _assert_lazy_load_targets_self(resp.text, f"/v2/partials/customers/{co.id}/insights")

    def test_detail_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/customers/999999")
        assert resp.status_code == 404


class TestCustomerCRUD:
    """Test create company, typeahead, duplicate check."""

    def test_create_form(self, client: TestClient):
        resp = client.get("/v2/partials/customers/create-form")
        assert resp.status_code == 200
        assert "Create Company" in resp.text
        assert 'hx-post="/v2/partials/customers/create"' in resp.text

    def test_create_company(self, client: TestClient, db_session: Session):
        resp = client.post(
            "/v2/partials/customers/create",
            data={"name": "New Corp", "website": "https://newcorp.com"},
        )
        assert resp.status_code == 200
        assert "New Corp" in resp.text  # detail panel for the new account
        assert resp.headers.get("HX-Trigger") == "cdmListRefresh"
        created = db_session.query(Company).filter_by(name="New Corp").one()
        assert created.website == "https://newcorp.com"
        # A default HQ site is auto-created alongside the company.
        site = db_session.query(CustomerSite).filter_by(company_id=created.id).one()
        assert site.site_name == "HQ"

    def test_create_company_no_name(self, client: TestClient):
        resp = client.post("/v2/partials/customers/create", data={"name": ""})
        assert resp.status_code == 400

    def test_create_company_duplicate(self, client: TestClient, db_session: Session):
        _make_company(db_session, name="Dupe Corp")
        db_session.commit()
        resp = client.post("/v2/partials/customers/create", data={"name": "Dupe Corp"})
        assert resp.status_code == 409

    def test_typeahead_short_query(self, client: TestClient):
        resp = client.get("/v2/partials/customers/typeahead?q=a")
        assert resp.status_code == 200
        assert resp.text == ""

    def test_typeahead_valid(self, client: TestClient, db_session: Session):
        co = _make_company(db_session, name="Acme Corp")
        _make_company(db_session, name="Unrelated Ltd")
        db_session.commit()
        resp = client.get("/v2/partials/customers/typeahead?q=acme")
        assert resp.status_code == 200
        assert f'<option value="{co.id}">Acme Corp</option>' in resp.text
        assert "Unrelated Ltd" not in resp.text

    def test_check_duplicate(self, client: TestClient, db_session: Session):
        co = _make_company(db_session, name="Dup Check Inc")
        db_session.commit()
        resp = client.get("/v2/partials/customers/check-duplicate?name=Dup+Check+Inc")
        assert resp.status_code == 200
        assert f'A company named "Dup Check Inc" already exists (ID {co.id})' in resp.text


class TestCustomerQuickCreate:
    """Test quick-create from AI lookup."""

    def test_quick_create_new(self, client: TestClient):
        resp = client.post(
            "/v2/partials/customers/quick-create",
            data={"company_name": "Quick Corp", "website": "https://quick.com", "city": "Austin"},
        )
        assert resp.status_code == 200
        assert "Created" in resp.text

    def test_quick_create_duplicate(self, client: TestClient, db_session: Session):
        co = _make_company(db_session, name="Existing Corp")
        _make_customer_site(db_session, co)
        db_session.commit()
        resp = client.post(
            "/v2/partials/customers/quick-create",
            data={"company_name": "Existing Corp"},
        )
        assert resp.status_code == 200
        assert "already exists" in resp.text


class TestCustomerLookup:
    """Test AI customer lookup."""

    def test_lookup_success(self, client: TestClient):
        mock_result = {
            "company_name": "Acme",
            "website": "https://acme.com",
            "phone": "555-1234",
            "address_line1": "123 Main",
            "city": "Austin",
            "state": "TX",
            "zip": "78701",
            "country": "US",
        }
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_result):
            resp = client.post(
                "/v2/partials/customers/lookup",
                data={"company_name": "Acme", "location": "Austin TX"},
            )
            assert resp.status_code == 200
            assert "Acme" in resp.text

    def test_lookup_failure(self, client: TestClient):
        from app.utils.claude_errors import ClaudeUnavailableError

        with patch(
            "app.utils.claude_client.claude_json", new_callable=AsyncMock, side_effect=ClaudeUnavailableError("down")
        ):
            resp = client.post(
                "/v2/partials/customers/lookup",
                data={"company_name": "Acme", "location": ""},
            )
            assert resp.status_code == 200
            assert "Could not look up" in resp.text

    def test_lookup_no_result(self, client: TestClient):
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=None):
            resp = client.post(
                "/v2/partials/customers/lookup",
                data={"company_name": "Acme", "location": ""},
            )
            assert resp.status_code == 200
            assert "Could not look up" in resp.text


# ══════════════════════════════════════════════════════════════════════════
# Settings Endpoints
# ══════════════════════════════════════════════════════════════════════════


class TestSettings:
    """Test settings partials."""

    def test_settings_index(self, client: TestClient):
        """A buyer lacks MANAGE_CONNECTORS — the default tab falls back to Profile."""
        resp = client.get("/v2/partials/settings")
        assert resp.status_code == 200
        assert "x-data=\"{ tab: 'profile' }\"" in resp.text
        assert 'hx-get="/v2/partials/settings/profile"' in resp.text

    def test_settings_sources(self, client: TestClient):
        # Sources tab retired → unified Connectors tab; old URL 302-redirects.
        resp = client.get("/v2/partials/settings/sources", follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert "/connectors" in resp.headers["location"]

    def test_settings_system_admin(self, client: TestClient, test_user: User, db_session: Session):
        test_user.role = "admin"
        db_session.commit()
        with patch("app.services.admin_service.get_all_config", return_value={}):
            resp = client.get("/v2/partials/settings/system")
            assert resp.status_code == 200
            assert "System Settings" in resp.text

    def test_settings_profile(self, client: TestClient, test_user: User):
        resp = client.get("/v2/partials/settings/profile")
        assert resp.status_code == 200
        assert "Test Buyer" in resp.text
        assert test_user.email in resp.text

    def test_toggle_8x8(self, client: TestClient, db_session: Session, test_user: User):
        assert not test_user.eight_by_eight_enabled
        resp = client.post("/api/user/toggle-8x8")
        assert resp.status_code == 200
        assert resp.headers["HX-Trigger"] == '{"showToast": "8x8 click-to-call enabled"}'
        db_session.expire_all()
        assert db_session.get(User, test_user.id).eight_by_eight_enabled is True

    def test_settings_data_ops(self, client: TestClient, test_user: User, db_session: Session):
        test_user.role = "admin"
        db_session.commit()
        with patch("app.vendor_utils.find_vendor_dedup_candidates", return_value=[]):
            with patch("app.company_utils.find_company_dedup_candidates", return_value=[]):
                resp = client.get("/v2/partials/settings/data-ops")
                assert resp.status_code == 200
                # Empty scans render the clean-dataset empty states, not error blocks.
                assert "No duplicate vendors found at the current threshold." in resp.text


# ══════════════════════════════════════════════════════════════════════════
# Buy Plans
# ══════════════════════════════════════════════════════════════════════════


class TestBuyPlans:
    """Test buy plan list and detail partials."""

    @pytest.mark.parametrize(
        "query",
        ["", "?status=pending", "?mine=true"],
        ids=["all", "status", "mine"],
    )
    def test_list(self, client: TestClient, query: str):
        resp = client.get(f"/v2/partials/buy-plans{query}")
        assert resp.status_code == 200
        # Hub shell: both lens tabs render and the body lazy-loads into #bp-hub-body.
        assert "My Queue" in resp.text
        assert "Pipeline" in resp.text
        assert 'hx-target="#bp-hub-body"' in resp.text


# ══════════════════════════════════════════════════════════════════════════
# Quotes (standalone list retired; see test_quotes_relocation.py)
# ══════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════
# Offer Endpoints
# ══════════════════════════════════════════════════════════════════════════


class TestOfferEndpoints:
    """Test add, edit, delete, review offer endpoints."""

    def test_add_offer_form(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req, primary_mpn="LM317T")
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/add-offer-form")
        assert resp.status_code == 200
        assert f'hx-post="/v2/partials/requisitions/{req.id}/add-offer"' in resp.text
        assert "LM317T" in resp.text  # requirement selectable on the form

    def test_add_offer(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req, primary_mpn="LM317T")
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offer",
            data={
                "vendor_name": "Arrow",
                "mpn": "LM317T",
                "qty_available": "500",
                "unit_price": "0.55",
            },
        )
        assert resp.status_code == 200
        assert "Arrow" in resp.text  # refreshed offers tab shows the new offer
        offer = db_session.query(Offer).filter_by(requisition_id=req.id).one()
        assert (offer.vendor_name, offer.mpn) == ("Arrow", "LM317T")
        assert offer.qty_available == 500
        assert float(offer.unit_price) == 0.55
        assert offer.source == "manual"
        assert offer.entered_by_id == test_user.id

    def test_edit_offer_form(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/edit-form")
        assert resp.status_code == 200
        assert "Arrow Electronics" in resp.text  # prefilled with current values
        assert f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/edit" in resp.text

    def test_edit_offer(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/edit",
            data={"vendor_name": "Arrow", "mpn": "LM317T", "qty_available": "999", "unit_price": "0.60"},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        edited = db_session.get(Offer, offer.id)
        assert edited.qty_available == 999
        assert float(edited.unit_price) == 0.60
        assert edited.updated_by_id == test_user.id
        # Field-level audit trail is written for each change.
        changed_fields = {
            c.field_name for c in db_session.query(ChangeLog).filter_by(entity_type="offer", entity_id=offer.id)
        }
        assert {"qty_available", "unit_price"} <= changed_fields

    def test_delete_offer(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        db_session.commit()
        resp = client.delete(f"/v2/partials/requisitions/{req.id}/offers/{offer.id}")
        assert resp.status_code == 200
        assert "No offers received yet" in resp.text  # tab re-renders empty
        db_session.expire_all()
        assert db_session.get(Offer, offer.id) is None

    def test_review_offer(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user, status=OfferStatus.PENDING_REVIEW)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/review",
            data={"action": "approve"},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        approved = db_session.get(Offer, offer.id)
        assert approved.status == OfferStatus.APPROVED
        assert approved.approved_by_id == test_user.id
        assert approved.approved_at is not None

    def test_reconfirm_offer(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        db_session.commit()
        resp = client.post(f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/reconfirm")
        assert resp.status_code == 200
        db_session.expire_all()
        reconfirmed = db_session.get(Offer, offer.id)
        assert reconfirmed.reconfirm_count == 1
        assert reconfirmed.reconfirmed_at is not None
        assert reconfirmed.expires_at is not None
        assert reconfirmed.is_stale is False

    def test_mark_offer_sold(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/mark-sold",
            data={},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.get(Offer, offer.id).status == OfferStatus.SOLD
        log = db_session.query(ChangeLog).filter_by(entity_type="offer", entity_id=offer.id, field_name="status").one()
        assert log.new_value == "sold"


# ══════════════════════════════════════════════════════════════════════════
# Materials
# ══════════════════════════════════════════════════════════════════════════


class TestMaterials:
    """Test materials workspace and list."""

    def test_materials_workspace(self, client: TestClient):
        resp = client.get("/v2/partials/materials/workspace")
        assert resp.status_code == 200
        assert 'id="materials-workspace"' in resp.text
        assert "All Materials" in resp.text

    def test_materials_faceted(self, client: TestClient):
        with patch("app.services.faceted_search_service.search_materials_faceted", return_value=([], 0)):
            with patch("app.services.faceted_search_service.get_facet_counts", return_value={}):
                resp = client.get("/v2/partials/materials/faceted")
                assert resp.status_code == 200
                # Result-count strip renders the (mocked) zero total.
                assert '<span class="text-sm font-semibold text-gray-700 tabular-nums">0</span>' in resp.text
                assert "results" in resp.text


# ══════════════════════════════════════════════════════════════════════════
# Trouble Tickets
# ══════════════════════════════════════════════════════════════════════════


class TestTroubleTickets:
    """Test trouble tickets workspace and list."""

    def test_workspace(self, client: TestClient):
        resp = client.get("/v2/partials/trouble-tickets/workspace")
        assert resp.status_code == 200
        assert 'hx-post="/api/trouble-tickets/analyze"' in resp.text
        assert "Analyze" in resp.text

    @pytest.mark.parametrize("query", ["", "?status=open"], ids=["all", "status"])
    def test_list(self, client: TestClient, db_session: Session, test_user: User, query: str):
        from app.constants import TicketSource, TicketStatus
        from app.models.trouble_ticket import TroubleTicket

        db_session.add(
            TroubleTicket(
                ticket_number="TT-0001",
                submitted_by=test_user.id,
                status=TicketStatus.SUBMITTED,
                source=TicketSource.REPORT_BUTTON,
                title="Broken save button",
                description="Clicking save does nothing",
            )
        )
        db_session.commit()
        resp = client.get(f"/v2/partials/trouble-tickets/list{query}")
        assert resp.status_code == 200
        # A submitted report_button ticket shows under both All and Open.
        assert "Broken save button" in resp.text

    def test_detail_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/trouble-tickets/999999")
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Prospecting
# ══════════════════════════════════════════════════════════════════════════


class TestProspecting:
    """Test prospecting list."""

    def test_list(self, client: TestClient):
        resp = client.get("/v2/partials/prospecting")
        assert resp.status_code == 200
        assert "No prospects found" in resp.text  # empty pool renders the empty state

    def test_stats(self, client: TestClient):
        resp = client.get("/v2/partials/prospecting/stats")
        assert resp.status_code == 200
        # KPI tiles render with zero counts on an empty pool.
        assert "Suggested" in resp.text
        assert "Buyer-ready" in resp.text
        assert "Call now" in resp.text


# ══════════════════════════════════════════════════════════════════════════
# Proactive
# ══════════════════════════════════════════════════════════════════════════


class TestProactive:
    """Test proactive endpoints."""

    def test_list(self, client: TestClient):
        resp = client.get("/v2/partials/proactive")
        assert resp.status_code == 200
        assert '<h1 class="h2">Proactive</h1>' in resp.text
        assert "AI-matched vendor stock to customer purchase history" in resp.text

    def test_scorecard(self, client: TestClient):
        resp = client.get("/v2/partials/proactive/scorecard")
        assert resp.status_code == 200
        assert "Proactive Scorecard" in resp.text
        assert "Sent" in resp.text

    def test_badge(self, client: TestClient, db_session: Session, test_user: User):
        # No NEW matches → empty badge.
        empty = client.get("/v2/partials/proactive/badge")
        assert empty.status_code == 200
        assert empty.text == ""
        # A NEW match for this user renders the count pill.
        from app.models import ProactiveMatch

        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        db_session.add(ProactiveMatch(offer_id=offer.id, mpn="LM317T", salesperson_id=test_user.id, status="new"))
        db_session.commit()
        resp = client.get("/v2/partials/proactive/badge")
        assert resp.status_code == 200
        assert ">1</span>" in resp.text


# ══════════════════════════════════════════════════════════════════════════
# Insights
# ══════════════════════════════════════════════════════════════════════════


class TestInsights:
    """Test AI insights panels."""

    def test_requisition_insights(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        with patch("app.services.knowledge_service.get_cached_insights", return_value=None):
            resp = client.get(f"/v2/partials/requisitions/{req.id}/insights")
            assert resp.status_code == 200
            assert "No insights yet." in resp.text
            assert f'hx-post="/v2/partials/requisitions/{req.id}/insights/refresh"' in resp.text

    def test_requisition_insights_refresh(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        entry = SimpleNamespace(confidence=0.9, content="Requisition insight body", expires_at=None)
        with patch(
            "app.services.knowledge_service.generate_insights", new_callable=AsyncMock, return_value=[entry]
        ) as mock_gen:
            resp = client.post(f"/v2/partials/requisitions/{req.id}/insights/refresh")
            assert resp.status_code == 200
            mock_gen.assert_awaited_once()
        assert "Requisition insight body" in resp.text
        assert "No insights yet." not in resp.text

    def test_vendor_insights(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        with patch("app.services.knowledge_service.get_cached_vendor_insights", return_value=None):
            resp = client.get(f"/v2/partials/vendors/{vc.id}/insights")
            assert resp.status_code == 200
            assert "No insights yet." in resp.text
            assert f'hx-post="/v2/partials/vendors/{vc.id}/insights/refresh"' in resp.text

    def test_vendor_insights_refresh(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        entry = SimpleNamespace(confidence=0.9, content="Vendor insight body", expires_at=None)
        with patch(
            "app.services.knowledge_service.generate_vendor_insights", new_callable=AsyncMock, return_value=[entry]
        ) as mock_gen:
            resp = client.post(f"/v2/partials/vendors/{vc.id}/insights/refresh")
            assert resp.status_code == 200
            mock_gen.assert_awaited_once()
        assert "Vendor insight body" in resp.text

    def test_company_insights(self, client: TestClient, db_session: Session):
        co = _make_company(db_session)
        db_session.commit()
        with patch("app.services.knowledge_service.get_cached_company_insights", return_value=None):
            resp = client.get(f"/v2/partials/customers/{co.id}/insights")
            assert resp.status_code == 200
            assert "No insights yet." in resp.text
            assert f'hx-post="/v2/partials/customers/{co.id}/insights/refresh"' in resp.text

    def test_company_insights_refresh(self, client: TestClient, db_session: Session):
        co = _make_company(db_session)
        db_session.commit()
        entry = SimpleNamespace(confidence=0.9, content="Company insight body", expires_at=None)
        with patch(
            "app.services.knowledge_service.generate_company_insights", new_callable=AsyncMock, return_value=[entry]
        ) as mock_gen:
            resp = client.post(f"/v2/partials/customers/{co.id}/insights/refresh")
            assert resp.status_code == 200
            mock_gen.assert_awaited_once()
        assert "Company insight body" in resp.text

    def test_dashboard_partial_pipeline_loader_targets_self(self, client: TestClient):
        """Pipeline lazy-load must set hx-target so it does not inherit <main hx-
        target="this">."""
        resp = client.get("/v2/partials/dashboard")
        assert resp.status_code == 200
        assert "Loading pipeline insights..." in resp.text
        _assert_lazy_load_targets_self(resp.text, "/v2/partials/dashboard/pipeline-insights")

    def test_pipeline_insights(self, client: TestClient):
        with patch("app.services.knowledge_service.get_cached_pipeline_insights", return_value=None):
            resp = client.get("/v2/partials/dashboard/pipeline-insights")
            assert resp.status_code == 200
            assert "No insights yet." in resp.text
            assert 'hx-post="/v2/partials/dashboard/pipeline-insights/refresh"' in resp.text

    def test_pipeline_insights_refresh(self, client: TestClient):
        entry = SimpleNamespace(confidence=0.9, content="Pipeline insight body", expires_at=None)
        with patch(
            "app.services.knowledge_service.generate_pipeline_insights", new_callable=AsyncMock, return_value=[entry]
        ) as mock_gen:
            resp = client.post("/v2/partials/dashboard/pipeline-insights/refresh")
            assert resp.status_code == 200
            mock_gen.assert_awaited_once()
        assert "Pipeline insight body" in resp.text


# ══════════════════════════════════════════════════════════════════════════
# Parts List (split-panel)
# ══════════════════════════════════════════════════════════════════════════


class TestPartsList:
    """Test the parts list partial (the new split-panel view)."""

    def test_list(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get("/v2/partials/parts")
        assert resp.status_code == 200
        assert "LM317T" in resp.text

    def test_list_with_search(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req, primary_mpn="LM317T")
        _make_requirement(db_session, req, primary_mpn="NE555P")
        db_session.commit()
        resp = client.get("/v2/partials/parts?q=LM317T")
        assert resp.status_code == 200
        assert "LM317T" in resp.text
        assert "NE555P" not in resp.text

    def test_list_with_status_filter(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req, primary_mpn="LM317T", sourcing_status=SourcingStatus.OPEN)
        _make_requirement(db_session, req, primary_mpn="NE555P", sourcing_status=SourcingStatus.WON)
        db_session.commit()
        resp = client.get("/v2/partials/parts?status=open")
        assert resp.status_code == 200
        assert "LM317T" in resp.text
        assert "NE555P" not in resp.text

    def test_list_sort(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req, primary_mpn="ZZZ999")
        _make_requirement(db_session, req, primary_mpn="AAA111")
        db_session.commit()
        resp = client.get("/v2/partials/parts?sort=mpn&dir=asc")
        assert resp.status_code == 200
        assert resp.text.index("AAA111") < resp.text.index("ZZZ999")


class TestPartTabs:
    """Test part-level tab endpoints."""

    @pytest.mark.parametrize(
        "tab, marker",
        [
            ("offers", "0 offers"),
            ("sourcing", "0 vendors"),
            ("req-details", 'id="req-details-fields"'),
            ("activity", "0 events"),
            ("comms", "New task or note..."),
            ("notes", "Sales Notes"),
        ],
    )
    def test_tab(self, client: TestClient, db_session: Session, test_user: User, tab: str, marker: str):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/parts/{item.id}/tab/{tab}")
        assert resp.status_code == 200
        assert marker in resp.text


class TestPartHeader:
    """Test part header and inline edits."""

    def test_header(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/parts/{item.id}/header")
        assert resp.status_code == 200
        assert "LM317T" in resp.text
        assert f"/v2/partials/parts/{item.id}/header/edit/substitutes" in resp.text

    def test_header_edit_mpn(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/parts/{item.id}/header/edit/brand")
        assert resp.status_code == 200
        assert 'name="value"' in resp.text
        assert f"/v2/partials/parts/{item.id}/header" in resp.text

    def test_header_save(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/parts/{item.id}/header",
            data={"field": "brand", "value": "Texas Instruments"},
        )
        assert resp.status_code == 200
        assert json.loads(resp.headers["HX-Trigger"]) == {"part-updated": {"id": item.id}}
        db_session.expire_all()
        assert db_session.get(Requirement, item.id).brand == "Texas Instruments"


class TestPartCellEdit:
    """Test part cell edit and save."""

    def test_cell_edit(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/parts/{item.id}/cell/edit/target_qty")
        assert resp.status_code == 200
        # Edit input prefilled with the current qty, saving back to the cell PATCH.
        assert 'value="1000"' in resp.text
        assert f'hx-patch="/v2/partials/parts/{item.id}/cell"' in resp.text

    def test_cell_display(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/parts/{item.id}/cell/display/target_qty")
        assert resp.status_code == 200
        assert "1,000" in resp.text  # formatted current value

    def test_cell_save(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/parts/{item.id}/cell",
            data={"field": "target_qty", "value": "500"},
        )
        assert resp.status_code == 200
        assert "500" in resp.text  # display cell re-renders with the new value
        db_session.expire_all()
        assert db_session.get(Requirement, item.id).target_qty == 500


class TestPartSpecEdit:
    """Test part spec edit/save."""

    def test_spec_edit(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/parts/{item.id}/edit-spec/condition")
        assert resp.status_code == 200
        assert f'hx-patch="/v2/partials/parts/{item.id}/save-spec"' in resp.text
        assert '<input type="hidden" name="field" value="condition">' in resp.text

    def test_spec_save(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/parts/{item.id}/save-spec",
            data={"field": "condition", "value": "new"},
        )
        assert resp.status_code == 200
        assert ">new</span>" in resp.text  # spec_display fragment with the saved value
        db_session.expire_all()
        assert db_session.get(Requirement, item.id).condition == "new"


class TestPartNotes:
    """Test part notes save."""

    def test_save_notes(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/parts/{item.id}/notes",
            data={"sale_notes": "This is a test note"},
        )
        assert resp.status_code == 200
        assert "This is a test note" in resp.text  # notes tab re-renders with the note
        db_session.expire_all()
        assert db_session.get(Requirement, item.id).sale_notes == "This is a test note"


class TestPartTasks:
    """Test part task create, done, reopen."""

    def test_create_task(self, client: TestClient, db_session: Session, test_user: User):
        from app.models import RequisitionTask

        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/parts/{item.id}/tasks",
            data={"title": "Test task", "priority": "1"},
        )
        assert resp.status_code == 200
        assert "Test task" in resp.text  # refreshed comms tab lists the task
        task = db_session.query(RequisitionTask).filter_by(requirement_id=item.id).one()
        assert task.title == "Test task"
        assert task.created_by == test_user.id


class TestPartArchive:
    """Test archive/unarchive single parts and bulk."""

    def test_archive_part(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.patch(f"/v2/partials/parts/{item.id}/archive")
        assert resp.status_code == 200
        assert json.loads(resp.headers["HX-Trigger"]) == {"part-archived": {"id": item.id}}
        db_session.expire_all()
        assert db_session.get(Requirement, item.id).sourcing_status == SourcingStatus.ARCHIVED

    def test_unarchive_part(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req, sourcing_status="archived")
        db_session.commit()
        resp = client.patch(f"/v2/partials/parts/{item.id}/unarchive")
        assert resp.status_code == 200
        assert "LM317T" in resp.text  # restored part is back in the default list
        db_session.expire_all()
        assert db_session.get(Requirement, item.id).sourcing_status == SourcingStatus.OPEN

    def test_bulk_archive(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.post(
            "/v2/partials/parts/bulk-archive",
            json={"requirement_ids": [item.id], "requisition_ids": []},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.get(Requirement, item.id).sourcing_status == SourcingStatus.ARCHIVED

    def test_bulk_unarchive(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req, sourcing_status="archived")
        db_session.commit()
        resp = client.post(
            "/v2/partials/parts/bulk-unarchive",
            json={"requirement_ids": [item.id], "requisition_ids": []},
        )
        assert resp.status_code == 200
        assert "LM317T" in resp.text  # restored part renders in the refreshed list
        db_session.expire_all()
        assert db_session.get(Requirement, item.id).sourcing_status == SourcingStatus.OPEN


# ══════════════════════════════════════════════════════════════════════════
# Knowledge Endpoints
# ══════════════════════════════════════════════════════════════════════════


class TestKnowledge:
    """Test knowledge list and create."""

    def test_list(self, client: TestClient, db_session: Session, test_user: User):
        from app.models.knowledge import KnowledgeEntry

        db_session.add(
            KnowledgeEntry(
                entry_type="note", content="Existing knowledge item", source="manual", created_by=test_user.id
            )
        )
        db_session.commit()
        resp = client.get("/v2/partials/knowledge")
        assert resp.status_code == 200
        assert "Knowledge Base" in resp.text
        assert "Existing knowledge item" in resp.text

    def test_create(self, client: TestClient, db_session: Session, test_user: User):
        from app.models.knowledge import KnowledgeEntry

        resp = client.post(
            "/v2/partials/knowledge",
            data={
                "content": "Some knowledge content",
                "entry_type": "note",
            },
        )
        assert resp.status_code == 200
        assert "Some knowledge content" in resp.text  # refreshed list shows the entry
        entry = db_session.query(KnowledgeEntry).one()
        assert entry.content == "Some knowledge content"
        assert entry.entry_type == "note"
        assert entry.created_by == test_user.id


# ══════════════════════════════════════════════════════════════════════════
# Admin Endpoints
# ══════════════════════════════════════════════════════════════════════════


class TestAdminEndpoints:
    """Test admin-level endpoints (merge, import, api health)."""

    def test_api_health(self, client: TestClient):
        """No app.services.connector_health module exists — the route's guarded import
        always falls back to the empty dashboard, which must still render cleanly."""
        resp = client.get("/v2/partials/admin/api-health")
        assert resp.status_code == 200
        assert "Connector Health" in resp.text
        assert "No connector data available." in resp.text

    def test_vendor_merge(self, client: TestClient, db_session: Session, test_user: User):
        test_user.role = "admin"
        db_session.commit()
        v1 = _make_vendor_card(db_session, normalized_name="vendor_a", display_name="Vendor A")
        v2 = _make_vendor_card(db_session, normalized_name="vendor_b", display_name="Vendor B")
        db_session.commit()
        with patch("app.services.vendor_merge_service.merge_vendor_cards") as mock_merge:
            mock_merge.return_value = {"kept": v1.id, "reassigned": 3}
            resp = client.post(
                "/v2/partials/admin/vendor-merge",
                data={"keep_id": str(v1.id), "remove_id": str(v2.id)},
            )
            assert resp.status_code == 200
        mock_merge.assert_called_once_with(v1.id, v2.id, db_session)
        assert "Merged into Vendor A. 3 records reassigned." in resp.headers.get("HX-Trigger", "")

    def test_company_merge(self, client: TestClient, db_session: Session, test_user: User):
        test_user.role = "admin"
        db_session.commit()
        c1 = _make_company(db_session, name="Company A")
        c2 = _make_company(db_session, name="Company B")
        db_session.commit()
        with patch("app.services.company_merge_service.merge_companies") as mock_merge:
            mock_merge.return_value = {"kept": c1.id}
            resp = client.post(
                "/v2/partials/admin/company-merge",
                data={"keep_id": str(c1.id), "remove_id": str(c2.id)},
            )
            assert resp.status_code == 200
        mock_merge.assert_called_once_with(c1.id, c2.id, db_session)
        assert "Merged into Company A." in resp.headers.get("HX-Trigger", "")


# ══════════════════════════════════════════════════════════════════════════
# Follow-ups
# ══════════════════════════════════════════════════════════════════════════


class TestFollowUps:
    """Test follow-ups list."""

    def test_list(self, client: TestClient):
        resp = client.get("/v2/partials/follow-ups")
        assert resp.status_code == 200
        # Empty queue renders the zero-count summary line.
        assert '<span class="font-medium text-amber-600">0</span> contacts' in resp.text


# ══════════════════════════════════════════════════════════════════════════
# Sightings
# ══════════════════════════════════════════════════════════════════════════


class TestSightingsWorkspace:
    """Test sightings workspace."""

    def test_workspace(self, client: TestClient):
        resp = client.get("/v2/partials/sightings/workspace")
        assert resp.status_code == 200
        # Split-panel shell: lazy-loaded board table + detail panel target.
        assert 'id="sightings-table"' in resp.text
        assert "'/v2/partials/sightings/' + id + '/detail'" in resp.text


# ══════════════════════════════════════════════════════════════════════════
# Offer Review Queue
# ══════════════════════════════════════════════════════════════════════════


class TestOfferReviewQueue:
    """Test offer review queue."""

    def test_review_queue(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_offer(db_session, req, test_user, status=OfferStatus.PENDING_REVIEW)
        _make_offer(db_session, req, test_user, mpn="NE555P", status=OfferStatus.ACTIVE)
        db_session.commit()
        resp = client.get("/v2/partials/offers/review-queue")
        assert resp.status_code == 200
        # Only PENDING_REVIEW offers appear in the queue.
        assert '<span class="font-medium text-amber-600">1</span> offer' in resp.text
        assert "LM317T" in resp.text
        assert "NE555P" not in resp.text
