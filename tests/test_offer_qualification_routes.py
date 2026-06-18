"""tests/test_offer_qualification_routes.py
What: integration tests for qualification capture across the sightings offer flow.
Called by: pytest. Depends on: conftest client/db_session/test_requisition/test_user.
"""

from app.models.offers import Offer


def _req(test_requisition):
    return test_requisition.id, test_requisition.requirements[0].id


def test_sightings_create_pulls_composes_note(client, db_session, test_requisition):
    _, requirement_id = _req(test_requisition)
    resp = client.post(
        f"/v2/partials/sightings/{requirement_id}/offers",
        data={
            "vendor_name": "Acme",
            "mpn": "LM317T",
            "condition": "pulls",
            "packaging": "Trays",
            "usage": "systems",
            "part_condition": "Clean",
        },
    )
    assert resp.status_code == 200
    o = db_session.query(Offer).filter_by(vendor_name="Acme").one()
    assert o.condition == "pulls"
    assert o.qualification["usage"] == "systems"
    assert o.qualification_note == "Pulls — packaged in Trays, pulled from systems. Condition: Clean."
    assert o.qualification_status in ("essentials", "complete")


def test_sightings_create_pulls_missing_usage_is_blocked(client, db_session, test_requisition):
    _, requirement_id = _req(test_requisition)
    resp = client.post(
        f"/v2/partials/sightings/{requirement_id}/offers",
        data={"vendor_name": "NoUsage", "mpn": "LM317T", "condition": "pulls", "packaging": "Trays"},
    )
    # No offer persisted; the buyer sees an inline error (re-rendered form, 200).
    assert db_session.query(Offer).filter_by(vendor_name="NoUsage").first() is None
    assert b"Usage" in resp.content


def test_sightings_create_bulk_packaging_rejected(client, db_session, test_requisition):
    _, requirement_id = _req(test_requisition)
    resp = client.post(
        f"/v2/partials/sightings/{requirement_id}/offers",
        data={"vendor_name": "Bulky", "mpn": "LM317T", "condition": "new_no_pkg", "packaging": "bulk"},
    )
    assert db_session.query(Offer).filter_by(vendor_name="Bulky").first() is None
    assert b"bulk" in resp.content.lower()


def test_legacy_used_normalizes_to_pulls(client, db_session, test_requisition):
    _, requirement_id = _req(test_requisition)
    client.post(
        f"/v2/partials/sightings/{requirement_id}/offers",
        data={"vendor_name": "Legacy", "mpn": "LM317T", "condition": "used", "packaging": "Trays", "usage": "boards"},
    )
    o = db_session.query(Offer).filter_by(vendor_name="Legacy").one()
    assert o.condition == "pulls"


def test_request_from_vendor_logs_pending_and_returns_draft(client, db_session, test_requisition, test_user):
    from app.models.offers import Offer

    rid = test_requisition.requirements[0].id
    o = Offer(
        requisition_id=test_requisition.id,
        requirement_id=rid,
        vendor_name="V",
        mpn="LM317T",
        qualification={"requests": []},
        entered_by_id=test_user.id,
    )
    db_session.add(o)
    db_session.commit()
    resp = client.post(f"/v2/partials/sightings/{rid}/offers/{o.id}/request", data={"kind": "images"})
    assert resp.status_code == 200
    db_session.refresh(o)
    reqs = (o.qualification or {}).get("requests", [])
    assert reqs and reqs[-1]["kind"] == "images" and reqs[-1]["status"] == "pending"
    assert b"images" in resp.content.lower()


def test_request_rejects_invalid_kind(client, db_session, test_requisition, test_user):
    from app.models.offers import Offer

    rid = test_requisition.requirements[0].id
    o = Offer(
        requisition_id=test_requisition.id,
        requirement_id=rid,
        vendor_name="V",
        mpn="LM317T",
        qualification={"requests": []},
        entered_by_id=test_user.id,
    )
    db_session.add(o)
    db_session.commit()
    resp = client.post(f"/v2/partials/sightings/{rid}/offers/{o.id}/request", data={"kind": "bogus"})
    assert resp.status_code == 400


