"""Load test script — creates realistic transactions across the full workflow.

Creates requisitions, requirements, sightings, offers, quotes, buy plans,
and proactive offers for all salespeople. Runs in a loop for ~20 minutes.

NO EMAILS SENT. All records created directly in DB.

Usage: docker compose exec app python scripts/load_test.py
"""

import json
import os
import random
import secrets
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

# Must set before any app imports
os.environ.setdefault("TESTING", "1")

from app.database import SessionLocal

# ── Real-ish electronic component part numbers ────────────────────────
PARTS = [
    ("LM317T", "Texas Instruments", ["LM317DCYR", "LM317LZ"]),
    ("STM32F103C8T6", "STMicroelectronics", ["STM32F103CBT6", "STM32F103RBT6"]),
    ("ESP32-WROOM-32", "Espressif", ["ESP32-WROOM-32D", "ESP32-WROOM-32E"]),
    ("ATMEGA328P-PU", "Microchip", ["ATMEGA328P-AU", "ATMEGA168PA-PU"]),
    ("NE555P", "Texas Instruments", ["NE555DR", "LM555CN"]),
    ("IRF540N", "Infineon", ["IRF540NPBF", "IRFZ44N"]),
    ("SN74HC595N", "Texas Instruments", ["SN74HC595DR", "SN74HC165N"]),
    ("TL431ACLP", "Texas Instruments", ["TL431BIDR", "LM431ACZ"]),
    ("MAX232CPE", "Maxim", ["MAX232ECPE", "MAX3232ECPE"]),
    ("LM7805CT", "Texas Instruments", ["LM7805ACT", "LM7812CT"]),
    ("AD8605ARTZ", "Analog Devices", ["AD8605ACBZ", "AD8606ARMZ"]),
    ("TPS54331DR", "Texas Instruments", ["TPS54331DDAR", "TPS5430DDAR"]),
    ("LTC3780EGN", "Analog Devices", ["LTC3780EUHF", "LTC3786EMS"]),
    ("OPA2134PA", "Texas Instruments", ["OPA2134UA", "OPA1612AIDR"]),
    ("MCP2515-I/ST", "Microchip", ["MCP2515-I/SO", "MCP2515T-I/ST"]),
    ("ADS1115IDGSR", "Texas Instruments", ["ADS1115IDGST", "ADS1015IDGSR"]),
    ("BQ24195RGER", "Texas Instruments", ["BQ24196RGER", "BQ25895RTWR"]),
    ("FT232RL", "FTDI", ["FT232RQ", "FT2232HL"]),
    ("CP2102N-A02-GQFN28", "Silicon Labs", ["CP2102-GMR", "CP2104-F03-GMR"]),
    ("TPS62130RGTR", "Texas Instruments", ["TPS62130ARGTR", "TPS62140RGTR"]),
    ("LP5907MFX-3.3", "Texas Instruments", ["LP5907MFX-1.8", "LP5907MFX-5.0"]),
    ("SHT31-DIS-B", "Sensirion", ["SHT30-DIS-B", "SHT40-AD1B-R2"]),
    ("BME280", "Bosch", ["BMP280", "BME680"]),
    ("INA219BIDR", "Texas Instruments", ["INA219AIDR", "INA226AIDGSR"]),
    ("TCA9548APWR", "Texas Instruments", ["TCA9548ARGER", "PCA9548APW"]),
    ("W25Q128JVSIQ", "Winbond", ["W25Q128JVSIM", "W25Q64JVSIQ"]),
    ("IS31FL3731-QFLS2-TR", "ISSI", ["IS31FL3731-SALS2-TR", "IS31FL3733-TQLS3-TR"]),
    ("DRV8825PWPR", "Texas Instruments", ["DRV8825PWP", "A4988SETTR-T"]),
    ("TUSB320HAIRWBR", "Texas Instruments", ["TUSB320LAIZXR", "TUSB322IRGBR"]),
    ("XC6206P332MR", "Torex", ["XC6206P182MR", "XC6206P502MR"]),
    ("RT9013-33GB", "Richtek", ["RT9013-18GB", "RT9013-12GB"]),
    ("TXB0108PWR", "Texas Instruments", ["TXB0104PWR", "SN74LVC8T245PWR"]),
    ("SI5351A-B-GT", "Silicon Labs", ["SI5351A-B-GTR", "SI5351C-B-GM"]),
    ("MAX17048G+T10", "Maxim", ["MAX17048G+T", "MAX17055ETB+T"]),
    ("STUSB4500QTR", "STMicroelectronics", ["STUSB4500LQTR", "FUSB302BMPX"]),
]

