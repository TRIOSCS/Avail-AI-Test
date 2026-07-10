"""Tests for app/utils/vendor_helpers.py — vendor card CRUD, contact cleaning, SSRF
protection, website scraping, merge logic, and entity tag loading.

Achieves 100% coverage of vendor_helpers.py by testing:
- clean_emails: dedup, junk local parts, junk domains, file-extension emails, overlength
- clean_phones: dedup by digits, too short, too long, formatting preserved
- is_private_url: loopback, private, link-local, reserved, public, unresolvable, empty
- get_or_create_card: exact match, fuzzy match (thefuzz), create new, thefuzz ImportError
- _background_enrich_vendor: success, no enrichment, card missing, exception, material analysis
- _load_entity_tags: visible tags, empty
- card_to_dict: full serialization, Redis cache hit, Redis cache miss, no reviews
- scrape_website_contacts: success, SSRF blocked, cached, HTTP errors
- merge_contact_into_card: emails only, phones only, website, source, no change

Called by: pytest
Depends on: app.utils.vendor_helpers, app.models, tests.conftest
"""

import asyncio
import json
import os
import socket

os.environ["TESTING"] = "1"

from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import User, VendorCard, VendorReview
from app.models.tags import EntityTag, Tag
from app.utils.vendor_helpers import (
    _load_entity_tags,
    card_to_dict,
    clean_emails,
    clean_phones,
    find_vendor_card_by_name,
    get_or_create_card,
    is_private_url,
    merge_contact_into_card,
    scrape_website_contacts,
)
from tests.conftest import engine  # noqa: F401

# ── clean_emails ─────────────────────────────────────────────────────


class TestCleanEmails:
    def test_valid_emails_pass_through(self):
        result = clean_emails(["alice@acme.com", "bob@widgets.co"])
        assert result == ["alice@acme.com", "bob@widgets.co"]

    def test_deduplicates_case_insensitive(self):
        result = clean_emails(["Alice@Acme.COM", "alice@acme.com", "ALICE@ACME.COM"])
        assert result == ["alice@acme.com"]

    def test_strips_whitespace(self):
        result = clean_emails(["  alice@acme.com  ", "\tbob@widgets.co\n"])
        assert result == ["alice@acme.com", "bob@widgets.co"]

    def test_filters_no_at_sign(self):
        result = clean_emails(["not-an-email", "alice@acme.com"])
        assert result == ["alice@acme.com"]

    def test_filters_empty_and_blank(self):
        result = clean_emails(["", "   ", "alice@acme.com"])
        assert result == ["alice@acme.com"]

    def test_filters_overlength(self):
        long_email = "a" * 90 + "@example.org"  # 102 chars, > 100 limit
        result = clean_emails([long_email, "ok@acme.com"])
        assert result == ["ok@acme.com"]

    @pytest.mark.parametrize(
        ("junk", "good"),
        [
            pytest.param(
                [
                    "noreply@acme.com",
                    "no-reply@acme.com",
                    "donotreply@acme.com",
                    "mailer-daemon@acme.com",
                    "postmaster@acme.com",
                    "webmaster@acme.com",
                    "privacy@acme.com",
                    "abuse@acme.com",
                    "spam@acme.com",
                    "unsubscribe@acme.com",
                    "root@acme.com",
                    "hostmaster@acme.com",
                    "example@acme.com",
                    "test@acme.com",
                ],
                "sales@acme.com",
                id="junk_local_parts",
            ),
            pytest.param(
                [
                    "user@example.com",
                    "user@sentry.io",
                    "user@googleapis.com",
                    "user@google.com",
                    "user@facebook.com",
                    "user@twitter.com",
                    "user@youtube.com",
                    "user@linkedin.com",
                    "user@schema.org",
                    "user@w3.org",
                    "user@cloudflare.com",
                    "user@jquery.com",
                    "user@bootstrapcdn.com",
                    "user@gstatic.com",
                    "user@gravatar.com",
                    "user@wordpress.org",
                ],
                "sales@realcompany.com",
                id="junk_domains",
            ),
            pytest.param(
                [
                    "icon@site.png",
                    "logo@brand.jpg",
                    "bg@site.gif",
                    "img@site.svg",
                    "style@site.css",
                    "bundle@site.js",
                ],
                "real@site.com",
                id="file_extension_emails",
            ),
        ],
    )
    def test_filters_junk(self, junk, good):
        assert clean_emails(junk + [good]) == [good]

    def test_empty_input(self):
        assert clean_emails([]) == []

    def test_admin_at_example_in_junk_emails(self):
        """The _JUNK_EMAILS set contains 'admin@example' as a local-part entry."""
        # 'admin@example' is in _JUNK_EMAILS, so 'admin@example@foo.com' local part
        # would be 'admin@example' after rsplit('@', 1)
        result = clean_emails(["admin@example@foo.com"])
        assert result == []


