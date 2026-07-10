"""tests/test_partsurfer_enrich.py -- PartSurfer description → category + facets, via the
F1 ladder.

Covers the DB-side contract of the PartSurfer enrichment channel: feeding the OEM's own
verbatim description into ``categorize_and_record`` with source="partsurfer_desc"
categorizes an UNCATEGORIZED HP card and records facets at tier 84; an already-categorized
card is a no-op; and via ``record_spec`` a partsurfer_desc value BEATS a pre-existing
desc_parse (83) facet but LOSES to an mpn_decode (85) facet (the ladder check).

Depends on: conftest.py (db_session), seed_commodity_schemas, MaterialCard +
MaterialSpecFacet schema, spec_tiers.SOURCE_TIER (partsurfer_desc=84).
"""

from datetime import UTC
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.models import MaterialCard, MaterialSpecFacet
from app.services.commodity_registry import seed_commodity_schemas
from app.services.desc_extractor._common import PARTSURFER_DESC_CONFIDENCE, PARTSURFER_DESC_SOURCE
from app.services.desc_extractor.writer import categorize_and_record
from app.services.spec_tiers import SOURCE_TIER, set_category
from app.services.spec_write_service import record_spec

# Real PartSurfer descriptions (the captured fixtures' lblDescription text).
_DIMM_DESC = "HPE 16GB (1X16GB) DUAL RANK X4 DDR4-2133 CAS-15-15-15 REGISTERED MEMORY KIT"
_SSD_DESC = "HPE 240GB SATA 6G READ INTENSIVE SFF RW PM883 SSD"


def _facets(db: Session, card_id: int) -> dict:
    rows = db.query(MaterialSpecFacet).filter_by(material_card_id=card_id).all()
    return {r.spec_key: (r.value_text if r.value_text is not None else r.value_numeric) for r in rows}


def _hp_card(db: Session, mpn: str = "726719-B21") -> MaterialCard:
    """An UNCATEGORIZED HP spare card — the real PartSurfer target population."""
    card = MaterialCard(normalized_mpn=mpn.lower(), display_mpn=mpn, category=None)
    db.add(card)
    db.flush()
    return card


def _patch_fetch(monkeypatch, fake_fetch) -> None:
    """Patch fetch_partsurfer_description on the resolver module (the pass imports it
    lazily from there, so that module is the only effective patch target)."""
    import app.services.enrichment_worker.partsurfer_resolver as resolver_mod

    monkeypatch.setattr(resolver_mod, "fetch_partsurfer_description", fake_fetch)


def _patch_no_sleep(monkeypatch, worker) -> None:
    """Replace the pass's worker.asyncio.sleep(2.0) pacing with a no-op."""

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(worker.asyncio, "sleep", no_sleep)


def test_partsurfer_source_is_registered_at_tier_84():
    # The source string MUST be a registered ladder key (tier 84) or every write loses.
    assert SOURCE_TIER[PARTSURFER_DESC_SOURCE] == 84
    assert SOURCE_TIER["partsurfer_desc"] > SOURCE_TIER["desc_parse"]  # 84 > 83
    assert SOURCE_TIER["mpn_decode"] > SOURCE_TIER["partsurfer_desc"]  # 85 > 84


def test_dram_description_categorizes_and_writes_facets_at_tier_84(db_session: Session):
    seed_commodity_schemas(db_session)
    card = _hp_card(db_session)

    categorized, written = categorize_and_record(
        db_session,
        card,
        description=_DIMM_DESC,
        source=PARTSURFER_DESC_SOURCE,
        confidence=PARTSURFER_DESC_CONFIDENCE,
    )
    db_session.commit()

    assert categorized is True
    assert card.category == "dram"
    # Category written THROUGH the ladder at partsurfer_desc / tier 84.
    assert card.category_source == "partsurfer_desc"
    assert card.category_tier == 84
    assert card.category_confidence == PARTSURFER_DESC_CONFIDENCE
    # At least one facet, also at partsurfer_desc.
    assert written >= 1
    f = _facets(db_session, card.id)
    assert f["capacity_gb"] == 16
    assert f["ddr_type"] == "DDR4"
    assert card.specs_structured["capacity_gb"]["source"] == "partsurfer_desc"
    assert card.specs_structured["capacity_gb"]["tier"] == 84


