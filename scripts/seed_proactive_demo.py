#!/usr/bin/env python
"""Seed realistic TRIO-style demo data for the Proactive tab.

What it does: builds a believable proactive-selling scenario for one salesperson —
enterprise customers (data-center / server / storage buyers), their purchase history
(customer_part_history), live vendor stock (offers) for the same parts, then runs the
matching engine to mint ProactiveMatch rows, plus a few sent/converted ProactiveOffers
so the Sent tab and Scorecard are non-empty.

Called by: a human, manually, against the live DB —
    docker compose exec app python scripts/seed_proactive_demo.py
    docker compose exec app python scripts/seed_proactive_demo.py --wipe   # remove demo data

Depends on: app.models, app.services.proactive_matching.find_matches_for_offer.
Idempotent: re-running refreshes match created_at (keeps them inside the 7-day "new"
window) without duplicating companies/parts/offers. All demo rows are tagged with the
SEED_TAG marker so --wipe removes exactly what this script created and nothing else.

NOTE: writes only CRM/offer/proactive rows. It never mutates MaterialCard category/specs
(that must go through the F1 spec_tiers ladder), so created demo cards carry MPN only.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from loguru import logger

from app.database import SessionLocal
from app.models import (
    Company,
    CustomerSite,
    MaterialCard,
    Offer,
    Requisition,
    SiteContact,
    User,
    VendorCard,
)
from app.models.intelligence import ProactiveMatch, ProactiveOffer, ProactiveThrottle
from app.models.purchase_history import CustomerPartHistory
from app.services.proactive_matching import find_matches_for_offer

SEED_TAG = "PROACTIVE_DEMO_SEED"
VIEWER_EMAIL = "mkhoury@trioscs.com"
NOW = datetime.now(UTC)


def norm(mpn: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", mpn.upper())


# ── Curated enterprise parts (display values drive the match rows) ──────────────
# (key, display_mpn, manufacturer, description, our_cost, condition)
PARTS = {
    "P1": ("HMA82GR7CJR4N-XN", "SK Hynix", "16GB DDR4-3200 RDIMM 1Rx4 ECC Registered", 58.0, "New"),
    "P2": ("M393A4K40DB3-CWE", "Samsung", "32GB DDR4-3200 RDIMM 2Rx4 ECC Registered", 72.0, "New"),
    "P3": ("SSDPEDME800G401", "Intel", "DC P3700 800GB NVMe PCIe 3.0 HHHL SSD", 210.0, "New"),
    "P4": ("MZQL23T8HCLS-00A07", "Samsung", "PM9A3 3.84TB U.2 NVMe Enterprise SSD", 295.0, "New"),
    "P5": ("WUH722222ALE6L4", "Western Digital", "Ultrastar DC HC570 22TB SATA 3.5in HDD", 290.0, "New"),
    "P6": ("ST16000NM001G", "Seagate", "Exos X16 16TB SATA 7200RPM 3.5in HDD", 175.0, "New"),
    "P7": ("X710-DA2", "Intel", "Ethernet Converged Network Adapter Dual SFP+", 180.0, "New"),
    "P8": ("MCX516A-CCAT", "NVIDIA Mellanox", "ConnectX-5 EN 100GbE Dual QSFP28 NIC", 405.0, "New"),
    "P9": ("SRF7J", "Intel", "Xeon Gold 6248R 24-Core 3.0GHz Processor", 820.0, "Refurbished"),
    "P10": ("UCS-MR-X32G2RW", "Cisco", "32GB DDR4-3200 RDIMM (Cisco UCS)", 76.0, "New"),
}

# ── Vendors offering the stock (some get a VendorCard for the reliability badge) ─
# (key, vendor_name, qty, vendor_card: None | ("trusted", score) | ("unreliable", ghost_rate))
VENDOR_OF = {
    "P1": ("Avnet", 1200, ("trusted", 88.0)),
    "P2": ("TD SYNNEX", 800, None),
    "P3": ("Arrow Electronics", 240, ("trusted", 82.0)),
    "P4": ("Flagship Technologies", 150, None),
    "P5": ("ServerSupply", 320, None),
    "P6": ("Stordis", 500, None),
    "P7": ("Arrow Electronics", 600, ("trusted", 82.0)),
    "P8": ("Apex Brokers", 90, ("unreliable", 0.42)),
    "P9": ("ServerMonkey", 60, None),
    "P10": ("ASI Corp", 400, None),
}

# ── Customers (TRIO-style fictional buyers) + their contacts ────────────────────
# name, site_name, city, state, [(full_name, title, email, is_primary)]
CUSTOMERS = {
    "cascade": (
        "Cascade Data Systems",
        "Cascade DC-1",
        "Hillsboro",
        "OR",
        [
            ("Priya Nair", "Procurement Manager", "priya.nair@cascadedata.example", True),
            ("Tom Reyes", "Hardware Buyer", "tom.reyes@cascadedata.example", False),
        ],
    ),
    "northwind": (
        "Northwind Server Solutions",
        "Northwind HQ",
        "Eden Prairie",
        "MN",
        [("Greg Olsen", "Senior Buyer", "greg.olsen@northwindservers.example", True)],
    ),
    "meridian": (
        "Meridian Cloud Infrastructure",
        "Meridian East",
        "Ashburn",
        "VA",
        [
            ("Dana Whitfield", "Supply Chain Lead", "dana.whitfield@meridiancloud.example", True),
            ("Luis Ortega", "Component Buyer", "luis.ortega@meridiancloud.example", False),
        ],
    ),
    "atlas": (
        "Atlas Storage Networks",
        "Atlas West",
        "Fremont",
        "CA",
        [("Karen Liu", "Procurement Specialist", "karen.liu@atlasstorage.example", True)],
    ),
    "helix": (
        "Helix Semiconductor Trading",
        "Helix HQ",
        "Plano",
        "TX",
        [("Sam Becker", "Senior Trader", "sam.becker@helixsemi.example", True)],
    ),
}

# ── Purchase history matrix: customer -> [(part, count, days_ago, markup, last_qty, total_qty)]
#    markup = avg_unit_price / our_cost  → margin% = (1 - 1/markup) * 100
PURCHASES = {
    "cascade": [
        ("P1", 5, 45, 1.45, 600, 3000),
        ("P3", 3, 90, 1.30, 120, 360),
        ("P5", 2, 200, 1.20, 100, 200),
        ("P6", 4, 120, 1.55, 250, 1000),
    ],
    "northwind": [("P2", 6, 30, 1.50, 400, 2400), ("P9", 2, 300, 1.25, 24, 48), ("P10", 3, 75, 1.35, 200, 600)],
    "meridian": [
        ("P4", 4, 60, 1.40, 80, 320),
        ("P8", 3, 110, 1.60, 40, 120),
        ("P7", 5, 40, 1.30, 300, 1500),
        ("P1", 2, 210, 1.18, 200, 400),
    ],
    "atlas": [("P5", 6, 25, 1.50, 300, 1800), ("P6", 3, 150, 1.35, 150, 450), ("P4", 2, 220, 1.22, 40, 80)],
    "helix": [
        ("P2", 8, 20, 1.28, 500, 4000),
        ("P9", 4, 95, 1.45, 48, 192),
        ("P10", 5, 55, 1.33, 250, 1250),
        ("P8", 2, 260, 1.30, 20, 40),
    ],
}

# ── Sent / converted offers for the Sent tab + Scorecard ────────────────────────
# (customer_key, status, days_ago, [(part, qty, markup)])
SENT_OFFERS = [
    ("cascade", "converted", 12, [("P6", 250, 1.55), ("P1", 300, 1.45)]),
    ("helix", "converted", 9, [("P2", 500, 1.28)]),
    ("northwind", "sent", 6, [("P10", 200, 1.35), ("P9", 24, 1.25)]),
    ("meridian", "sent", 4, [("P7", 300, 1.30)]),
    ("atlas", "sent", 3, [("P5", 300, 1.50)]),
    ("cascade", "sent", 2, [("P3", 120, 1.30)]),
]


def get_or_create_card(db, key) -> MaterialCard:
    display, mfr, desc, cost, cond = PARTS[key]
    nm = norm(display)
    card = (
        db.query(MaterialCard)
        .filter((MaterialCard.normalized_mpn == nm) | (MaterialCard.display_mpn == display))
        .first()
    )
    if card:
        return card
    card = MaterialCard(normalized_mpn=nm, display_mpn=display)
    db.add(card)
    db.flush()
    logger.info(f"  created demo card {display} (id={card.id})")
    return card


def get_or_create_vendor_card(db, vendor_name, kind):
    nn = vendor_name.lower().strip()
    vc = db.query(VendorCard).filter(VendorCard.normalized_name == nn).first()
    if not vc:
        vc = VendorCard(normalized_name=nn, display_name=vendor_name)
        db.add(vc)
    if kind:
        flavor, val = kind
        if flavor == "trusted":
            vc.vendor_score = val
            vc.ghost_rate = 0.05
            vc.total_wins = 18
            vc.overall_win_rate = 0.46
        else:
            vc.vendor_score = 35.0
            vc.ghost_rate = val
            vc.total_wins = 1
            vc.overall_win_rate = 0.06
    db.flush()
    return vc


def wipe(db, viewer):
    """Remove exactly the demo data this script created."""
    company_ids = [c.id for c in db.query(Company.id).filter(Company.name.in_([v[0] for v in CUSTOMERS.values()]))]
    site_ids = (
        [s.id for s in db.query(CustomerSite.id).filter(CustomerSite.company_id.in_(company_ids))]
        if company_ids
        else []
    )
    n_pm = (
        db.query(ProactiveMatch).filter(ProactiveMatch.company_id.in_(company_ids)).delete(synchronize_session=False)
        if company_ids
        else 0
    )
    n_po = (
        db.query(ProactiveOffer)
        .filter(ProactiveOffer.graph_message_id.like(f"{SEED_TAG}%"))
        .delete(synchronize_session=False)
    )
    if site_ids:
        db.query(ProactiveThrottle).filter(ProactiveThrottle.customer_site_id.in_(site_ids)).delete(
            synchronize_session=False
        )
    n_off = db.query(Offer).filter(Offer.source == SEED_TAG).delete(synchronize_session=False)
    n_cph = (
        db.query(CustomerPartHistory)
        .filter(CustomerPartHistory.company_id.in_(company_ids))
        .delete(synchronize_session=False)
        if company_ids
        else 0
    )
    if site_ids:
        db.query(SiteContact).filter(SiteContact.customer_site_id.in_(site_ids)).delete(synchronize_session=False)
    db.query(Requisition).filter(Requisition.name == f"AVAIL Vendor Stock — {SEED_TAG}").delete(
        synchronize_session=False
    )
    if site_ids:
        db.query(CustomerSite).filter(CustomerSite.id.in_(site_ids)).delete(synchronize_session=False)
    if company_ids:
        db.query(Company).filter(Company.id.in_(company_ids)).delete(synchronize_session=False)
    db.commit()
    logger.info(
        f"WIPED demo data: {n_pm} matches, {n_po} sent-offers, {n_off} offers, {n_cph} CPH, "
        f"{len(site_ids)} sites, {len(company_ids)} companies"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wipe", action="store_true", help="remove demo data and exit")
    ap.add_argument("--viewer", default=VIEWER_EMAIL, help="salesperson email who will view the tab")
    args = ap.parse_args()

    db = SessionLocal()
    viewer = db.query(User).filter(User.email == args.viewer).first()
    if not viewer:
        logger.error(f"viewer {args.viewer!r} not found — aborting")
        sys.exit(1)
    logger.info(f"viewer = {viewer.email} (id={viewer.id})")

    if args.wipe:
        wipe(db, viewer)
        return

    # 1. Cards + vendor cards
    cards = {k: get_or_create_card(db, k) for k in PARTS}
    vcards = {k: get_or_create_vendor_card(db, VENDOR_OF[k][0], VENDOR_OF[k][2]) for k in PARTS}

    # 2. Holding requisition for the vendor offers (FK container only)
    holding = db.query(Requisition).filter(Requisition.name == f"AVAIL Vendor Stock — {SEED_TAG}").first()
    if not holding:
        holding = Requisition(name=f"AVAIL Vendor Stock — {SEED_TAG}", status="archived", created_by=viewer.id)
        db.add(holding)
        db.flush()

    # 3. Companies + sites + contacts (owned by viewer)
    companies, sites = {}, {}
    for key, (name, site_name, city, state, contacts) in CUSTOMERS.items():
        co = db.query(Company).filter(Company.name == name).first()
        if not co:
            co = Company(name=name)
            db.add(co)
        co.is_active = True
        co.account_owner_id = viewer.id
        db.flush()
        companies[key] = co
        site = (
            db.query(CustomerSite).filter(CustomerSite.company_id == co.id, CustomerSite.site_name == site_name).first()
        )
        if not site:
            site = CustomerSite(company_id=co.id, site_name=site_name)
            db.add(site)
        site.is_active = True
        site.city, site.state, site.country = city, state, "USA"
        db.flush()
        sites[key] = site
        for full_name, title, email, primary in contacts:
            sc = (
                db.query(SiteContact)
                .filter(SiteContact.customer_site_id == site.id, SiteContact.email == email)
                .first()
            )
            if not sc:
                sc = SiteContact(customer_site_id=site.id, full_name=full_name, email=email)
                db.add(sc)
            sc.title, sc.is_primary, sc.is_active = title, primary, True
            db.flush()

    # 4. Purchase history (CPH)
    for ckey, rows in PURCHASES.items():
        co = companies[ckey]
        for pkey, count, days_ago, markup, last_qty, total_qty in rows:
            card = cards[pkey]
            cost = PARTS[pkey][3]
            avg = round(cost * markup, 2)
            cph = (
                db.query(CustomerPartHistory)
                .filter(
                    CustomerPartHistory.company_id == co.id,
                    CustomerPartHistory.material_card_id == card.id,
                    CustomerPartHistory.source == "demo_seed",
                )
                .first()
            )
            if not cph:
                cph = CustomerPartHistory(
                    company_id=co.id, material_card_id=card.id, mpn=PARTS[pkey][0], source="demo_seed"
                )
                db.add(cph)
            cph.purchase_count = count
            cph.last_purchased_at = NOW - timedelta(days=days_ago)
            cph.avg_unit_price = Decimal(str(avg))
            cph.last_unit_price = Decimal(str(avg))
            cph.last_quantity = last_qty
            cph.total_quantity = total_qty
            db.flush()

    # 5. Vendor offers (one per part) — refresh created_at each run
    offers = {}
    for pkey, (display, mfr, desc, cost, cond) in PARTS.items():
        vname, qty, _ = VENDOR_OF[pkey]
        off = db.query(Offer).filter(Offer.material_card_id == cards[pkey].id, Offer.source == SEED_TAG).first()
        if not off:
            off = Offer(requisition_id=holding.id, material_card_id=cards[pkey].id, source=SEED_TAG)
            db.add(off)
        off.vendor_name = vname
        off.vendor_card_id = vcards[pkey].id
        off.mpn = display
        off.manufacturer = mfr
        off.qty_available = qty
        off.unit_price = Decimal(str(cost))
        off.currency = "USD"
        off.condition = cond
        off.warranty = "90 days"
        off.lead_time = "In stock"
        off.country_of_origin = "US"
        off.status = "active"
        off.entered_by_id = viewer.id
        off.created_at = NOW
        db.flush()
        offers[pkey] = off

    # 6. Clear prior demo matches/throttle for these companies, then regenerate fresh
    company_ids = [c.id for c in companies.values()]
    site_ids = [s.id for s in sites.values()]
    db.query(ProactiveMatch).filter(ProactiveMatch.company_id.in_(company_ids)).delete(synchronize_session=False)
    db.query(ProactiveThrottle).filter(ProactiveThrottle.customer_site_id.in_(site_ids)).delete(
        synchronize_session=False
    )
    db.flush()
    total_matches = 0
    for pkey, off in offers.items():
        total_matches += len(find_matches_for_offer(off.id, db))
    db.commit()

    # 7. Sent / converted ProactiveOffers (Sent tab + Scorecard)
    db.query(ProactiveOffer).filter(ProactiveOffer.graph_message_id.like(f"{SEED_TAG}%")).delete(
        synchronize_session=False
    )
    db.flush()
    for i, (ckey, status, days_ago, lines) in enumerate(SENT_OFFERS):
        site = sites[ckey]
        contacts = db.query(SiteContact).filter(SiteContact.customer_site_id == site.id).all()
        emails = [c.email for c in contacts if c.email]
        line_items, total_sell, total_cost = [], 0.0, 0.0
        for pkey, qty, markup in lines:
            cost = PARTS[pkey][3]
            sell = round(cost * markup, 2)
            line_items.append(
                {"mpn": PARTS[pkey][0], "vendor_name": VENDOR_OF[pkey][0], "qty": qty, "cost": cost, "sell": sell}
            )
            total_sell += sell * qty
            total_cost += cost * qty
        po = ProactiveOffer(
            customer_site_id=site.id,
            salesperson_id=viewer.id,
            line_items=line_items,
            recipient_emails=emails,
            recipient_contact_ids=[c.id for c in contacts],
            subject=f"Parts Available — {companies[ckey].name}",
            status=status,
            total_sell=round(total_sell, 2),
            total_cost=round(total_cost, 2),
            sent_at=NOW - timedelta(days=days_ago),
            graph_message_id=f"{SEED_TAG}-{i}",
        )
        if status == "converted":
            po.converted_at = NOW - timedelta(days=days_ago - 1)
        db.add(po)
    db.commit()

    # 8. Summary
    n_new = (
        db.query(ProactiveMatch)
        .filter(ProactiveMatch.company_id.in_(company_ids), ProactiveMatch.status == "new")
        .count()
    )
    n_sent = (
        db.query(ProactiveOffer)
        .filter(ProactiveOffer.salesperson_id == viewer.id, ProactiveOffer.graph_message_id.like(f"{SEED_TAG}%"))
        .count()
    )
    n_conv = (
        db.query(ProactiveOffer)
        .filter(
            ProactiveOffer.salesperson_id == viewer.id,
            ProactiveOffer.graph_message_id.like(f"{SEED_TAG}%"),
            ProactiveOffer.status == "converted",
        )
        .count()
    )
    logger.info("─" * 60)
    logger.info(f"DONE. {len(companies)} customers, {len(PARTS)} parts, {total_matches} matches generated")
    logger.info(f"  Matches tab (status=new): {n_new}")
    logger.info(f"  Sent tab: {n_sent} offers ({n_conv} converted)")
    logger.info(
        f"  Scorecard: sent={n_sent} converted={n_conv} conv_rate={round(n_conv / n_sent * 100, 1) if n_sent else 0}%"
    )
    logger.info(f"  All visible to: {viewer.email} (id={viewer.id})")
    db.close()


if __name__ == "__main__":
    main()
