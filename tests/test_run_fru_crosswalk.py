"""Targeted FRU-graph drain CLI (app.management.run_fru_crosswalk, §2.6).

Covers:
  - PHASE A drain: selects linked-but-unfaceted/uncategorized cards only; dry-run reports
    a real yield WITHOUT persisting; --apply persists.
  - PHASE B card creation: dangling enrichable FRUs + dangling canonical models become
    cards; the lenovo_ppn danglers are SKIPPED; dry-run counts without inserting.
  - §2.6(c) drive_pn decode-widening misread gate logic (pass / fail / unverifiable).
  - §2.6(d) deterministic maker propagation: positive (unanimous vendor → maker set) and
    the negative (vendor disagreement → specs write but NO maker).

Real decoding MPNs used: ST4000NM0035 / ST8000NM0055 (Seagate hdd), HUS726040ALE610
(HGST hdd), MZ7LH960HAJR (Samsung ssd). drive_pn related parts are IBM FRU numbers
(00VN…) that do NOT decode — exactly the live-data shape.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.constants import FruLinkKind, MaterialEnrichmentStatus
from app.management.run_fru_crosswalk import (
    collect_creatable_cards,
    measure_drive_pn_misreads,
    run_create,
    run_drain,
    select_drain_card_ids,
)
from app.models import FruLink, MaterialCard, MaterialSpecFacet
from app.services.commodity_registry import seed_commodity_schemas
from app.services.fru_crosswalk_enrich import crosswalk_and_record_specs
from app.utils.normalization import normalize_mpn_key


def _card(db: Session, mpn: str, category: str | None = None, **kw) -> MaterialCard:
    card = MaterialCard(normalized_mpn=normalize_mpn_key(mpn), display_mpn=mpn, category=category, **kw)
    db.add(card)
    db.flush()
    return card


def _link(
    db: Session,
    fru: str,
    related: str,
    *,
    mfg: str | None = "Seagate",
    kind: str = FruLinkKind.MFG_MODEL.value,
    sheet: str = "Main",
    description: str | None = None,
) -> FruLink:
    link = FruLink(
        fru_raw=fru,
        fru_norm=normalize_mpn_key(fru),
        related_raw=related,
        related_norm=normalize_mpn_key(related),
        rel_kind=kind,
        manufacturer=mfg,
        description=description,
        source_sheet=sheet,
    )
    db.add(link)
    db.flush()
    return link


# ── PHASE A: targeted drain ─────────────────────────────────────────────────────────


def test_drain_selects_only_linked_unfaceted_or_uncategorized(db_session: Session):
    seed_commodity_schemas(db_session)
    # In scope: linked + uncategorized.
    linked_uncat = _card(db_session, "00AJ141", category=None)
    _link(db_session, "00AJ141", "ST4000NM0035")
    # In scope: linked + categorized but unfaceted.
    linked_unfaceted = _card(db_session, "00AJ200", category="hdd")
    _link(db_session, "00AJ200", "ST8000NM0055")
    # OUT of scope: linked but already faceted AND categorized.
    faceted = _card(db_session, "00AJ300", category="hdd")
    _link(db_session, "00AJ300", "ST4000NM0035")
    db_session.add(
        MaterialSpecFacet(material_card_id=faceted.id, category="hdd", spec_key="form_factor", value_text='3.5"')
    )
    # OUT of scope: unfaceted+uncategorized but NO FRU link.
    _card(db_session, "ZZZ999", category=None)
    db_session.flush()

    ids = set(select_drain_card_ids(db_session))
    assert ids == {linked_uncat.id, linked_unfaceted.id}


def test_drain_dry_run_reports_yield_without_persisting(db_session: Session):
    seed_commodity_schemas(db_session)
    card = _card(db_session, "00AJ141", category=None)
    _link(db_session, "00AJ141", "ST4000NM0035")  # decodes hdd, Seagate
    db_session.flush()

    summary = run_drain(db_session, apply=False)
    assert summary["mode"] == "dry-run"
    assert summary["candidates"] == 1
    assert summary["stats"]["decoded"] == 1
    assert summary["stats"]["categorized"] == 1
    assert summary["stats"]["manufacturers_set"] == 1
    # Nothing persisted — the savepoint rolled back.
    db_session.expire_all()
    assert db_session.get(MaterialCard, card.id).category is None
    assert db_session.query(MaterialSpecFacet).count() == 0


def test_drain_apply_persists_specs_category_and_maker(db_session: Session):
    seed_commodity_schemas(db_session)
    card = _card(db_session, "00AJ141", category=None)
    _link(db_session, "00AJ141", "ST4000NM0035")
    db_session.flush()

    summary = run_drain(db_session, apply=True)
    assert summary["mode"] == "apply"
    assert summary["stats"]["written"] >= 1

    db_session.expire_all()
    refreshed = db_session.get(MaterialCard, card.id)
    assert refreshed.category == "hdd"
    assert refreshed.manufacturer == "Seagate"  # §2.6(d) deterministic maker
    assert db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id).count() >= 1


def test_drain_no_candidates(db_session: Session):
    seed_commodity_schemas(db_session)
    summary = run_drain(db_session, apply=False)
    assert summary == {"mode": "dry-run", "candidates": 0, "stats": {}}


# ── PHASE B: dangling-card creation ─────────────────────────────────────────────────


def test_create_skips_lenovo_ppn_danglers(db_session: Session):
    seed_commodity_schemas(db_session)
    # A FRU that already has a card, with a dangling lenovo_ppn (out of scope) and a
    # dangling canonical model (in scope).
    _card(db_session, "00AJ141", category="hdd")
    _link(db_session, "00AJ141", "ST4000NM0035")  # dangling canonical model — creatable
    _link(db_session, "00AJ141", "0000000NV340_E00", kind=FruLinkKind.LENOVO_PPN.value, mfg=None)
    db_session.flush()

    plan = collect_creatable_cards(db_session)
    keys = set(plan)
    assert normalize_mpn_key("ST4000NM0035") in keys
    # The lenovo_ppn dangler is NEVER collected.
    assert normalize_mpn_key("0000000NV340_E00") not in keys
    assert all(plan[k]["reason"] == "canonical_model" for k in keys)


def test_create_collects_dangling_canonical_models_and_enrichable_frus(db_session: Session):
    seed_commodity_schemas(db_session)
    # Dangling FRU (no card) with an enrichable mfg_model link → both the FRU (b1) and the
    # canonical model (b2) are creatable.
    _link(db_session, "00AAA01", "ST4000NM0035")
    db_session.flush()

    plan = collect_creatable_cards(db_session)
    assert plan[normalize_mpn_key("00AAA01")]["reason"] == "enrichable_fru"
    assert plan[normalize_mpn_key("ST4000NM0035")]["reason"] == "canonical_model"


def test_create_skips_non_enrichable_dangling_fru(db_session: Session):
    seed_commodity_schemas(db_session)
    # A dangling FRU whose only link is a non-decoding IBM FRU drive_pn with no description
    # → not enrichable → no FRU card, no canonical card.
    _link(db_session, "49Y9999", "00VN528", kind=FruLinkKind.DRIVE_PN.value, mfg=None, description=None)
    db_session.flush()

    plan = collect_creatable_cards(db_session)
    assert plan == {}


def test_create_skips_parts_that_already_have_cards(db_session: Session):
    seed_commodity_schemas(db_session)
    _card(db_session, "ST4000NM0035", category="hdd")  # canonical model already a card
    _card(db_session, "00AJ141", category="hdd")  # FRU already a card
    _link(db_session, "00AJ141", "ST4000NM0035")
    db_session.flush()

    plan = collect_creatable_cards(db_session)
    assert plan == {}


def test_create_apply_inserts_unenriched_null_category_cards(db_session: Session):
    seed_commodity_schemas(db_session)
    _link(db_session, "00AAA01", "ST4000NM0035")
    db_session.flush()

    summary = run_create(db_session, apply=True)
    assert summary["mode"] == "apply"
    assert summary["created"] == 2  # the FRU + the canonical model
    db_session.expire_all()

    created = db_session.execute(
        select(MaterialCard).where(MaterialCard.normalized_mpn == normalize_mpn_key("ST4000NM0035"))
    ).scalar_one()
    assert created.category is None
    assert created.enrichment_status == MaterialEnrichmentStatus.UNENRICHED.value


def test_create_dry_run_does_not_insert(db_session: Session):
    seed_commodity_schemas(db_session)
    _link(db_session, "00AAA01", "ST4000NM0035")
    db_session.flush()
    before = db_session.query(MaterialCard).count()

    summary = run_create(db_session, apply=False)
    assert summary["mode"] == "dry-run"
    assert summary["creatable"] == 2
    assert summary["created"] == 0
    assert db_session.query(MaterialCard).count() == before


# ── §2.6(c): drive_pn decode-widening misread gate ──────────────────────────────────


def test_measure_drive_pn_gate_passes_when_nothing_decodes(db_session: Session):
    seed_commodity_schemas(db_session)
    # Live shape: drive_pn related parts are IBM FRU numbers that never decode.
    _link(
        db_session,
        "49Y7443",
        "00VN528",
        kind=FruLinkKind.DRIVE_PN.value,
        mfg=None,
        description="HDD; Seagate; 14000GB; 3.5; 7200 RPM; SAS",
    )
    db_session.flush()

    result = measure_drive_pn_misreads(db_session, sample=100)
    assert result["decoded"] == 0
    assert result["misread"] == 0
    assert result["misread_pct"] == 0.0
    assert result["passes"] is True  # 0% <= 2% gate


def test_measure_drive_pn_gate_counts_misread_on_contradiction(db_session: Session):
    seed_commodity_schemas(db_session)
    # A drive_pn whose related part DECODES (a real Seagate MPN) but whose qual-sheet
    # description contradicts the decode → a misread. The decode is hdd/Seagate/4000GB;
    # the prose says SSD → commodity contradiction.
    _link(
        db_session,
        "49Y7443",
        "ST4000NM0035",
        kind=FruLinkKind.DRIVE_PN.value,
        mfg="Seagate",
        description="SSD; Samsung; 960GB; 2.5; SATA",
    )
    db_session.flush()

    result = measure_drive_pn_misreads(db_session, sample=100)
    assert result["decoded"] == 1
    assert result["misread"] == 1
    assert result["misread_pct"] == 100.0
    assert result["passes"] is False  # 100% > 2% gate


def test_measure_drive_pn_unverifiable_when_no_description(db_session: Session):
    seed_commodity_schemas(db_session)
    # Decodes but has no description to check against → unverifiable (not pass, not misread).
    _link(
        db_session,
        "49Y7443",
        "ST4000NM0035",
        kind=FruLinkKind.DRIVE_PN.value,
        mfg="Seagate",
        description=None,
    )
    db_session.flush()

    result = measure_drive_pn_misreads(db_session, sample=100)
    assert result["decoded"] == 1
    assert result["misread"] == 0
    assert result["unverifiable"] == 1
    assert result["passes"] is True


def test_measure_drive_pn_no_misread_when_decode_agrees_with_prose(db_session: Session):
    seed_commodity_schemas(db_session)
    # Decodes hdd/4000GB and the prose agrees (hdd, 4000GB) → not a misread.
    _link(
        db_session,
        "49Y7443",
        "ST4000NM0035",
        kind=FruLinkKind.DRIVE_PN.value,
        mfg="Seagate",
        description="HDD; Seagate; 4000GB; 3.5; 7200 RPM; SAS",
    )
    db_session.flush()

    result = measure_drive_pn_misreads(db_session, sample=100)
    assert result["decoded"] == 1
    assert result["misread"] == 0
    assert result["passes"] is True


# ── §2.6(c): drive_pn decode-WIDENING flag gates the decode channel ─────────────────


def test_drive_pn_decode_widening_flag_includes_decoding_drive_pn(db_session: Session, monkeypatch):
    # Flag ON (default): a drive_pn link whose related part DECODES feeds the decode
    # channel → the card is decoded + categorized from it.
    seed_commodity_schemas(db_session)
    monkeypatch.setattr(settings, "fru_crosswalk_drive_pn_decode_enabled", True)
    card = _card(db_session, "00AJ141", category=None)
    _link(db_session, "00AJ141", "ST4000NM0035", kind=FruLinkKind.DRIVE_PN.value, mfg="Seagate")
    db_session.flush()

    stats = crosswalk_and_record_specs(db_session, [card.id])
    db_session.commit()
    assert stats["decoded"] == 1
    assert stats["categorized"] == 1
    assert db_session.get(MaterialCard, card.id).category == "hdd"


def test_drive_pn_decode_widening_flag_off_excludes_drive_pn_from_decode(db_session: Session, monkeypatch):
    # Flag OFF: the SAME decoding drive_pn link is NOT decoded (only mfg_model feeds the
    # decode channel). With no decodable model and no description, the card gets nothing.
    seed_commodity_schemas(db_session)
    monkeypatch.setattr(settings, "fru_crosswalk_drive_pn_decode_enabled", False)
    card = _card(db_session, "00AJ141", category=None)
    _link(db_session, "00AJ141", "ST4000NM0035", kind=FruLinkKind.DRIVE_PN.value, mfg="Seagate")
    db_session.flush()

    stats = crosswalk_and_record_specs(db_session, [card.id])
    db_session.commit()
    assert stats["decoded"] == 0
    assert stats["categorized"] == 0
    assert db_session.get(MaterialCard, card.id).category is None


# ── §2.6(d): deterministic maker propagation (positive + negative) ───────────────────


def test_maker_propagated_when_all_substitutes_agree(db_session: Session):
    seed_commodity_schemas(db_session)
    # Two Seagate models → unanimous vendor → maker set deterministically at tier 84.
    card = _card(db_session, "00AJ141", category=None)
    _link(db_session, "00AJ141", "ST4000NM0035")
    _link(db_session, "00AJ141", "ST8000NM0055")
    db_session.flush()

    summary = run_drain(db_session, apply=True)
    assert summary["stats"]["manufacturers_set"] == 1
    db_session.expire_all()
    refreshed = db_session.get(MaterialCard, card.id)
    assert refreshed.manufacturer == "Seagate"
    assert refreshed.manufacturer_source == "fru_matrix_decode"
    assert refreshed.manufacturer_tier == 84


def test_maker_not_propagated_when_vendors_disagree(db_session: Session):
    seed_commodity_schemas(db_session)
    # Two HDDs that AGREE on commodity (hdd, form_factor, usage_class) but DISAGREE on
    # vendor (Seagate vs HGST) → specs still write, but NO maker is inferred (D4: never
    # guess a maker when the deterministic decoders disagree).
    card = _card(db_session, "00AJ141", category=None, manufacturer="IBM")
    _link(db_session, "00AJ141", "ST4000NM0035", mfg="Seagate")
    _link(db_session, "00AJ141", "HUS726040ALE610", mfg="HGST")
    db_session.flush()

    summary = run_drain(db_session, apply=True)
    assert summary["stats"]["written"] >= 1  # shared specs still land
    assert summary["stats"]["manufacturers_set"] == 0
    db_session.expire_all()
    refreshed = db_session.get(MaterialCard, card.id)
    # The legacy IBM label is untouched — no deterministic maker to upgrade it with.
    assert refreshed.manufacturer == "IBM"