def test_ssd_description_categorizes_to_storage(db_session: Session):
    seed_commodity_schemas(db_session)
    card = _hp_card(db_session, "875507-B21")

    categorized, written = categorize_and_record(
        db_session, card, description=_SSD_DESC, source=PARTSURFER_DESC_SOURCE, confidence=PARTSURFER_DESC_CONFIDENCE
    )
    db_session.commit()

    assert categorized is True
    assert card.category == "ssd"
    assert card.category_source == "partsurfer_desc"
    assert written >= 1
    assert _facets(db_session, card.id)["capacity_gb"] == 240


def test_already_categorized_card_is_a_noop(db_session: Session):
    seed_commodity_schemas(db_session)
    card = _hp_card(db_session)
    # Pre-categorize via the sanctioned ladder path (never hand-set category).
    assert set_category(card, "ssd", source="mpn_decode", confidence=0.95)
    db_session.flush()

    categorized, written = categorize_and_record(
        db_session, card, description=_DIMM_DESC, source=PARTSURFER_DESC_SOURCE, confidence=PARTSURFER_DESC_CONFIDENCE
    )
    db_session.commit()

    # categorize_and_record is fill-only — never reclassifies.
    assert (categorized, written) == (False, 0)
    assert card.category == "ssd"  # untouched
    assert card.category_source == "mpn_decode"


def test_partsurfer_desc_beats_desc_parse_but_loses_to_mpn_decode(db_session: Session):
    # The ladder check via record_spec: partsurfer_desc (84) BEATS a pre-existing
    # desc_parse (83) facet, but LOSES to an mpn_decode (85) facet.
    seed_commodity_schemas(db_session)
    card = _hp_card(db_session)
    # A category is required for record_spec; set it through the ladder.
    assert set_category(card, "dram", source="partsurfer_desc", confidence=PARTSURFER_DESC_CONFIDENCE)
    db_session.flush()

    # A pre-existing desc_parse (83) value.
    assert record_spec(db_session, card.id, "ddr_type", "DDR3", source="desc_parse", confidence=0.90) is True
    assert card.specs_structured["ddr_type"]["source"] == "desc_parse"

    # partsurfer_desc (84) > desc_parse (83) → wins, overwrites.
    assert record_spec(db_session, card.id, "ddr_type", "DDR4", source="partsurfer_desc", confidence=0.90) is True
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"
    assert card.specs_structured["ddr_type"]["source"] == "partsurfer_desc"
    assert card.specs_structured["ddr_type"]["tier"] == 84

    # mpn_decode (85) > partsurfer_desc (84) → wins.
    assert record_spec(db_session, card.id, "ddr_type", "DDR5", source="mpn_decode", confidence=0.95) is True
    assert card.specs_structured["ddr_type"]["value"] == "DDR5"
    assert card.specs_structured["ddr_type"]["source"] == "mpn_decode"
    assert card.specs_structured["ddr_type"]["tier"] == 85

    # partsurfer_desc (84) can no longer downgrade the mpn_decode (85) value.
    assert record_spec(db_session, card.id, "ddr_type", "DDR4", source="partsurfer_desc", confidence=0.99) is False
    assert card.specs_structured["ddr_type"]["value"] == "DDR5"
    db_session.commit()


# --- the worker pass (_partsurfer_desc_pass) -------------------------------------------


