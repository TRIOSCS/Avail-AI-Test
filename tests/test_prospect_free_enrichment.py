"""
test_prospect_free_enrichment.py — Tests for free enrichment sources (SAM.gov + Google News RSS)

Covers: enrich_from_sam_gov, enrich_from_google_news, _classify_headline,
        run_free_enrichment, run_free_enrichment_batch

Called by: pytest
Depends on: conftest fixtures, prospect_free_enrichment module
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.prospect_free_enrichment import (
    _classify_headline,
    enrich_from_google_news,
    enrich_from_sam_gov,
    run_free_enrichment,
    run_free_enrichment_batch,
)


# ── _classify_headline ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "headline,expected",
    [
        ("Acme Corp raises $50M in Series C funding round", "funding"),
        ("Company announces IPO plans for 2026", "funding"),
        ("TechCo acquires rival firm for $2B", "acquisition"),
        ("Merger between Alpha and Beta finalized", "acquisition"),
        ("Company expands with new manufacturing facility in Texas", "expansion"),
        ("New office headquarters announced in Boston", "expansion"),
        ("Company launches revolutionary new product line", "product"),
        ("Company unveils next-gen semiconductor chip", "product"),
        ("Company hiring 500 engineers for new division", "hiring"),
        ("Major tech firm announces layoffs of 1,000 workers", "layoffs"),
        ("Company restructures operations amid downturn", "layoffs"),
        ("Company wins $100M DoD defense contract", "contract"),
        ("Pentagon awards new government contract to company", "contract"),
        ("Company receives FDA certification for medical device", "regulatory"),
        ("Just a regular news article about the company", "general"),
        ("Quarterly earnings report shows growth", "general"),
    ],
)
def test_classify_headline(headline, expected):
    assert _classify_headline(headline) == expected


def test_classify_headline_case_insensitive():
    assert _classify_headline("COMPANY RAISES FUNDING") == "funding"
    assert _classify_headline("New Acquisition Announced") == "acquisition"


# ── enrich_from_sam_gov ────────────────────────────────────────────────


class TestEnrichFromSamGov:
    def test_empty_name_returns_none(self):
        prospect = SimpleNamespace(name="")
        result = asyncio.get_event_loop().run_until_complete(enrich_from_sam_gov(prospect))
        assert result is None

    def test_none_name_returns_none(self):
        prospect = SimpleNamespace(name=None)
        result = asyncio.get_event_loop().run_until_complete(enrich_from_sam_gov(prospect))
        assert result is None

    def test_successful_response(self):
        prospect = SimpleNamespace(name="Lockheed Martin")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "entityData": [{
                "entityRegistration": {
                    "ueiSAM": "UEI123", "cageCode": "CAGE1",
                    "legalBusinessName": "Lockheed Martin", "dbaName": "LM",
                    "registrationStatus": "Active", "purposeOfRegistrationDesc": "Federal",
                },
                "coreData": {
                    "generalInformation": {"entityTypeDesc": "Business", "organizationTypeDesc": "Corporation"},
                    "physicalAddress": {"stateOrProvinceCode": "MD", "countryCode": "US"},
                    "naicsCodeList": [
                        {"naicsCode": "336411", "naicsDescription": "Aircraft Mfg", "primaryNaicsCode": True},
                    ],
                },
            }],
        }
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        with patch("app.http_client.http", mock_http):
            result = asyncio.get_event_loop().run_until_complete(enrich_from_sam_gov(prospect))
        assert result is not None
        assert result["source"] == "sam_gov"
        assert result["uei"] == "UEI123"
        assert result["cage_code"] == "CAGE1"
        assert len(result["naics_codes"]) == 1

    def test_non_200_returns_none(self):
        prospect = SimpleNamespace(name="TestCo")
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Server Error"
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        with patch("app.http_client.http", mock_http):
            result = asyncio.get_event_loop().run_until_complete(enrich_from_sam_gov(prospect))
        assert result is None

    def test_empty_entities_returns_none(self):
        prospect = SimpleNamespace(name="NoCo")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"entityData": []}
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        with patch("app.http_client.http", mock_http):
            result = asyncio.get_event_loop().run_until_complete(enrich_from_sam_gov(prospect))
        assert result is None

    def test_exception_returns_none(self):
        prospect = SimpleNamespace(name="ErrorCo")
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(side_effect=Exception("connection error"))
        with patch("app.http_client.http", mock_http):
            result = asyncio.get_event_loop().run_until_complete(enrich_from_sam_gov(prospect))
        assert result is None

    def test_naics_non_dict_skipped(self):
        prospect = SimpleNamespace(name="TestCo")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "entityData": [{
                "entityRegistration": {},
                "coreData": {
                    "generalInformation": {},
                    "physicalAddress": {},
                    "naicsCodeList": ["not-a-dict", {"naicsCode": "123", "naicsDescription": "Test"}],
                },
            }],
        }
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        with patch("app.http_client.http", mock_http):
            result = asyncio.get_event_loop().run_until_complete(enrich_from_sam_gov(prospect))
        assert result is not None
        assert len(result["naics_codes"]) == 1

    def test_none_naics_list(self):
        prospect = SimpleNamespace(name="NoNaics")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "entityData": [{
                "entityRegistration": {"ueiSAM": "U1"},
                "coreData": {"generalInformation": {}, "physicalAddress": {}, "naicsCodeList": None},
            }],
        }
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        with patch("app.http_client.http", mock_http):
            result = asyncio.get_event_loop().run_until_complete(enrich_from_sam_gov(prospect))
        assert result is not None
        assert result["naics_codes"] == []


# ── enrich_from_google_news ───────────────────────────────────────────


_SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test News</title>
    <item>
      <title>Acme raises $100M in Series C funding</title>
      <link>https://news.example.com/1</link>
      <pubDate>Mon, 01 Jan 2026 00:00:00 GMT</pubDate>
      <source>TechCrunch</source>
    </item>
    <item>
      <title>Acme quarterly results beat expectations</title>
      <link>https://news.example.com/2</link>
      <pubDate>Tue, 02 Jan 2026 00:00:00 GMT</pubDate>
      <source>Reuters</source>
    </item>
  </channel>
</rss>"""


