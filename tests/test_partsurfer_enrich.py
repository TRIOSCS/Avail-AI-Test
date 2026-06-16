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

    monkeypatch.setattr(worker, "fetch_partsurfer_description", fake_fetch, raising=False)
    import app.services.enrichment_worker.partsurfer_resolver as resolver_mod

    monkeypatch.setattr(resolver_mod, "fetch_partsurfer_description", fake_fetch)
    # Avoid the real 2s sleep between fetches.

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(worker.asyncio, "sleep", no_sleep)

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

    import app.services.enrichment_worker.partsurfer_resolver as resolver_mod

    monkeypatch.setattr(resolver_mod, "fetch_partsurfer_description", fake_fetch)

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(worker.asyncio, "sleep", no_sleep)
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

    import app.services.enrichment_worker.partsurfer_resolver as resolver_mod

    monkeypatch.setattr(resolver_mod, "fetch_partsurfer_description", fake_fetch)

    stats = await worker._partsurfer_desc_pass(db_session, [non_hp])
    assert stats == {"fetched": 0, "categorized": 0, "specs_written": 0}
    assert called is False