async def test_worker_pass_only_fetches_uncategorized_hp_cards_and_dedupes(db_session: Session, monkeypatch):
    # The pass: candidates = UNCATEGORIZED + classify_oem_vendor=="hpe", deduped by
    # display_mpn. A non-HP card and an already-categorized HP card are never fetched.
    from app.services.enrichment_worker import worker

    seed_commodity_schemas(db_session)
    # Two cards sharing display_mpn "726719-B21" (the dedup key) but distinct
    # normalized_mpn (a UNIQUE column) → one fetch must cover both.
    hp_a1 = MaterialCard(normalized_mpn="726719-b21", display_mpn="726719-B21", category=None)
    hp_a2 = MaterialCard(normalized_mpn="726719-b21-dup", display_mpn="726719-B21", category=None)
    db_session.add_all([hp_a1, hp_a2])
    db_session.flush()
    hp_b = _hp_card(db_session, "875507-B21")
    # An already-categorized HP card — skipped (fill-only NULL-category gate).
    hp_done = _hp_card(db_session, "918042-601")
    assert set_category(hp_done, "ssd", source="mpn_decode", confidence=0.95)
    # A non-HP card — classify_oem_vendor != "hpe", never fetched.
    non_hp = MaterialCard(normalized_mpn="grm155", display_mpn="GRM155R71C104MA88D", category=None)
    db_session.add(non_hp)
    db_session.flush()

    fetched_spares: list[str] = []

    async def fake_fetch(spare, **_kw):
        fetched_spares.append(spare)
        return {"726719-B21": _DIMM_DESC, "875507-B21": _SSD_DESC}.get(spare)

    _patch_fetch(monkeypatch, fake_fetch)
    _patch_no_sleep(monkeypatch, worker)

    batch = [hp_a1, hp_a2, hp_b, hp_done, non_hp]
    stats = await worker._partsurfer_desc_pass(db_session, batch)
    db_session.commit()

    # Deduped by spare: only the two distinct HP spares are fetched (not the dup, not the
    # categorized card, not the non-HP card).
    assert sorted(fetched_spares) == ["726719-B21", "875507-B21"]
    assert stats["fetched"] == 2
    # Both cards sharing 726719-B21 get categorized + the 875507-B21 card → 3 categorized.
    assert stats["categorized"] == 3
    assert stats["specs_written"] >= 3
    assert stats["failed"] == 0
    assert hp_a1.category == "dram"
    assert hp_a2.category == "dram"
    assert hp_b.category == "ssd"
    assert hp_a1.category_source == "partsurfer_desc"
    assert non_hp.category is None  # never touched


async def test_worker_pass_respects_fetch_cap(db_session: Session, monkeypatch):
    from app.config import settings
    from app.services.enrichment_worker import worker

    seed_commodity_schemas(db_session)
    # 3 distinct HP spares but a cap of 1 → only one fetch.
    cards = [_hp_card(db_session, mpn) for mpn in ("726719-B21", "875507-B21", "918042-601")]

    fetched: list[str] = []

    async def fake_fetch(spare, **_kw):
        fetched.append(spare)
        return _DIMM_DESC

    _patch_fetch(monkeypatch, fake_fetch)
    _patch_no_sleep(monkeypatch, worker)
    monkeypatch.setattr(settings, "partsurfer_fetch_per_batch", 1)

    stats = await worker._partsurfer_desc_pass(db_session, cards)
    db_session.commit()

    assert stats["fetched"] == 1
    assert len(fetched) == 1


async def test_worker_pass_no_hp_cards_is_a_noop(db_session: Session, monkeypatch):
    from app.services.enrichment_worker import worker

    seed_commodity_schemas(db_session)
    non_hp = MaterialCard(normalized_mpn="grm155", display_mpn="GRM155R71C104MA88D", category=None)
    db_session.add(non_hp)
    db_session.flush()

    called = False

    async def fake_fetch(spare, **_kw):
        nonlocal called
        called = True
        return None

    _patch_fetch(monkeypatch, fake_fetch)

    stats = await worker._partsurfer_desc_pass(db_session, [non_hp])
    assert stats == {"fetched": 0, "categorized": 0, "specs_written": 0, "failed": 0}
    assert called is False


