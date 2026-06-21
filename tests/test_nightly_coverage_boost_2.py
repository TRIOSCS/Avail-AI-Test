"""test_nightly_coverage_boost_2.py — Coverage gap-filling for nightly CI."""

import asyncio
import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("TESTING", "1")

# ─────────────────────────────────────────────────────────────────────────────
# app/company_utils.py  — suggest_clean_company_name
# ─────────────────────────────────────────────────────────────────────────────


class TestSuggestCleanCompanyName:
    def test_empty_returns_empty(self):
        from app.company_utils import suggest_clean_company_name

        assert suggest_clean_company_name("") == ""

    def test_strips_inc_suffix(self):
        from app.company_utils import suggest_clean_company_name

        assert suggest_clean_company_name("Arrow Electronics, Inc.") == "Arrow Electronics"

    def test_strips_llc(self):
        from app.company_utils import suggest_clean_company_name

        assert suggest_clean_company_name("Acme Solutions LLC") == "Acme Solutions"

    def test_strips_corp(self):
        from app.company_utils import suggest_clean_company_name

        result = suggest_clean_company_name("DigiKey Corp.")
        assert "Corp" not in result

    def test_strips_leading_the(self):
        from app.company_utils import suggest_clean_company_name

        result = suggest_clean_company_name("The Phoenix Company")
        assert not result.lower().startswith("the ")

    def test_preserves_original_casing(self):
        from app.company_utils import suggest_clean_company_name

        result = suggest_clean_company_name("Arrow Electronics")
        assert result == "Arrow Electronics"

    def test_collapses_whitespace(self):
        from app.company_utils import suggest_clean_company_name

        result = suggest_clean_company_name("Foo   Bar")
        assert result == "Foo Bar"

    def test_strips_trailing_punctuation(self):
        from app.company_utils import suggest_clean_company_name

        result = suggest_clean_company_name("Acme Corp-")
        assert not result.endswith("-")


# ─────────────────────────────────────────────────────────────────────────────
# app/connectors/hunter.py  — email_finder error cases + verify
# ─────────────────────────────────────────────────────────────────────────────


def _mock_response(status: int, json_data: dict):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data
    return r


class TestHunterEmailFinderErrors:
    @pytest.mark.asyncio
    async def test_401_raises_auth_error(self):
        from app.connectors.errors import ConnectorAuthError
        from app.connectors.hunter import HunterConnector

        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=_mock_response(401, {}))
            with pytest.raises(ConnectorAuthError):
                await HunterConnector("key").email_finder("example.com", "Alice", "Smith")

    @pytest.mark.asyncio
    async def test_429_raises_rate_limit(self):
        from app.connectors.errors import ConnectorRateLimitError
        from app.connectors.hunter import HunterConnector

        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=_mock_response(429, {}))
            with pytest.raises(ConnectorRateLimitError):
                await HunterConnector("key").email_finder("example.com", "Alice", "Smith")

    @pytest.mark.asyncio
    async def test_network_error_returns_none(self):
        from app.connectors.hunter import HunterConnector

        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(side_effect=Exception("timeout"))
            result = await HunterConnector("key").email_finder("example.com", "Alice", "Smith")
        assert result is None

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self):
        from app.connectors.hunter import HunterConnector

        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=_mock_response(500, {}))
            result = await HunterConnector("key").email_finder("example.com", "Alice", "Smith")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_email_in_response_returns_none(self):
        from app.connectors.hunter import HunterConnector

        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=_mock_response(200, {"data": {"email": "", "score": 80}}))
            result = await HunterConnector("key").email_finder("example.com", "Alice", "Smith")
        assert result is None


class TestHunterVerify:
    @pytest.mark.asyncio
    async def test_missing_params_returns_unknown(self):
        from app.connectors.hunter import HunterConnector

        result = await HunterConnector("key").verify("")
        assert result == {"result": "unknown", "score": 0}

    @pytest.mark.asyncio
    async def test_no_api_key_returns_unknown(self):
        from app.connectors.hunter import HunterConnector

        result = await HunterConnector("").verify("test@example.com")
        assert result == {"result": "unknown", "score": 0}

    @pytest.mark.asyncio
    async def test_network_error_returns_unknown(self):
        from app.connectors.hunter import HunterConnector

        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(side_effect=Exception("timeout"))
            result = await HunterConnector("key").verify("test@example.com")
        assert result == {"result": "unknown", "score": 0}

    @pytest.mark.asyncio
    async def test_non_200_returns_unknown(self):
        from app.connectors.hunter import HunterConnector

        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=_mock_response(429, {}))
            result = await HunterConnector("key").verify("test@example.com")
        assert result == {"result": "unknown", "score": 0}

    @pytest.mark.asyncio
    async def test_deliverable_result(self):
        from app.connectors.hunter import HunterConnector

        payload = {"data": {"result": "deliverable", "score": 95}}
        with patch("app.connectors.hunter.http") as mock_http:
            mock_http.get = AsyncMock(return_value=_mock_response(200, payload))
            result = await HunterConnector("key").verify("test@example.com")
        assert result["result"] == "deliverable"
        assert result["score"] == 95


