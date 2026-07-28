"""test_startup_resell_mirror_backfill.py — Tests for startup._backfill_resell_mirrors.

Deep-review-2 finding #19: migration 199's clean-sweep DELETE stranded lists that were
already open/collecting at deploy time with no customer_excess mirror and no in-app
rebuild path. The idempotent startup backfill restores line-keyed mirror rows via
excess_mirror.sync_list_mirror for exactly those lists, and no-ops on healthy ones.

Called by: pytest
Depends on: tests.conftest (db_session), app.startup, app.services.excess_mirror.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus, UserRole
from app.models.auth import User
from app.models.crm import Company
from app.models.excess import ExcessLineItem, ExcessList
from app.models.sourcing import Sighting
from app.services import excess_mirror
from app.utils.normalization import normalize_mpn_key


@pytest.fixture()
def owner(db_session: Session) -> User:
    u = User(email="mirror-owner@trioscs.com", name="Mirror Owner", role=UserRole.TRADER, is_active=True)
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture()
def company(db_session: Session) -> Company:
    c = Company(name="Mirror Backfill Co")
    db_session.add(c)
    db_session.commit()
    return c


def _published_list(db: Session, owner: User, company: Company, *, title: str) -> ExcessList:
    el = ExcessList(
        title=title,
        company_id=company.id,
        owner_id=owner.id,
        status=ExcessListStatus.DRAFT,
        total_line_items=1,
    )
    db.add(el)
    db.flush()
    db.add(
        ExcessLineItem(
            excess_list_id=el.id,
            part_number="MIRROR-PN-1",
            normalized_part_number=normalize_mpn_key("MIRROR-PN-1"),
            quantity=100,
            condition="New",
        )
    )
    db.commit()
    excess_mirror.publish_list(db, el.id, owner)
    db.refresh(el)
    return el


def _mirror_rows(db: Session, el: ExcessList) -> list[Sighting]:
    line_ids = [r[0] for r in db.query(ExcessLineItem.id).filter_by(excess_list_id=el.id)]
    return (
        db.query(Sighting)
        .filter(
            Sighting.source_type == excess_mirror.MIRROR_SOURCE_TYPE,
            Sighting.excess_line_item_id.in_(line_ids),
        )
        .all()
    )


def _run_backfill(db: Session) -> None:
    from app import startup

    with patch.object(startup, "SessionLocal", lambda: db):
        # The backfill closes its session; hand it a closable proxy-free session by
        # patching close to a no-op so the shared test session survives.
        with patch.object(db, "close", lambda: None):
            startup._backfill_resell_mirrors()


class TestResellMirrorBackfill:
    def test_restores_swept_open_list(self, db_session, owner, company):
        el = _published_list(db_session, owner, company, title="Swept open")
        assert _mirror_rows(db_session, el), "publish must mirror"
        # Simulate migration 199's sweep: delete every mirror sighting for the list.
        for s in _mirror_rows(db_session, el):
            db_session.delete(s)
        db_session.commit()
        assert not _mirror_rows(db_session, el)

        _run_backfill(db_session)

        restored = _mirror_rows(db_session, el)
        assert restored, "backfill must restore line-keyed mirror rows"
        assert all(s.excess_line_item_id is not None for s in restored)

    def test_noop_on_healthy_list(self, db_session, owner, company):
        el = _published_list(db_session, owner, company, title="Healthy open")
        before = {s.id for s in _mirror_rows(db_session, el)}

        _run_backfill(db_session)

        after = {s.id for s in _mirror_rows(db_session, el)}
        assert after == before, "a healthy list must not be rewritten"

    def test_skips_resolved_lists(self, db_session, owner, company):
        el = _published_list(db_session, owner, company, title="Closed later")
        for s in _mirror_rows(db_session, el):
            db_session.delete(s)
        el.status = ExcessListStatus.CLOSED
        db_session.commit()

        _run_backfill(db_session)

        assert not _mirror_rows(db_session, el), "a resolved list must stay unmirrored"

    def test_idempotent_second_run(self, db_session, owner, company):
        el = _published_list(db_session, owner, company, title="Twice swept")
        for s in _mirror_rows(db_session, el):
            db_session.delete(s)
        db_session.commit()

        _run_backfill(db_session)
        first = {s.id for s in _mirror_rows(db_session, el)}
        _run_backfill(db_session)
        second = {s.id for s in _mirror_rows(db_session, el)}
        assert first == second