async def test_worker_pass_paces_one_sleep_between_each_fetch(db_session: Session, monkeypatch):
    # Politeness pacing: asyncio.sleep(2.0) fires exactly len(candidates)-1 times (between
    # fetches, never before the first), each with 2.0. Capture the sleeps instead of no-op'ing.
    from app.config import settings
    from app.services.enrichment_worker import worker

    seed_commodity_schemas(db_session)
    cards = [_hp_card(db_session, mpn) for mpn in ("726719-B21", "875507-B21", "918042-601")]

    async def fake_fetch(spare, **_kw):
        return _DIMM_DESC

    _patch_fetch(monkeypatch, fake_fetch)
    monkeypatch.setattr(settings, "partsurfer_fetch_per_batch", 10)

    sleeps: list[float] = []

    async def rec(s):
        sleeps.append(s)

    # The pass calls worker.asyncio.sleep(2.0) between fetches — patch that exact symbol.
    monkeypatch.setattr(worker.asyncio, "sleep", rec)

    await worker._partsurfer_desc_pass(db_session, cards)
    db_session.commit()

    # 3 candidates → 2 paced sleeps, each 2.0s, none before the first fetch.
    assert sleeps == [2.0, 2.0]


async def test_worker_pass_aborts_on_transient_and_keeps_earlier(db_session: Session, monkeypatch):
    # A PartSurferTransient on the 2nd of 3 candidates BREAKS the pass (3rd never fetched),
    # but the 1st card — already fetched + categorized — is kept. The aborted fetch is not
    # counted in `fetched`.
    from app.services.enrichment_worker import worker
    from app.services.enrichment_worker.partsurfer_resolver import PartSurferTransient

    seed_commodity_schemas(db_session)
    c1 = _hp_card(db_session, "726719-B21")  # DIMM → categorizes
    c2 = _hp_card(db_session, "875507-B21")  # raises transient
    c3 = _hp_card(db_session, "918042-601")  # must never be fetched

    fetched_spares: list[str] = []

    async def fake_fetch(spare, **_kw):
        fetched_spares.append(spare)
        if spare == "875507-B21":
            raise PartSurferTransient("partsurfer 503 for 875507-B21")
        return _DIMM_DESC

    _patch_fetch(monkeypatch, fake_fetch)
    _patch_no_sleep(monkeypatch, worker)

    stats = await worker._partsurfer_desc_pass(db_session, [c1, c2, c3])
    db_session.commit()

    # The loop BREAKS at the transient: the 3rd spare is never fetched.
    assert fetched_spares == ["726719-B21", "875507-B21"]
    # Only the 1st succeeded — the aborted (transient) fetch is NOT counted.
    assert stats["fetched"] == 1
    assert stats["categorized"] == 1
    assert stats["failed"] == 0
    assert c1.category == "dram"
    assert c3.category is None


async def test_worker_pass_isolates_a_single_failing_card(db_session: Session, monkeypatch):
    # One card raising IntegrityError in categorize_and_record must not abort the pass —
    # the other card still categorizes and summary["failed"] == 1.
    from sqlalchemy.exc import IntegrityError

    from app.services.desc_extractor import writer as desc_writer
    from app.services.enrichment_worker import worker

    seed_commodity_schemas(db_session)
    # Two distinct HP spares (so two separate fetches → two categorize_and_record calls).
    bad = _hp_card(db_session, "726719-B21")
    good = _hp_card(db_session, "875507-B21")

    async def fake_fetch(spare, **_kw):
        return {"726719-B21": _DIMM_DESC, "875507-B21": _SSD_DESC}[spare]

    _patch_fetch(monkeypatch, fake_fetch)
    _patch_no_sleep(monkeypatch, worker)

    real_categorize = desc_writer.categorize_and_record

    def flaky_categorize(db, card, **kw):
        if card.id == bad.id:
            raise IntegrityError("simulated", {}, Exception("boom"))
        return real_categorize(db, card, **kw)

    # The pass imports categorize_and_record from the writer module — patch it there.
    monkeypatch.setattr(desc_writer, "categorize_and_record", flaky_categorize)

    stats = await worker._partsurfer_desc_pass(db_session, [bad, good])
    db_session.commit()

    # Both fetched; the bad card failed in isolation; the good card still categorized.
    assert stats["fetched"] == 2
    assert stats["failed"] == 1
    assert stats["categorized"] == 1
    assert good.category == "ssd"
    assert bad.category is None


