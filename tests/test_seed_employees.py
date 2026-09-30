"""Tests for app/management/seed_employees.py — TRIO employee seeder + 8x8 mapping.

Covers dry-run (writes nothing), --apply creation (roles, extensions, the
Trading-only eight_by_eight_enabled flag, system INVITE audit rows), idempotent
re-runs, the never-modify-existing-users guarantee, empty-extension first-fill
(including the inactive-user and pre-existing-enabled-extension guards), the
enabled-extensions-are-unique and Trading<->8x8 data invariants, and the
main() CLI wiring of --apply.

Called by: pytest
Depends on: app/management/seed_employees.py, conftest (db_session),
            models.User / UserAdminAudit.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger
from sqlalchemy.orm import Session

from app.constants import UserAuditAction, UserRole
from app.management.seed_employees import EMPLOYEES, main, seed
from app.models import User, UserAdminAudit

TRADING_EMAILS = {e["email"] for e in EMPLOYEES if e["enable_8x8"]}


def test_dry_run_writes_nothing(db_session: Session):
    counts = seed(db_session, apply=False)
    assert counts["created"] == len(EMPLOYEES)
    assert db_session.query(User).count() == 0
    assert db_session.query(UserAdminAudit).count() == 0


def test_apply_creates_all_with_roles_extensions_and_8x8(db_session: Session):
    counts = seed(db_session, apply=True)
    assert counts == {"created": len(EMPLOYEES), "extension_filled": 0, "skipped": 0}
    assert db_session.query(User).count() == len(EMPLOYEES)

    # 8x8 CDR flag is on for exactly the Trading desk
    enabled = {u.email for u in db_session.query(User).filter(User.eight_by_eight_enabled.is_(True))}
    assert enabled == TRADING_EMAILS
    assert len(TRADING_EMAILS) == 12

    # Spot-check one of each role mapping + extension
    atmojo = db_session.query(User).filter(User.email == "datmojo@trioscs.com").one()
    assert (atmojo.role, atmojo.eight_by_eight_extension) == (UserRole.BUYER, "1012")
    cash = db_session.query(User).filter(User.email == "pcash@trioscs.com").one()
    assert (cash.role, cash.eight_by_eight_extension) == (UserRole.SALES, "1004")
    tuckman = db_session.query(User).filter(User.email == "dtuckman@trioscs.com").one()
    assert (tuckman.role, tuckman.eight_by_eight_extension) == (UserRole.TRADER, "1006")
    khoury = db_session.query(User).filter(User.email == "mkhoury@trioscs.com").one()
    assert (khoury.role, khoury.eight_by_eight_enabled) == (UserRole.MANAGER, False)
    assert all(u.is_active for u in db_session.query(User))

    # Every creation wrote a system INVITE audit row (actor NULL)
    audits = db_session.query(UserAdminAudit).all()
    assert len(audits) == len(EMPLOYEES)
    assert all(a.actor_id is None and a.action == UserAuditAction.INVITE for a in audits)
    assert all(a.detail.get("source") == "seed_employees" for a in audits)


def test_apply_is_idempotent(db_session: Session):
    seed(db_session, apply=True)
    counts = seed(db_session, apply=True)
    assert counts == {"created": 0, "extension_filled": 0, "skipped": len(EMPLOYEES)}
    assert db_session.query(User).count() == len(EMPLOYEES)
    assert db_session.query(UserAdminAudit).count() == len(EMPLOYEES)  # no new audit rows


def test_existing_user_with_extension_is_never_modified(db_session: Session):
    existing = User(
        email="pcash@trioscs.com",
        name="Patti Cash (edited)",
        role=str(UserRole.MANAGER),
        is_active=False,
        eight_by_eight_extension="9999",
        eight_by_eight_enabled=False,
    )
    db_session.add(existing)
    db_session.commit()

    counts = seed(db_session, apply=True)
    assert counts["skipped"] == 1
    assert counts["created"] == len(EMPLOYEES) - 1

    db_session.refresh(existing)
    assert existing.name == "Patti Cash (edited)"
    assert existing.role == UserRole.MANAGER
    assert existing.is_active is False
    assert existing.eight_by_eight_extension == "9999"
    assert existing.eight_by_eight_enabled is False


def test_existing_user_empty_extension_gets_first_fill(db_session: Session):
    existing = User(email="jfu@trioscs.com", name="Jenny Fu", role=str(UserRole.BUYER), is_active=True)
    db_session.add(existing)
    db_session.commit()

    counts = seed(db_session, apply=True)
    assert counts["extension_filled"] == 1

    db_session.refresh(existing)
    assert existing.eight_by_eight_extension == "1023"
    assert existing.eight_by_eight_enabled is True  # Trading row → CDR mapping on
    # First-fill touches only the 8x8 columns — no audit row for the existing user
    assert db_session.query(UserAdminAudit).count() == len(EMPLOYEES) - 1


def test_enabled_extensions_are_unique():
    """CDR attribution requires a 1:1 extension→user map among enabled rows.

    Shared site lines (x1022 etc.) must therefore never carry enable_8x8=True.
    """
    enabled_exts = [e["extension"] for e in EMPLOYEES if e["enable_8x8"]]
    assert len(enabled_exts) == len(set(enabled_exts))
    emails = [e["email"] for e in EMPLOYEES]
    assert len(emails) == len(set(emails))


def test_trading_department_is_exactly_the_8x8_enabled_set():
    """A Trading row added without enable_8x8=True (or the reverse) must fail loudly."""
    trading = {e["email"] for e in EMPLOYEES if e["department"] == "Trading"}
    enabled = {e["email"] for e in EMPLOYEES if e["enable_8x8"]}
    assert trading == enabled


def _capture_warnings() -> tuple[list[str], int]:
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    return messages, sink_id


def test_create_leaves_8x8_off_when_another_user_already_enabled_on_extension(db_session: Session):
    other = User(
        email="former.owner@trioscs.com",
        name="Former Owner",
        role=str(UserRole.BUYER),
        is_active=True,
        eight_by_eight_extension="1012",
        eight_by_eight_enabled=True,
    )
    db_session.add(other)
    db_session.commit()

    messages, sink_id = _capture_warnings()
    try:
        counts = seed(db_session, apply=True)
    finally:
        logger.remove(sink_id)
    assert counts["created"] == len(EMPLOYEES)

    atmojo = db_session.query(User).filter(User.email == "datmojo@trioscs.com").one()
    assert atmojo.eight_by_eight_extension == "1012"
    assert atmojo.eight_by_eight_enabled is False

    # The poller's ext->user dict still has exactly one owner for x1012
    enabled_1012 = db_session.query(User).filter(
        User.eight_by_eight_enabled.is_(True), User.eight_by_eight_extension == "1012"
    )
    assert [u.email for u in enabled_1012] == ["former.owner@trioscs.com"]

    # Pre-existing user untouched
    db_session.refresh(other)
    assert (other.eight_by_eight_extension, other.eight_by_eight_enabled) == ("1012", True)

    # Other Trading rows are unaffected, and the warning names both emails + extension
    enabled = {u.email for u in db_session.query(User).filter(User.eight_by_eight_enabled.is_(True))}
    assert enabled == (TRADING_EMAILS - {"datmojo@trioscs.com"}) | {"former.owner@trioscs.com"}
    assert len(messages) == 1
    assert "former.owner@trioscs.com" in messages[0]
    assert "datmojo@trioscs.com" in messages[0]
    assert "1012" in messages[0]


def test_first_fill_leaves_8x8_off_when_another_user_already_enabled_on_extension(db_session: Session):
    other = User(
        email="former.owner@trioscs.com",
        name="Former Owner",
        role=str(UserRole.BUYER),
        is_active=True,
        eight_by_eight_extension="1023",
        eight_by_eight_enabled=True,
    )
    existing = User(email="jfu@trioscs.com", name="Jenny Fu", role=str(UserRole.BUYER), is_active=True)
    db_session.add_all([other, existing])
    db_session.commit()

    counts = seed(db_session, apply=True)
    assert counts["extension_filled"] == 1

    db_session.refresh(existing)
    assert existing.eight_by_eight_extension == "1023"
    assert existing.eight_by_eight_enabled is False
    db_session.refresh(other)
    assert (other.eight_by_eight_extension, other.eight_by_eight_enabled) == ("1023", True)


def test_first_fill_does_not_enable_inactive_user(db_session: Session):
    existing = User(email="jfu@trioscs.com", name="Jenny Fu", role=str(UserRole.BUYER), is_active=False)
    db_session.add(existing)
    db_session.commit()

    counts = seed(db_session, apply=True)
    assert counts["extension_filled"] == 1

    db_session.refresh(existing)
    assert existing.eight_by_eight_extension == "1023"  # extension still filled
    assert existing.eight_by_eight_enabled is False  # deactivated employee stays out of the poller
    assert existing.is_active is False


@pytest.mark.parametrize(("extra_args", "expected_apply"), [([], False), (["--apply"], True)])
def test_main_passes_apply_flag_to_seed_and_closes_session(extra_args: list[str], expected_apply: bool):
    session = MagicMock()
    with (
        patch.object(sys, "argv", ["seed_employees", *extra_args]),
        patch("app.database.SessionLocal", return_value=session),
        patch("app.management.seed_employees.seed", return_value={"created": 0}) as mock_seed,
    ):
        main()

    mock_seed.assert_called_once_with(session, apply=expected_apply)
    session.close.assert_called_once_with()


def test_main_closes_session_when_seed_raises():
    session = MagicMock()
    with (
        patch.object(sys, "argv", ["seed_employees", "--apply"]),
        patch("app.database.SessionLocal", return_value=session),
        patch("app.management.seed_employees.seed", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        main()

    session.close.assert_called_once_with()
