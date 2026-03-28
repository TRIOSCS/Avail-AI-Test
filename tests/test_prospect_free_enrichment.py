"""Tests for app/services/prospect_free_enrichment.py — SAM.gov + Google News
enrichment.

Called by: pytest
Depends on: conftest fixtures, unittest.mock
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from xml.etree.ElementTree import Element, SubElement, tostring

from sqlalchemy.orm import Session

from app.models.prospect_account import ProspectAccount
from app.services.prospect_free_enrichment import (
    _classify_headline,
    enrich_from_google_news,
    enrich_from_sam_gov,
    run_free_enrichment,
    run_free_enrichment_batch,
)

# ── Helpers ──────────────────────────────────────────────────────────


def _make_prospect(db: Session, **overrides) -> ProspectAccount:
    defaults = {
        "name": "Acme Corp",
        "domain": f"acme-{id(overrides)}.com",
        "status": "suggested",
        "fit_score": 50,
        "discovery_source": "manual",
        "enrichment_data": {},
        "readiness_signals": {},
        "created_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    p = ProspectAccount(**defaults)
    db.add(p)
    db.flush()
    return p


def _build_rss_xml(items: list[dict]) -> bytes:
    """Build minimal RSS XML for testing."""
    rss = Element("rss")
    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = "Google News"
    for item_data in items:
        item = SubElement(channel, "item")
        SubElement(item, "title").text = item_data.get("title", "Headline")
        SubElement(item, "link").text = item_data.get("link", "https://example.com")
        SubElement(item, "pubDate").text = item_data.get("pubDate", "Mon, 01 Jan 2026 00:00:00 GMT")
        SubElement(item, "source").text = item_data.get("source", "Reuters")
    return tostring(rss)


# ── _classify_headline tests ─────────────────────────────────────────


class TestClassifyHeadline:
    def test_funding(self):
        assert _classify_headline("Acme raises $50M in Series B") == "funding"
        assert _classify_headline("Company IPO expected next quarter") == "funding"

    def test_acquisition(self):
        assert _classify_headline("BigCorp acquires SmallCo for $1B") == "acquisition"
        assert _classify_headline("Merger between Acme and Widget Corp") == "acquisition"
        assert _classify_headline("Company buys rival firm") == "acquisition"

    def test_expansion(self):
        assert _classify_headline("Acme opens new facility in Texas") == "expansion"
        assert _classify_headline("Company expands operations globally") == "expansion"
        assert _classify_headline("New plant announced in Ohio") == "expansion"

    def test_product(self):
        assert _classify_headline("Acme launches new product line") == "product"
        assert _classify_headline("Company unveils groundbreaking tech") == "product"
        assert _classify_headline("Company introduces new component") == "product"

    def test_hiring(self):
        assert _classify_headline("Acme hiring 500 engineers") == "hiring"
        assert _classify_headline("Company recruits top talent") == "hiring"

    def test_layoffs(self):
        assert _classify_headline("Company announces layoffs") == "layoffs"
        assert _classify_headline("Acme cuts 200 jobs") == "layoffs"
        assert _classify_headline("Restructuring plan announced") == "layoffs"

    def test_contract(self):
        assert _classify_headline("Acme wins $100M defense contract") == "contract"
        assert _classify_headline("Government awards major deal") == "contract"
        assert _classify_headline("Pentagon contract to Acme Corp") == "contract"

    def test_regulatory(self):
        assert _classify_headline("FDA approves new device") == "regulatory"
        assert _classify_headline("Company receives FAA certification") == "regulatory"
        assert _classify_headline("Regulatory compliance update") == "regulatory"

    def test_general(self):
        assert _classify_headline("Acme Corp reports Q4 earnings") == "general"
        assert _classify_headline("Market update for semiconductors") == "general"


# ── enrich_from_sam_gov tests ────────────────────────────────────────


class TestEnrichFromSamGov:
    def test_empty_name_returns_none(self, db_session):
        prospect = _make_prospect(db_session, name="")
        result = asyncio.get_event_loop().run_until_complete(enrich_from_sam_gov(prospect))
        assert result is None

    def test_whitespace_name_returns_none(self, db_session):
        prospect = _make_prospect(db_session, name="   ")
        result = asyncio.get_event_loop().run_until_complete(enrich_from_sam_gov(prospect))
        assert result is None

    def test_successful_enrichment(self, db_session):
        prospect = _make_prospect(db_session, name="Acme Corp")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "entityData": [
                {
                    "entityRegistration": {
                        "ueiSAM": "UEI123456",
                        "cageCode": "CAGE1",
                        "legalBusinessName": "Acme Corp",
                        "dbaName": "Acme",
                        "registrationStatus": "Active",
                        "purposeOfRegistrationDesc": "All Awards",
                    },
                    "coreData": {
                        "generalInformation": {
                            "entityTypeDesc": "Business or Organization",
                            "organizationTypeDesc": "LLC",
                        },
                        "physicalAddress": {
                            "stateOrProvinceCode": "TX",
                            "countryCode": "USA",
                        },
                        "naicsCodeList": [
                            {
                                "naicsCode": "334413",
                                "naicsDescription": "Semiconductor Manufacturing",
                                "primaryNaicsCode": True,
                            }
                        ],
                    },
                }
            ]
        }

        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_resp)

        with patch("app.http_client.http", mock_http):
            result = asyncio.get_event_loop().run_until_complete(enrich_from_sam_gov(prospect))

        assert result is not None
        assert result["source"] == "sam_gov"
        assert result["uei"] == "UEI123456"
        assert result["cage_code"] == "CAGE1"
        assert result["legal_name"] == "Acme Corp"
        assert result["state"] == "TX"
        assert len(result["naics_codes"]) == 1
        assert result["naics_codes"][0]["code"] == "334413"
        assert result["naics_codes"][0]["primary"] is True

    def test_non_200_returns_none(self, db_session):
        prospect = _make_prospect(db_session, name="Acme Corp")
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.text = "Rate limited"

        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_resp)

        with patch("app.http_client.http", mock_http):
            result = asyncio.get_event_loop().run_until_complete(enrich_from_sam_gov(prospect))
        assert result is None

    def test_no_entities_returns_none(self, db_session):
        prospect = _make_prospect(db_session, name="Nonexistent Corp")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"entityData": []}

        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_resp)

        with patch("app.http_client.http", mock_http):
            result = asyncio.get_event_loop().run_until_complete(enrich_from_sam_gov(prospect))
        assert result is None

    def test_exception_returns_none(self, db_session):
        prospect = _make_prospect(db_session, name="Acme Corp")
        mock_http = MagicMock()
        mock_http.get = AsyncMock(side_effect=ConnectionError("DNS failure"))

        with patch("app.http_client.http", mock_http):
            result = asyncio.get_event_loop().run_until_complete(enrich_from_sam_gov(prospect))
        assert result is None

    def test_naics_dict_filter(self, db_session):
        """Only dict entries in naicsCodeList are processed."""
        prospect = _make_prospect(db_session, name="Acme Corp")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "entityData": [
                {
                    "entityRegistration": {},
                    "coreData": {
                        "generalInformation": {},
                        "physicalAddress": {},
                        "naicsCodeList": [
                            "not_a_dict",
                            {"naicsCode": "111", "naicsDescription": "test", "primaryNaicsCode": False},
                        ],
                    },
                }
            ]
        }

        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_resp)

        with patch("app.http_client.http", mock_http):
            result = asyncio.get_event_loop().run_until_complete(enrich_from_sam_gov(prospect))

        assert len(result["naics_codes"]) == 1
        assert result["naics_codes"][0]["code"] == "111"


# ── enrich_from_google_news tests ────────────────────────────────────


class TestEnrichFromGoogleNews:
    def test_empty_name_returns_empty(self, db_session):
        prospect = _make_prospect(db_session, name="")
        result = asyncio.get_event_loop().run_until_complete(enrich_from_google_news(prospect))
        assert result == []

    def test_successful_fetch(self, db_session):
        prospect = _make_prospect(db_session, name="Acme Corp")
        rss_xml = _build_rss_xml(
            [
                {"title": "Acme Corp raises $50M in Series C", "source": "TechCrunch"},
                {"title": "Acme Corp quarterly earnings report", "source": "Reuters"},
            ]
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = rss_xml

        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_resp)

        with patch("app.http_client.http", mock_http):
            result = asyncio.get_event_loop().run_until_complete(enrich_from_google_news(prospect))

        assert len(result) == 2
        assert result[0]["signal_type"] == "funding"
        assert result[1]["signal_type"] == "general"

    def test_non_200_returns_empty(self, db_session):
        prospect = _make_prospect(db_session, name="Acme Corp")
        mock_resp = MagicMock()
        mock_resp.status_code = 503

        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_resp)

        with patch("app.http_client.http", mock_http):
            result = asyncio.get_event_loop().run_until_complete(enrich_from_google_news(prospect))
        assert result == []

    def test_max_items_limit(self, db_session):
        prospect = _make_prospect(db_session, name="Acme Corp")
        items = [{"title": f"News {i}"} for i in range(10)]
        rss_xml = _build_rss_xml(items)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = rss_xml

        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_resp)

        with patch("app.http_client.http", mock_http):
            result = asyncio.get_event_loop().run_until_complete(enrich_from_google_news(prospect, max_items=3))
        assert len(result) == 3

    def test_no_channel_returns_empty(self, db_session):
        prospect = _make_prospect(db_session, name="Acme Corp")
        # RSS with no <channel> element
        rss_xml = b"<rss></rss>"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = rss_xml

        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_resp)

        with patch("app.http_client.http", mock_http):
            result = asyncio.get_event_loop().run_until_complete(enrich_from_google_news(prospect))
        assert result == []

    def test_exception_returns_empty(self, db_session):
        prospect = _make_prospect(db_session, name="Acme Corp")
        mock_http = MagicMock()
        mock_http.get = AsyncMock(side_effect=Exception("Network error"))

        with patch("app.http_client.http", mock_http):
            result = asyncio.get_event_loop().run_until_complete(enrich_from_google_news(prospect))
        assert result == []


# ── run_free_enrichment tests ────────────────────────────────────────


class TestRunFreeEnrichment:
    def test_prospect_not_found(self, db_session):
        result = asyncio.get_event_loop().run_until_complete(run_free_enrichment(99999, db=db_session))
        assert result == {"error": "not_found"}

    def test_sam_gov_enriches(self, db_session):
        prospect = _make_prospect(db_session, name="Acme Corp")
        db_session.commit()

        sam_data = {
            "source": "sam_gov",
            "uei": "UEI123",
            "cage_code": "CAGE1",
            "naics_codes": [{"code": "334413", "description": "Semiconductors", "primary": True}],
        }

        with patch(
            "app.services.prospect_free_enrichment.enrich_from_sam_gov",
            new_callable=AsyncMock,
            return_value=sam_data,
        ):
            with patch(
                "app.services.prospect_free_enrichment.enrich_from_google_news",
                new_callable=AsyncMock,
                return_value=[],
            ):
                result = asyncio.get_event_loop().run_until_complete(run_free_enrichment(prospect.id, db=db_session))

        assert result["sam_gov"] is True
        assert result["news_count"] == 0
        db_session.refresh(prospect)
        assert prospect.enrichment_data.get("sam_gov") is not None
        assert prospect.naics_code == "334413"

    def test_skips_existing_sam_data(self, db_session):
        prospect = _make_prospect(
            db_session,
            name="Already Enriched",
            enrichment_data={"sam_gov": {"uei": "existing"}},
        )
        db_session.commit()

        with patch(
            "app.services.prospect_free_enrichment.enrich_from_sam_gov",
            new_callable=AsyncMock,
        ) as mock_sam:
            with patch(
                "app.services.prospect_free_enrichment.enrich_from_google_news",
                new_callable=AsyncMock,
                return_value=[],
            ):
                result = asyncio.get_event_loop().run_until_complete(run_free_enrichment(prospect.id, db=db_session))

        mock_sam.assert_not_called()
        assert result["sam_gov"] is False

    def test_news_enrichment_with_signals(self, db_session):
        prospect = _make_prospect(db_session, name="News Corp")
        db_session.commit()

        news = [
            {
                "title": "News Corp raises $100M",
                "link": "http://a.com",
                "pub_date": "2026-01-01",
                "source": "TC",
                "signal_type": "funding",
            },
            {
                "title": "Quarterly report released",
                "link": "http://b.com",
                "pub_date": "2026-01-02",
                "source": "BBG",
                "signal_type": "general",
            },
        ]

        with patch(
            "app.services.prospect_free_enrichment.enrich_from_sam_gov",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with patch(
                "app.services.prospect_free_enrichment.enrich_from_google_news",
                new_callable=AsyncMock,
                return_value=news,
            ):
                result = asyncio.get_event_loop().run_until_complete(run_free_enrichment(prospect.id, db=db_session))

        assert result["news_count"] == 2
        db_session.refresh(prospect)
        assert "recent_news" in prospect.enrichment_data
        # Only non-general signals get added to readiness_signals events
        signals = prospect.readiness_signals or {}
        events = signals.get("events", [])
        assert len(events) == 1  # "funding" only, not "general"
        assert events[0]["type"] == "funding"

    def test_exception_handling(self, db_session):
        prospect = _make_prospect(db_session, name="Error Corp")
        db_session.commit()

        with patch(
            "app.services.prospect_free_enrichment.enrich_from_sam_gov",
            new_callable=AsyncMock,
            side_effect=Exception("boom"),
        ):
            result = asyncio.get_event_loop().run_until_complete(run_free_enrichment(prospect.id, db=db_session))

        assert "error" in result

    def test_creates_own_session_when_none(self):
        mock_session = MagicMock(spec=Session)
        mock_session.get.return_value = None
        mock_session.close = MagicMock()

        with patch(
            "app.database.SessionLocal",
            return_value=mock_session,
        ):
            result = asyncio.get_event_loop().run_until_complete(run_free_enrichment(123))

        assert result == {"error": "not_found"}
        mock_session.close.assert_called_once()

    def test_naics_not_overwritten_if_already_set(self, db_session):
        prospect = _make_prospect(db_session, name="Has NAICS", naics_code="999999")
        db_session.commit()

        sam_data = {
            "source": "sam_gov",
            "naics_codes": [{"code": "111111", "primary": True}],
        }

        with patch(
            "app.services.prospect_free_enrichment.enrich_from_sam_gov",
            new_callable=AsyncMock,
            return_value=sam_data,
        ):
            with patch(
                "app.services.prospect_free_enrichment.enrich_from_google_news",
                new_callable=AsyncMock,
                return_value=[],
            ):
                asyncio.get_event_loop().run_until_complete(run_free_enrichment(prospect.id, db=db_session))

        db_session.refresh(prospect)
        assert prospect.naics_code == "999999"  # Unchanged

    def test_news_dedup_in_readiness_signals(self, db_session):
        prospect = _make_prospect(
            db_session,
            name="Dedup Corp",
            readiness_signals={
                "events": [{"type": "funding", "description": "Dedup Corp raises $100M in Series C funding round"}]
            },
        )
        db_session.commit()

        news = [
            {
                "title": "Dedup Corp raises $100M in Series C funding round",
                "link": "http://a.com",
                "pub_date": "2026-01-01",
                "source": "TC",
                "signal_type": "funding",
            },
        ]

        with patch(
            "app.services.prospect_free_enrichment.enrich_from_sam_gov",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with patch(
                "app.services.prospect_free_enrichment.enrich_from_google_news",
                new_callable=AsyncMock,
                return_value=news,
            ):
                asyncio.get_event_loop().run_until_complete(run_free_enrichment(prospect.id, db=db_session))

        db_session.refresh(prospect)
        events = prospect.readiness_signals.get("events", [])
        # Should not duplicate — both share same first 50 chars
        assert len(events) == 1


# ── run_free_enrichment_batch tests ──────────────────────────────────


class TestRunFreeEnrichmentBatch:
    def test_batch_processes_prospects(self, db_session):
        # Create prospects in the DB for the batch query
        for i in range(3):
            _make_prospect(
                db_session,
                name=f"Prospect {i}",
                domain=f"batch-{i}.com",
                status="suggested",
                fit_score=60,
            )
        db_session.commit()

        mock_session = MagicMock(spec=Session)
        prospects_query = MagicMock()
        prospects_query.filter.return_value = prospects_query
        prospects_query.order_by.return_value = prospects_query
        prospects_query.limit.return_value = prospects_query
        prospects_query.all.return_value = [(1,), (2,), (3,)]
        mock_session.query.return_value = prospects_query
        mock_session.close = MagicMock()

        with patch(
            "app.database.SessionLocal",
            return_value=mock_session,
        ):
            with patch(
                "app.services.prospect_free_enrichment.run_free_enrichment",
                new_callable=AsyncMock,
                return_value={"sam_gov": True, "news_count": 2},
            ) as mock_enrich:
                result = asyncio.get_event_loop().run_until_complete(run_free_enrichment_batch(min_fit_score=40))

        assert result["processed"] == 3
        assert result["sam_hits"] == 3
        assert result["news_hits"] == 3
        assert mock_enrich.call_count == 3

    def test_batch_counts_errors(self):
        mock_session = MagicMock(spec=Session)
        prospects_query = MagicMock()
        prospects_query.filter.return_value = prospects_query
        prospects_query.order_by.return_value = prospects_query
        prospects_query.limit.return_value = prospects_query
        prospects_query.all.return_value = [(1,)]
        mock_session.query.return_value = prospects_query
        mock_session.close = MagicMock()

        with patch(
            "app.database.SessionLocal",
            return_value=mock_session,
        ):
            with patch(
                "app.services.prospect_free_enrichment.run_free_enrichment",
                new_callable=AsyncMock,
                return_value={"error": "not_found"},
            ):
                result = asyncio.get_event_loop().run_until_complete(run_free_enrichment_batch())

        assert result["errors"] == 1
        assert result["processed"] == 0

    def test_batch_handles_exception_in_enrichment(self):
        mock_session = MagicMock(spec=Session)
        prospects_query = MagicMock()
        prospects_query.filter.return_value = prospects_query
        prospects_query.order_by.return_value = prospects_query
        prospects_query.limit.return_value = prospects_query
        prospects_query.all.return_value = [(1,)]
        mock_session.query.return_value = prospects_query
        mock_session.close = MagicMock()

        with patch(
            "app.database.SessionLocal",
            return_value=mock_session,
        ):
            with patch(
                "app.services.prospect_free_enrichment.run_free_enrichment",
                new_callable=AsyncMock,
                side_effect=RuntimeError("crash"),
            ):
                result = asyncio.get_event_loop().run_until_complete(run_free_enrichment_batch())

        assert result["errors"] == 1

    def test_batch_empty_prospects(self):
        mock_session = MagicMock(spec=Session)
        prospects_query = MagicMock()
        prospects_query.filter.return_value = prospects_query
        prospects_query.order_by.return_value = prospects_query
        prospects_query.limit.return_value = prospects_query
        prospects_query.all.return_value = []
        mock_session.query.return_value = prospects_query
        mock_session.close = MagicMock()

        with patch(
            "app.database.SessionLocal",
            return_value=mock_session,
        ):
            result = asyncio.get_event_loop().run_until_complete(run_free_enrichment_batch())

        assert result["processed"] == 0
        assert result["errors"] == 0