async def test_worker_pass_grammar_declines_categorize_none(db_session: Session, monkeypatch):
    # Fetch succeeds but the description is non-categorizable (categorize_and_record →
    # (False, 0)) over 2 HP candidates → a clean {fetched:2, categorized:0, specs:0, failed:0}.
    from app.services.desc_extractor import writer as desc_writer
    from app.services.enrichment_worker import worker

    seed_commodity_schemas(db_session)
    cards = [_hp_card(db_session, mpn) for mpn in ("726719-B21", "875507-B21")]

    async def fake_fetch(spare, **_kw):
        return "SOME OPAQUE HP DESCRIPTION THE GRAMMAR DECLINES"

    _patch_fetch(monkeypatch, fake_fetch)
    _patch_no_sleep(monkeypatch, worker)
    # Grammar declines every description.
    monkeypatch.setattr(desc_writer, "categorize_and_record", lambda *a, **k: (False, 0))

    stats = await worker._partsurfer_desc_pass(db_session, cards)
    db_session.commit()

    assert stats == {"fetched": 2, "categorized": 0, "specs_written": 0, "failed": 0, "skipped_cached": 0}


async def test_run_one_batch_gates_partsurfer_pass_on_flag(db_session: Session, monkeypatch):
    # The run_one_batch gate: a seeded uncategorized HP card is fetched + categorized ONLY
    # when settings.partsurfer_desc_enabled is True. Flag OFF → no fetch at all.
    from app.config import settings
    from app.constants import MaterialEnrichmentStatus
    from app.services.enrichment_worker import worker
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

    seed_commodity_schemas(db_session)

    fetched: list[str] = []

    async def fake_fetch(spare, **_kw):
        fetched.append(spare)
        return _DIMM_DESC  # the DIMM description → categorizes to dram

    _patch_fetch(monkeypatch, fake_fetch)
    _patch_no_sleep(monkeypatch, worker)
    # Isolate the partsurfer pass: enrich_card is a no-op miss; the other deterministic
    # passes are gated off so only the partsurfer flag is under test.
    monkeypatch.setattr(settings, "oem_crosswalk_enrich_enabled", False)
    monkeypatch.setattr(settings, "fru_crosswalk_enrich_enabled", False)
    monkeypatch.setattr(settings, "mpn_decode_enabled", False)
    monkeypatch.setattr(settings, "desc_parse_enabled", False)

    async def fake_enrich(card, db, **kw):
        # Leaves the card uncategorized → eligible for the partsurfer pass.
        return MaterialEnrichmentStatus.NOT_FOUND

    cfg = EnrichmentWorkerConfig(batch_size=5, web_daily_cap=80)
    breaker = EnrichmentCircuitBreaker(cfg)

    def _seed_card(mpn: str) -> int:
        card = MaterialCard(
            normalized_mpn=mpn.lower(),
            display_mpn=mpn,
            category=None,
            enrichment_status=MaterialEnrichmentStatus.UNENRICHED,
        )
        db_session.add(card)
        db_session.flush()
        return card.id

    async def _drive():
        return await worker.run_one_batch(db_session, cfg, {}, breaker, set(), {"web_calls": 0})

    with (
        patch("app.services.enrichment_worker.worker.enrich_card", side_effect=fake_enrich),
        patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
        patch("app.services.spec_enrichment_service.enrich_card_specs", new=AsyncMock(return_value={})),
    ):
        # Flag OFF → the pass is never entered, nothing fetched.
        monkeypatch.setattr(settings, "partsurfer_desc_enabled", False)
        cid_off = _seed_card("726719-B21")
        await _drive()
        assert fetched == []
        assert db_session.get(MaterialCard, cid_off).category is None

        # Flag ON → the still-uncategorized HP card is fetched + categorized.
        monkeypatch.setattr(settings, "partsurfer_desc_enabled", True)
        cid_on = _seed_card("875507-B21")
        await _drive()
        assert "875507-B21" in fetched
        assert db_session.get(MaterialCard, cid_on).category == "dram"


