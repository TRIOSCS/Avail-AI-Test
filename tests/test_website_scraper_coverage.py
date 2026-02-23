"""
test_website_scraper_coverage.py -- Additional coverage tests for website_scraper.py

Targets missing lines: 110-206 (scrape_vendor_websites function)
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from app.models import VendorCard, VendorContact
from app.services.website_scraper import (
    scrape_vendor_websites,
    _scrape_vendor,
    _fetch_page,
)


class TestFetchPage:
    @pytest.mark.asyncio
    async def test_success(self):
        client = AsyncMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "text/html"}
        resp.text = "<html>Hello</html>"
        client.get = AsyncMock(return_value=resp)
        result = await _fetch_page(client, "https://example.com")
        assert result == "<html>Hello</html>"

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self):
        client = AsyncMock()
        resp = MagicMock()
        resp.status_code = 404
        resp.headers = {"content-type": "text/html"}
        client.get = AsyncMock(return_value=resp)
        result = await _fetch_page(client, "https://example.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_non_text_content_type_returns_none(self):
        client = AsyncMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "application/json"}
        client.get = AsyncMock(return_value=resp)
        result = await _fetch_page(client, "https://example.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_exception_returns_none(self):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=Exception("Network error"))
        result = await _fetch_page(client, "https://example.com")
        assert result is None


class TestScrapeVendorWebsites:
    def _make_vendor(self, db_session, vid, website, name="vendor", num_contacts=0):
        """Create a vendor card with optional contacts in the real DB."""
        card = VendorCard(
            id=vid,
            normalized_name=f"{name}_{vid}",
            display_name=name.title(),
            website=website,
            is_blacklisted=False,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()
        for i in range(num_contacts):
            vc = VendorContact(
                vendor_card_id=card.id,
                email=f"contact{i}@{name}.com",
                source="manual",
            )
            db_session.add(vc)
        db_session.flush()
        return card

    @pytest.mark.asyncio
    async def test_no_vendors_returns_zero(self, db_session):
        """Lines 130-131: no vendors found."""
        result = await scrape_vendor_websites(db_session, max_vendors=10)
        assert result == {"vendors_scraped": 0, "emails_found": 0}

    @pytest.mark.asyncio
    async def test_scrapes_vendors_and_inserts_contacts(self, db_session):
        """Lines 140-195: full scrape path with email insertion."""
        self._make_vendor(db_session, 1001, "https://testvendor.com", "testvendor")

        scrape_results = [{"email": "sales@testvendor.com", "confidence": 70}]

        with patch("app.services.website_scraper._scrape_vendor", new_callable=AsyncMock, return_value=scrape_results), \
             patch("app.services.website_scraper.merge_emails_into_card"), \
             patch("app.services.website_scraper.asyncio.sleep", new_callable=AsyncMock):
            result = await scrape_vendor_websites(db_session, max_vendors=10)

        assert result["vendors_scraped"] == 1
        assert result["emails_found"] == 1
        # Verify the VendorContact was actually inserted
        contact = db_session.query(VendorContact).filter_by(email="sales@testvendor.com").first()
        assert contact is not None
        assert contact.source == "website_scrape"

    @pytest.mark.asyncio
    async def test_existing_contact_skipped(self, db_session):
        """Lines 174-176: existing contact not re-inserted."""
        card = self._make_vendor(db_session, 1002, "https://testvendor2.com", "testvendor2")
        # Add an existing contact with the same email we'll scrape
        existing = VendorContact(
            vendor_card_id=card.id,
            email="sales@testvendor2.com",
            source="manual",
        )
        db_session.add(existing)
        db_session.flush()

        scrape_results = [{"email": "sales@testvendor2.com", "confidence": 70}]

        with patch("app.services.website_scraper._scrape_vendor", new_callable=AsyncMock, return_value=scrape_results), \
             patch("app.services.website_scraper.merge_emails_into_card"), \
             patch("app.services.website_scraper.asyncio.sleep", new_callable=AsyncMock):
            result = await scrape_vendor_websites(db_session, max_vendors=10)

        assert result["vendors_scraped"] == 1
        assert result["emails_found"] == 0

    @pytest.mark.asyncio
    async def test_scrape_exception_skipped(self, db_session):
        """Lines 160-163: vendor scrape exception does not crash."""
        self._make_vendor(db_session, 1003, "https://testvendor3.com", "testvendor3")

        with patch("app.services.website_scraper._scrape_vendor", new_callable=AsyncMock, side_effect=Exception("Scrape failed")), \
             patch("app.services.website_scraper.asyncio.sleep", new_callable=AsyncMock):
            result = await scrape_vendor_websites(db_session, max_vendors=10)

        # Exception results in None from _scrape_one, so vendors_scraped stays 0
        assert result["vendors_scraped"] == 0

    @pytest.mark.asyncio
    async def test_no_website_skipped(self, db_session):
        """Lines 155-156: card with empty website results in None from _scrape_one."""
        # The query filters out NULL/empty websites, so this vendor won't be returned
        self._make_vendor(db_session, 1004, "", "nowebsite")
        result = await scrape_vendor_websites(db_session, max_vendors=10)
        assert result["vendors_scraped"] == 0

    @pytest.mark.asyncio
    async def test_empty_scrape_results(self, db_session):
        """Lines 166-167: vendor scraped but no emails found."""
        self._make_vendor(db_session, 1005, "https://testvendor5.com", "testvendor5")

        with patch("app.services.website_scraper._scrape_vendor", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.website_scraper.asyncio.sleep", new_callable=AsyncMock):
            result = await scrape_vendor_websites(db_session, max_vendors=10)

        assert result["vendors_scraped"] == 1
        assert result["emails_found"] == 0

    @pytest.mark.asyncio
    async def test_vendor_with_2_contacts_excluded(self, db_session):
        """Vendors with >= 2 email contacts are excluded from scraping."""
        self._make_vendor(db_session, 1006, "https://testvendor6.com", "testvendor6", num_contacts=2)
        result = await scrape_vendor_websites(db_session, max_vendors=10)
        assert result["vendors_scraped"] == 0

    @pytest.mark.asyncio
    async def test_final_commit_failure_rollback(self, db_session):
        """Lines 194-196: final commit fails -> rollback."""
        self._make_vendor(db_session, 1007, "https://testvendor7.com", "testvendor7")

        original_commit = db_session.commit
        original_rollback = db_session.rollback
        commit_count = [0]
        rollback_called = [False]

        def fail_commit():
            commit_count[0] += 1
            raise Exception("Final commit failed")

        def track_rollback():
            rollback_called[0] = True
            original_rollback()

        with patch("app.services.website_scraper._scrape_vendor", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.website_scraper.asyncio.sleep", new_callable=AsyncMock):
            db_session.commit = fail_commit
            db_session.rollback = track_rollback
            result = await scrape_vendor_websites(db_session, max_vendors=10)

        assert rollback_called[0] is True
        db_session.commit = original_commit
        db_session.rollback = original_rollback

    @pytest.mark.asyncio
    async def test_periodic_commit_on_50th_vendor(self, db_session):
        """Lines 187-191: periodic commit every 50 vendors."""
        for i in range(50):
            self._make_vendor(db_session, 2000 + i, f"https://vendor{i}.example.com", f"vendor{i}")

        scrape_results = [{"email": "a@b.com", "confidence": 55}]
        commit_count = [0]
        original_commit = db_session.commit

        def count_commit():
            commit_count[0] += 1
            original_commit()

        db_session.commit = count_commit

        with patch("app.services.website_scraper._scrape_vendor", new_callable=AsyncMock, return_value=scrape_results), \
             patch("app.services.website_scraper.merge_emails_into_card"), \
             patch("app.services.website_scraper.asyncio.sleep", new_callable=AsyncMock):
            result = await scrape_vendor_websites(db_session, max_vendors=500)

        assert result["vendors_scraped"] == 50
        # At least 2 commits: one periodic at 50th, one final
        assert commit_count[0] >= 2
        db_session.commit = original_commit

    @pytest.mark.asyncio
    async def test_periodic_commit_failure_rollback(self, db_session):
        """Lines 189-191: periodic commit fails -> rollback."""
        for i in range(50):
            self._make_vendor(db_session, 3000 + i, f"https://pvendor{i}.example.com", f"pvendor{i}")

        scrape_results = [{"email": "x@y.com", "confidence": 55}]
        original_commit = db_session.commit
        original_rollback = db_session.rollback
        commit_count = [0]
        rollback_called = [False]

        def failing_commit():
            commit_count[0] += 1
            if commit_count[0] == 1:
                raise Exception("Periodic commit failed")
            original_commit()

        def track_rollback():
            rollback_called[0] = True
            original_rollback()

        db_session.commit = failing_commit
        db_session.rollback = track_rollback

        with patch("app.services.website_scraper._scrape_vendor", new_callable=AsyncMock, return_value=scrape_results), \
             patch("app.services.website_scraper.merge_emails_into_card"), \
             patch("app.services.website_scraper.asyncio.sleep", new_callable=AsyncMock):
            result = await scrape_vendor_websites(db_session, max_vendors=500)

        assert rollback_called[0] is True
        db_session.commit = original_commit
        db_session.rollback = original_rollback
    @pytest.mark.asyncio
    async def test_card_website_none_at_runtime(self, db_session):
        """Line 146: card.website is falsy at runtime -> return None from _scrape_one."""
        # Create a vendor card in DB with a real website
        card = self._make_vendor(db_session, 1008, "https://testvendor8.com", "testvendor8")
        # Expunge and null out website to simulate a card with no website at runtime
        db_session.expunge(card)
        card.website = None

        original_query = db_session.query

        def patched_query(*args, **kwargs):
            q = original_query(*args, **kwargs)
            if args and args[0] is VendorCard:
                mock_chain = MagicMock()
                mock_chain.outerjoin.return_value.filter.return_value.filter.return_value.limit.return_value.all.return_value = [card]
                return mock_chain
            return q

        db_session.query = patched_query

        with patch("app.services.website_scraper.asyncio.sleep", new_callable=AsyncMock):
            result = await scrape_vendor_websites(db_session, max_vendors=10)

        db_session.query = original_query
        # Card with None website gets skipped -> vendors_scraped stays 0
        assert result["vendors_scraped"] == 0

