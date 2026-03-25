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
  - Excess lists in every status (with line items and bids)

Called by: manual execution via `docker compose exec app python scripts/seed_test_data.py`
Depends on: app.models, app.constants, app.database
"""

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from sqlalchemy import text

from app.constants import (
    BidStatus,
    BuyPlanLineStatus,
    BuyPlanStatus,
    ExcessLineItemStatus,
    ExcessListStatus,
    OfferStatus,
    QuoteStatus,
    RequisitionStatus,
    SourcingStatus,
    SOVerificationStatus,
)
from app.database import SessionLocal
from app.models.auth import User
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.crm import Company, CustomerSite
from app.models.excess import Bid, ExcessLineItem, ExcessList
from app.models.intelligence import MaterialCard
from app.models.offers import Offer
from app.models.quotes import Quote, QuoteLine
from app.models.sourcing import Requirement, Requisition
from app.models.vendors import VendorCard

now = datetime.now(timezone.utc)


def get_or_create_user(db):
    """Get first admin user for FK references."""
    user = db.query(User).filter(User.role == "admin").first()
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
        "normalized_mpn": "STM32F407VGT6",
        "display_mpn": "STM32F407VGT6",
        "manufacturer": "STMicroelectronics",
        "description": "ARM Cortex-M4 MCU, 1MB Flash, 168MHz",
    },
    {
        "normalized_mpn": "LM7805CT",
        "display_mpn": "LM7805CT",
        "manufacturer": "Texas Instruments",
        "description": "5V Linear Voltage Regulator, TO-220",
    },
    {
        "normalized_mpn": "MAX232CPE",
        "display_mpn": "MAX232CPE+",
        "manufacturer": "Maxim Integrated",
        "description": "Dual RS-232 Driver/Receiver",
    },
    {
        "normalized_mpn": "ADS1115IDGST",
        "display_mpn": "ADS1115IDGST",
        "manufacturer": "Texas Instruments",
        "description": "16-Bit ADC, 4-Ch, I2C",
    },
    {
        "normalized_mpn": "IRFZ44NPBF",
        "display_mpn": "IRFZ44NPBF",
        "manufacturer": "Infineon",
        "description": "N-Channel MOSFET, 55V, 49A",
    },
    {
        "normalized_mpn": "ESP32WROVER-E",
        "display_mpn": "ESP32-WROVER-E",
        "manufacturer": "Espressif",
        "description": "Wi-Fi+BT MCU Module, 4MB PSRAM",
    },
    {
        "normalized_mpn": "SN74HC595N",
        "display_mpn": "SN74HC595N",
        "manufacturer": "Texas Instruments",
        "description": "8-Bit Shift Register, DIP-16",
    },
    {
        "normalized_mpn": "NE555P",
        "display_mpn": "NE555P",
        "manufacturer": "Texas Instruments",
        "description": "Precision Timer, DIP-8",
    },
    {
        "normalized_mpn": "ATmega328P-PU",
        "display_mpn": "ATmega328P-PU",
        "manufacturer": "Microchip",
        "description": "8-bit AVR MCU, 32KB Flash",
    },
    {
        "normalized_mpn": "BAT54S",
        "display_mpn": "BAT54S",
        "manufacturer": "Nexperia",
        "description": "Schottky Barrier Diode, SOT-23",
    },
]

# ── Requisition configs (one per status) ──────────────────────────────

REQ_CONFIGS = [
    {"name": "Acme - MCU Order Q3", "status": RequisitionStatus.DRAFT, "urgency": "normal"},
    {"name": "GlobalChip - Regulator Restock", "status": RequisitionStatus.ACTIVE, "urgency": "normal"},
    {"name": "Pinnacle - ADC Sourcing", "status": RequisitionStatus.SOURCING, "urgency": "hot"},
    {"name": "Acme - MOSFET Eval Kit", "status": RequisitionStatus.OFFERS, "urgency": "normal"},
    {"name": "GlobalChip - WiFi Module RFQ", "status": RequisitionStatus.QUOTING, "urgency": "critical"},
    {"name": "Pinnacle - Shift Register Build", "status": RequisitionStatus.QUOTED, "urgency": "normal"},
    {"name": "Acme - Timer IC Rush", "status": RequisitionStatus.REOPENED, "urgency": "hot"},
    {
        "name": "GlobalChip - AVR Board Win",
        "status": RequisitionStatus.WON,
        "urgency": "normal",
        "opp_value": Decimal("25400.00"),
    },
    {"name": "Pinnacle - Diode Array (lost)", "status": RequisitionStatus.LOST, "urgency": "normal"},
    {"name": "Acme - Legacy RS-232 Parts", "status": RequisitionStatus.ARCHIVED, "urgency": "normal"},
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
        existing = db.query(MaterialCard).filter(MaterialCard.normalized_mpn == m["normalized_mpn"]).first()
        if existing:
            cards.append(existing)
            continue
        mc = MaterialCard(
            normalized_mpn=m["normalized_mpn"],
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
                condition="New",
                notes=f"Test requirement for {mc.display_mpn}",
            )
            db.add(requirement)
            db.flush()
            all_requirements.append(requirement)

            # Create offers for reqs that are past sourcing stage
            if cfg["status"].value in ("offers", "quoting", "quoted", "won", "lost", "reopened"):
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
                        condition="New",
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


def seed_quotes(db, user, requisitions, requirements, offers, sites):
    """Create quotes in every status."""
    quote_configs = [
        {"status": QuoteStatus.DRAFT, "req_idx": 5},
        {"status": QuoteStatus.SENT, "req_idx": 4},
        {"status": QuoteStatus.WON, "req_idx": 7, "won_revenue": Decimal("25400.00")},
        {"status": QuoteStatus.LOST, "req_idx": 8, "result_reason": "Price too high"},
        {"status": QuoteStatus.REVISED, "req_idx": 6, "revision": 2},
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


def seed_buy_plans(db, user, quotes, requisitions, requirements, offers):
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


def seed_excess_lists(db, user, companies, sites, vendor_cards):
    """Create excess lists in every status with line items and bids."""
    excess_configs = [
        {"title": "Acme Q3 Surplus - Passives", "status": ExcessListStatus.DRAFT, "co_idx": 0},
        {"title": "GlobalChip EOL Parts", "status": ExcessListStatus.ACTIVE, "co_idx": 1},
        {"title": "Pinnacle Defense Excess", "status": ExcessListStatus.BIDDING, "co_idx": 3},
        {"title": "Acme Reel Closeout", "status": ExcessListStatus.CLOSED, "co_idx": 0},
        {"title": "Pacific Components Clearance", "status": ExcessListStatus.EXPIRED, "co_idx": 2},
    ]

    excess_parts = [
        ("100K 0402 RES", "Yageo", 50000, Decimal("0.005")),
        ("10uF 0805 CAP", "Samsung Electro-Mechanics", 25000, Decimal("0.012")),
        ("BAV99", "Nexperia", 10000, Decimal("0.035")),
        ("1N4148W", "Vishay", 100000, Decimal("0.008")),
        ("USB-C-16P", "Molex", 5000, Decimal("0.45")),
    ]

    line_statuses = list(ExcessLineItemStatus)
    bid_statuses = list(BidStatus)

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
            status=ecfg["status"].value,
            total_line_items=3,
        )
        db.add(el)
        db.flush()

        # 3 line items per list
        for j in range(3):
            pn, mfg, qty, price = excess_parts[(i + j) % len(excess_parts)]
            ls = line_statuses[(i + j) % len(line_statuses)]

            eli = ExcessLineItem(
                excess_list_id=el.id,
                part_number=pn,
                normalized_part_number=pn.replace(" ", "").upper(),
                manufacturer=mfg,
                quantity=qty,
                condition="New",
                asking_price=price,
                market_price=price * Decimal("1.15"),
                demand_score=30 + (i + j) * 10,
                status=ls.value,
            )
            db.add(eli)
            db.flush()

            # Add bids for bidding/closed lists
            if ecfg["status"].value in ("bidding", "closed"):
                for k in range(2):
                    vc = vendor_cards[(i + j + k) % len(vendor_cards)]
                    bs = bid_statuses[(i + j + k) % len(bid_statuses)]

                    bid = Bid(
                        excess_line_item_id=eli.id,
                        bidder_vendor_card_id=vc.id,
                        unit_price=price * Decimal(str(0.85 + k * 0.1)),
                        quantity_wanted=qty // (2 + k),
                        lead_time_days=5 + k * 3,
                        status=bs.value,
                        source="manual",
                        created_by=user.id,
                    )
                    db.add(bid)

    db.flush()
    logger.info("Excess lists with line items and bids seeded")


def main():
    logger.info("=== Seeding test data ===")
    db = SessionLocal()
    try:
        user = get_or_create_user(db)
        logger.info(f"Using user: {user.name} (id={user.id})")

        companies, sites = seed_companies_and_sites(db, user)
        vendor_cards = seed_vendor_cards(db)
        material_cards = seed_material_cards(db)
        requisitions, requirements, offers = seed_requisitions(db, user, companies, sites, material_cards, vendor_cards)
        quotes = seed_quotes(db, user, requisitions, requirements, offers, sites)
        seed_buy_plans(db, user, quotes, requisitions, requirements, offers)
        seed_excess_lists(db, user, companies, sites, vendor_cards)

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
            UNION ALL SELECT 'bids', count(*) FROM bids
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
        db.rollback()
        logger.exception("Seed failed — rolled back")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
