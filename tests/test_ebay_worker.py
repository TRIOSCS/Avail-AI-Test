"""Tests for the ebay_worker package (eBay Browse API poller).

Covers:
- EbayConfig env defaults + overrides
- result_parser: strict MPN filter (incl. the Dell leading-zero variant and a
  negative case), condition mapping, auction / for-parts skipping, confidence
  range + rank order
- search_client: request params (marketplace, buying-option filter, category ids)
- queue_manager: enqueue, compound (requirement_id, normalized_mpn) dedup, claim
- save_ebay_sightings: EbaySighting list -> Sighting rows (source_type='ebay'),
  condition set, click_url in raw_data, dedup by ebay_item_id,
  apply_to_fresh_sightings gating
- scheduler: daily call budget + midnight-UTC rollover
- worker: status singleton seed, budget bookkeeping, and budget exhaustion
  making the loop sleep instead of calling the API
- wiring: _worker_enqueues includes EBAY, _build_connectors no longer builds
  EbayConnector while connector_registry still resolves 'ebay', liveness job
  covers ebay

No network is ever touched: the Browse API payload comes from a fixture.

Called by: pytest
Depends on: conftest.py, ebay_worker modules, tests/fixtures/ebay_browse_search.json
"""

import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import EbaySearchQueue, EbayWorkerStatus, Sighting
from app.services.ebay_worker.config import EbayConfig
from app.services.ebay_worker.queue_manager import (
    claim_next_queued_item,
    enqueue_for_ebay_search,
    get_next_queued_item,
    get_queue_stats,
    mark_completed,
    mark_status,
    recover_stale_searches,
)
from app.services.ebay_worker.result_parser import (
    EbaySighting,
    compute_confidence,
    mpn_match_variants,
    normalize_ebay_condition,
    normalize_for_match,
    parse_item_summaries,
    title_matches_mpn,
)
from app.services.ebay_worker.sighting_writer import ebay_dedup_key, save_ebay_sightings

_FIXTURES = Path(__file__).parent / "fixtures"
# The fixture is a Browse API response for the Dell part 0F8NV.
FIXTURE_MPN = "0F8NV"


def _payload() -> dict:
    return json.loads((_FIXTURES / "ebay_browse_search.json").read_text())


# ═══════════════════════════════════════════════════════════════════════
# IMPORTABILITY — the whole worker package must import without a network
# ═══════════════════════════════════════════════════════════════════════


class TestImportability:
    def test_worker_module_imports(self):
        """worker.main() and its lazy deps import cleanly (no HTTP at import)."""
        import app.services.ebay_worker.worker as worker_mod

        assert callable(worker_mod.main)

    def test_package_exports(self):
        import app.services.ebay_worker as pkg

        assert pkg.EbayConfig is EbayConfig
        assert callable(pkg.enqueue_for_ebay_search)
        assert callable(pkg.save_ebay_sightings)

    def test_no_browser_modules(self):
        """The eBay worker is an API poller — it must not grow browser modules."""
        import app.services.ebay_worker as pkg

        # Resolved from the imported package, never from the cwd: a cwd-relative
        # glob returns an empty set when pytest runs from anywhere else, and the
        # guard would pass while inspecting nothing.
        pkg_dir = Path(pkg.__file__).parent
        names = {p.name for p in pkg_dir.glob("*.py")}
        assert "worker.py" in names, f"package not found at {pkg_dir}"
        assert not names & {"session_manager.py", "search_engine.py", "human_behavior.py", "ai_gate.py"}

    def test_circuit_breaker_starts_closed(self):
        from app.services.ebay_worker.circuit_breaker import CircuitBreaker

        assert CircuitBreaker().should_stop() is False


# ═══════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════


class TestEbayConfig:
    def test_defaults(self):
        cfg = EbayConfig()
        assert cfg.EBAY_MARKETPLACE_ID == "EBAY_US"
        assert cfg.EBAY_CATEGORY_IDS == ""
        assert cfg.category_id_list == []  # no category filter = all of eBay
        assert cfg.EBAY_PAGE_LIMIT == 50
        assert cfg.EBAY_MAX_PAGES == 2
        assert cfg.EBAY_DAILY_CALL_BUDGET == 4000
        assert cfg.EBAY_MIN_DELAY_SECONDS == 3
        assert cfg.EBAY_SEARCH_TIMEOUT_SECONDS == 30
        assert cfg.EBAY_INCLUDE_AUCTIONS is False
        assert cfg.EBAY_POLL_IDLE_SECONDS == 30
        assert cfg.EBAY_DEDUP_WINDOW_DAYS == 7
        assert cfg.EBAY_BREAKER_COOLDOWN_MINUTES == 30

    def test_no_browser_knobs(self):
        """No browser profile / business-hours / random-break knobs on an API poller."""
        cfg = EbayConfig()
        for dead in ("EBAY_BROWSER_PROFILE_DIR", "EBAY_MAX_DELAY_SECONDS", "EBAY_TYPICAL_DELAY_SECONDS"):
            assert not hasattr(cfg, dead)

    def test_env_override(self):
        env = {
            "EBAY_MARKETPLACE_ID": "EBAY_GB",
            "EBAY_CATEGORY_IDS": "175673, 58058",
            "EBAY_PAGE_LIMIT": "120",
            "EBAY_MAX_PAGES": "5",
            "EBAY_DAILY_CALL_BUDGET": "100",
            "EBAY_MIN_DELAY_SECONDS": "9",
            "EBAY_INCLUDE_AUCTIONS": "true",
            "EBAY_POLL_IDLE_SECONDS": "5",
            # Every knob documented in .env.ebay-worker.example is pinned BY NAME
            # here, so a typo in an os.environ.get key fails loudly instead of
            # leaving the operator with a dead setting.
            "EBAY_SEARCH_TIMEOUT_SECONDS": "12",
            "EBAY_BREAKER_COOLDOWN_MINUTES": "5",
        }
        with patch.dict(os.environ, env):
            cfg = EbayConfig()
        assert cfg.EBAY_MARKETPLACE_ID == "EBAY_GB"
        assert cfg.category_id_list == ["175673", "58058"]
        assert cfg.EBAY_PAGE_LIMIT == 120
        assert cfg.EBAY_MAX_PAGES == 5
        assert cfg.EBAY_DAILY_CALL_BUDGET == 100
        assert cfg.EBAY_MIN_DELAY_SECONDS == 9
        assert cfg.EBAY_INCLUDE_AUCTIONS is True
        assert cfg.EBAY_POLL_IDLE_SECONDS == 5
        assert cfg.EBAY_SEARCH_TIMEOUT_SECONDS == 12
        assert cfg.EBAY_BREAKER_COOLDOWN_MINUTES == 5

    def test_page_limit_capped_at_api_maximum(self):
        """EBay's Browse API rejects limit > 200 — clamp rather than 400."""
        with patch.dict(os.environ, {"EBAY_PAGE_LIMIT": "5000"}):
            assert EbayConfig().EBAY_PAGE_LIMIT == 200


# ═══════════════════════════════════════════════════════════════════════
# STRICT PART-NUMBER MATCH
# ═══════════════════════════════════════════════════════════════════════


