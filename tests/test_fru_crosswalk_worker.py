"""run_one_batch wires the FRU crosswalk pass: after mpn-decode, before desc-parse,
gated by settings.fru_crosswalk_enrich_enabled, over the FULL batch (not enriched_ids),
and isolated from batch failures."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

from app.constants import MaterialEnrichmentStatus
from app.models import MaterialCard
from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
from app.services.enrichment_worker.config import EnrichmentWorkerConfig
from app.services.enrichment_worker.worker import run_one_batch

_FRU_ZERO = {
    "matched": 0,
    "decoded": 0,
    "written": 0,
    "categorized": 0,
    "desc_parsed": 0,
    "desc_written": 0,
    "failed": 0,
    "desc_failed": 0,
    "dropped_conflict": 0,
    "desc_dropped_conflict": 0,
    "commodity_conflict": 0,
    "desc_commodity_conflict": 0,
    "category_mismatch": 0,
    "desc_category_mismatch": 0,
}


def _seed_card(db, mpn: str) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        enrichment_status="unenriched",
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.flush()
    return card


async def _fake_enrich_verified(card, db, **kw):
    card.enrichment_status = MaterialEnrichmentStatus.VERIFIED
    return MaterialEnrichmentStatus.VERIFIED


def _run(db_session, fru_mock, enrich=_fake_enrich_verified, decode_mock=None, desc_mock=None, spec_mock=None):
    cfg = EnrichmentWorkerConfig(batch_size=5, web_daily_cap=80)
    breaker = EnrichmentCircuitBreaker(cfg)
    decode_mock = decode_mock or Mock(return_value={"decoded": 0, "written": 0, "categorized": 0})
    desc_mock = desc_mock or Mock(return_value={"parsed": 0, "written": 0, "failed": 0})
    spec_mock = spec_mock or AsyncMock(return_value={"cards_processed": 1, "specs_written": 1})
    with (
        patch("app.services.enrichment_worker.worker.enrich_card", side_effect=enrich),
        patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
        patch("app.services.enrichment_worker.worker.intel_cache.get_cached", return_value=None),
        patch("app.services.enrichment_worker.worker.intel_cache.set_cached"),
        patch("app.services.mpn_decoder.writer.decode_and_record_specs", decode_mock),
        patch("app.services.fru_crosswalk_enrich.crosswalk_and_record_specs", fru_mock),
        patch("app.services.desc_extractor.writer.extract_and_record_specs", desc_mock),
        patch("app.services.spec_enrichment_service.enrich_card_specs", spec_mock),
    ):
        return asyncio.run(run_one_batch(db_session, cfg, {}, breaker, set(), {"web_calls": 0}))


def test_crosswalk_gated_by_settings_flag(db_session, monkeypatch):
    from app.config import settings

    _seed_card(db_session, "00AJ141")
    monkeypatch.setattr(settings, "fru_crosswalk_enrich_enabled", False)
    fru_mock = Mock(return_value=dict(_FRU_ZERO))

    _run(db_session, fru_mock)

    fru_mock.assert_not_called()


def test_not_found_card_still_receives_crosswalk(db_session):
    # Scope is the FULL batch, not enriched_ids — a FRU spare PN that every
    # connector misses (not_found) is precisely this feature's primary target.
    card = _seed_card(db_session, "00AJ141")

    async def fake_not_found(c, db, **kw):
        c.enrichment_status = MaterialEnrichmentStatus.NOT_FOUND
        return MaterialEnrichmentStatus.NOT_FOUND

    fru_mock = Mock(return_value=dict(_FRU_ZERO))
    desc_mock = Mock(return_value={"parsed": 0, "written": 0, "failed": 0})

    _run(db_session, fru_mock, enrich=fake_not_found, desc_mock=desc_mock)

    fru_mock.assert_called_once()
    assert fru_mock.call_args.args[1] == [card.id]
    assert card.enrichment_status == MaterialEnrichmentStatus.NOT_FOUND  # status untouched by the pass
    desc_mock.assert_not_called()  # desc-parse still gates on enriched_ids


def test_crosswalk_receives_full_batch_ids(db_session):
    cards = [_seed_card(db_session, f"00AJ14{i}") for i in range(3)]
    fru_mock = Mock(return_value=dict(_FRU_ZERO))

    _run(db_session, fru_mock)

    # select_batch orders by demand telemetry (sourced_qty_90d / last_sourced_at, then
    # id — migration 105); these cards carry none, so compare membership, not order.
    assert sorted(fru_mock.call_args.args[1]) == sorted(c.id for c in cards)


def test_passes_run_decode_then_crosswalk_then_desc_then_ai(db_session):
    # The pass ordering IS the confidence tiering: 0.95 decode, 0.93 crosswalk,
    # 0.90 desc, 0.85 AI.
    _seed_card(db_session, "00AJ141")
    order: list[str] = []
    decode_mock = Mock(
        side_effect=lambda *a, **k: order.append("decode") or {"decoded": 0, "written": 0, "categorized": 0}
    )
    fru_mock = Mock(side_effect=lambda *a, **k: order.append("crosswalk") or dict(_FRU_ZERO))
    desc_mock = Mock(side_effect=lambda *a, **k: order.append("desc") or {"parsed": 0, "written": 0, "failed": 0})

    async def ai(*a, **k):
        order.append("ai")
        return {"cards_processed": 0, "specs_written": 0}

    _run(db_session, fru_mock, decode_mock=decode_mock, desc_mock=desc_mock, spec_mock=ai)

    assert order == ["decode", "crosswalk", "desc", "ai"]


def test_crosswalk_failure_does_not_crash_batch(db_session):
    # A crosswalk exception is logged; desc-parse and the AI pass still run, and the
    # batch still commits.
    _seed_card(db_session, "00AJ141")
    fru_mock = Mock(side_effect=RuntimeError("boom"))
    desc_mock = Mock(return_value={"parsed": 0, "written": 0, "failed": 0})
    spec_mock = AsyncMock(return_value={"cards_processed": 1, "specs_written": 0})

    counts = _run(db_session, fru_mock, desc_mock=desc_mock, spec_mock=spec_mock)

    fru_mock.assert_called_once()
    desc_mock.assert_called_once()
    spec_mock.assert_awaited_once()
    assert counts.get(MaterialEnrichmentStatus.VERIFIED, 0) == 1


def test_desc_channel_shares_the_single_crosswalk_stage(db_session):
    # Wave 3A folded the linked-description parse (fru_desc_parse, tier 82) INTO the
    # crosswalk pass: same stage, same fru_crosswalk_enrich_enabled flag, ONE call
    # over the full batch — the worker grew NO new pass and the second-pass order is
    # unchanged (decode → crosswalk → desc → ai).
    card = _seed_card(db_session, "00AJ141")
    order: list[str] = []
    decode_mock = Mock(
        side_effect=lambda *a, **k: order.append("decode") or {"decoded": 0, "written": 0, "categorized": 0}
    )
    fru_stats = dict(_FRU_ZERO, matched=1, desc_parsed=1, desc_written=2)
    fru_mock = Mock(side_effect=lambda *a, **k: order.append("crosswalk") or fru_stats)
    desc_mock = Mock(side_effect=lambda *a, **k: order.append("desc") or {"parsed": 0, "written": 0, "failed": 0})

    async def ai(*a, **k):
        order.append("ai")
        return {"cards_processed": 0, "specs_written": 0}

    _run(db_session, fru_mock, decode_mock=decode_mock, desc_mock=desc_mock, spec_mock=ai)

    assert order == ["decode", "crosswalk", "desc", "ai"]
    fru_mock.assert_called_once()
    assert fru_mock.call_args.args[1] == [card.id]
