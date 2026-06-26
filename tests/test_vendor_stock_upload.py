"""tests/test_vendor_stock_upload.py — Vendors-page stock-list upload + removals.

Covers:
- POST /v2/partials/vendors/import-stock (HTMX modal): valid CSV ingests rows as
  MaterialCard + MaterialVendorHistory and renders an HTML result banner; invalid file
  type / missing vendor / missing file are rejected with an HTML error (no 500).
- The shared ``stock_list_ingest.ingest_stock_list`` service (valid + invalid).
- Removal of the old vendor CSV-import route and the CRM "Find by Part" sub-tab/route.

Called by: pytest
Depends on: routers/htmx_views.py, routers/materials.py, services/stock_list_ingest.py
"""

import io
from pathlib import Path

import pytest

from app.models import MaterialCard, MaterialVendorHistory, VendorCard
from app.services.stock_list_ingest import StockListValidationError, ingest_stock_list
from app.vendor_utils import normalize_vendor_name

# ── HTMX upload route: POST /v2/partials/vendors/import-stock ──────────────


def test_stock_upload_valid_csv_ingests(client, db_session, monkeypatch):
    """A valid CSV creates MaterialCard + MaterialVendorHistory rows and returns the
    success banner."""
    monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())

    csv_content = b"mpn,qty,price,manufacturer\nLM358N,1000,0.25,Texas Instruments\nNE555P,500,0.30,TI"
    resp = client.post(
        "/v2/partials/vendors/import-stock",
        data={"vendor_name": "Stock Upload Vendor"},
        files={"file": ("stock.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert resp.status_code == 200
    assert "Imported" in resp.text
    assert "Stock Upload Vendor" in resp.text

    # Vendor + cards persisted
    norm = normalize_vendor_name("Stock Upload Vendor")
    vc = db_session.query(VendorCard).filter_by(normalized_name=norm).first()
    assert vc is not None
    card = db_session.query(MaterialCard).filter_by(normalized_mpn="lm358n").first()
    assert card is not None
    mvh = db_session.query(MaterialVendorHistory).filter_by(material_card_id=card.id, vendor_name=norm).first()
    assert mvh is not None
    assert mvh.source_type == "stock_list"


def test_stock_upload_invalid_file_type_rejected(client, db_session, monkeypatch):
    """A .pdf upload is rejected with an HTML error banner (not a 500)."""
    monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())

    resp = client.post(
        "/v2/partials/vendors/import-stock",
        data={"vendor_name": "Bad File Vendor"},
        files={"file": ("stock.pdf", io.BytesIO(b"%PDF-1.4 not a spreadsheet"), "application/pdf")},
    )
    assert resp.status_code == 200  # HTML banner, not an HTTP error
    assert "Invalid file type" in resp.text
    # Nothing ingested
    norm = normalize_vendor_name("Bad File Vendor")
    assert db_session.query(VendorCard).filter_by(normalized_name=norm).first() is None


def test_stock_upload_missing_vendor_name_rejected(client, db_session, monkeypatch):
    """Missing vendor name is rejected with an HTML error banner."""
    monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())

    resp = client.post(
        "/v2/partials/vendors/import-stock",
        data={"vendor_name": ""},
        files={"file": ("stock.csv", io.BytesIO(b"mpn,qty\nABC123,10"), "text/csv")},
    )
    assert resp.status_code == 200
    assert "Vendor name is required" in resp.text


def test_stock_upload_missing_file_rejected(client, db_session, monkeypatch):
    """Missing file is rejected with an HTML error banner."""
    monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())

    resp = client.post(
        "/v2/partials/vendors/import-stock",
        data={"vendor_name": "No File Vendor"},
    )
    assert resp.status_code == 200
    assert "file is required" in resp.text.lower()


