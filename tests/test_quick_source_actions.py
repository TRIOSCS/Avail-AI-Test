"""Tests for the quick-source action endpoints (scratch-req Send RFQ / Add Offer).

What: POST /v2/partials/search/quick-source/{rfq,offer} create (idempotently) a scratch
      requisition, persist the posted market rows as Sightings, and HX-Redirect to the
      scratch req's full workspace page.
Calls: app.routers.part_dossier quick_source_rfq / quick_source_offer.
Depends on: conftest fixtures (client, db_session, test_user), models.sourcing.
"""

import json

from app.models.sourcing import Requirement, Requisition, Sighting


def _items():
    return json.dumps(
        [
            {
                "vendor_name": "Broker A",
                "mpn": "LM317T",
                "unit_price": 0.8,
                "qty_available": 1000,
                "source_type": "brokerbin",
            },
            {
                "vendor_name": "Broker B",
                "mpn": "LM317T",
                "unit_price": 0.9,
                "qty_available": 500,
                "source_type": "nexar",
            },
        ]
    )


def _scratch(db):
    return db.query(Requisition).filter(Requisition.is_scratch.is_(True)).one()


def _sightings_for(db, req):
    return (
        db.query(Sighting)
        .join(Requirement, Sighting.requirement_id == Requirement.id)
        .filter(Requirement.requisition_id == req.id)
        .all()
    )


def test_rfq_creates_scratch_req_persists_sightings_and_redirects(client, db_session, test_user):
    r = client.post("/v2/partials/search/quick-source/rfq", data={"mpn": "LM317T", "items": _items()})

    assert r.status_code == 200
    req = _scratch(db_session)
    assert r.headers.get("HX-Redirect") == f"/v2/requisitions/{req.id}"
    sightings = _sightings_for(db_session, req)
    assert {s.vendor_name for s in sightings} == {"Broker A", "Broker B"}
    # mpn (shortlist key) falls back to mpn_matched so scratch sightings carry the MPN.
    assert all(s.mpn_matched == "LM317T" for s in sightings)


def test_offer_creates_scratch_req_and_redirects(client, db_session, test_user):
    r = client.post("/v2/partials/search/quick-source/offer", data={"mpn": "LM317T", "items": _items()})

    assert r.status_code == 200
    req = _scratch(db_session)
    assert r.headers.get("HX-Redirect") == f"/v2/requisitions/{req.id}"
    assert len(_sightings_for(db_session, req)) == 2


def test_per_row_single_vendor_payload(client, db_session, test_user):
    r = client.post("/v2/partials/search/quick-source/rfq", data={"mpn": "LM317T", "vendor_name": "Solo Vendor"})

    assert r.status_code == 200
    sightings = _sightings_for(db_session, _scratch(db_session))
    assert len(sightings) == 1
    assert sightings[0].vendor_name == "Solo Vendor"
    assert sightings[0].mpn_matched == "LM317T"


def test_rfq_then_offer_reuse_one_scratch_req(client, db_session, test_user):
    client.post("/v2/partials/search/quick-source/rfq", data={"mpn": "LM317T", "items": _items()})
    client.post("/v2/partials/search/quick-source/offer", data={"mpn": "lm317t", "vendor_name": "X"})

    assert db_session.query(Requisition).filter(Requisition.is_scratch.is_(True)).count() == 1


def test_empty_mpn_is_rejected(client, db_session, test_user):
    r = client.post("/v2/partials/search/quick-source/rfq", data={"mpn": "  "})

    assert r.status_code == 400
    assert db_session.query(Requisition).filter(Requisition.is_scratch.is_(True)).count() == 0


def test_no_market_rows_still_creates_req(client, db_session, test_user):
    r = client.post("/v2/partials/search/quick-source/rfq", data={"mpn": "LM317T"})

    assert r.status_code == 200
    req = _scratch(db_session)
    assert r.headers.get("HX-Redirect") == f"/v2/requisitions/{req.id}"
    assert _sightings_for(db_session, req) == []
