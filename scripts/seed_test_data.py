"""Seed test data — creates transactions of every type in every stage.

Creates realistic test data across the full transaction lifecycle:
  - Companies (customers, vendors, prospects)
  - Customer sites
  - Vendor cards
  - Material cards (electronic parts)
  - Requisitions in every status
  - Requirements in every sourcing status
  - Offers in every status
  - Quotes in every status (with quote lines)
  - Buy plans in every status (with buy plan lines)
  - Excess lists across the post-rework lifecycle (draft/open/collecting/awarded/
    closed), with inbound broker offers submitted through excess_service and the
    awarded shape derived through award_offer (the single award chokepoint)

Production guard: REFUSES to run unless ALLOW_SAMPLE_DATA_SEED=true is set (same
opt-in flag as app/management/seed_sample_data.py and seed_resell_demo.py), checked
before any DB session is opened.

NOT single-transaction: the excess section's service chokepoints (submit_offer /
award_offer / close_list_without_bid, plus the mirror) commit incrementally, so a
mid-seed failure leaves the work committed up to that point. Every section is
find-or-create idempotent — re-run to complete a partial seed.

Called by: manual execution via
  `docker compose exec -e ALLOW_SAMPLE_DATA_SEED=true app python scripts/seed_test_data.py`
Depends on: app.models, app.constants, app.database, app.services.excess_service
"""

import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from sqlalchemy import text

from app.constants import (
    BuyPlanLineStatus,
    BuyPlanStatus,
    ExcessLineItemStatus,
    ExcessListStatus,
    ExcessOfferScope,
    OfferStatus,
    QuoteStatus,
    RequisitionStatus,
    SourcingStatus,
    SOVerificationStatus,
    UserRole,
)
from app.database import SessionLocal
from app.models.auth import User
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.crm import Company, CustomerSite
from app.models.excess import ExcessLineItem, ExcessList
from app.models.intelligence import MaterialCard
from app.models.offers import Offer
from app.models.quotes import Quote, QuoteLine
from app.models.sourcing import Requirement, Requisition
from app.models.vendors import VendorCard
from app.services import excess_mirror, excess_service
from app.utils.normalization import normalize_mpn_key


def get_or_create_user(db):
    """Get first admin user for FK references."""
    user = db.query(User).filter(User.role == UserRole.ADMIN).first()
    if not user:
        raise RuntimeError("No admin user found — seed at least one user first")
    return user


# ── Companies ─────────────────────────────────────────────────────────

COMPANIES = [
    {
        "name": "Acme Electronics Corp",
        "account_type": "Customer",
        "domain": "acme-electronics.com",
        "industry": "Electronics Manufacturing",
    },
    {
        "name": "GlobalChip Solutions",
        "account_type": "Customer",
        "domain": "globalchip.com",
        "industry": "Semiconductor Distribution",
    },
    {
        "name": "Pacific Components Ltd",
        "account_type": "Prospect",
        "domain": "pacificcomponents.co",
        "industry": "Electronic Components",
    },
    {
        "name": "Pinnacle Micro Systems",
        "account_type": "Customer",
        "domain": "pinnaclemicro.com",
        "industry": "Defense Electronics",
    },
    {"name": "Vertex Supply Co", "account_type": "Partner", "domain": "vertexsupply.com", "industry": "Distribution"},
]

SITES = [
    {
        "company_idx": 0,
        "site_name": "San Jose HQ",
        "city": "San Jose",
        "state": "CA",
        "country": "US",
        "contact_name": "John Smith",
        "contact_email": "jsmith@acme-electronics.com",
    },
    {
        "company_idx": 0,
        "site_name": "Austin Plant",
        "city": "Austin",
        "state": "TX",
        "country": "US",
        "contact_name": "Jane Doe",
        "contact_email": "jdoe@acme-electronics.com",
    },
    {
        "company_idx": 1,
        "site_name": "Dallas Office",
        "city": "Dallas",
        "state": "TX",
        "country": "US",
        "contact_name": "Bob Lee",
        "contact_email": "blee@globalchip.com",
    },
    {
        "company_idx": 3,
        "site_name": "Colorado Springs",
        "city": "Colorado Springs",
        "state": "CO",
        "country": "US",
        "contact_name": "Sarah Chen",
        "contact_email": "schen@pinnaclemicro.com",
    },
]