def test_request_scoped_to_requirement_blocks_cross_requirement_offer(client, db_session, test_requisition, test_user):
    """IDOR guard: an offer belonging to another requirement is 404, not mutated."""
    from app.models.offers import Offer
    from app.models.sourcing import Requirement

    rid = test_requisition.requirements[0].id
    other = Requirement(requisition_id=test_requisition.id, primary_mpn="OTHER123", target_qty=1)
    db_session.add(other)
    db_session.commit()
    # Offer belongs to `other`, but we POST under the original requirement's path.
    o = Offer(
        requisition_id=test_requisition.id,
        requirement_id=other.id,
        vendor_name="V",
        mpn="OTHER123",
        qualification={"requests": []},
        entered_by_id=test_user.id,
    )
    db_session.add(o)
    db_session.commit()
    resp = client.post(f"/v2/partials/sightings/{rid}/offers/{o.id}/request", data={"kind": "images"})
    assert resp.status_code == 404
    db_session.refresh(o)
    assert (o.qualification or {}).get("requests", []) == []  # not mutated


def test_sightings_edit_merges_qualification_preserving_requests(client, db_session, test_requisition, test_user):
    """Regression: editing an unrelated field must MERGE (not overwrite) the qualification
    JSON — the logged #7 requests array and the stored `usage` survive, and the merged
    essentials satisfy the gate so the edit is not falsely blocked (200, not re-render)."""
    rid = test_requisition.requirements[0].id
    logged_request = {
        "kind": "images",
        "status": "pending",
        "requested_at": "2026-06-17T00:00:00+00:00",
        "contact_id": None,
    }
    o = Offer(
        requisition_id=test_requisition.id,
        requirement_id=rid,
        vendor_name="MergeMe",
        mpn="LM317T",
        condition="pulls",
        packaging="Trays",
        qualification={"usage": "systems", "requests": [logged_request]},
        entered_by_id=test_user.id,
    )
    db_session.add(o)
    db_session.commit()
    offer_id = o.id

    # Edit only an unrelated field (unit_price) + re-supply condition, WITHOUT resubmitting usage.
    resp = client.post(
        f"/v2/partials/sightings/{rid}/offers/{offer_id}",
        data={
            "vendor_name": "MergeMe",
            "mpn": "LM317T",
            "condition": "pulls",
            "packaging": "Trays",
            "unit_price": "12.50",
        },
    )
    # Not blocked: merged usage="systems" satisfies the pulls gate → success refresh, not re-render.
    assert resp.status_code == 200
    db_session.refresh(o)
    assert float(o.unit_price) == 12.50
    # MERGE preserved stored usage and the logged request.
    assert o.qualification["usage"] == "systems"
    reqs = o.qualification.get("requests", [])
    assert reqs and reqs[0]["kind"] == "images" and reqs[0]["status"] == "pending"


def test_sightings_edit_form_repopulates_stored_qualification_chip(client, db_session, test_requisition, test_user):
    """The edit-modal GET must repopulate qualification chips from stored JSON so a re-
    save does not appear to clear them."""
    rid = test_requisition.requirements[0].id
    o = Offer(
        requisition_id=test_requisition.id,
        requirement_id=rid,
        vendor_name="PrefillMe",
        mpn="LM317T",
        condition="pulls",
        packaging="Trays",
        qualification={"usage": "systems", "requests": []},
        entered_by_id=test_user.id,
    )
    db_session.add(o)
    db_session.commit()
    resp = client.get(f"/v2/partials/sightings/{rid}/offers/{o.id}/edit-form")
    assert resp.status_code == 200
    assert b"systems" in resp.content  # stored usage rendered into the prefill x-data


def test_modal_open_prefills_country_from_last_vendor_offer(client, db_session, test_requisition, test_user):
    from app.vendor_utils import normalize_vendor_name

    db_session.add(
        Offer(
            requisition_id=test_requisition.id,
            vendor_name="MemVendor",
            vendor_name_normalized=normalize_vendor_name("MemVendor"),
            mpn="LM317T",
            country_of_origin="JP",
            entered_by_id=test_user.id,
        )
    )
    db_session.commit()
    rid = test_requisition.requirements[0].id
    resp = client.get(f"/v2/partials/sightings/{rid}/offer-form", params={"vendor_name": "MemVendor"})
    assert resp.status_code == 200
    assert b"JP" in resp.content  # country prefilled into the form
