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


class TestAPISubstituteFormat:
    def test_batch_create_stores_dict_subs(self, client, db_session):
        """POST /api/requisitions/{id}/requirements should store subs as dicts."""
        req = Requisition(name="API Test", status=RequisitionStatus.ACTIVE, customer_name="Acme")
        db_session.add(req)
        db_session.commit()
        resp = client.post(
            f"/api/requisitions/{req.id}/requirements",
            json={
                "primary_mpn": "TEST-001",
                "manufacturer": "TestMfr",
                "target_qty": 100,
                "substitutes": ["alt-001", "alt-002"],
            },
        )
        assert resp.status_code == 200
        r = db_session.query(Requirement).filter_by(requisition_id=req.id).first()
        assert r is not None
        assert r.substitutes is not None
        assert len(r.substitutes) > 0
        for sub in r.substitutes:
            assert isinstance(sub, dict), f"Expected dict, got {type(sub)}: {sub}"
            assert "mpn" in sub

    def test_patch_stores_dict_subs(self, client, db_session):
        """PATCH should store subs as dicts."""
        req = Requisition(name="API Test", status=RequisitionStatus.ACTIVE, customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="PATCH-001",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status=SourcingStatus.OPEN,
        )
        db_session.add(r)
        db_session.commit()
        resp = client.put(
            f"/api/requirements/{r.id}",
            json={"substitutes": ["sub-a", "sub-b"]},
        )
        assert resp.status_code == 200
        db_session.refresh(r)
        assert r.substitutes is not None
        for sub in r.substitutes:
            assert isinstance(sub, dict), f"Expected dict, got {type(sub)}: {sub}"
            assert "mpn" in sub


from app.template_env import _sub_mpns_filter


class TestSubMpnsFilter:
    def test_empty_input(self):
        assert _sub_mpns_filter(None) == []
        assert _sub_mpns_filter([]) == []

    def test_string_subs(self):
        result = _sub_mpns_filter(["ne5559", "esp32-wrover-e"])
        assert result == ["NE5559", "ESP32-WROVER-E"]

    def test_dict_subs(self):
        result = _sub_mpns_filter(
            [
                {"mpn": "17p9905", "manufacturer": "TI"},
                {"mpn": "SL9bt", "manufacturer": ""},
            ]
        )
        assert result == ["17P9905", "SL9BT"]

    def test_mixed_format(self):
        result = _sub_mpns_filter(
            [
                "abc123",
                {"mpn": "def456", "manufacturer": "Analog"},
            ]
        )
        assert result == ["ABC123", "DEF456"]

    def test_skips_empty_mpn(self):
        result = _sub_mpns_filter(
            [
                {"mpn": "", "manufacturer": "TI"},
                {"mpn": None, "manufacturer": ""},
                "",
            ]
        )
        assert result == []

    def test_skips_short_mpn(self):
        """normalize_mpn returns None for MPNs shorter than 3 chars."""
        result = _sub_mpns_filter(["AB", {"mpn": "XY", "manufacturer": ""}])
        assert result == []