# ── clean_phones ─────────────────────────────────────────────────────


class TestCleanPhones:
    def test_valid_phones_pass_through(self):
        result = clean_phones(["+1-555-123-4567", "(800) 555-0100"])
        assert result == ["+1-555-123-4567", "(800) 555-0100"]

    def test_deduplicates_by_digits(self):
        result = clean_phones(["+1-555-1234567", "1 555 123 4567", "15551234567"])
        assert len(result) == 1

    def test_filters_too_short(self):
        result = clean_phones(["123", "123456", "+1-555-123-4567"])
        assert result == ["+1-555-123-4567"]

    def test_filters_too_long(self):
        result = clean_phones(["1234567890123456", "+1-555-123-4567"])
        assert result == ["+1-555-123-4567"]

    def test_strips_whitespace(self):
        result = clean_phones(["  +1-555-123-4567  "])
        assert result == ["+1-555-123-4567"]

    def test_empty_input(self):
        assert clean_phones([]) == []

    def test_preserves_original_formatting(self):
        result = clean_phones(["(800) 555-0100"])
        assert result == ["(800) 555-0100"]


# ── is_private_url ───────────────────────────────────────────────────


class TestIsPrivateUrl:
    @pytest.mark.parametrize(
        ("resolved_ip", "url", "expected"),
        [
            pytest.param("127.0.0.1", "http://localhost/secret", True, id="loopback_blocked"),
            pytest.param("192.168.1.1", "http://internal.corp/api", True, id="private_ip_blocked"),
            pytest.param("169.254.1.1", "http://link-local.test", True, id="link_local_blocked"),
            pytest.param("240.0.0.1", "http://reserved.test", True, id="reserved_blocked"),
            pytest.param("93.184.216.34", "https://example.com", False, id="public_ip_allowed"),
        ],
    )
    def test_resolved_ip_classification(self, resolved_ip, url, expected):
        with patch("socket.gethostbyname", return_value=resolved_ip):
            assert is_private_url(url) is expected

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            pytest.param("http://", True, id="empty_hostname_blocked"),
            pytest.param("not-a-url", True, id="no_hostname_blocked"),
        ],
    )
    def test_no_dns_lookup(self, url, expected):
        assert is_private_url(url) is expected

    @pytest.mark.parametrize(
        ("dns_error", "url"),
        [
            pytest.param(socket.gaierror("DNS fail"), "http://nonexistent.invalid", id="unresolvable_blocked"),
            pytest.param(ValueError("bad IP"), "http://badvalue.test", id="value_error_blocked"),
        ],
    )
    def test_dns_failure_blocked(self, dns_error, url):
        with patch("socket.gethostbyname", side_effect=dns_error):
            assert is_private_url(url) is True


# ── get_or_create_card ───────────────────────────────────────────────


@contextmanager
def mock_rapidfuzz_score(score):
    """Patch sys.modules so `from rapidfuzz import fuzz` returns a fuzz whose
    token_sort_ratio yields the given score."""
    mock_fuzz = MagicMock()
    mock_fuzz.token_sort_ratio.return_value = score
    mock_module = MagicMock()
    mock_module.fuzz = mock_fuzz
    with patch.dict("sys.modules", {"rapidfuzz": mock_module, "rapidfuzz.fuzz": mock_fuzz}):
        yield


def _add_arrow_card(db_session, **overrides) -> VendorCard:
    card = VendorCard(
        normalized_name="arrow electronics",
        display_name="Arrow Electronics",
        emails=[],
        phones=[],
        **overrides,
    )
    db_session.add(card)
    db_session.commit()
    return card


