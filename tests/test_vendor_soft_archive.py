"""tests/test_vendor_soft_archive.py — TDD tests for vendor soft-archive (CRM P5 slice).

Covers:
- Migration 165 metadata (id <= 32 vs PG VARCHAR(32), chains onto 164) + an executable
  upgrade -> downgrade -> upgrade pass on a scratch in-memory SQLite engine that asserts
  the vendor_cards.is_active column + index appear on upgrade and are gone on downgrade.
- VendorCard.is_active model column: NOT NULL, server_default, defaults True on insert.
- Archive / unarchive routes flip is_active (never delete) + auth (404 / anon-denied).
- Default vendor list excludes archived vendors; ?include_archived=1 includes them.

Mirrors the customer/company soft-archive pattern (Company.is_active + deactivate/
reactivate routes) and the vendor-parity test harness style.

Called by: pytest
Depends on: conftest fixtures (client, unauthenticated_client, test_vendor_card,
            db_session); alembic/versions/165_vendor_is_active.py; migration_harness.run_ops
"""

from __future__ import annotations

import importlib.util
import os

from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import VendorCard
from tests.migration_harness import run_ops

_MIGRATION_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "165_vendor_is_active.py")
_spec = importlib.util.spec_from_file_location("migration_165", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ── Migration 165 metadata ───────────────────────────────────────────────────


class TestMigration165Metadata:
    def test_revision_id(self):
        assert _mod.revision == "165_vendor_is_active"

    def test_revision_id_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores length.
        assert len(_mod.revision) <= 32

    def test_down_revision_chains_onto_164(self):
        assert _mod.down_revision == "164_sp2_qp_sales_rename"


class TestMigration165Execution:
    """Upgrade -> downgrade -> upgrade on a scratch SQLite engine."""

    @staticmethod
    def _engine():
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        md = MetaData()
        Table("vendor_cards", md, Column("id", Integer, primary_key=True))
        md.create_all(engine)
        return engine

    def test_upgrade_adds_is_active_column_and_index(self):
        engine = self._engine()
        run_ops(engine, _mod.upgrade)
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("vendor_cards")}
        assert "is_active" in cols, "upgrade must add vendor_cards.is_active"
        idx = {i["name"] for i in insp.get_indexes("vendor_cards")}
        assert "ix_vendor_cards_is_active" in idx, "upgrade must create ix_vendor_cards_is_active"

    def test_downgrade_drops_is_active(self):
        engine = self._engine()
        run_ops(engine, _mod.upgrade)
        run_ops(engine, _mod.downgrade)
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("vendor_cards")}
        assert "is_active" not in cols, "downgrade must drop vendor_cards.is_active"

    def test_upgrade_downgrade_upgrade_round_trips(self):
        engine = self._engine()
        run_ops(engine, _mod.upgrade)
        run_ops(engine, _mod.downgrade)
        run_ops(engine, _mod.upgrade)  # must not raise


# ── Model column ─────────────────────────────────────────────────────────────


class TestVendorCardIsActiveColumn:
    def test_column_is_not_null_with_server_default(self):
        col = VendorCard.__table__.c.is_active
        assert col.nullable is False, "VendorCard.is_active should be NOT NULL"
        assert col.server_default is not None, "VendorCard.is_active needs a server_default"

    def test_defaults_true_on_insert(self, db_session: Session):
        card = VendorCard(normalized_name="default active co", display_name="Default Active Co")
        db_session.add(card)
        db_session.commit()
        db_session.refresh(card)
        assert card.is_active is True


# ── Archive / unarchive routes ───────────────────────────────────────────────


class TestArchiveUnarchive:
    def test_archive_sets_inactive(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        assert test_vendor_card.is_active is True
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/archive")
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.is_active is False

    def test_unarchive_sets_active(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        test_vendor_card.is_active = False
        db_session.commit()
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/unarchive")
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.is_active is True

    def test_archive_never_deletes_row(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        vid = test_vendor_card.id
        client.post(f"/v2/partials/vendors/{vid}/archive")
        assert db_session.get(VendorCard, vid) is not None, "soft-archive must not delete the row"

    def test_archive_not_found_404(self, client: TestClient):
        assert client.post("/v2/partials/vendors/999999/archive").status_code == 404

    def test_archive_anon_denied(self, unauthenticated_client: TestClient, test_vendor_card: VendorCard):
        resp = unauthenticated_client.post(f"/v2/partials/vendors/{test_vendor_card.id}/archive")
        assert resp.status_code == 401


# ── Default-list filtering ───────────────────────────────────────────────────


class TestListExcludesArchived:
    @staticmethod
    def _make_archived(db_session: Session) -> VendorCard:
        card = VendorCard(
            normalized_name="zarchived vendor co",
            display_name="ZArchived Vendor Co",
            sighting_count=7,
            is_active=False,
        )
        db_session.add(card)
        db_session.commit()
        db_session.refresh(card)
        return card

    def test_default_list_hides_archived(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        archived = self._make_archived(db_session)
        resp = client.get("/v2/partials/vendors")
        assert resp.status_code == 200
        assert test_vendor_card.display_name in resp.text  # active vendor present
        assert archived.display_name not in resp.text  # archived hidden

    def test_include_archived_shows_archived(self, client: TestClient, db_session: Session):
        archived = self._make_archived(db_session)
        resp = client.get("/v2/partials/vendors?include_archived=1")
        assert resp.status_code == 200
        assert archived.display_name in resp.text
