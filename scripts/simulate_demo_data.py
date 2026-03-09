"""
Populate demo offers and tasks for a test requisition.
Simulates a full sourcing workflow so the new UI can be demoed.
Run from Docker: docker compose exec app python scripts/simulate_demo_data.py --req-id 23433
Depends on: app.models, app.database
Called by: manual execution for demo purposes
"""
import argparse
import sys
import os
from datetime import datetime, timedelta, timezone

# Ensure the app module is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.database import SessionLocal
from loguru import logger


def simulate(req_id: int):
    db = SessionLocal()
    try:
        # Get requirements for this requisition
        rows = db.execute(
            text("SELECT id, primary_mpn, target_qty, target_price FROM requirements WHERE requisition_id = :rid"),
            {"rid": req_id},
        ).fetchall()
        if not rows:
            logger.error(f"No requirements found for requisition {req_id}")
            return

        logger.info(f"Found {len(rows)} requirements for requisition {req_id}")

        # Get a user_id for the demo
        user_row = db.execute(text("SELECT id, display_name FROM users LIMIT 1")).fetchone()
        if not user_row:
            logger.error("No users found in database")
            return
        user_id = user_row[0]
        user_name = user_row[1] or "Demo User"

        now = datetime.now(timezone.utc)

        # ── Create demo offers for each requirement ──
        offer_count = 0
        demo_vendors = [
            {"name": "Arrow Electronics", "condition": "new", "lead": "2-3 weeks", "sub": False},
            {"name": "Digi-Key", "condition": "new", "lead": "In Stock", "sub": False},
            {"name": "Mouser Electronics", "condition": "new", "lead": "4 weeks", "sub": False},
            {"name": "Rochester Electronics", "condition": "new", "lead": "6-8 weeks", "sub": True},
            {"name": "Smith & Associates", "condition": "refurbished", "lead": "1 week", "sub": False},
        ]

        for req in rows:
            r_id, mpn, qty, price = req
            if not mpn:
                continue
            base_price = float(price) if price else 5.00

            for i, vendor in enumerate(demo_vendors[:3 + (r_id % 3)]):
                # Vary price around target
                multiplier = [0.85, 1.0, 1.15, 1.30, 0.95][i]
                offer_price = round(base_price * multiplier, 4)
                offer_qty = int((qty or 100) * [1.5, 1.0, 2.0, 0.5, 3.0][i])
                offered_mpn = mpn if not vendor["sub"] else mpn + "-ALT"
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
                        )
                    """),
                    {
                        "req_id": req_id, "r_id": r_id, "vendor": vendor["name"],
                        "mpn": mpn, "offered_mpn": offered_mpn,
                        "mfr": "Texas Instruments" if i % 2 == 0 else "STMicroelectronics",
                        "qty": offer_qty, "price": offer_price,
                        "lead": vendor["lead"], "cond": vendor["condition"],
                        "pkg": ["tube", "tray", "reel", "bulk", "cut tape"][i],
                        "source": ["rfq_response", "manual", "email_mining", "api_search", "manual"][i],
                        "status": status,
                        "notes": [
                            "Best price, confirmed stock",
                            "Standard distributor pricing",
                            "MOQ applies, volume discount available",
                            "Substitute part - pin compatible",
                            "Broker stock, inspect before use",
                        ][i],
                        "entered_by": user_name,
                        "created": now - timedelta(days=[1, 2, 3, 5, 7][i]),
                        "updated": now - timedelta(days=[0, 1, 2, 4, 6][i]),
                    },
                )
                offer_count += 1

        logger.info(f"Created {offer_count} demo offers")

        # ── Create demo tasks ──
        demo_tasks = [
            {
                "title": "Follow up with Arrow on LM317T pricing",
                "description": "Arrow quoted $4.25 but target is $0.50. Negotiate volume discount.",
                "task_type": "sourcing", "priority": 3, "status": "in_progress",
                "due": now + timedelta(days=2),
            },
            {
                "title": "Send RFQs for remaining unsourced parts",
                "description": "3 parts still need vendor quotes. Check BrokerBin and Sourcengine.",
                "task_type": "sourcing", "priority": 2, "status": "todo",
                "due": now + timedelta(days=1),
            },
            {
                "title": "Review substitute offers for QA-TEST-001",
                "description": "Rochester offered ALT part. Verify pin compatibility with engineering.",
                "task_type": "sourcing", "priority": 2, "status": "todo",
                "due": now + timedelta(days=3),
            },
            {
                "title": "Prepare customer quote for approval",
                "description": "Build quote from selected offers. Include lead time summary.",
                "task_type": "sales", "priority": 3, "status": "todo",
                "due": now + timedelta(days=4),
            },
            {
                "title": "Schedule call with customer purchasing team",
                "description": "Discuss timeline and shipping requirements for the order.",
                "task_type": "sales", "priority": 1, "status": "todo",
                "due": now + timedelta(days=7),
            },
            {
                "title": "Verify date codes meet customer spec",
                "description": "Customer requires DC 2024+ for all parts. Check with vendors.",
                "task_type": "sourcing", "priority": 2, "status": "done",
                "due": now - timedelta(days=1),
            },
            {
                "title": "Update CRM with bid due date",
                "description": "Customer mentioned Friday deadline. Update requisition.",
                "task_type": "sales", "priority": 1, "status": "done",
                "due": now - timedelta(days=2),
            },
        ]

        for t in demo_tasks:
            db.execute(
                text("""
                    INSERT INTO requisition_tasks (
                        requisition_id, title, description, task_type, priority,
                        status, assigned_to_id, due_at, source, created_at
                    ) VALUES (
                        :req_id, :title, :desc, :type, :pri,
                        :status, :user_id, :due, :source, :created
                    )
                """),
                {
                    "req_id": req_id, "title": t["title"], "desc": t["description"],
                    "type": t["task_type"], "pri": t["priority"],
                    "status": t["status"], "user_id": user_id,
                    "due": t["due"], "source": "manual",
                    "created": now - timedelta(days=1),
                },
            )

        logger.info(f"Created {len(demo_tasks)} demo tasks")

        db.commit()
        logger.success(f"Demo data populated for requisition {req_id}")

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
