"""Regression: vendor detail "offers" tab must match offers by normalized vendor
name, not by exact display-name string.

What it covers: the offers branch of ``vendor_tab``
(GET /v2/partials/vendors/{id}/tab/offers) historically filtered on
``Offer.vendor_name == vendor.display_name`` (exact raw string), so an offer
stored under a slightly different name string (different case / spacing /
company suffix) was dropped — even though the SAME vendor view matches
sightings via ``vendor_name_normalized == normalized_name``. The fix aligns the
offers branch with the normalized-name match used everywhere else on the view.

Called by: pytest. Depends on: conftest fixtures (client, db_session,
test_vendor_card).
"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Offer, VendorCard


def test_offers_tab_matches_on_normalized_name_not_display_string(
    client: TestClient, db_session: Session, test_vendor_card: VendorCard
):
    """An offer whose vendor_name differs from the card's display_name in
    case/spacing/suffix, but whose vendor_name_normalized equals the card's
    normalized_name, MUST appear in the vendor offers tab."""
    # test_vendor_card: display_name="Arrow Electronics", normalized_name="arrow electronics"
    assert test_vendor_card.display_name == "Arrow Electronics"
    assert test_vendor_card.normalized_name == "arrow electronics"

    # Raw vendor_name intentionally != display_name (uppercased + suffix + comma),
    # but the normalized form matches the card's normalized_name.
    offer = Offer(
        vendor_card_id=test_vendor_card.id,
        vendor_name="ARROW ELECTRONICS, INC.",
        vendor_name_normalized="arrow electronics",
        mpn="XZ-NORMMATCH-9001",
    )
    db_session.add(offer)
    db_session.commit()

    resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/offers")
    assert resp.status_code == 200
    # The offer's MPN should be rendered in the offers table.
    assert "XZ-NORMMATCH-9001" in resp.text
    assert "No offers from this vendor yet" not in resp.text
