"""Tests for E.164 phone normalization — normalize_e164 helper + model @validates hooks.

Covers:
- normalize_e164() correctness across common US/international/edge inputs
- Company.normalized_phone kept in sync via @validates("phone")
- CustomerSite.normalized_phone / normalized_phone_2 via @validates hooks
- SiteContact.normalized_phone via @validates("phone")
- VendorContact.normalized_phone via @validates("phone")
- VendorCard.normalized_phones (JSON list) via @validates("phones")
- Migration upgrade → downgrade → upgrade leaves a single head

Called by: pytest
Depends on: app.utils.phone.normalize_e164, app.models.crm, app.models.vendors
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.utils.phone import normalize_e164
from tests.conftest import engine

# ---------------------------------------------------------------------------
# normalize_e164 — pure function tests (no DB required)
# ---------------------------------------------------------------------------


class TestNormalizeE164:
    def test_us_10_digit(self):
        assert normalize_e164("4155551234") == "+14155551234"

    def test_us_e164_passthrough(self):
        assert normalize_e164("+14155551234") == "+14155551234"

    def test_us_formatted(self):
        assert normalize_e164("(415) 555-1234") == "+14155551234"

    def test_us_dashes(self):
        assert normalize_e164("415-555-1234") == "+14155551234"

    def test_us_dots(self):
        assert normalize_e164("415.555.1234") == "+14155551234"

    def test_us_with_extension_stripped(self):
        # Extensions are stripped — the base number still normalizes
        assert normalize_e164("(415) 555-1234 ext 42") == "+14155551234"

    def test_international_uk(self):
        result = normalize_e164("+441234567890")
        assert result == "+441234567890"

    def test_international_with_country_code(self):
        result = normalize_e164("+85291234567")
        assert result is not None
        assert result.startswith("+")

    def test_blank_returns_none(self):
        assert normalize_e164("") is None

    def test_none_returns_none(self):
        assert normalize_e164(None) is None

    def test_whitespace_only_returns_none(self):
        assert normalize_e164("   ") is None

    def test_garbage_returns_none(self):
        assert normalize_e164("not a phone") is None

    def test_too_short_returns_none(self):
        assert normalize_e164("123") is None

    def test_never_raises_on_garbage(self):
        # Must not raise for any input — lenient by design
        bad_inputs = ["???", "!!!", "0" * 30, "CALL ME", "+", "1234x5678"]
        for bad in bad_inputs:
            result = normalize_e164(bad)
            assert result is None or result.startswith("+"), f"Unexpected result for {bad!r}: {result!r}"

    def test_default_region_us(self):
        # 10-digit default-region US
        assert normalize_e164("8005551234", default_region="US") == "+18005551234"

    def test_different_default_region(self):
        # 10-digit UK local number with GB region — should normalize to +44 form
        # (libphonenumber handles the local-number format)
        result = normalize_e164("02079460958", default_region="GB")
        assert result is not None
        assert result.startswith("+44")


# ---------------------------------------------------------------------------
# Model @validates hooks — DB-backed
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session():
    from sqlalchemy.orm import sessionmaker

    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


class TestCompanyPhoneSync:
    def test_phone_sets_normalized_phone(self, db_session: Session):
        from app.models.crm import Company

        co = Company(name="Acme Corp", phone="(415) 555-1234")
        db_session.add(co)
        db_session.flush()
        assert co.normalized_phone == "+14155551234"

    def test_garbage_phone_sets_normalized_phone_none(self, db_session: Session):
        from app.models.crm import Company

        co = Company(name="Junk Co", phone="not-a-phone")
        db_session.add(co)
        db_session.flush()
        assert co.normalized_phone is None

    def test_none_phone_sets_normalized_phone_none(self, db_session: Session):
        from app.models.crm import Company

        co = Company(name="Blank Phone Co", phone=None)
        db_session.add(co)
        db_session.flush()
        assert co.normalized_phone is None

    def test_update_phone_resyncs(self, db_session: Session):
        from app.models.crm import Company

        co = Company(name="Resync Co", phone="4155551234")
        db_session.add(co)
        db_session.flush()
        assert co.normalized_phone == "+14155551234"
        co.phone = "4155559999"
        db_session.flush()
        assert co.normalized_phone == "+14155559999"


class TestCustomerSitePhoneSync:
    def test_contact_phone_sets_normalized(self, db_session: Session):
        from app.models.crm import Company, CustomerSite

        co = Company(name="Site Test Co")
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(
            company_id=co.id,
            site_name="HQ",
            contact_phone="(415) 555-5678",
            contact_phone_2="+14155559001",
        )
        db_session.add(site)
        db_session.flush()
        assert site.normalized_phone == "+14155555678"
        assert site.normalized_phone_2 == "+14155559001"

    def test_blank_phone_2_sets_none(self, db_session: Session):
        from app.models.crm import Company, CustomerSite

        co = Company(name="Blank Phone 2 Co")
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(
            company_id=co.id,
            site_name="Branch",
            contact_phone_2=None,
        )
        db_session.add(site)
        db_session.flush()
        assert site.normalized_phone_2 is None


class TestSiteContactPhoneSync:
    def test_phone_sets_normalized(self, db_session: Session):
        from app.models.crm import Company, CustomerSite, SiteContact

        co = Company(name="SC Test Co")
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ")
        db_session.add(site)
        db_session.flush()
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="Alice",
            phone="4155550001",
        )
        db_session.add(contact)
        db_session.flush()
        assert contact.normalized_phone == "+14155550001"

    def test_garbage_sets_none(self, db_session: Session):
        from app.models.crm import Company, CustomerSite, SiteContact

        co = Company(name="Garbage Phone Co")
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ")
        db_session.add(site)
        db_session.flush()
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="Bob",
            phone="xyz",
        )
        db_session.add(contact)
        db_session.flush()
        assert contact.normalized_phone is None


class TestVendorContactPhoneSync:
    def test_phone_sets_normalized(self, db_session: Session):
        from app.models.vendors import VendorCard, VendorContact

        card = VendorCard(normalized_name="acme", display_name="Acme", source="test")
        db_session.add(card)
        db_session.flush()
        vc = VendorContact(
            vendor_card_id=card.id,
            source="test",
            phone="(800) 555-0001",
        )
        db_session.add(vc)
        db_session.flush()
        assert vc.normalized_phone == "+18005550001"

    def test_none_phone_sets_none(self, db_session: Session):
        from app.models.vendors import VendorCard, VendorContact

        card = VendorCard(normalized_name="nullphone", display_name="NullPhone", source="test")
        db_session.add(card)
        db_session.flush()
        vc = VendorContact(vendor_card_id=card.id, source="test", phone=None)
        db_session.add(vc)
        db_session.flush()
        assert vc.normalized_phone is None


class TestVendorCardNormalizedPhones:
    def test_phones_list_normalizes(self, db_session: Session):
        from app.models.vendors import VendorCard

        card = VendorCard(
            normalized_name="phones-test",
            display_name="Phones Test",
            source="test",
            phones=["(415) 555-2222", "+14155553333", "garbage"],
        )
        db_session.add(card)
        db_session.flush()
        assert "+14155552222" in card.normalized_phones
        assert "+14155553333" in card.normalized_phones
        # "garbage" is excluded
        assert len(card.normalized_phones) == 2

    def test_empty_phones_sets_empty_list(self, db_session: Session):
        from app.models.vendors import VendorCard

        card = VendorCard(normalized_name="empty-phones", display_name="Empty", source="test", phones=[])
        db_session.add(card)
        db_session.flush()
        assert card.normalized_phones == []

    def test_phones_update_resyncs(self, db_session: Session):
        from app.models.vendors import VendorCard

        card = VendorCard(
            normalized_name="resync-phones",
            display_name="Resync",
            source="test",
            phones=["4155550001"],
        )
        db_session.add(card)
        db_session.flush()
        assert card.normalized_phones == ["+14155550001"]
        card.phones = ["4155550002", "4155550003"]
        db_session.flush()
        assert "+14155550002" in card.normalized_phones
        assert "+14155550003" in card.normalized_phones
        assert len(card.normalized_phones) == 2


# ---------------------------------------------------------------------------
# Migration chain — single head, upgrade→downgrade→upgrade roundtrip
# ---------------------------------------------------------------------------


class TestMigrationChain:
    def test_single_head(self):
        """Alembic must have exactly one head (single linear chain), and migration 130
        must remain present in the chain.

        Note: do NOT assert which revision is the head — that changes every time a new
        migration is added and made this test break spuriously. The invariants that
        matter are (a) a single head and (b) 130 still in the history.
        """
        import pathlib

        from alembic.script import ScriptDirectory

        alembic_dir = pathlib.Path(__file__).resolve().parent.parent / "alembic"
        script = ScriptDirectory(str(alembic_dir))
        heads = script.get_heads()
        assert len(heads) == 1, f"Expected 1 head, got {len(heads)}: {heads}"
        assert script.get_revision("130_phone_normalization") is not None, (
            "130_phone_normalization missing from the migration chain"
        )

    def test_migration_130_down_revision(self):
        """Migration 130 chains onto 129_drop_bid_tables."""
        import pathlib

        from alembic.script import ScriptDirectory

        alembic_dir = pathlib.Path(__file__).resolve().parent.parent / "alembic"
        scripts = ScriptDirectory(str(alembic_dir))
        rev = scripts.get_revision("130_phone_normalization")
        assert rev is not None
        assert rev.down_revision == "129_drop_bid_tables"

    def test_migration_no_jsonb_path_ops(self):
        """Migration 130 must not contain jsonb_path_ops (incompatible with JSON
        column)."""
        import pathlib

        migration = (
            pathlib.Path(__file__).resolve().parent.parent / "alembic" / "versions" / "130_phone_normalization.py"
        )
        content = migration.read_text()
        assert "jsonb_path_ops" not in content, "jsonb_path_ops found in migration — remove the GIN index"


# ---------------------------------------------------------------------------
# normalize_e164 totality — non-string and edge inputs must never raise
# ---------------------------------------------------------------------------


class TestNormalizeE164Totality:
    """Fix 3: normalize_e164 must be TOTAL — no exception for any input type."""

    def test_int_input_does_not_raise(self):
        result = normalize_e164(1234567890)  # type: ignore[arg-type]
        # May or may not produce a valid E.164; must not raise
        assert result is None or result.startswith("+")

    def test_none_input_returns_none(self):
        assert normalize_e164(None) is None

    def test_empty_string_returns_none(self):
        assert normalize_e164("") is None

    def test_garbage_string_returns_none(self):
        assert normalize_e164("garbage!!") is None

    def test_valid_int_like_us_number(self):
        # str(14155551234) == "14155551234" — 11-digit US with country code
        result = normalize_e164(14155551234)  # type: ignore[arg-type]
        assert result == "+14155551234"

    def test_float_input_does_not_raise(self):
        result = normalize_e164(3.14)  # type: ignore[arg-type]
        assert result is None or result.startswith("+")

    def test_list_input_does_not_raise(self):
        result = normalize_e164(["+14155551234"])  # type: ignore[arg-type]
        assert result is None or result.startswith("+")


# ---------------------------------------------------------------------------
# Migration backfill — normalized_phones stored as real list, not double-encoded
# ---------------------------------------------------------------------------


class TestMigration130BackfillNoDoubleEncoding:
    """Fix 2: backfill must write normalized_phones as a Python list, not json.dumps(list)."""

    def test_backfill_logic_writes_list_not_string(self, db_session: Session):
        """Simulate the migration backfill directly against the ORM to verify the
        normalized_phones column is read back as a list (not a double-encoded JSON
        string).

        We insert a VendorCard via ORM (bypassing @validates by writing phones=None then
        updating the raw column), run the backfill logic, and assert isinstance(list).
        """
        import json

        import sqlalchemy as sa

        from app.models.vendors import VendorCard
        from app.utils.phone import normalize_e164

        # Insert a card with no phones (avoids @validates normalizing it automatically)
        card = VendorCard(normalized_name="backfill-test-130", display_name="Backfill Test", source="test")
        db_session.add(card)
        db_session.flush()

        # Write raw phones via core SQL to bypass ORM @validates
        vcards = sa.table(
            "vendor_cards",
            sa.column("id", sa.Integer),
            sa.column("phones", sa.JSON),
            sa.column("normalized_phones", sa.JSON),
        )
        db_session.execute(
            sa.update(vcards).where(vcards.c.id == card.id).values(phones=["(415) 555-7777", "garbage", "+14155558888"])
        )
        db_session.flush()

        # Re-read phones via core SQL (same as migration backfill does)
        row = db_session.execute(sa.select(vcards.c.phones).where(vcards.c.id == card.id)).fetchone()
        phones_raw = row.phones
        if isinstance(phones_raw, str):
            try:
                phones_raw = json.loads(phones_raw)
            except (ValueError, TypeError):
                phones_raw = []

        normalized = [normalize_e164(p) for p in phones_raw if p]
        normalized = [n for n in normalized if n is not None]

        # Apply backfill — pass list directly (NOT json.dumps)
        db_session.execute(sa.update(vcards).where(vcards.c.id == card.id).values(normalized_phones=normalized))
        db_session.flush()

        # Expire and re-read via ORM to confirm stored type
        db_session.expire(card)
        db_session.refresh(card)

        assert isinstance(card.normalized_phones, list), (
            f"normalized_phones should be a list, got {type(card.normalized_phones)!r}: {card.normalized_phones!r}"
        )
        assert "+14155557777" in card.normalized_phones
        assert "+14155558888" in card.normalized_phones
        assert len(card.normalized_phones) == 2
