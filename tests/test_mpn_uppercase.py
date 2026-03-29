"""Tests for MPN auto-capitalization and substitutes display.

Called by: pytest
Depends on: conftest.py fixtures, app models, app constants
"""

from app.constants import RequisitionStatus, SourcingStatus
from app.models.sourcing import Requirement, Requisition


class TestMPNUppercaseValidator:
    def test_primary_mpn_uppercased_on_create(self, db_session):
        req = Requisition(name="Test", status=RequisitionStatus.ACTIVE, customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="ne5559",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status=SourcingStatus.OPEN,
        )
        db_session.add(r)
        db_session.flush()
        assert r.primary_mpn == "NE5559"

    def test_primary_mpn_uppercased_on_update(self, db_session):
        req = Requisition(name="Test", status=RequisitionStatus.ACTIVE, customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="ABC123",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status=SourcingStatus.OPEN,
        )
        db_session.add(r)
        db_session.flush()
        r.primary_mpn = "xyz789"
        assert r.primary_mpn == "XYZ789"

    def test_customer_pn_uppercased(self, db_session):
        req = Requisition(name="Test", status=RequisitionStatus.ACTIVE, customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="ABC123",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status=SourcingStatus.OPEN,
            customer_pn="cust-part-01",
        )
        db_session.add(r)
        db_session.flush()
        assert r.customer_pn == "CUST-PART-01"

    def test_oem_pn_uppercased(self, db_session):
        req = Requisition(name="Test", status=RequisitionStatus.ACTIVE, customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="ABC123",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status=SourcingStatus.OPEN,
            oem_pn="oem-part-x",
        )
        db_session.add(r)
        db_session.flush()
        assert r.oem_pn == "OEM-PART-X"

    def test_none_passes_through(self, db_session):
        req = Requisition(name="Test", status=RequisitionStatus.ACTIVE, customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="ABC123",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status=SourcingStatus.OPEN,
            customer_pn=None,
        )
        db_session.add(r)
        db_session.flush()
        assert r.customer_pn is None

    def test_strips_whitespace(self, db_session):
        req = Requisition(name="Test", status=RequisitionStatus.ACTIVE, customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="  abc123  ",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status=SourcingStatus.OPEN,
        )
        db_session.add(r)
        db_session.flush()
        assert r.primary_mpn == "ABC123"