# ── Vendor Cards ──────────────────────────────────────────────────────

VENDOR_CARDS_DATA = [
    {"normalized_name": "arrow electronics", "display_name": "Arrow Electronics", "domain": "arrow.com"},
    {"normalized_name": "digi-key electronics", "display_name": "Digi-Key Electronics", "domain": "digikey.com"},
    {"normalized_name": "mouser electronics", "display_name": "Mouser Electronics", "domain": "mouser.com"},
    {"normalized_name": "newark element14", "display_name": "Newark Element14", "domain": "newark.com"},
    {"normalized_name": "future electronics", "display_name": "Future Electronics", "domain": "futureelectronics.com"},
    {"normalized_name": "smith micro llc", "display_name": "Smith Micro LLC", "domain": "smithmicro.com"},
]

# ── Material Cards ────────────────────────────────────────────────────

MATERIAL_CARDS_DATA = [
    {
        "display_mpn": "STM32F407VGT6",
        "manufacturer": "STMicroelectronics",
        "description": "ARM Cortex-M4 MCU, 1MB Flash, 168MHz",
    },
    {
        "display_mpn": "LM7805CT",
        "manufacturer": "Texas Instruments",
        "description": "5V Linear Voltage Regulator, TO-220",
    },
    {
        "display_mpn": "MAX232CPE+",
        "manufacturer": "Maxim Integrated",
        "description": "Dual RS-232 Driver/Receiver",
    },
    {
        "display_mpn": "ADS1115IDGST",
        "manufacturer": "Texas Instruments",
        "description": "16-Bit ADC, 4-Ch, I2C",
    },
    {
        "display_mpn": "IRFZ44NPBF",
        "manufacturer": "Infineon",
        "description": "N-Channel MOSFET, 55V, 49A",
    },
    {
        "display_mpn": "ESP32-WROVER-E",
        "manufacturer": "Espressif",
        "description": "Wi-Fi+BT MCU Module, 4MB PSRAM",
    },
    {
        "display_mpn": "SN74HC595N",
        "manufacturer": "Texas Instruments",
        "description": "8-Bit Shift Register, DIP-16",
    },
    {
        "display_mpn": "NE555P",
        "manufacturer": "Texas Instruments",
        "description": "Precision Timer, DIP-8",
    },
    {
        "display_mpn": "ATmega328P-PU",
        "manufacturer": "Microchip",
        "description": "8-bit AVR MCU, 32KB Flash",
    },
    {
        "display_mpn": "BAT54S",
        "manufacturer": "Nexperia",
        "description": "Schottky Barrier Diode, SOT-23",
    },
]

# ── Requisition configs (one per status) ──────────────────────────────

REQ_CONFIGS = [
    {"name": "Acme - MCU Order Q3", "status": RequisitionStatus.DRAFT, "urgency": "normal"},
    {"name": "GlobalChip - Regulator Restock", "status": RequisitionStatus.OPEN, "urgency": "normal"},
    {"name": "Pinnacle - ADC Sourcing", "status": RequisitionStatus.RFQS_SENT, "urgency": "hot"},
    {"name": "Acme - MOSFET Eval Kit", "status": RequisitionStatus.OFFERS, "urgency": "critical"},
    {"name": "Pinnacle - Shift Register Build", "status": RequisitionStatus.QUOTED, "urgency": "normal"},
    {
        "name": "GlobalChip - AVR Board Win",
        "status": RequisitionStatus.WON,
        "urgency": "normal",
        "opp_value": Decimal("25400.00"),
    },
    {"name": "Pinnacle - Diode Array (lost)", "status": RequisitionStatus.LOST, "urgency": "normal"},
    {"name": "Acme - Timer IC Watch", "status": RequisitionStatus.HOTLIST, "urgency": "hot"},
    {"name": "GlobalChip - Cancelled Prototype", "status": RequisitionStatus.CANCELLED, "urgency": "normal"},
]


