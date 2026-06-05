"""Track-A: part-centric Offers tab on the sightings detail.

Called by: pytest.
Depends on: conftest fixtures (client, db_session), Offer/Requirement/Requisition
models, app.services.part_offers, the sightings offer endpoints.
"""

from app.constants import ActivityType, OfferStatus
from app.models.intelligence import ActivityLog
from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition
from app.models.vendor_sighting_summary import VendorSightingSummary
from app.services.part_offers import part_offers_for


def _req(db, mpn="LM317T", subs=None, customer="Acme Corp"):
    rq = Requisition(name="RFQ", status="active", customer_name=customer)
    db.add(rq)
    db.flush()
    r = Requirement(
        requisition_id=rq.id,
        primary_mpn=mpn,
        manufacturer="TI",
        target_qty=100,
        sourcing_status="open",
        substitutes=subs or [],
    )
    db.add(r)
    db.flush()
    db.commit()
    return rq, r


def _offer(db, rq, r, vendor, mpn, normalized, status=OfferStatus.ACTIVE, price=1.0):
    o = Offer(
        requisition_id=rq.id,
        requirement_id=r.id,
        vendor_name=vendor,
        mpn=mpn,
        normalized_mpn=normalized,
        status=status,
        unit_price=price,
    )
    db.add(o)
    db.commit()
    return o


# ── Task 1: part-centric query helper ──────────────────────────────────────


def test_part_offers_includes_cross_req_and_substitute(db_session):
    rq1, r1 = _req(db_session, mpn="LM317T", subs=[{"mpn": "LM317-ALT", "manufacturer": "ON"}])
    # offer for the same part on a DIFFERENT requisition (dedup-key form, no dashes)
    rq2, r2 = _req(db_session, mpn="LM317T", customer="Beta Inc")
    _offer(db_session, rq2, r2, "Mouser", "LM317T", "lm317t")
    # offer entered against a SUBSTITUTE mpn (display form, with dash)
    _offer(db_session, rq1, r1, "Arrow", "LM317-ALT", "LM317-ALT")
    # unrelated offer must NOT appear
    rq3, r3 = _req(db_session, mpn="NE555")
    _offer(db_session, rq3, r3, "Digi", "NE555", "ne555")

    offers = part_offers_for(r1, db_session)
    assert {o.vendor_name for o in offers} == {"Mouser", "Arrow"}


# ── Task 2: root-cause fix — add_offer normalized_mpn ───────────────────────


def test_add_offer_writes_dedup_key_normalized_mpn(client, db_session):
    rq, r = _req(db_session, mpn="LM2596S-5.0")
    resp = client.post(
        f"/v2/partials/requisitions/{rq.id}/add-offer",
        data={"vendor_name": "Arrow", "mpn": "LM2596S-5.0", "requirement_id": r.id},
    )
    assert resp.status_code == 200
    o = db_session.query(Offer).filter(Offer.vendor_name == "Arrow").one()
    assert o.normalized_mpn == "lm2596s50"


# ── Task 3: Offers tab + panel + pending move ───────────────────────────────


def test_offers_tab_lists_part_offers_with_source_hint(client, db_session):
    rq1, r1 = _req(db_session, mpn="LM317T", customer="Acme Corp")
    rq2, r2 = _req(db_session, mpn="LM317T", customer="Beta Inc")
    _offer(db_session, rq2, r2, "Mouser", "LM317T", "lm317t", price=0.51)
    body = client.get(f"/v2/partials/sightings/{r1.id}/detail").text
    assert "activeTab = 'offers'" in body
    assert "Mouser" in body
    assert "Beta Inc" in body  # source hint


def test_pending_offer_in_offers_panel_not_vendors(client, db_session):
    rq, r = _req(db_session, mpn="LM317T")
    _offer(db_session, rq, r, "PendVend", "LM317T", "lm317t", status=OfferStatus.PENDING_REVIEW)
    body = client.get(f"/v2/partials/sightings/{r.id}/detail").text
    panel = body.split('id="sightings-offers-panel"', 1)[1]
    assert "PendVend" in panel
    assert "Approve" in panel and "Reject" in panel


# ── Task 5: Convert button + offer-form endpoint ────────────────────────────


def test_convert_button_on_vendor_row_collapsed(client, db_session):
    rq, r = _req(db_session, mpn="LM317T")
    db_session.add(
        VendorSightingSummary(
            requirement_id=r.id,
            vendor_name="Arrow",
            listing_count=1,
            score=70.0,
            best_price=0.45,
            estimated_qty=5000,
        )
    )
    db_session.commit()
    body = client.get(f"/v2/partials/sightings/{r.id}/detail").text
    assert "Convert to offer" in body
    assert body.index("Convert to offer") < body.index('x-show="expanded"')


def test_offer_form_prefill_from_vendor(client, db_session):
    rq, r = _req(db_session, mpn="LM317T")
    body = client.get(f"/v2/partials/sightings/{r.id}/offer-form?vendor_name=Arrow&unit_price=0.45&qty=5000").text
    assert 'value="Arrow"' in body
    assert 'value="0.45"' in body
    assert "Convert to Offer" in body


def test_offer_form_blank_enter(client, db_session):
    rq, r = _req(db_session, mpn="LM317T")
    body = client.get(f"/v2/partials/sightings/{r.id}/offer-form").text
    assert "Enter Offer" in body
    assert 'value="LM317T"' in body  # mpn prefilled to the part


# ── Task 6: create offer (reuses create_offer) ──────────────────────────────


def test_create_offer_appears_in_panel_and_logs_activity(client, db_session):
    rq, r = _req(db_session, mpn="LM317T")
    resp = client.post(
        f"/v2/partials/sightings/{r.id}/offers",
        data={
            "vendor_name": "Arrow",
            "mpn": "LM317T",
            "qty_available": "5000",
            "unit_price": "0.45",
            "condition": "new",
        },
    )
    assert resp.status_code == 200
    assert "Arrow" in resp.text
    o = db_session.query(Offer).filter(Offer.vendor_name.ilike("%arrow%")).one()
    assert o.requirement_id == r.id
    assert (
        db_session.query(ActivityLog)
        .filter(
            ActivityLog.activity_type == ActivityType.OFFER_CREATED,
            ActivityLog.requirement_id == r.id,
        )
        .count()
        == 1
    )


# ── Task 7: mutation endpoints ──────────────────────────────────────────────


def test_approve_pending_offer_via_panel(client, db_session):
    rq, r = _req(db_session, mpn="LM317T")
    o = _offer(db_session, rq, r, "PendVend", "LM317T", "lm317t", status=OfferStatus.PENDING_REVIEW)
    resp = client.post(
        f"/v2/partials/sightings/{r.id}/offers/{o.id}/review",
        data={"action": "approve"},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(Offer, o.id).status == OfferStatus.ACTIVE


def test_delete_offer_via_panel(client, db_session):
    rq, r = _req(db_session, mpn="LM317T")
    o = _offer(db_session, rq, r, "Arrow", "LM317T", "lm317t")
    resp = client.delete(f"/v2/partials/sightings/{r.id}/offers/{o.id}")
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(Offer, o.id) is None


def test_edit_offer_updates_field(client, db_session):
    rq, r = _req(db_session, mpn="LM317T")
    o = _offer(db_session, rq, r, "Arrow", "LM317T", "lm317t", price=1.0)
    resp = client.post(
        f"/v2/partials/sightings/{r.id}/offers/{o.id}",
        data={"vendor_name": "Arrow", "mpn": "LM317T", "unit_price": "2.50"},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    assert float(db_session.get(Offer, o.id).unit_price) == 2.50
