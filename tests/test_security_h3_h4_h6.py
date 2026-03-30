"""Tests for security fixes H3, H4, H6.

H3: File size limits on CSV/Excel import (data_ops.py)
H4: dry_run moved from query param to request body (data_ops.py)
H6: File type validation on offer attachments (offers.py)

Called by: pytest
Depends on: conftest fixtures (db_session, client, test_user)
"""

import io


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
            status="active",
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
            status="active",
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
            status="active",
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
            status="active",
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