class TestStrictMpnMatch:
    def test_normalize_strips_everything_non_alnum(self):
        assert normalize_for_match("Dell 0F8NV / H730-Mini") == "DELL0F8NVH730MINI"
        assert normalize_for_match(None) == ""

    def test_variants_plain_mpn(self):
        assert mpn_match_variants("LM317T") == {"LM317T"}

    def test_variants_dell_leading_zero(self):
        """A 5-char Dell part carrying the leading zero also matches without it."""
        assert mpn_match_variants("0F8NV") == {"0F8NV", "F8NV"}

    def test_variants_blank(self):
        assert mpn_match_variants("") == set()

    def test_title_substring_match(self):
        variants = mpn_match_variants("LM317T")
        assert title_matches_mpn("New TI LM317T Voltage Regulator TO-220", variants) is True

    def test_title_match_ignores_punctuation_and_case(self):
        """Both sides are stripped to [A-Z0-9], so a seller's dashes/spaces/case never
        hide a real match."""
        variants = mpn_match_variants("LM317-T")
        assert variants == {"LM317T"}
        assert title_matches_mpn("Genuine lm317t regulators", variants) is True
        assert title_matches_mpn("Genuine LM-317/T regulators", variants) is True
        assert title_matches_mpn("Genuine LM318T regulators", variants) is False

    def test_title_leading_zero_variant_matches(self):
        variants = mpn_match_variants("0F8NV")
        assert title_matches_mpn("Dell PERC H730 Raid Card F8NV Tested Working", variants) is True

    def test_negative_unrelated_title_is_rejected(self):
        variants = mpn_match_variants("0F8NV")
        assert title_matches_mpn("HP Smart Array P440 Controller 726821-B21", variants) is False

    def test_blank_mpn_parses_nothing(self):
        assert parse_item_summaries(_payload(), "") == []

    def test_none_payload_parses_nothing(self):
        assert parse_item_summaries(None, FIXTURE_MPN) == []


# ═══════════════════════════════════════════════════════════════════════
# CONDITION MAPPING
# ═══════════════════════════════════════════════════════════════════════


class TestConditionMapping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("New", "new"),
            ("New other", "new"),
            ("New other (see details)", "new"),
            ("Open box", "new"),
            ("Seller refurbished", "refurb"),
            ("Certified - Refurbished", "refurb"),
            ("Excellent - Refurbished", "refurb"),
            ("Very Good - Refurbished", "refurb"),
            ("Good - Refurbished", "refurb"),
            ("Used", "used"),
            ("", None),
            (None, None),
        ],
    )
    def test_explicit_ebay_labels(self, raw, expected):
        assert normalize_ebay_condition(raw) == expected

    def test_unknown_label_falls_through_to_shared_normalizer(self):
        """An unlisted label still gets the shared keyword matcher's verdict."""
        assert normalize_ebay_condition("Factory New Sealed") == "new"
        assert normalize_ebay_condition("Zorblax") is None


# ═══════════════════════════════════════════════════════════════════════
# CONFIDENCE
# ═══════════════════════════════════════════════════════════════════════


class TestConfidence:
    def test_base_only(self):
        assert compute_confidence(quantity_estimated=False, feedback_pct=None, whole_token=False) == 0.55

    def test_all_bonuses_capped(self):
        assert compute_confidence(quantity_estimated=True, feedback_pct=99.9, whole_token=True) == 0.95

    def test_feedback_boundary_is_inclusive(self):
        assert compute_confidence(quantity_estimated=False, feedback_pct=98.0, whole_token=False) == 0.70
        assert compute_confidence(quantity_estimated=False, feedback_pct=97.9, whole_token=False) == 0.55

    def test_every_parsed_row_is_inside_the_check_constraint_range(self):
        rows = parse_item_summaries(_payload(), FIXTURE_MPN)
        assert rows
        for r in rows:
            assert 0.0 <= r.confidence <= 1.0

    def test_rank_order_best_seller_first(self):
        """A quantity + 98%+ feedback + whole-token listing outranks a bare one."""
        parsed = parse_item_summaries(_payload(), FIXTURE_MPN)
        best = next(r for r in parsed if r.item_id == "v1|110000000001|0")  # qty, 99.5%, token
        worst = next(r for r in parsed if r.item_id == "v1|110000000002|0")  # no qty, 97.2%
        assert best.confidence == 0.95
        assert worst.confidence == 0.65
        assert best.confidence > worst.confidence


# ═══════════════════════════════════════════════════════════════════════
# RESULT PARSER — the fixture Browse API payload
# ═══════════════════════════════════════════════════════════════════════


class TestResultParser:
    def test_filters_the_payload_down_to_matching_listings(self):
        """10 raw items -> 7 kept: the HP title, the for-parts row and the auction-only
        row are all dropped."""
        rows = parse_item_summaries(_payload(), FIXTURE_MPN)
        assert len(rows) == 7
        kept = {r.item_id for r in rows}
        assert "v1|110000000006|0" not in kept, "title without the MPN must be dropped"
        assert "v1|110000000004|0" not in kept, "conditionId 7000 (for parts) must be dropped"
        assert "v1|110000000005|0" not in kept, "auction-only listing must be dropped"

    def test_duplicate_item_id_survives_the_parser(self):
        """The parser does not dedup — the writer does (on the item id)."""
        rows = parse_item_summaries(_payload(), FIXTURE_MPN)
        assert [r.item_id for r in rows].count("v1|110000000001|0") == 2

    def test_include_auctions_flag_keeps_the_auction(self):
        rows = parse_item_summaries(_payload(), FIXTURE_MPN, include_auctions=True)
        assert "v1|110000000005|0" in {r.item_id for r in rows}
        assert len(rows) == 8

    def test_auction_plus_buy_it_now_listing_is_kept(self):
        """BuyingOptions ["AUCTION", "BEST_OFFER"] is a real, quotable listing.

        The filter drops AUCTION-ONLY rows (all(...)), never a row that merely
        offers an auction alongside a fixed price — that is exactly the shape the
        buyingOptions:{FIXED_PRICE|BEST_OFFER} request filter returns.
        """
        rows = {r.item_id: r for r in parse_item_summaries(_payload(), FIXTURE_MPN)}
        mixed = rows["v1|110000000009|0"]
        assert mixed.buying_options == ["AUCTION", "BEST_OFFER"]
        assert mixed.vendor_name == "mixed_options_seller"
        # ...while the auction-ONLY row stays dropped in the same pass.
        assert "v1|110000000005|0" not in rows

    def test_zero_reported_quantity_is_not_treated_as_supply(self):
        """EstimatedAvailableQuantity 0 = sold out, not "0 in stock".

        It must not land as qty_available=0 and must not earn the quantity confidence
        bonus, or a sold-out listing outranks an unknown-stock one.
        """
        payload = {
            "itemSummaries": [
                {
                    "itemId": "v1|zero|0",
                    "title": "Dell 0F8NV PERC H730 Controller",
                    "price": {"value": "10.00", "currency": "USD"},
                    "seller": {"username": "sold_out_seller"},
                    "estimatedAvailabilities": [{"estimatedAvailableQuantity": 0}],
                }
            ]
        }
        (row,) = parse_item_summaries(payload, FIXTURE_MPN)
        assert row.quantity == 1
        assert row.quantity_estimated is False
        assert row.confidence == 0.65  # 0.55 base + 0.10 whole token, no quantity bonus

    def test_for_parts_stays_dropped_even_with_auctions_included(self):
        rows = parse_item_summaries(_payload(), FIXTURE_MPN, include_auctions=True)
        assert "v1|110000000004|0" not in {r.item_id for r in rows}

    def test_dell_leading_zero_listing_is_kept(self):
        rows = {r.item_id: r for r in parse_item_summaries(_payload(), FIXTURE_MPN)}
        f8nv = rows["v1|110000000003|0"]
        assert f8nv.title == "Dell PERC H730 Raid Card F8NV Tested Working"
        assert f8nv.condition == "used"
        # part_number is the QUEUED mpn, never the seller's spelling.
        assert f8nv.part_number == FIXTURE_MPN

    def test_field_mapping_on_the_richest_row(self):
        # First occurrence: the fixture repeats this item id at a different qty.
        r = next(r for r in parse_item_summaries(_payload(), FIXTURE_MPN) if r.item_id == "v1|110000000001|0")
        assert r.vendor_name == "trio_surplus"
        assert r.part_number == FIXTURE_MPN
        assert r.quantity == 12
        assert r.quantity_estimated is True
        assert r.unit_price == 129.99
        assert r.currency == "USD"
        assert r.condition == "new"
        assert r.seller_feedback_pct == 99.5
        assert r.seller_feedback_score == 4120
        assert r.item_location_country == "US"
        assert r.buying_options == ["FIXED_PRICE"]
        assert r.click_url == "https://www.ebay.com/itm/110000000001"
        assert r.image_url.endswith("s-l500.jpg")
        assert r.fetched_at

    def test_missing_availability_defaults_to_one(self):
        rows = {r.item_id: r for r in parse_item_summaries(_payload(), FIXTURE_MPN)}
        r = rows["v1|110000000002|0"]
        assert r.quantity == 1
        assert r.quantity_estimated is False

    def test_seller_without_username_is_skipped(self):
        payload = {"itemSummaries": [{"itemId": "x", "title": "Dell 0F8NV card", "seller": {}}]}
        assert parse_item_summaries(payload, FIXTURE_MPN) == []

    def test_non_dict_items_are_skipped_not_raised(self):
        payload = {"itemSummaries": ["nonsense", None, 7]}
        assert parse_item_summaries(payload, FIXTURE_MPN) == []


