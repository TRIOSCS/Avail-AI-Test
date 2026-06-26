"""Tests for MPN auto-capitalization and substitutes display.

Called by: pytest
Depends on: conftest.py fixtures, app models, app constants
"""

import pytest

from app.constants import RequisitionStatus, SourcingStatus
from app.models.sourcing import Requirement, Requisition


def _make_requirement(db_session, **fields):
    """Persist a Requirement (with its parent Requisition) and return it flushed."""
    req = Requisition(name="Test", status=RequisitionStatus.OPEN, customer_name="Acme")
    db_session.add(req)
    db_session.flush()
    r = Requirement(
        requisition_id=req.id,
        manufacturer="TestMfr",
        target_qty=100,
        sourcing_status=SourcingStatus.OPEN,
        **fields,
    )
    db_session.add(r)
    db_session.flush()
    return r


class TestMPNUppercaseValidator:
    @pytest.mark.parametrize(
        "field,raw,expected",
        [
            ("primary_mpn", "ne5559", "NE5559"),
            ("customer_pn", "cust-part-01", "CUST-PART-01"),
            ("oem_pn", "oem-part-x", "OEM-PART-X"),
            ("customer_pn", None, None),
            ("primary_mpn", "  abc123  ", "ABC123"),
        ],
        ids=["primary_on_create", "customer_pn", "oem_pn", "none_passes_through", "strips_whitespace"],
    )
    def test_field_uppercased_on_create(self, db_session, field, raw, expected):
        defaults = {"primary_mpn": "ABC123"}
        defaults[field] = raw
        r = _make_requirement(db_session, **defaults)
        assert getattr(r, field) == expected

    def test_primary_mpn_uppercased_on_update(self, db_session):
        r = _make_requirement(db_session, primary_mpn="ABC123")
        r.primary_mpn = "xyz789"
        assert r.primary_mpn == "XYZ789"


class TestAPISubstituteFormat:
    def test_batch_create_stores_dict_subs(self, client, db_session):
        """POST /api/requisitions/{id}/requirements should store subs as dicts."""
        req = Requisition(name="API Test", status=RequisitionStatus.OPEN, customer_name="Acme")
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
        req = Requisition(name="API Test", status=RequisitionStatus.OPEN, customer_name="Acme")
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
    @pytest.mark.parametrize(
        "subs,expected",
        [
            (None, []),
            ([], []),
            (["ne5559", "esp32-wrover-e"], ["NE5559", "ESP32-WROVER-E"]),
            (
                [{"mpn": "17p9905", "manufacturer": "TI"}, {"mpn": "SL9bt", "manufacturer": ""}],
                ["17P9905", "SL9BT"],
            ),
            (["abc123", {"mpn": "def456", "manufacturer": "Analog"}], ["ABC123", "DEF456"]),
            ([{"mpn": "", "manufacturer": "TI"}, {"mpn": None, "manufacturer": ""}, ""], []),
            # normalize_mpn returns None for MPNs shorter than 3 chars.
            (["AB", {"mpn": "XY", "manufacturer": ""}], []),
        ],
        ids=[
            "empty_none",
            "empty_list",
            "string_subs",
            "dict_subs",
            "mixed_format",
            "skips_empty_mpn",
            "skips_short_mpn",
        ],
    )
    def test_sub_mpns_filter(self, subs, expected):
        assert _sub_mpns_filter(subs) == expected