VENDORS = [
    "Arrow Electronics",
    "Digi-Key",
    "Mouser Electronics",
    "Newark",
    "Farnell",
    "Future Electronics",
    "TTI Inc",
    "Avnet",
    "RS Components",
    "Rochester Electronics",
    "Chip1Stop",
    "LCSC",
    "TME",
    "Heilind Electronics",
    "Master Electronics",
    "Smith & Associates",
    "Fusion Worldwide",
    "Sourcengine",
    "Bisco Industries",
    "Symmetry Electronics",
]

CONDITIONS = ["new", "new", "new", "refurb", "used"]
REQ_CONDITIONS = ["new", "new", "refurb", "used", None]  # DB check constraint
PACKAGINGS = ["reel", "tube", "tray", "bag", "cut_tape", "each"]
REQ_PACKAGINGS = ["reel", "tube", "tray", None]  # subset that's valid for reqs
SOURCES = ["nexar", "brokerbin", "digikey", "mouser", "oemsecrets", "sourcengine"]
QUOTE_RESULTS = ["won", "won", "won", "lost", "lost", "lost", "lost"]
BP_STATUSES = ["pending_approval", "approved", "po_entered", "po_confirmed", "complete", "rejected", "cancelled"]

NOTES = """
Workflow notes will be collected here during the load test.
Check the load_test_notes.md file after the run.
""".strip()

# ── Globals ────────────────────────────────────────────────────────────
SALES_USERS = []
CUSTOMER_SITES = []
SITE_CONTACTS = {}
NEXT_QN = 0
WORKFLOW_NOTES = []


def note(msg):
    """Record a workflow observation."""
    WORKFLOW_NOTES.append(f"- {msg}")
    print(f"  NOTE: {msg}")


def init_globals(db):
    """Load reference data from DB."""
    global SALES_USERS, CUSTOMER_SITES, SITE_CONTACTS, NEXT_QN

    SALES_USERS = db.execute(
        text(
            "SELECT id, name, role FROM users "
            "WHERE role IN ('sales','manager') AND is_active=true "
            "AND email NOT LIKE '%%@availai.local'"
        )
    ).fetchall()
    print(f"Sales users: {[r[1] for r in SALES_USERS]}")

    CUSTOMER_SITES = db.execute(
        text(
            "SELECT cs.id, cs.site_name, c.name "
            "FROM customer_sites cs JOIN companies c ON c.id=cs.company_id "
            "ORDER BY RANDOM() LIMIT 50"
        )
    ).fetchall()
    print(f"Customer sites loaded: {len(CUSTOMER_SITES)}")

    # Load contacts per site
    for site in CUSTOMER_SITES:
        contacts = db.execute(
            text(
                "SELECT id, full_name, email FROM site_contacts "
                "WHERE customer_site_id=:sid AND email IS NOT NULL LIMIT 3"
            ),
            {"sid": site[0]},
        ).fetchall()
        if contacts:
            SITE_CONTACTS[site[0]] = contacts

    # Next quote number
    r = db.execute(
        text("SELECT COALESCE(MAX(CAST(quote_number AS INTEGER)),95000) FROM quotes WHERE quote_number ~ '^[0-9]+$'")
    ).scalar()
    NEXT_QN = r + 1
    print(f"Starting quote number: {NEXT_QN}")


def next_quote_number():
    global NEXT_QN
    qn = NEXT_QN
    NEXT_QN += 1
    return str(qn)


def random_date(days_back=25):
    """Random datetime within this month."""
    now = datetime.now(timezone.utc)
    delta = random.randint(0, days_back * 24 * 60)
    return now - timedelta(minutes=delta)