# ═══════════════════════════════════════════════════════════════════════
# SEARCH CLIENT — request shape (no network)
# ═══════════════════════════════════════════════════════════════════════


class TestSearchClientParams:
    def test_default_params_exclude_auctions_and_all_categories(self):
        from app.services.ebay_worker.search_client import FIXED_PRICE_FILTER, build_params

        params = build_params("0F8NV", EbayConfig(), offset=0)
        assert params["q"] == "0F8NV"
        assert params["limit"] == "50"
        assert params["offset"] == "0"
        assert params["filter"] == FIXED_PRICE_FILTER
        assert "category_ids" not in params, "empty EBAY_CATEGORY_IDS must mean ALL of eBay"

    def test_category_ids_sent_only_when_configured(self):
        from app.services.ebay_worker.search_client import build_params

        with patch.dict(os.environ, {"EBAY_CATEGORY_IDS": "175673,58058"}):
            params = build_params("0F8NV", EbayConfig(), offset=100)
        assert params["category_ids"] == "175673,58058"
        assert params["offset"] == "100"

    def test_include_auctions_drops_the_buying_option_filter(self):
        from app.services.ebay_worker.search_client import build_params

        with patch.dict(os.environ, {"EBAY_INCLUDE_AUCTIONS": "true"}):
            params = build_params("0F8NV", EbayConfig(), offset=0)
        assert "filter" not in params

    def test_token_helper_shares_the_connector_cache_key(self):
        """The worker and EbayConnector must share ONE cached bearer."""
        from app.connectors.ebay import EbayConnector, ebay_token_cache_key

        c = EbayConnector(client_id="cid", client_secret="sec")
        assert ebay_token_cache_key("cid") == c._token_cache_key()


# ═══════════════════════════════════════════════════════════════════════
# QUEUE MANAGER
# ═══════════════════════════════════════════════════════════════════════


