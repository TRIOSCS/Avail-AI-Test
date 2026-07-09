"""run_one_batch wires the desc-parse pass: after mpn-decode, before the AI spec pass,
gated by settings.desc_parse_enabled, and isolated from batch failures."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

from app.constants import MaterialEnrichmentStatus
from app.models import MaterialCard
from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
from app.services.enrichment_worker.config import EnrichmentWorkerConfig
from app.services.enrichment_worker.worker import run_one_batch


def _seed_card(db, mpn: str) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        enrichment_status="unenriched",
        created_at=datetime.now(UTC),
    )
    db.add(card)
    db.flush()
    return card


async def _fake_enrich_verified(card, db, **kw):
    card.enrichment_status = MaterialEnrichmentStatus.VERIFIED
    return MaterialEnrichmentStatus.VERIFIED


def _run(db_session, desc_mock, decode_mock=None, spec_mock=None):
    cfg = EnrichmentWorkerConfig(batch_size=5, web_daily_cap=80)
    breaker = EnrichmentCircuitBreaker(cfg)
    decode_mock = decode_mock or Mock(return_value={"decoded": 0, "written": 0, "categorized": 0})
    spec_mock = spec_mock or AsyncMock(return_value={"cards_processed": 1, "specs_written": 1})
    with (
        patch("app.services.enrichment_worker.worker.enrich_card", side_effect=_fake_enrich_verified),
        patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
        patch("app.services.enrichment_worker.worker.intel_cache.get_cached", return_value=None),
        patch("app.services.enrichment_worker.worker.intel_cache.set_cached"),
        patch("app.services.mpn_decoder.writer.decode_and_record_specs", decode_mock),
        patch("app.services.desc_extractor.writer.extract_and_record_specs", desc_mock),
        patch("app.services.spec_enrichment_service.enrich_card_specs", spec_mock),
    ):
        return asyncio.run(run_one_batch(db_session, cfg, {}, breaker, set(), {"web_calls": 0}))


def test_run_one_batch_triggers_desc_parse_for_enriched_cards(db_session):
    card = _seed_card(db_session, "DP1")
    desc_mock = Mock(return_value={"parsed": 1, "written": 3})

    _run(db_session, desc_mock)

    desc_mock.assert_called_once()
    assert desc_mock.call_args.args[1] == [card.id]


def test_desc_parse_runs_after_decode_and_before_ai(db_session):
    # The ordering IS the tiering: 0.95 decode lands first, 0.90 desc-parse second,
    # 0.85 AI last.
    _seed_card(db_session, "DP2")
    order: list[str] = []
    decode_mock = Mock(
        side_effect=lambda *a, **k: order.append("decode") or {"decoded": 0, "written": 0, "categorized": 0}
    )
    desc_mock = Mock(side_effect=lambda *a, **k: order.append("desc") or {"parsed": 0, "written": 0})

    async def ai(*a, **k):
        order.append("ai")
        return {"cards_processed": 0, "specs_written": 0}

    _run(db_session, desc_mock, decode_mock=decode_mock, spec_mock=ai)

    assert order == ["decode", "desc", "ai"]


def test_desc_parse_gated_by_settings_flag(db_session, monkeypatch):
    from app.config import settings

    _seed_card(db_session, "DP3")
    monkeypatch.setattr(settings, "desc_parse_enabled", False)
    desc_mock = Mock(return_value={"parsed": 0, "written": 0})

    _run(db_session, desc_mock)

    desc_mock.assert_not_called()


def test_desc_parse_failure_does_not_crash_batch(db_session):
    # A desc-parse exception is logged, the AI pass still runs, the batch still commits.
    _seed_card(db_session, "DP4")
    desc_mock = Mock(side_effect=RuntimeError("boom"))
    spec_mock = AsyncMock(return_value={"cards_processed": 1, "specs_written": 0})

    counts = _run(db_session, desc_mock, spec_mock=spec_mock)

    desc_mock.assert_called_once()
    spec_mock.assert_awaited_once()
    assert counts.get(MaterialEnrichmentStatus.VERIFIED, 0) == 1


def test_desc_parse_not_called_when_nothing_enriched(db_session):
    card = _seed_card(db_session, "DP5")
    desc_mock = Mock(return_value={"parsed": 0, "written": 0})

    async def fake_not_found(c, db, **kw):
        c.enrichment_status = MaterialEnrichmentStatus.NOT_FOUND
        return MaterialEnrichmentStatus.NOT_FOUND

    cfg = EnrichmentWorkerConfig(batch_size=5, web_daily_cap=80)
    breaker = EnrichmentCircuitBreaker(cfg)
    with (
        patch("app.services.enrichment_worker.worker.enrich_card", side_effect=fake_not_found),
        patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
        patch("app.services.enrichment_worker.worker.intel_cache.get_cached", return_value=None),
        patch("app.services.enrichment_worker.worker.intel_cache.set_cached"),
        patch("app.services.desc_extractor.writer.extract_and_record_specs", desc_mock),
        patch("app.services.spec_enrichment_service.enrich_card_specs", AsyncMock(return_value={})),
    ):
        asyncio.run(run_one_batch(db_session, cfg, {}, breaker, set(), {"web_calls": 0}))

    desc_mock.assert_not_called()
    assert card.enrichment_status == MaterialEnrichmentStatus.NOT_FOUND