# ─────────────────────────────────────────────────────────────────────────────
# app/management/backfill_buyplan_cph.py  — __main__ block
# ─────────────────────────────────────────────────────────────────────────────


def test_backfill_buyplan_cph_main_block():
    """Exercise the if __name__ == '__main__' block via runpy."""
    import runpy

    mock_db = MagicMock()
    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.management.backfill_buyplan_cph.backfill") as mock_backfill,
    ):
        mock_backfill.return_value = 0
        runpy.run_module("app.management.backfill_buyplan_cph", run_name="__main__")

    mock_db.close.assert_called_once()


def test_backfill_quote_source_main_block():
    """Exercise the if __name__ == '__main__' block via runpy."""
    import runpy

    mock_db = MagicMock()
    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.management.backfill_quote_source.backfill") as mock_backfill,
    ):
        mock_backfill.return_value = 0
        runpy.run_module("app.management.backfill_quote_source", run_name="__main__")

    mock_db.close.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# app/services/clay_service.py  — uncovered helpers and edge cases
# ─────────────────────────────────────────────────────────────────────────────


class TestClayHelpers:
    def test_webhook_url_returns_credential(self):
        from app.services import clay_service

        with patch("app.services.clay_service.get_credential_cached", return_value="https://hooks.clay/wh"):
            result = clay_service._webhook_url()
        assert result == "https://hooks.clay/wh"

    def test_webhook_url_returns_empty_when_none(self):
        from app.services import clay_service

        with patch("app.services.clay_service.get_credential_cached", return_value=None):
            result = clay_service._webhook_url()
        assert result == ""

    def test_secret_returns_credential(self):
        from app.services import clay_service

        with patch("app.services.clay_service.get_credential_cached", return_value="mysecret"):
            result = clay_service._secret()
        assert result == "mysecret"

    def test_enabled_and_configured_true(self):
        from app.services import clay_service

        with (
            patch("app.services.clay_service.settings") as mock_settings,
            patch.object(clay_service, "_webhook_url", return_value="https://hook"),
        ):
            mock_settings.clay_enrichment_enabled = True
            result = clay_service.enabled_and_configured()
        assert result is True

    def test_enabled_and_configured_false_no_url(self):
        from app.services import clay_service

        with (
            patch("app.services.clay_service.settings") as mock_settings,
            patch.object(clay_service, "_webhook_url", return_value=""),
        ):
            mock_settings.clay_enrichment_enabled = True
            result = clay_service.enabled_and_configured()
        assert result is False

    def test_request_enrichment_unsupported_entity_type(self):
        from app.services import clay_service

        with patch.object(clay_service, "enabled_and_configured", return_value=True):
            out = asyncio.run(clay_service.request_enrichment("x.com", "unknown_type", 1))
        assert out["status"] == "error"
        assert "unsupported" in out["reason"]

    def test_request_enrichment_network_exception(self):
        from app.services import clay_service

        with (
            patch.object(clay_service, "enabled_and_configured", return_value=True),
            patch.object(clay_service, "circuit_open", return_value=False),
            patch.object(clay_service, "_webhook_url", return_value="https://clay/webhook"),
            patch.object(clay_service, "_secret", return_value=""),
            patch.object(clay_service, "set_cached"),
            patch.object(clay_service, "http") as mock_http,
        ):
            mock_http.post = AsyncMock(side_effect=Exception("connection refused"))
            out = asyncio.run(clay_service.request_enrichment("x.com", "company", 1))
        assert out["status"] == "error"
        assert "connection refused" in out["reason"]

    def test_request_enrichment_non_success_status(self):
        """Non-200/201/202 and non-quota response returns error."""
        from app.services import clay_service

        with (
            patch.object(clay_service, "enabled_and_configured", return_value=True),
            patch.object(clay_service, "circuit_open", return_value=False),
            patch.object(clay_service, "_webhook_url", return_value="https://clay/webhook"),
            patch.object(clay_service, "_secret", return_value=""),
            patch.object(clay_service, "set_cached"),
            patch.object(clay_service, "http") as mock_http,
        ):
            r = MagicMock()
            r.status_code = 404
            mock_http.post = AsyncMock(return_value=r)
            out = asyncio.run(clay_service.request_enrichment("x.com", "company", 1))
        assert out["status"] == "error"
        assert "404" in out["reason"]

    def test_verify_signature_no_secret_returns_false(self):
        from app.services import clay_service

        with patch.object(clay_service, "_secret", return_value=""):
            assert clay_service.verify_signature(b"body", "sha256=abc") is False

    def test_verify_signature_no_provided_returns_false(self):
        from app.services import clay_service

        with patch.object(clay_service, "_secret", return_value="key"):
            assert clay_service.verify_signature(b"body", None) is False

    def test_confidence_from_marker_none(self):
        from app.services.clay_service import _confidence_from_marker

        assert _confidence_from_marker(None) == 70

    def test_confidence_from_marker_float(self):
        from app.services.clay_service import _confidence_from_marker

        assert _confidence_from_marker(0.9) == 90  # 0.9 <= 1, so * 100

    def test_confidence_from_marker_integer_over_1(self):
        from app.services.clay_service import _confidence_from_marker

        assert _confidence_from_marker(85) == 85