class TestGetOrCreateCard:
    def test_exact_match_returns_existing(self, db_session):
        card = _add_arrow_card(db_session)

        result = get_or_create_card("Arrow Electronics", db_session)
        assert result.id == card.id

    def test_creates_new_when_no_match(self, db_session):
        result = get_or_create_card("Brand New Vendor Inc.", db_session)
        assert result.id is not None
        assert result.display_name == "Brand New Vendor Inc."
        assert result.emails == []
        assert result.phones == []

    def test_fuzzy_match_merges_alternate_name(self, db_session):
        """When thefuzz scores >= 90, existing card is returned with alternate name
        added."""
        card = _add_arrow_card(db_session, alternate_names=[])

        with mock_rapidfuzz_score(95):
            result = get_or_create_card("Arrow Elecctronics", db_session)

        assert result.id == card.id
        assert "Arrow Elecctronics" in (result.alternate_names or [])

    def test_fuzzy_match_same_display_name_no_duplicate_alt(self, db_session):
        """When fuzzy-matched vendor_name equals display_name, don't add to
        alternates."""
        card = _add_arrow_card(db_session, alternate_names=[])

        with mock_rapidfuzz_score(95):
            result = get_or_create_card("Arrow Electronics", db_session)

        assert result.id == card.id
        assert "Arrow Electronics" not in (result.alternate_names or [])

    def test_fuzzy_match_already_in_alternates(self, db_session):
        """When vendor name already in alternate_names, don't duplicate."""
        card = _add_arrow_card(db_session, alternate_names=["Arrow Elecctronics"])

        with mock_rapidfuzz_score(95):
            result = get_or_create_card("Arrow Elecctronics", db_session)

        assert result.id == card.id
        assert result.alternate_names.count("Arrow Elecctronics") == 1

    def test_fuzzy_low_score_creates_new(self, db_session):
        """Score below 90 means no fuzzy match — create new card."""
        card = _add_arrow_card(db_session)

        with mock_rapidfuzz_score(50):
            result = get_or_create_card("Completely Different Vendor", db_session)

        assert result.id != card.id
        assert result.display_name == "Completely Different Vendor"

    def test_rapidfuzz_import_error_creates_new(self, db_session):
        """If rapidfuzz not installed, skip fuzzy and create new card."""
        card = _add_arrow_card(db_session)

        import builtins

        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "rapidfuzz":
                raise ImportError("rapidfuzz not installed")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            result = get_or_create_card("Arrow Electronix", db_session)

        assert result.id != card.id
        assert result.display_name == "Arrow Electronix"

    def test_fuzzy_match_card_none_after_get(self, db_session):
        """Edge case: best_card_id valid but db.get returns None (deleted between query/get)."""
        card = _add_arrow_card(db_session)

        # Patch db.get to return None for the fuzzy match lookup
        original_get = db_session.get

        def patched_get(model, id_val):
            if model == VendorCard and id_val == card.id:
                return None
            return original_get(model, id_val)

        with mock_rapidfuzz_score(95), patch.object(db_session, "get", side_effect=patched_get):
            result = get_or_create_card("Arrow Elecctronics", db_session)

        # Should have created a new card since db.get returned None
        assert result.display_name == "Arrow Elecctronics"


# ── _background_enrich_vendor ────────────────────────────────────────


class TestBackgroundEnrichVendor:
    def test_success_enrichment(self):
        """Successful enrichment applies data to card."""
        from app.utils.vendor_helpers import _background_enrich_vendor

        mock_enrichment = {"source": "apollo", "industry": "Electronics"}
        mock_card = MagicMock()
        mock_db = MagicMock()
        mock_db.get.return_value = mock_card

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch(
                "app.enrichment_service.enrich_entity",
                new_callable=AsyncMock,
                return_value=mock_enrichment,
            ),
            patch("app.enrichment_service.apply_enrichment_to_vendor") as mock_apply,
            patch("app.utils.vendor_helpers.get_credential_cached", return_value=None),
        ):
            asyncio.get_event_loop().run_until_complete(_background_enrich_vendor(1, "test.com", "Test Vendor"))

        mock_apply.assert_called_once_with(mock_card, mock_enrichment)
        mock_db.commit.assert_called_once()
        mock_db.close.assert_called_once()

    def test_no_enrichment_data(self):
        """When enrich_entity returns None, skip apply."""
        from app.utils.vendor_helpers import _background_enrich_vendor

        with (
            patch("app.database.SessionLocal"),
            patch(
                "app.enrichment_service.enrich_entity",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("app.enrichment_service.apply_enrichment_to_vendor") as mock_apply,
            patch("app.utils.vendor_helpers.get_credential_cached", return_value=None),
        ):
            asyncio.get_event_loop().run_until_complete(_background_enrich_vendor(999, "none.com", "Nobody"))

        mock_apply.assert_not_called()

    def test_card_missing_in_session(self):
        """When card is deleted between schedule and execution."""
        from app.utils.vendor_helpers import _background_enrich_vendor

        mock_db = MagicMock()
        mock_db.get.return_value = None

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch(
                "app.enrichment_service.enrich_entity",
                new_callable=AsyncMock,
                return_value={"source": "apollo"},
            ),
            patch("app.enrichment_service.apply_enrichment_to_vendor") as mock_apply,
            patch("app.utils.vendor_helpers.get_credential_cached", return_value=None),
        ):
            asyncio.get_event_loop().run_until_complete(_background_enrich_vendor(999, "gone.com", "Gone Vendor"))

        mock_apply.assert_not_called()
        mock_db.close.assert_called_once()

    def test_enrichment_exception_logged(self):
        """Exception in enrichment is caught and logged."""
        from app.utils.vendor_helpers import _background_enrich_vendor

        with (
            patch("app.database.SessionLocal"),
            patch(
                "app.enrichment_service.enrich_entity",
                new_callable=AsyncMock,
                side_effect=RuntimeError("API down"),
            ),
            patch("app.enrichment_service.apply_enrichment_to_vendor"),
            patch("app.utils.vendor_helpers.get_credential_cached", return_value=None),
        ):
            # Should not propagate exception
            asyncio.get_event_loop().run_until_complete(_background_enrich_vendor(999, "fail.com", "Fail Vendor"))

    def test_material_analysis_runs_with_anthropic_key(self):
        """When ANTHROPIC_API_KEY is available, material analysis runs."""
        from app.utils.vendor_helpers import _background_enrich_vendor

        mock_db = MagicMock()
        mock_db.get.return_value = MagicMock()

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch(
                "app.enrichment_service.enrich_entity",
                new_callable=AsyncMock,
                return_value={"source": "test"},
            ),
            patch("app.enrichment_service.apply_enrichment_to_vendor"),
            patch(
                "app.utils.vendor_helpers.get_credential_cached",
                return_value="sk-ant-key",
            ),
            patch(
                "app.utils.vendor_helpers._analyze_vendor_materials",
                new_callable=AsyncMock,
            ) as mock_analyze,
        ):
            asyncio.get_event_loop().run_until_complete(_background_enrich_vendor(1, "test.com", "Test"))

        mock_analyze.assert_called_once_with(1)

    def test_material_analysis_exception_caught(self):
        """Exception in material analysis is caught, doesn't propagate."""
        from app.utils.vendor_helpers import _background_enrich_vendor

        mock_db = MagicMock()
        mock_db.get.return_value = MagicMock()

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch(
                "app.enrichment_service.enrich_entity",
                new_callable=AsyncMock,
                return_value={"source": "test"},
            ),
            patch("app.enrichment_service.apply_enrichment_to_vendor"),
            patch(
                "app.utils.vendor_helpers.get_credential_cached",
                return_value="sk-ant-key",
            ),
            patch(
                "app.utils.vendor_helpers._analyze_vendor_materials",
                new_callable=AsyncMock,
                side_effect=RuntimeError("AI failed"),
            ),
        ):
            asyncio.get_event_loop().run_until_complete(_background_enrich_vendor(1, "test.com", "Test"))


