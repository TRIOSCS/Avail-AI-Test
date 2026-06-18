# tests/test_offer_qualification_model.py
# What: asserts the 3 new qualification columns + the new condition enum exist and round-trip.
# Called by: pytest. Depends on: conftest db_session/test_requisition/test_user fixtures.
from app.constants import OfferCondition, QualificationStatus
from app.models.offers import Offer


def test_offer_qualification_columns_roundtrip(db_session, test_requisition, test_user):
    o = Offer(
        requisition_id=test_requisition.id,
        vendor_name="Arrow",
        mpn="LM317T",
        condition=OfferCondition.PULLS.value,
        qualification={"usage": "systems", "requests": []},
        qualification_note="Pulls — packaged in Trays, pulled from systems.",
        qualification_status=QualificationStatus.ESSENTIALS.value,
        entered_by_id=test_user.id,
    )
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    assert o.qualification["usage"] == "systems"
    assert o.qualification_status == "essentials"
    assert OfferCondition("new_no_pkg") is OfferCondition.NEW_NO_PKG


def test_qualification_summary_property(db_session, test_requisition, test_user):
    from app.models.offers import Offer

    o = Offer(
        requisition_id=test_requisition.id,
        vendor_name="V",
        mpn="LM317T",
        condition="pulls",
        packaging="Trays",
        qualification={"usage": "boards"},
        entered_by_id=test_user.id,
    )
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    s = o.qualification_summary
    assert s["status"] in ("essentials", "complete", "incomplete")
    assert s["total"] == 4 and 0 <= s["filled"] <= 4
