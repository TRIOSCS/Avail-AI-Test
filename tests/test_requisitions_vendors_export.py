"""Tests for the Requisitions and Vendors list CSV exports.

Covers GET /v2/partials/requisitions/export and GET /v2/partials/vendors/export:
each streams a CSV attachment, writes a header row + one row per matching record,
mirrors its list route's filter parity (only matching records export), enforces the
same auth as its list route, and renders a plain ``hx-boost="false"`` Export CSV anchor
in its list toolbar.

Called by: pytest
Depends on: conftest.py fixtures (db_session, test_user, client, unauthenticated_client),
            app.models.sourcing (Requisition, Requirement),
            app.models.vendors (VendorCard, VendorContact)
"""

import csv
import io
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.sourcing import Requirement, Requisition
from app.models.vendors import VendorCard, VendorContact

REQ_EXPORT_URL = "/v2/partials/requisitions/export"
VENDOR_EXPORT_URL = "/v2/partials/vendors/export"


def _parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def _body(rows: list[list[str]]) -> str:
    """Flatten the data rows (everything after the header) into one searchable
    string."""
    return "\n".join(",".join(row) for row in rows[1:])


# ── Requisitions export ──────────────────────────────────────────────────


def _make_requisition(
    db: Session,
    *,
    name: str,
    customer: str = "Acme Corp",
    status: str = "open",
    n_requirements: int = 0,
    is_scratch: bool = False,
    claimed_by_id: int | None = None,
    opportunity_value=None,
    deadline: str | None = None,
) -> Requisition:
    req = Requisition(
        name=name,
        customer_name=customer,
        status=status,
        is_scratch=is_scratch,
        claimed_by_id=claimed_by_id,
        opportunity_value=opportunity_value,
        deadline=deadline,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    for i in range(n_requirements):
        db.add(
            Requirement(
                requisition_id=req.id,
                primary_mpn=f"{name}-MPN-{i}",
                manufacturer="Texas Instruments",
                target_qty=10,
                created_at=datetime.now(timezone.utc),
            )
        )
    db.flush()
    return req


def test_requisitions_export_returns_csv_attachment(client: TestClient, db_session: Session):
    """200 + text/csv + attachment Content-Disposition with the fixed filename."""
    _make_requisition(db_session, name="RFQ-One")
    db_session.commit()

    resp = client.get(REQ_EXPORT_URL)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    assert 'filename="requisitions_export.csv"' in disposition


def test_requisitions_export_header_and_one_row_per_requisition(client: TestClient, db_session: Session, test_user):
    """Header row + one data row per matching requisition, key fields present."""
    _make_requisition(
        db_session,
        name="RFQ-Alpha",
        customer="Acme Corp",
        n_requirements=2,
        claimed_by_id=test_user.id,
        opportunity_value=1500,
        deadline="2026-08-01",
    )
    _make_requisition(db_session, name="RFQ-Beta", customer="Beta LLC")
    db_session.commit()

    rows = _parse_csv(client.get(REQ_EXPORT_URL).text)

    header = rows[0]
    assert header == ["Name", "Customer", "Status", "Owner", "Value", "Deadline", "Created", "# Requirements"]
    # Header + exactly two requisition rows.
    assert len(rows) == 3

    body = _body(rows)
    assert "RFQ-Alpha" in body
    assert "RFQ-Beta" in body
    assert "Acme Corp" in body
    assert test_user.name in body  # Owner (claimed_by)
    assert "1500" in body  # opportunity_value
    assert "2026-08-01" in body  # deadline
    # The requirement-count column reflects the 2 seeded requirements on RFQ-Alpha.
    alpha_row = next(r for r in rows[1:] if r[0] == "RFQ-Alpha")
    assert alpha_row[-1] == "2"


def test_requisitions_export_respects_status_filter(client: TestClient, db_session: Session):
    """The status filter (same predicate as the list) selects only matching
    requisitions."""
    _make_requisition(db_session, name="RFQ-Open", status="open")
    _make_requisition(db_session, name="RFQ-Won", status="won")
    db_session.commit()

    rows = _parse_csv(client.get(REQ_EXPORT_URL, params={"status": "won"}).text)

    body = _body(rows)
    assert "RFQ-Won" in body
    assert "RFQ-Open" not in body
    assert len(rows) == 2  # header + one matching requisition


def test_requisitions_export_excludes_scratch(client: TestClient, db_session: Session):
    """Scratch requisitions are hidden from the list, so they never export either."""
    _make_requisition(db_session, name="RFQ-Real", is_scratch=False)
    _make_requisition(db_session, name="RFQ-Scratch", is_scratch=True)
    db_session.commit()

    rows = _parse_csv(client.get(REQ_EXPORT_URL).text)

    body = _body(rows)
    assert "RFQ-Real" in body
    assert "RFQ-Scratch" not in body
    assert len(rows) == 2  # header + the one non-scratch requisition


def test_requisitions_export_unauthenticated_rejected(unauthenticated_client: TestClient, db_session: Session):
    """Unauthenticated requests are rejected like the list route (401/403)."""
    resp = unauthenticated_client.get(REQ_EXPORT_URL, follow_redirects=False)
    assert resp.status_code in (401, 403)


def test_requisitions_export_button_rendered_in_list_toolbar(client: TestClient, db_session: Session):
    """The requisitions list partial renders the Export CSV anchor: a plain (non-htmx)
    download pointing at the export endpoint that opts out of nav-boost."""
    _make_requisition(db_session, name="RFQ-One")
    db_session.commit()

    html = client.get("/v2/partials/requisitions").text

    assert "Export CSV" in html
    assert 'hx-boost="false"' in html
    assert "/v2/partials/requisitions/export" in html


# ── Vendors export ───────────────────────────────────────────────────────


def _make_vendor(
    db: Session,
    *,
    name: str,
    domain: str | None = None,
    website: str | None = None,
    source: str | None = None,
    is_blacklisted: bool = False,
    is_active: bool = True,
    commodity_tags: list[str] | None = None,
    n_contacts: int = 0,
) -> VendorCard:
    card = VendorCard(
        normalized_name=name.lower(),
        display_name=name,
        domain=domain,
        website=website,
        source=source,
        is_blacklisted=is_blacklisted,
        is_active=is_active,
        commodity_tags=commodity_tags or [],
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.flush()
    for i in range(n_contacts):
        db.add(
            VendorContact(
                vendor_card_id=card.id,
                full_name=f"{name} Contact {i}",
                email=f"contact{i}@{domain or 'example.com'}",
                source="test",
            )
        )
    db.flush()
    return card


def test_vendors_export_returns_csv_attachment(client: TestClient, db_session: Session):
    """200 + text/csv + attachment Content-Disposition with the fixed filename."""
    _make_vendor(db_session, name="Arrow Electronics")
    db_session.commit()

    resp = client.get(VENDOR_EXPORT_URL)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    assert 'filename="vendors_export.csv"' in disposition


def test_vendors_export_header_and_one_row_per_vendor(client: TestClient, db_session: Session):
    """Header row + one data row per matching vendor, key fields present."""
    _make_vendor(
        db_session,
        name="Arrow Electronics",
        domain="arrow.com",
        website="https://arrow.com",
        source="brokerbin",
        commodity_tags=["MCU", "FPGA"],
        n_contacts=3,
    )
    _make_vendor(db_session, name="Mouser", domain="mouser.com")
    db_session.commit()

    rows = _parse_csv(client.get(VENDOR_EXPORT_URL).text)

    header = rows[0]
    assert header == [
        "Vendor",
        "Domain",
        "Website",
        "Source",
        "Blacklisted",
        "Active",
        "Commodity Tags",
        "Contacts",
        "Created",
    ]
    # Header + exactly two vendor rows.
    assert len(rows) == 3

    body = _body(rows)
    assert "Arrow Electronics" in body
    assert "Mouser" in body
    assert "arrow.com" in body
    assert "https://arrow.com" in body
    assert "brokerbin" in body  # Source (provenance)
    assert "MCU; FPGA" in body  # commodity tags joined
    # Contact-count column reflects the 3 seeded contacts on Arrow.
    arrow_row = next(r for r in rows[1:] if r[0] == "Arrow Electronics")
    assert arrow_row[3] == "brokerbin"
    assert arrow_row[4] == "No"  # Blacklisted
    assert arrow_row[5] == "Yes"  # Active
    assert arrow_row[7] == "3"  # Contacts


def test_vendors_export_respects_search_filter(client: TestClient, db_session: Session):
    """The q search filter (same predicate as the list) selects only matching
    vendors."""
    _make_vendor(db_session, name="Arrow Electronics", domain="arrow.com")
    _make_vendor(db_session, name="Mouser", domain="mouser.com")
    db_session.commit()

    rows = _parse_csv(client.get(VENDOR_EXPORT_URL, params={"q": "Arrow"}).text)

    body = _body(rows)
    assert "Arrow Electronics" in body
    assert "Mouser" not in body
    assert len(rows) == 2  # header + one matching vendor


def test_vendors_export_hides_blacklisted_by_default(client: TestClient, db_session: Session):
    """Blacklisted vendors are hidden by default (hide_blacklisted=True), matching the
    list; toggling the filter off includes them."""
    _make_vendor(db_session, name="CleanVendor", is_blacklisted=False)
    _make_vendor(db_session, name="BadVendor", is_blacklisted=True)
    db_session.commit()

    default_rows = _parse_csv(client.get(VENDOR_EXPORT_URL).text)
    default_body = _body(default_rows)
    assert "CleanVendor" in default_body
    assert "BadVendor" not in default_body
    assert len(default_rows) == 2  # header + the one non-blacklisted vendor

    shown_rows = _parse_csv(client.get(VENDOR_EXPORT_URL, params={"hide_blacklisted": "false"}).text)
    shown_body = _body(shown_rows)
    assert "BadVendor" in shown_body
    assert len(shown_rows) == 3  # header + both vendors


def test_vendors_export_unauthenticated_rejected(unauthenticated_client: TestClient, db_session: Session):
    """Unauthenticated requests are rejected like the list route (401/403)."""
    resp = unauthenticated_client.get(VENDOR_EXPORT_URL, follow_redirects=False)
    assert resp.status_code in (401, 403)


def test_vendors_export_button_rendered_in_list_header(client: TestClient, db_session: Session):
    """The vendor list partial renders the Export CSV anchor: a plain (non-htmx) download
    pointing at the export endpoint that opts out of nav-boost."""
    _make_vendor(db_session, name="Arrow Electronics")
    db_session.commit()

    html = client.get("/v2/partials/vendors").text

    assert "Export CSV" in html
    assert 'hx-boost="false"' in html
    assert "/v2/partials/vendors/export" in html
