"""Tests for ProspectAccount and DiscoveryBatch models."""

import os

os.environ["TESTING"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "false"

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, User
from app.models.discovery_batch import DiscoveryBatch
from app.models.prospect_account import ProspectAccount


class TestProspectAccountModel:
    """ProspectAccount ORM model tests."""

    def test_create_with_all_fields(self, db_session: Session):
        """All columns populated — round-trips through the DB."""
        pa = ProspectAccount(
            name="Sensata Technologies",
            domain="sensata.com",
            website="https://sensata.com",
            industry="Aerospace & Defense",
            naics_code="336412",
            employee_count_range="1001-5000",
            revenue_range="$1B+",
            hq_location="Attleboro, MA, US",
            region="US",
            description="Sensor-heavy manufacturer",
            parent_company_domain=None,
            fit_score=85,
            fit_reasoning="Strong ICP match: aerospace, large employee count",
            readiness_score=60,
            readiness_signals={"hiring_buyers": True, "recent_rfq": False},
            discovery_source="explorium",
            status="suggested",
            import_priority="priority",
            historical_context={"quote_count": 47},
            contacts_preview=[{"name": "John Doe", "title": "VP Purchasing"}],
            similar_customers=[{"name": "Honeywell", "score": 0.92}],
            enrichment_data={"explorium_id": "EX-12345"},
            email_pattern="{first}.{last}@sensata.com",
            ai_writeup="Sensata is a strong prospect because...",
        )
        db_session.add(pa)
        db_session.commit()
        db_session.refresh(pa)

        assert pa.id is not None
        assert pa.name == "Sensata Technologies"
        assert pa.domain == "sensata.com"
        assert pa.fit_score == 85
        assert pa.readiness_signals == {"hiring_buyers": True, "recent_rfq": False}
        assert pa.contacts_preview[0]["name"] == "John Doe"
        assert pa.similar_customers[0]["name"] == "Honeywell"
        assert pa.created_at is not None

    def test_create_minimal(self, db_session: Session):
        """Only required fields — name, domain, discovery_source."""
        pa = ProspectAccount(
            name="Test Corp",
            domain="testcorp.com",
            discovery_source="manual",
        )
        db_session.add(pa)
        db_session.commit()
        db_session.refresh(pa)

        assert pa.id is not None
        assert pa.fit_score == 0
        assert pa.readiness_score == 0
        assert pa.status == "suggested"

    def test_unique_domain_constraint(self, db_session: Session):
        """Duplicate domain raises IntegrityError."""
        pa1 = ProspectAccount(name="Corp A", domain="dupe.com", discovery_source="explorium")
        pa2 = ProspectAccount(name="Corp B", domain="dupe.com", discovery_source="apollo")
        db_session.add(pa1)
        db_session.commit()
        db_session.add(pa2)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_status_transitions(self, db_session: Session):
        """Status can be changed through the full lifecycle."""
        pa = ProspectAccount(
            name="Lifecycle Co",
            domain="lifecycle.com",
            discovery_source="salesforce_import",
            status="suggested",
        )
        db_session.add(pa)
        db_session.commit()

        for new_status in ("claimed", "converted", "dismissed", "expired"):
            pa.status = new_status
            db_session.commit()
            db_session.refresh(pa)
            assert pa.status == new_status

    def test_fk_claimed_by_user(self, db_session: Session, test_user: User):
        """claimed_by FK links to users table."""
        pa = ProspectAccount(
            name="Claimed Co",
            domain="claimed.com",
            discovery_source="apollo",
            claimed_by=test_user.id,
            claimed_at=datetime.now(timezone.utc),
        )
        db_session.add(pa)
        db_session.commit()
        db_session.refresh(pa)

        assert pa.claimed_by == test_user.id

    def test_fk_company_id(self, db_session: Session, test_company: Company):
        """company_id FK links to companies table."""
        pa = ProspectAccount(
            name="Linked Co",
            domain="linked.com",
            discovery_source="salesforce_import",
            company_id=test_company.id,
        )
        db_session.add(pa)
        db_session.commit()
        db_session.refresh(pa)

        assert pa.company_id == test_company.id

    def test_defaults(self, db_session: Session):
        """Default values are applied correctly."""
        pa = ProspectAccount(
            name="Defaults Co",
            domain="defaults.com",
            discovery_source="email_history",
        )
        db_session.add(pa)
        db_session.commit()
        db_session.refresh(pa)

        assert pa.fit_score == 0
        assert pa.readiness_score == 0
        assert pa.status == "suggested"
        assert pa.claimed_by is None
        assert pa.dismissed_by is None
        assert pa.company_id is None

    def test_jsonb_fields(self, db_session: Session):
        """JSONB columns accept complex nested data."""
        pa = ProspectAccount(
            name="JSON Co",
            domain="jsonco.com",
            discovery_source="explorium",
            readiness_signals={
                "hiring_buyers": True,
                "recent_rfq": False,
                "growth_indicators": ["new_facility", "acquisition"],
            },
            historical_context={
                "quote_count": 47,
                "last_activity": "2024-08-15",
                "total_revenue": 125000.50,
                "years_active": 3,
            },
            contacts_preview=[
                {
                    "name": "Jane Doe",
                    "title": "Director of Purchasing",
                    "email": "jane@jsonco.com",
                    "verified": True,
                    "linkedin_url": "https://linkedin.com/in/janedoe",
                    "seniority": "director",
                },
                {
                    "name": "Bob Smith",
                    "title": "Buyer",
                    "email": "bob@jsonco.com",
                    "verified": False,
                },
            ],
            enrichment_data={
                "explorium": {"company_id": "EX-999", "fetched_at": "2026-02-25"},
                "apollo": {"org_id": "AP-555"},
            },
        )
        db_session.add(pa)
        db_session.commit()
        db_session.refresh(pa)

        assert pa.readiness_signals["growth_indicators"] == ["new_facility", "acquisition"]
        assert pa.historical_context["total_revenue"] == 125000.50
        assert len(pa.contacts_preview) == 2
        assert pa.enrichment_data["explorium"]["company_id"] == "EX-999"

    def test_null_optional_fields(self, db_session: Session):
        """Nullable fields can be None."""
        pa = ProspectAccount(
            name="Sparse Co",
            domain="sparse.com",
            discovery_source="manual",
        )
        db_session.add(pa)
        db_session.commit()
        db_session.refresh(pa)

        assert pa.website is None
        assert pa.industry is None
        assert pa.naics_code is None
        assert pa.email_pattern is None
        assert pa.ai_writeup is None
        assert pa.last_enriched_at is None
        assert pa.parent_company_domain is None


class TestDiscoveryBatchModel:
    """DiscoveryBatch ORM model tests."""

    def test_create_batch(self, db_session: Session):
        """Create a discovery batch with all fields."""
        batch = DiscoveryBatch(
            batch_id="2026-02-25-aero-us",
            source="explorium",
            segment="aerospace",
            regions=["US"],
            search_filters={"naics": ["336412"], "employee_min": 200},
            status="complete",
            prospects_found=150,
            prospects_new=42,
            prospects_updated=8,
            credits_used=150,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )
        db_session.add(batch)
        db_session.commit()
        db_session.refresh(batch)

        assert batch.id is not None
        assert batch.batch_id == "2026-02-25-aero-us"
        assert batch.prospects_new == 42
        assert batch.regions == ["US"]
        assert batch.search_filters["naics"] == ["336412"]

    def test_unique_batch_id(self, db_session: Session):
        """Duplicate batch_id raises IntegrityError."""
        b1 = DiscoveryBatch(
            batch_id="dup-batch",
            source="explorium",
            started_at=datetime.now(timezone.utc),
        )
        b2 = DiscoveryBatch(
            batch_id="dup-batch",
            source="apollo",
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(b1)
        db_session.commit()
        db_session.add(b2)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_batch_defaults(self, db_session: Session):
        """Default values: status=running, counts=0."""
        batch = DiscoveryBatch(
            batch_id="defaults-batch",
            source="email_mining",
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(batch)
        db_session.commit()
        db_session.refresh(batch)

        assert batch.status == "running"
        assert batch.prospects_found == 0
        assert batch.prospects_new == 0
        assert batch.prospects_updated == 0
        assert batch.credits_used == 0
        assert batch.error_message is None

    def test_fk_prospect_to_batch(self, db_session: Session):
        """ProspectAccount.discovery_batch_id links to DiscoveryBatch."""
        batch = DiscoveryBatch(
            batch_id="linked-batch",
            source="explorium",
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(batch)
        db_session.flush()

        pa = ProspectAccount(
            name="Linked Prospect",
            domain="linked-prospect.com",
            discovery_source="explorium",
            discovery_batch_id=batch.id,
        )
        db_session.add(pa)
        db_session.commit()
        db_session.refresh(pa)

        assert pa.discovery_batch_id == batch.id


class TestMigrationScript:
    """Tests for the SF pool migration logic (normalize_domain)."""

    def test_normalize_domain_basic(self):
        from scripts.migrate_sf_pool import normalize_domain

        assert normalize_domain("Example.COM") == "example.com"
        assert normalize_domain("www.example.com") == "example.com"
        assert normalize_domain("https://www.example.com/") == "example.com"
        assert normalize_domain("http://example.com") == "example.com"
        assert normalize_domain("  EXAMPLE.com  ") == "example.com"

    def test_normalize_domain_empty(self):
        from scripts.migrate_sf_pool import normalize_domain

        assert normalize_domain(None) is None
        assert normalize_domain("") is None
        assert normalize_domain("   ") is None

    def test_migrate_creates_prospects(self, db_session: Session):
        """Migration script copies unowned companies into prospect_accounts."""
        from unittest.mock import patch

        from scripts.migrate_sf_pool import migrate

        # Create pool companies (unowned, active, not dismissed)
        co1 = Company(
            name="Pool Co A",
            domain="poolco-a.com",
            industry="Aerospace",
            is_active=True,
            account_owner_id=None,
            import_priority="priority",
            sf_account_id="SF-001",
        )
        co2 = Company(
            name="Pool Co B",
            domain="poolco-b.com",
            industry="EMS",
            is_active=True,
            account_owner_id=None,
            import_priority="standard",
        )
        # Owned company — should NOT be migrated
        user = User(
            email="owner@test.com",
            name="Owner",
            role="buyer",
            azure_id="az-owner",
        )
        db_session.add(user)
        db_session.flush()
        co_owned = Company(
            name="Owned Co",
            domain="owned.com",
            is_active=True,
        )
        db_session.add_all([co1, co2, co_owned])
        db_session.flush()
        # Create a CustomerSite with an owner so co_owned is excluded
        site = CustomerSite(
            company_id=co_owned.id,
            site_name="Owned Site",
            owner_id=user.id,
        )
        db_session.add(site)
        # Dismissed company — should NOT be migrated
        co_dismissed = Company(
            name="Dismissed Co",
            domain="dismissed.com",
            is_active=True,
            import_priority="dismissed",
        )
        # Company with no domain — should be skipped
        co_no_domain = Company(
            name="No Domain Co",
            domain=None,
            is_active=True,
        )
        db_session.add_all([co_dismissed, co_no_domain])
        db_session.commit()

        # Patch SessionLocal to return our test session
        with patch("scripts.migrate_sf_pool.SessionLocal", return_value=db_session):
            result = migrate(dry_run=False)

        assert result["total_pool"] == 3  # co1, co2, co_no_domain
        assert result["migrated"] == 2  # co1 and co2
        assert result["skipped_no_domain"] == 1
        assert result["skipped_duplicate"] == 0

        # Verify prospect records
        prospects = db_session.query(ProspectAccount).all()
        assert len(prospects) == 2

        pa_a = db_session.query(ProspectAccount).filter_by(domain="poolco-a.com").first()
        assert pa_a.name == "Pool Co A"
        assert pa_a.company_id == co1.id
        assert pa_a.discovery_source == "salesforce_import"
        assert pa_a.import_priority == "priority"
        assert pa_a.historical_context == {"sf_account_id": "SF-001"}

    def test_migrate_dry_run(self, db_session: Session):
        """Dry run logs but doesn't write to DB."""
        from unittest.mock import patch

        from scripts.migrate_sf_pool import migrate

        co = Company(
            name="Dry Run Co",
            domain="dryrun.com",
            is_active=True,
            account_owner_id=None,
        )
        db_session.add(co)
        db_session.commit()

        with patch("scripts.migrate_sf_pool.SessionLocal", return_value=db_session):
            result = migrate(dry_run=True)

        assert result["migrated"] == 1
        # No actual records should be created
        assert db_session.query(ProspectAccount).count() == 0
        assert db_session.query(DiscoveryBatch).count() == 0

    def test_migrate_idempotent(self, db_session: Session):
        """Running twice doesn't create duplicates."""
        from unittest.mock import patch

        from scripts.migrate_sf_pool import migrate

        co = Company(
            name="Idempotent Co",
            domain="idempotent.com",
            is_active=True,
            account_owner_id=None,
        )
        db_session.add(co)
        db_session.commit()

        with patch("scripts.migrate_sf_pool.SessionLocal", return_value=db_session):
            result1 = migrate(dry_run=False)
            result2 = migrate(dry_run=False)

        assert result1["migrated"] == 1
        assert result2["migrated"] == 0
        assert result2["skipped_duplicate"] == 1
        assert db_session.query(ProspectAccount).count() == 1


class TestCompanySourceColumn:
    """Verify the source column on Company model."""

    def test_source_default(self, db_session: Session):
        """New Company gets source='manual' by default."""
        co = Company(name="Source Test", is_active=True)
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)
        assert co.source == "manual"

    def test_source_set_explicitly(self, db_session: Session):
        """Source can be set to other values."""
        co = Company(
            name="SF Import Co",
            is_active=True,
            source="salesforce_import",
        )
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)
        assert co.source == "salesforce_import"