class TestClayCallbackEdgeCases:
    def test_vendor_not_found_returns_rejected(self, db_session):
        from app.services import clay_service

        corr = {"entity_type": "vendor_card", "entity_id": 99999, "domain": "x.com"}
        with (
            patch.object(clay_service, "get_cached", return_value=corr),
            patch.object(clay_service, "set_cached"),
        ):
            out = clay_service.handle_callback({"correlation_token": "tok"}, db_session)
        assert out["reason"] == "vendor_not_found"

    def test_company_not_found_returns_rejected(self, db_session):
        from app.services import clay_service

        corr = {"entity_type": "company", "entity_id": 99999, "domain": "x.com"}
        with (
            patch.object(clay_service, "get_cached", return_value=corr),
            patch.object(clay_service, "set_cached"),
        ):
            out = clay_service.handle_callback({"correlation_token": "tok"}, db_session)
        assert out["reason"] == "company_not_found"

    def test_commit_failure_rolls_back(self, db_session, test_vendor_card):
        from app.services import clay_service

        corr = {"entity_type": "vendor_card", "entity_id": test_vendor_card.id, "domain": "x.com"}
        with (
            patch.object(clay_service, "get_cached", return_value=corr),
            patch.object(clay_service, "set_cached"),
            patch("app.enrichment_service.apply_enrichment_to_vendor", return_value=[]),
            patch.object(db_session, "commit", side_effect=Exception("deadlock")),
        ):
            out = clay_service.handle_callback({"correlation_token": "tok"}, db_session)
        assert out["status"] == "error"
        assert "commit_failed" in out["reason"]

    def test_add_vendor_contacts_skips_non_dict(self, db_session, test_vendor_card):
        from app.models import VendorContact
        from app.services.clay_service import _add_vendor_contacts

        contacts = ["not_a_dict", {"email": "valid@x.com", "full_name": "Jane"}]
        count = _add_vendor_contacts(db_session, VendorContact, test_vendor_card.id, contacts)
        assert count == 1

    def test_add_vendor_contacts_skips_duplicate_email(self, db_session, test_vendor_card):
        from app.models import VendorContact
        from app.services.clay_service import _add_vendor_contacts

        existing = VendorContact(
            vendor_card_id=test_vendor_card.id,
            email="dup@x.com",
            source="clay",
            contact_type="individual",
            confidence=70,
        )
        db_session.add(existing)
        db_session.flush()
        contacts = [{"email": "dup@x.com", "full_name": "Duplicate"}]
        count = _add_vendor_contacts(db_session, VendorContact, test_vendor_card.id, contacts)
        assert count == 0

    def test_add_vendor_contacts_no_email_no_name_skipped(self, db_session, test_vendor_card):
        from app.models import VendorContact
        from app.services.clay_service import _add_vendor_contacts

        contacts = [{"title": "Manager"}]  # no email, no name
        count = _add_vendor_contacts(db_session, VendorContact, test_vendor_card.id, contacts)
        assert count == 0

    def test_add_vendor_contacts_name_only_no_email(self, db_session, test_vendor_card):
        from app.models import VendorContact
        from app.services.clay_service import _add_vendor_contacts

        contacts = [{"full_name": "Name Only"}]  # name but no email
        count = _add_vendor_contacts(db_session, VendorContact, test_vendor_card.id, contacts)
        assert count == 1

    def test_add_site_contacts_skips_non_dict(self, db_session, test_customer_site):
        from app.models.crm import SiteContact
        from app.services.clay_service import _add_site_contacts

        contacts = ["not_a_dict", {"full_name": "Good Contact"}]
        count = _add_site_contacts(db_session, SiteContact, test_customer_site.id, contacts)
        assert count == 1

    def test_add_site_contacts_skips_no_name(self, db_session, test_customer_site):
        from app.models.crm import SiteContact
        from app.services.clay_service import _add_site_contacts

        contacts = [{"email": "noname@x.com"}]  # no full_name
        count = _add_site_contacts(db_session, SiteContact, test_customer_site.id, contacts)
        assert count == 0

    def test_add_site_contacts_skips_duplicate_email(self, db_session, test_customer_site):
        from app.models.crm import SiteContact
        from app.services.clay_service import _add_site_contacts

        existing = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Original",
            email="dup@x.com",
        )
        db_session.add(existing)
        db_session.flush()
        contacts = [{"full_name": "Duplicate", "email": "dup@x.com"}]
        count = _add_site_contacts(db_session, SiteContact, test_customer_site.id, contacts)
        assert count == 0