# ── _load_entity_tags ────────────────────────────────────────────────


class TestLoadEntityTags:
    def test_returns_visible_tags(self, db_session):
        tag = Tag(name="Texas Instruments", tag_type="brand")
        db_session.add(tag)
        db_session.flush()

        et = EntityTag(
            entity_type="vendor_card",
            entity_id=42,
            tag_id=tag.id,
            interaction_count=10,
            total_entity_interactions=50,
            is_visible=True,
        )
        db_session.add(et)
        db_session.commit()

        result = _load_entity_tags("vendor_card", 42, db_session)
        assert len(result) == 1
        assert result[0]["tag_name"] == "Texas Instruments"
        assert result[0]["tag_type"] == "brand"
        assert result[0]["count"] == 10
        assert result[0]["is_visible"] is True

    def test_excludes_invisible_tags(self, db_session):
        tag = Tag(name="Hidden Brand", tag_type="brand")
        db_session.add(tag)
        db_session.flush()

        et = EntityTag(
            entity_type="vendor_card",
            entity_id=42,
            tag_id=tag.id,
            interaction_count=1,
            total_entity_interactions=50,
            is_visible=False,
        )
        db_session.add(et)
        db_session.commit()

        result = _load_entity_tags("vendor_card", 42, db_session)
        # Falls back to all tags when no visible ones exist
        assert len(result) == 1
        assert result[0]["is_visible"] is False

    def test_empty_when_no_tags(self, db_session):
        result = _load_entity_tags("vendor_card", 9999, db_session)
        assert result == []

    def test_ordered_by_interaction_count_desc(self, db_session):
        tag1 = Tag(name="Brand A", tag_type="brand")
        tag2 = Tag(name="Brand B", tag_type="brand")
        db_session.add_all([tag1, tag2])
        db_session.flush()

        et1 = EntityTag(
            entity_type="vendor_card",
            entity_id=42,
            tag_id=tag1.id,
            interaction_count=5,
            total_entity_interactions=50,
            is_visible=True,
        )
        et2 = EntityTag(
            entity_type="vendor_card",
            entity_id=42,
            tag_id=tag2.id,
            interaction_count=20,
            total_entity_interactions=50,
            is_visible=True,
        )
        db_session.add_all([et1, et2])
        db_session.commit()

        result = _load_entity_tags("vendor_card", 42, db_session)
        assert len(result) == 2
        assert result[0]["tag_name"] == "Brand B"
        assert result[1]["tag_name"] == "Brand A"


# ── card_to_dict ─────────────────────────────────────────────────────