class TestEnrichFromGoogleNews:
    def test_empty_name_returns_empty(self):
        prospect = SimpleNamespace(name="")
        result = asyncio.get_event_loop().run_until_complete(enrich_from_google_news(prospect))
        assert result == []

    def test_none_name_returns_empty(self):
        prospect = SimpleNamespace(name=None)
        result = asyncio.get_event_loop().run_until_complete(enrich_from_google_news(prospect))
        assert result == []

    def test_successful_rss_parse(self):
        prospect = SimpleNamespace(name="Acme Corp")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = _SAMPLE_RSS
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        with patch("app.http_client.http", mock_http):
            result = asyncio.get_event_loop().run_until_complete(enrich_from_google_news(prospect))
        assert len(result) == 2
        assert result[0]["signal_type"] == "funding"
        assert result[1]["signal_type"] == "general"
        assert result[0]["source"] == "TechCrunch"

    def test_non_200_returns_empty(self):
        prospect = SimpleNamespace(name="TestCo")
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        with patch("app.http_client.http", mock_http):
            result = asyncio.get_event_loop().run_until_complete(enrich_from_google_news(prospect))
        assert result == []

    def test_exception_returns_empty(self):
        prospect = SimpleNamespace(name="ErrorCo")
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(side_effect=Exception("timeout"))
        with patch("app.http_client.http", mock_http):
            result = asyncio.get_event_loop().run_until_complete(enrich_from_google_news(prospect))
        assert result == []

    def test_max_items_limits_results(self):
        prospect = SimpleNamespace(name="Acme Corp")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = _SAMPLE_RSS
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        with patch("app.http_client.http", mock_http):
            result = asyncio.get_event_loop().run_until_complete(enrich_from_google_news(prospect, max_items=1))
        assert len(result) == 1

    def test_no_channel_returns_empty(self):
        prospect = SimpleNamespace(name="TestCo")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'<?xml version="1.0"?><rss><nochannel/></rss>'
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        with patch("app.http_client.http", mock_http):
            result = asyncio.get_event_loop().run_until_complete(enrich_from_google_news(prospect))
        assert result == []


