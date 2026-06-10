"""Writer — oem_crosswalk_and_record_specs: spare-PN cards inherit category + specs from
their cached resolved PartSurfer/PSREF rows via record_spec (source="partsurfer"/
"psref", F1 ladder tier 80; decode channel 0.90, title channel 0.85), with the agreement
gate / category-mismatch skip / cross-ref dedupe / status-upgrade matrix / per-card
SAVEPOINT isolation guarantees stated inline on each test.

Resolution payloads mirror the recorded fixtures in tests/fixtures/oem_crosswalk/.
"""

from collections import Counter
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.constants import MaterialEnrichmentStatus, OemCrosswalkStatus
from app.models import MaterialCard, MaterialSpecFacet, OemCrosswalk
from app.services.commodity_registry import seed_commodity_schemas
from app.services.oem_crosswalk_enrich import (
    OEM_DECODE_CONFIDENCE,
    OEM_TITLE_CONFIDENCE,
    oem_crosswalk_and_record_specs,
)
from app.services.spec_write_service import record_spec
from app.utils.normalization import normalize_mpn_key

ZERO_STATS = Counter(
    matched=0,
    canonical_conflict=0,
    category_mismatch=0,
    categorized=0,
    decode_written=0,
    title_written=0,
    xref_added=0,
    status_upgraded=0,
    failed=0,
)


def _facets(db: Session, card_id: int) -> dict:
    rows = db.query(MaterialSpecFacet).filter_by(material_card_id=card_id).all()
    return {r.spec_key: (r.value_text if r.value_text is not None else r.value_numeric) for r in rows}


def _card(db: Session, mpn: str, category: str | None = None, **kw) -> MaterialCard:
    card = MaterialCard(normalized_mpn=mpn.lower(), display_mpn=mpn, category=category, **kw)
    db.add(card)
    db.flush()
    return card


