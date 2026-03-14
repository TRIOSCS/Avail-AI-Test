"""
tests/test_phase3_data_model_fixes.py — Tests for Phase 3 data model fixes.

Covers:
- material_cards.deleted_at has index for soft-delete performance
- site_contacts has unique constraint on (customer_site_id, email)
- Company count columns are NOT NULL with server_default

Called by: pytest
Depends on: app/models/intelligence.py, app/models/crm.py, alembic/versions/077
"""

from app.models import Company, MaterialCard
from app.models.crm import SiteContact


class TestMaterialCardDeletedAtIndex:
    """Verify deleted_at column has index=True in model definition."""

    def test_deleted_at_has_index(self):
        col = MaterialCard.__table__.columns["deleted_at"]
        assert col.index is True, "deleted_at should have index=True for soft-delete performance"


class TestCompanyCountColumnsNotNull:
    """Verify denormalized count columns are NOT NULL."""

    def test_site_count_not_nullable(self):
        col = Company.__table__.columns["site_count"]
        assert col.nullable is False

    def test_open_req_count_not_nullable(self):
        col = Company.__table__.columns["open_req_count"]
        assert col.nullable is False

    def test_site_count_default(self):
        col = Company.__table__.columns["site_count"]
        assert col.server_default is not None

    def test_new_company_has_zero_counts(self, db_session):
        c = Company(name="Test Corp")
        db_session.add(c)
        db_session.commit()
        assert c.site_count == 0
        assert c.open_req_count == 0


class TestSiteContactUniqueConstraint:
    """Verify site_contacts unique constraint is in migration (PostgreSQL-only)."""

    def test_unique_index_in_migration(self):
        """Check migration 077 creates the partial unique index."""
        import os

        migration_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "alembic",
            "versions",
            "077_add_indexes_and_constraints.py",
        )
        with open(migration_path) as f:
            content = f.read()
        assert "uq_site_contacts_site_email" in content


class TestMigration077Exists:
    """Verify migration file exists and has correct structure."""

    def test_migration_file_has_upgrade_and_downgrade(self):
        import os

        migration_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "alembic",
            "versions",
            "077_add_indexes_and_constraints.py",
        )
        assert os.path.exists(migration_path), "Migration 077 file must exist"
        with open(migration_path) as f:
            content = f.read()
        assert 'revision = "077"' in content
        assert 'down_revision = "076"' in content
        assert "def upgrade" in content
        assert "def downgrade" in content
        assert "ix_material_cards_deleted_at" in content
        assert "uq_site_contacts_site_email" in content
