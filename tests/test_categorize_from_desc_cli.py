"""The categorize-from-desc one-shot CLI: dry-run yield, real-desc gate, channels, --apply.

Dry-run must write NOTHING while reporting the would-categorize yield (per channel + per
resulting category). --apply categorizes through the ladder, fills facets, and logs an
audit row per card. Fixtures use category=None — never hand-set an off-vocab category.
"""

from sqlalchemy.orm import Session

from app.management.categorize_from_desc import _alnum_norm, _has_real_own_desc, run
from app.models import MaterialCard, MaterialCardAudit, MaterialSpecFacet
from app.models.fru_link import FruLink
from app.services.commodity_registry import seed_commodity_schemas


def _card(db: Session, mpn: str, description: str | None, category=None) -> MaterialCard:
    card = MaterialCard(normalized_mpn=mpn.lower(), display_mpn=mpn, category=category, description=description)
    db.add(card)
    db.flush()
    return card


def _facets(db: Session, card_id: int) -> dict:
    rows = db.query(MaterialSpecFacet).filter_by(material_card_id=card_id).all()
    return {r.spec_key: (r.value_text if r.value_text is not None else r.value_numeric) for r in rows}


def test_real_desc_gate():
    # The MPN-as-description rows (desc == display_mpn, alphanumeric) are NOT real.
    assert _alnum_norm("00-AR-327 ") == "00ar327"
    real = MaterialCard(normalized_mpn="x", display_mpn="00AR327", description='HD, 450GB, 15KRPM, 3.5", FC')
    mpn_echo = MaterialCard(normalized_mpn="y", display_mpn="00AR327", description="00AR327")
    short = MaterialCard(normalized_mpn="z", display_mpn="ABC", description="HDD")
    assert _has_real_own_desc(real) is True
    assert _has_real_own_desc(mpn_echo) is False
    assert _has_real_own_desc(short) is False


def test_dry_run_writes_nothing_but_reports_yield(db_session: Session):
    seed_commodity_schemas(db_session)
    _card(db_session, "00AR327", 'HD, 450GB, 15KRPM, 3.5", Fibre Channel')  # -> hdd
    _card(db_session, "01KL563", "PSU, 1460W 240V/200V AC Hot Swap")  # -> power_supplies
    _card(db_session, "CBL55", "CABLE, LVDS 40-pin display harness 500mm")  # -> cables
    _card(db_session, "JUNK1", "JUNK1")  # MPN-as-desc -> skipped_no_desc
    _card(db_session, "ALREADY", "HDD, 1TB drive", category="hdd")  # already categorized -> not selected
    db_session.commit()

    summary = run(db_session, apply=False)

    # Dry-run reports the yield but writes nothing.
    assert summary["mode"] == "dry-run"
    assert summary["categorized"] == 3
    assert summary["by_category"] == {"hdd": 1, "power_supplies": 1, "cables": 1}
    assert summary["by_channel"] == {"own_desc": 3}
    assert summary["skipped_no_desc"] == 1
    # NOTHING persisted: no facets, no category mutations survived the rollback.
    db_session.rollback()
    assert db_session.query(MaterialSpecFacet).count() == 0
    for mpn in ("00ar327", "01kl563", "cbl55"):
        card = db_session.query(MaterialCard).filter_by(normalized_mpn=mpn).one()
        assert card.category is None


def test_apply_categorizes_through_ladder_and_audits(db_session: Session):
    seed_commodity_schemas(db_session)
    _card(db_session, "00AR327", 'HD, 450GB, 15KRPM, 3.5", Fibre Channel')
    db_session.commit()

    summary = run(db_session, apply=True)

    assert summary["mode"] == "apply"
    assert summary["categorized"] == 1
    assert summary["specs_written"] >= 1
    card = db_session.query(MaterialCard).filter_by(normalized_mpn="00ar327").one()
    assert card.category == "hdd"
    assert card.category_source == "desc_parse"
    assert card.category_tier == 83
    assert _facets(db_session, card.id)["capacity_gb"] == 450
    # One audit row per categorized card.
    audit = db_session.query(MaterialCardAudit).filter_by(material_card_id=card.id, action="categorized").one()
    assert audit.created_by == "categorize_from_desc"
    assert audit.details["category"] == "hdd"
    assert audit.details["channel"] == "own_desc"
    assert audit.details["tier"] == 83


def test_fru_desc_channel_when_own_desc_is_unusable(db_session: Session):
    # A card whose own description is just the MPN, but with a linked fru_links row that
    # carries a real description, categorizes via the fru_desc channel (tier 82).
    seed_commodity_schemas(db_session)
    card = _card(db_session, "FRU100", "FRU100")  # own desc == MPN
    db_session.add(
        FruLink(
            fru_raw="FRU100",
            fru_norm="fru100",
            related_raw="ST1200MM0017",
            related_norm="st1200mm0017",
            rel_kind="mfg_model",
            description="HDD, 6Gbps 1.2TB 10K 2.5 Inch HDD, IBM",
            source_sheet="test",
        )
    )
    db_session.commit()

    summary = run(db_session, apply=True)

    assert summary["categorized"] == 1
    assert summary["by_channel"] == {"fru_desc": 1}
    db_session.refresh(card)
    assert card.category == "hdd"
    assert card.category_source == "fru_desc_parse"
    assert card.category_tier == 82


def test_limit_caps_examined_cards(db_session: Session):
    seed_commodity_schemas(db_session)
    for i in range(5):
        _card(db_session, f"LIM{i}", 'HD, 450GB, 15KRPM, 3.5", Fibre Channel')
    db_session.commit()

    summary = run(db_session, apply=False, limit=2)
    assert summary["cards_examined"] == 2
    db_session.rollback()


def test_soft_deleted_cards_excluded(db_session: Session):
    from datetime import datetime, timezone

    seed_commodity_schemas(db_session)
    live = _card(db_session, "LIVE1", 'HD, 450GB, 15KRPM, 3.5", Fibre Channel')
    dead = _card(db_session, "DEAD1", 'HD, 600GB, 10KRPM, 2.5", SAS')
    dead.deleted_at = datetime.now(timezone.utc)
    db_session.commit()

    summary = run(db_session, apply=True)
    assert summary["categorized"] == 1
    db_session.refresh(live)
    db_session.refresh(dead)
    assert live.category == "hdd"
    assert dead.category is None