# ── run_free_enrichment ──────────────────────────────────────────────


class TestRunFreeEnrichment:
    def test_prospect_not_found(self, db_session):
        with patch("app.database.SessionLocal", return_value=db_session):
            with patch.object(db_session, "close"):
                result = asyncio.get_event_loop().run_until_complete(run_free_enrichment(99999))
        assert result == {"error": "not_found"}

    def test_enriches_sam_and_news(self, db_session):
        from app.models.prospect_account import ProspectAccount
        pa = ProspectAccount(
            name="Lockheed Martin", domain="lockheedmartin.com",
            discovery_source="manual", status="suggested",
            enrichment_data={}, readiness_signals={},
        )
        db_session.add(pa)
        db_session.commit()

        sam_data = {"source": "sam_gov", "naics_codes": [{"code": "336411", "primary": True}]}
        news_data = [{"title": "LM wins contract", "link": "http://x", "pub_date": "", "source": "NYT", "signal_type": "contract"}]

        with patch("app.database.SessionLocal", return_value=db_session):
            with patch.object(db_session, "close"):
                with patch("app.services.prospect_free_enrichment.enrich_from_sam_gov", new_callable=AsyncMock, return_value=sam_data):
                    with patch("app.services.prospect_free_enrichment.enrich_from_google_news", new_callable=AsyncMock, return_value=news_data):
                        result = asyncio.get_event_loop().run_until_complete(run_free_enrichment(pa.id))

        assert result["sam_gov"] is True
        assert result["news_count"] == 1
        db_session.refresh(pa)
        assert pa.naics_code == "336411"
        assert "sam_gov" in pa.enrichment_data
        assert "recent_news" in pa.enrichment_data

    def test_skips_existing_sam(self, db_session):
        from app.models.prospect_account import ProspectAccount
        pa = ProspectAccount(
            name="Existing", domain="existing.com",
            discovery_source="manual", status="suggested",
            enrichment_data={"sam_gov": {"source": "sam_gov"}},
        )
        db_session.add(pa)
        db_session.commit()

        with patch("app.database.SessionLocal", return_value=db_session):
            with patch.object(db_session, "close"):
                with patch("app.services.prospect_free_enrichment.enrich_from_sam_gov", new_callable=AsyncMock) as mock_sam:
                    with patch("app.services.prospect_free_enrichment.enrich_from_google_news", new_callable=AsyncMock, return_value=[]):
                        result = asyncio.get_event_loop().run_until_complete(run_free_enrichment(pa.id))
        mock_sam.assert_not_called()
        assert result["sam_gov"] is False

    def test_sam_returns_none(self, db_session):
        from app.models.prospect_account import ProspectAccount
        pa = ProspectAccount(
            name="NoSam", domain="nosam.com",
            discovery_source="manual", status="suggested",
            enrichment_data={},
        )
        db_session.add(pa)
        db_session.commit()

        with patch("app.database.SessionLocal", return_value=db_session):
            with patch.object(db_session, "close"):
                with patch("app.services.prospect_free_enrichment.enrich_from_sam_gov", new_callable=AsyncMock, return_value=None):
                    with patch("app.services.prospect_free_enrichment.enrich_from_google_news", new_callable=AsyncMock, return_value=[]):
                        result = asyncio.get_event_loop().run_until_complete(run_free_enrichment(pa.id))
        assert result["sam_gov"] is False
        assert result["news_count"] == 0

    def test_exception_returns_error(self, db_session):
        with patch("app.database.SessionLocal", return_value=db_session):
            with patch.object(db_session, "close"):
                with patch.object(db_session, "get", side_effect=Exception("db error")):
                    result = asyncio.get_event_loop().run_until_complete(run_free_enrichment(1))
        assert "error" in result

    def test_news_signals_merged(self, db_session):
        from app.models.prospect_account import ProspectAccount
        pa = ProspectAccount(
            name="SignalCo", domain="signal.com",
            discovery_source="manual", status="suggested",
            enrichment_data={}, readiness_signals={"events": []},
        )
        db_session.add(pa)
        db_session.commit()

        news = [
            {"title": "SignalCo wins $50M defense contract", "link": "http://x", "pub_date": "2026-01-01", "source": "Reuters", "signal_type": "contract"},
            {"title": "Quarterly earnings OK", "link": "http://y", "pub_date": "2026-01-02", "source": "WSJ", "signal_type": "general"},
        ]

        with patch("app.database.SessionLocal", return_value=db_session):
            with patch.object(db_session, "close"):
                with patch("app.services.prospect_free_enrichment.enrich_from_sam_gov", new_callable=AsyncMock, return_value=None):
                    with patch("app.services.prospect_free_enrichment.enrich_from_google_news", new_callable=AsyncMock, return_value=news):
                        result = asyncio.get_event_loop().run_until_complete(run_free_enrichment(pa.id))

        assert result["news_count"] == 2
        # After commit the readiness_signals should have been set
        # Re-query to get the committed value (SQLite JSONB mutation tracking)
        pa2 = db_session.get(ProspectAccount, pa.id)
        signals = pa2.readiness_signals or {}
        events = signals.get("events", [])
        assert len(events) == 1
        assert events[0]["type"] == "contract"

    def test_naics_not_overwritten_if_exists(self, db_session):
        from app.models.prospect_account import ProspectAccount
        pa = ProspectAccount(
            name="HasNaics", domain="hasnaics.com",
            discovery_source="manual", status="suggested",
            enrichment_data={}, naics_code="999999",
        )
        db_session.add(pa)
        db_session.commit()

        sam_data = {"source": "sam_gov", "naics_codes": [{"code": "111111", "primary": True}]}
        with patch("app.database.SessionLocal", return_value=db_session):
            with patch.object(db_session, "close"):
                with patch("app.services.prospect_free_enrichment.enrich_from_sam_gov", new_callable=AsyncMock, return_value=sam_data):
                    with patch("app.services.prospect_free_enrichment.enrich_from_google_news", new_callable=AsyncMock, return_value=[]):
                        asyncio.get_event_loop().run_until_complete(run_free_enrichment(pa.id))
        db_session.refresh(pa)
        assert pa.naics_code == "999999"

    def test_naics_picks_primary(self, db_session):
        from app.models.prospect_account import ProspectAccount
        pa = ProspectAccount(
            name="PrimaryNaics", domain="primary.com",
            discovery_source="manual", status="suggested",
            enrichment_data={},
        )
        db_session.add(pa)
        db_session.commit()

        sam_data = {
            "source": "sam_gov",
            "naics_codes": [
                {"code": "222222", "primary": False},
                {"code": "333333", "primary": True},
            ],
        }
        with patch("app.database.SessionLocal", return_value=db_session):
            with patch.object(db_session, "close"):
                with patch("app.services.prospect_free_enrichment.enrich_from_sam_gov", new_callable=AsyncMock, return_value=sam_data):
                    with patch("app.services.prospect_free_enrichment.enrich_from_google_news", new_callable=AsyncMock, return_value=[]):
                        asyncio.get_event_loop().run_until_complete(run_free_enrichment(pa.id))
        db_session.refresh(pa)
        assert pa.naics_code == "333333"

    def test_naics_falls_back_to_first(self, db_session):
        """When no naics_code is primary, falls back to first entry."""
        from app.models.prospect_account import ProspectAccount
        pa = ProspectAccount(
            name="FirstNaics", domain="first.com",
            discovery_source="manual", status="suggested",
            enrichment_data={},
        )
        db_session.add(pa)
        db_session.commit()

        sam_data = {
            "source": "sam_gov",
            "naics_codes": [
                {"code": "444444", "primary": False},
                {"code": "555555", "primary": False},
            ],
        }
        with patch("app.database.SessionLocal", return_value=db_session):
            with patch.object(db_session, "close"):
                with patch("app.services.prospect_free_enrichment.enrich_from_sam_gov", new_callable=AsyncMock, return_value=sam_data):
                    with patch("app.services.prospect_free_enrichment.enrich_from_google_news", new_callable=AsyncMock, return_value=[]):
                        asyncio.get_event_loop().run_until_complete(run_free_enrichment(pa.id))
        db_session.refresh(pa)
        assert pa.naics_code == "444444"


