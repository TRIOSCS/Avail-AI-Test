"""Tests for the tbf_worker package (The Broker Forum browser worker).

Covers:
- TbfConfig env defaults + overrides
- queue_manager: enqueue, compound (requirement_id, normalized_mpn) dedup, claim atomicity
- save_tbf_sightings: synthetic TbfSighting list -> Sighting rows (source_type='thebrokersite'),
  dedup, apply_to_fresh_sightings gating
- tbf_worker_status singleton seed (startup) + worker.update_worker_status
- result_parser: real captured thebrokersite.com results HTML (Phase-2 fixtures) ->
  TbfSighting rows, including price/currency parsing, CALL rows, and malformed-row skip

Patchright/network are never touched here — the worker package imports cleanly and the
browser-only modules (session_manager, search_engine) are asserted by import + signature.

Called by: pytest
Depends on: conftest.py, tbf_worker modules, tests/fixtures/tbf_results_*.html
"""

import inspect
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import Sighting, TbfSearchQueue, TbfWorkerStatus
from app.services.tbf_worker.config import TbfConfig
from app.services.tbf_worker.queue_manager import (
    claim_next_queued_item,
    enqueue_for_tbf_search,
    get_next_queued_item,
    get_queue_stats,
    mark_completed,
    mark_status,
    recover_stale_searches,
)
from app.services.tbf_worker.result_parser import TbfSighting, parse_results_html
from app.services.tbf_worker.sighting_writer import save_tbf_sightings

_FIXTURES = Path(__file__).parent / "fixtures"


def _read_fixture(name: str) -> str:
    return (_FIXTURES / name).read_text()


# ═══════════════════════════════════════════════════════════════════════
# IMPORTABILITY — the whole worker package must import without a browser/net
# ═══════════════════════════════════════════════════════════════════════


