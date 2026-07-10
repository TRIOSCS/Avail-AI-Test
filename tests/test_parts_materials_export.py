"""Tests for the Sales-Hub parts worklist CSV export (GET /v2/partials/parts/export) and
the Materials results CSV export (GET /v2/partials/materials/export).

Each export streams CSV with the attachment headers, a header row plus one row per
matching record, mirrors its list route's filters (filter parity), and enforces the
same auth as its list route. Both toolbars render the plain hx-boost="false" download
anchor.

Called by: pytest
Depends on: conftest.py fixtures (db_session, test_user, client, unauthenticated_client),
            app.models.sourcing (Requisition, Requirement), app.models.offers (Offer),
            app.models.intelligence (MaterialCard, MaterialVendorHistory)
"""

import csv
import io
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import OfferStatus, SourcingStatus
from app.models import Offer, Requirement, Requisition
from app.models.intelligence import MaterialCard, MaterialVendorHistory

PARTS_EXPORT_URL = "/v2/partials/parts/export"
PARTS_LIST_URL = "/v2/partials/parts"
MATERIALS_EXPORT_URL = "/v2/partials/materials/export"
MATERIALS_LIST_URL = "/v2/partials/materials/faceted"


def _parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def _body(rows: list[list[str]]) -> str:
    return "\n".join(",".join(cell for cell in row) for row in rows[1:])


# ── Parts helpers ────────────────────────────────────────────────────────


def _make_part(
    db: Session,
    *,
    mpn: str,
    customer: str = "Acme Corp",
    brand: str = "Texas Instruments",
    sourcing_status: str = SourcingStatus.OPEN,
    req_status: str = "open",
    target_qty: int = 100,
) -> Requirement:
    """Seed an open requisition + requirement that shows on the default parts list."""
    req = Requisition(
        name=f"RFQ-{mpn}",
        status=req_status,
        customer_name=customer,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        brand=brand,
        target_qty=target_qty,
        target_price=1.25,
        sourcing_status=sourcing_status,
        created_at=datetime.now(UTC),
    )
    db.add(requirement)
    db.flush()
    return requirement


def _make_offer(db: Session, requirement: Requirement, *, vendor: str, unit_price: float) -> Offer:
    offer = Offer(
        requirement_id=requirement.id,
        vendor_name=vendor,
        mpn=requirement.primary_mpn,
        unit_price=unit_price,
        status=OfferStatus.ACTIVE,
        created_at=datetime.now(UTC),
    )
    db.add(offer)
    db.flush()
    return offer


# ── Parts export ─────────────────────────────────────────────────────────


def test_parts_export_returns_csv_attachment(client: TestClient, db_session: Session):
    """200 + text/csv + attachment Content-Disposition with the fixed filename."""
    _make_part(db_session, mpn="LM317T")
    db_session.commit()

    resp = client.get(PARTS_EXPORT_URL)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    assert 'filename="parts_export.csv"' in disposition


def test_parts_export_header_and_one_row_per_part(client: TestClient, db_session: Session):
    """Header row + one data row per matching requirement, with the key fields
    present."""
    a = _make_part(db_session, mpn="LM317T", customer="Acme Corp", brand="Texas Instruments")
    _make_offer(db_session, a, vendor="Digi-Key", unit_price=1.2345)
    _make_part(db_session, mpn="AD8232", customer="Beta LLC", brand="Analog Devices")
    db_session.commit()

    rows = _parse_csv(client.get(PARTS_EXPORT_URL).text)

    header = rows[0]
    assert header[0] == "MPN"
    for col in (
        "Description",
        "Brand",
        "Status",
        "Qty",
        "Target $",
        "Offers",
        "Best $",
        "Requisition",
        "Customer",
        "Owner",
        "Created",
    ):
        assert col in header
    # Header + exactly two part rows.
    assert len(rows) == 3

    body = _body(rows)
    assert "LM317T" in body
    assert "AD8232" in body
    assert "Acme Corp" in body
    assert "Beta LLC" in body
    assert "Texas Instruments" in body
    assert "1.2345" in body  # best active offer price


def test_parts_export_respects_status_filter(client: TestClient, db_session: Session):
    """The status filter selects only requirements in that sourcing status."""
    _make_part(db_session, mpn="OPEN-PART", sourcing_status=SourcingStatus.OPEN)
    _make_part(db_session, mpn="SOURCING-PART", sourcing_status=SourcingStatus.SOURCING)
    db_session.commit()

    rows = _parse_csv(client.get(PARTS_EXPORT_URL, params={"status": "sourcing"}).text)

    body = _body(rows)
    assert "SOURCING-PART" in body
    assert "OPEN-PART" not in body
    assert len(rows) == 2  # header + one matching part


