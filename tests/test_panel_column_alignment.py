"""test_panel_column_alignment.py — guards that detail-panel tables show data under the
columns their headers promise, and that the requisitions2 detail panel has no dead
placeholder tabs.

Covers two reviewed defects:
  Track 1 (sightings vendors tab): the <thead> declared 7 columns
  (Vendor|Status|Qty|Best Price|Score|Phone|Actions) while every row was a single
  <td colspan="7"> flex card, so 5 of 7 headers labelled nothing. After the fix the
  vendors table is real fitted columns (Vendor|Qty|Best Price|Score|⋯) with the
  per-vendor summary row carrying exactly one <td> per <th>, phone folded into the
  vendor cell, and actions in a kebab.
  Track 2 (requisitions2 detail panel): the Offers and Activity tabs were hard-coded
  placeholders ("will appear here" / "coming soon"). After the fix both lazy-load real
  data from dedicated endpoints.

Called by: pytest
Depends on: app/routers/sightings.py, app/routers/requisitions2.py, bs4
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Offer, Requirement, Requisition, User
from app.models.vendor_sighting_summary import VendorSightingSummary


# ── Track 1 fixtures ────────────────────────────────────────────────────────
@pytest.fixture()
def req_with_vendor_summary(db_session: Session, test_user: User) -> tuple:
    """Requisition + requirement + one VendorSightingSummary with known cells."""
    req = Requisition(
        name="COL-ALIGN-REQ",
        customer_name="Column Co",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn="LM741CN",
        target_qty=500,
        sourcing_status="open",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.flush()
    db_session.add(
        VendorSightingSummary(
            requirement_id=item.id,
            vendor_name="Acme Components",
            estimated_qty=500,
            best_price=12.40,
            avg_price=15.10,
            score=82.0,
            vendor_phone="555-0100",
            listing_count=3,
        )
    )
    db_session.commit()
    db_session.refresh(item)
    return req, item


def _vendors_table(html: str) -> BeautifulSoup:
    """The single compact-table in the sightings detail panel = the vendors table."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="compact-table")
    assert table is not None, "vendors table not found in detail panel"
    return table


def _summary_rows(table) -> list:
    """Data rows that are vendor *summary* rows (exclude header + the colspan drawer
    row)."""
    rows = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if not tds:
            continue  # header row
        if len(tds) == 1 and tds[0].get("colspan"):
            continue  # expandable drawer row
        rows.append(tr)
    return rows


class TestSightingsVendorColumns:
    def test_headers_have_no_dataless_columns(self, client: TestClient, req_with_vendor_summary):
        """Visible header labels are exactly the columns that carry data — no orphan
        Status/Phone/Actions headers labelling empty space."""
        _req, item = req_with_vendor_summary
        resp = client.get(f"/v2/partials/sightings/{item.id}/detail")
        assert resp.status_code == 200
        table = _vendors_table(resp.text)
        labels = [th.get_text(strip=True) for th in table.find("thead").find_all("th")]
        visible = [t for t in labels if t]
        assert visible == ["Vendor", "Qty", "Best Price", "Score"], (
            f"vendors header should label only data columns, got {visible}"
        )

    def test_summary_row_cell_count_matches_header_count(self, client: TestClient, req_with_vendor_summary):
        """Every vendor summary row carries exactly one <td> per <th> — the core
        'columns match data' invariant."""
        _req, item = req_with_vendor_summary
        resp = client.get(f"/v2/partials/sightings/{item.id}/detail")
        table = _vendors_table(resp.text)
        th_count = len(table.find("thead").find_all("th"))
        rows = _summary_rows(table)
        assert rows, "expected at least one vendor summary row"
        for tr in rows:
            assert len(tr.find_all("td", recursive=False)) == th_count, (
                f"summary row has {len(tr.find_all('td', recursive=False))} cells, header has {th_count}"
            )

    def test_phone_renders_in_visible_summary_row(self, client: TestClient, req_with_vendor_summary):
        """Phone is shown in the always-visible summary row (folded into the vendor cell
        as a tel link), not hidden only inside the expand drawer."""
        _req, item = req_with_vendor_summary
        resp = client.get(f"/v2/partials/sightings/{item.id}/detail")
        table = _vendors_table(resp.text)
        rows = _summary_rows(table)
        assert rows, "expected a vendor summary row"
        first = rows[0]
        tel = first.find("a", href=lambda h: h and h.startswith("tel:"))
        assert tel is not None, "summary row must surface the vendor phone as a tel link"
        assert "555-0100" in tel["href"]

    def test_qty_price_score_still_render(self, client: TestClient, req_with_vendor_summary):
        """Regression: the rewrite must not drop the qty / best-price / score data."""
        _req, item = req_with_vendor_summary
        resp = client.get(f"/v2/partials/sightings/{item.id}/detail")
        body = resp.text
        assert "500" in body  # estimated_qty
        assert "$12.40" in body  # best_price
        assert "82%" in body  # score