class TestImportability:
    def test_worker_module_imports(self):
        """worker.main() and its lazy deps import cleanly (no Patchright at import)."""
        import app.services.tbf_worker.worker as worker_mod

        assert callable(worker_mod.main)

    def test_session_manager_imports(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        assert TbfSessionManager is not None

    def test_search_engine_imports(self):
        from app.services.tbf_worker.search_engine import search_part

        assert callable(search_part)

    def test_circuit_breaker_imports(self):
        from app.services.tbf_worker.circuit_breaker import CircuitBreaker

        # Defaults to base self-healing behavior; no markers needed to construct.
        assert CircuitBreaker().should_stop() is False

    def test_parser_empty_inputs_return_empty(self):
        """Parser returns [] for empty/None and tables without data rows."""
        # A table whose rows are NOT tr.hover-higlight-anchor -> no data rows.
        assert parse_results_html("<table><tr><td>x</td></tr></table>") == []
        assert parse_results_html("") == []
        assert parse_results_html(None) == []
        # HTML with content but no results table at all.
        assert parse_results_html("<html><body><div>No results</div></body></html>") == []


# ═══════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════


class TestTbfConfig:
    def test_defaults(self):
        cfg = TbfConfig()
        assert cfg.TBF_MAX_DAILY_SEARCHES == 50
        assert cfg.TBF_MAX_HOURLY_SEARCHES == 10
        assert cfg.TBF_MIN_DELAY_SECONDS == 180
        assert cfg.TBF_MAX_DELAY_SECONDS == 600
        assert cfg.TBF_TYPICAL_DELAY_SECONDS == 300
        assert cfg.TBF_DEDUP_WINDOW_DAYS == 7
        assert cfg.TBF_BUSINESS_HOURS_START == 8
        assert cfg.TBF_BUSINESS_HOURS_END == 18
        assert cfg.TBF_BROWSER_PROFILE_DIR == "/root/tbf_browser_profile"
        assert cfg.TBF_SEARCH_TIMEOUT_SECONDS == 150
        assert cfg.TBF_BREAKER_COOLDOWN_MINUTES == 30
        # No account number — member login only.
        assert cfg.TBF_USERNAME == ""
        assert cfg.TBF_PASSWORD == ""

    def test_env_override(self):
        with patch.dict(os.environ, {"TBF_MAX_DAILY_SEARCHES": "25", "TBF_USERNAME": "member@trio.com"}):
            cfg = TbfConfig()
            assert cfg.TBF_MAX_DAILY_SEARCHES == 25
            assert cfg.TBF_USERNAME == "member@trio.com"


# ═══════════════════════════════════════════════════════════════════════
# QUEUE MANAGER — enqueue, compound dedup, claim atomicity
# ═══════════════════════════════════════════════════════════════════════


class TestQueueManager:
    def test_enqueue_no_requirement(self, db_session):
        assert enqueue_for_tbf_search(99999, db_session) is None

    def test_enqueue_no_mpn(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        req.primary_mpn = None
        db_session.commit()
        assert enqueue_for_tbf_search(req.id, db_session) is None

    def test_enqueue_success(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        item = enqueue_for_tbf_search(req.id, db_session)
        assert item is not None
        assert item.mpn == "LM317T"
        assert item.normalized_mpn == "LM317T"
        assert item.status == "pending"

    def test_enqueue_already_queued_returns_existing(self, db_session, test_requisition):
        """Same (requirement, mpn) re-enqueue returns the existing row (idempotent)."""
        req = test_requisition.requirements[0]
        item1 = enqueue_for_tbf_search(req.id, db_session)
        item2 = enqueue_for_tbf_search(req.id, db_session)
        assert item1.id == item2.id

    def test_compound_dedup_allows_distinct_mpn_same_requirement(self, db_session, test_requisition):
        """The COMPOUND (requirement_id, normalized_mpn) key lets one requirement hold
        multiple queue rows — a resolver-driven AVL MPN coexists with the primary."""
        req = test_requisition.requirements[0]
        primary = enqueue_for_tbf_search(req.id, db_session)
        avl = enqueue_for_tbf_search(req.id, db_session, override_mpn="AVL-SUB-9000", resolved_via_spec_code="SPEC1")
        assert primary is not None
        assert avl is not None
        assert primary.id != avl.id
        assert avl.normalized_mpn == "AVL-SUB-9000"
        assert avl.resolved_via_spec_code == "SPEC1"

    def test_compound_dedup_same_mpn_same_requirement_dedups(self, db_session, test_requisition):
        """Same normalized MPN for the same requirement does NOT create a second row."""
        req = test_requisition.requirements[0]
        first = enqueue_for_tbf_search(req.id, db_session)
        # Packaging suffix normalizes to the same MPN — must dedup to the same row.
        second = enqueue_for_tbf_search(req.id, db_session, override_mpn="LM317T/TR")
        assert second is not None
        assert second.id == first.id

    def test_claim_atomicity_marks_searching_and_single_winner(self, db_session, test_requisition):
        """claim_next_queued_item marks the row 'searching'; a second claim gets
        None."""
        req = test_requisition.requirements[0]
        item = TbfSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="queued",
        )
        db_session.add(item)
        db_session.commit()

        claimed = claim_next_queued_item(db_session)
        assert claimed is not None
        assert claimed.id == item.id
        assert claimed.status == "searching"

        # Nothing left to claim — the only row is now 'searching'.
        assert claim_next_queued_item(db_session) is None

    def test_recover_stale_searches(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        item = TbfSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="searching",
        )
        db_session.add(item)
        db_session.commit()

        assert recover_stale_searches(db_session) == 1
        db_session.refresh(item)
        assert item.status == "queued"

    def test_get_next_queued_item(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        item = TbfSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="queued",
        )
        db_session.add(item)
        db_session.commit()
        assert get_next_queued_item(db_session).id == item.id

    def test_mark_status_and_completed(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        item = TbfSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="queued",
        )
        db_session.add(item)
        db_session.commit()

        mark_status(db_session, item, "failed", error="boom")
        db_session.refresh(item)
        assert item.status == "failed"
        assert item.error_message == "boom"

        mark_completed(db_session, item, results_found=4, sightings_created=2)
        db_session.refresh(item)
        assert item.status == "completed"
        assert item.results_count == 4
        assert item.search_count == 1
        assert item.last_searched_at is not None

    def test_get_queue_stats(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        db_session.add(
            TbfSearchQueue(
                requirement_id=req.id,
                requisition_id=test_requisition.id,
                mpn="LM317T",
                normalized_mpn="LM317T",
                status="queued",
            )
        )
        db_session.commit()
        stats = get_queue_stats(db_session)
        assert stats["queued"] == 1
        assert stats["remaining"] == 1
        assert "completed" in stats


# ═══════════════════════════════════════════════════════════════════════
# SIGHTING WRITER — synthetic TbfSighting list -> Sighting rows
# ═══════════════════════════════════════════════════════════════════════


def _queue_item(db_session, test_requisition):
    req = test_requisition.requirements[0]
    item = TbfSearchQueue(
        requirement_id=req.id,
        requisition_id=test_requisition.id,
        mpn="LM317T",
        normalized_mpn="LM317T",
        status="searching",
    )
    db_session.add(item)
    db_session.commit()
    return item


class TestSightingWriter:
    def test_requirement_not_found(self, db_session):
        queue_item = MagicMock()
        queue_item.requirement_id = 99999
        assert save_tbf_sightings(db_session, queue_item, []) == 0

    def test_empty_list_creates_nothing(self, db_session, test_requisition):
        item = _queue_item(db_session, test_requisition)
        assert save_tbf_sightings(db_session, item, []) == 0

    def test_creates_sightings_with_source_type(self, db_session, test_requisition):
        item = _queue_item(db_session, test_requisition)
        tbf = [
            TbfSighting(
                part_number="LM317T",
                manufacturer="TI",
                vendor_name="Euro Broker GmbH",
                vendor_email="sales@eurobroker.de",
                vendor_phone="+49 30 123456",
                quantity=500,
                price="EUR 1.20",
                currency="EUR",
                country="DE",
                region="Europe",
                vendor_company_id="TBF-777",
                supplier_product_url="https://www.thebrokersite.com/listing/777",
                uploaded_date="06/20/2026",
                in_stock=True,
                is_authorized=True,
            )
        ]
        created = save_tbf_sightings(db_session, item, tbf)
        assert created == 1

        s = db_session.query(Sighting).filter(Sighting.source_type == "thebrokersite").one()
        assert s.source_type == "thebrokersite"
        assert s.vendor_email == "sales@eurobroker.de"
        assert s.vendor_phone == "+49 30 123456"
        assert s.currency == "EUR"
        assert s.confidence == 0.6  # in_stock
        assert s.is_authorized is True
        assert s.raw_data["region"] == "Europe"
        assert s.raw_data["country"] == "DE"
        assert s.raw_data["vendor_company_id"] == "TBF-777"
        assert s.raw_data["supplier_product_url"] == "https://www.thebrokersite.com/listing/777"
        assert s.raw_data["inventory_type"] == "in_stock"

    def test_confidence_brokered(self, db_session, test_requisition):
        item = _queue_item(db_session, test_requisition)
        tbf = [TbfSighting(part_number="LM317T", vendor_name="Broker X", quantity=10, in_stock=False)]
        save_tbf_sightings(db_session, item, tbf)
        s = db_session.query(Sighting).filter(Sighting.source_type == "thebrokersite").one()
        assert s.confidence == 0.3
        assert s.raw_data["inventory_type"] == "brokered"

    def test_skips_rows_without_vendor(self, db_session, test_requisition):
        item = _queue_item(db_session, test_requisition)
        tbf = [TbfSighting(part_number="LM317T", vendor_name="", quantity=5)]
        assert save_tbf_sightings(db_session, item, tbf) == 0

    def test_dedup_within_batch_and_existing(self, db_session, test_requisition):
        item = _queue_item(db_session, test_requisition)
        dup = TbfSighting(part_number="LM317T", vendor_name="Acme Brokers", quantity=100, in_stock=True)
        # Two identical rows in one batch -> one created (intra-batch dedup).
        assert save_tbf_sightings(db_session, item, [dup, dup]) == 1
        # Same (vendor, mpn, qty) again -> deduped against the existing row.
        assert save_tbf_sightings(db_session, item, [dup]) == 0
        assert db_session.query(Sighting).filter(Sighting.source_type == "thebrokersite").count() == 1

    def test_apply_to_fresh_sightings_gating_invoked(self, db_session, test_requisition):
        """save_tbf_sightings runs durable-unavailability gating on the created rows."""
        item = _queue_item(db_session, test_requisition)
        tbf = [TbfSighting(part_number="LM317T", vendor_name="Gated Vendor", quantity=50, in_stock=True)]
        with patch("app.services.tbf_worker.sighting_writer.apply_to_fresh_sightings") as mock_gate:
            created = save_tbf_sightings(db_session, item, tbf)
        assert created == 1
        mock_gate.assert_called_once()
        # The created rows (not an empty list) are passed to the gate.
        passed_rows = mock_gate.call_args[0][2]
        assert len(passed_rows) == 1


# ═══════════════════════════════════════════════════════════════════════
# WORKER STATUS SINGLETON
# ═══════════════════════════════════════════════════════════════════════


class TestWorkerStatusSingleton:
    def test_seed_singleton_idempotent(self, db_session):
        """startup.seed_tbf_worker_status_singleton inserts id=1 once, idempotently."""
        from app.startup import seed_tbf_worker_status_singleton

        assert db_session.query(TbfWorkerStatus).filter_by(id=1).one_or_none() is None
        seed_tbf_worker_status_singleton(db_session)
        db_session.commit()
        row = db_session.query(TbfWorkerStatus).filter_by(id=1).one()
        assert row.is_running is False

        # Second call is a no-op (no duplicate, no error).
        seed_tbf_worker_status_singleton(db_session)
        db_session.commit()
        assert db_session.query(TbfWorkerStatus).filter_by(id=1).count() == 1

    def test_update_worker_status(self, db_session):
        from app.services.tbf_worker.worker import update_worker_status

        db_session.add(TbfWorkerStatus(id=1, is_running=False, searches_today=0))
        db_session.commit()

        update_worker_status(db_session, is_running=True, searches_today=7)
        row = db_session.query(TbfWorkerStatus).filter_by(id=1).one()
        assert row.is_running is True
        assert row.searches_today == 7

    def test_update_worker_status_no_row_is_noop(self, db_session):
        from app.services.tbf_worker.worker import update_worker_status

        # No singleton row -> silent no-op (does not raise).
        update_worker_status(db_session, is_running=True)

    def test_record_heartbeat_advances_timestamp(self, db_session):
        from datetime import timedelta

        from app.services.tbf_worker.worker import _record_heartbeat

        stale = datetime.now(timezone.utc) - timedelta(hours=1)
        db_session.add(TbfWorkerStatus(id=1, is_running=False, last_heartbeat=stale))
        db_session.commit()

        _record_heartbeat(db_session)
        row = db_session.query(TbfWorkerStatus).filter_by(id=1).one()
        assert row.is_running is True
        assert row.last_heartbeat > stale


# ═══════════════════════════════════════════════════════════════════════
# RESULT PARSER — real thebrokersite.com results HTML (captured fixtures)
# ═══════════════════════════════════════════════════════════════════════


class TestResultParser:
    def test_parses_ddr4_fixture_into_ten_sightings(self):
        """The captured DDR4 results <table> yields exactly 10 TbfSighting."""
        sightings = parse_results_html(_read_fixture("tbf_results_ddr4.html"))
        assert len(sightings) == 10
        assert all(isinstance(s, TbfSighting) for s in sightings)

    def test_first_row_fields(self):
        """First DDR4 row: part#, manufacturer, quantity, vendor, country."""
        sightings = parse_results_html(_read_fixture("tbf_results_ddr4.html"))
        first = sightings[0]
        assert first.part_number == "4GBDDR4"
        assert first.manufacturer == "Major Brand"
        assert first.quantity == 18
        assert isinstance(first.quantity, int)
        # Logged-OUT fixture: vendor anonymized, no phone line.
        assert first.vendor_name == "TBS Member"
        assert first.vendor_phone == ""
        assert first.country == "GR"
        # No region mapping available — region falls back to the country code.
        assert first.region == "GR"
        # Active broker-marketplace listing.
        assert first.in_stock is True
        assert first.is_authorized is False

    def test_first_row_condition_is_ref(self):
        """The first row's condition cell (td[3]) is 'REF' — verified against the raw
        table since TbfSighting has no condition field (dropped per contract)."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(_read_fixture("tbf_results_ddr4.html"), "html.parser")
        first_row = soup.select_one("table.table-fixed tr.hover-higlight-anchor")
        condition = first_row.find_all("td")[3].get_text(strip=True)
        assert condition == "REF"

    def test_priced_row_has_currency_and_numeric_price(self):
        """A '€ 114'-style row yields currency 'EUR' + a numeric price string."""
        sightings = parse_results_html(_read_fixture("tbf_results_ddr4.html"))
        priced = [s for s in sightings if s.currency]
        assert priced, "expected at least one priced (non-CALL) row"
        for s in priced:
            assert s.currency == "EUR"
            assert s.price  # non-empty numeric string
            assert float(s.price) > 0

    def test_call_row_has_no_numeric_price(self):
        """A 'CALL' price row yields no numeric price and no currency."""
        sightings = parse_results_html(_read_fixture("tbf_results_ddr4.html"))
        call_rows = [s for s in sightings if not s.currency]
        assert call_rows, "expected at least one CALL row"
        for s in call_rows:
            assert s.price == ""
            assert s.currency == ""

    def test_countries_are_iso_codes(self):
        sightings = parse_results_html(_read_fixture("tbf_results_ddr4.html"))
        countries = {s.country for s in sightings}
        assert "GR" in countries
        assert "US" in countries

    def test_logged_out_fixture_anonymizes_vendor(self):
        """Logged OUT, TBF anonymizes every seller to 'TBS Member' with no phone — the
        parser captures that faithfully (the worker's session check / circuit breaker
        are what keep it logged in so this is the exception, not the norm)."""
        sightings = parse_results_html(_read_fixture("tbf_results_ddr4.html"))
        for s in sightings:
            assert s.vendor_name == "TBS Member"
            assert s.vendor_email == ""
            assert s.vendor_phone == ""
            assert s.vendor_company_id == ""

    def test_logged_in_fixture_reveals_real_vendor_and_phone(self):
        """Logged IN, td[6] carries the real company name (cell title / first div) plus
        a phone number (second div) — the parser splits them into vendor_name +
        vendor_phone instead of mashing them together."""
        sightings = parse_results_html(_read_fixture("tbf_results_loggedin_ddr4.html"))
        assert len(sightings) == 10
        # No row is the anonymized placeholder, and every seller has a real name.
        assert all(s.vendor_name and s.vendor_name != "TBS Member" for s in sightings)
        first = sightings[0]
        assert first.vendor_name == "D. Gerasis - K. Karpouzas G.P. Rename"
        assert first.vendor_phone == "+30 2492024777"
        # The name must not have the phone digits mashed onto it.
        assert "+" not in first.vendor_name
        # A US seller's name + phone are also split cleanly.
        dynamic = next(s for s in sightings if s.vendor_name == "Dynamic Lifecycle Innovations LLC")
        assert dynamic.vendor_phone == "+1 6087814030"
        # Contract: even authenticated, td[6] yields only name+phone — email and
        # company_id stay empty (those identities are still behind a row click).
        for s in sightings:
            assert s.vendor_email == ""
            assert s.vendor_company_id == ""

    def test_title_absent_falls_back_to_first_div_name(self):
        """If TBF drops the cell `title` attr, vendor_name must still come from the
        first <div> (a real company name), with the phone from the second div."""
        html = (
            "<table><tr class='hover-higlight-anchor'>"
            "<td><div class='text-red-600'>17P9905</div><div title='d'>d</div></td>"
            "<td>IBM</td><td>5</td><td>NEW</td><td><span>CALL</span></td><td>Gold</td>"
            "<td><div>Acme Components Ltd</div><div> +44 2071234567 </div></td><td>GB</td>"
            "</tr></table>"
        )
        s = parse_results_html(html)[0]
        assert s.vendor_name == "Acme Components Ltd"  # first div, no title attr
        assert s.vendor_phone == "+44 2071234567"

    def test_phone_only_company_cell_does_not_leak_phone_as_vendor_name(self):
        """A nameless seller (no company set) renders only a phone in td[6].

        That phone must NOT become the vendor identity — it would corrupt vendor
        matching/dedup downstream (sighting_writer normalizes vendor_name into the dedup
        key). The parser leaves vendor_name empty (so sighting_writer's no-vendor guard
        skips the row) and recovers the phone into vendor_phone.
        """
        html = (
            "<table><tr class='hover-higlight-anchor'>"
            "<td><div class='text-red-600'>17P9905</div><div title='d'>d</div></td>"
            "<td>IBM</td><td>50</td><td>NEW</td><td><span>CALL</span></td><td>Gold</td>"
            "<td><div> +44 2071234567 </div></td><td>GB</td>"
            "</tr></table>"
        )
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        s = sightings[0]
        assert s.vendor_name == "", "a phone must never be used as the vendor name"
        assert s.vendor_phone == "+44 2071234567", "the phone should be recovered, not dropped"

    def test_no_results_fixture_returns_empty(self):
        assert parse_results_html(_read_fixture("tbf_results_none.html")) == []

    def test_malformed_row_is_skipped_not_raised(self):
        """A data row missing cells is skipped defensively — never raises."""
        html = (
            "<table class='table-fixed'><tbody>"
            "<tr class='hover-higlight-anchor'><td>only one cell</td></tr>"
            "</tbody></table>"
        )
        assert parse_results_html(html) == []


# ═══════════════════════════════════════════════════════════════════════
# INTEGRATION-ONLY MODULES — assert import + signature (no browser / no net)
# ═══════════════════════════════════════════════════════════════════════


class TestBrowserModuleContracts:
    def test_search_part_signature(self):
        """search_part(page, part_number) -> coroutine — browser-only at runtime."""
        from app.services.tbf_worker.search_engine import search_part

        assert inspect.iscoroutinefunction(search_part)
        params = list(inspect.signature(search_part).parameters)
        assert params == ["page", "part_number"]

    def test_session_manager_signature(self):
        """TbfSessionManager exposes the browser lifecycle the worker loop calls."""
        from app.services.tbf_worker.session_manager import TbfSessionManager

        session = TbfSessionManager(TbfConfig())
        assert session.is_logged_in is False
        for name in ("start", "login", "check_session_health", "ensure_session", "stop"):
            assert inspect.iscoroutinefunction(getattr(session, name))

    def test_circuit_breaker_check_page_health_is_coroutine(self):
        from app.services.tbf_worker.circuit_breaker import CircuitBreaker

        assert inspect.iscoroutinefunction(CircuitBreaker().check_page_health)


# ═══════════════════════════════════════════════════════════════════════
# SESSION / BREAKER LOGIN-STATE DETECTION
#
# Regression guard for the original bug: the code keyed on "TBS Member" (the
# logged-OUT company label) as a logged-IN marker, so both the session-health
# check and the circuit breaker were inverted. The fix uses a POSITIVE,
# fail-safe marker: logged-in == the "Sign out" control is present.
# ═══════════════════════════════════════════════════════════════════════


def _mock_page(marker_count: int, url: str = "https://www.thebrokersite.com/parts?query=x"):
    """Mock Patchright page whose ``locator(...).count()`` returns marker_count (the
    LOGGED_IN_MARKER 'Sign out' control count) and whose evaluate() succeeds."""
    page = MagicMock()
    page.url = url
    page.evaluate = AsyncMock(return_value="body text")
    locator = MagicMock()
    locator.count = AsyncMock(return_value=marker_count)
    page.locator = MagicMock(return_value=locator)
    return page


class TestSessionHealthMarker:
    def _session(self, marker_count: int):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        session = TbfSessionManager(TbfConfig())
        session._page = _mock_page(marker_count)
        return session

    async def test_logged_in_when_signout_marker_present(self):
        """'Sign out' control present == authenticated."""
        assert await self._session(1).check_session_health() is True

    async def test_logged_out_when_signout_marker_absent(self):
        """No 'Sign out' control == logged out (fail-safe; old code returned True)."""
        assert await self._session(0).check_session_health() is False

    async def test_locator_error_reads_as_logged_out(self):
        """A locator exception must fail SAFE (logged out → re-login), not raise."""
        from app.services.tbf_worker.session_manager import TbfSessionManager

        session = TbfSessionManager(TbfConfig())
        page = MagicMock()
        loc = MagicMock()
        loc.count = AsyncMock(side_effect=RuntimeError("boom"))
        page.locator = MagicMock(return_value=loc)
        session._page = page
        assert await session.check_session_health() is False


class TestCircuitBreakerSessionDetection:
    async def test_session_expired_when_marker_absent(self):
        from app.services.tbf_worker.circuit_breaker import CircuitBreaker

        assert await CircuitBreaker().check_page_health(_mock_page(0)) == "SESSION_EXPIRED"

    async def test_healthy_when_marker_present(self):
        from app.services.tbf_worker.circuit_breaker import CircuitBreaker

        assert await CircuitBreaker().check_page_health(_mock_page(1)) == "HEALTHY"

    async def test_unexpected_redirect_off_domain_trips(self):
        from app.services.tbf_worker.circuit_breaker import CircuitBreaker

        page = _mock_page(1, url="https://evil.example.com/phish")
        assert await CircuitBreaker().check_page_health(page) == "UNEXPECTED_REDIRECT"

    async def test_check_failed_when_evaluate_raises_and_trips_after_three(self):
        """A hung/broken page (evaluate raises) → CHECK_FAILED, tripping after 3."""
        from app.services.tbf_worker.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker()
        page = MagicMock()
        page.url = "https://www.thebrokersite.com/parts?query=x"
        page.evaluate = AsyncMock(side_effect=RuntimeError("hung"))
        assert await cb.check_page_health(page) == "CHECK_FAILED"
        assert cb.consecutive_failures == 1
        await cb.check_page_health(page)
        await cb.check_page_health(page)
        assert cb.should_stop() is True

    async def test_locator_error_reads_as_session_expired(self):
        """A locator exception must fail SAFE (re-login), never HEALTHY."""
        from app.services.tbf_worker.circuit_breaker import CircuitBreaker

        page = MagicMock()
        page.url = "https://www.thebrokersite.com/parts?query=x"
        page.evaluate = AsyncMock(return_value="body")
        loc = MagicMock()
        loc.count = AsyncMock(side_effect=RuntimeError("boom"))
        page.locator = MagicMock(return_value=loc)
        assert await CircuitBreaker().check_page_health(page) == "SESSION_EXPIRED"


class TestLoginMarkerAgainstRealDom:
    """Pin LOGGED_IN_MARKER's text against real captured DOM so a label/scope drift on
    the Vue SPA fails CI instead of silently regressing to anonymized results."""

    @staticmethod
    def _signout_controls(fixture: str) -> int:
        # Mirror LOGGED_IN_MARKER = "a:has-text('Sign out'), button:has-text('Sign out')".
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(_read_fixture(fixture), "html.parser")
        return sum(1 for el in soup.find_all(["a", "button"]) if "sign out" in el.get_text(strip=True).lower())

    def test_marker_present_on_real_logged_in_page(self):
        assert self._signout_controls("tbf_results_loggedin_ddr4.html") > 0

    def test_marker_absent_on_real_logged_out_page(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(_read_fixture("tbf_page_loggedout.html"), "html.parser")
        signin = sum(1 for el in soup.find_all(["a", "button"]) if "sign in" in el.get_text(strip=True).lower())
        assert signin > 0, "logged-out fixture must carry a Sign In control (sanity)"
        assert self._signout_controls("tbf_page_loggedout.html") == 0

    def test_marker_constant_targets_signout_not_tbs_member(self):
        from app.services.tbf_worker.session_manager import LOGGED_IN_MARKER

        assert "Sign out" in LOGGED_IN_MARKER
        assert "TBS Member" not in LOGGED_IN_MARKER


def _login_mock_page(logged_in: bool, code_present: bool = False):
    """Mock page that routes locator(selector) by what login()/check_session_health
    query: the 'Sign out' marker and the 2FA code field get scripted counts; all
    other selectors return a chainable, clickable/fillable locator. Every selector
    (top-level and nested form.locator) is recorded on ``page._selectors``."""
    page = MagicMock()
    page._selectors = []
    page.url = "https://www.thebrokersite.com/"
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    # No consent banner.
    consent = MagicMock()
    consent.count = AsyncMock(return_value=0)
    consent.first = MagicMock()
    consent.first.is_visible = AsyncMock(return_value=False)
    page.get_by_role = MagicMock(return_value=consent)

    def _locator(selector):
        page._selectors.append(selector)
        loc = MagicMock()
        loc.first = MagicMock()
        loc.first.click = AsyncMock()
        loc.click = AsyncMock()
        loc.fill = AsyncMock()
        loc.wait_for = AsyncMock()
        loc.locator = MagicMock(side_effect=_locator)
        if "Sign out" in selector:
            loc.count = AsyncMock(return_value=1 if logged_in else 0)
        elif "code" in selector:
            loc.count = AsyncMock(return_value=1 if code_present else 0)
        else:
            loc.count = AsyncMock(return_value=1)
        return loc

    page.locator = MagicMock(side_effect=_locator)
    return page


class TestLoginFlow:
    @pytest.fixture(autouse=True)
    def _no_real_sleep(self):
        """Login() has fixed asyncio.sleep settles — stub them so tests stay fast."""
        with patch("app.services.tbf_worker.session_manager.asyncio.sleep", new=AsyncMock()):
            yield

    def _session(self, page, username="member@trio.com", password="pw"):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        cfg = TbfConfig()
        cfg.TBF_USERNAME = username
        cfg.TBF_PASSWORD = password
        s = TbfSessionManager(cfg)
        s._page = page
        return s

    async def test_returns_false_when_creds_unset_without_touching_page(self):
        page = _login_mock_page(logged_in=True)
        s = self._session(page, username="", password="")
        assert await s.login() is False
        page.goto.assert_not_called()

    async def test_success_when_marker_present_after_submit(self):
        page = _login_mock_page(logged_in=True)
        s = self._session(page)
        assert await s.login() is True
        assert s.is_logged_in is True
        # The form's submit control was used (not the nav 'Sign In' toggle).
        assert any("button[type=submit]" in sel for sel in page._selectors)

    async def test_failure_when_marker_absent_and_no_2fa(self):
        page = _login_mock_page(logged_in=False, code_present=False)
        s = self._session(page)
        assert await s.login() is False
        assert s.is_logged_in is False

    async def test_detects_2fa_block(self):
        page = _login_mock_page(logged_in=False, code_present=True)
        s = self._session(page)
        assert await s.login() is False
        assert any("code" in sel for sel in page._selectors)
