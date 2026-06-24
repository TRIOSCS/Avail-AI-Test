"""seed_sample_data.py — Idempotent, additive sample-dataset seeder for AvailAI.

Builds a full, deterministic demo dataset spanning every major workflow (CRM,
sourcing, offers, quotes, buy plans, resell/excess, vendor intelligence, My Day
timeline) so a fresh / staging database can be driven across all screens without
any live side effects.

    python -m app.management.seed_sample_data          # seed / top-up (idempotent)
    python -m app.management.seed_sample_data --wipe    # tear down sample rows only

Every sample root row carries a stable, queryable marker (the ``AVSAMPLE`` naming
convention + existing free-text columns — never a new column) so re-runs top up
only missing rows and ``--wipe`` deletes ONLY tagged sample rows in FK-safe order.

HARD SAFETY RULES (enforced here):
  * No real outbound effects — all RFQ/offer/activity/outreach rows are constructed
    directly as ORM objects; never via email_service / Graph / Apollo / Clay /
    Hunter / supplier connectors / search_service.
  * ORM only — no raw DDL, no Base.metadata.create_all, no raw SQL (read-only
    counts excepted). Schema already exists.
  * StrEnum constants only for status writes (``Enum.X.value``); never raw strings.
  * MaterialCard category/spec writes go through the F1 ladder
    (spec_tiers.set_category/set_brand/set_manufacturer + spec_write_service
    .record_spec) under the registered ``trio_source`` tier — never assigned directly.
  * Idempotent additive — get-or-create on stable natural keys; never UPDATE or
    DELETE a non-sample row.

Called by: an operator (manually, post-deploy / on a fresh staging DB). NOT cron.
Depends on: app.database.SessionLocal, the ORM models, app.constants enums,
    app.services.spec_tiers / spec_write_service (F1 ladder),
    app.services.offer_qualification, app.scoring, app.evidence_tiers,
    app.vendor_utils, app.utils.normalization.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from app.constants import (
    ActivityType,
    AIFlagSeverity,
    BuyPlanLineStatus,
    BuyPlanStatus,
    Channel,
    CustomerBidStatus,
    Direction,
    ExcessLineItemStatus,
    ExcessListStatus,
    ExcessOfferScope,
    ExcessOfferStatus,
    ExcessOutreachChannel,
    ExcessOutreachStatus,
    LineIssueType,
    MaterialEnrichmentStatus,
    OfferCondition,
    OfferLineMatchStatus,
    OfferStatus,
    QuoteStatus,
    ReleaseTrigger,
    RequisitionStatus,
    SourcingStatus,
    SOVerificationStatus,
    TaskStatus,
    UnavailabilityReason,
    UserRole,
)
from app.evidence_tiers import tier_for_sighting
from app.models.auth import User
from app.models.buy_plan import BuyPlan, BuyPlanLine, VerificationGroupMember
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
from app.scoring import score_sighting_v2
from app.services.commodity_registry import seed_commodity_schemas
from app.services.offer_qualification import apply_qualification
from app.services.spec_tiers import set_brand, set_category, set_manufacturer
from app.services.spec_write_service import record_spec
from app.utils.normalization import normalize_mpn_key
from app.vendor_utils import normalize_vendor_name

# ── Sample tag & naming convention ───────────────────────────────────
SAMPLE_TAG = "AVSAMPLE"  # appears in every taggable free-text marker column
SAMPLE_EMAIL_DOMAIN = "avsample.test"  # obviously fictional; never a real user
SEED_SRC = "trio_source"  # registered F1 source, tier 95 (do NOT invent a new one)
SEED_CONF = 0.95

# Per-run tally of created vs skipped rows, keyed by model name.
_Counts = dict[str, dict[str, int]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tally(counts: _Counts, model: str, created: bool) -> None:
    bucket = counts.setdefault(model, {"created": 0, "skipped": 0})
    bucket["created" if created else "skipped"] += 1


def get_or_create(db: Session, counts: _Counts, model: type, defaults: dict[str, Any], **key: Any) -> tuple[Any, bool]:
    """Find a row by its natural key; create with *defaults* only when missing.

    Returns ``(row, created)``. Never UPDATEs an existing row, so re-runs are a
    no-op for already-seeded (and all non-sample) data.
    """
    row = db.query(model).filter_by(**key).one_or_none()
    if row is not None:
        _tally(counts, model.__name__, created=False)
        return row, False
    row = model(**key, **defaults)
    db.add(row)
    db.flush()
    _tally(counts, model.__name__, created=True)
    return row, True


# ── WF-A: users, companies, sites, contacts, vendors, material cards ──


def _seed_users(db: Session, counts: _Counts) -> dict[str, User]:
    spec = [
        ("u_seeder", "agent", "AVSAMPLE · Seeder Agent", UserRole.AGENT),
        ("u_sales", "sales", "AVSAMPLE · Sam Sales", UserRole.SALES),
        ("u_buyer1", "buyer1", "AVSAMPLE · Bonnie Buyer", UserRole.BUYER),
        ("u_buyer2", "buyer2", "AVSAMPLE · Bob Buyer", UserRole.BUYER),
        ("u_trader", "trader", "AVSAMPLE · Tina Trader", UserRole.TRADER),
        ("u_manager", "manager", "AVSAMPLE · Morgan Manager", UserRole.MANAGER),
    ]
    out: dict[str, User] = {}
    for var, local, name, role in spec:
        email = f"{local}.avsample@{SAMPLE_EMAIL_DOMAIN}"
        row, created = get_or_create(
            db, counts, User, {"name": name, "role": role.value, "is_active": True}, email=email
        )
        out[var] = row
    return out


def _seed_companies(db: Session, counts: _Counts, u: dict[str, User]) -> dict[str, Company]:
    spec = [
        ("co_cust1", "AVSAMPLE · Northwind Aerospace", "Customer", "key", "northwind.avsample.test", "u_sales"),
        ("co_cust2", "AVSAMPLE · Cascade Medical Devices", "Customer", "core", None, "u_sales"),
        ("co_cust3", "AVSAMPLE · Brightline Robotics", "Prospect", "prospect", None, "u_sales"),
        ("co_brk1", "AVSAMPLE · Pinnacle Components (Broker)", "Vendor", None, "pinnacle.avsample.test", "u_buyer1"),
        ("co_brk2", "AVSAMPLE · Meridian Electronics (Broker)", "Vendor", None, None, "u_buyer2"),
        ("co_brk3", "AVSAMPLE · Apex Surplus (Broker)", "Vendor", None, None, "u_trader"),
    ]
    out: dict[str, Company] = {}
    for var, name, acct_type, tier, domain, owner in spec:
        defaults: dict[str, Any] = {
            "account_type": acct_type,
            "source": SAMPLE_TAG,
            "account_owner_id": u[owner].id,
            "tier": tier,
            "domain": domain,
            "is_active": True,
        }
        out[var], _ = get_or_create(db, counts, Company, defaults, name=name)
    return out


def _seed_sites(db: Session, counts: _Counts, co: dict[str, Company], u: dict[str, User]) -> dict[str, CustomerSite]:
    spec = [
        ("site_c1_hq", "co_cust1", "AVSAMPLE · Northwind HQ", "HQ", "u_sales"),
        ("site_c1_wh", "co_cust1", "AVSAMPLE · Northwind Warehouse", "Warehouse", "u_sales"),
        ("site_c2_hq", "co_cust2", "AVSAMPLE · Cascade HQ", "HQ", "u_sales"),
        ("site_c3_hq", "co_cust3", "AVSAMPLE · Brightline HQ", "HQ", "u_sales"),
    ]
    out: dict[str, CustomerSite] = {}
    for var, company, site_name, site_type, owner in spec:
        defaults = {"site_type": site_type, "owner_id": u[owner].id, "is_active": True}
        out[var], _ = get_or_create(db, counts, CustomerSite, defaults, company_id=co[company].id, site_name=site_name)
    return out


def _seed_contacts(
    db: Session, counts: _Counts, site: dict[str, CustomerSite], co: dict[str, Company]
) -> dict[str, SiteContact]:
    spec = [
        ("con_c1a", "site_c1_hq", "AVSAMPLE · Dana Procurement", "Dana", "Procurement", "Buyer", True),
        ("con_c1b", "site_c1_wh", "AVSAMPLE · Lee Receiving", "Lee", "Receiving", "Warehouse Mgr", False),
        ("con_c2a", "site_c2_hq", "AVSAMPLE · Priya Sourcing", "Priya", "Sourcing", "Sr Buyer", True),
        ("con_c3a", "site_c3_hq", "AVSAMPLE · Quinn Engineering", "Quinn", "Engineering", "Eng Lead", True),
        ("con_c3b", "site_c3_hq", "AVSAMPLE · Rio Finance", "Rio", "Finance", "Controller", False),
    ]
    out: dict[str, SiteContact] = {}
    for var, s, full, first, last, title, primary in spec:
        defaults = {
            "first_name": first,
            "last_name": last,
            "title": title,
            "is_primary": primary,
            "is_active": True,
        }
        out[var], _ = get_or_create(db, counts, SiteContact, defaults, customer_site_id=site[s].id, full_name=full)
    # Optionally surface the primary contact on the customer company.
    if co["co_cust1"].primary_contact_id is None:
        co["co_cust1"].primary_contact_id = out["con_c1a"].id
    return out


def _seed_vendors(db: Session, counts: _Counts) -> dict[str, VendorCard]:
    spec: list[tuple[str, str, float | None, float | None, dict[str, Any]]] = [
        ("vc_pinnacle", "AVSAMPLE Pinnacle Components", 87.5, 0.92, {"sighting_count": 150}),
        ("vc_meridian", "AVSAMPLE Meridian Electronics", 62.0, 0.55, {"sighting_count": 30}),
        ("vc_apex", "AVSAMPLE Apex Surplus", 45.0, 0.30, {"is_blacklisted": False}),
        ("vc_newvendor", "AVSAMPLE Fresh Supply Co", None, None, {"is_new_vendor": True}),
        ("vc_highrel", "AVSAMPLE Keystone Distribution", 95.0, 0.97, {}),
        ("vc_digikeyish", "AVSAMPLE Authorized Dist", 90.0, 0.99, {}),
    ]
    out: dict[str, VendorCard] = {}
    for var, display, score, rate, extra in spec:
        defaults: dict[str, Any] = {
            "display_name": display,
            "source": SAMPLE_TAG,
            "vendor_score": score,
            "response_rate": rate,
            **extra,
        }
        out[var], _ = get_or_create(db, counts, VendorCard, defaults, normalized_name=normalize_vendor_name(display))
    return out


def _seed_vendor_contacts(db: Session, counts: _Counts, vc: dict[str, VendorCard]) -> None:
    spec = [
        ("vc_pinnacle", "AVSAMPLE · Pat Pinnacle", "pat", 95, True),
        ("vc_pinnacle", "AVSAMPLE · Casey Pinnacle", "casey", 80, False),
        ("vc_meridian", "AVSAMPLE · Mel Meridian", "mel", 70, True),
        ("vc_meridian", "AVSAMPLE · Drew Meridian", "drew", 60, False),
        ("vc_apex", "AVSAMPLE · Alex Apex", "alex", 50, False),
        ("vc_apex", "AVSAMPLE · Sky Apex", "sky", 40, False),
    ]
    for var, name, local, conf, verified in spec:
        email = f"{local}.avsample@{SAMPLE_EMAIL_DOMAIN}"
        defaults = {"full_name": name, "source": SAMPLE_TAG, "confidence": conf, "is_verified": verified}
        get_or_create(db, counts, VendorContact, defaults, vendor_card_id=vc[var].id, email=email)


def _seed_material_cards(db: Session, counts: _Counts) -> dict[str, MaterialCard]:
    """Build the 3 sample MaterialCards through the F1 ladder (never direct writes)."""
    # record_spec() looks up a per-commodity schema row from commodity_spec_schemas;
    # without it EVERY structured-spec write returns False ("no schema for commodity=..").
    # On a fresh / staging DB the operator may run this seeder before app startup has
    # run its schema seed, and the test SQLite DB never seeds it at all — so make the
    # seeder self-contained and seed the schemas here. Idempotent (skips existing rows).
    seed_commodity_schemas(db)
    db.flush()

    out: dict[str, MaterialCard] = {}

    # mc_mcu — full ladder + 2 specs + demand telemetry + a validation conflict.
    mc_mcu, created = get_or_create(
        db,
        counts,
        MaterialCard,
        {
            "display_mpn": "AVSAMPLE-STM32F103RB",
            "enrichment_status": MaterialEnrichmentStatus.VERIFIED.value,
            "sourced_qty_90d": 1200,
            "last_sourced_at": _now() - timedelta(days=2),
        },
        normalized_mpn=normalize_mpn_key("AVSAMPLE-STM32F103RB"),
    )
    if created:
        # Seed a manual lower-confidence manufacturer first, then beat it with a
        # higher-tier *different* value to exercise the validation-conflict path.
        set_manufacturer(mc_mcu, "ST Micro", source="manual", confidence=1.0)
        set_category(mc_mcu, "microcontrollers", source=SEED_SRC, confidence=SEED_CONF)
        set_brand(mc_mcu, "STMicroelectronics", source=SEED_SRC, confidence=SEED_CONF)
        set_manufacturer(mc_mcu, "STMicroelectronics", source=SEED_SRC, confidence=SEED_CONF)
        db.flush()
        # ``core`` is an enum: the allowed value is "Cortex-M3" (NOT "ARM Cortex-M3"
        # — an enum mismatch silently returns False). ``package`` is a free-text enum
        # (enum_values=None) so "LQFP48" is accepted as-is. Log the return so an
        # unexpected False can never silently leave the dossier specs panel empty.
        wrote_pkg = record_spec(db, mc_mcu.id, "package", "LQFP48", source=SEED_SRC, confidence=SEED_CONF)
        wrote_core = record_spec(db, mc_mcu.id, "core", "Cortex-M3", source=SEED_SRC, confidence=SEED_CONF)
        logger.info("seed-sample: mc_mcu record_spec package={} core={} (expected both True)", wrote_pkg, wrote_core)
        db.flush()
    out["mc_mcu"] = mc_mcu

    # mc_conn — category + 1 spec.
    mc_conn, created = get_or_create(
        db,
        counts,
        MaterialCard,
        {
            "display_mpn": "AVSAMPLE-MAX3232",
            "enrichment_status": MaterialEnrichmentStatus.VERIFIED.value,
        },
        normalized_mpn=normalize_mpn_key("AVSAMPLE-MAX3232"),
    )
    if created:
        set_category(mc_conn, "analog_ic", source=SEED_SRC, confidence=SEED_CONF)
        set_manufacturer(mc_conn, "Maxim Integrated", source=SEED_SRC, confidence=SEED_CONF)
        db.flush()
        # analog_ic.package is an enum: the allowed value is "SOIC-8" (NOT "SOIC16",
        # which fails enum validation → False). Log the return for the same reason.
        wrote_pkg = record_spec(db, mc_conn.id, "package", "SOIC-8", source=SEED_SRC, confidence=SEED_CONF)
        logger.info("seed-sample: mc_conn record_spec package={} (expected True)", wrote_pkg)
        db.flush()
    out["mc_conn"] = mc_conn

    # mc_bare — NULL category: exercises the legacy floor + "record_spec on NULL
    # category returns False" gate (we call it once and log the False).
    mc_bare, created = get_or_create(
        db,
        counts,
        MaterialCard,
        {
            "display_mpn": "AVSAMPLE-LEGACY-01",
            "enrichment_status": MaterialEnrichmentStatus.UNENRICHED.value,
        },
        normalized_mpn=normalize_mpn_key("AVSAMPLELEGACY01"),
    )
    if created:
        wrote = record_spec(db, mc_bare.id, "package", "TO-220", source=SEED_SRC, confidence=SEED_CONF)
        logger.info("seed-sample: record_spec on NULL-category mc_bare returned {} (expected False)", wrote)
        db.flush()
    out["mc_bare"] = mc_bare
    return out


def _seed_verification_group(db: Session, counts: _Counts, u: dict[str, User]) -> None:
    get_or_create(db, counts, VerificationGroupMember, {"is_active": True}, user_id=u["u_manager"].id)


def _seed_tasks(
    db: Session,
    counts: _Counts,
    co: dict[str, Company],
    con: dict[str, SiteContact],
    u: dict[str, User],
    req: dict[str, Requisition],
) -> None:
    spec: list[tuple[str, str, str, TaskStatus, dict[str, Any]]] = [
        (
            "AVSAMPLE:task:1",
            "AVSAMPLE · Follow up on Northwind quote",
            "sales",
            TaskStatus.TODO,
            {"company_id": co["co_cust1"].id, "assigned_to_id": u["u_sales"].id, "due_at": _now() + timedelta(days=1)},
        ),
        (
            "AVSAMPLE:task:2",
            "AVSAMPLE · Confirm Cascade contact details",
            "sales",
            TaskStatus.IN_PROGRESS,
            {"site_contact_id": con["con_c2a"].id, "assigned_to_id": u["u_sales"].id},
        ),
        (
            "AVSAMPLE:task:3",
            "AVSAMPLE · Source MCUs for Northwind",
            "sourcing",
            TaskStatus.TODO,
            {"requisition_id": req["req1"].id, "assigned_to_id": u["u_buyer1"].id},
        ),
        (
            "AVSAMPLE:task:4",
            "AVSAMPLE · Archive closed Cascade lead",
            "general",
            TaskStatus.DONE,
            {"company_id": co["co_cust2"].id, "completed_at": _now() - timedelta(days=1)},
        ),
    ]
    for ref, title, ttype, status, extra in spec:
        defaults = {"title": title, "task_type": ttype, "status": status.value, "priority": 2, **extra}
        get_or_create(db, counts, RequisitionTask, defaults, source_ref=ref)


def _seed_activity(
    db: Session,
    counts: _Counts,
    co: dict[str, Company],
    vc: dict[str, VendorCard],
    req: dict[str, Requisition],
    quotes: dict[str, Quote],
    u: dict[str, User],
) -> None:
    today, yest, d3, d10 = _now(), _now() - timedelta(days=1), _now() - timedelta(days=3), _now() - timedelta(days=10)
    spec = [
        (
            "AVSAMPLE:act:1",
            today,
            ActivityType.EMAIL_SENT,
            Channel.EMAIL,
            Direction.OUTBOUND,
            {"company_id": co["co_cust1"].id},
        ),
        (
            "AVSAMPLE:act:2",
            today,
            ActivityType.EMAIL_RECEIVED,
            Channel.EMAIL,
            Direction.INBOUND,
            {"company_id": co["co_cust1"].id},
        ),
        (
            "AVSAMPLE:act:3",
            yest,
            ActivityType.CALL_LOGGED,
            Channel.PHONE,
            Direction.OUTBOUND,
            {"vendor_card_id": vc["vc_pinnacle"].id},
        ),
        (
            "AVSAMPLE:act:4",
            yest,
            ActivityType.RFQ_SENT,
            Channel.SYSTEM,
            Direction.OUTBOUND,
            {"requisition_id": req["req1"].id},
        ),
        (
            "AVSAMPLE:act:5",
            d3,
            ActivityType.OFFER_CREATED,
            Channel.AVAIL_SYSTEM,
            Direction.INBOUND,
            {"requisition_id": req["req2"].id},
        ),
        (
            "AVSAMPLE:act:6",
            d3,
            ActivityType.SALES_NOTE,
            Channel.MANUAL,
            Direction.OUTBOUND,
            {"quote_id": quotes["q_won"].id},
        ),
        (
            "AVSAMPLE:act:7",
            d10,
            ActivityType.STATUS_CHANGED,
            Channel.SYSTEM,
            Direction.OUTBOUND,
            {"requisition_id": req["req3"].id},
        ),
        (
            "AVSAMPLE:act:8",
            today,
            ActivityType.MEETING,
            Channel.CALENDAR,
            Direction.OUTBOUND,
            {"company_id": co["co_cust2"].id},
        ),
    ]
    for ext, when, atype, channel, direction, fk in spec:
        defaults = {
            "activity_type": atype.value,
            "channel": channel.value,
            "direction": direction.value,
            "summary": f"{SAMPLE_TAG} {atype.value} activity",
            "notes": f"{SAMPLE_TAG} sample timeline entry",
            "user_id": u["u_sales"].id,
            "occurred_at": when,
            "created_at": when,
            **fk,
        }
        get_or_create(db, counts, ActivityLog, defaults, external_id=ext)


# ── Sourcing / offers / quotes / buy plans (WF-B..D) ─────────────────


def _mk_requisition(
    db: Session, counts: _Counts, name: str, co: Company, site_id: int | None, urgency: str, value: int, u: User
) -> Requisition:
    defaults = {
        "company_id": co.id,
        "customer_site_id": site_id,
        "status": RequisitionStatus.ACTIVE.value,
        "urgency": urgency,
        "opportunity_value": Decimal(value),
        "created_by": u.id,
    }
    row, _ = get_or_create(db, counts, Requisition, defaults, name=name)
    return row


def _mk_requirement(
    db: Session,
    counts: _Counts,
    req: Requisition,
    mpn: str,
    status: SourcingStatus,
    *,
    card_id: int | None = None,
    target_qty: int | None = None,
    target_price: Decimal | None = None,
) -> Requirement:
    defaults = {
        "manufacturer": "",
        "normalized_mpn": normalize_mpn_key(mpn),
        "material_card_id": card_id,
        "sourcing_status": status.value,
        "target_qty": target_qty,
        "target_price": target_price,
    }
    row, _ = get_or_create(db, counts, Requirement, defaults, requisition_id=req.id, primary_mpn=mpn)
    return row


def _mk_offer(
    db: Session,
    counts: _Counts,
    req: Requirement,
    requisition_id: int,
    vendor_name: str,
    *,
    vendor_card_id: int | None,
    mpn: str,
    condition: OfferCondition,
    status: OfferStatus,
    unit_price: float,
    qty: int,
    u_seeder: User,
    qualification: dict | None = None,
    selected: bool = False,
    evidence_tier: str = "T2",
    source: str = "manual",
    created_at: datetime | None = None,
    extra: dict | None = None,
) -> Offer:
    defaults: dict[str, Any] = {
        "requisition_id": requisition_id,
        "vendor_card_id": vendor_card_id,
        "vendor_name_normalized": normalize_vendor_name(vendor_name),
        "manufacturer": "",
        "qty_available": qty,
        "unit_price": unit_price,
        "condition": condition.value,
        "status": status.value,
        "source": source,
        "entered_by_id": u_seeder.id,
        "evidence_tier": evidence_tier,
        "parse_confidence": 0.9,
        "selected_for_quote": selected,
        "qualification": qualification or {},
        "notes": f"{SAMPLE_TAG} sample offer",
        "created_at": created_at or _now(),
        **(extra or {}),
    }
    row, created = get_or_create(
        db,
        counts,
        Offer,
        defaults,
        requirement_id=req.id,
        vendor_name=vendor_name,
        mpn=mpn,
        normalized_mpn=normalize_mpn_key(mpn),
    )
    if created:
        apply_qualification(row)  # stamps qualification_status + note (never hand-set)
        db.flush()
    return row


def _mk_quote(
    db: Session,
    counts: _Counts,
    number: str,
    requisition_id: int,
    status: QuoteStatus,
    u_seeder: User,
    *,
    revision: int = 1,
    extra: dict | None = None,
) -> Quote:
    defaults: dict[str, Any] = {
        "requisition_id": requisition_id,
        "revision": revision,
        "line_items": [],
        "status": status.value,
        "created_by_id": u_seeder.id,
        **(extra or {}),
    }
    row, _ = get_or_create(db, counts, Quote, defaults, quote_number=number)
    return row


def _mk_quote_line(
    db: Session,
    counts: _Counts,
    quote: Quote,
    *,
    offer: Offer | None,
    mpn: str,
    qty: int,
    cost: Decimal,
    sell: Decimal,
) -> QuoteLine:
    margin = ((sell - cost) / sell * 100) if sell else Decimal(0)
    defaults = {
        "qty": qty,
        "cost_price": cost,
        "sell_price": sell,
        "margin_pct": round(margin, 2),
    }
    key = {"quote_id": quote.id, "offer_id": offer.id if offer else None, "mpn": mpn}
    row, _ = get_or_create(db, counts, QuoteLine, defaults, **key)
    return row


def _seed_wf_b(
    db: Session, counts: _Counts, co: dict, site: dict, mc: dict, vc: dict, u: dict
) -> tuple[Requisition, dict[str, Requirement], dict[str, Offer], Quote]:
    req1 = _mk_requisition(
        db,
        counts,
        "AVSAMPLE · REQ Northwind MCUs",
        co["co_cust1"],
        site["site_c1_hq"].id,
        "normal",
        15000,
        u["u_sales"],
    )
    r1a = _mk_requirement(
        db,
        counts,
        req1,
        "AVSAMPLE-STM32F103RB",
        SourcingStatus.OFFERED,
        card_id=mc["mc_mcu"].id,
        target_qty=200,
        target_price=Decimal(50),
    )
    r1b = _mk_requirement(
        db,
        counts,
        req1,
        "AVSAMPLE-MAX3232",
        SourcingStatus.OFFERED,
        card_id=mc["mc_conn"].id,
        target_qty=100,
        target_price=Decimal(25),
    )
    r1c = _mk_requirement(db, counts, req1, "AVSAMPLE-OPEN-01", SourcingStatus.OPEN)
    r1d = _mk_requirement(db, counts, req1, "AVSAMPLE-SOURCING-01", SourcingStatus.SOURCING)
    reqs = {"r1a": r1a, "r1b": r1b, "r1c": r1c, "r1d": r1d}

    seeder = u["u_seeder"]
    o1 = _mk_offer(
        db,
        counts,
        r1a,
        req1.id,
        "AVSAMPLE Pinnacle Components",
        vendor_card_id=vc["vc_pinnacle"].id,
        mpn="AVSAMPLE-STM32F103RB",
        condition=OfferCondition.NEW,
        status=OfferStatus.ACTIVE,
        unit_price=50.0,
        qty=200,
        u_seeder=seeder,
        selected=True,
        evidence_tier="T1",
        qualification={"usage": "stock", "part_condition": "new"},
        created_at=_now() - timedelta(days=5),
    )
    o2 = _mk_offer(
        db,
        counts,
        r1a,
        req1.id,
        "AVSAMPLE Meridian Electronics",
        vendor_card_id=vc["vc_meridian"].id,
        mpn="AVSAMPLE-STM32F103RB",
        condition=OfferCondition.REFURB,
        status=OfferStatus.ACTIVE,
        unit_price=35.0,
        qty=150,
        u_seeder=seeder,
        qualification={"refurbished_by": "Meridian", "refurb_process": "reballed + tested"},
    )
    o3 = _mk_offer(
        db,
        counts,
        r1a,
        req1.id,
        "AVSAMPLE Apex Surplus",
        vendor_card_id=vc["vc_apex"].id,
        mpn="AVSAMPLE-STM32F103RB",
        condition=OfferCondition.PULLS,
        status=OfferStatus.SOLD,
        unit_price=40.0,
        qty=80,
        u_seeder=seeder,
    )
    o4 = _mk_offer(
        db,
        counts,
        r1b,
        req1.id,
        "AVSAMPLE Keystone Distribution",
        vendor_card_id=vc["vc_highrel"].id,
        mpn="AVSAMPLE-MAX3232",
        condition=OfferCondition.PULLS,
        status=OfferStatus.ACTIVE,
        unit_price=25.0,
        qty=100,
        u_seeder=seeder,
        selected=True,
    )
    o5 = _mk_offer(
        db,
        counts,
        r1b,
        req1.id,
        "AVSAMPLE Fresh Supply Co",
        vendor_card_id=None,
        mpn="AVSAMPLE-MAX3232",
        condition=OfferCondition.NEW,
        status=OfferStatus.ACTIVE,
        unit_price=100.0,
        qty=50,
        u_seeder=seeder,
        source="proactive",
    )
    o6 = _mk_offer(
        db,
        counts,
        r1a,
        req1.id,
        "AVSAMPLE Pinnacle Components",
        vendor_card_id=vc["vc_pinnacle"].id,
        mpn="AVSAMPLE-STM32F103RB-ALT",
        condition=OfferCondition.NEW,
        status=OfferStatus.ACTIVE,
        unit_price=48.0,
        qty=120,
        u_seeder=seeder,
        created_at=_now() - timedelta(days=20),
        extra={"is_stale": True, "expires_at": _now() - timedelta(days=2)},
    )
    o6r = _mk_offer(
        db,
        counts,
        r1a,
        req1.id,
        "AVSAMPLE Pinnacle Components",
        vendor_card_id=vc["vc_pinnacle"].id,
        mpn="AVSAMPLE-STM32F103RB-RECONF",
        condition=OfferCondition.NEW,
        status=OfferStatus.ACTIVE,
        unit_price=48.0,
        qty=120,
        u_seeder=seeder,
        extra={"reconfirmed_at": _now() - timedelta(days=5), "reconfirm_count": 1},
    )
    offers = {"o1": o1, "o2": o2, "o3": o3, "o4": o4, "o5": o5, "o6": o6, "o6r": o6r}

    q_draft = _mk_quote(db, counts, "AVSAMPLE-Q-0001", req1.id, QuoteStatus.DRAFT, seeder)
    return req1, reqs, offers, q_draft


def _seed_wf_c(
    db: Session, counts: _Counts, co: dict, vc: dict, u: dict
) -> tuple[Requisition, dict[str, Offer], Quote, BuyPlan]:
    seeder = u["u_seeder"]
    req2 = _mk_requisition(
        db, counts, "AVSAMPLE · REQ Cascade Sensors", co["co_cust2"], None, "hot", 40000, u["u_sales"]
    )
    r2a = _mk_requirement(db, counts, req2, "AVSAMPLE-SENS-A", SourcingStatus.QUOTED, target_qty=100)
    r2b = _mk_requirement(db, counts, req2, "AVSAMPLE-SENS-B", SourcingStatus.QUOTED, target_qty=50)
    r2c = _mk_requirement(db, counts, req2, "AVSAMPLE-SENS-C", SourcingStatus.QUOTED, target_qty=10)

    o7 = _mk_offer(
        db,
        counts,
        r2a,
        req2.id,
        "AVSAMPLE Pinnacle Components",
        vendor_card_id=vc["vc_pinnacle"].id,
        mpn="AVSAMPLE-SENS-A",
        condition=OfferCondition.NEW,
        status=OfferStatus.ACTIVE,
        unit_price=30.0,
        qty=100,
        u_seeder=seeder,
        selected=True,
    )
    o8 = _mk_offer(
        db,
        counts,
        r2b,
        req2.id,
        "AVSAMPLE Keystone Distribution",
        vendor_card_id=vc["vc_highrel"].id,
        mpn="AVSAMPLE-SENS-B",
        condition=OfferCondition.NEW,
        status=OfferStatus.ACTIVE,
        unit_price=110.0,
        qty=50,
        u_seeder=seeder,
        selected=True,
    )
    o9 = _mk_offer(
        db,
        counts,
        r2c,
        req2.id,
        "AVSAMPLE Meridian Electronics",
        vendor_card_id=vc["vc_meridian"].id,
        mpn="AVSAMPLE-SENS-C",
        condition=OfferCondition.PULLS,
        status=OfferStatus.ACTIVE,
        unit_price=80.0,
        qty=20,
        u_seeder=seeder,
    )
    o10 = _mk_offer(
        db,
        counts,
        r2a,
        req2.id,
        "AVSAMPLE Apex Surplus",
        vendor_card_id=vc["vc_apex"].id,
        mpn="AVSAMPLE-SENS-A",
        condition=OfferCondition.REFURB,
        status=OfferStatus.REJECTED,
        unit_price=28.0,
        qty=100,
        u_seeder=seeder,
    )
    offers = {"o7": o7, "o8": o8, "o9": o9, "o10": o10}

    q_sent = _mk_quote(
        db, counts, "AVSAMPLE-Q-0002", req2.id, QuoteStatus.SENT, seeder, extra={"sent_at": _now() - timedelta(days=1)}
    )
    _mk_quote_line(db, counts, q_sent, offer=o7, mpn=o7.mpn, qty=100, cost=Decimal(30), sell=Decimal(35))
    _mk_quote_line(db, counts, q_sent, offer=o8, mpn=o8.mpn, qty=50, cost=Decimal(110), sell=Decimal(115))
    _mk_quote_line(
        db, counts, q_sent, offer=None, mpn="AVSAMPLE-CUSTOM-001", qty=10, cost=Decimal(500), sell=Decimal(600)
    )

    bp_draft = _mk_buy_plan(db, counts, q_sent, req2, BuyPlanStatus.DRAFT, SOVerificationStatus.PENDING, u, extra={})
    if bp_draft is not None:
        _mk_buy_plan_line(
            db,
            counts,
            bp_draft,
            offer=o7,
            requirement_id=r2a.id,
            qty=100,
            status=BuyPlanLineStatus.AWAITING_PO,
            cost=Decimal(30),
            sell=Decimal(35),
            ai_score=88.0,
            buyer=u["u_buyer1"],
            reason="vendor_ownership",
        )
        _mk_buy_plan_line(
            db,
            counts,
            bp_draft,
            offer=o8,
            requirement_id=r2b.id,
            qty=50,
            status=BuyPlanLineStatus.AWAITING_PO,
            cost=Decimal(110),
            sell=Decimal(115),
            ai_score=74.0,
            buyer=u["u_buyer2"],
            reason="vendor_ownership",
        )
    return req2, offers, q_sent, bp_draft


def _mk_buy_plan(
    db: Session,
    counts: _Counts,
    quote: Quote,
    req: Requisition,
    status: BuyPlanStatus,
    so_status: SOVerificationStatus,
    u: dict,
    *,
    extra: dict,
) -> BuyPlan | None:
    """One non-cancelled plan per quote: skip if one already exists."""
    existing = (
        db.query(BuyPlan)
        .filter(BuyPlan.quote_id == quote.id, BuyPlan.status != BuyPlanStatus.CANCELLED.value)
        .one_or_none()
    )
    if existing is not None:
        _tally(counts, "BuyPlan", created=False)
        return existing
    bp = BuyPlan(
        quote_id=quote.id,
        requisition_id=req.id,
        status=status.value,
        so_status=so_status.value,
        **extra,
    )
    db.add(bp)
    db.flush()
    _tally(counts, "BuyPlan", created=True)
    return bp


def _mk_buy_plan_line(
    db: Session,
    counts: _Counts,
    bp: BuyPlan,
    *,
    offer: Offer | None,
    requirement_id: int | None,
    qty: int,
    status: BuyPlanLineStatus,
    cost: Decimal,
    sell: Decimal,
    ai_score: float,
    buyer: User,
    reason: str,
    extra: dict | None = None,
) -> BuyPlanLine:
    margin = ((sell - cost) / sell * 100) if sell else Decimal(0)
    defaults = {
        "quantity": qty,
        "status": status.value,
        "unit_cost": cost,
        "unit_sell": sell,
        "margin_pct": round(margin, 2),
        "ai_score": ai_score,
        "buyer_id": buyer.id,
        "assignment_reason": reason,
        **(extra or {}),
    }
    # Offer-less re-quoted lines key on requirement_id; offer-backed lines key on
    # offer_id (requirement_id then lives in defaults only).
    key = {"buy_plan_id": bp.id, "offer_id": offer.id if offer else None}
    if offer is None:
        key["requirement_id"] = requirement_id
    else:
        defaults["requirement_id"] = requirement_id
    row, _ = get_or_create(db, counts, BuyPlanLine, defaults, **key)
    return row


def _seed_wf_d(db: Session, counts: _Counts, co: dict, mc: dict, vc: dict, u: dict) -> tuple[Requisition, dict]:
    seeder = u["u_seeder"]
    req3 = _mk_requisition(
        db, counts, "AVSAMPLE · REQ Northwind Power", co["co_cust1"], None, "critical", 100000, u["u_sales"]
    )
    r3a = _mk_requirement(
        db, counts, req3, "AVSAMPLE-PWR-A", SourcingStatus.WON, card_id=mc["mc_mcu"].id, target_qty=200
    )
    r3b = _mk_requirement(db, counts, req3, "AVSAMPLE-PWR-B", SourcingStatus.WON, target_qty=100)

    o11 = _mk_offer(
        db,
        counts,
        r3a,
        req3.id,
        "AVSAMPLE Keystone Distribution",
        vendor_card_id=vc["vc_highrel"].id,
        mpn="AVSAMPLE-PWR-A",
        condition=OfferCondition.NEW,
        status=OfferStatus.WON,
        unit_price=50.0,
        qty=200,
        u_seeder=seeder,
        selected=True,
    )
    o12 = _mk_offer(
        db,
        counts,
        r3b,
        req3.id,
        "AVSAMPLE Pinnacle Components",
        vendor_card_id=vc["vc_pinnacle"].id,
        mpn="AVSAMPLE-PWR-B",
        condition=OfferCondition.NEW,
        status=OfferStatus.WON,
        unit_price=35.0,
        qty=100,
        u_seeder=seeder,
        selected=True,
    )
    _mk_offer(
        db,
        counts,
        r3a,
        req3.id,
        "AVSAMPLE Meridian Electronics",
        vendor_card_id=vc["vc_meridian"].id,
        mpn="AVSAMPLE-PWR-A",
        condition=OfferCondition.PULLS,
        status=OfferStatus.ACTIVE,
        unit_price=52.0,
        qty=200,
        u_seeder=seeder,
    )

    _mk_quote(
        db,
        counts,
        "AVSAMPLE-Q-0003",
        req3.id,
        QuoteStatus.LOST,
        seeder,
        revision=1,
        extra={"result": "lost", "result_reason": "price"},
    )
    q_won = _mk_quote(
        db,
        counts,
        "AVSAMPLE-Q-0004",
        req3.id,
        QuoteStatus.WON,
        seeder,
        revision=2,
        extra={"result": "won", "result_at": _now() - timedelta(days=1), "won_revenue": Decimal(50000)},
    )
    _mk_quote_line(db, counts, q_won, offer=o11, mpn=o11.mpn, qty=200, cost=Decimal(50), sell=Decimal(60))
    _mk_quote_line(db, counts, q_won, offer=o12, mpn=o12.mpn, qty=100, cost=Decimal(35), sell=Decimal(42))

    bp_pending = _mk_buy_plan(
        db,
        counts,
        q_won,
        req3,
        BuyPlanStatus.PENDING,
        SOVerificationStatus.PENDING,
        u,
        extra={
            "submitted_by_id": seeder.id,
            "submitted_at": _now() - timedelta(hours=12),
            "approved_by_id": u["u_manager"].id,
            "approved_at": _now() - timedelta(hours=6),
        },
    )
    if bp_pending is not None:
        _mk_buy_plan_line(
            db,
            counts,
            bp_pending,
            offer=o11,
            requirement_id=r3a.id,
            qty=200,
            status=BuyPlanLineStatus.AWAITING_PO,
            cost=Decimal(50),
            sell=Decimal(60),
            ai_score=92.5,
            buyer=u["u_buyer1"],
            reason="vendor_ownership",
        )
        _mk_buy_plan_line(
            db,
            counts,
            bp_pending,
            offer=o12,
            requirement_id=r3b.id,
            qty=100,
            status=BuyPlanLineStatus.AWAITING_PO,
            cost=Decimal(35),
            sell=Decimal(42),
            ai_score=78.0,
            buyer=u["u_buyer2"],
            reason="vendor_ownership",
        )

    # Separate won quote carries the ACTIVE plan (one-plan-per-quote guard).
    q_won2 = _mk_quote(
        db,
        counts,
        "AVSAMPLE-Q-0005",
        req3.id,
        QuoteStatus.WON,
        seeder,
        revision=1,
        extra={"result": "won", "won_revenue": Decimal(15000)},
    )
    bp_active = _mk_buy_plan(
        db,
        counts,
        q_won2,
        req3,
        BuyPlanStatus.ACTIVE,
        SOVerificationStatus.APPROVED,
        u,
        extra={
            "submitted_by_id": seeder.id,
            "approved_by_id": u["u_manager"].id,
            "so_verified_by_id": u["u_manager"].id,
            "so_verified_at": _now() - timedelta(hours=2),
            "total_cost": Decimal(12500),
            "total_revenue": Decimal(15000),
            "total_margin_pct": Decimal("16.67"),
            "ai_summary": "AVSAMPLE solid sourcing",
        },
    )
    if bp_active is not None and not bp_active.ai_flags:
        bpl_v = _mk_buy_plan_line(
            db,
            counts,
            bp_active,
            offer=o11,
            requirement_id=r3a.id,
            qty=200,
            status=BuyPlanLineStatus.VERIFIED,
            cost=Decimal(50),
            sell=Decimal(60),
            ai_score=92.5,
            buyer=u["u_buyer1"],
            reason="vendor_ownership",
            extra={
                "po_number": "AVSAMPLE-PO-0001",
                "po_confirmed_at": _now() - timedelta(hours=4),
                "po_verified_by_id": u["u_manager"].id,
                "po_verified_at": _now() - timedelta(hours=2),
            },
        )
        bpl_issue = _mk_buy_plan_line(
            db,
            counts,
            bp_active,
            offer=o12,
            requirement_id=r3b.id,
            qty=100,
            status=BuyPlanLineStatus.ISSUE,
            cost=Decimal(35),
            sell=Decimal(42),
            ai_score=78.0,
            buyer=u["u_buyer2"],
            reason="vendor_ownership",
            extra={
                "issue_type": LineIssueType.PRICE_CHANGED.value,
                "issue_note": "AVSAMPLE vendor raised price",
                "po_number": "AVSAMPLE-PO-0002",
            },
        )
        _mk_buy_plan_line(
            db,
            counts,
            bp_active,
            offer=None,
            requirement_id=r3a.id,
            qty=10,
            status=BuyPlanLineStatus.AWAITING_PO,
            cost=Decimal(550),
            sell=Decimal(660),
            ai_score=70.0,
            buyer=u["u_trader"],
            reason="commodity",
        )
        # ai_flags references the issue line id — set only after it is flushed.
        bp_active.ai_flags = [
            {
                "type": "price_increase",
                "severity": AIFlagSeverity.WARNING.value,
                "line_id": bpl_issue.id,
                "message": "Unit cost exceeded target",
            }
        ]
        _ = bpl_v  # constructed for the VERIFIED/PO state coverage
    return req3, {"q_won": q_won, "q_won2": q_won2, "r3a": r3a, "r3b": r3b, "o11": o11, "o12": o12}


# ── WF-E: resell / excess ────────────────────────────────────────────


def _seed_wf_e(db: Session, counts: _Counts, co: dict, mc: dict, vc: dict, u: dict) -> None:
    seeder_owner = u["u_trader"]
    ex1, _ = get_or_create(
        db,
        counts,
        ExcessList,
        {
            "owner_id": seeder_owner.id,
            "status": ExcessListStatus.COLLECTING.value,
            "source_filename": SAMPLE_TAG,
            "version": 1,
            "open_at": _now() - timedelta(days=3),
        },
        company_id=co["co_cust3"].id,
        title="AVSAMPLE · Brightline Surplus Q3",
    )

    def _line(part: str, qty: int, asking: Decimal, condition: str, card_id: int | None) -> ExcessLineItem:
        defaults = {
            "normalized_part_number": normalize_mpn_key(part),
            "quantity": qty,
            "asking_price": asking,
            "condition": condition,
            "material_card_id": card_id,
            "status": ExcessLineItemStatus.AVAILABLE.value,
        }
        row, _ = get_or_create(db, counts, ExcessLineItem, defaults, excess_list_id=ex1.id, part_number=part)
        return row

    eli1 = _line("AVSAMPLE-STM32F103RB", 500, Decimal(30), "New", mc["mc_mcu"].id)
    eli2 = _line("AVSAMPLE-MAX3232", 200, Decimal(12), "New", mc["mc_conn"].id)
    eli3 = _line("AVSAMPLE-NOMATCH-1", 50, Decimal(5), "New", None)

    def _offer(submitted_by: User, vcard: VendorCard, scope: ExcessOfferScope, take_all: Decimal | None) -> ExcessOffer:
        defaults = {
            "offerer_vendor_card_id": vcard.id,
            "status": ExcessOfferStatus.OPEN.value,
            "take_all_total_price": take_all,
            "notes": f"{SAMPLE_TAG} broker offer",
        }
        row, _ = get_or_create(
            db, counts, ExcessOffer, defaults, excess_list_id=ex1.id, submitted_by=submitted_by.id, scope=scope.value
        )
        return row

    def _offer_line(
        off: ExcessOffer,
        mpn: str,
        qty: int,
        price: Decimal | None,
        match: OfferLineMatchStatus,
        eli: ExcessLineItem | None,
    ) -> ExcessOfferLine:
        defaults = {
            "quantity": qty,
            "unit_price": price,
            "match_status": match.value,
            "excess_line_item_id": eli.id if eli else None,
        }
        row, _ = get_or_create(db, counts, ExcessOfferLine, defaults, offer_id=off.id, mpn_raw=mpn)
        return row

    eo1 = _offer(u["u_buyer1"], vc["vc_pinnacle"], ExcessOfferScope.PER_LINE, None)
    eol1a = _offer_line(eo1, "AVSAMPLE-STM32F103RB", 500, Decimal(28), OfferLineMatchStatus.MATCHED, eli1)
    _offer_line(eo1, "AVSAMPLE-MAX3232", 200, Decimal(11), OfferLineMatchStatus.MATCHED, eli2)

    eo2 = _offer(u["u_buyer2"], vc["vc_meridian"], ExcessOfferScope.PER_LINE, None)
    eol2a = _offer_line(eo2, "AVSAMPLE-STM32F103RB", 500, Decimal(26), OfferLineMatchStatus.MATCHED, eli1)
    _offer_line(eo2, "AVSAMPLE-NOMATCH-1", 50, Decimal(4), OfferLineMatchStatus.UNMATCHED, None)

    eo3 = _offer(u["u_trader"], vc["vc_apex"], ExcessOfferScope.PER_LINE, None)
    eol3a = _offer_line(eo3, "AVSAMPLE-MAX3232", 200, Decimal(10), OfferLineMatchStatus.MATCHED, eli2)
    _offer_line(eo3, "avsample-stm32f-103rb", 500, Decimal(27), OfferLineMatchStatus.AMBIGUOUS, None)

    _offer(u["u_buyer1"], vc["vc_highrel"], ExcessOfferScope.TAKE_ALL, Decimal(18000))

    # Best-price rollup (seeder writes it so the column renders without a service call).
    if eli1.best_offer_id is None:
        eli1.best_offer_unit_price = Decimal(26)
        eli1.best_offer_id = eol2a.id
        eli1.offer_count = 2
    if eli2.best_offer_id is None:
        eli2.best_offer_unit_price = Decimal(10)
        eli2.best_offer_id = eol3a.id
        eli2.offer_count = 2
    _ = eol1a  # Pinnacle's matched line retained for the spread/comparison view.

    # Mirrored excess→demand sightings on a scratch virtual req (customer_excess).
    ex_req = _mk_requisition(db, counts, "AVSAMPLE · EXCESS DEMAND", co["co_cust3"], None, "normal", 0, u["u_trader"])
    ex_req.is_scratch = True
    for eli in (eli1, eli2, eli3):
        ex_rq = _mk_requirement(db, counts, ex_req, eli.part_number, SourcingStatus.OPEN, card_id=eli.material_card_id)
        _mk_sighting(
            db,
            counts,
            ex_rq,
            "AVSAMPLE Customer Excess",
            "customer_excess",
            mpn=eli.part_number,
            qty=eli.quantity,
            price=float(eli.asking_price),
            is_authorized=False,
            vendor_score=None,
            source_company_id=co["co_cust3"].id,
        )

    # Clean customer bid-back (internal provenance — never exported).
    cb1, _ = get_or_create(
        db,
        counts,
        CustomerBid,
        {"owner_id": u["u_trader"].id, "status": CustomerBidStatus.DRAFT.value, "notes": SAMPLE_TAG},
        excess_list_id=ex1.id,
        revision=1,
    )
    get_or_create(
        db,
        counts,
        CustomerBidLine,
        {
            "customer_unit_price": Decimal(26),
            "quantity": 500,
            "selected_offer_id": eo2.id,
            "selected_offer_line_id": eol2a.id,
        },
        customer_bid_id=cb1.id,
        excess_line_item_id=eli1.id,
    )
    get_or_create(
        db,
        counts,
        CustomerBidLine,
        {
            "customer_unit_price": Decimal(10),
            "quantity": 200,
            "selected_offer_id": eo3.id,
            "selected_offer_line_id": eol3a.id,
        },
        customer_bid_id=cb1.id,
        excess_line_item_id=eli2.id,
    )

    # Outreach (constructed only — never sent).
    parts_snapshot = [{"mpn": eli1.part_number, "qty": eli1.quantity}]
    out_spec = [
        ("vc_pinnacle", ExcessOutreachChannel.EMAIL, ExcessOutreachStatus.BID, _now() - timedelta(days=3)),
        ("vc_meridian", ExcessOutreachChannel.PHONE, ExcessOutreachStatus.RESPONDED, None),
        ("vc_newvendor", ExcessOutreachChannel.EMAIL, ExcessOutreachStatus.NO_RESPONSE, _now() - timedelta(days=6)),
    ]
    for vkey, channel, status, sent in out_spec:
        get_or_create(
            db,
            counts,
            ExcessOutreach,
            {
                "submitted_by": u["u_trader"].id,
                "channel": channel.value,
                "status": status.value,
                "parts_included": parts_snapshot,
                "sent_at": sent,
            },
            excess_list_id=ex1.id,
            target_vendor_card_id=vc[vkey].id,
            excess_line_item_id=None,
        )

    # BuyerScore — one per offering vendor.
    bs_spec = [
        ("vc_pinnacle", 12, 4, 0.92, {"microcontrollers": 0.8}),
        ("vc_meridian", 6, 1, 0.55, {"analog_ic": 0.6}),
        ("vc_apex", 3, 0, 0.30, {"connectors": 0.4}),
    ]
    for vkey, received, wins, rate, affinity in bs_spec:
        get_or_create(
            db,
            counts,
            BuyerScore,
            {"offers_received": received, "wins": wins, "response_rate": rate, "commodity_affinity": affinity},
            vendor_card_id=vc[vkey].id,
        )


# ── WF-F: sightings spread + vendor unavailability ───────────────────


def _mk_sighting(
    db: Session,
    counts: _Counts,
    req: Requirement,
    vendor_name: str,
    source_type: str,
    *,
    mpn: str,
    qty: int | None,
    price: float | None,
    is_authorized: bool,
    vendor_score: float | None,
    source_company_id: int | None = None,
) -> Sighting:
    score, components = score_sighting_v2(
        vendor_score,
        is_authorized,
        unit_price=price,
        qty_available=qty,
        target_qty=req.target_qty,
        age_hours=24.0,
        has_price=price is not None,
        has_qty=qty is not None,
    )
    defaults = {
        "vendor_name_normalized": normalize_vendor_name(vendor_name),
        "mpn_matched": mpn,
        "qty_available": qty,
        "unit_price": price,
        "is_authorized": is_authorized,
        "confidence": 0.8,
        "score": score,
        "score_components": components,
        "evidence_tier": tier_for_sighting(source_type, is_authorized),
        "source_company_id": source_company_id,
    }
    key = {
        "requirement_id": req.id,
        "source_type": source_type,
        "vendor_name": vendor_name,
        "normalized_mpn": normalize_mpn_key(mpn),
    }
    row, _ = get_or_create(db, counts, Sighting, defaults, **key)
    return row


def _seed_wf_f(db: Session, counts: _Counts, reqs1: dict, vc: dict, u: dict) -> None:
    r1a = reqs1["r1a"]
    r1b = reqs1["r1b"]
    spread = [
        (r1a, "AVSAMPLE Pinnacle Components", "nexar", "AVSAMPLE-STM32F103RB", 500, 49.0, True, 87.5),
        (r1a, "AVSAMPLE Authorized Dist", "digikey", "AVSAMPLE-STM32F103RB", 1000, 51.0, True, 90.0),
        (r1a, "AVSAMPLE Meridian Electronics", "mouser", "AVSAMPLE-STM32F103RB", 200, 53.0, False, 62.0),
        (r1a, "AVSAMPLE Apex Surplus", "brokerbin", "AVSAMPLE-STM32F103RB", 80, 40.0, False, 45.0),
        (r1a, "AVSAMPLE Keystone Distribution", "email_parse", "AVSAMPLE-STM32F103RB", None, 47.0, False, 95.0),
        (r1a, "AVSAMPLE Fresh Supply Co", "manual", "AVSAMPLE-STM32F103RB", 60, None, False, None),
        (r1b, "AVSAMPLE Pinnacle Components", "nexar", "AVSAMPLE-MAX3232", 300, 24.0, True, 87.5),
        (r1b, "AVSAMPLE Meridian Electronics", "brokerbin", "AVSAMPLE-MAX3232", 150, 26.0, False, 62.0),
        (r1b, "AVSAMPLE Keystone Distribution", "digikey", "AVSAMPLE-MAX3232", None, None, False, 95.0),
        # Duplicate-vendor pair (digikey + digikey.com) to exercise fuzzy dedup.
        (r1b, "AVSAMPLE digikey", "digikey", "AVSAMPLE-MAX3232", 400, 23.0, True, 90.0),
        (r1b, "AVSAMPLE digikey.com", "digikey", "AVSAMPLE-MAX3232", 410, 23.5, True, 90.0),
    ]
    for req, vendor, st, mpn, qty, price, auth, vscore in spread:
        _mk_sighting(
            db, counts, req, vendor, st, mpn=mpn, qty=qty, price=price, is_authorized=auth, vendor_score=vscore
        )

    # VendorPartUnavailability — 4 states (armed/released/expired/identity).
    un_spec = [
        (
            "vc_apex",
            "AVSAMPLE-STM32F103RB",
            UnavailabilityReason.BOUGHT_BY_US,
            _now() - timedelta(days=2),
            None,
            None,
            r1a.id,
            80,
        ),
        (
            "vc_meridian",
            "AVSAMPLE-MAX3232",
            UnavailabilityReason.SOLD_ELSEWHERE,
            _now() - timedelta(days=2),
            _now() - timedelta(days=1),
            ReleaseTrigger.OFFER_RECEIVED,
            r1b.id,
            150,
        ),
        (
            "vc_pinnacle",
            "AVSAMPLE-STM32F103RB",
            UnavailabilityReason.BROKEN,
            _now() - timedelta(days=40),
            None,
            None,
            r1a.id,
            500,
        ),
        (
            "vc_apex",
            "AVSAMPLE-MAX3232",
            UnavailabilityReason.DIFFERENT_PART,
            _now() - timedelta(days=120),
            None,
            None,
            r1b.id,
            200,
        ),
    ]
    for vkey, mpn, reason, created_at, released, trigger, req_id, qty in un_spec:
        vendor_norm = normalize_vendor_name(vc[vkey].display_name)
        defaults = {
            "reason": reason.value,
            "created_by_id": u["u_buyer1"].id,
            "created_at": created_at,
            "qty_at_mark": qty,
            "released_at": released,
            "release_trigger": trigger.value if trigger else None,
            "requirement_id": req_id,
            "note": f"{SAMPLE_TAG} unavailability",
        }
        get_or_create(
            db,
            counts,
            VendorPartUnavailability,
            defaults,
            vendor_name_normalized=vendor_norm,
            normalized_mpn=normalize_mpn_key(mpn),
        )


# ── Orchestration ────────────────────────────────────────────────────


def seed(db: Session) -> _Counts:
    """Idempotently seed (or top up) the full sample dataset.

    Returns per-model counts.
    """
    counts: _Counts = {}
    u = _seed_users(db, counts)
    db.flush()
    co = _seed_companies(db, counts, u)
    site = _seed_sites(db, counts, co, u)
    con = _seed_contacts(db, counts, site, co)
    vc = _seed_vendors(db, counts)
    _seed_vendor_contacts(db, counts, vc)
    mc = _seed_material_cards(db, counts)
    _seed_verification_group(db, counts, u)
    db.flush()

    req1, reqs1, _offers_b, _q_draft = _seed_wf_b(db, counts, co, site, mc, vc, u)
    req2, _offers_c, _q_sent, _bp_draft = _seed_wf_c(db, counts, co, vc, u)
    req3, wf_d = _seed_wf_d(db, counts, co, mc, vc, u)
    db.flush()

    _seed_wf_e(db, counts, co, mc, vc, u)
    _seed_wf_f(db, counts, reqs1, vc, u)
    db.flush()

    _seed_tasks(db, counts, co, con, u, {"req1": req1, "req2": req2, "req3": req3})
    _seed_activity(db, counts, co, vc, {"req1": req1, "req2": req2, "req3": req3}, {"q_won": wf_d["q_won"]}, u)

    db.commit()
    _log_counts(counts, action="seed")
    return counts


def _log_counts(counts: _Counts, *, action: str) -> None:
    total_c = sum(b["created"] for b in counts.values())
    total_s = sum(b.get("skipped", 0) for b in counts.values())
    for model in sorted(counts):
        b = counts[model]
        logger.info("seed-sample [{}]: {} → created={} skipped={}", action, model, b["created"], b.get("skipped", 0))
    logger.info("seed-sample [{}]: TOTAL created={} skipped={}", action, total_c, total_s)


# ── Teardown ─────────────────────────────────────────────────────────


def wipe(db: Session) -> dict[str, int]:
    """Delete ONLY tagged sample rows, FK-safe (leaf → root).

    No-op on real data.

    LOAD-BEARING DELETION ORDER (do NOT reorder): several sample-User-referencing
    tables use ``ondelete="RESTRICT"`` in Postgres —
    ``ExcessOffer.submitted_by``, ``ExcessOutreach.submitted_by``,
    ``CustomerBid.owner_id`` and ``ExcessList.owner_id`` (app/models/excess.py).
    The sample Users are therefore deleted **last** and only succeed because every
    RESTRICT child (Excess* rows, BuyPlan*, etc.) is removed earlier. SQLite ignores
    FKs unless ``PRAGMA foreign_keys=ON`` (the test engine sets it), so the order is
    additionally verified by ``test_wipe_succeeds_with_fk_enforcement`` against a
    fully-seeded, FK-enforcing DB. A future reorder that strands a RESTRICT child
    will fail that test (and crash a real Postgres wipe) — keep this order.
    """
    like_email = f"%avsample@{SAMPLE_EMAIL_DOMAIN}"

    user_ids = [r[0] for r in db.query(User.id).filter(User.email.like(like_email))]
    company_ids = [r[0] for r in db.query(Company.id).filter(Company.source == SAMPLE_TAG)]
    vc_ids = [r[0] for r in db.query(VendorCard.id).filter(VendorCard.source == SAMPLE_TAG)]
    card_ids = [r[0] for r in db.query(MaterialCard.id).filter(MaterialCard.normalized_mpn.like("avsample%"))]
    req_ids = [r[0] for r in db.query(Requisition.id).filter(Requisition.name.like(f"{SAMPLE_TAG} ·%"))]
    list_ids = [r[0] for r in db.query(ExcessList.id).filter(ExcessList.source_filename == SAMPLE_TAG)]
    quote_ids = [r[0] for r in db.query(Quote.id).filter(Quote.quote_number.like(f"{SAMPLE_TAG}-Q-%"))]
    requirement_ids = (
        [r[0] for r in db.query(Requirement.id).filter(Requirement.requisition_id.in_(req_ids))] if req_ids else []
    )
    offer_ids = (
        [r[0] for r in db.query(ExcessOffer.id).filter(ExcessOffer.excess_list_id.in_(list_ids))] if list_ids else []
    )
    bid_ids = (
        [r[0] for r in db.query(CustomerBid.id).filter(CustomerBid.excess_list_id.in_(list_ids))] if list_ids else []
    )
    bp_ids = [r[0] for r in db.query(BuyPlan.id).filter(BuyPlan.quote_id.in_(quote_ids))] if quote_ids else []

    deleted: dict[str, int] = {}

    def _del(model: type, where) -> None:
        n = db.query(model).filter(where).delete(synchronize_session=False)
        if n:
            deleted[model.__name__] = deleted.get(model.__name__, 0) + n

    _del(ActivityLog, ActivityLog.external_id.like(f"{SAMPLE_TAG}:%"))
    _del(RequisitionTask, RequisitionTask.source_ref.like(f"{SAMPLE_TAG}:%"))
    _del(VendorPartUnavailability, VendorPartUnavailability.vendor_name_normalized.like("avsample%"))
    if vc_ids:
        _del(BuyerScore, BuyerScore.vendor_card_id.in_(vc_ids))
    if list_ids:
        _del(ExcessOutreach, ExcessOutreach.excess_list_id.in_(list_ids))
    if bid_ids:
        _del(CustomerBidLine, CustomerBidLine.customer_bid_id.in_(bid_ids))
        _del(CustomerBid, CustomerBid.id.in_(bid_ids))
    if offer_ids:
        _del(ExcessOfferLine, ExcessOfferLine.offer_id.in_(offer_ids))
        _del(ExcessOffer, ExcessOffer.id.in_(offer_ids))
    if list_ids:
        # Clear best-offer pointer before dropping lines, then drop the list.
        db.query(ExcessLineItem).filter(ExcessLineItem.excess_list_id.in_(list_ids)).update(
            {"best_offer_id": None}, synchronize_session=False
        )
        _del(ExcessLineItem, ExcessLineItem.excess_list_id.in_(list_ids))
    if bp_ids:
        _del(BuyPlanLine, BuyPlanLine.buy_plan_id.in_(bp_ids))
        _del(BuyPlan, BuyPlan.id.in_(bp_ids))
    if quote_ids:
        _del(QuoteLine, QuoteLine.quote_id.in_(quote_ids))
        _del(Quote, Quote.id.in_(quote_ids))
    if requirement_ids:
        _del(Offer, Offer.requirement_id.in_(requirement_ids))
        _del(Sighting, Sighting.requirement_id.in_(requirement_ids))
    if company_ids:
        _del(Sighting, Sighting.source_company_id.in_(company_ids))
    if requirement_ids:
        _del(Requirement, Requirement.id.in_(requirement_ids))
    if req_ids:
        _del(Requisition, Requisition.id.in_(req_ids))
    if list_ids:
        _del(ExcessList, ExcessList.id.in_(list_ids))
    if vc_ids:
        _del(VendorContact, VendorContact.vendor_card_id.in_(vc_ids))
        _del(VendorCard, VendorCard.id.in_(vc_ids))
    if card_ids:
        _del(MaterialCard, MaterialCard.id.in_(card_ids))
    if company_ids:
        # Detach the primary-contact pointer before dropping contacts.
        db.query(Company).filter(Company.id.in_(company_ids)).update(
            {"primary_contact_id": None}, synchronize_session=False
        )
        site_ids = [r[0] for r in db.query(CustomerSite.id).filter(CustomerSite.company_id.in_(company_ids))]
        if site_ids:
            _del(SiteContact, SiteContact.customer_site_id.in_(site_ids))
            _del(CustomerSite, CustomerSite.id.in_(site_ids))
        _del(Company, Company.id.in_(company_ids))
    if user_ids:
        _del(VerificationGroupMember, VerificationGroupMember.user_id.in_(user_ids))
        _del(User, User.id.in_(user_ids))

    db.commit()
    for model in sorted(deleted):
        logger.info("seed-sample [wipe]: {} → deleted={}", model, deleted[model])
    logger.info("seed-sample [wipe]: TOTAL deleted={}", sum(deleted.values()))
    return deleted


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Seed (or wipe) the AvailAI sample dataset (idempotent additive).")
    parser.add_argument("--wipe", action="store_true", help="Delete only tagged sample rows, then exit.")
    args = parser.parse_args(argv)

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        if args.wipe:
            wipe(db)
        else:
            seed(db)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