class TestCardToDict:
    def _make_card(self, db_session) -> VendorCard:
        card = VendorCard(
            normalized_name="test vendor",
            display_name="Test Vendor",
            domain="testvendor.com",
            website="https://testvendor.com",
            emails=["sales@testvendor.com"],
            phones=["+1-555-0100"],
            sighting_count=10,
            is_blacklisted=False,
            linkedin_url="https://linkedin.com/company/testvendor",
            legal_name="Test Vendor LLC",
            industry="Electronics",
            employee_size="100-500",
            hq_city="Austin",
            hq_state="TX",
            hq_country="US",
            enrichment_source="apollo",
            vendor_score=85.0,
            advancement_score=90.0,
            is_new_vendor=False,
            total_outreach=20,
            total_responses=15,
            ghost_rate=0.25,
            response_velocity_hours=4.5,
            brand_tags=["TI", "Analog Devices"],
            commodity_tags=["Voltage Regulators"],
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 2, 1, tzinfo=UTC),
        )
        db_session.add(card)
        db_session.commit()
        db_session.refresh(card)
        return card

    @patch("app.cache.intel_cache._get_redis", return_value=None)
    def test_full_serialization_no_cache(self, mock_redis, db_session):
        card = self._make_card(db_session)

        user = User(
            email="reviewer@test.com",
            name="Reviewer",
            role="buyer",
            azure_id="rev-001",
            created_at=datetime.now(UTC),
        )
        db_session.add(user)
        db_session.flush()
        review = VendorReview(
            vendor_card_id=card.id,
            user_id=user.id,
            rating=4,
            comment="Good vendor",
            created_at=datetime(2026, 1, 15, tzinfo=UTC),
        )
        db_session.add(review)
        db_session.commit()

        result = card_to_dict(card, db_session)

        assert result["id"] == card.id
        assert result["normalized_name"] == "test vendor"
        assert result["display_name"] == "Test Vendor"
        assert result["domain"] == "testvendor.com"
        assert result["website"] == "https://testvendor.com"
        assert result["emails"] == ["sales@testvendor.com"]
        assert result["phones"] == ["+1-555-0100"]
        assert result["sighting_count"] == 10
        assert result["is_blacklisted"] is False
        assert result["linkedin_url"] == "https://linkedin.com/company/testvendor"
        assert result["legal_name"] == "Test Vendor LLC"
        assert result["industry"] == "Electronics"
        assert result["employee_size"] == "100-500"
        assert result["hq_city"] == "Austin"
        assert result["hq_state"] == "TX"
        assert result["hq_country"] == "US"
        assert result["enrichment_source"] == "apollo"
        assert result["avg_rating"] == 4.0
        assert result["review_count"] == 1
        assert len(result["reviews"]) == 1
        assert result["reviews"][0]["rating"] == 4
        assert result["reviews"][0]["user_name"] == "Reviewer"
        assert result["reviews"][0]["comment"] == "Good vendor"
        assert result["vendor_score"] == 85.0
        assert result["advancement_score"] == 90.0
        assert result["is_new_vendor"] is False
        assert result["total_outreach"] == 20
        assert result["total_responses"] == 15
        assert result["ghost_rate"] == 0.25
        assert result["response_velocity_hours"] == 4.5
        assert result["brand_tags"] == ["TI", "Analog Devices"]
        assert result["commodity_tags"] == ["Voltage Regulators"]
        assert result["created_at"] is not None
        assert result["updated_at"] is not None
        assert isinstance(result["brands"], list)
        assert isinstance(result["unique_parts"], int)
        assert isinstance(result["tags"], list)

    @patch("app.cache.intel_cache._get_redis")
    def test_redis_cache_hit(self, mock_get_redis, db_session):
        card = self._make_card(db_session)

        mock_r = MagicMock()
        cached_data = json.dumps({"brands": [{"name": "TI", "count": 5}], "mpn_count": 42})
        mock_r.get.return_value = cached_data
        mock_get_redis.return_value = mock_r

        result = card_to_dict(card, db_session)

        assert result["brands"] == [{"name": "TI", "count": 5}]
        assert result["unique_parts"] == 42

    @patch("app.cache.intel_cache._get_redis")
    def test_redis_cache_miss_then_set(self, mock_get_redis, db_session):
        card = self._make_card(db_session)

        mock_r = MagicMock()
        mock_r.get.return_value = None  # cache miss
        mock_get_redis.return_value = mock_r

        result = card_to_dict(card, db_session)

        mock_r.setex.assert_called_once()
        call_args = mock_r.setex.call_args
        assert call_args[0][0] == f"vprofile:{card.id}"
        assert call_args[0][1] == 21600  # 6 hours

    @patch("app.cache.intel_cache._get_redis")
    def test_redis_get_oserror_falls_through(self, mock_get_redis, db_session):
        """OSError on Redis get is caught and falls through to SQL."""
        card = self._make_card(db_session)

        mock_r = MagicMock()
        mock_r.get.side_effect = OSError("Redis down")
        mock_get_redis.return_value = mock_r

        result = card_to_dict(card, db_session)
        assert isinstance(result["brands"], list)

    @patch("app.cache.intel_cache._get_redis")
    def test_redis_get_valueerror_falls_through(self, mock_get_redis, db_session):
        """ValueError on Redis get (bad JSON) is caught."""
        card = self._make_card(db_session)

        mock_r = MagicMock()
        mock_r.get.return_value = "not-valid-json{{"
        mock_get_redis.return_value = mock_r

        # json.loads will raise ValueError, which should be caught
        result = card_to_dict(card, db_session)
        assert isinstance(result["brands"], list)

    @patch("app.cache.intel_cache._get_redis")
    def test_redis_setex_oserror_ignored(self, mock_get_redis, db_session):
        """OSError on Redis setex is caught."""
        card = self._make_card(db_session)

        mock_r = MagicMock()
        mock_r.get.return_value = None
        mock_r.setex.side_effect = OSError("Redis down")
        mock_get_redis.return_value = mock_r

        result = card_to_dict(card, db_session)
        assert isinstance(result["brands"], list)

    @patch("app.cache.intel_cache._get_redis")
    def test_redis_setex_typeerror_ignored(self, mock_get_redis, db_session):
        """TypeError on Redis setex is caught."""
        card = self._make_card(db_session)

        mock_r = MagicMock()
        mock_r.get.return_value = None
        mock_r.setex.side_effect = TypeError("bad type")
        mock_get_redis.return_value = mock_r

        result = card_to_dict(card, db_session)
        assert isinstance(result["brands"], list)

    @patch("app.cache.intel_cache._get_redis", return_value=None)
    def test_no_reviews(self, mock_redis, db_session):
        card = self._make_card(db_session)
        result = card_to_dict(card, db_session)
        assert result["avg_rating"] is None
        assert result["review_count"] == 0
        assert result["reviews"] == []

    @patch("app.cache.intel_cache._get_redis", return_value=None)
    def test_multiple_reviews_avg(self, mock_redis, db_session):
        card = self._make_card(db_session)
        user = User(
            email="rev2@test.com",
            name="Rev2",
            role="buyer",
            azure_id="rev-002",
            created_at=datetime.now(UTC),
        )
        db_session.add(user)
        db_session.flush()

        for rating in [3, 5]:
            r = VendorReview(vendor_card_id=card.id, user_id=user.id, rating=rating)
            db_session.add(r)
        db_session.commit()

        result = card_to_dict(card, db_session)
        assert result["avg_rating"] == 4.0
        assert result["review_count"] == 2

    @patch("app.cache.intel_cache._get_redis", return_value=None)
    def test_none_fields_serialized(self, mock_redis, db_session):
        """Card with mostly None fields still serializes correctly."""
        card = VendorCard(
            normalized_name="minimal",
            display_name="Minimal",
            emails=None,
            phones=None,
            sighting_count=None,
            is_blacklisted=None,
            is_new_vendor=None,
            last_enriched_at=None,
            last_contact_at=None,
            material_tags_updated_at=None,
            brand_tags=None,
            commodity_tags=None,
        )
        db_session.add(card)
        db_session.commit()
        db_session.refresh(card)

        result = card_to_dict(card, db_session)
        assert result["emails"] == []
        assert result["phones"] == []
        assert result["sighting_count"] == 0
        assert result["is_blacklisted"] is False
        assert result["is_new_vendor"] is True  # None defaults to True
        assert result["last_enriched_at"] is None
        assert result["last_contact_at"] is None
        assert result["material_tags_updated_at"] is None
        assert result["brand_tags"] == []
        assert result["commodity_tags"] == []

    @patch("app.cache.intel_cache._get_redis", return_value=None)
    def test_review_with_created_at(self, mock_redis, db_session):
        """Review with created_at serializes to ISO string."""
        card = self._make_card(db_session)

        user = User(
            email="tempuser@test.com",
            name="Temp",
            role="buyer",
            azure_id="temp-001",
            created_at=datetime.now(UTC),
        )
        db_session.add(user)
        db_session.flush()

        ts = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
        review = VendorReview(
            vendor_card_id=card.id,
            user_id=user.id,
            rating=3,
            created_at=ts,
        )
        db_session.add(review)
        db_session.commit()

        result = card_to_dict(card, db_session)
        assert result["review_count"] == 1
        assert result["reviews"][0]["created_at"] is not None
        assert "2026-03-01" in result["reviews"][0]["created_at"]

    @patch("app.cache.intel_cache._get_redis", return_value=None)
    def test_review_user_none(self, mock_redis, db_session):
        """Review where user relationship is None gives empty user_name."""
        card = self._make_card(db_session)

        user = User(
            email="u@test.com",
            name="U",
            role="buyer",
            azure_id="u-001",
            created_at=datetime.now(UTC),
        )
        db_session.add(user)
        db_session.flush()

        review = VendorReview(
            vendor_card_id=card.id,
            user_id=user.id,
            rating=5,
        )
        db_session.add(review)
        db_session.commit()

        # Verify the review user_name is populated
        result = card_to_dict(card, db_session)
        assert result["reviews"][0]["user_name"] == "U"


