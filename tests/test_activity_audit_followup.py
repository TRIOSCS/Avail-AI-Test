"""Follow-up audit-repair regressions (#31 NULL increment, #9 third site).

Depends on: tests/conftest.py fixtures (client, db_session, test_user, test_requisition,
            test_vendor_card).
"""


def test_increment_vendor_contact_handles_null_count(db_session, test_vendor_card):
    """#31: _increment_vendor_contact must coalesce a NULL interaction_count, not
    produce NULL+1 (which silently drops the increment on Postgres)."""
    from sqlalchemy import text

    from app.models import VendorContact
    from app.services.activity_service import _increment_vendor_contact

    vc = VendorContact(vendor_card_id=test_vendor_card.id, full_name="Null Counter", source="manual")
    db_session.add(vc)
    db_session.commit()
    # Force a genuine NULL (a column default otherwise coerces it to 0 and masks the bug).
    db_session.execute(text("UPDATE vendor_contacts SET interaction_count = NULL WHERE id = :id"), {"id": vc.id})
    db_session.commit()

    _increment_vendor_contact(vc.id, db_session)
    db_session.commit()
    db_session.refresh(vc)

    assert vc.interaction_count == 1


def test_log_phone_call_uses_canonical_call_logged(client, db_session, test_requisition):
    """#9 (third site): the requisition manual phone-log must record the canonical
    CALL_LOGGED type (rule-meaningful), not the legacy 'phone_call' literal."""
    from app.models import ActivityLog

    resp = client.post(
        f"/v2/partials/requisitions/{test_requisition.id}/log-phone",
        data={"vendor_name": "Arrow Electronics", "vendor_phone": "+14155550000", "notes": "Called re LM317T"},
    )
    assert resp.status_code == 200

    log = db_session.query(ActivityLog).order_by(ActivityLog.id.desc()).first()
    assert log is not None
    assert log.activity_type == "call_logged"
    assert log.channel == "phone"
    assert log.is_meaningful is True
    assert log.contact_phone == "+14155550000"
