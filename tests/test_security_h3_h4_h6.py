"""Tests for security fixes H3, H4, H6.

H3: File size limits on CSV/Excel import (data_ops.py)
H4: dry_run moved from query param to request body (data_ops.py)
H6: File type validation on offer attachments (offers.py)

Called by: pytest
Depends on: conftest fixtures (db_session, client, test_user)
"""

import io
from unittest.mock import patch

# ── H3: CSV Upload File Size Limits ──────────────────────────────────


class TestCSVFileSizeLimits:
    """Verify oversized CSV uploads are rejected with 400."""

    def test_customer_csv_too_large(self, client):
        """Customer CSV import rejects files over 10 MB."""
        big_content = b"x" * (10 * 1024 * 1024 + 1)
        resp = client.post(
            "/api/admin/import/customers",
            files={"file": ("big.csv", io.BytesIO(big_content), "text/csv")},
        )
        assert resp.status_code == 400
        assert "too large" in resp.json()["error"].lower()

    def test_vendor_csv_too_large(self, client):
        """Vendor CSV import rejects files over 10 MB."""
        big_content = b"x" * (10 * 1024 * 1024 + 1)
        resp = client.post(
            "/api/admin/import/vendors",
            files={"file": ("big.csv", io.BytesIO(big_content), "text/csv")},
        )
        assert resp.status_code == 400
        assert "too large" in resp.json()["error"].lower()

    def test_customer_csv_within_limit(self, client):
        """Customer CSV import accepts files under 10 MB (may fail on content, not
        size)."""
        small_content = b"company_name\nAcme Corp\n"
        resp = client.post(
            "/api/admin/import/customers",
            files={"file": ("ok.csv", io.BytesIO(small_content), "text/csv")},
        )
        # Should not be a size error — might be 200 or other validation error
        if resp.status_code == 400:
            assert "too large" not in resp.json().get("error", "").lower()


# ── H4: dry_run Moved to Request Body ───────────────────────────────


class TestDataCleanupRequestBody:
    """Verify data cleanup endpoints use request body, not query params."""

    @patch("app.services.data_cleanup_service.scan_junk_data", return_value={"flagged": []})
    def test_scan_accepts_body_dry_run(self, mock_scan, client):
        """POST /api/admin/data-cleanup/scan accepts dry_run in JSON body."""
        resp = client.post(
            "/api/admin/data-cleanup/scan",
            json={"dry_run": True},
        )
        assert resp.status_code == 200
        mock_scan.assert_called_once()
        _, kwargs = mock_scan.call_args
        assert kwargs["dry_run"] is True

    @patch("app.services.data_cleanup_service.fix_sentinel_dates", return_value={"fixed": 0})
    def test_fix_dates_accepts_body_dry_run(self, mock_fix, client):
        """POST /api/admin/data-cleanup/fix-dates accepts dry_run in JSON body."""
        resp = client.post(
            "/api/admin/data-cleanup/fix-dates",
            json={"dry_run": True},
        )
        assert resp.status_code == 200
        mock_fix.assert_called_once()

    def test_scan_requires_confirm_when_not_dry_run(self, client):
        """dry_run=False without confirm=True returns 400."""
        resp = client.post(
            "/api/admin/data-cleanup/scan",
            json={"dry_run": False, "confirm": False},
        )
        assert resp.status_code == 400
        assert "confirm" in resp.json()["error"].lower()

    def test_fix_dates_requires_confirm_when_not_dry_run(self, client):
        """dry_run=False without confirm=True returns 400."""
        resp = client.post(
            "/api/admin/data-cleanup/fix-dates",
            json={"dry_run": False, "confirm": False},
        )
        assert resp.status_code == 400
        assert "confirm" in resp.json()["error"].lower()

    @patch("app.services.data_cleanup_service.scan_junk_data", return_value={"flagged": []})
    def test_scan_executes_with_confirm(self, mock_scan, client):
        """dry_run=False + confirm=True proceeds normally."""
        resp = client.post(
            "/api/admin/data-cleanup/scan",
            json={"dry_run": False, "confirm": True},
        )
        assert resp.status_code == 200
        _, kwargs = mock_scan.call_args
        assert kwargs["dry_run"] is False

    def test_scan_defaults_to_dry_run(self, client):
        """Empty body defaults to dry_run=True (safe default)."""
        with patch("app.services.data_cleanup_service.scan_junk_data", return_value={"ok": True}) as mock_scan:
            resp = client.post(
                "/api/admin/data-cleanup/scan",
                json={},
            )
            assert resp.status_code == 200
            _, kwargs = mock_scan.call_args
            assert kwargs["dry_run"] is True