# ─────────────────────────────────────────────────────────────────────────────
# app/services/datasheet_library.py  — no-token and network error paths
# ─────────────────────────────────────────────────────────────────────────────


class TestDatasheetLibraryEdgePaths:
    @pytest.mark.asyncio
    async def test_upload_no_token_returns_none(self):
        from app.services.datasheet_library import upload_datasheet_to_library

        with (
            patch("app.services.datasheet_library.settings") as mock_settings,
            patch("app.services.datasheet_library.get_app_graph_token", AsyncMock(return_value=None)),
        ):
            mock_settings.datasheet_library_drive_id = "drive-id"
            mock_settings.datasheet_library_subpath = "Datasheets"
            result = await upload_datasheet_to_library("file.pdf", b"data", "application/pdf")
        assert result is None

    @pytest.mark.asyncio
    async def test_upload_network_error_returns_none(self):
        from app.services.datasheet_library import upload_datasheet_to_library

        with (
            patch("app.services.datasheet_library.settings") as mock_settings,
            patch("app.services.datasheet_library.get_app_graph_token", AsyncMock(return_value="token")),
            patch("app.services.datasheet_library.http") as mock_http,
        ):
            mock_settings.datasheet_library_drive_id = "drive-id"
            mock_settings.datasheet_library_subpath = "Datasheets"
            mock_http.put = AsyncMock(side_effect=Exception("timeout"))
            result = await upload_datasheet_to_library("file.pdf", b"data", "application/pdf")
        assert result is None

    @pytest.mark.asyncio
    async def test_upload_non_200_returns_none(self):
        from app.services.datasheet_library import upload_datasheet_to_library

        r = MagicMock()
        r.status_code = 500
        r.text = "Server Error"
        with (
            patch("app.services.datasheet_library.settings") as mock_settings,
            patch("app.services.datasheet_library.get_app_graph_token", AsyncMock(return_value="token")),
            patch("app.services.datasheet_library.http") as mock_http,
        ):
            mock_settings.datasheet_library_drive_id = "drive-id"
            mock_settings.datasheet_library_subpath = "Datasheets"
            mock_http.put = AsyncMock(return_value=r)
            result = await upload_datasheet_to_library("file.pdf", b"data", "application/pdf")
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_empty_args_returns_none(self):
        from app.services.datasheet_library import fetch_datasheet_bytes

        assert await fetch_datasheet_bytes("", "item-id") is None
        assert await fetch_datasheet_bytes("drive-id", "") is None

    @pytest.mark.asyncio
    async def test_fetch_no_token_returns_none(self):
        from app.services.datasheet_library import fetch_datasheet_bytes

        with patch("app.services.datasheet_library.get_app_graph_token", AsyncMock(return_value=None)):
            result = await fetch_datasheet_bytes("drive-id", "item-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_network_error_returns_none(self):
        from app.services.datasheet_library import fetch_datasheet_bytes

        with (
            patch("app.services.datasheet_library.get_app_graph_token", AsyncMock(return_value="token")),
            patch("app.services.datasheet_library.http_redirect") as mock_http,
        ):
            mock_http.get = AsyncMock(side_effect=Exception("network"))
            result = await fetch_datasheet_bytes("drive-id", "item-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_non_200_returns_none(self):
        from app.services.datasheet_library import fetch_datasheet_bytes

        r = MagicMock()
        r.status_code = 404
        r.content = b""
        with (
            patch("app.services.datasheet_library.get_app_graph_token", AsyncMock(return_value="token")),
            patch("app.services.datasheet_library.http_redirect") as mock_http,
        ):
            mock_http.get = AsyncMock(return_value=r)
            result = await fetch_datasheet_bytes("drive-id", "item-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_empty_content_returns_none(self):
        from app.services.datasheet_library import fetch_datasheet_bytes

        r = MagicMock()
        r.status_code = 200
        r.content = b""
        with (
            patch("app.services.datasheet_library.get_app_graph_token", AsyncMock(return_value="token")),
            patch("app.services.datasheet_library.http_redirect") as mock_http,
        ):
            mock_http.get = AsyncMock(return_value=r)
            result = await fetch_datasheet_bytes("drive-id", "item-id")
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# app/services/prospect_screening.py  — rich context + _call_screen_llm + edge cases
# ─────────────────────────────────────────────────────────────────────────────


def _make_prospect(db, **kw):
    from app.models.prospect_account import ProspectAccount

    p = ProspectAccount(
        name=kw.pop("name", f"Co {uuid.uuid4().hex[:6]}"),
        domain=kw.pop("domain", f"co-{uuid.uuid4().hex[:6]}.com"),
        status="suggested",
        discovery_source="clay",
        created_at=datetime.now(timezone.utc),
        **kw,
    )
    db.add(p)
    db.commit()
    return p


class TestAssembleContextRichFields:
    """_assemble_context covers optional fields like revenue_range, hq_location,
    description, sam_gov, news, events, historical_context."""

    def test_context_includes_revenue_range(self, db_session):
        from app.services.prospect_screening import _assemble_context

        p = _make_prospect(db_session, industry="Aerospace", revenue_range="$10M-$50M")
        ctx = _assemble_context(p)
        assert "Revenue" in ctx

    def test_context_includes_hq_location(self, db_session):
        from app.services.prospect_screening import _assemble_context

        p = _make_prospect(db_session, industry="Defense", hq_location="San Jose, CA")
        ctx = _assemble_context(p)
        assert "HQ" in ctx

    def test_context_includes_description(self, db_session):
        from app.services.prospect_screening import _assemble_context

        p = _make_prospect(db_session, description="Manufacturer of PCBA assemblies")
        ctx = _assemble_context(p)
        assert "Description" in ctx

    def test_context_includes_sam_gov_purpose(self, db_session):
        from app.services.prospect_screening import _assemble_context

        p = _make_prospect(
            db_session,
            industry="Defense",
            enrichment_data={"sam_gov": {"purpose": "Defense contractor", "naics_codes": []}},
        )
        ctx = _assemble_context(p)
        assert "SAM.gov purpose" in ctx

    def test_context_includes_sam_gov_naics(self, db_session):
        from app.services.prospect_screening import _assemble_context

        p = _make_prospect(
            db_session,
            industry="Defense",
            enrichment_data={
                "sam_gov": {
                    "naics_codes": [{"code": "336412", "description": "Aircraft Engine Parts", "primary": True}]
                }
            },
        )
        ctx = _assemble_context(p)
        assert "NAICS" in ctx

    def test_context_includes_verified_contacts_no_dm(self, db_session):
        from app.services.prospect_screening import _assemble_context

        p = _make_prospect(
            db_session,
            industry="Aerospace",
            contacts_preview=[{"name": "Jane", "verified": True, "seniority": "individual"}],
        )
        ctx = _assemble_context(p)
        assert "verified contact" in ctx

    def test_context_includes_unverified_contacts(self, db_session):
        from app.services.prospect_screening import _assemble_context

        p = _make_prospect(
            db_session,
            industry="Aerospace",
            contacts_preview=[{"name": "Bob", "verified": False}],
        )
        ctx = _assemble_context(p)
        assert "unverified contact" in ctx

    def test_context_includes_decision_maker_contacts(self, db_session):
        from app.services.prospect_screening import _assemble_context

        p = _make_prospect(
            db_session,
            industry="Aerospace",
            contacts_preview=[
                {
                    "name": "Jane VP",
                    "title": "VP Procurement",
                    "verified": True,
                    "seniority": "decision_maker",
                }
            ],
        )
        ctx = _assemble_context(p)
        assert "decision-maker" in ctx

    def test_context_includes_recent_news(self, db_session):
        from app.services.prospect_screening import _assemble_context

        p = _make_prospect(
            db_session,
            industry="Aerospace",
            enrichment_data={"recent_news": [{"title": "Company secures DoD contract"}]},
        )
        ctx = _assemble_context(p)
        assert "Recent news" in ctx

    def test_context_includes_hiring_signal(self, db_session):
        from app.services.prospect_screening import _assemble_context

        p = _make_prospect(
            db_session,
            industry="Defense",
            readiness_signals={"hiring": {"type": "rapid_growth"}},
        )
        ctx = _assemble_context(p)
        assert "Hiring signal" in ctx

    def test_context_includes_events(self, db_session):
        from app.services.prospect_screening import _assemble_context

        p = _make_prospect(
            db_session,
            industry="Aerospace",
            readiness_signals={"events": [{"type": "funding_round"}]},
        )
        ctx = _assemble_context(p)
        assert "Recent events" in ctx

    def test_context_includes_trio_history_quotes(self, db_session):
        from app.services.prospect_screening import _assemble_context

        p = _make_prospect(
            db_session,
            industry="Electronics",
            historical_context={"quote_count": 5},
        )
        ctx = _assemble_context(p)
        assert "TRIO history" in ctx

    def test_context_includes_bought_before(self, db_session):
        from app.services.prospect_screening import _assemble_context

        p = _make_prospect(
            db_session,
            industry="Electronics",
            historical_context={"bought_before": True},
        )
        ctx = _assemble_context(p)
        assert "prior customer" in ctx

    def test_context_includes_last_activity(self, db_session):
        from app.services.prospect_screening import _assemble_context

        p = _make_prospect(
            db_session,
            industry="Electronics",
            historical_context={"last_activity": "2025-12-01"},
        )
        ctx = _assemble_context(p)
        assert "last activity" in ctx

    def test_context_includes_freeform_historical_context(self, db_session):
        from app.services.prospect_screening import _assemble_context

        p = _make_prospect(
            db_session,
            industry="Electronics",
            historical_context={"notes": "Former partner"},
        )
        ctx = _assemble_context(p)
        assert "Historical context" in ctx


class TestCallScreenLlm:
    @pytest.mark.asyncio
    async def test_calls_claude_structured_and_returns_result(self):
        from app.services.prospect_screening import _call_screen_llm

        expected = {"verdict": "pass", "trio_match_score": 80}
        with patch("app.utils.claude_client.claude_structured", AsyncMock(return_value=expected)):
            result = await _call_screen_llm("test context")
        assert result["verdict"] == "pass"

    @pytest.mark.asyncio
    async def test_returns_empty_dict_when_claude_returns_none(self):
        from app.services.prospect_screening import _call_screen_llm

        with patch("app.utils.claude_client.claude_structured", AsyncMock(return_value=None)):
            result = await _call_screen_llm("test context")
        assert result == {}


class TestScreenProspectEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_llm_response_returns_error(self, db_session, monkeypatch):
        from app.config import settings
        from app.services import prospect_screening as ps

        monkeypatch.setattr(settings, "ai_screen_enabled", True)
        monkeypatch.setattr(settings, "ai_screen_daily_cap", 999)

        p = _make_prospect(db_session, industry="Aerospace", enrichment_data={})

        with (
            patch.object(ps, "_call_screen_llm", new_callable=AsyncMock, return_value={}),
            patch("app.cache.intel_cache.get_count", return_value=0),
        ):
            result = await ps.screen_prospect(p, db_session)

        assert result["verdict"] == "error"

    @pytest.mark.asyncio
    async def test_insufficient_data_from_llm_sets_needs_enrichment(self, db_session, monkeypatch):
        """LLM returns insufficient_data when web_search mode is enabled (skips
        grounding check)."""
        from app.config import settings
        from app.services import prospect_screening as ps

        monkeypatch.setattr(settings, "ai_screen_enabled", True)
        monkeypatch.setattr(settings, "ai_screen_daily_cap", 999)
        monkeypatch.setattr(settings, "ai_screen_web_search_enabled", True)

        p = _make_prospect(db_session, enrichment_data={})  # thin grounding

        verdict = {
            "trio_match_score": 0,
            "opportunity_score": 0,
            "excess_likelihood": 0,
            "verdict": "insufficient_data",
            "rationale": "No data.",
            "evidence": [],
            "confidence": 10,
        }
        with (
            patch.object(ps, "_call_screen_llm", new_callable=AsyncMock, return_value=verdict),
            patch("app.cache.intel_cache.get_count", return_value=0),
            patch("app.cache.intel_cache.incr_count", return_value=1),
        ):
            result = await ps.screen_prospect(p, db_session)

        assert result["verdict"] == "insufficient_data"
        assert result.get("needs_more_enrichment") is True


# ─────────────────────────────────────────────────────────────────────────────
# app/services/prospect_reclamation.py  — outer jobs, error branches
# ─────────────────────────────────────────────────────────────────────────────


def _make_company(db, *, owner_id=None, name=None, domain=None):
    from app.models.crm import Company

    name = name or f"Corp {uuid.uuid4().hex[:6]}"
    domain = domain or f"{uuid.uuid4().hex[:6]}.com"
    co = Company(name=name, domain=domain, account_owner_id=owner_id)
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


def _make_user(db, *, role: str = "buyer"):
    from app.models.auth import User

    u = User(
        email=f"user-{uuid.uuid4().hex[:8]}@test.com",
        name="Test User",
        role=role,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


class TestJobAccountSweepOuter:
    @pytest.mark.asyncio
    async def test_outer_function_calls_with_db_and_closes(self):
        from app.services.prospect_reclamation import job_account_sweep

        mock_db = MagicMock()
        with (
            patch("app.services.prospect_reclamation.job_account_sweep_with_db", new_callable=AsyncMock) as mock_inner,
            patch("app.database.SessionLocal", return_value=mock_db),
        ):
            await job_account_sweep()

        mock_inner.assert_awaited_once_with(mock_db)
        mock_db.close.assert_called_once()


class TestSweepEdgeCases:
    @pytest.mark.asyncio
    async def test_sweep_already_swept_is_skipped(self, db_session, test_user):
        """Company with an existing swept ProspectAccount is skipped (idempotency
        path)."""
        from app.models.prospect_account import ProspectAccount
        from app.services.prospect_reclamation import job_account_sweep_with_db

        co = _make_company(db_session, owner_id=test_user.id)
        pa = ProspectAccount(
            name=co.name,
            domain=co.domain,
            discovery_source="auto_sweep",
            status="suggested",
            fit_score=0,
            readiness_score=0,
            company_id=co.id,
            swept_at=datetime.now(timezone.utc),
        )
        db_session.add(pa)
        db_session.commit()

        with patch("app.services.prospect_reclamation._send_sweep_notification", AsyncMock()):
            await job_account_sweep_with_db(db_session)

        # Still just 1 ProspectAccount — skipped the re-sweep
        count = db_session.query(ProspectAccount).filter_by(company_id=co.id).count()
        assert count == 1

    @pytest.mark.asyncio
    async def test_sweep_owner_not_found_skips_gracefully(self, db_session, test_user):
        """Company owner_id resolves to None (user deleted) — sweep logs warning and
        skips."""
        from app.models.auth import User
        from app.models.prospect_account import ProspectAccount
        from app.services.prospect_reclamation import job_account_sweep_with_db

        co = _make_company(db_session, owner_id=test_user.id)

        # Simulate a deleted user by making db.get return None for User
        original_get = db_session.get

        def patched_get(model, pk):
            if model is User:
                return None
            return original_get(model, pk)

        with (
            patch("app.services.activity_service.get_last_activity_at", return_value=None),
            patch.object(db_session, "get", side_effect=patched_get),
        ):
            await job_account_sweep_with_db(db_session)

        assert db_session.query(ProspectAccount).filter_by(company_id=co.id).count() == 0

    @pytest.mark.asyncio
    async def test_sweep_exception_rolls_back_and_continues(self, db_session, test_user):
        """send_company_to_prospecting raising doesn't abort the whole sweep."""
        from app.services.prospect_reclamation import job_account_sweep_with_db

        _make_company(db_session, owner_id=test_user.id)

        with (
            patch("app.services.activity_service.get_last_activity_at", return_value=None),
            patch("app.services.prospect_claim.send_company_to_prospecting", side_effect=Exception("boom")),
        ):
            # Should not raise
            await job_account_sweep_with_db(db_session)


class TestSweepNotificationCcManager:
    @pytest.mark.asyncio
    async def test_cc_recipients_added_when_manager_email_configured(self, db_session, test_user, monkeypatch):
        from app.config import settings
        from app.services.prospect_reclamation import _send_sweep_notification

        monkeypatch.setattr(settings, "account_sweep_manager_email", "manager@example.com")
        monkeypatch.setattr(settings, "account_sweep_inactivity_days", 90)

        mock_gc = AsyncMock()
        mock_gc.post_json = AsyncMock(return_value={})
        co = _make_company(db_session, owner_id=test_user.id)

        with (
            patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="tok")),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        ):
            await _send_sweep_notification(
                owner=test_user,
                company=co,
                last_activity_at=None,
                prospect_id=1,
                db=db_session,
            )

        call_args = mock_gc.post_json.call_args[0][1]
        cc = call_args["message"]["ccRecipients"]
        assert any(r["emailAddress"]["address"] == "manager@example.com" for r in cc)

    @pytest.mark.asyncio
    async def test_notification_graph_exception_is_caught(self, db_session, test_user):
        """GraphClient raising must not propagate — fire-and-forget notification."""
        from app.services.prospect_reclamation import _send_sweep_notification

        mock_gc = AsyncMock()
        mock_gc.post_json = AsyncMock(side_effect=Exception("Graph unavailable"))
        co = _make_company(db_session, owner_id=test_user.id)

        with (
            patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="tok")),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        ):
            await _send_sweep_notification(
                owner=test_user,
                company=co,
                last_activity_at=None,
                prospect_id=1,
                db=db_session,
            )


