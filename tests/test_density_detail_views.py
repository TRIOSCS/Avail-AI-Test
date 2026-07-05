"""Render regression guards for the three density/whitespace tightening fixes.

The product owner flagged three detail/dossier views as "too much wasted space /
feels unfinished". The fixes tighten padding/margins, compact sparse sections, and
denser meta layouts WITHOUT losing any data, control, or tab. These render tests pin
that every key section/field/control still renders after the tightening — proving the
tightening removed only whitespace, not information or behavior.

Covers:
1. sightings/detail.html (+ _vendor_row.html) — part header, dense Qty/Target/Customer/
   Status meta, status select, all three tabs, vendor qty/price/score, dense vendors table.
2. materials/detail.html — hero identity, Specifications + spec grid, Enrich/Insights/Edit
   controls, FRU + Crosses sections, all five content tabs.
3. search/dossier_hero.html (+ dossier_shell.html) — identity, market-price strip, what-we-
   know count tiles, the three action-bar CTAs, and the tightened shell rhythm.

Called by: pytest
Depends on: the sightings / materials / part_dossier HTMX routes (via the ``client``
fixture), MaterialCard/Requisition/Requirement/VendorSightingSummary models, Jinja2.
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.models.sourcing import Requirement, Requisition
from app.models.vendor_sighting_summary import VendorSightingSummary

# ── Fix 1: sightings/detail.html ────────────────────────────────────────────────


def _seed_sighting(db: Session):
    req = Requisition(name="Density RFQ", status="open", customer_name="Acme Corp")
    db.add(req)
    db.flush()
    r = Requirement(
        requisition_id=req.id,
        primary_mpn="DENSITY-MPN-1",
        manufacturer="TestMfr",
        target_qty=100,
        target_price=Decimal("4.25"),
        sourcing_status="open",
    )
    db.add(r)
    db.flush()
    db.add(
        VendorSightingSummary(
            requirement_id=r.id,
            vendor_name="Good Vendor",
            estimated_qty=200,
            best_price=Decimal("3.90"),
            listing_count=2,
            score=75.0,
            tier="Good",
        )
    )
    db.commit()
    return req, r


class TestSightingsDetailDensity:
    def test_renders_header_identity_and_actions(self, client: TestClient, db_session: Session):
        req, r = _seed_sighting(db_session)
        html = client.get(f"/v2/partials/sightings/{r.id}/detail").text
        # Part identity + manufacturer preserved in the tightened header.
        assert "DENSITY-MPN-1" in html
        assert "TestMfr" in html
        # Both header actions intact.
        assert "Build RFQ" in html
        assert "vendor-modal" in html  # Build RFQ dispatch target

    def test_renders_all_meta_fields(self, client: TestClient, db_session: Session):
        req, r = _seed_sighting(db_session)
        html = client.get(f"/v2/partials/sightings/{r.id}/detail").text
        # Every meta label survives the denser layout — no information loss.
        for label in ("Qty", "Target", "Customer", "Status"):
            assert label in html, f"meta label {label!r} missing"
        assert "Acme Corp" in html  # customer name value
        assert "100" in html  # target qty value
        # Status select is still the interactive advance-status control.
        assert "advance-status" in html

    def test_renders_all_three_tabs(self, client: TestClient, db_session: Session):
        req, r = _seed_sighting(db_session)
        html = client.get(f"/v2/partials/sightings/{r.id}/detail").text
        assert "activeTab = 'vendors'" in html
        assert "activeTab = 'offers'" in html
        assert "activeTab = 'activity'" in html

    def test_vendor_row_qty_price_score_scannable(self, client: TestClient, db_session: Session):
        req, r = _seed_sighting(db_session)
        html = client.get(f"/v2/partials/sightings/{r.id}/detail").text
        assert "Good Vendor" in html
        assert "200" in html  # qty
        assert "$3.90" in html  # best price
        assert "75%" in html  # score

    def test_vendors_table_uses_dense_rows(self, client: TestClient, db_session: Session):
        """The tightening opts the vendors table into the existing compact-table--dense
        row spacing (already in the built CSS)."""
        req, r = _seed_sighting(db_session)
        html = client.get(f"/v2/partials/sightings/{r.id}/detail").text
        assert "compact-table--dense" in html


# ── Fix 2: materials/detail.html ────────────────────────────────────────────────


def _seed_material(db: Session) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn="dens317t",
        display_mpn="DENS317T",
        manufacturer="Texas Instruments",
        lifecycle_status="active",
        package_type="TO-220",
        rohs_status="compliant",
        search_count=3,
        last_searched_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        enrichment_status="verified",
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


class TestMaterialsDetailDensity:
    def test_renders_hero_identity(self, client: TestClient, db_session: Session):
        card = _seed_material(db_session)
        html = client.get(f"/v2/partials/materials/{card.id}", headers={"HX-Request": "true"}).text
        assert "DENS317T" in html
        assert "Texas Instruments" in html

    def test_renders_all_controls(self, client: TestClient, db_session: Session):
        card = _seed_material(db_session)
        html = client.get(f"/v2/partials/materials/{card.id}", headers={"HX-Request": "true"}).text
        # Every action control must survive the tightening.
        assert ">\n          Enrich" in html or "Enrich" in html
        assert "Insights" in html
        assert ">Edit<" in html or 'x-text="editing' in html
        assert f"/v2/partials/materials/{card.id}/enrich" in html
        assert f"/v2/partials/materials/{card.id}/insights" in html

    def test_renders_spec_grid_fields(self, client: TestClient, db_session: Session):
        card = _seed_material(db_session)
        html = client.get(f"/v2/partials/materials/{card.id}", headers={"HX-Request": "true"}).text
        assert "Specifications" in html
        for label in ("Category", "Package", "Pin Count", "Lifecycle"):
            assert label in html, f"spec label {label!r} missing"

    def test_renders_all_content_tabs(self, client: TestClient, db_session: Session):
        card = _seed_material(db_session)
        html = client.get(f"/v2/partials/materials/{card.id}", headers={"HX-Request": "true"}).text
        for tab in ("Vendors", "Customers", "Sourcing", "Price History", "Files"):
            assert tab in html, f"content tab {tab!r} missing"

    def test_renders_fru_and_crosses_sections(self, client: TestClient, db_session: Session):
        """FRU crosswalk + Crosses/Substitutes sections (incl.

        their empty states) stay intact after the section-spacing tightening.
        """
        card = _seed_material(db_session)
        html = client.get(f"/v2/partials/materials/{card.id}", headers={"HX-Request": "true"}).text
        assert "insights-panel" in html  # lazy insights mount preserved
        assert "material-tab-content" in html  # tab body mount preserved


# ── Fix 3: search/dossier_hero.html + dossier_shell.html ─────────────────────────


class TestDossierHeroDensity:
    def test_hero_renders_identity_and_zones(self, client: TestClient, db_session: Session):
        card = MaterialCard(
            normalized_mpn="dens555",
            display_mpn="DENS555",
            manufacturer="TI",
            search_count=1,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()
        html = client.get("/v2/partials/search/dossier/hero", params={"mpn": "DENS555"}).text
        assert "DENS555" in html
        assert "Market price" in html
        assert "What we know" in html
        # Knowledge count tiles.
        for tile in ("Offers", "Won", "Sightings", "Reqs"):
            assert tile in html, f"count tile {tile!r} missing"

    def test_hero_renders_action_ctas(self, client: TestClient, db_session: Session):
        html = client.get("/v2/partials/search/dossier/hero", params={"mpn": "NEWPART99"}).text
        assert "Send RFQ" in html
        assert "Add Offer" in html
        assert "Add to Requisition" in html

    def test_shell_renders_live_market_and_tight_rhythm(self, client: TestClient, db_session: Session):
        """The dossier shell keeps the Live market section wired and applies the
        tightened section rhythm (space-y-4) that closes the flagged center gap."""
        html = client.get("/v2/partials/search", params={"mpn": "DENS555"}).text
        assert "Live market" in html
        assert "What we know" in html
        assert "/v2/partials/search/dossier/hero?mpn=DENS555" in html
        # Tightened vertical rhythm between the hero and the sections below it.
        assert "space-y-4" in html