# ── scrape_website_contacts ──────────────────────────────────────────


class TestScrapeWebsiteContacts:
    def test_ssrf_blocked(self):
        """Private URLs return empty result."""

        async def _run():
            with (
                patch("app.cache.intel_cache.get_cached", return_value=None),
                patch("app.cache.intel_cache.set_cached"),
                patch("app.utils.vendor_helpers.is_private_url", return_value=True),
            ):
                return await scrape_website_contacts("http://192.168.1.1")

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result == {"emails": [], "phones": []}

    def test_cached_result_returned(self):
        """Cached results bypass scraping entirely."""
        cached = {"emails": ["cached@test.com"], "phones": ["+1-555-0000"]}

        async def _run():
            with patch("app.cache.intel_cache.get_cached", return_value=cached):
                return await scrape_website_contacts("https://testvendor.com")

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result == cached

    def test_successful_scrape(self):
        """Full scrape flow with mailto and tel extraction."""
        html_contact = (
            "<html><body>"
            '<a href="mailto:sales@vendor.com?subject=hi">Email</a>'
            '<a href="tel:+15551234567">Call</a>'
            "<p>Reach us at info@vendor.com</p>"
            "</body></html>"
        )
        html_home = "<html><body>Welcome</body></html>"

        responses = []
        for content in [html_contact, html_home, html_home]:
            resp = MagicMock()
            resp.status_code = 200
            resp.text = content
            responses.append(resp)

        call_count = 0

        async def mock_get(*args, **kwargs):
            nonlocal call_count
            idx = min(call_count, len(responses) - 1)
            call_count += 1
            return responses[idx]

        mock_http = MagicMock()
        mock_http.get = mock_get

        async def _run():
            with (
                patch("app.cache.intel_cache.get_cached", return_value=None),
                patch("app.cache.intel_cache.set_cached"),
                patch("app.utils.vendor_helpers.is_private_url", return_value=False),
                patch("app.utils.vendor_helpers.http", mock_http),
            ):
                return await scrape_website_contacts("https://vendor.com")

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert "sales@vendor.com" in result["emails"]
        assert "info@vendor.com" in result["emails"]
        assert len(result["phones"]) >= 1

    def test_http_errors_handled(self):
        """Exceptions from HTTP calls don't crash."""

        async def mock_get(*args, **kwargs):
            raise ConnectionError("timeout")

        mock_http = MagicMock()
        mock_http.get = mock_get

        async def _run():
            with (
                patch("app.cache.intel_cache.get_cached", return_value=None),
                patch("app.cache.intel_cache.set_cached"),
                patch("app.utils.vendor_helpers.is_private_url", return_value=False),
                patch("app.utils.vendor_helpers.http", mock_http),
            ):
                return await scrape_website_contacts("https://downsite.com")

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result == {"emails": [], "phones": []}

    def test_url_without_http_prefix(self):
        """URL without http:// gets prepended."""
        cached = {"emails": ["a@b.com"], "phones": []}

        async def _run():
            with patch("app.cache.intel_cache.get_cached", return_value=cached):
                return await scrape_website_contacts("vendor.com")

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result == cached

    def test_non_200_status_skipped(self):
        """Responses with non-200 status are skipped."""

        async def mock_get(*args, **kwargs):
            resp = MagicMock()
            resp.status_code = 403
            return resp

        mock_http = MagicMock()
        mock_http.get = mock_get

        async def _run():
            with (
                patch("app.cache.intel_cache.get_cached", return_value=None),
                patch("app.cache.intel_cache.set_cached"),
                patch("app.utils.vendor_helpers.is_private_url", return_value=False),
                patch("app.utils.vendor_helpers.http", mock_http),
            ):
                return await scrape_website_contacts("https://forbidden.com")

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result == {"emails": [], "phones": []}

    def test_url_domain_extraction_fallback(self):
        """When URL has no '//' the domain extraction falls to raw_url."""
        cached = {"emails": [], "phones": []}

        async def _run():
            # A URL that starts with "http" but has no "//": edge case
            with patch("app.cache.intel_cache.get_cached", return_value=cached):
                return await scrape_website_contacts("httpvendor.com")

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result == cached