def _row(
    db: Session,
    spare: str,
    canonical: str | None = "ST4000NM0035",
    mfg: str | None = "Seagate",
    title: str | None = None,
    vendor: str = "hpe",
    status: str = OemCrosswalkStatus.RESOLVED,
    domain: str = "partsurfer.hp.com",
    confidence: float | None = 0.95,
) -> OemCrosswalk:
    row = OemCrosswalk(
        spare_raw=spare,
        spare_norm=normalize_mpn_key(spare),
        vendor=vendor,
        status=status,
        canonical_mpn_raw=canonical,
        canonical_mpn_norm=normalize_mpn_key(canonical) if canonical else None,
        canonical_manufacturer=mfg,
        title=title,
        confidence=confidence,
        source_url=f"https://{domain}/Search.aspx?SearchText={spare}",
        source_domain=domain,
        looked_up_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.flush()
    return row


def test_decode_channel_writes_specs_categorizes_and_upgrades(db_session: Session):
    # The canonical Seagate model decodes deterministically: a NULL-category spare card
    # is categorized hdd, gets the decode's specs at partsurfer/0.90 (tier 80), an
    # audit cross-reference, and the oem_sourced status upgrade.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "695510-B21", category=None)
    row = _row(db_session, "695510-B21")

    stats = oem_crosswalk_and_record_specs(db_session, [card.id])
    db_session.commit()

    assert stats == Counter(
        ZERO_STATS,
        matched=1,
        categorized=1,
        decode_written=3,
        xref_added=1,
        status_upgraded=1,
    )
    assert card.category == "hdd"
    assert card.category_source == "partsurfer"
    f = _facets(db_session, card.id)
    assert f == {"capacity_gb": 4000, "form_factor": '3.5"', "usage_class": "Enterprise / Datacenter"}
    entry = card.specs_structured["capacity_gb"]
    assert entry["source"] == "partsurfer"
    assert entry["tier"] == 80
    assert entry["confidence"] == OEM_DECODE_CONFIDENCE == 0.90
    assert card.cross_references == [{"mpn": "ST4000NM0035", "manufacturer": "Seagate", "source": "partsurfer"}]
    assert card.enrichment_status == MaterialEnrichmentStatus.OEM_SOURCED
    assert card.enrichment_source == "partsurfer"
    assert card.enriched_at is not None
    prov = card.enrichment_provenance["oem_crosswalk"]
    assert prov["spare"] == "695510-B21"
    assert prov["canonical_mpn"] == "ST4000NM0035"
    assert prov["source_url"] == row.source_url
    assert prov["confidence"] == 0.95
    assert prov["fetched_at"]


def test_cpu_title_yields_all_six_facets(db_session: Session):
    # The CPU path today: the canonical Intel tray MPN has NO decoder, but the OEM page
    # title hits desc_extractor/cpu.py + cpu_model_specs.json → all six cpu facets at
    # partsurfer/0.85 (title channel). Never writes a category — the card brings cpu.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "875942-001", category="cpu")
    _row(
        db_session,
        "875942-001",
        canonical="CD8067303409000",
        mfg="Intel",
        title="Intel Xeon-Gold 6130 (2.1GHz/16-core/125W) FIO processor kit",
    )

    stats = oem_crosswalk_and_record_specs(db_session, [card.id])
    db_session.commit()

    assert stats["matched"] == 1
    assert stats["decode_written"] == 0  # no Intel CPU MPN decoder
    assert stats["title_written"] == 6
    assert stats["categorized"] == 0
    assert stats["status_upgraded"] == 1
    f = _facets(db_session, card.id)
    assert f["family"] == "Xeon"
    assert f["socket"] == "LGA3647"
    assert f["core_count"] == 16
    assert f["clock_speed_ghz"] == 2.1
    assert f["tdp_watts"] == 125
    assert f["architecture"] == "Skylake"
    assert card.specs_structured["socket"]["confidence"] == OEM_TITLE_CONFIDENCE == 0.85


def test_decode_beats_title_intra_tier(db_session: Session):
    # Same source/tier 80, decode 0.90 vs title 0.85: when the OEM title contradicts
    # the canonical MPN decode on a shared key, the ladder keeps the decode value —
    # arbitration by (tier, confidence), not run order.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "695510-B21", category="hdd")
    _row(db_session, "695510-B21", title='8TB 3.5" SAS 7.2K Midline hard drive')  # title claims 8TB

    stats = oem_crosswalk_and_record_specs(db_session, [card.id])
    db_session.commit()

    assert stats["decode_written"] == 3
    assert _facets(db_session, card.id)["capacity_gb"] == 4000  # decode's 0.90 won
    assert card.specs_structured["capacity_gb"]["confidence"] == 0.90


def test_loses_to_mpn_decode_prior_beats_web_search_prior(db_session: Session):
    # F1 ladder behavior: partsurfer (80) never overwrites an mpn_decode (85) prior and
    # always overwrites a web_search (70) prior, regardless of confidences.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "695510-B21", category="hdd")
    _row(db_session, "695510-B21")
    assert record_spec(db_session, card.id, "capacity_gb", 8000, source="mpn_decode", confidence=0.95)
    assert record_spec(db_session, card.id, "form_factor", '2.5"', source="web_search", confidence=0.99)

    stats = oem_crosswalk_and_record_specs(db_session, [card.id])
    db_session.commit()

    f = _facets(db_session, card.id)
    assert f["capacity_gb"] == 8000  # tier-85 prior kept
    assert f["form_factor"] == '3.5"'  # tier-70 prior overwritten
    assert card.specs_structured["form_factor"]["source"] == "partsurfer"
    # usage_class was free + form_factor overwrote: only capacity_gb lost the ladder.
    assert stats["decode_written"] == 2


def test_canonical_conflict_skips_card(db_session: Session):
    # Two resolved rows (different domains) that disagree on the canonical norm: the
    # strict-intersect spirit — assert NOTHING (no specs, no xref, no status).
    seed_commodity_schemas(db_session)
    card = _card(db_session, "695510-B21", category=None)
    _row(db_session, "695510-B21", canonical="ST4000NM0035", domain="partsurfer.hp.com")
    _row(db_session, "695510-B21", canonical="ST8000NM0055", domain="parts.hp.com")

    stats = oem_crosswalk_and_record_specs(db_session, [card.id])

    assert stats["matched"] == 1
    assert stats["canonical_conflict"] == 1
    assert stats["decode_written"] == 0
    assert stats["xref_added"] == 0
    assert card.category is None
    assert card.enrichment_status == MaterialEnrichmentStatus.UNENRICHED
    assert _facets(db_session, card.id) == {}


def test_agreeing_rows_across_domains_write_once(db_session: Session):
    # Multiple resolved rows that AGREE on the canonical norm are fine — the highest-
    # confidence row is picked deterministically and the card writes once.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "695510-B21", category=None)
    _row(db_session, "695510-B21", domain="partsurfer.hp.com", confidence=0.92)
    _row(db_session, "695510-B21", domain="parts.hp.com", confidence=0.97)

    stats = oem_crosswalk_and_record_specs(db_session, [card.id])

    assert stats["canonical_conflict"] == 0
    assert stats["decode_written"] == 3
    assert card.enrichment_provenance["oem_crosswalk"]["confidence"] == 0.97  # best row picked


def test_category_mismatch_skips_card_entirely(db_session: Session):
    # An existing category is authoritative — a dram card whose canonical decodes hdd
    # gets NOTHING (no specs from either channel, no xref, no status change).
    seed_commodity_schemas(db_session)
    card = _card(db_session, "695510-B21", category="dram")
    _row(db_session, "695510-B21", title='4TB 3.5" SAS Midline hard drive')

    stats = oem_crosswalk_and_record_specs(db_session, [card.id])

    assert stats["category_mismatch"] == 1
    assert stats["decode_written"] == 0
    assert stats["title_written"] == 0
    assert stats["xref_added"] == 0
    assert card.category == "dram"
    assert card.cross_references in (None, [])
    assert _facets(db_session, card.id) == {}


def test_status_upgrade_matrix(db_session: Session):
    # unenriched / not_found / not_catalogued → oem_sourced; verified untouched (the
    # specs still write — only the status uplift is gated).
    seed_commodity_schemas(db_session)
    cases = {
        MaterialEnrichmentStatus.UNENRICHED: MaterialEnrichmentStatus.OEM_SOURCED,
        MaterialEnrichmentStatus.NOT_FOUND: MaterialEnrichmentStatus.OEM_SOURCED,
        MaterialEnrichmentStatus.NOT_CATALOGUED: MaterialEnrichmentStatus.OEM_SOURCED,
        MaterialEnrichmentStatus.VERIFIED: MaterialEnrichmentStatus.VERIFIED,
    }
    cards = {}
    for i, status in enumerate(cases):
        spare = f"69551{i}-B21"
        cards[status] = _card(db_session, spare, category=None, enrichment_status=status)
        _row(db_session, spare)

    stats = oem_crosswalk_and_record_specs(db_session, [c.id for c in cards.values()])

    assert stats["status_upgraded"] == 3
    for status, expected in cases.items():
        assert cards[status].enrichment_status == expected, status
        assert _facets(db_session, cards[status].id) != {}  # specs written regardless


def test_no_write_no_status_upgrade_but_xref_recorded(db_session: Session):
    # A canonical that neither decodes nor title-parses writes no category/specs → the
    # status must stay untouched; the crosswalk linkage itself is still recorded.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "695510-B21", category=None)
    _row(db_session, "695510-B21", canonical="NOTDECODABLE99", mfg=None, title=None)

    stats = oem_crosswalk_and_record_specs(db_session, [card.id])

    assert stats["status_upgraded"] == 0
    assert stats["xref_added"] == 1
    assert card.enrichment_status == MaterialEnrichmentStatus.UNENRICHED
    assert card.cross_references == [{"mpn": "NOTDECODABLE99", "manufacturer": None, "source": "partsurfer"}]


def test_cross_references_dedupe_on_norm_and_source(db_session: Session):
    # Running the pass twice must not duplicate the cross-reference entry.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "695510-B21", category=None)
    _row(db_session, "695510-B21")

    s1 = oem_crosswalk_and_record_specs(db_session, [card.id])
    s2 = oem_crosswalk_and_record_specs(db_session, [card.id])

    assert s1["xref_added"] == 1
    assert s2["xref_added"] == 0
    assert len(card.cross_references) == 1


def test_savepoint_isolation_poison_card_fails_alone(db_session: Session):
    # A record_spec blow-up on one card rolls back ONLY that card's writes (per-card
    # SAVEPOINT); the sibling card still gets its full enrichment.
    seed_commodity_schemas(db_session)
    poison = _card(db_session, "695510-B21", category=None)
    healthy = _card(db_session, "695511-B21", category=None)
    _row(db_session, "695510-B21")
    _row(db_session, "695511-B21")

    real_record_spec = record_spec

    def exploding(db, card_id, *args, **kwargs):
        if card_id == poison.id:
            raise RuntimeError("poison row")
        return real_record_spec(db, card_id, *args, **kwargs)

    with patch("app.services.oem_crosswalk_enrich.record_spec", side_effect=exploding):
        stats = oem_crosswalk_and_record_specs(db_session, [poison.id, healthy.id])
    db_session.commit()

    assert stats["failed"] == 1
    assert stats["decode_written"] == 3  # the healthy card's writes survived
    assert _facets(db_session, poison.id) == {}
    assert poison.enrichment_status == MaterialEnrichmentStatus.UNENRICHED
    assert _facets(db_session, healthy.id) != {}
    assert healthy.enrichment_status == MaterialEnrichmentStatus.OEM_SOURCED


def test_psref_source_for_lenovo_rows(db_session: Session):
    # Phase B readiness: a vendor='lenovo' row writes at source "psref" (same tier 80).
    seed_commodity_schemas(db_session)
    card = _card(db_session, "01HW917", category=None)
    _row(db_session, "01HW917", vendor="lenovo", domain="psref.lenovo.com")

    oem_crosswalk_and_record_specs(db_session, [card.id])

    assert card.category_source == "psref"
    assert card.enrichment_source == "psref"
    assert card.cross_references[0]["source"] == "psref"


def test_no_match_rows_and_unmatched_cards_are_ignored(db_session: Session):
    # no_match rows are a negative cache, never write evidence; cards without any
    # crosswalk row pass through untouched.
    seed_commodity_schemas(db_session)
    negative = _card(db_session, "695510-B21", category=None)
    unmatched = _card(db_session, "918042-601", category=None)
    _row(db_session, "695510-B21", status=OemCrosswalkStatus.NO_MATCH, canonical=None, mfg=None, confidence=None)

    stats = oem_crosswalk_and_record_specs(db_session, [negative.id, unmatched.id])

    assert stats == ZERO_STATS
    assert negative.enrichment_status == MaterialEnrichmentStatus.UNENRICHED
    assert _facets(db_session, negative.id) == {}