def seed_companies_and_sites(db, user):
    """Create companies and customer sites, return lists."""
    companies = []
    for c in COMPANIES:
        existing = db.query(Company).filter(Company.domain == c["domain"]).first()
        if existing:
            companies.append(existing)
            continue
        co = Company(
            name=c["name"],
            account_type=c["account_type"],
            domain=c["domain"],
            industry=c["industry"],
            is_active=True,
            account_owner_id=user.id,
        )
        db.add(co)
        db.flush()
        companies.append(co)
    logger.info(f"Companies: {len(companies)} ready")

    sites = []
    for s in SITES:
        co = companies[s["company_idx"]]
        existing = (
            db.query(CustomerSite)
            .filter(CustomerSite.company_id == co.id, CustomerSite.site_name == s["site_name"])
            .first()
        )
        if existing:
            sites.append(existing)
            continue
        site = CustomerSite(
            company_id=co.id,
            site_name=s["site_name"],
            city=s["city"],
            state=s["state"],
            country=s["country"],
            contact_name=s["contact_name"],
            contact_email=s["contact_email"],
            owner_id=user.id,
            is_active=True,
        )
        db.add(site)
        db.flush()
        sites.append(site)
    logger.info(f"Sites: {len(sites)} ready")
    return companies, sites


def seed_vendor_cards(db):
    """Create vendor cards, return list."""
    cards = []
    for v in VENDOR_CARDS_DATA:
        existing = db.query(VendorCard).filter(VendorCard.normalized_name == v["normalized_name"]).first()
        if existing:
            cards.append(existing)
            continue
        vc = VendorCard(
            normalized_name=v["normalized_name"],
            display_name=v["display_name"],
            domain=v["domain"],
        )
        db.add(vc)
        db.flush()
        cards.append(vc)
    logger.info(f"Vendor cards: {len(cards)} ready")
    return cards


def seed_material_cards(db):
    """Create material cards, return list."""
    cards = []
    for m in MATERIAL_CARDS_DATA:
        # Canonical key form — literal display-form values would recreate the
        # exact drift migration 206 cleans up.
        norm = normalize_mpn_key(m["display_mpn"])
        existing = db.query(MaterialCard).filter(MaterialCard.normalized_mpn == norm).first()
        if existing:
            cards.append(existing)
            continue
        mc = MaterialCard(
            normalized_mpn=norm,
            display_mpn=m["display_mpn"],
            manufacturer=m["manufacturer"],
            description=m["description"],
        )
        db.add(mc)
        db.flush()
        cards.append(mc)
    logger.info(f"Material cards: {len(cards)} ready")
    return cards


def seed_requisitions(db, user, companies, sites, material_cards, vendor_cards):
    """Create requisitions in every status, with requirements, offers, quotes, buy
    plans."""
    requisitions = []
    all_requirements = []
    all_offers = []

    sourcing_statuses = list(SourcingStatus)

    for i, cfg in enumerate(REQ_CONFIGS):
        # Check if already seeded (by name)
        existing = db.query(Requisition).filter(Requisition.name == cfg["name"]).first()
        if existing:
            requisitions.append(existing)
            logger.info(f"  Requisition '{cfg['name']}' already exists, skipping")
            continue

        co = companies[i % len(companies)]
        site = sites[i % len(sites)]

        req = Requisition(
            name=cfg["name"],
            status=cfg["status"].value,
            company_id=co.id,
            customer_site_id=site.id,
            created_by=user.id,
            urgency=cfg.get("urgency", "normal"),
            opportunity_value=cfg.get("opp_value"),
            deadline="2026-04-15" if cfg["urgency"] == "critical" else None,
        )
        db.add(req)
        db.flush()
        requisitions.append(req)

        # Create 2-3 requirements per req, cycling through sourcing statuses
        num_reqs = 2 + (i % 2)
        for j in range(num_reqs):
            mc = material_cards[(i * 3 + j) % len(material_cards)]
            ss = sourcing_statuses[(i + j) % len(sourcing_statuses)]

            requirement = Requirement(
                requisition_id=req.id,
                material_card_id=mc.id,
                primary_mpn=mc.display_mpn,
                normalized_mpn=mc.normalized_mpn,
                target_qty=25 * (j + 1),
                target_price=Decimal(str(round(1.5 + i * 0.75, 2))),
                sourcing_status=ss.value,
                condition="new",  # chk_req_condition vocab
                notes=f"Test requirement for {mc.display_mpn}",
            )
            db.add(requirement)
            db.flush()
            all_requirements.append(requirement)

            # Create offers for reqs that are past sourcing stage
            if cfg["status"] in (
                RequisitionStatus.OFFERS,
                RequisitionStatus.QUOTED,
                RequisitionStatus.WON,
                RequisitionStatus.LOST,
            ):
                offer_statuses = list(OfferStatus)
                for k in range(2):
                    vc = vendor_cards[(i + j + k) % len(vendor_cards)]
                    os_ = offer_statuses[(i + j + k) % len(offer_statuses)]
                    offer = Offer(
                        requisition_id=req.id,
                        requirement_id=requirement.id,
                        material_card_id=mc.id,
                        vendor_card_id=vc.id,
                        vendor_name=vc.display_name,
                        vendor_name_normalized=vc.normalized_name,
                        mpn=mc.display_mpn,
                        normalized_mpn=mc.normalized_mpn,
                        manufacturer=mc.manufacturer,
                        qty_available=50 * (k + 1),
                        unit_price=Decimal(str(round(2.25 + k * 0.5, 2))),
                        currency="USD",
                        lead_time=f"{3 + k * 2} weeks",
                        condition="new",  # chk_req_condition vocab
                        source="manual",
                        status=os_.value,
                        entered_by_id=user.id,
                    )
                    db.add(offer)
                    db.flush()
                    all_offers.append(offer)

    logger.info(
        f"Requisitions: {len(requisitions)} | Requirements: {len(all_requirements)} | Offers: {len(all_offers)}"
    )
    return requisitions, all_requirements, all_offers