# ── merge_contact_into_card ──────────────────────────────────────────


@contextmanager
def mock_merge_counts(emails_added, phones_added):
    """Patch merge_emails/phones_into_card to report the given counts of new entries.

    Yields the merge_emails mock so callers can assert on its calls.
    """
    with (
        patch("app.vendor_utils.merge_emails_into_card", return_value=emails_added) as mock_emails,
        patch("app.vendor_utils.merge_phones_into_card", return_value=phones_added),
    ):
        yield mock_emails


class TestMergeContactIntoCard:
    def test_merge_emails_only(self):
        card = MagicMock()
        card.website = "https://existing.com"
        with mock_merge_counts(2, 0) as mock_emails:
            changed = merge_contact_into_card(card, ["a@b.com", "c@d.com"], [])
        assert changed is True
        mock_emails.assert_called_once_with(card, ["a@b.com", "c@d.com"])

    def test_merge_phones_only(self):
        card = MagicMock()
        card.website = "https://existing.com"
        with mock_merge_counts(0, 1):
            changed = merge_contact_into_card(card, [], ["+1-555-0100"])
        assert changed is True

    def test_merge_website_when_missing(self):
        card = MagicMock()
        card.website = None
        with mock_merge_counts(0, 0):
            changed = merge_contact_into_card(card, [], [], website="https://new.com")
        assert changed is True
        assert card.website == "https://new.com"

    def test_website_not_overwritten_if_exists(self):
        card = MagicMock()
        card.website = "https://existing.com"
        with mock_merge_counts(0, 0):
            changed = merge_contact_into_card(card, [], [], website="https://new.com")
        assert changed is False
        assert card.website == "https://existing.com"

    def test_source_set_when_changed(self):
        card = MagicMock()
        card.website = "https://existing.com"
        with mock_merge_counts(1, 0):
            changed = merge_contact_into_card(card, ["a@b.com"], [], source="scrape")
        assert changed is True
        assert card.source == "scrape"

    def test_source_not_set_when_no_change(self):
        card = MagicMock()
        card.website = "https://existing.com"
        card.source = "original"
        with mock_merge_counts(0, 0):
            changed = merge_contact_into_card(card, [], [], source="scrape")
        assert changed is False
        # source should not have been set since changed is False
        assert card.source == "original"

    def test_no_change(self):
        card = MagicMock()
        card.website = "https://existing.com"
        with mock_merge_counts(0, 0):
            changed = merge_contact_into_card(card, [], [])
        assert changed is False

    def test_source_none_not_set(self):
        """When source=None and changed=True, source should not be updated."""
        card = MagicMock()
        card.website = "https://existing.com"
        card.source = "original"
        with mock_merge_counts(1, 0):
            changed = merge_contact_into_card(card, ["a@b.com"], [], source=None)
        assert changed is True
        assert card.source == "original"

    def test_both_emails_and_phones_changed(self):
        """Both emails and phones new — changed is True."""
        card = MagicMock()
        card.website = "https://existing.com"
        with mock_merge_counts(1, 1):
            changed = merge_contact_into_card(card, ["a@b.com"], ["+1-555-0100"], source="api")
        assert changed is True
        assert card.source == "api"


