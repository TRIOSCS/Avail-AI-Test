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


# ---------------------------------------------------------------------------
# IDOR scope guard — mutation routes (review / reconfirm / mark-sold / delete)
# ---------------------------------------------------------------------------


def _make_cross_req_offer(db_session, test_requisition, test_user, status="active"):
    """Return (own_req_id, other_req_id, offer_on_other_req).

    Creates a second Requirement under the same Requisition and attaches an Offer to
    *that* one, so POSTing under the first requirement's path hits the IDOR guard.
    """
    from app.models.sourcing import Requirement

    own_rid = test_requisition.requirements[0].id
    other = Requirement(requisition_id=test_requisition.id, primary_mpn="CROSS123", target_qty=1)
    db_session.add(other)
    db_session.commit()
    o = Offer(
        requisition_id=test_requisition.id,
        requirement_id=other.id,
        vendor_name="CrossVendor",
        mpn="CROSS123",
        status=status,
        qualification={},
        entered_by_id=test_user.id,
    )
    db_session.add(o)
    db_session.commit()
    return own_rid, other.id, o


import pytest


@pytest.mark.parametrize(
    "method,path_suffix,data,initial_status",
    [
        ("post", "/review", {"action": "approve"}, "pending_review"),
        ("post", "/reconfirm", {}, "active"),
        ("post", "/mark-sold", {}, "active"),
        ("delete", "", {}, "active"),
    ],
    ids=["review", "reconfirm", "mark-sold", "delete"],
)
def test_offer_mutation_idor_guard(
    client, db_session, test_requisition, test_user, method, path_suffix, data, initial_status
):
    """IDOR guard: mutating an offer that belongs to a DIFFERENT requirement via path
    → 404 and the offer is NOT mutated."""
    own_rid, _other_rid, o = _make_cross_req_offer(db_session, test_requisition, test_user, status=initial_status)
    url = f"/v2/partials/sightings/{own_rid}/offers/{o.id}{path_suffix}"
    kwargs = {"data": data} if data else {}
    resp = getattr(client, method)(url, **kwargs)
    assert resp.status_code == 404, f"Expected 404 for {method.upper()} {url}, got {resp.status_code}"

    db_session.expire(o)
    db_session.refresh(o)
    # Offer must NOT have been mutated: status unchanged and still exists.
    assert o.status == initial_status, "Offer status was mutated despite IDOR guard"
    assert db_session.get(type(o), o.id) is not None, "Offer was deleted despite IDOR guard"


@pytest.mark.parametrize(
    "method,path_suffix,data,initial_status",
    [
        ("post", "/review", {"action": "approve"}, "pending_review"),
        ("post", "/reconfirm", {}, "active"),
        ("post", "/mark-sold", {}, "active"),
        ("delete", "", {}, "active"),
    ],
    ids=["review", "reconfirm", "mark-sold", "delete"],
)
def test_offer_mutation_happy_path(
    client, db_session, test_requisition, test_user, method, path_suffix, data, initial_status
):
    """Happy path: mutating an offer that correctly belongs to the path requirement → 200."""
    rid = test_requisition.requirements[0].id
    o = Offer(
        requisition_id=test_requisition.id,
        requirement_id=rid,
        vendor_name="HappyVendor",
        mpn="LM317T",
        status=initial_status,
        qualification={},
        entered_by_id=test_user.id,
    )
    db_session.add(o)
    db_session.commit()
    url = f"/v2/partials/sightings/{rid}/offers/{o.id}{path_suffix}"
    kwargs = {"data": data} if data else {}
    resp = getattr(client, method)(url, **kwargs)
    assert resp.status_code == 200, f"Expected 200 for {method.upper()} {url}, got {resp.status_code}"


# ---------------------------------------------------------------------------
# Qualification-status filter facet on Offers tab
# ---------------------------------------------------------------------------


def test_offers_tab_qual_filter_server_side(client, db_session, test_requisition, test_user):
    """GET /v2/partials/requisitions/{req_id}/tab/offers?qual=incomplete returns only
    the incomplete offer; without qual returns both.

    Filter is index-backed WHERE clause, not Python post-filter.
    """
    rid = test_requisition.requirements[0].id
    o_incomplete = Offer(
        requisition_id=test_requisition.id,
        requirement_id=rid,
        vendor_name="FilterVendorIncomplete",
        mpn="FILTMPN1",
        qualification_status="incomplete",
        entered_by_id=test_user.id,
    )
    o_complete = Offer(
        requisition_id=test_requisition.id,
        requirement_id=rid,
        vendor_name="FilterVendorComplete",
        mpn="FILTMPN2",
        qualification_status="complete",
        entered_by_id=test_user.id,
    )
    db_session.add_all([o_incomplete, o_complete])
    db_session.commit()

    req_id = test_requisition.id

    # Filtered: only incomplete
    resp = client.get(f"/v2/partials/requisitions/{req_id}/tab/offers?qual=incomplete")
    assert resp.status_code == 200
    body = resp.content
    assert b"FilterVendorIncomplete" in body
    assert b"FilterVendorComplete" not in body

    # Unfiltered: both visible
    resp_all = client.get(f"/v2/partials/requisitions/{req_id}/tab/offers")
    assert resp_all.status_code == 200
    body_all = resp_all.content
    assert b"FilterVendorIncomplete" in body_all
    assert b"FilterVendorComplete" in body_all

    # FIX 1: "All" pill must carry the active class when no qual param is sent.
    assert b"bg-brand-500" in body_all, "All pill should be active (bg-brand-500) when no qual filter"

    # FIX 2: filter-aware empty state — ?qual=essentials on a req with only incomplete/complete offers.
    resp_empty = client.get(f"/v2/partials/requisitions/{req_id}/tab/offers?qual=essentials")
    assert resp_empty.status_code == 200
    body_empty = resp_empty.content
    assert b"FilterVendorIncomplete" not in body_empty
    assert b"FilterVendorComplete" not in body_empty
    assert b"No offers match this filter" in body_empty, (
        "Filter-aware empty copy should appear when qual is set and no results"
    )