# ── run_free_enrichment_batch ────────────────────────────────────────


class TestRunFreeEnrichmentBatch:
    def test_processes_prospects(self, db_session):
        from app.models.prospect_account import ProspectAccount
        pa = ProspectAccount(
            name="BatchCo", domain="batch.com",
            discovery_source="manual", status="suggested", fit_score=60,
        )
        db_session.add(pa)
        db_session.commit()

        with patch("app.database.SessionLocal", return_value=db_session):
            with patch.object(db_session, "close"):
                with patch("app.services.prospect_free_enrichment.run_free_enrichment", new_callable=AsyncMock, return_value={"sam_gov": True, "news_count": 2}):
                    result = asyncio.get_event_loop().run_until_complete(run_free_enrichment_batch(min_fit_score=40))

        assert result["processed"] == 1
        assert result["sam_hits"] == 1
        assert result["news_hits"] == 1

    def test_counts_errors(self, db_session):
        from app.models.prospect_account import ProspectAccount
        pa = ProspectAccount(
            name="ErrorBatch", domain="errbatch.com",
            discovery_source="manual", status="suggested", fit_score=50,
        )
        db_session.add(pa)
        db_session.commit()

        with patch("app.database.SessionLocal", return_value=db_session):
            with patch.object(db_session, "close"):
                with patch("app.services.prospect_free_enrichment.run_free_enrichment", new_callable=AsyncMock, return_value={"error": "not_found"}):
                    result = asyncio.get_event_loop().run_until_complete(run_free_enrichment_batch(min_fit_score=40))

        assert result["errors"] == 1
        assert result["processed"] == 0

    def test_exception_in_single_enrichment(self, db_session):
        from app.models.prospect_account import ProspectAccount
        pa = ProspectAccount(
            name="ExcBatch", domain="excbatch.com",
            discovery_source="manual", status="suggested", fit_score=50,
        )
        db_session.add(pa)
        db_session.commit()

        with patch("app.database.SessionLocal", return_value=db_session):
            with patch.object(db_session, "close"):
                with patch("app.services.prospect_free_enrichment.run_free_enrichment", new_callable=AsyncMock, side_effect=Exception("boom")):
                    result = asyncio.get_event_loop().run_until_complete(run_free_enrichment_batch(min_fit_score=40))

        assert result["errors"] == 1

    def test_empty_pool(self, db_session):
        with patch("app.database.SessionLocal", return_value=db_session):
            with patch.object(db_session, "close"):
                result = asyncio.get_event_loop().run_until_complete(run_free_enrichment_batch(min_fit_score=99))
        assert result["processed"] == 0

    def test_no_news_hit(self, db_session):
        from app.models.prospect_account import ProspectAccount
        pa = ProspectAccount(
            name="NoNews", domain="nonews.com",
            discovery_source="manual", status="suggested", fit_score=80,
        )
        db_session.add(pa)
        db_session.commit()

        with patch("app.database.SessionLocal", return_value=db_session):
            with patch.object(db_session, "close"):
                with patch("app.services.prospect_free_enrichment.run_free_enrichment", new_callable=AsyncMock, return_value={"sam_gov": False, "news_count": 0}):
                    result = asyncio.get_event_loop().run_until_complete(run_free_enrichment_batch(min_fit_score=40))

        assert result["news_hits"] == 0
        assert result["sam_hits"] == 0
