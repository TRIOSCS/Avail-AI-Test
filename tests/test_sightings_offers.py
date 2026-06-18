"""Track-A: part-centric Offers tab on the sightings detail.

Called by: pytest.
Depends on: conftest fixtures (client, db_session), Offer/Requirement/Requisition
models, app.services.part_offers, the sightings offer endpoints.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.constants import ActivityType, OfferStatus
from app.models.intelligence import ActivityLog, MaterialCard
from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition
from app.models.vendor_part_unavailability import VendorPartUnavailability
from app.models.vendor_sighting_summary import VendorSightingSummary
from app.services.part_offers import part_offers_for
from app.utils.normalization import normalize_mpn_key


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
    panel = body.split('id="sightings-offers-panel"', 1)[1]
    assert "Mouser" in panel
    assert "Beta Inc" in panel  # source hint, anchored to the offers panel


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
            "manufacturer": "Texas Instruments",  # required for condition=new
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


@pytest.mark.parametrize(
    ("action", "expected_status"),
    [
        ("approve", OfferStatus.ACTIVE),
        ("reject", OfferStatus.REJECTED),
    ],
)
def test_review_pending_offer_via_panel(client, db_session, action, expected_status):
    rq, r = _req(db_session, mpn="LM317T")
    o = _offer(db_session, rq, r, "PendVend", "LM317T", "lm317t", status=OfferStatus.PENDING_REVIEW)
    resp = client.post(
        f"/v2/partials/sightings/{r.id}/offers/{o.id}/review",
        data={"action": action},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(Offer, o.id).status == expected_status


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


# ── Review follow-ups: hardening + coverage gaps ────────────────────────────


def test_part_offers_legacy_string_substitutes_no_crash(db_session):
    """Legacy plain-string substitutes must not crash the part-offers query."""
    rq, r = _req(db_session, mpn="PRIME-1", subs=["LEGACY-SUB-1"])  # plain strings
    _offer(db_session, rq, r, "Arrow", "LEGACY-SUB-1", "LEGACY-SUB-1")
    offers = part_offers_for(r, db_session)
    assert {o.vendor_name for o in offers} == {"Arrow"}


def test_part_offers_matches_via_material_card_id(db_session):
    """An offer linked by material_card_id is found even if normalized_mpn differs."""
    card = MaterialCard(normalized_mpn=normalize_mpn_key("LM317T"), display_mpn="LM317T")
    db_session.add(card)
    db_session.flush()
    rq, r = _req(db_session, mpn="LM317T")
    o = Offer(
        requisition_id=rq.id,
        requirement_id=r.id,
        vendor_name="CardVend",
        mpn="LM317T",
        normalized_mpn="zzz999",  # deliberately non-matching
        material_card_id=card.id,
        status=OfferStatus.ACTIVE,
    )
    db_session.add(o)
    db_session.commit()
    assert "CardVend" in {x.vendor_name for x in part_offers_for(r, db_session)}


def test_part_offers_empty_when_no_match(db_session):
    """A part with no offers returns an empty list (no query error)."""
    _, r = _req(db_session, mpn="NOOFFERS-XYZ")
    assert part_offers_for(r, db_session) == []


def test_reconfirm_offer_via_panel(client, db_session):
    rq, r = _req(db_session, mpn="LM317T")
    o = _offer(db_session, rq, r, "Arrow", "LM317T", "lm317t", status=OfferStatus.ACTIVE)
    resp = client.post(f"/v2/partials/sightings/{r.id}/offers/{o.id}/reconfirm")
    assert resp.status_code == 200
    db_session.expire_all()
    refreshed = db_session.get(Offer, o.id)
    assert refreshed.reconfirm_count == 1
    assert refreshed.reconfirmed_at is not None


def test_mark_sold_via_panel_for_creator(client, db_session):
    """Mark-sold works for an offer the current buyer entered (via the create path)."""
    rq, r = _req(db_session, mpn="LM317T")
    client.post(
        f"/v2/partials/sightings/{r.id}/offers",
        data={
            "vendor_name": "Arrow",
            "mpn": "LM317T",
            "unit_price": "0.45",
            "manufacturer": "Texas Instruments",
        },  # required for condition=new
    )
    o = db_session.query(Offer).filter(Offer.vendor_name.ilike("%arrow%")).one()
    resp = client.post(f"/v2/partials/sightings/{r.id}/offers/{o.id}/mark-sold")
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(Offer, o.id).status == OfferStatus.SOLD


def test_edit_form_prefills_and_404(client, db_session):
    rq, r = _req(db_session, mpn="LM317T")
    o = _offer(db_session, rq, r, "Arrow", "LM317T", "lm317t", price=1.25)
    body = client.get(f"/v2/partials/sightings/{r.id}/offers/{o.id}/edit-form").text
    assert "Edit Offer" in body
    assert 'value="Arrow"' in body
    # bogus offer id → 404
    assert client.get(f"/v2/partials/sightings/{r.id}/offers/999999/edit-form").status_code == 404


def test_offer_form_prefills_lead_days_and_moq(client, db_session):
    rq, r = _req(db_session, mpn="LM317T")
    body = client.get(
        f"/v2/partials/sightings/{r.id}/offer-form?vendor_name=Arrow&lead_days=14&moq=100&manufacturer=TI"
    ).text
    assert 'value="14 days"' in body
    assert 'value="100"' in body
    assert 'value="TI"' in body


def test_create_offer_persists_valid_until(client, db_session):
    rq, r = _req(db_session, mpn="LM317T")
    resp = client.post(
        f"/v2/partials/sightings/{r.id}/offers",
        data={
            "vendor_name": "Arrow",
            "mpn": "LM317T",
            "valid_until": "2026-12-31",
            "manufacturer": "Texas Instruments",
        },  # required for condition=new
    )
    assert resp.status_code == 200
    o = db_session.query(Offer).filter(Offer.vendor_name.ilike("%arrow%")).one()
    assert o.valid_until == date(2026, 12, 31)


def test_create_offer_bad_numeric_is_4xx_not_500(client, db_session):
    """A constraint-violating value (spq=0, ge=1) is reported as 4xx, never a 500."""
    rq, r = _req(db_session, mpn="LM317T")
    resp = client.post(
        f"/v2/partials/sightings/{r.id}/offers",
        data={"vendor_name": "Arrow", "mpn": "LM317T", "spq": "0"},
    )
    assert 400 <= resp.status_code < 500
    assert db_session.query(Offer).filter(Offer.vendor_name.ilike("%arrow%")).count() == 0


def test_mutation_response_is_panel_scoped(client, db_session):
    """Mutations return only the offers panel (not the full tab shell), so the user
    stays on the Offers tab."""
    rq, r = _req(db_session, mpn="LM317T")
    o = _offer(db_session, rq, r, "Arrow", "LM317T", "lm317t")
    resp = client.delete(f"/v2/partials/sightings/{r.id}/offers/{o.id}")
    assert resp.status_code == 200
    assert "All offers for" in resp.text  # panel heading
    assert "activeTab: 'vendors'" not in resp.text  # not the full detail shell


# ── Offer hook: the sightings offer-creation route releases via the canonical
#    create_offer (maybe_release_on_offer) — no route-level call ──────────────


def _unav(db, vendor_norm, key, reason="sold_elsewhere", age_days=0):
    rec = VendorPartUnavailability(
        vendor_name_normalized=vendor_norm,
        normalized_mpn=key,
        reason=reason,
        created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
    )
    db.add(rec)
    db.commit()
    return rec


def test_create_offer_releases_active_unavailability_record(client, db_session):
    """An incoming offer is proof of availability: the sightings offer-creation route
    releases the vendor's matching ACTIVE records ('offer_received')."""
    rq, r = _req(db_session, mpn="LM317T")
    rec = _unav(db_session, "arrow", "lm317t")
    resp = client.post(
        f"/v2/partials/sightings/{r.id}/offers",
        data={
            "vendor_name": "Arrow",
            "mpn": "LM317T",
            "qty_available": "5000",
            "unit_price": "0.45",
            "condition": "new",
            "manufacturer": "Texas Instruments",  # required for condition=new
        },
    )
    assert resp.status_code == 200
    db_session.expire_all()
    rec = db_session.get(VendorPartUnavailability, rec.id)
    assert rec.released_at is not None
    assert rec.release_trigger == "offer_received"


def test_create_offer_never_releases_different_part(client, db_session):
    """Availability evidence never releases identity knowledge — different_part stays
    active through the offer hook."""
    rq, r = _req(db_session, mpn="LM317T")
    rec = _unav(db_session, "arrow", "lm317t", reason="different_part")
    resp = client.post(
        f"/v2/partials/sightings/{r.id}/offers",
        data={
            "vendor_name": "Arrow",
            "mpn": "LM317T",
            "unit_price": "0.45",
            "condition": "new",
        },
    )
    assert resp.status_code == 200
    db_session.expire_all()
    rec = db_session.get(VendorPartUnavailability, rec.id)
    assert rec.released_at is None
    assert rec.release_trigger is None
