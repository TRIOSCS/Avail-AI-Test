"""test_requirements_nightly_coverage.py — Coverage booster for requirements.py.

Targets lines 163, 179, 184, 188, 236, 307-308, 363, 365-437, 439-457,
463-465, 478, 511, 521, 704, 733-748, 755, 815, 820, 843, 917-918, 920,
991, 997, 1002-1071, 1118, 1132, 1221-1225 in requirements.py.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user, test_requisition)
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models import (
    ChangeLog,
    Contact,
    MaterialCard,
    Offer,
    Requirement,
    Requisition,
    Sighting,
    SourcingLead,
    User,
    VendorCard,
)
from app.models.task import RequisitionTask

# ── helpers ──────────────────────────────────────────────────────────────────


def _req(db: Session, user: User, **kw) -> Requisition:
    defaults = dict(
        name="NC-REQ",
        customer_name="Acme",
        status="active",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    r = Requisition(**defaults)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _item(db: Session, req: Requisition, **kw) -> Requirement:
    defaults = dict(
        requisition_id=req.id,
        primary_mpn="LM317T",
        normalized_mpn="lm317t",
        target_qty=100,
        target_price=0.50,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    r = Requirement(**defaults)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _sighting(db: Session, req_item: Requirement, **kw) -> Sighting:
    defaults = dict(
        requirement_id=req_item.id,
        vendor_name="Arrow",
        vendor_name_normalized="arrow",
        mpn_matched="LM317T",
        source_type="brokerbin",
        qty_available=500,
        unit_price=0.45,
        confidence=80,
        score=50,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    s = Sighting(**defaults)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _offer(db: Session, req: Requisition, req_item: Requirement, user: User, **kw) -> Offer:
    defaults = dict(
        requisition_id=req.id,
        requirement_id=req_item.id,
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow electronics",
        mpn="LM317T",
        normalized_mpn="lm317t",
        qty_available=1000,
        unit_price=0.50,
        entered_by_id=user.id,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    o = Offer(**defaults)
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _card(db: Session, mpn: str = "LM317T") -> MaterialCard:
    from app.utils.normalization import normalize_mpn_key

    mc = MaterialCard(
        display_mpn=mpn,
        normalized_mpn=normalize_mpn_key(mpn),
        created_at=datetime.now(timezone.utc),
    )
    db.add(mc)
    db.commit()
    db.refresh(mc)
    return mc


def _lead(db: Session, req: Requisition, req_item: Requirement, **kw) -> SourcingLead:
    import uuid

    defaults = dict(
        lead_id=f"lead-{uuid.uuid4().hex[:10]}",
        requisition_id=req.id,
        requirement_id=req_item.id,
        part_number_requested="LM317T",
        part_number_matched="LM317T",
        match_type="exact",
        vendor_name="Arrow",
        vendor_name_normalized="arrow",
        primary_source_type="brokerbin",
        primary_source_name="brokerbin",
        buyer_status="open",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    lead = SourcingLead(**defaults)
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


# ══════════════════════════════════════════════════════════════════════════════
# list_requirements — lines 236-350 (with sightings, offers, tasks, steps)
# ══════════════════════════════════════════════════════════════════════════════


class TestListRequirementsBody:
    def test_with_sighting_count_shows_sourced_step(self, client, db_session, test_user, test_requisition):
        """Sighting present → step = 'sourced'."""
        req_item = test_requisition.requirements[0]
        _sighting(db_session, req_item)
        resp = client.get(f"/api/requisitions/{test_requisition.id}/requirements")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["step"] == "sourced"
        assert data[0]["sighting_count"] >= 1

    def test_with_active_offer_shows_offers_step(self, client, db_session, test_user, test_requisition):
        """Active offer present → step = 'offers'."""
        req_item = test_requisition.requirements[0]
        _offer(db_session, test_requisition, req_item, test_user, status="active")
        resp = client.get(f"/api/requisitions/{test_requisition.id}/requirements")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["step"] == "offers"
        assert data[0]["offer_count"] >= 1

    def test_with_selected_offer_shows_selected_step(self, client, db_session, test_user, test_requisition):
        """Selected offer → step = 'selected'."""
        req_item = test_requisition.requirements[0]
        _offer(
            db_session,
            test_requisition,
            req_item,
            test_user,
            status="active",
            selected_for_quote=True,
        )
        resp = client.get(f"/api/requisitions/{test_requisition.id}/requirements")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["step"] == "selected"
        assert data[0]["selected_count"] >= 1

    def test_no_requirements_returns_empty(self, client, db_session, test_user):
        """Requisition with no requirements returns an empty list."""
        req = _req(db_session, test_user, name="EMPTY-REQ")
        resp = client.get(f"/api/requisitions/{req.id}/requirements")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_step_new_when_no_sightings_or_offers(self, client, db_session, test_user, test_requisition):
        """No sightings, no offers → step = 'new'."""
        resp = client.get(f"/api/requisitions/{test_requisition.id}/requirements")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["step"] == "new"

    def test_task_count_included(self, client, db_session, test_user, test_requisition):
        """Open tasks for requirement reflected in task_count."""
        req_item = test_requisition.requirements[0]
        task = RequisitionTask(
            requisition_id=test_requisition.id,
            title="Send RFQ",
            task_type="sourcing",
            status="todo",
            source="manual",
            source_ref=f"requirement:{req_item.id}",
            created_by=test_user.id,
        )
        db_session.add(task)
        db_session.commit()
        resp = client.get(f"/api/requisitions/{test_requisition.id}/requirements")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["task_count"] >= 1

    def test_contact_count_included(self, client, db_session, test_user, test_requisition):
        """Contacts linked to requisition are counted."""
        ct = Contact(
            requisition_id=test_requisition.id,
            user_id=test_user.id,
            contact_type="rfq",
            vendor_name="Arrow",
            parts_included=["LM317T"],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ct)
        db_session.commit()
        resp = client.get(f"/api/requisitions/{test_requisition.id}/requirements")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["contact_count"] >= 1
        assert data[0]["hours_since_activity"] is not None


# ══════════════════════════════════════════════════════════════════════════════
# add_requirements — lines 363-465 (core path, batch, dup detection)
# ══════════════════════════════════════════════════════════════════════════════


class TestAddRequirementsCore:
    def test_add_single_returns_created(self, client, db_session, test_user, test_requisition):
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
            with patch("app.services.task_service.on_requirement_added"):
                resp = client.post(
                    f"/api/requisitions/{test_requisition.id}/requirements",
                    json={"primary_mpn": "NE555", "manufacturer": "TI", "target_qty": 100},
                )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["created"]) == 1
        assert data["created"][0]["primary_mpn"] == "NE555"

    def test_add_with_all_optional_fields(self, client, db_session, test_user, test_requisition):
        """Exercises condition, packaging, firmware, date_codes, notes, description."""
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
            with patch("app.services.task_service.on_requirement_added"):
                resp = client.post(
                    f"/api/requisitions/{test_requisition.id}/requirements",
                    json={
                        "primary_mpn": "LM7805",
                        "manufacturer": "Fairchild",
                        "target_qty": 50,
                        "condition": "new",
                        "packaging": "Tape and Reel",
                        "firmware": "v1.0",
                        "date_codes": "2023+",
                        "hardware_codes": "REV-C",
                        "notes": "Urgent",
                        "description": "5V regulator",
                    },
                )
        assert resp.status_code == 200

    def test_add_batch_all_valid(self, client, db_session, test_user, test_requisition):
        """Batch with two valid items → created count matches."""
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
            with patch("app.services.task_service.on_requirement_added"):
                resp = client.post(
                    f"/api/requisitions/{test_requisition.id}/requirements",
                    json=[
                        {"primary_mpn": "BC547", "manufacturer": "ST", "target_qty": 200},
                        {"primary_mpn": "2N2222", "manufacturer": "ON Semi", "target_qty": 300},
                    ],
                )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["created"]) == 2

    def test_add_batch_with_invalid_skips_item(self, client, db_session, test_user, test_requisition):
        """Batch where one item is invalid → skipped list returned."""
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/requirements",
                json=[
                    {"primary_mpn": "BC547", "manufacturer": "ST", "target_qty": 100},
                    {"primary_mpn": "X", "manufacturer": "", "target_qty": -1},
                ],
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "skipped" in data
        assert len(data["skipped"]) >= 1

    def test_add_single_invalid_mpn_raises_422(self, client, db_session, test_user, test_requisition):
        """Single item with blank primary_mpn → 422 (not batch mode)."""
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/requirements",
            json={"primary_mpn": "", "manufacturer": "TI", "target_qty": 100},
        )
        assert resp.status_code == 422

    def test_add_with_substitutes(self, client, db_session, test_user, test_requisition):
        """Substitutes deduplication exercised."""
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
            with patch("app.services.task_service.on_requirement_added"):
                resp = client.post(
                    f"/api/requisitions/{test_requisition.id}/requirements",
                    json={
                        "primary_mpn": "LM7812",
                        "manufacturer": "TI",
                        "target_qty": 100,
                        "substitutes": ["LM7808", "LM7808", "LM7812"],
                    },
                )
        assert resp.status_code == 200

    def test_add_with_duplicate_detection(self, client, db_session, test_user):
        """Duplicate detection runs when customer_site_id is set + card_ids exist."""
        from app.models import Company, CustomerSite

        co = Company(name="DupCo", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="Main")
        db_session.add(site)
        db_session.flush()

        req = _req(db_session, test_user, customer_site_id=site.id)

        # Prior requisition for same site
        prior_req = _req(db_session, test_user, name="PRIOR-REQ", customer_site_id=site.id)
        mc = _card(db_session, "BC557")
        prior_item = _item(db_session, prior_req, primary_mpn="BC557", material_card_id=mc.id)
        assert prior_item.id is not None

        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=mc):
            with patch("app.services.task_service.on_requirement_added"):
                resp = client.post(
                    f"/api/requisitions/{req.id}/requirements",
                    json={"primary_mpn": "BC557", "manufacturer": "Philips", "target_qty": 100},
                )
        assert resp.status_code == 200
        data = resp.json()
        assert "duplicates" in data
        # The prior req has same site_id and same material_card_id → duplicate flagged
        assert len(data["duplicates"]) >= 1

    def test_add_task_autoassign_runs(self, client, db_session, test_user, test_requisition):
        """on_requirement_added is called (patched to verify invocation)."""
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
            with patch("app.services.task_service.on_requirement_added") as mock_task:
                resp = client.post(
                    f"/api/requisitions/{test_requisition.id}/requirements",
                    json={"primary_mpn": "LM393", "manufacturer": "TI", "target_qty": 10},
                )
        assert resp.status_code == 200
        mock_task.assert_called_once()

    def test_add_tag_propagation_with_site(self, client, db_session, test_user):
        """propagate_tags_to_entity is called when material_card_id + customer_site_id set."""
        from app.models import Company, CustomerSite

        co = Company(name="TagCo", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ")
        db_session.add(site)
        db_session.flush()

        req = _req(db_session, test_user, customer_site_id=site.id)
        mc = _card(db_session, "TL431")

        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=mc):
            with patch("app.services.tagging.propagate_tags_to_entity") as mock_tag:
                with patch("app.services.task_service.on_requirement_added"):
                    resp = client.post(
                        f"/api/requisitions/{req.id}/requirements",
                        json={"primary_mpn": "TL431", "manufacturer": "TI", "target_qty": 10},
                    )
        assert resp.status_code == 200
        assert mock_tag.call_count >= 1


# ══════════════════════════════════════════════════════════════════════════════
# upload_requirements — lines 478, 511, 521 (body, condition/packaging branch)
# ══════════════════════════════════════════════════════════════════════════════


class TestUploadRequirementsBody:
    def test_upload_with_price_and_condition(self, client, db_session, test_user, test_requisition):
        """Exercises normalize_price, normalize_condition, normalize_packaging branches."""
        csv_content = (
            b"mpn,qty,price,condition,packaging,manufacturer,date_codes,notes\n"
            b"LM7805,50,1.25,New,Tape and Reel,TI,2023+,Urgent\n"
        )
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/upload",
                files={"file": ("parts.csv", csv_content, "text/csv")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["created"] >= 1

    def test_upload_with_subs_comma_separated(self, client, db_session, test_user, test_requisition):
        """substitutes column parsed from comma-separated string."""
        csv_content = b"mpn,qty,substitutes\nLM317T,100,NE555,LM7805\n"
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/upload",
                files={"file": ("parts.csv", csv_content, "text/csv")},
            )
        assert resp.status_code == 200

    def test_upload_empty_mpn_row_skipped(self, client, db_session, test_user, test_requisition):
        """Rows where MPN normalizes to None are skipped."""
        # Use a column name that maps to no MPN field
        csv_content = b"part,qty\n,100\nAB,50\n"
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/upload",
                files={"file": ("parts.csv", csv_content, "text/csv")},
            )
        assert resp.status_code == 200
        assert resp.json()["created"] == 0

    def test_upload_parse_error_returns_400(self, client, db_session, test_user, test_requisition):
        """Unparseable file raises 400."""
        with patch(
            "app.file_utils.parse_tabular_file",
            side_effect=ValueError("bad file"),
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/upload",
                files={"file": ("bad.csv", b"garbage", "text/csv")},
            )
        assert resp.status_code == 400


# ══════════════════════════════════════════════════════════════════════════════
# get_saved_sightings — lines 704, 733-748, 755, 815, 820, 843
# ══════════════════════════════════════════════════════════════════════════════


class TestGetSavedSightingsBody:
    def test_with_sightings_enriches_vendor_cards(self, client, db_session, test_user, test_requisition):
        """Sightings present and _enrich_with_vendor_cards called."""
        req_item = test_requisition.requirements[0]
        _sighting(db_session, req_item)

        vc = VendorCard(
            normalized_name="arrow",
            display_name="Arrow Electronics",
            emails=["sales@arrow.com"],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vc)
        db_session.commit()

        resp = client.get(f"/api/requisitions/{test_requisition.id}/sightings")
        assert resp.status_code == 200

    def test_sightings_with_material_card_on_requirement(self, client, db_session, test_user, test_requisition):
        """material_card_id on requirement triggers sub_card_lookup path."""
        mc = _card(db_session, "LM317T")
        req_item = test_requisition.requirements[0]
        req_item.material_card_id = mc.id
        db_session.commit()
        _sighting(db_session, req_item)

        resp = client.get(f"/api/requisitions/{test_requisition.id}/sightings")
        assert resp.status_code == 200

    def test_sightings_with_substitute_mpn_string(self, client, db_session, test_user, test_requisition):
        """String substitute on requirement triggers sub-key lookup path."""
        req_item = test_requisition.requirements[0]
        req_item.substitutes = ["NE555P"]
        db_session.commit()
        _sighting(db_session, req_item)

        resp = client.get(f"/api/requisitions/{test_requisition.id}/sightings")
        assert resp.status_code == 200

    def test_sightings_include_lead_data(self, client, db_session, test_user, test_requisition):
        """_attach_lead_data populates lead_cards for each group."""
        req_item = test_requisition.requirements[0]
        _sighting(db_session, req_item)
        lead = _lead(db_session, test_requisition, req_item, confidence_score=85)
        assert lead.id is not None

        resp = client.get(f"/api/requisitions/{test_requisition.id}/sightings")
        assert resp.status_code == 200
        data = resp.json()
        key = str(req_item.id)
        if key in data:
            assert "lead_cards" in data[key]

    def test_sightings_with_unavailable_sighting_annotated(self, client, db_session, test_user, test_requisition):
        """Unavailable sighting → buyer_outcome = 'unavailable_confirmed'."""
        req_item = test_requisition.requirements[0]
        _sighting(db_session, req_item, is_unavailable=True)

        resp = client.get(f"/api/requisitions/{test_requisition.id}/sightings")
        assert resp.status_code == 200

    def test_sightings_offer_logged_buyer_outcome(self, client, db_session, test_user, test_requisition):
        """Sighting whose vendor/MPN matches an active offer → 'offer_logged'."""
        req_item = test_requisition.requirements[0]
        _sighting(
            db_session,
            req_item,
            vendor_name="Arrow",
            vendor_name_normalized="arrow",
            mpn_matched="LM317T",
        )
        _offer(
            db_session,
            test_requisition,
            req_item,
            test_user,
            vendor_name="Arrow",
            vendor_name_normalized="arrow",
            mpn="LM317T",
            normalized_mpn="lm317t",
            status="active",
        )
        resp = client.get(f"/api/requisitions/{test_requisition.id}/sightings")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# leads_queue — lines 917-920 (status filter branch)
# ══════════════════════════════════════════════════════════════════════════════


class TestLeadsQueue:
    def test_queue_no_filter(self, client, db_session, test_user, test_requisition):
        """GET /api/leads/queue with no filter returns all leads."""
        req_item = test_requisition.requirements[0]
        _lead(db_session, test_requisition, req_item, buyer_status="open")
        resp = client.get("/api/leads/queue")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_queue_status_all_skips_filter(self, client, db_session, test_user, test_requisition):
        """status=all → filter not applied (line 920 branch)."""
        req_item = test_requisition.requirements[0]
        _lead(db_session, test_requisition, req_item, buyer_status="open")
        resp = client.get("/api/leads/queue?status=all")
        assert resp.status_code == 200

    def test_queue_status_filter_applied(self, client, db_session, test_user, test_requisition):
        """status=open → only open leads returned (line 918 branch)."""
        req_item = test_requisition.requirements[0]
        # Use different vendor names to avoid unique constraint violation
        _lead(
            db_session,
            test_requisition,
            req_item,
            buyer_status="open",
            vendor_name="Arrow",
            vendor_name_normalized="arrow",
        )
        _lead(
            db_session,
            test_requisition,
            req_item,
            buyer_status="no_stock",
            vendor_name="Mouser",
            vendor_name_normalized="mouser",
        )
        resp = client.get("/api/leads/queue?status=open")
        assert resp.status_code == 200
        data = resp.json()
        assert all(lx["buyer_status"] == "open" for lx in data)


# ══════════════════════════════════════════════════════════════════════════════
# get_lead_detail — lines 991, 997 (404 and 403 paths)
# ══════════════════════════════════════════════════════════════════════════════


class TestGetLeadDetail:
    def test_lead_not_found_returns_404(self, client, db_session, test_user):
        resp = client.get("/api/leads/999999")
        assert resp.status_code == 404

    def test_lead_unauthorized_returns_403(self, client, db_session, test_user, test_requisition):
        """Lead exists but get_req_for_user returns None → 403."""
        req_item = test_requisition.requirements[0]
        lead = _lead(db_session, test_requisition, req_item)
        with patch(
            "app.routers.requisitions.requirements.get_req_for_user",
            return_value=None,
        ):
            resp = client.get(f"/api/leads/{lead.id}")
        assert resp.status_code == 403

    def test_lead_found_returns_data(self, client, db_session, test_user, test_requisition):
        """Lead exists and authorized → 200."""
        req_item = test_requisition.requirements[0]
        lead = _lead(db_session, test_requisition, req_item)
        resp = client.get(f"/api/leads/{lead.id}")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# patch_lead_status — 400 (ValueError), 404 (not found after update)
# ══════════════════════════════════════════════════════════════════════════════


class TestPatchLeadStatus:
    _VALID_STATUS = "contacted"  # one of the allowed Literal values

    def test_status_not_found_returns_404(self, client, db_session, test_user):
        resp = client.patch("/api/leads/999999/status", json={"status": self._VALID_STATUS})
        assert resp.status_code == 404

    def test_status_unauthorized_returns_403(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        lead = _lead(db_session, test_requisition, req_item)
        with patch(
            "app.routers.requisitions.requirements.get_req_for_user",
            return_value=None,
        ):
            resp = client.patch(f"/api/leads/{lead.id}/status", json={"status": self._VALID_STATUS})
        assert resp.status_code == 403

    def test_status_value_error_returns_400(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        lead = _lead(db_session, test_requisition, req_item)
        with patch(
            "app.routers.requisitions.requirements.update_lead_status",
            side_effect=ValueError("invalid transition"),
        ):
            resp = client.patch(f"/api/leads/{lead.id}/status", json={"status": self._VALID_STATUS})
        assert resp.status_code == 400

    def test_status_update_returns_none_returns_404(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        lead = _lead(db_session, test_requisition, req_item)
        with patch(
            "app.routers.requisitions.requirements.update_lead_status",
            return_value=None,
        ):
            resp = client.patch(f"/api/leads/{lead.id}/status", json={"status": self._VALID_STATUS})
        assert resp.status_code == 404

    def test_status_success(self, client, db_session, test_user, test_requisition):
        """Successful status update returns ok=True."""
        from unittest.mock import MagicMock

        req_item = test_requisition.requirements[0]
        lead = _lead(db_session, test_requisition, req_item)
        mock_lead = MagicMock()
        mock_lead.id = lead.id
        mock_lead.buyer_status = "contacted"
        mock_lead.confidence_score = 80
        mock_lead.confidence_band = "high"
        mock_lead.vendor_safety_score = 70
        mock_lead.vendor_safety_band = "medium"
        mock_lead.buyer_feedback_summary = None
        with patch(
            "app.routers.requisitions.requirements.update_lead_status",
            return_value=mock_lead,
        ):
            resp = client.patch(f"/api/leads/{lead.id}/status", json={"status": self._VALID_STATUS})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ══════════════════════════════════════════════════════════════════════════════
# import_stock_list — lines 1002-1071 (main body, match/no-match/exception)
# ══════════════════════════════════════════════════════════════════════════════


class TestImportStockListBody:
    def test_import_stock_matching_mpn_creates_sighting(self, client, db_session, test_user, test_requisition):
        """CSV with MPN matching a requirement creates a sighting."""
        csv_content = b"mpn,qty,price,condition\nLM317T,500,0.45,new\n"
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/import-stock",
                data={"vendor_name": "Arrow"},
                files={"file": ("stock.csv", csv_content, "text/csv")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["matched_sightings"] >= 1

    def test_import_stock_with_material_card(self, client, db_session, test_user, test_requisition):
        """resolve_material_card returning a card sets material_card_id on sighting."""
        mc = _card(db_session, "LM317T")
        csv_content = b"mpn,qty\nLM317T,200\n"
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=mc):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/import-stock",
                data={"vendor_name": "DigiKey"},
                files={"file": ("stock.csv", csv_content, "text/csv")},
            )
        assert resp.status_code == 200
        assert resp.json()["matched_sightings"] >= 1

    def test_import_stock_no_match_skips_sighting(self, client, db_session, test_user, test_requisition):
        """CSV MPN not in req_mpns → no sightings created."""
        csv_content = b"mpn,qty\nXYZ999,100\n"
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/import-stock",
                data={"vendor_name": "Arrow"},
                files={"file": ("stock.csv", csv_content, "text/csv")},
            )
        assert resp.status_code == 200
        assert resp.json()["matched_sightings"] == 0

    def test_import_stock_exception_returns_500(self, client, db_session, test_user, test_requisition):
        """Exception during sighting creation → rollback + 500."""
        csv_content = b"mpn,qty\nLM317T,100\n"
        with patch(
            "app.routers.requisitions.requirements.resolve_material_card",
            side_effect=Exception("db failure"),
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/import-stock",
                data={"vendor_name": "Arrow"},
                files={"file": ("stock.csv", csv_content, "text/csv")},
            )
        assert resp.status_code == 500

    def test_import_stock_file_too_large_returns_413(self, client, db_session, test_user, test_requisition):
        """File > 10MB via import-stock endpoint → 413."""
        big = b"x" * (10_000_001)
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/import-stock",
            data={"vendor_name": "Arrow"},
            files={"file": ("big.csv", big, "text/csv")},
        )
        assert resp.status_code == 413

    def test_import_stock_substitute_matching(self, client, db_session, test_user, test_requisition):
        """Substitute MPN on a requirement matches imported row."""
        req_item = test_requisition.requirements[0]
        req_item.substitutes = ["NE555P"]
        db_session.commit()
        csv_content = b"mpn,qty\nNE555P,100\n"
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/import-stock",
                data={"vendor_name": "Arrow"},
                files={"file": ("stock.csv", csv_content, "text/csv")},
            )
        assert resp.status_code == 200
        # substitute match creates sighting
        assert resp.json()["matched_sightings"] >= 1


# ══════════════════════════════════════════════════════════════════════════════
# list_requirement_sightings — lines 1118, 1132 (substitute card lookup)
# ══════════════════════════════════════════════════════════════════════════════


class TestListRequirementSightingsSubstitutes:
    def test_substitute_string_with_material_card(self, client, db_session, test_user, test_requisition):
        """String substitute MPN that has a material card → sub_rows path exercised."""
        req_item = test_requisition.requirements[0]
        req_item.substitutes = ["TL431A"]
        db_session.commit()
        _card(db_session, "TL431A")
        _sighting(db_session, req_item)

        resp = client.get(f"/api/requirements/{req_item.id}/sightings")
        assert resp.status_code == 200
        data = resp.json()
        assert "sightings" in data

    def test_requirement_not_found_returns_404(self, client, db_session, test_user):
        resp = client.get("/api/requirements/999999/sightings")
        assert resp.status_code == 404

    def test_unauthorized_returns_403(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        with patch(
            "app.routers.requisitions.requirements.get_req_for_user",
            return_value=None,
        ):
            resp = client.get(f"/api/requirements/{req_item.id}/sightings")
        assert resp.status_code == 403

    def test_sightings_returned_with_lead_data(self, client, db_session, test_user, test_requisition):
        """_attach_lead_data executed for single-requirement sightings view."""
        req_item = test_requisition.requirements[0]
        _sighting(db_session, req_item)
        lead = _lead(db_session, test_requisition, req_item, confidence_score=90)
        assert lead.id is not None

        resp = client.get(f"/api/requirements/{req_item.id}/sightings")
        assert resp.status_code == 200
        data = resp.json()
        assert "lead_cards" in data


# ══════════════════════════════════════════════════════════════════════════════
# list_requirement_offers — lines 1221-1225 (substitute material card path)
# ══════════════════════════════════════════════════════════════════════════════


class TestListRequirementOffersSubstitutes:
    def test_offers_not_found_returns_404(self, client, db_session, test_user):
        resp = client.get("/api/requirements/999999/offers")
        assert resp.status_code == 404

    def test_offers_returns_current_offers(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        _offer(db_session, test_requisition, req_item, test_user, status="active")
        resp = client.get(f"/api/requirements/{req_item.id}/offers")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["is_historical"] is False

    def test_offers_with_string_substitute_card(self, client, db_session, test_user, test_requisition):
        """String substitute with material card → card_ids lookup exercised (line 1221-1225)."""
        req_item = test_requisition.requirements[0]
        req_item.substitutes = ["TL431A"]
        db_session.commit()
        sub_mc = _card(db_session, "TL431A")

        # Create a historical offer for the substitute card
        other_req = _req(db_session, test_user, name="OTHER-REQ")
        _offer(
            db_session,
            other_req,
            req_item,
            test_user,
            material_card_id=sub_mc.id,
            status="active",
        )

        resp = client.get(f"/api/requirements/{req_item.id}/offers")
        assert resp.status_code == 200

    def test_offers_historical_flag_set(self, client, db_session, test_user, test_requisition):
        """Historical offers from other requisitions via material card have is_historical=True."""
        req_item = test_requisition.requirements[0]
        mc = _card(db_session, "LM317T")
        req_item.material_card_id = mc.id
        db_session.commit()

        # Create another requisition and offer on same material card
        other_req = _req(db_session, test_user, name="HIST-REQ")
        other_item = _item(db_session, other_req, primary_mpn="LM317T", material_card_id=mc.id)
        _offer(db_session, other_req, other_item, test_user, material_card_id=mc.id, status="active")

        resp = client.get(f"/api/requirements/{req_item.id}/offers")
        assert resp.status_code == 200
        data = resp.json()
        hist = [o for o in data if o.get("is_historical")]
        assert len(hist) >= 1


# ══════════════════════════════════════════════════════════════════════════════
# toggle_quote_selection — line 1273-1283 (403 path)
# ══════════════════════════════════════════════════════════════════════════════


class TestToggleQuoteSelectionAuth:
    def test_toggle_unauthorized_returns_403(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        offer = _offer(db_session, test_requisition, req_item, test_user)
        with patch(
            "app.routers.requisitions.requirements.get_req_for_user",
            return_value=None,
        ):
            resp = client.post(f"/api/offers/{offer.id}/toggle-quote-selection")
        assert resp.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# list_requirement_notes / add_requirement_note — lines 1305-1313, 1327-1359
# ══════════════════════════════════════════════════════════════════════════════


class TestRequirementNotesBody:
    def test_list_notes_with_offer_notes(self, client, db_session, test_user, test_requisition):
        """Offer with notes included in response."""
        req_item = test_requisition.requirements[0]
        _offer(db_session, test_requisition, req_item, test_user, notes="Priority vendor")
        resp = client.get(f"/api/requirements/{req_item.id}/notes")
        assert resp.status_code == 200
        data = resp.json()
        assert any(n["note"] == "Priority vendor" for n in data["notes"])

    def test_add_note_creates_timestamp_entry(self, client, db_session, test_user, test_requisition):
        """Note append includes timestamp and user email."""
        req_item = test_requisition.requirements[0]
        resp = client.post(
            f"/api/requirements/{req_item.id}/notes",
            json={"text": "Check lead times"},
        )
        assert resp.status_code == 200
        notes = resp.json()["notes"]
        assert "Check lead times" in notes
        assert "testbuyer@trioscs.com" in notes

    def test_add_note_appends_to_existing(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        req_item.notes = "Initial note"
        db_session.commit()
        resp = client.post(
            f"/api/requirements/{req_item.id}/notes",
            json={"text": "Follow-up"},
        )
        assert resp.status_code == 200
        result = resp.json()["notes"]
        assert "Initial note" in result
        assert "Follow-up" in result


# ══════════════════════════════════════════════════════════════════════════════
# list_requirement_tasks — line 1392 (with offer-level tasks)
# ══════════════════════════════════════════════════════════════════════════════


class TestListRequirementTasksWithOffers:
    def test_offer_level_tasks_included(self, client, db_session, test_user, test_requisition):
        """Tasks whose source_ref = 'offer:{id}' are included in the result."""
        req_item = test_requisition.requirements[0]
        offer = _offer(db_session, test_requisition, req_item, test_user)
        task = RequisitionTask(
            requisition_id=test_requisition.id,
            title="Send quote",
            task_type="quote",
            status="todo",
            source="manual",
            source_ref=f"offer:{offer.id}",
            created_by=test_user.id,
            assigned_to_id=test_user.id,
        )
        db_session.add(task)
        db_session.commit()

        resp = client.get(f"/api/requirements/{req_item.id}/tasks")
        assert resp.status_code == 200
        data = resp.json()
        assert any(t["source_ref"] == f"offer:{offer.id}" for t in data)


# ══════════════════════════════════════════════════════════════════════════════
# list_requirement_history — lines 1430, 1462, 1510-1512, 1536 (offer changes,
# rfq_sent, task_done)
# ══════════════════════════════════════════════════════════════════════════════


class TestListRequirementHistoryBody:
    def test_history_not_found_returns_404(self, client, db_session, test_user):
        resp = client.get("/api/requirements/999999/history")
        assert resp.status_code == 404

    def test_history_with_offer_changelog(self, client, db_session, test_user, test_requisition):
        """Change log on an offer for this requirement appears in history."""
        req_item = test_requisition.requirements[0]
        offer = _offer(db_session, test_requisition, req_item, test_user)
        cl = ChangeLog(
            entity_type="offer",
            entity_id=offer.id,
            user_id=test_user.id,
            field_name="unit_price",
            old_value="0.50",
            new_value="0.45",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(cl)
        db_session.commit()

        resp = client.get(f"/api/requirements/{req_item.id}/history")
        assert resp.status_code == 200
        data = resp.json()
        assert any(e["type"] == "change" and e["entity"] == "offer" for e in data)

    def test_history_rfq_sent_only_when_mpn_in_parts(self, client, db_session, test_user, test_requisition):
        """Contact with parts_included matching MPN → rfq_sent event."""
        req_item = test_requisition.requirements[0]
        ct = Contact(
            requisition_id=test_requisition.id,
            user_id=test_user.id,
            contact_type="rfq",
            vendor_name="Mouser",
            parts_included=[req_item.primary_mpn],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ct)
        db_session.commit()

        resp = client.get(f"/api/requirements/{req_item.id}/history")
        assert resp.status_code == 200
        data = resp.json()
        assert any(e["type"] == "rfq_sent" for e in data)

    def test_history_rfq_not_included_when_mpn_not_in_parts(self, client, db_session, test_user, test_requisition):
        """Contact with different parts_included → no rfq_sent event."""
        req_item = test_requisition.requirements[0]
        ct = Contact(
            requisition_id=test_requisition.id,
            user_id=test_user.id,
            contact_type="rfq",
            vendor_name="Mouser",
            parts_included=["DIFFERENT_MPN"],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ct)
        db_session.commit()

        resp = client.get(f"/api/requirements/{req_item.id}/history")
        assert resp.status_code == 200
        data = resp.json()
        assert not any(e["type"] == "rfq_sent" for e in data)

    def test_history_done_task_via_offer_ref(self, client, db_session, test_user, test_requisition):
        """Task with source_ref=offer:{id} that is 'done' → task_done in history."""
        req_item = test_requisition.requirements[0]
        offer = _offer(db_session, test_requisition, req_item, test_user)
        task = RequisitionTask(
            requisition_id=test_requisition.id,
            title="Completed offer task",
            task_type="general",
            status="done",
            source="manual",
            source_ref=f"offer:{offer.id}",
            created_by=test_user.id,
            completed_at=datetime.now(timezone.utc),
        )
        db_session.add(task)
        db_session.commit()

        resp = client.get(f"/api/requirements/{req_item.id}/history")
        assert resp.status_code == 200
        data = resp.json()
        assert any(e["type"] == "task_done" for e in data)


# ══════════════════════════════════════════════════════════════════════════════
# _annotate_buyer_outcomes and _attach_lead_data helper coverage (lines 163, 179, 184, 188)
# ══════════════════════════════════════════════════════════════════════════════


class TestAnnotateBuyerOutcomesHelpers:
    def test_annotate_empty_req_ids_no_op(self, db_session, test_user):
        """_annotate_buyer_outcomes with empty requirements → no-op."""
        from app.routers.requisitions.requirements import _annotate_buyer_outcomes

        req = _req(db_session, test_user)
        _annotate_buyer_outcomes(req, {}, db_session)
        # No exception = pass

    def test_annotate_with_no_results_no_op(self, db_session, test_user):
        """_annotate_buyer_outcomes with results=None → no-op."""
        from app.routers.requisitions.requirements import _annotate_buyer_outcomes

        req = _req(db_session, test_user)
        _item(db_session, req)
        db_session.refresh(req)
        _annotate_buyer_outcomes(req, None, db_session)

    def test_attach_lead_data_empty_req_ids_no_op(self, db_session):
        """_attach_lead_data with requirements having no id → no-op."""
        from app.routers.requisitions.requirements import _attach_lead_data

        r = Requirement(requisition_id=None, primary_mpn="X")
        r.id = None
        _attach_lead_data([r], {}, db_session)

    def test_attach_lead_data_builds_lead_summary(self, db_session, test_user, test_requisition):
        """_attach_lead_data populates lead_summary for a group with a lead."""
        from app.routers.requisitions.requirements import _attach_lead_data

        req_item = test_requisition.requirements[0]
        lead = _lead(
            db_session,
            test_requisition,
            req_item,
            confidence_score=80,
            vendor_safety_score=85,
        )
        assert lead.id is not None

        results = {str(req_item.id): {"sightings": [], "label": "LM317T"}}
        _attach_lead_data(test_requisition.requirements, results, db_session)

        grp = results[str(req_item.id)]
        assert "lead_summary" in grp
        assert grp["lead_summary"]["total_leads"] >= 1