# ── Track 2: requisitions2 detail panel tabs ────────────────────────────────
class TestReqsDetailTabs:
    def test_detail_panel_has_no_dead_placeholder_tabs(self, client: TestClient, test_requisition):
        """The Offers/Activity tabs must not ship hard-coded placeholders."""
        resp = client.get(f"/requisitions2/{test_requisition.id}/detail")
        assert resp.status_code == 200
        low = resp.text.lower()
        assert "coming soon" not in low
        assert "will appear here" not in low

    def test_offers_tab_endpoint_renders_offer(
        self, client: TestClient, db_session: Session, test_requisition: Requisition
    ):
        """GET /requisitions2/{id}/offers lists real offers for the requisition."""
        item = Requirement(
            requisition_id=test_requisition.id,
            primary_mpn="OFFER-MPN",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.flush()
        db_session.add(
            Offer(
                requisition_id=test_requisition.id,
                requirement_id=item.id,
                vendor_name="Zenith Supply",
                mpn="OFFER-MPN",
                qty_available=250,
                unit_price=11.90,
                status="active",
                created_at=datetime.now(timezone.utc),
            )
        )
        db_session.commit()
        resp = client.get(f"/requisitions2/{test_requisition.id}/offers")
        assert resp.status_code == 200
        assert "Zenith Supply" in resp.text

    def test_activity_tab_endpoint_renders(self, client: TestClient, test_requisition: Requisition):
        """GET /requisitions2/{id}/activity returns the timeline partial (200)."""
        resp = client.get(f"/requisitions2/{test_requisition.id}/activity")
        assert resp.status_code == 200

    def test_detail_offer_count_reflects_real_offers(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """The detail panel's metadata 'Offers' count must reflect real offers, not a
        hardcoded 0 — otherwise it contradicts the now-wired Offers tab."""
        from app.services.requisition_list_service import get_requisition_detail

        item = Requirement(
            requisition_id=test_requisition.id,
            primary_mpn="OC-MPN",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.flush()
        for vendor in ("V-one", "V-two"):
            db_session.add(
                Offer(
                    requisition_id=test_requisition.id,
                    requirement_id=item.id,
                    vendor_name=vendor,
                    mpn="OC-MPN",
                    status="active",
                    created_at=datetime.now(timezone.utc),
                )
            )
        db_session.commit()
        detail = get_requisition_detail(db_session, test_requisition.id, test_user.id, "buyer")
        assert detail["req"]["offer_count"] == 2

    def test_lazy_tabs_enforce_ownership_for_sales(
        self, client: TestClient, sales_user: User, test_requisition: Requisition
    ):
        """The lazy Offers/Activity endpoints must enforce the same role-based access as
        the detail panel they live in — a SALES user gets 404 for a req they don't own
        (IDOR guard), not a leaked offers/activity list."""
        from app.dependencies import require_user
        from app.main import app

        # test_requisition is owned by test_user (buyer); sales_user does not own it.
        original = app.dependency_overrides.get(require_user)
        app.dependency_overrides[require_user] = lambda: sales_user
        try:
            assert client.get(f"/requisitions2/{test_requisition.id}/offers").status_code == 404
            assert client.get(f"/requisitions2/{test_requisition.id}/activity").status_code == 404
        finally:
            if original is not None:
                app.dependency_overrides[require_user] = original