def test_stock_upload_skips_bad_rows_with_warnings(client, db_session, monkeypatch):
    """Rows with no recognizable MPN are skipped and surfaced as warnings."""
    monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())

    csv_content = b"mpn,qty,price\n,100,0.50\nWARN001,200,0.75"
    resp = client.post(
        "/v2/partials/vendors/import-stock",
        data={"vendor_name": "Warn Vendor"},
        files={"file": ("stock.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert resp.status_code == 200
    assert "Imported 1" in resp.text
    assert "skipped" in resp.text.lower()


# ── Shared service: ingest_stock_list ─────────────────────────────────────


def test_ingest_service_valid(db_session):
    """The service upserts a vendor + cards and reports counts."""
    result = ingest_stock_list(
        db_session,
        filename="stock.csv",
        content=b"mpn,qty,price\nSVC001,10,1.50\nSVC002,20,2.50",
        vendor_name="Service Vendor",
    )
    assert result.imported_rows == 2
    assert result.total_rows == 2
    assert result.vendor_name == "Service Vendor"
    assert result.vendor_card_id is not None
    assert result.new_vendor is True


def test_ingest_service_invalid_file_type_raises(db_session):
    """Invalid extension raises StockListValidationError (400)."""
    with pytest.raises(StockListValidationError) as exc:
        ingest_stock_list(db_session, filename="stock.docx", content=b"junk", vendor_name="X Vendor")
    assert exc.value.status_code == 400


def test_ingest_service_oversize_raises(db_session):
    """A >10MB file raises StockListValidationError (413)."""
    with pytest.raises(StockListValidationError) as exc:
        ingest_stock_list(
            db_session,
            filename="big.csv",
            content=b"x" * 10_000_001,
            vendor_name="Big Vendor",
        )
    assert exc.value.status_code == 413


def test_ingest_service_existing_vendor_reused(db_session):
    """An existing vendor (by normalized name) is reused, not duplicated."""
    norm = normalize_vendor_name("Reuse Vendor")
    db_session.add(VendorCard(normalized_name=norm, display_name="Reuse Vendor", emails=[], phones=[]))
    db_session.commit()

    result = ingest_stock_list(
        db_session,
        filename="stock.csv",
        content=b"mpn,qty,price\nREUSE001,5,1.00",
        vendor_name="Reuse Vendor",
    )
    assert result.new_vendor is False
    assert db_session.query(VendorCard).filter_by(normalized_name=norm).count() == 1


# ── Removals: old vendor-import route + CRM "Find by Part" sub-tab ─────────


def test_old_vendor_import_route_removed(client):
    """The old admin vendor-CSV import endpoint no longer exists (404)."""
    resp = client.post(
        "/v2/partials/admin/import/vendors",
        files={"file": ("v.csv", io.BytesIO(b"name\nAcme"), "text/csv")},
    )
    assert resp.status_code == 404


def test_find_by_part_route_removed(client):
    """The CRM 'Find by Part' sub-tab route no longer renders.

    With the dedicated handler gone, the path is captured by
    ``GET /v2/partials/vendors/{vendor_id}`` which can't parse ``find-by-part`` as an int
    (422). Either way it must NOT serve the find-by-part view (200 + that template).
    """
    resp = client.get("/v2/partials/vendors/find-by-part?mpn=LM358N")
    assert resp.status_code != 200
    assert "Find vendors for a part" not in resp.text


def test_find_by_part_handler_symbol_gone():
    """The handler function is removed from the router module."""
    import app.routers.htmx_views as views

    assert not hasattr(views, "find_by_part_partial")
    assert not hasattr(views, "import_vendors_csv")


def test_find_by_part_template_deleted():
    """The find_by_part.html template file is deleted."""
    assert not Path("app/templates/htmx/partials/vendors/find_by_part.html").exists()


def test_vendors_list_has_no_find_by_part_tab_and_has_stock_upload():
    """The Vendors list partial drops the Find-by-Part tab and adds the stock-upload
    button + endpoint."""
    src = Path("app/templates/htmx/partials/vendors/list.html").read_text()
    assert "find-by-part" not in src
    assert "Find by Part" not in src
    assert "Upload stock list" in src
    assert "/v2/partials/vendors/import-stock" in src
    # Old vendor-import endpoint is no longer wired in the template
    assert "/v2/partials/admin/import/vendors" not in src
