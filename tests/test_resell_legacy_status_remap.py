"""test_resell_legacy_status_remap.py — migration 193 legacy ExcessList status remap.

Covers the data-only migration that retires the two pre-Resell ``ExcessList`` statuses:
  • ``active``  -> ``open``       (+ stamp ``open_at`` where NULL — a posted window needs
    a start; an already-set ``open_at`` is preserved via COALESCE);
  • ``bidding`` -> ``collecting``;
  • ``closed``  -> ``closed``     (casing normalize only — CLOSED stays DISTINCT from
    ``bid_out``, decision D5);
  • canonical statuses (draft/open/collecting/...) are left untouched.

The migration factors its three data UPDATEs into ``remap_legacy_statuses(connection)`` so
these tests drive the EXACT SQL the migration runs (loaded via importlib, mirroring
tests/test_migration_188_canonical_offers_excess_fk.py — no live PG needed; the remap is
dialect-neutral). Revision metadata is asserted here too (id length, chain onto head).

Called by: pytest
Depends on: alembic/versions/193_resell_legacy_status_remap.py, app.models.excess,
    app.constants, tests.conftest
"""

from __future__ import annotations

import importlib.util
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus
from app.models import Company, User
from app.models.excess import ExcessList

_MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "alembic", "versions", "193_resell_legacy_status_remap.py"
)
_spec = importlib.util.spec_from_file_location("migration_193", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ── Fixtures / helpers ───────────────────────────────────────────────


@pytest.fixture()
def company(db_session: Session) -> Company:
    co = Company(name="Legacy Remap Co")
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def owner(db_session: Session) -> User:
    u = User(email="remap-owner@trioscs.com", name="Remy Owner", role="trader", azure_id="remap-owner-1")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


def _make_list(db: Session, owner: User, company: Company, *, status: str, open_at=None) -> ExcessList:
    el = ExcessList(title="L", company_id=company.id, owner_id=owner.id, status=status, open_at=open_at)
    db.add(el)
    db.commit()
    db.refresh(el)
    return el


def _remap(db: Session) -> None:
    """Drive the migration's data UPDATE against the test session, then re-read the
    ORM."""
    _mod.remap_legacy_statuses(db.connection())
    db.expire_all()


# ── Revision metadata ────────────────────────────────────────────────


class TestRevisionMetadata:
    def test_revision_id(self):
        assert _mod.revision == "193_resell_legacy_status_remap"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores length.
        assert len(_mod.revision) <= 32

    def test_down_revision_chains_onto_current_head(self):
        assert _mod.down_revision == "191_companies_account_type_index"


# ── Data remap ───────────────────────────────────────────────────────


def test_remap_maps_legacy_active_to_open_and_stamps_open_at(db_session, owner, company):
    a = _make_list(db_session, owner, company, status="active", open_at=None)
    assert a.open_at is None

    _remap(db_session)

    assert a.status == ExcessListStatus.OPEN
    assert a.open_at is not None  # a posted window needs a start


def test_remap_maps_legacy_bidding_to_collecting_and_stamps_open_at(db_session, owner, company):
    b = _make_list(db_session, owner, company, status="bidding", open_at=None)
    assert b.open_at is None

    _remap(db_session)

    assert b.status == ExcessListStatus.COLLECTING
    assert b.open_at is not None  # a collecting window must carry a posting-window start


def test_remap_keeps_closed_distinct_from_bid_out(db_session, owner, company):
    """D5: legacy ``closed`` stays CLOSED — it is NOT collapsed into ``bid_out``."""
    c = _make_list(db_session, owner, company, status="closed")

    _remap(db_session)

    assert c.status == ExcessListStatus.CLOSED
    assert c.status != ExcessListStatus.BID_OUT


def test_remap_preserves_existing_open_at(db_session, owner, company):
    """An ``active`` list that already carries ``open_at`` keeps it (COALESCE, not
    overwrite)."""
    stamped = datetime.now(UTC) - timedelta(days=3)
    a = _make_list(db_session, owner, company, status="active", open_at=stamped)

    _remap(db_session)

    assert a.status == ExcessListStatus.OPEN
    assert a.open_at is not None
    assert abs((a.open_at - stamped).total_seconds()) < 1  # unchanged


def test_remap_leaves_canonical_statuses_untouched(db_session, owner, company):
    draft = _make_list(db_session, owner, company, status=ExcessListStatus.DRAFT)
    collecting = _make_list(db_session, owner, company, status=ExcessListStatus.COLLECTING)
    bid_out = _make_list(db_session, owner, company, status=ExcessListStatus.BID_OUT)

    _remap(db_session)

    assert draft.status == ExcessListStatus.DRAFT
    assert draft.open_at is None  # a canonical draft is NOT stamped
    assert collecting.status == ExcessListStatus.COLLECTING
    assert bid_out.status == ExcessListStatus.BID_OUT


def test_downgrade_is_documented_noop(db_session, owner, company):
    """The downgrade is an irreversible-remap no-op — it must not raise or mutate."""
    open_list = _make_list(db_session, owner, company, status=ExcessListStatus.OPEN)

    _mod.downgrade()  # must not raise
    db_session.expire_all()

    assert open_list.status == ExcessListStatus.OPEN