# --- negative cache: no-result / ungrammatical spares are durably cached + not re-queried -


def _neg_rows(db: Session):
    from app.models import PartsurferDescNegative

    return db.query(PartsurferDescNegative).all()


async def test_no_result_is_negative_cached(db_session: Session, monkeypatch):
    # A fetch that returns None (genuine no-result) writes a no_result negative row so the
    # dead spare is not re-fetched daily.
    from app.models import PartsurferDescNegative
    from app.services.enrichment_worker import worker

    seed_commodity_schemas(db_session)
    card = _hp_card(db_session, "918042-601")

    async def fake_fetch(spare, **_kw):
        return None  # no description on PartSurfer

    _patch_fetch(monkeypatch, fake_fetch)
    _patch_no_sleep(monkeypatch, worker)

    stats = await worker._partsurfer_desc_pass(db_session, [card])
    db_session.commit()

    assert stats["fetched"] == 1
    assert stats["categorized"] == 0
    rows = _neg_rows(db_session)
    assert len(rows) == 1
    assert rows[0].reason == "no_result"
    assert isinstance(rows[0], PartsurferDescNegative)
    assert rows[0].spare_raw == "918042-601"


async def test_fresh_negative_suppresses_refetch(db_session: Session, monkeypatch):
    # A spare with a FRESH negative row is dropped before the fetch -- never re-queried
    # within the window. The fetcher must not be called for it.
    from app.services.enrichment_worker import worker
    from app.services.enrichment_worker.partsurfer_negative_cache import record_negative
    from app.utils.normalization import normalize_mpn_key

    seed_commodity_schemas(db_session)
    card = _hp_card(db_session, "918042-601")
    # Pre-seed a fresh no_result negative on the worker's actual key (normalize_mpn_key
    # strips the hyphen) -> retry_after 90d out.
    norm = normalize_mpn_key(card.display_mpn)
    record_negative(db_session, card.display_mpn, norm, "no_result")
    db_session.commit()

    fetched: list[str] = []

    async def fake_fetch(spare, **_kw):
        fetched.append(spare)
        return None

    _patch_fetch(monkeypatch, fake_fetch)
    _patch_no_sleep(monkeypatch, worker)

    stats = await worker._partsurfer_desc_pass(db_session, [card])
    db_session.commit()

    assert fetched == []  # suppressed by the negative cache
    assert stats["fetched"] == 0
    assert stats["skipped_cached"] == 1


async def test_stale_negative_is_retried_after_window(db_session: Session, monkeypatch):
    # A STALE negative row (retry_after in the past) does NOT suppress -- the spare is
    # re-fetched and the row refreshed in place (no duplicate).
    from datetime import datetime, timedelta

    from app.services.enrichment_worker import worker
    from app.services.enrichment_worker.partsurfer_negative_cache import record_negative
    from app.utils.normalization import normalize_mpn_key

    seed_commodity_schemas(db_session)
    card = _hp_card(db_session, "726719-B21")
    stale = datetime.now(UTC) - timedelta(days=100)  # past the 90d window
    record_negative(db_session, card.display_mpn, normalize_mpn_key(card.display_mpn), "no_result", now=stale)
    db_session.commit()

    fetched: list[str] = []

    async def fake_fetch(spare, **_kw):
        fetched.append(spare)
        return _DIMM_DESC  # now PartSurfer DOES return a description

    _patch_fetch(monkeypatch, fake_fetch)
    _patch_no_sleep(monkeypatch, worker)

    stats = await worker._partsurfer_desc_pass(db_session, [card])
    db_session.commit()

    assert fetched == ["726719-B21"]  # retried after the window
    assert stats["fetched"] == 1
    assert card.category == "dram"  # categorized this time
    # The stale row was consulted but the spare hit this time -> no NEW negative kept it,
    # and the prior stale row is not blocking anymore (one row max either way).
    assert len(_neg_rows(db_session)) <= 1