def seed_quotes(db, user, requisitions, offers, sites, *, now):
    """Create quotes in every status."""
    quote_configs = [
        {"status": QuoteStatus.DRAFT, "req_idx": 3},
        {"status": QuoteStatus.SENT, "req_idx": 4},
        {"status": QuoteStatus.WON, "req_idx": 5, "won_revenue": Decimal("25400.00")},
        {"status": QuoteStatus.LOST, "req_idx": 6, "result_reason": "Price too high"},
        {"status": QuoteStatus.REVISED, "req_idx": 4, "revision": 2},
    ]

    quotes = []
    for i, qcfg in enumerate(quote_configs):
        quote_num = f"Q-2026-TEST-{i + 1:03d}"
        existing = db.query(Quote).filter(Quote.quote_number == quote_num).first()
        if existing:
            quotes.append(existing)
            continue

        req = requisitions[qcfg["req_idx"]]
        site = sites[i % len(sites)]

        q = Quote(
            requisition_id=req.id,
            customer_site_id=site.id,
            quote_number=quote_num,
            revision=qcfg.get("revision", 1),
            line_items=[],  # legacy JSON field
            subtotal=Decimal("5000.00") + Decimal(str(i * 1200)),
            total_cost=Decimal("3500.00") + Decimal(str(i * 800)),
            total_margin_pct=Decimal("30.00"),
            payment_terms="Net 30",
            shipping_terms="FOB Origin",
            validity_days=7,
            status=qcfg["status"].value,
            created_by_id=user.id,
            sent_at=now - timedelta(days=3) if qcfg["status"] != QuoteStatus.DRAFT else None,
            won_revenue=qcfg.get("won_revenue"),
            result_reason=qcfg.get("result_reason"),
        )
        db.add(q)
        db.flush()
        quotes.append(q)

        # Add 2 quote lines per quote
        for j in range(2):
            # Find offers linked to this requisition
            req_offers = [o for o in offers if o.requisition_id == req.id]
            offer = req_offers[j] if j < len(req_offers) else None

            ql = QuoteLine(
                quote_id=q.id,
                material_card_id=offer.material_card_id if offer else None,
                offer_id=offer.id if offer else None,
                mpn=offer.mpn if offer else f"TEST-MPN-{j}",
                manufacturer=offer.manufacturer if offer else "Test Mfg",
                qty=25 * (j + 1),
                cost_price=Decimal("2.50") + Decimal(str(j)),
                sell_price=Decimal("3.75") + Decimal(str(j * 0.5)),
                margin_pct=Decimal("33.33"),
            )
            db.add(ql)

    db.flush()
    logger.info(f"Quotes: {len(quotes)} with lines")
    return quotes


