"""test_seed_sample_data.py — Tests for the AvailAI sample-data seeder.

Verifies the management command at app/management/seed_sample_data.py:
  * a fresh seed creates the expected entities (spot-check counts/links per workflow),
  * a SECOND seed run is idempotent (no duplicate rows, counts stable),
  * --wipe removes ALL sample rows while leaving a pre-existing non-sample row
    untouched.

Runs under TESTING=1 against the in-memory SQLite engine from conftest (db_session
fixture). Calls the seeder's seed()/wipe() functions directly with the test session
— no live email/Graph/supplier/MCP effects (the seeder constructs all rows directly).

Called by: pytest autodiscovery.
Depends on: tests.conftest fixtures, app.management.seed_sample_data, ORM models.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.management import seed_sample_data as sds
from app.models.auth import User
from app.models.buy_plan import BuyPlan, VerificationGroupMember
from app.models.crm import Company, CustomerSite, SiteContact
from app.models.excess import (
    BuyerScore,
    CustomerBid,
    CustomerBidLine,
    ExcessLineItem,
    ExcessList,
    ExcessOffer,
    ExcessOfferLine,
    ExcessOutreach,
)
from app.models.intelligence import ActivityLog, MaterialCard
from app.models.offers import Offer
from app.models.quotes import Quote, QuoteLine
from app.models.sourcing import Requirement, Requisition, Sighting
from app.models.task import RequisitionTask
from app.models.vendor_part_unavailability import VendorPartUnavailability
from app.models.vendors import VendorCard, VendorContact


def _count(db: Session, model: type) -> int:
    return db.query(model).count()


def _all_counts(db: Session) -> dict[str, int]:
    models = [
        User,
        Company,
        CustomerSite,
        SiteContact,
        VendorCard,
        VendorContact,
        MaterialCard,
        Requisition,
        Requirement,
        Sighting,
        Offer,
        Quote,
        QuoteLine,
        BuyPlan,
        VerificationGroupMember,
        ExcessList,
        ExcessLineItem,
        ExcessOffer,
        ExcessOfferLine,
        CustomerBid,
        CustomerBidLine,
        ExcessOutreach,
        BuyerScore,
        ActivityLog,
        RequisitionTask,
        VendorPartUnavailability,
    ]
    return {m.__name__: _count(db, m) for m in models}


def test_fresh_seed_creates_expected_entities(db_session: Session) -> None:
    """A fresh seed builds the full cast and the documented per-workflow records."""
    sds.seed(db_session)

    # Named cast (§2).
    assert _count(db_session, User) == 6
    assert _count(db_session, Company) == 6
    assert _count(db_session, CustomerSite) == 4
    assert _count(db_session, SiteContact) == 5
    assert _count(db_session, VendorCard) == 6
    assert _count(db_session, VendorContact) == 6
    assert _count(db_session, MaterialCard) == 3
    assert _count(db_session, VerificationGroupMember) == 1

    # Requisitions: req1/req2/req3 + the excess-demand scratch req.
    assert _count(db_session, Requisition) == 4

    # Excess / resell (WF-E).
    assert _count(db_session, ExcessList) == 1
    assert _count(db_session, ExcessLineItem) == 3
    assert _count(db_session, ExcessOffer) == 4  # 3 per-line + 1 take-all
    assert _count(db_session, ExcessOfferLine) == 6
    assert _count(db_session, CustomerBid) == 1
    assert _count(db_session, CustomerBidLine) == 2
    assert _count(db_session, ExcessOutreach) == 3
    assert _count(db_session, BuyerScore) == 3

    # My Day timeline.
    assert _count(db_session, ActivityLog) == 8
    assert _count(db_session, RequisitionTask) == 4

    # Vendor intelligence.
    assert _count(db_session, VendorPartUnavailability) == 4

    # Quotes (Q-0001..0005).
    assert _count(db_session, Quote) == 5


def test_material_card_uses_f1_ladder(db_session: Session) -> None:
    """Category/brand/manufacturer land via the ladder; mc_bare stays NULL-category."""
    sds.seed(db_session)

    mc_mcu = db_session.query(MaterialCard).filter_by(normalized_mpn="avsamplestm32f103rb").one()
    assert mc_mcu.category == "microcontrollers"
    assert mc_mcu.category_source == sds.SEED_SRC
    assert mc_mcu.brand == "STMicroelectronics"
    # Manual ST Micro then higher-tier STMicroelectronics → recorded conflict.
    assert mc_mcu.has_validation_conflict is True

    mc_bare = db_session.query(MaterialCard).filter_by(normalized_mpn="avsamplelegacy01").one()
    assert mc_bare.category is None  # legacy floor; record_spec returned False (no category)


def test_material_card_structured_specs_persist(db_session: Session) -> None:
    """mc_mcu (2 specs) and mc_conn (1 spec) actually persist through record_spec.

    Guards the §8 coverage claim: the seeder self-seeds commodity_spec_schemas and uses
    enum-VALID values ("Cortex-M3", "SOIC-8" — not "ARM Cortex-M3"/"SOIC16"), so the
    dossier specs panel is non-empty. Without both fixes record_spec returns False and
    specs_structured stays None — this asserts it cannot silently regress.
    """
    sds.seed(db_session)

    mc_mcu = db_session.query(MaterialCard).filter_by(normalized_mpn="avsamplestm32f103rb").one()
    assert mc_mcu.specs_structured, "mc_mcu structured specs must not be empty"
    assert set(mc_mcu.specs_structured) >= {"package", "core"}
    assert mc_mcu.specs_structured["package"]["value"] == "LQFP48"
    assert mc_mcu.specs_structured["core"]["value"] == "Cortex-M3"

    mc_conn = db_session.query(MaterialCard).filter_by(normalized_mpn="avsamplemax3232").one()
    assert mc_conn.specs_structured, "mc_conn structured specs must not be empty"
    assert mc_conn.specs_structured["package"]["value"] == "SOIC-8"


def test_offer_qualification_and_buy_plan_links(db_session: Session) -> None:
    """Offers get qualification stamped; the active buy plan carries an AI flag."""
    sds.seed(db_session)

    # The COMPLETE-qualification offer (o1) is selected for quote.
    o1 = (
        db_session.query(Offer)
        .filter_by(mpn="AVSAMPLE-STM32F103RB", vendor_name="AVSAMPLE Pinnacle Components", unit_price=50.0)
        .one()
    )
    assert o1.qualification_status is not None
    assert o1.selected_for_quote is True

    # The ACTIVE plan (on q_won2) has the WARNING price_increase ai_flag.
    q_won2 = db_session.query(Quote).filter_by(quote_number="AVSAMPLE-Q-0005").one()
    bp_active = db_session.query(BuyPlan).filter_by(quote_id=q_won2.id).one()
    assert bp_active.status == "active"
    assert bp_active.ai_flags and bp_active.ai_flags[0]["type"] == "price_increase"
    assert bp_active.ai_flags[0]["line_id"] is not None


def test_second_seed_is_idempotent(db_session: Session) -> None:
    """Re-running the seeder creates nothing new — counts are stable."""
    sds.seed(db_session)
    before = _all_counts(db_session)

    counts = sds.seed(db_session)
    after = _all_counts(db_session)

    assert after == before, "second seed changed row counts"
    # The tally must report zero created on the second run.
    total_created = sum(b["created"] for b in counts.values())
    assert total_created == 0, f"second run created {total_created} rows"


def test_wipe_removes_sample_rows_only(db_session: Session) -> None:
    """--wipe deletes every sample row but leaves a pre-existing real row untouched."""
    # Pre-existing NON-sample data that must survive the wipe.
    real_user = User(email="real.person@trioscs.com", name="Real Person", role="buyer", is_active=True)
    real_company = Company(name="Real Customer Inc", account_type="Customer", source="sfdc", is_active=True)
    real_card = MaterialCard(normalized_mpn="lm358n", display_mpn="LM358N", enrichment_status="verified")
    db_session.add_all([real_user, real_company, real_card])
    db_session.commit()

    sds.seed(db_session)
    assert _count(db_session, User) == 7  # 6 sample + 1 real

    sds.wipe(db_session)

    # All sample rows gone.
    assert db_session.query(User).filter(User.email.like("%avsample@avsample.test")).count() == 0
    assert db_session.query(Company).filter(Company.source == sds.SAMPLE_TAG).count() == 0
    assert db_session.query(VendorCard).filter(VendorCard.source == sds.SAMPLE_TAG).count() == 0
    assert db_session.query(MaterialCard).filter(MaterialCard.normalized_mpn.like("avsample%")).count() == 0
    assert db_session.query(Requisition).filter(Requisition.name.like("AVSAMPLE ·%")).count() == 0
    assert db_session.query(ExcessList).filter(ExcessList.source_filename == sds.SAMPLE_TAG).count() == 0
    assert _count(db_session, Quote) == 0
    assert _count(db_session, Offer) == 0
    assert _count(db_session, Sighting) == 0
    assert _count(db_session, ActivityLog) == 0
    assert _count(db_session, RequisitionTask) == 0
    assert _count(db_session, VendorPartUnavailability) == 0
    assert _count(db_session, BuyerScore) == 0

    # Real rows untouched.
    assert db_session.get(User, real_user.id) is not None
    assert db_session.get(Company, real_company.id) is not None
    assert db_session.get(MaterialCard, real_card.id) is not None


def test_wipe_succeeds_with_fk_enforcement(db_session: Session) -> None:
    """Wipe() of a fully-seeded DB succeeds under SQLite FK enforcement.

    The conftest engine sets ``PRAGMA foreign_keys=ON``, so this proves the
    hand-ordered deletion cascade satisfies the four ``ondelete=RESTRICT`` FKs on the
    sample Users (ExcessList.owner_id, ExcessOffer.submitted_by, CustomerBid.owner_id,
    ExcessOutreach.submitted_by). A reorder that deletes Users before a RESTRICT child
    would raise IntegrityError here instead of silently passing on an FK-off engine.
    """
    # Assert FK enforcement is actually active for this connection — otherwise this
    # test would be a no-op guarantee (sqlite-masks-postgres trap).
    assert db_session.execute(text("PRAGMA foreign_keys")).scalar() == 1

    sds.seed(db_session)
    deleted = sds.wipe(db_session)  # must not raise under RESTRICT FK enforcement

    assert sum(deleted.values()) > 0
    # Every sample User is gone — proves the RESTRICT children were deleted first.
    assert db_session.query(User).filter(User.email.like("%avsample@avsample.test")).count() == 0


def test_wipe_on_empty_db_is_noop(db_session: Session) -> None:
    """--wipe with no sample data deletes nothing (and does not raise)."""
    real_company = Company(name="Lonely Real Co", account_type="Customer", source="sfdc", is_active=True)
    db_session.add(real_company)
    db_session.commit()

    deleted = sds.wipe(db_session)

    assert sum(deleted.values()) == 0
    assert db_session.get(Company, real_company.id) is not None


def test_owner_assigns_deals_to_existing_user(db_session: Session) -> None:
    """--owner redirects deal ownership to the named user's own-work lenses.

    Requisition created_by, buy-plan-line buyer_id, buy-plan submitted_by_id and
    excess owner_id all become the owner — while u_manager stays the distinct
    approver so the approve/verify workflow still shows a second actor.
    """
    owner = User(email="boss@trioscs.com", name="Boss", role="admin", is_active=True)
    db_session.add(owner)
    db_session.commit()

    sds.seed(db_session, owner_email="boss@trioscs.com")

    # The existing user was reused, not duplicated: 6 sample users + the owner.
    assert _count(db_session, User) == 7
    assert db_session.query(User).filter(User.email == "boss@trioscs.com").count() == 1

    # Every sample requisition is owned by the owner ('mine' requisitions lens).
    reqs = db_session.query(Requisition).filter(Requisition.name.like("AVSAMPLE%")).all()
    assert reqs and all(r.created_by == owner.id for r in reqs)

    # Every buy-plan line's buyer is the owner ('orders' lens = BuyPlanLine.buyer_id).
    from app.models.buy_plan import BuyPlanLine

    lines = db_session.query(BuyPlanLine).all()
    assert lines and all(line.buyer_id == owner.id for line in lines)

    # EVERY buy plan — draft, pending AND active — is submitted by the owner so all
    # three surface in the owner's "deals" board (deals_board scope=mine filters
    # BuyPlan.submitted_by_id == user; the DRAFT must not be left with NULL).
    plans = db_session.query(BuyPlan).all()
    assert len(plans) == 3
    assert all(bp.submitted_by_id == owner.id for bp in plans)

    # The ACTIVE plan is APPROVED by the distinct sample manager (not the owner).
    q_won2 = db_session.query(Quote).filter_by(quote_number="AVSAMPLE-Q-0005").one()
    bp_active = db_session.query(BuyPlan).filter_by(quote_id=q_won2.id).one()
    assert bp_active.approved_by_id is not None and bp_active.approved_by_id != owner.id
    manager = db_session.query(User).filter(User.email.like("manager.avsample@%")).one()
    assert bp_active.approved_by_id == manager.id

    # The sample excess list is owned by the owner ('Open to Me' resell lens).
    assert db_session.query(ExcessList).filter(ExcessList.owner_id == owner.id).count() == 1


def test_owner_pre_provisions_missing_user_and_survives_wipe(db_session: Session) -> None:
    """An unknown --owner email pre-provisions a REAL (non-sample) user that --wipe
    keeps."""
    sds.seed(db_session, owner_email="newowner@trioscs.com")

    owner = db_session.query(User).filter(User.email == "newowner@trioscs.com").one()
    assert owner.is_active is True
    # Real account — NOT sample-tagged (its email is not on the avsample domain).
    assert not owner.email.endswith("avsample.test")
    assert db_session.query(Requisition).filter(Requisition.name.like("AVSAMPLE%")).first().created_by == owner.id

    sds.wipe(db_session)

    # Sample users gone; the pre-provisioned real owner survives.
    assert db_session.query(User).filter(User.email.like("%avsample@avsample.test")).count() == 0
    assert db_session.get(User, owner.id) is not None


def test_wipe_with_owner_succeeds_under_fk_enforcement(db_session: Session) -> None:
    """Wipe() of an --owner-seeded DB succeeds under FK enforcement.

    With --owner the RESTRICT FKs (ExcessList.owner_id, ExcessOffer.submitted_by,
    CustomerBid.owner_id, ExcessOutreach.submitted_by) point at the REAL owner user,
    which wipe() does NOT delete. Deleting the tagged child rows while that real parent
    survives must still satisfy ondelete=RESTRICT — this exercises that path under the
    conftest engine's PRAGMA foreign_keys=ON (else it's a no-op guarantee).
    """
    assert db_session.execute(text("PRAGMA foreign_keys")).scalar() == 1

    sds.seed(db_session, owner_email="fkowner@trioscs.com")
    owner = db_session.query(User).filter(User.email == "fkowner@trioscs.com").one()

    deleted = sds.wipe(db_session)  # must not raise IntegrityError under RESTRICT

    assert sum(deleted.values()) > 0
    # All tagged excess rows (which referenced the real owner) are gone, no orphans.
    assert _count(db_session, ExcessList) == 0
    assert _count(db_session, ExcessOffer) == 0
    assert _count(db_session, CustomerBid) == 0
    assert _count(db_session, ExcessOutreach) == 0
    # The real owner survives the wipe.
    assert db_session.get(User, owner.id) is not None