async def test_ungrammatical_description_is_short_cached(db_session: Session, monkeypatch):
    # Fetch SUCCEEDS but the grammar declines (categorize_and_record -> (False, 0)). That is
    # NOT a no-result: it is cached as 'ungrammatical' with the SHORT window, never long.
    from datetime import datetime

    from app.services.desc_extractor import writer as desc_writer
    from app.services.enrichment_worker import worker
    from app.services.enrichment_worker.partsurfer_negative_cache import (
        PARTSURFER_UNGRAMMATICAL_RETRY_DAYS,
    )

    seed_commodity_schemas(db_session)
    card = _hp_card(db_session, "918042-601")

    async def fake_fetch(spare, **_kw):
        return "SOME OPAQUE HP DESCRIPTION THE GRAMMAR DECLINES"

    _patch_fetch(monkeypatch, fake_fetch)
    _patch_no_sleep(monkeypatch, worker)
    monkeypatch.setattr(desc_writer, "categorize_and_record", lambda *a, **k: (False, 0))

    stats = await worker._partsurfer_desc_pass(db_session, [card])
    db_session.commit()

    assert stats["fetched"] == 1
    assert stats["categorized"] == 0
    rows = _neg_rows(db_session)
    assert len(rows) == 1
    assert rows[0].reason == "ungrammatical"
    # Short window: retry_after is ~14d out, NOT 90d.
    delta_days = (rows[0].retry_after - rows[0].looked_up_at).days
    assert delta_days == PARTSURFER_UNGRAMMATICAL_RETRY_DAYS
    # And it really does suppress a re-fetch within the short window.
    assert datetime.now(UTC) < rows[0].retry_after


async def test_transient_is_never_negative_cached(db_session: Session, monkeypatch):
    # A throttle/outage (PartSurferTransient) is NOT a verdict on the spare -- it must NOT
    # write a negative row (otherwise a transient outage would lock spares out for 90 days).
    from app.services.enrichment_worker import worker
    from app.services.enrichment_worker.partsurfer_resolver import PartSurferTransient

    seed_commodity_schemas(db_session)
    card = _hp_card(db_session, "918042-601")

    async def fake_fetch(spare, **_kw):
        raise PartSurferTransient("partsurfer 503 for 918042-601")

    _patch_fetch(monkeypatch, fake_fetch)
    _patch_no_sleep(monkeypatch, worker)

    stats = await worker._partsurfer_desc_pass(db_session, [card])
    db_session.commit()

    assert stats["fetched"] == 0
    assert _neg_rows(db_session) == []  # no negative row written for a transient


async def test_grammar_decline_with_db_failure_is_not_cached(db_session: Session, monkeypatch):
    # If the grammar would have run but the per-card write RAISED (IntegrityError), that is a
    # DB failure, not a grammar verdict -- it must NOT be cached as ungrammatical.
    from sqlalchemy.exc import IntegrityError

    from app.services.desc_extractor import writer as desc_writer
    from app.services.enrichment_worker import worker

    seed_commodity_schemas(db_session)
    card = _hp_card(db_session, "918042-601")

    async def fake_fetch(spare, **_kw):
        return _DIMM_DESC

    _patch_fetch(monkeypatch, fake_fetch)
    _patch_no_sleep(monkeypatch, worker)

    def boom(*a, **k):
        raise IntegrityError("simulated", {}, Exception("boom"))

    monkeypatch.setattr(desc_writer, "categorize_and_record", boom)

    stats = await worker._partsurfer_desc_pass(db_session, [card])
    db_session.commit()

    assert stats["fetched"] == 1
    assert stats["failed"] == 1
    assert _neg_rows(db_session) == []  # a DB failure is not negative-cached