def seed_buy_plans(db, user, quotes, offers, *, now):
    """Create buy plans in every status."""
    bp_configs = [
        {"status": BuyPlanStatus.DRAFT, "so_status": SOVerificationStatus.PENDING, "quote_idx": 0},
        {"status": BuyPlanStatus.PENDING, "so_status": SOVerificationStatus.PENDING, "quote_idx": 1},
        {"status": BuyPlanStatus.ACTIVE, "so_status": SOVerificationStatus.APPROVED, "quote_idx": 2},
        {"status": BuyPlanStatus.HALTED, "so_status": SOVerificationStatus.APPROVED, "quote_idx": 2},
        {"status": BuyPlanStatus.COMPLETED, "so_status": SOVerificationStatus.APPROVED, "quote_idx": 2},
        {"status": BuyPlanStatus.CANCELLED, "so_status": SOVerificationStatus.REJECTED, "quote_idx": 3},
    ]

    buy_plans = []
    line_statuses = list(BuyPlanLineStatus)

    for i, bpcfg in enumerate(bp_configs):
        q = quotes[bpcfg["quote_idx"]]

        # Check if already seeded
        existing = db.query(BuyPlan).filter(BuyPlan.quote_id == q.id, BuyPlan.status == bpcfg["status"].value).first()
        if existing:
            buy_plans.append(existing)
            continue

        bp = BuyPlan(
            quote_id=q.id,
            requisition_id=q.requisition_id,
            sales_order_number=f"SO-TEST-{i + 1:03d}" if i >= 2 else None,
            customer_po_number=f"PO-CUST-{i + 1:03d}" if i >= 2 else None,
            status=bpcfg["status"].value,
            so_status=bpcfg["so_status"].value,
            total_cost=Decimal("3500.00"),
            total_revenue=Decimal("5000.00"),
            total_margin_pct=Decimal("30.00"),
            submitted_by_id=user.id if i >= 1 else None,
            submitted_at=now - timedelta(days=5) if i >= 1 else None,
            approved_by_id=user.id if i >= 2 else None,
            approved_at=now - timedelta(days=4) if i >= 2 else None,
            completed_at=now - timedelta(days=1) if bpcfg["status"] == BuyPlanStatus.COMPLETED else None,
            cancelled_at=now if bpcfg["status"] == BuyPlanStatus.CANCELLED else None,
            cancelled_by_id=user.id if bpcfg["status"] == BuyPlanStatus.CANCELLED else None,
            cancellation_reason="Customer cancelled order" if bpcfg["status"] == BuyPlanStatus.CANCELLED else None,
        )
        db.add(bp)
        db.flush()
        buy_plans.append(bp)

        # Add 2 buy plan lines per plan
        req_offers = [o for o in offers if o.requisition_id == q.requisition_id]
        for j in range(min(2, max(1, len(req_offers)))):
            offer = req_offers[j] if j < len(req_offers) else None
            ls = line_statuses[(i + j) % len(line_statuses)]

            bpl = BuyPlanLine(
                buy_plan_id=bp.id,
                requirement_id=offer.requirement_id if offer else None,
                offer_id=offer.id if offer else None,
                quantity=25 * (j + 1),
                unit_cost=Decimal("2.50"),
                unit_sell=Decimal("3.75"),
                margin_pct=Decimal("33.33"),
                buyer_id=user.id,
                status=ls.value,
                po_number=f"PO-V-{i:02d}-{j}"
                if ls in (BuyPlanLineStatus.VERIFIED, BuyPlanLineStatus.PENDING_VERIFY)
                else None,
            )
            db.add(bpl)

    db.flush()
    logger.info(f"Buy plans: {len(buy_plans)} with lines")
    return buy_plans


# Non-owner broker who submits the seeded inbound offers — the Phase-1 self-offer
# guard (excess_service.submit_offer) forbids the list owner offering on their own list.
SEED_BROKER_EMAIL = "seed.broker@availai.test"


def get_or_create_offer_broker(db):
    """Find-or-create the non-owner broker user who submits seeded inbound offers."""
    broker = db.query(User).filter(User.email == SEED_BROKER_EMAIL).first()
    if broker is None:
        broker = User(email=SEED_BROKER_EMAIL, name="Seed Test Broker", role=UserRole.BUYER.value, is_active=True)
        db.add(broker)
        db.flush()
    return broker