# ── H6: Offer Attachment File Type Validation ────────────────────────


class TestOfferAttachmentFileType:
    """Verify rejected file types return 400."""

    def test_rejected_extension_exe(self, client, db_session):
        """Uploading .exe attachment returns 400."""
        from app.models import Offer, Requisition, User

        # Create minimal offer for the endpoint
        user = db_session.query(User).first()
        req = Requisition(
            name="REQ-ATT-001",
            customer_name="Test Co",
            status="open",
            created_by=user.id,
        )
        db_session.add(req)
        db_session.flush()
        offer = Offer(
            requisition_id=req.id,
            vendor_name="Test Vendor",
            mpn="TEST-001",
            qty_available=100,
            unit_price=1.0,
            status="new",
        )
        db_session.add(offer)
        db_session.commit()

        content = b"fake exe content"
        resp = client.post(
            f"/api/offers/{offer.id}/attachments",
            files={"file": ("malware.exe", io.BytesIO(content), "application/octet-stream")},
        )
        assert resp.status_code == 400
        assert "not allowed" in resp.json()["error"].lower()

    def test_rejected_extension_bat(self, client, db_session):
        """Uploading .bat attachment returns 400."""
        from app.models import Offer, Requisition, User

        user = db_session.query(User).first()
        req = Requisition(
            name="REQ-ATT-002",
            customer_name="Test Co",
            status="open",
            created_by=user.id,
        )
        db_session.add(req)
        db_session.flush()
        offer = Offer(
            requisition_id=req.id,
            vendor_name="Test Vendor",
            mpn="TEST-002",
            qty_available=100,
            unit_price=1.0,
            status="new",
        )
        db_session.add(offer)
        db_session.commit()

        content = b"echo hello"
        resp = client.post(
            f"/api/offers/{offer.id}/attachments",
            files={"file": ("script.bat", io.BytesIO(content), "application/octet-stream")},
        )
        assert resp.status_code == 400
        assert "not allowed" in resp.json()["error"].lower()

    def test_allowed_extension_pdf_reaches_onedrive(self, client, db_session):
        """Uploading .pdf passes file type check (may fail at OneDrive step)."""
        from app.models import Offer, Requisition, User

        user = db_session.query(User).first()
        req = Requisition(
            name="REQ-ATT-003",
            customer_name="Test Co",
            status="open",
            created_by=user.id,
        )
        db_session.add(req)
        db_session.flush()
        offer = Offer(
            requisition_id=req.id,
            vendor_name="Test Vendor",
            mpn="TEST-003",
            qty_available=100,
            unit_price=1.0,
            status="new",
        )
        db_session.add(offer)
        db_session.commit()

        content = b"%PDF-1.4 fake pdf"
        resp = client.post(
            f"/api/offers/{offer.id}/attachments",
            files={"file": ("doc.pdf", io.BytesIO(content), "application/pdf")},
        )
        # Should NOT be a file-type error; will likely fail at OneDrive auth
        if resp.status_code == 400:
            assert "not allowed" not in resp.json().get("error", "").lower()

    def test_no_extension_rejected(self, client, db_session):
        """Uploading a file with no extension returns 400."""
        from app.models import Offer, Requisition, User

        user = db_session.query(User).first()
        req = Requisition(
            name="REQ-ATT-004",
            customer_name="Test Co",
            status="open",
            created_by=user.id,
        )
        db_session.add(req)
        db_session.flush()
        offer = Offer(
            requisition_id=req.id,
            vendor_name="Test Vendor",
            mpn="TEST-004",
            qty_available=100,
            unit_price=1.0,
            status="new",
        )
        db_session.add(offer)
        db_session.commit()

        content = b"some data"
        resp = client.post(
            f"/api/offers/{offer.id}/attachments",
            files={"file": ("noext", io.BytesIO(content), "application/octet-stream")},
        )
        assert resp.status_code == 400
        assert "not allowed" in resp.json()["error"].lower()