class TestQueueManager:
    def test_enqueue_no_requirement(self, db_session):
        assert enqueue_for_ebay_search(99999, db_session) is None

    def test_enqueue_no_mpn(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        req.primary_mpn = None
        db_session.commit()
        assert enqueue_for_ebay_search(req.id, db_session) is None

    def test_enqueue_success(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        item = enqueue_for_ebay_search(req.id, db_session)
        assert item is not None
        assert item.mpn == "LM317T"
        assert item.normalized_mpn == "LM317T"
        # QUEUED, not PENDING: PENDING exists for the browser workers' AI gate,
        # which promotes PENDING -> QUEUED. The eBay worker has no gate, so a
        # PENDING row would never be claimable.
        assert item.status == "queued"

    def test_enqueued_row_is_immediately_claimable(self, db_session, test_requisition):
        """End-to-end enqueue -> claim.

        Nothing in the codebase moves an eBay row from PENDING to QUEUED, so a PENDING
        enqueue would idle the worker forever.
        """
        req = test_requisition.requirements[0]
        enqueued = enqueue_for_ebay_search(req.id, db_session)

        claimed = claim_next_queued_item(db_session)
        assert claimed is not None
        assert claimed.id == enqueued.id
        assert claimed.status == "searching"

    def test_browser_worker_enqueue_still_lands_pending(self, db_session, test_requisition):
        """The initial_status hook is opt-in — TBF/ICS/NC must be untouched."""
        from app.services.tbf_worker.queue_manager import enqueue_for_tbf_search

        req = test_requisition.requirements[0]
        item = enqueue_for_tbf_search(req.id, db_session)
        assert item is not None
        assert item.status == "pending"

    def test_enqueue_already_queued_returns_existing(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        first = enqueue_for_ebay_search(req.id, db_session)
        second = enqueue_for_ebay_search(req.id, db_session)
        assert first.id == second.id

    def test_compound_dedup_allows_distinct_mpn_same_requirement(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        primary = enqueue_for_ebay_search(req.id, db_session)
        avl = enqueue_for_ebay_search(req.id, db_session, override_mpn="AVL-SUB-9000", resolved_via_spec_code="SPEC1")
        assert primary.id != avl.id
        assert avl.resolved_via_spec_code == "SPEC1"

    def test_claim_atomicity_marks_searching_and_single_winner(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        item = EbaySearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="queued",
        )
        db_session.add(item)
        db_session.commit()

        claimed = claim_next_queued_item(db_session)
        assert claimed.id == item.id
        assert claimed.status == "searching"
        assert claim_next_queued_item(db_session) is None

    def test_recover_stale_searches(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        item = EbaySearchQueue(
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

    def test_get_next_queued_item_and_stats(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        item = EbaySearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="queued",
        )
        db_session.add(item)
        db_session.commit()
        assert get_next_queued_item(db_session).id == item.id
        stats = get_queue_stats(db_session)
        assert stats["queued"] == 1
        assert stats["remaining"] == 1

    def test_mark_status_and_completed(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        item = EbaySearchQueue(
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

        mark_completed(db_session, item, results_found=6, sightings_created=5)
        db_session.refresh(item)
        assert item.status == "completed"
        assert item.results_count == 6
        assert item.search_count == 1


# ═══════════════════════════════════════════════════════════════════════
# SIGHTING WRITER
# ═══════════════════════════════════════════════════════════════════════


def _queue_item(db_session, test_requisition, mpn="0F8NV"):
    req = test_requisition.requirements[0]
    item = EbaySearchQueue(
        requirement_id=req.id,
        requisition_id=test_requisition.id,
        mpn=mpn,
        normalized_mpn=mpn,
        status="searching",
    )
    db_session.add(item)
    db_session.commit()
    return item


class TestSightingWriter:
    def test_requirement_not_found(self, db_session):
        queue_item = MagicMock()
        queue_item.requirement_id = 99999
        assert save_ebay_sightings(db_session, queue_item, []) == 0

    def test_empty_list_creates_nothing(self, db_session, test_requisition):
        item = _queue_item(db_session, test_requisition)
        assert save_ebay_sightings(db_session, item, []) == 0

    def test_writes_ebay_sightings_from_the_fixture(self, db_session, test_requisition):
        item = _queue_item(db_session, test_requisition)
        rows = parse_item_summaries(_payload(), FIXTURE_MPN)
        # 7 parsed rows, but two share one eBay item id -> 6 stored.
        created = save_ebay_sightings(db_session, item, rows)
        assert created == 6
        stored = db_session.query(Sighting).filter(Sighting.source_type == "ebay").all()
        assert len(stored) == 6
        assert {s.source_type for s in stored} == {"ebay"}
        assert all(s.is_authorized is False for s in stored)
        assert all(s.mpn_matched == FIXTURE_MPN for s in stored)

    def test_condition_and_click_url_are_persisted(self, db_session, test_requisition):
        item = _queue_item(db_session, test_requisition)
        rows = parse_item_summaries(_payload(), FIXTURE_MPN)
        save_ebay_sightings(db_session, item, rows)
        s = (
            db_session.query(Sighting)
            .filter(Sighting.source_type == "ebay", Sighting.vendor_name == "itad_liquidators")
            .one()
        )
        assert s.condition == "used"
        assert s.unit_price is not None and float(s.unit_price) == 75.50
        assert s.currency == "USD"
        assert s.qty_available == 3
        assert s.raw_data["click_url"] == "https://www.ebay.com/itm/110000000003"
        assert s.raw_data["ebay_item_id"] == "v1|110000000003|0"
        assert s.raw_data["ebay_condition"] == "Used"
        assert s.raw_data["ebay_condition_id"] == "3000"
        assert s.raw_data["seller_feedback_pct"] == 98.0
        assert s.raw_data["seller_feedback_score"] == 15200
        assert s.raw_data["item_location_country"] == "CA"
        assert s.raw_data["buying_options"] == ["FIXED_PRICE"]
        assert s.raw_data["image_url"]
        assert s.raw_data["fetched_at"]

    def test_dedups_by_item_id_not_by_quantity(self, db_session, test_requisition):
        """The same seller's same listing appearing twice at DIFFERENT quantities is one
        sighting — the shared (vendor, mpn, qty) key would have kept both."""
        item = _queue_item(db_session, test_requisition)
        dup_rows = [r for r in parse_item_summaries(_payload(), FIXTURE_MPN) if r.item_id == "v1|110000000001|0"]
        assert len(dup_rows) == 2
        assert dup_rows[0].quantity != dup_rows[1].quantity
        assert save_ebay_sightings(db_session, item, dup_rows) == 1

    def test_distinct_listings_from_one_seller_are_both_kept(self, db_session, test_requisition):
        """Two DIFFERENT listings by the same seller at the same qty must both land."""
        item = _queue_item(db_session, test_requisition)
        rows = [
            EbaySighting(part_number="0F8NV", vendor_name="trio_surplus", quantity=4, item_id="A1", confidence=0.6),
            EbaySighting(part_number="0F8NV", vendor_name="trio_surplus", quantity=4, item_id="A2", confidence=0.6),
        ]
        assert save_ebay_sightings(db_session, item, rows) == 2

    def test_dedups_against_already_stored_rows(self, db_session, test_requisition):
        item = _queue_item(db_session, test_requisition)
        rows = parse_item_summaries(_payload(), FIXTURE_MPN)
        assert save_ebay_sightings(db_session, item, rows) == 6
        # Re-running the same search creates nothing new.
        assert save_ebay_sightings(db_session, item, rows) == 0
        assert db_session.query(Sighting).filter(Sighting.source_type == "ebay").count() == 6

    def test_skips_rows_without_vendor(self, db_session, test_requisition):
        item = _queue_item(db_session, test_requisition)
        rows = [EbaySighting(part_number="0F8NV", vendor_name="", quantity=5, item_id="Z")]
        assert save_ebay_sightings(db_session, item, rows) == 0

    def test_apply_to_fresh_sightings_gating_invoked(self, db_session, test_requisition):
        item = _queue_item(db_session, test_requisition)
        rows = [EbaySighting(part_number="0F8NV", vendor_name="gated_seller", quantity=2, item_id="G1", confidence=0.6)]
        with patch("app.services.search_worker_base.sighting_writer.apply_to_fresh_sightings") as mock_gate:
            created = save_ebay_sightings(db_session, item, rows)
        assert created == 1
        mock_gate.assert_called_once()
        assert len(mock_gate.call_args[0][2]) == 1

    def test_dedup_key_falls_back_to_the_shared_triple_without_an_item_id(self):
        """A legacy eBay row with no item id must not collapse onto another vendor's."""
        from app.services.search_worker_base.sighting_writer import default_dedup_key

        legacy = ebay_dedup_key(vendor_norm="acme", mpn="lm317t", qty=5, raw_data={})
        assert legacy == default_dedup_key(vendor_norm="acme", mpn="lm317t", qty=5, raw_data=None)
        assert ebay_dedup_key(vendor_norm="acme", mpn="lm317t", qty=5, raw_data={"ebay_item_id": "x"}) == (
            "acme",
            "x",
        )


class TestSharedWriterBackwardCompatibility:
    def test_tbf_writer_still_uses_the_default_triple(self, db_session, test_requisition):
        """The dedup hook is opt-in — TBF's behavior must be unchanged."""
        from app.models import TbfSearchQueue
        from app.services.tbf_worker.result_parser import TbfSighting
        from app.services.tbf_worker.sighting_writer import save_tbf_sightings

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

        dup = TbfSighting(part_number="LM317T", vendor_name="Acme Brokers", quantity=100, in_stock=True)
        assert save_tbf_sightings(db_session, item, [dup, dup]) == 1
        assert save_tbf_sightings(db_session, item, [dup]) == 0


# ═══════════════════════════════════════════════════════════════════════
# SCHEDULER — daily call budget
# ═══════════════════════════════════════════════════════════════════════


class TestScheduler:
    def _sched(self, budget=10):
        from app.services.ebay_worker.scheduler import EbayScheduler

        with patch.dict(os.environ, {"EBAY_DAILY_CALL_BUDGET": str(budget)}):
            return EbayScheduler(EbayConfig())

    def test_budget_remaining_and_exhaustion(self):
        s = self._sched(budget=10)
        assert s.budget_remaining(0) == 10
        assert s.budget_exhausted(0) is False
        assert s.budget_remaining(10) == 0
        assert s.budget_exhausted(10) is True
        assert s.budget_exhausted(11) is True
        assert s.budget_remaining(11) == 0

    def test_delays_come_from_config(self):
        from app.services.ebay_worker.scheduler import EbayScheduler

        with patch.dict(os.environ, {"EBAY_MIN_DELAY_SECONDS": "7", "EBAY_POLL_IDLE_SECONDS": "11"}):
            s = EbayScheduler(EbayConfig())
        assert s.next_delay() == 7.0
        assert s.idle_delay() == 11.0

    def test_rollover_resets_a_stale_budget_day(self):
        from app.services.ebay_worker.scheduler import rollover_calls

        today = date(2026, 9, 5)
        assert rollover_calls(4000, date(2026, 9, 4), today) == 0  # yesterday's spend
        assert rollover_calls(4000, None, today) == 0  # never set
        assert rollover_calls(120, today, today) == 120  # today's spend survives

    def test_sleep_until_next_utc_day(self):
        from app.services.ebay_worker.scheduler import seconds_until_next_utc_day

        now = datetime(2026, 9, 5, 23, 0, tzinfo=UTC)
        assert seconds_until_next_utc_day(now) == 3600.0
        assert seconds_until_next_utc_day(datetime(2026, 9, 5, 0, 0, tzinfo=UTC)) == 86400.0


# ═══════════════════════════════════════════════════════════════════════
# WORKER STATUS SINGLETON + BUDGET BOOKKEEPING
# ═══════════════════════════════════════════════════════════════════════


class TestWorkerStatusSingleton:
    def test_seed_singleton_idempotent(self, db_session):
        from app.startup import seed_ebay_worker_status_singleton

        assert db_session.query(EbayWorkerStatus).filter_by(id=1).one_or_none() is None
        seed_ebay_worker_status_singleton(db_session)
        db_session.commit()
        row = db_session.query(EbayWorkerStatus).filter_by(id=1).one()
        assert row.is_running is False

        seed_ebay_worker_status_singleton(db_session)
        db_session.commit()
        assert db_session.query(EbayWorkerStatus).filter_by(id=1).count() == 1

    def test_update_worker_status(self, db_session):
        from app.services.ebay_worker.worker import update_worker_status

        db_session.add(EbayWorkerStatus(id=1, is_running=False, searches_today=0))
        db_session.commit()

        update_worker_status(db_session, is_running=True, searches_today=7, calls_today=21)
        row = db_session.query(EbayWorkerStatus).filter_by(id=1).one()
        assert row.is_running is True
        assert row.searches_today == 7
        assert row.calls_today == 21

    def test_update_worker_status_no_row_is_noop(self, db_session):
        from app.services.ebay_worker.worker import update_worker_status

        update_worker_status(db_session, is_running=True)

    def test_record_heartbeat_advances_timestamp(self, db_session):
        from app.services.ebay_worker.worker import _record_heartbeat

        stale = datetime.now(UTC) - timedelta(hours=1)
        db_session.add(EbayWorkerStatus(id=1, is_running=False, last_heartbeat=stale))
        db_session.commit()

        _record_heartbeat(db_session)
        row = db_session.query(EbayWorkerStatus).filter_by(id=1).one()
        assert row.is_running is True
        assert row.last_heartbeat > stale


class TestBudgetBookkeeping:
    def test_read_budget_resets_a_stale_day(self, db_session):
        from app.services.ebay_worker.worker import read_budget

        db_session.add(EbayWorkerStatus(id=1, calls_today=4000, budget_day=date(2026, 9, 4)))
        db_session.commit()

        assert read_budget(db_session, date(2026, 9, 5)) == 0
        row = db_session.query(EbayWorkerStatus).filter_by(id=1).one()
        assert row.calls_today == 0
        assert row.budget_day == date(2026, 9, 5)

    def test_read_budget_keeps_todays_spend(self, db_session):
        from app.services.ebay_worker.worker import read_budget

        today = date(2026, 9, 5)
        db_session.add(EbayWorkerStatus(id=1, calls_today=37, budget_day=today))
        db_session.commit()
        assert read_budget(db_session, today) == 37

    def test_record_calls_accumulates(self, db_session):
        from app.services.ebay_worker.worker import record_calls

        today = date(2026, 9, 5)
        db_session.add(EbayWorkerStatus(id=1, calls_today=0, budget_day=today))
        db_session.commit()

        assert record_calls(db_session, today, 2) == 2
        assert record_calls(db_session, today, 3) == 5
        row = db_session.query(EbayWorkerStatus).filter_by(id=1).one()
        assert row.calls_today == 5

    def test_a_missing_singleton_is_reseeded_not_silently_ignored(self, db_session):
        """The daily call budget lives ENTIRELY on this row.

        Returning 0-spent forever would give the worker an uncapped Browse API allowance
        with no log line.
        """
        from app.services.ebay_worker.worker import read_budget, record_calls

        assert db_session.query(EbayWorkerStatus).count() == 0
        today = date(2026, 9, 5)
        assert read_budget(db_session, today) == 0
        assert db_session.query(EbayWorkerStatus).filter_by(id=1).one_or_none() is not None
        # ...and the spend now actually persists, instead of evaporating.
        assert record_calls(db_session, today, 3) == 3
        assert read_budget(db_session, today) == 3

    def test_load_credentials_reads_db_first(self, db_session):
        from app.services.ebay_worker.worker import load_credentials

        with patch("app.services.credential_service.get_credential", side_effect=["cid", "sec"]) as mock_cred:
            assert load_credentials(db_session) == ("cid", "sec")
        assert [c.args[1:] for c in mock_cred.call_args_list] == [
            ("ebay", "EBAY_CLIENT_ID"),
            ("ebay", "EBAY_CLIENT_SECRET"),
        ]


class _StopLoop(BaseException):
    """Escape hatch: the worker loop catches Exception, not BaseException."""


class TestWorkerLoopBudgetGate:
    @pytest.mark.asyncio
    async def test_exhausted_budget_sleeps_instead_of_calling_the_api(self, db_session):
        """With the day's budget spent, the loop must sleep to midnight UTC and never
        touch the Browse API."""
        import app.services.ebay_worker.worker as worker_mod

        today = datetime.now(UTC).date()
        db_session.add(EbayWorkerStatus(id=1, is_running=False, calls_today=4000, budget_day=today))
        db_session.commit()

        sleeps: list[float] = []

        async def _fake_sleep(seconds):
            sleeps.append(seconds)
            raise _StopLoop

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(worker_mod, "_async_sleep", _fake_sleep),
            patch("app.services.ebay_worker.search_client.search_mpn", new_callable=AsyncMock) as mock_search,
            patch("app.services.ebay_worker.worker.load_credentials", return_value=("cid", "sec")),
            patch("app.services.ebay_worker.queue_manager.claim_next_queued_item") as mock_claim,
            pytest.raises(_StopLoop),
        ):
            await worker_mod.main()

        mock_search.assert_not_awaited()
        mock_claim.assert_not_called()
        assert len(sleeps) == 1
        # Slept toward midnight UTC, not the 3s inter-call delay.
        assert sleeps[0] > 60

    @pytest.mark.asyncio
    async def test_missing_credentials_idle_instead_of_calling(self, db_session, test_requisition):
        """With no credentials the worker must idle 15 min WITHOUT claiming anything —
        and say so on the status singleton, or the Connectors card reads green while
        nothing is searched."""
        import app.services.ebay_worker.worker as worker_mod

        db_session.add(EbayWorkerStatus(id=1, is_running=False, calls_today=0))
        db_session.commit()
        # A real claimable row: without it this test would pass on the empty-queue
        # idle path even if the credential guard were deleted.
        queued_id = enqueue_for_ebay_search(test_requisition.requirements[0].id, db_session).id

        sleeps: list[float] = []

        async def _fake_sleep(seconds):
            sleeps.append(seconds)
            raise _StopLoop

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(worker_mod, "_async_sleep", _fake_sleep),
            patch("app.services.ebay_worker.search_client.search_mpn", new_callable=AsyncMock) as mock_search,
            patch("app.services.ebay_worker.worker.load_credentials", return_value=(None, None)),
            pytest.raises(_StopLoop),
        ):
            await worker_mod.main()

        mock_search.assert_not_awaited()
        assert sleeps == [15 * 60], "must be the credential idle, not the 30s empty-queue idle"
        # Re-read: the worker closes the (patched) session, so `queued` is detached.
        still_queued = db_session.query(EbaySearchQueue).filter_by(id=queued_id).one()
        assert still_queued.status == "queued", "an unconfigured worker must not claim work"
        status = db_session.query(EbayWorkerStatus).filter_by(id=1).one()
        assert status.circuit_breaker_open is True
        assert status.circuit_breaker_reason == worker_mod.CREDENTIALS_MISSING_REASON

    @pytest.mark.asyncio
    async def test_budget_gate_reserves_a_whole_search(self, db_session):
        """One search spends up to EBAY_MAX_PAGES calls, so it must not START with fewer
        than that left — otherwise the last search of the day overshoots."""
        import app.services.ebay_worker.worker as worker_mod

        today = datetime.now(UTC).date()
        # 1 call left, EBAY_MAX_PAGES defaults to 2.
        db_session.add(EbayWorkerStatus(id=1, is_running=False, calls_today=3999, budget_day=today))
        db_session.commit()

        sleeps: list[float] = []

        async def _fake_sleep(seconds):
            sleeps.append(seconds)
            raise _StopLoop

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(worker_mod, "_async_sleep", _fake_sleep),
            patch("app.services.ebay_worker.queue_manager.claim_next_queued_item") as mock_claim,
            patch("app.services.ebay_worker.worker.load_credentials", return_value=("cid", "sec")),
            pytest.raises(_StopLoop),
        ):
            await worker_mod.main()

        mock_claim.assert_not_called()
        assert sleeps[0] > 60  # slept toward midnight UTC


# ═══════════════════════════════════════════════════════════════════════
# CIRCUIT BREAKER
# ═══════════════════════════════════════════════════════════════════════


class TestCircuitBreaker:
    def test_auth_failure_trips_immediately(self):
        from app.services.ebay_worker.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker()
        assert cb.record_api_failure(RuntimeError("bad creds"), status_code=401) == "AUTH_FAILED"
        assert cb.should_stop() is True

    def test_three_consecutive_failures_trip(self):
        from app.services.ebay_worker.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker()
        assert cb.record_api_failure(RuntimeError("500")) == "FAILED"
        assert cb.record_api_failure(RuntimeError("500")) == "FAILED"
        assert cb.record_api_failure(RuntimeError("500")) == "TRIPPED"
        assert cb.should_stop() is True

    def test_success_clears_the_failure_streak(self):
        from app.services.ebay_worker.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker()
        cb.record_api_failure(RuntimeError("500"))
        cb.record_api_failure(RuntimeError("500"))
        cb.record_api_success()
        assert cb.record_api_failure(RuntimeError("500")) == "FAILED"
        assert cb.should_stop() is False


# ═══════════════════════════════════════════════════════════════════════
# WIRING — fan-out, connector registry, liveness, connectors tab
# ═══════════════════════════════════════════════════════════════════════


class TestWiring:
    def test_worker_enqueues_includes_ebay(self):
        from app.search_service import _worker_enqueues

        labels = [label for _fn, label in _worker_enqueues()]
        assert labels == ["ICS", "NC", "TBF", "EBAY"]

    def test_build_connectors_no_longer_builds_ebay(self, db_session):
        """eBay is worker-owned: it must not run in the synchronous fan-out."""
        from app.search_service import _CONNECTOR_SOURCE_MAP, _MARKET_SOURCE_DISPLAY, _build_connectors

        assert "EbayConnector" not in _CONNECTOR_SOURCE_MAP
        assert "ebay" not in _MARKET_SOURCE_DISPLAY
        with patch("app.search_service.get_credentials_batch", side_effect=lambda db, reqs: dict.fromkeys(reqs, "k")):
            connectors, stats, _disabled = _build_connectors(db_session)
        assert not any(c.__class__.__name__ == "EbayConnector" for c in connectors)
        assert "ebay" not in stats

    def test_connector_registry_still_resolves_ebay(self):
        """The Test button / health ping / title mining still need the connector."""
        from app.services.connector_registry import get_connector_for_source

        with patch.dict(os.environ, {"EBAY_CLIENT_ID": "cid", "EBAY_CLIENT_SECRET": "sec"}):
            conn = get_connector_for_source("ebay")
        assert conn is not None
        assert conn.__class__.__name__ == "EbayConnector"

    def test_ebay_is_worker_backed_but_not_a_browser_worker(self):
        """It gets worker health on the Connectors tab, but health_monitor must keep
        pinging its real API credentials."""
        from app.constants import BROWSER_WORKER_SOURCES
        from app.services.connector_service import WORKER_BACKED_SOURCES

        assert WORKER_BACKED_SOURCES["ebay"] == "ebay"
        assert "ebay" not in BROWSER_WORKER_SOURCES

    def test_settings_resolves_the_ebay_status_row(self, db_session):
        from app.routers.htmx.settings import _worker_status_row

        db_session.add(EbayWorkerStatus(id=1, is_running=True))
        db_session.commit()
        row = _worker_status_row("ebay", db_session)
        assert row is not None
        assert row.is_running is True

    def test_liveness_job_watches_ebay(self, db_session):
        """A stale eBay heartbeat raises the same watchdog alert as the other three."""
        import asyncio

        from app.jobs.worker_liveness_jobs import _job_monitor_worker_heartbeats

        db_session.add(
            EbayWorkerStatus(id=1, is_running=True, last_heartbeat=datetime.now(UTC) - timedelta(minutes=45))
        )
        db_session.commit()

        teams = AsyncMock()
        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch("app.cache.intel_cache.get_cached", return_value=None),
            patch("app.cache.intel_cache.set_cached"),
            patch("app.services.teams_notifications.post_teams_channel", teams),
        ):
            asyncio.run(_job_monitor_worker_heartbeats())

        teams.assert_awaited_once()
        assert "eBay" in teams.await_args.args[0]


# ═══════════════════════════════════════════════════════════════════════
# SEARCH CLIENT — paging, retries and call accounting (no network)
# ═══════════════════════════════════════════════════════════════════════


def _response(status: int, body: dict | None = None, headers: dict | None = None):
    """Build an httpx.Response bound to a request (raise_for_status needs one)."""
    import httpx

    from app.services.ebay_worker.search_client import EBAY_SEARCH_URL

    return httpx.Response(
        status,
        json=body if body is not None else {},
        headers=headers or {},
        request=httpx.Request("GET", EBAY_SEARCH_URL),
    )


def _page(count: int, offset: int = 0) -> dict:
    return {
        "total": 999,
        "itemSummaries": [{"itemId": f"v1|{offset + i}|0", "title": "x"} for i in range(count)],
    }


class _ClientCfg:
    """Minimal config stand-in for the client (2 pages of 3)."""

    EBAY_PAGE_LIMIT = 3
    EBAY_MAX_PAGES = 2
    EBAY_MARKETPLACE_ID = "EBAY_US"
    EBAY_INCLUDE_AUCTIONS = False
    EBAY_CATEGORY_IDS = ""
    EBAY_SEARCH_TIMEOUT_SECONDS = 30
    category_id_list: list[str] = []


class TestSearchClient:
    """The only I/O in the package — and the sole source of the daily call count."""

    @pytest.mark.asyncio
    async def test_two_full_pages_merge_with_offsets_and_count_two_calls(self):
        from app.services.ebay_worker import search_client as sc

        gets = AsyncMock(side_effect=[_response(200, _page(3, 0)), _response(200, _page(3, 3))])
        with (
            patch.object(sc, "get_ebay_access_token", new=AsyncMock(return_value="tok")),
            patch.object(sc.http, "get", new=gets),
        ):
            payload, calls = await sc.search_mpn("0F8NV", _ClientCfg(), "cid", "sec")

        assert calls == 2
        assert len(payload["itemSummaries"]) == 6
        offsets = [c.kwargs["params"]["offset"] for c in gets.await_args_list]
        assert offsets == ["0", "3"]
        assert gets.await_args_list[0].kwargs["headers"]["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"

    @pytest.mark.asyncio
    async def test_short_page_stops_paging_early(self):
        from app.services.ebay_worker import search_client as sc

        gets = AsyncMock(return_value=_response(200, _page(1, 0)))
        with (
            patch.object(sc, "get_ebay_access_token", new=AsyncMock(return_value="tok")),
            patch.object(sc.http, "get", new=gets),
        ):
            payload, calls = await sc.search_mpn("0F8NV", _ClientCfg(), "cid", "sec")

        assert calls == 1
        assert len(payload["itemSummaries"]) == 1

    @pytest.mark.asyncio
    async def test_401_invalidates_the_token_and_re_mints_once(self):
        from app.services.ebay_worker import search_client as sc

        gets = AsyncMock(side_effect=[_response(401), _response(200, _page(1, 0))])
        mint = AsyncMock(side_effect=["stale", "fresh"])
        with (
            patch.object(sc, "get_ebay_access_token", new=mint),
            patch.object(sc, "invalidate_ebay_token") as mock_invalidate,
            patch.object(sc.http, "get", new=gets),
        ):
            _payload_out, calls = await sc.search_mpn("0F8NV", _ClientCfg(), "cid", "sec")

        mock_invalidate.assert_called_once_with("cid")
        assert mint.await_count == 2
        assert gets.await_args_list[1].kwargs["headers"]["Authorization"] == "Bearer fresh"
        # Budget semantics: BOTH HTTP requests are charged — the retry spent quota too.
        assert calls == 2

    @pytest.mark.asyncio
    async def test_429_honors_retry_after_then_retries_once(self):
        from app.services.ebay_worker import search_client as sc

        gets = AsyncMock(side_effect=[_response(429, headers={"Retry-After": "1"}), _response(200, _page(1, 0))])
        with (
            patch.object(sc, "get_ebay_access_token", new=AsyncMock(return_value="tok")),
            patch.object(sc, "_parse_retry_after", return_value=0.0),
            patch.object(sc.http, "get", new=gets),
        ):
            payload, calls = await sc.search_mpn("0F8NV", _ClientCfg(), "cid", "sec")

        assert len(payload["itemSummaries"]) == 1
        assert calls == 2

    @pytest.mark.asyncio
    async def test_persistent_429_raises_the_typed_rate_limit_error(self):
        from app.connectors.errors import ConnectorRateLimitError
        from app.services.ebay_worker import search_client as sc

        gets = AsyncMock(return_value=_response(429, headers={"Retry-After": "1"}))
        counter = sc.CallCounter()
        with (
            patch.object(sc, "get_ebay_access_token", new=AsyncMock(return_value="tok")),
            patch.object(sc, "_parse_retry_after", return_value=0.0),
            patch.object(sc.http, "get", new=gets),
            pytest.raises(ConnectorRateLimitError),
        ):
            await sc.search_mpn("0F8NV", _ClientCfg(), "cid", "sec", counter)

        # The calls spent before the raise are still observable for the budget.
        assert counter.calls == 2

    @pytest.mark.asyncio
    async def test_404_raises_instead_of_faking_an_empty_result(self):
        """A 404 means the endpoint/marketplace is wrong.

        Reporting it as "0 results" would COMPLETE the queue row and suppress re-search
        for the dedup window.
        """
        import httpx

        from app.services.ebay_worker import search_client as sc

        with (
            patch.object(sc, "get_ebay_access_token", new=AsyncMock(return_value="tok")),
            patch.object(sc.http, "get", new=AsyncMock(return_value=_response(404))),
            pytest.raises(httpx.HTTPStatusError),
        ):
            await sc.search_mpn("0F8NV", _ClientCfg(), "cid", "sec")

    @pytest.mark.asyncio
    async def test_calls_spent_before_a_mid_search_failure_are_observable(self):
        """Page 1 OK, page 2 500 -> two calls really spent, and the counter says so."""
        import httpx

        from app.services.ebay_worker import search_client as sc

        gets = AsyncMock(side_effect=[_response(200, _page(3, 0)), _response(500)])
        counter = sc.CallCounter()
        with (
            patch.object(sc, "get_ebay_access_token", new=AsyncMock(return_value="tok")),
            patch.object(sc.http, "get", new=gets),
            pytest.raises(httpx.HTTPStatusError),
        ):
            await sc.search_mpn("0F8NV", _ClientCfg(), "cid", "sec", counter)

        assert counter.calls == 2


# ═══════════════════════════════════════════════════════════════════════
# WORKER MAIN LOOP — one full iteration, and every failure branch
# ═══════════════════════════════════════════════════════════════════════


class _LoopHarness:
    """Drive exactly one main-loop iteration against the test session.

    ``_async_sleep`` raises _StopLoop, so the loop runs a single pass: claim ->
    search -> parse -> save -> log -> mark_completed, then stops at the pacing sleep.
    """

    def __init__(self, db_session, search_side_effect, *, calls_today=0):
        self.db = db_session
        self.search_side_effect = search_side_effect
        self.calls_today = calls_today
        self.sleeps: list[float] = []
        self.breaker = None

    async def run(self):
        import app.services.ebay_worker.circuit_breaker as breaker_mod
        import app.services.ebay_worker.worker as worker_mod

        today = datetime.now(UTC).date()
        self.db.add(EbayWorkerStatus(id=1, is_running=False, calls_today=self.calls_today, budget_day=today))
        self.db.commit()

        async def _fake_sleep(seconds):
            self.sleeps.append(seconds)
            raise _StopLoop

        real_breaker_cls = breaker_mod.CircuitBreaker

        def _capture_breaker(*args, **kwargs):
            self.breaker = real_breaker_cls(*args, **kwargs)
            return self.breaker

        with (
            patch("app.database.SessionLocal", return_value=self.db),
            patch.object(worker_mod, "_async_sleep", _fake_sleep),
            patch.object(breaker_mod, "CircuitBreaker", _capture_breaker),
            patch("app.services.ebay_worker.worker.load_credentials", return_value=("cid", "sec")),
            patch(
                "app.services.ebay_worker.search_client.search_mpn",
                new=AsyncMock(side_effect=self.search_side_effect),
            ),
            pytest.raises(_StopLoop),
        ):
            await worker_mod.main()


def _enqueue_claimable(db_session, test_requisition, mpn=FIXTURE_MPN):
    """A real queued row, created the way the fan-out creates it."""
    req = test_requisition.requirements[0]
    req.primary_mpn = mpn
    db_session.commit()
    return enqueue_for_ebay_search(req.id, db_session).id


class TestWorkerLoopIteration:
    @pytest.mark.asyncio
    async def test_happy_path_writes_sightings_a_log_row_and_books_the_calls(self, db_session, test_requisition):
        from app.models import EbaySearchLog

        queue_id = _enqueue_claimable(db_session, test_requisition)
        harness = _LoopHarness(db_session, [(_payload(), 2)])
        await harness.run()

        stored = db_session.query(Sighting).filter(Sighting.source_type == "ebay").all()
        assert len(stored) == 6
        row = db_session.query(EbaySearchQueue).filter_by(id=queue_id).one()
        assert row.status == "completed"
        assert row.results_count == 7
        log = db_session.query(EbaySearchLog).one()
        assert log.queue_id == queue_id
        assert log.results_found == 7
        assert log.sightings_created == 6
        assert log.error is None
        assert log.page_html_hash
        status = db_session.query(EbayWorkerStatus).filter_by(id=1).one()
        assert status.calls_today == 2
        assert status.searches_today == 1
        assert status.sightings_today == 6
        assert harness.sleeps == [3.0]  # the flat inter-call delay

    @pytest.mark.asyncio
    async def test_results_that_all_fail_the_mpn_filter_do_not_count_as_empty(self, db_session, test_requisition):
        """The empty-results streak is a shadow-block detector.

        eBay answering 200 with real listings that simply don't match is NORMAL —
        counting those trips the breaker (10 in a row) during healthy operation.
        """
        _enqueue_claimable(db_session, test_requisition)
        unmatched = {"itemSummaries": [{"itemId": "v1|x|0", "title": "HP something else", "seller": {"username": "s"}}]}
        harness = _LoopHarness(db_session, [(unmatched, 1)])
        await harness.run()

        assert harness.breaker.empty_results_streak == 0
        assert db_session.query(Sighting).filter(Sighting.source_type == "ebay").count() == 0

    @pytest.mark.asyncio
    async def test_a_truly_empty_payload_still_counts_toward_the_streak(self, db_session, test_requisition):
        _enqueue_claimable(db_session, test_requisition)
        harness = _LoopHarness(db_session, [({"itemSummaries": [], "total": 0}, 1)])
        await harness.run()

        assert harness.breaker.empty_results_streak == 1

    @pytest.mark.asyncio
    async def test_timeout_books_the_calls_already_spent_and_logs_the_failure(self, db_session, test_requisition):
        from app.models import EbaySearchLog

        queue_id = _enqueue_claimable(db_session, test_requisition)

        async def _slow(mpn, config, client_id, client_secret, counter=None):
            if counter is not None:
                counter.calls += 2  # both pages went out before the deadline hit
            raise TimeoutError

        harness = _LoopHarness(db_session, _slow)
        await harness.run()

        row = db_session.query(EbaySearchQueue).filter_by(id=queue_id).one()
        assert row.status == "failed"
        status = db_session.query(EbayWorkerStatus).filter_by(id=1).one()
        assert status.calls_today == 2, "calls spent on a timed-out search must still be charged"
        log = db_session.query(EbaySearchLog).one()
        assert log.error == "Search timeout"
        assert log.results_found == 0

    @pytest.mark.asyncio
    async def test_http_error_on_page_two_books_both_calls(self, db_session, test_requisition):
        import httpx

        from app.models import EbaySearchLog

        queue_id = _enqueue_claimable(db_session, test_requisition)

        async def _boom(mpn, config, client_id, client_secret, counter=None):
            if counter is not None:
                counter.calls += 2  # page 1 OK, page 2 500
            raise httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock(status_code=500))

        harness = _LoopHarness(db_session, _boom)
        await harness.run()

        row = db_session.query(EbaySearchQueue).filter_by(id=queue_id).one()
        assert row.status == "failed"
        status = db_session.query(EbayWorkerStatus).filter_by(id=1).one()
        assert status.calls_today == 2, "a hardcoded 1 under-reports real spend"
        assert db_session.query(EbaySearchLog).one().error

    @pytest.mark.asyncio
    async def test_persistent_rate_limit_requeues_and_backs_off(self, db_session, test_requisition):
        """FAILED is terminal for a (requirement, mpn) pair — a throttle must never cost
        a requirement its eBay coverage."""
        from app.connectors.errors import ConnectorRateLimitError
        from app.services.ebay_worker.scheduler import RATE_LIMIT_BACKOFF_SECONDS

        queue_id = _enqueue_claimable(db_session, test_requisition)

        async def _throttled(mpn, config, client_id, client_secret, counter=None):
            if counter is not None:
                counter.calls += 2
            raise ConnectorRateLimitError("eBay rate limited (persistent 429)")

        harness = _LoopHarness(db_session, _throttled)
        await harness.run()

        row = db_session.query(EbaySearchQueue).filter_by(id=queue_id).one()
        assert row.status == "queued", "re-queued for a later attempt, not failed"
        assert harness.sleeps == [float(RATE_LIMIT_BACKOFF_SECONDS)]
        assert db_session.query(EbayWorkerStatus).filter_by(id=1).one().calls_today == 2

    @pytest.mark.asyncio
    async def test_unexpected_error_feeds_the_circuit_breaker(self, db_session, test_requisition):
        """A KeyError from the token mint is neither httpx.HTTPError nor ValueError.

        Without the breaker it would fail one queue item every 3s, forever.
        """
        queue_id = _enqueue_claimable(db_session, test_requisition)

        async def _bug(mpn, config, client_id, client_secret, counter=None):
            raise KeyError("access_token")

        harness = _LoopHarness(db_session, _bug)
        await harness.run()

        assert harness.breaker.consecutive_failures == 1
        assert db_session.query(EbaySearchQueue).filter_by(id=queue_id).one().status == "failed"


class TestSchedulerDeadlines:
    def test_search_deadline_covers_every_page(self):
        """The per-REQUEST httpx timeout must not double as the whole-search cap, or a
        slow first page cancels a healthy 2-page search and discards its results."""
        from app.services.ebay_worker.scheduler import SEARCH_DEADLINE_SLACK_SECONDS, EbayScheduler

        with patch.dict(os.environ, {"EBAY_SEARCH_TIMEOUT_SECONDS": "30", "EBAY_MAX_PAGES": "2"}):
            cfg = EbayConfig()
        sched = EbayScheduler(cfg)
        assert sched.search_deadline() == 30 * 2 + SEARCH_DEADLINE_SLACK_SECONDS
        assert sched.search_deadline() > cfg.EBAY_SEARCH_TIMEOUT_SECONDS

    def test_can_afford_search_reserves_the_whole_page_budget(self):
        from app.services.ebay_worker.scheduler import EbayScheduler

        with patch.dict(os.environ, {"EBAY_DAILY_CALL_BUDGET": "10", "EBAY_MAX_PAGES": "2"}):
            sched = EbayScheduler(EbayConfig())
        assert sched.can_afford_search(8) is True
        assert sched.can_afford_search(9) is False  # 1 call left, a search needs 2
        assert sched.budget_exhausted(9) is False  # ...which the old gate allowed


class TestConnectorsTabCard:
    """EBay is the only worker-backed source that also owns real API credentials, so its
    card keeps the Test button the other three deliberately do not have."""

    def _source(self, db_session, name, env_vars):
        """Build the ApiSource row the seeder creates (see
        app/data/api_sources.json)."""
        from app.models import ApiSource

        src = ApiSource(
            name=name,
            display_name=name,
            category="api",
            source_type="marketplace",
            env_vars=env_vars,
            is_active=True,
            status="active",
        )
        db_session.add(src)
        db_session.commit()
        return src

    def _ebay_source(self, db_session):
        return self._source(db_session, "ebay", ["EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET"])

    def test_test_button_stays_when_credentials_exist(self, db_session):
        from app.routers.htmx.settings import _enrich_source

        with patch.dict(os.environ, {"EBAY_CLIENT_ID": "cid", "EBAY_CLIENT_SECRET": "sec"}):
            enriched = _enrich_source(self._ebay_source(db_session), db_session)
        assert enriched["testable"] is True

    def test_test_button_hidden_without_credentials(self, db_session):
        from app.routers.htmx.settings import _enrich_source

        with patch.dict(os.environ, {"EBAY_CLIENT_ID": "", "EBAY_CLIENT_SECRET": ""}):
            enriched = _enrich_source(self._ebay_source(db_session), db_session)
        assert enriched["testable"] is False

    def test_browser_workers_still_have_no_test_button(self, db_session):
        """The carve-out: ICS/NC/TBF have no connector at all, so nothing to probe."""
        from app.routers.htmx.settings import _enrich_source

        nc = self._source(db_session, "netcomponents", [])
        assert _enrich_source(nc, db_session)["testable"] is False


class TestStartupSeeding:
    def test_seed_browser_workers_creates_the_ebay_singleton(self, db_session):
        """Pins the WIRING, not just the helper: dropping the eBay line from the seed
        batch would leave a fresh DB with no row for the worker to heartbeat into."""
        import app.startup as startup_mod

        with patch.object(startup_mod, "SessionLocal", return_value=db_session):
            startup_mod.seed_browser_workers()

        row = db_session.query(EbayWorkerStatus).filter_by(id=1).one_or_none()
        assert row is not None
        # A freshly seeded singleton is idle with an untouched daily budget.
        assert row.is_running is False
        assert row.calls_today == 0
        assert row.budget_day is None
        assert db_session.query(EbayWorkerStatus).count() == 1