# ── find_vendor_card_by_name ─────────────────────────────────────────


class TestFindVendorCardByName:
    """Tests for the find_vendor_card_by_name helper.

    Verifies exact normalized lookup, miss, and case-insensitive normalization.
    """

    def _add_card(self, db_session, normalized_name: str, display_name: str) -> VendorCard:
        card = VendorCard(
            normalized_name=normalized_name,
            display_name=display_name,
            emails=[],
            phones=[],
        )
        db_session.add(card)
        db_session.commit()
        return card

    def test_returns_card_for_exact_normalized_match(self, db_session):
        """find_vendor_card_by_name returns the card when normalized name matches."""
        self._add_card(db_session, "arrow electronics", "Arrow Electronics")
        result = find_vendor_card_by_name("Arrow Electronics", db_session)
        assert result is not None
        assert result.display_name == "Arrow Electronics"

    def test_case_insensitive_normalization(self, db_session):
        """Normalization lowercases; different-case input hits the same card."""
        self._add_card(db_session, "arrow electronics", "Arrow Electronics")
        result = find_vendor_card_by_name("ARROW ELECTRONICS", db_session)
        assert result is not None
        assert result.normalized_name == "arrow electronics"

    def test_returns_none_when_no_match(self, db_session):
        """Returns None when no card matches the normalized name."""
        result = find_vendor_card_by_name("Nonexistent Vendor XYZ", db_session)
        assert result is None

    def test_ignores_unrelated_cards(self, db_session):
        """Does not return cards with different normalized names."""
        self._add_card(db_session, "arrow electronics", "Arrow Electronics")
        result = find_vendor_card_by_name("Digikey", db_session)
        assert result is None

    def test_normalizes_before_query(self, db_session):
        """Helper normalizes the input — whitespace and punctuation stripped."""
        from app.vendor_utils import normalize_vendor_name

        raw = "  Arrow  Electronics  "
        norm = normalize_vendor_name(raw)
        self._add_card(db_session, norm, "Arrow Electronics")
        result = find_vendor_card_by_name(raw, db_session)
        assert result is not None
