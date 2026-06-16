"""Tests for security fixes H3, H4, H6.

H3: File size limits on CSV/Excel import (data_ops.py)
H4: dry_run moved from query param to request body (data_ops.py)
H6: File type validation on offer attachments (offers.py)

Called by: pytest
Depends on: conftest fixtures (db_session, client, test_user)
"""

import io

import pytest


def _make_offer(db_session, req_name, mpn):
    """Create a minimal Requisition + Offer and return the offer id."""
    from app.models import Offer, Requisition, User

    user = db_session.query(User).first()
    req = Requisition(
        name=req_name,
        customer_name="Test Co",
        status="active",
        created_by=user.id,
    )
    db_session.add(req)
    db_session.flush()
    offer = Offer(
        requisition_id=req.id,
        vendor_name="Test Vendor",
        mpn=mpn,
        qty_available=100,
        unit_price=1.0,
        status="new",
    )
    db_session.add(offer)
    db_session.commit()
    return offer.id


class TestOfferAttachmentFileType:
    """Verify rejected file types return 400."""

    @pytest.mark.parametrize(
        "req_name,mpn,filename,content",
        [
            ("REQ-ATT-001", "TEST-001", "malware.exe", b"fake exe content"),
            ("REQ-ATT-002", "TEST-002", "script.bat", b"echo hello"),
            ("REQ-ATT-004", "TEST-004", "noext", b"some data"),
        ],
        ids=["exe", "bat", "no_extension"],
    )
    def test_rejected_file_type(self, client, db_session, req_name, mpn, filename, content):
        """Uploading a disallowed (or extension-less) attachment returns 400."""
        offer_id = _make_offer(db_session, req_name, mpn)

        resp = client.post(
            f"/api/offers/{offer_id}/attachments",
            files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
        )
        assert resp.status_code == 400
        assert "not allowed" in resp.json()["error"].lower()

    def test_allowed_extension_pdf_reaches_onedrive(self, client, db_session):
        """Uploading .pdf passes file type check (may fail at OneDrive step)."""
        offer_id = _make_offer(db_session, "REQ-ATT-003", "TEST-003")

        content = b"%PDF-1.4 fake pdf"
        resp = client.post(
            f"/api/offers/{offer_id}/attachments",
            files={"file": ("doc.pdf", io.BytesIO(content), "application/pdf")},
        )
        # Should NOT be a file-type error; will likely fail at OneDrive auth
        if resp.status_code == 400:
            assert "not allowed" not in resp.json().get("error", "").lower()
