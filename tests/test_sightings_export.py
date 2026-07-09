"""Tests for the Sightings Board bulk CSV export (GET /v2/sightings/export).

Verifies the export streams CSV with the attachment headers, one row per matching
Sighting, and that it mirrors the board's filter parity (same predicates as
GET /v2/partials/sightings) plus the same auth rejection.

Called by: pytest
Depends on: conftest.py fixtures (db_session, test_user, client, unauthenticated_client),
            app.models.sourcing (Requisition, Requirement, Sighting)
"""

import csv
import io
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.sourcing import Requirement, Requisition, Sighting

EXPORT_URL = "/v2/sightings/export"


def _make_requirement(
    db: Session,
    *,
    mpn: str,
    manufacturer: str = "Texas Instruments",
    sourcing_status: str = "open",
    customer: str = "Acme Corp",
) -> Requirement:
    """Seed an active requisition + requirement that shows on the sightings board."""
    req = Requisition(
        name=f"RFQ-{mpn}",
        status="open",
        customer_name=customer,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        manufacturer=manufacturer,
        target_qty=100,
        sourcing_status=sourcing_status,
        created_at=datetime.now(UTC),
    )
    db.add(requirement)
    db.flush()
    return requirement


def _make_sighting(db: Session, requirement: Requirement, *, vendor: str, **kwargs) -> Sighting:
    sighting = Sighting(
        requirement_id=requirement.id,
        vendor_name=vendor,
        created_at=datetime.now(UTC),
        **kwargs,
    )
    db.add(sighting)
    db.flush()
    return sighting


def _parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def test_export_returns_csv_attachment(client: TestClient, db_session: Session):
    """200 + text/csv + attachment Content-Disposition with the fixed filename."""
    r = _make_requirement(db_session, mpn="LM317T")
    _make_sighting(db_session, r, vendor="Digi-Key")
    db_session.commit()

    resp = client.get(EXPORT_URL)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    assert 'filename="sightings_export.csv"' in disposition


def test_export_header_and_one_row_per_sighting(client: TestClient, db_session: Session):
    """Header row + one data row per matching sighting, with the key fields present."""
    r = _make_requirement(db_session, mpn="LM317T", manufacturer="Texas Instruments")
    _make_sighting(
        db_session,
        r,
        vendor="Digi-Key",
        mpn_matched="LM317T",
        manufacturer="Texas Instruments",
        qty_available=500,
        unit_price=1.2345,
        currency="USD",
        condition="new",
        source_type="brokerbin",
        score=88.0,
        evidence_tier="T1",
    )
    _make_sighting(db_session, r, vendor="Mouser", mpn_matched="LM317T", qty_available=200)
    db_session.commit()

    rows = _parse_csv(client.get(EXPORT_URL).text)

    header = rows[0]
    assert header[0] == "Requirement ID"
    assert "Vendor" in header and "Unit Price" in header and "Evidence Tier" in header
    # Header + exactly two sighting rows.
    assert len(rows) == 3

    body = "\n".join(",".join(row) for row in rows[1:])
    assert "Digi-Key" in body
    assert "Mouser" in body
    assert "LM317T" in body
    assert "Acme Corp" in body
    assert "1.2345" in body
    assert "brokerbin" in body
    assert "T1" in body


def test_export_respects_manufacturer_filter(client: TestClient, db_session: Session):
    """The manufacturer filter (a board filter on Requirement.manufacturer) is
    honored."""
    ti = _make_requirement(db_session, mpn="LM317T", manufacturer="Texas Instruments")
    adi = _make_requirement(db_session, mpn="AD8232", manufacturer="Analog Devices")
    _make_sighting(db_session, ti, vendor="Digi-Key", mpn_matched="LM317T")
    _make_sighting(db_session, adi, vendor="Arrow", mpn_matched="AD8232")
    db_session.commit()

    rows = _parse_csv(client.get(EXPORT_URL, params={"manufacturer": "Texas"}).text)

    body = "\n".join(",".join(row) for row in rows[1:])
    assert "Digi-Key" in body  # TI sighting kept
    assert "Arrow" not in body  # ADI sighting excluded
    assert len(rows) == 2  # header + one matching sighting


def test_export_respects_status_filter(client: TestClient, db_session: Session):
    """The status filter selects only sightings on requirements in that sourcing
    status."""
    open_req = _make_requirement(db_session, mpn="LM317T", sourcing_status="open")
    sourcing_req = _make_requirement(db_session, mpn="AD8232", sourcing_status="sourcing")
    _make_sighting(db_session, open_req, vendor="OpenVendor")
    _make_sighting(db_session, sourcing_req, vendor="SourcingVendor")
    db_session.commit()

    rows = _parse_csv(client.get(EXPORT_URL, params={"status": "sourcing"}).text)

    body = "\n".join(",".join(row) for row in rows[1:])
    assert "SourcingVendor" in body
    assert "OpenVendor" not in body
    assert len(rows) == 2  # header + one matching sighting


def test_export_unauthenticated_rejected(unauthenticated_client: TestClient, db_session: Session):
    """Unauthenticated requests are rejected like the board route (401/403)."""
    resp = unauthenticated_client.get(EXPORT_URL, follow_redirects=False)
    assert resp.status_code in (401, 403)


def test_export_button_rendered_in_board_toolbar(client: TestClient, db_session: Session):
    """The board table partial renders the Export CSV anchor: a plain (non-htmx) download
    that points at the export endpoint and opts out of nav-boost."""
    r = _make_requirement(db_session, mpn="LM317T")
    _make_sighting(db_session, r, vendor="Digi-Key")
    db_session.commit()

    html = client.get("/v2/partials/sightings").text

    assert "Export CSV" in html
    assert 'hx-boost="false"' in html
    assert "/v2/sightings/export" in html
