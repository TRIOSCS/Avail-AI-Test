"""
Populate demo sightings, offers, quotes, activity, and tasks for a test requisition.
Simulates a full sourcing workflow so the new UI can be demoed.
Run: PYTHONPATH=/root/availai python scripts/simulate_demo_data.py --req-id 23433
Depends on: app.models, app.database
Called by: manual execution for demo purposes
"""

import argparse
import os
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger
from sqlalchemy import text

from app.database import SessionLocal

VENDORS = [
    {"name": "Arrow Electronics", "norm": "arrow electronics", "cond": "new", "lead": "2-3 weeks", "auth": True},
    {"name": "Digi-Key", "norm": "digi-key", "cond": "new", "lead": "In Stock", "auth": True},
    {"name": "Mouser Electronics", "norm": "mouser electronics", "cond": "new", "lead": "4 weeks", "auth": True},
    {
        "name": "Rochester Electronics",
        "norm": "rochester electronics",
        "cond": "new",
        "lead": "6-8 weeks",
        "auth": True,
    },
    {
        "name": "Smith & Associates",
        "norm": "smith & associates",
        "cond": "refurbished",
        "lead": "1 week",
        "auth": False,
    },
    {"name": "Win Source", "norm": "win source", "cond": "new", "lead": "3-5 days", "auth": False},
    {"name": "Chip One Stop", "norm": "chip one stop", "cond": "new", "lead": "2 weeks", "auth": False},
    {"name": "TTI Inc", "norm": "tti inc", "cond": "new", "lead": "In Stock", "auth": True},
]

SOURCES = ["nexar", "brokerbin", "digikey", "mouser", "oemsecrets", "email_mining"]
PACKAGINGS = ["reel", "tube", "tray", "bulk", "cut_tape", "bag"]
MFRS = ["Texas Instruments", "STMicroelectronics", "ON Semiconductor", "Infineon", "NXP", "Analog Devices"]


