"""Tests for app/services/contact_quality.py — validation, dedup, scoring, stale
detection.

Called by: pytest
Depends on: conftest fixtures (db_session, test_company, test_customer_site)
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.crm import Company, SiteContact
from app.services.contact_quality import (
    compute_enrichment_status,
    dedup_contacts,
    flag_stale_contacts,
    score_contact_completeness,
    update_company_enrichment_status,
    validate_contact,
)

# ── validate_contact ────────────────────────────────────────────────


class TestValidateContact:
    """Tests for contact dict validation."""

    @pytest.mark.parametrize(
        ("contact", "expect_empty_issues"),
        [
            ({"full_name": "Jane Doe", "email": "jane@example.com", "phone": "+1-555-1234"}, True),
            ({"full_name": "Jane Doe", "email": "jane@example.com", "phone": ""}, True),
            ({"full_name": "Jane Doe", "email": "jane@example.com"}, False),
            ({"full_name": "Jo", "email": "jo@a.co"}, False),
        ],
        ids=["valid_contact", "phone_empty_is_ok", "phone_absent_is_ok", "valid_minimal_name"],
    )
    def test_valid_contacts(self, contact, expect_empty_issues):
        is_valid, issues = validate_contact(contact)
        assert is_valid is True
        if expect_empty_issues:
            assert issues == []

    @pytest.mark.parametrize(
        ("contact", "expected_issue"),
        [
            ({"full_name": "", "email": "jane@example.com"}, "missing_name"),
            ({"full_name": "J", "email": "jane@example.com"}, "missing_name"),
            ({"email": "jane@example.com"}, "missing_name"),
            ({"full_name": "  ", "email": "jane@example.com"}, "missing_name"),
            ({"full_name": "Jane Doe", "email": ""}, "missing_email"),
            ({"full_name": "Jane Doe"}, "missing_email"),
            ({"full_name": "Jane Doe", "email": "not-an-email"}, "invalid_email_format"),
            ({"full_name": "Jane Doe", "email": "jane@example.com", "phone": "123"}, "phone_too_short"),
        ],
        ids=[
            "missing_name",
            "name_too_short",
            "name_none",
            "whitespace_name_stripped",
            "missing_email",
            "email_none",
            "invalid_email_format",
            "phone_too_short",
        ],
    )
    def test_invalid_contacts(self, contact, expected_issue):
        is_valid, issues = validate_contact(contact)
        assert is_valid is False
        assert expected_issue in issues

    def test_multiple_issues(self):
        contact = {"full_name": "", "email": "bad", "phone": "12"}
        is_valid, issues = validate_contact(contact)
        assert is_valid is False
        assert len(issues) == 3
        assert "missing_name" in issues
        assert "invalid_email_format" in issues
        assert "phone_too_short" in issues


# ── dedup_contacts ──────────────────────────────────────────────────


class TestDedupContacts:
    """Tests for deduplication by email within a site."""

    def test_no_duplicates(self, db_session: Session, test_customer_site):
        c1 = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Alice",
            email="alice@example.com",
            is_active=True,
        )
        c2 = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Bob",
            email="bob@example.com",
            is_active=True,
        )
        db_session.add_all([c1, c2])
        db_session.commit()

        merged = dedup_contacts(db_session, test_customer_site.id)
        assert merged == 0

    def test_merge_duplicate_emails(self, db_session: Session, test_customer_site):
        c1 = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Alice",
            email="alice@example.com",
            phone=None,
            title=None,
            is_active=True,
            created_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
        c2 = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Alice Smith",
            email="Alice@example.com",  # same email, different case
            phone="+1-555-0001",
            phone_verified=True,
            title="Manager",
            linkedin_url="https://linkedin.com/in/alice",
            contact_role="buyer",
            is_active=True,
            created_at=datetime(2024, 2, 1, tzinfo=UTC),
        )
        db_session.add_all([c1, c2])
        db_session.commit()

        merged = dedup_contacts(db_session, test_customer_site.id)
        assert merged == 1

        db_session.refresh(c1)
        db_session.refresh(c2)

        # Primary (c1) got merged fields from c2
        assert c1.is_active is True
        assert c1.phone == "+1-555-0001"
        assert c1.phone_verified is True
        assert c1.title == "Manager"
        assert c1.linkedin_url == "https://linkedin.com/in/alice"
        assert c1.contact_role == "buyer"
        # Duplicate deactivated
        assert c2.is_active is False

    def test_merge_preserves_existing_fields(self, db_session: Session, test_customer_site):
        """Primary already has phone/title/linkedin/role — the merge must NOT overwrite
        them.

        ``dedup_contacts`` normalizes emails with ``.lower().strip()``, so two rows whose
        emails differ ONLY by case/whitespace collide as duplicates under dedup while
        staying byte-distinct to the ``uq_site_contacts_site_email`` UNIQUE index — that
        is how a real (pre-guard / mixed-case) duplicate arises. Here the primary (created
        first) is fully populated and the later duplicate carries DIFFERENT values, so the
        ``if <field> and not primary.<field>`` guards must leave every primary field intact.
        """
        primary = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Frank Primary",
            email="frank@example.com",
            phone="+1-555-PRIMARY",
            phone_verified=True,
            title="Director",
            linkedin_url="https://linkedin.com/in/frank-primary",
            contact_role="approver",
            is_active=True,
            created_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
        duplicate = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Frank Dupe",
            email="Frank@Example.com",  # same email normalized; byte-distinct → dodges UNIQUE
            phone="+1-555-DUPE",
            phone_verified=False,
            title="Manager",
            linkedin_url="https://linkedin.com/in/frank-dupe",
            contact_role="buyer",
            is_active=True,
            created_at=datetime(2024, 2, 1, tzinfo=UTC),
        )
        db_session.add_all([primary, duplicate])
        db_session.commit()

        merged = dedup_contacts(db_session, test_customer_site.id)
        assert merged == 1

        db_session.refresh(primary)
        db_session.refresh(duplicate)

        # Primary keeps ALL of its own populated fields (nothing pulled from the duplicate).
        assert primary.phone == "+1-555-PRIMARY"
        assert primary.phone_verified is True
        assert primary.title == "Director"
        assert primary.linkedin_url == "https://linkedin.com/in/frank-primary"
        assert primary.contact_role == "approver"
        # The duplicate is deactivated.
        assert duplicate.is_active is False

    def test_contacts_with_no_email_skipped(self, db_session: Session, test_customer_site):
        c1 = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="No Email 1",
            email=None,
            is_active=True,
        )
        c2 = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="No Email 2",
            email="",
            is_active=True,
        )
        db_session.add_all([c1, c2])
        db_session.commit()

        merged = dedup_contacts(db_session, test_customer_site.id)
        assert merged == 0

    def test_inactive_contacts_excluded(self, db_session: Session, test_customer_site):
        """An INACTIVE duplicate is excluded from dedup — not merged, not touched.

        ``dedup_contacts`` only loads ``is_active=True`` rows, so an inactive contact
        sharing an active contact's (normalized) email must be ignored entirely: no
        merge counted, and none of its fields leak into the active primary. The two
        rows use case-differing emails so both persist past the byte-exact UNIQUE index
        while normalizing equal under dedup's ``.lower().strip()``.
        """
        active = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Eve Active",
            email="eve@example.com",
            phone=None,
            title=None,
            is_active=True,
            created_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
        inactive = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Eve Inactive",
            email="EVE@example.com",  # same email normalized; byte-distinct → dodges UNIQUE
            phone="+1-555-GHOST",
            title="Ghost",
            is_active=False,
            created_at=datetime(2024, 2, 1, tzinfo=UTC),
        )
        db_session.add_all([active, inactive])
        db_session.commit()

        merged = dedup_contacts(db_session, test_customer_site.id)
        assert merged == 0  # inactive dup never enters the merge

        db_session.refresh(active)
        db_session.refresh(inactive)
        # The inactive contact's data was NOT merged into the active primary.
        assert active.phone is None
        assert active.title is None
        # The inactive contact is left untouched.
        assert inactive.is_active is False

    def test_empty_site(self, db_session: Session, test_customer_site):
        merged = dedup_contacts(db_session, test_customer_site.id)
        assert merged == 0


# ── score_contact_completeness ──────────────────────────────────────


class TestScoreContactCompleteness:
    """Tests for the 0-100 completeness scorer."""

    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            # 25 email + 20 name + 15 phone + 5 phone_verified + 15 title + 10 linkedin + 10 email_verified = 100
            (
                {
                    "full_name": "Alice Smith",
                    "email": "alice@example.com",
                    "email_verified": True,
                    "phone": "+1-555-0001",
                    "phone_verified": True,
                    "title": "VP Purchasing",
                    "linkedin_url": "https://linkedin.com/in/alice",
                },
                100,
            ),
            ({"full_name": "Unknown", "email": None}, 0),
            ({"full_name": "Unknown", "email": "x@y.com"}, 25),  # email only
            ({"full_name": "Alice", "email": "alice@test.com"}, 45),  # 25 + 20
            # 25 email + 20 name + 15 phone = 60
            ({"full_name": "Alice", "email": "a@b.com", "phone": "+1-555", "phone_verified": False}, 60),
        ],
        ids=["fully_complete", "empty", "email_only", "name_and_email", "phone_without_verified"],
    )
    def test_score(self, db_session: Session, test_customer_site, fields, expected):
        c = SiteContact(customer_site_id=test_customer_site.id, **fields)
        db_session.add(c)
        db_session.flush()

        assert score_contact_completeness(c) == expected

    def test_score_capped_at_100(self, db_session: Session, test_customer_site):
        """Ensure score never exceeds 100 even with all fields."""
        c = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Alice Smith",
            email="alice@example.com",
            email_verified=True,
            phone="+1-555-0001",
            phone_verified=True,
            title="VP",
            linkedin_url="https://linkedin.com/in/alice",
        )
        db_session.add(c)
        db_session.flush()

        score = score_contact_completeness(c)
        assert score <= 100


# ── flag_stale_contacts ─────────────────────────────────────────────


class TestFlagStaleContacts:
    """Tests for the stale contact flagger."""

    def test_flag_never_enriched(self, db_session: Session, test_customer_site):
        c = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Old Alice",
            email="old@example.com",
            is_active=True,
            needs_refresh=False,
            last_enriched_at=None,
        )
        db_session.add(c)
        db_session.commit()

        count = flag_stale_contacts(db_session)
        assert count == 1
        db_session.refresh(c)
        assert c.needs_refresh is True

    def test_flag_enriched_long_ago(self, db_session: Session, test_customer_site):
        c = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Old Bob",
            email="oldbob@example.com",
            is_active=True,
            needs_refresh=False,
            last_enriched_at=datetime.now(UTC) - timedelta(days=200),
        )
        db_session.add(c)
        db_session.commit()

        count = flag_stale_contacts(db_session)
        assert count == 1

    def test_recently_enriched_not_flagged(self, db_session: Session, test_customer_site):
        c = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Fresh Carol",
            email="carol@example.com",
            is_active=True,
            needs_refresh=False,
            last_enriched_at=datetime.now(UTC) - timedelta(days=10),
        )
        db_session.add(c)
        db_session.commit()

        count = flag_stale_contacts(db_session)
        assert count == 0

    def test_already_needs_refresh_not_counted(self, db_session: Session, test_customer_site):
        c = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Flagged Dave",
            email="dave@example.com",
            is_active=True,
            needs_refresh=True,
            last_enriched_at=None,
        )
        db_session.add(c)
        db_session.commit()

        count = flag_stale_contacts(db_session)
        assert count == 0

    def test_inactive_contacts_not_flagged(self, db_session: Session, test_customer_site):
        c = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Inactive Eve",
            email="eve@example.com",
            is_active=False,
            needs_refresh=False,
            last_enriched_at=None,
        )
        db_session.add(c)
        db_session.commit()

        count = flag_stale_contacts(db_session)
        assert count == 0

    def test_custom_stale_days(self, db_session: Session, test_customer_site):
        c = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Custom Frank",
            email="frank@example.com",
            is_active=True,
            needs_refresh=False,
            last_enriched_at=datetime.now(UTC) - timedelta(days=50),
        )
        db_session.add(c)
        db_session.commit()

        # 30 day threshold — should flag
        assert flag_stale_contacts(db_session, stale_days=30) == 1

    def test_no_contacts(self, db_session: Session):
        count = flag_stale_contacts(db_session)
        assert count == 0


# ── compute_enrichment_status ───────────────────────────────────────


class TestComputeEnrichmentStatus:
    """Tests for company-level enrichment status."""

    def test_no_sites_returns_missing(self, db_session: Session, test_company):
        # test_company has a site from the fixture; use a fresh company without sites
        co = Company(name="Empty Co", is_active=True)
        db_session.add(co)
        db_session.commit()
        status = compute_enrichment_status(db_session, co.id)
        assert status == "missing"

    def test_sites_with_no_contacts_returns_missing(self, db_session: Session, test_company, test_customer_site):
        status = compute_enrichment_status(db_session, test_company.id)
        assert status == "missing"

    def test_partial_contacts(self, db_session: Session, test_company, test_customer_site):
        # Add 1 contact (below default target of 5)
        c = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Alice",
            email="alice@acme.com",
            is_active=True,
            needs_refresh=False,
        )
        db_session.add(c)
        db_session.commit()

        status = compute_enrichment_status(db_session, test_company.id)
        assert status == "partial"

    def test_complete_status(self, db_session: Session, test_company, test_customer_site):
        # Need >= 5 contacts with >= 3 verified (target/2 rounded)
        for i in range(6):
            c = SiteContact(
                customer_site_id=test_customer_site.id,
                full_name=f"Person {i}",
                email=f"p{i}@acme.com",
                is_active=True,
                needs_refresh=False,
                email_verified=i < 3,  # 3 verified
            )
            db_session.add(c)
        db_session.commit()

        status = compute_enrichment_status(db_session, test_company.id)
        assert status == "complete"

    def test_stale_when_majority_needs_refresh(self, db_session: Session, test_company, test_customer_site):
        # 3 contacts, 2 need refresh (>50%)
        for i in range(3):
            c = SiteContact(
                customer_site_id=test_customer_site.id,
                full_name=f"Stale {i}",
                email=f"stale{i}@acme.com",
                is_active=True,
                needs_refresh=i < 2,  # 2 out of 3 stale
            )
            db_session.add(c)
        db_session.commit()

        status = compute_enrichment_status(db_session, test_company.id)
        assert status == "stale"

    def test_not_stale_when_minority_needs_refresh(self, db_session: Session, test_company, test_customer_site):
        for i in range(4):
            c = SiteContact(
                customer_site_id=test_customer_site.id,
                full_name=f"Fresh {i}",
                email=f"fresh{i}@acme.com",
                is_active=True,
                needs_refresh=i < 1,  # only 1 out of 4 stale
            )
            db_session.add(c)
        db_session.commit()

        status = compute_enrichment_status(db_session, test_company.id)
        assert status == "partial"  # not stale, but partial (not enough verified)


# ── update_company_enrichment_status ────────────────────────────────


class TestUpdateCompanyEnrichmentStatus:
    """Tests for persisting enrichment status."""

    def test_updates_company_status(self, db_session: Session, test_company, test_customer_site):
        c = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Alice",
            email="alice@acme.com",
            is_active=True,
        )
        db_session.add(c)
        db_session.commit()

        status = update_company_enrichment_status(db_session, test_company.id)
        assert status == "partial"
        db_session.refresh(test_company)
        assert test_company.customer_enrichment_status == "partial"

    def test_nonexistent_company(self, db_session: Session):
        status = update_company_enrichment_status(db_session, 99999)
        assert status == "missing"