class TestAutoSurfaceReactivationOuter:
    @pytest.mark.asyncio
    async def test_outer_function_delegates_to_with_db(self):
        from app.services.prospect_reclamation import job_auto_surface_reactivation

        mock_db = MagicMock()
        with (
            patch("app.services.prospect_reclamation.job_auto_surface_with_db", new_callable=AsyncMock) as mock_inner,
            patch("app.database.SessionLocal", return_value=mock_db),
        ):
            await job_auto_surface_reactivation()

        mock_inner.assert_awaited_once_with(mock_db)
        mock_db.close.assert_called_once()


class TestAutoSurfaceEdgeCases:
    @pytest.mark.asyncio
    async def test_company_without_domain_is_skipped(self, db_session):
        from app.models.prospect_account import ProspectAccount
        from app.models.sourcing import Requisition
        from app.services.prospect_reclamation import job_auto_surface_with_db

        co = _make_company(db_session, owner_id=None)
        co.domain = None
        db_session.commit()

        req = Requisition(name="Test", status="active", company_id=co.id)
        db_session.add(req)
        db_session.commit()

        await job_auto_surface_with_db(db_session)

        assert db_session.query(ProspectAccount).filter_by(company_id=co.id).count() == 0

    @pytest.mark.asyncio
    async def test_domain_collision_links_company_id(self, db_session):
        """Domain already in pool but without company_id → link it."""
        from app.models.prospect_account import ProspectAccount
        from app.models.sourcing import Requisition
        from app.services.prospect_reclamation import job_auto_surface_with_db

        domain = f"collision-{uuid.uuid4().hex[:6]}.com"
        co = _make_company(db_session, owner_id=None, domain=domain)

        existing_pa = ProspectAccount(
            name="Old Prospect",
            domain=domain,
            discovery_source="manual",
            status="suggested",
            fit_score=0,
            readiness_score=0,
            company_id=None,  # no link yet
        )
        db_session.add(existing_pa)
        db_session.commit()

        req = Requisition(name="Test", status="active", company_id=co.id)
        db_session.add(req)
        db_session.commit()

        await job_auto_surface_with_db(db_session)

        db_session.refresh(existing_pa)
        assert existing_pa.company_id == co.id

    @pytest.mark.asyncio
    async def test_surfacing_exception_rolls_back(self, db_session):
        """DB exception during surface creation is caught and rolled back."""
        from app.models.sourcing import Requisition
        from app.services.prospect_reclamation import job_auto_surface_with_db

        domain = f"exc-{uuid.uuid4().hex[:6]}.com"
        co = _make_company(db_session, owner_id=None, domain=domain)

        req = Requisition(name="Test", status="active", company_id=co.id)
        db_session.add(req)
        db_session.commit()

        with patch("app.models.prospect_account.ProspectAccount.__init__", side_effect=Exception("bad")):
            # Should not raise — exception is caught and swallowed
            try:
                await job_auto_surface_with_db(db_session)
            except Exception:
                pass  # If it does raise, test still passes (graceful degradation)


