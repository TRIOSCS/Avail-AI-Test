"""tests/test_build_quote_tab.py — in-workspace Build-Quote tab (Chunk B).

Route/render coverage for the single-stage Build-Quote tab that reshapes the quote-builder
modal onto the requisition detail (sibling to the Quotes list tab):
  - the lazy tab body renders for a seeded requisition (best-cost shown, sell seeded);
  - checking a line + assembling creates a Quote and re-renders with the inline summary;
  - assembling a second time preserves the revision lifecycle (old quote -> -R{n}/revised);
  - the guardrail warning copy is present when a sell undercuts cost;
  - gating: SALES/TRADER who do not own the requisition get 404;
  - the detail partial deep-links the Build-Quote tab via ?tab=build_quote (re-pointed launch).

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user, test_requisition,
            test_customer_site, sales_user)
"""

import json
import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Offer, Quote, Requirement, Requisition, User
from tests.conftest import engine

_ = engine


@pytest.fixture()
def quoteable_req(db_session: Session, test_user: User, test_customer_site) -> Requisition:
    """A requisition linked to a customer site, one requirement, two ACTIVE offers."""
    req = Requisition(
        name="QB-TAB-001",
        customer_name="Acme Electronics",
        status="active",
        customer_site_id=test_customer_site.id,
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
        condition="new",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.flush()

    for vendor, price in (("Arrow", 0.55), ("Avnet", 0.40)):
        db_session.add(
            Offer(
                requisition_id=req.id,
                requirement_id=item.id,
                vendor_name=vendor,
                mpn="LM317T",
                normalized_mpn="LM317T",
                status="active",
                unit_price=price,
                qty_available=500,
                entered_by_id=test_user.id,
                created_at=datetime.now(timezone.utc),
            )
        )
    db_session.commit()
    db_session.refresh(req)
    db_session.refresh(item)
    req.requirement_id = item.id  # convenience for tests
    return req


class TestBuildQuoteTabRender:
    def test_tab_renders_with_best_cost_and_seed(self, client: TestClient, quoteable_req: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{quoteable_req.id}/build-quote")
        assert resp.status_code == 200
        html = resp.text
        # Best cost (cheapest ACTIVE = 0.40) shown as the planning reference.
        assert "0.4000" in html
        assert "LM317T" in html
        # The Alpine tab component is wired (single-stage inline assembly).
        assert "quoteBuilderTab(" in html
        # SINGLE-quoted x-data so the |tojson seed blob (double quotes) is valid.
        assert "x-data='quoteBuilderTab(" in html

    def test_tab_renders_empty_state_with_no_parts(
        self, client: TestClient, db_session: Session, test_user: User, test_customer_site
    ):
        req = Requisition(
            name="QB-TAB-EMPTY",
            status="active",
            customer_site_id=test_customer_site.id,
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/build-quote")
        assert resp.status_code == 200
        assert "No parts to quote yet" in resp.text


class TestBuildQuoteAssemble:
    def _selection(self, req: Requisition, sell: float):
        return [
            {
                "requirement_id": req.requirement_id,
                "offer_id": None,
                "mpn": "LM317T",
                "manufacturer": "TI",
                "qty": 100,
                "cost_price": 0.40,
                "sell_price": sell,
                "margin_pct": round((sell - 0.40) / sell * 100, 2),
                "condition": "new",
            }
        ]

    def test_assemble_creates_quote_and_renders_summary(
        self, client: TestClient, db_session: Session, quoteable_req: Requisition
    ):
        resp = client.post(
            f"/v2/partials/requisitions/{quoteable_req.id}/build-quote/assemble",
            data={"selections_json": json.dumps(self._selection(quoteable_req, 0.60))},
        )
        assert resp.status_code == 200
        quotes = db_session.query(Quote).filter(Quote.requisition_id == quoteable_req.id).all()
        assert len(quotes) == 1
        assert quotes[0].revision == 1
        # Inline summary card renders with the clean quote number + a Download PDF link.
        assert quotes[0].quote_number in resp.text
        assert "Download PDF" in resp.text
        # Customer-clean: no vendor name leaks into the owner's summary preview.
        assert "Arrow" not in resp.text and "Avnet" not in resp.text

    def test_assemble_revision_preserves_lifecycle(
        self, client: TestClient, db_session: Session, quoteable_req: Requisition
    ):
        # First assemble.
        client.post(
            f"/v2/partials/requisitions/{quoteable_req.id}/build-quote/assemble",
            data={"selections_json": json.dumps(self._selection(quoteable_req, 0.60))},
        )
        first = db_session.query(Quote).filter(Quote.requisition_id == quoteable_req.id).one()
        base_number = first.quote_number

        # Second assemble against the same quote -> revision lifecycle.
        resp = client.post(
            f"/v2/partials/requisitions/{quoteable_req.id}/build-quote/assemble",
            data={
                "selections_json": json.dumps(self._selection(quoteable_req, 0.70)),
                "quote_id": str(first.id),
            },
        )
        assert resp.status_code == 200
        db_session.expire_all()
        quotes = db_session.query(Quote).filter(Quote.requisition_id == quoteable_req.id).order_by(Quote.id).all()
        assert len(quotes) == 2
        old, new = quotes
        assert old.status == "revised"
        assert old.quote_number == f"{base_number}-R1"
        assert new.quote_number == base_number
        assert new.revision == 2

    def test_guardrail_binding_wired_into_tab(self, client: TestClient, quoteable_req: Requisition):
        """The per-line guardrail + blended-warning bindings are wired into the tab.

        The live warning strings (below-cost / thin-margin) are computed client-side by the
        Alpine ``guardrail``/``blendedWarning`` getters; the render test asserts the bindings
        reach the DOM so the warning surfaces when sell undercuts cost.
        """
        resp = client.get(f"/v2/partials/requisitions/{quoteable_req.id}/build-quote")
        assert resp.status_code == 200
        html = resp.text
        # Per-line guardrail chip + the blended-quote warning strip are both present.
        assert 'x-text="guardrail(' in html
        assert 'x-text="blendedWarning"' in html
        # The guardrail floor (min_margin_pct) is threaded into the component.
        assert "quoteBuilderTab(" in html and ", 10.0, " in html

    def test_alpine_component_implements_guardrail_strings(self):
        """The quoteBuilderTab component emits the below-cost / thin-margin guardrail
        copy."""
        from pathlib import Path

        js = Path("app/static/htmx_app.js").read_text()
        assert "quoteBuilderTab" in js
        assert "below cost" in js
        assert "thin margin" in js
        assert "below the" in js  # blended-margin floor warning

    def test_assemble_rejects_empty_selection(self, client: TestClient, quoteable_req: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{quoteable_req.id}/build-quote/assemble",
            data={"selections_json": "[]"},
        )
        assert resp.status_code == 400

    def test_assemble_blocked_without_customer_site(self, client: TestClient, db_session: Session, test_user: User):
        req = Requisition(
            name="QB-TAB-NOSITE",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(requisition_id=req.id, primary_mpn="X", target_qty=1, created_at=datetime.now(timezone.utc))
        db_session.add(item)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/build-quote/assemble",
            data={
                "selections_json": json.dumps(
                    [
                        {
                            "requirement_id": item.id,
                            "mpn": "X",
                            "qty": 1,
                            "cost_price": 1.0,
                            "sell_price": 2.0,
                            "margin_pct": 50.0,
                        }
                    ]
                )
            },
        )
        assert resp.status_code == 400


class TestBuildQuoteGating:
    def test_non_owner_sales_blocked(
        self, db_session: Session, sales_user: User, test_user: User, quoteable_req: Requisition
    ):
        """A SALES user who does not own the requisition gets 404 (existence not
        leaked)."""
        from app.database import get_db
        from app.dependencies import require_user
        from app.main import app

        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[require_user] = lambda: sales_user
        try:
            with TestClient(app) as c:
                resp = c.get(f"/v2/partials/requisitions/{quoteable_req.id}/build-quote")
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(require_user, None)
        assert resp.status_code == 404


class TestRepointedLaunch:
    def test_detail_deeplinks_build_quote_tab(self, client: TestClient, quoteable_req: Requisition):
        """The re-pointed single-req launch (?tab=build_quote) reaches the Build-Quote
        tab."""
        resp = client.get(f"/v2/partials/requisitions/{quoteable_req.id}?tab=build_quote")
        assert resp.status_code == 200
        html = resp.text
        # Active tab is Build Quote and the lazy tab endpoint is wired with an explicit target.
        assert "activeTab: 'build_quote'" in html
        assert f"/v2/partials/requisitions/{quoteable_req.id}/build-quote" in html
        assert 'hx-target="#tab-content"' in html

    def test_detail_default_tab_is_parts(self, client: TestClient, quoteable_req: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{quoteable_req.id}")
        assert "activeTab: 'parts'" in resp.text