def test_parts_export_respects_search_filter(client: TestClient, db_session: Session):
    """The q search filter (MPN/customer/req/brand) is honored."""
    _make_part(db_session, mpn="LM317T", customer="Acme Corp")
    _make_part(db_session, mpn="AD8232", customer="Beta LLC")
    db_session.commit()

    rows = _parse_csv(client.get(PARTS_EXPORT_URL, params={"q": "LM317"}).text)

    body = _body(rows)
    assert "LM317T" in body
    assert "AD8232" not in body
    assert len(rows) == 2  # header + one matching part


def test_parts_export_unauthenticated_rejected(unauthenticated_client: TestClient, db_session: Session):
    """Unauthenticated requests are rejected like the parts list route (401/403)."""
    resp = unauthenticated_client.get(PARTS_EXPORT_URL, follow_redirects=False)
    assert resp.status_code in (401, 403)


def test_parts_export_button_rendered_in_toolbar(client: TestClient, db_session: Session):
    """The parts list renders the Export CSV anchor: a plain (non-htmx) download that
    points at the export endpoint and opts out of nav-boost."""
    _make_part(db_session, mpn="LM317T")
    db_session.commit()

    html = client.get(PARTS_LIST_URL).text

    assert "Export CSV" in html
    assert 'hx-boost="false"' in html
    assert "/v2/partials/parts/export" in html


# ── Materials helpers ────────────────────────────────────────────────────


def _make_card(
    db: Session,
    *,
    normalized_mpn: str,
    display_mpn: str | None = None,
    manufacturer: str = "Micron",
    category: str | None = None,
    lifecycle: str | None = "active",
    package: str | None = "BGA-96",
) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=normalized_mpn,
        display_mpn=display_mpn or normalized_mpn,
        manufacturer=manufacturer,
        category=category,
        lifecycle_status=lifecycle,
        package_type=package,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(card)
    db.flush()
    return card


def _make_vendor_history(db: Session, card: MaterialCard, *, vendor: str, price: float) -> MaterialVendorHistory:
    mvh = MaterialVendorHistory(
        material_card_id=card.id,
        vendor_name=vendor,
        last_price=price,
        created_at=datetime.now(UTC),
    )
    db.add(mvh)
    db.flush()
    return mvh


# ── Materials export ─────────────────────────────────────────────────────


def test_materials_export_returns_csv_attachment(client: TestClient, db_session: Session):
    """200 + text/csv + attachment Content-Disposition with the fixed filename."""
    _make_card(db_session, normalized_mpn="MT40A1G8")
    db_session.commit()

    resp = client.get(MATERIALS_EXPORT_URL)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    assert 'filename="materials_export.csv"' in disposition


def test_materials_export_header_and_one_row_per_card(client: TestClient, db_session: Session):
    """Header row + one data row per matching material card, with key fields present."""
    a = _make_card(db_session, normalized_mpn="MT40A1G8", manufacturer="Micron")
    _make_vendor_history(db_session, a, vendor="Digi-Key", price=4.5678)
    _make_card(db_session, normalized_mpn="K4A8G165", manufacturer="Samsung")
    db_session.commit()

    rows = _parse_csv(client.get(MATERIALS_EXPORT_URL).text)

    header = rows[0]
    assert header[0] == "MPN"
    for col in (
        "Manufacturer",
        "Category",
        "Package",
        "Lifecycle",
        "Enrichment Status",
        "Vendor Count",
        "Best Price",
        "Created",
        "Updated",
    ):
        assert col in header
    # Header + exactly two card rows.
    assert len(rows) == 3

    body = _body(rows)
    assert "MT40A1G8" in body
    assert "K4A8G165" in body
    assert "Micron" in body
    assert "Samsung" in body
    assert "4.5678" in body  # min recorded vendor price


def test_materials_export_respects_search_filter(client: TestClient, db_session: Session):
    """The q search filter (MPN/manufacturer/description) is honored."""
    _make_card(db_session, normalized_mpn="MT40A1G8", manufacturer="Micron")
    _make_card(db_session, normalized_mpn="K4A8G165", manufacturer="Samsung")
    db_session.commit()

    rows = _parse_csv(client.get(MATERIALS_EXPORT_URL, params={"q": "MT40A"}).text)

    body = _body(rows)
    assert "MT40A1G8" in body
    assert "K4A8G165" not in body
    assert len(rows) == 2  # header + one matching card


def test_materials_export_unauthenticated_rejected(unauthenticated_client: TestClient, db_session: Session):
    """Unauthenticated requests are rejected like the materials list route (401/403)."""
    resp = unauthenticated_client.get(MATERIALS_EXPORT_URL, follow_redirects=False)
    assert resp.status_code in (401, 403)


def test_materials_export_button_rendered_in_toolbar(client: TestClient, db_session: Session):
    """The materials results partial renders the Export CSV anchor: a plain (non-htmx)
    download that points at the export endpoint and opts out of nav-boost."""
    _make_card(db_session, normalized_mpn="MT40A1G8")
    db_session.commit()

    html = client.get(MATERIALS_LIST_URL).text

    assert "Export CSV" in html
    assert 'hx-boost="false"' in html
    assert "/v2/partials/materials/export" in html