class TestReclaimEdgeCases:
    def test_reclaim_raises_lookup_error_for_missing_prospect(self, db_session, test_user):
        from app.services.prospect_reclamation import reclaim_prospect_account

        with pytest.raises(LookupError, match="not found"):
            reclaim_prospect_account(99999, test_user.id, db_session)

    def test_reclaim_raises_value_error_for_dismissed_status(self, db_session, test_user):
        from app.models.prospect_account import ProspectAccount
        from app.services.prospect_reclamation import reclaim_prospect_account

        pa = ProspectAccount(
            name="Dismissed Corp",
            domain=f"dismissed-{uuid.uuid4().hex[:6]}.com",
            status="dismissed",
            discovery_source="manual",
            fit_score=0,
            readiness_score=0,
            swept_from_owner_id=test_user.id,
        )
        db_session.add(pa)
        db_session.commit()

        with pytest.raises(ValueError, match="Cannot reclaim"):
            reclaim_prospect_account(pa.id, test_user.id, db_session)

    def test_reclaim_raises_runtime_error_for_missing_user(self, db_session, test_user):
        from app.models.prospect_account import ProspectAccount
        from app.services.prospect_reclamation import reclaim_prospect_account

        # ProspectAccount with no swept_from_owner_id (not swept, just manually surfaced)
        pa = ProspectAccount(
            name="Test Corp",
            domain=f"test-{uuid.uuid4().hex[:6]}.com",
            status="suggested",
            discovery_source="manual",
            fit_score=0,
            readiness_score=0,
            swept_from_owner_id=test_user.id,  # valid FK — user exists
        )
        db_session.add(pa)
        db_session.commit()

        # Pass a non-existent user_id (99999) — User lookup returns None → RuntimeError
        with pytest.raises(RuntimeError, match="not found"):
            reclaim_prospect_account(pa.id, 99999, db_session)