def run_cycle(db, cycle_num):
    """Run one full cycle: create reqs, search, offer, quote, process, proactive."""
    now = datetime.now(timezone.utc)
    print(f"\n{'=' * 60}")
    print(f"  CYCLE {cycle_num} — {now.strftime('%H:%M:%S')}")
    print(f"{'=' * 60}")

    cycle_reqs = []
    cycle_offers = []
    cycle_quotes = []

    for user_id, user_name, user_role in SALES_USERS:
        try:
            site = random.choice(CUSTOMER_SITES)
            site_id = site[0]

            # ── 1. Create Requisition ──────────────────────────────────
            parts_batch = random.sample(PARTS, min(25, len(PARTS)))
            req_name = f"LT-{cycle_num}-{user_name.split()[0]}-{random.randint(1000, 9999)}"

            result = db.execute(
                text(
                    "INSERT INTO requisitions (name, customer_site_id, customer_name, status, created_by, created_at, updated_at) "
                    "VALUES (:name, :site, :cname, 'active', :uid, :now, :now) RETURNING id"
                ),
                {"name": req_name, "site": site_id, "cname": site[2], "uid": user_id, "now": now},
            )
            req_id = result.scalar()
            db.commit()
            print(f"  [{user_name}] Req #{req_id} '{req_name}' — {len(parts_batch)} parts for {site[2]}")

            # ── 2. Add Requirements ───────────────────────────────────
            req_items = []
            for mpn, mfg, subs in parts_batch:
                target_qty = random.choice([100, 250, 500, 1000, 2500, 5000, 10000])
                target_price = round(random.uniform(0.10, 150.00), 4)
                sub_json = json.dumps(subs[:2]) if subs else "[]"

                r = db.execute(
                    text(
                        "INSERT INTO requirements "
                        "(requisition_id, primary_mpn, normalized_mpn, brand, target_qty, target_price, "
                        " substitutes, condition, packaging, notes, created_at) "
                        "VALUES (:rid, :mpn, :nmpn, :brand, :qty, :price, :subs, :cond, :pkg, :notes, :now) "
                        "RETURNING id"
                    ),
                    {
                        "rid": req_id,
                        "mpn": mpn,
                        "nmpn": mpn.upper().replace(" ", ""),
                        "brand": mfg,
                        "qty": target_qty,
                        "price": target_price,
                        "subs": sub_json,
                        "cond": random.choice(REQ_CONDITIONS),
                        "pkg": random.choice(REQ_PACKAGINGS),
                        "notes": f"Load test cycle {cycle_num}" if random.random() > 0.7 else None,
                        "now": now,
                    },
                )
                req_items.append((r.scalar(), mpn, mfg, target_qty, target_price))
            db.commit()

            # ── 3. Create Sightings (simulated search results) ────────
            sighting_count = 0
            for item_id, mpn, mfg, qty, price in req_items:
                num_sightings = random.randint(2, 8)
                for _ in range(num_sightings):
                    vendor = random.choice(VENDORS)
                    s_qty = random.randint(50, qty * 3)
                    s_price = round(price * random.uniform(0.7, 1.5), 4)
                    score = round(random.uniform(20, 100), 1)

                    db.execute(
                        text(
                            "INSERT INTO sightings "
                            "(requirement_id, vendor_name, vendor_name_normalized, mpn_matched, normalized_mpn, "
                            " manufacturer, qty_available, unit_price, currency, moq, "
                            " source_type, is_authorized, confidence, score, "
                            " condition, packaging, date_code, lead_time, lead_time_days, created_at) "
                            "VALUES (:rid, :vn, :vnn, :mpn, :nmpn, :mfg, :qty, :price, 'USD', :moq, "
                            " :src, :auth, :conf, :score, :cond, :pkg, :dc, :lt, :ltd, :now)"
                        ),
                        {
                            "rid": item_id,
                            "vn": vendor,
                            "vnn": vendor.lower(),
                            "mpn": mpn,
                            "nmpn": mpn.upper().replace(" ", ""),
                            "mfg": mfg,
                            "qty": s_qty,
                            "price": s_price,
                            "moq": random.choice([1, 10, 50, 100, None]),
                            "src": random.choice(SOURCES),
                            "auth": random.random() > 0.6,
                            "conf": round(random.uniform(0.5, 1.0), 2),
                            "score": score,
                            "cond": random.choice(CONDITIONS),
                            "pkg": random.choice(PACKAGINGS),
                            "dc": f"20{random.randint(22, 25)}{random.randint(1, 52):02d}",
                            "lt": f"{random.randint(1, 12)} weeks",
                            "ltd": random.randint(7, 84),
                            "now": now,
                        },
                    )
                    sighting_count += 1
            db.commit()

            # Update req status to sourcing
            db.execute(
                text("UPDATE requisitions SET status='sourcing', last_searched_at=:now WHERE id=:id"),
                {"id": req_id, "now": now},
            )
            db.commit()
            print(f"    Sourced: {sighting_count} sightings across {len(req_items)} requirements")

            # ── 4. Log Offers (pick best sightings) ───────────────────
            offers_created = []
            for item_id, mpn, mfg, qty, price in req_items:
                # Pick 1-3 vendors to make offers from
                num_offers = random.randint(1, 3)
                vendors_used = random.sample(VENDORS, num_offers)
                for vendor in vendors_used:
                    o_qty = random.randint(qty // 2, qty * 2)
                    o_price = round(price * random.uniform(0.6, 1.3), 4)

                    r = db.execute(
                        text(
                            "INSERT INTO offers "
                            "(requisition_id, requirement_id, vendor_card_id, vendor_name, vendor_name_normalized, "
                            " entered_by_id, mpn, normalized_mpn, manufacturer, "
                            " qty_available, unit_price, currency, lead_time, date_code, "
                            " condition, packaging, source, status, created_at, updated_at) "
                            "VALUES (:rid, :iid, 5165, :vn, :vnn, :uid, :mpn, :nmpn, :mfg, "
                            " :qty, :price, 'USD', :lt, :dc, :cond, :pkg, 'manual', 'active', :now, :now) "
                            "RETURNING id"
                        ),
                        {
                            "rid": req_id,
                            "iid": item_id,
                            "vn": vendor,
                            "vnn": vendor.lower(),
                            "uid": user_id,
                            "mpn": mpn,
                            "nmpn": mpn.upper().replace(" ", ""),
                            "mfg": mfg,
                            "qty": o_qty,
                            "price": o_price,
                            "lt": f"{random.randint(1, 8)} weeks",
                            "dc": f"20{random.randint(23, 26)}{random.randint(1, 52):02d}",
                            "cond": random.choice(CONDITIONS[:3]),
                            "pkg": random.choice(PACKAGINGS[:4]),
                            "now": now,
                        },
                    )
                    offer_id = r.scalar()
                    offers_created.append((offer_id, mpn, vendor, o_qty, o_price, item_id))

            # Update req status to offers
            db.execute(text("UPDATE requisitions SET status='offers' WHERE id=:id"), {"id": req_id})
            db.commit()
            print(f"    Offers: {len(offers_created)} logged")
            cycle_offers.extend(offers_created)

            # ── 5. Create Quote from subset of offers ─────────────────
            # Pick a subset of offers for the quote
            quote_offers = random.sample(offers_created, min(random.randint(5, 15), len(offers_created)))
            line_items = []
            subtotal = 0
            total_cost = 0
            for oid, mpn, vendor, qty, cost_price, item_id in quote_offers:
                sell_price = round(cost_price * random.uniform(1.10, 1.45), 4)
                margin = round((sell_price - cost_price) / sell_price * 100, 2) if sell_price else 0
                line_items.append(
                    {
                        "offer_id": oid,
                        "mpn": mpn,
                        "vendor_name": vendor,
                        "qty": qty,
                        "cost_price": cost_price,
                        "sell_price": sell_price,
                        "margin_pct": margin,
                        "manufacturer": mfg,
                    }
                )
                subtotal += qty * sell_price
                total_cost += qty * cost_price

            margin_pct = round((subtotal - total_cost) / subtotal * 100, 2) if subtotal else 0
            qn = next_quote_number()

            r = db.execute(
                text(
                    "INSERT INTO quotes "
                    "(requisition_id, customer_site_id, quote_number, revision, "
                    " line_items, subtotal, total_cost, total_margin_pct, "
                    " payment_terms, shipping_terms, validity_days, "
                    " status, created_by_id, created_at, updated_at) "
                    "VALUES (:rid, :sid, :qn, 1, :items, :sub, :cost, :margin, "
                    " :pay, :ship, :valid, 'draft', :uid, :now, :now) RETURNING id"
                ),
                {
                    "rid": req_id,
                    "sid": site_id,
                    "qn": qn,
                    "items": json.dumps(line_items),
                    "sub": round(subtotal, 2),
                    "cost": round(total_cost, 2),
                    "margin": margin_pct,
                    "pay": random.choice(["Net 30", "Net 45", "Net 60", "CIA", "COD"]),
                    "ship": random.choice(["FOB Origin", "FOB Destination", "DDP", "EXW"]),
                    "valid": random.choice([7, 14, 30]),
                    "uid": user_id,
                    "now": now,
                },
            )
            quote_id = r.scalar()

            # Mark quote as sent (skip email)
            db.execute(text("UPDATE quotes SET status='sent', sent_at=:now WHERE id=:id"), {"id": quote_id, "now": now})

            # Update req status
            db.execute(text("UPDATE requisitions SET status='quoted' WHERE id=:id"), {"id": req_id})
            db.commit()
            print(f"    Quote #{qn} — {len(quote_offers)} lines, ${subtotal:,.2f} subtotal, {margin_pct:.1f}% margin")
            cycle_quotes.append((quote_id, qn, req_id, site_id, user_id, line_items, subtotal, total_cost))
            cycle_reqs.append((req_id, user_id, site_id, offers_created))

        except Exception as e:
            db.rollback()
            note(f"User {user_name} cycle {cycle_num} error: {type(e).__name__}: {str(e)[:200]}")
            print(f"    ERROR for {user_name}: {e}")
            continue

    # ── 6. Process Quote Results (mix of wins/losses) ──────────
    if not cycle_quotes:
        print("  No quotes to process this cycle")
        return {"reqs": 0, "offers": 0, "quotes": 0, "won": 0, "proactive": 0}

    print(f"\n  Processing {len(cycle_quotes)} quote results...")
    won_quotes = []
    for quote_id, qn, req_id, site_id, uid, items, subtotal, total_cost in cycle_quotes:
        result = random.choice(QUOTE_RESULTS)
        reason = (
            random.choice(
                [
                    "Price competitive",
                    "Good lead time",
                    "Preferred vendor",
                    "Customer urgent need",
                    "Repeat order",
                ]
            )
            if result == "won"
            else random.choice(
                [
                    "Price too high",
                    "Lead time too long",
                    "Found alternate source",
                    "Customer cancelled project",
                    "Budget cut",
                    "Went with competitor",
                ]
            )
        )

        db.execute(
            text(
                "UPDATE quotes SET status=:st, result=:res, result_reason=:reason, "
                "result_at=:now, won_revenue=:rev WHERE id=:id"
            ),
            {
                "id": quote_id,
                "st": result,
                "res": result,
                "reason": reason,
                "now": now,
                "rev": round(subtotal, 2) if result == "won" else None,
            },
        )

        db.execute(text("UPDATE requisitions SET status=:st WHERE id=:id"), {"id": req_id, "st": result})

        if result == "won":
            won_quotes.append((quote_id, qn, req_id, site_id, uid, items, subtotal, total_cost))

    db.commit()
    won_count = len(won_quotes)
    lost_count = len(cycle_quotes) - won_count
    print(f"    Won: {won_count}  Lost: {lost_count}")

    # ── 7. Create Buy Plans from won quotes ────────────────────
    po_confirmed_plans = []
    for quote_id, qn, req_id, site_id, uid, items, subtotal, total_cost in won_quotes:
        token = secrets.token_urlsafe(24)
        bp_items = []
        for item in items:
            bp_items.append(
                {
                    **item,
                    "po_number": None,
                    "po_entered_at": None,
                    "po_sent_at": None,
                    "po_verified": False,
                    "entered_by_id": uid,
                }
            )

        # Random final status for the buy plan
        final_status = random.choice(BP_STATUSES)

        r = db.execute(
            text(
                "INSERT INTO buy_plans "
                "(requisition_id, quote_id, status, line_items, "
                " submitted_by_id, submitted_at, approval_token, token_expires_at, created_at) "
                "VALUES (:rid, :qid, 'pending_approval', :items, :uid, :now, :tok, :exp, :now) "
                "RETURNING id"
            ),
            {
                "rid": req_id,
                "qid": quote_id,
                "items": json.dumps(bp_items),
                "uid": uid,
                "now": now,
                "tok": token,
                "exp": now + timedelta(days=30),
            },
        )
        bp_id = r.scalar()

        # Advance through workflow based on final_status
        if final_status in ("approved", "po_entered", "po_confirmed", "complete"):
            db.execute(
                text("UPDATE buy_plans SET status='approved', approved_by_id=1, approved_at=:now WHERE id=:id"),
                {"id": bp_id, "now": now},
            )

        if final_status in ("po_entered", "po_confirmed", "complete"):
            # Add PO numbers to line items
            for i, item in enumerate(bp_items):
                item["po_number"] = f"PO-LT-{cycle_num}-{bp_id}-{i}"
                item["po_entered_at"] = now.isoformat()
            db.execute(
                text("UPDATE buy_plans SET status='po_entered', line_items=:items WHERE id=:id"),
                {"id": bp_id, "items": json.dumps(bp_items)},
            )

        if final_status in ("po_confirmed", "complete"):
            db.execute(text("UPDATE buy_plans SET status='po_confirmed' WHERE id=:id"), {"id": bp_id})
            po_confirmed_plans.append(bp_id)

        if final_status == "complete":
            db.execute(
                text("UPDATE buy_plans SET status='complete', completed_at=:now, completed_by_id=:uid WHERE id=:id"),
                {"id": bp_id, "uid": uid, "now": now},
            )

        if final_status == "rejected":
            db.execute(
                text("UPDATE buy_plans SET status='rejected', rejection_reason='Test rejection' WHERE id=:id"),
                {"id": bp_id},
            )

        if final_status == "cancelled":
            db.execute(
                text(
                    "UPDATE buy_plans SET status='cancelled', cancellation_reason='Test cancel', "
                    "cancelled_at=:now, cancelled_by_id=:uid WHERE id=:id"
                ),
                {"id": bp_id, "uid": uid, "now": now},
            )

    db.commit()
    print(f"    Buy plans: {len(won_quotes)} created, {len(po_confirmed_plans)} PO confirmed")

    # ── 8. Proactive Offers ────────────────────────────────────
    proactive_count = 0
    for user_id, user_name, user_role in SALES_USERS:
        for _ in range(5):
            site = random.choice(CUSTOMER_SITES)
            site_id = site[0]

            # Pick random offers to include
            if not cycle_offers:
                continue
            match_offers = random.sample(cycle_offers, min(3, len(cycle_offers)))
            pro_items = []
            total_sell = 0
            total_cost = 0
            for oid, mpn, vendor, qty, cost, _ in match_offers:
                sell = round(cost * random.uniform(1.15, 1.50), 4)
                pro_items.append(
                    {
                        "offer_id": oid,
                        "mpn": mpn,
                        "vendor_name": vendor,
                        "qty": qty,
                        "cost_price": cost,
                        "sell_price": sell,
                    }
                )
                total_sell += qty * sell
                total_cost += qty * cost

            # Simulate customer response
            response = random.choice(["sent", "sent", "sent", "converted", "converted"])

            contacts = SITE_CONTACTS.get(site_id, [])
            recipient_emails = json.dumps([c[2] for c in contacts[:2]]) if contacts else "[]"
            recipient_ids = json.dumps([c[0] for c in contacts[:2]]) if contacts else "[]"

            r = db.execute(
                text(
                    "INSERT INTO proactive_offers "
                    "(customer_site_id, salesperson_id, line_items, "
                    " recipient_contact_ids, recipient_emails, "
                    " subject, email_body_html, status, sent_at, "
                    " converted_at, total_sell, total_cost, created_at) "
                    "VALUES (:sid, :uid, :items, :cids, :emails, :subj, :html, :status, :sent, "
                    " :conv_at, :sell, :cost, :now) RETURNING id"
                ),
                {
                    "sid": site_id,
                    "uid": user_id,
                    "items": json.dumps(pro_items),
                    "cids": recipient_ids,
                    "emails": recipient_emails,
                    "subj": f"Parts Available — {match_offers[0][1]} and more",
                    "html": f"<p>Hi, we have {len(pro_items)} parts available.</p>",
                    "status": response,
                    "sent": now,
                    "conv_at": now if response == "converted" else None,
                    "sell": round(total_sell, 2),
                    "cost": round(total_cost, 2),
                    "now": now,
                },
            )
            proactive_count += 1

    db.commit()
    print(f"    Proactive offers: {proactive_count} created")

    # ── Cycle summary ──────────────────────────────────────────
    total_reqs = len(SALES_USERS)
    total_items = total_reqs * 25
    total_sightings = sum(random.randint(2, 8) for _ in range(total_items))  # approx
    print(
        f"\n  Cycle {cycle_num} complete: {total_reqs} reqs, ~{total_items} items, "
        f"{len(cycle_offers)} offers, {len(cycle_quotes)} quotes, "
        f"{won_count} won, {proactive_count} proactive"
    )

    return {
        "reqs": total_reqs,
        "offers": len(cycle_offers),
        "quotes": len(cycle_quotes),
        "won": won_count,
        "proactive": proactive_count,
    }


def main():
    print("=" * 60)
    print("  AVAIL AI LOAD TEST — 20 MINUTE RUN")
    print("  NO EMAILS SENT — DB records only")
    print("=" * 60)

    db = SessionLocal()
    init_globals(db)

    start_time = time.time()
    duration = 20 * 60  # 20 minutes
    cycle = 0
    totals = {"reqs": 0, "offers": 0, "quotes": 0, "won": 0, "proactive": 0}

    try:
        while time.time() - start_time < duration:
            cycle += 1
            elapsed = time.time() - start_time
            remaining = duration - elapsed
            print(f"\n  Time: {elapsed / 60:.1f}min elapsed, {remaining / 60:.1f}min remaining")

            result = run_cycle(db, cycle)
            for k in totals:
                totals[k] += result[k]

            # Brief pause between cycles to let the server breathe
            time.sleep(2)

    except KeyboardInterrupt:
        print("\n\n  Interrupted by user")
    except Exception as e:
        print(f"\n\n  ERROR in main loop: {e}")
        note(f"CRASH in main loop cycle {cycle}: {e}")
        import traceback

        traceback.print_exc()
    finally:
        elapsed = time.time() - start_time
        print(f"\n{'=' * 60}")
        print(f"  LOAD TEST COMPLETE — {elapsed / 60:.1f} minutes, {cycle} cycles")
        print(f"{'=' * 60}")
        print(f"  Requisitions:     {totals['reqs']}")
        print(f"  Offers:           {totals['offers']}")
        print(f"  Quotes:           {totals['quotes']}")
        print(f"  Won:              {totals['won']}")
        print(f"  Proactive:        {totals['proactive']}")
        print(f"{'=' * 60}")

        # Recompute multiplier scores with new data
        print("\n  Recomputing multiplier scores with new data...")
        try:
            from datetime import date

            from app.services.multiplier_score_service import compute_all_multiplier_scores

            result = compute_all_multiplier_scores(db, date.today().replace(day=1))
            print(f"  Multiplier scores: {result}")
        except Exception as e:
            print(f"  Multiplier recompute failed: {e}")

        # Save workflow notes
        if WORKFLOW_NOTES:
            print("\n  WORKFLOW NOTES:")
            for n in WORKFLOW_NOTES:
                print(f"    {n}")

        db.close()


if __name__ == "__main__":
    main()
