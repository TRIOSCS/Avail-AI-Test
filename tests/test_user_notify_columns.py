"""Test that User model has notification-preference columns defaulting to True.

Tests Task 6 of the settings-refine program: migration 149 adds
notify_buyplan_email_enabled and notify_new_offer_alert_enabled to users.

Called by: pytest
Depends on: app/models/auth.py (User), tests/conftest.py (db_session fixture)
"""

from datetime import UTC, datetime


def test_user_has_notify_pref_columns_default_true(db_session):
    from app.models.auth import User

    u = User(
        email="n@trioscs.com",
        name="N",
        role="buyer",
        azure_id="az-notify",
        created_at=datetime.now(UTC),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    assert u.notify_buyplan_email_enabled is True
    assert u.notify_new_offer_alert_enabled is True