def simulate(req_id: int):
    db = SessionLocal()
    try:
        # Get requirements
        reqs = db.execute(
            text("SELECT id, primary_mpn, target_qty, target_price FROM requirements WHERE requisition_id = :rid"),
            {"rid": req_id},
        ).fetchall()
        if not reqs:
            logger.error(f"No requirements found for requisition {req_id}")
            return

        logger.info(f"Found {len(reqs)} requirements for requisition {req_id}")

        # Get user and customer_site
        user = db.execute(text("SELECT id, display_name FROM users LIMIT 1")).fetchone()
        if not user:
            logger.error("No users found")
            return
        user_id, user_name = user[0], user[1] or "Demo User"

        site_row = db.execute(
            text("SELECT customer_site_id FROM requisitions WHERE id = :rid"), {"rid": req_id}
        ).fetchone()
        customer_site_id = site_row[0] if site_row and site_row[0] else None

        now = datetime.now(timezone.utc)

        # ── 1. SIGHTINGS (8-15 per requirement — raw API search results) ──
        sighting_count = 0
        for req in reqs:
            r_id, mpn, qty, price = req
            if not mpn:
                continue
            base_price = float(price) if price else 5.00
            base_qty = int(qty) if qty else 100
            n_sightings = random.randint(8, 15)

            for i in range(n_sightings):
                v = random.choice(VENDORS)
                s_price = round(base_price * random.uniform(0.6, 2.0), 4)
                s_qty = int(base_qty * random.uniform(0.2, 5.0))
                score = round(random.uniform(20, 95), 1)
                confidence = round(random.uniform(0.5, 1.0), 2)

                db.execute(
                    text("""
                        INSERT INTO sightings (
                            requirement_id, vendor_name, vendor_name_normalized, mpn_matched,
                            normalized_mpn, manufacturer, qty_available, unit_price, currency,
                            moq, source_type, is_authorized, confidence, score,
                            date_code, packaging, condition, lead_time, lead_time_days,
                            created_at
                        ) VALUES (
                            :r_id, :vendor, :vnorm, :mpn,
                            :nmpn, :mfr, :qty, :price, 'USD',
                            :moq, :source, :auth, :conf, :score,
                            :dc, :pkg, :cond, :lead, :lead_days,
                            :created
                        )
                    """),
                    {
                        "r_id": r_id,
                        "vendor": v["name"],
                        "vnorm": v["norm"],
                        "mpn": mpn,
                        "nmpn": mpn.upper().replace("-", "").replace(" ", ""),
                        "mfr": random.choice(MFRS),
                        "qty": max(1, s_qty),
                        "price": s_price,
                        "moq": random.choice([1, 10, 50, 100, 500, None]),
                        "source": random.choice(SOURCES),
                        "auth": v["auth"],
                        "conf": confidence,
                        "score": score,
                        "dc": random.choice(["2024+", "2025+", "2023", "2024", None]),
                        "pkg": random.choice(PACKAGINGS),
                        "cond": v["cond"],
                        "lead": v["lead"],
                        "lead_days": random.choice([0, 3, 7, 14, 21, 30, 45]),
                        "created": now - timedelta(hours=random.randint(1, 72)),
                    },
                )
                sighting_count += 1

        logger.info(f"Created {sighting_count} sightings")

        # ── 2. OFFERS (3-5 per requirement — curated from best sightings) ──
        offer_count = 0
        offer_ids = {}  # mpn -> list of offer IDs for quote building
        for req in reqs:
            r_id, mpn, qty, price = req
            if not mpn:
                continue
            base_price = float(price) if price else 5.00
            base_qty = int(qty) if qty else 100
            offer_ids[mpn] = []

            for i, v in enumerate(VENDORS[: 3 + (r_id % 3)]):
                multiplier = [0.85, 1.0, 1.15, 1.30, 0.95, 0.90, 1.10, 1.05][i]
                o_price = round(base_price * multiplier, 4)
                o_qty = int(base_qty * [1.5, 1.0, 2.0, 0.5, 3.0, 1.2, 0.8, 2.5][i])
                status = "active" if i < 3 else "pending_review"

                db.execute(
                    text("""
                        INSERT INTO offers (
                            requisition_id, requirement_id, vendor_name, mpn, offered_mpn,
                            manufacturer, qty_available, unit_price, lead_time, condition,
                            packaging, source, status, notes, entered_by, created_at, updated_at
                        ) VALUES (
                            :req_id, :r_id, :vendor, :mpn, :offered_mpn,
                            :mfr, :qty, :price, :lead, :cond,
                            :pkg, :source, :status, :notes, :entered_by, :created, :updated
                        ) RETURNING id
                    """),
                    {
                        "req_id": req_id,
                        "r_id": r_id,
                        "vendor": v["name"],
                        "mpn": mpn,
                        "offered_mpn": mpn,
                        "mfr": random.choice(MFRS),
                        "qty": max(1, o_qty),
                        "price": o_price,
                        "lead": v["lead"],
                        "cond": v["cond"],
                        "pkg": random.choice(PACKAGINGS),
                        "source": [
                            "rfq_response",
                            "manual",
                            "email_mining",
                            "api_search",
                            "manual",
                            "rfq_response",
                            "manual",
                            "api_search",
                        ][i],
                        "status": status,
                        "notes": [
                            "Best price, confirmed stock",
                            "Standard distributor pricing",
                            "MOQ applies, volume discount available",
                            "Long lead — factory order only",
                            "Broker stock, inspect before use",
                            "Competitive quote received",
                            "Authorized distributor, reliable",
                            "Quick ship available",
                        ][i],
                        "entered_by": user_name,
                        "created": now - timedelta(days=random.randint(0, 5)),
                        "updated": now - timedelta(hours=random.randint(0, 48)),
                    },
                )
                # Get the inserted offer ID
                last_id = db.execute(text("SELECT lastval()")).scalar()
                offer_ids[mpn].append(last_id)
                offer_count += 1

        logger.info(f"Created {offer_count} offers")

        # ── 3. QUOTES (2 quotes — one draft, one sent) ──
        if customer_site_id:
            quote_count = 0
            for qi, (q_status, q_label) in enumerate([("draft", "Draft"), ("sent", "Sent to customer")]):
                q_num = f"Q-{req_id}-{qi + 1:02d}"
                # Check if quote_number exists
                exists = db.execute(text("SELECT 1 FROM quotes WHERE quote_number = :qn"), {"qn": q_num}).fetchone()
                if exists:
                    q_num = f"Q-{req_id}-{qi + 10:02d}"

                line_items_json = []
                total_cost = 0
                total_sell = 0
                for req in reqs:
                    r_id, mpn, qty, price = req
                    if not mpn or mpn not in offer_ids or not offer_ids[mpn]:
                        continue
                    base_price = float(price) if price else 5.00
                    cost = round(base_price * random.uniform(0.85, 1.1), 4)
                    sell = round(cost * random.uniform(1.15, 1.35), 4)
                    line_qty = int(qty) if qty else 100
                    line_items_json.append(
                        {
                            "mpn": mpn,
                            "manufacturer": random.choice(MFRS),
                            "qty": line_qty,
                            "cost_price": cost,
                            "sell_price": sell,
                            "currency": "USD",
                        }
                    )
                    total_cost += cost * line_qty
                    total_sell += sell * line_qty

                margin_pct = round((total_sell - total_cost) / total_sell * 100, 2) if total_sell else 0

                import json

                db.execute(
                    text("""
                        INSERT INTO quotes (
                            requisition_id, customer_site_id, quote_number, revision,
                            line_items, subtotal, total_cost, total_margin_pct,
                            payment_terms, shipping_terms, validity_days, notes,
                            status, sent_at, created_by_id, created_at, updated_at
                        ) VALUES (
                            :req_id, :site_id, :qnum, 1,
                            :lines, :subtotal, :cost, :margin,
                            :pay, :ship, :validity, :notes,
                            :status, :sent, :user_id, :created, :updated
                        )
                    """),
                    {
                        "req_id": req_id,
                        "site_id": customer_site_id,
                        "qnum": q_num,
                        "lines": json.dumps(line_items_json),
                        "subtotal": round(total_sell, 2),
                        "cost": round(total_cost, 2),
                        "margin": margin_pct,
                        "pay": "Net 30",
                        "ship": "FOB Origin",
                        "validity": 14,
                        "notes": f"{q_label} — demo quote",
                        "status": q_status,
                        "sent": (now - timedelta(days=1)) if q_status == "sent" else None,
                        "user_id": user_id,
                        "created": now - timedelta(days=2),
                        "updated": now - timedelta(hours=6),
                    },
                )
                quote_count += 1

            logger.info(f"Created {quote_count} quotes")
        else:
            logger.warning("No customer_site_id on requisition — skipping quotes")

        # ── 4. ACTIVITY (RFQ sends, email replies, calls, notes) ──
        activities = [
            {
                "type": "rfq_sent",
                "channel": "email",
                "subject": "RFQ sent to Arrow Electronics — 6 parts",
                "contact": "sales@arrow.com",
                "name": "Arrow Sales Desk",
                "days_ago": 5,
            },
            {
                "type": "rfq_sent",
                "channel": "email",
                "subject": "RFQ sent to Digi-Key — LM317T, STM32F407",
                "contact": "rfq@digikey.com",
                "name": "Digi-Key RFQ Team",
                "days_ago": 5,
            },
            {
                "type": "reply_received",
                "channel": "email",
                "subject": "RE: RFQ — Arrow quote attached",
                "contact": "john.smith@arrow.com",
                "name": "John Smith",
                "days_ago": 3,
            },
            {
                "type": "reply_received",
                "channel": "email",
                "subject": "RE: RFQ — Digi-Key pricing for LM317T",
                "contact": "pricing@digikey.com",
                "name": "Digi-Key Pricing",
                "days_ago": 2,
            },
            {
                "type": "call",
                "channel": "phone",
                "subject": "Called Mouser re: STM32F407 lead time",
                "contact": "+1-800-346-6873",
                "name": "Mouser Rep",
                "days_ago": 2,
                "duration": 480,
            },
            {
                "type": "note",
                "channel": "manual",
                "subject": "Customer confirmed 2024+ date code requirement",
                "contact": None,
                "name": None,
                "days_ago": 4,
            },
            {
                "type": "rfq_sent",
                "channel": "email",
                "subject": "RFQ sent to Rochester — TPS65217CRSLR",
                "contact": "quotes@rocelec.com",
                "name": "Rochester Quotes",
                "days_ago": 3,
            },
            {
                "type": "reply_received",
                "channel": "email",
                "subject": "RE: Rochester quote — 6-8 week lead",
                "contact": "mike.jones@rocelec.com",
                "name": "Mike Jones",
                "days_ago": 1,
            },
            {
                "type": "call",
                "channel": "phone",
                "subject": "Customer call — discussed timeline and budget",
                "contact": "+1-555-0123",
                "name": "Customer Purchasing",
                "days_ago": 1,
                "duration": 1200,
            },
            {
                "type": "note",
                "channel": "manual",
                "subject": "Smith & Associates offered refurb LM317T at 15% below market",
                "contact": None,
                "name": None,
                "days_ago": 1,
            },
        ]

        for a in activities:
            db.execute(
                text("""
                    INSERT INTO activity_log (
                        user_id, activity_type, channel, requisition_id,
                        contact_email, contact_phone, contact_name,
                        subject, duration_seconds, notes, auto_logged, occurred_at, created_at,
                        direction, event_type
                    ) VALUES (
                        :uid, :type, :channel, :req_id,
                        :email, :phone, :name,
                        :subject, :duration, :notes, :auto, :occurred, :created,
                        :direction, :event_type
                    )
                """),
                {
                    "uid": user_id,
                    "type": a["type"],
                    "channel": a["channel"],
                    "req_id": req_id,
                    "email": a["contact"] if a["channel"] == "email" else None,
                    "phone": a["contact"] if a["channel"] == "phone" else None,
                    "name": a.get("name"),
                    "subject": a["subject"],
                    "duration": a.get("duration"),
                    "notes": a["subject"],
                    "auto": a["channel"] == "email",
                    "occurred": now - timedelta(days=a["days_ago"]),
                    "created": now - timedelta(days=a["days_ago"]),
                    "direction": "outbound" if "sent" in a["type"] or a["type"] == "call" else "inbound",
                    "event_type": "email"
                    if a["channel"] == "email"
                    else ("call" if a["channel"] == "phone" else "note"),
                },
            )

        logger.info(f"Created {len(activities)} activity entries")

        # ── 5. TASKS ──
        tasks = [
            {
                "title": "Follow up with Arrow on pricing",
                "desc": "Arrow quoted high. Negotiate volume discount.",
                "type": "sourcing",
                "pri": 3,
                "status": "in_progress",
                "due_days": 2,
            },
            {
                "title": "Send RFQs for remaining unsourced parts",
                "desc": "3 parts still need vendor quotes.",
                "type": "sourcing",
                "pri": 2,
                "status": "todo",
                "due_days": 1,
            },
            {
                "title": "Review Rochester substitute offer",
                "desc": "Verify pin compatibility with engineering.",
                "type": "sourcing",
                "pri": 2,
                "status": "todo",
                "due_days": 3,
            },
            {
                "title": "Prepare customer quote for approval",
                "desc": "Build quote from selected offers.",
                "type": "sales",
                "pri": 3,
                "status": "todo",
                "due_days": 4,
            },
            {
                "title": "Schedule customer call",
                "desc": "Discuss timeline and shipping requirements.",
                "type": "sales",
                "pri": 1,
                "status": "todo",
                "due_days": 7,
            },
            {
                "title": "Verify date codes meet spec",
                "desc": "Customer requires DC 2024+. Check with vendors.",
                "type": "sourcing",
                "pri": 2,
                "status": "done",
                "due_days": -1,
            },
            {
                "title": "Update CRM with bid due date",
                "desc": "Customer mentioned Friday deadline.",
                "type": "sales",
                "pri": 1,
                "status": "done",
                "due_days": -2,
            },
        ]

        for t in tasks:
            db.execute(
                text("""
                    INSERT INTO requisition_tasks (
                        requisition_id, title, description, task_type, priority,
                        status, assigned_to_id, due_at, source, created_at
                    ) VALUES (
                        :req_id, :title, :desc, :type, :pri,
                        :status, :uid, :due, 'manual', :created
                    )
                """),
                {
                    "req_id": req_id,
                    "title": t["title"],
                    "desc": t["desc"],
                    "type": t["type"],
                    "pri": t["pri"],
                    "status": t["status"],
                    "uid": user_id,
                    "due": now + timedelta(days=t["due_days"]),
                    "created": now - timedelta(days=1),
                },
            )

        logger.info(f"Created {len(tasks)} tasks")

        db.commit()
        logger.success(
            f"Demo data populated for requisition {req_id}: "
            f"{sighting_count} sightings, {offer_count} offers, "
            f"quotes, {len(activities)} activities, {len(tasks)} tasks"
        )

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create demo data: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Populate demo data for a requisition")
    parser.add_argument("--req-id", type=int, required=True, help="Requisition ID to populate")
    args = parser.parse_args()
    simulate(args.req_id)
