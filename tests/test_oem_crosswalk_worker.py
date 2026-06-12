"""run_one_batch wires the OEM web-resolution passes: Pass A (paced resolution —
per-batch bound, BOTH daily caps, breaker on ClaudeError, no-row-on-ClaudeError, the
90-day no_match window) and Pass B (deterministic writer BEFORE the per-card core
loop — the oem_sourced upgrade short-circuits enrich_card and saves its web calls),
both gated by settings.oem_crosswalk_enrich_enabled."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

from app.constants import MaterialEnrichmentStatus, OemCrosswalkStatus
from app.models import MaterialCard, OemCrosswalk
from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
from app.services.enrichment_worker.config import EnrichmentWorkerConfig
from app.services.enrichment_worker.oem_crosswalk_resolver import OemResolveResult
from app.services.enrichment_worker.worker import run_one_batch
from app.utils.claude_errors import ClaudeError
from app.utils.normalization import normalize_mpn_key

RESOLVED = OemResolveResult(
    status="resolved",
    canonical_mpn="ST4000NM0035",
    manufacturer="Seagate",
    title="4TB 12G SAS 7.2K LFF Midline hard drive",
    source_url="https://partsurfer.hp.com/Search.aspx?SearchText=695510-001",
    source_domain="partsurfer.hp.com",
    confidence=0.95,
    payload={"canonical_mpn": "ST4000NM0035"},
)
NO_MATCH = OemResolveResult(status="no_match", payload={"canonical_mpn": None})


def _seed_card(db, mpn: str, status: str = MaterialEnrichmentStatus.UNENRICHED) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        enrichment_status=status,
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.flush()
    return card


def _seed_no_match_row(db, mpn: str, age_days: int) -> OemCrosswalk:
    row = OemCrosswalk(
        spare_raw=mpn,
        spare_norm=normalize_mpn_key(mpn),
        vendor="hpe",
        status=OemCrosswalkStatus.NO_MATCH,
        looked_up_at=datetime.now(timezone.utc) - timedelta(days=age_days),
    )
    db.add(row)
    db.flush()
    return row


async def _fake_enrich_metered(card, db, web_meter=None, **kw):
    """Mirrors enrich_card's contract: VERIFIED/OEM_SOURCED early-return (zero web
    calls); anything else bills one web call and lands web_sourced."""
    if card.enrichment_status in (MaterialEnrichmentStatus.VERIFIED, MaterialEnrichmentStatus.OEM_SOURCED):
        return card.enrichment_status
    if web_meter is not None:
        web_meter.reserve_web_call()
        web_meter.mark_claude_ok()
    card.enrichment_status = MaterialEnrichmentStatus.WEB_SOURCED
    return MaterialEnrichmentStatus.WEB_SOURCED


def _run(
    db_session,
    resolve_mock,
    *,
    cfg: EnrichmentWorkerConfig | None = None,
    breaker: EnrichmentCircuitBreaker | None = None,
    web_state: dict | None = None,
    cache_counts: dict | None = None,
    oem_writer_mock=None,
    lossy_cache: bool = False,
):
    cfg = cfg or EnrichmentWorkerConfig(batch_size=5, web_daily_cap=80)
    breaker = breaker or EnrichmentCircuitBreaker(cfg)
    web_state = web_state if web_state is not None else {"web_calls": 0, "oem_resolves": 0}
    # Simulated shared counter store (what Redis INCRBY provides in prod): keyed by
    # the counter-name fragment, seeded from cache_counts, advanced by incr_count.
    # lossy_cache simulates BOTH cache backends down (reads 0, increments don't
    # stick) — the scenario web_state's in-process backstop exists for.
    counters = dict(cache_counts or {})

    def _fragment(key):
        for fragment in ("web_calls", "oem_resolves"):
            if fragment in key:
                return fragment
        return key

    if lossy_cache:

        def get_count(key):
            return 0

        def incr_count(key, amount=1, ttl_days=1.0):
            return amount

    else:

        def get_count(key):
            return counters.get(_fragment(key), 0)

        def incr_count(key, amount=1, ttl_days=1.0):
            frag = _fragment(key)
            counters[frag] = counters.get(frag, 0) + amount
            return counters[frag]

    patches = [
        patch("app.services.enrichment_worker.worker.enrich_card", side_effect=_fake_enrich_metered),
        patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
        patch("app.services.enrichment_worker.worker.intel_cache.get_count", side_effect=get_count),
        patch("app.services.enrichment_worker.worker.intel_cache.incr_count", side_effect=incr_count),
        patch("app.services.enrichment_worker.oem_crosswalk_resolver.resolve_oem_spare", resolve_mock),
        patch(
            "app.services.mpn_decoder.writer.decode_and_record_specs",
            Mock(return_value={"decoded": 0, "written": 0, "categorized": 0}),
        ),
        patch(
            "app.services.fru_crosswalk_enrich.crosswalk_and_record_specs",
            Mock(return_value={"matched": 0}),
        ),
        patch(
            "app.services.desc_extractor.writer.extract_and_record_specs",
            Mock(return_value={"parsed": 0, "written": 0, "failed": 0}),
        ),
        patch(
            "app.services.spec_enrichment_service.enrich_card_specs",
            AsyncMock(return_value={"cards_processed": 0, "specs_written": 0}),
        ),
    ]
    if oem_writer_mock is not None:
        patches.append(patch("app.services.oem_crosswalk_enrich.oem_crosswalk_and_record_specs", oem_writer_mock))
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        counts = asyncio.run(run_one_batch(db_session, cfg, {}, breaker, set(), web_state))
    return counts, web_state, breaker


def test_pass_a_respects_per_batch_bound(db_session):
    # Three uncached HPE spares in the batch, oem_resolve_per_batch=2 → exactly two
    # resolves; both outcomes are upserted as rows.
    for i in range(3):
        _seed_card(db_session, f"87594{i}-001")
    resolve_mock = AsyncMock(side_effect=[RESOLVED, NO_MATCH])

    _run(db_session, resolve_mock, cfg=EnrichmentWorkerConfig(batch_size=5, oem_resolve_per_batch=2))

    assert resolve_mock.await_count == 2
    rows = db_session.query(OemCrosswalk).all()
    assert {r.status for r in rows} == {OemCrosswalkStatus.RESOLVED, OemCrosswalkStatus.NO_MATCH}


def test_pass_a_resolved_row_fields(db_session):
    _seed_card(db_session, "695510-001")
    resolve_mock = AsyncMock(return_value=RESOLVED)

    _, web_state, _ = _run(db_session, resolve_mock)

    row = db_session.query(OemCrosswalk).one()
    assert row.spare_raw == "695510-001"
    assert row.spare_norm == "695510001"
    assert row.vendor == "hpe"
    assert row.status == OemCrosswalkStatus.RESOLVED
    assert row.canonical_mpn_raw == "ST4000NM0035"
    assert row.canonical_mpn_norm == "st4000nm0035"
    assert row.canonical_manufacturer == "Seagate"
    assert row.source_domain == "partsurfer.hp.com"
    assert row.confidence == 0.95
    assert row.payload == {"canonical_mpn": "ST4000NM0035"}
    assert row.looked_up_at is not None
    # Billing: one resolve = one web call + one oem resolve (sub-cap INSIDE web cap).
    assert web_state["web_calls"] == 1
    assert web_state["oem_resolves"] == 1


def test_pass_a_skips_non_hpe_cards(db_session):
    # Phase A is HPE-only: Lenovo FRUs and generic MPNs never reach the resolver.
    _seed_card(db_session, "01HW917")  # lenovo
    _seed_card(db_session, "LM2596S-5.0")  # generic
    resolve_mock = AsyncMock(return_value=RESOLVED)

    _run(db_session, resolve_mock)

    resolve_mock.assert_not_awaited()


def test_pass_a_respects_web_daily_cap(db_session):
    _seed_card(db_session, "875942-001")
    resolve_mock = AsyncMock(return_value=RESOLVED)

    _run(db_session, resolve_mock, cache_counts={"web_calls": 80})

    resolve_mock.assert_not_awaited()
    assert db_session.query(OemCrosswalk).count() == 0


def test_pass_a_respects_oem_daily_sub_cap(db_session):
    _seed_card(db_session, "875942-001")
    resolve_mock = AsyncMock(return_value=RESOLVED)

    _run(db_session, resolve_mock, cache_counts={"oem_resolves": 40})

    resolve_mock.assert_not_awaited()
    assert db_session.query(OemCrosswalk).count() == 0


def test_pass_a_claude_error_feeds_breaker_writes_no_row_still_bills(db_session):
    # Transient Claude failure: breaker fed, NO row (retried next batch for free), but
    # the dispatched call is still billed (reserve-before-await, flush-in-finally).
    # The breaker is SPIED, not inspected after the batch — the core loop's later
    # Claude success legitimately resets the consecutive counter.
    _seed_card(db_session, "875942-001")
    resolve_mock = AsyncMock(side_effect=ClaudeError("boom"))
    cfg = EnrichmentWorkerConfig(batch_size=5, web_daily_cap=80)
    breaker = EnrichmentCircuitBreaker(cfg)
    breaker.record_claude_error = Mock(wraps=breaker.record_claude_error)

    _, web_state, _ = _run(db_session, resolve_mock, cfg=cfg, breaker=breaker)

    assert resolve_mock.await_count == 1
    assert db_session.query(OemCrosswalk).count() == 0
    breaker.record_claude_error.assert_called_once()
    assert web_state["web_calls"] >= 1
    assert web_state["oem_resolves"] == 1


def test_pass_a_web_cap_boundary_mid_pass(db_session):
    # Boundary, not exhaustion: at 79/80 with two pending candidates the contract is
    # EXACTLY one resolve then stop — an off-by-one in the gate (>= vs >) or a reserve
    # moved above the cap check would leak past the cap or strand the final slot.
    _seed_card(db_session, "875940-001")
    _seed_card(db_session, "875941-001")
    resolve_mock = AsyncMock(return_value=RESOLVED)

    _, web_state, _ = _run(db_session, resolve_mock, cache_counts={"web_calls": 79})

    assert resolve_mock.await_count == 1
    assert db_session.query(OemCrosswalk).count() == 1
    assert web_state["oem_resolves"] == 1  # exactly the one boundary slot was spent


def test_pass_a_oem_sub_cap_boundary_mid_pass(db_session):
    # Mirror boundary for the sub-cap: 39/40 with two pending → exactly one resolve.
    _seed_card(db_session, "875940-001")
    _seed_card(db_session, "875941-001")
    resolve_mock = AsyncMock(return_value=RESOLVED)

    _, web_state, _ = _run(db_session, resolve_mock, cache_counts={"oem_resolves": 39})

    assert resolve_mock.await_count == 1
    assert db_session.query(OemCrosswalk).count() == 1
    assert web_state["oem_resolves"] == 40


def test_pass_a_upsert_race_skips_spare_and_batch_survives(db_session):
    # The drain CLI can commit the same (spare_norm, vendor, source_domain) edge while
    # the worker's resolve await is in flight: the worker's flush then raises
    # IntegrityError. The SAVEPOINT must roll back ONLY that upsert (the concurrent
    # row is the desired end state) — the session stays usable and the rest of the
    # batch still enriches; the billed call stays billed.
    from sqlalchemy import text

    _seed_card(db_session, "875942-001")
    _seed_card(db_session, "LM2596S-5.0")  # generic card — proves the batch survives

    async def racing_resolve(display, norm, vendor):
        db_session.execute(
            text(
                "INSERT INTO oem_crosswalk (spare_raw, spare_norm, vendor, status, canonical_mpn_raw, "
                "canonical_mpn_norm, source_domain, looked_up_at) VALUES ('875942-001', '875942001', 'hpe', "
                "'resolved', 'ST4000NM0035', 'st4000nm0035', 'partsurfer.hp.com', '2026-06-10')"
            )
        )
        return RESOLVED

    resolve_mock = AsyncMock(side_effect=racing_resolve)

    counts, web_state, _ = _run(db_session, resolve_mock)

    assert resolve_mock.await_count == 1
    rows = db_session.query(OemCrosswalk).filter_by(spare_norm="875942001").all()
    assert len(rows) == 1  # the concurrent writer's row won; no duplicate, no crash
    # 1 OEM bill (the race never unbills) + 1 core-loop call for the generic card;
    # the spare card itself was upgraded by Pass B off the racing row (zero calls).
    assert web_state["web_calls"] == 2
    assert counts[MaterialEnrichmentStatus.WEB_SOURCED] == 1  # the core loop still ran
    assert counts[MaterialEnrichmentStatus.OEM_SOURCED] == 1  # Pass B used the winner's row


def test_pass_a_non_claude_failure_keeps_billed_calls_in_web_state(db_session):
    # A non-ClaudeError escaping the pass is swallowed by run_one_batch's wrapper; the
    # in-process backstop (web_state — THE defense when the cache is unavailable,
    # hence lossy_cache) must keep the already-billed OEM call instead of being
    # clobbered by the stale local at the end-of-batch reconciliation.
    _seed_card(db_session, "875942-001")
    resolve_mock = AsyncMock(side_effect=RuntimeError("boom after billing"))

    counts, web_state, _ = _run(db_session, resolve_mock, lossy_cache=True)

    assert resolve_mock.await_count == 1
    # 1 OEM bill (survived the swallowed crash) + 1 core-loop call for the card the
    # dead pass left unupgraded — the stale-local clobber would report 1.
    assert web_state["web_calls"] == 2
    assert web_state["oem_resolves"] == 1
    assert counts  # the batch itself still completed


def test_pass_a_fresh_no_match_blocks_resolution(db_session):
    # The 90-day negative cache: a 10-day-old no_match row blocks re-resolution.
    _seed_card(db_session, "875942-001")
    _seed_no_match_row(db_session, "875942-001", age_days=10)
    resolve_mock = AsyncMock(return_value=RESOLVED)

    _run(db_session, resolve_mock)

    resolve_mock.assert_not_awaited()


def test_pass_a_stale_no_match_retried_and_updated_in_place(db_session):
    # A 91-day-old no_match row is stale: the spare re-resolves and the SAME row is
    # updated in place (upsert on the unique key — no second row).
    _seed_card(db_session, "875942-001")
    stale = _seed_no_match_row(db_session, "875942-001", age_days=91)
    resolve_mock = AsyncMock(return_value=RESOLVED)

    _run(db_session, resolve_mock)

    assert resolve_mock.await_count == 1
    rows = db_session.query(OemCrosswalk).all()
    assert len(rows) == 1
    assert rows[0].id == stale.id
    assert rows[0].status == OemCrosswalkStatus.RESOLVED
    assert rows[0].canonical_mpn_raw == "ST4000NM0035"


def test_pass_a_resolved_row_is_permanent(db_session):
    # resolved rows are NEVER re-fetched, however old.
    _seed_card(db_session, "695510-001")
    row = OemCrosswalk(
        spare_raw="695510-001",
        spare_norm="695510001",
        vendor="hpe",
        status=OemCrosswalkStatus.RESOLVED,
        canonical_mpn_raw="ST4000NM0035",
        canonical_mpn_norm="st4000nm0035",
        looked_up_at=datetime.now(timezone.utc) - timedelta(days=400),
    )
    db_session.add(row)
    db_session.flush()
    resolve_mock = AsyncMock(return_value=RESOLVED)

    _run(db_session, resolve_mock)

    resolve_mock.assert_not_awaited()


def test_pass_b_upgrade_saves_web_calls(db_session, monkeypatch):
    # Meter assertion: a seeded resolved row lets Pass B (real writer, runs BEFORE the
    # core loop) upgrade the spare card to oem_sourced — enrich_card early-returns and
    # the card costs ZERO web calls this batch.
    from app.services.commodity_registry import seed_commodity_schemas

    seed_commodity_schemas(db_session)
    card = _seed_card(db_session, "695510-001")
    row = OemCrosswalk(
        spare_raw="695510-001",
        spare_norm="695510001",
        vendor="hpe",
        status=OemCrosswalkStatus.RESOLVED,
        canonical_mpn_raw="ST4000NM0035",
        canonical_mpn_norm="st4000nm0035",
        canonical_manufacturer="Seagate",
        confidence=0.95,
        looked_up_at=datetime.now(timezone.utc),
    )
    db_session.add(row)
    db_session.flush()
    resolve_mock = AsyncMock(return_value=RESOLVED)

    counts, web_state, _ = _run(db_session, resolve_mock)

    resolve_mock.assert_not_awaited()  # resolved row is fresh — Pass A no-ops
    assert card.enrichment_status == MaterialEnrichmentStatus.OEM_SOURCED
    assert card.category == "hdd"
    assert counts == {MaterialEnrichmentStatus.OEM_SOURCED: 1}
    assert web_state["web_calls"] == 0  # the early-return saved the card's web calls


def test_flag_off_both_passes_inert(db_session, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "oem_crosswalk_enrich_enabled", False)
    _seed_card(db_session, "875942-001")
    resolve_mock = AsyncMock(return_value=RESOLVED)
    oem_writer_mock = Mock(return_value={"matched": 0})

    _run(db_session, resolve_mock, oem_writer_mock=oem_writer_mock)

    resolve_mock.assert_not_awaited()
    oem_writer_mock.assert_not_called()
    assert db_session.query(OemCrosswalk).count() == 0


def test_pass_b_runs_before_core_loop_over_full_batch(db_session):
    # Order assertion: the OEM writer fires BEFORE enrich_card (the whole point — the
    # upgrade must short-circuit the core loop), over the FULL batch ids.
    cards = [_seed_card(db_session, f"87594{i}-001") for i in range(2)]
    order: list[str] = []
    oem_writer_mock = Mock(side_effect=lambda *a, **k: order.append("oem_writer") or {"matched": 0})

    async def tracking_enrich(card, db, web_meter=None, **kw):
        order.append("enrich")
        return await _fake_enrich_metered(card, db, web_meter=web_meter, **kw)

    cfg = EnrichmentWorkerConfig(batch_size=5)
    breaker = EnrichmentCircuitBreaker(cfg)
    with (
        patch("app.services.enrichment_worker.worker.enrich_card", side_effect=tracking_enrich),
        patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
        patch("app.services.enrichment_worker.worker.intel_cache.get_count", return_value=0),
        patch("app.services.enrichment_worker.worker.intel_cache.incr_count", side_effect=lambda *a, **k: 1),
        patch(
            "app.services.enrichment_worker.oem_crosswalk_resolver.resolve_oem_spare", AsyncMock(return_value=NO_MATCH)
        ),
        patch("app.services.oem_crosswalk_enrich.oem_crosswalk_and_record_specs", oem_writer_mock),
        patch("app.services.mpn_decoder.writer.decode_and_record_specs", Mock(return_value={})),
        patch("app.services.fru_crosswalk_enrich.crosswalk_and_record_specs", Mock(return_value={})),
        patch("app.services.desc_extractor.writer.extract_and_record_specs", Mock(return_value={})),
        patch("app.services.spec_enrichment_service.enrich_card_specs", AsyncMock(return_value={})),
    ):
        asyncio.run(run_one_batch(db_session, cfg, {}, breaker, set(), {"web_calls": 0, "oem_resolves": 0}))

    assert order[0] == "oem_writer"
    assert order.count("enrich") == len(cards)
    assert sorted(oem_writer_mock.call_args.args[1]) == sorted(c.id for c in cards)