def seed_excess_lists(db, user, companies, sites):
    """Create excess lists across the post-rework lifecycle with genuine offers.

    Every list starts as a DRAFT and derives its final shape through the real service
    chokepoints, so statuses, rollups, and mirrors are exactly what the app produces:
      - draft       — constructed only,
      - open        — excess_mirror.publish_list (mirrors the lines),
      - collecting  — publish + inbound offers via excess_service.submit_offer
                      (the first offer flips open → collecting),
      - awarded     — publish + a full-coverage offer + excess_service.award_offer
                      (the single chokepoint: WON offer, awarded lines, rollups),
      - closed      — publish + excess_service.close_list_without_bid.
    Offers come from a dedicated non-owner broker user with buyer attribution
    (buyer_company_id → counterparty VendorCard), mirroring the module's own flow.
    """
    excess_configs = [
        {"title": "Acme Q3 Surplus - Passives", "shape": "draft", "co_idx": 0},
        {"title": "GlobalChip EOL Parts", "shape": "open", "co_idx": 1},
        {"title": "Pinnacle Defense Excess", "shape": "collecting", "co_idx": 3},
        {"title": "Acme Reel Closeout", "shape": "awarded", "co_idx": 0},
        {"title": "Pacific Components Clearance", "shape": "closed", "co_idx": 2},
    ]

    excess_parts = [
        ("100K 0402 RES", "Yageo", 50000, Decimal("0.005")),
        ("10uF 0805 CAP", "Samsung Electro-Mechanics", 25000, Decimal("0.012")),
        ("BAV99", "Nexperia", 10000, Decimal("0.035")),
        ("1N4148W", "Vishay", 100000, Decimal("0.008")),
        ("USB-C-16P", "Molex", 5000, Decimal("0.45")),
    ]

    broker = get_or_create_offer_broker(db)
    buyer_co = companies[4]  # Vertex Supply Co (Partner) — the buying counterparty

    for i, ecfg in enumerate(excess_configs):
        co = companies[ecfg["co_idx"]]

        existing = db.query(ExcessList).filter(ExcessList.title == ecfg["title"]).first()
        if existing:
            logger.info(f"  Excess '{ecfg['title']}' exists, skipping")
            continue

        site = sites[ecfg["co_idx"] % len(sites)]
        el = ExcessList(
            company_id=co.id,
            customer_site_id=site.id,
            owner_id=user.id,
            title=ecfg["title"],
            status=ExcessListStatus.DRAFT.value,
            total_line_items=3,
        )
        db.add(el)
        db.flush()

        # 3 line items per list — all start AVAILABLE; award_offer flips the sold ones.
        for j in range(3):
            pn, mfg, qty, price = excess_parts[(i + j) % len(excess_parts)]
            eli = ExcessLineItem(
                excess_list_id=el.id,
                part_number=pn,
                normalized_part_number=normalize_mpn_key(pn) or None,
                manufacturer=mfg,
                quantity=qty,
                condition="new",  # chk_req_condition vocab
                asking_price=price,
                status=ExcessLineItemStatus.AVAILABLE.value,
            )
            db.add(eli)
        db.flush()

        shape = ecfg["shape"]
        if shape == "draft":
            continue

        # draft → open through the real publish path (stamps open_at, mirrors lines).
        excess_mirror.publish_list(db, el.id, user)

        if shape in ("collecting", "awarded"):
            lines = db.query(ExcessLineItem).filter_by(excess_list_id=el.id).order_by(ExcessLineItem.id).all()
            offer = excess_service.submit_offer(
                db,
                list_id=el.id,
                user=broker,
                scope=ExcessOfferScope.PER_LINE,
                notes="Seeded inbound broker offer (full coverage)",
                buyer_company_id=buyer_co.id,
                lines=[
                    {
                        "mpn_raw": ln.part_number,
                        "quantity": max(1, (ln.quantity or 1) // 2),
                        "unit_price": (ln.asking_price or Decimal("1")) * Decimal("0.90"),
                        "lead_time_days": 5,
                    }
                    for ln in lines
                ],
            )
            # A competing offer on the lead line so rollups have a spread to pick from.
            excess_service.submit_offer(
                db,
                list_id=el.id,
                user=broker,
                scope=ExcessOfferScope.PER_LINE,
                notes="Seeded competing broker offer",
                buyer_company_id=buyer_co.id,
                lines=[
                    {
                        "mpn_raw": lines[0].part_number,
                        "quantity": lines[0].quantity,
                        "unit_price": (lines[0].asking_price or Decimal("1")) * Decimal("0.95"),
                        "lead_time_days": 12,
                    }
                ],
            )
            if shape == "awarded":
                # The single award chokepoint: flips the offer WON, lines AWARDED,
                # closes competitors LOST, recomputes rollups, retires the mirror,
                # and derives the list's own awarded status.
                excess_service.award_offer(db, offer.id, user)
        elif shape == "closed":
            excess_service.close_list_without_bid(db, el.id, user)

    db.flush()
    logger.info("Excess lists seeded across the post-rework lifecycle (real offers/awards)")


def main():
    # Hard production guard: refuse to seed synthetic test data unless the operator has
    # explicitly opted in (same ALLOW_SAMPLE_DATA_SEED flag as seed_sample_data /
    # seed_resell_demo). Checked BEFORE opening a DB session so a refused run never
    # touches whatever database SessionLocal resolves to.
    if os.getenv("ALLOW_SAMPLE_DATA_SEED", "").strip().lower() not in {"1", "true", "yes", "on"}:
        print(
            "REFUSED: test-data seeding is disabled unless ALLOW_SAMPLE_DATA_SEED=true is set "
            "(guards against injecting synthetic data into production). Set it explicitly to seed.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    logger.info("=== Seeding test data ===")
    now = datetime.now(UTC)
    db = SessionLocal()
    try:
        user = get_or_create_user(db)
        logger.info(f"Using user: {user.name} (id={user.id})")

        companies, sites = seed_companies_and_sites(db, user)
        vendor_cards = seed_vendor_cards(db)
        material_cards = seed_material_cards(db)
        requisitions, requirements, offers = seed_requisitions(db, user, companies, sites, material_cards, vendor_cards)
        quotes = seed_quotes(db, user, requisitions, offers, sites, now=now)
        seed_buy_plans(db, user, quotes, offers, now=now)
        seed_excess_lists(db, user, companies, sites)

        db.commit()
        logger.info("=== All test data committed ===")

        # Summary
        counts = db.execute(
            text("""
            SELECT 'requisitions' as tbl, count(*) as cnt FROM requisitions
            UNION ALL SELECT 'requirements', count(*) FROM requirements
            UNION ALL SELECT 'offers', count(*) FROM offers
            UNION ALL SELECT 'quotes', count(*) FROM quotes
            UNION ALL SELECT 'quote_lines', count(*) FROM quote_lines
            UNION ALL SELECT 'buy_plans', count(*) FROM buy_plans_v3
            UNION ALL SELECT 'buy_plan_lines', count(*) FROM buy_plan_lines
            UNION ALL SELECT 'excess_lists', count(*) FROM excess_lists
            UNION ALL SELECT 'excess_line_items', count(*) FROM excess_line_items
            UNION ALL SELECT 'excess_offers', count(*) FROM excess_offers
            UNION ALL SELECT 'excess_offer_lines', count(*) FROM excess_offer_lines
            UNION ALL SELECT 'companies', count(*) FROM companies
            UNION ALL SELECT 'vendor_cards', count(*) FROM vendor_cards
            UNION ALL SELECT 'material_cards', count(*) FROM material_cards
            ORDER BY 1
        """)
        ).fetchall()
        logger.info("── Final counts ──")
        for tbl, cnt in counts:
            logger.info(f"  {tbl}: {cnt}")

    except Exception:
        # NOT all-or-nothing: seed_excess_lists routes through the real service
        # chokepoints (submit_offer/award_offer/close_list_without_bid), which commit
        # the shared session incrementally — so a failure here rolls back only work
        # since the last service commit. Every section is find-or-create idempotent:
        # re-run to complete a partial seed.
        db.rollback()
        logger.exception(
            "Seed failed — uncommitted work rolled back (service chokepoints commit incrementally; re-run to complete a partial seed)"
        )
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
